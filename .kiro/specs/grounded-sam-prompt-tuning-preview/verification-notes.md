# Grounded-SAM Prompt Tuning Preview — Task 6.2 Live Verification Notes

Spec: `.kiro/specs/grounded-sam-prompt-tuning-preview` (feature)
Account 164152369890, us-east-1. Portal `https://d23v4ltibogb5x.cloudfront.net`,
rest-api `yqvyoowugk`. Date: 2026-09-07 (all times UTC).

- Deploy under test: 2026-09-07 13:46–13:50Z (`EdgeCVPortalComputeStack`
  UPDATE_COMPLETE 13:49:56Z, log
  `edge-cv-portal/deploy-grounded-sam-prompt-tuning-preview-20260907T134616Z.log`);
  frontend bundle `index-BtfaZrcB.js`
- `DdaLabelingHandler` physical name:
  `EdgeCVPortalComputeStack-DdaLabelingHandler95BC6FD-YAEO6xw5PLvd`
  (LastModified 13:49:09Z, timeout 900 s, env
  `GROUNDED_SAM_WORKER_FUNCTION_NAME` present — the task-3.1 gated wiring, live)
- `DdaGroundedSamWorker` physical name:
  `EdgeCVPortalComputeStack-DdaGroundedSamWorkerA3B13-xnyXEaM1eNXB`
  (CREATE_COMPLETE 13:48:41Z in this deploy — a restore, see §7; Image package,
  10240 MB, timeout 300 s, state Active)
- Analysis artifacts: `/tmp/gsam-preview-62/` on the verification host
  (events, raw responses, poll logs, payloads, `validate_payloads.py`,
  `tune-loop-diff.json`)

## 1. Method — synthesized-event precedent

Browser UI automation unavailable; deployed behavior verified by invoking the
**real deployed** `DdaLabelingHandler` with synthesized API-Gateway-shaped
events through the handlers' own auth path (the
grounded-sam-prompt-guardrails-and-prelabel-retry task-8.2 precedent):
`requestContext.authorizer.claims` carrying
`sub = a4b804e8-5061-7004-12f2-38a0149dcd4c`, `custom:role = UseCaseAdmin`
(plus `email` / `cognito:username` fillers for `get_user_from_event`). RBAC
resolved scope from the real tables against
`usecase_id = 645504ce-a60a-4009-8349-7548c0025cd3` (dataset
`s3://ryvan-cookies`).

Routes exercised: `POST /labeling-preview/runs` (resource
`/labeling-preview/runs`) and `GET /labeling-preview/runs/{runId}`
(pathParameters `{runId}`), polled at ~15 s. Sample images — the same three
the mask-offset verification used:
`training-images/anomaly-{1,10,11}.jpg` (576×768 each).

## 2. RUN 1 — prompt `gap between broken cookie pieces`

Request body: `{usecase_id: 645504ce-…, dataset_prefix: "training-images/",
model: "grounded-sam", task_type: "Segmentation", label_set: ["cookie_gap"],
prompt_overrides: {"cookie_gap": "gap between broken cookie pieces"},
sample_images: [anomaly-1, anomaly-10, anomaly-11]}`.

- 14:03:00Z — POST → **202** `{"run_id": "preview-5d2b9c76",
  "sample_count": 3, "status": "Running"}`
- 14:03:27Z — first poll: `Running`, samples 0 and 1 already `Succeeded`
- 14:03:44Z — second poll: **`Completed`, 3/3 `Succeeded`** (44 s wall clock
  for the whole run)

Per-sample timings (item `resolved_at` + worker REPORT lines):

| sample | resolved_at | Δ from 202 | worker duration | note |
|---|---|---|---|---|
| anomaly-1.jpg | 14:03:20Z | +20 s | 17.67 s (Init 305 ms) | **true cold start** — far under the 240 s Worker_Invoke_Bound |
| anomaly-10.jpg | 14:03:26Z | +26 s | 5.89 s | warm |
| anomaly-11.jpg | 14:03:32Z | +32 s | 5.32 s | warm |

The feared ~140 s cold first invoke did not materialize: the restored
worker's first inference cost 17.7 s. No sample approached the 240 s bound;
zero timeouts. (Timing expectation in the panel — "≈ 5 s warm" — matches the
measured 5.3–5.9 s warm invokes.)

Result payloads (S3 `labeling-previews/645504ce-…/preview-5d2b9c76/{0,1,2}.json`,
written 14:03:21–14:03:33Z, fetched via each result entry's presigned
`result_url`): top-level `{sample_key, state: "Succeeded", prelabel,
image_width: 576, image_height: 768}`; `prelabel = {modality: "Segmentation",
image_width, image_height, regions: [{class, rle, score}]}`.

**RLE decode proof** — every region decoded with the shared layer's
`dda_manifest.rle_decode(rle, 576, 768)` (raises unless the counts sum to
width×height): all 4 regions decoded, counts sum **442368 = 576×768** each.

| sample | regions | scores | mask areas (px) | rle sha256/12 |
|---|---|---|---|---|
| anomaly-1.jpg | 2 × cookie_gap | 0.6455, 0.4541 | 132531, 3730 | 04621ee39c1d, 13613f4a743a |
| anomaly-10.jpg | 1 × cookie_gap | 0.6985 | 137634 | 847ecfb6da88 |
| anomaly-11.jpg | 1 × cookie_gap | 0.6327 | 141932 | 42114f21f1b6 |

Cross-check: areas and scores are numerically identical to the mask-offset
verification's fixed-worker AFTER baselines on the same images and prompt
(§3 of those notes: 132531/3730, 137634, 141932; DINO scores 0.6455/0.4541,
0.6985, 0.6327) — the preview executor path and the labeling-time path
produce the same worker output. (Req 4.1, 4.2, 4.3 exercised live.)

## 3. RUN 2 — prompt `crack` (same samples)

Identical body except `prompt_overrides: {"cookie_gap": "crack"}`.

- 14:05:17Z — POST → **202** `{"run_id": "preview-cf7057c4",
  "sample_count": 3, "status": "Running"}`
- 14:05:24Z — first poll: sample 0 `Succeeded`
- 14:05:40Z — second poll: **`Completed`, 3/3 `Succeeded`** (23 s wall clock)

Per-sample: resolved 14:05:24Z / 14:05:29Z / 14:05:35Z (+7 s, +12 s, +18 s);
worker durations 5.51 s / 5.39 s / 5.42 s — all warm. All regions
`class = cookie_gap`, all RLEs decoded (counts sum 442368 each), scores
present.

| sample | regions | scores | mask areas (px) | rle sha256/12 |
|---|---|---|---|---|
| anomaly-1.jpg | 2 × cookie_gap | 0.3948, 0.3757 | 3754, 4618 | 90b0c5d7eec7, fc02ae1ed634 |
| anomaly-10.jpg | 1 × cookie_gap | 0.5671 | 137615 | 555098e869dd |
| anomaly-11.jpg | 1 × cookie_gap | 0.5377 | 141914 | 62ad81772fdb |

## 4. Tune-loop proof — the two prompts produce different region sets

Diff of run 1 vs run 2 (`/tmp/gsam-preview-62/tune-loop-diff.json`):

| sample | RLE sets | scores | concrete difference |
|---|---|---|---|
| anomaly-1.jpg | **disjoint** | **differ** | the dominant 132,531 px gap mask found by the noun-phrase prompt **vanishes** under `crack` (largest region drops to 4,618 px); scores fall 0.6455/0.4541 → 0.3948/0.3757 |
| anomaly-10.jpg | **disjoint** | **differ** | area 137634 → 137615, score 0.6985 → 0.5671, different RLE bytes |
| anomaly-11.jpg | **disjoint** | **differ** | area 141932 → 141914, score 0.6327 → 0.5377, different RLE bytes |

Every sample's region set differs between the runs — mask bytes (sha256) on
all three, scores on all three, region structure dramatically on anomaly-1.
Each run's Preview_Prompt_Map was derived from its own recorded overrides
(§6 confirms the per-run recording). **The tune loop is proven live**
(Req 2.2, 7.1): an edited prompt drives the next run and visibly changes
what the worker finds, in ~25–45 s per 3-image iteration instead of a
72-image job cycle.

## 5. Deployed bundle check

- `GET https://d23v4ltibogb5x.cloudfront.net/index.html` references exactly
  `assets/index-BtfaZrcB.js`
- The bundle (2,089,044 bytes) contains the grounded-sam preview surface:
  `grep -c preview-gsam-timing-note` → **1** (the task-2.3 timing-note
  testid; Req 1.6 marker). In-browser spot-check was out of scope (no UI
  automation on this host); deployed-bundle content is the proxy, per the
  guardrails-spec precedent.

## 6. RUN item shape (DynamoDB)

`aws dynamodb get-item` on `dda-portal-labeling-tasks`
`{job_id: PREVIEW#preview-5d2b9c76, task_id: RUN}`:

- `model = "grounded-sam"`, `task_type = "Segmentation"`,
  `label_set = ["cookie_gap"]`, `status = "Completed"`, `sample_count = 3`
- **`prompt_overrides = {"cookie_gap": "gap between broken cookie pieces"}`
  carried on the RUN item** (Req 3.1); run 2's item carries
  `{"cookie_gap": "crack"}` — each run records its own prompts (Req 7.1)
- **few-shot disabled shape**: `few_shot_enabled = false`,
  `attached_example_count = 0`, `omitted_example_count = 0`
- **No** `detection_prompt`, `token_budget`, or `downscale_max_edge`
  attributes on the item (full attribute list: attached_example_count,
  created_at, created_by, expires_at, few_shot_enabled, job_id, label_set,
  model, omitted_example_count, prompt_overrides, sample_count, status,
  task_id, task_type, ttl, updated_at, usecase_id)
- GET response mirror: `few_shot: {enabled: false, attached: 0, omitted: 0}`,
  `downscale_max_edge: null`, no `token_budget` key

## 7. Deploy-collision / reconcile note

The deploy under test (13:46–13:50Z) is the **rebased tree `ba0055c`**
(`integration/all-specs`), carrying both this spec's changes and
`cloud-static-camera-provisioning` (`8b8cb60`) in one compute-stack deploy —
one stack update served two specs' live verifications.

The worker was **restored by this deploy after its third flag-less
deletion** (`DdaGroundedSamWorkerA3B1314A` CREATE_COMPLETE 13:48:41Z in the
deploy log — a CREATE, not an UPDATE), yielding the new physical name
`…-xnyXEaM1eNXB`. Lineage: `…-qHx8huoBWZFA` (guardrails-era) →
deleted/restored as `…-i6P1oAqkvVtZ` (mask-offset §2, deletions one and two)
→ deleted again by a flag-less deploy → restored as `…-xnyXEaM1eNXB` (this
deploy). The `-c deployGroundedSamWorker=true` context flag remains mandatory
on **every** compute deploy (including deploy-frontend.sh's internal step-6
deploy, which is why the frontend shipped via steps 1–5 only); a flag-less
deploy deletes the live worker.

Because the restore was fresh (~13:50Z), this verification expected a COLD
first invoke of ≈ 140 s; the measured cold start was 17.7 s (§2) — well
inside the 240 s per-sample bound either way.

## Verdict

Every step passed, nothing retried, no failure categories observed:

- Two Grounded_SAM_Preview_Runs accepted (202), executed against the
  deployed worker, and **Completed** with 6/6 samples `Succeeded`
  (run 1 `preview-5d2b9c76`, run 2 `preview-cf7057c4`)
- All 7 result regions: `class = "cookie_gap"` (Label_Set member), non-empty
  RLE **decodable by the shared `dda_manifest.rle_decode`** with counts
  summing to the worker-reported 576×768, and a `score` on every region
- Per-sample timings: 17.7 s cold / 5.3–5.9 s warm — the 240 s
  Worker_Invoke_Bound never approached, zero timeouts
- **Tune loop proven**: disjoint RLE sets and differing scores on all three
  samples between the two prompts
- Deployed bundle `index-BtfaZrcB.js` referenced by index.html and carrying
  the grounded-sam timing-note marker
- RUN items carry the grounded-sam family shape: model, per-run
  `prompt_overrides`, few-shot disabled, no llm-only attributes

Requirements exercised live: 2.2 (per-run Prompt_Map from recorded
overrides), 4.1 (sync invoke of `GROUNDED_SAM_WORKER_FUNCTION_NAME` within
the bound), 4.2/4.3 (consumer-rule-validated payloads in the
`PreviewResultCanvas` shapes with scores and dimensions), 5.1 (decodable
Segmentation RLEs; bundle proxy for rendering), 7.1 (edited prompt drives
the next run).

Cleanup: none — preview runs are user-scoped, TTL'd state under
`labeling-previews/` (one-day lifecycle rule) and were deliberately left in
place.
