# Implementation Plan: VLM Anomaly + Reference Parity

## Overview

Give `llm_inference` the Bedrock node's authoring surface: the `anomaly_mode` checkbox (executor contract already shipped — this exposes it) and a `reference` VideoFrames port whose frame rides the generate request end to end (catalog → compiler capture plan → executor → Text_Generation_API → vLLM two-image prompt). Portal catalog piece needs a portal deploy; executor/API/runtime pieces ride the NEXT LocalServer build; workflows must be repackaged to gain `reference` capture paths (old packages tolerated).

## Task Dependency Graph

```json
{
  "waves": [
    {"wave": 1, "tasks": ["1", "3", "4"], "description": "1: catalog descriptor (both copies) + compiler verification. 3: Text_Generation_API reference_image. 4: vLLM runtime two-image support. Mutually independent modules."},
    {"wave": 2, "tasks": ["2"], "description": "2: executor reference attachment. Depends on 1 (capturePaths.reference shape) and 3 (request field name)."},
    {"wave": 3, "tasks": ["5"], "description": "Checkpoint: full suites, catalog byte-identity, frontend regression test."}
  ]
}
```

## Tasks

- [x] 1. Catalog: anomaly_mode parameter + reference port, compiler capture-plan verification (both copies)
  - `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`: `LLM_INFERENCE` inputs become `[PortDescriptor("in", PORT_TYPE_VIDEO_FRAMES), PortDescriptor("reference", PORT_TYPE_VIDEO_FRAMES)]`; add `ParameterDescriptor("anomaly_mode", "bool", required=False, default=False)` with the Bedrock-mirroring description (note the llm-specific default FALSE and contained verdict-parse failures); extend the `prompt_template` description with the anomaly-mode auto-append note; update the descriptor comment for two-frame capture
  - Verify the compiler needs NO change: `_bedrock_capture_plan` iterates descriptor inputs, so `capturePaths["reference"]` (path when fed / `None` when unfed) and feeder sink sharing must fall out of the descriptor edit — if any compiler assumption breaks, fix it in both copies
  - Re-vendor via `src/backend/workflow_engine/vendor/re_vendor.sh`; `diff` the two `nodes.py` (and `compiler.py` if touched) copies byte-identical
  - Regenerate `edge-cv-portal/backend/layers/workflow_core/tests/catalog_baseline.json` per the documented maintenance path (diff must show only the llm_inference additions); catalog content tests assert the new parameter and port list (`EXPECTED_TYPE_IDS` unchanged)
  - Run `cd edge-cv-portal/backend && python3 -m pytest layers/workflow_core/tests/ -q` — green
  - _Requirements: 1.1, 1.2, 2.1, 2.2, 7.4_

  - [ ]* 1.1 Write property test for the reference capture plan
    - **Property 1: Reference capture plan completeness**
    - Hypothesis over generated workflow definitions (reuse the bedrock/llm capture-plan generators): fed reference ⇔ `{work_dir}` path with a matching capture sink chain; unfed ⇒ `None`; shared feeders across bedrock/llm consumers get exactly one sink chain and a shared file
    - **Validates: Requirements 3.1, 3.2, 3.3**

  - [ ]* 1.2 Write property test for validator reference-port optionality
    - **Property 2: Validator accepts optional reference**
    - Hypothesis over definitions with unconnected llm `reference` ports: no finding attributable to the port
    - **Validates: Requirements 2.2**

  - [ ]* 1.3 Write property test for compilation non-interference
    - **Property 3: Compilation non-interference**
    - Hypothesis over in-only-llm and llm-free definitions: compiled documents (segments, capture plans, bedrock bindings, sim stub) identical to pre-feature except `capturePaths["reference"] = None` on llm bindings
    - **Validates: Requirements 3.4, 7.2, 7.3**

- [x] 2. Executor: reference frame attachment (`src/backend/workflow_engine/output_bindings.py`)
  - `LlmInferenceProcessor._run_one`: after the existing `in` block, read `capturePaths["reference"]` with Bedrock's optional semantics — `None`/absent/unreadable ⇒ warning log + single-image inference (never a node error); readable ⇒ base64-encode and call the invoker with the extended arity (`invoker(model, prompt, params, image_b64, reference_b64)`); keep the shipped shorter arities for no-reference/no-image calls so pre-feature injected invokers keep working
  - `_default_llm_invoker(..., reference_b64=None)`: add `"reference_image"` to the POST body only when set; 409-loading loop, timeout, and error shape untouched
  - Anomaly-mode handling, prompt rendering, verdict parsing, containment untouched
  - Extend `test/backend-test/workflow_engine/` llm inference tests: readable/None/absent/unreadable reference shapes, arity checks, no-error-on-missing-reference
  - Run the workflow_engine llm suites — green
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 7.1_

  - [ ]* 2.1 Write property test for processor reference-attachment trichotomy and invariance
    - **Property 4: Processor reference-attachment trichotomy and invariance**
    - Hypothesis over prompts, answers (parseable/unparseable), anomaly_mode values, and the three reference shapes: exactly one invocation, correct arity/argument, no node error for missing reference; prompt/verdict/containment identical across shapes (pre-feature bindings byte-identical)
    - **Validates: Requirements 1.3, 4.1, 4.2, 4.4, 7.1**

  - [ ]* 2.2 Write property test for invoker request-body additivity
    - **Property 5: Invoker request-body additivity**
    - Hypothesis over params/payloads with mocked `requests`: body carries `reference_image` iff supplied; reference-less bodies byte-identical to pre-feature
    - **Validates: Requirements 4.3**

- [x] 3. Text_Generation_API: reference_image field (`src/backend/endpoints/text_generation.py`)
  - Extract the inline `image` validation into a shared helper and apply it to both `image` and `reference_image` (findings name the failing field); add the `reference_image`-without-`image` rejection finding
  - `effective["reference_image_bytes"]` set only when valid; absent field ⇒ normalization byte-identical to pre-feature
  - `generate_text`/`generate_text_stream`: pass `reference_image=` to the runtime only when present (keyword, fake-runtime compatible)
  - Extend the text_generation endpoint tests: valid two-image pass-through, each invalid shape, absent-field identity
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 7.5_

  - [ ]* 3.1 Write property test for reference_image validation exactness
    - **Property 6: reference_image validation exactness**
    - Hypothesis over request bodies (valid/invalid base64, sizes, wrong types, reference-without-image, absent field): findings iff invalid; absent-field normalization identical; runtime never invoked on findings
    - **Validates: Requirements 5.1, 5.3, 5.4, 7.5**

  - [ ]* 3.2 Write property test for reference bytes round-trip
    - **Property 7: Reference bytes round-trip to the runtime**
    - Hypothesis over valid two-image requests with a fake runtime capturing kwargs: forwarded bytes equal the base64-decoded fields; prompt/sampling unchanged
    - **Validates: Requirements 5.2**

- [x] 4. vLLM runtime: two-image generation (`src/backend/vllm_runtime/manager.py`, `server.py`)
  - `generate`/`generate_stream`/`_request` gain `reference_image: Optional[bytes] = None` (threading only); `_request` extends the shipped prompt trichotomy — both images + multimodal ⇒ `_build_multimodal_prompt(model, prompt, image, reference_image)`
  - `_build_multimodal_prompt(..., reference_bytes=None)`: single-image path byte-identical to today; two-image path builds the labeled content list (`"Input image:"`, image, `"Reference image:"`, image, prompt) with `multi_modal_data: {"image": [pil_in, pil_ref]}`; reference decode failure raises `GenerationError` naming the reference before engine invocation; two-pad Qwen-VL literal fallback
  - `load()`: `engine_args.setdefault("limit_mm_per_prompt", {"image": 2})` before the engine factory (explicit model.json value wins)
  - `server.py`: `GenerateRequest.reference_image: Optional[str] = None`; both endpoints decode via `_decoded_image` and pass through
  - Unit tests: `limit_mm_per_prompt` defaulting (absent ⇒ defaulted, explicit ⇒ honored) with a fake engine factory; Triton server reference pass-through and invalid-base64 rejection
  - _Requirements: 5.5, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [ ]* 4.1 Write property test for runtime prompt-construction cases
    - **Property 8: Runtime prompt-construction cases**
    - Hypothesis over prompts and optional in-memory JPEG/garbage payloads with a fake engine capturing the prompt argument: bare string (no image), pre-feature single-image dict, ordered two-image dict (input before reference in text and `multi_modal_data` list), text-only warning path for non-multimodal stubs, `GenerationError` on undecodable reference with the engine never invoked
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5**

- [x] 5. Checkpoint — full verification
  - `cd edge-cv-portal/backend && python3 -m pytest layers/workflow_core/tests/ -q` — green; `diff` both catalog (and compiler, if touched) copies byte-identical
  - `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/` and the text_generation / vllm_runtime suites — no new failures (steering-known pre-existing failures tolerated)
  - Frontend regression: one vitest asserting the llm node renders `in` + `reference` input handles (catalog-driven generic path, Requirement 2.3); `npx vitest run` for the touched suite
  - Ensure all tests pass, ask the user if questions arise
  - _Requirements: all_

## Notes

- Tasks marked with `*` are optional property-test tasks and can be skipped for a faster MVP; core implementation tasks include their own example-based tests
- `anomaly_mode` defaults FALSE (unlike Bedrock's TRUE) to match the already-shipped executor default — existing llm workflows keep freeform behavior without repackage
- The executor anomaly-mode contract (Requirement 1.3) is already implemented (vlm-parity-run-results task 1); task 1 exposes it in the designer and Property 4 regression-covers it
- Old packages have no `capturePaths["reference"]` — the processor tolerates absence; workflows gain the reference path on repackage
- Ship vehicles: task 1's portal copy needs a portal compute-stack deploy; tasks 2–4 plus the vendored catalog ride the next LocalServer build
