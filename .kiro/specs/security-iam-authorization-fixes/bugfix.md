# Bugfix Requirements Document

## Introduction

A code security review of the DefectDetectionApplication (DDA) — the AWS "DDA
Code Review" scan captured in `security-findings-report.json` at the repo root
(63 findings total) — surfaced 17 HIGH-severity **IAM & Authorization** findings
that are the subject of this spec. Every finding is an IAM policy statement in
either a CDK stack, a customer-facing shell installer, a customer-facing JSON
policy template, or a customer-facing setup README that grants privileges wider
than the code path that consumes the role actually uses:

1. **Unscoped resource ARNs** — statements that combine SageMaker, Greengrass,
   IoT, S3, and STS actions on `resources: ['*']` (or `"Resource": "*"`) even
   when the underlying API supports resource-level permissions or tag
   conditions.
2. **Service-wildcard actions** — `greengrass:*`, `greengrassv2:*`, and
   `iot:*` grants where the code path only exercises a small, enumerable set of
   actions (Greengrass edge-device pulls / IoT data-plane connect+publish+
   subscribe+receive+shadow).
3. **Un-tagged S3 grants that a code comment already promises to tag-scope** —
   the DDA Portal access role's `S3BucketAccess` and `S3ObjectAccess` statements
   are documented as tag-conditioned ("Buckets must be tagged with
   `dda-portal:managed` = `true`") but the `Condition` was never wired up.
4. **Wildcard-account `sts:AssumeRole` grants** — cross-account role assumption
   statements that scope the role name but leave the account portion wildcarded
   (`arn:aws:iam::*:role/DDAPortalAccessRole`) or fully wildcarded (`resources:
   ['*']`).

This spec ("IAM & Authorization hardening" — the fourth remediation group in
the AWS code-review sequence, stacked after
`security-secrets-credentials-jwt-fixes`) is scoped **strictly** to the 17
findings enumerated below (labelled I1–I17) plus a repository audit gate
(I18). It uses the SAME bug-condition methodology, EARS format, and Property 1
(Fix Checking) / Property 2 (Preservation) framing as the sibling specs
`security-injection-deserialization-fixes` (findings #1–#8) and
`security-secrets-credentials-jwt-fixes` (findings S1–S9).

**Explicitly out of scope (handled elsewhere, or fundamentally out of scope):**

- **Services and API operations that legitimately require `Resource: "*"`** —
  actions the AWS IAM reference lists as unscopable, e.g. `s3:ListAllMyBuckets`,
  `cloudwatch:PutMetricData`, `sts:GetCallerIdentity`, several read-only
  `logs:DescribeLogGroups`-class operations, `sagemaker:ListWorkteams`,
  `ecr:GetAuthorizationToken`, and `tag:GetResources` /
  `resourcegroupstaggingapi:GetResources`. Where a finding covers a statement
  that mixes scopable and unscopable actions, the fix isolates the unscopable
  actions into their own statement (documented with a code comment) rather than
  attempting to scope them.
- **Injection / unsafe-deserialization (findings #1–#8)** — remediated in the
  sibling spec `security-injection-deserialization-fixes`.
- **Secrets, credentials & JWT/token handling (findings S1–S9)** — remediated
  in the sibling spec `security-secrets-credentials-jwt-fixes`.
- **Vendored / generated / duplicate copies of the in-scope files** — build
  artifacts such as `edge-cv-portal/infrastructure/cdk.out/**/asset.*/**` or
  any other vendored copy regenerate from the real source; only the real
  source paths listed below are to be fixed. All findings were triaged against
  the real source, not the vendored/generated copies.
- **Any other finding class from the 63-finding report** not listed as I1–I17
  below.

The findings and their real source locations:

**CDK stacks (live infrastructure — synth-diff verifiable, deployment-testable)**

| # | File | Loc | Finding | Fix |
|---|------|-----|---------|-----|
| I1 | `edge-cv-portal/infrastructure/lib/compute-stack.ts` | ~146 (scanner ~87, drift) | Portal Lambda execution role — a single combined statement over SageMaker / Greengrass / IoT / logs / `sts:AssumeRole` / `execute-api:Invoke` / S3 CORS actions on `resources: ['*']`. | Split into per-service statements and scope each: SageMaker to `arn:aws:sagemaker:*:*:{training-job,compilation-job,labeling-job,model}/dda-*` and `arn:aws:sagemaker:*:*:workteam/*` (ListWorkteams stays wildcarded in its own statement); Greengrass v2 to `arn:aws:greengrass:*:*:{components,coreDevices,deployments}:*` (with `aws:ResourceTag/dda-portal:managed=true` where the API supports it, else broad service ARN); IoT to `arn:aws:iot:*:*:thing/dda-*`, `topic/dda/*`, `job/*`, `thinggroup/*`; `sts:AssumeRole` to `arn:aws:iam::*:role/DDAPortalAccessRole` (matching the labeling-workflow-stack pattern); `execute-api:Invoke` limited to the portal's own API Gateway ARN if statically known, else portal-only per code comment. Additionally scope the sibling `iam:PassRole` statement's resource from `*` to `arn:aws:iam::*:role/DDA*Role` while preserving its `iam:PassedToService=sagemaker.amazonaws.com` condition. |
| I2 | `edge-cv-portal/infrastructure/lib/compute-stack.ts` | ~183 (scanner ~177) | Portal Lambda S3 permissions (`GetObject / PutObject / ListBucket / ListAllMyBuckets / CreateBucket / GetBucketLocation / GetBucketTagging`) on `resources: ['*']` in the PORTAL account. The comment "Restricted by assumed role in UseCase Account" is true only for cross-account access; portal-account access is unbounded. | Scope to the portal's own artifacts bucket ARN (`props.portalArtifactsBucket.bucketArn` + `/*`) plus a tag-conditioned wildcard for buckets carrying `aws:ResourceTag/dda-portal:managed=true`. Split `s3:ListAllMyBuckets` (unscopable) into its own statement with `resources: ['*']` and a code comment recording why it must stay wildcarded. Preserve the cross-account comment and add a note that access is now enforced at both ends. |
| I3 | `edge-cv-portal/infrastructure/lib/usecase-account-stack.ts` | ~197 (`DDASageMakerExecutionRole` — Ground Truth) | Ground Truth SageMaker execution role S3 grant (`GetObject / PutObject / DeleteObject / ListBucket / GetBucketLocation / GetBucketCors / PutBucketCors`) with `resources: ['arn:aws:s3:::*', 'arn:aws:s3:::*/*']`. Customer input+output buckets have customer-chosen names, so full ARN-scoping would break legitimate Ground Truth flows. | Add `Condition: StringEquals { 'aws:ResourceTag/dda-portal:managed': 'true' }` to the bucket-level statement (matching the intent stated at `:425`, made explicit); additionally allowlist the SageMaker-managed `arn:aws:s3:::sagemaker-*` and `arn:aws:s3:::sagemaker-*/*` ARNs in a separate statement (unconditional, mirrors the `S3SageMakerAccess` sid in the same file). Document the tag requirement in a code comment so operators know Ground Truth input/output buckets must carry `dda-portal:managed=true`. |
| I4 | `edge-cv-portal/infrastructure/lib/usecase-account-stack.ts` | ~425 (`DDAPortalAccessRole` — `S3BucketAccess` + `S3ObjectAccess`) | The DDA Portal access role's bucket-level statement (`ListBucket / GetBucketLocation / GetBucketVersioning / GetBucketTagging / CreateBucket / PutBucketVersioning / PutBucketEncryption / PutBucketTagging`) uses `resources: ['arn:aws:s3:::*']` and the object-level statement (`GetObject / PutObject / DeleteObject`) uses `resources: ['arn:aws:s3:::*/*']`. The in-code comment "Tag-based access for flexibility. Buckets must be tagged with 'dda-portal:managed' = 'true'" declares intent but is NOT enforced by a `Condition`. | Implement the tag `Condition` the comment already promises — add `Condition: StringEquals { 'aws:ResourceTag/dda-portal:managed': 'true' }` to `S3BucketAccess` AND `S3ObjectAccess`. Preserve the `S3SageMakerAccess` sid statement, the `ResourceTaggingAccess` RGTA statement, and every unrelated statement in the file byte-for-byte. |
| I5 | `edge-cv-portal/infrastructure/lib/labeling-workflow-stack.ts` | ~44 (`LabelingMonitorFunction`) | Monitor Function role's `sts:AssumeRole` grant with `resources: ['arn:aws:iam::*:role/DDAPortalAccessRole']` — role name is fixed but the account portion is wildcarded. | Replace `iam::*` with a config-driven list of trusted UseCase account IDs (`props.trustedUseCaseAccountIds: string[]`, read from CDK context / parameter), mapped to `arn:aws:iam::${id}:role/DDAPortalAccessRole` per account. If the list is unknown at synth time (multi-tenant), document a required deployment-time SSM parameter and default the list to the values read from that parameter. The role name `DDAPortalAccessRole` remains fixed. |
| I6 | `edge-cv-portal/infrastructure/lib/training-workflow-stack.ts` | ~67 (`assumeRoleFunction`) | `assumeRoleFunction` execution role: `actions: ['sts:AssumeRole']`, `resources: ['*']`. The in-code comment "Will be scoped to specific roles with ExternalId" acknowledges the gap. | Same pattern as I5 — parameterize on `props.trustedUseCaseAccountIds` and scope to `arn:aws:iam::${id}:role/DDAPortalAccessRole` per account. Preserve the runtime `ExternalId` check performed by `handler()` in the inline Lambda code. |

**Customer-facing shell installers and JSON policy templates**

| # | File | Loc | Finding | Fix |
|---|------|-----|---------|-----|
| I7 | `edge-cv-portal/deploy-account-role.sh` | ~211 (`S3_POLICY` — inline SageMaker exec role) | The first S3 statement (`GetObject / PutObject / DeleteObject / ListBucket / GetBucketLocation / GetBucketCors / PutBucketCors`) uses `"Resource": ["arn:aws:s3:::*", "arn:aws:s3:::*/*"]`. A second, already-scoped statement in the same policy already restricts read to `arn:aws:s3:::sagemaker-*` and `arn:aws:s3:::sagemaker-*/*`. | Narrow the first statement's `Resource` to the union of the DDA and SageMaker prefixes: `arn:aws:s3:::dda-*`, `arn:aws:s3:::dda-*/*`, `arn:aws:s3:::sagemaker-*`, `arn:aws:s3:::sagemaker-*/*`. Preserve the second (already scoped) statement unchanged. |
| I8 | `edge-cv-portal/deploy-account-role.sh` | ~281 (`SAGEMAKER_POLICY`) | The inline SageMaker exec role SageMaker statement (`CreateTrainingJob / DescribeTrainingJob / StopTrainingJob / ListTrainingJobs / CreateCompilationJob / DescribeCompilationJob / StopCompilationJob / ListCompilationJobs / CreateLabelingJob / DescribeLabelingJob / ListLabelingJobs / CreateModel / DescribeModel / DeleteModel / ListModels`) uses `"Resource": "*"`. | Scope to `arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:training-job/dda-*`, `arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:compilation-job/dda-*`, `arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:labeling-job/dda-*`, and `arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:model/dda-*`. Preserve the sibling `iam:PassRole` statement (already scoped to `arn:aws:iam::*:role/DDASageMakerExecutionRole`) unchanged. |
| I9 | `edge-cv-portal/deploy-account-role.sh` | ~378 (`DDAPortalComponentAccessPolicy` — Greengrass device policy) | The `AllowDDABucketPatternAccess` sid includes overly-broad S3 patterns `arn:aws:s3:::*-dda-*` and `arn:aws:s3:::*-dda-*/*`, which match any bucket with `dda` anywhere in its name. | Narrow the S3 patterns to `arn:aws:s3:::dda-*` and `arn:aws:s3:::dda-*/*` only (remove the two `*-dda-*` entries). The `AllowInferenceResultsUpload` sid (already scoped to `arn:aws:s3:::dda-inference-results-*` and `.../* `) stays as-is; the `AllowPortalComponentBucketAccess` sid (already scoped to `arn:aws:s3:::dda-component-*`) stays as-is. |
| I10 | `station_install/create-edge-device-iam-role.sh` | ~114 (`GreengrassPermissions` sid) | `"Action": ["greengrass:*", "greengrassv2:*"]` on `"Resource": "*"`. | Replace service-wildcard actions with the specific edge-device actions the code path uses: `greengrass:GetComponentVersionArtifact`, `greengrass:ResolveComponentCandidates`, `greengrass:GetDeploymentConfiguration`, `greengrassv2:GetDeployment`, `greengrassv2:GetCoreDevice`, `greengrassv2:UpdateConnectivityInfo`, `greengrassv2:ListComponents`, `greengrassv2:GetComponentVersionArtifact`, `greengrassv2:ResolveComponentCandidates`. `Resource` stays `"*"` because Greengrass v1 API resource support is limited and this template runs on customer edge devices where action-scoping is the primary defense. |
| I11 | `station_install/create-edge-device-iam-role.sh` | ~122 (`IoTPermissions` sid) | `"Action": ["iot:*"]` on `"Resource": "*"`. | Replace with the enumerable set the edge device actually uses — `iot:Connect`, `iot:Publish`, `iot:Subscribe`, `iot:Receive`, `iot:GetThingShadow`, `iot:UpdateThingShadow`, `iot:DescribeEndpoint`, `iot:DescribeThing` — split by IoT resource type: `iot:Connect` scoped to `arn:aws:iot:*:*:client/dda-*`; `iot:Publish`/`Subscribe`/`Receive` scoped to `arn:aws:iot:*:*:topic/dda/*` and `arn:aws:iot:*:*:topicfilter/dda/*`; thing-shadow ops and `DescribeThing` scoped to `arn:aws:iot:*:*:thing/dda-*`; `iot:DescribeEndpoint` on `"*"` (unscopable). |
| I12 | `station_install/create-edge-device-iam-role.sh` | ~130 (`S3Permissions` sid) | S3 actions (`GetObject / PutObject / ListBucket / GetBucketLocation / GetBucketVersioning / ListBucketVersions`) on `"Resource": "*"`. | Scope to `arn:aws:s3:::dda-component-*`, `arn:aws:s3:::dda-component-*/*`, `arn:aws:s3:::dda-inference-results-*`, and `arn:aws:s3:::dda-inference-results-*/*` (mirrors the narrowed `DDAPortalComponentAccessPolicy` in I9). |
| I13 | `edge-cv-portal/launch-arm64-build-server.sh` | ~155 (`IoTPermissions` sid) | `"Action": ["iot:*"]` on `"Resource": "*"`. | Narrow to the build-server-needed subset: `iot:DescribeThing`, `iot:DescribeEndpoint`, `iot:UpdateThingShadow`, `iot:CreateThing`, `iot:AttachPolicy`, `iot:DescribeJob`. Scope resources where the action supports it (thing ops to `arn:aws:iot:*:*:thing/dda-*`, job ops to `arn:aws:iot:*:*:job/*`); the remaining actions stay on `"*"` because IoT does not support scoping them by resource. |
| I14 | `edge-cv-portal/launch-arm64-build-server.sh` | ~161 (`S3Permissions` sid) | S3 actions including `CreateBucket / GetBucketLocation / PutBucketVersioning / GetObject / PutObject / ListBucket / DeleteObject / GetBucketVersioning / ListBucketVersions / GetBucketPolicy / PutBucketPolicy / GetBucketAcl / PutBucketAcl / GetBucketTagging / PutBucketTagging / ListAllMyBuckets` on `"Resource": "*"`. | Scope to `arn:aws:s3:::dda-component-*`, `arn:aws:s3:::dda-component-*/*`, `arn:aws:s3:::dda-inference-results-*`, and `arn:aws:s3:::dda-inference-results-*/*`. Split `s3:ListAllMyBuckets` into its own statement with `"Resource": "*"` and a JSON comment/adjacent shell comment recording that it is unscopable. |
| I15 | `station_install/edge-device-iam-policy.json` | ~34 (`IoTDataPlane` sid) | IoT data-plane actions (`Connect / Publish / Subscribe / Receive`) on `"Resource": "*"`. | Scope by resource type: `iot:Connect` on `arn:aws:iot:*:*:client/dda-*`; `iot:Publish` on `arn:aws:iot:*:*:topic/dda/*`; `iot:Subscribe` on `arn:aws:iot:*:*:topicfilter/dda/*`; `iot:Receive` on `arn:aws:iot:*:*:topic/dda/*`. Preserve the `GreengrassComponentDownload`, `CloudWatchLogsUpload`, `AssumeDataAccountRole`, and `GreengrassConnectivity` sids unchanged. |

**Customer-facing setup documentation (README)**

| # | File | Loc | Finding | Fix |
|---|------|-----|---------|-----|
| I16 | `README_main.md` | ~230 (`dda-build-policy` example JSON) | Example JSON contains `greengrass:*`, `iot:*`, and S3 CreateBucket / GetBucketLocation / PutBucketVersioning / GetObject / PutObject / ListBucket all on `"Resource": "*"`. | Replace the JSON block with the same narrowed policy shape as I13/I14 (specific IoT actions, S3 scoped to `dda-component-*` and `dda-inference-results-*`, `s3:ListAllMyBuckets` in its own statement). Preserve the surrounding prose explaining how to customize for larger scope if needed. |
| I17 | `README_main.md` | ~256 (`dda-greengrass-policy` example JSON) | Example JSON contains `greengrass:*`, S3 `arn:aws:s3:::*` / `arn:aws:s3:::*/*` wildcards, and multiple IoT actions on `"Resource": "*"`. | Replace with the same narrowed policy shape as I10/I11/I15 (specific Greengrass v2 edge-device actions; IoT actions scoped by resource type against `dda-*` clients/things and `dda/*` topics; S3 scoped to `dda-component-*` and `dda-inference-results-*`). Preserve the surrounding prose. |

**Repository audit gate**

| # | File | Loc | Finding | Fix |
|---|------|-----|---------|-----|
| I18 | (repo-wide, in-scope files only) | — | No runnable check exists that asserts the fixes above are in place; regressions could re-introduce `resources: ['*']`, `"Resource": "*"`, `greengrass:*` / `iot:*` service-wildcard actions, or wildcard-account `sts:AssumeRole` grants in in-scope files. | Add a runnable audit that greps the in-scope files (I1–I17 files only) for the disallowed patterns and asserts zero disallowed hits, minus documented exceptions (e.g. `s3:ListAllMyBuckets`, `cloudwatch:PutMetricData`, `sts:GetCallerIdentity`, `logs:DescribeLogGroups`-class, `sagemaker:ListWorkteams`, `ecr:GetAuthorizationToken`, `tag:GetResources`, `iot:DescribeEndpoint`, statements carrying an in-file `# nosec` / JSON comment / adjacent code comment recording why the wildcard is required). |

### Naming-convention commitments the fix depends on

The scoped ARN patterns in the fixes above depend on the following naming
conventions, which this spec **explicitly commits to** so operators and
downstream tooling can rely on them:

- **Portal-created SageMaker resources** use the `dda-*` prefix:
  - `training-job/dda-*`
  - `compilation-job/dda-*`
  - `labeling-job/dda-*`
  - `model/dda-*`
- **Portal-created IoT resources** use the `dda-*` / `dda/*` prefix:
  - IoT client id: `client/dda-*`
  - IoT thing name: `thing/dda-*`
  - IoT topic path: `topic/dda/*` and `topicfilter/dda/*`
- **DDA-managed S3 buckets** follow one of:
  - Name prefix `dda-*` (portal artifacts and general portal-managed buckets),
  - Name prefix `dda-component-*` (Greengrass component artifact buckets),
  - Name prefix `dda-inference-results-*` (edge-device inference upload buckets), OR
  - Any name, provided the bucket carries the tag `dda-portal:managed=true`.
- **SageMaker's own managed buckets** (`arn:aws:s3:::sagemaker-*`) remain
  allowlisted where a SageMaker service role needs them.
- **Existing customer resources** that predate these conventions and need
  portal access must be tagged `dda-portal:managed=true` (bucket-level and
  object-level tag conditions apply). Operators are notified of this
  requirement via code comments at the tag-conditioned statements (I3, I4) and
  via the updated `README_main.md` prose (I16, I17).

### Testability + Rollback commitments

The 17 fixes span three surface types with three verification approaches:

- **CDK stacks (I1–I6)** are verifiable via `cdk synth` output diff. The fix
  MUST NOT change any statement in the emitted CloudFormation
  (`edge-cv-portal/infrastructure/cdk.out/*.template.json`) other than the
  intended ones — validated by snapshot comparison of the synthesized templates
  against a baseline captured before the fix. Statements not enumerated in
  I1–I6 must remain byte-for-byte identical.
- **Shell scripts and JSON templates (I7–I15)** are verifiable via
  `jq` / JSON-AST assertions that the resulting policy JSON has the expected
  `Action` list and `Resource` shape (specific ARN patterns, no
  `service:*` action wildcards, no `"Resource": "*"` except in enumerated
  unscopable-action statements).
- **README example policies (I16, I17)** are verifiable by extracting the JSON
  code fences and running the same `jq` / AST assertions used for I7–I15.
- **Live deployment verification** requires re-running `cdk deploy` on the
  affected stacks and executing an end-to-end portal smoke test (create a
  training job, run a labeling job, cross-account assume, edge-device
  component pull, inference-results upload). The requirements call this out as
  a **deployment-time gate** — the CI-runnable audit (I18) is a necessary but
  not sufficient check; a green audit does not replace a portal-flow smoke
  test.
- **Rollback plan:** each of I1–I17 is a **separate commit / task** in this
  spec's task breakdown. If a portal flow breaks after deploy, the specific
  IAM statement that caused the regression can be reverted independently —
  isolated to a single file's diff — without touching the other 16 fixes or
  the sibling remediation branches (`security-injection-deserialization-fixes`
  or `security-secrets-credentials-jwt-fixes`).

### Bug Condition and Properties

The bug-condition methodology frames this fix as follows.

**Bug Condition `C(X)`** — identifies the inputs/code paths that trigger the
defect. Here the "input" is any IAM policy statement in an in-scope file that
grants privileges wider than the calling code path actually uses:

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type PolicyStatement   // an IAM policy statement in an in-scope
                                     // CDK stack, shell installer, JSON
                                     // template, or README example block
  OUTPUT: boolean

  // True when the statement grants privileges wider than the code path uses:
  //   - Resource: '*' / arn:aws:s3:::*  for an action whose AWS-defined
  //     resource type supports scoping AND the code path only touches DDA-
  //     managed resources; OR
  //   - service:*  action wildcard when the code path only uses an
  //     enumerable subset (greengrass:*, greengrassv2:*, iot:*); OR
  //   - sts:AssumeRole with resources: ['*'] or iam::* wildcard account
  //     when the trusted-account set is known / bounded; OR
  //   - the statement's own in-code comment declares a tag-based / scoped
  //     restriction that is not actually enforced by a Condition or
  //     scoped Resource ARN.
  RETURN resourceWildcardOnScopableAction(X)
      OR serviceWildcardActionOverEnumerableSet(X)
      OR wildcardAccountAssumeRole(X)
      OR commentPromisesRestrictionNotEnforced(X)
END FUNCTION
```

**Fix Property `P` (Fix Checking)** — desired behavior for all buggy inputs
after the fix `F'`:

```pascal
// Property: Fix Checking - the granted privileges are the narrowest set that
//                          still supports the calling code path
FOR ALL X WHERE isBugCondition(X) DO
  result ← F'(X)
  ASSERT scoped(result)
     // Resource ARNs match the naming-convention patterns (dda-*, dda/*,
     // dda-component-*, dda-inference-results-*, sagemaker-*) OR carry an
     // aws:ResourceTag/dda-portal:managed=true Condition, OR are in the
     // enumerated unscopable-action set with an in-file comment recording
     // why the wildcard is required.
  ASSERT enumeratedActions(result)
     // service:* action wildcards are replaced by the enumerable subset the
     // code path actually uses.
  ASSERT boundedAccounts(result)
     // sts:AssumeRole resources reference specific account IDs from the
     // trustedUseCaseAccountIds config, not arn:aws:iam::*:role/....
  ASSERT documentedException(result) WHEN X is in the unscopable set
     // For statements that must remain wildcarded (ListAllMyBuckets,
     // PutMetricData, GetCallerIdentity, DescribeLogGroups-class,
     // ListWorkteams, GetAuthorizationToken, GetResources,
     // DescribeEndpoint), the fix isolates them into their own statement
     // with an in-file comment recording the reason.
END FOR
```

**Preservation Property (Preservation Checking)** — for every input that does
NOT trigger the bug condition (i.e. every legitimate portal / edge-device /
build-server / cross-account flow), the fixed code behaves identically to the
original code `F`:

```pascal
// Property: Preservation Checking - no behavior change for legitimate flows
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
     // The union of (Resource ARN, Action) pairs actually exercised by the
     // portal Lambdas, the SageMaker execution role, the Ground Truth role,
     // the DDA Portal access role, the labeling monitor, the training
     // assume-role Lambda, the edge-device role, and the build-server role
     // at runtime is a SUBSET of the fixed policy — every legitimate flow
     // continues to succeed.
     // Every statement in the affected files that is NOT one of I1-I17
     // remains byte-for-byte identical (verified by cdk synth diff for
     // I1-I6 and by JSON-AST comparison for I7-I17).
     // The portal end-to-end smoke test (training + labeling + cross-
     // account + edge-device pull + inference upload) passes identically
     // against F and F'.
END FOR
```

- **F**: the original (unfixed) infrastructure, where the IAM policy statement
  grants a wildcard resource / a service-wildcard action / a wildcard account
  / an unenforced tag condition.
- **F'**: the fixed infrastructure, where every statement grants the narrowest
  privilege set that supports the calling code path, and any remaining
  wildcards carry an in-file documented exception.

Where the input domain is generatable (e.g. DDA-managed resource ARNs
following the naming conventions, non-DDA resource ARNs that should be
denied, tagged vs untagged buckets), **property-based testing** is emphasized
in the design phase: generate resource ARNs that match / do not match the
`dda-*` prefixes and assert the resulting policy allows / denies as expected;
generate SSM-visible cross-account role ARNs with allowed vs disallowed
account IDs and assert `sts:AssumeRole` succeeds / fails as expected.

## Bug Analysis

### Current Behavior (Defect)

The application's IAM policies grant privileges wider than the calling code
paths actually use — combining scopable actions on `resources: ['*']`,
granting service-wildcard actions when only a small subset is exercised,
leaving `sts:AssumeRole` on wildcard accounts, and declaring tag-based
restrictions in code comments without enforcing them via `Condition` blocks.

1.1 WHEN `createLambdaRole(name)` in
`edge-cv-portal/infrastructure/lib/compute-stack.ts` (~line 146; scanner
reported ~87 with subsequent drift) synthesizes the portal Lambda execution
role THEN the system emits a single combined `PolicyStatement` over SageMaker
(`CreateTrainingJob`, `CreateCompilationJob`, `CreateLabelingJob`,
`DescribeWorkteam`, `ListWorkteams`, `AddTags`, …), Greengrass v2
(`CreateComponentVersion`, `CreateDeployment`, `ListCoreDevices`, …), IoT
(`DescribeThing`, `CreateJob`, `UpdateThingShadow`, …), CloudWatch Logs read
operations, `sts:AssumeRole`, `execute-api:Invoke`, and S3 CORS with
`resources: ['*']`; the sibling `iam:PassRole` statement is scoped by
`iam:PassedToService=sagemaker.amazonaws.com` but its resource is `*` as well.

1.2 WHEN `createLambdaRole(name)` in
`edge-cv-portal/infrastructure/lib/compute-stack.ts` (~line 183; scanner
reported ~177) synthesizes the portal Lambda S3 grants THEN the system emits a
statement granting `s3:GetObject`, `s3:PutObject`, `s3:ListBucket`,
`s3:ListAllMyBuckets`, `s3:CreateBucket`, `s3:GetBucketLocation`, and
`s3:GetBucketTagging` on `resources: ['*']` in the PORTAL account, even though
the code comment says "Restricted by assumed role in UseCase Account" —
portal-account S3 access is unbounded.

1.3 WHEN `UseCaseAccountStack` in
`edge-cv-portal/infrastructure/lib/usecase-account-stack.ts` (~line 197)
synthesizes the `DDASageMakerExecutionRole` (used by SageMaker Ground Truth,
training, and compilation) THEN the system grants `s3:GetObject`,
`s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket`, `s3:GetBucketLocation`,
`s3:GetBucketCors`, and `s3:PutBucketCors` on `resources: ['arn:aws:s3:::*',
'arn:aws:s3:::*/*']` — every bucket in the account, with no tag condition.

1.4 WHEN `UseCaseAccountStack` (~line 425) synthesizes the
`DDAPortalAccessRole` `S3BucketAccess` and `S3ObjectAccess` statements THEN
the system grants `s3:ListBucket`, `GetBucketLocation`, `GetBucketVersioning`,
`GetBucketTagging`, `CreateBucket`, `PutBucketVersioning`,
`PutBucketEncryption`, `PutBucketTagging` on `resources: ['arn:aws:s3:::*']`
and `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on
`resources: ['arn:aws:s3:::*/*']`, even though the code comment immediately
above declares "Tag-based access for flexibility. Buckets must be tagged with
'dda-portal:managed' = 'true'" — the promised tag condition is not present in
the statement.

1.5 WHEN `LabelingWorkflowStack` in
`edge-cv-portal/infrastructure/lib/labeling-workflow-stack.ts` (~line 44)
synthesizes the `LabelingMonitorFunction` role THEN the system grants
`sts:AssumeRole` on `resources: ['arn:aws:iam::*:role/DDAPortalAccessRole']`
— the role name is fixed but the account portion is wildcarded, so the
monitor Lambda can assume the role in any AWS account that has one.

1.6 WHEN `TrainingWorkflowStack` in
`edge-cv-portal/infrastructure/lib/training-workflow-stack.ts` (~line 67)
synthesizes the `assumeRoleFunction` execution role THEN the system grants
`sts:AssumeRole` on `resources: ['*']`, and the code comment "Will be scoped
to specific roles with ExternalId" acknowledges the gap.

1.7 WHEN `edge-cv-portal/deploy-account-role.sh` (~line 211) executes and
writes the `S3_POLICY` document for the `DDASageMakerExecutionRole` THEN the
inline first S3 statement grants `s3:GetObject`, `s3:PutObject`,
`s3:DeleteObject`, `s3:ListBucket`, `s3:GetBucketLocation`,
`s3:GetBucketCors`, and `s3:PutBucketCors` on `"Resource":
["arn:aws:s3:::*", "arn:aws:s3:::*/*"]`, while a second statement in the same
policy is already scoped to `sagemaker-*` — the first statement's overbreadth
subsumes the scoping intent.

1.8 WHEN `edge-cv-portal/deploy-account-role.sh` (~line 281) writes the
`SAGEMAKER_POLICY` document THEN the inline statement grants
`sagemaker:CreateTrainingJob`, `CreateCompilationJob`, `CreateLabelingJob`,
`CreateModel`, and the corresponding `Describe`/`Stop`/`List`/`Delete` actions
on `"Resource": "*"`, allowing the role to operate on jobs and models with
any name in any account.

1.9 WHEN `edge-cv-portal/deploy-account-role.sh` (~line 378) writes the
`DDAPortalComponentAccessPolicy` (Greengrass device policy) `GREENGRASS_POLICY`
document THEN the inline `AllowDDABucketPatternAccess` sid grants
`s3:GetObject`, `s3:GetBucketLocation`, and `s3:HeadObject` on `Resource`
values including `arn:aws:s3:::*-dda-*` and `arn:aws:s3:::*-dda-*/*`, which
match any bucket that contains the substring `dda` anywhere in its name.

1.10 WHEN `station_install/create-edge-device-iam-role.sh` (~line 114) writes
the `INLINE_POLICY` document THEN the `GreengrassPermissions` sid grants
`"Action": ["greengrass:*", "greengrassv2:*"]` on `"Resource": "*"`, granting
every Greengrass v1 and v2 API operation on every resource in the account.

1.11 WHEN `station_install/create-edge-device-iam-role.sh` (~line 122) writes
the `INLINE_POLICY` document THEN the `IoTPermissions` sid grants `"Action":
["iot:*"]` on `"Resource": "*"`, granting every IoT API operation on every
resource in the account.

1.12 WHEN `station_install/create-edge-device-iam-role.sh` (~line 130) writes
the `INLINE_POLICY` document THEN the `S3Permissions` sid grants
`s3:GetObject`, `s3:PutObject`, `s3:ListBucket`, `s3:GetBucketLocation`,
`s3:GetBucketVersioning`, and `s3:ListBucketVersions` on `"Resource": "*"`,
even though the edge device only needs component-artifact reads and
inference-results uploads.

1.13 WHEN `edge-cv-portal/launch-arm64-build-server.sh` (~line 155) writes the
inline build-server policy THEN the `IoTPermissions` sid grants `"Action":
["iot:*"]` on `"Resource": "*"`, even though the build server only needs a
small set of thing / job / shadow ops.

1.14 WHEN `edge-cv-portal/launch-arm64-build-server.sh` (~line 161) writes
the inline build-server policy THEN the `S3Permissions` sid grants
`CreateBucket`, `GetBucketLocation`, `PutBucketVersioning`, `GetObject`,
`PutObject`, `ListBucket`, `DeleteObject`, `GetBucketVersioning`,
`ListBucketVersions`, `GetBucketPolicy`, `PutBucketPolicy`, `GetBucketAcl`,
`PutBucketAcl`, `GetBucketTagging`, `PutBucketTagging`, and
`ListAllMyBuckets` on `"Resource": "*"`.

1.15 WHEN an edge device is provisioned with
`station_install/edge-device-iam-policy.json` (~line 34) THEN the
`IoTDataPlane` sid grants `iot:Connect`, `iot:Publish`, `iot:Subscribe`, and
`iot:Receive` on `"Resource": "*"`, allowing the device to connect as any
client id and publish / subscribe / receive on any topic in the account.

1.16 WHEN a reader follows the `README_main.md` setup instructions (~line 230)
and creates the example `dda-build-policy` THEN the documentation instructs
them to attach a policy that grants `greengrass:*`, `iot:*`, and multiple S3
bucket-management actions all on `"Resource": "*"`.

1.17 WHEN a reader follows the `README_main.md` setup instructions (~line 256)
and creates the example `dda-greengrass-policy` THEN the documentation
instructs them to attach a policy that grants `greengrass:*` on `"Resource":
"*"`, S3 `GetObject` / `ListBucket` on `arn:aws:s3:::*` / `arn:aws:s3:::*/*`,
and multiple IoT actions on `"Resource": "*"`.

1.18 WHEN the repository is audited for the bug-condition patterns in the
in-scope files (I1–I17) — `resources: ['*']` in a CDK `PolicyStatement`
whose actions include any scopable action; `"Resource": "*"` /
`"Resource": ["arn:aws:...*"]` in JSON policies where the resource type
supports scoping; `greengrass:*` / `greengrassv2:*` / `iot:*` / `s3:*`
service-wildcard actions; and `sts:AssumeRole` on `resources: ['*']` or
`arn:aws:iam::*:role/...` — THEN the unfixed tree contains the disallowed
occurrences above with no documented, justified exception.

### Expected Behavior (Correct)

After the fix, every in-scope IAM policy statement grants the narrowest set of
`(Action, Resource)` pairs that supports the calling code path; service-
wildcard actions are replaced by their exercised subset; `sts:AssumeRole` is
bounded to a known set of account IDs; tag-based restrictions declared in
comments are enforced by `Condition` blocks; and every remaining wildcard is
isolated into its own statement with an in-file documented exception recording
that the action is unscopable.

2.1 WHEN `createLambdaRole(name)` in `compute-stack.ts` (~line 146) synthesizes
the portal Lambda execution role THEN the system SHALL split the combined
statement into per-service statements and SHALL scope each: SageMaker actions
to `arn:aws:sagemaker:*:*:training-job/dda-*`,
`arn:aws:sagemaker:*:*:compilation-job/dda-*`,
`arn:aws:sagemaker:*:*:labeling-job/dda-*`, and
`arn:aws:sagemaker:*:*:model/dda-*` (with `sagemaker:ListWorkteams` isolated
in its own statement on `*` per the AWS reference); Greengrass v2 to
`arn:aws:greengrass:*:*:components:*`, `arn:aws:greengrass:*:*:coreDevices:*`,
and `arn:aws:greengrass:*:*:deployments:*`, with an
`aws:ResourceTag/dda-portal:managed=true` condition where the API supports
it; IoT to `arn:aws:iot:*:*:thing/dda-*`, `arn:aws:iot:*:*:topic/dda/*`,
`arn:aws:iot:*:*:job/*`, and `arn:aws:iot:*:*:thinggroup/*`; `sts:AssumeRole`
to `arn:aws:iam::*:role/DDAPortalAccessRole` (matching the labeling-workflow
pattern; account wildcard will be tightened in I5); `execute-api:Invoke`
limited to the portal's own API Gateway ARN when statically known.
Additionally, the sibling `iam:PassRole` statement SHALL scope its resource
from `*` to `arn:aws:iam::*:role/DDA*Role` while preserving its
`iam:PassedToService=sagemaker.amazonaws.com` condition.

2.2 WHEN `createLambdaRole(name)` in `compute-stack.ts` (~line 183)
synthesizes the portal Lambda S3 grants THEN the system SHALL scope
`s3:GetObject`, `s3:PutObject`, `s3:ListBucket`, `s3:CreateBucket`,
`s3:GetBucketLocation`, and `s3:GetBucketTagging` to
`props.portalArtifactsBucket.bucketArn` and `props.portalArtifactsBucket.bucketArn + '/*'`
plus a tag-conditioned wildcard for buckets carrying
`aws:ResourceTag/dda-portal:managed=true`, and SHALL isolate
`s3:ListAllMyBuckets` into its own statement with `resources: ['*']` and an
in-code comment recording that the action is unscopable. The existing
cross-account comment SHALL be preserved and updated to note that access is
now enforced at both ends.

2.3 WHEN `UseCaseAccountStack` (~line 197) synthesizes the
`DDASageMakerExecutionRole` Ground Truth S3 grant THEN the system SHALL add
`Condition: StringEquals { 'aws:ResourceTag/dda-portal:managed': 'true' }` to
the statement (making the intent explicit) AND SHALL preserve a separate
statement allowlisting `arn:aws:s3:::sagemaker-*` and
`arn:aws:s3:::sagemaker-*/*` (unconditional, mirroring the `S3SageMakerAccess`
sid). A code comment SHALL document that Ground Truth input/output buckets
must carry the `dda-portal:managed=true` tag.

2.4 WHEN `UseCaseAccountStack` (~line 425) synthesizes the
`DDAPortalAccessRole` `S3BucketAccess` and `S3ObjectAccess` statements THEN
the system SHALL add `Condition: StringEquals { 'aws:ResourceTag/dda-portal:managed':
'true' }` to both statements — making the tag-based restriction promised by
the in-code comment actually enforced — and SHALL preserve the
`S3SageMakerAccess`, `ResourceTaggingAccess`, and every other statement in the
file byte-for-byte.

2.5 WHEN `LabelingWorkflowStack` (~line 44) synthesizes the
`LabelingMonitorFunction` role's `sts:AssumeRole` grant THEN the system SHALL
replace the `iam::*` wildcard account with the trusted UseCase account IDs
read from `props.trustedUseCaseAccountIds`, mapped to
`arn:aws:iam::${id}:role/DDAPortalAccessRole` per account; if the list is
unknown at synth time, the system SHALL read it from a documented deployment-
time SSM parameter and SHALL default to that parameter's value. The role name
`DDAPortalAccessRole` SHALL remain fixed.

2.6 WHEN `TrainingWorkflowStack` (~line 67) synthesizes the
`assumeRoleFunction` execution role THEN the system SHALL replace
`resources: ['*']` with the same trusted-account list as I5 (via
`props.trustedUseCaseAccountIds`), mapped to
`arn:aws:iam::${id}:role/DDAPortalAccessRole`. The runtime `ExternalId` check
performed inside the inline Lambda handler SHALL be preserved.

2.7 WHEN `deploy-account-role.sh` (~line 211) writes the `S3_POLICY` document
THEN the system SHALL narrow the first S3 statement's `Resource` list to the
union `arn:aws:s3:::dda-*`, `arn:aws:s3:::dda-*/*`, `arn:aws:s3:::sagemaker-*`,
`arn:aws:s3:::sagemaker-*/*`, and SHALL preserve the second (already-scoped)
`sagemaker-*` statement unchanged.

2.8 WHEN `deploy-account-role.sh` (~line 281) writes the `SAGEMAKER_POLICY`
document THEN the system SHALL scope the SageMaker statement's `Resource` to
`arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:training-job/dda-*`,
`arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:compilation-job/dda-*`,
`arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:labeling-job/dda-*`, and
`arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:model/dda-*`, and SHALL preserve the
sibling `iam:PassRole` statement (already scoped to
`arn:aws:iam::*:role/DDASageMakerExecutionRole`) unchanged.

2.9 WHEN `deploy-account-role.sh` (~line 378) writes the
`DDAPortalComponentAccessPolicy` `AllowDDABucketPatternAccess` sid THEN the
system SHALL narrow the S3 `Resource` list to `arn:aws:s3:::dda-*` and
`arn:aws:s3:::dda-*/*` only (removing the `arn:aws:s3:::*-dda-*` and
`arn:aws:s3:::*-dda-*/*` entries). The `AllowInferenceResultsUpload` and
`AllowPortalComponentBucketAccess` sids SHALL remain unchanged.

2.10 WHEN `create-edge-device-iam-role.sh` (~line 114) writes the
`GreengrassPermissions` sid THEN the system SHALL replace `greengrass:*` and
`greengrassv2:*` with the enumerable subset the edge device uses:
`greengrass:GetComponentVersionArtifact`,
`greengrass:ResolveComponentCandidates`,
`greengrass:GetDeploymentConfiguration`, `greengrassv2:GetDeployment`,
`greengrassv2:GetCoreDevice`, `greengrassv2:UpdateConnectivityInfo`,
`greengrassv2:ListComponents`, `greengrassv2:GetComponentVersionArtifact`, and
`greengrassv2:ResolveComponentCandidates`. `Resource` SHALL remain `"*"` with
an adjacent shell comment recording that Greengrass v1 API resource support is
limited and action-scoping is the primary defense on customer edge devices.

2.11 WHEN `create-edge-device-iam-role.sh` (~line 122) writes the
`IoTPermissions` sid THEN the system SHALL replace `iot:*` with the
enumerable subset the edge device uses, split by resource type: `iot:Connect`
on `arn:aws:iot:*:*:client/dda-*`; `iot:Publish`, `iot:Subscribe`, and
`iot:Receive` on `arn:aws:iot:*:*:topic/dda/*` and
`arn:aws:iot:*:*:topicfilter/dda/*`; `iot:GetThingShadow`,
`iot:UpdateThingShadow`, and `iot:DescribeThing` on
`arn:aws:iot:*:*:thing/dda-*`; and `iot:DescribeEndpoint` on `"*"` with an
adjacent shell comment recording that the action is unscopable.

2.12 WHEN `create-edge-device-iam-role.sh` (~line 130) writes the
`S3Permissions` sid THEN the system SHALL scope the S3 statement's `Resource`
to `arn:aws:s3:::dda-component-*`, `arn:aws:s3:::dda-component-*/*`,
`arn:aws:s3:::dda-inference-results-*`, and
`arn:aws:s3:::dda-inference-results-*/*`.

2.13 WHEN `launch-arm64-build-server.sh` (~line 155) writes the
`IoTPermissions` sid THEN the system SHALL replace `iot:*` with the build-
server-needed subset: `iot:DescribeThing`, `iot:DescribeEndpoint`,
`iot:UpdateThingShadow`, `iot:CreateThing`, `iot:AttachPolicy`, and
`iot:DescribeJob`, split by resource type where the action supports it (thing
ops on `arn:aws:iot:*:*:thing/dda-*`, job ops on `arn:aws:iot:*:*:job/*`) and
otherwise on `"*"` with an adjacent shell comment recording the reason.

2.14 WHEN `launch-arm64-build-server.sh` (~line 161) writes the
`S3Permissions` sid THEN the system SHALL scope the S3 statement's `Resource`
to `arn:aws:s3:::dda-component-*`, `arn:aws:s3:::dda-component-*/*`,
`arn:aws:s3:::dda-inference-results-*`, and
`arn:aws:s3:::dda-inference-results-*/*`, and SHALL isolate
`s3:ListAllMyBuckets` into its own statement with `"Resource": "*"` and an
adjacent shell comment recording that the action is unscopable.

2.15 WHEN `edge-device-iam-policy.json` (~line 34) is applied to an edge
device THEN the `IoTDataPlane` sid SHALL scope its resources by IoT resource
type: `iot:Connect` on `arn:aws:iot:*:*:client/dda-*`; `iot:Publish` on
`arn:aws:iot:*:*:topic/dda/*`; `iot:Subscribe` on
`arn:aws:iot:*:*:topicfilter/dda/*`; `iot:Receive` on
`arn:aws:iot:*:*:topic/dda/*`. The `GreengrassComponentDownload`,
`CloudWatchLogsUpload`, `AssumeDataAccountRole`, and `GreengrassConnectivity`
sids SHALL remain unchanged.

2.16 WHEN a reader follows the `README_main.md` setup instructions (~line
230) and creates the example `dda-build-policy` THEN the documentation SHALL
present the narrowed policy shape used in I13 / I14 (specific IoT actions,
S3 scoped to `dda-component-*` and `dda-inference-results-*`,
`s3:ListAllMyBuckets` in its own statement) and SHALL preserve the surrounding
prose explaining how to customize for larger scope.

2.17 WHEN a reader follows the `README_main.md` setup instructions (~line
256) and creates the example `dda-greengrass-policy` THEN the documentation
SHALL present the narrowed policy shape used in I10 / I11 / I15 (specific
Greengrass v2 edge-device actions; IoT actions scoped by resource type against
`dda-*` clients / things and `dda/*` topics; S3 scoped to `dda-component-*`
and `dda-inference-results-*`) and SHALL preserve the surrounding prose.

2.18 WHEN the repository is audited for the bug-condition patterns in the
in-scope files (I1–I17) THEN the system SHALL contain no remaining
disallowed occurrence — no `resources: ['*']` in a CDK `PolicyStatement`
whose actions include a scopable action, no `"Resource": "*"` /
`"Resource": ["arn:aws:...*"]` in the JSON policies for resource types that
support scoping, no `greengrass:*` / `greengrassv2:*` / `iot:*` /
`s3:*` service-wildcard actions, and no `sts:AssumeRole` on `resources:
['*']` or an `arn:aws:iam::*:role/...` wildcard account — other than
occurrences carrying a documented, justified exception (an in-file
`# nosec` / JSON comment / adjacent code comment recording why the wildcard
is required, e.g. `s3:ListAllMyBuckets`, `cloudwatch:PutMetricData`,
`sts:GetCallerIdentity`, `logs:DescribeLogGroups`-class,
`sagemaker:ListWorkteams`, `ecr:GetAuthorizationToken`, `tag:GetResources`,
`iot:DescribeEndpoint`). The audit SHALL be runnable and SHALL assert zero
disallowed hits minus documented exceptions.

### Unchanged Behavior (Regression Prevention)

All legitimate portal, cross-account, edge-device, build-server, and Ground
Truth flows must continue to work exactly as before. For every input that
does NOT trigger the bug condition — i.e. every `(Resource ARN, Action)` pair
that the code actually exercises at runtime — the fixed system must behave
identically to the original. Statements in the affected files that are NOT
one of I1–I17 must remain byte-for-byte identical, verified by `cdk synth`
snapshot comparison (I1–I6) and JSON-AST comparison (I7–I17).

3.1 WHEN the portal Lambda execution role is used by any of the portal
Lambda handlers (UseCases, Training, Labeling, Models, Deployments, …) to
call SageMaker / Greengrass / IoT / logs / STS / API Gateway APIs on
DDA-managed resources following the naming conventions THEN the system SHALL
CONTINUE TO succeed — every `(Action, Resource)` pair exercised at runtime
against `dda-*` prefixed resources, `dda/*` topics, and
`DDAPortalAccessRole` cross-account targets remains within the fixed
policy's allowed set.

3.2 WHEN the portal Lambda execution role is used to read / write / list /
create / tag the portal artifacts bucket, list all buckets, or access
`dda-portal:managed=true`-tagged buckets THEN the system SHALL CONTINUE TO
succeed identically; only untagged non-portal-artifacts buckets in the portal
account newly deny access.

3.3 WHEN the Ground Truth SageMaker execution role is used to read / write
labeling input+output data on buckets carrying the
`dda-portal:managed=true` tag OR on SageMaker's own `sagemaker-*` managed
buckets THEN the system SHALL CONTINUE TO succeed identically; every
existing Ground Truth training / labeling flow whose buckets are tagged (or
`sagemaker-*`) remains functional.

3.4 WHEN the `DDAPortalAccessRole` is assumed cross-account and used to
list / read / write / create / tag `dda-portal:managed=true`-tagged buckets
in the UseCase account THEN the system SHALL CONTINUE TO succeed identically;
the `S3SageMakerAccess`, `ResourceTaggingAccess`, `InferenceResultsAccess`,
and every other unrelated statement in the file SHALL remain byte-for-byte
identical.

3.5 WHEN the `LabelingMonitorFunction` assumes `DDAPortalAccessRole` in a
UseCase account listed in `trustedUseCaseAccountIds` THEN the system SHALL
CONTINUE TO succeed and monitor Ground Truth labeling jobs at the same
schedule and with the same downstream behavior; only assume-role attempts
against accounts NOT in the trusted list newly deny.

3.6 WHEN `TrainingWorkflowStack`'s `assumeRoleFunction` is invoked with a
`usecase.cross_account_role_arn` targeting an account in
`trustedUseCaseAccountIds` and passing the correct `ExternalId` THEN the
system SHALL CONTINUE TO succeed and return credentials with the same shape
and TTL as before; only assume-role attempts against accounts NOT in the
trusted list newly deny.

3.7 WHEN `deploy-account-role.sh` runs and applies the `S3_POLICY` to a
`DDASageMakerExecutionRole` that is used against `dda-*` or `sagemaker-*`
buckets THEN the system SHALL CONTINUE TO succeed identically; the second
(already-scoped) `sagemaker-*` statement remains byte-for-byte identical.

3.8 WHEN `deploy-account-role.sh` runs and applies the `SAGEMAKER_POLICY`
to a SageMaker exec role that operates on `dda-*` prefixed training,
compilation, labeling jobs, and models in the current account THEN the
system SHALL CONTINUE TO succeed identically; the sibling `iam:PassRole`
statement (scoped to `arn:aws:iam::*:role/DDASageMakerExecutionRole`) SHALL
remain unchanged.

3.9 WHEN `deploy-account-role.sh` runs and applies the
`DDAPortalComponentAccessPolicy` to a Greengrass device that pulls artifacts
from `arn:aws:s3:::dda-*` and `arn:aws:s3:::dda-*/*` buckets and uploads
inference results to `arn:aws:s3:::dda-inference-results-*` THEN the system
SHALL CONTINUE TO succeed identically; only the `*-dda-*` wildcard buckets
newly deny.

3.10 WHEN `create-edge-device-iam-role.sh` runs and provisions an edge
device that pulls Greengrass v2 component artifacts, resolves component
candidates, gets deployment configuration, and reports connectivity info
against Greengrass v2 APIs THEN the system SHALL CONTINUE TO succeed
identically; only Greengrass v1 mutating operations (that the edge device
never invokes) newly deny.

3.11 WHEN `create-edge-device-iam-role.sh` runs and provisions an edge
device that connects to IoT with a `dda-*` client id, publishes to
`dda/*` topics, subscribes to `dda/*` topic filters, receives on `dda/*`
topics, gets and updates thing shadows for `dda-*` things, describes its
own thing, and discovers the IoT endpoint THEN the system SHALL CONTINUE TO
succeed identically; only IoT operations against non-`dda` clients / topics
/ things newly deny.

3.12 WHEN `create-edge-device-iam-role.sh` runs and provisions an edge
device that reads Greengrass component artifacts from
`arn:aws:s3:::dda-component-*` and uploads inference results to
`arn:aws:s3:::dda-inference-results-*` THEN the system SHALL CONTINUE TO
succeed identically; only S3 reads/writes against non-DDA-prefixed buckets
newly deny.

3.13 WHEN `launch-arm64-build-server.sh` runs and provisions the ARM64
build server that describes IoT things and endpoints, updates thing
shadows, creates IoT things, attaches policies, and describes IoT jobs
against `dda-*` things and generic jobs THEN the system SHALL CONTINUE TO
succeed identically; only broader IoT operations (that the build server
never invokes) newly deny.

3.14 WHEN `launch-arm64-build-server.sh` runs and provisions the ARM64
build server that creates, reads, writes, lists, deletes, versions, tags,
and applies ACLs and bucket policies against buckets in the
`dda-component-*` and `dda-inference-results-*` families, plus lists all
buckets globally THEN the system SHALL CONTINUE TO succeed identically;
only S3 bucket-management operations against non-DDA-prefixed buckets newly
deny.

3.15 WHEN an edge device applies `edge-device-iam-policy.json` and connects
to IoT as a `dda-*` client, publishes to `dda/*` topics, subscribes to
`dda/*` topic filters, and receives on `dda/*` topics THEN the system SHALL
CONTINUE TO succeed identically; the `GreengrassComponentDownload`,
`CloudWatchLogsUpload`, `AssumeDataAccountRole`, and
`GreengrassConnectivity` sids SHALL remain unchanged.

3.16 WHEN a reader who has followed the updated `README_main.md`
`dda-build-policy` example and attached the narrowed policy to their build
server operates the DDA build workflow (component publishing, Greengrass
deployment creation, IoT thing registration) against `dda-*` prefixed
resources THEN the system SHALL CONTINUE TO succeed identically to the
original documented workflow.

3.17 WHEN a reader who has followed the updated `README_main.md`
`dda-greengrass-policy` example and attached the narrowed policy to their
edge device operates the DDA edge runtime (Greengrass component pull, IoT
data-plane, S3 component reads, inference-results upload) against `dda-*`
prefixed resources THEN the system SHALL CONTINUE TO succeed identically to
the original documented workflow.

3.18 WHEN the review's out-of-scope items are considered — the AWS services
and API operations that legitimately require `Resource: "*"` (e.g.
`s3:ListAllMyBuckets`, `cloudwatch:PutMetricData`, `sts:GetCallerIdentity`,
`logs:DescribeLogGroups`-class actions, `sagemaker:ListWorkteams`,
`ecr:GetAuthorizationToken`, `tag:GetResources` /
`resourcegroupstaggingapi:GetResources`, `iot:DescribeEndpoint`); the
generated / vendored duplicate copies of the in-scope files (`cdk.out/asset.*`
etc.); and the already-remediated batches
(`security-injection-deserialization-fixes` for findings #1–#8,
`security-secrets-credentials-jwt-fixes` for findings S1–S9) — THEN this
spec SHALL CONTINUE TO leave them unchanged.
