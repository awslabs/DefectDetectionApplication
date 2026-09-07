# Requirements Document

## Introduction

The user's grounded-sam Segmentation job prompted `gap between broken cookie pieces` and got whole-cookie masks back. Diagnosing that today means editing the prompt in the wizard, submitting a 72-image job, waiting for the fan-out to grind through the dataset, and inspecting the pre-labels — a multi-minute loop per prompt idea. The user's ask, verbatim: *"it would be good if the preview worked with the sample images like the LLM, that way I can prompt tune before submitting the job."*

The original grounded-sam-autolabel spec considered and rejected exactly this ("Prompt tuning preview stays `llm:`-only … Considered and rejected as a follow-up"). This spec is that follow-up, explicitly requested by the user; it **supersedes** that scoping decision and the grounded-sam-autolabel Requirement 7.3 clause pinning the preview's absence under a `grounded-sam` selection.

What exists to build on, verified in the shipped code:

- **The preview machinery is family-agnostic below its `llm:` gates.** `dda_labeling.py` already owns the run record (`PREVIEW#{run_id}` RUN + `IMAGE#{i:03d}` items), the per-user/per-Use_Case in-flight lock (conditional write, TTL `min(sample_count × 120 + 60, 900)`), the async self-invoke executor (`{'action': 'execute_preview_run'}`), per-sample progressive resolution (payload to `labeling-previews/{usecase_id}/{run_id}/{i}.json` before the item update), and the status route's presigned result URLs. The `llm:` restriction is one validation rule (`PREVIEW_MODEL_PREFIX`) plus the executor's single LLM sample path.
- **The renderer already draws grounded-sam's output shapes.** `PreviewResultCanvas.tsx` decodes Segmentation RLE regions through `AnnotationCanvas`'s shared `parseRleCounts`/`decodeRleColumnMajor` helpers and paints translucent per-class fills; it positions ObjectDetection boxes proportionally; it renders an explicit "No detections" empty state. The grounded-sam consumer's Pre_Label shapes (`{regions: [{class, rle, score?}]}` / `{boxes: [{class, left, top, width, height}]}`) are the shapes this component consumes today.
- **The worker contract is deployed and verified** (grounded-sam-mask-offset, live 2026-09-07): invoke `{image_s3_presigned_url, prompts: [{label, prompt}], modality}` → Segmentation `{regions: [{class, rle, score?}], image_width, image_height}` / ObjectDetection `{regions: [{class, score, box}], image_width, image_height}`; roughly 5 s warm per image, up to ~140 s cold start; the consumer bounds each synchronous invoke at `GROUNDED_SAM_MAX_TIMEOUT_SECONDS = 240` with retries disabled and validates the response strictly (`_generate_grounded_sam_prelabel`).
- **The Prompt_Map derivation and Prompt_Guardrail are shared, shipped rules.** `_grounded_sam_prompts` (override-or-label-name fallback) in the consumer; `_prompt_guardrail_errors` / `ALIGNMENT_BREAKING_CHARACTER = '.'` in `dda_labeling.py`; `promptOverrideGuardrails.tsx` in the frontend. A period-bearing Effective_Prompt deterministically fails the worker's caption alignment, so preview requests must enforce the identical rule at both ends.
- **Infrastructure is gated.** `compute-stack.ts` defines `DdaGroundedSamWorker` and wires `GROUNDED_SAM_WORKER_FUNCTION_NAME` + `grantInvoke` to `DdaAutolabelWorker` only, inside the `deployGroundedSamWorker`-gated block. The preview executor runs in `DdaLabelingHandler` (declared before that block; timeout already 900 s), which therefore needs the same env var and grant added *inside* the existing gated block — flag off keeps today's template.

Scoping decisions, each with its rationale:

- **The per-sample invocation bound is 240 seconds, not the LLM preview's 120.** The worker's CPU cold start alone approaches 140 s; a 120 s bound would deterministically fail every first-run sample. 240 s is the consumer's shipped `GROUNDED_SAM_MAX_TIMEOUT_SECONDS`, so preview and labeling time share one bound and a prompt that times out in preview times out in the job. The in-flight lock TTL keeps its `min(n × per-sample + 60, 900)` form with the family's per-sample seconds substituted.
- **The executor gains a deadline guard for grounded-sam runs.** Five samples at a worst-case 240 s each (1,200 s) exceeds `DdaLabelingHandler`'s 900 s Lambda timeout — a pathological all-timeouts run would die mid-flight leaving the run `Running` forever. Before each grounded-sam sample the executor checks the invocation's remaining time and, when another 240 s invoke cannot complete, resolves the remaining samples as `timeout` failures without invoking, so every run reaches a terminal status. LLM runs (bounded at 5 × 120 + 60 = 660 s < 900 s) cannot trip this and are left untouched.
- **Worker-not-deployed is a start-time validation rejection, not a per-sample failure.** At labeling time a job is accepted and each image fails with "worker is not configured" (grounded-sam-autolabel Req 5.4) because the job is durable state. A preview run is ephemeral; burning a run, a lock claim, and five S3-backed failure payloads to report a deployment fact known before the run starts helps nobody. The start route rejects with a validation error stating 'Grounded-SAM worker is not deployed'; the panel surfaces that message; the wizard and job creation stay fully functional (Req 5.4 semantics preserved).
- **Preview ObjectDetection results keep the worker's score; the stored job shape is untouched.** The consumer deliberately drops `score` from stored OD Pre_Labels (Bedrock-shape byte-exactness). The preview payload is not a pipeline artifact — it lives under the ephemeral `labeling-previews/` prefix — and the score is precisely what a prompt-tuner needs to judge threshold proximity. Segmentation already keeps `score` in the stored shape, so preview keeps it there too.
- **No detection prompt, no few-shot, no sizing controls for grounded-sam runs.** The family's prompt inputs are the per-label overrides; `detection_prompt`, `few_shot`, `downscale_max_edge` and `token_budget` are `llm:` concepts. The preview panel renders none of the llm-only controls under grounded-sam (those absences stay pinned), and the Preview_API rejects a grounded-sam request that carries few-shot enablement or a sizing value.

**Test-suite consequence, declared up front (the spec's entire permitted rebaseline class):** the shipped suites pin the preview's *absence* under grounded-sam. Exactly three files may change, in exactly these ways; every other pre-existing assertion stays byte-identical (zero-rebaseline rule):

1. `CreateLabelingJob.groundedsam.test.tsx` — the Req 7.3 example test asserts `queryByTestId('prompt-tuning-preview')` is null under grounded-sam. That one assertion inverts (the preview now renders, with a settling await); its sibling absences in the same test (detection prompt field, few-shot toggle, `preview-sizing-controls`, downscale/token controls) remain asserted absent under grounded-sam.
2. `CreateLabelingJob.guardrails.test.tsx` — the Req 3.1 guidance test selects grounded-sam without awaiting any preview settling (none existed). It gains the settling await for the now-mounting preview listing; every existing assertion (guidance renders under grounded-sam only, absent under sam/llm) is unchanged.
3. `CreateLabelingJob.guardrails.property.test.tsx` and `CreateLabelingJob.groundedsam.property.test.tsx` — their `apiService` Proxy mock resolves unmocked calls to `{}`; the newly mounting preview's listing call needs a benign `getImagePreview` resolution for deterministic settling. Mock-table/settling additions only; **no oracle or assertion changes**.

## Glossary

Terms carried over from the dda-data-labeling, llm-autolabel-prompt-tuning, grounded-sam-autolabel, and grounded-sam-prompt-guardrails-and-prelabel-retry specs keep their existing definitions (Portal, DDA_Labeling_System, Labeling_Job, Label_Set, Labeling_Modality, Job_Creator, Auto_Labeler, Pre_Label, Prompt_Override, Prompt_Map, Effective_Prompt, Prompt_Guardrail, Alignment_Breaking_Character, Grounded_SAM_Worker, Worker_Flag, Sample_Image, Sample_Limit, Preview_Run, Preview_Result, Use_Case). New or constrained terms:

- **Preview_Panel**: The `PromptTuningPreview` surface rendered inside the labeling job creation wizard (testid `prompt-tuning-preview`): sample picker, run control, and results area.
- **Preview_API**: The two shipped routes on `DdaLabelingHandler`: `POST /labeling-preview/runs` (start) and `GET /labeling-preview/runs/{runId}` (status), both authorized by `MANAGE_LABELING_JOBS` through `rbac_check`.
- **Preview_Executor**: The async self-invoked `execute_preview_run` path of `DdaLabelingHandler` that resolves a Preview_Run's Sample_Images sequentially.
- **Grounded_SAM_Preview_Run**: A Preview_Run whose recorded model is `grounded-sam`.
- **Preview_Prompt_Map**: The `[{label, prompt}]` list a Grounded_SAM_Preview_Run sends the Grounded_SAM_Worker per Sample_Image: one entry per Label_Set label in Label_Set order, each prompt the label's Prompt_Override when it survives trimming, otherwise the label name — the identical derivation the Auto_Labeler's `_grounded_sam_prompts` applies at labeling time.
- **Worker_Invoke_Bound**: 240 seconds — `GROUNDED_SAM_MAX_TIMEOUT_SECONDS`, the synchronous Grounded_SAM_Worker invocation wall-clock bound the Auto_Labeler already enforces, adopted per Sample_Image by the Preview_Executor for Grounded_SAM_Preview_Runs.
- **Run_Deadline_Guard**: The Preview_Executor check, applied to Grounded_SAM_Preview_Runs only, that resolves each remaining Sample_Image as a `timeout` failure without invocation when the executor's remaining Lambda time cannot accommodate another Worker_Invoke_Bound invocation.
- **Not_Deployed_Message**: The exact validation error message `Grounded-SAM worker is not deployed`.
- **Region_Score**: The optional per-region confidence the Grounded_SAM_Worker returns (`score`), carried on Grounded_SAM_Preview_Run result payloads for display.

## Requirements

### Requirement 1: The Preview_Panel is offered for the grounded-sam family

**User Story:** As a Job_Creator, I want the prompt tuning preview available when grounded-sam is my selected auto-label model, so that I can iterate on per-label text prompts against a few sample images before submitting a full job.

#### Acceptance Criteria

1. WHILE auto-labeling is enabled and `grounded-sam` is the selected auto-label model and the Labeling_Modality is Segmentation, THE Portal SHALL render the Preview_Panel inside the wizard's labeling setup step.
2. WHILE auto-labeling is enabled and `grounded-sam` is the selected auto-label model and the Labeling_Modality is ObjectDetection, THE Portal SHALL render the Preview_Panel inside the wizard's labeling setup step.
3. WHILE `grounded-sam` is the selected auto-label model, THE Preview_Panel SHALL offer the same Sample_Image selection surface it offers for `llm:` selections: the paged dataset-prefix listing with thumbnails, checkbox selection capped at the Sample_Limit of 5, selection retained across pages and runs, thumbnail-failure fallback to the object key, distinct empty-prefix and inaccessible-prefix messages naming the prefix, and the refresh control.
4. WHILE `grounded-sam` is the selected auto-label model, THE Preview_Panel SHALL render none of the `llm:`-only controls: no detection prompt entry, no few-shot toggle, no image downscaling control, and no output token budget control.
5. WHILE `sam` or a `bedrock:` model is the selected auto-label model, or no auto-label model is selected, THE Portal SHALL render no Preview_Panel.
6. WHILE `grounded-sam` is the selected auto-label model, THE Preview_Panel SHALL display a timing expectation stating that inference runs on CPU at roughly 5 seconds per image once warm and that the first run after idle can take a few minutes while the worker starts.

### Requirement 2: Preview prompts are the job's prompts, guardrail-validated

**User Story:** As a Job_Creator, I want the preview to send exactly the per-label prompts my job would send, and to refuse prompts the worker would deterministically reject, so that what I tune in the preview is what the job will do.

#### Acceptance Criteria

1. WHEN a Grounded_SAM_Preview_Run is started, THE Preview_Panel SHALL send the wizard's Prompt_Override entries pruned exactly as job creation prunes them: only entries non-empty after trimming whose label is in the effective Label_Set, each value character-for-character as entered.
2. WHEN the Preview_Executor invokes the Grounded_SAM_Worker for a Sample_Image, THE DDA_Labeling_System SHALL derive the Preview_Prompt_Map from the run's recorded Label_Set and Prompt_Override entries with the override-or-label-name fallback the Auto_Labeler's `_grounded_sam_prompts` applies.
3. WHEN the run control is activated for a grounded-sam selection while any label's Effective_Prompt contains an Alignment_Breaking_Character, THE Preview_Panel SHALL list one corrective error per offending label using the shared Prompt_Guardrail wording, SHALL issue no request, and SHALL leave wizard state untouched.
4. IF a Preview_Run request with the model value `grounded-sam` carries a Label_Set and Prompt_Override entries whose Effective_Prompts include an Alignment_Breaking_Character, THEN THE Preview_API SHALL reject the request with one validation error per offending label using the shared `_prompt_guardrail_errors` wording, and SHALL claim no lock, persist no run state, and invoke no worker.
5. WHEN a Preview_Run request with the model value `grounded-sam` carries Prompt_Override entries, THE Preview_API SHALL validate them with the job-creation rules: a JSON object, keys within the request's Label_Set, string values of at most 256 characters, entries blank after trimming dropped.

### Requirement 3: Grounded-sam requests ride the existing Preview_API machinery

**User Story:** As a portal operator, I want grounded-sam preview runs to use the shipped run-record, lock, and polling machinery, so that the preview surface stays one mechanism with family-specific execution.

#### Acceptance Criteria

1. WHEN `POST /labeling-preview/runs` receives a request with the model value `grounded-sam`, a Labeling_Modality of Segmentation or ObjectDetection, a valid Label_Set, guardrail-clean prompts, and 1 to 5 in-scope Sample_Images, THE Preview_API SHALL accept it with the 202 `{run_id, sample_count, status}` response, write the RUN item and one Pending sample item per Sample_Image, record the pruned Prompt_Override entries on the RUN item, and start the Preview_Executor asynchronously.
2. IF a Preview_Run request carries a model value that is neither `grounded-sam` nor an `llm:`-prefixed identifier, THEN THE Preview_API SHALL reject the request with a validation error naming the accepted families.
3. IF a Preview_Run request with the model value `grounded-sam` names the Classification modality, THEN THE Preview_API SHALL reject the request with a validation error stating the grounded-sam family supports Segmentation and ObjectDetection.
4. IF a Preview_Run request with the model value `grounded-sam` carries few-shot enablement, a `downscale_max_edge` value other than null, or a `token_budget` value, THEN THE Preview_API SHALL reject the request with one validation error per inapplicable field stating the field does not apply to the grounded-sam family.
5. WHEN a Grounded_SAM_Preview_Run start request is validated, THE Preview_API SHALL apply the existing shared rules unchanged: Use_Case resolution, dataset-prefix requirement, 1-to-5 Sample_Image count, per-reference scope resolution against the Use_Case dataset location, all violations enumerated in one 400 response, and no S3 read or model invocation on any rejection path.
6. WHEN a Grounded_SAM_Preview_Run is accepted, THE Preview_API SHALL claim the existing per-user, per-Use_Case in-flight lock with an expiry of `min(sample_count × 240 + 60, 900)` seconds, and SHALL answer 409 with the existing in-progress message while the caller already holds an active claim.
7. WHEN a Grounded_SAM_Preview_Run is started, THE DDA_Labeling_System SHALL record the same `preview_run` audit event the `llm:` start path records, carrying the requesting identity, Use_Case, the model value `grounded-sam`, the Sample_Image count, and the Labeling_Modality.
8. THE Preview_API SHALL authorize Grounded_SAM_Preview_Run start and status requests exactly as it authorizes `llm:` ones: `MANAGE_LABELING_JOBS` through `rbac_check` with the flattened fixed-body 403, and the status route's fixed 404 for an unknown run or another user's run.
9. WHILE a Grounded_SAM_Preview_Run is in flight, THE Preview_Panel SHALL short-poll the status route at the existing 2-second interval, render per-sample results progressively as they resolve, and stop polling on a terminal status, a 404, or after `min(sample_count × 240 + 60, 900) + 60` seconds.
10. WHEN a new Preview_Run's first result arrives, THE Preview_Panel SHALL replace the previously displayed result set wholesale, and a run that fails before producing any result SHALL leave the previous result set displayed unchanged — the existing replacement semantics.

### Requirement 4: The Preview_Executor runs grounded-sam samples against the deployed worker

**User Story:** As a Job_Creator, I want each sample image processed by the same worker with the same contract my job would use, so that preview results faithfully predict labeling-time results.

#### Acceptance Criteria

1. WHEN the Preview_Executor processes a Grounded_SAM_Preview_Run Sample_Image, THE Preview_Executor SHALL invoke the function named by `GROUNDED_SAM_WORKER_FUNCTION_NAME` synchronously with a time-limited presigned URL for the Sample_Image, the Preview_Prompt_Map, and the run's Labeling_Modality, with the invocation wall clock bounded at the Worker_Invoke_Bound and retries disabled.
2. WHEN a Grounded_SAM_Preview_Run Sample_Image invocation returns, THE Preview_Executor SHALL validate the response with the Auto_Labeler's rules: a `regions` list and integer image dimensions; for Segmentation a Label_Set member class and non-empty RLE per region; for ObjectDetection a Label_Set member class and box geometry with positive extent inside the image bounds per region.
3. WHEN a Grounded_SAM_Preview_Run Sample_Image resolves successfully, THE Preview_Executor SHALL write a result payload carrying the Pre_Label in the shapes `PreviewResultCanvas` consumes — Segmentation `{regions: [{class, rle}]}`, ObjectDetection `{boxes: [{class, left, top, width, height}]}` — with each region or box additionally carrying its Region_Score when the worker returned one, plus the worker-reported image dimensions.
4. IF the Grounded_SAM_Worker returns an empty `regions` list for a Sample_Image, THEN THE Preview_Executor SHALL resolve that Sample_Image as Succeeded with an empty Pre_Label.
5. IF a Grounded_SAM_Preview_Run Sample_Image invocation raises, times out, returns a function error, or returns a payload failing Requirement 4.2's validation, THEN THE Preview_Executor SHALL resolve that Sample_Image as Failed with exactly one existing failure category (`timeout` for a timed-out invocation, `model_error` otherwise) and a reason carrying the worker's error detail, and SHALL continue with the next Sample_Image.
6. IF a Grounded_SAM_Preview_Run Sample_Image's presigned URL cannot be produced, THEN THE Preview_Executor SHALL resolve that Sample_Image as Failed with the `image_access_failure` category naming the object.
7. WHEN the Preview_Executor's remaining invocation time cannot accommodate another Worker_Invoke_Bound invocation for a Grounded_SAM_Preview_Run, THE Run_Deadline_Guard SHALL resolve each remaining Pending Sample_Image as a `timeout` failure with a reason stating the run deadline was reached, without invoking the worker.
8. WHEN every Sample_Image of a Grounded_SAM_Preview_Run has resolved, THE Preview_Executor SHALL mark the run Completed — including a run in which every Sample_Image failed — and SHALL release the in-flight lock on every terminal path.
9. THE Preview_Executor SHALL process Grounded_SAM_Preview_Run Sample_Images sequentially in request order, writing each result payload before the sample item that references it, and SHALL create no Labeling_Job record, no Task_Assignment item, no pipeline Pre_Label artifact, and no labeler notification.

### Requirement 5: Results render as overlays with class, score, and clear empty and failure states

**User Story:** As a Job_Creator, I want each sample's masks or boxes drawn over the image with the class and confidence visible, so that I can judge at a glance whether my prompt found the right thing.

#### Acceptance Criteria

1. WHEN a Segmentation Grounded_SAM_Preview_Run Sample_Image resolves successfully with regions, THE Preview_Panel SHALL render each region's RLE mask as a translucent class-colored overlay on the Sample_Image through the existing client-side RLE decoding path, with a legend associating each region's class name.
2. WHEN an ObjectDetection Grounded_SAM_Preview_Run Sample_Image resolves successfully with boxes, THE Preview_Panel SHALL render each box as a proportionally positioned rectangle over the Sample_Image with its class name adjacent.
3. WHERE a rendered region or box carries a Region_Score, THE Preview_Panel SHALL display that score alongside the class name.
4. WHEN a Grounded_SAM_Preview_Run Sample_Image resolves successfully with an empty Pre_Label, THE Preview_Panel SHALL render the existing explicit no-detections indication as a success state, visually distinct from a failure.
5. WHEN a Grounded_SAM_Preview_Run Sample_Image resolves as Failed, THE Preview_Panel SHALL display the failure category and the reason beside that sample — including the worker-not-configured, timeout, and invalid-response reasons the Preview_Executor recorded.

### Requirement 6: Graceful degradation when the worker is not deployed

**User Story:** As a portal operator running a flag-off deployment, I want the preview to state plainly that the worker is unavailable, so that the wizard keeps working and nobody mistakes a deployment gap for a product bug.

#### Acceptance Criteria

1. IF `GROUNDED_SAM_WORKER_FUNCTION_NAME` is absent from the Preview_API's environment and a Preview_Run request names the model value `grounded-sam`, THEN THE Preview_API SHALL reject the request with a validation error carrying the Not_Deployed_Message, and SHALL claim no lock and persist no run state.
2. WHEN a Grounded_SAM_Preview_Run start attempt is rejected with the Not_Deployed_Message, THE Preview_Panel SHALL display the Not_Deployed_Message to the Job_Creator and SHALL leave the wizard and its sample selection intact and operable.
3. WHILE the Grounded_SAM_Worker is not deployed, THE DDA_Labeling_System SHALL keep accepting `grounded-sam` job creations with the grounded-sam-autolabel Requirement 5.4 semantics unchanged.

### Requirement 7: The tune loop — edited prompts drive the next run

**User Story:** As a Job_Creator, I want to edit a prompt and immediately re-run the preview, so that I can converge on a working prompt in minutes instead of job-submission cycles.

#### Acceptance Criteria

1. WHEN a Job_Creator edits a Prompt_Override entry after a completed Grounded_SAM_Preview_Run and starts a new run, THE Preview_Panel SHALL send the edited entries with the new request, and THE Preview_Executor SHALL derive that run's Preview_Prompt_Map from the new run's own recorded entries.
2. WHILE a Grounded_SAM_Preview_Run is in flight, THE Preview_Panel SHALL disable the run control and show the in-progress indication, re-enabling it when the run reaches a terminal state, a 404, or the polling bound.
3. WHEN a new Grounded_SAM_Preview_Run is started after a previous one, THE Preview_Panel SHALL retain the existing Sample_Image selection so the same samples are re-previewed without re-selection.

### Requirement 8: Infrastructure — additive wiring inside the existing gated block

**User Story:** As a portal operator, I want the preview's worker access wired only when the worker is deployed, so that flag-off deployments keep today's template exactly.

#### Acceptance Criteria

1. WHERE the Worker_Flag is set true, THE compute stack SHALL set `GROUNDED_SAM_WORKER_FUNCTION_NAME` on `DdaLabelingHandler`'s environment and grant `DdaLabelingHandler` invoke permission on the Grounded_SAM_Worker, inside the existing `deployGroundedSamWorker`-gated block.
2. WHERE the Worker_Flag is absent or not true, THE compute stack SHALL leave `DdaLabelingHandler`'s environment without a `GROUNDED_SAM_WORKER_FUNCTION_NAME` entry and grant no new permission, producing today's flag-off template.
3. WHERE the Worker_Flag is set true, THE compute stack SHALL leave the existing `DdaAutolabelWorker` wiring, the Grounded_SAM_Worker definition, and every other function's configuration unchanged by this feature.

### Requirement 9: Preservation

**User Story:** As a portal operator, I want everything outside the grounded-sam preview surface to behave byte-identically, so that this feature cannot regress the shipped preview, wizard, or labeling flows.

#### Acceptance Criteria

1. WHILE an `llm:` model is the selected auto-label model, THE Portal SHALL render and operate the Preview_Panel byte-identically to before this feature: the same controls, the same validation, the same request shapes, the same polling bound of `sample_count × 120 + 60` seconds, and the same result rendering.
2. WHEN `POST /labeling-preview/runs` receives a request with an `llm:`-prefixed model value, THE Preview_API SHALL validate, record, audit, execute, and report it byte-identically to before this feature, including the lock TTL's 120-second per-sample term.
3. WHEN a labeling job is created or submitted, THE DDA_Labeling_System SHALL produce job records, fan-out messages, and Pre_Label artifacts unaffected by any Preview_Run activity, with the submit payload assembled from wizard state exactly as before this feature.
4. THE DDA_Labeling_System SHALL leave the Auto_Labeler's `_generate_grounded_sam_prelabel`, `_grounded_sam_prompts`, and every labeling-time code path unchanged by this feature.
5. WHEN a Preview_Result payload for an `llm:` run is written or rendered, THE DDA_Labeling_System SHALL keep its shape and rendering byte-identical to before this feature, the Region_Score display appearing only when a region or box carries a score.

### Requirement 10: Declared test rebaseline class

**User Story:** As a maintainer, I want the shipped suites that pin the preview's absence under grounded-sam amended in exactly the declared ways, so that the supersession of grounded-sam-autolabel Requirement 7.3 is auditable and everything else stays pinned.

#### Acceptance Criteria

1. WHEN this feature's changes land, THE test suite SHALL amend `CreateLabelingJob.groundedsam.test.tsx`'s Req 7.3 example test to assert the Preview_Panel's presence under grounded-sam while keeping its sibling absence assertions (detection prompt, few-shot toggle, sizing controls) asserted under grounded-sam.
2. WHEN this feature's changes land, THE test suite SHALL amend `CreateLabelingJob.guardrails.test.tsx`'s Req 3.1 test only by adding preview-settling awaits for the grounded-sam selection, keeping every existing assertion unchanged.
3. WHERE the wizard property suites (`CreateLabelingJob.guardrails.property.test.tsx`, `CreateLabelingJob.groundedsam.property.test.tsx`) require the newly mounting Preview_Panel's listing call to settle, THE test suite SHALL add benign `getImagePreview` mock resolutions only, with zero oracle or assertion changes.
4. THE test suite SHALL keep every pre-existing assertion outside the three files above byte-identical, and any further pre-existing assertion change SHALL be treated as a design violation to stop on.
