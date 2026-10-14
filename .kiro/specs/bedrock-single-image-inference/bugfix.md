# Bugfix Requirements Document

## Introduction

The workflow engine's `BedrockInferenceProcessor` (`src/backend/workflow_engine/output_bindings.py::_run_one`) hard-requires BOTH captured frames — the `in` (primary/input) frame and the `reference` frame — and raises `BedrockInferenceError` (failing the whole run) if either is unavailable. The portal-side compiler, however, explicitly permits a `bedrock_inference` node whose reference port is not fed by any video source: it emits `capturePaths.reference = None` (asserted by `edge-cv-portal/backend/layers/workflow_core/tests/test_compiler_bedrock.py`). The two sides disagree on the contract, so a workflow wiring only the primary image to the Bedrock node compiles and packages successfully but can never execute on device.

The fix makes the reference image optional: when the reference frame is unavailable (port unwired, or the captured file missing/unreadable at run time), the processor performs inference with the primary image alone, logging the omission. The primary `in` frame remains required — its absence still fails the node with the existing error surfacing (node id, message).

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a `bedrock_inference` node's reference port is not fed by any video source (the compiler emits `capturePaths.reference = None`) THEN the executor raises `BedrockInferenceError` ("has no captured frame for its 'reference' input") and the run finalizes FAILED — even though the compiler accepted the workflow

1.2 WHEN the reference port is wired but the captured reference file is missing or unreadable at run time THEN the executor raises `BedrockInferenceError` and the run finalizes FAILED, instead of proceeding with the available primary frame

1.3 WHEN a user builds a single-image Bedrock inspection workflow (primary image only) THEN the system can never execute it on device — Bedrock inference always requires two images

### Expected Behavior (Correct)

2.1 WHEN the `in` frame is captured and readable and the reference port is unwired (`capturePaths.reference` missing or None) THEN the system SHALL invoke Bedrock with only the primary image attached (single image content block) and process the answer normally

2.2 WHEN the `in` frame is captured and readable and the reference port is wired but its captured file is missing or unreadable THEN the system SHALL log a warning naming the node and the unavailable reference frame, invoke Bedrock with only the primary image, and process the answer normally

2.3 WHEN both frames are captured and readable THEN the system SHALL invoke Bedrock with both images attached exactly as today (same labels, same order: Input image then Reference image)

2.4 WHEN the `in` (primary) frame is unwired, missing, or unreadable THEN the system SHALL CONTINUE TO raise `BedrockInferenceError` naming the node — inference with no image is never attempted

### Unchanged Behavior (Regression Prevention)

3.1 WHEN both frames are available THEN the system SHALL CONTINUE TO produce a byte-identical invoker call (model, prompt, images list with both labeled frames, region, max_tokens) and identical merged metadata

3.2 WHEN the Bedrock invocation itself fails or the answer is unparseable THEN the system SHALL CONTINUE TO raise `BedrockInferenceError` carrying the failing node id (the existing failure-surfacing contract)

3.3 WHEN a document has no `bedrock_inference` bindings THEN the system SHALL CONTINUE TO pass tag_values through unchanged

3.4 WHEN answer parsing (`parse_bedrock_answer`) and metadata merging run THEN the system SHALL CONTINUE TO behave identically — the fix touches only frame collection in `_run_one`
