# Implementation Plan

## Overview

This plan follows the bug-condition methodology. Any IAM policy statement in an
in-scope file (a CDK stack, a customer-run shell installer's inline JSON
heredoc, a standalone JSON policy template, or a `README_main.md` example JSON
block) that grants privileges wider than the calling code path actually uses is
the bug (`isBugCondition(X)` true). The fix **scopes / enumerates / bounds /
enforces / documents** every one of the seventeen sites — resource ARNs are
narrowed to the committed naming-convention patterns (`dda-*`, `dda/*`,
`dda-component-*`, `dda-inference-results-*`, `sagemaker-*`) or gated by an
`aws:ResourceTag/dda-portal:managed=true` `Condition`; `service:*` action
wildcards (`greengrass:*`, `greengrassv2:*`, `iot:*`, `s3:*`) are replaced by
the enumerable subset the code path exercises; `sts:AssumeRole` is bounded to
`props.trustedUseCaseAccountIds`; every remaining wildcard is isolated into
its own statement with an in-file documented exception — while preserving
behavior for every legitimate portal / cross-account / edge-device /
build-server / Ground Truth flow byte-for-byte (`F(X) = F'(X)`).

- **Property 1: Fix Checking** — for all inputs where `isBugCondition` is true,
  the fixed code narrows the granted `(Action, Resource)` set (scoped ARNs,
  enumerated actions, bounded accounts, tag conditions enforced, documented
  exceptions on the unscopable subset) and the repo audit returns zero
  disallowed hits (Requirements 2.1–2.18).
- **Property 2: Preservation** — for all inputs where `isBugCondition` is false,
  `F(X) = F'(X)` — every legitimate flow succeeds and every statement / sid
  that is NOT one of I1–I17 is byte-for-byte identical (verified by `cdk synth`
  snapshot diff for I1–I6 and JSON-AST comparison for I7–I17)
  (Requirements 3.1–3.18).

Finding traceability to the scan (real source paths only; the
`edge-cv-portal/infrastructure/cdk.out/asset.*` copies and any vendored
duplicate under a build tree are generated / vendored and out of scope):

- **I1** `edge-cv-portal/infrastructure/lib/compute-stack.ts:~146` — portal Lambda combined SageMaker/Greengrass/IoT/logs/STS/API-Gateway/S3-CORS `PolicyStatement` on `resources: ['*']` (+ sibling `iam:PassRole` on `*`)
- **I2** `edge-cv-portal/infrastructure/lib/compute-stack.ts:~183` — portal Lambda S3 grant on `resources: ['*']` in the portal account
- **I3** `edge-cv-portal/infrastructure/lib/usecase-account-stack.ts:~197` — Ground Truth `DDASageMakerExecutionRole` S3 grant on `arn:aws:s3:::*` / `.../*` with no tag condition
- **I4** `edge-cv-portal/infrastructure/lib/usecase-account-stack.ts:~425` — `DDAPortalAccessRole` `S3BucketAccess` + `S3ObjectAccess` sids with the tag `Condition` promised by the comment but not wired up
- **I5** `edge-cv-portal/infrastructure/lib/labeling-workflow-stack.ts:~44` — `LabelingMonitorFunction` `sts:AssumeRole` on `arn:aws:iam::*:role/DDAPortalAccessRole` (wildcard account)
- **I6** `edge-cv-portal/infrastructure/lib/training-workflow-stack.ts:~67` — `assumeRoleFunction` `sts:AssumeRole` on `resources: ['*']`
- **I7** `edge-cv-portal/deploy-account-role.sh:~211` — `S3_POLICY` first statement on `arn:aws:s3:::*` / `.../*`
- **I8** `edge-cv-portal/deploy-account-role.sh:~281` — `SAGEMAKER_POLICY` statement on `"Resource": "*"`
- **I9** `edge-cv-portal/deploy-account-role.sh:~378` — `DDAPortalComponentAccessPolicy` `AllowDDABucketPatternAccess` sid with substring-match `arn:aws:s3:::*-dda-*` entries
- **I10** `station_install/create-edge-device-iam-role.sh:~114` — `GreengrassPermissions` sid `greengrass:*` / `greengrassv2:*` on `"Resource": "*"`
- **I11** `station_install/create-edge-device-iam-role.sh:~122` — `IoTPermissions` sid `iot:*` on `"Resource": "*"`
- **I12** `station_install/create-edge-device-iam-role.sh:~130` — `S3Permissions` sid on `"Resource": "*"`
- **I13** `edge-cv-portal/launch-arm64-build-server.sh:~155` — build-server `IoTPermissions` sid `iot:*` on `"Resource": "*"`
- **I14** `edge-cv-portal/launch-arm64-build-server.sh:~161` — build-server `S3Permissions` sid on `"Resource": "*"` (incl. `s3:ListAllMyBuckets`)
- **I15** `station_install/edge-device-iam-policy.json:~34` — `IoTDataPlane` sid (`Connect`/`Publish`/`Subscribe`/`Receive`) on `"Resource": "*"`
- **I16** `README_main.md:~230` — `dda-build-policy` example JSON with `greengrass:*`/`iot:*`/S3 on `"Resource": "*"`
- **I17** `README_main.md:~256` — `dda-greengrass-policy` example JSON with `greengrass:*`/`iot:*` and S3 `arn:aws:s3:::*` wildcards
- **I18** repo-audit gate (Req 2.18) — `iam_audit.py`

## Tasks

- [ ] 1. Write bug-condition exploration test (IAM audit + targeted CDK / JSON-AST / README inspections)
  - **Property 1: Bug Condition** - An IAM policy statement in an in-scope file grants privileges wider than the calling code path uses — a scopable action on `resources: ['*']` / `"Resource": "*"`, a `service:*` action wildcard over an enumerable subset, `sts:AssumeRole` on `resources: ['*']` / `arn:aws:iam::*:role/...` (wildcard account), or a tag-based restriction declared in a code comment but not enforced by a `Condition` — across seventeen in-scope sites
  - **CRITICAL**: This test MUST FAIL (surface non-empty hits / observe the wildcard at each site) on the unfixed tree - the hits ARE the counterexamples that confirm the bug exists
  - **DO NOT attempt to fix any infrastructure source code in this task** - this task only writes tests and documents the counterexamples
  - **NOTE**: This same audit + targeted inspection set becomes the fix-checking assertion in task 8 (it must return zero disallowed hits / observe scoped resources at every site after the fix)
  - **GOAL**: Enumerate every bug-condition site and demonstrate each wildcard / service-wildcard / wildcard-account / unenforced-tag pattern so the fix scope is grounded in real code
  - **Scoped PBT Approach**: the audit is deterministic (scope it to a concrete, reproducible grep over the twelve in-scope files); the targeted tests use Hypothesis (already vendored under `.hypothesis/`) where the input domain is generatable — I1 DDA-vs-non-DDA resource ARNs; I3/I4 tagged-vs-untagged bucket states; I5/I6 trusted-vs-untrusted account IDs — scoped to concrete failing shapes for reproducibility
  - **Companion audit module (Req 2.18 / I18)** — create `test/backend-test/security/iam_audit.py` mirroring the sibling `repo_audit.py` / `secrets_audit.py` two-layer shape:
    - Import the proven low-level helpers from the sibling modules where sensible — `REPO_ROOT`, `EXCLUDE_DIRS`, `EXCLUDED_PATH_SUBSTRING`, `Hit`, `_grep`, `_parse_line`, `_has_nosem`, `_is_comment_line`, `_is_in_scope` (or a thin re-implementation if import coupling is undesirable)
    - Define this spec's OWN `AUDIT_PATTERNS`, `IN_SCOPE_FILES`, and a precise `_is_disallowed` for the four categories, scoped to `IN_SCOPE_FILES` = `edge-cv-portal/infrastructure/lib/compute-stack.ts`, `edge-cv-portal/infrastructure/lib/usecase-account-stack.ts`, `edge-cv-portal/infrastructure/lib/labeling-workflow-stack.ts`, `edge-cv-portal/infrastructure/lib/training-workflow-stack.ts`, `edge-cv-portal/deploy-account-role.sh`, `station_install/create-edge-device-iam-role.sh`, `edge-cv-portal/launch-arm64-build-server.sh`, `station_install/edge-device-iam-policy.json`, `README_main.md`
    - `cdk_wildcard_resource` — a `new iam.PolicyStatement(` block whose `resources:\s*\[\s*['\"]\*['\"]` sits alongside an `actions:` list intersecting the scopable set; disallowed unless the actions are exclusively in the documented unscopable set (`sagemaker:ListWorkteams`, `sts:GetCallerIdentity`, `cloudwatch:PutMetricData`, `logs:DescribeLogGroups`, `ecr:GetAuthorizationToken`, `tag:GetResources`, `resourcegroupstaggingapi:GetResources`, `iot:DescribeEndpoint`, `s3:ListAllMyBuckets`) OR a `// nosec: iam-resource-wildcard — reason` marker sits immediately above the `resources:` line
    - `json_resource_wildcard` — in the in-scope JSON code (extracted heredocs from `deploy-account-role.sh`, `create-edge-device-iam-role.sh`, `launch-arm64-build-server.sh`, the standalone `edge-device-iam-policy.json`, and README JSON code fences), a `Statement[]` element whose `.Resource == "*"` or `.Resource | any(contains("arn:aws:s3:::*"))` / `arn:aws:iam::*:role/` AND whose `.Action` overlaps a scopable action; documented exception via a `# nosec` shell comment or a `"//": "unscopable — reason"` sibling key
    - `service_wildcard_action` — `['\"](greengrass|greengrassv2|iot|s3):\*['\"]` in `Action` / `actions` lists; disallowed unconditionally in in-scope files
    - `assume_role_wildcard_account` — `sts:AssumeRole` in `actions`/`Action` correlated with `arn:aws:iam::\\*:role/` or `resources:\s*\[\s*['\"]\*['\"]` / `"Resource": "*"` in the same block; disallowed unless a `// nosec: assume-role-account-wildcard — reason` marker is present
    - (Enumeration-only) `substring_bucket_pattern` — `arn:aws:s3:::\*-dda-\*` in in-scope files (surfaced by the raw `run_audit()` for I9; the precise gate collapses this into `json_resource_wildcard`)
    - (Enumeration-only) `unenforced_tag_condition` — a `PolicyStatement` block whose two-lines-up comment contains `dda-portal:managed` but whose block does not contain `conditions:` (heuristic, exploration-only)
    - Provide a raw `run_audit()` (broad enumeration, non-empty on unfixed tree, used by the exploration test) and a precise `disallowed_hits()` (zero after fix, minus documented exceptions)
    - Scope exclusions: `edge-cv-portal/infrastructure/cdk.out/asset.*`, any vendored duplicate of the shell installers under a build tree, and the security test files' own pattern strings
  - **Targeted exploration tests** — create `test/backend-test/security/test_iam_bug_condition_exploration.py`:
    - **I1**: parse `compute-stack.ts` and locate the combined `createLambdaRole(...)` `PolicyStatement`; assert on unfixed code that `resources: ['*']` sits alongside scopable actions (`sagemaker:CreateTrainingJob`, `greengrass:CreateComponentVersion`, `iot:UpdateThingShadow`, `execute-api:Invoke`, …) in the same block (counterexample); assert the sibling `iam:PassRole` block has `resources: ['*']`
    - **I2**: assert the second `PolicyStatement` at `~183` has `resources: ['*']` with a scopable S3 action list and NO tag `conditions:` block
    - **I3**: assert the `groundTruthRole.addToPolicy(...)` block at `~197` has `resources: ['arn:aws:s3:::*', 'arn:aws:s3:::*/*']` AND NO `conditions:` block (counterexample)
    - **I4**: parse the `S3BucketAccess` and `S3ObjectAccess` sids in `usecase-account-stack.ts` and assert on unfixed code that neither has a `conditions:` block despite the two-lines-up comment declaring `"Buckets must be tagged with 'dda-portal:managed' = 'true'"` (counterexample)
    - **I5**: assert `labeling-workflow-stack.ts:~44` grants `sts:AssumeRole` on `arn:aws:iam::*:role/DDAPortalAccessRole` (wildcard account)
    - **I6**: assert `training-workflow-stack.ts:~67` grants `sts:AssumeRole` on `resources: ['*']`
    - **I7 / I8 / I9**: `jq`-extract each inline heredoc from `deploy-account-role.sh` and assert the `S3_POLICY` first statement's `Resource` is `["arn:aws:s3:::*", "arn:aws:s3:::*/*"]` (I7); the `SAGEMAKER_POLICY` statement's `Resource` is `"*"` (I8); the `AllowDDABucketPatternAccess` sid's `Resource` list contains `arn:aws:s3:::*-dda-*` (I9)
    - **I10 / I11 / I12**: `jq`-extract `create-edge-device-iam-role.sh`'s `INLINE_POLICY` heredoc and assert `GreengrassPermissions.Action` contains `greengrass:*` and `greengrassv2:*` (I10); `IoTPermissions.Action` contains `iot:*` (I11); `S3Permissions.Resource == "*"` (I12)
    - **I13 / I14**: `jq`-extract `launch-arm64-build-server.sh`'s inline `put-role-policy` payload and assert `IoTPermissions.Action` contains `iot:*` (I13); `S3Permissions.Resource == "*"` and its `Action` list contains `s3:ListAllMyBuckets` bundled with scopable actions (I14)
    - **I15**: read `station_install/edge-device-iam-policy.json` and assert the `IoTDataPlane` sid's `Resource == "*"` (counterexample)
    - **I16 / I17**: extract the `dda-build-policy` and `dda-greengrass-policy` JSON code fences from `README_main.md` and assert each contains `greengrass:*` / `iot:*` / an S3 statement on `arn:aws:s3:::*` or `"Resource": "*"` (counterexample)
  - Run the audit and targeted tests on the UNFIXED tree
  - **EXPECTED OUTCOME**: `run_audit()` returns NON-EMPTY hits across all four categories (I1–I17) AND every targeted test surfaces its counterexample (wildcard resource / service-wildcard action / wildcard-account `sts:AssumeRole` / missing tag `Condition`) - this is correct, it proves the bug exists
  - Document the counterexamples found per finding (e.g. `compute-stack.ts` combined statement has `resources: ['*']` with `sagemaker:CreateTrainingJob` and `iot:UpdateThingShadow` in the same block; `usecase-account-stack.ts` `S3BucketAccess` has no `conditions:` despite the tag comment; `labeling-workflow-stack.ts` `sts:AssumeRole` resource is `arn:aws:iam::*:role/DDAPortalAccessRole`; `create-edge-device-iam-role.sh` `IoTPermissions.Action == ["iot:*"]`; `edge-device-iam-policy.json` `IoTDataPlane.Resource == "*"`; README `dda-build-policy` JSON contains `iot:*`)
  - Mark task complete when the audit + targeted tests are written, run, and the counterexamples are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10, 1.11, 1.12, 1.13, 1.14, 1.15, 1.16, 1.17, 1.18_


- [ ] 2. Write preservation baseline tests on the UNFIXED code (BEFORE implementing any fix)
  - **Property 2: Preservation** - No behavior change for legitimate (non-bug-condition) inputs
  - **IMPORTANT**: Follow observation-first methodology - capture `F(X)` baselines on the UNFIXED tree, then (in task 9) assert the fixed code `F'(X)` matches exactly
  - **Emphasize property-based tests** (Hypothesis, already vendored under `.hypothesis/`) wherever the input domain is generatable — I1 DDA-vs-non-DDA ARNs, I3/I4 tagged-vs-untagged buckets, I5/I6 trusted-vs-untrusted accounts, IoT enumerable action subset; place the tests under `test/backend-test/security/preservation/` alongside the sibling suite
  - Observe and record baselines on unfixed code:
    - **CDK synth baseline (I1–I6)** — `cd edge-cv-portal/infrastructure && npm ci && npx cdk synth --all --context portalAccountId=111111111111 --context externalId=fixture-eid --context trustedUseCaseAccountIds=222222222222,333333333333` in region `us-east-1` on the pre-fix commit; copy `cdk.out/EdgeCVPortalComputeStack.template.json`, `cdk.out/DDAPortalUseCaseAccountStack.template.json`, `cdk.out/LabelingWorkflowStack.template.json`, and `cdk.out/TrainingWorkflowStack.template.json` (or their nested-stack equivalents) to `test/backend-test/security/baselines/iam_baseline_<stack>.json`; the fixture context makes the diff deterministic (Req 3.1, 3.2, 3.3, 3.4, 3.5, 3.6)
    - **Shell heredoc JSON baseline (I7–I14)** — `jq`-extract each inline heredoc (`S3_POLICY`, `SAGEMAKER_POLICY`, `LOGS_POLICY`, `PASS_ROLE_POLICY`, `ECR_POLICY`, `GREENGRASS_POLICY` from `deploy-account-role.sh`; `INLINE_POLICY` from `create-edge-device-iam-role.sh`; the inline `put-role-policy` payload from `launch-arm64-build-server.sh`) and record the exact `Statement[]` list per file; capture as JSON golden files under `test/backend-test/security/baselines/iam_baseline_heredoc_<file>_<policy>.json` (Req 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14)
    - **JSON template baseline (I15)** — record the exact bytes of `station_install/edge-device-iam-policy.json`, in particular the four sids (`GreengrassComponentDownload`, `CloudWatchLogsUpload`, `AssumeDataAccountRole`, `GreengrassConnectivity`) that are NOT modified by I15; capture as `test/backend-test/security/baselines/iam_baseline_edge-device-iam-policy.json` (Req 3.15)
    - **README prose baseline (I16, I17)** — record the exact bytes of `README_main.md` with the two `dda-build-policy` and `dda-greengrass-policy` JSON code fences excised (the prose that must remain byte-for-byte identical); capture as `test/backend-test/security/baselines/iam_baseline_readme_prose.md` (Req 3.16, 3.17)
    - **Out-of-scope guard** — record the exact bytes of `cdk.out/asset.*`, any vendored duplicate under a build tree, and the files owned by the sibling remediation batches (`security-injection-deserialization-fixes` findings #1–#8; `security-secrets-credentials-jwt-fixes` findings S1–S9) so task 9 can assert they are unchanged (Req 3.18)
  - Write tests that assert the recorded baselines. Use **property-based tests** where the domain is generatable (per the design's Testing Strategy):
    - **PBT 1 — DDA vs non-DDA resource ARNs (I1, I7–I14)**: generate `(action, resource_arn)` pairs where `action` is drawn from the fixed policy's action set and `resource_arn` is either (a) a `dda-*`/`dda/*`/`sagemaker-*`/`dda-component-*`/`dda-inference-results-*` ARN following the committed naming conventions or (b) a random non-DDA ARN; invariant on the UNFIXED tree — both (a) and (b) are in the allow-set of the wildcard policy (the baseline the fixed policy must preserve for (a) only); capture the union of (Action, Resource) pairs the portal Lambdas / installers actually exercise at runtime against DDA-prefixed resources
    - **PBT 2 — tagged vs untagged buckets (I3, I4)**: generate bucket states `{ arn, tags: {"dda-portal:managed": <maybe>} }`; invariant on the UNFIXED tree — every bucket is allowed regardless of tag (the pre-fix behavior); record which bucket states the Ground Truth role and `DDAPortalAccessRole` actually access in the current portal flows so task 9 can assert the tagged subset is preserved
    - **PBT 3 — trusted vs untrusted account IDs (I5, I6)**: generate a `trusted = {A1, A2, ...}` set and a `probe` account drawn either from `trusted` or its complement; invariant on the UNFIXED tree — every `probe` succeeds against the wildcard `sts:AssumeRole` (the pre-fix behavior); record the `LabelingMonitorFunction`'s and `assumeRoleFunction`'s successful cross-account calls against known-trusted accounts so task 9 can assert those still succeed
    - **PBT 4 — IoT enumerable action subset (I11, I13, I15)**: generate IoT actions from the full IoT API set split by whether the action is in the edge-device / build-server / data-plane exercised subset; invariant on the UNFIXED tree — every action is allowed by the `iot:*` wildcard; record the specific action list the code path exercises so task 9 can assert exactly the exercised subset is preserved
  - Run the tests on the UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this captures the baseline behavior to preserve — the four CDK stack templates, the shell heredoc JSON, the JSON template, the README prose, and the DDA-ARN / tagged-bucket / trusted-account / IoT-subset allow-sets)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15, 3.16, 3.17, 3.18_


- [ ] 3. Wave 1 — README example policies FIRST (I16, I17) — documentation-only, zero runtime blast radius
  - **Property 1: Fix Checking** - The two README example JSONs match the narrowed shape used by the shell installers; the surrounding prose is byte-for-byte identical

  - [ ] 3.1 I16 — `README_main.md` `dda-build-policy` example (~line 230)
    - Replace the JSON body inside the `dda-build-policy` code fence with the same narrowed shape used by `launch-arm64-build-server.sh` after I13 / I14: `IoTPermissions` split by resource type (thing ops on `arn:aws:iot:*:*:thing/dda-*`, `iot:DescribeJob` on `arn:aws:iot:*:*:job/*`, `iot:DescribeEndpoint` isolated on `"*"` with a JSON-adjacent comment); specific Greengrass v2 edge-device actions on the broad service ARN; `S3Permissions` scoped to `arn:aws:s3:::dda-component-*`, `arn:aws:s3:::dda-component-*/*`, `arn:aws:s3:::dda-inference-results-*`, `arn:aws:s3:::dda-inference-results-*/*`; `s3:ListAllMyBuckets` in its own statement with `"Resource": "*"` and a `"//": "unscopable — reason"` sibling key; preserve the `EC2Permissions`, `CloudWatchLogsPermissions`, `CloudWatchMetricsPermissions`, and `ECRPermissions` sids the installer emits
    - Preserve the surrounding prose byte-for-byte — the paragraph describing the policy purpose, the "replace `[AWS account id]` with your account ID" instruction, and the numbered step context (`1.` heading, follow-up `2.` / `3.` steps)
    - _Bug_Condition: isBugCondition(X) where X = README example `dda-build-policy` JSON with `greengrass:*` / `iot:*` / S3 on `"Resource": "*"` (I16)_
    - _Expected_Behavior: the JSON body matches the narrowed shape from I13 / I14 (specific IoT / Greengrass v2 actions, S3 scoped to `dda-component-*` and `dda-inference-results-*`, `s3:ListAllMyBuckets` isolated with a documented comment)_
    - _Preservation: the surrounding README prose is byte-for-byte identical; only the JSON code fence content changes (Req 3.16)_
    - _Requirements: 2.16_

  - [ ] 3.2 I17 — `README_main.md` `dda-greengrass-policy` example (~line 256)
    - Replace the JSON body inside the `dda-greengrass-policy` code fence with the same narrowed shape used by `create-edge-device-iam-role.sh` after I10 / I11 / I12 and by `edge-device-iam-policy.json` after I15: specific Greengrass v2 edge-device actions (`GetComponentVersionArtifact`, `ResolveComponentCandidates`, `GetDeployment`, `GetCoreDevice`, `UpdateConnectivityInfo`, `ListComponents`, …) on the broad service ARN; IoT actions split by resource type (`iot:Connect` on `arn:aws:iot:*:*:client/dda-*`; `iot:Publish` / `Subscribe` / `Receive` on `arn:aws:iot:*:*:topic/dda/*` / `topicfilter/dda/*`; thing-shadow + `DescribeThing` on `arn:aws:iot:*:*:thing/dda-*`; `iot:DescribeEndpoint` isolated on `"*"`); S3 scoped to `arn:aws:s3:::dda-component-*`, `arn:aws:s3:::dda-component-*/*`, `arn:aws:s3:::dda-inference-results-*`, `arn:aws:s3:::dda-inference-results-*/*`; keep the CloudWatch Logs statement on `arn:aws:logs:*:*:*` as-is
    - Preserve the surrounding prose byte-for-byte — the paragraph describing when to attach the policy and the "Attach S3 permissions for component downloads" note
    - _Bug_Condition: isBugCondition(X) where X = README example `dda-greengrass-policy` JSON with `greengrass:*`, S3 `arn:aws:s3:::*` / `arn:aws:s3:::*/*` wildcards, and multiple IoT actions on `"Resource": "*"` (I17)_
    - _Expected_Behavior: the JSON body matches the narrowed shape from I10 / I11 / I12 / I15 (specific Greengrass v2 actions; IoT scoped by resource type against `dda-*` clients / things and `dda/*` topics; S3 scoped to `dda-component-*` and `dda-inference-results-*`)_
    - _Preservation: the surrounding README prose is byte-for-byte identical; only the JSON code fence content changes (Req 3.17)_
    - _Requirements: 2.17_


- [ ] 4. Wave 2 — Standalone JSON policy template (I15) — applied at edge-device provisioning time
  - **Property 1: Fix Checking** - The `IoTDataPlane` sid is split by IoT resource type; the four preserved sids are byte-for-byte identical

  - [ ] 4.1 I15 — `station_install/edge-device-iam-policy.json` `IoTDataPlane` sid (~line 34)
    - Replace the single `IoTDataPlane` sid with four resource-type-split statements (or one statement with four `(Action, Resource)` correlations; the design's preferred readable shape is four separate sids): `iot:Connect` on `arn:aws:iot:*:*:client/dda-*`; `iot:Publish` on `arn:aws:iot:*:*:topic/dda/*`; `iot:Subscribe` on `arn:aws:iot:*:*:topicfilter/dda/*`; `iot:Receive` on `arn:aws:iot:*:*:topic/dda/*`
    - Preserve the `GreengrassComponentDownload`, `CloudWatchLogsUpload`, `AssumeDataAccountRole`, and `GreengrassConnectivity` sids byte-for-byte
    - The impact radius is future edge-device provisions only; existing devices are unaffected until they re-run `attach-role-policy`
    - _Bug_Condition: isBugCondition(X) where X = `IoTDataPlane` sid granting `iot:Connect` / `Publish` / `Subscribe` / `Receive` on `"Resource": "*"` (I15)_
    - _Expected_Behavior: the sid is split by IoT resource type: `Connect` on `client/dda-*`, `Publish` / `Receive` on `topic/dda/*`, `Subscribe` on `topicfilter/dda/*`_
    - _Preservation: the four other sids (`GreengrassComponentDownload`, `CloudWatchLogsUpload`, `AssumeDataAccountRole`, `GreengrassConnectivity`) are byte-for-byte identical against the baseline JSON (Req 3.15)_
    - _Requirements: 2.15_


- [ ] 5. Wave 3 — Shell installers (I7, I8, I9, I10, I11, I12, I13, I14) — inline JSON heredocs in customer-run scripts
  - **Property 1: Fix Checking** - Every modified heredoc's resource ARNs match the naming-convention patterns and every remaining wildcard is isolated with a documented comment; every non-modified sid / heredoc is byte-for-byte identical

  - [ ] 5.1 I7 — `edge-cv-portal/deploy-account-role.sh` `S3_POLICY` first statement (~line 211)
    - Narrow the first statement's `Resource` list from `["arn:aws:s3:::*", "arn:aws:s3:::*/*"]` to `["arn:aws:s3:::dda-*", "arn:aws:s3:::dda-*/*", "arn:aws:s3:::sagemaker-*", "arn:aws:s3:::sagemaker-*/*"]`
    - Preserve the second statement (the already-scoped `sagemaker-*` read allowlist) byte-for-byte, and the surrounding `LOGS_POLICY`, `SAGEMAKER_POLICY`, `PASS_ROLE_POLICY`, `ECR_POLICY` heredocs and the `aws iam put-role-policy` invocations
    - Add a shell comment above the heredoc recording the naming conventions and why the union of `dda-*` and `sagemaker-*` is the correct scope
    - _Bug_Condition: isBugCondition(X) where X = `S3_POLICY` first statement on `arn:aws:s3:::*` / `arn:aws:s3:::*/*` (I7)_
    - _Expected_Behavior: the `Resource` list is the union of `dda-*` and `sagemaker-*` prefixes at both bucket and object levels_
    - _Preservation: the second (already-scoped) `sagemaker-*` statement and the other heredocs (`LOGS_POLICY`, `SAGEMAKER_POLICY`, `PASS_ROLE_POLICY`, `ECR_POLICY`) are byte-for-byte identical (Req 3.7)_
    - _Requirements: 2.7_

  - [ ] 5.2 I8 — `edge-cv-portal/deploy-account-role.sh` `SAGEMAKER_POLICY` (~line 281)
    - Narrow the first statement's `Resource` from `"*"` to `["arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:training-job/dda-*", "arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:compilation-job/dda-*", "arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:labeling-job/dda-*", "arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:model/dda-*"]`
    - Preserve the sibling `iam:PassRole` / `iam:GetRole` statement byte-for-byte (already scoped to `arn:aws:iam::*:role/DDASageMakerExecutionRole`)
    - `sagemaker:ListWorkteams` is NOT in this heredoc's action list, so no unscopable-action isolation is required
    - _Bug_Condition: isBugCondition(X) where X = `SAGEMAKER_POLICY` statement on `"Resource": "*"` (I8)_
    - _Expected_Behavior: the `Resource` is scoped to `dda-*`-prefixed training / compilation / labeling / model ARNs in the current account_
    - _Preservation: the sibling `iam:PassRole` / `iam:GetRole` statement is byte-for-byte identical (Req 3.8)_
    - _Requirements: 2.8_

  - [ ] 5.3 I9 — `edge-cv-portal/deploy-account-role.sh` `GREENGRASS_POLICY` `AllowDDABucketPatternAccess` sid (~line 378)
    - Narrow the `Resource` list from `["arn:aws:s3:::dda-*", "arn:aws:s3:::dda-*/*", "arn:aws:s3:::*-dda-*", "arn:aws:s3:::*-dda-*/*"]` to `["arn:aws:s3:::dda-*", "arn:aws:s3:::dda-*/*"]` — remove the two substring-match entries
    - Preserve the `AllowPortalComponentBucketAccess`, `AllowInferenceResultsUpload`, `AllowEcrAuthToken`, and `AllowEcrImagePull` sids byte-for-byte
    - _Bug_Condition: isBugCondition(X) where X = `AllowDDABucketPatternAccess` sid `Resource` list containing `arn:aws:s3:::*-dda-*` substring-match entries (I9)_
    - _Expected_Behavior: only the exact `dda-*` prefix ARNs remain at bucket and object levels; substring matches like `my-dda-random`, `foo-dda-bar` no longer match_
    - _Preservation: the `AllowPortalComponentBucketAccess`, `AllowInferenceResultsUpload`, `AllowEcrAuthToken`, `AllowEcrImagePull` sids are byte-for-byte identical (Req 3.9)_
    - _Requirements: 2.9_

  - [ ] 5.4 I10 — `station_install/create-edge-device-iam-role.sh` `GreengrassPermissions` sid (~line 114)
    - Replace `["greengrass:*", "greengrassv2:*"]` with the enumerable subset the edge device uses: `greengrass:GetComponentVersionArtifact`, `greengrass:ResolveComponentCandidates`, `greengrass:GetDeploymentConfiguration`, `greengrassv2:GetDeployment`, `greengrassv2:GetCoreDevice`, `greengrassv2:UpdateConnectivityInfo`, `greengrassv2:ListComponents`, `greengrassv2:GetComponentVersionArtifact`, `greengrassv2:ResolveComponentCandidates`
    - Keep `"Resource": "*"` with an adjacent shell comment recording that Greengrass v1 API resource support is limited and action-scoping is the primary defense on customer edge devices
    - Preserve the sibling sids (`IoTPermissions` — modified in 5.5; `S3Permissions` — modified in 5.6; `CloudWatchLogsPermissions`, `CloudWatchMetricsPermissions`, `ECRPermissions`, `STSPermissions`) byte-for-byte
    - _Bug_Condition: isBugCondition(X) where X = `GreengrassPermissions` sid granting `greengrass:*` / `greengrassv2:*` on `"Resource": "*"` (I10)_
    - _Expected_Behavior: the `Action` list is the enumerable subset the edge device exercises; `Resource` stays `"*"` with a documented shell comment_
    - _Preservation: the sibling sids and the other heredocs are byte-for-byte identical (Req 3.10)_
    - _Requirements: 2.10_

  - [ ] 5.5 I11 — `station_install/create-edge-device-iam-role.sh` `IoTPermissions` sid (~line 122)
    - Split into four statements and drop the `iot:*` wildcard: (a) `iot:Connect` on `arn:aws:iot:*:*:client/dda-*`; (b) `iot:Publish`, `iot:Subscribe`, `iot:Receive` on `["arn:aws:iot:*:*:topic/dda/*", "arn:aws:iot:*:*:topicfilter/dda/*"]`; (c) `iot:GetThingShadow`, `iot:UpdateThingShadow`, `iot:DescribeThing` on `arn:aws:iot:*:*:thing/dda-*`; (d) `iot:DescribeEndpoint` on `"*"` with an adjacent shell comment recording the action is unscopable
    - Preserve the sibling sids byte-for-byte
    - _Bug_Condition: isBugCondition(X) where X = `IoTPermissions` sid granting `iot:*` on `"Resource": "*"` (I11)_
    - _Expected_Behavior: the sid is split into four resource-type-scoped statements plus an isolated `iot:DescribeEndpoint` with a documented shell comment_
    - _Preservation: the sibling sids and the other heredocs are byte-for-byte identical (Req 3.11)_
    - _Requirements: 2.11_

  - [ ] 5.6 I12 — `station_install/create-edge-device-iam-role.sh` `S3Permissions` sid (~line 130)
    - Replace `"Resource": "*"` with `["arn:aws:s3:::dda-component-*", "arn:aws:s3:::dda-component-*/*", "arn:aws:s3:::dda-inference-results-*", "arn:aws:s3:::dda-inference-results-*/*"]`
    - Preserve the `Action` list byte-for-byte (`s3:GetObject`, `PutObject`, `ListBucket`, `GetBucketLocation`, `GetBucketVersioning`, `ListBucketVersions`)
    - Preserve the sibling sids byte-for-byte
    - _Bug_Condition: isBugCondition(X) where X = `S3Permissions` sid on `"Resource": "*"` (I12)_
    - _Expected_Behavior: the `Resource` list is scoped to `dda-component-*` and `dda-inference-results-*` at both bucket and object levels_
    - _Preservation: the `Action` list is unchanged; the sibling sids and other heredocs are byte-for-byte identical (Req 3.12)_
    - _Requirements: 2.12_

  - [ ] 5.7 I13 — `edge-cv-portal/launch-arm64-build-server.sh` `IoTPermissions` sid (~line 155)
    - Split into three statements and drop the `iot:*` wildcard: (a) thing ops (`iot:DescribeThing`, `iot:CreateThing`, `iot:UpdateThingShadow`, `iot:AttachPolicy`) on `arn:aws:iot:*:*:thing/dda-*`; (b) job ops (`iot:DescribeJob`) on `arn:aws:iot:*:*:job/*`; (c) `iot:DescribeEndpoint` on `"*"` with an adjacent shell comment recording the reason
    - Preserve the sibling sids (`GreengrassPermissions` — already enumerated; `S3Permissions` — modified in 5.8; `EC2Permissions`, `CloudWatchLogsPermissions`, `CloudWatchMetricsPermissions`, `ECRPermissions`) byte-for-byte
    - _Bug_Condition: isBugCondition(X) where X = build-server `IoTPermissions` sid granting `iot:*` on `"Resource": "*"` (I13)_
    - _Expected_Behavior: the sid is split into thing ops / job ops / endpoint discovery, scoped by resource type where the action supports it_
    - _Preservation: the sibling sids and other heredocs are byte-for-byte identical (Req 3.13)_
    - _Requirements: 2.13_

  - [ ] 5.8 I14 — `edge-cv-portal/launch-arm64-build-server.sh` `S3Permissions` sid (~line 161)
    - Split into two statements: (a) scoped bucket ops (every action currently in the sid EXCEPT `s3:ListAllMyBuckets` — `CreateBucket`, `GetBucketLocation`, `PutBucketVersioning`, `GetObject`, `PutObject`, `ListBucket`, `DeleteObject`, `GetBucketVersioning`, `ListBucketVersions`, `GetBucketPolicy`, `PutBucketPolicy`, `GetBucketAcl`, `PutBucketAcl`, `GetBucketTagging`, `PutBucketTagging`) on `["arn:aws:s3:::dda-component-*", "arn:aws:s3:::dda-component-*/*", "arn:aws:s3:::dda-inference-results-*", "arn:aws:s3:::dda-inference-results-*/*"]`; (b) `s3:ListAllMyBuckets` isolated on `"Resource": "*"` with an adjacent shell comment recording the action is unscopable
    - Preserve the sibling sids byte-for-byte
    - _Bug_Condition: isBugCondition(X) where X = build-server `S3Permissions` sid with mixed scopable + unscopable actions on `"Resource": "*"` (I14)_
    - _Expected_Behavior: the sid is split into a scoped-bucket-ops statement (on the DDA prefixes) and an isolated `ListAllMyBuckets` statement with a documented shell comment_
    - _Preservation: the sibling sids and other heredocs are byte-for-byte identical (Req 3.14)_
    - _Requirements: 2.14_


- [ ] 6. Wave 4 — CDK non-`sts:AssumeRole` fixes (I1, I2, I3, I4) — synth-diff-verifiable `PolicyStatement` changes
  - **Property 1: Fix Checking** - Every modified `PolicyStatement` grants the narrowest privilege set; every non-modified statement in the four stacks is byte-for-byte identical in the emitted CloudFormation

  - [ ] 6.1 I1 — `edge-cv-portal/infrastructure/lib/compute-stack.ts` `createLambdaRole` combined statement (~line 146) + sibling `iam:PassRole`
    - Split the single combined `PolicyStatement` into eight per-service statements, dropping the umbrella `resources: ['*']`:
      1. **SageMaker (scopable)**: `CreateTrainingJob`, `DescribeTrainingJob`, `ListTrainingJobs`, `CreateCompilationJob`, `DescribeCompilationJob`, `ListCompilationJobs`, `CreateLabelingJob`, `DescribeLabelingJob`, `ListLabelingJobs`, `StopLabelingJob`, `DescribeWorkteam`, `AddTags` on `arn:aws:sagemaker:*:*:training-job/dda-*`, `compilation-job/dda-*`, `labeling-job/dda-*`, `model/dda-*`, `workteam/*`
      2. **SageMaker (unscopable)**: `sagemaker:ListWorkteams` on `resources: ['*']` with a `//` code comment recording the AWS IAM reference
      3. **Greengrass v2 (scopable)**: `CreateComponentVersion`, `DescribeComponent`, `GetComponent`, `ListComponents`, `ListComponentVersions`, `ListCoreDevices`, `GetCoreDevice`, `ListInstalledComponents`, `ListEffectiveDeployments`, `ListTagsForResource`, `TagResource`, `ListDeployments`, `GetDeployment`, `CreateDeployment`, `CancelDeployment` on `arn:aws:greengrass:*:*:components:*`, `coreDevices:*`, `deployments:*`, with a `conditions: { StringEquals: { 'aws:ResourceTag/dda-portal:managed': 'true' } }` where the Greengrass v2 API supports it (else broad service ARN + code comment)
      4. **IoT (scopable)**: `DescribeThing`, `DescribeThingGroup`, `GetThingType`, `ListThings`, `ListThingsInThingGroup`, `ListThingGroups`, `CreateThingGroup`, `AddThingToThingGroup`, `RemoveThingFromThingGroup`, `CreateJob`, `DescribeJob`, `UpdateJob`, `GetJobDocument`, `ListJobs`, `CancelJob`, `GetThingShadow`, `UpdateThingShadow`, `DeleteThingShadow` on `arn:aws:iot:*:*:thing/dda-*`, `topic/dda/*`, `job/*`, `thinggroup/*`
      5. **IoT (unscopable)**: `iot:DescribeEndpoint` on `resources: ['*']` with a `//` code comment
      6. **CloudWatch Logs read (unscopable-ish)**: `logs:GetLogEvents`, `DescribeLogStreams`, `DescribeLogGroups`, `FilterLogEvents` on `resources: ['*']` with a `//` code comment (portal queries arbitrary Greengrass log groups)
      7. **STS AssumeRole**: `sts:AssumeRole` on `arn:aws:iam::*:role/DDAPortalAccessRole` with a `//` code comment cross-referencing I5 / I6 as the places that bound the account list
      8. **execute-api:Invoke**: scoped to `props.api.arnForExecuteApi()` when statically known (else `arn:aws:execute-api:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:*/*` with a `//` code comment)
      9. **S3 CORS on portal artifacts**: `s3:GetBucketCors`, `s3:PutBucketCors` scoped to `props.portalArtifactsBucket.bucketArn`
    - Tighten the sibling `iam:PassRole` statement (currently `resources: ['*']`, ~line 158, with `iam:PassedToService=sagemaker.amazonaws.com` condition): change its resource to `arn:aws:iam::*:role/DDA*Role` while preserving the `iam:PassedToService=sagemaker.amazonaws.com` condition byte-for-byte
    - Sum the actions across the eight post-split statements and verify the total equals the pre-fix combined action list (no action is dropped)
    - Preserve every other statement in the file byte-for-byte — `useCasesHandler.addToRolePolicy` bucket-scoped grants, `devicesHandler` Secure Tunneling grant, the Cognito auth grant, `sharedComponentsHandler` grants, `packagingHandler` grants, and the `tag:GetResources` statement at ~line 195 — verified by `cdk synth` snapshot diff against `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json`
    - _Bug_Condition: isBugCondition(X) where X = combined `PolicyStatement` over SageMaker / Greengrass / IoT / logs / STS / API Gateway / S3 CORS actions on `resources: ['*']` (I1)_
    - _Expected_Behavior: eight per-service statements each with resource ARNs matching the naming-convention patterns; `ListWorkteams`, `DescribeEndpoint`, `DescribeLogGroups`-class isolated with code comments; `iam:PassRole` scoped to `arn:aws:iam::*:role/DDA*Role` with condition preserved_
    - _Preservation: every non-modified `PolicyStatement` in `compute-stack.ts` is byte-for-byte identical in the emitted CloudFormation (Req 3.1)_
    - _Requirements: 2.1_

  - [ ] 6.2 I2 — `edge-cv-portal/infrastructure/lib/compute-stack.ts` portal Lambda S3 grant (~line 183)
    - Replace `resources: ['*']` with `[props.portalArtifactsBucket.bucketArn, `${props.portalArtifactsBucket.bucketArn}/*`]` (bucket + object levels), keeping the same actions list except `s3:ListAllMyBuckets`
    - Add a tag-conditioned wildcard statement for `dda-portal:managed=true` buckets: `role.addToPolicy(new iam.PolicyStatement({ effect: iam.Effect.ALLOW, actions: ['s3:GetObject', 's3:PutObject', 's3:ListBucket', 's3:GetBucketLocation', 's3:GetBucketTagging'], resources: ['arn:aws:s3:::*'], conditions: { StringEquals: { 'aws:ResourceTag/dda-portal:managed': 'true' } } }))`
    - Isolate `s3:ListAllMyBuckets` into its own statement on `resources: ['*']` with a `//` code comment recording the action is unscopable per the AWS IAM reference
    - Update the misleading "Restricted by assumed role in UseCase Account" comment to note that access is now enforced at both ends: portal-account grant scoped to `portalArtifactsBucket` + tagged buckets; cross-account still via `DDAPortalAccessRole`
    - Preserve every other statement in the file byte-for-byte — verified by the same `cdk synth` snapshot diff as I1
    - _Bug_Condition: isBugCondition(X) where X = portal Lambda S3 grant on `resources: ['*']` in the portal account (I2)_
    - _Expected_Behavior: portal-account access scoped to `props.portalArtifactsBucket.bucketArn` + tagged buckets; `s3:ListAllMyBuckets` isolated with a documented comment_
    - _Preservation: every non-modified statement in `compute-stack.ts` is byte-for-byte identical (Req 3.2)_
    - _Requirements: 2.2_

  - [ ] 6.3 I3 — `edge-cv-portal/infrastructure/lib/usecase-account-stack.ts` Ground Truth S3 grant (~line 197)
    - Split the `groundTruthRole.addToPolicy(...)` block into two statements:
      1. **Tag-conditioned**: same actions (`s3:GetObject`, `PutObject`, `DeleteObject`, `ListBucket`, `GetBucketLocation`, `GetBucketCors`, `PutBucketCors`) on `arn:aws:s3:::*` and `arn:aws:s3:::*/*` with `conditions: { StringEquals: { 'aws:ResourceTag/dda-portal:managed': 'true' } }`; add a `//` code comment recording that Ground Truth input/output buckets MUST carry the tag
      2. **Unconditional `sagemaker-*` allowlist**: same actions on `arn:aws:s3:::sagemaker-*` and `arn:aws:s3:::sagemaker-*/*` (mirrors the `S3SageMakerAccess` sid)
    - Preserve every sibling statement byte-for-byte — the CloudWatch Logs statement, the SageMaker training/compilation/labeling statement (out of scope for this spec), the ECR statement, and the cross-account Data Account statements (`CrossAccountDataBucketRead`, `CrossAccountDataBucketWrite`) — verified by `cdk synth` snapshot diff against `test/backend-test/security/baselines/iam_baseline_DDAPortalUseCaseAccountStack.template.json`
    - _Bug_Condition: isBugCondition(X) where X = Ground Truth `DDASageMakerExecutionRole` S3 grant on `arn:aws:s3:::*` / `arn:aws:s3:::*/*` with no tag condition (I3)_
    - _Expected_Behavior: split into a tag-conditioned statement (on `dda-portal:managed=true`) and an unconditional `sagemaker-*` allowlist; code comment records the tag requirement_
    - _Preservation: every sibling statement in `usecase-account-stack.ts` is byte-for-byte identical (Req 3.3)_
    - _Requirements: 2.3_

  - [ ] 6.4 I4 — `edge-cv-portal/infrastructure/lib/usecase-account-stack.ts` `DDAPortalAccessRole` `S3BucketAccess` + `S3ObjectAccess` sids (~line 425)
    - Add `conditions: { StringEquals: { 'aws:ResourceTag/dda-portal:managed': 'true' } }` to the `S3BucketAccess` `PolicyStatement` (bucket-level actions on `arn:aws:s3:::*`); the in-code comment above the block ("Tag-based access for flexibility. Buckets must be tagged with `'dda-portal:managed' = 'true'`") remains — the comment now accurately describes the enforced behavior
    - Add the same `Condition` to the `S3ObjectAccess` `PolicyStatement` (object-level actions on `arn:aws:s3:::*/*`)
    - Preserve every sibling statement byte-for-byte — `SageMakerTraining`, `SageMakerCompilation`, `SageMakerAlgorithm`, `GroundTruthLabelingV2`, `GroundTruthWorkteams`, `ResourceTaggingAccess`, `S3SageMakerAccess`, `CloudWatchLogs`, and every other sid — verified by the same `cdk synth` snapshot diff as I3
    - **Deployment-time gate**: every currently-working legitimate customer bucket MUST carry the `dda-portal:managed=true` tag BEFORE this fix lands, or cross-account list/read/write against it will start denying; the deployment runbook must include a "tag your buckets first" step
    - _Bug_Condition: isBugCondition(X) where X = `S3BucketAccess` + `S3ObjectAccess` sids with the tag `Condition` promised by the code comment but not wired up (I4)_
    - _Expected_Behavior: both sids carry `Condition: StringEquals { 'aws:ResourceTag/dda-portal:managed': 'true' }` — the tag-based restriction the comment already promises_
    - _Preservation: every sibling sid (`S3SageMakerAccess`, `ResourceTaggingAccess`, `SageMakerTraining`, `GroundTruthLabelingV2`, …) is byte-for-byte identical (Req 3.4)_
    - _Requirements: 2.4_


- [ ] 7. Wave 5 — CDK `sts:AssumeRole` fixes (I5, I6) + audit gate (I18) LAST — highest blast radius
  - **Property 1: Fix Checking** - `sts:AssumeRole` resources reference specific account IDs from `props.trustedUseCaseAccountIds`; the audit gate returns zero disallowed hits

  - [ ] 7.1 I5 — `edge-cv-portal/infrastructure/lib/labeling-workflow-stack.ts` `LabelingMonitorFunction` `sts:AssumeRole` (~line 44)
    - Add `trustedUseCaseAccountIds: string[]` to `LabelingWorkflowStackProps` as a required prop; the constructor validates it is non-empty at synth time (empty array throws — the design DOES NOT fall back to a wildcard account); the value is sourced from CDK context (`this.node.tryGetContext('trustedUseCaseAccountIds')`) or a documented SSM parameter (`/dda-portal/trusted-usecase-account-ids`) as the default fallback
    - Replace the wildcard-account resource with the mapped list: `resources: props.trustedUseCaseAccountIds.map(id => \`arn:aws:iam::${id}:role/DDAPortalAccessRole\`)` — the role name `DDAPortalAccessRole` stays fixed; only the account portion is scoped
    - Update the app entry (`bin/*.ts`) to pass `trustedUseCaseAccountIds` to `new LabelingWorkflowStack(...)`, sourced from CDK context (`-c trustedUseCaseAccountIds=111111111111,222222222222`) or the SSM parameter at synth time
    - Preserve every sibling statement byte-for-byte — the `labelingJobsTable.grantReadWriteData`, `useCasesTable.grantReadData`, EventBridge rules, and `CfnOutput`s — verified by `cdk synth` snapshot diff against `test/backend-test/security/baselines/iam_baseline_LabelingWorkflowStack.template.json`
    - _Bug_Condition: isBugCondition(X) where X = `LabelingMonitorFunction` `sts:AssumeRole` on `arn:aws:iam::*:role/DDAPortalAccessRole` (wildcard account) (I5)_
    - _Expected_Behavior: `sts:AssumeRole` resources reference specific account IDs from `props.trustedUseCaseAccountIds` mapped to `arn:aws:iam::${id}:role/DDAPortalAccessRole`; empty list is a synth-time error_
    - _Preservation: every sibling construct in `labeling-workflow-stack.ts` is byte-for-byte identical in the emitted CloudFormation; the monitor Lambda still succeeds against accounts in the trusted list (Req 3.5)_
    - _Requirements: 2.5_

  - [ ] 7.2 I6 — `edge-cv-portal/infrastructure/lib/training-workflow-stack.ts` `assumeRoleFunction` `sts:AssumeRole` (~line 67)
    - Add `trustedUseCaseAccountIds: string[]` to `TrainingWorkflowStackProps` as a required prop (same pattern as I5)
    - Replace `resources: ['*']` with the mapped list: `resources: props.trustedUseCaseAccountIds.map(id => \`arn:aws:iam::${id}:role/DDAPortalAccessRole\`)`
    - Update the "Will be scoped to specific roles with ExternalId" comment to record that account scoping is now at synth time via the trusted list while the runtime `ExternalId` check remains in the inline handler (or remove the comment — the scoping is now in place)
    - Preserve the inline Lambda `handler(event, context)` code byte-for-byte — the `sts.assume_role(RoleArn=role_arn, RoleSessionName='EdgeCVPortalTraining', ExternalId=external_id, DurationSeconds=3600)` call is unchanged; the runtime `ExternalId` check is preserved (defense-in-depth alongside the synth-time account scoping)
    - Update the app entry (`bin/*.ts`) to pass `trustedUseCaseAccountIds` (same sourcing as I5)
    - Preserve every sibling function and Step Function state byte-for-byte — `startTrainingFunction`, `checkTrainingStatusFunction`, `startCompilationFunction`, `publishComponentFunction`, and the `waitForTraining` → `startCompilationTask` → … → `sendSuccessNotification` chain — verified by `cdk synth` snapshot diff against `test/backend-test/security/baselines/iam_baseline_TrainingWorkflowStack.template.json`
    - _Bug_Condition: isBugCondition(X) where X = `assumeRoleFunction` `sts:AssumeRole` on `resources: ['*']` (I6)_
    - _Expected_Behavior: `sts:AssumeRole` resources reference specific account IDs from `props.trustedUseCaseAccountIds` mapped to `arn:aws:iam::${id}:role/DDAPortalAccessRole`; the runtime `ExternalId` check inside the inline handler is preserved (defense-in-depth)_
    - _Preservation: every sibling function / Step Function state in `training-workflow-stack.ts` is byte-for-byte identical in the emitted CloudFormation; the inline handler's `ExternalId` check is unchanged (Req 3.6)_
    - _Requirements: 2.6_

  - [ ] 7.3 I18 — repo-audit gate finalize (Req 2.18)
    - Keep the audit from task 1 as a runnable check that greps the twelve in-scope files for the four bug-condition categories (`cdk_wildcard_resource`, `json_resource_wildcard`, `service_wildcard_action`, `assume_role_wildcard_account`)
    - Assert `iam_audit.disallowed_hits()` returns zero across `IN_SCOPE_FILES`, allowing ONLY occurrences carrying a documented `// nosec` / `# nosec` / JSON-adjacent-comment exception (the isolated `s3:ListAllMyBuckets`, `sagemaker:ListWorkteams`, `iot:DescribeEndpoint`, `logs:DescribeLogGroups`-class, `sts:GetCallerIdentity`, `cloudwatch:PutMetricData`, `ecr:GetAuthorizationToken`, `tag:GetResources`, `resourcegroupstaggingapi:GetResources` statements)
    - Enforce precise in-scope scoping: exclude `edge-cv-portal/infrastructure/cdk.out/asset.*`, any vendored duplicate under a build tree, and the security test files' own pattern strings; the gate must still FAIL if a wildcard resource on a scopable action, a `service:*` action wildcard, a wildcard-account `sts:AssumeRole`, or an unenforced-tag `PolicyStatement` is reintroduced into any real in-scope source file
    - _Bug_Condition: isBugCondition(X) where X = any remaining disallowed wildcard-resource / service-wildcard / wildcard-account / unenforced-tag occurrence in in-scope code (I18)_
    - _Expected_Behavior: `disallowed_hits()` returns zero, minus documented justified exceptions; a reintroduced wildcard still fails the gate_
    - _Preservation: the generated CDK artifacts (`cdk.out/asset.*`), any vendored duplicate, and out-of-scope findings are not touched (Req 3.18)_
    - _Requirements: 2.18_


- [ ] 8. Verify the bug-condition exploration test now passes (Fix Checking)
  - **Property 1: Expected Behavior** - Every wildcard scoped / service-wildcard enumerated / wildcard-account bounded / tag condition enforced / unscopable action documented
  - **IMPORTANT**: Re-run the SAME audit + targeted tests from task 1 - do NOT write new tests
  - Re-run the targeted tests on the fixed tree: I1 — eight per-service statements each with ARNs matching the naming-convention patterns and `iam:PassRole` scoped to `arn:aws:iam::*:role/DDA*Role`; I2 — portal S3 scoped to `props.portalArtifactsBucket.bucketArn` + tag-conditioned wildcard, `ListAllMyBuckets` isolated with a code comment; I3 — Ground Truth S3 split into tag-conditioned + `sagemaker-*` allowlist; I4 — `S3BucketAccess` + `S3ObjectAccess` both carry the tag `Condition`; I5, I6 — `sts:AssumeRole` resources reference the mapped `trustedUseCaseAccountIds` list (no `iam::*` wildcard, no `resources: ['*']`); I7–I9 — `deploy-account-role.sh` heredocs narrow to `dda-*` / `sagemaker-*` prefixes with the `*-dda-*` substring entries gone; I10–I12 — `create-edge-device-iam-role.sh` heredocs enumerate the Greengrass v2 / IoT actions and scope S3 to `dda-component-*` / `dda-inference-results-*`; I13, I14 — `launch-arm64-build-server.sh` heredocs split IoT by resource type and isolate `ListAllMyBuckets`; I15 — `edge-device-iam-policy.json` `IoTDataPlane` sid split by resource type; I16, I17 — `README_main.md` example JSONs match the narrowed shape
  - Re-run `iam_audit.disallowed_hits()` over the full in-scope tree
  - Run the property-based tests: PBT 1 (DDA vs non-DDA ARNs), PBT 2 (tagged vs untagged buckets), PBT 3 (trusted vs untrusted accounts), PBT 4 (IoT enumerable subset) — each PBT's invariant holds under the fixed policies (the DIFFERENCE between `F` and `F'` is exactly the non-DDA / untagged / non-trusted / non-exercised inputs, which are correctly denied)
  - **EXPECTED OUTCOME**: No wildcard is observed at any site AND the audit returns ZERO disallowed hits (minus the documented `// nosec` / `# nosec` / JSON-adjacent-comment exceptions for the unscopable subset) AND every property-based test's invariant holds
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 2.12, 2.13, 2.14, 2.15, 2.16, 2.17, 2.18_

- [ ] 9. Verify preservation baseline tests still pass (Preservation Checking)
  - **Property 2: Preservation** - No behavior change for legitimate inputs
  - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
  - Run the preservation baselines/property tests under the fix:
    - **CDK synth diff (I1–I6)**: run `cdk synth --all` with the same fixture context and compare each of `EdgeCVPortalComputeStack.template.json`, `DDAPortalUseCaseAccountStack.template.json`, `LabelingWorkflowStack.template.json`, `TrainingWorkflowStack.template.json` against its captured baseline — every JSON node MUST be byte-for-byte identical EXCEPT inside the enumerated I1–I6 statements; any drift outside those statements fails the test
    - **Shell heredoc JSON diff (I7–I14)**: `jq`-extract each modified heredoc and compare its non-modified sids against the baseline golden files — sibling sids MUST be byte-for-byte identical
    - **JSON template diff (I15)**: read `edge-device-iam-policy.json` and confirm the four preserved sids (`GreengrassComponentDownload`, `CloudWatchLogsUpload`, `AssumeDataAccountRole`, `GreengrassConnectivity`) are byte-for-byte identical against the baseline
    - **README prose diff (I16, I17)**: excise the two JSON code fences from the fixed `README_main.md` and compare the remaining prose against `iam_baseline_readme_prose.md` — byte-for-byte identical
    - **PBT 1**: for all generated DDA-prefixed ARN pairs, the fixed policies allow (unchanged from `F`); the non-DDA pairs are the only DIFFERENCE
    - **PBT 2**: for all generated tagged bucket states (`dda-portal:managed=true`) and `sagemaker-*` buckets, the fixed I3 / I4 policies allow (unchanged from `F`); untagged non-`sagemaker-*` buckets are the only DIFFERENCE
    - **PBT 3**: for all generated `probe in trusted` account IDs, the fixed I5 / I6 policies succeed (unchanged from `F`); `probe not in trusted` is the only DIFFERENCE
    - **PBT 4**: for all generated IoT actions in the edge-device / build-server / data-plane exercised subset AND matching resource prefixes, the fixed I11 / I13 / I15 policies allow (unchanged from `F`); non-exercised actions / non-DDA resources are the only DIFFERENCE
    - The runtime `ExternalId` check inside the training assume-role Lambda handler is preserved (I6)
  - Confirm the `cdk.out/asset.*` copies, any vendored duplicate under a build tree, and the files owned by the sibling remediation batches (`security-injection-deserialization-fixes` and `security-secrets-credentials-jwt-fixes`) are unchanged
  - **EXPECTED OUTCOME**: Tests PASS (no regressions); `F(X) = F'(X)` for all non-bug-condition inputs (every legitimate portal / cross-account / edge-device / build-server / Ground Truth flow succeeds identically)
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15, 3.16, 3.17, 3.18_

- [ ] 10. Integration + CI-gate verification
  - [ ] 10.1 Run the backend security suites and wire `iam_audit.py` into `build-custom.sh`
    - Run the backend security suites to completion — `iam_audit.py`, `test_iam_bug_condition_exploration.py`, `test_iam_cdk_synth_preservation.py`, and the `security/preservation` suite — and confirm the IAM / authorization unit + property tests pass with no regressions
    - Wire the new gate into `build-custom.sh`'s existing security-audit gate block (next to the Group-1 injection/deserialization gate and the Group-3 secrets/credentials/JWT gate, under `set -e`, ~lines 236–241):
      - `python${PYTHON_VERSION} test/backend-test/security/iam_audit.py`
      - `python${PYTHON_VERSION} -m pytest test/backend-test/security/test_iam_bug_condition_exploration.py -v`
      - `python${PYTHON_VERSION} -m pytest test/backend-test/security/test_iam_cdk_synth_preservation.py -v`
      - `python${PYTHON_VERSION} -m pytest test/backend-test/security/preservation -p no:cacheprovider --noconftest -v`
    - Confirm the gate FAILS the build on a reintroduced wildcard-resource on a scopable action, a `service:*` action wildcard, a wildcard-account `sts:AssumeRole`, or an unenforced-tag `PolicyStatement` in in-scope infrastructure code
    - End-to-end spot checks (deployment-time gate — the CI-runnable audit is a necessary but not sufficient check): deploy the four CDK stacks with fixture context to a staging AWS account; run a scripted portal workflow that (a) creates a training job with a `dda-*` name, (b) triggers a Ground Truth labeling job with a `dda-portal:managed=true`-tagged input bucket, (c) triggers `LabelingMonitorFunction` cross-account against a trusted UseCase account, (d) triggers `assumeRoleFunction` with a valid `ExternalId` against a trusted account — every step MUST succeed identically to the pre-fix baseline; attempts (e) with a non-DDA training job name and (f) `assumeRoleFunction` against a non-trusted account MUST FAIL (these are the intended DIFFERENCEs)
    - Installer replay (I7–I14): re-run each shell installer against a staging AWS account; use `aws iam get-role-policy` to fetch the applied policy and `jq`-compare to the expected narrowed shape
    - Edge-device provisioning (I15): apply the narrowed `edge-device-iam-policy.json` to a provisioned edge device; assert IoT connect / publish / subscribe / receive succeed on `dda-*` clients / `dda/*` topics; assert a non-DDA client id fails
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 2.12, 2.13, 2.14, 2.15, 2.16, 2.17, 2.18, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15, 3.16, 3.17, 3.18_

- [ ] 11. Checkpoint - Ensure all tests pass and the CI gate is wired
  - Confirm the task-1 audit + targeted tests now observe no disallowed wildcard at any site and `iam_audit.disallowed_hits()` returns zero (task 8), the task-2 preservation baselines still pass — `cdk synth` diff clean for the four stacks, heredoc JSON goldens match, JSON template preserved sids match, README prose byte-for-byte identical, PBTs 1–4 invariants hold (task 9), and the backend security suites + integration checks pass (task 10)
  - Confirm the `iam_audit.py` gate (plus the exploration and preservation suites) is wired into `build-custom.sh` so a disallowed wildcard-resource / service-wildcard / wildcard-account / unenforced-tag pattern reappearing in in-scope infrastructure code fails the build
  - Confirm the deployment-time gate items — the `dda-portal:managed=true` tag has been applied to every currently-working legitimate customer bucket BEFORE the I4 fix lands, and `trustedUseCaseAccountIds` is correctly sourced at synth time for every environment
  - Ensure all tests pass; ask the user if questions arise

---

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "description": "Run on UNFIXED code: surface wildcard-resource / service-wildcard / wildcard-account / unenforced-tag counterexamples (IAM audit + targeted CDK / JSON-AST / README inspections) and capture preservation baselines (cdk synth for 4 stacks, shell heredoc JSON, JSON template, README prose) (independent).", "tasks": ["1", "2"] },
    { "wave": 2, "description": "Wave 1 fixes — README example policies FIRST (I16, I17): documentation-only, zero runtime blast radius on live infrastructure.", "tasks": ["3.1", "3.2"] },
    { "wave": 3, "description": "Wave 2 fix — Standalone JSON policy template (I15): applied at edge-device provisioning time; existing devices unaffected until they re-run attach-role-policy.", "tasks": ["4.1"] },
    { "wave": 4, "description": "Wave 3 fixes — Shell installers (I7, I8, I9, I10, I11, I12, I13, I14): inline JSON heredocs in customer-run scripts; impact radius is one AWS account per script run.", "tasks": ["5.1", "5.2", "5.3", "5.4", "5.5", "5.6", "5.7", "5.8"] },
    { "wave": 5, "description": "Wave 4 fixes — CDK non-sts:AssumeRole (I1, I2, I3, I4): synth-diff-verifiable PolicyStatement changes on compute-stack.ts and usecase-account-stack.ts; the tag-conditioned statements (I3, I4) validate against the naming-convention baseline the installer wave established.", "tasks": ["6.1", "6.2", "6.3", "6.4"] },
    { "wave": 6, "description": "Wave 5 fixes LAST — CDK sts:AssumeRole (I5, I6) + audit gate (I18): highest blast radius; changes what accounts the portal Lambdas assume roles into. Land last with the audit gate green so a regression to a wildcard account cannot silently sneak back in.", "tasks": ["7.1", "7.2", "7.3"] },
    { "wave": 7, "description": "Fix Checking and Preservation Checking (re-run tasks 1 and 2 on fixed code, including PBTs 1–4).", "tasks": ["8", "9"] },
    { "wave": 8, "description": "Integration + CI-gate verification (backend security suites + wire iam_audit into build-custom.sh + deployment-time smoke test).", "tasks": ["10.1"] },
    { "wave": 9, "description": "Checkpoint: all green + CI gate wired + deployment-time gate items confirmed.", "tasks": ["11"] }
  ]
}
```

Visual summary of the critical path:

```
1. Bug-condition audit + targeted tests (FAILS: wildcard resources across I1–I17, missing tag Conditions, wildcard-account sts:AssumeRole)
2. Preservation baselines (PASS on unfixed tree: cdk synth for 4 stacks, shell heredoc JSON goldens, JSON template preserved sids, README prose, PBT 1–4 baselines)
        │  (1 and 2 are independent; both run on UNFIXED code first)
        ▼
2. WAVE 1 — README EXAMPLES FIRST (documentation-only, zero runtime blast radius)
   3.1 I16 README dda-build-policy → narrowed shape (IoT split, Greengrass v2 enumerated, S3 to dda-component-*/dda-inference-results-*, ListAllMyBuckets isolated)
   3.2 I17 README dda-greengrass-policy → narrowed shape (Greengrass v2 enumerated, IoT split by resource type, S3 to dda-component-*/dda-inference-results-*)
        │
        ▼
3. WAVE 2 — STANDALONE JSON TEMPLATE (future-device only, existing devices unaffected)
   4.1 I15 edge-device-iam-policy.json IoTDataPlane → split by resource type (Connect on client/dda-*, Publish/Receive on topic/dda/*, Subscribe on topicfilter/dda/*)
        │
        ▼
4. WAVE 3 — SHELL INSTALLERS (customer-run scripts, one account per run)
   5.1 I7  deploy-account-role.sh S3_POLICY first stmt → dda-* ∪ sagemaker-*
   5.2 I8  deploy-account-role.sh SAGEMAKER_POLICY → dda-*-prefixed training/compilation/labeling/model ARNs
   5.3 I9  deploy-account-role.sh AllowDDABucketPatternAccess → drop *-dda-* substring entries
   5.4 I10 create-edge-device-iam-role.sh GreengrassPermissions → enumerated Greengrass v2 subset
   5.5 I11 create-edge-device-iam-role.sh IoTPermissions → split by resource type + DescribeEndpoint isolated
   5.6 I12 create-edge-device-iam-role.sh S3Permissions → dda-component-* / dda-inference-results-*
   5.7 I13 launch-arm64-build-server.sh IoTPermissions → split by resource type + DescribeEndpoint isolated
   5.8 I14 launch-arm64-build-server.sh S3Permissions → dda-component-* / dda-inference-results-* + ListAllMyBuckets isolated
        │
        ▼
5. WAVE 4 — CDK NON-sts:AssumeRole (synth-diff-verifiable, tag-conditioned)
   6.1 I1 compute-stack.ts createLambdaRole → split into 8 per-service statements + iam:PassRole → arn:aws:iam::*:role/DDA*Role
   6.2 I2 compute-stack.ts portal S3 grant → portalArtifactsBucket + tag-conditioned wildcard + ListAllMyBuckets isolated
   6.3 I3 usecase-account-stack.ts Ground Truth S3 → tag-conditioned + sagemaker-* allowlist
   6.4 I4 usecase-account-stack.ts DDAPortalAccessRole S3BucketAccess+S3ObjectAccess → wire the tag Condition the comment promises
        │                                    ── deployment-time gate: tag your buckets first!
        ▼
6. WAVE 5 — CDK sts:AssumeRole + AUDIT GATE (LAST — highest blast radius)
   7.1 I5  labeling-workflow-stack.ts LabelingMonitorFunction sts:AssumeRole → props.trustedUseCaseAccountIds
   7.2 I6  training-workflow-stack.ts assumeRoleFunction sts:AssumeRole → props.trustedUseCaseAccountIds (ExternalId check preserved)
   7.3 I18 iam_audit.disallowed_hits() == 0 (minus documented exceptions)
        │                                    ── deployment-time gate: source trustedUseCaseAccountIds correctly!
        │
        ├──────────────┐
        ▼              ▼
7. 8. Fix Checking    9. Preservation Checking
    (re-run task 1:    (re-run task 2:
     no wildcard at    F(X) = F'(X),
     any site, zero    cdk synth diff clean,
     audit hits, PBTs) heredoc/template/prose
                       preserved, PBTs 1–4 hold)
        │              │
        └──────┬───────┘
               ▼
8. 10.1 Integration + CI-gate verification (security suites + wire build-custom.sh gate + deployment smoke test)
               │
               ▼
9. 11. Checkpoint (all green + CI audit guard wired + deployment-time gate items confirmed)
```

**Critical path:** 3.1 → 3.2 → 4.1 → 5.1 → 5.2 → 5.3 → 5.4 → 5.5 → 5.6 → 5.7 → 5.8 → 6.1 → 6.2 → 6.3 → 6.4 → 7.1 → 7.2 → 7.3 → 8/9 → 10.1 → 11.

**Ordering rationale (from the design's "Ordering and risk — five waves"):**
Wave 1 (README examples) lands first because it is documentation-only with
zero runtime blast radius on live infrastructure — customers who copy the
policy today see a change on their next install but no live infrastructure
changes. Wave 2 (JSON template I15) is next because the file is applied at
device provisioning time — future devices get the narrowed shape; existing
devices are unaffected until they re-run `attach-role-policy`. Wave 3
(shell installers I7–I14) follows — they share the same JSON-AST assertion
shape and have no coupling to the CDK stack risk; the impact radius is one
AWS account per script run. Wave 4 (CDK non-`sts:AssumeRole` I1–I4) lands
after the installer wave so the tag-conditioned statements (I3, I4) can be
validated against the same tag / naming-convention baseline the installers
established; changes are synth-diff-verifiable against the pre-fix
CloudFormation baseline captured in task 2. Wave 5 (CDK `sts:AssumeRole`
I5, I6 + audit gate I18) lands LAST as the highest-blast-radius wave —
mis-configuring `trustedUseCaseAccountIds` breaks every legitimate
cross-account portal flow (Ground Truth labeling monitoring, cross-account
training). It lands with the audit gate green so a regression to a
wildcard account cannot silently sneak back in. Deploy-time verification
(smoke test in task 10.1) is the necessary companion — the CI-runnable
audit is a necessary but not sufficient check; a green audit does not
replace a portal-flow smoke test.

## Notes

**Bug-condition methodology reminders:**
- Task 1 is the exploration test — it is EXPECTED to surface non-empty
  audit hits and observe the wildcard at each site (wildcard resources
  across I1–I17, missing tag `Condition` on I3/I4, wildcard-account
  `sts:AssumeRole` on I5/I6, `service:*` action wildcards across I10–I17,
  `*-dda-*` substring entries on I9) on the unfixed tree (the
  counterexamples that confirm the bug). Do not "fix" it, and do not modify
  infrastructure source code in task 1.
- Task 2 captures preservation baselines that must PASS on the unfixed
  tree — the four CDK stack templates via `cdk synth`, the shell heredoc
  JSON via `jq`, the JSON template's preserved sids, the README prose with
  the JSON code fences excised, and the PBT 1–4 baselines (DDA vs non-DDA
  ARNs, tagged vs untagged buckets, trusted vs untrusted accounts, IoT
  enumerable subset).
- Tasks 8 and 9 re-run the SAME task-1 audit/targeted tests and task-2
  baselines against the fixed tree — every wildcard must be scoped /
  enumerated / bounded / enforced / documented (zero disallowed audit
  hits) and the preservation baselines / PBT invariants must still hold.
- The only occurrences allowed to survive the audit are those carrying a
  documented, justified exception in an in-scope file: the isolated
  unscopable statements for `s3:ListAllMyBuckets`, `sagemaker:ListWorkteams`,
  `iot:DescribeEndpoint`, `logs:DescribeLogGroups`-class,
  `sts:GetCallerIdentity`, `cloudwatch:PutMetricData`,
  `ecr:GetAuthorizationToken`, `tag:GetResources`, and
  `resourcegroupstaggingapi:GetResources` — each with an adjacent `//` /
  `#` / `"//"` comment recording the AWS IAM reference reason.
- Property-based testing (Hypothesis, already vendored under `.hypothesis/`)
  is emphasized where the input domain is generatable:
  - **PBT 1 (I1, I7–I14)**: DDA-managed vs non-DDA resource ARNs — the
    fixed policies allow iff the ARN matches the committed naming
    conventions AND the action is in the exercised set; the DIFFERENCE
    between `F` and `F'` is exactly the non-DDA resource ARNs.
  - **PBT 2 (I3, I4)**: tagged vs untagged buckets — the fixed I3 / I4
    policies allow iff `tags["dda-portal:managed"] == "true"` OR the
    bucket is `sagemaker-*`; the unfixed policies allow regardless of
    tag; the DIFFERENCE is exactly untagged non-`sagemaker-*` buckets.
  - **PBT 3 (I5, I6)**: trusted vs untrusted account IDs — the fixed
    I5 / I6 policies allow `sts:AssumeRole` on
    `arn:aws:iam::${probe}:role/DDAPortalAccessRole` iff `probe in
    trusted`; empty trusted set is a synth-time error (the design DOES
    NOT fall back to a wildcard account); the DIFFERENCE is exactly
    `probe not in trusted`.
  - **PBT 4 (I11, I13, I15)**: IoT enumerable action subset — the fixed
    policies allow iff the action is in the edge-device / build-server /
    data-plane exercised subset AND the resource matches the appropriate
    prefix; the DIFFERENCE is exactly the non-exercised actions.
- The generated / vendored copies (`cdk.out/asset.*`, any vendored
  duplicate under a build tree) and the files owned by the sibling
  remediation batches (`security-injection-deserialization-fixes` for
  findings #1–#8, `security-secrets-credentials-jwt-fixes` for findings
  S1–S9) are NOT touched by this spec and are asserted unchanged (Req 3.18).
- The **deployment-time gate** is a first-class part of the workflow, not
  an afterthought: the CI-runnable audit (I18) proves no disallowed
  pattern remains in-source, but a green audit does not replace the
  portal-flow smoke test in task 10.1 — deploy the four stacks to a
  staging account, run the scripted end-to-end workflow (training +
  labeling + cross-account assume + edge-device pull + inference upload)
  against DDA-prefixed / tagged / trusted-account inputs, and confirm
  every step succeeds identically to the pre-fix baseline before merging
  to main. The I4 tag `Condition` requires operators tag their buckets
  BEFORE the fix lands, and the I5 / I6 `trustedUseCaseAccountIds`
  requires the account list is correctly sourced at synth time.

