# Design Document: Deployed Workflow Run Observability

## Overview

This design adds run observability to the on-device Deployed Workflow experience: **View results** (output images + overlay toggle), **View run log**, and a **Run status graph** (per-node green/red/yellow). It extends the existing `workflow_engine` execution path, the `WorkflowExecution` record, the device API, and the on-device frontend, without touching the Portal authoring/packaging flow or the Pipeline_Configuration path.

The work divides into a **capture layer** (make the executor produce artifacts, logs, and per-node status during a run), an **API layer** (expose them), and a **presentation layer** (three frontend surfaces). The capture layer is the substance; the two prerequisite gaps it closes are (a) the deployed executor does not currently write image artifacts, and (b) it records only a single terminal `failing_node_id` rather than per-node state or a per-run log.

Design principle throughout: **mirror the proven Pipeline_Configuration behavior** (`gstreamer/pipeline_builder.py`, `endpoints/download_file.py`, `endpoints/inference_result.py`, and the `live-result` frontend components) rather than invent parallel mechanisms, and keep every capture path **contained** (a failure in artifact/log/status capture never fails a run or crashes LocalServer — workflow-manager Requirement 13.7).

### Requirements coverage map

| Requirement | Design section |
|---|---|
| R1 Persist artifacts | §3.1 Artifact routing in the executor |
| R2 Capture logs | §3.2 Per-execution log capture |
| R3 Per-node status | §3.3 Node status collection |
| R4 Device API | §4 API layer |
| R5 View results | §5.1 |
| R6 View run log | §5.2 |
| R7 Run status graph | §5.3 |
| R8 Non-regression | §6 + Testing Strategy |

## Architecture

```mermaid
graph TD
    subgraph Device Frontend (src/frontend)
        DET[DeployedWorkflowDetails\nexecutions table + links]
        RES[RunResults screen\nInteractableImage + toggle]
        LOG[RunLog viewer]
        GRAPH[RunStatusGraph\nlightweight SVG renderer]
    end
    subgraph Device Backend (src/backend)
        API[workflow_engine/api.py\n+ run-observability routes]
        EX  [workflow_engine/pipeline_executor.py\nWorkflowExecutor.execute]
        STORE[(WorkflowExecution\n+ artifacts/log/nodeStatus)]
        FILES[/aws_dda/captures/{workflowId}/{executionId}/...]
        DL[endpoints/download_file.py\nimage serving]
    end
    DET -->|GET registration+executions| API
    DET --> RES
    DET --> LOG
    DET --> GRAPH
    RES -->|results meta + images| API
    RES -->|base/overlay image| DL
    LOG -->|GET .../log| API
    GRAPH -->|GET graph + node-status| API
    API --> STORE
    EX --> STORE
    EX --> FILES
    API --> FILES
```

The executor produces three new outputs per run — Run_Artifacts on disk, a Run_Log, and a per-node status map — persisted against the `WorkflowExecution`. The API exposes them plus the registration's `workflow.json` graph. The frontend adds three views hung off the existing executions table.

## Data Models

`workflow_engine/models.py` `WorkflowExecution` gains nullable columns (additive; existing columns unchanged per R8.3). SQLite migrations in this codebase are additive column adds guarded by the existing migration path (`dao.sqlite_db.db_migration`).

- `capture_id` (String, nullable) — the per-run capture id the executor synthesizes (`{workflow_id}-{execution_id}`, already computed for METADATA injection).
- `output_dir` (String, nullable) — the per-run artifact directory (see §3.1).
- `has_image_results` (Boolean, default False) — whether the terminal node is a File_Output_Node and artifacts were routed (drives the "View results" link, R5.1/R5.2).
- `node_status_json` (Text, nullable) — JSON map `{ nodeId: {status, detail?} }` (R3).
- `log_path` (String, nullable) — path to the per-execution Run_Log file (R2).

`node_status_json` and the log are files/JSON rather than normalized tables to keep the change small and the write path contained; they are read-mostly and per-execution bounded.

The API contract shapes (`execution_to_dict` additions, `results`, `node-status`, `graph` payloads) are defined inline in §4.

## Components and Interfaces

The changed/added components, grouped by layer: the shared on-disk artifact location (§2), the executor capture layer (§3: artifact routing, log capture, node-status collection), the device API layer (§4: results/log/graph/node-status routes and `execution_to_dict` additions), and the frontend presentation layer (§5: RunResults, RunLog, RunStatusGraph and their `WorkflowRegistrationAPI` clients). The interface contracts for each are specified in those sections.

## 2. Per-run output location

Artifacts land under a per-run directory, mirroring the Pipeline_Configuration `workflowOutputPath` convention but scoped per execution to satisfy R1.2:

```
/aws_dda/captures/{workflowId}/{executionId}/
    {capture_id}.jpg          # base captured frame
    {capture_id}.overlay.jpg  # overlay (when produced)
    {capture_id}.mask.png     # mask (when produced)
    {capture_id}.jsonl        # result record (when produced)
```

`_WORKFLOW_CAPTURE_ROOT` (already `/aws_dda/captures`) is extended with the `{workflowId}/{executionId}` suffix. The METADATA `sagemaker_edge_core_capture_data_disk_path` (§3.1) is set to this directory so the marshal model's `workflow_id` derivation and the emlcapture file targets agree.

## 3. Capture layer (executor)

### 3.1 Artifact routing (R1)

Today `WorkflowExecutor` injects a `metadata` arg (capture id / disk path / fleet name) and appends a terminal `fakesink`, but leaves `emlcapture`'s `meta` empty, so overlay/mask/jsonl are not written. This design adds `_route_capture_outputs(document, output_dir, capture_id)` run before render, alongside the existing `_stage_frame_sources` / `_resolve_model_names` / `_inject_inference_metadata` / `_ensure_terminal_sink` steps:

- For a terminal `emlcapture` element whose `meta` is empty/`{capture_meta}`, populate it with the same `triton_inference_output_*` routing string the Pipeline_Configuration builder uses (`pipeline_builder._add_post_processing_plugins`):
  `triton_inference_output_overlay:file-target_{p}-overlay.jpg, triton_inference_output_mask:file-target_{p}-mask.png, triton_inference_output_capture:file-target_{p}-jsonl, triton_inference_output_anomalous:{p}_is-anomalous, triton_inference_output_confidence:{p}_confidence` where `p = {output_dir}/{capture_id}` (the message-broker `file-target_` convention yields `{p}-{ext}` files; verified by `test/backend-test/test_overlay_path_consistency.py`).
- The routing is only populated when the deployed model declares the corresponding outputs — reuse the config-gated approach from `_model_declares_metadata_input` (extend to read the ensemble's declared `output {}` names from `config.pbtxt`), so models without overlay/mask outputs get only the applicable targets and non-ensemble/plain models are untouched (R1.5).
- `output_dir` is created (best-effort) before the run; `has_image_results` is set True when routing was applied.
- Additive: the `is_anomalous`/`confidence` tag values still come from the emltriton tags exactly as today (R1.6).

Determining "terminal node is a File_Output_Node" (R5.1): the terminal segment's last non-synthetic element is `emlcapture`. This is derived from the Compiled_Pipeline_Document the executor already loads.

### 3.2 Per-execution log capture (R2)

A scoped logging handler captures the run's logs without altering existing logging (R2.5):

- `RunLogCapture(execution_id, log_path)` — a context manager that attaches a `logging.FileHandler` (bounded via a size cap / truncation, R2.4) to the `workflow_engine` logger (and the `gstreamer` logger, which emits the element/backend errors R2.3 needs) for the duration of `execute()`, then detaches it. The root/component logging is untouched, so the Greengrass component log still receives everything (R2.5).
- The file lives at `log_path = {output_dir}/run.log` (or under a logs dir when there is no output_dir, e.g. non-capture workflows), recorded on the execution.
- All of `execute()`'s existing `logger.info/error` calls — launch string, per-node resolution/injection, completion tags, failure error — are thereby captured (R2.2, R2.3).
- Wrapped so any handler error is swallowed and never fails the run (R2.6). Best-effort, contained.

### 3.3 Node status collection (R3)

A `NodeStatusCollector` built from `rendering.element_name_map(document)` (element-name → nodeId) accumulates per-node status over the run:

- Initialize every participating `nodeId` to `pending`.
- The executor sets the run to `running`; for the single-shot pipeline model, nodes are marked `success` on clean completion (R3.3). Because the current `run_pipeline` runs the whole graph in one call, live per-element progress (R3.5) is a **best-effort enhancement**: the `GstPipelineManager` bus already sees per-element ERROR (`Pipeline ERROR - {src_name}`) and can be extended to also forward WARNING messages and element state-changed transitions to a callback. The design threads an **optional** `status_sink` callback into `run_pipeline` (default None → today's behavior) so the executor can receive `(element_name, state)` and map to nodeId. When available it drives `running`/`warning` live; when not, R3.6 guarantees a fully-resolved terminal map.
- On failure, the failing element → nodeId (via existing `failing_node_id_from_error`) is marked `failure` with the error `detail`; other participating nodes are left at their last-known state (R3.2, R3.6).
- On a bus WARNING mapped to a node, mark that node `warning` and retain the message (R3.4).
- Persist as `node_status_json` at run end. Contained: collector errors never fail the run (R8.5).

Warning forwarding is the one `gstreamer` change; it is additive (a new optional callback + adding `Gst.MessageType.WARNING` to the handled set, forwarding to the sink, without changing existing error/EOS/TAG handling), keeping R8.1 (Pipeline_Configuration behavior) intact since the Pipeline_Configuration caller passes no sink.

## 4. API layer (`workflow_engine/api.py`)

New routes, additive to the existing four (R4, R8.3). `execution_to_dict` gains `hasImageResults`, `captureId`, and `outputDir` (new fields only).

- `GET /workflows/executions/{execution_id}/results` → `{ hasImageResults, captureId, images: [{ kind: "output"|"input", hasOverlay }] }` (R4.1). Returns 404 when the execution is unknown (R4.6).
- `GET /workflows/executions/{execution_id}/output-image` and `/overlay-image` (or a `variant` query) → serve files from `output_dir` via `FileResponse`, mirroring `download_file.py`'s capture-image serving and its auth/token-in-query behavior (R4.2, R4.7). The mask is served as image bytes; the frontend chroma-keys it (the existing overlay pipeline uses a base64 mask + background color — see §5.1 for the variant decision).
- `GET /workflows/executions/{execution_id}/log` → `text/plain` Run_Log, or an empty-but-200 body when not yet available (R4.3, R6.4).
- `GET /workflows/registrations/{registration_id}/graph` → the registration's `workflow.json` (nodes with positions + connections) needed to render the mirror graph (R4.4). Read from `registration.artifact_path/workflow.json`; 404 when absent.
- `GET /workflows/executions/{execution_id}/node-status` → `{ nodeId: { status, detail? } }` from `node_status_json` (R4.5).

`node-status` and `results`/`log` are separate lightweight endpoints so the graph and log views can poll independently of the (heavier) image fetches.

## 5. Presentation layer (`src/frontend`)

New API client functions in `api/WorkflowRegistrationAPI.ts` (results meta, log text, graph, node-status), new routes in `App.tsx` under the existing deployed-workflow area, and per-execution links in `DeployedWorkflowDetails.tsx` (rendered by `presentation.ts` helpers so they stay unit-testable). Cloudscape components throughout, matching the existing screens.

### 5.1 View results (R5)

- A new `RunResults` screen mirrors `result-history/ResultDetailsCardDisplay.tsx`: it holds `showMask` state and renders `InteractableImage` with the mask overlay and `RefreshDisplayActions` toggle (R5.4, R5.6) — the exact components the "run inference" results screen uses.
- Base image `imageSrc` = the new `/workflows/executions/{id}/output-image` endpoint (+ token when auth enabled), matching how `ResultDetailsCardDisplay` builds `getCaptureAPI`.
- **Overlay source decision**: the existing UI consumes the mask as a base64 string + background color from the result record (`getMaskImageProp`). To reuse `getMaskImageProp`/`setupMaskImage` unchanged, the results endpoint returns the mask as base64 + background derived from the run's `.jsonl`/`.mask.png` artifact. When no mask exists, `maskImageProp` is empty and the toggle is hidden (`showAnomalyMaskToggle={!!mask}`), giving the plain-image path (R5.5). This keeps `InteractableImage` and `helpers.ts` untouched.
- Link shown only when `execution.hasImageResults` (R5.1/R5.2). Load failure → Cloudscape empty/error state (R5.7).

### 5.2 View run log (R6)

- `RunLog` viewer fetches `/log` and renders it in a scrollable, copyable Cloudscape `CodeEditor`/`<pre>`-style container (R6.5). Shown for any started execution (R6.1). Empty/pending → explanatory empty state (R6.4). Failed runs render the same way, with the error/failing-node visible in the text (R6.3).

### 5.3 Run status graph (R7)

- **Renderer choice**: add a **lightweight read-only SVG/HTML renderer** in `src/frontend` rather than the Portal's `@xyflow/react` dependency. Rationale: the edge bundle has no graph lib today; the authored `workflow.json` nodes already carry `position {x,y}` and connections, so a read-only renderer (absolutely-positioned node cards + SVG edge lines) is small, offline (R7.7), and avoids pulling React Flow onto the device. Node category colors reuse the Portal's palette values (`CATEGORY_META`) copied as constants.
- Data: `GET .../graph` (nodes+connections+positions) + `GET .../node-status` (per-node state). The component overlays status color on each node: green `success`, red `failure`, yellow `warning`, and an in-progress affordance (spinner/pulse) for `running` (R7.3).
- Selecting/hovering a `failure`/`warning` node shows its `detail` (R7.4).
- While the execution is active, poll `node-status` (reuse the details page's active-poll pattern from `presentation.shouldPoll`) and stop at terminal (R7.5). On finish, coloring is fully resolved (R7.6, backed by R3.6).

## 6. Non-regression & isolation (R8)

- No change to `gstreamer/pipeline_builder.py` behavior or the "run inference" screens (R8.1). The one `gst_pipeline.py` change (optional `status_sink` + WARNING forwarding) is inert when no sink is passed, which is the case for every existing caller.
- New model columns are nullable/defaulted and added via the existing additive migration path; existing endpoint response fields are only added to, never removed (R8.3).
- Every capture path (artifact routing, log handler, status collector) is wrapped so its failure is logged and swallowed — a run still reaches a terminal state and LocalServer never crashes (R8.5, workflow-manager 13.7).
- Graceful degradation: non-capture workflows get no results link; runs with no mask show a plain image; runs with no warnings show plain success/failure coloring (R8.2).

## Error Handling

- **Missing artifacts/log/graph**: endpoints return 404 (unknown execution/registration) or a 200 empty state (not-yet-available log), never a 500 (R4.6, R6.4, R5.7).
- **Executor capture failures**: caught and logged inside `execute()`; the run's terminal status is still recorded (R2.6, R8.5).
- **Frontend fetch failures**: each view renders an explicit empty/error state.
- **Partial node status**: R3.6 fallback guarantees a populated map for any finished run even if live tracking was unavailable.

## Correctness Properties

These invariants hold for any run and drive the property tests:

### Property 1: Node-status coverage and terminality
For any finished WorkflowExecution, `node_status_json` contains exactly the set of `nodeId`s that map to elements in the Compiled_Pipeline_Document, and every entry is in a terminal state (`success`/`failure`/`warning`), never `pending`/`running`.
**Validates: Requirements 3.1, 3.6**

### Property 2: Single failure attribution
When a run fails at an identifiable element, exactly the mapped node is `failure` and carries the error detail; when no element is identifiable, no node is spuriously marked `failure`.
**Validates: Requirements 3.2**

### Property 3: Artifact routing is capture-gated
`_route_capture_outputs` mutates only a terminal `emlcapture` element and only adds `triton_inference_output_*` targets for outputs the model declares; documents without a capture terminal render byte-identically to today.
**Validates: Requirements 1.1, 1.5, 8.2**

### Property 4: Additive tags
The `{is_anomalous, confidence}` tag values returned for a run are unchanged by the presence or absence of artifact routing.
**Validates: Requirements 1.6**

### Property 5: Capture containment
For any injected failure in artifact routing, log capture, or status collection, `execute()` still records a terminal execution status and raises nothing.
**Validates: Requirements 2.6, 8.5**

### Property 6: Endpoint backward compatibility
The four existing deployed-workflow endpoints return supersets of their current response shapes: all prior keys present with prior semantics.
**Validates: Requirements 8.3**

### Property 7: Results-link and artifacts equivalence
`hasImageResults` is true if and only if the run routed capture artifacts (terminal File_Output_Node), so the "View results" link appears exactly when viewable images exist.
**Validates: Requirements 5.1, 5.2**

## Testing Strategy

Baselines that stay green (per repo conventions): backend `PYTHONPATH=src/backend:test/backend-test` scoped to `test/backend-test/workflow_engine` (+ the security gate suites in the build), and on-device frontend vitest + `npm run build` under `src/frontend`.

- **Executor (unit, isolation)**: `_route_capture_outputs` populates emlcapture `meta` only for capture terminals and declared outputs; `output_dir`/`capture_id`/`has_image_results` set correctly; non-capture and plain-model documents untouched (R1, R8.2). Log capture attaches/detaches a handler and writes a bounded file without touching root logging (R2). `NodeStatusCollector` maps element→node correctly and yields a fully-resolved terminal map on completion and on failure (R3, R3.6). All capture failures are swallowed (R8.5). Follows the existing `test_workflow_pipeline_executor.py` fixture style (no real Triton repo → gating no-ops).
- **API (unit)**: each new route's success and 404 shapes; `execution_to_dict` new fields; existing four routes' shapes unchanged (R4, R8.3).
- **Frontend (vitest)**: `presentation.ts` helpers decide link visibility from `hasImageResults`/status; `RunResults` shows/hides the toggle based on mask presence and reuses `InteractableImage`; `RunLog` empty vs populated; `RunStatusGraph` colors nodes by status and polls while active. Mirrors existing `deployed-workflow` and `result-history` test conventions.
- **Property tests** (where a general invariant exists, per repo convention, `fast-check`/`hypothesis`, ≥100 runs): node-status map always covers exactly the document's participating nodeIds and is fully terminal for any finished run.
