# Design Document

## Overview

Four coordinated changes (investigated against the current tree):

1. **Catalog** (both copies, byte-in-sync): `llm_inference` gains `ParameterDescriptor("anomaly_mode", "bool", required=False, default=False)` (default False = today's behavior, unlike Bedrock's default True — llm is a text node first).
2. **Executor — LlmInferenceProcessor** (`src/backend/workflow_engine/output_bindings.py`): in `_run_one`, when `anomaly_mode` truthy → append `BEDROCK_JSON_INSTRUCTION` to the RENDERED prompt; invoke; `parse_bedrock_answer(text)`; on success return `{'generated_text': text, 'is_anomalous': ..., 'confidence': ...}` — and `process()` merges the verdict keys FLAT into metadata (in addition to nesting the whole outcome under `llm[nodeId]`); on ValueError record `{'error': ..., 'generated_text': text}` (never raise — llm containment contract). Freeform path untouched.
3. **Compiler** (both copies): generalize the bedrock capture plan to llm_inference — include llm node ids in the `opaque` set and the capture-plan call (frames already terminate logically at llm nodes since their output is InferenceMeta; verify no existing workflow relies on frames flowing THROUGH llm nodes — the compiler's `_stream_successors` collapses executor nodes out of the stream, so a source feeding llm AND a downstream gst node does so via separate connections; confirm with compiler tests). Emit `capturePaths: {"in": ...}` on llm bindings. File naming: reuse `bedrock_frame_{feeder}.jpg` plan (shared feeders share files).
4. **Executor — artifact persistence** (`pipeline_executor.py`):
   - `_route_capture_outputs` currently gates `output_dir`/`capture_id`/`has_image_results` on a terminal emlcapture. Change: ALWAYS record `output_dir`/`capture_id` (dir already created unconditionally); set `has_image_results` when terminal capture OR inference-node captures exist (known after the copy step; update the execution row then).
   - New `_persist_node_frames(execution, document, work_dir)`: for each bedrock/llm binding with capturePaths, copy existing frame files to `{output_dir}/{capture_id}.node.{nodeId}.{port}.jpg` (sanitize nodeId for filenames); call on success AND output-binding-failure paths, before the `finally` rmtree; contained.
5. **API** (`workflow_engine/api.py` + `run_artifacts.py` + `endpoints/download_file.py`):
   - `run_artifacts.list_node_images(output_dir, capture_id)` → `[{nodeId, port, filename}]` by globbing the `.node.` pattern.
   - `/results` response gains `images` entries `{kind: "node", nodeId, port, hasOverlay: false}` and returns `hasImageResults: true` when node images exist even without the base image; `captureId` populated.
   - New unauthenticated route `GET /workflows/executions/{execution_id}/node-image?nodeId=&port=&token=` serving the file (validate nodeId/port against the listed set — no path traversal; mirror the output-image route).
6. **Frontend** (`src/frontend`): `RunResults.tsx` renders, in addition to the existing output-image container: one section per inference node with images — the node's 1–2 images side by side; a verdict badge/border when run metadata has `is_anomalous` (red/ANOMALOUS + confidence, green/NORMAL) from the flat metadata (single-verdict runs) — and a metadata panel below showing the node's text (`bedrock.{nodeId}.text` or `llm.{nodeId}.generated_text`) plus verdict fields when present; freeform nodes show image + text only. Uses existing `getWorkflowExecutionMetadata`. API client types extended additively.

## Verdict-to-node attribution

Flat `is_anomalous`/`confidence` are run-level (last writer wins). For display: a node's section shows the verdict badge when (a) the node's mode was anomaly (its parameters aren't in the metadata, so infer from presence: bedrock nodes — `bedrock[nodeId].text` exists and flat verdict exists; llm nodes — `llm[nodeId]` carries no `error` and flat verdict exists) — SIMPLER RULE adopted: show the badge on every inference-node section when the run metadata carries `is_anomalous`, labeled as the run verdict. Document this in the UI copy ("Run verdict"). Per-node verdicts are out of scope (single-inference-node workflows are the operative case).

## Correctness Properties

Property 1: VLM anomaly-mode parity — for any prompt/answer, anomaly-mode llm bindings send rendered-prompt + "\n\n" + instruction, merge parsed verdict flat AND keep generated_text nested; unparseable answers record error+text without raising and merge no verdict; absent/false anomaly_mode is byte-identical to today. (Validates 1.1–1.4)

Property 2: Frame persistence — for any document with bedrock/llm capturePaths whose files exist in work_dir, after the run the files exist as {capture_id}.node.{nodeId}.{port}.jpg in output_dir, on success and output-binding-failure paths; missing capture files are skipped without error; runs without inference captures copy nothing. (Validates 2.1–2.4)

Property 3: API additivity — /results returns the node images additively (existing fields unchanged); the node-image route serves exactly the listed files and 404s for unlisted node/port combinations (no traversal); runs with no artifacts keep today's empty responses. (Validates 3.1, 3.5, 4.1)

Property 4: Compiler preservation — non-inference compilation byte-identical; bedrock plans unchanged; llm nodes gain capturePaths and their feeding branches gain capture sinks; sim path unchanged. (Validates 4.2, 4.3)

## Testing Strategy

- Executor: extend the llm inference tests (test_workflow_llm_inference.py) for anomaly-mode parity (Hypothesis over prompts/answers, error path); new persistence tests in the executor harness (frames copied on success/failure paths, no-capture tolerance).
- Compiler: extend test_compiler_bedrock.py-style tests in workflow_core tests for the llm capture plan (portal copy) + vendored sync check.
- API: tests for list_node_images, /results additivity, node-image route validation (404 traversal attempts).
- Frontend: vitest tests for RunResults rendering states (anomaly badge, freeform, metadata panel, mixed with File_Output image).
- Device-side + catalog + frontend: device pieces ride the NEXT LocalServer build; catalog is portal (deploy) + vendored (build); the results-view frontend is the DEVICE frontend (src/frontend — ships in the LocalServer image, no portal deploy).
