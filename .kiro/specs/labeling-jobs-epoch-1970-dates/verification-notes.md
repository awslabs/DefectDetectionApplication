# Labeling Jobs Epoch 1970 Dates — Task 5 Live Verification Notes

Spec: `.kiro/specs/labeling-jobs-epoch-1970-dates` (bugfix)
Account 164152369890, us-east-1. Portal `https://d23v4ltibogb5x.cloudfront.net`,
rest-api `yqvyoowugk`. Date: 2026-09-08 (all times UTC).

- Deploy under test: 2026-09-08 06:05–06:08Z via `./deploy-frontend.sh`, log
  `edge-cv-portal/deploy-frontend-labeling-jobs-epoch-1970-dates-20260908T060515Z.out`;
  frontend bundle `index-BE4O437x.js` (replaces `index-Bf9XkYCh.js`, deleted from S3
  by the sync)
- CloudFront invalidation `I4DZAX7B303GULTE7UBNB2SH23` (distribution `E13FEMIUFTIRQ1`)
  created 06:06:16Z, status **Completed** before bundle fetches
- Compute deploy (script step 6, flag-less): `EdgeCVPortalComputeStack` ✅
  UPDATE_COMPLETE (88.69 s; started 06:07:31Z). Changeset was custom-resource churn
  only (LambdaEnvUpdater / SageMakerEventBridgeIntegration replacements + nested-stack
  no-ops) — **zero function deletions, zero GroundedSam mentions in the log**. First
  live exercise of the portal-deploy-flag-hardening default-ON path
  (`context-helpers.ts` `groundedSamWorkerEnabled`): worker survived, as designed
- `DdaGroundedSamWorker` physical name
  `EdgeCVPortalComputeStack-DdaGroundedSamWorkerA3B13-9paW7gMXvjg2` **unchanged**
  pre/post deploy (LastModified 2026-09-07T17:12:10Z both times) — no deletion, no
  restore cycle
- Deployed tree: HEAD == `origin/integration/all-specs` (`32bd97c`) plus only this
  fix's working-tree diff (`Labeling.tsx`, `LabelingDetail.tsx`; git diff --stat
  confirmed 2 files, no backend)
- Pre-deploy gates: `pgrep -af "gdk component build"` and `pgrep -af "build-custom.sh"`
  both empty (builds.md sequencing respected)
- Analysis artifacts: `/tmp/epoch1970-verify/` on the verification host (events,
  raw lambda responses, served bundle copy)
- Drift-guard hygiene note: `infrastructure/cdk.out` had been moved aside
  (`cdk.out.bak-20260908T042746Z`); this deploy's synth regenerated a fresh
  `infrastructure/cdk.out` — expected, guard hashes untouched

## 1. Method — synthesized-event precedent

Browser UI automation unavailable; deployed behavior verified per the
grounded-sam-prompt-tuning-preview precedent: (a) served-bundle content checks via
CloudFront, (b) direct invoke of the **real deployed** list/detail handlers with
synthesized API-Gateway-shaped events through the handlers' own auth path
(`requestContext.authorizer.claims`: `sub = a4b804e8-5061-7004-12f2-38a0149dcd4c`,
`custom:role = UseCaseAdmin`, email/username fillers),
`usecase_id = 645504ce-a60a-4009-8349-7548c0025cd3` (cookies).

Routing note: the jobs LIST (`GET /labeling`) and job DETAIL (`GET /labeling/{id}`)
routes the fixed pages consume are served by **`LabelingHandler`** (`labeling.py`) —
physical name `EdgeCVPortalComputeStack-LabelingHandler2286A030-5u22VvCdqprm` — not
`DdaLabelingHandler` (which serves teams/labeler/preview/review routes). The datasets
table's `GET /datasets/pre-labeled` is served by
`EdgeCVPortalComputeStack-PreLabeledDatasetsHandler-Nw8h3SpXQwRa`.

## 2. Deployed bundle carries the fix

`GET https://d23v4ltibogb5x.cloudfront.net/index.html` (cache-busted) references
exactly `assets/index-BE4O437x.js` + `assets/index-BcxTwsXV.css`. Served JS
(2,095,492 bytes) is **byte-identical** to the locally built
`frontend/dist/assets/index-BE4O437x.js`:
sha256 `ecf0f799327e614dd8281645e691f7a5ad91a48e45e63d4e867846f1a2d84bd8` both sides.

All four fixed sites present in the SERVED bundle (minifier renders `* 1000` as `*1e3`):

| site (bugfix.md) | served minified pattern |
|---|---|
| jobs table Created, `Labeling.tsx:524` | `new Date(ee.created_at*1e3).toLocaleString(),sortingField:"created_at"` |
| DDA detail, `LabelingDetail.tsx:859-872` | `created_at?new Date(s.created_at*1e3)…:"-"`, `completed_at?new Date(s.completed_at*1e3)…:"-"`, `stopped_at?new Date(s.stopped_at*1e3)…:"-"` |
| GT detail, `LabelingDetail.tsx:1244-1249` | `new Date(n.created_at*1e3).toLocaleString()`, `completed_at?new Date(n.completed_at*1e3)…:"-"` |
| GT Duration, `LabelingDetail.tsx:1254-1256` | `Duration",value:n.completed_at?`${Math.round((n.completed_at-n.created_at)/3600)} hours`:`${Math.round((Date.now()/1e3-n.created_at)/3600)} hours (ongoing)`` |

OLD pattern gone: zero matches for `new Date(ee.created_at).toLocaleString()` and for
any bare-`created_at` Date construction adjacent to `sortingField:"created_at"`'s
`toLocaleString` cell. (The bundle's remaining bare `new Date(X.created_at)` matches
were each traced to out-of-scope features — users table, ManifestTransformer job
dropdown, key-label panels with `"—"` fallback — none of the four spec sites; the
spec's scope guard forbids touching them.)

Preservation in the same bundle: the already-correct `*1e3` +
`toLocaleDateString()` datasets-table pattern present twice
(`Labeling.tsx:596` and `PreLabeledDatasets.tsx:338`) — untouched (Req 3.1, 3.2).

## 3. Data proof — list endpoint returns epoch SECONDS (Req 3.3 live)

`aws lambda invoke` on `LabelingHandler…-5u22VvCdqprm`, event
`GET /labeling?usecase_id=645504ce-…` → **200**, `count: 12`. The screenshot's jobs
are present with `created_at` ≈ 1.7888e9 (seconds — the backend contract unchanged):

| job | created_at (s) | FIXED `new Date(s*1000)` | OLD `new Date(s)` (the bug) |
|---|---|---|---|
| `sdfsdasdfadfs` | 1788843135 | **2026-09-08T04:52:15Z** | 1/21/1970, 4:54:03 PM UTC |
| `tttttasdf` | 1788756292 | **2026-09-07T04:44:52Z** | 1/21/1970, 4:52:36 PM UTC |
| `234234asdadfs` | 1788734925 | **2026-09-06T22:48:45Z** | 1/21/1970, 4:52:14 PM UTC |

The OLD rendering of `sdfsdasdfadfs` — 1/21/1970 16:54:03 UTC — is exactly the
screenshot's `1/21/1970, 10:54:03 AM` in a UTC-6 browser: same job, same defect,
now impossible in the served bundle. Every fixed rendering is a 2026-09 date;
none is 1970 (Req 2.1).

## 4. Detail payload + Duration math (Req 2.2, 2.3, 2.4)

`GET /labeling/{id}` for `labeling-72cbf8c1` (`sdfsdasdfadfs`, DDA, Completed) → 200,
seconds throughout: `created_at = 1788843135` → 2026-09-08T04:52:15Z,
`completed_at = 1788843774` → 2026-09-08T05:02:54Z (10.7 min run).

Duration math across the live list (fixed `/3600` seconds vs old `/3600000` ms):

| job | status | fixed | old (the bug) |
|---|---|---|---|
| `cookies-segmentation` (GT) | Completed, Δ 3.48 h | **3 hours** | 0 hours |
| `cookies-binary` (GT) | Completed, Δ 26.44 h | **26 hours** | 0 hours |
| `tttttasdf` | InProgress | **25 hours (ongoing)** | 496,405 hours |
| `test` | ongoing-branch | **152 hours (ongoing)** | 496,405 hours |

Both defect symptoms from bugfix.md reproduced-then-fixed with live data: completed
GT jobs collapse to 0 hours under the old math and report real hours under the fixed
math; ongoing jobs report the absurd ~496k hours old vs realistic 25–152 h fixed.

## 5. Datasets table spot-check (Req 3.1 live)

`GET /datasets/pre-labeled?usecase_id=645504ce-…` on
`PreLabeledDatasetsHandler…-Nw8h3SpXQwRa` → **200, `datasets: []`** — the cookies
use case has no registered pre-labeled datasets, so the live table is empty (nothing
to regress at the data level). Preservation is carried by the served bundle's
untouched `*1e3`+`toLocaleDateString()` patterns (§2) and task 3.3's zero-diff
confirmation on `Labeling.tsx:596` / `PreLabeledDatasets.tsx` / `CreateTraining.tsx`.

## Verdict

Every step passed, nothing retried, no failure categories observed:

- Frontend deployed 06:05–06:08Z; invalidation **Completed**; served bundle
  `index-BE4O437x.js` byte-identical to the local build (sha256 match)
- All four fixed render sites verified present in the SERVED bundle; the old
  no-conversion pattern verified absent at the jobs-table site
- Live list endpoint returns epoch seconds; the screenshot's own jobs
  (`sdfsdasdfadfs`, `tttttasdf`) now render 2026-09-08 / 2026-09-07 — the exact
  1/21/1970 screenshot rendering is reproducible only from the OLD math
- Duration fixed live: 3 h / 26 h for completed GT jobs (was 0), 25–152 h ongoing
  (was ~496,405)
- `DdaGroundedSamWorker` `…-9paW7gMXvjg2` survived the flag-less compute deploy
  unchanged — first live proof of the flag-hardening default
- Backend untouched (2-file frontend diff; compute changeset custom-resource churn
  only)

Requirements exercised live: 2.1 (jobs table Created via bundle + data oracle),
2.2 (DDA detail sites in served bundle + seconds detail payload), 2.3 (GT detail
sites in served bundle), 2.4 (Duration seconds math with live values), 3.1 (datasets
table preserved in bundle; live table empty), 3.3 (list/detail endpoints still return
epoch seconds).

Cleanup: none required — verification artifacts confined to `/tmp/epoch1970-verify/`;
no portal state created or mutated (read-only GET invokes).
