import importlib.util
from pathlib import Path

import bpy

_tween_path = Path(__file__).resolve().parents[1] / "utils" / "tween.py"
_spec = importlib.util.spec_from_file_location("cbb_tween", _tween_path)
tween = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tween)


def _armature(name, bones):
    arm_data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, arm_data)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    created = {}
    for bone_name, parent_name, head, tail, deform in bones:
        edit = arm_data.edit_bones.new(bone_name)
        edit.head = head
        edit.tail = tail
        edit.use_deform = deform
        if parent_name:
            edit.parent = created[parent_name]
        created[bone_name] = edit
    bpy.ops.object.mode_set(mode="OBJECT")
    return obj


def run_self_check():
    plain = _armature(
        "CSC_TEST_PLAIN",
        [
            ("hips", None, (0, 0, 0), (0, 0, 1), True),
            ("spine", "hips", (0, 0, 1), (0, 0, 2), True),
        ],
    )
    parents = tween.deform_parent_map(plain)
    assert parents["spine"] == "hips", parents
    assert parents["hips"] is None, parents
    rest = tween._bone_rest(plain)
    assert set(rest) == {"hips", "spine"}, rest

    rig = _armature(
        "CSC_TEST_CLOUDRIG",
        [
            ("spine", None, (0, 0, 0), (0, 0, 1), False),
            ("DEF-spine", "spine", (0, 0, 0), (0, 0, 1), True),
            ("upper_arm.L", "spine", (0.2, 0, 1), (0.6, 0, 1), False),
            ("DEF-upper_arm.L", "upper_arm.L", (0.2, 0, 1), (0.6, 0, 1), True),
        ],
    )
    parents = tween.deform_parent_map(rig)
    assert parents["DEF-upper_arm.L"] == "DEF-spine", parents
    assert tween.is_control_rig(rig)
    assert not tween.is_control_rig(plain)

    # Posed parent and child: the temp deform chain must reproduce both on the first key.
    rig.pose.bones["spine"].rotation_mode = "XYZ"
    rig.pose.bones["spine"].rotation_euler = (0.5, 0.0, 0.3)
    rig.pose.bones["upper_arm.L"].rotation_mode = "XYZ"
    rig.pose.bones["upper_arm.L"].rotation_euler = (0.0, 0.7, 0.0)
    bpy.context.view_layer.update()
    temp = tween.build_deform_armature(rig, "CSC_TEST_TEMP")
    tween.key_blocking_poses(rig, temp, [1])
    bpy.context.scene.frame_set(1)
    bpy.context.view_layer.update()
    for name in ("DEF-spine", "DEF-upper_arm.L"):
        delta = rig.pose.bones[name].matrix.inverted() @ temp.pose.bones[name].matrix
        off = max(abs(delta[i][j] - (i == j)) for i in range(4) for j in range(4))
        assert off < 1e-4, (name, delta)
    temp_action = temp.animation_data.action
    bpy.data.objects.remove(temp, do_unlink=True)
    temp_action.use_fake_user = False
    rig.pose.bones["spine"].rotation_euler = (0, 0, 0)
    rig.pose.bones["upper_arm.L"].rotation_euler = (0, 0, 0)

    driver = _armature(
        "CSC_TEST_DRIVER",
        [
            ("FK-bone", None, (0, 0, 0), (0, 0, 1), False),
            ("mech", None, (0, 0, 0), (0, 0, 1), False),
            ("DEF-bone", "mech", (0, 0, 0), (0, 0, 1), True),
        ],
    )
    copy = driver.pose.bones["mech"].constraints.new("COPY_TRANSFORMS")
    copy.target = driver
    copy.subtarget = "FK-bone"
    copy.owner_space = "WORLD"
    copy.target_space = "WORLD"
    fk = driver.pose.bones["FK-bone"]
    fk.rotation_mode = "XYZ"
    fk.rotation_euler = (0.4, 0.2, 0.0)
    bpy.context.view_layer.update()
    desired = driver.pose.bones["DEF-bone"].matrix.copy()
    imported = _armature(
        "CSC_TEST_IMPORT",
        [("DEF-bone", None, (0, 0, 0), (0, 0, 1), True)],
    )
    imported_bone = imported.pose.bones["DEF-bone"]
    imported_bone.matrix = desired
    tween.insert_visual_key(imported_bone, 1)
    fk.rotation_euler = (0, 0, 0)
    written = tween.apply_control_poses(driver, imported, [(1, 1)], "CSC_TEST_APPLIED")
    bpy.context.scene.frame_set(1)
    bpy.context.view_layer.update()
    got = driver.pose.bones["DEF-bone"].matrix
    loc = (got.inverted() @ desired).to_translation().length
    rot = got.to_quaternion().rotation_difference(desired.to_quaternion()).angle
    assert written == 1, written
    assert loc < 1e-4 and rot < 1e-4, (loc, rot)
    applied = driver.animation_data.action
    fk.keyframe_insert("rotation_euler", frame=10)
    tween.apply_control_poses(driver, imported, [(1, 1)], overwrite=True)
    assert driver.animation_data.action == applied
    outside = [
        key.co[0]
        for fcurve in tween.action_fcurves(applied, driver.animation_data.action_slot)
        if 'FK-bone' in fcurve.data_path
        for key in fcurve.keyframe_points
        if abs(key.co[0] - 10) < 0.01
    ]
    assert outside, "key outside the written range was removed"

    action = bpy.data.actions.new("CSC_TEST_KEYS")
    rig.animation_data_create()
    rig.animation_data.action = action
    pose = rig.pose.bones["DEF-spine"]
    pose.keyframe_insert("location", frame=1)
    pose.keyframe_insert("location", frame=11)
    slot = rig.animation_data.action_slot
    frames = tween.keyed_frames(action, slot)
    assert frames == [1, 11], frames
    scene = bpy.context.scene
    old_range = (scene.frame_start, scene.frame_end)
    scene.frame_start, scene.frame_end = 1, 20
    assert tween.shot_frames(action, slot, 1, 20) == [1, 11, 20]
    assert tween.shot_frames(action, slot, 5, 8) == [5, 8]
    scene.frame_start, scene.frame_end = old_range

    # IK/FK switch: the later, active copy wins; an IK-driven DEF is left to the IK solve.
    switch = _armature(
        "CSC_TEST_SWITCH",
        [
            ("root", None, (0, 0, 0), (0, 0, 0.2), False),
            ("FK-bone", "root", (0, 0, 0), (0, 0, 1), False),
            ("IK-M-bone", "root", (0, 0, 0), (0, 0, 1), False),
            ("IK-M-tip", "IK-M-bone", (0, 0, 1), (0, 0, 2), False),
            ("IK-target", "root", (0, 0, 2), (0, 0, 2.2), False),
            ("mech", "root", (0, 0, 0), (0, 0, 1), False),
            ("DEF-bone", "mech", (0, 0, 0), (0, 0, 1), True),
        ],
    )
    for subtarget in ("FK-bone", "IK-M-bone"):
        item = switch.pose.bones["mech"].constraints.new("COPY_TRANSFORMS")
        item.target, item.subtarget = switch, subtarget
    ik = switch.pose.bones["IK-M-tip"].constraints.new("IK")
    ik.target, ik.subtarget, ik.chain_count = switch, "IK-target", 2
    ik_bones = tween._ik_chain_bones(switch)
    assert tween._control_for(switch, "DEF-bone", ik_bones)[0] is None
    switch.pose.bones["mech"].constraints[1].influence = 0.0
    assert tween._control_for(switch, "DEF-bone", ik_bones)[0] == "FK-bone"

    for obj in (plain, rig, driver, imported, switch):
        bpy.data.objects.remove(obj, do_unlink=True)
    for action in list(bpy.data.actions):
        if action.name.startswith("CSC_TEST"):
            action.use_fake_user = False
            if action.users == 0:
                bpy.data.actions.remove(action)
    return "ok"
