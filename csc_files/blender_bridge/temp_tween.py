import csc


AI_MAX_GAP = 120


def name():
    return "Blender Bridge.Temp Tween"


def description():
    return (
        "SHOULD ONLY BE CALLED FROM BLENDER! "
        "Imports a blocked skeleton, runs Cascadeur inbetweening, and exports the baked result."
    )


def _norm(name):
    name = name.split(":")[-1]
    if name.startswith("DEF-"):
        name = name[4:]
    return name.lower().replace("-", "_").replace(".", "_").replace(" ", "_")


def _aliases():
    # CloudRig / Rigify-ish names, plus a few Mixamo fallbacks.
    return {
        "pelvis": ["hips", "pelvis", "hip"],
        "stomach": ["spine_001", "spine1", "spine_01", "abdomen", "stomach"],
        "chest": ["spine_002", "spine2", "spine_02", "chest"],
        "neck": ["neck"],
        "head": ["head"],
        "clavicle_l": ["shoulder_l", "clavicle_l", "clavicleleft", "leftshoulder"],
        "arm_l": ["upper_arm_l", "arm_l", "upperarm_l", "leftarm"],
        "forearm_l": ["forearm_l", "lowerarm_l", "leftforearm"],
        "hand_l": ["hand_l", "lefthand"],
        "thigh_l": ["thigh_l", "upleg_l", "leftupleg"],
        "calf_l": ["shin_l", "calf_l", "leg_l", "leftleg"],
        "foot_l": ["foot_l", "leftfoot"],
        "toe_l": ["toe_l", "toes_l", "lefttoebase"],
        "clavicle_r": ["shoulder_r", "clavicle_r", "clavicleright", "rightshoulder"],
        "arm_r": ["upper_arm_r", "arm_r", "upperarm_r", "rightarm"],
        "forearm_r": ["forearm_r", "lowerarm_r", "rightforearm"],
        "hand_r": ["hand_r", "righthand"],
        "thigh_r": ["thigh_r", "upleg_r", "rightupleg"],
        "calf_r": ["shin_r", "calf_r", "leg_r", "rightleg"],
        "foot_r": ["foot_r", "rightfoot"],
        "toe_r": ["toe_r", "toes_r", "righttoebase"],
    }


def _map_joints(model_viewer):
    by_norm = {}
    for obj in model_viewer.get_objects():
        by_norm.setdefault(_norm(model_viewer.get_object_name(obj)), obj)
    aliases = _aliases()
    if "hips" not in by_norm and "pelvis" not in by_norm and "hip" not in by_norm and "spine" in by_norm:
        aliases["pelvis"] = ["spine"] + aliases["pelvis"]
    mapped = {}
    missing = []
    for field, names in aliases.items():
        found = None
        for alias in names:
            if alias in by_norm:
                found = by_norm[alias]
                break
        if found is None:
            missing.append(field)
        else:
            mapped[field] = found
    return mapped, missing


def _layer_keys(domain):
    viewer = domain.layers_viewer()
    keys = set()
    for layer_id in viewer.all_layer_ids():
        keys.update(int(key) for key in viewer.layer(layer_id).sections.keys())
    return keys


def _align_keep_frames(domain, keep):
    keep = [int(frame) for frame in keep]
    actual = _layer_keys(domain)
    if set(keep).issubset(actual):
        return keep
    if not actual or not keep:
        raise RuntimeError("Cascadeur imported no animation keys.")
    delta = min(actual) - min(keep)
    shifted = [frame + delta for frame in keep]
    if set(shifted).issubset(actual):
        return shifted
    raise RuntimeError(
        f"Blocking frames {keep[:6]} were not in the imported keys {sorted(actual)[:6]}."
    )


def _drop_extra_keys(domain, keep):
    keep = set(keep)

    def mod(model, update, scene_):
        editor = model.layers_editor()
        viewer = scene_.layers_viewer()
        for layer_id in list(viewer.all_layer_ids()):
            for key in list(viewer.layer(layer_id).sections.keys()):
                if int(key) not in keep:
                    editor.unset_section(int(key), layer_id)

    if not domain.modify("Drop in-between keys", mod):
        raise RuntimeError("Could not drop in-between keys.")


def _skip_rig_warning_dialogs():
    # Hinge warnings otherwise pop a Continue/Cancel dialog and stall the round trip.
    import prototypes.rigs._rigs as rigs

    def run_rig_process(self, main_process_method, *args, **kwargs):
        self.pre_process()
        errors = self.get_preprocess_errors()
        if errors:
            raise RuntimeError(" ".join(errors))
        main_process_method(self, *args, **kwargs)

    rigs.RigDefinition.run_rig_process = run_rig_process


def _quick_rig(domain, mapped):
    import numpy as np
    from prototypes.qrt_prototypes.create import create_biped_rig

    _skip_rig_warning_dialogs()

    data = csc.rig.QrtData()
    props = []
    viewer = domain.model_viewer()
    for field, obj in mapped.items():
        prop = csc.rig.BoneProperty()
        prop.bone_name = field
        prop.object_id = obj
        prop.joint_path_name = csc.model.PathName.get_path_name(obj, viewer)
        props.append(prop)
    data.body = [props]
    data.left_hand = []
    data.right_hand = []
    data.is_create_layers = True
    data.is_replace_existing = True
    data.is_align_pelvis = True
    data.is_spline_ik = True
    create_biped_rig(
        domain,
        data,
        10.0,
        10.0,
        np.array([0.0, 0.5, 0.0], dtype=np.float32),
        "",
    )
    points = domain.model_viewer().behaviour_viewer().get_behaviours("Point")
    if len(points) < 10:
        raise RuntimeError("Quick rig did not create a Cascadeur character.")


def _set_interpolation(domain, interpolation):
    skipped = []

    def mod(model, update, scene_):
        editor = model.layers_editor()
        viewer = scene_.layers_viewer()
        for layer_id in list(viewer.all_layer_ids()):
            keys = sorted(int(key) for key in viewer.layer(layer_id).sections.keys())
            for index, key in enumerate(keys[:-1]):
                gap = keys[index + 1] - key
                if gap < 2 or gap > AI_MAX_GAP:
                    if gap > AI_MAX_GAP:
                        skipped.append(gap)
                    continue

                def change(section, _interpolation=interpolation):
                    section.interval.interpolation = _interpolation

                editor.change_section(key, layer_id, change)

    if not domain.modify("Set interpolation", mod):
        raise RuntimeError("Could not set interpolation.")
    return skipped


def _joint_axis(domain, joint_name, frame):
    import numpy as np

    viewer = domain.model_viewer()
    behaviour = viewer.behaviour_viewer()
    for joint in behaviour.get_behaviours("Joint"):
        owner = behaviour.get_behaviour_owner(joint)
        if viewer.get_object_name(owner) == joint_name:
            matrix_id = behaviour.get_behaviour_data(joint, "global_matrix")
            matrix = np.array(viewer.data_viewer().get_data_value(matrix_id, frame))
            return matrix[:3, 0].round(3).tolist()
    return None


def _ai_generated(domain, joint_name, keep):
    if len(keep) < 2 or not joint_name:
        return False
    start, end = keep[0], keep[-1]
    mid = (start + end) // 2
    if mid == start:
        return False
    start_axis = _joint_axis(domain, joint_name, start)
    mid_axis = _joint_axis(domain, joint_name, mid)
    return start_axis is not None and mid_axis is not None and start_axis != mid_axis


def _enter_rig_mode(domain):
    import rig_mode.on as rig_on

    rig_on.run_raw(domain, [0.0, 0.5, 0.0], with_dialogs=False)


def _leave_rig_mode(domain):
    import rig_mode.off as rig_off

    rig_off.run(domain, True)


def _run_inbetweening(app, domain):
    viewer = domain.layers_viewer()
    layer_ids = list(viewer.all_layer_ids())
    keys = _layer_keys(domain)
    if layer_ids and keys:
        domain.get_layers_selector().set_full_selection_by_parts(
            layer_ids, min(keys), max(keys)
        )
    app.get_action_manager().call_action("Timeline.AI.AI on selected interval")
    tool = app.get_tools_manager().get_tool("InbetweeningTool").editor(app.current_scene())
    tool.set_update_parameter(False)
    tool.set_update_parameter(True)

    def force(model, update, scene_updater):
        scene_updater.get_interpolator().interpolate()

    domain.modify_update("Interpolate tween", force)


def _export(app, scene_view, export_path):
    from . import commons

    if not app.is_export_available():
        # Some installs report this false and still export. Try, then fail closed.
        pass
    message = {"export_settings": {"euler_filter": True, "up_axis": "Y", "bake_animation": True}}
    commons.export_fbx(app, scene_view, message, export_path)
    if not commons.path_exists(export_path):
        if not app.is_export_available():
            raise PermissionError("Export is not available with the current license.")
        raise RuntimeError("Export file was not created.")


def run(scene):
    from .client_socket import ClientSocket
    from . import commons

    app = csc.app.get_application()
    client = None
    try:
        client = ClientSocket()
    except Exception as error:
        scene.error(f"Couldn't connect to Blender. Error: {error}")
        return

    try:
        message = client.receive_message()
        file_path = message.get("file_path")
        keep_frames = message.get("keep_frames") or []

        scene_manager = app.get_scene_manager()
        app_scene = scene_manager.create_application_scene()
        scene_manager.set_current_scene(app_scene)
        domain = app_scene.domain_scene()

        loader = app.get_tools_manager().get_tool("FbxSceneLoader").get_fbx_loader(app_scene)
        settings = csc.fbx.FbxSettings()
        settings.mode = csc.fbx.FbxSettingsMode.Binary
        settings.up_axis = csc.fbx.FbxSettingsAxis.Y
        settings.bake_animation = False
        loader.set_settings(settings)
        loader.import_scene(file_path)

        keep_frames = _align_keep_frames(domain, keep_frames)
        _enter_rig_mode(domain)
        mapped, missing = _map_joints(domain.model_viewer())
        if "arm_l" not in mapped or "arm_r" not in mapped:
            raise RuntimeError(
                "Could not map both arms. Need upper_arm.L/R, arm.L/R, or Mixamo arms. "
                f"Missing: {', '.join(missing)}"
            )
        arm_name = domain.model_viewer().get_object_name(mapped["arm_l"])
        # Rig the baked import first. Quick rig fails on an already-sparse timeline.
        _quick_rig(domain, mapped)
        try:
            _leave_rig_mode(domain)
        except Exception as error:
            scene.warning(f"Rig mode off failed, continuing with the quick rig. {error}")
        _drop_extra_keys(domain, keep_frames)
        skipped = _set_interpolation(domain, csc.layers.layer.Interpolation.AI)
        _run_inbetweening(app, domain)
        if not _ai_generated(domain, arm_name, keep_frames):
            _set_interpolation(domain, csc.layers.layer.Interpolation.BEZIER)
            _run_inbetweening(app, domain)
            skipped = skipped or ["ai-fallback"]

        export_path = commons.get_export_path(app_scene.name() or "tween")
        _export(app, app_scene, export_path)
        client.send_message(
            {
                "status": "completed",
                "files": [export_path],
                "ai_skipped_gaps": skipped,
                "mapped": sorted(mapped.keys()),
            }
        )
    except PermissionError as error:
        client.send_message(
            {
                "status": "error",
                "error_code": "LICENSE_REQUIRED",
                "message": str(error),
            }
        )
    except Exception as error:
        scene.error(f"Tween failed. Error: {error}")
        try:
            client.send_message(
                {
                    "status": "error",
                    "error_code": "TWEEN_FAILED",
                    "message": str(error),
                }
            )
        except Exception:
            pass
    finally:
        if client is not None:
            client.close()
