# Design Document — Grounded-SAM Prompt Guardrails and Pre-Label Retry

## Overview

Two features, one incident. Job `labeling-8022a9dc` shipped an instruction-style prompt override with inner periods to Grounding DINO; the worker's caption-alignment guard correctly failed all 72 images, and the user had no way to notice quickly, no way to fix the frozen `prompt_overrides`, and no way to re-run. Feature 1 moves the failure to the input boundary: the wizard and the create-job API reject any grounded-sam Effective_Prompt containing a period, and the override inputs teach what a groundable prompt looks like. Feature 2 adds recovery: a job-scoped rerun API (all four auto-label families) that can update a grounded-sam job's overrides in the same request, resets exactly the Failed tasks, and re-enqueues them through the existing fan-out machinery — plus failure-reason visibility on the job detail page.

The design's center of gravity is **reusing shipped machinery unchanged**: the consumer (`dda_autolabel_worker.py`) is untouched (it already reads `prompt_overrides` from the job record per message), the worker container is untouched (its guard remains the backstop), the fan-out message shape is reproduced byte-identically, and the retry's counter arithmetic re-arms the exact skip-verification machinery the consumer already drives.

### Research notes informing the design

- **The reject set is exactly the ASCII period, verified in `handler.py`.** `_marker_token_ids` builds the span-separator set from `tokenizer.token_to_id('.')` alone, plus `[CLS]`/`[SEP]`/`[PAD]` as specials, with `[UNK]` deliberately excluded "so an unknown token inside a phrase does not split its span". `?`, `!`, `;`, `,` and the CJK full stop `。` each tokenize to their own non-separator ids and merely extend the phrase span. The guard at `_detect` (~482-491) raises exactly when `len(spans) != len(phrases)` — the incident's error text verbatim. Rejecting characters other than `.` would be guesswork; the guardrail rejects `.` only.
- **`build_caption` strips only trailing dots** (`.rstrip('. ')` after whitespace collapse), so "scratch." would survive at the worker. The guardrail still rejects it: "no periods" is teachable, a trailing dot costs nothing to delete, and an all-dots override ("...") — which `build_caption` would reject as an empty phrase — falls under the same rule with no special case.
- **The label-name fallback reproduces the incident.** `_grounded_sam_prompts` (consumer) substitutes the label name when no override survives trimming, so a label like "v1.2 defect" with a blank override yields a period-bearing caption phrase. The guardrail therefore judges the Effective_Prompt (override-or-label-name), with an error message that names which source offends.
- **The retry needs no new permissions.** `compute-stack.ts` grants `ddaAutolabelQueue.grantSendMessages(ddaLabelingWorker)` and `ddaLabelingWorker.grantInvoke(ddaLabelingHandler)` today. Routing the retry as a new `dda_labeling_worker` action keeps every credential where it already is; the API handler never touches SQS.
- **Task items store `image_s3_uri`** (written by `_distribute`'s `batch.put_item`), so retry messages are built from the task items — no dataset re-enumeration, no drift between the original enumeration and the retry.
- **The consumer's success path never removes `prelabel_error`/`autolabel_error`** (`_mark_task` only SETs), and skip-verification review-eligibility keys off `autolabel_error`. The reset must REMOVE both attributes or a retried-then-successful task would stay review-ineligible forever.
- **`DdaLabelingApiStack` is a nested stack inside `EdgeCVPortalComputeStack`** (compute-stack.ts ~2883) whose API deployment logical id is salted with the route table — adding the rerun route re-points the stage automatically, and one compute-stack deploy ships all three backend files plus the route. `labeling.py` (job detail) is also a compute-stack function (~1168).
- **The job detail already computes `prelabel_failed_count`** from a full task query in `labeling.py`'s `_get_dda_labeling_job`, and `LabelingDetail.tsx` renders Available/Failed counts — but no failure reason and no action. The Failure_Reason_Summary aggregates over the same already-fetched task list at zero additional read cost.

## Architecture

```mermaid
sequenceDiagram
    participant U as Job_Detail_Page (LabelingDetail.tsx)
    participant A as DdaLabelingHandler (dda_labeling.py)
    participant J as LabelingJobs table
    participant W as DdaLabelingWorker (dda_labeling_worker.py)
    participant T as labeling-tasks table
    participant Q as dda-portal-autolabel-queue
    participant C as DdaAutolabelWorker (unchanged)

    U->>A: POST /labeling/{id}/rerun-prelabels {prompt_overrides?}
    A->>J: load job; authorize (manage_labeling_jobs; +admin for skip-verification)
    A->>A: eligibility gates (DDA, InProgress, autolabel on, not review_finalized, failed>0)
    A->>A: validate overrides = creation rules + Prompt_Guardrail on Effective_Prompts
    A->>J: persist surviving overrides (before any reset/enqueue)
    A->>W: async invoke {action:'retry_prelabels', job_id}
    A-->>U: 202 {job_id, retried_count} + prelabels_rerun audit
    W->>T: query Failed tasks; conditional reset each (Failed→Pending, REMOVE errors)
    W->>J: skip-verification only: pending+=n, completed-=n, review_ready=false
    W->>Q: one Fanout_Message per reset task (distributor's exact shape)
    Q->>C: existing consumer path (reads prompt_overrides from job record per message)
```

Feature 1 has no new architecture: it is two validation arms (one per runtime) restating one documented oracle, plus static guidance content on existing form fields.

**Retry ordering rationale.** Reset-then-enqueue inside one worker invocation keeps the state transition adjacent to the send: a validation failure or a failed async invoke leaves every task still `Failed` (re-clickable, nothing lost); a partial enqueue failure after reset leaves the affected tasks `Pending` and loudly logged — exactly the distributor's existing posture for lost fan-out messages, not a new failure mode. The alternative (API handler enqueues directly) was rejected: it would need queue permissions the handler doesn't have, and enqueue-before-reset would let the consumer resolve a still-`Failed` task's message into a skipped conditional update, silently consuming the retry.

**Concurrency.** The per-task conditional reset (`prelabel_status = Failed` as the update condition) partitions concurrent retries: whichever invocation flips a task enqueues it; the loser's condition fails and it excludes that task. The consumer's `_mark_task` condition (resolve only while unresolved) keeps stale in-flight messages from double-resolving, and the skip-verification counter moves only with a performed resolution — so counters move exactly once per generation regardless of interleaving.

## Components and Interfaces

### 1. New frontend module — `edge-cv-portal/frontend/src/pages/promptOverrideGuardrails.tsx`

The frontend's single source of truth for the guardrail and the guidance, imported by both `CreateLabelingJob.tsx` and `LabelingDetail.tsx` (the cross-page-import precedent: tests already import `MAX_PROMPT_OVERRIDE_LENGTH` from the wizard).

- `ALIGNMENT_BREAKING_PATTERN = /\./` — the reject set, with a comment citing `_marker_token_ids`.
- `effectivePrompt(label: string, override: string | undefined): string` — trim-fallback, mirroring `_grounded_sam_prompts`.
- `findPromptGuardrailViolation(labels: string[], overrides: Record<string, string>): {label: string, source: 'override' | 'label'} | null` — first offender in Label_Set order (the wizard's `.find()` error style).
- `promptGuardrailMessage(violation): string` — the two message variants: override source → `The text prompt for label "X" contains a period. Periods separate labels in the detection caption — remove them or split the idea into a short noun phrase`; label source → `Label "X" contains a period and has no text prompt. Grounded-SAM uses the label name as its text prompt — enter a text prompt without periods for this label`.
- `PROMPT_GUIDANCE_CONSTRAINT` — constraint-text string: `Optional, at most 256 characters. No periods — they separate labels in the caption`.
- `PromptGuidanceContent` — the info-slot JSX (a `Box` per the page's Label Categories `info={<Box>…}` precedent) teaching: a prompt is a short noun phrase naming the visual thing to find (examples: "gap between broken cookie pieces", "scratch on metal surface"); the detector localizes what the text names and the mask model turns the boxes into masks; instruction-style text ("draw a polygon around each gap", "produce json") does not work — the model grounds noun phrases, it does not follow directions; no periods (they separate labels); commas are fine; leave the entry empty to use the label name.

### 2. Wizard — `edge-cv-portal/frontend/src/pages/CreateLabelingJob.tsx`

- `validateDdaSetup`'s grounded-sam arm: after the existing over-length check, `findPromptGuardrailViolation(effectiveLabelSet, groundedSamPromptOverrides)` → return `promptGuardrailMessage(...)` on a hit. Length first (existing order preserved), guardrail second.
- The override `FormField`s: `constraintText` becomes `PROMPT_GUIDANCE_CONSTRAINT`; `info={<PromptGuidanceContent />}` added; `errorText` gains the period variant (field-level mirror of the step error, the existing over-length errorText pattern) — over-length keeps precedence so its pinned message is untouched.
- Nothing else: submit assembly, draft wiring, picker composition, and every non-grounded-sam path are byte-identical.

### 3. Job detail — `edge-cv-portal/frontend/src/pages/LabelingDetail.tsx`

- **Failure visibility**: when `prelabel_failed_count >= 1`, a warning `Alert` ("Pre-labeling failures") above the Auto-Labeling container stating the count and listing `prelabel_failure_reasons` entries (`reason (n images)`), the page's notification-failures list pattern.
- **Re-run action**: `canRerunPrelabels(job)` exported beside `canStopDdaJob` — DDA + `InProgress` + (`auto_label.enabled` or `skip_verification`) + no truthy `review_finalized` + `prelabel_failed_count >= 1`. Button `Re-run pre-labels (N failed)` in the Auto-Labeling container header.
- **Dialog** (Cloudscape `Modal`, the stop-modal precedent): failed count; for grounded-sam jobs one `Input` per `label_set` label pre-filled from `rawJob.auto_label.prompt_overrides`, with `PROMPT_GUIDANCE_CONSTRAINT`/`PromptGuidanceContent`/guardrail validation from the shared module; other families get a plain confirmation. Submit assembles `prompt_overrides` with the creation pruning rules (non-blank-after-trim entries keyed by Label_Set labels, raw values) and omits the body field only when the pruned map equals the record's persisted map (no-op edit ⇒ pure retry). 202 → close + refetch; error → response error shown inline, dialog open, values retained.

### 4. API client — `edge-cv-portal/frontend/src/services/api.ts`

- `rerunPrelabels(jobId, body?: { prompt_overrides?: Record<string, string> }): Promise<{job_id, retried_count, message?}>` — POST `/labeling/${encodeURIComponent(jobId)}/rerun-prelabels` (the `stopLabelingJob` pattern).
- Job-detail response type gains optional `prelabel_failure_reasons?: { reason: string; count: number }[]` and `review_finalized?: boolean`.

### 5. API handler — `edge-cv-portal/backend/functions/dda_labeling.py`

- **Creation guardrail** (grounded-sam validation arm, ~1640-1681): after the existing per-entry checks, judge each label's Effective_Prompt (surviving override else label name) against `'.'`; one `_validation_error` per offending label — override source: `The text prompt for label '<label>' contains a period; periods separate labels in the detection caption`; label source: `Label '<label>' contains a period and has no text prompt; the label name is used as the text prompt — provide a prompt override without periods`. Runs only in the grounded-sam arm (other families untouched). Module constant `ALIGNMENT_BREAKING_CHARACTER = '.'` with the `_marker_token_ids` citation.
- **Rerun route**: router gains `POST /labeling/{id}/rerun-prelabels` in the existing `/labeling/{id}` block (after `_inject_job_usecase_scope`), dispatching to `rerun_prelabels(event, context)` decorated `@rbac_check([Permission.MANAGE_LABELING_JOBS], allow_global=True)`:
  1. Load job → 404 when absent; 400 unless `labeling_backend == 'DDA'`.
  2. Skip-verification jobs: `_require_review_admin`-equivalent role gate (403 + `unauthorized_access` audit on denial).
  3. Eligibility: status `InProgress`; auto-labeling enabled (`auto_label.enabled` or `skip_verification`); `review_finalized` falsy; live count of `prelabel_status == 'Failed'` tasks ≥ 1 — each miss a distinct 400 naming the condition.
  4. Body: `prompt_overrides` present on a non-grounded-sam job → 400. On a grounded-sam job → creation rules verbatim (dict; keys ⊆ `job['label_set']`; string values; raw length ≤ `PROMPT_OVERRIDE_MAX_LENGTH`; blank-after-trim dropped) then the guardrail over the resulting Effective_Prompts (submitted survivors else persisted survivors else label name). Body absent on a grounded-sam job → guardrail over the persisted Effective_Prompts (the incident job fails here with the corrective message until fixed). Errors enumerated per label; nothing mutated on rejection.
  5. Persist accepted overrides to `auto_label.prompt_overrides` (key removed when none survives) with `updated_at`.
  6. `_invoke_labeling_worker({'action': 'retry_prelabels', 'job_id': job_id})` (existing fire-and-forget helper), `prelabels_rerun` audit event (`log_audit_event`: usecase_id, retried count, `overrides_updated` bool), respond `202 {job_id, retried_count}`.

### 6. Worker — `edge-cv-portal/backend/functions/dda_labeling_worker.py`

- Action dispatcher gains `retry_prelabels` → `retry_prelabels_job(job_id)`:
  1. Load job; re-check eligibility (the distribute guard pattern — a stop/finalize raced the async invoke) → `{'skipped': True}` on a miss, nothing written.
  2. Query the job's task items with `prelabel_status == 'Failed'` (paginated query, `_job_task_ids` pattern, projecting `task_id`/`image_s3_uri`).
  3. Per task, conditional update: `SET prelabel_status = :pending, updated_at = :now REMOVE prelabel_error, autolabel_error` with `ConditionExpression: prelabel_status = :failed`; a `ConditionalCheckFailedException` marks the task not-reset (concurrent retry or in-flight resolution won).
  4. Skip-verification jobs with `reset_count > 0`: one job update `ADD autolabel_pending :n, autolabel_completed_count :minus_n SET review_ready = :false, updated_at = :now` — before enqueueing, so the consumer's decrement-to-zero re-arms.
  5. Enqueue one message per reset task via a shared helper refactored from `_enqueue_autolabel_messages`' body (same model resolution incl. the skip-verification `bedrock:{bedrock_model_id}` fallback and llm precedence, same `detection_prompt`/`per_label_prompts` riders, `SQS_BATCH_SIZE` batching, same log-loudly-on-partial-failure) — `image_s3_uri` taken from the task item instead of re-enumeration. The refactor keeps `distribute`'s observable behavior byte-identical (same messages, same logs).
  6. Return `{'job_id', 'action': 'retry_prelabels', 'reset_count', 'enqueued_count'}`.

### 7. Job detail API — `edge-cv-portal/backend/functions/labeling.py`

- `_get_dda_labeling_job` aggregates over the already-queried `active_tasks`: `prelabel_failure_reasons = [{reason, count}]` — distinct `prelabel_error` values of Failed tasks (missing error → `'unknown'`), descending count (ties by first occurrence), capped at 5 — included only when at least one Failed task exists; also passes `review_finalized` through untouched (it is on the job item already). Zero extra table reads; zero-failed responses byte-identical.

### 8. Infrastructure — `edge-cv-portal/infrastructure/lib/dda-labeling-api-stack.ts`

- `labelingJobResource.addResource('rerun-prelabels', { defaultCorsPreflightOptions: corsOptions })` + `addMethod(..., 'POST')` beside the review routes (default integration: `DdaLabelingHandler`). The deployment's route-table salt re-points the stage. **No compute-stack.ts change**: no new env vars, no new grants, no worker flag — the worker image is untouched.

### 9. Explicitly unchanged components

`dda_autolabel_worker.py` (consumer), `grounded-sam-worker/` (all files — the alignment guard is the designed backstop), `compute-stack.ts`, the wizard's draft module (`labelingJobDraft.ts` — overrides were already draftable), the distributor's `distribute`/`notify_new_members`/`generate_manifest` actions, all existing routes, and every non-grounded-sam validation path.

## Data Models

**Retry request/response** (`POST /labeling/{id}/rerun-prelabels`):

```json
// request (body optional; prompt_overrides grounded-sam only)
{ "prompt_overrides": { "cookie_gap": "gap between broken cookie pieces" } }
// 202 response
{ "job_id": "labeling-8022a9dc", "retried_count": 72,
  "message": "Re-run started for 72 failed pre-label task(s)" }
// 400 (guardrail, per-label enumeration — creation's validation_errors shape)
{ "error": "Validation failed", "validation_errors": [
  { "parameter": "auto_label",
    "message": "The text prompt for label 'cookie_gap' contains a period; periods separate labels in the detection caption",
    "label": "cookie_gap" } ] }
```

**Job record delta** (retry with accepted overrides): `auto_label.prompt_overrides` replaced by the surviving map (key removed when empty), `updated_at` bumped. Skip-verification re-arm: `autolabel_pending += n`, `autolabel_completed_count -= n`, `review_ready = false`.

**Task item transition** (reset): `prelabel_status: 'Failed' → 'Pending'`, `prelabel_error`/`autolabel_error` removed, `updated_at` bumped; `prelabel_s3_key` never present on a Failed task (set only on Available). All other attributes untouched.

**Fanout_Message** (retry — byte-identical to the distributor's): `{job_id, task_id, image_s3_uri, modality: job.task_type, label_set, model}` + `detection_prompt` (llm:) + `per_label_prompts` (skip-verification), model resolved with the distributor's llm-over-skip-verification precedence.

**Job detail additions**: `prelabel_failure_reasons?: [{reason: string, count: number}]` (≤ 5, descending count, only when failed ≥ 1); `review_finalized` passed through.

**Shared guardrail oracle** (restated in both runtimes, tested with aligned generator domains): for labels `L` and override map `O`, submission-visible overrides survive iff non-blank after trim; `effective(l) = O[l]` if surviving else `l`; **valid iff ∀ l ∈ L: '.' ∉ effective(l)**; first/each offender reported with its source (`override` vs `label`).

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

Each property gets exactly one property-based test at a minimum of 100 iterations (Hypothesis `@settings(max_examples=100, deadline=None)` backend, fast-check `{ numRuns: 100 }` frontend), tagged `Feature: grounded-sam-prompt-guardrails-and-prelabel-retry, Property {n}: {title}`. The prework was consolidated: the wizard's four guardrail criteria (1.1, 1.2, 1.4, 1.5) collapse into Property 1 and the backend's six (2.1-2.6, 8.5) into Property 2 — each side one acceptance-iff-oracle property, agreement (2.5) coming from both restating the Data Models oracle over aligned generator domains. The reset's transition and untouched-tasks invariants (6.1, 6.4) are one property; visibility's two directions (7.1, 7.2) are one. Route/worker eligibility (5.4, 6.8) stay one property with two arms because they guard the same predicate at two entry points. Guidance content (3.x), dialog composition (7.3, 7.4), interaction outcomes (7.6, 7.7), authorization branches (5.2, 5.3), the accepted-response shape (5.9), partial-enqueue logging (6.6), and the end-to-end retry-then-consume wiring (6.7) are examples per the prework; repo-inventory criteria (8.1, 8.3, 8.4, 8.6, 8.7) are checkpoint smoke checks.

| # | Property (title) | Validates | Test file |
|---|---|---|---|
| 1 | Wizard accepts iff the guardrail holds | 1.1, 1.2, 1.4, 1.5 | `frontend/src/pages/CreateLabelingJob.guardrails.property.test.tsx` |
| 2 | Creation accepts iff the guardrail holds, enumerating offenders, records pre-feature-identical on acceptance | 2.1-2.6, 8.2, 8.5 | `backend/tests/test_property_gsam_prompt_guardrails.py` |
| 3 | Failure_Reason_Summary is the capped descending distinct-count aggregation, present iff failures exist | 4.1, 4.4 | `backend/tests/test_property_prelabel_failure_summary.py` |
| 4 | Retry is accepted iff the job is Retry_Eligible, and rejection mutates nothing | 5.4, 6.8 | `backend/tests/test_property_prelabel_retry_route.py` |
| 5 | Retry override validation equals creation's rules and persists before triggering; rejection leaves record, tasks, and queue untouched | 5.5, 5.6, 5.7, 5.8 | `backend/tests/test_property_prelabel_retry_route.py` |
| 6 | The reset flips exactly the Failed tasks to Pending with errors removed; every other task byte-identical | 6.1, 6.4 | `backend/tests/test_property_prelabel_retry_worker.py` |
| 7 | The enqueued set equals the reset set, each message the distributor's exact shape for its family | 5.10, 6.2 | `backend/tests/test_property_prelabel_retry_worker.py` |
| 8 | Skip-verification counters re-arm by exactly the reset count with review_ready cleared; other jobs' counters untouched | 6.3 | `backend/tests/test_property_prelabel_retry_worker.py` |
| 9 | Repeating the retry enqueues at most one message per originally-Failed task | 6.5 | `backend/tests/test_property_prelabel_retry_worker.py` |
| 10 | The re-run action renders iff the record satisfies the eligibility predicate | 7.1, 7.2 | `frontend/src/pages/LabelingDetail.rerun.property.test.tsx` |
| 11 | The dialog's request carries exactly the pruned overrides, omitted iff equal to the persisted map | 7.5 | `frontend/src/pages/LabelingDetail.rerun.property.test.tsx` |

### Property 1: Wizard accepts iff the guardrail holds

*For any* Label_Set rows (labels with and without periods), *any* Prompt_Override entry state (values mixing empty, whitespace-only, period-bearing, comma/question/exclamation/semicolon-bearing, unicode), and *any* auto-label model selection, advancing the wizard's setup step SHALL succeed exactly when the model is not `grounded-sam` or every label's Effective_Prompt (per the Data Models oracle) contains no period — a violation blocking the step with the error naming the first offending label and its source (override vs label name).

**Validates: Requirements 1.1, 1.2, 1.4, 1.5**

### Property 2: Creation accepts iff the guardrail holds, enumerating offenders, records pre-feature-identical on acceptance

*For any* labeling job submission (family drawn from grounded-sam/sam/bedrock:/llm:; labels and override values with and without periods), creation SHALL be rejected exactly when the family is `grounded-sam` and some Effective_Prompt contains a period — the rejection enumerating one validation error per offending label naming it and its source, persisting nothing — and SHALL otherwise accept with a job record equal to the pre-feature creation rules' record for the same submission.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 8.2, 8.5**

### Property 3: Failure_Reason_Summary is the capped descending distinct-count aggregation, present iff failures exist

*For any* task population (statuses mixing Available/Pending/Failed/None/Inactive; arbitrary `prelabel_error` strings including duplicates and absent values), the job detail SHALL carry `prelabel_failure_reasons` exactly when at least one active Failed task exists, equal to the distinct error values of active Failed tasks (absent errors as `'unknown'`) with their counts, in descending count order, capped at 5 — and the summed counts of an uncapped population SHALL equal `prelabel_failed_count`.

**Validates: Requirements 4.1, 4.4**

### Property 4: Retry is accepted iff the job is Retry_Eligible, and rejection mutates nothing

*For any* job record state (backend, status, `review_finalized`, `auto_label.enabled`, `skip_verification`, Failed-task count) and entry point (the route with an authorized caller, or the worker action directly), the retry SHALL proceed exactly when the job is a Retry_Eligible_Job; every rejection or skip SHALL name the unmet condition (404 for a missing job) and SHALL leave the job record, every task item, the queue, and the audit trail's non-denial entries unchanged.

**Validates: Requirements 5.4, 6.8**

### Property 5: Retry override validation equals creation's rules and persists before triggering; rejection leaves record, tasks, and queue untouched

*For any* grounded-sam Retry_Request body (`prompt_overrides` absent, or maps mixing valid, blank, period-bearing, over-length, unknown-key, and non-string values) and *any* persisted override state (including period-bearing persisted overrides and period-bearing labels), the request SHALL be accepted exactly when creation's override rules and the guardrail hold over the resulting Effective_Prompts (submitted survivors, else persisted survivors, else label names); acceptance SHALL persist the surviving submitted overrides character-for-character (key removed when none survives) before the worker invocation; rejection SHALL enumerate the offenses and change nothing; and *for any* non-grounded-sam job a body carrying `prompt_overrides` SHALL be rejected.

**Validates: Requirements 5.5, 5.6, 5.7, 5.8**

### Property 6: The reset flips exactly the Failed tasks to Pending with errors removed; every other task byte-identical

*For any* task population with arbitrary `prelabel_status` values and error attributes, running the Retry_Action SHALL set exactly the Failed tasks' `prelabel_status` to `Pending` with `prelabel_error` and `autolabel_error` removed, and SHALL leave every Available, Pending, None, and Inactive task item attribute-for-attribute unchanged.

**Validates: Requirements 6.1, 6.4**

### Property 7: The enqueued set equals the reset set, each message the distributor's exact shape for its family

*For any* job configuration (family drawn from grounded-sam/sam/bedrock:/llm:, with and without Skip_Verification_Mode) and *any* task population, the Retry_Action SHALL enqueue exactly one message per reset task, each message equal to the Fanout_Message the distributor would build for that task — same key set, `image_s3_uri` from the task item, the llm-over-skip-verification model precedence, `detection_prompt` for `llm:` and `per_label_prompts` for skip-verification jobs.

**Validates: Requirements 5.10, 6.2**

### Property 8: Skip-verification counters re-arm by exactly the reset count with review_ready cleared; other jobs' counters untouched

*For any* Skip_Verification_Mode job with counters in *any* consistent state and *any* Failed-task count n ≥ 0, the Retry_Action SHALL increase `autolabel_pending` by exactly the reset count, decrease `autolabel_completed_count` by exactly the reset count, and set `review_ready` false when the reset count is positive; and *for any* non-skip-verification job the Retry_Action SHALL write none of those attributes.

**Validates: Requirements 6.3**

### Property 9: Repeating the retry enqueues at most one message per originally-Failed task

*For any* task population, running the Retry_Action twice in succession (the conditional-reset model of two concurrent retries) SHALL enqueue exactly one message total per originally-Failed task, the second run resetting zero tasks and moving no counters.

**Validates: Requirements 6.5**

### Property 10: The re-run action renders iff the record satisfies the eligibility predicate

*For any* job-detail record shape (backend, status, `review_finalized`, `auto_label.enabled`, `skip_verification`, `prelabel_failed_count` drawn across present/absent/zero/positive), the Job_Detail_Page SHALL render the "Re-run pre-labels" action (with the failed count) exactly when the record is a Retry_Eligible_Job.

**Validates: Requirements 7.1, 7.2**

### Property 11: The dialog's request carries exactly the pruned overrides, omitted iff equal to the persisted map

*For any* persisted `prompt_overrides` map and *any* dialog entry edits (values mixing kept, cleared, whitespace-padded, and new period-free text), submitting the re-run dialog SHALL send `prompt_overrides` equal to exactly the entries non-empty after trimming whose label belongs to the Label_Set, raw values character-for-character — the body field omitted exactly when that pruned map equals the persisted map.

**Validates: Requirements 7.5**

## Error Handling

| Condition | Behavior | Requirement |
|---|---|---|
| Override contains `.` (wizard) | Step blocked; error names the label, says periods separate labels; field-level errorText on the entry | 1.1, 1.3 |
| Label name contains `.`, override blank (wizard, grounded-sam) | Step blocked; error names the label, says the label name is the prompt, directs to an override | 1.2 |
| Override/label-fallback contains `.` (create API) | 400, one validation error per offending label with source-specific message; nothing persisted | 2.1-2.3 |
| `?` `!` `;` `,` in prompts | Accepted — not in the worker's separator set (verified) | 1.4, 2.4 |
| Rerun on missing job | 404 | 5.4 |
| Rerun on non-DDA / stopped / completed / review-finalized / autolabel-off / zero-failed job | 400 naming the unmet condition; nothing reset or enqueued | 5.4 |
| Rerun caller lacks manage_labeling_jobs | 403 (rbac middleware) | 5.2 |
| Rerun on skip-verification job by non-admin | 403 + `unauthorized_access` audit event (the Admin_Review gate) | 5.3 |
| Rerun `prompt_overrides` on non-grounded-sam job | 400: overrides apply only to grounded-sam jobs | 5.7 |
| Rerun overrides invalid (shape/key/length/guardrail) | 400 enumerating offenses; record, tasks, queue untouched | 5.5, 5.8 |
| Rerun omitted overrides but persisted Effective_Prompts violate guardrail | 400 with the corrective per-label errors — the incident job's retry is refused until the prompt is fixed | 5.6 |
| Async worker invoke fails after 202 | Logged by `_invoke_labeling_worker`; tasks remain Failed; the action stays available (re-click) | 6 (design) |
| Stop/finalize races the async retry invoke | Worker re-checks eligibility, records skipped, writes nothing (the distribute guard pattern) | 6.8 |
| Concurrent retry clicks | Conditional resets partition the tasks; at most one message per task; counters move once | 6.5 |
| Stale in-flight message resolves a reset task | Consumer's conditional `_mark_task` lets exactly one resolution win; no double decrement | 6.7 |
| Partial SQS batch failure during retry enqueue | Each failed entry logged loudly; remaining batches proceed (distributor precedent); affected tasks stay Pending pending a redrive-or-support path — the platform's existing posture | 6.6 |
| Retried-then-successful task previously review-ineligible | Reset REMOVEs `autolabel_error`, restoring eligibility | 6.1 |

## Testing Strategy

Backend tests live in `edge-cv-portal/backend/tests/` (pytest + Hypothesis; moto-backed stack from `conftest.py`; fake Lambda/SQS clients per the `test_dda_autolabel_worker.py` precedent; **targeted runs only** — the full session has known pollution). Frontend: vitest + testing-library + fast-check in `edge-cv-portal/frontend/src/` (tsc must also pass), `localStorage.clear()` per the established precedent. Each correctness property gets exactly one property-based test at ≥ 100 iterations tagged `Feature: grounded-sam-prompt-guardrails-and-prelabel-retry, Property {n}: {title}`. The property→file mapping is the table in Correctness Properties.

### Example / unit / smoke tests (new)

- `backend/tests/test_dda_labeling_create_job.py` (existing, **extended with new tests only**): period override rejected naming the label; period label with blank override rejected with the label-source message; comma/question/exclamation values accepted; trailing-dot override rejected (stricter-than-worker stance pinned).
- `backend/tests/test_dda_prelabel_retry_route.py` (new, examples beside the route properties): route dispatch smoke (5.1); 403 without the permission (5.2); skip-verification non-admin 403 + audit (5.3); accepted request → 202 shape, `prelabels_rerun` audit row, captured `{action: 'retry_prelabels', job_id}` invoke (5.9); the incident replay — persisted period-bearing override, retry without body → 400 corrective, retry with corrected noun phrase → 202 and record updated (5.5, 5.6).
- `backend/tests/test_dda_prelabel_retry_worker.py` (new, examples beside the worker properties): partial `send_message_batch` failure logged and continued (6.6); retry-then-consume wiring — after a retry persisting new overrides, processing the re-enqueued message invokes the grounded-sam worker with the updated prompts (6.7); stop-raced action records skipped (6.8 example).
- `frontend/src/pages/CreateLabelingJob.guardrails.test.tsx` (new): field-level error on the offending entry (1.3); guidance constraint text and info affordance present under grounded-sam and absent for sam/llm (3.1); guidance content strings — noun phrase, the two examples, instructions-don't-work, no-periods/commas/empty-default (3.2-3.4).
- `frontend/src/pages/LabelingDetail.rerun.test.tsx` (new): warning alert with count and reasons when failed ≥ 1, absent at zero (4.2, 4.3); grounded-sam dialog pre-fill + shared guidance + period rejection in-dialog (3.5, 7.3); other-family dialog without entries (7.4); 202 closes and refetches (7.6); error keeps dialog open with values and shows the response error (7.7).

### Non-regression inventory (existing tests that pin this area)

**Permitted rebaseline class — exactly two generator amendments** (declared in requirements Req 8.7). The shipped grounded-sam-autolabel suites generated override/label text over alphabets containing `.` and asserted such submissions succeed; the new validation makes those inputs reject, so the *generators* (not the oracles) are amended to the new valid domain:

> 1. `frontend/src/pages/CreateLabelingJob.groundedsam.property.test.tsx` — `labelArb` and `overrideValueArb` gain a period exclusion (filter `!v.includes('.')`), shrinking Property 1/2's scenario space to the post-guardrail valid domain. No assertion changes.
> 2. `backend/tests/test_property_grounded_sam_job_creation.py` — the valid-space strategies used by its Property 3 and the base (non-offending) portions of Property 4 move to period-free label/value variants (`_gsam_safe_label_names`, `_gsam_safe_values`); Property 15's other-family generators keep the full alphabet (no guardrail applies there). No assertion changes.
>
> Any *other* required change in any pinned file is a design violation to stop on, not a rebaseline.

| Existing test | Expected disposition | Why |
|---|---|---|
| `frontend/.../CreateLabelingJob.groundedsam.property.test.tsx` | **generator amendment (permitted class 1)** | generated `.` in labels/overrides and asserted successful submission |
| `backend/tests/test_property_grounded_sam_job_creation.py` | **generator amendment (permitted class 2)** | `_TEXT_ALPHABET` (32-0x2FFF) contains `.`; P3/P4 valid spaces shrink; P15 untouched |
| `frontend/.../CreateLabelingJob.groundedsam.test.tsx` | green, **byte-identical** | fixtures (`'x'.repeat`, `'y'.repeat`, "a shallow scratch mark, hairline") are period-free; no constraint-text assertion exists (verified) |
| `frontend/.../CreateLabelingJob.modelpicker.property.test.tsx` / `.modelpicker.test.tsx` | green, byte-identical | picker composition untouched |
| `frontend/.../labelingJobDraft.*.test.ts`, `CreateLabelingJob.recovery.*` | green, byte-identical | draft schema untouched; no scenario submits period-bearing grounded-sam prompts |
| `frontend/.../CreateLabelingJob.test.tsx`, `.fewshot.`, `.sizing.`, `PromptTuningPreview.property.` | green, byte-identical | non-grounded-sam paths untouched (Req 8.6) |
| `frontend/.../LabelingDetail.test.tsx` | green, byte-identical | fixtures have zero failed counts or assert count rendering the feature preserves; new alert renders only at failed ≥ 1 with reasons present |
| `backend/tests/test_dda_labeling_create_job.py` | extended (new tests only); every pre-existing assertion untouched | existing grounded-sam fixtures are period-free; message pins unaffected |
| `backend/tests/test_property_grounded_sam_prompt_map.py`, `test_property_grounded_sam_consumer.py`, `test_dda_grounded_sam_consumer.py`, `test_dda_grounded_sam_worker_utils.py` | green, byte-identical | consumer and worker untouched; period-bearing *persisted* records (pre-guardrail) must keep processing — totality stays pinned |
| `backend/tests/test_dda_autolabel_worker.py`, `test_dda_autolabel_worker_few_shot.py`, `test_property_llm_autolabel_invariance.py` | green, byte-identical | consumer untouched |
| `backend/tests/test_dda_labeling_worker_distribute.py` | green, byte-identical | `distribute`'s observable behavior preserved through the enqueue-helper refactor (messages and logs identical) |
| `backend/tests/test_labeling_stop_route.py`, `test_dda_labeling_admin_review.py` | green, byte-identical | existing routes untouched; rerun route additive |
| `infrastructure/test/*` | green, byte-identical | nested-stack route addition; no test pins the DDA API route table (verified: no dda-labeling-api test file exists) |

### Verification commands

- Backend (targeted): `cd edge-cv-portal/backend && python3 -m pytest tests/test_property_gsam_prompt_guardrails.py tests/test_property_prelabel_failure_summary.py tests/test_property_prelabel_retry_route.py tests/test_property_prelabel_retry_worker.py tests/test_dda_prelabel_retry_route.py tests/test_dda_prelabel_retry_worker.py tests/test_dda_labeling_create_job.py tests/test_property_grounded_sam_job_creation.py tests/test_property_grounded_sam_prompt_map.py tests/test_property_grounded_sam_consumer.py tests/test_dda_grounded_sam_consumer.py tests/test_dda_autolabel_worker.py tests/test_dda_labeling_worker_distribute.py tests/test_labeling_stop_route.py tests/test_dda_labeling_admin_review.py -q`
- Frontend: `cd edge-cv-portal/frontend && npx tsc --noEmit -p tsconfig.json && npx vitest run`
- Infrastructure: `cd edge-cv-portal/infrastructure && npx jest`
- Deploy: one routine compute-stack deploy (`EdgeCVPortalComputeStack` — ships all three backend files and the nested-stack route; **no worker flag, no Docker build**) plus the frontend bundle, under the builds.md pgrep gates with spec-named logs; then the live incident replay on `labeling-8022a9dc`.
