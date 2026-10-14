# Implementation Plan

## Overview

VLM (llm_inference) returned-value parity with Bedrock (anomaly_mode verdict contract) and a run results view that shows the sent images with a verdict overlay and the returned metadata. Feature spec: tests accompany implementation per task. Device executor/frontend/vendored-catalog pieces ride the NEXT LocalServer build; the portal catalog/compiler piece needs a portal deploy.

## Task Dependency Graph

```json
{
  "waves": [
    {"wave": 1, "tasks": ["1", "2", "3"], "description": "1: llm executor anomaly-mode parity. 2: catalog+compiler capture plan (both copies). 3: executor artifact persistence + dir assignment. Mutually independent files except 2/3 touch different modules."},
    {"wave": 2, "tasks": ["4"], "description": "API: node-image listing/serving + /results additivity. Depends on 3 (file naming)."},
    {"wave": 3, "tasks": ["5"], "description": "Device frontend results view. Depends on 4 (API shape)."},
    {"wave": 4, "tasks": ["6"], "description": "Checkpoint: full suites + catalog sync + frontend tests. Depends on all."}
  ]
}
```

## Tasks

- [ ] 1. LlmInferenceProcessor anomaly-mode parity (Requirement 1)
  - `src/backend/workflow_engine/output_bindings.py`: `LlmInferenceProcessor._run_one` — when `anomaly_mode` truthy (absent/false → today's path byte-identical): append `BEDROCK_JSON_INSTRUCTION` to the RENDERED prompt before invoking; on 200, `parse_bedrock_answer(text)` — success → outcome `{generated_text, is_anomalous, confidence}`; ValueError → `{error: <reason + excerpt>, generated_text: text}` (recorded, never raised)
  - `process()`: merge verdict keys (`is_anomalous`/`confidence`) FLAT into metadata when present in an outcome (in addition to the nested `llm[nodeId]` record), so downstream gates work like Bedrock's
  - Extend `test/backend-test/workflow_engine/test_workflow_llm_inference.py` (or a new sibling file): Hypothesis parity properties (prompt suffix, verdict merge, unparseable→error+text no-raise no-verdict, absent/false identical to today), mixed bedrock+llm runs merge sanely
  - Run the workflow_engine llm/bedrock suites — green
  - _Requirements: 1.1 (executor side), 1.2, 1.3, 1.4_

- [ ] 2. Catalog + compiler: llm anomaly_mode parameter and frame capture plan (both copies, in sync)
  - Catalog (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py` + vendored copy): `LLM_INFERENCE` gains `anomaly_mode` bool, required=False, DEFAULT FALSE, description mirroring Bedrock's (note the different default: existing llm workflows stay freeform); prompt_template description notes the anomaly-mode auto-appended instruction
  - Compiler (both copies): include llm_inference node ids in the opaque/capture-plan path (design §3): frames feeding llm `in` ports terminate in the shared synthetic capture sinks; emit `capturePaths: {"in": ...}` on llm bindings; bedrock plans byte-identical; sim path unchanged; verify via compiler tests that a source feeding llm + a gst branch still compiles both
  - Extend workflow_core tests (test_compiler_bedrock.py pattern + catalog content tests + baseline regeneration per the documented maintenance path); verify both copies byte-identical (`diff`)
  - Run `cd edge-cv-portal/backend && python3 -m pytest layers/workflow_core/tests/ -q` — green
  - _Requirements: 1.1 (catalog side), 2.1, 4.2, 4.3_

- [x] 3. Executor: artifact dir assignment + node-frame persistence (Requirement 2)
  - `src/backend/workflow_engine/pipeline_executor.py`: always record `output_dir`/`capture_id` on the execution (dir creation already unconditional); `has_image_results` true when terminal capture routed OR node frames persisted (update after the copy step)
  - New `_persist_node_frames(execution, document, work_dir)`: copy each bedrock/llm binding's existing capturePaths files to `{output_dir}/{capture_id}.node.{sanitized_nodeId}.{port}.jpg`; call on the success path AND the output-binding-failure path, before the finally-rmtree; fully contained (R8.5 style); tolerate absent capturePaths (old packages, 4.4)
  - Extend the executor harness tests: frames persisted on both paths; bedrock-only run (no File_Output) now records output_dir/capture_id, persists metadata JSON + node images, has_image_results true; File_Output runs unchanged; no-capture llm runs tolerate absence
  - _Requirements: 2.2, 2.3, 2.4, 4.4_

- [ ] 4. API: node images in /results + serving route (Requirement 3.1)
  - `run_artifacts.list_node_images(output_dir, capture_id)` parsing the `.node.` filename pattern → `[{nodeId, port}]`
  - `api.py /results`: additive `{kind: "node", nodeId, port, hasOverlay: false}` entries; `hasImageResults`/`captureId` populated when only node images exist
  - `endpoints/download_file.py`: `GET /workflows/executions/{execution_id}/node-image?nodeId=&port=&token=` — validate against list_node_images (404 otherwise; no path traversal), FileResponse jpeg; token-in-query like output-image
  - Tests: run_artifacts listing, /results additivity (existing-shape preservation), route validation incl. traversal attempts
  - _Requirements: 3.1, 3.5, 4.1_

- [ ] 5. Device frontend: results view sections (Requirements 3.2, 3.3, 3.4, 3.5)
  - `src/frontend/src/api/WorkflowRegistrationAPI.ts`: extend `WorkflowExecutionResultImage` additively (`kind: "node"`, `nodeId?`, `port?`); `workflowExecutionNodeImageUrl(executionId, nodeId, port, token?)`
  - `RunResults.tsx`: keep the existing output-image container as-is (3.4); add one section per inference node with images: 1–2 images side by side; when run metadata carries `is_anomalous` show the verdict badge/border (ANOMALOUS red / NORMAL green + confidence, labeled "Run verdict" per design attribution rule); metadata panel below (node text from `bedrock.{nodeId}.text` or `llm.{nodeId}.generated_text`, verdict fields when present; freeform → text only); graceful empty/partial states
  - Vitest tests (`npx vitest run`) for the rendering states
  - _Requirements: 3.2, 3.3, 3.4, 3.5_

- [ ] 6. Checkpoint
  - `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/` — no new failures (steering-known tolerated)
  - `cd edge-cv-portal/backend && python3 -m pytest layers/workflow_core/tests/ -q` — green; catalog copies byte-identical
  - Frontend: `npx vitest run` for the touched suites; `tsc` clean for src/frontend if the repo gates on it
  - _Requirements: all_

## Notes

- Ship vehicle: tasks 1/3/4/5 + vendored catalog/compiler are device-side (NEXT LocalServer build); task 2's portal copy needs a portal compute-stack deploy for the designer checkbox + compiler capture plan (workflows must be REPACKAGED to gain llm capturePaths — 4.4 tolerates old packages).
- llm anomaly_mode defaults FALSE (unlike bedrock's TRUE): llm is a text node first; existing workflows keep behavior without repackage.
- Verdict overlay is a frontend badge/border presentation, not a pixel mask (Bedrock/VLM verdicts carry no mask).
- Known pre-existing test failures per repo steering apply at the checkpoint.
