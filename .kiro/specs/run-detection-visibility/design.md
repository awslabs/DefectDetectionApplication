# Design Document

## Overview

This feature surfaces detection results that deployed-workflow runs already write to disk, in the two on-device run views:

- the results page, as an overlay/original image toggle and a table of detected objects;
- the run-status graph, as a `model_inference` preview with an overlay thumbnail and a compact list.

The backend change is small and additive. The Detection_List comes from the existing `/metadata` route (decision D2). Nothing in the pipeline, executor, marshal or model packaging changes.

```
Backend (additive)
  workflow_engine/run_artifacts.py  + overlay_image_path(output_dir, capture_id)
  workflow_engine/api.py            /results output entry + hasOverlayImage
  endpoints/download_file.py        + GET /workflows/executions/{id}/overlay-image

Frontend
  api/WorkflowRegistrationAPI.ts    + hasOverlayImage?, + workflowExecutionOverlayImageUrl
  deployed-workflow/detections.ts   NEW pure: runDetections, formatConfidence, formatBox,
                                    detectionLabelSummary, PREVIEW_DETECTION_LIMIT
  results/DetectedObjectsTable.tsx  NEW Cloudscape table (collection-hooks sorting)
  results/RunResults.tsx            overlay-image mode + table; metadata fetched for every run
  graph/previewModel.ts             model_inference previewable; new "detections" kind
  graph/NodePreviewCard.tsx         renders "detections" (thumbnail + compact list)
  graph/RunStatusGraph.tsx          metadata + results queries for a selected model node
```

## Backend

### `run_artifacts.overlay_image_path(output_dir, capture_id) -> Optional[str]`

It returns `{output_dir}/{capture_id}.overlay.jpg` when that file exists, and `None` otherwise, including when `output_dir` or `capture_id` is missing. The path is built only from the execution record's fields, which the executor sets. Request input never reaches it (Requirement 4.5), the same as `base_output_image_path`.

### `/results`

The `output` entry becomes `{"kind": "output", "hasOverlay": <unchanged>, "hasOverlayImage": overlay_image_path(...) is not None}`. `hasOverlay` keeps its meaning ("an overlay or mask artifact exists"). Node entries are unchanged. The tests that assert the exact output-entry shape are updated to include the new key (Requirement 4.4).

### `GET /workflows/executions/{execution_id}/overlay-image`

This sits on `download_file.unauthenticated_router` beside `/output-image`, and its body is the same:

1. It calls `validate_token_in_query_param(token)`, the shared decision matrix: open when device auth is off; otherwise a valid bearer or Local_Session_Token is required (Requirement 4.3). A browser `<img>` cannot send an Authorization header, which is why these image routes carry the token in the query.
2. An unknown execution returns 404.
3. It returns `FileResponse(path, media_type="image/jpeg")` when `overlay_image_path` resolves, and 404 otherwise (Requirements 4.1, 4.2).

## Frontend

### API client

- `WorkflowExecutionResultImage.hasOverlayImage?: boolean` is present on the `output` entry.
- `workflowExecutionOverlayImageUrl(id, token?)` returns `${EXECUTIONS_ENDPOINT}/${id}/overlay-image`, adding `?token=` when auth is enabled. It mirrors `workflowExecutionOutputImageUrl`.

### `deployed-workflow/detections.ts` (pure)

```typescript
export interface RunDetection {
  id?: string;                                  // Detection_ID, when recorded
  label: string;                                // "object" when absent
  confidence: number;                           // finite, as recorded (0..1)
  box?: [number, number, number, number];       // x_min, y_min, x_max, y_max (px)
}
export const PREVIEW_DETECTION_LIMIT = 10;
export function runDetections(metadata?: WorkflowExecutionMetadata): RunDetection[] | null;
export function formatConfidence(confidence: number): string;          // "93.5%"
export function formatBox(box?: [number, number, number, number]): string; // "x 92–170, y 202–255" | "-"
export function detectionLabelSummary(detections: RunDetection[]): string; // "helmet 2 · human 5"
```

`runDetections` returns `null` unless `metadata.detections` is an array. That means "no Detection_List", so no table and no detections preview. Otherwise it returns the entries in order, with these rules (Requirement 2.8):

- Entries that are not objects, or lack a finite numeric `confidence`, are skipped.
- The label is the entry's non-empty string `label`, or `"object"`.
- The box is present only when all four coordinates are finite numbers.

It never throws. `detectionLabelSummary` counts labels and orders them by label, so the summary is stable across runs.

### `results/DetectedObjectsTable.tsx`

- A Cloudscape `Table` (`variant="container"`), sorted with `@cloudscape-design/collection-hooks` `useCollection` (sorting only). The package is already a dependency (Requirement 5.4).
- Columns:
  - **Index**: the 0-based Detection_List position, sortable (D7).
  - **Object**: the label, sortable.
  - **Confidence**: `formatConfidence`, sorted on the raw number.
  - **Bounding box (px)**: `formatBox`.
- The header is "Objects detected", with `counter="(N)"` and `description=detectionLabelSummary(...)`.
- The empty state reads "No objects were detected in this run." (Requirement 2.5).

### `results/RunResults.tsx`

- **Metadata query.** The metadata query is always enabled, not only when node frames exist. The table and the node sections share it.
- **Image mode.** It is chosen per run:

| Run | Condition | Image shown | Toggle |
|---|---|---|---|
| Mask | `maskImage` non-null | Original_Image + mask composite (today) | "Show anomaly masks" (today) |
| Overlay image | `hasOverlayImage` and the `/overlay` query settled with `maskImage` null | toggle on: Overlay_Image; off: Original_Image | "Show bounding boxes" when `runDetections` is non-null, else "Show overlay" |
| Neither | anything else, including while `/overlay` is loading | Original_Image | none |

The overlay-image mode reuses the existing `showMask` state (default `true`) and `RefreshDisplayActions` with `toggleLabel`. This is the same pairing `live-result/ResultsLayout.tsx` uses for detections (D1). It passes no mask props, so no chroma-key canvas is drawn over the server overlay. The existing "overlay could not be loaded; showing the base image" warning appears only outside overlay-image mode, where it is still true.

- **Table placement.** The Detected_Objects_Table renders below the output container when `runDetections` is non-null. It also renders in the no-results state (Requirement 2.7). The loading and error states for the results request are otherwise unchanged (Requirement 5.1).

### `graph/previewModel.ts`

- `"model_inference"` is added to `OUTPUT_NODE_TYPES` (Requirement 3.1). This extends the output-node-preview-popover D2 set. Existing Property 1 ("`none` exactly when not in the set") holds as written.
- New view-model kind: `{ kind: "detections"; detections: RunDetection[]; imageSrc?: string }`.
- New optional argument `overlayImageSrc?: string`.
- `model_inference` follows the existing precedence (Requirement 3.5): not previewable → `none`; non-terminal → `pending`; `failure` → `failure`; then:
  - `metadataLoading` → `loading`;
  - `metadataError` → `unavailable`;
  - `runDetections(metadata)` non-null → `detections`, with `imageSrc` set to `overlayImageSrc` when given;
  - otherwise, `is_anomalous` / `confidence` → `fields`, through a helper shared with the unchanged Bedrock branch;
  - otherwise → `unavailable` (Requirement 3.4).

### `graph/NodePreviewCard.tsx`

The `detections` kind renders:

- the optional thumbnail, `data-testid="preview-overlay-thumbnail"`, with the same size box as the capture thumbnail. On `onError` only the thumbnail is hidden; the list stays (Requirement 3.3).
- the list, `data-testid="preview-detections"`:
  - a label, "Objects detected in this run (N)";
  - the first `PREVIEW_DETECTION_LIMIT` entries as "label — 93.5%";
  - "and K more" when entries are truncated;
  - "No objects were detected." for an empty list.

The footer link is untouched (Requirement 3.6).

### `graph/RunStatusGraph.tsx`

- `metadataEnabled` also covers a selected `model_inference` node with a Terminal_Status.
- A results query, keyed `["getWorkflowExecutionResults", executionId]` so the results page can reuse it, is enabled only when all of these hold (Requirement 3.7):
  - the selected node is `model_inference`;
  - its status is `success` or `warning`;
  - the execution reports `hasImageResults`.
- When the `output` entry reports `hasOverlayImage`, `overlayImageSrc` is `workflowExecutionOverlayImageUrl(executionId, token-when-auth)`.

## Correctness Properties

### Property 1: Results flags track the artifacts

For every combination of base, overlay and mask files present, the `/results` `output` entry (present when the base exists) has:

- `hasOverlayImage === overlay present`;
- `hasOverlay === (overlay present || mask present)`, which is unchanged.

**Validates: Requirements 4.4, 5.3**

### Property 2: Detection-list parsing is total and faithful

For arbitrary JSON metadata, `runDetections` never throws, and returns `null` exactly when `detections` is not an array. For an array of well-formed entries, it returns them in order, with equal labels, confidences, boxes and ids. Malformed entries are dropped and no other entry changes.

**Validates: Requirements 2.1, 2.6, 2.8**

### Property 3: Image mode selection

For every combination of mask presence, overlay-image presence, overlay-query state and toggle state, the image source and mask props follow the table above:

- a mask always wins;
- the Overlay_Image is shown only when the toggle is on and the mask query has settled without a mask;
- otherwise the Original_Image is shown.

**Validates: Requirements 1.1, 1.2, 1.4, 1.5, 1.6**

### Property 4: `model_inference` preview precedence

For any status entry and metadata:

- missing or non-terminal status → `pending`;
- `failure` → `failure` with its detail;
- terminal with metadata loading → `loading`;
- terminal with metadata error → `unavailable`;
- terminal with a Detection_List → `detections`, carrying exactly `runDetections(metadata)`, and `imageSrc === overlayImageSrc`;
- otherwise → `fields` or `unavailable`.

**Validates: Requirements 3.2, 3.4, 3.5**

### Property 5: Results link in every state

The existing Property 4 of output-node-preview-popover, with the arbitrary view-models extended by the `detections` kind: the "View full results" link is always rendered.

**Validates: Requirement 3.6**

## Error Handling

- **Backend.** Both helpers are contained. A missing file yields `None`/404, never 500. The route adds no new failure mode beyond `/output-image`'s.
- **Frontend.**
  - Malformed Detection_List entries are skipped.
  - A metadata error hides the table and maps the preview to `unavailable`.
  - A results query error leaves the preview without a thumbnail.
  - An overlay image that fails to load, on the results page, leaves the zoom controls disabled by `InteractableImage`, as for any image. The toggle still reaches the Original_Image.

## Testing Strategy

- **Backend (pytest).**
  - `test/backend-test/workflow_engine/test_workflow_run_results_api.py`: the helper, the new flag, and a Hypothesis property for Property 1 (≥100 examples); the updated exact-shape assertions.
  - `test_triple_node_image_serving.py`: its exact-shape assertions, updated.
  - `api-endpoints/test_workflows_api.py`: route tests (serves the file, unknown execution 404, missing file 404), mirroring the `/output-image` tests.
  - Where they run: the workflow_engine tests on the host venv, and the api-endpoints file in the flask-app container (it imports the full app).
- **Frontend (jest + React Testing Library + fast-check).**
  - `detections.test.ts`: Property 2, plus the formatters.
  - `DetectedObjectsTable.test.tsx`: rows, header and summary, empty state, sorting.
  - `RunResults.test.tsx`: Property 3 cases, the table, the table with no images, and the existing mask tests unchanged.
  - `previewModel.test.ts`: Property 4.
  - `NodePreviewCard.test.tsx`: the Property 5 arbitrary extended, plus the list, limit and thumbnail rendering.
  - `RunStatusGraph.preview.test.tsx`: selecting a model node shows the list and thumbnail; lazy query gating.
  - Command: `CI=true npx react-scripts test --watchAll=false src/components/deployed-workflow`, then `npm run build` for type and lint checks.
- **Device.**
  1. Hot-patch `jetson-thor1`: copy the three backend modules into the backend container and restart it, then copy the built bundle into the frontend container's nginx root. Exercise the `ppe-detector-v3-static-test` workflow.
  2. Then build the JP7 LocalServer component, deploy it to `jetson-thor1`, and repeat (Requirement 6).
