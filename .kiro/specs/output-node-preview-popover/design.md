# Design Document

## Overview

Enhance the deployed-workflow run status graph so selecting an output node shows an output preview and a "View full results" link. The feature is almost entirely frontend; one small additive backend endpoint (`GET /workflows/executions/{execution_id}/metadata`) is added because the run metadata JSON (`{output_dir}/{capture_id}.json`, the LLM `generated_text` source) is written to disk by `pipeline_executor._persist_run_metadata` but not served by any existing route.

Key design decisions (carried from requirements D1–D5):

- **Detail-area preview card, not a floating popover (D1).** The graph canvas is an `overflow: auto` container with absolutely-positioned node buttons; a floating Cloudscape `Popover` would be clipped by the canvas and its uncontrolled trigger model conflicts with the existing controlled `selectedNodeId` toggle. The graph already renders a selected-node detail area below the canvas — we enrich that area into a preview card, preserving all existing selection semantics, failure/warning alerts, and their tests.
- **Pure view-model selector.** All preview logic (which node types get a preview, state precedence, content extraction, snippet truncation) lives in a new pure module `previewModel.ts` beside `graphGeometry.ts`, mirroring the existing pattern of keeping testable logic out of the React component. `RunStatusGraph.tsx` only wires the selector to react-query and Cloudscape.
- **Data sources.** Capture thumbnail: existing `workflowExecutionOutputImageUrl` (token-aware). Publish node status/detail: the already-fetched node-status map. LLM text and Bedrock fields: the new metadata endpoint, fetched lazily (only when an output node needing metadata is selected). Execution (already fetched) supplies `hasImageResults`.

## Architecture

```
RunStatusGraph.tsx (existing screen)
├── existing queries: execution, graph, node-status
├── NEW query: getWorkflowExecutionMetadata (enabled only when the selected
│   node's preview needs metadata: llm_inference / bedrock_inference)
├── previewModel.ts (NEW, pure)
│   ├── isOutputNode(type)
│   ├── snippet(text)                     — 280-char truncation with ellipsis
│   └── previewViewModel(node, statusEntry, execution, metadata, metadataState)
│         → { kind: "none" | "pending" | "failure" | "image" | "text"
│                  | "fields" | "status" | "unavailable" | "loading", ... }
└── NodePreviewCard.tsx (NEW, presentational)
      renders the view-model + always renders the results link

Backend (additive):
workflow_engine/run_artifacts.py  + read_run_metadata(output_dir, capture_id)
workflow_engine/api.py            + GET /workflows/executions/{id}/metadata
```

### Backend endpoint

`GET /workflows/executions/{execution_id}/metadata`

- 404 for an unknown execution (mirrors `_get_execution_or_404`).
- Otherwise returns `run_artifacts.read_run_metadata(execution.output_dir, execution.capture_id)`: the parsed `{output_dir}/{capture_id}.json` when it exists and parses to a JSON object; `{}` (200, never 500) for a missing `output_dir`/`capture_id`, missing/unreadable file, malformed JSON, or a non-object top level — the same best-effort containment style as `parse_node_status` / `read_run_log`.

### Frontend API client

Add to `WorkflowRegistrationAPI.ts`:

```typescript
/** The run's metadata JSON ({capture_id}.json), or {} when unavailable. */
export type WorkflowExecutionMetadata = Record<string, unknown>;

export async function getWorkflowExecutionMetadata(
  id: string,
): Promise<WorkflowExecutionMetadata> {
  const { data } = await axios.get<WorkflowExecutionMetadata>(
    `${EXECUTIONS_ENDPOINT}/${id}/metadata`,
  );
  return data;
}
```

## Components and Interfaces

### previewModel.ts (pure)

```typescript
/** Node types that get an output preview (Requirements D2 scope). */
export const OUTPUT_NODE_TYPES = new Set([
  "capture",
  "llm_inference",
  "bedrock_inference",
  "mqtt_publish",
  "opcua_write",
  "digital_output",
]);

export function isOutputNode(type: string): boolean;

/** Max snippet length before ellipsis truncation (D5). */
export const SNIPPET_MAX_LENGTH = 280;

/** Truncate to SNIPPET_MAX_LENGTH chars, appending "…" when truncated. */
export function snippet(text: string): string;

export type PreviewViewModel =
  | { kind: "none" }                                   // not an output node
  | { kind: "pending" }                                // non-terminal status (R3.1)
  | { kind: "failure"; detail?: string }               // failure status (R3.2)
  | { kind: "loading" }                                // metadata query in flight (R3.4)
  | { kind: "image"; src: string }                     // capture (R2.1)
  | { kind: "text"; text: string }                     // llm_inference (R2.2, snippet applied)
  | { kind: "fields"; fields: [string, string][] }     // bedrock_inference (R2.3)
  | { kind: "status"; status: string; detail?: string }// publish types (R2.4)
  | { kind: "unavailable" };                           // missing data (R3.3)

export function previewViewModel(args: {
  nodeType: string;
  nodeId: string;
  statusEntry?: NodeRunStatus;
  hasImageResults: boolean;
  imageSrc: string;
  metadata?: WorkflowExecutionMetadata;
  metadataLoading: boolean;
  metadataError: boolean;
}): PreviewViewModel;
```

State precedence inside `previewViewModel` (evaluated in order):

1. Not an output type → `none`.
2. Status absent or `pending`/`running` → `pending` (in-flight run; R3.1).
3. Status `failure` → `failure` with the node's detail (R3.2, preserves existing alert content).
4. Type-specific extraction (status is `success` or `warning`):
   - `capture`: `hasImageResults` → `image` with `imageSrc`; else `unavailable`.
   - `llm_inference`: `metadataLoading` → `loading`; metadata `llm[nodeId].generated_text` (non-empty string) → `text` with `snippet(...)`; `llm[nodeId].error` → `unavailable` (the error also surfaces via node status); else → `unavailable`.
   - `bedrock_inference`: `metadataLoading` → `loading`; metadata has `is_anomalous` or `confidence` → `fields`; else `unavailable`.
   - `mqtt_publish` / `opcua_write` / `digital_output`: → `status` with the node's status and detail (no fetch needed).
5. Metadata request error (`metadataError`) for metadata-backed types → `unavailable` (R3.3).

### NodePreviewCard.tsx (presentational)

Cloudscape `Container` with a header (`{nodeId} — output preview`) rendered in the existing detail area below the canvas, replacing the plain `Box`/`Alert` for output nodes:

- `kind: "failure"` → the existing `Alert type="error"` presentation (same header/detail text as today) inside the card (R3.2 / R5.2).
- `kind: "image"` → an `<img>` thumbnail (`max-width: 320px`, `max-height: 200px`, `object-fit: contain`) with an `onError` fallback to the unavailable message (R3.3).
- `kind: "text"` → the snippet in a `Box`.
- `kind: "fields"` → Cloudscape `KeyValuePairs`-style rows (plain markup, no new deps).
- `kind: "status"` → `StatusIndicator` + detail text.
- `kind: "pending"` → "No output yet — the run is still in progress."
- `kind: "loading"` → `Spinner`.
- `kind: "unavailable"` → "No preview is available for this node."
- Footer, always rendered (R1.3, R3.5): a `Link` (react-router `Link` styled via Cloudscape) to `/deployed-workflows/{registrationId}/executions/{executionId}/results` labeled "View full results".

Non-output nodes keep the exact pre-existing rendering path (failure/warning `Alert`, plain `Box` otherwise) — the component change is purely additive branching on `isOutputNode` (R1.4, R5.1, R5.2).

### RunStatusGraph.tsx wiring

- Add the metadata query, `enabled` only when the selected node's type is `llm_inference` or `bedrock_inference` and its status is terminal — avoids fetching metadata for every graph view and during in-flight runs.
- Build `imageSrc` with `workflowExecutionOutputImageUrl(executionId, authEnabled ? token : undefined)` via the existing `useAuth` hook (same pattern as `RunResults.tsx`).
- Selected node type comes from the already-computed `visuals` (which carry `type`).

## Data Models

Run metadata (served by the new endpoint, shape produced by `_persist_run_metadata`):

```typescript
// Loosely typed on the frontend; only these paths are read:
// metadata.llm?.[nodeId]?.generated_text : string  — llm preview text
// metadata.llm?.[nodeId]?.error          : string  — llm binding failure
// metadata.is_anomalous                  : unknown — bedrock merged field
// metadata.confidence                    : unknown — bedrock merged field
```

Extraction is defensive: every path access type-checks (`typeof value === "string"`, object guards) so arbitrary tag values can never crash the selector.

## Error Handling

- Backend: `read_run_metadata` is fully contained (mirrors `parse_node_status`) — any read/parse problem yields `{}` with 200; only an unknown execution yields 404 (R4.2, R4.3).
- Frontend: metadata query errors map to the `unavailable` view-model; thumbnail load errors flip the card to `unavailable` via `onError`; non-terminal statuses short-circuit before any data access (R3.1–R3.3). The results link renders in every state (R3.5).

## Testing Strategy

- **Frontend**: jest + React Testing Library + fast-check, following the existing patterns in `graphGeometry.test.ts` (pure-module tests) and `RunStatusGraph.test.tsx` (mocked `api/WorkflowRegistrationAPI`, `MemoryRouter` with the graph route). New pure tests target `previewModel.ts`; component tests cover click-to-preview, per-type rendering, degraded states, and preservation of the non-output-node detail path. Run with `CI=true npx react-scripts test --watchAll=false`.
- **Backend**: pytest under `test/backend-test/workflow_engine/`, mirroring `test_workflow_graph_node_status_api.py` (route via FastAPI TestClient + helper unit tests with `tmp_path`); hypothesis (already used in the repo) for the metadata round-trip property.
- Property tests run ≥100 iterations and each references its design property.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Output-node classification gates the preview

For any node type string, `previewViewModel` returns `kind: "none"` exactly when the type is not in `OUTPUT_NODE_TYPES`, and returns a non-`"none"` view-model for every type in the set.

**Validates: Requirements 1.1, 1.4**

### Property 2: Preview state precedence

For any Output_Node type and any node-status entry: a missing or non-terminal (`pending`/`running`) status yields `kind: "pending"` regardless of metadata or image availability; a `failure` status yields `kind: "failure"` carrying the entry's detail; a terminal `success`/`warning` status with its type's data source unavailable (capture without image results, llm/bedrock with metadata lacking the node's entry or an errored request) yields `kind: "unavailable"`; and publish types with a terminal status always yield `kind: "status"` carrying the entry's status and detail.

**Validates: Requirements 2.4, 3.1, 3.2, 3.3**

### Property 3: Snippet truncation

For any string, `snippet` returns the string unchanged when its length is at most 280 characters; otherwise it returns exactly the first 280 characters followed by an ellipsis. In all cases the returned text (minus any ellipsis) is a prefix of the input.

**Validates: Requirements 2.2, 2.5**

### Property 4: Results link is present in every preview state

For any preview view-model produced by `previewViewModel` for an Output_Node (every kind: pending, failure, loading, image, text, fields, status, unavailable), rendering `NodePreviewCard` yields a link whose destination is the Run_Results_Page path for the current registration and execution ids.

**Validates: Requirements 1.3, 3.5**

### Property 5: Run metadata read round trip

For any JSON-serializable object written to `{output_dir}/{capture_id}.json`, `read_run_metadata(output_dir, capture_id)` returns an equal object.

**Validates: Requirements 4.1**
