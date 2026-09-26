# Implementation Plan: Run Detection Visibility

## Overview

Show object-detection results in the on-device deployed-workflow run views, on two surfaces:

- the results page: an overlay/original image toggle and a table of detected objects with confidence;
- the run-status graph: a `model_inference` preview.

Order of work:

1. The small additive backend, first.
2. The pure frontend layer.
3. The views.
4. Verification on `jetson-thor1`: a hot-patch first, then a built and deployed LocalServer component (`.kiro/steering/builds.md`).

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"], "description": "Backend: overlay_image_path, /results hasOverlayImage, /overlay-image route, tests." },
    { "wave": 2, "tasks": ["2"], "description": "Checkpoint: backend suites green (host workflow_engine + flask-app api-endpoints + gate replay)." },
    { "wave": 3, "tasks": ["3"], "description": "Frontend pure layer: API helpers, detections.ts, previewModel extension, property tests." },
    { "wave": 4, "tasks": ["4"], "description": "Frontend views: DetectedObjectsTable, RunResults, NodePreviewCard, RunStatusGraph, component tests." },
    { "wave": 5, "tasks": ["5"], "description": "Checkpoint: deployed-workflow frontend suites + production build green." },
    { "wave": 6, "tasks": ["6"], "description": "Hot-patch validation on jetson-thor1 with ppe-detector-v3-static-test." },
    { "wave": 7, "tasks": ["7"], "description": "USER ACTION: JP7 LocalServer build from a wip verify branch, deploy to jetson-thor1, re-verify, record notes." },
    { "wave": 8, "tasks": ["8"], "description": "USER ACTION: commit and merge on the owner's request." }
  ]
}
```

## Tasks

- [x] 1. Backend: serve the overlay image and report it
  - [x] 1.1 Add `overlay_image_path(output_dir, capture_id)` to `src/backend/workflow_engine/run_artifacts.py`
    - It returns `{output_dir}/{capture_id}.overlay.jpg` when the file exists; otherwise `None`, including for a missing `output_dir` or `capture_id`.
    - _Requirements: 4.1, 4.2, 4.5_
  - [x] 1.2 Add `hasOverlayImage` to the `/results` `output` entry in `src/backend/workflow_engine/api.py`
    - `hasOverlay` and the node entries are unchanged.
    - _Requirements: 4.4, 5.3_
  - [x] 1.3 Add `GET /workflows/executions/{execution_id}/overlay-image` to `src/backend/endpoints/download_file.py`
    - Place it on `unauthenticated_router`, with `validate_token_in_query_param(token)`.
    - Unknown execution → 404; `FileResponse(image/jpeg)`; missing file → 404.
    - _Requirements: 4.1, 4.2, 4.3, 4.5_
  - [x] 1.4 Tests
    - `test_workflow_run_results_api.py`:
      - helper cases;
      - the flag with overlay only, mask only, and both;
      - the exact-shape assertions, updated;
      - a Hypothesis property over base/overlay/mask presence (Property 1, ≥100 examples).
    - `test_triple_node_image_serving.py`: the exact-shape assertions, updated.
    - `api-endpoints/test_workflows_api.py`: overlay-image serves the file, unknown execution 404, missing file 404.
    - _Requirements: 4.1, 4.2, 4.4, 5.3_

- [x] 2. Checkpoint: backend
  - Run `test/backend-test/workflow_engine/` on the host (`~/.venvs/dda-portal-tests`, `PYTHONPATH=src/backend:test/backend-test`).
  - Run `api-endpoints/test_workflows_api.py`, plus the security gate replay, in the flask-app container.
  - Result, 2026-09-26:
    - Host `workflow_engine`: 1611 passed, 9 skipped. The touched files and the new `test_overlay_image_serving.py` add up to 41 passed.
    - Gate replay: green (every audit and exploration suite; preservation 138 passed, 8 skipped).
    - `api-endpoints/test_workflows_api.py` does not collect on the x86 flask-app image: `panorama` needs `GLIBCXX_3.4.32`. Its three new route tests therefore run only in the device-container suite.
    - `test_overlay_image_serving.py` covers the route on the host instead. It compiles the shipped route function from `download_file.py` with `ast`; a mutation check confirmed it fails when the route serves the wrong file.

- [x] 3. Frontend: pure layer
  - [x] 3.1 `src/frontend/src/api/WorkflowRegistrationAPI.ts`
    - Add `hasOverlayImage?: boolean` on `WorkflowExecutionResultImage`.
    - Add `workflowExecutionOverlayImageUrl(id, token?)`.
    - _Requirements: 1.1, 3.3, 4.1_
  - [x] 3.2 Create `src/frontend/src/components/deployed-workflow/detections.ts`
    - `RunDetection`, `runDetections`, `formatConfidence`, `formatBox`, `detectionLabelSummary`, `PREVIEW_DETECTION_LIMIT = 10`.
    - _Requirements: 2.1, 2.2, 2.4, 2.6, 2.8, 3.2_
  - [x] 3.3 Extend `graph/previewModel.ts`
    - Add `model_inference` to `OUTPUT_NODE_TYPES`.
    - Add the `detections` kind and the `overlayImageSrc` argument.
    - Extract the verdict-fields helper, shared with Bedrock.
    - _Requirements: 3.1, 3.2, 3.4, 3.5_
  - [x] 3.4 Tests
    - `detections.test.ts`: Property 2 and the formatters.
    - `previewModel.test.ts`: Property 4.
    - _Requirements: 2.8, 3.5_

- [x] 4. Frontend: views
  - [x] 4.1 Create `results/DetectedObjectsTable.tsx`
    - Columns: Index, Object, Confidence, Bounding box.
    - Header counter and label summary; collection-hooks sorting; empty state.
    - _Requirements: 2.1–2.5_
  - [x] 4.2 Update `results/RunResults.tsx`
    - Always-on metadata query.
    - Overlay-image mode, per the design table.
    - The table below the image, and in the no-results state.
    - _Requirements: 1.1–1.6, 2.6, 2.7, 5.1_
  - [x] 4.3 Update `graph/NodePreviewCard.tsx`
    - Render the `detections` kind: thumbnail with `onError` hide, compact list, "and K more", empty message.
    - _Requirements: 3.2, 3.3, 3.6_
  - [x] 4.4 Update `graph/RunStatusGraph.tsx`
    - Gate the metadata and results queries for a selected `model_inference` node.
    - Pass `overlayImageSrc`.
    - _Requirements: 3.1, 3.3, 3.7_
  - [x] 4.5 Tests
    - `DetectedObjectsTable.test.tsx`.
    - `RunResults.test.tsx`: overlay-image mode, labels, toggle, table, table without images; the mask tests unchanged.
    - `NodePreviewCard.test.tsx`: Property 5, extended, plus rendering.
    - `RunStatusGraph.preview.test.tsx`: model node list and thumbnail; query gating.
    - _Requirements: 1.1–1.6, 2.1–2.7, 3.1–3.7, 5.1, 5.2_

- [x] 5. Checkpoint: frontend
  - Run `CI=true npx react-scripts test --watchAll=false src/components/deployed-workflow` and the full frontend suite.
  - Run `npm run build`: types and lint.
  - Result:
    - deployed-workflow: 17 suites, 158 tests, all passed.
    - Full suite: 24 suites. One run gave 186/187. The failure is a pre-existing flake in the untouched `DeployedWorkflowDetails.exploration.test.tsx`: fast-check counterexample name `" !"`, where the heading trims the leading space. It passes on re-run.
    - `tsc`: clean. ESLint: clean on the changed files. `npm run build`: OK.

- [x] 6. Hot-patch validation on `jetson-thor1`
  - Copy `run_artifacts.py`, `api.py` and `download_file.py` into the backend container and restart it. Reload the Triton models it unloads.
  - Build the frontend with the device's build settings, and copy it into the frontend container's nginx root.
  - Trigger `ppe-detector-v3-static-test` and check:
    - `/results` reports `hasOverlayImage: true`;
    - `/overlay-image` serves the `.overlay.jpg` bytes;
    - `/output-image` still serves the `.jpg`;
    - the served bundle carries the new UI;
    - the backend stays healthy across repeated requests.
  - The hot-patch is lost when LocalServer recreates its containers. That is fine; task 7 replaces it.
  - _Requirements: 6.1, 6.2_
  - Result, 2026-09-26 (details in `verification-notes.md`):
    - All checks passed on `jetson-thor1`. The results page showed the overlay, then the original frame with the toggle off, and the objects table. The graph's `model_1` preview listed the objects with the overlay thumbnail.
    - Soak: 20 runs over 600 s, 80 of 80 route calls returned 200, backend healthy.
    - One native abort (`forkable.cc` fork check) happened 11 s after the deliberate restart, during vLLM start-up. It was recovered by Docker and was not seen again.

- [x] 7. USER ACTION: build, deploy and verify on hardware
  - Before the build, follow `.kiro/steering/builds.md`: no other build running, no portal deploy in flight, `cdk.out` moved aside, guard suite green, no preservation-tracked file changed.
  - Snapshot the change onto a `wip/run-detection-visibility-jp7-verify` branch and push it. Submit one JP7 LocalServer build through the portal build system.
  - Deploy the new `aws.edgeml.dda.LocalServer.arm64JP7` version to `jetson-thor1` as a revision that keeps every component and configuration.
  - Repeat the task 6 checks, plus the UI on the results page and the graph. Keep the backend healthy for a sustained period.
  - Record the results in `verification-notes.md`.
  - _Requirements: 6.1, 6.2, 6.3_
  - Result, 2026-09-26 (the owner approved the push, build and deploy):
    - Pre-build checks: no build running, no portal deploy in flight, no `cdk.out`, guard suite green (4 passed, 3 skipped), no preservation-tracked file changed.
    - `wip/run-detection-visibility-jp7-verify` = `2f40e88` was pushed.
    - Build `31771cf5` succeeded and published LocalServer 1.0.46.
    - Deployment `6f87c51a` was COMPLETED on `jetson-thor1`: 21 components carried over, only LocalServer bumped.
    - On the built component: 20 of 20 soak runs passed every route and byte check, the UI was re-verified, and the backend and device were healthy.
    - JP5 and JP6 were not verified on a device.

- [ ] 8. USER ACTION: commit on the owner's request
  - Commit to `spec/run-detection-visibility`, stating what was verified on which device, and merge into `integration/all-specs` when asked.
  - _Requirements: 6.3_

## Notes

- No preservation-tracked file changes, so no baseline rebaselining is expected. The build gate still runs; replay it before the build.
- The `/results` exact-shape tests change on purpose: `hasOverlayImage` is additive.
- Segmentation (mask) runs keep today's rendering exactly (D4).
