# Implementation Plan

## Overview

Adds the `anomaly_mode` checkbox to the Bedrock Inference node and hardens the anomaly contract: the executor auto-appends the canonical JSON instruction in anomaly mode (default) and records raw text (`bedrock_text` / `bedrock.{nodeId}.text`) in freeform mode. Feature spec: tests accompany implementation per task (not the bugfix exploration flow).

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Task 1: executor change + tests. Task 2: catalog parameter (both copies) + tests. Independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3"],
      "description": "Checkpoint - full workflow_engine suite + portal catalog/compiler tests + existing bedrock suites reconciled. Depends on wave 1."
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Portal deploy (catalog visible in designer). Depends on wave 2. Device-side executor + vendored catalog ride the NEXT LocalServer build."
    }
  ]
}
```

## Tasks

- [x] 1. Executor: anomaly-mode instruction append + freeform mode
  - In `src/backend/workflow_engine/output_bindings.py`: add `BEDROCK_JSON_INSTRUCTION` module constant ('Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.'); in `BedrockInferenceProcessor._run_one`, read `anomaly_mode` from parameters (absent/None → True, coerce bool)
  - Anomaly mode: invoke with `prompt + "\n\n" + BEDROCK_JSON_INSTRUCTION`; parse and return the verdict dict exactly as today (Property 1)
  - Freeform mode: invoke with the prompt unchanged; return the raw text via `process()`-level merge as flat `bedrock_text` plus nested `bedrock.{nodeId}.text` (process() merges the `bedrock` sub-dict without clobbering earlier nodes); no `parse_bedrock_answer`, no verdict keys, answer format never raises (Property 2)
  - Frame handling (required `in`, optional reference) and error surfacing untouched (Property 3)
  - Write `test/backend-test/workflow_engine/test_bedrock_response_mode.py` (injectable-invoker harness): Hypothesis property — for any prompt, anomaly-mode invoker prompt == prompt + separator + instruction, default-absent param behaves as anomaly; freeform passthrough + `bedrock_text`/nested recording + unparseable-answer tolerance + no verdict keys; `render_template("{bedrock_text}", metadata)` renders the text; two freeform nodes — nested keys both present
  - Update the existing suites that assert the invoker prompt equals the configured prompt (`test_workflow_bedrock_inference.py`, `test_bedrock_single_image_exploration.py`, `test_bedrock_single_image_preservation.py` both-frames call-shape property): the expected prompt becomes the appended form in anomaly mode — note the intended contract change in docstrings
  - Run: `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/test_bedrock_response_mode.py test/backend-test/workflow_engine/test_bedrock_single_image_exploration.py test/backend-test/workflow_engine/test_bedrock_single_image_preservation.py test/backend-test/workflow_engine/test_workflow_bedrock_inference.py test/backend-test/workflow_engine/test_opcua_binding_preservation.py -v` — all green
  - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3, 2.4, 4.1, 4.2, 4.3_

- [x] 2. Catalog: `anomaly_mode` bool parameter on the bedrock node (both copies, in sync)
  - In `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`: add `ParameterDescriptor("anomaly_mode", "bool", required=False, default=True, description=...)` to `BEDROCK_INFERENCE.parameters` (description: checked → anomaly JSON verdict with the executor auto-appending the JSON instruction; unchecked → freeform text recorded as bedrock_text); update the `prompt` parameter description per Req 3.3; simplify `BEDROCK_DEFAULT_PROMPT` to drop the inline JSON-shape sentence per design (executor now guarantees it)
  - Apply the IDENTICAL edit to the vendored copy `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`; verify byte-in-sync afterwards (`diff`)
  - No compiler change (parameters copy into executor bindings generically); no frontend change (bool renders as checkbox — mqtt `greengrass`/`aws_iot` precedent)
  - Add/extend a portal catalog test (follow `edge-cv-portal/backend/layers/workflow_core/tests/` patterns, e.g. alongside test_compiler_bedrock.py): the bedrock descriptor carries `anomaly_mode` bool default True; the compiled executor binding for a bedrock node carries the parameter; and a vendored-copy equality assertion if an existing sync test exists (else include a diff check in the test)
  - Run the workflow_core test suite (`cd edge-cv-portal/backend && python3 -m pytest layers/workflow_core/tests/ -q` or the repo's established invocation) — green
  - _Requirements: 3.1, 3.2, 3.3, 4.4_

- [x] 3. Checkpoint - Ensure all tests pass
  - Run `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/` — no new failures (ignore the stale-workflow-registrations exploration failures if that spec's fix has not landed; they are the parallel spec's expected-fail tests)
  - Run the portal backend tests covering workflow_core catalog/compiler (`edge-cv-portal/backend`) — no new failures beyond the known pre-existing list (repo steering)
  - Verify the two catalog copies are byte-identical
  - _Requirements: all_

- [x] 4. Portal deploy (designer shows the checkbox)
  - Deploy the portal so the catalog change is live (same `deploy_portal_fixes.sh` procedure as recent deploys; the workflow_core layer ships with the compute stack — verify which stacks carry the layer and deploy those)
  - Verify: fetch the deployed catalog surface (or the Lambda layer content) and confirm the `anomaly_mode` parameter is present
  - NOTE: the device-side executor change and vendored catalog ride the NEXT LocalServer build (do NOT start a build in this task; several device-side fixes are queued for the next build cycle)
  - _Requirements: 3.1_

- [x] 5. Anomaly mode preserves the model's textual notes (Requirement 5, added after on-device verification)
  - In `src/backend/workflow_engine/output_bindings.py` `_run_one` anomaly path: on a successful parse, return the verdict dict PLUS `bedrock_text` (raw answer) and nested `bedrock.{node_id: {"text": answer}}` — the same recording shape as freeform (process() already merges the nested sub-dict)
  - Unparseable answers keep the existing BedrockInferenceError path unchanged (5.3)
  - Extend `test/backend-test/workflow_engine/test_bedrock_response_mode.py`: anomaly-mode metadata carries verdict AND bedrock_text/nested text (Hypothesis over prompts/answers with valid verdict JSON plus surrounding prose — assert the FULL raw answer is recorded, not just the JSON); `{bedrock_text}` renders; mixed-mode test updated (anomaly node now also contributes text keys)
  - Update the anomaly-mode assertions in the same file's existing tests and any other suite asserting anomaly-mode metadata equals ONLY the verdict keys (test_workflow_bedrock_inference.py merge tests, test_bedrock_single_image_* where applicable)
  - Run the full bedrock test set + workflow_engine suite — green
  - Device-side change: rides the NEXT LocalServer build
  - _Requirements: 5.1, 5.2, 5.3_

## Notes

- Feature spec (not bugfix): tests accompany implementation in the same task.
- Device-side pieces (`output_bindings.py`, vendored catalog) ride the NEXT LocalServer build alongside anything else queued; portal-side catalog is live after task 4.
- Existing-workflow compatibility: bindings without `anomaly_mode` default to anomaly mode (Req 4.1) — packaged workflows keep working and gain the hardening once the device build ships.
- Known pre-existing test failures to ignore per repo steering (IAM CDK-synth, cdk.out drift, portal workflow test-runner, collection-order issues, stale-workflow-registrations exploration tests).
