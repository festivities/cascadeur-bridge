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

    action = bpy.data.actions.new("CSC_TEST_KEYS")
    rig.animation_data_create()
    rig.animation_data.action = action
    pose = rig.pose.bones["DEF-spine"]
    pose.keyframe_insert("location", frame=1)
    pose.keyframe_insert("location", frame=11)
    slot = rig.animation_data.action_slot
    frames = tween.keyed_frames(action, slot)
    assert frames == [1, 11], frames

    for obj in (plain, rig):
        bpy.data.objects.remove(obj, do_unlink=True)
    return "ok"
