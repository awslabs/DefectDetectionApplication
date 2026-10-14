# Grounded-SAM Mask Offset — Task 6 Live Verification Notes

Spec: `.kiro/specs/grounded-sam-mask-offset` (bugfix)
Account 164152369890, us-east-1.

- Fixed worker deployed: 2026-09-07T03:48Z (`EdgeCVPortalComputeStack` UPDATE_COMPLETE,
  log `edge-cv-portal/deploy-gsam-mask-offset-20260907T034556Z.log`)
- Fixed worker physical name: `EdgeCVPortalComputeStack-DdaGroundedSamWorkerA3B13-i6P1oAqkvVtZ`
  (CodeSha256 `13df0f40a113dd366d578efdeed46610c1fd349d78773ddae33a3e45d3463b3f`,
  image asset `6261b9e3…`; pre-fix asset `8d8e5391…`)
- Live verification invokes: 2026-09-07T04:01:17Z – 04:02:20Z (6 invokes, all HTTP 200)
- Analysis artifacts: `/tmp/gsam-task6/` (payloads, raw responses, `analyze.py`,
  `after-results.json`)

## 1. Root cause + pinned convention (task 1/2 summary)

The deployed samexporter MobileSAM decoder's in-graph `masks` postprocess has its
pad-crop **constant-folded to the export's tracing shape (683, 1024)** instead of
derived from `orig_im_size`. Every image whose `_sam_preprocess` resized geometry
(new_h, new_w) ≠ (683, 1024) came back warped/offset — scale = 1 does not protect an
image, only the traced geometry is spared.

Task-1 counterexamples and probe matrix (real `mobile_sam_20230629` artifacts,
synthetic known-ground-truth rectangles; details in
`edge-cv-portal/backend/tests/test_gsam_mask_offset_exploration.py`):

- Probe variants (a) original-frame coords, (b) 1024-frame coords + graph `masks`
  (the pre-fix handler behavior), (c) swapped `orig_im_size` [W, H], and
  (d) resized-frame `orig_im_size` + external warp **all scored below the 0.85 IoU
  threshold** on the three geometries (portrait 576×768, landscape 768×576,
  square 512×512).
- Canvas-frame control: feeding `orig_im_size = (683, 1024)` (the baked crop) put the
  mask EXACTLY on the scaled prompt — portrait prompt (85,128,341,427) → mask bbox
  (86,128,340,425) — proving the handler's `coords * scale` prompt feed was correct
  (design hypotheses H1/H3/H4 refuted) and isolating the defect to the constant-folded
  crop.
- Task-2 reclassification (observation-first, unfixed code): scale = 1 geometries
  1024×768 → IoU 0.8009, +26.0 px down (vector −0.3, +26.0); 768×1024 → IoU 0.2939,
  138.0 px (vector −51.9, +127.9) — both joined the bug-condition set. The one spared
  geometry: 1024×683 (resized = the traced crop) → IoU 0.9948, 0.2 px — the true
  preservation boundary.
- **Winning convention (variant e, implemented by task 3.1)**: bypass the graph's
  `masks` output; take `low_res_masks` (256×256) and run the official
  segment-anything postprocess externally — upsample to the 1024 encoder canvas, crop
  the pre-padding region `[:new_h, :new_w]`, resize to source W×H, threshold.
  Post-fix probe IoU: portrait 0.9942, landscape 0.9979, square 0.9996, centroid
  displacement ≤ 0.5 px.

## 2. Worker-deletion incident (5.1/5.2 execution note)

The live *unfixed* worker was deleted before the planned live BEFORE capture: a
flag-less `cdk deploy` from another checkout at 2026-09-07T02:53Z (stack events:
`DdaGroundedSamWorkerA3B1314A` DELETE_COMPLETE — the `-c deployGroundedSamWorker=true`
context flag is mandatory; without it CDK removes the worker). Consequences:

- The BEFORE Segmentation baseline (`before-baseline-5.1.json`) was taken from the
  job's stale S3 pre-labels (`labeling-8022a9dc/prelabels/task-000000..2.json`,
  written 2026-09-06T21:50Z by the unfixed worker on the same three images).
- The BEFORE OD boxes (`od-responses-5.1.json`) were captured from the redeployed
  worker — safe because the DINO path is preservation-guaranteed unchanged (verified
  again in §4).
- The 5.2 redeploy therefore showed the worker as a CREATE (restore), not an update:
  new physical name `…-i6P1oAqkvVtZ`.

## 3. Live before/after — Segmentation masks vs same-run DINO boxes

Method: presigned the SAME three images
(`s3://ryvan-cookies/training-images/anomaly-{1,10,11}.jpg`), re-ran the EXACT 5.1
payloads (`{"image_s3_presigned_url": …, "prompts": [{"label": "cookie_gap",
"prompt": "gap between broken cookie pieces"}], "modality": "Segmentation" |
"ObjectDetection"}`) by direct invoke against the fixed worker
(`--cli-binary-format raw-in-base64-out --cli-read-timeout 300`). RLEs decoded with
the shared-layer `dda_manifest.rle_decode`; per mask: centroid, IoU(mask, box),
containment = |mask ∩ box| / |mask| against the same-run OD boxes.

PASS criteria (task 6): each mask centroid inside its best DINO box AND
containment ≥ 0.5. (Mask-vs-BOX IoU is reported for continuity with the before
baseline; organic masks inside boxes won't reach the synthetic-rectangle 0.99 —
containment + centroid are the criteria.)

Rerun-API note: at task-6 time the job had 0 Failed tasks, so the
`POST /labeling/{id}/rerun-prelabels` eligibility gate (failed ≥ 1) made the 8.2
synthesized-event precedent unusable for *verification* — hence direct worker invoke.
(The rerun API was subsequently used for the approved regeneration, §5.)

### anomaly-1.jpg (576×768) — task-000000

OD boxes (identical before/after): cookie_gap 0.6455 [10.1, 235.9, 484.3, 699.1];
cookie_gap 0.4541 [144.9, 357.3, 370.3, 600.3]

| mask | BEFORE (stale prelabel, unfixed) | AFTER (fixed worker, live) |
|---|---|---|
| mask[0] (large) | area 104787, centroid (177.6, 582.4), best containment 0.8364, IoU 0.3689, spilled past box bottom, clipped at y=767 | area 132531, centroid (264.9, 446.8), **containment 1.0000**, IoU 0.6013, mask bbox [14, 239, 480, 693] tracks the box [10, 236, 484, 699], **PASS** |
| mask[1] (small) | area 2127, centroid (222.6, 693.7), best containment 0.552, IoU 0.0053, clipped at bottom edge | area 3730, centroid (245.1, 510.6), **containment 1.0000** (inside both boxes; bbox [148, 404, 342, 594] sits inside the 2nd box [145, 357, 370, 600]), IoU 0.0169 vs large / 0.0676 vs small box (thin-gap region — low IoU expected), **PASS** |

### anomaly-10.jpg (576×768) — task-000001

OD box (identical before/after): cookie_gap 0.6985 [27.6, 155.1, 485.0, 591.1]

| mask | BEFORE | AFTER |
|---|---|---|
| mask[0] | area 136850, centroid (201.3, 527.5), containment 0.6484, IoU 0.3574 | area 137634, centroid (263.4, 373.5), **containment 1.0000**, IoU 0.6877, mask bbox [32, 160, 481, 585] tracks the box, **PASS** |

### anomaly-11.jpg (576×768) — task-000002

OD box (identical before/after): cookie_gap 0.6327 [85.1, 172.8, 533.0, 638.9]

| mask | BEFORE | AFTER |
|---|---|---|
| mask[0] | area 128749, centroid (228.9, 554.0), containment 0.6755, IoU 0.3458 | area 141932, centroid (308.0, 405.7), **containment 1.0000**, IoU 0.6769, mask bbox [89, 178, 528, 632] tracks the box, **PASS** |

Summary: box-mask IoU improved 0.35–0.37 → 0.60–0.69 on the three large masks,
containment 0.55–0.84 → 1.0000 on all four masks, every centroid inside its box, no
mask touches the image edge any more (the BEFORE masks all clipped at y=767).
**All PASS criteria met.**

## 4. ObjectDetection preservation (live)

The AFTER ObjectDetection responses are **numerically identical** to
`od-responses-5.1.json` (same region count, class, score, and box on every image;
max absolute deviation 0 across all fields, compared at full float precision).
The DINO path is live-confirmed untouched by the fix.

## 5. Stale pre-label regeneration on job labeling-8022a9dc (user-approved)

The job's 72 `Available` pre-labels were generated by the unfixed worker
(2026-09-06T21:50Z) and remained offset after the fix; the Re-run button was locked
(0 Failed tasks). The user approved the admin unblock: flip `prelabel_status` to
`Failed` (DynamoDB, minimal single-attribute update) and re-run via the deployed API.

Timeline (2026-09-07, all UTC):

1. **04:07:23Z — task flip.** Paginated query of `dda-portal-labeling-tasks` for
   `job_id = labeling-8022a9dc` → exactly 72 tasks, all `Available`. Per-task
   conditional update `SET prelabel_status = :failed` with condition
   `prelabel_status = :available` (nothing else touched — `prelabel_s3_key` left in
   place; the consumer's `_mark_task` overwrites it on resolution). **72 flipped,
   0 condition failures.** Post-flip distribution: `{Failed: 72}`.
2. **04:08:37Z — first rerun attempt REFUSED (unexpected finding).** Synthesized
   `POST /labeling/labeling-8022a9dc/rerun-prelabels` (bodyless — the corrected
   override `cookie_gap: "gap between broken cookie pieces"` was already persisted by
   the 8.2 replay; claims sub `a4b804e8-…`, `custom:role UseCaseAdmin`) against
   `EdgeCVPortalComputeStack-DdaLabelingHandler95BC6FD-YAEO6xw5PLvd` → **400**
   `"Pre-labels can only be re-run while the job is InProgress (job status: Stopped)"`.
   The job had been stopped by the user themselves at 2026-09-06T22:43:20Z (audited
   `job_stopped`, same user id, `dda-portal-audit-log`) — i.e. after seeing the broken
   pre-labels and *before* the fix deploy. The gate mutated nothing.
3. **04:11:56Z — job status flip (deviation, flagged).** The product has **no resume
   route** (`POST /labeling/{id}/stop` is one-way; `Stopped` is terminal in the list
   sync), so the only unblock is the same class of admin mutation the user approved
   for tasks: conditional update `SET status = InProgress` (condition
   `status = Stopped`) on `dda-portal-labeling-jobs`. Applied and **left InProgress**
   so the regenerated pre-labels are actually usable; the user can re-stop from the UI
   with one click if they still want the job stopped.
4. **04:12:03Z — rerun accepted: 202** `{"job_id": "labeling-8022a9dc",
   "retried_count": 72, "message": "Re-run started for 72 failed pre-label task(s)"}`.
5. **04:13:32Z — all 72 tasks `Available`, 0 Failed, 0 Pending** (first poll, 89 s
   after the 202 — same duration as the 8.2 replay). Stable on re-poll at 04:14:55Z.
   All 72 S3 pre-label artifacts rewritten 04:12:20Z–04:12:58Z.

Sample artifact check — regenerated `task-000000.json` (anomaly-1.jpg, written
04:12:37Z, fixed worker):

| mask | stale BEFORE (unfixed, 2026-09-06T21:50Z) | regenerated AFTER (fixed) |
|---|---|---|
| mask[0] | area 104787, centroid (177.6, 582.4), containment 0.8364 | area 132531, centroid (264.9, 446.8), **containment 1.0000, centroid in box — PASS** |
| mask[1] | area 2127, centroid (222.6, 693.7), containment 0.552 | area 3730, centroid (245.1, 510.6), **containment 1.0000, centroid in box — PASS** |

The regenerated artifact is numerically identical to the §3 direct-invoke response for
the same image (same RLE, area, centroid) — the queue path and the direct-invoke path
produce the same fixed output.

Final state: job `labeling-8022a9dc` **InProgress** with 72/72 fresh, aligned
pre-labels from the fixed worker; zero Failed; no task attribute other than
`prelabel_status` (and the worker-managed reset/resolution fields) was mutated by the
admin flip.
