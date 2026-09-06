# Requirements Document

## Introduction

Job `labeling-8022a9dc` — a `grounded-sam` Segmentation job over 72 images with one label, `cookie_gap`, and an instruction-style Prompt_Override containing inner periods ("draw and fill in the gaps ... within the bounds of the image. If there is a large crack ...") — reached the Grounded_SAM_Worker on every image and failed on every image with `prelabel_error: Grounded-SAM worker failed: {"errorMessage": "caption token spans (2) do not align with the 1 prompts; a prompt likely contains inner sentence punctuation", "errorType": "ValueError"}`. The failure chain, verified in the shipped code:

1. `gsam_utils.build_caption` joins the per-label prompts into one Grounding DINO caption with `'. '` as the phrase separator, stripping only *trailing* dots from each phrase — an inner `.` survives into the caption.
2. `handler.py`'s `_marker_token_ids` derives the span-splitting separator set from `tokenizer.token_to_id('.')` — exactly the ASCII period token, plus the `[CLS]`/`[SEP]`/`[PAD]` specials (`[UNK]` deliberately excluded). The tokenizer emits an inner `.` as its own token, so one prompt yields two token spans.
3. The deliberate alignment guard at `handler.py` `_detect` (~line 482-491) sees more spans than prompts and raises — the correct behavior for the worker (mis-attributed classes would be worse), but the job's every image is doomed at creation time.

The user could not recover: no re-run mechanism exists, and `auto_label.prompt_overrides` is frozen on the job record at creation. The user also did not notice the 72 Failed pre-labels for some time ("It's not attempting to pre-label anything") — the job detail page shows a failed count once tasks resolve, but never the failure reason.

This spec adds two features:

**Feature 1 — Prompt guardrails.** The wizard and the API reject grounded-sam prompts that would deterministically trip the alignment guard, and the override inputs teach what a good Grounding DINO prompt looks like. The reject set is grounded in the separator derivation, not guessed:

- **Exactly the ASCII period `.` breaks alignment.** `_marker_token_ids` puts only `tokenizer.token_to_id('.')` in the separator set. `?`, `!`, `;`, `,` and the CJK full stop `。` tokenize as ordinary tokens with their own ids, extend the phrase span, and do not split it — rejecting them would be unjustified. The rule is therefore: no `.` anywhere in the prompt.
- **The rule is judged on the Effective_Prompt, not just the override.** The Prompt_Map falls back to the label name when no override survives trimming (`_grounded_sam_prompts`), so a *label name* containing `.` (e.g. "v1.2 defect") with no override reproduces the incident identically. Validation covers the effective prompt per label, with an error that distinguishes the two sources.
- **Stricter than the worker on trailing dots, deliberately.** `build_caption` strips trailing dots, so "scratch." would actually work — but "no periods" is a teachable rule and deleting a trailing dot costs the user nothing. Simplicity wins.
- **The worker is unchanged.** Its guard already fails descriptively per image and protects against mis-attribution; the fix belongs at the input boundary. Considered and recorded — the worker image is not rebuilt by this spec.

**Feature 2 — Re-run pre-labels for Failed tasks.** A new job-scoped API re-runs Pre_Label generation for a job's Failed-pre-label tasks, for **all** auto-label families (`grounded-sam`, `sam`, `bedrock:`, `llm:` — transient failures affect all of them), optionally updating a grounded-sam job's `prompt_overrides` in the same request (validated identically to creation, including the new rule — a retry with the same broken prompt is useless). Mechanics grounded in the shipped machinery:

- **The retry rides the existing fan-out and consumer, untouched.** The API handler persists updated overrides to the job record and async-invokes `dda_labeling_worker` with a new action; the worker (which already holds `AUTOLABEL_QUEUE_URL` and queue send permission — and `grantInvoke` from the API handler already exists) conditionally resets each Failed task and enqueues one message per reset task in the exact fan-out shape. The consumer reads `prompt_overrides` from the job record per message ("Prompt_Overrides ride the job record, not the fan-out message"), so updated prompts take effect with zero consumer change and **zero compute-stack permission change**.
- **Task items already carry `image_s3_uri`**, so the retry enqueues from the task items without re-enumerating the dataset.
- **Counters re-arm consistently.** The consumer's conditional `_mark_task` (resolve only while not Available/Failed) makes duplicate deliveries and concurrent retries safe; for Skip_Verification_Mode jobs the retry reverses the counter movement the Failed resolutions already made (`autolabel_pending` re-incremented, `autolabel_completed_count` re-decremented, `review_ready` cleared) so the consumer's existing decrement-to-zero machinery re-arms.
- **The reset clears `prelabel_error` and `autolabel_error`.** The consumer's success path never removes them, so a task that failed once and then succeeded would otherwise stay review-ineligible forever.
- **Failure visibility.** The job detail response gains a distinct-failure-reason summary and the page surfaces failures prominently with the reasons — the incident's invisibility gap.

**Test-suite consequence, declared up front:** the shipped grounded-sam-autolabel suites pinned override values as free unicode text. Two generators legitimately produce `.` and assert such submissions succeed — the new validation makes them reject. This spec's **permitted rebaseline class** is exactly two generator amendments: `CreateLabelingJob.groundedsam.property.test.tsx` (`labelArb`, `overrideValueArb`) and `test_property_grounded_sam_job_creation.py` (period-free label/value strategies for its valid-space properties). Every other pinned suite stays byte-identical; the design inventories them.

## Glossary

Terms carried over from the dda-data-labeling, llm-auto-labeling, and grounded-sam-autolabel specs keep their existing definitions (Portal, DDA_Labeling_System, Labeling_Job, Label_Set, Modality, Job_Creator, Auto_Labeler, Pre_Label, Prompt_Override, Prompt_Map, Grounded_SAM_Worker, Skip_Verification_Mode). New or constrained terms:

- **Alignment_Breaking_Character**: The ASCII period `.` (U+002E) — the only character in the Grounded_SAM_Worker's caption-span separator set, derived from `tokenizer.token_to_id('.')`. A prompt containing one anywhere fails the Prompt_Guardrail.
- **Effective_Prompt**: For one Label_Set label of a grounded-sam Labeling_Job: the label's Prompt_Override when one is present and non-empty after trimming, otherwise the label name itself — the exact value the Prompt_Map sends to Grounding DINO.
- **Prompt_Guardrail**: The validation rule this spec adds: every Effective_Prompt of a grounded-sam Labeling_Job contains no Alignment_Breaking_Character.
- **Prompt_Guidance**: The explanatory content on Prompt_Override inputs teaching what a Grounding DINO prompt is (a short noun phrase naming the visual thing to find, not an instruction), with examples, the no-periods rule, and the empty-means-label-name default.
- **Failed_Prelabel_Task**: A Labeling_Job task item whose `prelabel_status` is `Failed`.
- **Retry_Request**: A `POST /labeling/{id}/rerun-prelabels` request, optionally carrying replacement `prompt_overrides` for a grounded-sam job.
- **Retry_Eligible_Job**: A DDA Labeling_Job with status `InProgress`, auto-labeling enabled (`auto_label.enabled` or Skip_Verification_Mode), no truthy `review_finalized` flag, and at least one Failed_Prelabel_Task.
- **Retry_Action**: The `dda_labeling_worker` action (`{action: 'retry_prelabels', job_id}`) that resets Failed_Prelabel_Tasks and re-enqueues their auto-label messages.
- **Fanout_Message**: The auto-label SQS message shape the distributor enqueues per image: `{job_id, task_id, image_s3_uri, modality, label_set, model}` plus `detection_prompt` for the `llm:` family and `per_label_prompts` for Skip_Verification_Mode jobs.
- **Failure_Reason_Summary**: The job-detail field listing the distinct `prelabel_error` strings of a job's Failed_Prelabel_Tasks, each with its occurrence count.
- **Job_Detail_Page**: The Portal's DDA labeling job detail rendering (`LabelingDetail.tsx`), including its Auto-Labeling container.

## Requirements

### Requirement 1: Wizard rejects alignment-breaking prompts

**User Story:** As a Job_Creator, I want the wizard to reject grounded-sam text prompts that would break detection caption alignment, so that I cannot create a job whose every image is doomed to fail pre-labeling.

#### Acceptance Criteria

1. WHEN a Job_Creator advances the wizard's setup step while `grounded-sam` is the selected auto-label model and any label's Prompt_Override contains an Alignment_Breaking_Character, THE Portal SHALL reject the step with an error naming the label and stating that periods separate labels in the detection caption.
2. WHEN a Job_Creator advances the wizard's setup step while `grounded-sam` is the selected auto-label model and any label whose Prompt_Override is empty after trimming has a label name containing an Alignment_Breaking_Character, THE Portal SHALL reject the step with an error naming the label and stating that the label name is used as the text prompt, directing the Job_Creator to enter a Prompt_Override without periods for that label.
3. WHILE a Prompt_Override entry contains an Alignment_Breaking_Character, THE Portal SHALL display the Requirement 1.1 error as that entry's field-level error text.
4. WHEN a Job_Creator advances the wizard's setup step while `grounded-sam` is the selected auto-label model and every Effective_Prompt satisfies the Prompt_Guardrail and the existing length rule, THE Portal SHALL accept the step exactly as before this feature — including Effective_Prompts containing commas, question marks, exclamation points, or semicolons, which do not split caption spans.
5. WHILE an auto-label model other than `grounded-sam` is selected (or none), THE Portal SHALL apply no Prompt_Guardrail to any wizard input.

### Requirement 2: API rejects alignment-breaking prompts

**User Story:** As a portal operator, I want the job-creation API to enforce the same prompt rules the wizard enforces, so that no client path can create a grounded-sam job that the alignment guard will deterministically fail.

#### Acceptance Criteria

1. IF a labeling job submission with the model value `grounded-sam` carries a `prompt_overrides` value that survives trimming and contains an Alignment_Breaking_Character, THEN THE DDA_Labeling_System SHALL reject the submission with a validation error naming the label and stating that periods separate labels in the detection caption, and SHALL persist nothing.
2. IF a labeling job submission with the model value `grounded-sam` includes a Label_Set label whose name contains an Alignment_Breaking_Character and whose Prompt_Override is absent or empty after trimming, THEN THE DDA_Labeling_System SHALL reject the submission with a validation error naming the label and stating that the label name is used as the text prompt, and SHALL persist nothing.
3. WHEN a labeling job submission with the model value `grounded-sam` carries multiple Prompt_Guardrail violations, THE DDA_Labeling_System SHALL enumerate one validation error per offending label in the rejection response.
4. WHEN a labeling job submission with the model value `grounded-sam` satisfies the Prompt_Guardrail and the pre-existing validation rules, THE DDA_Labeling_System SHALL accept and persist the submission exactly as before this feature.
5. THE DDA_Labeling_System SHALL judge a Prompt_Override against the Prompt_Guardrail exactly when the override survives trimming, so that the set of submissions the DDA_Labeling_System accepts equals the set the Portal's wizard accepts for the same Label_Set and override values.
6. WHEN a labeling job submission with a model value other than `grounded-sam` is validated, THE DDA_Labeling_System SHALL apply no Prompt_Guardrail, leaving those families' validation and persisted records byte-identical to before this feature.

### Requirement 3: Prompt guidance on the override inputs

**User Story:** As a Job_Creator, I want the override inputs to teach me what a good detection prompt looks like, so that I write noun phrases the model can ground instead of instructions it cannot.

#### Acceptance Criteria

1. WHILE `grounded-sam` is the selected auto-label model, THE Portal SHALL present Prompt_Guidance on the Prompt_Override entries comprising constraint text stating the no-periods rule and an info affordance (the page's established FormField `info` pattern) expanding the full guidance.
2. THE Prompt_Guidance SHALL state that a prompt is a short noun phrase naming the visual thing to find, SHALL include at least two noun-phrase examples (for example "gap between broken cookie pieces" and "scratch on metal surface"), and SHALL state that the detector localizes what the text names while the mask model turns the resulting boxes into masks.
3. THE Prompt_Guidance SHALL state that instruction-style text (for example "draw a polygon around each gap" or "produce json") does not work because the model grounds noun phrases rather than following directions.
4. THE Prompt_Guidance SHALL state that periods are not allowed because they separate labels in the caption, that commas are acceptable, and that an empty entry means the label name itself is used as the prompt.
5. WHERE the re-run override editor of Requirement 7 presents Prompt_Override entries, THE Portal SHALL present the same Prompt_Guidance content on those entries.

### Requirement 4: Failed pre-labels are visible with reasons on the job detail

**User Story:** As a Job_Creator, I want the job page to show me clearly that pre-labels failed and why, so that I notice the failure and know what to fix instead of concluding the system is not attempting anything.

#### Acceptance Criteria

1. WHEN the DDA_Labeling_System serves a DDA job detail for a job with at least one Failed_Prelabel_Task, THE DDA_Labeling_System SHALL include a Failure_Reason_Summary in the response: the distinct `prelabel_error` values among the job's active Failed_Prelabel_Tasks, each with its occurrence count, ordered by descending count and capped at 5 distinct reasons.
2. WHEN the Job_Detail_Page renders a DDA job whose `prelabel_failed_count` is at least 1, THE Job_Detail_Page SHALL display a warning alert stating the failed pre-label count and listing the Failure_Reason_Summary entries with their counts.
3. WHEN the Job_Detail_Page renders a DDA job whose `prelabel_failed_count` is 0 or absent, THE Job_Detail_Page SHALL display no failed-pre-label warning alert and SHALL render exactly as before this feature.
4. WHEN the DDA_Labeling_System serves a DDA job detail for a job with zero Failed_Prelabel_Tasks, THE DDA_Labeling_System SHALL omit the Failure_Reason_Summary field, leaving the response byte-identical to before this feature.

### Requirement 5: Re-run pre-labels API

**User Story:** As a Job_Creator, I want an API that re-runs pre-label generation for a job's failed tasks, optionally with corrected prompts, so that a transient failure or a bad prompt does not permanently cost the job its pre-labels.

#### Acceptance Criteria

1. THE DDA_Labeling_System SHALL expose `POST /labeling/{id}/rerun-prelabels` on the DDA labeling API, accepting an optional request body carrying `prompt_overrides`.
2. WHEN a Retry_Request arrives, THE DDA_Labeling_System SHALL authorize the caller for the `manage_labeling_jobs` permission in the job's Use_Case scope before any other processing, and SHALL answer an unauthorized caller with 403.
3. IF a Retry_Request targets a Skip_Verification_Mode job and the caller does not hold the UseCaseAdmin or PortalAdmin role, THEN THE DDA_Labeling_System SHALL reject the Retry_Request with 403 and write an `unauthorized_access` audit event, mirroring the Admin_Review authorization.
4. IF a Retry_Request targets a job that is not a Retry_Eligible_Job, THEN THE DDA_Labeling_System SHALL reject the Retry_Request with a 400 response naming the unmet condition (job not found answers 404), and SHALL reset no task and enqueue no message.
5. WHEN a Retry_Request on a grounded-sam Retry_Eligible_Job carries `prompt_overrides`, THE DDA_Labeling_System SHALL validate the value with the identical rules job creation applies — an object whose keys belong to the job's Label_Set, string values of raw length at most 256, blank-after-trim values dropped, survivors kept character-for-character, and the Prompt_Guardrail over the resulting Effective_Prompts — and on success SHALL persist the surviving overrides under the job record's `auto_label.prompt_overrides` (removing the key when none survives) before any task reset or message enqueue.
6. IF a Retry_Request on a grounded-sam Retry_Eligible_Job omits `prompt_overrides` and the job's persisted Effective_Prompts violate the Prompt_Guardrail, THEN THE DDA_Labeling_System SHALL reject the Retry_Request with the Requirement 2 error content naming each offending label, so that a retry that would deterministically re-fail the alignment guard is refused with the corrective guidance.
7. IF a Retry_Request carries `prompt_overrides` for a job whose auto-label model is not `grounded-sam`, THEN THE DDA_Labeling_System SHALL reject the Retry_Request with a validation error stating that prompt overrides apply only to grounded-sam jobs.
8. IF a Retry_Request's `prompt_overrides` validation fails, THEN THE DDA_Labeling_System SHALL persist no override change, reset no task, and enqueue no message.
9. WHEN a Retry_Request is accepted, THE DDA_Labeling_System SHALL answer 202 with the job id and the count of Failed_Prelabel_Tasks being retried, SHALL write a `prelabels_rerun` audit event recording the job id, the retried count, and whether overrides were updated, and SHALL trigger the Retry_Action asynchronously.
10. THE DDA_Labeling_System SHALL accept Retry_Requests for every auto-label family — `grounded-sam`, `sam`, `bedrock:`, and `llm:` — and for Skip_Verification_Mode jobs.

### Requirement 6: Retry state reset and re-enqueue

**User Story:** As a portal operator, I want the retry to reuse the existing fan-out message shape, consumer, and counters, so that retried images are indistinguishable from first-run images to every downstream component.

#### Acceptance Criteria

1. WHEN the Retry_Action runs, THE DDA_Labeling_System SHALL reset each of the job's Failed_Prelabel_Tasks with a conditional per-task update — setting `prelabel_status` to `Pending` and removing `prelabel_error` and `autolabel_error`, applied only while the task's `prelabel_status` is still `Failed` — and SHALL treat a task whose condition fails as not reset.
2. WHEN the Retry_Action has reset tasks, THE DDA_Labeling_System SHALL enqueue exactly one Fanout_Message per reset task on the auto-label queue, built from the job record and the task item's stored `image_s3_uri`, carrying `detection_prompt` for `llm:` jobs and `per_label_prompts` for Skip_Verification_Mode jobs — the byte-identical shape the distributor enqueues.
3. WHEN the Retry_Action resets tasks of a Skip_Verification_Mode job, THE DDA_Labeling_System SHALL increase `autolabel_pending` by the reset count, decrease `autolabel_completed_count` by the reset count, and set `review_ready` to false before enqueueing any message, so the consumer's existing decrement-to-zero machinery re-arms the review gate.
4. THE Retry_Action SHALL leave tasks whose `prelabel_status` is `Available`, `Pending`, or `None` unmodified.
5. WHEN two Retry_Actions for one job run concurrently, THE DDA_Labeling_System SHALL enqueue at most one message per Failed_Prelabel_Task across both, each task's message coming from exactly the action whose conditional reset succeeded.
6. IF a batch of retry messages partially fails to enqueue, THEN THE DDA_Labeling_System SHALL log each failed entry and continue with the remaining batches, mirroring the distributor's enqueue-failure behavior.
7. WHEN a retried task's Pre_Label resolves, THE Auto_Labeler SHALL process it through the existing unmodified consumer path — including the conditional resolution idempotency, the per-family storage semantics, and the Skip_Verification_Mode counter decrement — with the job record's then-current `prompt_overrides` applied for grounded-sam jobs.
8. IF the Retry_Action is invoked for a job that is not a Retry_Eligible_Job, THEN THE DDA_Labeling_System SHALL reset no task, enqueue no message, and record the invocation as skipped.

### Requirement 7: Re-run action on the job detail page

**User Story:** As a Job_Creator, I want a re-run button on the job page that shows me the failed count and lets me fix the prompts inline, so that recovering from the incident is one visit to one page.

#### Acceptance Criteria

1. WHILE the Job_Detail_Page renders a Retry_Eligible_Job, THE Job_Detail_Page SHALL present a "Re-run pre-labels" action displaying the Failed_Prelabel_Task count.
2. WHILE the rendered job is not a Retry_Eligible_Job, THE Job_Detail_Page SHALL present no re-run action.
3. WHEN a Job_Creator activates the re-run action on a grounded-sam job, THE Job_Detail_Page SHALL present a confirmation dialog containing one Prompt_Override entry per Label_Set label pre-filled from the job record's persisted `auto_label.prompt_overrides`, carrying the Requirement 3 Prompt_Guidance, and validated with the Requirement 1 rules before submission.
4. WHEN a Job_Creator activates the re-run action on a job of any other auto-label family, THE Job_Detail_Page SHALL present a confirmation dialog with the failed count and no override entries.
5. WHEN the confirmation dialog is submitted for a grounded-sam job, THE Job_Detail_Page SHALL send the Retry_Request carrying `prompt_overrides` assembled with the creation-time pruning rules (entries non-empty after trimming whose label belongs to the Label_Set, raw values), and SHALL omit the `prompt_overrides` body field only when the pruned override set equals the job record's persisted overrides.
6. WHEN the Retry_Request answers 202, THE Job_Detail_Page SHALL close the dialog and refresh the job detail.
7. IF the Retry_Request answers an error, THEN THE Job_Detail_Page SHALL display the response's error content in the dialog and leave the dialog open with the entered values retained.

### Requirement 8: Preservation of existing behavior

**User Story:** As a portal operator, I want the guardrails and retry to change nothing else, so that shipped jobs, other model families, and the worker keep behaving exactly as deployed.

#### Acceptance Criteria

1. THE DDA_Labeling_System SHALL leave the Grounded_SAM_Worker's code, container image, and deployment untouched — the alignment guard remains the worker-side backstop.
2. WHEN a labeling job of the `sam`, `bedrock:`, or `llm:` family is created, THE DDA_Labeling_System SHALL produce byte-identical validation outcomes, job records, and audit events to those produced before this feature.
3. WHEN the Auto_Labeler processes any auto-label message, THE Auto_Labeler SHALL execute the identical code path it executes today — this feature changes no consumer code.
4. THE DDA_Labeling_System SHALL leave the distributor's `distribute`, `notify_new_members`, and `generate_manifest` actions and the existing `/labeling` routes byte-identical, the Retry_Action and the rerun route being additive.
5. WHEN a grounded-sam labeling job whose Effective_Prompts satisfy the Prompt_Guardrail is created or processed, THE DDA_Labeling_System SHALL behave exactly as before this feature.
6. THE Portal SHALL leave the wizard's rendering and validation for non-grounded-sam selections, and the Job_Detail_Page's rendering for jobs without Failed_Prelabel_Tasks, exactly as before this feature.
7. WHERE the shipped grounded-sam-autolabel test suites pin behavior this spec changes, THE Portal and THE DDA_Labeling_System SHALL satisfy those suites amended only by the two generator changes named in the introduction, with every other pinned suite passing byte-identical.
