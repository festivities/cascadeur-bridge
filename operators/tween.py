import bpy

from ..utils import file_handling, tween
from ..utils.csc_handling import CascadeurHandler
from .file_transfer import OperatorBaseClass, get_cascadeur_error


MAX_FRAMES = 2000


class CBB_OT_tween_blocked_animation(OperatorBaseClass):
    """Send the active action's key poses to Cascadeur, inbetween them, and bring the result back"""

    bl_idname = "cbb.tween_blocked_animation"
    bl_label = "Tween in Cascadeur"

    @classmethod
    def poll(cls, context):
        return tween.active_armature(context) is not None

    def execute(self, context):
        error = self._prepare(context)
        if error:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        return super().execute(context)

    def _prepare(self, context):
        armature = tween.active_armature(context)
        if armature is None:
            return "Make the rig you want the active object."
        if not tween.is_control_rig(armature):
            return "Tween writes onto the controls that drive deform bones. This rig has none."

        action, slot = tween.blocking_action(armature)
        if action is None:
            return "Assign the blocked action to the armature first."
        scene = context.scene
        if scene.frame_end - scene.frame_start > MAX_FRAMES:
            return f"Frame range is longer than {MAX_FRAMES} frames."
        frames = tween.shot_frames(action, slot, scene.frame_start, scene.frame_end)
        if len(frames) < 2:
            return "The frame range needs at least two frames."

        self.armature_name = armature.name
        self.action_name = action.name
        self.slot_identifier = slot.identifier if slot else None
        self.frames = frames
        self.export_start = scene.frame_start
        self.overwrite = tween.overwrite_action(context)
        self.file_path = file_handling.get_export_path("fbx")
        self.result_path = None
        self.skipped_gaps = []

        previous_active = context.view_layer.objects.active
        previous_selected = [obj for obj in context.selected_objects]
        previous_mode = previous_active.mode if previous_active else "OBJECT"
        deform = tween.build_deform_armature(armature, armature.name + "_csc_block")
        try:
            tween.key_blocking_poses(armature, deform, frames)
            bpy.ops.object.select_all(action="DESELECT")
            deform.select_set(True)
            context.view_layer.objects.active = deform
            self._export_deform(self.file_path)
        finally:
            if deform.name in bpy.data.objects:
                bpy.data.objects.remove(deform, do_unlink=True)

        bpy.ops.object.select_all(action="DESELECT")
        for obj in previous_selected:
            if obj.name in bpy.data.objects:
                obj.select_set(True)
        if previous_active and previous_active.name in bpy.data.objects:
            context.view_layer.objects.active = previous_active
            if previous_mode != "OBJECT" and previous_active.mode != previous_mode:
                bpy.ops.object.mode_set(mode=previous_mode)
        return None

    def _export_deform(self, path):
        bpy.ops.export_scene.fbx(
            filepath=path,
            use_selection=True,
            object_types={"ARMATURE"},
            add_leaf_bones=False,
            bake_anim=True,
            bake_anim_use_all_bones=True,
            bake_anim_use_nla_strips=False,
            bake_anim_use_all_actions=False,
            bake_anim_force_startend_keying=True,
            bake_anim_step=1,
            bake_anim_simplify_factor=0.0,
            axis_forward="-Z",
            axis_up="Y",
        )

    def on_execute(self, context):
        CascadeurHandler().execute_csc_command("scripts.blender_bridge.temp_tween")

    def on_connected(self, context):
        # Cascadeur frames are relative to the exported range start.
        keep_frames = [frame - self.export_start for frame in self.frames]
        self.server_socket.send_message(
            {
                "file_path": self.file_path,
                "keep_frames": keep_frames,
            }
        )
        response = self.server_socket.receive_message()
        file_handling.delete_file(self.file_path)

        if not isinstance(response, dict):
            self.report({"ERROR"}, f"Unexpected response from Cascadeur: {response}")
            return {"CANCELLED"}
        if response.get("error_code") == "LICENSE_REQUIRED":
            bpy.ops.cbb.license_required_popup("INVOKE_DEFAULT")
            return {"CANCELLED"}
        if response.get("status") != "completed":
            error = get_cascadeur_error(response)
            self.report({"ERROR"}, error or "Tween failed.")
            return {"CANCELLED"}

        files = response.get("files") or []
        if not files:
            self.report({"ERROR"}, "Cascadeur did not return an animation.")
            return {"CANCELLED"}

        self.skipped_gaps = response.get("ai_skipped_gaps") or []
        self._apply_result(context, files[0])
        for path in files:
            file_handling.delete_file(path)
        note = "Tweened animation applied."
        if self.skipped_gaps:
            note += " Gaps longer than 120 frames were left linear."
        self.report({"INFO"}, note)
        return {"FINISHED"}

    def _apply_result(self, context, path):
        armature = bpy.data.objects[self.armature_name]
        action = bpy.data.actions[self.action_name]
        slot = None
        if self.slot_identifier and getattr(action, "slots", None):
            for candidate in action.slots:
                if candidate.identifier == self.slot_identifier:
                    slot = candidate
                    break

        before = set(bpy.data.objects)
        bpy.ops.import_scene.fbx(
            filepath=path,
            ignore_leaf_bones=False,
            automatic_bone_orientation=False,
            use_anim=True,
            axis_forward="-Z",
            axis_up="Y",
        )
        imported = [
            obj
            for obj in bpy.data.objects
            if obj not in before and obj.type == "ARMATURE"
        ]
        if not imported:
            raise RuntimeError("Imported tween had no armature.")
        imported_arm = imported[0]
        imported_arm.matrix_world = armature.matrix_world.copy()

        if not self.overwrite:
            tween.copy_action(armature, action, slot)
        pairs = tween.frame_pairs(
            imported_arm.animation_data.action if imported_arm.animation_data else None,
            self.frames[0],
            self.frames[-1],
        )
        tween.apply_control_tween(armature, imported_arm, pairs, self.frames)

        for obj in [obj for obj in bpy.data.objects if obj not in before]:
            bpy.data.objects.remove(obj, do_unlink=True)

        context.view_layer.objects.active = armature
        armature.select_set(True)
