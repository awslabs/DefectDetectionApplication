# Design Document

## Overview

Two small, paired changes:

1. **Executor** (`src/backend/workflow_engine/output_bindings.py::BedrockInferenceProcessor._run_one`): read the binding's `anomaly_mode` parameter (absent → True). Anomaly mode: append the canonical JSON instruction constant (`BEDROCK_JSON_INSTRUCTION`, new module constant) to the prompt with a blank-line separator, invoke, `parse_bedrock_answer`, merge — as today. Freeform mode: invoke with the prompt as-is, return `{"bedrock_text": text, "bedrock": {node_id: {"text": text}}}` for the metadata merge (mirroring the llm_inference nesting pattern), no parsing.
2. **Catalog** (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py` + vendored copy `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`, identical edits): add `ParameterDescriptor("anomaly_mode", "bool", required=False, default=True, description=...)` to `BEDROCK_INFERENCE.parameters`; update the `prompt` parameter description per Req 3.3. Bool parameters already render as checkboxes in the designer (mqtt node's `greengrass`/`aws_iot` precedent) — no frontend change. The compiler copies node parameters into the executor binding generically — no compiler change.

## Key decisions

- **Instruction constant lives in the executor**, not the prompt default: `BEDROCK_JSON_INSTRUCTION = 'Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.'` Appended as `prompt + "\n\n" + BEDROCK_JSON_INSTRUCTION` in anomaly mode. `BEDROCK_DEFAULT_PROMPT` in the catalog is simplified to drop its inline JSON-shape sentence (the executor now guarantees it); kept semantically equivalent ("Compare the input image to the reference image; is_anomalous is true when the input meaningfully differs from the reference.").
- **`process()` merge shape for freeform**: `metadata.update(self._run_one(...))` already merges whatever dict `_run_one` returns, so `_run_one` returns the verdict dict (anomaly) or the `bedrock_text`/`bedrock` dict (freeform). Multiple bedrock nodes in freeform: later nodes overwrite flat `bedrock_text` (documented; per-node `bedrock.{nodeId}.text` disambiguates) — same convention as the flat is_anomalous/confidence merge today. `_run_one` must merge into any existing `bedrock` sub-dict passed back via process()'s metadata rather than clobbering it; simplest: `process()` handles the nested merge for the `bedrock` key.
- **`{bedrock_text}` templating** comes free: `render_template` exposes all metadata keys as placeholders.
- **Parameter name** `anomaly_mode` (checkbox label derives from the descriptor display machinery; description states checked = anomaly JSON verdict, unchecked = freeform text).

## Correctness Properties

Property 1: Anomaly-mode prompt always carries the canonical instruction

_For any_ configured prompt string and binding whose `anomaly_mode` is absent or true, the invoker SHALL receive exactly `prompt + "\n\n" + BEDROCK_JSON_INSTRUCTION` as its prompt argument, and the parsed verdict SHALL merge into the metadata as today.

**Validates: Requirements 1.1, 1.2, 1.3, 4.1**

Property 2: Freeform mode records raw text and never parses

_For any_ configured prompt and any model answer text (parseable or not), a binding with `anomaly_mode: false` SHALL send the prompt unchanged, record the answer as `bedrock_text` and `bedrock.{nodeId}.text` in the returned metadata, add no `is_anomalous`/`confidence` keys, and never raise for answer format.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Property 3: Preservation - error surfacing, frame handling, and catalog surface unchanged

_For any_ binding in either mode: missing primary frame, raising invoker, and (anomaly mode only) unparseable answers surface `BedrockInferenceError` with the node id exactly as today; the single-image reference fallback and both-frames image list are byte-identical to today; documents without bedrock bindings pass tag_values through; the two catalog copies are byte-identical after the edit and all other node descriptors are untouched.

**Validates: Requirements 4.1, 4.2, 4.3, 4.4, 3.2**

## Testing Strategy

- Extend the existing injectable-invoker harness (`test/backend-test/workflow_engine/test_workflow_bedrock_inference.py` pattern). New test file per task; Hypothesis over prompt strings/answers where natural.
- Anomaly-mode hardening: invoker receives prompt + separator + instruction (property over generated prompts); default-absent parameter behaves as anomaly mode.
- Freeform: prompt passthrough, `bedrock_text` + nested key recorded, unparseable text fine, no verdict keys, template rendering of `{bedrock_text}` via render_template.
- Preservation: re-run the bedrock-single-image exploration/preservation suites and the legacy bedrock tests (existing tests assert the invoker's prompt equals the configured prompt — they will need the instruction-suffix expectation updated for anomaly mode, which is the intended contract change; update them in the same task with the docstring noting the new contract).
- Catalog: portal-side test asserting the new parameter descriptor exists with default True, and a vendored-copy byte-equality check (`diff` in test or existing sync test if present).
- Device-side change rides the NEXT LocalServer build; catalog change is portal-side (needs portal deploy) AND device-side vendored copy (rides the build).
