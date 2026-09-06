# Implementation Plan: LLM Model Token Budget and Image Sizing

## Overview

Implementation proceeds bottom-up from the pure shared layer outward, so that "one downscale, one budget, computed in the code path both callers share" holds by construction. First the new `dda_llm_image.py` (the Image_Downscaler, with its pinned encoder block and the relocated header parser), the `resolve_token_budget` tier resolver in `dda_llm_request.py`, and `scale_detections` in `dda_llm_guidance.py` — three independent, dependency-light additions that carry most of the correctness weight and are testable with no AWS at all. Then the chokepoint `dda_llm_prelabel.generate_llm_prelabel` is extended to downscale, resolve the budget, split Sent from Source dimensions and return the Sent_Dimensions, with both callers updated for the new return shape in the same task so the tree never breaks. The settings surface (`llm_model_token_limits` item, its validator, its two routes, the `token_limit` option field and the memoized loader) lands beside that. The Preview_API then gains request validation, the two `RUN` attributes, the audit fields, the status and payload fields and the executor plumbing; the Auto_Labeler gains the two job-record reads. The cross-path property tests — byte identity, budget plumbing, few-shot downscaling, the closed category set, untouched families — land only once both paths are wired, because that equality is what they assert. Frontend and infrastructure are independent of the backend order and start in the first wave.

Backend tests live in `edge-cv-portal/backend/tests/` (pytest + moto + Hypothesis, stubbed Converse clients); frontend tests use vitest + `@testing-library/react` with fast-check; infrastructure tests use CDK assertions. Every correctness property from the design gets exactly one property-based test, at 100 iterations, in the file the design's placement table names, tagged `Feature: llm-model-token-and-image-sizing, Property {n}: {text}`.

## Tasks

- [x] 1. Implement the Image_Downscaler shared-layer module
  - [x] 1.1 Create `dda_llm_image.py` in the shared layer
    - Create `edge-cv-portal/backend/layers/shared/python/dda_llm_image.py` — module contract in the docstring: no boto3, no I/O, no network, and **no Pillow at import time** (`from PIL import Image` lives inside `_resize_and_encode` only)
    - `DOWNSCALE_OFF = None`, `MAX_IMAGE_EDGE_OPTIONS = (512, 768, 1024, 1280, 1536, 2048)`, `MAX_SOURCE_PIXEL_COUNT = 100_000_000`, `IMAGE_FORMAT_PNG`/`IMAGE_FORMAT_JPEG`, `DownscaleError` carrying reason text only (the caller owns the category)
    - `normalize_downscale_setting(value)`: total and safe in the shape of `resolve_model_image_limit` — absent, null, boolean, string, float and any integer outside `MAX_IMAGE_EDGE_OPTIONS` all resolve to `None`
    - `declared_dimensions(image_bytes)`: the PNG IHDR / JPEG SOF parse, relocated **verbatim** from `dda_autolabel_worker._image_dimensions` so this becomes the one copy; dependency-free
    - `downscale_image(image_bytes, image_format, downscale_setting, *, source_dimensions=None)` in the requirement's step order: Downscale_Off returns the same `bytes` object with no Pillow import; then header-only `Image.open(...).size`; then the `< 1` refusal; then the `> MAX_SOURCE_PIXEL_COUNT` refusal **before any `load()`**; then the already-fits pass-through; only then `_resize_and_encode`
    - Target size from the requirement's formula in integer arithmetic: `max(1, (source_edge * max_image_edge) // max(source_width, source_height))`
    - The pinned constant block: `RESAMPLING_FILTER = Image.Resampling.LANCZOS`, `REDUCING_GAP = 2.0`, `JPEG_SAVE_PARAMS`, `PNG_SAVE_PARAMS`, `JPEG_MODE_MAP` / `PNG_MODE_MAP` keyed on the source mode alone, `resized.info.clear()` before `save`, no `exif_transpose`, first frame only
    - `Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXEL_COUNT` at import of the resize path so the bomb guard coincides with this feature's bound; `DecompressionBombError` re-raised as `DownscaleError`
    - Output container is always the caller's key-derived format, never re-derived from content
    - _Requirements: 5.1, 5.9, 5.12, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.9, 6.10, 6.11, 7.6_

  - [x] 1.2 Write property test for the downscale algebra
    - `edge-cv-portal/backend/tests/test_property_image_downscaler.py` — pure, plus one `subprocess` run comparing sha256 digests for cross-process determinism, and a decoder spy asserting zero decodes on both pass-through branches
    - **Property 4: Downscaling is deterministic, shrinking, and idempotent at the bound**
    - **Validates: Requirements 6.2, 6.3, 6.4, 6.5, 6.6, 6.7**

  - [x] 1.3 Write property test for downscaler bounds and totality
    - `edge-cv-portal/backend/tests/test_property_image_downscaler_bounds.py` — `st.binary`, truncated/corrupt/empty bytes, hand-built zero-dimension and 20000×20000 headers; `tracemalloc` and `perf_counter` instrumentation
    - **Property 13: Downscaling is bounded in resource use and always yields one outcome**
    - **Validates: Requirements 6.9, 6.10, 6.11**

  - [x] 1.4 Write unit tests for `dda_llm_image`
    - `edge-cv-portal/backend/tests/test_dda_llm_image.py`: `normalize_downscale_setting` over the seven valid inputs and over `False`, `True`, `"1024"`, `1024.0`, `1023`, `4096`, `None`, `{}`
    - Downscale_Off returns the same `bytes` object and the passed-in dimensions with `sys.modules` asserted to hold no `PIL` entry; exact-bound source passes through; one-pixel-over is re-encoded; the floor formula hand-checked for 3000×2000 at all six options; 5000×1 at 512 → 512×1 and its transpose
    - Mode coverage (`P` → `RGBA`, `RGBA` JPEG → `RGB`, `L` stays `L`, `CMYK` → `RGB`); EXIF / ICC / JFIF density absent from the output; `Image.MAX_IMAGE_PIXELS == MAX_SOURCE_PIXEL_COUNT`; a `.jpg` key with PNG content re-encodes to a real JPEG at a bound and passes the PNG bytes through at Downscale_Off
    - _Requirements: 5.1, 5.9, 6.2, 6.3, 6.4, 6.5, 6.7, 6.10_

- [x] 2. Implement the token budget resolver and the coordinate scale-back (pure)
  - [x] 2.1 Add `resolve_token_budget` to `dda_llm_request.py`
    - `edge-cv-portal/backend/layers/shared/python/dda_llm_request.py`, beside `resolve_model_image_limit`, with **no new imports** so the module's purity contract is untouched
    - `MODEL_TOKEN_LIMIT_DEFAULT = 10000`, `MODEL_TOKEN_LIMIT_CEILING = 128000`, `_valid_token_value` rejecting `bool` before the `int` check and rejecting strings and floats with no numeric conversion and out-of-range integers with no clamping
    - Three tiers: valid selection, then `limits[model_identifier]` by exact string comparison (no trimming, no case folding) when the identifier is a string and the mapping is a dict, then the default
    - The deliberate divergence from `resolve_model_image_limit` documented in the docstring: a non-string identifier skips the lookup but does **not** discard a valid selection
    - _Requirements: 1.1, 1.2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10_

  - [x] 2.2 Write property test for budget resolution
    - `edge-cv-portal/backend/tests/test_property_token_budget_resolution.py` — pure, no AWS; inputs deep-copied and evaluated twice per example; a mapping subclass recording `get`/`__getitem__` asserts zero lookups for the non-string-identifier tier
    - **Property 1: Output token budget resolution is total and safe**
    - **Validates: Requirements 1.2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10**

  - [x] 2.3 Add `scale_detections` to `dda_llm_guidance.py`
    - `edge-cv-portal/backend/layers/shared/python/dda_llm_guidance.py`: `_scale_coordinate` using `math.floor(v * source / sent + 0.5)` (round-half-up, not `round`'s banker's rounding) then clamped into `[0, source_extent]`
    - `scale_detections(detections, sent_w, sent_h, source_w, source_h)` returns **the same list object** by early return when the dimension pairs are equal or either sent extent is non-positive — a genuine no-op, not a multiply by 1.0
    - Boxes mapped as two corners `(left, top)` and `(left + width, top + height)` with the extents re-derived as differences; polygons mapped per vertex; never called for Classification
    - _Requirements: 7.3, 7.4, 7.5, 7.8_

  - [x] 2.4 Write property test for the coordinate space transform
    - `edge-cv-portal/backend/tests/test_property_coordinate_space.py` — pure `scale_detections` + `guidance_to_prelabel`, no AWS; dimension pairs constrained to `sent <= source` with the equal case weighted and near-equal pairs (1001/1000) included for the sub-pixel-collapse edge
    - **Property 7: Pre_Label geometry is expressed in the original image's coordinate space**
    - **Validates: Requirements 7.3, 7.4, 7.5, 7.8**

  - [x] 2.5 Write unit tests for the resolver and the scale-back
    - `edge-cv-portal/backend/tests/test_dda_llm_request_token_budget.py`: the three tiers at `1`, `128000`, `0`, `128001`; `True`/`False` at both tiers; a `Decimal` value rejected (documenting why the loader converts); the non-string-identifier divergence asserted side by side with `resolve_model_image_limit`
    - `edge-cv-portal/backend/tests/test_dda_llm_guidance_scaling.py`: identity dimensions return the same list object; round-half-up at exactly `.5`; clamping at both ends; a box whose right edge lands on the source width; a polygon with vertices at `(0, 0)` and at the extent; the sub-pixel collapse raising the pre-existing `GuidanceError` with its pre-existing message
    - _Requirements: 2.2, 2.3, 2.4, 2.5, 2.8, 2.9, 2.10, 7.3, 7.4, 7.5, 9.3, 9.6_

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Extend the shared chokepoint and update both callers
  - [x] 4.1 Extend `generate_llm_prelabel` in `dda_llm_prelabel.py`
    - `edge-cv-portal/backend/functions/dda_llm_prelabel.py`: three new keyword arguments (`downscale_setting=None`, `token_budget_selection=None`, `model_token_limits=None`), all defaulting to pre-feature behavior; `width`/`height` keep their meaning as the **Source_Dimensions**
    - Downscale the target image and every attached Few_Shot_Example through `dda_llm_image.downscale_image` exactly once each, after `select_few_shot_examples` (selection stays independent of the setting), before any image becomes a Converse block
    - `inference_config = dict(build_inference_config(config))` then `inference_config['maxTokens'] = resolve_token_budget(...)` — `build_inference_config` gains no parameter and no branch and is never mutated in place
    - Sent/Source split: `build_detection_prompt` and `parse_guidance` receive the Sent_Dimensions; `scale_detections` maps into Source space for the geometry modalities only; `guidance_to_prelabel` receives the Source_Dimensions and is unchanged
    - Add `CATEGORY_UNSUPPORTED_IMAGE` / `CATEGORY_UNREADABLE_EXAMPLE` constants and raise `LlmPrelabelError` with them for a refused target and a refused example respectively, with the reason strings of the design's error-handling table; both imply zero invocations
    - Extend the return to carry the Pre_Label plus the Sent_Dimensions
    - _Requirements: 1.3, 1.4, 6.1, 6.8, 7.1, 7.2, 7.3, 7.4, 7.5, 7.8, 8.1, 8.2, 8.3, 8.6, 8.7, 8.8, 9.1, 9.2, 9.4, 10.1, 10.2, 10.5, 10.7, 10.8_

  - [x] 4.2 Update both callers for the new return shape and delegate the header parsers
    - `edge-cv-portal/backend/functions/dda_autolabel_worker.py`: `_generate_llm_prelabel` returns the result's Pre_Label; `_image_dimensions` becomes a thin delegation to `dda_llm_image.declared_dimensions` accepting exactly the inputs it accepted before
    - `edge-cv-portal/backend/functions/dda_labeling.py`: `_run_preview_sample` returns the Sent_Dimensions alongside the Source_Dimensions; `_preview_image_dimensions` becomes the same thin delegation
    - Behavior-neutral task: no sizing or budget values are read from any record yet, so every existing test must stay green unchanged
    - _Requirements: 7.6, 7.10, 10.1_

  - [x] 4.3 Extend the `dda_llm_prelabel` unit tests
    - `edge-cv-portal/backend/tests/test_dda_llm_prelabel.py` (existing, extended): the downscaler called once for the target and once per attached example and zero times at Downscale_Off; the prompt carries the Sent_Dimensions; `parse_guidance` receives Sent and `guidance_to_prelabel` receives Source; `build_inference_config`'s returned dict not mutated in place; budget override with selection winning, mapping winning and default winning; the two categories carrying the error-table reasons
    - _Requirements: 1.3, 6.1, 7.1, 7.2, 8.1, 9.1, 10.2_

  - [x] 4.4 Write property test for sent-dimension agreement
    - `edge-cv-portal/backend/tests/test_property_sent_dimension_agreement.py` — moto plus one stub Converse client; example dimensions drawn to differ deliberately from the target's; boundary guidance drawn against the sent and the source pair
    - **Property 6: Prompt dimensions equal the dimensions of the image actually sent**
    - **Validates: Requirements 7.1, 7.2, 8.2**

- [x] 5. Implement the Model_Token_Limits settings surface
  - [x] 5.1 Add the token-limits item, routes and loader to `data_accounts.py`
    - `edge-cv-portal/backend/functions/data_accounts.py`: `LLM_MODEL_TOKEN_LIMITS_SETTING_KEY = 'llm_model_token_limits'`, `MODEL_TOKEN_LIMITS_MAX_ENTRIES = 200`, `MODEL_TOKEN_LIMITS_MAX_KEY_LENGTH = 256`
    - `validate_model_token_limits(value)` reporting **every** violation with nothing short-circuiting: mapping-ness, entry count, key type/length, non-bool integer value in `[1, 128000]`
    - `handle_model_token_limits(event, user, http_method)`: `GET` returning the mapping plus `default`, `ceiling` and `source` (`"settings"` | `"environment"`); `PUT` doing a whole-item `put_item` replacement — never a merging update expression — so no omitted entry survives and `{}` persists as empty; reads and writes nothing on the `bedrock_configuration` item
    - Route `/token-limits` on `handle_bedrock_configuration` as a sibling ahead of the bare GET/PUT, with the existing `Permission.BEDROCK_CONFIG_WRITE` gate and its denied-attempt audit entry left exactly where they are
    - `_llm_model_token_limits()` loader: persisted item as source of truth with **whole-mapping** precedence over the `LLM_MODEL_TOKEN_LIMITS` environment bootstrap (never a per-key merge), `_decimal_to_native` conversion before the resolver sees any value, memoized **per invocation** and cleared at the top of `handler`
    - `list_bedrock_model_options` gains `option['token_limit'] = resolve_token_budget(option['id'], None, token_limits)` beside the existing `image_limit`, every other option field byte-identical
    - `update_bedrock_configuration_setting` and `validate_bedrock_configuration` **must not be modified**
    - _Requirements: 1.1, 1.6, 1.8, 3.1, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_

  - [x] 5.2 Write property test for token-limit write isolation
    - `edge-cv-portal/backend/tests/test_property_model_token_limits_isolation.py` — moto DynamoDB with the real handlers; overlapping and disjoint key sets, the empty mapping weighted, keys differing only in case and surrounding whitespace
    - **Property 14: Token limit writes fully replace and stay isolated from the global configuration**
    - **Validates: Requirements 1.1, 4.1, 4.4, 4.7, 4.8**

  - [x] 5.3 Write property test for global Bedrock configuration preservation
    - `edge-cv-portal/backend/tests/test_property_bedrock_global_config_preservation.py` — moto round trip so numbers arrive as `Decimal`; differential against a pinned in-test reimplementation of the pre-feature rules, with a populated token-limits item present in the same table and the workflow-generation / node-designer consumers' captured Converse kwargs asserted invariant to it
    - **Property 3: Global Bedrock configuration semantics are preserved for every other consumer**
    - **Validates: Requirements 1.5, 4.5, 4.6, 10.2, 10.3, 10.5, 10.8, 10.9**

  - [x] 5.4 Write unit tests for the settings surface
    - `edge-cv-portal/backend/tests/test_bedrock_configuration.py` (existing, extended — every existing case stays as-is): the two `/token-limits` routes, the PortalAdmin gate reached identically to `/models`, mutual isolation with the configuration item
    - `edge-cv-portal/backend/tests/test_model_token_limits_settings.py` (new): `validate_model_token_limits` at every boundary (200 vs 201 entries, 256 vs 257-character key, empty key, `True` value, `1`/`128000`/`0`/`128001`); `GET` reporting `source: "environment"` before the item exists and `"settings"` after; `PUT` of `{}` persisting empty; a settings item written through moto read back as native `int`, and a deliberately un-converted `Decimal` shown to fall through to the default
    - `edge-cv-portal/backend/tests/test_bedrock_model_options_image_limit.py` (existing, extended): `token_limit` carried beside `image_limit` with the rest of the payload unchanged
    - _Requirements: 3.1, 4.1, 4.2, 4.3, 4.4, 4.7, 4.8, 1.6_

- [x] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Wire the Preview_API for both sizing inputs
  - [x] 7.1 Add request validation, run state and reporting to `dda_labeling.py`
    - `edge-cv-portal/backend/functions/dda_labeling.py`: add `downscale_max_edge` and `token_budget` rules to `_validate_preview_run_request`'s single all-rules-evaluated pass — `downscale_max_edge` absent/`null`/an integer in `MAX_IMAGE_EDGE_OPTIONS` with the message listing the six permitted values, `token_budget` absent or a non-boolean integer in `[1, 128000]` with the message naming the range; booleans, strings (including `"1024"` and `"off"`) and floats (including `1024.0`) rejected
    - Resolve the Effective_Token_Budget **once at run start** through the per-invocation `_llm_model_token_limits()` loader and record it on the `PREVIEW#{run_id}` / `RUN` item as `token_budget`; record the validated `downscale_max_edge` (absent for Downscale_Off)
    - Add `downscale_max_edge` and `token_budget` to the single `preview_run` audit event's `details` — still exactly one event per run
    - Add `downscale_max_edge` and `token_budget` to the `GET /labeling-preview/runs/{runId}` response
    - No object read and no model invocation on any rejection path
    - _Requirements: 3.5, 5.3, 5.5, 5.10, 9.5, 1.6, 1.8_

  - [x] 7.2 Plumb the executor and the result payload in `dda_labeling.py`
    - `_run_preview_sample` passes the run's recorded values straight through to the chokepoint — `downscale_setting=normalize_downscale_setting(run.get('downscale_max_edge'))`, `token_budget_selection` from the recorded resolved integer, `model_token_limits` from the run's snapshot — and gains no sizing logic of its own
    - Result payload gains `source_width`, `source_height`, `sent_width`, `sent_height` and `downscale_max_edge`; `image_width` / `image_height` keep their meaning as the Source_Dimensions so the canvas is untouched
    - Map the chokepoint's `unsupported_image_content` / `unreadable_example_image` errors through the existing category-preserving `PreviewSampleFailure` translation with no new category
    - _Requirements: 5.4, 5.10, 5.11, 7.7, 9.1, 9.3, 9.8_

  - [x] 7.3 Persist both values with the Labeling_Job record in `dda_labeling.py`
    - `create_dda_job` writes `auto_label.downscale_max_edge` and `auto_label.token_budget` unchanged, **only** for the `llm:` family and **only** when the submission carried them, so a submission omitting both yields a record byte-identical to a pre-feature record
    - Never written for `sam` or `bedrock:` jobs; a submission omitting both is accepted under the pre-feature validation rules with no message mentioning either
    - _Requirements: 3.6, 5.2, 5.7, 10.4, 10.6_

  - [x] 7.4 Write unit tests for the preview routes, executor and job creation
    - `test_dda_labeling_preview_routes.py` (extended): the two new validation branches with single and combined violations; the `RUN` item's two new attributes; the audit event's two new detail fields; the status response's two new fields; a run started with an empty budget omitting the key and resolving the default
    - `test_dda_labeling_preview_executor.py` (extended): the payload's four new dimension fields; a target refused by the downscaler yielding `unsupported_image_content` with zero invocations; a refused example yielding `unreadable_example_image`
    - `test_dda_labeling_create_job.py` (extended): both values persisted unchanged for `llm:`, absent when not submitted, never written for `sam` / `bedrock:`
    - _Requirements: 3.5, 3.6, 3.10, 5.5, 5.7, 5.10, 9.1, 9.5, 10.4, 10.6_

  - [x] 7.5 Write property test for sizing validation guards
    - `edge-cv-portal/backend/tests/test_property_sizing_validation_guards.py` — moto tables with S3 and Bedrock spies asserting zero calls; request bodies with injected sizing violations crossed with the predecessor's existing violation set, plus submitted token-limit mappings with invalid keys and values, 201-entry mappings and non-mapping values, each submitted with and without `BEDROCK_CONFIG_WRITE`
    - **Property 11: Request validation rejects invalid sizing inputs and touches nothing**
    - **Validates: Requirements 3.3, 3.5, 4.2, 4.3, 5.5**

- [x] 8. Wire the Auto_Labeler for labeling time
  - [x] 8.1 Read both values off the job record in `dda_autolabel_worker.py`
    - `_generate_llm_prelabel` reads `auto_label.downscale_max_edge` through `normalize_downscale_setting` and `auto_label.token_budget` raw, next to where it already reads `auto_label.few_shot`, and passes both plus the `_llm_model_token_limits()` mapping into the chokepoint
    - A malformed, null or absent `downscale_max_edge` is Downscale_Off with no failure; a malformed or absent `token_budget` falls through to the mapping and then the default with no failure; a persisted valid budget is immutable for the life of the job
    - Neither value is read for `sam` or `bedrock:` jobs — those families never reach this function
    - _Requirements: 3.7, 3.8, 5.8, 5.9, 5.12, 8.1, 9.2, 9.7, 10.4, 10.10_

  - [x] 8.2 Extend the worker few-shot unit tests
    - `edge-cv-portal/backend/tests/test_dda_autolabel_worker_few_shot.py` (existing, extended — every existing case must pass unchanged, since selection is independent of the setting): reading both values off the record, malformed values resolving to Downscale_Off and the default, examples downscaled with the target's setting, a refused example failing only its own dataset image while the batch continues
    - _Requirements: 3.8, 5.8, 5.9, 5.12, 8.1, 8.5, 9.2, 10.10_

- [x] 9. Verify the two paths agree, end to end
  - [x] 9.1 Write property test for budget plumbing
    - `edge-cv-portal/backend/tests/test_property_token_budget_plumbing.py` — moto plus one stub Converse client serving both real entry points and the model-options handler; Global_Max_Tokens drawn twice per example; a mapping rewritten after the job record was persisted asserted not to move the worker's `maxTokens`
    - **Property 2: Every `llm:` request carries the resolved per-model budget, never the global value**
    - **Validates: Requirements 1.3, 1.4, 1.6, 1.7, 1.8, 3.7, 3.8**

  - [x] 9.2 Extend the preview/worker request identity property test
    - `edge-cv-portal/backend/tests/test_property_preview_worker_request_identity.py` (existing, extended — **never weakened**): `_identity_cases` gains `downscale_setting`, `token_budget_selection` and a token-limits mapping; `IdentityEnv` gains the settings item and the two record attributes; source dimensions widened so a subset exceeds each bound; a spy on `downscale_image` records exactly one call per image block
    - **Property 5: Preview and Auto_Labeler requests stay byte-identical under downscaling**
    - **Validates: Requirements 1.4, 6.1, 6.8, 8.4, 8.6**

  - [x] 9.3 Write property test for the unconfigured-sizing preservation
    - `edge-cv-portal/backend/tests/test_property_unconfigured_sizing_preservation.py` — moto plus a stub client, differential against a pinned pre-feature content-list builder; malformed and absent `downscale_max_edge`, `few_shot` and `token_budget` documents; a spy asserting zero re-encodes
    - **Property 8: An unconfigured Downscale_Setting reproduces the pre-feature request**
    - **Validates: Requirements 3.8, 3.10, 5.9, 5.12, 10.1, 10.6, 10.10**

  - [x] 9.4 Write property test for few-shot downscaling
    - `edge-cv-portal/backend/tests/test_property_few_shot_downscaling.py` — moto plus a stub client driving both real paths; good/bad counts 0–10, Model_Image_Limit including `1`, each example seeded with distinct dimensions and content
    - **Property 9: Few-shot selection and image bounds are unchanged by downscaling**
    - **Validates: Requirements 8.1, 8.3, 8.4, 8.7, 8.8, 10.7**

  - [x] 9.5 Extend the preview run outcomes property test
    - `edge-cv-portal/backend/tests/test_property_preview_run_outcomes.py` (existing, extended — pinned pre-existing reason strings must not be edited): the condition enumeration gains `undecodable_for_setting`, `oversize_declared_pixel_count`, `undecodable_attached_example` and `rejected_token_budget`; the run generator gains both sizing values; the sibling worker-side property drives `dda_autolabel_worker` over the same mix for batch continuation and no-retry
    - **Property 10: Every image yields exactly one categorized outcome from the closed category set**
    - **Validates: Requirements 7.9, 8.5, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7**

  - [x] 9.6 Write property test for untouched families and dimension determination
    - `edge-cv-portal/backend/tests/test_property_untouched_families_and_dimensions.py` — `sam` and `bedrock:` configurations with both sizing values deliberately planted, differential against pinned pre-feature builders with a spy asserting zero `downscale_image` calls; plus arbitrary byte strings compared against a **pinned verbatim copy of the pre-feature `_image_dimensions`** vendored into the test file, and the undeterminable-dimension path asserted to keep its pre-feature reason character-for-character
    - **Property 12: Untouched model families and dimension determination are unchanged**
    - **Validates: Requirements 7.6, 7.10, 10.4**

  - [x] 9.7 Write integration tests for the sizing flow
    - `edge-cv-portal/backend/tests/test_llm_sizing_integration.py` (new): seed a 3000×2000 JPEG, `POST` a run with `downscale_max_edge: 1024` and `token_budget: 20000`, drive the executor inline, poll to `Completed`, assert the captured image block decodes to 1024×682, `inferenceConfig.maxTokens == 20000`, the prompt names 1024×682, the payload geometry lies within 3000×2000 and `image_width`/`image_height` are 3000/2000
    - The cross-account read path through `get_s3_client_for_bucket`'s single-account fallback for both Sample_Images and example images with a Max_Image_Edge selected; the worker path for the same job configuration through the SQS record path asserted byte-equal to the preview's
    - Settings round trip: `PUT` token limits, confirm the worker's next request picks the new value up with no redeploy, confirm the `bedrock_configuration` item is untouched, then `PUT` `{}` and confirm the default applies
    - _Requirements: 1.6, 4.1, 4.4, 4.8, 5.4, 5.8, 6.8, 7.3, 8.4_

- [x] 10. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Implement the frontend controls and sizing display
  - [x] 11.1 Extend the API client in `api.ts`
    - `edge-cv-portal/frontend/src/services/api.ts`: additive fields on `StartPreviewRunRequest` (`downscale_max_edge?: number | null`, `token_budget?: number`), on the preview run response (`downscale_max_edge?`, `token_budget?`), on the result payload (`source_width?`, `source_height?`, `sent_width?`, `sent_height?`, `downscale_max_edge?`) and on the model-catalog option type (`token_limit?: number`)
    - Two new client methods, `getModelTokenLimits()` and `updateModelTokenLimits(mapping)`, against `/data-accounts/bedrock-configuration/token-limits`
    - _Requirements: 3.1, 4.1, 5.3, 5.10_

  - [x] 11.2 Add the sizing state to `CreateLabelingJob.tsx`
    - `edge-cv-portal/frontend/src/pages/CreateLabelingJob.tsx`: `downscaleMaxEdge: number | null` defaulting to `null` and `tokenBudget: string`, both gated on the existing `autoLabelEnabled && isLlmAutoLabelModel` condition so `sam`, `bedrock:` and no-model states render nothing new and submit neither value
    - The existing model-compatibility `useEffect` **replaces** the shown budget with the newly selected model's catalog `token_limit` (falling back to 10000) and leaves the Detection_Prompt, Label_Set, selected samples, few-shot toggle and downscale setting untouched; both values clear to their defaults when the selection leaves the `llm:` family
    - Submission carries both inside `auto_label` for an `llm:` model only; an empty budget field omits the key entirely; a blank downscale select submits `null`
    - Reject submission with the accepted-range message when the budget is non-empty and not a whole number in `[1, 128000]`, issuing no creation request and retaining every entered value
    - _Requirements: 3.1, 3.2, 3.3, 3.6, 3.9, 3.10, 5.1, 5.2, 5.7, 10.4, 10.6_

  - [x] 11.3 Add the controls, validation and sizing display to `PromptTuningPreview.tsx`
    - `edge-cv-portal/frontend/src/components/labeling/PromptTuningPreview.tsx`: two new optional props (`downscaleMaxEdge`, `tokenBudget`) threaded into the run request; a Downscale_Setting select with exactly seven options (Downscale_Off default plus the six Max_Image_Edge values labelled in pixels) and a Token_Budget_Selection input displaying its accepted range
    - `validatePreviewStart` gains the budget rule: a non-empty value that is not a whole number in `[1, 128000]` contributes a violation naming the range, no request is issued and no wizard state changes
    - Per-sample sizing row from the payload: `1920 × 1080 → 1024 × 576 (53%)` with the percentage `clamp(1, 100, Math.round(sentLong / sourceLong * 100))`; "dimensions unavailable" when either pair is missing, with the rest of the result still rendered
    - Failed results additionally show the run's applied Downscale_Setting and Effective_Token_Budget beside the existing category and reason; changing either control after a completed run keeps the sample selection and every other value and re-enables the run control
    - `PreviewResultCanvas.tsx` is **not** changed: it keeps receiving `payload.image_width` / `image_height`, and the new `sent_*` fields are display-only
    - _Requirements: 3.1, 3.3, 3.4, 3.9, 3.11, 5.1, 5.2, 5.3, 5.4, 5.6, 5.11, 7.7, 9.8_

  - [x] 11.4 Extend the preview property tests with the sizing controls
    - `edge-cv-portal/frontend/src/components/labeling/PromptTuningPreview.property.test.tsx` (existing, extended) — fast-check at 100 runs over control visibility for the three model families, client-side budget rejection sending nothing and keeping state, the sizing display's percentage and unavailable branches, and the failed-result display of the applied setting and budget
    - Covers the frontend halves the design's placement table lists (criteria 3.1–3.4, 3.11, 5.1–5.4, 5.6, 7.7, 9.8); the backend half of Property 11 is task 7.5
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.11, 5.1, 5.2, 5.3, 5.4, 5.6, 5.11, 7.7, 9.8_

  - [x] 11.5 Write frontend unit tests for the controls and the canvas guard
    - `PromptTuningPreview.test.tsx` (extended): seven select options with Downscale_Off default and hidden for `sam` / `bedrock:` / no model; invalid budget entries (`0`, `-1`, `128001`, `12.5`, `"abc"`) listing the accepted range with no API call; an empty input omitting `token_budget`; sizing display for `1920×1080 → 1024×576` (53%), a 1% floor case, a 100% case and the missing-dimension branch
    - `CreateLabelingJob.sizing.test.tsx` (new): budget pre-fill from the catalog `token_limit` with the 10000 fallback, replacement (not merge) on model change with prompt/labels/samples/few-shot/downscale unchanged, submission shape for `llm:` and absence for `sam` / `bedrock:`
    - `PreviewResultCanvas.test.tsx` (extended): the regression guard asserting the canvas gained **no new props** and still receives `payload.image_width` / `image_height` unchanged
    - _Requirements: 3.1, 3.2, 3.3, 3.10, 5.1, 5.2, 5.4, 5.11, 7.7_

- [x] 12. Wire the infrastructure
  - [x] 12.1 Update `compute-stack.ts` for the imaging layer, the mapping and memory
    - `edge-cv-portal/infrastructure/lib/compute-stack.ts`: `layers: [sharedLayer, imagingLayer]` on `DdaLabelingHandler` and `DdaAutolabelWorker`, using the same `imagingLayer` LayerVersion `DdaLabelingWorker` already attaches
    - A new `llmModelTokenLimits` context value built exactly like `llmModelImageLimits` (string passthrough, object stringify, `'{}'` default), exposed as `LLM_MODEL_TOKEN_LIMITS` on `DdaLabelingHandler`, `DdaAutolabelWorker` and `DataAccountsHandler`
    - `memorySize: 2048` on `DdaLabelingHandler` and `DdaAutolabelWorker` (both currently at the 128 MB default)
    - No new layer source, table, bucket or IAM change; `DdaLabelingWorker` and `SyntheticImagingLayer` definitions untouched; the standalone `DdaLabelingSelfInvokePolicy` `iam.Policy` left in place with `grantInvoke(self)` still absent
    - _Requirements: 1.8, 6.1, 6.6, 6.11_

  - [x] 12.2 Register the two `/token-limits` routes
    - `edge-cv-portal/infrastructure/lib/api-gateway-stack.ts`: add a `token-limits` sub-resource beside the existing `models` sub-resource on `dataAccountIdResource` (the file that registers `GET /data-accounts/{id}/models`) with `GET` and `PUT` methods on the data-accounts integration and the stack's Cognito authorizer attached
    - _Requirements: 4.1, 4.3_

  - [x] 12.3 Write CDK assertion tests for the sizing infrastructure
    - `edge-cv-portal/infrastructure/test/llm-model-token-and-image-sizing-infra.test.ts` (new): `DdaLabelingHandler` and `DdaAutolabelWorker` each carry two layers whose second is the same `imagingLayer` `Ref` as `DdaLabelingWorker`'s; both carry `LLM_MODEL_TOKEN_LIMITS` alongside `LLM_MODEL_IMAGE_LIMITS` from the same context-derived strings; both have `MemorySize: 2048`; `DataAccountsHandler` carries `LLM_MODEL_TOKEN_LIMITS`; the two `/token-limits` routes exist with the authorizer attached; `DdaLabelingWorker`'s and `SyntheticImagingLayer`'s definitions are unchanged; `DdaLabelingSelfInvokePolicy` is still a standalone `iam.Policy` and `grantInvoke(self)` is still absent
    - _Requirements: 1.8, 6.6, 4.1_

- [x] 13. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 14. Verification
  - [x] 14.1 Run the non-regression inventory and confirm zero rebaselines
    - Run each file in the design's non-regression inventory and confirm the expected disposition: `test_property_sampling_exclusivity.py`, `test_property_bedrock_sampling_exclusivity.py`, `test_property_bedrock_sampling_preservation.py`, `test_property_bedrock_config_resolution.py` and `test_node_generator_integration.py` **byte-identical and green**; `test_property_preview_worker_request_identity.py`, `test_property_preview_run_outcomes.py`, `test_bedrock_configuration.py`, `test_dda_llm_prelabel.py` and `test_dda_autolabel_worker_few_shot.py` extended with every pre-existing assertion still passing
    - The expected number of rebaselines across the whole inventory is **none**. A preservation test is never weakened or deleted; if an existing assertion has to change, stop and raise it as a design violation rather than rebaselining
    - _Requirements: 1.5, 10.1, 10.2, 10.3, 10.4, 10.5, 10.7, 10.8, 10.9_

  - [x] 14.2 Run the full verification sweep
    - Backend: `cd edge-cv-portal/backend && python3 -m pytest tests/ -q` — all new property, unit and integration tests green with no new failures beyond the known pre-existing list below
    - Frontend: `cd edge-cv-portal/frontend && npx tsc --noEmit`, then `npx vitest run`
    - Infrastructure: `cd edge-cv-portal/infrastructure && npm test` and `npm run build`, then `npx cdk synth` — the real dependency-cycle gate
    - Per `.kiro/steering/builds.md`, move `cdk.out` aside (`mv cdk.out cdk.out.bak-$(date +%Y%m%dT%H%M%SZ)`) before running the preservation guard suite, then run it **from the repo root with `PYTHONPATH=src/backend`**: `PYTHONPATH=src/backend python3 -m pytest test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py -p no:cacheprovider --noconftest -q`. If the `cdk.out` drift guards still fail, either move the remaining copies aside or add their sha256 entries to `cdk_out_baseline.json` / `secrets_cdk_out_baseline.json` and re-run
    - Do **not** run a portal deploy while a component build is running — check `pgrep -af "gdk component build"` and `pgrep -af "build-custom.sh"` first
    - **Known pre-existing failures — do not attempt to fix them, and do not count them as regressions:**
      - `test_vllm_publish_writeback.py` test pollution: ~18 downstream vllm/llm failures with 3-minute AWS retry hangs when run in the same pytest session; all pass in isolation
      - `EdgeCVPortalComputeStack` cognito IAM statement-count drift (3 vs baseline 2) in `test_preservation_iam_cdk_synth.py`
      - `test_property_setup_command_wellformed.py` collection error: `ImportError: cannot import name 'Permission' from 'shared_utils'`
    - _Requirements: 6.6, 6.8, 10.1, 10.2, 10.4, 10.5_

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; core implementation tasks are never optional
- Each of the 14 correctness properties has exactly one property-based test task, in the file the design's placement table names, run at a minimum of 100 iterations (`@settings(max_examples=100, deadline=None)` / `fc.assert(..., {numRuns: 100})`) and tagged `Feature: llm-model-token-and-image-sizing, Property {n}: {text}`. Task 11.4 covers the frontend halves the placement table lists and is deliberately not a numbered property task
- **Same-file scheduling constraint:** subtasks that write the same file are never in the same wave. `dda_labeling.py` is edited by 4.2, 7.1, 7.2 and 7.3 in four consecutive waves; `dda_autolabel_worker.py` by 4.2 then 8.1; `data_accounts.py` only by 5.1; `PromptTuningPreview.tsx` (11.3) lands before `CreateLabelingJob.tsx` (11.2) so the wizard never passes props the component has not declared yet
- Task 4.2 is deliberately behavior-neutral: it absorbs the `generate_llm_prelabel` return-shape change and the header-parser relocation in both callers at once, so no wave leaves the tree unbuildable and every existing test stays green before any sizing value is read from a record
- The Preview_API resolves the Effective_Token_Budget once at run start and passes the resolved integer back in as the selection at execution time; the resolver's idempotence on its own output is what makes the audited, reported and sent budgets provably equal
- The design's smoke tests (a deployed Preview_Run at a Max_Image_Edge, and a deployed Nova Pro run at the default budget) are deployment activities, not coding tasks, and are intentionally not in this plan

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "2.3", "11.1", "12.1", "12.2"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "2.2", "2.4", "2.5", "4.1", "5.1", "11.3", "12.3"] },
    { "id": 2, "tasks": ["4.2", "4.3", "5.2", "5.3", "5.4", "11.2"] },
    { "id": 3, "tasks": ["4.4", "7.1", "8.1", "11.4", "11.5"] },
    { "id": 4, "tasks": ["7.2", "7.5", "8.2"] },
    { "id": 5, "tasks": ["7.3"] },
    { "id": 6, "tasks": ["7.4", "9.1", "9.2", "9.3", "9.4", "9.5", "9.6"] },
    { "id": 7, "tasks": ["9.7"] },
    { "id": 8, "tasks": ["14.1"] },
    { "id": 9, "tasks": ["14.2"] }
  ]
}
```
