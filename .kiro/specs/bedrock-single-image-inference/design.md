# Bugfix Design Document

## Overview

Make the reference image optional in `BedrockInferenceProcessor._run_one` (`src/backend/workflow_engine/output_bindings.py`): the primary `in` frame stays required; the `reference` frame, when unavailable for any reason (unwired port → `capturePaths.reference` None/missing, or wired but the captured file is missing/unreadable), is skipped with a logged warning and inference proceeds with the primary image alone. Both-frames behavior is byte-identical to today.

## Glossary

- **`in` frame / primary image**: the frame captured from the node's input port (`capturePaths['in']`), always required.
- **`reference` frame**: the frame captured from the node's reference port (`capturePaths['reference']`); the compiler emits None when the port is not fed (see `test_compiler_bedrock.py::test_unfed_reference_port`).
- **invoker**: the injectable callable `(model, prompt, images, region, max_tokens) -> str`; `images` is a list of `(label, jpeg_bytes)` pairs. `_default_bedrock_invoker` attaches one image content block per pair — it already handles any list length, no change needed there.

## Bug Details

### Root Cause

`_run_one` iterates `(("in", "Input image"), ("reference", "Reference image"))` and applies the same hard-fail logic to both ports: missing path → `BedrockInferenceError` ("no captured frame for its '<port>' input"); unreadable file → `BedrockInferenceError` ("could not read the captured '<port>' frame"). The contract mismatch: the portal compiler treats the reference port as optional (emits `capturePaths.reference = None`), the executor treats it as mandatory.

### isBugCondition

isBugCondition(run) — a `bedrock_inference` binding whose `in` frame is available but whose `reference` frame is unavailable (path None/missing in capturePaths, or the file unreadable at the resolved path).

## Expected Behavior

For any bug-condition run: the processor invokes Bedrock with `images = [("Input image", <in bytes>)]`, logs a warning naming the node and the reason the reference was skipped, and merges the parsed answer into the metadata exactly as in the two-image path. For non-bug-condition runs (both frames available, or the `in` frame itself unavailable), behavior is unchanged.

## Correctness Properties

Property 1: Bug Condition - Single available primary image is sufficient

_For any_ `bedrock_inference` binding whose `in` frame is captured and readable (isBugCondition — the reference frame unavailable in any of the three shapes: `capturePaths.reference` absent, None, or the file missing/unreadable), the fixed processor SHALL invoke the injected invoker exactly once with an images list containing exactly the one `("Input image", ...)` pair carrying the primary frame's bytes, SHALL NOT raise for the missing reference, and SHALL merge the parsed answer into the run metadata identically to the two-image path.

**Validates: Requirements 2.1, 2.2**

Property 2: Preservation - Two-image and no-primary behavior unchanged

_For any_ binding where both frames are captured and readable, the fixed processor SHALL make an invoker call byte-identical to the unfixed processor (same model, prompt, images list with both labeled pairs in the same order, region, max_tokens) with identical merged metadata; _for any_ binding where the `in` frame is unwired, missing, or unreadable, the fixed processor SHALL raise `BedrockInferenceError` carrying the node id (same surfacing contract); invoker failures and unparseable answers SHALL continue to raise `BedrockInferenceError` with the node id.

**Validates: Requirements 2.3, 2.4, 3.1, 3.2, 3.3, 3.4**

## Fix Implementation

### Changes Required

**Location**: `src/backend/workflow_engine/output_bindings.py::BedrockInferenceProcessor._run_one` only.

1. Split frame collection into required and optional handling:
   - `in` port: unchanged — missing path or unreadable file raises `BedrockInferenceError` exactly as today (same messages).
   - `reference` port: if the path is absent/None, log a warning ("node '<id>': reference port not fed by any video source; performing single-image inference") and continue without it; if the path resolves but the file cannot be read, log a warning with the OSError detail and continue without it.
2. The invoker call site is unchanged — `images` simply has one or two pairs. `_default_bedrock_invoker` already iterates the list generically.
3. No changes to `bindings()`, `process()`, `parse_bedrock_answer`, error surfacing, the compiler, or the portal.

### Test Seam

The processor is already fully injectable (invoker records calls; `work_dir` is a tmp_path). Existing harness: `test/backend-test/workflow_engine/test_workflow_bedrock_inference.py` (RecordingInvoker, make_document, write_frames). Note two existing tests encode the OLD behavior and must be updated to the new contract as part of the fix: `TestBedrockInferenceProcessor::test_missing_captured_frame_fails_with_the_node` (reference file missing → currently expects raise; becomes single-image success) and `test_unfed_port_fails_with_the_node` (`reference: None` → currently expects raise; becomes single-image success). Their inverses (missing/unfed `in` frame) must still fail and gain explicit coverage.

## Testing Strategy

- Exploration (Property 1, fails on unfixed code): reference-None, reference-key-absent, and reference-file-missing cases each assert single-image invocation succeeds with exactly one ("Input image", bytes) pair and merged metadata.
- Preservation (Property 2, passes on unfixed code): both-frames invoker-call equality (Hypothesis over prompt/model/params); missing/unfed `in` frame still raises with node id; raising invoker still surfaces `BedrockInferenceError`; no-bindings passthrough.
- Update the two legacy tests noted above in the fix task (they assert the defect); run the full workflow_engine suite as the checkpoint.
