# Handoff: CloudRig blocked-animation tween round trip

This file is for an outside agent investigating why the result of this work did not
match the user's expectations. Read this before changing anything.

## Resolution (2026-11-01)

Root causes found and fixed; the sections below describe the state before the fix.

1. **Send was corrupt.** `key_blocking_poses` assigned `pose_bone.matrix` child by child
   without a depsgraph update, so each child solved against its parent's stale pose.
   Cascadeur received garbage poses. It now solves `matrix_basis` from the source poses
   (`Bone.convert_local_to_pose`). Verified: export -> FBX import of `RIG-attack`
   reproduces every DEF bone within 0.0006 / 0 deg on 142-168.
2. **Return remapped local quaternions onto the wrong controls.** `apply_control_tween`
   was removed. Tween now uses `apply_control_poses`: per frame,
   `control = desired_def @ live_def^-1 @ live_control`. Controls come from the *last*
   active COPY_TRANSFORMS (IK wins over FK), visible bone collections, and a climb from
   hidden IK mechanism to the IK foot control. IK thigh/shin and tweak bones riding on
   mechanism (twist, face, hair) are not keyed. On `RIG-attack`: 52 controls incl.
   `HIP-spine`, `IK-foot.L/R`, FK arms/spine/fingers, `center`.
3. `_clear_range` crashed removing keys (stale references); now removes back to front.
   DEF keys are no longer cleared.
4. A broken earlier run overwrote `RIG-attackAction`: FK rotations keyed on every frame
   142-168, so every frame counted as a blocking key. `RIG-attackAction_blocking`
   (made in the live session, unsaved at time of writing) keeps only 142/145/150/154/159.

Known limit: IK knees follow the existing pole, not Cascadeur's knee direction.
Not yet verified: a fresh Cascadeur round trip with the fixed send.

## What the user wants

The user animates a CloudRig character in Blender with sparse, blocked key poses on
the **controls**. They want to:

1. Send those blocking poses to Cascadeur.
2. Let Cascadeur generate the in-betweens (AI inbetweening).
3. Bring the result back onto the **same CloudRig controls**, not onto the deform
   bones.
4. Keep the existing holds and key timing where possible.
5. Work on the **active** rig only, even when the scene contains hundreds of other
   armatures.
6. Use the **scene frame range**, not the action's full length.
7. Optionally overwrite the current action instead of creating a new one.

The important constraint: do not put the in-betweens on `DEF-*` bones. CloudRig's
deform bones are driven by controls; keying the deform bones directly makes posed
FK limbs slide off their joints. Rotations written onto the driving control are
what should make a limb rotate through the in-betweens.

## Reported state

The plumbing now runs end to end, but the user says the results are not what they
expected. The exact symptom was not recorded in the session log (wrong pose, sliding,
lost holds, wrong timing, and keys on the wrong bones are all still possible).
**Reproduce the workflow first and write down the actual result before changing
code.** The unit-level and background checks below prove the transfer mechanics, so
the mismatch is most likely in pose fidelity, control selection, or Cascadeur's
handling of the skeleton.

## Current implementation

Branch `master`, latest commits:

- `ae2059c` Read deform rest bones without Edit Mode
- `a8c9147` Use the active rig and the scene frame range
- `7c450c9` Export a CloudRig as a deform skeleton and import onto controls
- `9bd6bf2` Keep tweens on the controls that were keyed

The installed copy Blender actually loads is:

```
C:\Apps\blender\portable\extensions\user_default\cascadeur-bridge
```

It is **not** a symlink. After editing the repo, copy the changed files there and
restart Blender; do not rely on addon disable/reload (it leaves half-registered
classes).

### Flow

**Send** (`cbb.tween_blocked_animation`, `operators/tween.py`):

1. `tween.active_armature(context)` gets the selected active armature.
2. `tween.shot_frames(action, slot, scene.frame_start, scene.frame_end)` collects
   keyed frames inside the scene range and always adds the range ends.
3. `tween.build_deform_armature` builds a temporary armature containing only the
   deform bones, re-parented into their real deform hierarchy.
4. `tween.key_blocking_poses` samples the live rig's evaluated pose on each blocking
   frame and keys the temporary armature.
5. The temporary armature is exported as FBX, `bake_anim=True`,
   `simplify_factor=0.0`, axes `-Z` forward / `Y` up.

**Cascadeur** (`csc_files/blender_bridge/temp_tween.py`):

1. Imports the FBX with `bake_animation=False`, `up_axis=Y`.
2. Aligns `keep_frames` to the imported key frames.
3. Creates a Cascadeur Quick Rig from roughly 22 biped joints (see `_map_joints`).
4. Drops every imported key not in `keep_frames`.
5. Runs AI inbetweening, falls back to Bezier if AI produces no motion, exports FBX.

**Return** (`CBB_OT_tween_blocked_animation.on_connected` -> `_apply_result`):

1. Imports the returned FBX.
2. `tween.copy_action` creates `<action>_csc_tween` unless **Overwrite Action** is on.
3. `tween.apply_control_tween` maps each imported deform bone to a driving control,
   remaps the imported rotation samples into the control's local rotation space, and
   keys only the control's rotation.
4. DEF-bone keys inside the written range are deleted from the action.
5. The imported helper objects are removed.

### Key functions

| Function | File | Purpose |
| --- | --- | --- |
| `deform_parent_map` | `utils/tween.py` | Rebuilds DEF hierarchy when DEF bones sit under controls |
| `_bone_rest` | `utils/tween.py` | Rest head/tail/roll without Edit Mode (library overrides cannot enter it) |
| `build_deform_armature` | `utils/tween.py` | Temporary deform-only skeleton |
| `key_blocking_poses` | `utils/tween.py` | Samples live poses onto the temporary skeleton |
| `export_blocked_deform` | `utils/tween.py` | Sparse blocked export used by the manual export path |
| `apply_control_poses` | `utils/tween.py` | World-space solve used by **Import Action** |
| `apply_control_tween` | `utils/tween.py` | Rotation remap used by **Tween Blocked Animation** |
| `_control_targets` / `_driver_control` | `utils/tween.py` | Finds the control that drives a DEF bone |

`apply_control_poses` (Import Action) uses a world-matrix solve:
`desired_def @ def_rest.inverted() @ mechanism_rest`, applied to the control. This
reproduces the DEF pose exactly in tests. `apply_control_tween` instead remaps local
quaternions per control across each interval; it is the more approximate of the two.

## Verified in this session

- Background Blender on the real saved scene (`shot-csc.blend`) exported the
  override rig `RIG-attack` successfully: 172 deform bones, frames
  `[142, 145, 150, 154, 159, 168]`, valid 841 KB FBX, no leftover objects, scene
  selection and range restored.
- A synthetic return test on the same override rig wrote 53 control keys
  (`FK-forearm.L` etc.) into a new action and left the original `RIG-attackAction`
  (7143 curves) untouched.
- `tests/test_tween.py::run_self_check()` passes under Blender 5.2
  (`--factory-startup --background`). It covers hierarchy, range slicing, overwrite
  behaviour, key preservation outside the written range, and the world-matrix solve.
- Earlier exports were verified to contain 172 DEF bones and no control bones, with
  holds preserved as sparse keys.

## Environment

- Blender: `5.2.0 LTS`, `C:\Apps\blender\blender.exe`.
- Scene: `D:\Work\General projects\2026-10-14 [CLOSER]\Assets\Visual\shot-csc.blend`
  (about 488 MB).
- Active rig: `RIG-attack`, a **library override** of `attack_rig.blend`. Scene range
  is 142-168. Each of its 172 DEF bones is parented to a non-deform control.
- The scene also contains hundreds of one-bone `Placement_*` armatures. They are
  ordinary rigs and must stay out of the transfer.
- Cascadeur-side scripts were installed manually by the user. Do not pursue the
  installer permission bug.
- An unrelated `PureSky_0_STUDIO` handler raises `KeyError: 'shot1'` on depsgraph
  updates. It is noise, not the tween bug.
- MCP `execute_blender_code` runs inside a timer. Calling `bpy.ops.object.mode_set`
  from there crashed Blender twice on this file. Use background Blender for anything
  that changes modes; do not repeat that live test.

## Likely places to look

1. **Rotation-only return.** `apply_control_tween` writes only rotation. IK controls
   and any control that carries translation or scale will not reproduce Cascadeur's
   pose. Check whether the unexpected result is limbs that keep their old locations.
2. **Local-quaternion remap.** `_remap_quats` assumes the control's rotation is the
   only thing changing and slerps a correction between the first and last sample.
   It does not solve world matrices, and it can drift inside long intervals.
3. **One control per DEF bone.** `driven[control] = name` overwrites when several DEF
   bones are driven by the same control. Only one of them decides the control's
   motion; the others are ignored.
4. **Mechanism assumptions.** `_driver_control` only understands `COPY_TRANSFORMS`
   constraints and `FK-`/`IK-` name prefixes. Twist bones, props, fingers, and
   helpers may map to the wrong control or not at all.
5. **DEF keys are deleted in range.** `_clear_range` removes DEF fcurves inside the
   written span, even when Overwrite Action is off (it acts on the copied action).
   That violates a literal reading of "do not modify DEF bones" and can change the
   pose if those DEF keys were meaningful.
6. **Cascadeur Quick Rig coverage.** `temp_tween.py` maps about 22 joints and
   creates a biped Quick Rig. Bones outside that set may be baked by the FBX export
   but not influenced by the AI result. Compare the returned FBX skeleton against
   the sent one before blaming the Blender side.
7. **Frame alignment.** `keep_frames` are sent relative to `export_start`
   (142 in this scene). Cascadeur may import them one frame later; `_align_keep_frames`
   compensates only when the whole key set is shifted. Check the returned action's
   first key and whether it is one frame off.
8. **Holds.** The tween path exports with `simplify_factor=0.0` and does not set
   CONSTANT interpolation before baking, so the FBX contains a key on every frame.
   Cascadeur drops non-keep keys, but the intermediate samples feed the Quick Rig
   setup. Verify that the blocking poses at 142/145/150/154/159/168 are exact.

## Reproduction checklist

1. Open the scene, make `RIG-attack` the active and only selected object.
2. Set the scene range to 142-168.
3. Leave **Overwrite Action** off for the first run.
4. Click **Tween Blocked Animation**.
5. Record: the new action name, whether it is assigned, the keyed frames on each
   control, and whether DEF bones have keys inside 142-168.
6. Compare the returned pose at each blocking frame against the original action
   (before the tween). Then compare an in-between frame against Cascadeur.
7. Check the returned FBX before it is deleted if pose fidelity is in question.

Do not overwrite `RIG-attackAction` until the off-path is understood.

## Commands

Run the self-check in a throwaway Blender:

```python
# runner.py
import importlib.util
spec = importlib.util.spec_from_file_location(
    "test_tween", r"D:\Dev\projects\cascadeur-bridge\tests\test_tween.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.run_self_check())
```

```
"C:\Apps\blender\blender.exe" --factory-startup --background --python runner.py
```

For work on the real file, run Blender in `--background` with the scene and a probe
script. The file takes a few minutes to load; the MCP CLI helper times out at 120 s,
so use a `.cmd` wrapper with redirected output instead.

## Open questions for the investigator

- Is the wrong result about pose, timing, or both? Which frames and which limbs?
- Does the user expect IK controls to receive location keys, or only FK rotations?
- Should the original blocking keys on controls remain exactly as authored at the
  keep frames? `apply_control_tween` forces the authored local rotation at those
  frames; verify that this matches Cascadeur's returned pose there.
- Is the final animation supposed to be a new action by default, or is overwriting
  the authored action the expected default for this workflow?
- Should the returned animation preserve keys outside the scene range on the copied
  action? It currently keeps them.
