# Verification Notes — portal-usecase-bucket-config

This file records the test environment, the pre-change baseline the implementation is compared against, the pre-deploy measurements the Customer impact table (design §16) depends on, and (filled in by the implementation and runbook steps) the after-change results, the deploy, the post-deploy checks and the migration counts. Everything here is portal-side; no device verification is owed. Counts and statuses only: no use case id, bucket name (other than the two `ucb-verify-*` verification buckets), account id or token appears in this file.

## Environment

Worktree: `/home/ubuntu/github/jp5/main/portal-bucket-config`, branch `portal/usecase-bucket-config`, base `48841a0` (= `origin/integration/all-specs` tip at the time of writing). This is the one place that names local paths; every other spec file refers to them as `$REPO` (the worktree root) and `$D` (the uncommitted state directory for this spec, below).

Host Python is 3.9 and cannot import the shared layer (the vendored `urllib3 2.8.0` needs 3.10+). Backend tests run through a Python 3.11 lane that matches the Lambda runtime (`lambda.Runtime.PYTHON_3_11`): a local Docker image `dda-portal-test:py311` with a virtualenv pinned to `edge-cv-portal/backend/requirements-dev.txt` (pytest 9.0.3, moto 5.1.22, PyJWT 2.15.0, hypothesis 6.168.1, boto3 1.43.101). Wrapper (run from the worktree root):

```
D=/home/ubuntu/github/jp5/main/.agents/portal-usecase-bucket-config
cd /home/ubuntu/github/jp5/main/portal-bucket-config
HYPOTHESIS_PROFILE=ci $D/portal_py -m pytest edge-cv-portal/backend/tests/<file> -q -p no:cacheprovider
```

The wrapper mounts the whole `/home/ubuntu/github/jp5/main` tree so the worktree's `.git` file resolves. Node v20.20.2 / npm 10.8.2 for the frontend and CDK. The owner's approval to run every task, including the deploy, the post-deploy verification and the trunk push, is recorded at `$D/owner-go-ahead.md`.

SDK note (verified in the lane on 2026-10-12): boto3 1.43.101 / botocore 1.43.101 — the `s3` service model carries `PutBucketAbac`, `GetBucketAbac`, `GetBucketTagging`, `PutBucketTagging`; the `s3control` service model carries `TagResource`, `UntagResource`, `ListTagsForResource`; the `s3` client has no `TagResource`. On-disk size: botocore 21.9 MB, boto3 1.3 MB. The design vendors exactly this version into the shared layer (design §6.2) so the deployed functions do not depend on the Lambda-managed runtime's bundled SDK, whose version is not known from the tree; the layer logs its `botocore.__version__` once per cold start so the deployed version is readable from any function's log stream (design §1.5).

SDK note, second read (same lane, 2026-10-13, for the `create_s3_bucket` order of design §5.5 and the IAM spelling of §6.1): the `s3` service model's `CreateBucketConfiguration` shape has the members `Bucket`, `Location`, `LocationConstraint`, `Tags`, and the `Tags` member's documentation states that `s3:TagResource` is required to create a general purpose bucket with tags and that `aws:ResourceTag` / `s3:BucketTag` conditions apply only after ABAC is enabled on the bucket; the `PutBucketAbac` operation's documentation names the required permission **`s3:PutBucketABAC`** and states that once ABAC is enabled tags are managed with `TagResource` / `UntagResource` and `PutBucketTagging` / `DeleteBucketTagging` can no longer be used; the `PutBucketTagging` documentation describes it as the operation for a general purpose bucket when ABAC is **not** enabled; the `s3control` `TagResource` documentation lists general purpose buckets among the supported resources with no ABAC precondition and names `s3:TagResource` as the permission. This is the documentary answer the runbook's migration write 11.4a relies on (no `put-bucket-tagging` fallback) and the evidence for the create-with-tags → ABAC → read-after-write → fallback order (no step depends on `TagResource` working on an ABAC-off bucket).

CLI note (verified on this host on 2026-10-13, AWS CLI 2.35.21): `aws s3api tag-resource` is rejected (`Found invalid choice 'tag-resource'`); the bucket tag write for the ABAC model is `aws s3control tag-resource` whose skeleton is `{AccountId, ResourceArn, Tags[{Key, Value}]}`; `aws s3api put-bucket-abac` exists with `AbacStatus.Status`. Every tagging command in the spec, the remedy constants, the admin guide and the runbook uses the `s3control` form with `--account-id`.

## Baseline (pre-change, taken on `48841a0`, no code changes)

| Suite | Runner | Collected | Passed | Failed | Errors | Skipped |
|---|---|---|---|---|---|---|
| `edge-cv-portal/backend/tests` (HYPOTHESIS_PROFILE=ci, 8 parallel file chunks; a single full-suite process hangs on this host) | 3.11 lane | 6137 (+8 files failing collection, pre-existing) | 6057 | 40 | 35 | 5 |
| `test/backend-test/security/preservation` | host python3 | 164 | 156 | 0 | 0 | 8 (known host-side skips) |
| IAM baseline suites (`test_preservation_iam_*.py`, `test_iam_bug_condition_exploration.py`) | host python3 | 70 | 66 | 0 | 0 | 4 |
| builds.md guard pair | host python3 | 7 | 4 | 0 | 0 | 3 |
| whole `test/backend-test/security` | host python3 | 276 | 268 | 0 | 0 | 8 |
| frontend `npm run build` | node 20 | — | exit 0 | — | — | — |
| frontend `npx vitest run` | node 20 | 228 files / 2282 tests | 2282 | 0 | 0 | 0 |
| frontend `npm run lint` | — | not runnable (eslint not installed; pre-existing) | | | | |

The 40 failures / 35 errors in the backend suite are pre-existing and unrelated (per-file isolation runs and classification in `$D/baseline-counts.md`, `$D/backend-failures-errors.txt`). The after-change run must show no new failing test and the new files green.

Baseline command shapes (from `$D/baseline-notes.md`): collection with `--collect-only --continue-on-collection-errors`; the 418 test files split into 8 chunks run in parallel with `--junitxml`, summed by `$D/aggregate.py`; stalled chunks re-run per file with `xargs -P`.

Preservation pins that this spec touches on purpose: `iam_out_of_scope_baseline.json` pins `edge-cv-portal/backend/functions/model_converter.py` (hash `295a015c…` at `48841a0`) — rebaselined by task 9.0; `iam_baseline_EdgeCVPortalComputeStack.template.json` — regenerated by task 9.1 through the approved-additions record. Pins that must stay byte-identical: the vendored `urllib3` tree and `requests/auth.py` (`test_preservation_dependency_out_of_scope.py`), which is why the SDK is vendored with `--no-deps`.

Writer-guard rule self-check on the unmodified tree (design §2.4, run while writing revision 3 of the design and re-run independently by the revision-3 review): the function-scoped, write-position-scoped rule finds exactly `usecases.create_usecase`, `usecases.update_usecase` and `data_management.configure_data_account`; a literal-anywhere rule finds twelve functions (nine of them read `usecase['s3_bucket']` and write unrelated tables), which is why the position scope exists. The rule detects writers that name the attributes; a pass-through writer is outside what it can see (design §2.4 states the allow-list convention it relies on).

Review findings judged wrong (revision-4 design review, re-read at `48841a0` while writing revision 5; every other item of that review was applied in the spec text, design §17 "Revision 5"):
- Finding 11, `_legacy_role` sub-item (review: step 4 at 1114–1120, step 2 at 1093–1099): the step comments in `shared_utils.py` are at 1090 (`# 1. Check if user is PortalAdmin from JWT`), 1096 (`# 2. Check for PortalAdmin in DynamoDB`), 1105 (3) and 1121 (4), so the design's 1096–1103 (step 2) and 1121–1124 (step 4) stand; not applied.
- Finding 11, `shared_components.py` sub-item (review: derivation at 52–53): line 52 is the comment, 53 is `COMPONENT_BUCKET_PREFIX = …` and 54 is `COMPONENT_BUCKET = … f'{COMPONENT_BUCKET_PREFIX}-{AWS_REGION}-{PORTAL_ACCOUNT_ID}'`, so the design's 53–54 stands; not applied. The three remaining sub-items (creation timestamp `usecases.py:1135`, `get_usecase` in `create_training_job` at `training.py:761`, `data_management.create_bucket` at 309 — it spans 309–324 with the instruction text at 321) were right and are applied.
- Finding 15, router lines (review: 975–976): line 975 is `return list_usecases(user)`; the `elif http_method == 'POST': return create_usecase(event, user)` pair is at 976–977 and the `verify-role` branch at 984 — the conclusion (every POST reaches `create_usecase` first) is right and applied with the corrected lines.
- Finding 17, "fourteen call sites in seven modules": the grep (`get_s3_client_for_bucket(` minus the definition, `functions/` and the layer, tests excluded) finds fourteen call sites in **six** modules — `dda_autolabel_worker` 1, `dda_labeling` 3, `dda_labeling_worker` 2, `pre_labeled_datasets` 1, `training` 6, `s3_browse_utils` 1; design §1.5 says six.

Test-harness probe owed by task 0.16 (filled by the API task): moto 5.1.22 honours `ReturnValuesOnConditionCheckFailure='ALL_OLD'` on `UpdateItem` (old item present in the `ConditionalCheckFailedException` response) — yes / no: —; the api tests therefore run the `_add_additional_bucket` 404-vs-`already_present` cases against moto / against a monkeypatched `table.update_item`: —.

Portal-owned bucket facts used by design §1.3 (read at `48841a0`): `PORTAL_ARTIFACTS_BUCKET`, `PORTAL_ACCOUNT_ID` and `COMPONENT_BUCKET_PREFIX` are in `lambdaEnvironment` (`compute-stack.ts:725/730/758`); `COMPONENT_BUCKET` is set explicitly on two functions (1828, 2595) and derived elsewhere as `{prefix}-{region}-{account}` (`shared_components.py:53–54`; CDK 1813); `storage-stack.ts` tags no bucket; the only bucket tag writer in the tree is `tag_bucket_for_portal` (`usecases.py:589`); the inference-results bucket is a use-case-account bucket (`captures.py:90`, `deployments.py:1501`) and `inference_uploader_s3_bucket` has no writer in `functions/` or the layer.

## IAM evidence (task 2.1; rows 1–3 recorded by the spec step, row 4 by the runbook)

| Question | Source (URL, date read) | Answer | Consequence |
|---|---|---|---|
| Does the documentation list `aws:RequestTag/${TagKey}` as applying to `s3:TagResource`? | S3 User Guide, "Using tags with S3 general purpose buckets", https://docs.aws.amazon.com/AmazonS3/latest/userguide/buckets-tagging.html, read 2026-10-13 (the Service Authorization Reference page `service-authorization/latest/reference/list_amazons3.html` is script-rendered and could not be text-fetched on 2026-10-13; SAR page not re-read, user guide authoritative) | **Yes.** `aws:RequestTag/key-name` is documented as restricting which tag key-value pairs can be passed during the `TagResource` and `CreateBucket` API operations; example policy 1.1 conditions `s3:CreateBucket` + `s3:TagResource` on `aws:RequestTag/project` | the `s3:TagResource` statement on the UseCases role **is conditioned** (`StringEquals aws:RequestTag/dda-portal:created-by = portal`); the one condition also covers the tags supplied in `CreateBucketConfiguration.Tags` (design §5.5 step 3), for which S3 requires `s3:TagResource` (SDK note, second read) |
| Does it list `aws:TagKeys` for `s3:TagResource`? | same page, same date | **Yes** — listed among the global condition keys for bucket ABAC; example policy 1.3 restricts tag modification to four permitted keys with `aws:TagKeys` | informational (the follow-on uses it); not added to this spec's statement |
| Spelling of the bucket ABAC action (`s3:PutBucketABAC`) | vendored botocore 1.43.101, `PutBucketAbac` operation documentation ("You must have s3:PutBucketABAC permission to perform this action"), read in the 3.11 lane on 2026-10-13; the follow-on spec `portal-authorization-and-iam-scoping` uses the same spelling | **`s3:PutBucketABAC`** (the operation keeps the service model's `PutBucketAbac` casing) | the stack, the CDK test and the design use `s3:PutBucketABAC` |
| `iam simulate-principal-policy` for the UseCases role statement, with and without the request tag | runbook (11.1 / 11.2, live account) | — | recorded shape works as the design says |

## After-change results (to be filled by the implementation step)

| Suite | Result | Log |
|---|---|---|
| new backend files (`test_usecase_bucket_config_*.py`, `test_shared_layer_sdk_operations.py`, `test_pre_labeled_datasets_authz.py`, `test_property_*bucket*`, `test_property_model_validate_role_gate.py`) | | |
| neighbour groups (helper stubs, RBAC group, `test_model_import_fine_tunable.py`, `test_dda_autolabel_worker_few_shot.py`, `test_model_converter_*.py`) | | |
| chunked full backend run vs baseline | | |
| `infrastructure`: `npm run build`, `npm test -- usecase-bucket-config-iam` | | |
| preservation: IAM synth after rebaseline, `iam_out_of_scope` after 9.0, dependency out-of-scope guard after 1.0, guard pair, whole security dir | | |
| frontend: new component tests, full `vitest`, `npm run build` | | |

## Runbook 11.0 — preconditions (to be filled)

| Check | Observed |
|---|---|
| credentials / region / three stacks `*_COMPLETE` | |
| no component build running | |
| tree clean; HEAD is this spec's commits on `portal/usecase-bucket-config` | |
| trunk moved since `48841a0`? rebase performed? gates re-run? | |
| guard pair green with `cdk.out` aside | |
| enforcement values in force (`portal_registry_deployed_values`: n on / n off) and the value the deploy passes | |
| pre-deploy frontend bundle name (for check 10) | |

## Runbook 11.1 — pre-deploy sizing and IAM measurement (to be filled)

Script: `$D/sizing_scan.py` (read-only; DynamoDB `Scan`/`Query`, S3 `GetBucketTagging`, IAM `SimulatePrincipalPolicy`). Default output is counts only; `--identifiers` goes to the terminal and to `$D` only. Each row maps to a design §16 impact row.

| Count | §16 row | Before (11.1) | After migration writes (11.4c) |
|---|---|---|---|
| use cases scanned | — | | |
| use cases whose `s3_bucket` lacks the portal tag (readable / tags unreadable with the caller's credentials) | 10, 22 | | |
| use cases whose `data_s3_bucket` lacks the portal tag | 10, 12 | | |
| use cases whose configured bucket is unreadable through the use case's own role (STOP if > 0) | 10 | | |
| use cases whose configured bucket is a portal-owned bucket (artifacts or component; STOP if > 0 — dropped from the set by rule after deploy) | 29 | | |
| use cases whose configured bucket name fails the strict bucket-name grammar (legacy names) | 20 | | — |
| buckets configured on more than one use case (STOP if > 0; owner decision per bucket) | 19 | | |
| tagged buckets configured on no use case **without** a `dda-portal:usecase-id` claim (opt-in pool) | 19, 30 | | |
| tagged buckets configured on no use case **with** a claim (claimed use case exists / no longer exists) | 19, 30 | | |
| use cases with a separate Data Account (`data_account_id != account_id`) | 9, 11, 12, 18, 27 | | — |
| `simulate-principal-policy`: UseCases role → `sts:AssumeRole` on each Data Account's `DDAPortalDataAccessRole` (allowed / denied) | 11, 18, 23 | | — |
| `simulate-principal-policy`: DataManagement role → same (allowed / denied) | 11, 27 | | — |
| imported models (`source == 'imported'`) with `artifact_s3` outside their use case's set | 1, 7 | | |
| vLLM records (`source == 'vllm'`) with `model_source.s3_model_artifact` outside the set | 28 | | |
| pre-labeled datasets with `manifest_s3_uri` outside the set | 2 | | |
| labeling jobs with stored `s3://` few-shot references outside the set | 17, 24 | | |
| labeling jobs with at least one bare-key few-shot reference (expected 0) | 17 | | — |
| use cases whose `owner` holds no global PortalAdmin registry row (self-created by a non-administrator; informational — the next such creation needs the administrator pre-step) | 33 | | — |

Owner decisions recorded here (from the STOP questions, if any were raised):
- Shared configured buckets: count found —; kept as legitimately shared —; corrected (use case re-pointed or deleted through the deployed handler as `ucb-migration`) —.
- Unreadable configured buckets: count found —; disposition —.
- Portal-owned configured buckets: count found —; corrected (re-pointed) — (must equal the count found).
- Separate-Data-Account use cases exist AND the simulation says denied — acknowledged in the design (fail closed, rows 11/12/18/23); count —.

Breaking-change answer carried to the CHANGELOG: yes, for design §16 rows 1–6, 8–19, 22–30 and 33 (self-service use-case creation by a non-PortalAdmin needs an administrator-tagged bucket first); each with a remedy; rows 31–32 informational; counts above.

## Runbook 11.2 — CDK diff review (to be filled)

| Item | Expected | Observed |
|---|---|---|
| changed IAM policy resources | 1 (UseCases role default policy; three added statements, shape per task 2.1) | |
| other IAM changes | none | |
| replaced resources | `SharedLayer` version only | |
| destroyed resources | none (old layer version removal only) | |
| new resources | none | |
| functions with code changes | the eight handler/worker functions whose source changed; layer reference change on every `sharedLayer` function | |
| environment changes | none | |

## Runbook 11.3 — deploy (to be filled)

Deployment start / end (UTC): —. Account: AWS-operated portal account. Enforcement value passed to the deploy: — (warning printed: yes / no). `EdgeCVPortalComputeStack` status after: —; failed/rollback events: —. Live UseCases role carries the three statements: —. `deploy-frontend.sh` exit / invalidation issued: —. Rollback performed: no / yes (reason).

## Runbook 11.4 — post-deploy verification and migration writes (to be filled)

Method: direct invokes of the deployed handlers with synthesized API Gateway events carrying three throwaway identities with registry rows for the duration of the runbook — `ucb-migration` (global PortalAdmin; performs every PortalAdmin step and every migration write, so no audit row attributes automation to a person), `ucb-verify-ds` (DataScientist on the verification use case) and `ucb-verify-viewer` (Viewer on it) — all removed at teardown; verification use case `ucb-verify-<ts>` created and deleted within the step; verification buckets `ucb-verify-<ts>` (still claimed for the deleted verification use case by its `dda-portal:usecase-id` tag — expected, design §16 row 30) and `ucb-verify-<ts>-b` left in place (no delete grant), to be removed manually.

| # | Check | Expected | Observed |
|---|---|---|---|
| 1 | PortalAdmin `POST /s3-buckets` (no `usecase_id`), then `POST /usecases` with it as `s3_bucket` | 201 `portal_created`, `abac: enabled`; tags and ABAC visible via `s3api`; use case 201 | |
| 2 | PortalAdmin `POST /s3-buckets` with `usecase_id` | 201 `added_to_usecase: true`; listed in `allowed_s3_buckets`; audit `s3_bucket_allowed` `source: auto_add` | |
| 3 | DataScientist `POST /models/validate`: configured bucket / portal artifacts bucket / component bucket / other bucket | 400 `valid: false` "not found" (download attempted, not `bucket_not_allowed`) / 400 `bucket_not_allowed` portal remedy / same / 400 `bucket_not_allowed` + one `s3_bucket_refused` row (the evidence); CloudTrail `AssumeRole` look-up recorded as informative only (single-account: no STS call either way) | |
| 4 | Viewer `POST /models/validate`; Viewer `POST /models/inspect` | 403; 403 | |
| 5 | Viewer `POST /labeling` (DDA); DataScientist with bare-key ref; with foreign `s3://` ref | 403, no job; 400 `invalid_s3_uri`; 400 `bucket_not_allowed` | |
| 6 | DataScientist transform with foreign output; pre-labeled validate with foreign bucket; Viewer on it | 400 / 400 / 403 | |
| 7 | DataScientist `GET .../data/folders?bucket=` other / configured | 400 `bucket_not_allowed` / 200 | |
| 8 | DataScientist add → 403; PortalAdmin add untagged → `no_tags`; PortalAdmin add portal artifacts bucket → `portal_bucket`; add present → `already_present`; remove → `removed`; `data_s3_bucket: ''` → 200, no `GetBucketTagging` | as listed | |
| 9 | UseCases function log stream | `shared layer botocore 1.43.101` | |
| 10 | CloudFront `index.html` bundle | new hash; bundle contains "Allow for this use case" | |
| 11 | DataScientist `POST .../data/upload-url` on the configured bucket, `curl -T` to the URL, `head-object`, folder listing | 200 / HTTP 200 / object present / listed (STOP on a non-200 upload) | |

Migration writes (through the deployed handlers as `ucb-migration`, and `aws s3control tag-resource --account-id …` for the tags, as approved):

| Write | Count |
|---|---|
| portal-account configured buckets tagged `dda-portal:managed=true` (11.4a; portal-owned buckets excluded — corrected in 11.1) | |
| configured buckets in other accounts left untagged (recorded; account administrator) | |
| `(use case, bucket)` pairs pre-populated into `additional_s3_buckets` via `PUT /usecases/{id}` (11.4b) | |
| pairs left ineligible (`bucket_not_eligible`, by reason) | |
| pairs deferred because the bucket is configured on another use case (owner decision; listed in the step's message, not here) | |
| pairs deferred because the bucket carries a `dda-portal:usecase-id` claim for another use case (existing / deleted; owner decision; listed in the step's message, not here) | |

Teardown: verification use case deleted —; the three identity rows deleted —; probe object removed —; `git status --porcelain` empty —.

## Runbook 11.6 — trunk (to be filled)

Rebase needed: —. Redeploy needed after rebase: —. `origin/portal/usecase-bucket-config` = —. `origin/integration/all-specs` fast-forwarded to — (= `HEAD`).
