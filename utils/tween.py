import bpy


def deform_parent_map(armature):
    """Parent each deform bone to the deform bone that actually drives it.

    CloudRig parents DEF bones to controls, not to each other. A plain
    deform armature already has the hierarchy we want.
    """
    bones = armature.data.bones
    control_to_def = {}
    for bone in bones:
        if bone.use_deform and bone.parent and not bone.parent.use_deform:
            control_to_def.setdefault(bone.parent.name, []).append(bone.name)

    parents = {}
    for bone in bones:
        if not bone.use_deform:
            continue
        if bone.parent and bone.parent.use_deform:
            parents[bone.name] = bone.parent.name
            continue
        node = bone.parent.parent if bone.parent else None
        parent = None
        while node:
            preferred = "DEF-" + node.name
            if preferred in control_to_def.get(node.name, []):
                parent = preferred
                break
            node = node.parent
        parents[bone.name] = parent
    return parents


def action_fcurves(action, slot):
    curves = []
    if slot is not None and getattr(action, "layers", None):
        for layer in action.layers:
            for strip in getattr(layer, "strips", []):
                bag = None
                try:
                    bag = strip.channelbag(slot)
                except Exception:
                    bag = None
                if bag is not None:
                    curves.extend(bag.fcurves)
        if curves:
            return curves
    fcurves = getattr(action, "fcurves", None)
    if fcurves:
        curves.extend(fcurves)
    return curves


def keyed_frames(action, slot):
    frames = set()
    for fcurve in action_fcurves(action, slot):
        if not fcurve.data_path.startswith("pose.bones"):
            continue
        for key in fcurve.keyframe_points:
            frames.add(int(round(key.co[0])))
    return sorted(frames)


def blocking_action(armature):
    animation = armature.animation_data
    if animation is None or animation.action is None:
        return None, None
    return animation.action, animation.action_slot


def insert_visual_key(pose_bone, frame):
    pose_bone.keyframe_insert("location", frame=frame)
    if pose_bone.rotation_mode == "QUATERNION":
        pose_bone.keyframe_insert("rotation_quaternion", frame=frame)
    elif pose_bone.rotation_mode == "AXIS_ANGLE":
        pose_bone.keyframe_insert("rotation_axis_angle", frame=frame)
    else:
        pose_bone.keyframe_insert("rotation_euler", frame=frame)
    pose_bone.keyframe_insert("scale", frame=frame)


def _bone_rest(armature):
    previous_active = bpy.context.view_layer.objects.active
    previous_mode = previous_active.mode if previous_active else "OBJECT"
    bpy.context.view_layer.objects.active = armature
    if armature.mode != "EDIT":
        bpy.ops.object.mode_set(mode="EDIT")
    rest = {
        bone.name: (bone.head.copy(), bone.tail.copy(), bone.roll)
        for bone in armature.data.edit_bones
        if bone.use_deform
    }
    bpy.ops.object.mode_set(mode="OBJECT")
    if previous_active:
        bpy.context.view_layer.objects.active = previous_active
        if previous_mode != "OBJECT":
            bpy.ops.object.mode_set(mode=previous_mode)
    return rest


def build_deform_armature(source, name):
    """Armature of deform bones in their real hierarchy, unlinked from controls."""
    parents = deform_parent_map(source)
    rest = _bone_rest(source)
    if name in bpy.data.objects:
        bpy.data.objects.remove(bpy.data.objects[name], do_unlink=True)
    if name in bpy.data.armatures:
        bpy.data.armatures.remove(bpy.data.armatures[name])

    arm_data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, arm_data)
    collection = source.users_collection[0] if source.users_collection else bpy.context.scene.collection
    collection.objects.link(obj)
    obj.matrix_world = source.matrix_world.copy()

    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    for bone_name, (head, tail, roll) in rest.items():
        edit_bone = arm_data.edit_bones.new(bone_name)
        edit_bone.head = head
        edit_bone.tail = tail
        edit_bone.roll = roll
    for bone_name, parent_name in parents.items():
        if parent_name:
            arm_data.edit_bones[bone_name].parent = arm_data.edit_bones[parent_name]
    bpy.ops.object.mode_set(mode="OBJECT")
    return obj


def key_blocking_poses(source, target, frames):
    """Key target deform bones to the evaluated source pose on each blocking frame."""
    action = bpy.data.actions.new(target.name + "_block")
    action.use_fake_user = True
    target.animation_data_create()
    target.animation_data.action = action
    if hasattr(action, "slots") and action.slots and target.animation_data.action_slot is None:
        target.animation_data.action_slot = action.slots[0]

    shared = [name for name in deform_parent_map(source) if name in target.pose.bones]
    for frame in frames:
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        for name in shared:
            target.pose.bones[name].matrix = source.pose.bones[name].matrix.copy()
            insert_visual_key(target.pose.bones[name], frame)
    return action


def copy_action(armature, action, slot):
    new_action = action.copy()
    new_action.use_fake_user = True
    new_action.name = action.name + "_csc_tween"
    animation = armature.animation_data or armature.animation_data_create()
    animation.action = new_action
    if slot is not None and getattr(new_action, "slots", None):
        match = None
        for candidate in new_action.slots:
            if candidate.identifier == slot.identifier:
                match = candidate
                break
        animation.action_slot = match or new_action.slots[0]
    return new_action


def frame_pairs(imported_action, our_start, our_end):
    """Map our frames to the imported action. FBX import often shifts by one frame."""
    our_count = our_end - our_start + 1
    if imported_action is None:
        return [(frame, frame) for frame in range(our_start, our_end + 1)]
    imported_start = int(round(imported_action.frame_range[0]))
    imported_end = int(round(imported_action.frame_range[1]))
    imported_count = max(imported_end - imported_start + 1, 1)
    count = min(our_count, imported_count)
    return [(our_start + index, imported_start + index) for index in range(count)]


def _local_quat(pose_bone):
    if pose_bone.rotation_mode == "QUATERNION":
        return pose_bone.rotation_quaternion.copy()
    return pose_bone.rotation_euler.to_quaternion()


def _set_local_quat(pose_bone, quat, previous_euler=None):
    if pose_bone.rotation_mode == "QUATERNION":
        pose_bone.rotation_quaternion = quat
        return None
    euler = quat.to_euler(pose_bone.rotation_mode, previous_euler)
    pose_bone.rotation_euler = euler
    return euler


def _keyed_controls(armature, action, slot):
    names = set()
    for fcurve in action_fcurves(action, slot):
        if not fcurve.data_path.startswith('pose.bones["'):
            continue
        name = fcurve.data_path.split('"')[1]
        bone = armature.pose.bones.get(name)
        if bone and not bone.bone.use_deform:
            names.add(name)
    return names


def _deform_name(control_name):
    if "-" in control_name and control_name.split("-", 1)[0] in {
        "FK",
        "IK",
        "CTR",
        "TGT",
        "POLE",
    }:
        return "DEF-" + control_name.split("-", 1)[1]
    return "DEF-" + control_name


def _driver_control(armature, def_name, controls):
    pose_bone = armature.pose.bones.get(def_name)
    node = pose_bone.parent if pose_bone else None
    while node:
        for constraint in node.constraints:
            if (
                constraint.type == "COPY_TRANSFORMS"
                and getattr(constraint, "influence", 1.0) > 0.5
                and constraint.subtarget in controls
            ):
                return constraint.subtarget
        node = node.parent
    stripped = def_name[4:] if def_name.startswith("DEF-") else def_name
    for prefix in ("FK-", "IK-", ""):
        candidate = prefix + stripped
        if candidate in controls:
            return candidate
    return None


def _remap_quats(samples, dst_start, dst_end):
    src_start = samples[0].copy()
    src_end = samples[-1].copy()
    left = dst_start @ src_start.inverted()
    right = dst_end @ src_end.inverted()
    last = max(len(samples) - 1, 1)
    return [left.slerp(right, index / last) @ quat for index, quat in enumerate(samples)]


def _remove_fcurves(action, slot, bone_names):
    prefixes = tuple(f'pose.bones["{name}"]' for name in bone_names)
    if not prefixes or slot is None or not getattr(action, "layers", None):
        return
    for layer in action.layers:
        for strip in getattr(layer, "strips", []):
            bag = None
            try:
                bag = strip.channelbag(slot)
            except Exception:
                bag = None
            if bag is None:
                continue
            for fcurve in list(bag.fcurves):
                if fcurve.data_path.startswith(prefixes):
                    bag.fcurves.remove(fcurve)


def apply_control_tween(source, imported, frames, key_frames=None):
    """Write Cascadeur's in-betweens onto the controls that drive the rig.

    Deform bones are left alone so they keep following those controls.
    Blocking-key rotations are kept. shortcut: keyframe_insert per control per
    frame, switch to fcurve insert if long actions hitch.
    """
    if frames and not isinstance(frames[0], tuple):
        frames = [(frame, frame) for frame in frames]
    pair_by_source = dict(frames)
    keys = [frame for frame in sorted(key_frames or []) if frame in pair_by_source]
    if len(frames) < 2 or not keys:
        return 0

    animation = source.animation_data
    action = animation.action if animation else None
    slot = animation.action_slot if animation else None
    controls = _keyed_controls(source, action, slot) if action else set()
    driven = {}
    for name in list(imported.pose.bones.keys()):
        if name not in source.pose.bones or not source.pose.bones[name].bone.use_deform:
            continue
        control = _driver_control(source, name, controls) or _driver_control(
            source, name, set(source.pose.bones.keys())
        )
        preferred = _deform_name(control) if control else None
        if control and (control not in driven or name == preferred):
            driven[control] = name
    if not driven:
        return 0

    scene = bpy.context.scene
    imported_samples = {control: [] for control in driven}
    for _source_frame, imported_frame in frames:
        scene.frame_set(imported_frame)
        bpy.context.view_layer.update()
        for control, def_name in driven.items():
            imported_samples[control].append(
                imported.pose.bones[def_name].matrix_basis.to_quaternion()
            )

    keyed_rotation = {}
    for frame in keys:
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        for control in driven:
            keyed_rotation[control, frame] = _local_quat(source.pose.bones[control])

    frame_index = {frame: index for index, (frame, _imported) in enumerate(frames)}
    written = 0
    for control, def_name in driven.items():
        samples = imported_samples[control]
        remapped = [None] * len(samples)
        if control in controls:
            intervals = list(zip(keys, keys[1:]))
        else:
            rest = _local_quat(source.pose.bones[control])
            keyed_rotation[control, keys[0]] = rest
            keyed_rotation[control, keys[-1]] = rest
            intervals = [(keys[0], keys[-1])]
        for left, right in intervals:
            start = frame_index[left]
            end = frame_index[right]
            chunk = _remap_quats(
                samples[start : end + 1],
                keyed_rotation[control, left],
                keyed_rotation[control, right],
            )
            remapped[start : end + 1] = chunk
        pose_bone = source.pose.bones[control]
        previous = None
        for (source_frame, _imported_frame), quat in zip(frames, remapped):
            if quat is None:
                continue
            if source_frame in keyed_rotation and control in controls:
                quat = keyed_rotation[control, source_frame]
            scene.frame_set(source_frame)
            previous = _set_local_quat(pose_bone, quat, previous)
            if pose_bone.rotation_mode == "QUATERNION":
                pose_bone.keyframe_insert("rotation_quaternion", frame=source_frame)
            else:
                pose_bone.keyframe_insert("rotation_euler", frame=source_frame)
        written += 1

    if action is not None:
        _remove_fcurves(
            action,
            slot,
            [bone.name for bone in source.data.bones if bone.use_deform],
        )
    return written


def is_control_rig(armature):
    """Deform bones hang off controls, as in CloudRig. A plain skeleton does not."""
    return any(
        bone.use_deform and bone.parent and not bone.parent.use_deform
        for bone in armature.data.bones
    )


def _set_constant(action, slot):
    for fcurve in action_fcurves(action, slot):
        for key in fcurve.keyframe_points:
            key.interpolation = "CONSTANT"


def _restore_view(previous_active, previous_selected, previous_mode):
    bpy.ops.object.select_all(action="DESELECT")
    for obj in previous_selected:
        if obj.name in bpy.data.objects:
            obj.select_set(True)
    if previous_active and previous_active.name in bpy.data.objects:
        bpy.context.view_layer.objects.active = previous_active
        if previous_mode != "OBJECT" and previous_active.mode != previous_mode:
            bpy.ops.object.mode_set(mode=previous_mode)


def export_blocked_deform(armature, path):
    """Write a deform-only FBX of the keyed poses. Holds stay holds.

    The live rig is not modified. shortcut: FBX bake plus simplify, because
    bake_anim=False writes no curves. Raise the simplify factor only if a
    long hold still comes back dense.
    """
    action, slot = blocking_action(armature)
    frames = keyed_frames(action, slot) if action else []
    if len(frames) < 2:
        return "The action needs at least two keyed frames."

    scene = bpy.context.scene
    previous_range = (scene.frame_start, scene.frame_end, scene.frame_current)
    previous_active = bpy.context.view_layer.objects.active
    previous_selected = list(bpy.context.selected_objects)
    previous_mode = previous_active.mode if previous_active else "OBJECT"
    deform = build_deform_armature(armature, armature.name + "_csc_export")
    exported = deform.animation_data.action if deform.animation_data else None
    try:
        exported = key_blocking_poses(armature, deform, frames)
        _set_constant(exported, deform.animation_data.action_slot)
        bpy.ops.object.select_all(action="DESELECT")
        deform.select_set(True)
        bpy.context.view_layer.objects.active = deform
        if deform.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        scene.frame_start = frames[0]
        scene.frame_end = frames[-1]
        bpy.ops.export_scene.fbx(
            filepath=path,
            use_selection=True,
            object_types={"ARMATURE"},
            add_leaf_bones=False,
            bake_anim=True,
            bake_anim_use_all_bones=False,
            bake_anim_use_nla_strips=False,
            bake_anim_use_all_actions=False,
            bake_anim_force_startend_keying=True,
            bake_anim_step=1,
            bake_anim_simplify_factor=0.01,
            axis_forward="-Z",
            axis_up="Y",
        )
    except Exception as error:
        return str(error)
    finally:
        scene.frame_start, scene.frame_end, scene.frame_current = previous_range
        if deform.name in bpy.data.objects:
            bpy.data.objects.remove(deform, do_unlink=True)
        if exported is not None:
            exported.use_fake_user = False
            if exported.users == 0:
                bpy.data.actions.remove(exported)
        _restore_view(previous_active, previous_selected, previous_mode)
    return None


def _mechanism_driver(armature, def_name):
    pose_bone = armature.pose.bones.get(def_name)
    node = pose_bone.parent if pose_bone else None
    while node:
        for constraint in node.constraints:
            if (
                constraint.type == "COPY_TRANSFORMS"
                and getattr(constraint, "influence", 1.0) > 0.5
                and constraint.subtarget in armature.pose.bones
            ):
                return node.name, constraint.subtarget
        node = node.parent
    return None, None


def _imported_name(imported, def_name):
    if def_name in imported.pose.bones:
        return def_name
    stripped = def_name[4:] if def_name.startswith("DEF-") else None
    if stripped and stripped in imported.pose.bones:
        return stripped
    return None


def _control_targets(source, imported):
    driven = {}
    for name in source.pose.bones.keys():
        if not source.pose.bones[name].bone.use_deform:
            continue
        imported_name = _imported_name(imported, name)
        if imported_name is None:
            continue
        mechanism, control = _mechanism_driver(source, name)
        if not control:
            continue
        preferred = _deform_name(control)
        if control not in driven or name == preferred:
            driven[control] = (mechanism, name, imported_name)
    return driven


def _parent_first(armature, names):
    depth = {}

    def _depth(name):
        if name in depth:
            return depth[name]
        parent = armature.pose.bones[name].parent
        depth[name] = 0 if parent is None or parent.name not in names else _depth(parent.name) + 1
        return depth[name]

    return sorted(names, key=_depth)


def pose_frame_pairs(imported_action, our_start):
    """Map an imported clip onto our frames. FBX import often starts one frame later."""
    if imported_action is None:
        return []
    start = int(round(imported_action.frame_range[0]))
    end = int(round(imported_action.frame_range[1]))
    shift = 1 if start == our_start + 1 else 0
    return [(frame - shift, frame) for frame in range(start, end + 1)]


def _ensure_action(armature, action_name):
    animation = armature.animation_data or armature.animation_data_create()
    if animation.action is not None:
        action = copy_action(armature, animation.action, animation.action_slot)
        if action_name:
            action.name = action_name
        return action
    action = bpy.data.actions.new(action_name or armature.name + "_csc")
    action.use_fake_user = True
    animation.action = action
    if getattr(action, "slots", None) and animation.action_slot is None:
        animation.action_slot = action.slots[0]
    return action


def apply_control_poses(source, imported, frames, action_name=None):
    """Pose the driving controls so deform bones match the imported skeleton.

    Deform bones are not keyed. Imported poses replace the old control keys,
    including the original blocking poses. shortcut: update per control per
    frame so a child solves against its posed parent.
    """
    if frames and not isinstance(frames[0], tuple):
        frames = [(frame, frame) for frame in frames]
    if not frames:
        return 0

    imported.matrix_world = source.matrix_world.copy()
    targets = _control_targets(source, imported)
    if not targets:
        return 0

    action = _ensure_action(source, action_name)
    slot = source.animation_data.action_slot
    _remove_fcurves(
        action,
        slot,
        list(targets) + [bone.name for bone in source.data.bones if bone.use_deform],
    )

    scene = bpy.context.scene
    order = _parent_first(source, set(targets))
    for source_frame, imported_frame in frames:
        scene.frame_set(imported_frame)
        bpy.context.view_layer.update()
        desired = {}
        for control, (mechanism, def_name, imported_name) in targets.items():
            world = imported.matrix_world @ imported.pose.bones[imported_name].matrix
            desired[control] = source.matrix_world.inverted() @ world
        scene.frame_set(source_frame)
        for control in order:
            mechanism, def_name, _imported_name = targets[control]
            def_bone = source.pose.bones[def_name].bone
            mech_bone = source.pose.bones[mechanism].bone
            source.pose.bones[control].matrix = (
                desired[control] @ def_bone.matrix_local.inverted() @ mech_bone.matrix_local
            )
            bpy.context.view_layer.update()
            insert_visual_key(source.pose.bones[control], source_frame)
    return len(targets)
