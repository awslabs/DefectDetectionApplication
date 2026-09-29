# Implementation Plan: camera-override-binding-500

## Overview

The fix is one line in `edge-cv-portal/infrastructure/lib/compute-stack.ts`: the `DeploymentsHandler` attaches the stack's existing `WorkflowCoreLayer` next to `sharedLayer`. The tests follow the repo's bugfix method:

1. a bug-condition exploration test that fails on the unfixed code;
2. preservation checks that pass on the unfixed code;
3. the fix, after which both pass.

The symptom is in the deployed Portal, so the fix is verified there, and on lab devices, before the commit. Results go in `verification-notes.md`.

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

- [ ] 3. Reproduce with today's Portal
  - [ ] 3.1 Create the temporary Portal principal. Portal_Identity enforcement is on, so it needs a global and a use-case role row.
  - [ ] 3.2 Create a test workflow with an `aravis_camera_source` feeding a capture, and package it for arm64_jp6 and arm64_jp7 only.
  - [ ] 3.3 On the Orin, a valid override (`camera_id: Fake_1`) and an invalid one (`gain: 500`) both answer 500, and no deployment is created.
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

- [ ] 6. Deploy the Portal backend
  - [ ] 6.1 Pre-checks. Build processes and stack status are checked again right before the deploy.
    - No component build running on this host. `EdgeCVPortalComputeStack` is `UPDATE_COMPLETE`; it was last updated at 01:01Z by the `unified-input-camera-binding` deploy.
    - `origin/integration/all-specs` is `62bb176`, this checkout's HEAD, so there is nothing to merge.
      - `origin/wip/rtsp-rtmp-stream-cameras-verify` (WIP, unverified, not deployed) makes the same `DeploymentsHandler` change, among others. Merging it later conflicts in that one hunk, where both sides agree.
    - The deployed `DeploymentsHandler` and `WorkflowPackagingHandler` code, `SharedLayer27DFABF0:89` and `WorkflowCoreLayer9FCF191C:40` equal this checkout: `diff -rq`, excluding `__pycache__` and the layer asset's excludes.
    - The function and both layers total 16.9 MiB unzipped, against Lambda's 250 MiB limit.
  - [x] 6.2 `cdk diff --all` with the deploy script's context (`cloudFrontDomain=d23v4ltibogb5x.cloudfront.net`, `portalRegistryEnforced=true`). One stack differs, `EdgeCVPortalComputeStack`:
    - `DeploymentsHandler70E83D88` `Layers` gains `WorkflowCoreLayer9FCF191C`;
    - `LambdaEnvUpdater` and `SageMakerEventBridgeIntegration` get their per-deploy `Timestamp`.
    - No IAM or security-group change, no function code or layer change, and the quick-setup bundle is unchanged.
  - [ ] 6.3 Run `deploy-infrastructure.sh`. Portal_Identity enforcement stays on, and the deployed `DeploymentsHandler` lists the `WorkflowCoreLayer`.
  - [ ] 6.4 Move `cdk.out` aside; the guard pair passes.

- [ ] 7. Verify on the Portal and devices
  - [ ] 7.1 The invalid override answers 409 `CAMERA_BINDINGS_INVALID`, with one `CAMERA_OVERRIDE_INVALID` naming the device, the node and `gain`. No deployment is created.
  - [ ] 7.2 The valid override answers 201 on the Orin, and its `dda-camera-bindings` shadow carries the override.
  - [ ] 7.3 On the Orin, the workflow registers and a Run grabs `Fake_1`.
  - [ ] 7.4 Preservation: on thor1, a `cameraSourceId` binding answers 201, the shadow carries the source, and a Run completes.
  - [ ] 7.5 Cleanup:
    - both devices' deployments restored;
    - test workflow, components, S3 zips, shadow keys and device directories removed;
    - the temporary principal removed;
    - models as found.
  - [ ] 7.6 Write `verification-notes.md`.
  - _Requirements: 2.1, 2.2, 2.3, 3.1_

- [ ] 8. Commit and push (approved 2026-09-29)
  - Another session deploys the Portal from the latest `integration/all-specs` after this deploy. So the fix is committed on `fix/camera-override-binding-500` and pushed to `integration/all-specs` before the deploy, stating what was verified locally.
  - `verification-notes.md` and the task outcomes follow in a second commit.
