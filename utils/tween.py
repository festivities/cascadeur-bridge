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


def _blend_matrix(left, right, factor):
    from mathutils import Matrix

    location = left.to_translation().lerp(right.to_translation(), factor)
    rotation = left.to_quaternion().slerp(right.to_quaternion(), factor)
    scale = left.to_scale().lerp(right.to_scale(), factor)
    return Matrix.LocRotScale(location, rotation, scale)


def _world_matrices(obj, names):
    bpy.context.view_layer.update()
    return {name: (obj.matrix_world @ obj.pose.bones[name].matrix).copy() for name in names}


def apply_world_poses(source, imported, frames, key_frames=None):
    """Key source deform bones so the visual pose matches the imported tween.

    frames is a list of source frames, or (source_frame, imported_frame) pairs.
    key_frames stay on the original blocking pose. In-betweens follow Cascadeur's
    motion between those keys.
    shortcut: keyframe_insert per bone per frame, switch to fcurve insert if long actions hitch.
    """
    if frames and not isinstance(frames[0], tuple):
        frames = [(frame, frame) for frame in frames]
    shared = [
        name
        for name in source.pose.bones.keys()
        if source.pose.bones[name].bone.use_deform and name in imported.pose.bones
    ]
    scene = bpy.context.scene
    pair_by_source = dict(frames)
    keys = [frame for frame in sorted(key_frames or []) if frame in pair_by_source]
    source_keys = {}
    imported_keys = {}
    for frame in keys:
        scene.frame_set(frame)
        source_keys[frame] = _world_matrices(source, shared)
        scene.frame_set(pair_by_source[frame])
        imported_keys[frame] = _world_matrices(imported, shared)

    for source_frame, imported_frame in frames:
        scene.frame_set(imported_frame)
        imported_now = _world_matrices(imported, shared)
        scene.frame_set(source_frame)
        bpy.context.view_layer.update()
        left = right = factor = None
        if keys:
            left = max(frame for frame in keys if frame <= source_frame)
            right = min(frame for frame in keys if frame >= source_frame)
            factor = 0.0 if left == right else (source_frame - left) / (right - left)
        for name in shared:
            if keys:
                from_left = (
                    source_keys[left][name]
                    @ imported_keys[left][name].inverted()
                    @ imported_now[name]
                )
                from_right = (
                    source_keys[right][name]
                    @ imported_keys[right][name].inverted()
                    @ imported_now[name]
                )
                desired_world = _blend_matrix(from_left, from_right, factor)
            else:
                desired_world = imported_now[name]
            source.pose.bones[name].matrix = source.matrix_world.inverted() @ desired_world
            insert_visual_key(source.pose.bones[name], source_frame)
    return len(shared)
