# Implementation Plan: LLM Auto-Label Prompt Tuning and Few-Shot Examples

## Overview

Implementation proceeds bottom-up so that "preview is a faithful predictor of labeling-time behavior" holds by construction rather than by duplicated logic. First the pure shared-layer module (`dda_llm_request.py`) that owns few-shot selection and Converse content layout, then the functions-bundle invocation module (`dda_llm_prelabel.py`) extracted literally from today's worker body, then the worker refactored to delegate — with the preservation property test landing before any few-shot behavior is added, so the extraction is proven behavior-neutral first. Few-shot at labeling time, job-record persistence, the listing filter and the Model_Image_Limit config surface follow. Infrastructure (Bedrock grant, raised timeout, self-invoke, the two API routes, tasks-table TTL, artifacts lifecycle) lands immediately before the Preview_API routes that need it, then the start/status routes, then the async executor. Frontend comes last: API client and wizard controls, then `PromptTuningPreview` / `PreviewResultCanvas`.

Backend tests live in `edge-cv-portal/backend/tests/` (pytest + moto + Hypothesis, stubbed Converse clients); frontend tests use vitest + `@testing-library/react` with fast-check. Every correctness property from the design gets exactly one property-based test, at 100 iterations minimum, in the file the design names, tagged `Feature: llm-autolabel-prompt-tuning, Property {n}: {text}`.

## Tasks

- [ ] 1. Implement the shared-layer request module (pure)
  - [x] 1.1 Create `dda_llm_request.py` in the shared layer
    - Create `edge-cv-portal/backend/layers/shared/python/dda_llm_request.py` — pure functions only, no boto3, no I/O (same contract as `dda_llm_guidance.py`)
    - `MODEL_IMAGE_LIMIT_DEFAULT = 20`, `FEW_SHOT_GOOD`/`FEW_SHOT_BAD`, and the few-shot header / target-intro text constants
    - `resolve_model_image_limit(model_identifier, limits)`: the configured integer when `>= 1`, else the default — a missing, non-integer, or `< 1` entry can never widen or zero the bound
    - `select_few_shot_examples(examples, model_image_limit)`: `(attached, omitted)` where `attached` is the prefix of *good in stored order ++ bad in stored order* of length `max(0, limit - 1)`; `limit == 1` attaches nothing
    - `build_llm_request(...)`: prompt text is `build_detection_prompt(...)` verbatim; content is `[image(target), text(prompt)]` when the few-shot list is empty, otherwise header, per-example identification text immediately preceding each example image, target intro, target image, prompt
    - `image_format_for_key(key)`: `'png'` for `.png` keys (case-insensitive), else `'jpeg'`
    - _Requirements: 3.1, 6.5, 7.1, 7.2, 7.3, 7.4, 10.2_

  - [x]* 1.2 Write property test for few-shot selection
    - `edge-cv-portal/backend/tests/test_property_few_shot_selection.py`
    - **Property 3: Few-shot selection is a deterministic, bounded, order-preserving prefix**
    - **Validates: Requirements 6.5, 7.2, 7.3, 7.4, 7.6**

  - [x]* 1.3 Write property test for the no-few-shot request shape
    - Same file: `test_property_few_shot_selection.py`
    - **Property 4: A request without few-shot examples keeps the pre-feature shape**
    - **Validates: Requirements 10.2, 10.3**

  - [x]* 1.4 Write property test for Model_Image_Limit resolution
    - Same file: `test_property_few_shot_selection.py`
    - **Property 13: Model_Image_Limit resolution is total and safe**
    - **Validates: Requirements 7.1**

  - [x]* 1.5 Write unit tests for `dda_llm_request`
    - Prompt text equals `build_detection_prompt` output character-for-character; content layout for empty / good-only / bad-only / mixed few-shot sets; `limit == 1` attaches nothing
    - `image_format_for_key` on `.PNG`, `.jpeg`, `.JPG` and extensionless keys
    - Every content block is an image block or a text block derived from prompt/label set/dimensions/few-shot identification — no credentials, URLs or ARNs
    - _Requirements: 3.1, 3.4, 6.5, 7.4_

- [ ] 2. Extract the shared invocation module and refactor the worker to delegate
  - [x] 2.1 Create `dda_llm_prelabel.py` in the functions bundle
    - Create `edge-cv-portal/backend/functions/dda_llm_prelabel.py` as the literal extraction of `dda_autolabel_worker._generate_llm_prelabel`'s body from prompt construction through Pre_Label conversion
    - `LlmPrelabelError(category, reason, raw_text)` with categories `model_error` | `timeout` | `unusable_model_output`; `reason` strings identical to what the Auto_Labeler records today
    - `generate_llm_prelabel(...)`: build the request via `dda_llm_request.build_llm_request` after `select_few_shot_examples`, get the client from `bedrock_common.get_bedrock_client` with the read timeout clamped to `min(config.timeout_seconds, 120)` and retries disabled, issue exactly one `converse` call, then `parse_guidance` + `guidance_to_prelabel`
    - Map `ReadTimeoutError`/`ConnectTimeoutError` → `timeout`, any other invocation exception → `model_error`, `GuidanceError` → `unusable_model_output` with `raw_text` set to the response text character-for-character
    - _Requirements: 3.1, 3.2, 3.3, 3.10, 3.11, 9.1, 9.2, 9.3_

  - [x] 2.2 Refactor `dda_autolabel_worker.py` to delegate to the shared modules
    - `_generate_llm_prelabel` keeps its S3 read, pixel-dimension decode, prompt/per-label-prompt resolution and task bookkeeping, and delegates request construction, invocation and response handling to `generate_llm_prelabel`
    - Translate `LlmPrelabelError` back into today's `GenerationFailure(reason)` so `prelabel_error` / `autolabel_error` strings and the `autolabel_pending` decrement are byte-identical for every existing failure mode
    - Leave the `sam` and `bedrock:` code paths untouched
    - _Requirements: 3.10, 3.11, 10.1, 10.2_

  - [x]* 2.3 Write property test for preservation of untouched paths
    - `edge-cv-portal/backend/tests/test_property_llm_autolabel_preservation.py`, baseline captured from the pre-change `sam` / `bedrock:` / `llm:`-without-few-shot code paths
    - **Property 5: Untouched model families and job creation are unchanged**
    - **Validates: Requirements 10.1, 10.4, 10.5, 10.6**

  - [x]* 2.4 Write unit tests for `dda_llm_prelabel` and worker delegation
    - Bedrock client built with `min(config.timeout_seconds, 120)` and retries disabled; exactly one `converse` call per invocation regardless of outcome
    - Exception→category mapping for read timeout, connect timeout, throttling, validation and generic errors; `raw_text` populated only when a response was received
    - Worker: each `LlmPrelabelError` category produces the pre-feature `GenerationFailure` reason string
    - _Requirements: 3.1, 3.3, 3.10, 3.11, 9.1, 9.2, 9.3_

- [ ] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Add few-shot resolution at labeling time
  - [x] 4.1 Implement `_resolve_few_shot_images` in `dda_autolabel_worker.py`
    - Read few-shot state from the job record's `auto_label.few_shot`; absent, `null`, non-dict, falsy `enabled`, or an empty/malformed `examples` list all resolve to disabled with no failure attributed to the configuration
    - When enabled, `select_few_shot_examples(stored examples, resolve_model_image_limit(model, LLM_MODEL_IMAGE_LIMITS))` and read **only** the attached refs through the same `get_s3_client_for_bucket` client the dataset image uses
    - An unreadable attached example raises `GenerationFailure("few-shot example image {ref} is not accessible: ...")` for that dataset image only; the batch loop continues
    - Pass the resolved images and limit into `generate_llm_prelabel`
    - _Requirements: 6.5, 6.7, 7.1, 7.2, 7.3, 7.4, 10.3_

  - [x]* 4.2 Write unit tests for worker few-shot resolution
    - Disabled by default for absent / `null` / non-dict / malformed `few_shot` documents; enabled only for `enabled is True` with at least one well-formed example
    - Omitted refs assert zero `get_object` calls; attached ordering is good-then-bad in stored order with each example identified
    - Unreadable example reason text names the ref and fails only that dataset image
    - _Requirements: 6.5, 6.7, 7.3, 7.4, 10.3_

- [ ] 5. Persist the Few_Shot_Option with the Labeling_Job record
  - [x] 5.1 Extend `create_dda_job` in `dda_labeling.py`
    - For `llm:` jobs with the option enabled, derive `auto_label.few_shot = {"enabled": true, "examples": [{ref, designation, position}]}` from the uploaded example images (at most 10 good, at most 10 bad), `position` per designation in wizard upload order
    - Write `{"enabled": false}` for `llm:` jobs without the option; write no `few_shot` key at all for `sam` / `bedrock:` jobs
    - Leave `example_images` and its labeler-instruction role unchanged; accept submissions that omit the field with the pre-feature validation outcome
    - _Requirements: 6.4, 10.1, 10.4, 10.6_

  - [x]* 5.2 Write unit tests for few-shot persistence
    - Designations and positions recover the submitted example set and order exactly; `{"enabled": false}` for `llm:` without the option; key absent for `sam` / `bedrock:`; omitted field accepted unchanged; `example_images` untouched in every case
    - _Requirements: 6.4, 10.1, 10.4, 10.6_

- [ ] 6. Extend the listing and model-options surfaces
  - [x] 6.1 Add the `extensions` filter to `datasets.get_image_preview`
    - Optional comma-separated `extensions` parameter; when present only those extensions are listed, when absent the existing six-extension behavior applies unchanged
    - Matching is case-insensitive on the object key suffix; `total_found`, `has_more`, offset/limit paging and presigned thumbnail URLs keep their current semantics over the filtered set
    - _Requirements: 2.1, 2.2, 2.7_

  - [x]* 6.2 Write property test for the filtered listing
    - `edge-cv-portal/backend/tests/test_property_image_preview_extensions.py`
    - **Property 19: Only JPEG and PNG objects are listed, and every one is reachable**
    - **Validates: Requirements 2.1, 2.2, 2.7**

  - [x]* 6.3 Write unit tests for the `extensions` parameter
    - Filter applied for `jpg,jpeg,png`; absent parameter preserves the existing six-extension behavior byte-for-byte; empty prefix reports `total_found == 0`; inaccessible prefix surfaces a non-2xx error
    - _Requirements: 2.1, 2.5_

  - [x] 6.4 Add `image_limit` to `list_bedrock_model_options`
    - Additive per-option `image_limit` field resolved through `resolve_model_image_limit` against `LLM_MODEL_IMAGE_LIMITS`, so existing consumers that ignore the field are unaffected
    - _Requirements: 7.1, 7.5_

  - [x]* 6.5 Write unit tests for the model-options `image_limit` field
    - Configured value returned when valid; default of 20 for unlisted, non-integer and `< 1` entries; the rest of the option payload unchanged
    - _Requirements: 7.1, 7.5_

- [ ] 7. Wire infrastructure for the Preview_API
  - [x] 7.1 Update `compute-stack.ts` for `ddaLabelingHandler`
    - Raise `timeout` from 30 s to 900 s; add `bedrock:InvokeModel` / `bedrock:InvokeModelWithResponseStream` with the same foundation-model plus inference-profile resource scope the autolabel worker uses
    - Add `LLM_MODEL_IMAGE_LIMITS` (JSON object, default `{}`) to `ddaLabelingHandler`, `ddaAutolabelWorker` and the data-accounts handler
    - `ddaLabelingHandler.grantInvoke(ddaLabelingHandler)` for the self async-invoke
    - _Requirements: 3.3, 7.1_

  - [x] 7.2 Register the preview routes in `dda-labeling-api-stack.ts`
    - `POST /labeling-preview/runs` and `GET /labeling-preview/runs/{runId}` on the imported API root with the stack's existing Cognito authorizer, CORS options and route-salted deployment
    - _Requirements: 1.3, 8.1_

  - [x] 7.3 Update `storage-stack.ts` for preview state and payloads
    - Add `timeToLiveAttribute: 'ttl'` to `labelingTasksTable` (cleanup only — correctness stays with the `expires_at` comparisons)
    - Add a portal artifacts bucket lifecycle rule expiring `labeling-previews/` after 1 day
    - _Requirements: 1.6, 3.5, 8.8_

  - [x]* 7.4 Write CDK assertion tests for the preview infrastructure
    - `DdaLabelingHandler` has `bedrock:InvokeModel`, a timeout of at least the per-run bound, self-invoke permission and `LLM_MODEL_IMAGE_LIMITS`; both preview routes exist with the authorizer attached; the tasks table has TTL enabled; the artifacts bucket has the `labeling-previews/` expiration rule
    - _Requirements: 3.3, 7.1, 8.1_

- [ ] 8. Implement the Preview_API start and status routes
  - [x] 8.1 Add preview run state helpers to `dda_labeling.py`
    - Run id generation; `PREVIEW#{run_id}` / `RUN` and `IMAGE#{i:03d}` item writers and readers in `dda-portal-labeling-tasks`; `PREVIEWLOCK#{usecase_id}` / `USER#{sub}` conditional-write claim (`attribute_not_exists(task_id) OR expires_at < :now`) with `lock_ttl = min(sample_count * 120 + 60, 900)` and an unconditional release
    - Result payload keys under `labeling-previews/{usecase_id}/{run_id}/{i}.json` in the portal artifacts bucket plus presigned GET generation; sample-reference resolution to `(bucket, key)` for both bare keys and `s3://` URIs
    - _Requirements: 1.6, 3.5, 8.7, 8.8_

  - [x] 8.2 Implement `POST /labeling-preview/runs`
    - Order fixed as authorization → request validation → scope resolution → concurrency claim: `@rbac_check([Permission.MANAGE_LABELING_JOBS])` with the body's `usecase_id` injected as scope, answering `403 {"error": "Not authorized"}` identically whether or not the Use_Case or referenced objects exist, plus an `unauthorized_access` audit event
    - Validate all rules together and enumerate every violation: `llm:`-family model identifier, Detection_Prompt non-empty after trim and ≤ 2000 raw characters, modality in the three values with a Label_Set valid for it, `1 <= len(sample_images) <= 5`, every sample resolving inside the Use_Case dataset bucket and prefix, few-shot enabled implying ≥ 1 example with ≤ 10 per designation and JPEG/PNG refs inside the Use_Case data bucket
    - On success claim the lock (`409` on `ConditionalCheckFailedException`), write the `RUN` item plus one `Pending` `IMAGE#{i}` item per sample, emit the `preview_run` audit event with user identity, Use_Case, model identifier and sample count, async self-invoke with `{action: 'execute_preview_run', run_id}` using `context.function_name`, and return `202 {run_id, sample_count, status: 'Running'}`
    - Add the non-HTTP `action` branch ahead of the HTTP dispatch in `handler`; flip the run to `Failed` with `run_error` and release the lock if the async invoke fails
    - No S3 read and no model invocation on any rejection path
    - _Requirements: 1.3, 1.6, 3.5, 3.8, 6.3, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8_

  - [x] 8.3 Implement `GET /labeling-preview/runs/{runId}`
    - Return run status, `sample_count`, few-shot attached/omitted counts, and one result entry per requested Sample_Image in request order for the life of the run, each carrying state and — when resolved — failure category, failure reason and a presigned result-payload URL
    - `404 {"error": "Preview run not found"}` for an unknown run id or a run owned by another user
    - _Requirements: 3.5, 4.6, 9.6_

  - [x]* 8.4 Write property test for request rejection
    - `edge-cv-portal/backend/tests/test_property_preview_api_guards.py`
    - **Property 6: Rejected Preview_Run requests enumerate every violation and touch nothing**
    - **Validates: Requirements 6.3, 8.3, 8.4, 8.5, 8.7**

  - [x]* 8.5 Write property test for authorization precedence
    - Same file: `test_property_preview_api_guards.py`
    - **Property 7: Authorization precedes and hides everything**
    - **Validates: Requirements 8.2, 8.6**

  - [x]* 8.6 Write property test for the in-flight run guard
    - Same file: `test_property_preview_api_guards.py`
    - **Property 8: One in-flight Preview_Run per user and Use_Case**
    - **Validates: Requirements 8.8**

  - [x]* 8.7 Write unit tests for the preview routes
    - `403` / `400` / `409` / `404` branches with their exact bodies; `usecase_id`-not-found reported only after the authorization check; audit event fields; presigned result URLs and expiry; unknown and foreign `run_id`; lock expiry allowing a later run
    - _Requirements: 3.8, 8.1, 8.2, 8.4, 8.8_

- [ ] 9. Implement the Preview_Run executor
  - [x] 9.1 Implement `execute_preview_run` in `dda_labeling.py`
    - Process Sample_Images sequentially: read bytes through `get_s3_client_for_bucket` including the direct-access fallback, decode pixel dimensions, read the selected few-shot example bytes, then call `generate_llm_prelabel` — the identical call the worker makes
    - Write each outcome immediately: payload JSON to `labeling-previews/{usecase_id}/{run_id}/{i}.json` (`prelabel` and dimensions on success; `failure_category`, `failure_reason` and verbatim `raw_model_output` on failure) plus status/category/reason on the `IMAGE#{i}` item
    - Assign exactly one category per failure from `image_access_failure`, `unsupported_image_content`, `unreadable_example_image`, `timeout`, `model_error`, `unusable_model_output`, with the first three implying zero model invocations for that sample
    - A per-sample failure never aborts the loop; flip the `RUN` item to `Completed` after the last sample (even when every sample failed) and release the lock on every terminal path including unexpected exceptions
    - Create no Labeling_Job record, Task_Assignment item, pipeline Pre_Label artifact or labeler notification
    - _Requirements: 1.6, 3.1, 3.2, 3.3, 3.5, 3.6, 3.7, 3.9, 3.10, 3.11, 6.6, 6.8, 7.2, 7.6, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_

  - [x]* 9.2 Write property test for per-sample outcomes
    - `edge-cv-portal/backend/tests/test_property_preview_run_outcomes.py`
    - **Property 9: Every Sample_Image yields exactly one categorized outcome, independently**
    - **Validates: Requirements 3.5, 3.7, 3.9, 6.8, 9.1, 9.2, 9.4, 9.5, 9.6**

  - [x]* 9.3 Write property test for unreadable example isolation
    - Same file: `test_property_preview_run_outcomes.py`
    - **Property 10: An unreadable example image fails only its own target image**
    - **Validates: Requirements 6.7, 6.8**

  - [x]* 9.4 Write property test for absence of pipeline state
    - Same file: `test_property_preview_run_outcomes.py`
    - **Property 11: A Preview_Run produces no labeling-pipeline state**
    - **Validates: Requirements 1.6, 3.5**

  - [x]* 9.5 Write property test for request content restriction
    - Same file: `test_property_preview_run_outcomes.py`
    - **Property 12: Model requests carry only image and prompt content**
    - **Validates: Requirements 3.4**

  - [x]* 9.6 Write property test for preview/worker request identity
    - `edge-cv-portal/backend/tests/test_property_preview_worker_request_identity.py` — drives the preview executor and `dda_autolabel_worker._generate_llm_prelabel` against one stub Converse client and compares captured kwargs
    - **Property 1: Preview and Auto_Labeler issue identical model requests**
    - **Validates: Requirements 3.1, 6.6, 7.6**

  - [x]* 9.7 Write property test for identical outcomes from identical output
    - Same file: `test_property_preview_worker_request_identity.py`
    - **Property 2: Preview and Auto_Labeler derive identical outcomes from identical model output**
    - **Validates: Requirements 3.2, 3.11, 9.3**

  - [x]* 9.8 Write unit tests for the executor
    - Sequential processing, per-sample item and payload writes, terminal run transition with all samples failed, lock release on success and on an unexpected exception, no retry or second invocation per sample
    - _Requirements: 3.1, 3.5, 3.7, 8.8_

  - [x]* 9.9 Write integration tests for the preview flow
    - End-to-end with moto and a stub Converse client: seed a dataset prefix, `POST` a run as an authorized user, drive the executor invocation inline, poll the status route to `Completed`, assert per-sample payloads, the audit event, the released lock and unchanged jobs/tasks tables
    - Cross-account read path: single-account direct-access fallback through `get_s3_client_for_bucket` for both Sample_Images and example images
    - Worker few-shot path through the SQS record path: attached example ordering and identification with the option on, pre-feature request with it off
    - _Requirements: 3.5, 3.6, 6.5, 6.6, 10.2_

- [ ] 10. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 11. Add the frontend API client and wizard few-shot controls
  - [x] 11.1 Add preview client methods to `api.ts`
    - `startPreviewRun(body)` and `getPreviewRun(runId)` with typed request/response shapes for the run status, per-sample entries, failure categories and result payload URLs; extend the image-preview call with the `extensions` parameter and the model-options type with `image_limit`
    - _Requirements: 1.3, 2.1, 7.5_

  - [x] 11.2 Add the Few_Shot_Option and attach/omit hint to `CreateLabelingJob.tsx`
    - `fewShotEnabled` state defaulting to disabled, rendered only while `isLlmAutoLabelModel`, cleared in the existing model-compatibility effect so a non-`llm:` selection submits `few_shot.enabled === false`
    - Attach/omit hint computing `attached = min(total, limit - 1)` and `omitted = total - attached` from the selected model's `image_limit` (falling back to 20), recomputed whenever the model or either example list changes
    - Hoist `uploadExampleImages()` into a memoized `ensureExampleImagesUploaded()` keyed by the current file lists so examples upload once per file set and preview and submission share the same refs; submission carries the form's Detection_Prompt, model and option values
    - Submit `few_shot` with designations and positions matching the persisted shape
    - _Requirements: 1.1, 1.2, 5.5, 6.1, 6.2, 6.4, 6.9, 7.5, 10.5_

  - [x]* 11.3 Write unit tests for the wizard controls
    - Toggle default off, hidden for `sam` / `bedrock:` / no model, cleared on model family change; hint text at limit boundaries (`total < limit-1`, `total == limit-1`, `total > limit-1`, `limit == 1`) and after a model change; `ensureExampleImagesUploaded` uploads once per file set and reuses cached URIs at submission (asserted by call counts)
    - _Requirements: 1.2, 6.1, 6.9, 7.5, 10.5_

- [ ] 12. Implement the Prompt Tuning Preview UI
  - [x] 12.1 Create `PreviewResultCanvas.tsx`
    - `edge-cv-portal/frontend/src/components/labeling/PreviewResultCanvas.tsx`, read-only, reusing `AnnotationCanvas`'s exported `parseRleCounts`, `decodeRleColumnMajor`, `clampBoxToImage` and `CLASS_PALETTE`
    - Boxes positioned proportionally to the displayed image with the Label_Set class name adjacent; mask regions as translucent per-class fills with the class name associated; Classification results as the label text beside the image; a zero-detection Pre_Label as an explicit empty-result state visually distinct from a populated result and from a failure
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [x] 12.2 Create `PromptTuningPreview.tsx`
    - `edge-cv-portal/frontend/src/components/labeling/PromptTuningPreview.tsx` with the sample picker (paged key + thumbnail grid at `limit=50`, checkbox selection capped at 5, selection retained across pages and runs, thumbnail failure falling back to the object key while staying selectable, empty-prefix vs inaccessible-prefix messages that name the prefix and disable the run control, refresh re-listing and re-enabling it)
    - Run control with pre-flight validation identical to the API rules — on rejection list every violated rule, issue no request, leave wizard state untouched; disabled with an in-progress indication while a run is in flight; example upload failure aborts the start naming the failing file
    - Polling every 2 s stopping on `Completed` / `Failed` / `404` and after `sample_count × 120 s + 60 s`, rendering progressively; one result entry per requested sample keyed by sample key; a new run's results replace the previous set wholesale once its first result arrives, and a run failing before any result leaves the previous set unchanged
    - Failure rendering with category badge and reason beside the sample, plus an untruncated expandable region for the raw model output on `unusable_model_output`
    - _Requirements: 1.3, 1.4, 1.7, 1.8, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 4.5, 4.6, 4.7, 5.1, 5.2, 5.3, 5.4, 9.7, 9.8_

  - [x] 12.3 Wire `PromptTuningPreview` into the DDA setup step
    - Render it off `isLlmAutoLabelModel` so `sam` / `bedrock:` / no-model states render nothing new; pass the current model, Detection_Prompt, modality, Label_Set, few-shot state and dataset prefix; keep job submission available whether or not any run has started
    - _Requirements: 1.1, 1.2, 1.5, 5.1, 10.5_

  - [x]* 12.4 Write property test for control visibility
    - `edge-cv-portal/frontend/src/components/labeling/PromptTuningPreview.property.test.tsx`
    - **Property 14: Prompt Tuning controls appear exactly for the `llm:` family**
    - **Validates: Requirements 1.1, 1.2, 6.1, 6.9, 10.5**

  - [x]* 12.5 Write property test for client-side validation
    - Same file: `PromptTuningPreview.property.test.tsx`
    - **Property 15: Client-side validation names every violation, sends nothing, and keeps state**
    - **Validates: Requirements 1.4, 2.4, 6.2**

  - [x]* 12.6 Write property test for run failure handling
    - Same file: `PromptTuningPreview.property.test.tsx`
    - **Property 16: Preview run failures leave the flow usable and intact**
    - **Validates: Requirements 1.8, 4.7**

  - [x]* 12.7 Write property test for result set replacement
    - Same file: `PromptTuningPreview.property.test.tsx`
    - **Property 17: Results are displayed per sample, replaced wholly, and preserved on failure**
    - **Validates: Requirements 4.6, 5.3, 5.4**

  - [x]* 12.8 Write property test for result rendering
    - Same file: `PromptTuningPreview.property.test.tsx`
    - **Property 18: Rendering reflects each result's modality, emptiness and failure**
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 9.7, 9.8**

  - [x]* 12.9 Write property test for the attach/omit counts
    - Same file: `PromptTuningPreview.property.test.tsx`
    - **Property 20: Attached and omitted counts shown match what is attached**
    - **Validates: Requirements 7.5**

  - [x]* 12.10 Write property test for submission values
    - Same file: `PromptTuningPreview.property.test.tsx`; the persistence half of the property is asserted by the backend creation test in task 5.2
    - **Property 21: Submission carries the form's values, not a run's values**
    - **Validates: Requirements 5.5, 6.4**

  - [x]* 12.11 Write unit tests for the preview components
    - Sample picker 5-selection cap, selection persistence across pages and runs, thumbnail fallback, empty-vs-inaccessible messages, retry re-enabling the run
    - Polling stops on `Completed` / `Failed` / `404`, honors the overall bound, renders progressive results as samples resolve
    - `PreviewResultCanvas`: box scaling and class labels, RLE mask decode through the shared helper, classification label, zero-detection indication, raw-output disclosure for `unusable_model_output`
    - _Requirements: 2.3, 2.5, 2.6, 2.8, 4.1, 4.2, 4.3, 4.4, 4.5, 9.8_

- [x] 13. Final verification
  - Portal backend suites: `cd edge-cv-portal/backend && python3 -m pytest tests/ -q` — all new property, unit and integration tests green, no new failures beyond the repo's known pre-existing list
  - Frontend: `cd edge-cv-portal/frontend && npx tsc --noEmit` for the type check and `npx vitest run` for the unit and property tests
  - Infrastructure: `cd edge-cv-portal/infrastructure && npm test` for the CDK assertion tests, plus `npm run build`
  - Security preservation guard suite must be run **from the repo root with `PYTHONPATH=src/backend`** per the repo's builds steering: `PYTHONPATH=src/backend python3 -m pytest test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py -p no:cacheprovider --noconftest -q`; if the `cdk.out` drift guards fail because infrastructure work regenerated `cdk.out`, move it aside or rebaseline the sha256 entries before re-running
  - Per the builds steering, do **not** run a portal deploy (`deploy-portal.sh` / `deploy-infrastructure.sh` / `deploy-frontend.sh`) while a component build is running — check `pgrep -af "gdk component build"` and `pgrep -af "build-custom.sh"` first and sequence deploys after any build fully finishes

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; core implementation tasks are never optional
- Every correctness property from the design has exactly one property-based test (21 total), placed in the file the design's placement table names, run at a minimum of 100 iterations (`@settings(max_examples=100)` / `fc.assert(..., {numRuns: 100})`) and tagged `Feature: llm-autolabel-prompt-tuning, Property {n}: {text}`
- Property 5 (preservation) is written against a baseline captured from the pre-change code paths, so task 2.3 must land before task 4.1 introduces any few-shot behavior
- The design's smoke test (a deployed `POST /labeling-preview/runs` reaching `Completed`) is a deployment activity, not a coding task, and is intentionally not in this plan

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "6.1", "7.3"] },
    { "id": 1, "tasks": ["1.2", "2.1", "6.2", "7.1"] },
    { "id": 2, "tasks": ["1.3", "2.2", "6.3", "7.2"] },
    { "id": 3, "tasks": ["1.4", "2.3", "2.4", "6.4", "7.4"] },
    { "id": 4, "tasks": ["1.5", "4.1", "5.1", "6.5"] },
    { "id": 5, "tasks": ["4.2", "5.2", "8.1"] },
    { "id": 6, "tasks": ["8.2"] },
    { "id": 7, "tasks": ["8.3", "11.1"] },
    { "id": 8, "tasks": ["8.4", "9.1", "11.2"] },
    { "id": 9, "tasks": ["8.5", "9.8", "11.3", "12.1"] },
    { "id": 10, "tasks": ["8.6", "9.2", "12.2"] },
    { "id": 11, "tasks": ["8.7", "9.3", "12.3"] },
    { "id": 12, "tasks": ["9.4", "12.4"] },
    { "id": 13, "tasks": ["9.5", "12.5"] },
    { "id": 14, "tasks": ["9.6", "12.6"] },
    { "id": 15, "tasks": ["9.7", "12.7"] },
    { "id": 16, "tasks": ["9.9", "12.8"] },
    { "id": 17, "tasks": ["12.9"] },
    { "id": 18, "tasks": ["12.10"] },
    { "id": 19, "tasks": ["12.11"] }
  ]
}
```
