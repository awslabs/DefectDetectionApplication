# Portal Deploy Flag Hardening — Task 6.2 Live Verification Notes

Spec: `.kiro/specs/portal-deploy-flag-hardening` (feature)
Account 164152369890, us-east-1. Portal `https://d23v4ltibogb5x.cloudfront.net`,
rest-api `yqvyoowugk`. Date: 2026-09-08 (all times UTC). HEAD `420e9a7`
(`integration/all-specs`).

- Deploy under test: task 6.1's **first-ever deliberately flag-less**
  `cdk deploy EdgeCVPortalComputeStack -c cloudFrontDomain=d23v4ltibogb5x.cloudfront.net`
  (no `deployGroundedSamWorker` context), 2026-09-08 04:25–04:26Z
  (UPDATE_COMPLETE 04:26:38Z, deployment time 73.95 s, log
  `edge-cv-portal/deploy-portal-deploy-flag-hardening-20260908T042510Z.log`)
- Analysis artifacts: `/tmp/pdfh-62/` on the verification host (events, raw
  responses, poll log, payload, validation output, AWS CLI captures)

## 0. The incident class this closes

Four flag-less deploys deleted the live `DdaGroundedSamWorker` before this
spec (lineage: grounded-sam-mask-offset verification-notes §2 and
grounded-sam-prompt-tuning-preview verification-notes §7): 2026-09-07 02:53Z,
08:04Z, and two more from other checkouts/sessions — physical-name lineage
`…-qHx8huoBWZFA` → `…-i6P1oAqkvVtZ` → `…-xnyXEaM1eNXB` → the current
`…-9paW7gMXvjg2` (restored flag-ON 2026-09-07 17:12Z). One of the four came
through `deploy-frontend.sh` step 6's internal flag-less deploy, which forced
the steps-1–5 manual workaround. Under this spec's default-ON flag
(`groundedSamWorkerEnabled` in `lib/context-helpers.ts`, wired into
`compute-stack.ts`; documented in `cdk.json`), task 6.1 ran the same
flag-less deploy shape deliberately — and the worker survived. This section
plus the checks below close the incident class.

## 1. Task 6.1 proving-deploy evidence (recap)

- **Synth equivalence** (Req 3.5): `EdgeCVPortalComputeStack` synthesized
  with `-c cloudFrontDomain=d23v4ltibogb5x.cloudfront.net` vs
  `-c cloudFrontDomain=https://d23v4ltibogb5x.cloudfront.net` — templates
  identical modulo the two `Date.now()` custom-resource `Timestamp` props;
  `EdgeCVPortalStorageStack` byte-identical for the two spellings
- **Flag-less diff showed no worker deletion**: only the two Timestamp
  custom-resource props (`SageMakerEventBridgeIntegration`,
  `LambdaEnvUpdater`)
- **The deploy was a worker no-op**: the deploy log contains **zero**
  `GroundedSam` resource events — the only resource churn was the two
  Timestamp-driven custom-resource replacements and their nested-stack no-op
  updates; `EdgeCVPortalAuthStack` and `EdgeCVPortalStorageStack` reported
  "(no changes)"

## 2. Check 1 — worker alive, physical name UNCHANGED

`aws lambda get-function --function-name
EdgeCVPortalComputeStack-DdaGroundedSamWorkerA3B13-9paW7gMXvjg2`:

- Exists; `State: Active`, `LastUpdateStatus: Successful`
- **`LastModified: 2026-09-07T17:12:10.007+0000` — pre-deploy** (the 6.1
  deploy ran 04:25–04:26Z on 09-08, ~11 h later): the function was neither
  replaced nor updated by the flag-less deploy
- MemorySize **10240**, Timeout **300**, PackageType **Image**,
  Architectures `['x86_64']`, no Environment block (the shipped
  configuration, Req 1.6)
- ImageUri
  `…/cdk-hnb659fds-container-assets-164152369890-us-east-1:6261b9e39448b5e93bcf49b91e6f5a38effbbc29006a5b9b4615a665513f8eae`
  (ECR-cached fingerprint, unchanged)
- CloudFormation agrees (`list-stack-resources`, 274 resources —
  `describe-stack-resources` truncates at 100 and misses these):
  `DdaGroundedSamWorkerA3B1314A` CREATE_COMPLETE, LastUpdated
  2026-09-07T17:12:32Z (pre-deploy); `DdaGroundedSamWorkerServiceRole5254DD63`
  CREATE_COMPLETE

Lineage closed: `…-9paW7gMXvjg2` is the same physical name recorded before
the deploy — the first flag-less deploy in this stack's history that did NOT
delete/recreate the worker.

## 3. Check 2 — Worker_Wiring intact

`aws lambda get-function-configuration` on both consumers:

| function | physical name | GROUNDED_SAM_WORKER_FUNCTION_NAME |
|---|---|---|
| DdaLabelingHandler | `…-DdaLabelingHandler95BC6FD-YAEO6xw5PLvd` (LastModified 2026-09-07T17:13:00Z) | `EdgeCVPortalComputeStack-DdaGroundedSamWorkerA3B13-9paW7gMXvjg2` |
| DdaAutolabelWorker | `…-DdaAutolabelWorkerC8FF5C7-ygTfXVLSFv01` (LastModified 2026-09-07T17:13:00Z) | `EdgeCVPortalComputeStack-DdaGroundedSamWorkerA3B13-9paW7gMXvjg2` |

Both name the live worker exactly; neither was disturbed by the deploy.

## 4. Check 3 — StaticImagePin resources intact

From the same `list-stack-resources` on `EdgeCVPortalComputeStack`:

- `StaticImagePinStagingLifecycle4E4AA8D9` (Custom::AWS) —
  **CREATE_COMPLETE**, LastUpdated 2026-09-07T08:02:38Z
- `StaticImagePinStagingLifecycleCustomResourcePolicy1FE4370B`
  (AWS::IAM::Policy) — **CREATE_COMPLETE**, LastUpdated 2026-09-07T08:02:30Z

Both healthy (non-deleted), timestamps pre-deploy — untouched.

## 5. Check 4 — domain configuration uncorrupted

- `aws s3api get-bucket-cors` on
  `dda-portal-artifacts-164152369890-us-east-1`: one rule, `AllowedOrigins`
  exactly `["https://d23v4ltibogb5x.cloudfront.net"]` — **one** scheme, no
  trailing slash (Req 3.3); methods GET/PUT/POST/HEAD
- `UseCasesHandler` (`…-UseCasesHandler9D45DC90-CbpY0ir1Jjfs`)
  `CLOUDFRONT_DOMAIN = d23v4ltibogb5x.cloudfront.net` — bare (Req 3.4).
  Notably its LastModified is **2026-09-08T04:26:26Z** — the deploy's
  `LambdaEnvUpdater` custom-resource replacement re-applied env config
  during 6.1 and the domain stayed clean, so the normalized value survived
  a real re-write, not just a no-op
- `DdaLabelingWorker` (`…-DdaLabelingWorkerD5E96C2A-foX8XixXUQp2`)
  `PORTAL_DOMAIN = d23v4ltibogb5x.cloudfront.net` — bare (Req 3.4),
  LastModified 2026-09-07T17:12:07Z (untouched)

No `https://https://…` corruption anywhere.

## 6. Check 5 — end-to-end worker proof (Prompt_Tuning_Preview run)

Method: the synthesized-event precedent of
`.kiro/specs/grounded-sam-prompt-tuning-preview/verification-notes.md` §1 —
real deployed `DdaLabelingHandler` invoked with API-Gateway-shaped events,
`requestContext.authorizer.claims` carrying
`sub = a4b804e8-5061-7004-12f2-38a0149dcd4c`, `custom:role = UseCaseAdmin`;
Use_Case `645504ce-a60a-4009-8349-7548c0025cd3` (dataset
`s3://ryvan-cookies`). Body: `dataset_prefix "training-images/"`, model
`grounded-sam`, task_type `Segmentation`, `label_set ["cookie_gap"]`,
`prompt_overrides {"cookie_gap": "crack"}`, **1 sample image**
(`training-images/anomaly-1.jpg`, 576×768).

- 04:33:51Z — POST `/labeling-preview/runs` → **202**
  `{"run_id": "preview-96c68e6c", "sample_count": 1, "status": "Running"}`
- 04:34:18Z — first poll of GET `/labeling-preview/runs/preview-96c68e6c`:
  **`Completed`, sample 0 `Succeeded`** (27 s wall clock; item
  `resolved_at` 04:34:11Z, +20 s from the 202)
- Worker log REPORT (log group
  `/aws/lambda/…-DdaGroundedSamWorkerA3B13-9paW7gMXvjg2`): Duration
  **16955 ms** with **Init 191 ms** — a true cold start, well inside the
  240 s Worker_Invoke_Bound; Max Memory Used 3802 MB of 10240 MB
- GET response mirror: `few_shot {enabled: false, attached: 0, omitted: 0}`,
  `downscale_max_edge: null` (grounded-sam family shape)

Result payload (fetched via the result entry's presigned `result_url`):
`{sample_key, state: "Succeeded", prelabel, image_width: 576,
image_height: 768}`, `prelabel.modality = "Segmentation"`, **2 regions**,
both validated — `class = "cookie_gap"` (Label_Set member), non-empty RLE
decoded by the shared layer's `dda_manifest.rle_decode(rle, 576, 768)`
(counts sum **442368 = 576×768** each):

| region | class | score | mask area (px) | rle sha256/12 |
|---|---|---|---|---|
| 0 | cookie_gap | 0.3948 | 3754 | 90b0c5d7eec7 |
| 1 | cookie_gap | 0.3757 | 4618 | fc02ae1ed634 |

Cross-check: numerically **identical** to the prompt-tuning-preview
verification's RUN 2 (`preview-cf7057c4`, same image and prompt, §3 of those
notes: scores 0.3948/0.3757, areas 3754/4618, same RLE sha256 prefixes) —
the worker serving inference AFTER the flag-less deploy produces
byte-identical masks to the pre-hardening baseline.

## Verdict

Every check passed, nothing retried:

1. Worker alive with the **unchanged** physical name `…-9paW7gMXvjg2`,
   Active/Successful, 10240 MB / 300 s / Image, LastModified pre-deploy —
   the flag-less deploy preserved it (Req 1.1, 4.1 live)
2. `GROUNDED_SAM_WORKER_FUNCTION_NAME` on both `DdaLabelingHandler` and
   `DdaAutolabelWorker`, naming that worker (Worker_Wiring live)
3. Both `StaticImagePin*` resources CREATE_COMPLETE and untouched
4. CORS `AllowedOrigins` exactly `["https://d23v4ltibogb5x.cloudfront.net"]`;
   `CLOUDFRONT_DOMAIN` / `PORTAL_DOMAIN` bare — even through a live
   `LambdaEnvUpdater` re-write during the deploy (Req 3.3, 3.4 live)
5. Grounded-sam preview run `preview-96c68e6c` accepted (202), **Completed**
   in 27 s with the sample `Succeeded` and both regions validated
   (cookie_gap, decodable non-empty RLEs, byte-identical to the pre-deploy
   baseline)

**`deploy-frontend.sh` is now safe end-to-end**: its step-6 internal deploy
is exactly the flag-less, bare-domain shape 6.1 exercised (it passes
`-c cloudFrontDomain="$CLOUDFRONT_URL"` from the bare
`DistributionDomainName` output and no worker context), so the script's
steps-1–5 workaround is no longer needed — with zero script changes
(Req 4.1, 4.2, 4.3). The flag-less-deletion incident class is closed.

Requirements exercised live: 1.1 (flag-absent deploy keeps the worker and
its wiring), 3.3 (single-scheme CORS origin), 3.4 (bare env domains), 4.1
(the Deploy_Script's flag-less deploy shape preserves the worker).

Cleanup: none — the preview run is user-scoped, TTL'd state under
`labeling-previews/` (one-day lifecycle rule) and was deliberately left in
place; `/tmp/pdfh-62/` artifacts left on the verification host.
