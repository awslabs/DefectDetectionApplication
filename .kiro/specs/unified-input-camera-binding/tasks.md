# Implementation Plan: unified-input-camera-binding

## Overview

The fix is one call site in `package_workflow` (`edge-cv-portal/backend/functions/workflow_packaging.py`): Camera_Input_Nodes are gathered from `expand_unified_inputs(graph, catalog)`. The tests follow the repo's bugfix method:

1. bug-condition exploration tests that fail on the unfixed code;
2. preservation tests that pass on the unfixed code;
3. the fix, after which both pass.

The fix is Portal-only, but the symptom and the result are on the device. It is therefore verified in the deployed Portal and on two devices before the commit. Results are in `verification-notes.md`.

**Needs your go-ahead**, asked at the step rather than assumed:
- a temporary Portal principal for tasks 3 and 7: a throwaway app client, a temp user and scoped role rows, all deleted afterwards (approved 2026-09-28);
- test workflow deployments to two lab devices, restored afterwards (approved);
- the Portal backend deploy in task 6, after pulling and merging the latest code (approved).

**Preservation-tracked files.** None. `workflow_packaging.py` is not hash-pinned, and the guards in task 5 confirm it. No component build is needed.

**Devices.** thor1 (JP7) and the Orin AGX (JP6). The Dell was planned, but it has no Portal use case, so the Portal cannot deploy to it; the Orin is in thor1's use case.

## Tasks

- [x] 1. Write the bug-condition exploration tests
  - **Property 1: Bug Condition.** A camera-kind Input Source is packaged without its dedicated node's binding metadata.
  - `edge-cv-portal/backend/tests/test_unified_input_camera_binding.py`:
    - `test_camera_input_source_packages_like_its_dedicated_node` (Hypothesis, through the packaging handler, W against E);
    - `test_aravis_input_source_gets_a_frame_feed_on_the_device`, using the device's `plan_aravis_feeds` on the packaged document;
    - `test_csi_input_source_gets_the_csi_sensor_binding`;
    - `test_icam_input_source_gets_the_device_slot`;
    - `test_deployment_check_asks_for_a_camera_for_the_input_source`, using `validate_camera_bindings`.
  - **Outcome on the unfixed code:** all 5 FAILED.
    - Smallest counterexample: one Input Source with `source_kind: icam` and `device: /dev/video0`. It had no `bindingPoints`, while its `icam_source` equivalent had one `device` slot.
    - The Aravis and CSI examples had no binding point, and the ICAM example had no `bindingPoints` key. The deployment check returned no error.
  - Correction to the tests themselves: the oracle first compiled the generated definition. The workflows handler stores the serializer's canonical form, which can reorder nodes, so the oracle now compiles the stored definition.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3_

- [x] 2. Write the preservation tests before the fix
  - **Property 2: Preservation.**
    - `test_packages_outside_the_bug_condition_are_unchanged` (Hypothesis, through the handler). Each architecture's `compiled_pipeline.json` text, and the version item's `has_binding_points` and `camera_input_nodes`, equal today's path rebuilt from the pure helpers over the stored graph.
    - `test_folder_input_source_with_camera_parameters_stays_unbound`.
    - `test_camera_input_source_pipeline_is_the_plain_compiler_output`: the compiled document without `bindingPoints` equals the compiler's output (3.4).
  - **Outcome on the unfixed code:** all 5 PASSED.
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 3. Reproduce on devices with today's Portal
  - [x] 3.1 Create the temporary Portal principal. Portal_Identity enforcement is on, so it needed a global and a use-case role row.
  - [x] 3.2 Create the repro workflows, one per device: an Input Source set to `aravis_camera` with camera id `Fake_1`, feeding a capture, packaged for that device's architecture only.
    - Recorded: `has_binding_points` false and `camera_input_nodes` empty.
  - [x] 3.3 Create and package the controls: `uicb-control-aravis`, with a dedicated `aravis_camera_source`, and `uicb-control-folder`, with a folder source. Their compiled documents and version items were kept for task 7.1.
  - [x] 3.4 Deploy the repro workflows; no camera step was offered. Trigger a Run on each through the engine API.
    - Outcome: both Runs failed at the pipeline watchdog, after 120 s on thor1 and 123 s on the Orin, with "Pipeline timed out after 120s without completing (no EOS/ERROR received)." No Aravis feed was planned.
  - _Requirements: 1.1, 1.2, 1.3_

- [x] 4. Fix
  - [x] 4.1 In `package_workflow`, gather Camera_Input_Nodes from `expand_unified_inputs(graph, catalog)`, imported from `workflow_core.compiler.compiler`. The comment above the gather and the section comment on binding points are updated.
  - [x] 4.2 The exploration tests now pass, and the preservation tests still pass: 10 of 10 with 100 examples per property.
  - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 5. Checkpoint
  - The new module, the packaging binding-point and camera-binding suites: 115 passed. The layer's `test_unified_input_expansion.py` and `test_property_zero_trigger_preservation.py`: 15 passed.
  - The full Portal backend suite, in 12 shards, against a base worktree at the pre-fix commit: the trees differ only in the expected tests. One scope check, `TestSourceTreeUntouched::test_workflow_packaging_still_emits_the_unpinned_model_dependency`, fails while `workflow_packaging.py` has uncommitted edits and passes once it is committed.
  - The guard pair, the IAM and S3 out-of-scope guards, and the IAM synth gate on the host: all green.

- [x] 6. Deploy the Portal backend
  - [x] 6.1 Pre-checks passed:
    - no component build running, here or on the fleet;
    - no Portal stack update in progress;
    - the latest `integration/all-specs` (`4951222`) merged;
    - the deployed functions and layers matched this checkout.
  - [x] 6.2 `cdk diff --all` with the deploy script's context: no IAM or resource changes beyond Lambda code, layer republishes with identical sources, and the per-deploy custom resources. The quick-setup bundle would have changed only in file modes, so the 8 `station_install` files were given production's modes, and the bundle is byte-identical.
  - [x] 6.3 Ran `deploy-infrastructure.sh` from 00:54 to 01:09Z. Enforcement stayed on. The packaging Lambda runs the fixed file (sha256 `814fe84f…`).
  - [x] 6.4 Moved `cdk.out` aside; the guard pair passes.

- [x] 7. Verify on the Portal and devices
  - [x] 7.1 Both controls were packaged again. Their compiled documents and `workflow.json` are byte-identical, the manifests differ only in `componentVersion` and `packagedAt`, and the version items are unchanged.
  - [x] 7.2 Both repro workflows were packaged again. `has_binding_points` is true, the Input Source is recorded as `aravis_camera_source`, the pipeline is unchanged, and the binding-context endpoint lists the node.
  - [x] 7.3 A deployment with no camera binding is refused with `CAMERA_NODE_UNBOUND` naming the node, on both devices.
  - [x] 7.4 Deploy with a binding and run.
    - thor1, bound to its Basler with the typed id left as `Fake_1`: 3 of 3 Runs completed, and the executor grabbed the Basler.
    - The Orin: a cameraSourceId binding cannot resolve there, because the device cannot read its own inventory (a pre-existing RTSP Image_Source row). The Portal's override option also fails, pre-existing. An override was written to the test key of its bindings shadow, and then 3 of 3 Runs completed.
    - A 10-minute soak with 12 runs per device: every run completed, every health probe answered 200, and there was no restart.
  - [x] 7.5 Cleanup:
    - both deployments restored;
    - test workflows, components, S3 zips, shadow keys and device directories removed;
    - the temporary principal removed;
    - models as found.
  - [x] 7.6 Wrote `verification-notes.md`.
  - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.5_

- [x] 8. Align multi-source-workflows with this fix
  - `design.md`: the "Pre-existing gap" section becomes a dependency on this fix. Decision 4's binding bullet notes that every package already binds a camera-kind Input Source by effective type.
  - `tasks.md`: in 3.1, the `unified_input(aravis_camera)` golden records the fixed output; 3.2 builds on the expanded graph.

- [x] 9. Commit, with your go-ahead
  - Commit on `fix/unified-input-camera-binding`, stating what was verified in the Portal and on which devices.
  - Merge into `integration/all-specs` and push as you direct.
  - Committed on `fix/unified-input-camera-binding` on top of `integration/all-specs` `4951222`, then fast-forwarded into `integration/all-specs` and pushed to origin on 2026-09-29, as the user directed. The code is the file the Portal has run since the 01:01Z deploy.
