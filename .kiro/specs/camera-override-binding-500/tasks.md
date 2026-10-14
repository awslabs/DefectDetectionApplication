# Implementation Plan: camera-override-binding-500

## Overview

The fix is one line in `edge-cv-portal/infrastructure/lib/compute-stack.ts`: the `DeploymentsHandler` attaches the stack's existing `WorkflowCoreLayer` next to `sharedLayer`. The tests follow the repo's bugfix method:

1. a bug-condition exploration test that fails on the unfixed code;
2. preservation checks that pass on the unfixed code;
3. the fix, after which both pass.

The symptom is in the deployed Portal, so the fix is verified there and on lab devices. Another session deploys from `integration/all-specs` after this one, so the fix was pushed right before the deploy, after the local checks (task 8). Results are in `verification-notes.md`.

**Approved on 2026-09-29:**
- a temporary Portal principal for tasks 3 and 7: a throwaway app client, a temp user and scoped role rows, all deleted afterwards;
- test workflow deployments to two lab devices, restored afterwards;
- the Portal backend deploy in task 6, after pulling and merging the latest code;
- pushing the fix to `integration/all-specs` (task 8).

**Preservation-tracked files.** None. `compute-stack.ts` is not hash-pinned. The IAM synth gate covers the ComputeStack's grants, and the fix adds none.

**Layer contents.** The two layers share no top-level module: `WorkflowCoreLayer` adds `workflow_core`, `jsonschema`, `referencing`, `rpds`, `attrs` and `typing_extensions`, and its asset excludes `__pycache__`. Nothing that `deployments.py` already imports is shadowed. `WorkflowPackagingHandler` runs the same code asset with the same two layers, in the same order.

**Devices.**
- The Orin AGX (JP6) for the override. It cannot read its own camera inventory, so an override is the only binding it can resolve.
- thor1 (JP7) for the `cameraSourceId` control, bound to its registered Basler.

## Tasks

- [x] 1. Write the bug-condition exploration test
  - **Property 1: Bug Condition.** The function that validates override bindings runs without the `workflow_core` package.
  - `edge-cv-portal/infrastructure/test/deployments-workflow-core-layer.test.ts` (jest, CDK assertions):
    - `deployments.handler` attaches `SharedLayer` and the same `WorkflowCoreLayer` Ref as `workflow_packaging.handler`;
    - every ComputeStack function whose handler module in `backend/functions` imports `workflow_core`, at module level or inside a function, attaches the `WorkflowCoreLayer`.
  - **Outcome on the unfixed code:** both FAILED. `deployments.handler` had only `SharedLayer27DFABF0`, and it was the only offender among the six importers.
  - **Build first.** Jest resolves `../lib/compute-stack` to the compiled `lib/compute-stack.js` when it exists: the default `moduleFileExtensions` lists `js` before `ts`. That file is the gitignored `tsc` output of the last `npm run build`. Run `npm run build` before the infra tests, or they test the previous build.
  - _Requirements: 1.1, 1.2, 1.3, 2.3_

- [x] 2. Record the preservation baseline before the fix
  - **Property 2: Preservation.**
    - The unfixed Storage, Compute and nested-stack templates were synthesized and kept. Two synths of the same code differ only in the per-synth `Timestamp` of `LambdaEnvUpdater` and `SageMakerEventBridgeIntegration`.
    - The existing infra jest suite passed: 24 suites, 267 tests. The IAM synth gate passed (11), and so did the guard pair.
    - 13 Python camera-binding and override modules passed: 97 tests.
  - **In the Lambda runtime.** The deployed `DeploymentsHandler` code and its layer were downloaded and cold-imported in `public.ecr.aws/lambda/python:3.11` (linux/amd64, no network).
    - With only `SharedLayer` in `/opt`, the module imports (3.3). Every `_override_errors` call raises `ModuleNotFoundError: No module named 'workflow_core'`, the production error.
    - With `SharedLayer` and then `WorkflowCoreLayer` in `/opt`, a valid override has no errors. `gain: 500` gives one `CAMERA_OVERRIDE_INVALID` (`PARAM_MAX`) naming the device, the node and `gain`, and an undeclared parameter gives one too.
  - _Requirements: 3.1, 3.2, 3.3_

- [x] 3. Reproduce with today's Portal
  - [x] 3.1 Created the temporary Portal principal, `kiro-cob500-verify-1790685494`. Portal_Identity enforcement is on, so it got a global Viewer row and a use-case UseCaseAdmin row.
  - [x] 3.2 Created two test workflows, each an `aravis_camera_source` (`Fake_1`) feeding a capture, one per device.
    - `cob500-override-orin` was packaged for arm64_jp6 only, and `cob500-control-thor1` for arm64_jp7 only.
    - Both validated with no findings.
  - [x] 3.3 On the Orin, the valid override and the `gain: 500` one both answered 500 `INTERNAL_ERROR` (12:39:55 and 12:39:57).
    - The log shows `No module named 'workflow_core'` at `deployments.py` line 4240.
    - No Greengrass deployment, no deployment record and no shadow change were created.
  - _Requirements: 1.1, 1.2_

- [x] 4. Fix
  - [x] 4.1 In `compute-stack.ts`, set the `DeploymentsHandler`'s `layers` to `[sharedLayer, workflowCoreLayer]`, with a comment saying why.
    - The hunk is byte-identical to the one on `wip/rtsp-rtmp-stream-cameras-verify`, so merging that branch stays clean; a legacy `git merge-tree` of the two gives no conflict.
    - The comment's `stream_url` clause describes that branch's stream node types.
  - [x] 4.2 After `npm run build`, the exploration test passes: 2 of 2.
  - [x] 4.3 The only template difference from the task 2 baseline is `/Resources/DeploymentsHandler70E83D88/Properties/Layers`, which gains `{"Ref": "WorkflowCoreLayer9FCF191C"}`. That excludes the two per-synth timestamps. The seven nested-stack, Storage and Deps templates are identical.
  - _Requirements: 2.3, 3.2_

- [x] 5. Checkpoint
  - The full infra jest suite: 25 suites, 269 tests.
  - The IAM synth gate (11) and the guard pair plus the IAM and S3 out-of-scope guards (7 passed, 4 skipped) passed on the host, as did `test_iam_bug_condition_exploration.py` (24).
  - The 13 Python camera-binding and override modules (97) passed. So did the seven backend modules that read `compute-stack.ts` (103).

- [x] 6. Deploy the Portal backend
  - [x] 6.1 Pre-checks, repeated right before the deploy.
    - No component build or Portal stack update was in progress. `EdgeCVPortalComputeStack` was last updated at 01:01Z, by the `unified-input-camera-binding` deploy.
    - `origin/integration/all-specs` was `76c6d9b`, this fix, pushed at 12:33Z; this checkout is the same commit.
      - `origin/wip/rtsp-rtmp-stream-cameras-verify` (WIP, unverified, not deployed) makes the same `DeploymentsHandler` change, among others. See 4.1: it merges cleanly.
    - The deployed `DeploymentsHandler` and `WorkflowPackagingHandler` code, `SharedLayer27DFABF0:89` and `WorkflowCoreLayer9FCF191C:40` equal this checkout: `diff -rq`, excluding `__pycache__` and the layer asset's excludes.
    - The function and both layers total 16.9 MiB unzipped, against Lambda's 250 MiB limit.
  - [x] 6.2 `cdk diff --all` with the deploy script's context (`cloudFrontDomain=d23v4ltibogb5x.cloudfront.net`, `portalRegistryEnforced=true`). One stack differs, `EdgeCVPortalComputeStack`:
    - `DeploymentsHandler70E83D88` `Layers` gains `WorkflowCoreLayer9FCF191C`;
    - `LambdaEnvUpdater` and `SageMakerEventBridgeIntegration` get their per-deploy `Timestamp`.
    - No IAM or security-group change, no function code or layer change, and the quick-setup bundle is unchanged.
  - [x] 6.3 Ran `deploy-infrastructure.sh` from 12:41 to 12:50Z. All 8 stacks succeeded, and only `EdgeCVPortalComputeStack` changed.
    - Enforcement stayed on: 54 handlers `true`.
    - `DeploymentsHandler` lists `SharedLayer27DFABF0:89` and `WorkflowCoreLayer9FCF191C:40`, the same as `WorkflowPackagingHandler`, and its code is unchanged.
  - [x] 6.4 Moved `cdk.out` to `cdk.out.bak-20260929T125137Z`; the guard pair passes.

- [x] 7. Verify on the Portal and devices
  - [x] 7.1 `gain: 500` answers 409 `CAMERA_BINDINGS_INVALID`, with one `CAMERA_OVERRIDE_INVALID` (`PARAM_MAX`) naming the device, `cam` and `gain`. An undeclared `shutter` answers the same way, and neither creates a deployment.
  - [x] 7.2 The valid override answers 201 on the Orin: deployment `f4a2b8f9`, rev 94, which adds only the workflow component. The `dda-camera-bindings` shadow carries `{"cam": {"override": {"camera_id": "Fake_1"}}}`.
  - [x] 7.3 On the Orin, the workflow registers, and 3 of 3 Runs complete: "Aravis frame feed planned for node cam: camera 'Fake_1' (512x512)".
  - [x] 7.4 Preservation, on thor1: `cameraSourceId: cfg-o70qz7ci` answers 201 (`1e22af7a`, rev 107), and the shadow carries the source. 3 of 3 Runs complete on its Basler, at 4608×3288.
  - [x] 7.5 Cleanup:
    - both devices' saved deployments were redeployed (the Orin rev 95, thor1 rev 108); component maps, policies and `desired` bindings are identical to the pre-test state;
    - the test workflows, component versions, S3 zips, shadow keys, device directories and registrations were removed;
    - the temporary principal was removed, and its files and the device password file were shredded;
    - models as found: thor1 9 of 9 READY; the Orin's `yolo-test` and vLLM model READY, and its two unloaded Triton models UNAVAILABLE again.
  - [x] 7.6 Wrote `verification-notes.md`.
  - _Requirements: 2.1, 2.2, 2.3, 3.1_

- [x] 8. Commit and push (approved 2026-09-29)
  - Another session deploys the Portal from the latest `integration/all-specs` after this deploy. So the fix was committed on `fix/camera-override-binding-500` and pushed to `integration/all-specs` before the deploy, as `76c6d9b` at 12:33Z. The commit message states what had been verified locally.
  - `verification-notes.md` and these outcomes follow in a second commit.
