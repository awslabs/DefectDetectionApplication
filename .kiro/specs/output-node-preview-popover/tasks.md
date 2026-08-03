# Implementation Plan: Output Node Preview Popover

## Overview

Enrich the deployed-workflow run status graph's selected-node detail area into an output preview card (image thumbnail / LLM text snippet / Bedrock fields / publish status) with a "View full results" link, backed by one small additive metadata endpoint. Backend first (the frontend query needs it), then the pure view-model, then the card and wiring.

Shipping note: a component build is currently running — no build task is included; this change rides the next LocalServer build, and on-hardware/visual verification folds into that next build's checks.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1"],
      "description": "Backend: run_artifacts.read_run_metadata helper + GET .../metadata route with tests. No dependencies."
    },
    {
      "wave": 2,
      "tasks": ["2"],
      "description": "Checkpoint - backend test suite passes. Depends on wave 1."
    },
    {
      "wave": 3,
      "tasks": ["3"],
      "description": "Frontend pure layer: API client function + previewModel.ts with property tests. Depends on wave 2 (the client targets the new endpoint)."
    },
    {
      "wave": 4,
      "tasks": ["4"],
      "description": "Frontend UI: NodePreviewCard + RunStatusGraph wiring + component/property tests. Depends on wave 3."
    },
    {
      "wave": 5,
      "tasks": ["5"],
      "description": "Final checkpoint - full frontend suite passes (including pre-existing graph tests, preservation). Depends on wave 4."
    }
  ]
}
```

## Tasks

- [x] 1. Backend: serve the run metadata JSON
  - [x] 1.1 Add `read_run_metadata(output_dir, capture_id)` to `src/backend/workflow_engine/run_artifacts.py`
    - Parse `{output_dir}/{capture_id}.json`; return the dict when it parses to a JSON object
    - Return `{}` for missing `output_dir`/`capture_id`, missing/unreadable file, malformed JSON, or a non-object top level (contained, mirrors `parse_node_status`)
    - _Requirements: 4.1, 4.2_
  - [x] 1.2 Add `GET /workflows/executions/{execution_id}/metadata` to `src/backend/workflow_engine/api.py`
    - Reuse `_get_execution_or_404`; return `read_run_metadata(execution.output_dir, execution.capture_id)`
    - Additive route only; no existing route or response shape changes
    - _Requirements: 4.1, 4.2, 4.3, 5.3_
  - [ ]* 1.3 Write property test for the metadata read round trip
    - **Property 5: Run metadata read round trip**
    - hypothesis-generated JSON-serializable dicts written to `tmp_path`, read back equal; ≥100 iterations
    - New file `test/backend-test/workflow_engine/test_workflow_run_metadata_api.py`, mirroring `test_workflow_graph_node_status_api.py`
    - **Validates: Requirements 4.1**
  - [x] 1.4 Write unit tests for the metadata endpoint and helper edge cases
    - Route: 200 with parsed object for a known execution; 200 with `{}` when artifacts are missing; 404 for an unknown execution
    - Helper: missing output_dir/capture_id, missing file, malformed JSON, non-object top level each yield `{}`
    - _Requirements: 4.1, 4.2, 4.3_

- [x] 2. Checkpoint - backend tests
  - Ensure all tests pass (run the new backend test file plus the existing `test/backend-test/workflow_engine/` suite), ask the user if questions arise.

- [x] 3. Frontend: pure preview view-model
  - [x] 3.1 Add `getWorkflowExecutionMetadata` to `src/frontend/src/api/WorkflowRegistrationAPI.ts`
    - `GET {EXECUTIONS_ENDPOINT}/{id}/metadata` returning `WorkflowExecutionMetadata` (`Record<string, unknown>`)
    - _Requirements: 4.1_
  - [x] 3.2 Create `src/frontend/src/components/deployed-workflow/graph/previewModel.ts`
    - `OUTPUT_NODE_TYPES`, `isOutputNode`, `SNIPPET_MAX_LENGTH = 280`, `snippet`, `PreviewViewModel`, `previewViewModel` with the design's state precedence (none → pending → failure → type-specific → unavailable)
    - Defensive metadata path access (`llm[nodeId].generated_text` / `error`, top-level `is_anomalous`/`confidence`)
    - _Requirements: 1.1, 1.4, 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4_
  - [ ]* 3.3 Write property test for output-node classification
    - **Property 1: Output-node classification gates the preview**
    - fast-check over arbitrary type strings plus the six output types; ≥100 runs
    - **Validates: Requirements 1.1, 1.4**
  - [ ]* 3.4 Write property test for preview state precedence
    - **Property 2: Preview state precedence**
    - fast-check generators for output type × status entry × metadata presence/absence × image availability
    - **Validates: Requirements 2.4, 3.1, 3.2, 3.3**
  - [ ]* 3.5 Write property test for snippet truncation
    - **Property 3: Snippet truncation**
    - fast-check over arbitrary strings including >280-char strings
    - **Validates: Requirements 2.2, 2.5**

- [x] 4. Frontend: preview card component and graph wiring
  - [x] 4.1 Create `src/frontend/src/components/deployed-workflow/graph/NodePreviewCard.tsx`
    - Render each view-model kind per the design (failure keeps the existing `Alert type="error"` presentation; image thumbnail with `onError` → unavailable; text snippet; fields rows; status indicator; pending/loading/unavailable messages)
    - Always render the "View full results" link to `/deployed-workflows/{registrationId}/executions/{executionId}/results`
    - _Requirements: 1.3, 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 3.5_
  - [x] 4.2 Wire the preview into `RunStatusGraph.tsx`
    - Add the metadata query (enabled only for a selected `llm_inference`/`bedrock_inference` node with terminal status); build `imageSrc` via `workflowExecutionOutputImageUrl` + `useAuth` (RunResults pattern)
    - Branch the detail area: output nodes render `NodePreviewCard`; non-output nodes keep the exact pre-existing `Alert`/`Box` rendering
    - _Requirements: 1.1, 1.2, 1.4, 5.1, 5.2_
  - [ ]* 4.3 Write property test for the results link presence
    - **Property 4: Results link is present in every preview state**
    - fast-check over generated view-models, render `NodePreviewCard` in a `MemoryRouter`, assert the link href
    - **Validates: Requirements 1.3, 3.5**
  - [x] 4.4 Write component tests for `RunStatusGraph` preview behavior
    - Click an output node opens the preview card; second click closes it (1.1, 1.2)
    - Capture node shows the thumbnail sourced from `workflowExecutionOutputImageUrl` (2.1); llm node shows the snippet from mocked metadata (2.2); bedrock node shows fields (2.3); mqtt node shows status + detail (2.4)
    - In-flight run shows the "no output yet" placeholder (3.1); failed output node keeps the failure alert content (3.2, 5.2); missing metadata entry shows the fallback (3.3); metadata query in flight shows the loading indicator (3.4)
    - Preservation: non-output node selection renders the pre-existing detail; existing coloring/edge assertions untouched (1.4, 5.1, 5.2)
    - Extend `RunStatusGraph.test.tsx` patterns (mocked API module, `MemoryRouter` graph route)
    - _Requirements: 1.1, 1.2, 1.4, 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 5.1, 5.2_

- [x] 5. Final checkpoint - frontend suite
  - Run the frontend test suite (`CI=true npx react-scripts test --watchAll=false` in `src/frontend`) and ensure all tests pass, including the pre-existing `RunStatusGraph`/`graphGeometry` tests (preservation). Ask the user if questions arise.
  - _Requirements: 5.1, 5.2_

## Notes

- Tasks marked with `*` are optional property-test tasks and can be skipped for a faster MVP
- No build task: a component build is currently in progress; this change ships with the next LocalServer build, where visual/on-hardware verification folds in
- Decisions D1–D5 (preview-card-over-popover, output-node scope, single additive endpoint, always-visible results link, 280-char snippets) are documented in requirements.md
