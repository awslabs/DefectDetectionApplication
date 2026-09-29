# Unified Input Camera Binding: Verification Notes

Date: 2026-09-29, all times UTC. Portal account 164152369890, us-east-1, use case `645504ce-a60a-4009-8349-7548c0025cd3`.

## Tests

The tests are in `edge-cv-portal/backend/tests/test_unified_input_camera_binding.py`: 10 tests, all through the real packaging handler. They ran in `dda-portal-test:py311` with `HYPOTHESIS_PROFILE=ci` (100 examples per property).

**On the unfixed code** (a worktree at `4951222`): the 5 bug-condition tests FAILED, and the 5 preservation tests PASSED.
- Property 1's smallest counterexample: one Input Source with `source_kind: icam` and `device: /dev/video0`. Its package has no `bindingPoints`, while the same workflow with an `icam_source` node has one `device` slot.
- The Aravis and CSI examples had no binding point, and the ICAM example had no `bindingPoints` key.
- The deployment check returned no error for an unbound Input Source.

**After the fix:** all 10 PASSED, in 47 s.

| Suite | Result |
|---|---|
| New module, plus the existing packaging binding-point and camera-binding suites (15 files) | 115 passed |
| `workflow_core` layer: `test_unified_input_expansion.py`, `test_property_zero_trigger_preservation.py` | 15 passed |
| Full Portal backend suite, 12 shards per tree, 411 files, fixed tree against the unfixed worktree | fixed: 5,514 passed, 38 failed, 72 errors; base: 5,488 passed, 62 failed, 72 errors |
| Guard pair, on the host | 4 passed, 3 skipped |
| IAM and S3 out-of-scope guards, on the host | 3 passed, 1 skipped |
| IAM synth gate, on the host | 11 passed |

The full-suite results differ between the trees only in these tests:
- **Fail on base only:** the 5 new bug-condition tests. Also the 20 `test_git_sync_runner` tests, which always fail in a worktree because its `.git` file points into the main repository.
- **Fails on the fixed tree only:** `test_deployment_preflight_preservation.py::TestSourceTreeUntouched::test_workflow_packaging_still_emits_the_unpinned_model_dependency`. It asserts that `git diff HEAD -- workflow_packaging.py` is empty, so it fails whenever that file has uncommitted edits, and passes once the fix is committed. The behavior it protects, `model_component_dependencies`, is untouched.

The 72 errors are the same in both trees. They are pre-existing module-scoped `mock_aws` collisions.

## Before the fix, on the Portal and devices

**Devices.** Thor1 (JP7, `LocalServer.arm64JP7` 1.0.49) and the Orin AGX (JP6, 1.0.72) were used. The Dell was planned, but it has no use case in `dda-portal-devices`, so the Portal cannot deploy to it. The Orin is in the same use case as thor1 and has `Fake_1` and a Basler.

**Temporary principal.** `kiro-uicb-verify-1790640999` was created on a throwaway app client. It had two `dda-portal-user-roles` rows (global Viewer, use-case UseCaseAdmin), because Portal_Identity enforcement is on.

**Workflows.** Each device got its own workflow, packaged only for that device's architecture: `uicb-repro-thor1` (arm64_jp7) and `uicb-repro-orin` (arm64_jp6). Each was an Input Source set to `aravis_camera` with camera id `Fake_1`, feeding a capture. Both validated with no findings.

**Today's Portal, on both repro workflows:**
- Packaging recorded `has_binding_points: false` and `camera_input_nodes: []`, and the compiled documents had no `bindingPoints`.
- The binding-context view answered `binding_required: false`, so there was no camera step.
- The deployment was accepted with no binding: thor1 `e6d7071e`, the Orin `91fc7cd5`. Each added only the workflow component.
- Both devices registered the workflow as `registered`.

**One run on each device.**

| Device | Execution | Result |
|---|---|---|
| thor1 | `ede4709c` | failed after 120 s: "Pipeline timed out after 120s without completing (no EOS/ERROR received)." |
| Orin | `5be47d7b` | the same failure, after 123 s |

Neither log has an "Aravis frame feed planned" line for `input_source_1`, and neither run wrote a capture. This confirms 1.3 on hardware.

**Controls.** `uicb-control-aravis` (a dedicated `aravis_camera_source`) and `uicb-control-folder` were packaged for arm64_jp6 and arm64_jp7, and their documents and version items were kept.

## Deploy

The code was pulled before the deploy. Origin `integration/all-specs` had moved to `4951222`, which makes the deploy scripts keep Portal_Identity enforcement at its deployed value, and it was fast-forwarded. The tree was still at `4951222` right before the deploy.

**Before the deploy.** The deployed packaging Lambda, shared layer and `workflow_core` layer matched this checkout file for file, so the deploy added only the fix.

**`cdk diff`** used the deploy script's own context: `cloudFrontDomain` and `portalRegistryEnforced=true`. It showed:
- no IAM or security-group change;
- 59 Lambda code updates, from the functions asset;
- 6 layer versions republished. Their sources are identical; only `__pycache__` files differ, because the previous deploy was built with CPython 3.11 and 3.14. The imaging layers hold the same Pillow 12.3.0.
- the two custom resources that run on every deploy, because of their timestamp;
- the quick-setup bundle asset.

**The quick-setup bundle.** Its contents were identical to production, but this checkout's umask made 8 `station_install` files group-writable. That would have changed the file modes inside the bundle that stations download. Those 8 files now have production's modes, and the rebuilt bundle's sha256 equals production's `74d834ff…`.

**`deploy-infrastructure.sh`** ran from 00:54 to 01:09Z, and all 8 stacks succeeded. Enforcement stayed on: 54 handlers `true`.
- The packaging Lambda was updated at 01:01:36Z. Its `workflow_packaging.py` sha256 `814fe84f…` equals the working tree's, and the whole functions directory matches.
- `cdk.out` was moved to `cdk.out.bak-20260929T011039Z`, and the guard pair passed afterwards.

## After the fix, on the Portal and devices

**Controls, packaged again as 2.0.0 on arm64_jp6 and arm64_jp7:**
- the Portal copies and the zip copies of `compiled_pipeline.json` are byte-identical to 1.0.0, and so is `workflow.json`;
- `manifest.json` differs only in `componentVersion` and `packagedAt`;
- the version-item binding fields are unchanged.

**Repro workflows, packaged again as 2.0.0:**
- `has_binding_points: true`, and `camera_input_nodes` is `[{node_id: input_source_1, node_type: aravis_camera_source, compiled_device_paths: {}}]`.
- Each document has one `aravisBinding` point with `nodeType: aravis_camera_source` and parameters `camera_id: Fake_1`, `gain: 4`, `exposure: 5000000`.
- The document without `bindingPoints` equals the 1.0.0 document, and the text is the 1.0.0 document plus `bindingPoints` in the packager's serialization (3.4).
- The binding-context view answers `binding_required: true` and lists the node.

**Deploying with no binding** is refused on both devices, with 409 `CAMERA_BINDINGS_INVALID` / `CAMERA_NODE_UNBOUND` naming `input_source_1` and the device.

**Deploying with a binding:**

| Device | Binding | Runs | Executor log |
|---|---|---|---|
| thor1, deployment `a0ef2daa` | `cfg-o70qz7ci`, its Basler; the node's typed id stayed `Fake_1` | 3 of 3 completed, each writing a 12.6 MB JPEG | "Aravis feed for node input_source_1 uses the device Image_Source configuration for camera 'Basler-267601652282-23405186'", then a feed planned at 4608×3288 |
| Orin, deployment `27f5c9d9` | `cfg-lyn5mwtf`, `Fake_1`; then a manual override, see below | 3 of 3 completed after the override, each writing a JPEG of about 60 KB | feed planned for `Fake_1` at 512×512 |

The thor1 log shows that the binding, not the typed id, chose the camera.

**The Orin's binding.** The device registered the workflow as invalid, "missing camera source cfg-lyn5mwtf", because of a pre-existing problem on the device (see Observations). A cameraSourceId binding cannot resolve there. The Portal's manual-override option failed as well (Observations).
- The test key in the Orin's `dda-camera-bindings` shadow was therefore set to `{"override": {"camera_id": "Fake_1"}}`. That is the shape the Portal delivers for an override. The device resolves it without its inventory, after checking it against the binding point's `nodeType`.
- The registration became `registered` within one watch cycle.
- This step bypassed the Portal, and only for the Orin. Thor1's run went through the Portal end to end.

**Soak.** From 01:34:49 to 01:44:08Z, 12 more runs were made on each device, each with a `/health` probe.
- 12 of 12 completed on each device, and every health probe answered 200.
- Neither backend restarted during the soak, and neither was killed for memory.

## Cleanup

- Each device's saved deployment was redeployed, removing the test workflow: thor1 `8d3e2d2e`, the Orin `5fa2217e`. Both COMPLETED at 01:50Z with the original component sets: thor1 21 components with 8 workflows, the Orin 13 with 4.
- On the devices, the test workflow and capture directories were removed, the test registrations are gone, and the test keys in `dda-camera-bindings` were nulled. The other keys are unchanged.
- In the Portal, all four test workflows were deleted. The two repro workflows answered 409 at first. After the workflow page's deployment listing synced their superseded deployments to INACTIVE, they deleted normally.
- The 8 component versions were deleted, and so were the 12 component zips in `ryvan-cookies`. No test objects remain in the Portal artifacts bucket.
- The four deployment records remain in `dda-portal-deployments` as INACTIVE history.
- The temporary principal is gone: 0 users, 0 `kiro-uicb-verify-*` clients and 0 role rows with its marker. The password, token and SSH login files are shredded.
- **Models.**
  - thor1: 9 of 9 READY, as found.
  - Orin: `yolo-test` and the vLLM model READY, as found. The deployment restarts had loaded the two Triton models that were not loaded when found, and the vLLM model was then refused by the device memory preflight. Those two Triton models were unloaded again, and the vLLM model was restarted. It was READY at 02:05:51Z.
- Both backends are healthy. Their last restart was at the cleanup deployment, 01:46Z.

## Observations (pre-existing, not caused by this change)

- **Every deployment revision restarts the LocalServer and the models on these devices.** Greengrass logs "dependency aws.greengrass.Cli was in a bad state" for the LocalServer, then restarts every model component because it depends on the LocalServer. That happened for each test deployment and for the cleanup.
- **The Orin cannot read its camera inventory.** `GET /image-sources` answers 500, and the watcher logs "Local camera inventory read failed … LookupError: 'RTSP' is not among the defined enum values". The database holds an RTSP Image_Source that LocalServer 1.0.72 does not know. Until that is resolved, every workflow bound by cameraSourceId registers as invalid on the Orin.
- **The Orin shows another session's activity.**
  - The device's camera-registry reports mention RTSP from at least 21:01Z on 2026-09-28.
  - Its backend was recreated at 21:35Z.
  - At 01:25:00Z today, the workflow and capture directories of `rtsp-verify-cont-people`, `rtsp-verify-cont-people-max` and `rtsp-verify-trig-rtsp` were removed, and their registrations were marked removed. None of this session's commands touched those paths.
- **A manual-override camera binding cannot be submitted through the Portal.** A workflow deployment with `{"override": …}` answers 500 "Failed to create workflow deployment". The log shows "No module named 'workflow_core'" from `validate_camera_bindings`, because override checking imports the `workflow_core` layer and the deployments Lambda does not attach it. Unit tests pass because the tests put the layer on the path.
