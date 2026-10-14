# Labeling Job Cleanup, Work Stealing, and Podium — Task 5.2 Live Verification Notes

Spec: `.kiro/specs/labeling-job-cleanup-work-stealing-and-podium` (feature)
Account 164152369890, us-east-1. Portal `https://d23v4ltibogb5x.cloudfront.net`,
rest-api `yqvyoowugk`. Date: 2026-09-07 (all times UTC).

- Deploy under test: compute 17:10–17:14Z (`EdgeCVPortalComputeStack`
  UPDATE_COMPLETE 17:14:21Z, log
  `edge-cv-portal/deploy-labeling-job-cleanup-work-stealing-and-podium-20260907T171006Z.log`,
  `-c deployGroundedSamWorker=true` + bare `cloudFrontDomain` per task 5.1);
  frontend steps 1–5 17:15–17:17Z (log
  `edge-cv-portal/deploy-frontend-labeling-job-cleanup-work-stealing-and-podium-20260907T171503Z.log`),
  bundle `index-Bf9XkYCh.js`, CloudFront invalidation
  `IAXV4H5E4VFU10KN1PQL7ORI6J`
- **This deploy carries two commits' worth of changes**: this spec
  (`c90ff22`) rebased onto `e0746cd` (cloud-static-camera-provisioning
  shadow partial-delta / map-merge fixes) — one compute-stack deploy
  shipped both (the grounded-sam-preview §7 pattern)
- Deployed handlers (physical names via
  `describe-stack-resources --logical-resource-id …`):
  - `DdaLabelingHandler95BC6FDD` →
    `EdgeCVPortalComputeStack-DdaLabelingHandler95BC6FD-YAEO6xw5PLvd`
    (LastModified 17:13:00Z) — owns the pool/steal/DELETE routes
  - `LabelingHandler2286A030` →
    `EdgeCVPortalComputeStack-LabelingHandler2286A030-5u22VvCdqprm`
    (LastModified 17:12:23Z) — owns `GET /labeling/{id}` (detail podium)
  - `DdaLabelingWorkerD5E96C2A` →
    `EdgeCVPortalComputeStack-DdaLabelingWorkerD5E96C2A-foX8XixXUQp2`
    (LastModified 17:12:07Z) — `delete_job` / `generate_manifest` actions
- Route registration confirmed on the live rest-api before any call:
  `GET /labeler/jobs/{jobId}/pool`, `POST /labeler/jobs/{jobId}/steal`,
  and `DELETE` on `/labeling/{id}` all present with OPTIONS preflights
- Analysis artifacts: `/tmp/labeling-cleanup-52/` on the verification
  host (seed + step scripts, every request/response pair as
  `step*-*.json`, `dataset-before.txt`, step summaries)

## 1. Method — synthesized-event precedent

Browser UI automation unavailable; deployed behavior verified by invoking
the **real deployed** Lambdas with synthesized API-Gateway-shaped events
through the handlers' own auth path (the
`.kiro/specs/grounded-sam-prompt-tuning-preview/verification-notes.md` §1
precedent): `requestContext.authorizer.claims` with real-table RBAC and
membership resolution inside the handlers.

- Admin calls: `sub = a4b804e8-5061-7004-12f2-38a0149dcd4c`,
  `custom:role = UseCaseAdmin` (holds MANAGE_LABELING_JOBS +
  MANAGE_LABELING_TEAMS)
- Labeler calls: two **fabricated** member subs
  `verify-labeler-a-44da1b` (`a@example.com`) and
  `verify-labeler-b-44da1b` (`b@example.com`), `custom:role =
  DataLabeler` (grants labeling:tasks-self via the JWT-claim fallback;
  actual access is decided by the live teams-table membership check,
  which is exactly what the seeded MEMBER# items exercise)

Use_Case `645504ce-a60a-4009-8349-7548c0025cd3` ("cookies", dataset
`s3://ryvan-cookies`, prefix `training-images/`; the use case's
`s3_bucket` output bucket is the same `ryvan-cookies` bucket — the
`labeled/` output prefix is disjoint from `training-images/` by key).

## 2. Scratch fixtures (direct DynamoDB/S3 seed, 17:30:53Z)

Everything scratch-named and confined to the scratch job/team; no
existing job, team, or dataset object touched at any point.

| Fixture | Value |
|---|---|
| Team | `team-scratch-verify-44da1b` (META + MEMBER# items for A and B) in `dda-portal-labeling-teams` |
| Job | `labeling-scr44da1b` in `dda-portal-labeling-jobs`: DDA, Classification, InProgress, `label_set [normal, anomaly]`, `image_count 4`, `dataset_bucket ryvan-cookies`, `dataset_prefix training-images/`, `team_id` set, `submitted_count 2` (matching the seeded Submitted pair, so the live counter stays exact) |
| Tasks | `task-0001`/`task-0002` **Assigned to A** (anomaly-1.jpg, anomaly-10.jpg); `task-0003`/`task-0004` **Submitted by B** at staggered epochs 1788801653 / 1788801753 (inline Classification annotations) — B "done", A holding the stealable work |
| Artifacts | 3 synthetic objects under `labeling/645504ce…/labeling-scr44da1b/` in `dda-portal-artifacts-164152369890-us-east-1` (2 × `prelabels/task-000{1,2}.json`, 1 × `annotations/seed-marker.json`) |

**Dataset baseline recorded before anything ran**:
`aws s3 ls s3://ryvan-cookies/training-images/ --recursive | wc -l` →
**74** objects (73 images + the zero-byte prefix marker), listing saved
to `dataset-before.txt`.

## 3. Work stealing (Req 5.1, 5.3, 5.9, 6.1)

All calls against the deployed `DdaLabelingHandler` as **B**:

1. `GET /labeler/jobs/{jobId}/pool` → **200
   `{stealable_count: 2, job_complete: false}`** (A's two Assigned
   tasks; no podium key)
2. `POST /labeler/jobs/{jobId}/steal` → **200 `{task_id: "task-0001",
   stolen_from: "verify-labeler-a-44da1b", stealable_count: 1}`** — the
   Steal_Order minimum (donor A's lowest task_id). Task item re-read:
   `assignee_user_id = B`, `status = Assigned`, **`stolen_from = A`,
   `stolen_at = 1788802319`** (17:31:59Z) — provenance recorded in the
   same conditional write
3. **Race attempt — the conditional write proven live**: the next
   Steal_Order candidate (`task-0002`, A's remaining Assigned task) was
   flipped directly to `Submitted` (simulated concurrent submission:
   `submitted_by = A`, `submitted_at = 1788802319`, + mirrored `+1` on
   the job counter since the direct flip bypasses the route). Steal
   again as B → **409 `{error: "No stealable tasks remain",
   stealable_count: 0}`**, and the flipped item — full-item compare
   before/after the losing steal — is **byte-identical**: still
   `Submitted` by A, no `stolen_from`/`stolen_at`. The
   `status = Assigned AND assignee_user_id = :donor` condition never
   moved a submitted task (Req 5.3/5.4)
4. `GET /labeler/jobs/{jobId}/next` as B → **200, `task_id =
   task-0001`** (the stolen task served through the untouched next-task
   flow, Req 5.9) with a presigned `image_url` for
   `training-images/anomaly-1.jpg` — fetched live: **206, image/jpeg,
   JPEG magic `ffd8`** — plus `submitted_count 2 / remaining_count 1`

## 4. Podium (Req 7.1, 8.2, 6.1)

1. B submitted the stolen `task-0001` through the deployed submit route
   (`{modality: 'Classification', label: 'normal'}`) → **200,
   `job_submitted_count: 4`** (17:33:21.585Z). The counter reaching
   `image_count` async-invoked the **real** manifest generation: worker
   log `generate_manifest` 17:33:22.581Z → manifest written
   17:33:22.742Z — `s3://ryvan-cookies/labeled/labeling-scr44da1b/output.manifest`,
   1236 bytes, **4 records** — and the job reached **Completed through
   the shipped flow** (no direct status write was needed;
   `job_completed` audit 17:33:22.731Z)
2. `GET .../pool` as B → **200 `{stealable_count: 0, job_complete:
   true}`** with the podium:
   - place 1: `verify-labeler-b-44da1b`, **submitted 3**,
     `final_submitted_at 1788802401`, `email b@example.com`
   - place 2: `verify-labeler-a-44da1b`, **submitted 1**,
     `final_submitted_at 1788802319`, `email a@example.com`
   (B 3 = 2 seeded + 1 stolen-and-submitted; A 1 = the race-flipped
   task — count-descending order, emails joined from the live MEMBER#
   items)
3. `GET /labeling/{id}` (admin claims, deployed **LabelingHandler**) →
   200, `status: Completed`, **`podium` exactly equal to the pool
   route's list** (asserted element-for-element) — the one shared
   `podium_ranking` consumed by both surfaces (Req 7.6), with
   `member_progress` riding alongside unchanged

## 5. Deletion (Req 1.1, 2.1–2.3, 3.1, 3.3)

1. `DELETE /labeling/{id}` (admin claims) → **202 `{job_id,
   status: "Deleting"}`**; `job_delete_requested` audit 17:34:22.847Z
2. The **real async invoke** ran the deployed worker — no fallback
   needed: worker log `delete_job` invoked 17:34:22.990Z, **“Deleted job
   labeling-scr44da1b: 4 task item(s), 3 artifact object(s)”**
   17:34:23.141Z (~150 ms); the job record was gone on the first poll
   (~3 s after the 202)
3. Post-deletion state, all verified:
   - tasks-table query on the job → **0 items**
   - artifact prefix `labeling/645504ce…/labeling-scr44da1b/` →
     **0 objects**
   - `labeled/labeling-scr44da1b/output.manifest` in the Use_Case
     output bucket → **retained** (Req 3.3 proven live)
   - **dataset intact**: `training-images/` recursive count after
     everything → **74 = the pre-run 74**, unchanged
   - audit trail: `job_delete_requested` (1) + `job_deleted` (1,
     details `{tasks_deleted: 4, artifact_objects_deleted: 3,
     usecase_id}`) + the earlier `task_stolen` (1, details carrying
     caller/donor/job) — scanned from `dda-portal-audit-log`
4. Scratch team deleted through the **deployed teams API**
   (`DELETE /labeling-teams/{teamId}`, admin claims) → 200; team
   partition query → **0 items**

## 6. Deployed bundle check

- `GET https://d23v4ltibogb5x.cloudfront.net/index.html` references
  exactly `assets/index-Bf9XkYCh.js`
- The bundle (2,095,464 bytes) carries every new surface's testid
  marker: `clear-prelabels` **1**, `restore-prelabels` **1**,
  `winner-podium` **1**, `steal-task-button` **1** (plus
  `podium-place-` **1**). In-browser spot check was out of scope (no UI
  automation on this host); deployed-bundle content is the proxy, per
  the prior specs' precedent.

## Verdict

Every step passed on the first attempt; the only fallback path
(direct worker invoke for a stalled async delete) was **not needed**.

- Work stealing proven live end to end: deterministic Steal_Order
  choice, provenance (`stolen_from`/`stolen_at`) in the atomic write,
  the race loser leaving a concurrently submitted task byte-identical
  (409 none-remain), and the stolen task served by the existing next
  flow with a working presigned URL
- Podium proven on both deployed surfaces from one shared ranking:
  pool route (labeler) and job detail payload (admin) returned the
  identical two-entry podium with emails joined from live membership
- Completion ran through the **real** counter → manifest → Completed
  pipeline (4-record manifest written by the deployed worker in ~1.2 s
  after the final submit)
- Deletion: 202 → real async worker → artifacts (3) → task items (4) →
  job record last, ~150 ms; zero traces of the job's own state; the
  dataset count unchanged (74 → 74) and the training manifest retained
- Deployed bundle `index-Bf9XkYCh.js` carries all four new UI markers

Requirements exercised live: 1.1 (202 + Deleting + async invoke), 2.1,
2.2, 2.3 (ordered cleanup with counts), 3.1 (zero dataset writes —
count-proof), 3.3 (manifest retained), 5.1 (steal transfer payload),
5.3 (conditional-write race), 5.9 (stolen task via next), 6.1 (pool
counts/complete/podium), 7.1 (count-descending ranking), 8.2 (detail
payload podium for the Completed team job), 9.1 (bundle-marker proxy
for the clear control).

Cleanup: scratch job, tasks, and artifacts removed by the deletion
under test itself; scratch team removed through the deployed teams API;
the scratch `labeled/labeling-scr44da1b/output.manifest` was removed
manually **after** its retention was verified (it was scratch data in
the customer bucket); final `training-images/` count re-confirmed 74.
The audit-log events (`job_delete_requested`, `job_completed`,
`job_deleted`, `task_stolen`) are append-only operational records and
were deliberately left in place.
