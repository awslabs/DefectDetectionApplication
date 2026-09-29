# Verification notes: camera-override-binding-500

All times are UTC on 2026-09-29. The fix (`76c6d9b`) was pushed to `integration/all-specs` at 12:33, before the deploy, so another session could pull it before deploying.

## Local

| Check | Result |
|---|---|
| `test/deployments-workflow-core-layer.test.ts`, unfixed code | 2 failed: `deployments.handler` is the only one of the six `workflow_core`-importing handlers without the layer |
| The same, fixed code, after `npm run build` | 2 passed |
| Synthesized Storage, Compute and nested-stack templates, unfixed against fixed | one difference: `DeploymentsHandler70E83D88` `Layers` gains `WorkflowCoreLayer9FCF191C` (per-synth timestamps aside) |
| Infra jest suite | 25 suites, 269 tests passed |
| IAM synth gate; guard pair plus IAM and S3 out-of-scope guards | 11 passed; 7 passed, 4 skipped |
| Python camera-binding and override modules; modules that read `compute-stack.ts` | 97 passed; 103 passed |
| The deployed code in `public.ecr.aws/lambda/python:3.11` (amd64, no network), `SharedLayer` only | the module imports; every override check raises `No module named 'workflow_core'` |
| The same with `SharedLayer`, then `WorkflowCoreLayer` | valid override: no errors; `gain: 500`: `CAMERA_OVERRIDE_INVALID` (`PARAM_MAX`); undeclared parameter: `CAMERA_OVERRIDE_INVALID` |

## Before the deploy, on today's Portal

**Setup.**
- **Temporary principal.** `kiro-cob500-verify-1790685494` on a throwaway app client. Its role rows were global Viewer and use-case UseCaseAdmin.
- **Workflows.** Two, each an `aravis_camera_source` (`camera_id: Fake_1`) feeding a capture. Both validated with no findings.
  - `cob500-override-orin` (`ad481ae5…`), packaged for arm64_jp6 only.
  - `cob500-control-thor1` (`29c798d2…`), packaged for arm64_jp7 only.
- **Binding context.** Both devices answer `binding_required: true`, and their registries are synced.

**On the Orin, both overrides answered 500** `INTERNAL_ERROR` "Failed to create workflow deployment":
- `{"override": {"camera_id": "Fake_1"}}` at 12:39:55;
- the same with `gain: 500` at 12:39:57.

The DeploymentsHandler log has `Error creating workflow deployment: No module named 'workflow_core'` at `deployments.py` line 4240 for each. No Greengrass deployment was created: the latest was still `5fa2217e`, rev 93. There was no deployment record, and the bindings shadow was unchanged.

## Deploy

**Pre-checks.**
- No component build or stack update was in progress.
- `origin/integration/all-specs` was `76c6d9b`, this checkout.
- The deployed functions and layers matched the tree.
- `cdk diff` showed only the `DeploymentsHandler` layer and the two per-deploy timestamps.

**`deploy-infrastructure.sh`** ran from 12:41:06 to 12:50:43.
- All 8 stacks succeeded, and only `EdgeCVPortalComputeStack` changed. Enforcement stayed on: 54 handlers `true`.
- `DeploymentsHandler` was updated at 12:47:19. It now lists `SharedLayer27DFABF0:89` and `WorkflowCoreLayer9FCF191C:40`, the same as `WorkflowPackagingHandler`, and its code sha256 is unchanged.
- `cdk.out` was moved to `cdk.out.bak-20260929T125137Z`, and the guard pair passed afterwards.

## After the deploy

**Invalid overrides on the Orin** answer 409 `CAMERA_BINDINGS_INVALID` "the deployment was not submitted", and nothing is created:
- `gain: 500` gives one `CAMERA_OVERRIDE_INVALID`: device `ryanorinagxdevkithomelabjp622`, node `cam`, parameter `gain`, `PARAM_MAX`, "value 500 is above the maximum 100";
- `shutter: 1` gives one `CAMERA_OVERRIDE_INVALID`: `shutter` "is not a declared parameter".

**Deployments with a binding:**

| Device | Binding | Portal | Shadow `desired.bindings["<wf>/1"]` | Runs |
|---|---|---|---|---|
| Orin, rev 94 `f4a2b8f9`, COMPLETED 12:56:00 | `{"override": {"camera_id": "Fake_1"}}` | 201, `camera_bindings_delivered: true` | `{"cam": {"override": {"camera_id": "Fake_1"}}}` | 3 of 3 completed, JPEGs of 59–68 KB; "Aravis frame feed planned for node cam: camera 'Fake_1' (512x512)" |
| thor1, rev 107 `1e22af7a`, COMPLETED 12:57:50 | `{"cameraSourceId": "cfg-o70qz7ci"}` | 201, `camera_bindings_delivered: true` | `{"cam": {"cameraSourceId": "cfg-o70qz7ci"}}` | 3 of 3 completed, JPEGs of 12.6 MB; "uses the device Image_Source configuration for camera 'Basler-267601652282-23405186'", feed planned at 4608×3288 |

- Each deployment added only the workflow component. Policies, job configuration and the other shadow keys were unchanged.
- Both workflows registered as `registered`.
- The Orin cannot resolve a `cameraSourceId` binding (see Observations), so this override is the only binding that works there. The thor1 row is the preservation control: a `cameraSourceId` binding behaves as before.

## Cleanup

- **Devices.** Each device's saved deployment was redeployed: the Orin as rev 95 `c07bca0c`, thor1 as rev 108 `a4870e21`. Both COMPLETED at 13:04.
  - The component maps and policies are identical to the pre-test deployments: the Orin 13 components with 4 workflows, thor1 21 with 8.
  - The test keys in `dda-camera-bindings` were nulled. `desired` is identical to the pre-test shadow on both devices.
  - The test workflow and capture directories were removed, and both registrations show `removed`.
  - `/aws_dda/workflows` matches the pre-test listing, and no `/tmp/cob500-*` files remain.
- **Portal.**
  - Both workflows were deleted after their deployment records were listed. Their component versions and the two zips in `ryvan-cookies` were deleted.
  - Nothing remains in the Portal artifacts bucket or the workflow tables.
  - The records `f4a2b8f9` and `1e22af7a` remain in `dda-portal-deployments` as history.
- **Temporary principal.** Gone: 0 users, 0 clients and 0 role rows with its marker. The password, token and device password files are shredded.
- **Models, as found.**
  - thor1: 9 of 9 READY. The deployment restarts left 8 Triton models UNKNOWN, and each was started.
  - Orin: `yolo-test` and the vLLM model READY; `cookies-binary` and `rf-detr-seg-nano` UNAVAILABLE. The restarts had loaded those two, and the vLLM model FAILED the memory preflight. The two were stopped, and the vLLM component was restarted; it was READY at 13:09.
  - Both backends answer `/health` 200.

## Observations (pre-existing, not caused by this change)

- **Every deployment revision restarts the LocalServer and all models on these devices.** It happened again for each test deployment and each restore.
- **The Orin cannot read its camera inventory.** An RTSP Image_Source row breaks `GET /image-sources`, so `cameraSourceId` bindings register invalid there. This is recorded in `unified-input-camera-binding`.
- **Two of the Orin's own workflows are registered invalid:** `0c7fe31a…:6` and `f81a4c66…:11`, since 2026-09-25T01:34.
  - Reason: "Plugin checksum verification failed: dda.plugin.7878501d…/resize-image.so … checksum mismatch".
  - The watcher logs it about 1,400 times an hour, as far back as the oldest kept log (2026-09-28T11:27). It was left alone.
- **The infra jest tests import compiled `lib/*.js` ahead of the `.ts` source.** Jest's default `moduleFileExtensions` lists `js` first, and `npm run build` writes those gitignored files next to the sources. The tests therefore check the last build unless `npm run build` runs first.
