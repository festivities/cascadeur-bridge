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


def shot_frames(action, slot, start, end):
    """Keyed frames inside the scene range. The range ends are always included."""
    start, end = int(start), int(end)
    if end < start:
        start, end = end, start
    frames = [frame for frame in (keyed_frames(action, slot) if action else []) if start <= frame <= end]
    if start not in frames:
        frames.insert(0, start)
    if end not in frames:
        frames.append(end)
    return frames


def active_armature(context):
    """The selected active armature. Other rigs in the scene are ignored."""
    obj = context.active_object
    if obj is None or obj.type != "ARMATURE" or not obj.select_get():
        return None
    return obj


def overwrite_action(context):
    return bool(getattr(context.scene, "cbb_overwrite_action", False))


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
    # Library overrides cannot enter edit mode. Pose bones already store rest.
    rest = {}
    for bone in armature.data.bones:
        if not bone.use_deform:
            continue
        head = bone.head_local.copy()
        tail = bone.tail_local.copy()
        axis = tail - head
        if axis.length == 0.0:
            roll = 0.0
        else:
            _axis, roll = bone.AxisRollFromMatrix(bone.matrix_local.to_3x3(), axis=axis)
        rest[bone.name] = (head, tail, roll)
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
    # Keep the temp rig out of a linked or override collection.
    bpy.context.scene.collection.objects.link(obj)
    obj.matrix_world = source.matrix_world.copy()

    previous_active = bpy.context.view_layer.objects.active
    previous_selected = [item for item in bpy.context.selected_objects]
    previous_mode = previous_active.mode if previous_active else "OBJECT"
    # mode_set edits every selected armature. An override cannot enter edit mode.
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        for bone_name, (head, tail, roll) in rest.items():
            edit_bone = arm_data.edit_bones.new(bone_name)
            edit_bone.head = head
            edit_bone.tail = tail
            edit_bone.roll = roll
        for bone_name, parent_name in parents.items():
            if parent_name and parent_name in arm_data.edit_bones:
                arm_data.edit_bones[bone_name].parent = arm_data.edit_bones[parent_name]
        bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        if obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        for item in previous_selected:
            if item.name in bpy.data.objects:
                item.select_set(True)
        if previous_active and previous_active.name in bpy.data.objects:
            bpy.context.view_layer.objects.active = previous_active
            if previous_mode not in {"OBJECT", "EDIT"} and previous_active.mode != previous_mode:
                bpy.ops.object.mode_set(mode=previous_mode)
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
        # Solve the basis from the source poses directly. Assigning pose_bone.matrix
        # would solve each child against its parent's stale, last-evaluated pose.
        for name in shared:
            bone = target.data.bones[name]
            pose = source.pose.bones[name].matrix
            if bone.parent:
                parent_pose = source.pose.bones[bone.parent.name].matrix
                basis = bone.convert_local_to_pose(
                    pose, bone.matrix_local, parent_matrix=parent_pose,
                    parent_matrix_local=bone.parent.matrix_local, invert=True,
                )
            else:
                basis = bone.convert_local_to_pose(pose, bone.matrix_local, invert=True)
            target.pose.bones[name].matrix_basis = basis
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


def _clear_range(action, slot, bone_names, start, end):
    """Drop keys on these bones. A start/end keeps keys outside that span."""
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
                if not fcurve.data_path.startswith(prefixes):
                    continue
                if start is None:
                    bag.fcurves.remove(fcurve)
                    continue
                # Back to front: removing a key shifts the ones after it.
                points = fcurve.keyframe_points
                for index in reversed(range(len(points))):
                    if start - 0.001 <= points[index].co[0] <= end + 0.001:
                        points.remove(points[index])
                if len(fcurve.keyframe_points) == 0:
                    bag.fcurves.remove(fcurve)


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
    scene = bpy.context.scene
    frames = shot_frames(action, slot, scene.frame_start, scene.frame_end)
    if scene.frame_end <= scene.frame_start or len(frames) < 2:
        return "The frame range needs at least two frames."

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


def _is_control(armature, bone):
    """Animator-facing bone: in a visible bone collection. Rigs without collections count all."""
    if not armature.data.collections_all:
        return True
    return any(collection.is_visible_effectively for collection in bone.collections)


def _ik_chain_bones(armature):
    chain = set()
    for pose_bone in armature.pose.bones:
        for constraint in pose_bone.constraints:
            if constraint.type != "IK" or constraint.mute or constraint.influence <= 0.5:
                continue
            node, count = pose_bone, constraint.chain_count or 1000
            while node and count > 0:
                chain.add(node.name)
                node, count = node.parent, count - 1
    return chain


def _active_copy(pose_bone):
    # Later constraints win; an IK/FK switch puts the IK copy after the FK one.
    active = None
    for constraint in pose_bone.constraints:
        if constraint.type == "COPY_TRANSFORMS" and not constraint.mute and constraint.influence > 0.5:
            active = constraint.subtarget
    return active


def _driven(pose_bone):
    return any(
        not item.type.startswith("LIMIT_") and not item.mute and item.influence > 0.0
        for item in pose_bone.constraints
    )


def _control_for(armature, def_name, ik_bones):
    """The control the animator keys to move this DEF bone, and how many hops away it is.

    None when the bone follows an IK solve (thigh/shin; the foot DEF reaches the IK
    control), or when it is a tweak riding on a mechanism chain (twist, face, hair):
    those follow the solved controls already.
    """
    bones = armature.data.bones
    node = armature.pose.bones[def_name].parent
    hops = 1
    while node:
        target = _active_copy(node)
        if target and target in armature.pose.bones:
            if target in ik_bones:
                return None, hops
            control = armature.pose.bones[target]
            if not _is_control(armature, bones[target]):
                # Hidden mechanism (IK-M-foot): climb to the free visible control (IK-foot).
                # ponytail: a hidden FK chain would climb too far; CloudRig shows the active chain.
                climb = control.parent
                while climb and climb.parent:
                    if _is_control(armature, bones[climb.name]) and not _driven(climb):
                        control = climb
                        break
                    climb = climb.parent
            if control.name in ik_bones:
                return None, hops
            return control.name, hops
        # A top-level bone (root) moves the whole rig; never solve onto it.
        if node.parent and _is_control(armature, bones[node.name]):
            rides_mechanism = any(_active_copy(parent) for parent in node.parent_recursive)
            return (None if rides_mechanism else node.name), hops
        node, hops = node.parent, hops + 1
    return None, hops


def _control_targets(source, imported):
    ik_bones = _ik_chain_bones(source)
    best = {}
    for bone in source.data.bones:
        if not bone.use_deform:
            continue
        imported_name = _imported_name(imported, bone.name)
        if imported_name is None:
            continue
        control, hops = _control_for(source, bone.name, ik_bones)
        if control and (control not in best or hops < best[control][0]):
            best[control] = (hops, bone.name, imported_name)
    return {control: (name, imported_name) for control, (_hops, name, imported_name) in best.items()}


def _imported_name(imported, def_name):
    if def_name in imported.pose.bones:
        return def_name
    stripped = def_name[4:] if def_name.startswith("DEF-") else None
    if stripped and stripped in imported.pose.bones:
        return stripped
    return None


def pose_frame_pairs(imported_action, start, end):
    """Map an imported clip onto the scene range. FBX import often starts one frame later."""
    start, end = int(start), int(end)
    if imported_action is None:
        return [(frame, frame) for frame in range(start, end + 1)]
    imported_start = int(round(imported_action.frame_range[0]))
    imported_end = int(round(imported_action.frame_range[1]))
    shift = 1 if imported_start == start + 1 else 0
    pairs = []
    for frame in range(imported_start, imported_end + 1):
        source = frame - shift
        if start <= source <= end:
            pairs.append((source, frame))
    return pairs


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


def _key_compatible(pose_bone, frame, previous):
    """Key the posed control without Euler or quaternion flips between frames."""
    if pose_bone.rotation_mode == "QUATERNION":
        if previous is not None:
            quat = pose_bone.rotation_quaternion.copy()
            quat.make_compatible(previous)
            pose_bone.rotation_quaternion = quat
        current = pose_bone.rotation_quaternion.copy()
    elif pose_bone.rotation_mode == "AXIS_ANGLE":
        current = None
    else:
        current = pose_bone.matrix_basis.to_euler(pose_bone.rotation_mode, previous)
        pose_bone.rotation_euler = current
    insert_visual_key(pose_bone, frame)
    return current


def apply_control_poses(source, imported, frames, action_name=None, overwrite=False):
    """Pose the driving controls so deform bones match the imported skeleton.

    Solved in world space per frame: control = desired_def @ live_def^-1 @ live_control,
    so FK, hips and IK foot controls all land exactly. Deform bones are not keyed.
    IK knees follow the existing pole. shortcut: three passes per frame with a view-layer
    update per control; batch updates per hierarchy depth if long shots hitch.
    """
    if frames and not isinstance(frames[0], tuple):
        frames = [(frame, frame) for frame in frames]
    if not frames:
        return 0

    imported.matrix_world = source.matrix_world.copy()
    targets = _control_targets(source, imported)
    if not targets:
        return 0

    animation = source.animation_data or source.animation_data_create()
    if overwrite and animation.action is not None:
        action = animation.action
    else:
        action = _ensure_action(source, action_name)
    slot = source.animation_data.action_slot
    span = [frame for frame, _imported in frames]
    _clear_range(action, slot, list(targets), min(span), max(span))

    scene = bpy.context.scene
    order = sorted(targets, key=lambda name: len(source.pose.bones[name].parent_recursive))
    to_local = source.matrix_world.inverted()
    previous = {}
    for source_frame, imported_frame in frames:
        scene.frame_set(imported_frame)
        bpy.context.view_layer.update()
        desired = {
            control: to_local @ imported.matrix_world @ imported.pose.bones[imported_name].matrix
            for control, (_def_name, imported_name) in targets.items()
        }
        scene.frame_set(source_frame)
        bpy.context.view_layer.update()
        solved = {}
        for control, (def_name, _imported_name) in targets.items():
            live_def = source.pose.bones[def_name].matrix
            solved[control] = desired[control] @ live_def.inverted() @ source.pose.bones[control].matrix
        # Pass 1 poses parent-first. Later passes correct for parents that hang off an
        # IK solve and for the control's own constraints (FK-toe's Armature constraint).
        placed = dict(solved)
        for _pass in range(3):
            for control in order:
                pose_bone = source.pose.bones[control]
                pose_bone.matrix = placed[control]
                bpy.context.view_layer.update()
                placed[control] = solved[control] @ pose_bone.matrix.inverted() @ placed[control]
        for control in order:
            previous[control] = _key_compatible(source.pose.bones[control], source_frame, previous.get(control))
    return len(targets)
