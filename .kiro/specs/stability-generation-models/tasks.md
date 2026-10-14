# Implementation Plan: Stability Generation Models

## Overview

Restore the synthetic defect generation pipeline by adding the Stability inpaint model (invoked via its inference profile) and making the availability filter lifecycle-aware. Work proceeds in the design's architectural order: freeze the Amazon request-body oracle first (before the relocation touches it), then the pure logic in `synthetic_core.py` with its property tests close behind, then the `synthetic_data.py` I/O seams, then the existing-test seam renames and example tests. All backend code is Python; test files live in `edge-cv-portal/backend/tests/`. There are zero frontend changes (`SyntheticData.test.tsx` must keep passing as-is) and zero infrastructure changes (the handler role's Bedrock grant already covers inference-profile ARNs).

Most property tests are required rather than optional: the pipeline is currently fully broken, and these are cheap pure-function tests that gate the fix.

## Tasks

- [x] 1. Freeze the Amazon request-body reference oracle
  - [x] 1.1 Copy the current `_build_image_request` implementation verbatim from `synthetic_data.py` into a new test file `edge-cv-portal/backend/tests/test_property_amazon_request_preservation.py` as a private `_reference_build_image_request` oracle
    - This MUST happen before task 2.3 relocates the function; the frozen copy is the byte-preservation reference, mirroring the pattern in `test_property_bedrock_sampling_preservation.py`
    - Include a module docstring noting the oracle is a frozen pre-change copy and must never be edited
    - _Requirements: 2.2, 8.1_

- [x] 2. Implement pure logic in synthetic_core.py
  - [x] 2.1 Add the Stability catalog entry, `invocation_model_id`, and lifecycle-aware `filter_available_models`
    - Append the `stability.stable-image-inpaint-v1:0` entry (with `invocation_id: "us.stability.stable-image-inpaint-v1:0"`, inpainting+seed true, text_to_image/image_variation/cfg_scale false, `max_images_per_call: 1`) after the untouched Amazon entries; Nova Canvas and Titan entries are not modified
    - Add `invocation_model_id(entry)` returning `entry.get("invocation_id") or entry["model_id"]`
    - Change `filter_available_models` to take model summaries (`{"model_id", "lifecycle_status"}`), admit only ACTIVE entries matched by bare `model_id` (never `invocation_id`), preserving catalog order
    - _Requirements: 1.1, 1.2, 4.1, 4.3, 4.4, 5.1, 5.3, 6.2_

  - [x] 2.2 Implement `derive_mask_rect(task_seed, image_width, image_height)`
    - Splitmix64-style integer mixer over the seed (no `random` module); sides in the clamped 15-40% band (min 1 px), center-biased placement with 10% margin fallback, rectangle always fully in-bounds; degenerate 1x1 images yield the 1x1 rectangle at the origin
    - Returns `{"left", "top", "width", "height"}`
    - _Requirements: 3.2, 3.3_

  - [x] 2.3 Relocate the Amazon builder and add `build_stability_inpaint_request_body`
    - Move `_build_image_request` verbatim (body unchanged) from `synthetic_data.py` to `synthetic_core.py` as `build_amazon_request_body`; update `synthetic_data.py` to import and call it so nothing breaks mid-stream
    - Add `build_stability_inpaint_request_body(prompt, source_image_b64, mask_image_b64, seed, output_format="png")` producing exactly `{image, mask, prompt, seed, output_format}` with seed passed unmodified, `seed` omitted when None, and no capability-excluded parameters (no negative_prompt, no guidance/cfg keys)
    - _Requirements: 2.2, 2.3, 2.4, 7.2, 8.1_

  - [x] 2.4 Implement `extract_stability_result` and `StabilityGenerationError`
    - Return `images[0]` when `finish_reasons[0]` is null and `images` is non-empty; raise `StabilityGenerationError` (subclass of `SyntheticCoreError`) with the reported reason in the message when `finish_reasons[0]` is non-null or `images` is empty
    - _Requirements: 2.5, 2.6_

  - [x] 2.5 Promote `select_generation_method(source_class, capabilities)` to synthetic_core.py
    - `"inpainting"` for normal sources on inpainting-capable models; `"image_variation"` when supported; otherwise raise `ValidationError` naming the missing image-variation capability; remove/delegate the private `_select_generation_method` in `synthetic_data.py`
    - _Requirements: 3.1, 3.5_

  - [x] 2.6 Implement `classify_bedrock_invocation_error(error_code, error_message, model_id)`
    - Total function, always a non-empty string: AccessDeniedException → model-access-not-granted reason containing the model id; ResourceNotFoundException with a Legacy-marking message → lifecycle-status reason; anything else → `"<code>: <message>"` passthrough
    - _Requirements: 9.1, 9.2_

- [x] 3. Property tests for the pure core functions
  - [x] 3.1 Write property test for lifecycle-aware availability filtering (`test_property_stability_model_filtering.py`)
    - **Property 1: Lifecycle-aware availability filtering is exact**
    - **Validates: Requirements 1.3, 4.3, 5.1, 5.2, 6.2, 8.2**
    - Generators cover LEGACY statuses, absent models, and entries with/without `invocation_id`

  - [x] 3.2 Write property test for Amazon request-body byte-preservation (in `test_property_amazon_request_preservation.py`, against the task 1.1 frozen oracle)
    - **Property 2: Amazon request body byte-preservation**
    - **Validates: Requirements 2.2, 2.4, 8.1**
    - Compare `json.dumps` bytes of `build_amazon_request_body` vs the frozen `_reference_build_image_request` across methods, seeds (incl. None), params (cfg_scale present/absent), and mask prompts

  - [x] 3.3 Write property test for the Stability inpaint request body (`test_property_stability_request_bodies.py`)
    - **Property 3: Stability inpaint request body exact schema and seed passthrough**
    - **Validates: Requirements 2.3, 2.4, 7.2**
    - Exact key set `{image, mask, prompt, seed, output_format}`, seed unmodified in 0..858,993,459, `seed` omitted when None, no excluded parameters ever present

  - [x] 3.4 Write property test for Stability response extraction (`test_property_stability_request_bodies.py`)
    - **Property 4: Stability response extraction is total over payload shapes**
    - **Validates: Requirements 2.5, 2.6**
    - Cover all documented `finish_reasons` values and empty `images` lists

  - [x] 3.5 Write property test for mask rectangle derivation (`test_property_stability_mask.py`)
    - **Property 5: Mask rectangle derivation is deterministic and in-bounds**
    - **Validates: Requirements 3.3**
    - Cover 1-pixel and tiny images; assert determinism, in-bounds placement, and the 15-40% clamped size band

  - [ ]* 3.6 Write property test for invocation identifier selection (`test_property_stability_model_filtering.py`)
    - **Property 9: Invocation identifier selection**
    - **Validates: Requirements 4.2, 4.4**
    - Optional: largely pinned by the catalog unit test (8.1) and the worker moto tests (8.2)

  - [x] 3.7 Write property test for invocation failure classification (`test_property_stability_failure_classification.py`)
    - **Property 10: Bedrock invocation failure classification is total**
    - **Validates: Requirements 9.1, 9.2**

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - Run the new property files plus the existing synthetic property files explicitly (this repo has known moto fixture leakage in unrelated test families during full sweeps; run the synthetic files standalone)

- [x] 5. Implement I/O seams in synthetic_data.py
  - [x] 5.1 Replace `_list_available_model_ids` with `_list_available_models` and update `MODELS_EMPTY_GUIDANCE`
    - Return `[{"model_id", "lifecycle_status"}]` from `list_foundation_models(byOutputModality='IMAGE')`, defaulting a missing `modelLifecycle` to ACTIVE; wire `get_models` through the new summaries into the lifecycle-aware `filter_available_models`
    - Update `MODELS_EMPTY_GUIDANCE` to also name the Stability inpaint model
    - _Requirements: 1.3, 5.1, 5.2, 6.2_

  - [x] 5.2 Add `_render_mask_png` and `_source_image_dimensions`
    - Pillow (imaging layer already attached): `Image.new('L', (w, h), 0)` + `ImageDraw.rectangle(fill=255)` saved to an in-memory PNG; `_source_image_dimensions(image_bytes)` returns `(width, height)` via `Image.open`, raising a reason naming the unreadable source image on decode failure
    - _Requirements: 3.2_

  - [x] 5.3 Add provider dispatch, inference-profile invocation, mask_region recording, and ClientError classification to the worker `invoke_task`
    - Dispatch on `task["model_id"].split(".", 1)[0]`: `stability.` path reads source dims, derives the mask rect from the Task_Seed, renders the mask PNG, builds the Stability body, and invokes via `_invoke_stability_model(invocation_model_id(entry), body)` which delegates parsing to `extract_stability_result` and base64-decodes to image bytes; `amazon.` path is unchanged (imported `build_amazon_request_body`, `invocation_model_id(entry)` == bare id)
    - Record `generation_method: 'inpainting'` and `mask_region` (the derived rect) on Stability previews; preserve the exact existing preview field set otherwise; staging PUT unchanged; all Bedrock clients remain the module-level portal-region clients
    - Wrap `InvokeModel` for both providers: on `ClientError`, raise `RuntimeError(classify_bedrock_invocation_error(code, message, invoke_id))` so `execute_generation_tasks` records it as the per-task `failure_reason` and continues
    - _Requirements: 2.1, 2.5, 3.2, 3.4, 4.2, 4.4, 6.1, 7.1, 7.3, 9.1, 9.2, 9.3_

  - [x] 5.4 Enforce capability rejection in the generate endpoint
    - Call `select_generation_method` inside the existing `ValidationError -> 400` block so a Defect_Image session targeting the Stability inpaint model is rejected with the missing-capability message before any plan persists; the worker calls the same function when building tasks
    - _Requirements: 3.1, 3.5_

- [x] 6. Update existing test seams
  - [x] 6.1 Move the `_list_available_model_ids` patch sites to `_list_available_models`
    - `test_synthetic_data_unit.py` (2 sites) and `test_property_synthetic_rbac.py` (1 site): patch the new name and return summaries (`{"model_id", "lifecycle_status": "ACTIVE"}`) instead of bare id lists
    - _Requirements: 8.2, 8.3_

  - [x] 6.2 Extend the existing plan-completeness property's model-id strategy to include the Stability model id
    - _Requirements: 1.4, 7.1_

- [x] 7. Property tests for imaging and annotation
  - [x] 7.1 Write property test for rendered mask PNG (`test_property_stability_mask.py`)
    - **Property 6: Rendered mask PNG is binary and matches the rectangle**
    - **Validates: Requirements 3.2**
    - Pillow in-memory only, no AWS mocks: decode the PNG, assert exact source dimensions, every pixel 0 or 255, and the 255 set equals the rectangle's area

  - [ ]* 7.2 Write property test for mask_region annotation precedence (`test_property_stability_annotation.py`)
    - **Property 7: Mask_Region takes annotation precedence**
    - **Validates: Requirements 3.4**
    - Optional: `_annotate_preview`'s `mask_region` → `'inpainting_mask'` precedence is pre-existing behavior, also pinned by the integration example (8.3); stub the S3 client argument (never reached when `mask_region` is present)

- [x] 8. Example and unit tests
  - [x] 8.1 Write unit tests for catalog statics and the models endpoint (`test_synthetic_data_unit.py`)
    - Stability entry flags and `invocation_id`; Nova Canvas entry retained in the catalog
    - With patched `_list_available_models`: Stability ACTIVE → included in `GET /synthetic/models`; Nova LEGACY → excluded; updated `MODELS_EMPTY_GUIDANCE` returned when the available set is empty
    - _Requirements: 1.1, 1.2, 1.3, 4.1, 5.2, 5.3_

  - [x] 8.2 Write worker moto tests for provider dispatch and inference-profile invocation (`test_synthetic_data_unit.py`)
    - A Stability-model session produces previews with the same base field set as Amazon previews plus `mask_region` and `generation_method: 'inpainting'`, and the stubbed `bedrock-runtime` receives `us.stability.stable-image-inpaint-v1:0` as the modelId; an Amazon-model session still sends the bare model id
    - _Requirements: 4.2, 4.4, 7.3_

  - [ ]* 8.3 Write integration test for Stability session integration (`test_synthetic_integration.py`)
    - Integrating a Stability session yields manifest records with `bounding-box-source: 'inpainting_mask'` and the existing record shape
    - _Requirements: 3.4, 7.4_

  - [x] 8.4 Write generate-endpoint rejection test (`test_synthetic_data_unit.py`)
    - Defect-classified sources with the Stability model → 400 whose message names the missing image-variation capability, with no plan persisted
    - _Requirements: 3.5_

- [x] 9. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - Run the full synthetic test family standalone with an explicit file list (known moto fixture leakage affects unrelated families in full sweeps): all `test_property_synthetic_*.py`, the new `test_property_stability_*.py` and `test_property_amazon_request_preservation.py`, `test_synthetic_data_unit.py`, and `test_synthetic_integration.py`
    - Existing synthetic suite must pass unchanged apart from the task 6.1 seam renames (pins Requirements 7.3, 8.1, 8.2, 8.3, 9.3)
    - Confirm `SyntheticData.test.tsx` passes unchanged with zero frontend code changes (Requirements 1.5, 8.2)

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP; most property tests are intentionally required since the pipeline is currently fully broken and they are cheap pure-function tests
- Task 1.1 MUST run before task 2.3: the byte-preservation oracle is frozen from the pre-relocation `_build_image_request`
- No frontend tasks: the design specifies zero frontend changes; capability-driven controls render the new entry as-is
- No infrastructure tasks: the handler role's existing Bedrock grant already covers inference-profile ARNs
- Live verification (deploy and a real generation session on the portal account: Stability model in the dropdown, Nova Canvas absent, previews with mask regions, manifest integration) is an orchestrator task performed after implementation, not a task in this plan
- Each task references specific requirements for traceability; checkpoints ensure incremental validation

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1"] },
    { "id": 1, "tasks": ["2.2", "3.1", "5.1", "6.2"] },
    { "id": 2, "tasks": ["2.3", "3.5", "3.6", "5.2", "6.1"] },
    { "id": 3, "tasks": ["2.4", "3.2", "3.3", "7.1", "7.2", "8.1"] },
    { "id": 4, "tasks": ["2.5", "3.4"] },
    { "id": 5, "tasks": ["2.6", "5.4"] },
    { "id": 6, "tasks": ["3.7", "5.3"] },
    { "id": 7, "tasks": ["8.2", "8.3"] },
    { "id": 8, "tasks": ["8.4"] }
  ]
}
```
