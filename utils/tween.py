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
