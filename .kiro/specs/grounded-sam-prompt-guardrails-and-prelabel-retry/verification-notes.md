# Live Verification Notes — Task 8.2: Incident Replay on `labeling-8022a9dc`

Date: 2026-09-06 (all times UTC). Account `164152369890`, region `us-east-1`.
Executed after the task 8.1 deploy (compute stack + frontend, all Lambdas last-modified 21:32Z, bundle `index-B1kd0eNX.js` shipped 21:38:52Z).

Browser UI automation was unavailable, so deployed behavior was verified by invoking the
**real deployed Lambdas** with synthesized API-Gateway-shaped events using the handlers' own
auth path: `requestContext.authorizer.claims` carrying the job creator's real user id
(`sub = a4b804e8-5061-7004-12f2-38a0149dcd4c`, `custom:role = UseCaseAdmin`); rbac resolved
scope from the real tables (job's `usecase_id = 645504ce-a60a-4009-8349-7548c0025cd3`).

Physical function names (via `aws cloudformation describe-stack-resources --stack-name EdgeCVPortalComputeStack` / `aws lambda list-functions`):

| Logical | Physical |
|---|---|
| LabelingHandler | `EdgeCVPortalComputeStack-LabelingHandler2286A030-5u22VvCdqprm` |
| DdaLabelingHandler | `EdgeCVPortalComputeStack-DdaLabelingHandler95BC6FD-YAEO6xw5PLvd` |
| DdaLabelingWorker | `EdgeCVPortalComputeStack-DdaLabelingWorkerD5E96C2A-foX8XixXUQp2` |
| DdaGroundedSamWorker | `EdgeCVPortalComputeStack-DdaGroundedSamWorkerA3B13-qHx8huoBWZFA` |
| DdaAutolabelWorker | `EdgeCVPortalComputeStack-DdaAutolabelWorkerC8FF5C7-ygTfXVLSFv01` |

## Incident job baseline (verified earlier + step 1)

`labeling-8022a9dc`: DDA backend, grounded-sam, `task_type = Segmentation`,
`label_set = ['cookie_gap']`, 72 tasks, `skip_verification = False`, status `InProgress`,
dataset `s3://ryvan-cookies`. Persisted broken override (instruction-style, inner periods):

> `draw and fill in the gaps between broken cookie pieces with a polygon, at least 3 verticies, all withing the bounds of the image. If there is a large crack fill in the gaps with the polygon. `

## Step 1 — Job detail BEFORE (Req 4.1) — 21:47:51Z — PASS

`GET /labeling/labeling-8022a9dc` (LabelingHandler) → **200**:

- `prelabel_failed_count = 72`, `prelabel_available_count = 0`
- `prelabel_failure_reasons` **present**: 5 entries (the Req 4.1 cap), **every entry** carrying the
  caption-alignment reason:
  `Grounded-SAM worker failed: {"errorMessage": "caption token spans (2) do not align with the 1 prompts; a prompt likely contains inner sentence punctuation", "errorType": "ValueError", "requestId": "…", "stackTrace": […]}`
- **Observation (not a defect):** each entry has `count = 1` rather than one entry with count 72,
  because the worker embeds a unique `requestId`/`stackTrace` in every `prelabel_error` string and
  Req 4.1 aggregates **distinct values**. The alert still surfaces the caption-alignment reason
  prominently (5 capped entries, all the same errorMessage).

## Step 2 — REFUSED retry, bodyless (Req 5.6) — 21:49:09Z — PASS

`POST /labeling/labeling-8022a9dc/rerun-prelabels`, resource `/labeling/{id}/rerun-prelabels`, **no body** (DdaLabelingHandler) → **400**:

```json
{"error": "Validation failed", "validation_errors": [{"parameter": "auto_label",
  "message": "The text prompt for label 'cookie_gap' contains a period; periods separate labels in the detection caption",
  "label": "cookie_gap"}]}
```

Nothing mutated (Req 5.8), checked immediately after:

- full task query: **all 72 tasks still `prelabel_status = Failed`** (not a spot check)
- job record `auto_label.prompt_overrides` unchanged (broken prompt intact), `updated_at = 1788719397` not bumped

## Step 3 — CORRECTED retry (Reqs 5.5, 6.2) — 21:50:22Z — PASS

Same POST with body `{"prompt_overrides": {"cookie_gap": "gap between broken cookie pieces"}}` → **202**:

```json
{"job_id": "labeling-8022a9dc", "retried_count": 72, "message": "Re-run started for 72 failed pre-label task(s)"}
```

Side effects, each verified against the live tables:

- **Override persisted** (`dynamodb get-item`): `auto_label.prompt_overrides = {"cookie_gap": "gap between broken cookie pieces"}`, `updated_at` bumped `1788719397 → 1788731423`
- **Tasks reset**: +26s query showed `{Pending: 66, Available: 6}` — all 72 left `Failed`; `prelabel_error`/`autolabel_error` removed from **0 of 72 remaining** (both attributes gone on every task)
- **Audit row** (`dda-portal-audit-log` scan by `resource_id`): `action = prelabels_rerun`, `result = success`, `user_id = a4b804e8-5061-7004-12f2-38a0149dcd4c`, `timestamp = 1788731423194`, details `{retried_count: 72, usecase_id: 645504ce-a60a-4009-8349-7548c0025cd3, overrides_updated: true}`

## Step 4 — Pre-labels appear (Req 6.7) — PASS

Task-status progression (dynamodb query, counting `prelabel_status`):

| Time (UTC) | Δ from 202 | Distribution |
|---|---|---|
| 21:50:22 | 0s | 202 accepted (72 Failed at that instant) |
| 21:50:26 | +4s | first `Grounded-SAM inference` worker log line |
| 21:50:48 | +26s | `{Pending: 66, Available: 6}` |
| 21:51:51 | +89s | `{Available: 72}` — **all resolved, zero Failed** |

Far faster than the 5-140s/image budget — warm containers and high SQS-driven concurrency.
**Zero residual Failed tasks. Zero worker errors** in the window (log filter
`?ERROR ?ValueError ?"caption token spans"` over `/aws/lambda/…DdaGroundedSamWorker…` → 0 events,
versus 72/72 alignment failures on the original run). Worker log lines show the retried
invocations: `Grounded-SAM inference: image=576x768 prompts=1 modality=Segmentation`.

Sample artifact — `s3://dda-portal-artifacts-164152369890-us-east-1/labeling/645504ce-a60a-4009-8349-7548c0025cd3/labeling-8022a9dc/prelabels/task-000000.json`:

- top-level keys `{image_height, image_width, modality, regions}`
- `modality = "Segmentation"`, `image_width = 576` (int), `image_height = 768` (int)
- `regions`: 2 entries, both `class = "cookie_gap"`, non-empty `rle` strings (3086 and 578 chars), scores `0.645` and `0.454`

Job detail AFTER (21:54:35Z, same GET): `prelabel_failed_count = 0`,
`prelabel_available_count = 72`, `prelabel_failure_reasons` **omitted** (Req 4.4),
corrected override on the record, job still `InProgress`. Task items' human-labeling fields
(`status = Submitted`, `human_annotated`, annotation keys) untouched by the reset — only the
pre-label attributes changed.

## Step 5 — Wizard guardrail live proxy (Reqs 1.1, 3.1) — PASS

Deployed bundle `s3://dda-portal-frontend-164152369890/assets/index-B1kd0eNX.js`
(2,080,603 bytes, shipped 21:38:52Z) contains:

- `contains a period` ×2 — both guardrail message variants:
  - override source: `The text prompt for label "${label}" contains a period. Periods separate labels in the detection caption — remove them or split the idea into a short noun phrase`
  - label source: `Label "${label}" contains a period and has no text prompt. Grounded-SAM uses the label name as its text prompt — enter a text prompt without periods…`
- `short noun phrase` ×2 — guardrail message + guidance content:
  `A text prompt is a short noun phrase naming the visual thing to find — for example "gap between broken cookie pieces" or "scratch on metal surface". The detector localizes what the text …`
- `Re-run pre-labels` ×3 — the detail-page action `` `Re-run pre-labels (${N} failed)` `` (`data-testid="rerun-prelabels-button"`) and the modal header
- `rerun-prelabels` ×6 — including the API client method posting `` `/labeling/${encodeURIComponent(id)}/rerun-prelabels` ``
- both guidance examples ×1 each

The creation-side guardrail rule itself was exercised live on the API in Step 2 (the same
shared rule, server-side); direct in-browser wizard interaction was out of scope (no UI
automation available).

## Verdict

Every step passed. The motivating incident is fully recovered live: the bodyless retry of the
broken prompt was **refused with the corrective per-label error**; the corrected noun-phrase
retry was **accepted (202, retried_count 72)**, persisted before triggering, audited, and all
**72/72 tasks resolved Available in 89 seconds with zero failures**; the sample pre-label
artifact is a well-formed Segmentation payload classified as `cookie_gap`.

Requirements exercised live: 4.1 (summary present with reasons), 4.4 (summary omitted at zero),
5.5 (creation-identical validation + persist-before-trigger), 5.6 (bodyless refusal with
corrective errors), 5.8 (rejection mutates nothing), 5.9 (202 + audit + async trigger),
6.1/6.4 (conditional reset with error removal, non-Failed tasks untouched), 6.2 (re-enqueue
consumed by the unmodified pipeline), 6.7 (pre-labels appear with then-current prompts);
1.1/3.1 by deployed-bundle content proxy.

Raw request/response payloads and poll log preserved under `/tmp/task82/` on the verification host.

## Anomalies

- None functional. Two observations: (1) the Failure_Reason_Summary's distinct-value
  aggregation yields 5 capped entries of count 1 for this job (worker error strings embed
  unique requestIds) instead of one count-72 line — conforms to Req 4.1 as specified;
  (2) resolution speed (72 images in 89s) far under the planning budget, thanks to warm
  concurrent workers.
