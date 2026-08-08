# Implementation Plan: Edge VLM Image Inference

## Overview

Implementation follows the data-flow order of the design: the shared `workflow_core` compiler gains the `llm_inference` capture plan first (canonical layer copy, then re-vendored byte-identical into `src/backend/workflow_engine/vendor`), then the device side builds bottom-up — the `LlmInferenceProcessor` frame attachment (+ `pipeline_executor` work_dir wiring), the `Text_Generation_API` image field, and the vLLM runtime multimodal generation (manager + Triton generate-extension schema). Every property test sits directly beside the code it validates. No new Python modules are created, so no Dockerfile COPY changes are needed; device-side changes ride the next LocalServer build.

Test baselines that must stay green throughout: portal backend pytest scoped to `tests/` from `edge-cv-portal/backend`, the device backend suite under `test/backend-test/`, and the security/preservation gates at the final checkpoint. Python property tests use `hypothesis` as `test_property_*.py` (project default provides ≥100 iterations), each tagged `**Feature: edge-vlm-image-inference, Property {number}: {property title}**`.

## Tasks

- [x] 1. Compiler — capture plan for llm_inference bindings
  - [x] 1.1 Extend the frame-capture plan in the canonical compiler
    - In `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/compiler/compiler.py`: add `BINDING_LLM_INFERENCE = "llm_inference"`; collect llm node ids beside `bedrock_node_ids` and pass the union to `_bedrock_capture_plan` (one shared `path_for(feeder)` map so a feeder serving both kinds shares one capture file); emit `entry["capturePaths"]` for `llm_inference` bindings in the executor-bindings loop mirroring the Bedrock branch
    - Do NOT add llm nodes to the `opaque` set — frames must keep flowing through the collapsed executor-level node to downstream elements; `_build_segments` already handles feeders with downstream continuation plus a capture branch
    - _Requirements: 1.1, 1.2, 1.3, 1.4_

  - [x] 1.2 Write property test for fed/unfed capture path emission
    - **Property 1: Fed ports get capture paths, unfed ports get None**
    - **Validates: Requirements 1.1, 1.2**
    - hypothesis in `edge-cv-portal/backend/tests/` over workflow definitions with llm nodes whose `in` port is fed or unfed: `capturePaths.in` is a `{work_dir}` path iff fed, and every emitted path has a matching `multifilesink` location in the segments

  - [x] 1.3 Write property test for feeder capture sharing
    - **Property 2: Feeder capture files are shared, one sink per feeder**
    - **Validates: Requirements 1.3**
    - workflows where one source feeds multiple llm and/or bedrock nodes: exactly one capture chain per feeder, all consuming bindings reference the same path

  - [x] 1.4 Write property test for llm-free compilation identity
    - **Property 3: Compilation identity for llm-free workflows**
    - **Validates: Requirements 1.5**
    - hypothesis over definitions containing no `llm_inference` node (reuse the existing definition generators): compiled per-architecture documents are unchanged by the compiler edit

  - [x] 1.5 Write property test for stream topology preservation
    - **Property 4: Stream topology preservation for llm workflows**
    - **Validates: Requirements 1.4**
    - compiled llm-workflow segments with the synthetic capture chains stripped equal the pre-feature pass-through segment structure

  - [x] 1.6 Re-vendor the compiler into the workflow engine
    - Copy the edited compiler verbatim to `src/backend/workflow_engine/vendor/workflow_core/compiler/compiler.py`; verify the two copies are byte-identical (diff)
    - _Requirements: 1.6_

- [x] 2. Processor — frame attachment (`src/backend/workflow_engine/`)
  - [x] 2.1 Attach the captured frame in LlmInferenceProcessor
    - `output_bindings.py`: `LlmInferenceProcessor.process(document, tag_values, work_dir=None)` threads `work_dir` to `_run_one`; `_run_one` resolves `capturePaths["in"]` — path readable → base64 image to the invoker with `{work_dir}` substituted; absent/None → image `None`, request byte-identical to today; path set but unreadable → `{'error': ...}` naming node/port/path, invoker never called; prompt rendering, anomaly mode, and containment untouched
    - `_default_llm_invoker(model_name, prompt, parameters, image_b64=None)`: add `"image": image_b64` to the POST body only when set; 409-loading polling, timeout, and error shape unchanged
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 6.1, 6.4_

  - [x] 2.2 Pass the per-run work directory from the executor
    - `pipeline_executor.py` (~line 1604): `self._llm_processor.process(document, tag_values, work_dir)`; `_needs_work_dir` already scans all bindings' `capturePaths`, so no other executor change
    - _Requirements: 2.4_

  - [x] 2.3 Write property test for the image attachment trichotomy
    - **Property 5: Processor image attachment trichotomy**
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.5, 6.1**
    - hypothesis in `test/backend-test/workflow_engine/` (style of `test_workflow_llm_inference.py`): tmp work dirs, random JPEG bytes, injected invoker; the three `capturePaths` shapes produce attach / text-only-identical / zero-invocations-with-error respectively, remaining bindings still processed

  - [x] 2.4 Write property test for behavior invariance under image attachment
    - **Property 10: Processor behavior invariance under image attachment**
    - **Validates: Requirements 5.1, 6.4**
    - same binding run with and without a captured frame: rendered prompt, anomaly-mode instruction/verdict handling, and error containment identical; a raising invoker with an image present records `{'error': ...}` and continues

  - [x] 2.5 Write unit tests for executor wiring and node-failure marking
    - Stub processor asserts `work_dir` is passed by the executor; an image-read error record marks that node failed in the status map while independent nodes complete
    - _Requirements: 2.4, 5.2_

- [x] 3. Checkpoint — device workflow_engine suite
  - Run `test/backend-test/workflow_engine/` — all tests pass (including the pre-existing llm/bedrock suites, unchanged). Ensure all tests pass, ask the user if questions arise.

- [x] 4. Text_Generation_API — optional image field (`src/backend/endpoints/text_generation.py`)
  - [x] 4.1 Validate and decode the image in the pure normalization core
    - `normalize_generation_request`: optional `image` field — absent leaves the result identical to today; present must be a base64 string decoding to 1..`MAX_IMAGE_BYTES` bytes (findings name the `image` field and reason otherwise); valid → `effective["image_bytes"]`
    - `MAX_IMAGE_BYTES` default 16 MiB, env-overridable `TEXT_GEN_MAX_IMAGE_BYTES` (pattern of `TEXT_GEN_RETRY_LIMIT`)
    - _Requirements: 3.1, 3.3, 3.4, 3.5, 6.2_

  - [x] 4.2 Forward image bytes and report image_used
    - `generate_text` / `generate_text_stream`: pass `image=effective.get("image_bytes")` to the runtime only when present; non-streaming response gains `"image_used"` (from `runtime.image_supported(model_name)`) only for image-carrying requests — text-only responses byte-identical
    - _Requirements: 3.2, 3.6_

  - [x]* 4.3 Write property test for image validation exactness
    - **Property 6: Image validation exactness at the API boundary**
    - **Validates: Requirements 3.1, 3.3, 3.4, 3.5, 6.2**
    - hypothesis over request bodies (absent / valid base64 of varying sizes around the cap / non-string / invalid base64 / empty): findings name `image` iff invalid; absent-image normalization equals pre-feature output; fake runtime never invoked on findings

  - [x]* 4.4 Write property test for the image byte round trip
    - **Property 7: Image bytes round-trip to the runtime**
    - **Validates: Requirements 3.2**
    - fake runtime (dependency override) captures `image=`: received bytes equal `b64decode(request.image)`; prompt and sampling params unchanged versus the imageless request

  - [x]* 4.5 Write property test for image_used reporting
    - **Property 9: image_used reporting**
    - **Validates: Requirements 3.6, 4.3**
    - fake runtime with toggled `image_supported`: response `image_used` mirrors capability for image-carrying requests; imageless responses carry no new keys

- [x] 5. vLLM runtime — multimodal generation (`src/backend/vllm_runtime/`)
  - [x] 5.1 Build multimodal prompts in the manager
    - `manager.py`: `generate`/`generate_stream`/`_request` gain `image: Optional[bytes] = None`; `_is_multimodal(model_name)` from the loaded engine's model config (Qwen2-VL / Qwen2.5-VL architectures at minimum), exposed as `image_supported(model_name)`; `_build_multimodal_prompt` decodes bytes to a PIL image (failure → `GenerationError` naming the decode, engine never invoked) and chat-templates `[{"type": "image"}, {"type": "text", ...}]` with the model tokenizer (Qwen VL literal fallback when no template) into `{"prompt": ..., "multi_modal_data": {"image": ...}}`
    - `_request`: image None → bare prompt string byte-identical; multimodal → prompt dict; non-multimodal with image → warning + bare prompt string; engine failures keep the existing `GenerationError` isolation
    - PIL import stays lazy inside the multimodal path
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 6.3_

  - [x] 5.2 Extend the Triton generate-extension schema
    - `server.py`: `GenerateRequest` gains `image: Optional[str] = None`; both generate endpoints decode it and pass `image=` to the manager (bad base64 → 422)
    - _Requirements: 4.8_

  - [x]* 5.3 Write property test for the prompt-construction trichotomy
    - **Property 8: Runtime prompt-construction trichotomy**
    - **Validates: Requirements 4.1, 4.3, 4.4, 6.3**
    - fake engine capturing the prompt argument, stubbed model configs: no image → bare string identical to pre-feature; multimodal → dict with `multi_modal_data` image and placeholder-bearing text; non-multimodal + image → bare string with `image_supported` False; generator includes valid JPEG bytes (tiny generated images) and non-image bytes for the decode-failure edge (4.7)

  - [x]* 5.4 Write unit tests for detection, error paths, and server pass-through
    - Multimodal detection examples: Qwen2-VL / Qwen2.5-VL configs → True, text-only (e.g. OPT) → False; engine raising during a multimodal generate → `GenerationError` with model name and reason, other models untouched; non-image bytes → `GenerationError`, engine call count 0; `TestClient` POST with `image` → fake manager receives the decoded bytes
    - _Requirements: 4.2, 4.5, 4.6, 4.7, 4.8_

- [x]* 6. On-hardware harness smoke stage
  - Add to `test/on-hardware/harness/stages/` a text-generation image smoke: POST an image-carrying generate to a deployed Qwen VL model → 200 with `image_used: true` and a non-empty answer; and a text-only generate unchanged (1–2 examples, integration — not property-based)
  - _Requirements: 4.5, 3.6_

- [x] 7. Final checkpoint — all baselines
  - Ensure all baselines pass: portal backend pytest scoped to `tests/` from `edge-cv-portal/backend`; the device backend suite under `test/backend-test/`; the security audit gates and preservation suite; verify the two compiler copies are byte-identical; the entire pre-existing test suite must pass unchanged (modulo the known pre-existing failures per repo steering). Ask the user if questions arise.

- [x] 8. Deploy coordination checkpoint
  - Portal side: the compiler change ships with the `workflow_core` layer — coordinate a portal deploy (same `deploy_portal_fixes.sh` procedure as recent deploys) so newly compiled workflows carry llm `capturePaths`; verify a recompiled folder_source → llm_inference workflow's document contains them
  - Device side: `output_bindings.py`, `pipeline_executor.py`, `text_generation.py`, `vllm_runtime/*`, and the vendored compiler ride the NEXT LocalServer build (do NOT start a build in this task; coordinate with whatever is queued for the next build cycle)
  - Rollout ordering is safe in both directions: old packages on a new engine run text-only byte-identically (no `capturePaths`); new packages on an old engine ignore the extra key (the frame-persistence path already tolerates llm `capturePaths`)
  - Ask the user before any deploy action.
  - _Requirements: all_

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP
- All 10 design properties are covered: Property 1 by 1.2, Property 2 by 1.3, Property 3 by 1.4, Property 4 by 1.5, Property 5 by 2.3, Property 10 by 2.4, Property 6 by 4.3, Property 7 by 4.4, Property 9 by 4.5, Property 8 by 5.3
- Python property tests use hypothesis with no hardcoded `max_examples` (project default ≥100 iterations); each tagged `**Feature: edge-vlm-image-inference, Property {number}: {property title}**`
- All device-side tests run against fakes (fake engine, injectable invoker, FastAPI dependency override, tmp-path filesystems); no test requires GPU hardware or a real vLLM install
- Known pre-existing test failures to ignore per repo steering (IAM CDK-synth, cdk.out drift, portal workflow test-runner, collection-order issues, stale-workflow-registrations exploration tests)
- Existing-workflow compatibility: already-deployed packages carry no llm `capturePaths` and keep byte-identical text-only behavior; the fix becomes effective for a given workflow only after recompiling/repackaging it with the deployed compiler AND the device running the new LocalServer build

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "4.1", "5.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "1.5", "1.6"] },
    { "id": 2, "tasks": ["2.2", "4.2", "5.2", "2.3", "2.4"] },
    { "id": 3, "tasks": ["2.5", "3", "4.3", "4.4", "4.5"] },
    { "id": 4, "tasks": ["5.3", "5.4", "6"] },
    { "id": 5, "tasks": ["7"] },
    { "id": 6, "tasks": ["8"] }
  ]
}
```
