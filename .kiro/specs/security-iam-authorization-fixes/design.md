# IAM & Authorization Hardening (Group 4) Bugfix Design

## Overview

A code security review of the DefectDetectionApplication (DDA) — the AWS "DDA
Code Review" scan captured in `security-findings-report.json` (63 findings) —
surfaced a group of infrastructure-code findings concerning **IAM policy
scoping and authorization**. This spec is the fourth remediation group (stacked
after `security-secrets-credentials-jwt-fixes`); it is scoped **strictly** to
findings **I1–I17** across CDK stacks, customer-facing shell installers, JSON
policy templates, and the `README_main.md` example policies, plus the
repo-audit gate (Req 2.18 / I18).

The defect, across seventeen sites, is one of four shapes:

1. **Unscoped resource ARNs on scopable actions.** A single combined statement
   grants SageMaker, Greengrass, IoT, logs, `sts:AssumeRole`,
   `execute-api:Invoke`, and S3 CORS actions on `resources: ['*']` in the
   portal Lambda execution role (I1); portal Lambda S3 read/write is on
   `resources: ['*']` in the portal account despite the code comment claiming
   the access is "restricted by assumed role in UseCase Account" (I2); the
   Ground Truth `DDASageMakerExecutionRole` S3 grant is on
   `arn:aws:s3:::*` / `arn:aws:s3:::*/*` with no tag condition (I3); the
   inline SageMaker exec role's first S3 statement in `deploy-account-role.sh`
   is on `arn:aws:s3:::*` / `arn:aws:s3:::*/*` (I7) and its SageMaker
   statement is on `"Resource": "*"` (I8); the `create-edge-device-iam-role.sh`
   S3 statement is on `"Resource": "*"` (I12); the `launch-arm64-build-server.sh`
   S3 statement is on `"Resource": "*"` (I14); the edge-device
   `IoTDataPlane` sid in `edge-device-iam-policy.json` is on `"Resource": "*"`
   (I15).
2. **Service-wildcard actions over an enumerable subset.**
   `greengrass:*` / `greengrassv2:*` (I10, I16, I17), `iot:*` (I11, I13, I16,
   I17), and overly-broad S3 patterns `arn:aws:s3:::*-dda-*` that match any
   bucket containing the substring `dda` (I9) are granted where the code path
   only exercises a small, enumerable set of actions (Greengrass edge-device
   pulls / IoT data-plane connect+publish+subscribe+receive+shadow /
   `dda-*`-prefixed buckets).
3. **Tag-based restrictions declared in comments but never enforced.** The DDA
   Portal access role's `S3BucketAccess` and `S3ObjectAccess` sids in
   `usecase-account-stack.ts:~425` are documented as tag-conditioned ("Buckets
   must be tagged with `dda-portal:managed` = `true`") but the `Condition`
   block was never wired up (I4); the Ground Truth role's S3 grant carries the
   same intent (I3) but no `Condition`.
4. **Wildcard-account `sts:AssumeRole` grants.** The
   `LabelingMonitorFunction`'s `sts:AssumeRole` resource is
   `arn:aws:iam::*:role/DDAPortalAccessRole` — role name fixed, account
   portion wildcarded (I5); the `TrainingWorkflowStack`'s `assumeRoleFunction`
   is on `resources: ['*']` outright (I6).

The fix **splits combined statements** into per-service statements, **scopes
resource ARNs** to the naming-convention prefixes this spec commits to
(`dda-*`, `dda/*`, `dda-component-*`, `dda-inference-results-*`, `sagemaker-*`),
**enforces tag conditions** where the code comment already promises them,
**replaces service-wildcard actions** with the exercised subset, **bounds
`sts:AssumeRole`** to a config-driven list of trusted account IDs
(`props.trustedUseCaseAccountIds`), and **isolates every remaining wildcard**
into its own statement with an in-file documented exception (`s3:ListAllMyBuckets`,
`cloudwatch:PutMetricData`, `sts:GetCallerIdentity`,
`logs:DescribeLogGroups`-class, `sagemaker:ListWorkteams`,
`ecr:GetAuthorizationToken`, `tag:GetResources`,
`resourcegroupstaggingapi:GetResources`, `iot:DescribeEndpoint`). Every
statement in the affected files that is NOT one of I1–I17 remains
**byte-for-byte identical**, verified for the CDK stacks by `cdk synth`
snapshot comparison against a pre-fix baseline (I1–I6) and for the shell /
JSON / README paths by `jq` / JSON-AST comparison (I7–I17).

The fix splits into five ordering waves, worst-blast-radius last (see
Fix Implementation → Ordering and risk):

1. **README example policies (I16, I17)** — documentation-only, zero runtime
   blast radius; a customer who copies the policy today may see a change on
   their next install but no live infrastructure changes.
2. **Standalone JSON policy template (I15)** — the edge-device policy JSON is
   applied at device provisioning time; changing the file changes what future
   devices get but not any running device until it re-runs
   `attach-role-policy`.
3. **Shell installers (I7, I8, I9, I10, I11, I12, I13, I14)** — inline JSON
   heredocs in customer-run scripts; re-running the script updates the role
   policy. Bucketed together because they share the same JSON-AST assertion
   shape and no coupling to the CDK stack risk.
4. **CDK non-`sts:AssumeRole` fixes (I1, I2, I3, I4)** — synth-diff-verifiable
   PolicyStatement changes on the four in-scope stacks. Land these after the
   installer wave so the tag-conditioned statements (I3, I4) can be
   validated by the same tag/naming-convention baseline the installers rely
   on.
5. **CDK `sts:AssumeRole` fixes (I5, I6) + audit gate (I18)** — the highest-
   blast-radius wave: they change what accounts the portal Lambdas will
   assume roles into, which if mis-configured breaks every legitimate
   cross-account portal flow. Land last with the audit gate green so a
   regression to a wildcard account cannot silently sneak back in.

A **repo audit** (mirroring the sibling specs' `repo_audit.py` and
`secrets_audit.py`) is the exploration test and CI gate: `iam_audit.py` at
`test/backend-test/security/iam_audit.py` greps the 12 in-scope files for the
four bug-condition patterns — `resources: ['*']` in a CDK `PolicyStatement`
whose actions include a scopable action; `"Resource": "*"` /
`"Resource": ["arn:aws:...*"]` in JSON policies for resource types that
support scoping; `greengrass:*` / `greengrassv2:*` / `iot:*` / `s3:*`
service-wildcard actions; and `sts:AssumeRole` on `resources: ['*']` or an
`arn:aws:iam::*:role/...` wildcard account — and asserts zero disallowed hits
after the fix (minus documented exceptions). It must be non-empty on the
unfixed tree and zero-disallowed after.

Duplicate / generated copies of any in-scope file — the
`edge-cv-portal/infrastructure/cdk.out/asset.*` templates, any vendored
duplicate of the shell installers, and any other generated copy — are build
artifacts that regenerate from source and are **out of scope**; only the
real source paths are fixed, and the audit is scoped to exclude them.

## Glossary

- **Bug_Condition (C)**: An IAM `PolicyStatement` (in a CDK stack, an inline
  JSON heredoc in a shell installer, a standalone JSON template, or a README
  example block) that either (a) grants a scopable action on
  `resources: ['*']` / `"Resource": "*"`, (b) uses a `service:*` action
  wildcard when the code path exercises only an enumerable subset, (c) grants
  `sts:AssumeRole` on `resources: ['*']` or an `arn:aws:iam::*:role/...`
  wildcard account, or (d) declares a tag-based restriction in a code
  comment that is not enforced by a `Condition` — formally
  `resourceWildcardOnScopableAction(X) OR serviceWildcardActionOverEnumerableSet(X) OR
  wildcardAccountAssumeRole(X) OR commentPromisesRestrictionNotEnforced(X)`.
- **Property (P) / Fix Checking**: After the fix, for every buggy statement
  the resource ARNs match the naming-convention patterns (`dda-*`, `dda/*`,
  `dda-component-*`, `dda-inference-results-*`, `sagemaker-*`) OR carry an
  `aws:ResourceTag/dda-portal:managed=true` `Condition`; service-wildcard
  actions are replaced by the enumerable subset the code path uses;
  `sts:AssumeRole` resources reference specific account IDs from
  `props.trustedUseCaseAccountIds`; and any remaining wildcard is isolated
  into its own statement with an in-file documented exception.
- **Preservation**: For every input that does NOT trigger the bug condition,
  the fixed code behaves identically to the original — `F(X) = F'(X)`. Every
  legitimate portal / edge-device / build-server / cross-account / Ground
  Truth flow continues to succeed; every statement in the affected files
  that is NOT one of I1–I17 remains byte-for-byte identical (verified by
  `cdk synth` snapshot diff for I1–I6 and JSON-AST comparison for I7–I17).
- **F / F'**: The original (unfixed) infrastructure where the IAM statement
  grants a wildcard resource / a service-wildcard action / a wildcard
  account / an unenforced tag condition; and the fixed infrastructure where
  every statement grants the narrowest privilege set that supports the
  calling code path and any remaining wildcard carries a documented
  exception.
- **Naming conventions (committed by this spec)**: `dda-*` prefix for
  portal-created SageMaker resources (`training-job/dda-*`,
  `compilation-job/dda-*`, `labeling-job/dda-*`, `model/dda-*`) and
  portal-managed S3 buckets; `dda-*` / `dda/*` prefix for IoT resources
  (`client/dda-*`, `thing/dda-*`, `topic/dda/*`, `topicfilter/dda/*`);
  `dda-component-*` for Greengrass component artifact buckets;
  `dda-inference-results-*` for edge-device inference upload buckets;
  `sagemaker-*` for SageMaker's own managed buckets. Existing customer
  resources that predate these conventions must carry the tag
  `dda-portal:managed=true` to keep portal access.
- **`props.trustedUseCaseAccountIds: string[]`**: A new CDK stack prop this
  spec introduces on `LabelingWorkflowStackProps` (I5) and
  `TrainingWorkflowStackProps` (I6). Sourced from CDK context
  (`-c trustedUseCaseAccountIds=111,222,333`) or a documented deployment-
  time SSM parameter (default). Empty list is a synth-time error — the
  design DOES NOT fall back to a wildcard account.
- **`aws:ResourceTag/dda-portal:managed=true`**: The tag `Condition` this
  spec wires up on the S3 bucket-level and object-level statements in
  `usecase-account-stack.ts` (I3, I4) that the in-file code comment already
  promises. AWS evaluates the tag against the target bucket at request
  time; buckets without the tag deny.
- **In-scope files**: The twelve real source paths this spec owns —
  `compute-stack.ts` (I1, I2), `usecase-account-stack.ts` (I3, I4),
  `labeling-workflow-stack.ts` (I5), `training-workflow-stack.ts` (I6),
  `deploy-account-role.sh` (I7, I8, I9),
  `create-edge-device-iam-role.sh` (I10, I11, I12),
  `launch-arm64-build-server.sh` (I13, I14),
  `edge-device-iam-policy.json` (I15), `README_main.md` (I16, I17). The
  audit is scoped strictly to these; vendored / generated copies (`cdk.out/
  asset.*`, any duplicate under the workspace) are excluded.

## Bug Details

### Bug Condition

The bug manifests on any IAM policy statement in an in-scope file that grants
privileges wider than the calling code path actually uses. The seventeen
sites are: the portal Lambda execution role's combined SageMaker / Greengrass
/ IoT / logs / STS / API Gateway / S3 CORS statement on `resources: ['*']`
in `compute-stack.ts:~146` (I1); the same file's portal Lambda S3 statement
on `resources: ['*']` at `~183` (I2); the Ground Truth SageMaker execution
role's S3 grant on `arn:aws:s3:::*` at `usecase-account-stack.ts:~197` (I3);
the DDA Portal access role's `S3BucketAccess` / `S3ObjectAccess` sids at
`:~425` with an unenforced tag `Condition` (I4); the labeling monitor's
`sts:AssumeRole` on `arn:aws:iam::*:role/DDAPortalAccessRole` at
`labeling-workflow-stack.ts:~44` (I5); the training assume-role Lambda's
`sts:AssumeRole` on `resources: ['*']` at `training-workflow-stack.ts:~67`
(I6); the SageMaker exec role's inline S3 / SageMaker / Greengrass device
policies in `deploy-account-role.sh:~211/~281/~378` (I7, I8, I9); the
edge-device role's Greengrass / IoT / S3 inline policies in
`create-edge-device-iam-role.sh:~114/~122/~130` (I10, I11, I12); the ARM64
build server's IoT / S3 inline policies in
`launch-arm64-build-server.sh:~155/~161` (I13, I14); the edge-device
`IoTDataPlane` sid in `edge-device-iam-policy.json:~34` (I15); and the
`dda-build-policy` / `dda-greengrass-policy` example JSONs in
`README_main.md:~230/~256` (I16, I17).

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type PolicyStatement   // an IAM policy statement in an in-scope
                                     // CDK stack, shell installer, JSON
                                     // template, or README example block
  OUTPUT: boolean

  RETURN resourceWildcardOnScopableAction(X)      // Resource='*' on a scopable action
      OR serviceWildcardActionOverEnumerableSet(X) // greengrass:*, iot:*, ...
      OR wildcardAccountAssumeRole(X)              // sts:AssumeRole on *:role/...
      OR commentPromisesRestrictionNotEnforced(X)  // comment says tag-scoped, no Condition
END FUNCTION
```

**Expected behavior for buggy inputs (Fix Checking):**
```
FOR ALL X WHERE isBugCondition(X) DO
  result := F'(X)
  ASSERT scoped(result)
     // Resource ARNs match the naming-convention patterns (dda-*, dda/*,
     // dda-component-*, dda-inference-results-*, sagemaker-*) OR carry an
     // aws:ResourceTag/dda-portal:managed=true Condition, OR are in the
     // enumerated unscopable-action set with an in-file comment recording
     // why the wildcard is required.
  ASSERT enumeratedActions(result)
     // service:* action wildcards are replaced by the enumerable subset the
     // code path actually uses (Greengrass edge-device pulls / IoT data-
     // plane connect+publish+subscribe+receive+shadow / DDA S3 prefixes).
  ASSERT boundedAccounts(result)
     // sts:AssumeRole resources reference specific account IDs from the
     // props.trustedUseCaseAccountIds config, not arn:aws:iam::*:role/....
  ASSERT documentedException(result) WHEN X is in the unscopable set
     // For statements that must remain wildcarded (ListAllMyBuckets,
     // PutMetricData, GetCallerIdentity, DescribeLogGroups-class,
     // ListWorkteams, GetAuthorizationToken, GetResources,
     // DescribeEndpoint), the fix isolates them into their own statement
     // with an in-file comment recording the reason.
END FOR
```

### Examples

Unscoped resource ARN on scopable actions (bug manifestation on unfixed code):

- I1: `createLambdaRole('UseCases')` in `compute-stack.ts` synthesizes a
  `PolicyStatement` combining `sagemaker:CreateTrainingJob`,
  `greengrass:CreateComponentVersion`, `iot:CreateJob`,
  `logs:DescribeLogGroups`, `sts:AssumeRole`, `execute-api:Invoke`,
  `s3:GetBucketCors`, `s3:PutBucketCors`, … all on `resources: ['*']`.
  Expected after fix: split into per-service statements; SageMaker to
  `arn:aws:sagemaker:*:*:training-job/dda-*` (+ compilation/labeling/model
  siblings, ListWorkteams isolated on `*`); Greengrass v2 to
  `arn:aws:greengrass:*:*:components:*` / `:coreDevices:*` / `:deployments:*`;
  IoT to `arn:aws:iot:*:*:thing/dda-*` / `topic/dda/*` / `job/*` /
  `thinggroup/*`; `sts:AssumeRole` to
  `arn:aws:iam::*:role/DDAPortalAccessRole` (matching I5's pattern; account
  wildcard tightened by I5); `execute-api:Invoke` limited to the portal's
  own API Gateway ARN; the sibling `iam:PassRole` `resources: ['*']` scoped
  to `arn:aws:iam::*:role/DDA*Role`.
- I2: `createLambdaRole('UseCases')` emits an S3 statement on
  `resources: ['*']` in the portal account. Expected after fix: scope to
  `props.portalArtifactsBucket.bucketArn` + `/*` plus tag-conditioned
  buckets; `s3:ListAllMyBuckets` isolated on `*` with a comment.
- I3: `DDASageMakerExecutionRole` gets S3 on
  `resources: ['arn:aws:s3:::*', 'arn:aws:s3:::*/*']`. Expected after fix:
  add `Condition: StringEquals { 'aws:ResourceTag/dda-portal:managed':
  'true' }` and a sibling `sagemaker-*` allowlist statement.

Tag-based restriction promised but not enforced (bug manifestation on
unfixed code):

- I4: `DDAPortalAccessRole`'s `S3BucketAccess` sid grants `s3:ListBucket`,
  `s3:GetBucketVersioning`, `s3:CreateBucket`, `s3:PutBucketEncryption`, …
  on `resources: ['arn:aws:s3:::*']` — the in-code comment two lines above
  reads "Tag-based access for flexibility. Buckets must be tagged with
  `'dda-portal:managed' = 'true'`" but no `Condition` block is present.
  Expected after fix: `Condition: StringEquals {
  'aws:ResourceTag/dda-portal:managed': 'true' }` on the `S3BucketAccess`
  AND `S3ObjectAccess` sids; every other statement (`S3SageMakerAccess`,
  `ResourceTaggingAccess`, `SageMakerTraining`, …) byte-for-byte identical.

Wildcard-account `sts:AssumeRole` (bug manifestation on unfixed code):

- I5: `LabelingMonitorFunction` role grants `sts:AssumeRole` on
  `resources: ['arn:aws:iam::*:role/DDAPortalAccessRole']` — the role name
  is fixed but the account portion is wildcarded, so the monitor Lambda can
  assume the role in any AWS account. Expected after fix: replaced by a
  map over `props.trustedUseCaseAccountIds` producing
  `arn:aws:iam::${id}:role/DDAPortalAccessRole` per account.
- I6: `assumeRoleFunction` grants `sts:AssumeRole` on `resources: ['*']`
  with a comment "Will be scoped to specific roles with ExternalId" that
  acknowledges the gap but never closes it. Expected after fix: same
  `props.trustedUseCaseAccountIds`-driven list as I5; the runtime
  `ExternalId` check in the inline handler is preserved.

Service-wildcard action over an enumerable subset (bug manifestation on
unfixed code):

- I10: `create-edge-device-iam-role.sh` `GreengrassPermissions` sid grants
  `greengrass:*` and `greengrassv2:*` on `"Resource": "*"`. Expected after
  fix: replaced by the enumerable edge-device subset —
  `greengrass:GetComponentVersionArtifact`,
  `greengrass:ResolveComponentCandidates`,
  `greengrass:GetDeploymentConfiguration`, `greengrassv2:GetDeployment`,
  `greengrassv2:GetCoreDevice`, `greengrassv2:UpdateConnectivityInfo`,
  `greengrassv2:ListComponents`,
  `greengrassv2:GetComponentVersionArtifact`,
  `greengrassv2:ResolveComponentCandidates` — with `Resource` staying `"*"`
  and an adjacent shell comment recording Greengrass v1's limited resource
  support.
- I11: same file, `IoTPermissions` grants `iot:*` on `"Resource": "*"`.
  Expected after fix: replaced by `iot:Connect` on
  `arn:aws:iot:*:*:client/dda-*`; `iot:Publish` / `iot:Subscribe` /
  `iot:Receive` on `arn:aws:iot:*:*:topic/dda/*` and
  `arn:aws:iot:*:*:topicfilter/dda/*`; `iot:GetThingShadow` /
  `iot:UpdateThingShadow` / `iot:DescribeThing` on
  `arn:aws:iot:*:*:thing/dda-*`; `iot:DescribeEndpoint` isolated on `"*"`
  with a shell comment.

Substring match over the intended prefix (bug manifestation on unfixed
code):

- I9: `deploy-account-role.sh` `DDAPortalComponentAccessPolicy`'s
  `AllowDDABucketPatternAccess` sid includes `arn:aws:s3:::*-dda-*` and
  `arn:aws:s3:::*-dda-*/*` — these match any bucket with `dda` anywhere in
  its name (e.g. `my-dda-random-bucket`, `dda-inference-something`,
  `foo-dda-bar`). Expected after fix: remove the two `*-dda-*` entries,
  keep only `arn:aws:s3:::dda-*` and `arn:aws:s3:::dda-*/*`. The sibling
  `AllowInferenceResultsUpload` and `AllowPortalComponentBucketAccess` sids
  remain unchanged.

Edge cases (preserved, NOT buggy):

- `iam:PassRole` in `compute-stack.ts:~150` carries a
  `iam:PassedToService=sagemaker.amazonaws.com` condition; the resource is
  `*` but the condition scopes it. The I1 fix tightens the resource to
  `arn:aws:iam::*:role/DDA*Role` while preserving the condition — same
  effective allow-set for legitimate PassRole calls.
- `sagemaker:ListWorkteams` in the same combined statement is unscopable
  per the AWS IAM reference; the I1 fix isolates it into its own statement
  on `*` with a code comment.
- `tag:GetResources` / `resourcegroupstaggingapi:GetResources` — already in
  their own statement on `*`; unscopable per the AWS reference; not
  modified by any fix.
- The sibling `deploy-account-role.sh` `DDASageMakerExecutionRole` second
  S3 statement (already scoped to `sagemaker-*`) — I7 preserves it
  byte-for-byte.
- The `edge-device-iam-policy.json` `GreengrassComponentDownload`,
  `CloudWatchLogsUpload`, `AssumeDataAccountRole`, and
  `GreengrassConnectivity` sids — I15 preserves them byte-for-byte.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- The portal Lambda execution role continues to succeed on every
  `(Action, Resource)` pair the portal Lambdas actually exercise at
  runtime against `dda-*` prefixed SageMaker jobs / models, `dda-*` IoT
  things, `dda/*` IoT topics, `DDAPortalAccessRole` cross-account targets,
  the portal artifacts bucket, and tagged buckets (Req 3.1).
- The portal Lambda still reads / writes / lists / creates / tags the
  portal artifacts bucket, lists all buckets, and accesses
  `dda-portal:managed=true`-tagged buckets in the portal account (Req 3.2).
- The Ground Truth SageMaker execution role still reads / writes labeling
  input+output data on tagged buckets AND on SageMaker's own `sagemaker-*`
  managed buckets (Req 3.3).
- The `DDAPortalAccessRole` still lists / reads / writes / creates / tags
  `dda-portal:managed=true` buckets cross-account; the `S3SageMakerAccess`,
  `ResourceTaggingAccess`, `InferenceResultsAccess`, `SageMakerTraining`,
  and every other unrelated statement in `usecase-account-stack.ts`
  remain byte-for-byte identical (Req 3.4).
- The `LabelingMonitorFunction` still assumes `DDAPortalAccessRole` in
  UseCase accounts listed in `props.trustedUseCaseAccountIds`, at the same
  schedule and with the same downstream monitoring behavior (Req 3.5).
- The `TrainingWorkflowStack.assumeRoleFunction` still returns credentials
  with the same shape and TTL for cross-account role ARNs in
  `props.trustedUseCaseAccountIds`, passing the correct `ExternalId`; the
  runtime `ExternalId` check in the inline handler is unchanged (Req 3.6).
- `deploy-account-role.sh` continues to grant `DDASageMakerExecutionRole`
  access to `dda-*` and `sagemaker-*` buckets and to `dda-*`-prefixed
  SageMaker jobs / models in the current account (Req 3.7, 3.8).
- `deploy-account-role.sh` continues to grant Greengrass devices access to
  `dda-*` / `dda-inference-results-*` / `dda-component-*` buckets; the
  `AllowInferenceResultsUpload` and `AllowPortalComponentBucketAccess`
  sids are unchanged (Req 3.9).
- `create-edge-device-iam-role.sh` continues to grant edge devices the
  Greengrass v2 component pull / deployment / connectivity actions they
  actually invoke, the IoT connect / publish / subscribe / receive /
  shadow / describe-thing / describe-endpoint actions on `dda-*` clients /
  things and `dda/*` topics, and S3 reads on `dda-component-*` and writes
  on `dda-inference-results-*` (Req 3.10, 3.11, 3.12).
- `launch-arm64-build-server.sh` continues to grant the ARM64 build server
  the IoT describe-thing / describe-endpoint / create-thing / attach-policy
  / describe-job actions on `dda-*` things and jobs, and S3
  create/read/write/list/delete/versioning/tagging/ACL/policy on
  `dda-component-*` and `dda-inference-results-*` buckets, plus
  `s3:ListAllMyBuckets` globally (Req 3.13, 3.14).
- `edge-device-iam-policy.json` continues to grant IoT data-plane on
  `dda-*` clients and `dda/*` topics; the `GreengrassComponentDownload`,
  `CloudWatchLogsUpload`, `AssumeDataAccountRole`, and
  `GreengrassConnectivity` sids are byte-for-byte identical (Req 3.15).
- The `README_main.md` prose around `dda-build-policy` and
  `dda-greengrass-policy` (the sentences that describe when and why to
  attach each policy) is unchanged; only the JSON body changes to the
  narrowed shape (Req 3.16, 3.17).

**Scope:**
All inputs that do NOT trigger the bug condition must be completely
unaffected. This explicitly includes:
- Every AWS service / API operation that legitimately requires
  `Resource: "*"` (`s3:ListAllMyBuckets`, `cloudwatch:PutMetricData`,
  `sts:GetCallerIdentity`, `logs:DescribeLogGroups`-class,
  `sagemaker:ListWorkteams`, `ecr:GetAuthorizationToken`,
  `tag:GetResources` / `resourcegroupstaggingapi:GetResources`,
  `iot:DescribeEndpoint`). Where a finding covers a statement that mixes
  scopable and unscopable actions, the fix isolates the unscopable
  actions into their own statement.
- Every generated / vendored duplicate copy of the in-scope files
  (`cdk.out/asset.*`, any vendored shell script under a build tree).
- Every finding from the sibling remediation batches — findings #1–#8
  (injection / deserialization) and S1–S9 (secrets / credentials / JWT).
  These are already remediated on separate branches and this spec makes
  no changes to those files (Req 3.18).

**Note:** The expected correct behavior for buggy inputs is defined in the
Correctness Properties section (Property 1); this section focuses on what
must NOT change.

## Hypothesized Root Cause

The infrastructure code was written for an early single-tenant deployment
context where an operator wanted the portal Lambdas and the customer-run
installers to "just work" against whatever buckets / things / topics /
accounts happened to be present, so IAM statements were widened to the
maximum shape that avoided broken flows during development. As the naming
conventions and cross-account model firmed up (portal-created resources use
the `dda-*` prefix, edge-device buckets are `dda-component-*` /
`dda-inference-results-*`, cross-account access goes through
`DDAPortalAccessRole`), the statements were not narrowed back. Concretely:

1. **Combined per-service statements (I1).** `createLambdaRole` in
   `compute-stack.ts` was written as a single mega-statement grouping every
   API the portal Lambdas might ever call, which forced `resources: ['*']`
   because the actions span services with heterogeneous ARN shapes. The fix
   splits the statement per-service so each can be scoped.

2. **Portal-account S3 comment misread (I2).** The code comment
   "Restricted by assumed role in UseCase Account" is true only for the
   cross-account access path (through the DDAPortalAccessRole); it does not
   apply to the portal-account S3 access this statement authorizes, but a
   reader may believe it does and leave `resources: ['*']` in place.

3. **Tag-condition intent stated but not wired (I3, I4).** The in-code
   comment reads "Tag-based access for flexibility. Buckets must be tagged
   with `'dda-portal:managed' = 'true'`", but the CDK `PolicyStatement`
   was constructed without a `conditions:` block. The fix wires the
   condition the comment already promises.

4. **`sts:AssumeRole` wildcarded pending "future scoping" (I5, I6).** The
   `TrainingWorkflowStack` code comment "Will be scoped to specific roles
   with ExternalId" acknowledges the gap; the `LabelingMonitorFunction`
   scoped the role name but left the account portion wildcarded, matching
   a common CDK pattern where the exact account set is not known at synth
   time. The fix introduces `props.trustedUseCaseAccountIds` so operators
   supply the list at deploy time (via CDK context or SSM parameter).

5. **`service:*` action wildcards in edge-device / build-server templates
   (I10, I11, I13, I16, I17).** The customer-run installers and README
   examples pre-date the "least-privilege by default" posture; they granted
   `greengrass:*` / `iot:*` because listing the exact actions manually was
   tedious and the templates run on customer-owned infrastructure where
   action-scoping is the primary defense. The fix enumerates the actions
   the code path actually exercises.

6. **Substring match (I9).** `arn:aws:s3:::*-dda-*` was intended to cover
   customer buckets that carry `dda` somewhere in the name (e.g. a naming
   prefix like `mycompany-dda-inference`), but it also matches unrelated
   buckets containing the substring. The fix restricts to the exact
   `dda-*` prefix.

7. **Copy-pasteable README policies (I16, I17).** The example JSONs were
   written to be short and readable at review time; readers copy them
   verbatim, so the wildcards land in customer accounts. The fix replaces
   the JSON bodies with the same narrowed shape used by the shell
   installers, preserving the surrounding prose.

## Correctness Properties

Property 1: Bug Condition — Scoped ARNs, enumerated actions, bounded accounts, documented exceptions

_For any_ IAM policy statement where the bug condition holds
(`isBugCondition` returns true — a scopable action on a wildcard resource, a
`service:*` action wildcard, a wildcard-account `sts:AssumeRole`, or a
comment-declared restriction not enforced by a `Condition`), the fixed code
SHALL grant the narrowest privilege set that supports the calling code
path: resource ARNs match the committed naming-convention patterns
(`arn:aws:sagemaker:*:*:{training-job,compilation-job,labeling-job,model}/dda-*`;
`arn:aws:greengrass:*:*:{components,coreDevices,deployments}:*`;
`arn:aws:iot:*:*:{thing/dda-*,topic/dda/*,topicfilter/dda/*,client/dda-*,job/*,thinggroup/*}`;
`arn:aws:s3:::{dda-*,dda-component-*,dda-inference-results-*,sagemaker-*}` and
`/*` object children; and `props.portalArtifactsBucket.bucketArn`) OR carry
`Condition: StringEquals { 'aws:ResourceTag/dda-portal:managed': 'true' }`;
`greengrass:*` / `greengrassv2:*` / `iot:*` / `s3:*` service-wildcard
actions are replaced by the enumerable subset the code path exercises
(edge-device Greengrass pulls / IoT data-plane connect+publish+subscribe+
receive+shadow / DDA-prefixed S3 reads/writes / build-server thing+job+
shadow); `sts:AssumeRole` resources reference specific account IDs from
`props.trustedUseCaseAccountIds` mapped to
`arn:aws:iam::${id}:role/DDAPortalAccessRole` (no `iam::*` wildcard); and
every remaining wildcard is isolated into its own statement with an in-file
documented exception (`s3:ListAllMyBuckets`, `cloudwatch:PutMetricData`,
`sts:GetCallerIdentity`, `logs:DescribeLogGroups`-class,
`sagemaker:ListWorkteams`, `ecr:GetAuthorizationToken`,
`tag:GetResources`, `resourcegroupstaggingapi:GetResources`,
`iot:DescribeEndpoint`). A full-repo audit for the bug-condition patterns
across in-scope files finds no remaining disallowed occurrence, other than
occurrences carrying a documented, justified exception (an in-file
`# nosec` / JSON-adjacent shell comment / CDK code comment recording why
the wildcard is required).

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 2.12, 2.13, 2.14, 2.15, 2.16, 2.17, 2.18**

Property 2: Preservation — No behavior change for legitimate inputs

_For any_ IAM policy statement where the bug condition does NOT hold
(`isBugCondition` returns false — a legitimate portal / cross-account /
edge-device / build-server / Ground Truth code path against a resource
that matches the committed naming conventions or carries the
`dda-portal:managed=true` tag), the fixed code SHALL produce the same
result as the original code (`F(X) = F'(X)`), preserving: every
`(Action, Resource)` pair the portal Lambdas exercise at runtime against
`dda-*` prefixed SageMaker jobs / models, `dda-*` IoT things, `dda/*`
topics, `DDAPortalAccessRole` cross-account targets, the portal artifacts
bucket, and tagged buckets; the Ground Truth role's access to tagged and
`sagemaker-*` buckets; the `DDAPortalAccessRole`'s cross-account
list/read/write/create/tag operations against tagged buckets and every
other sid in `usecase-account-stack.ts` byte-for-byte; the
`LabelingMonitorFunction`'s and `assumeRoleFunction`'s success against
accounts in `props.trustedUseCaseAccountIds` (with `ExternalId` check
unchanged); the shell installers' access to `dda-*` / `sagemaker-*` /
`dda-component-*` / `dda-inference-results-*` buckets and `dda-*` IoT
resources; the edge-device JSON's `GreengrassComponentDownload`,
`CloudWatchLogsUpload`, `AssumeDataAccountRole`, `GreengrassConnectivity`
sids byte-for-byte; and the `README_main.md` prose around the example
policies. For the CDK stacks (I1–I6), the synthesized CloudFormation
templates (`edge-cv-portal/infrastructure/cdk.out/*.template.json`) change
only in the statements enumerated by I1–I6 — every other JSON node is
byte-for-byte identical against a pre-fix baseline.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15, 3.16, 3.17, 3.18**

## Fix Implementation

### Changes Required

Assuming the root-cause analysis is correct, each site gets the minimal
change that makes `isBugCondition` false for it while preserving
`F(X) = F'(X)`.

#### I1 — `edge-cv-portal/infrastructure/lib/compute-stack.ts` (Req 2.1) — split combined statement, per-service scoping

**Function**: `createLambdaRole(name)` (the combined
SageMaker/Greengrass/IoT/logs/STS/API-Gateway/S3-CORS `PolicyStatement`
starting at ~line 87, currently landing ~line 146 with drift; and the
adjacent `iam:PassRole` statement).

1. **Split the single combined statement into six per-service statements**
   and drop the umbrella `resources: ['*']`:
   1. **SageMaker (scopable)**: `sagemaker:CreateTrainingJob`,
      `DescribeTrainingJob`, `ListTrainingJobs`, `CreateCompilationJob`,
      `DescribeCompilationJob`, `ListCompilationJobs`, `CreateLabelingJob`,
      `DescribeLabelingJob`, `ListLabelingJobs`, `StopLabelingJob`,
      `DescribeWorkteam`, `AddTags` on
      `arn:aws:sagemaker:*:*:training-job/dda-*`,
      `arn:aws:sagemaker:*:*:compilation-job/dda-*`,
      `arn:aws:sagemaker:*:*:labeling-job/dda-*`,
      `arn:aws:sagemaker:*:*:model/dda-*`, and
      `arn:aws:sagemaker:*:*:workteam/*` (for `DescribeWorkteam`).
   2. **SageMaker (unscopable)**: `sagemaker:ListWorkteams` in its own
      statement on `resources: ['*']` with a code comment:
      `// sagemaker:ListWorkteams does not support resource-level permissions per the AWS IAM reference.`
   3. **Greengrass v2 (scopable)**: `greengrass:CreateComponentVersion`,
      `DescribeComponent`, `GetComponent`, `ListComponents`,
      `ListComponentVersions`, `ListCoreDevices`, `GetCoreDevice`,
      `ListInstalledComponents`, `ListEffectiveDeployments`,
      `ListTagsForResource`, `TagResource`, `ListDeployments`,
      `GetDeployment`, `CreateDeployment`, `CancelDeployment` on
      `arn:aws:greengrass:*:*:components:*`,
      `arn:aws:greengrass:*:*:coreDevices:*`,
      `arn:aws:greengrass:*:*:deployments:*`. Where the Greengrass v2 API
      supports it, add
      `conditions: { StringEquals: { 'aws:ResourceTag/dda-portal:managed': 'true' } }`;
      otherwise the broad service ARN + code comment recording that
      resource-tag conditions on the Greengrass v2 API are limited.
   4. **IoT (scopable)**: `iot:DescribeThing`, `DescribeThingGroup`,
      `GetThingType`, `ListThings`, `ListThingsInThingGroup`,
      `ListThingGroups`, `CreateThingGroup`, `AddThingToThingGroup`,
      `RemoveThingFromThingGroup`, `CreateJob`, `DescribeJob`, `UpdateJob`,
      `GetJobDocument`, `ListJobs`, `CancelJob`, `GetThingShadow`,
      `UpdateThingShadow`, `DeleteThingShadow` on
      `arn:aws:iot:*:*:thing/dda-*`, `arn:aws:iot:*:*:topic/dda/*`,
      `arn:aws:iot:*:*:job/*`, `arn:aws:iot:*:*:thinggroup/*`.
   5. **IoT (unscopable)**: `iot:DescribeEndpoint` in its own statement on
      `resources: ['*']` with a code comment:
      `// iot:DescribeEndpoint does not support resource-level permissions.`
   6. **CloudWatch Logs read (unscopable-ish)**: `logs:GetLogEvents`,
      `DescribeLogStreams`, `DescribeLogGroups`, `FilterLogEvents` in
      their own statement on `resources: ['*']` with a code comment:
      `// logs:DescribeLogGroups and the Filter/Get log-events reads do not usefully scope by log-group ARN when the portal must query arbitrary Greengrass log groups.`
   7. **STS AssumeRole**: `sts:AssumeRole` scoped to
      `arn:aws:iam::*:role/DDAPortalAccessRole` (matching the
      labeling-workflow pattern; the `iam::*` account wildcard is
      tightened by I5). Code comment cross-references I5 and I6 as the
      places that bound the account list.
   8. **execute-api**: `execute-api:Invoke` scoped to the portal's own
      API Gateway ARN when statically known. This spec's implementation
      captures `props.api.arnForExecuteApi()` (or the equivalent
      `arn:aws:execute-api:${region}:${account}:${apiId}/*`) into the
      role construction. If the ARN cannot be resolved at `createLambdaRole`
      time (e.g. the role is built before the API), the fallback is a
      portal-account-scoped
      `arn:aws:execute-api:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:*/*` with
      a code comment.
   9. **S3 CORS on portal artifacts**: `s3:GetBucketCors`,
      `s3:PutBucketCors` scoped to `props.portalArtifactsBucket.bucketArn`.
      (Cross-account CORS on Data Account buckets is separately granted
      via the assumed role, unchanged.)
2. **Tighten the sibling `iam:PassRole` statement** (currently
   `resources: ['*']` at ~line 158, with
   `iam:PassedToService=sagemaker.amazonaws.com` condition). Change its
   resource to `arn:aws:iam::*:role/DDA*Role` (matches
   `DDAPortalAccessRole`, `DDASageMakerExecutionRole`,
   `DDAPortalDataAccessRole`, and any future `DDA…Role`). Preserve the
   `iam:PassedToService=sagemaker.amazonaws.com` condition byte-for-byte.
3. **Preserve every other statement in the file byte-for-byte** —
   `useCasesHandler.addToRolePolicy(...)`, `devicesHandler.addToRolePolicy`
   (the IoT Secure Tunneling grant), the Cognito grant for `AuthHandler`,
   the `sharedComponentsHandler` policies at
   `${componentBucketName}` (already scoped), and the `tag:GetResources`
   statement at ~line 195 (unscopable, already isolated in its own
   statement) are unchanged. This is verified by `cdk synth` snapshot
   diff — the pre-fix `cdk.out/EdgeCVPortalComputeStack.template.json`
   captured to a baseline; the post-fix template must equal it except
   inside the emitted statements for the split I1 rewrite.

#### I2 — `edge-cv-portal/infrastructure/lib/compute-stack.ts` (Req 2.2) — scope portal S3 grant

**Function**: `createLambdaRole(name)` (the S3 statement at ~line 183 with
the "Restricted by assumed role in UseCase Account" comment; note the
comment is misleading — this statement is portal-account access and is
not restricted by any assumed role).

1. **Replace `resources: ['*']`** with two resources:
   `props.portalArtifactsBucket.bucketArn` and
   `` `${props.portalArtifactsBucket.bucketArn}/*` `` (bucket and object
   levels). Keep the same actions list except `s3:ListAllMyBuckets`.
2. **Add a tag-conditioned wildcard statement** for
   `dda-portal:managed=true` buckets:
   ```typescript
   role.addToPolicy(new iam.PolicyStatement({
     effect: iam.Effect.ALLOW,
     actions: ['s3:GetObject', 's3:PutObject', 's3:ListBucket',
               's3:GetBucketLocation', 's3:GetBucketTagging'],
     resources: ['arn:aws:s3:::*'],
     conditions: { StringEquals: {
       'aws:ResourceTag/dda-portal:managed': 'true'
     } },
   }));
   ```
   The comment on this block records that the wildcard resource is
   safe because the `Condition` gates it to portal-managed buckets.
3. **Isolate `s3:ListAllMyBuckets`** into its own statement on
   `resources: ['*']` with a code comment:
   `// s3:ListAllMyBuckets does not support resource-level permissions per the AWS IAM reference; this statement is intentionally on '*'.`
4. **Update the misleading "Restricted by assumed role in UseCase
   Account" comment** to note that access is now enforced at both ends:
   the portal-account grant is scoped to `portalArtifactsBucket` +
   tagged buckets; cross-account access still goes through the assumed
   `DDAPortalAccessRole`.
5. **Preserve every other statement in the file byte-for-byte** —
   verified by the same `cdk synth` snapshot diff as I1.

#### I3 — `edge-cv-portal/infrastructure/lib/usecase-account-stack.ts` (Req 2.3) — Ground Truth tag condition + sagemaker-* allowlist

**Function**: `UseCaseAccountStack` constructor, the
`groundTruthRole.addToPolicy(...)` block at ~line 197 (the S3 statement
on `arn:aws:s3:::*` / `arn:aws:s3:::*/*`).

1. **Split into two statements**:
   1. **Tag-conditioned statement** covering the same actions
      (`s3:GetObject`, `PutObject`, `DeleteObject`, `ListBucket`,
      `GetBucketLocation`, `GetBucketCors`, `PutBucketCors`) on
      `arn:aws:s3:::*` and `arn:aws:s3:::*/*` with
      `conditions: { StringEquals: {
        'aws:ResourceTag/dda-portal:managed': 'true'
      } }`. A code comment records that Ground Truth input/output buckets
      MUST carry the `dda-portal:managed=true` tag.
   2. **Unconditional `sagemaker-*` allowlist** for the same actions on
      `arn:aws:s3:::sagemaker-*` and `arn:aws:s3:::sagemaker-*/*`
      (mirrors the `S3SageMakerAccess` sid in the DDAPortalAccessRole
      block for consistency).
2. **Preserve every sibling statement byte-for-byte** — the CloudWatch
   Logs statement, the SageMaker training/compilation/labeling
   statement (in scope of a different finding, addressed nowhere in
   this spec), the ECR statement, and the cross-account Data Account
   statements (`CrossAccountDataBucketRead`, `CrossAccountDataBucketWrite`).
   Verified by `cdk synth` snapshot diff against the pre-fix
   `DDAPortalUseCaseAccountStack.template.json`.

#### I4 — `edge-cv-portal/infrastructure/lib/usecase-account-stack.ts` (Req 2.4) — wire the tag Condition the comment promises

**Function**: `UseCaseAccountStack` constructor, the two
`this.role.addToPolicy(...)` blocks at ~line 425 for the
`DDAPortalAccessRole` (`S3BucketAccess` sid at ~line 428 and
`S3ObjectAccess` sid at ~line 447).

1. **Add `Condition: StringEquals { 'aws:ResourceTag/dda-portal:managed':
   'true' }`** to the `S3BucketAccess` `PolicyStatement` (bucket-level
   actions on `arn:aws:s3:::*`). The in-code comment above the block
   ("Tag-based access for flexibility. Buckets must be tagged with
   `'dda-portal:managed' = 'true'`") remains — the comment now
   accurately describes the enforced behavior.
2. **Add the same `Condition`** to the `S3ObjectAccess` `PolicyStatement`
   (object-level actions on `arn:aws:s3:::*/*`).
3. **Preserve every sibling statement byte-for-byte** — the
   `SageMakerTraining`, `SageMakerCompilation`, `SageMakerAlgorithm`,
   `GroundTruthLabelingV2`, `GroundTruthWorkteams`, `ResourceTaggingAccess`,
   `S3SageMakerAccess`, `CloudWatchLogs`, and every other sid in the file
   is unchanged. Verified by `cdk synth` snapshot diff.

#### I5 — `edge-cv-portal/infrastructure/lib/labeling-workflow-stack.ts` (Req 2.5) — bounded account list

**Function**: `LabelingWorkflowStack` constructor, the
`this.monitorFunction.addToRolePolicy(...)` call at ~line 44 (the
`sts:AssumeRole` on `arn:aws:iam::*:role/DDAPortalAccessRole`).

1. **Add `trustedUseCaseAccountIds: string[]` to
   `LabelingWorkflowStackProps`** as a required prop.
   `LabelingWorkflowStack`'s constructor validates it is non-empty at
   synth time (empty array throws), and reads from CDK context
   (`this.node.tryGetContext('trustedUseCaseAccountIds')`) or a
   documented SSM parameter (`/dda-portal/trusted-usecase-account-ids`)
   as the default fallback at deploy time.
2. **Replace the wildcard-account resource** with the mapped list:
   ```typescript
   resources: props.trustedUseCaseAccountIds.map(
     id => `arn:aws:iam::${id}:role/DDAPortalAccessRole`
   ),
   ```
   The role name `DDAPortalAccessRole` stays fixed; only the account
   portion is scoped.
3. **Update the app entry (`bin/*.ts`)** to pass
   `trustedUseCaseAccountIds` to `new LabelingWorkflowStack(...)`. The
   value is sourced from CDK context passed via
   `-c trustedUseCaseAccountIds=111111111111,222222222222,...` or read
   from the SSM parameter at synth time.
4. **Preserve every sibling statement byte-for-byte** — the
   `labelingJobsTable.grantReadWriteData`, `useCasesTable.grantReadData`,
   the EventBridge rules, and the CfnOutput.

#### I6 — `edge-cv-portal/infrastructure/lib/training-workflow-stack.ts` (Req 2.6) — bounded account list (same pattern as I5)

**Function**: `TrainingWorkflowStack` constructor, the
`assumeRoleFunction.addToRolePolicy(...)` call at ~line 67 (the
`sts:AssumeRole` on `resources: ['*']` with the "Will be scoped to
specific roles with ExternalId" comment).

1. **Add `trustedUseCaseAccountIds: string[]` to
   `TrainingWorkflowStackProps`** as a required prop (same pattern as
   I5).
2. **Replace `resources: ['*']`** with the mapped list:
   ```typescript
   resources: props.trustedUseCaseAccountIds.map(
     id => `arn:aws:iam::${id}:role/DDAPortalAccessRole`
   ),
   ```
   Remove the "Will be scoped to specific roles with ExternalId" comment
   (the scoping is now in place) OR update it to record that scoping is
   at synth time via the trusted list, while the runtime `ExternalId`
   check remains in the inline handler.
3. **Preserve the inline Lambda `handler(event, context)` code
   byte-for-byte** — the `sts.assume_role(RoleArn=role_arn,
   RoleSessionName='EdgeCVPortalTraining', ExternalId=external_id,
   DurationSeconds=3600)` call is unchanged; the runtime `ExternalId`
   check is preserved (defense-in-depth alongside the synth-time
   account scoping).
4. **Update the app entry (`bin/*.ts`)** to pass
   `trustedUseCaseAccountIds`; same sourcing as I5.
5. **Preserve every sibling function and Step Function state byte-for-
   byte** — `startTrainingFunction`, `checkTrainingStatusFunction`,
   `startCompilationFunction`, `publishComponentFunction`, and the
   Step Functions chain (`waitForTraining` → `startCompilationTask` →
   … → `sendSuccessNotification`) are unchanged.

#### I7 — `edge-cv-portal/deploy-account-role.sh` (Req 2.7) — narrow S3 to DDA + sagemaker prefixes

**Function**: `single-account` deployment branch, the `S3_POLICY`
heredoc at ~line 211.

1. **Narrow the first statement's `Resource` list** from
   `["arn:aws:s3:::*", "arn:aws:s3:::*/*"]` to
   `["arn:aws:s3:::dda-*", "arn:aws:s3:::dda-*/*",
   "arn:aws:s3:::sagemaker-*", "arn:aws:s3:::sagemaker-*/*"]`.
2. **Preserve the second statement byte-for-byte** (the already-scoped
   `sagemaker-*` read allowlist), the surrounding `LOGS_POLICY`,
   `SAGEMAKER_POLICY`, `PASS_ROLE_POLICY`, `ECR_POLICY`, and the
   `aws iam put-role-policy` invocations.
3. Add a shell comment above the heredoc recording the naming
   conventions and why the union of `dda-*` and `sagemaker-*` is the
   correct scope.

#### I8 — `edge-cv-portal/deploy-account-role.sh` (Req 2.8) — narrow SageMaker actions to dda-* prefixed resources

**Function**: `single-account` deployment branch, the `SAGEMAKER_POLICY`
heredoc at ~line 281.

1. **Narrow the first statement's `Resource`** from `"*"` to
   `["arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:training-job/dda-*",
   "arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:compilation-job/dda-*",
   "arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:labeling-job/dda-*",
   "arn:aws:sagemaker:*:${CURRENT_ACCOUNT}:model/dda-*"]`.
2. **Preserve the sibling `iam:PassRole` / `iam:GetRole` statement
   byte-for-byte** — it is already scoped to
   `arn:aws:iam::*:role/DDASageMakerExecutionRole`.
3. Since the SageMaker actions in this heredoc do not include
   `ListWorkteams`, no unscopable-action isolation is required for
   this file.

#### I9 — `edge-cv-portal/deploy-account-role.sh` (Req 2.9) — remove substring-match bucket patterns

**Function**: `single-account` deployment branch, the
`GREENGRASS_POLICY` heredoc at ~line 378, the
`AllowDDABucketPatternAccess` sid.

1. **Narrow the `Resource` list** from
   `["arn:aws:s3:::dda-*", "arn:aws:s3:::dda-*/*",
   "arn:aws:s3:::*-dda-*", "arn:aws:s3:::*-dda-*/*"]` to
   `["arn:aws:s3:::dda-*", "arn:aws:s3:::dda-*/*"]` (remove the two
   `*-dda-*` entries).
2. **Preserve the `AllowPortalComponentBucketAccess`,
   `AllowInferenceResultsUpload`, `AllowEcrAuthToken`, and
   `AllowEcrImagePull` sids byte-for-byte.**

#### I10 — `station_install/create-edge-device-iam-role.sh` (Req 2.10) — enumerate Greengrass actions

**Function**: The `INLINE_POLICY` heredoc, `GreengrassPermissions` sid
at ~line 114.

1. **Replace the `Action` list** `["greengrass:*", "greengrassv2:*"]`
   with the enumerable subset the edge device uses:
   `["greengrass:GetComponentVersionArtifact",
     "greengrass:ResolveComponentCandidates",
     "greengrass:GetDeploymentConfiguration",
     "greengrassv2:GetDeployment",
     "greengrassv2:GetCoreDevice",
     "greengrassv2:UpdateConnectivityInfo",
     "greengrassv2:ListComponents",
     "greengrassv2:GetComponentVersionArtifact",
     "greengrassv2:ResolveComponentCandidates"]`.
2. **Keep `"Resource": "*"`** with an adjacent shell comment recording
   that Greengrass v1 API resource support is limited and action-scoping
   is the primary defense on customer edge devices.
3. **Preserve the sibling sids byte-for-byte** — `IoTPermissions` (I11
   modifies it separately), `S3Permissions` (I12), `CloudWatchLogsPermissions`,
   `CloudWatchMetricsPermissions`, `ECRPermissions`, `STSPermissions`.

#### I11 — `station_install/create-edge-device-iam-role.sh` (Req 2.11) — enumerate IoT actions, split by resource type

**Function**: The `INLINE_POLICY` heredoc, `IoTPermissions` sid at
~line 122.

1. **Split into four statements** and drop the `iot:*` wildcard:
   1. **Data-plane connect**: `iot:Connect` on
      `arn:aws:iot:*:*:client/dda-*`.
   2. **Data-plane pub/sub/receive**: `iot:Publish`, `iot:Subscribe`,
      `iot:Receive` on
      `["arn:aws:iot:*:*:topic/dda/*", "arn:aws:iot:*:*:topicfilter/dda/*"]`.
   3. **Thing shadow + describe**: `iot:GetThingShadow`,
      `iot:UpdateThingShadow`, `iot:DescribeThing` on
      `arn:aws:iot:*:*:thing/dda-*`.
   4. **Endpoint discovery (unscopable)**: `iot:DescribeEndpoint` on
      `"*"` with an adjacent shell comment recording the action is
      unscopable.
2. **Preserve the sibling sids byte-for-byte.**

#### I12 — `station_install/create-edge-device-iam-role.sh` (Req 2.12) — scope S3 to DDA prefixes

**Function**: The `INLINE_POLICY` heredoc, `S3Permissions` sid at
~line 130.

1. **Replace `"Resource": "*"`** with
   `["arn:aws:s3:::dda-component-*", "arn:aws:s3:::dda-component-*/*",
     "arn:aws:s3:::dda-inference-results-*",
     "arn:aws:s3:::dda-inference-results-*/*"]`.
2. **Preserve the `Action` list byte-for-byte** — the edge device still
   needs `s3:GetObject`, `PutObject`, `ListBucket`, `GetBucketLocation`,
   `GetBucketVersioning`, `ListBucketVersions` on its narrowed prefixes.
3. **Preserve the sibling sids byte-for-byte.**

#### I13 — `edge-cv-portal/launch-arm64-build-server.sh` (Req 2.13) — enumerate IoT for build server

**Function**: The inline `put-role-policy` block, `IoTPermissions` sid
at ~line 155.

1. **Split into three statements** and drop the `iot:*` wildcard:
   1. **Thing ops (scopable)**: `iot:DescribeThing`, `iot:CreateThing`,
      `iot:UpdateThingShadow`, `iot:AttachPolicy` on
      `arn:aws:iot:*:*:thing/dda-*`.
   2. **Job ops (scopable)**: `iot:DescribeJob` on
      `arn:aws:iot:*:*:job/*`.
   3. **Endpoint discovery (unscopable)**: `iot:DescribeEndpoint` on
      `"*"` with an adjacent shell comment recording the reason.
2. **Preserve the sibling sids byte-for-byte** — `GreengrassPermissions`
   (already-enumerated), `S3Permissions` (I14), `EC2Permissions`,
   `CloudWatchLogsPermissions`, `CloudWatchMetricsPermissions`,
   `ECRPermissions`.

#### I14 — `edge-cv-portal/launch-arm64-build-server.sh` (Req 2.14) — scope S3, isolate ListAllMyBuckets

**Function**: The inline `put-role-policy` block, `S3Permissions` sid
at ~line 161.

1. **Split into two statements**:
   1. **Scoped bucket ops**: every action currently in the sid EXCEPT
      `s3:ListAllMyBuckets` (i.e. `CreateBucket`, `GetBucketLocation`,
      `PutBucketVersioning`, `GetObject`, `PutObject`, `ListBucket`,
      `DeleteObject`, `GetBucketVersioning`, `ListBucketVersions`,
      `GetBucketPolicy`, `PutBucketPolicy`, `GetBucketAcl`,
      `PutBucketAcl`, `GetBucketTagging`, `PutBucketTagging`) on
      `["arn:aws:s3:::dda-component-*", "arn:aws:s3:::dda-component-*/*",
        "arn:aws:s3:::dda-inference-results-*",
        "arn:aws:s3:::dda-inference-results-*/*"]`.
   2. **`ListAllMyBuckets` (unscopable)**: `s3:ListAllMyBuckets` on
      `"Resource": "*"` with an adjacent shell comment recording the
      action is unscopable.
2. **Preserve the sibling sids byte-for-byte.**

#### I15 — `station_install/edge-device-iam-policy.json` (Req 2.15) — scope IoT data-plane by resource type

**Function**: The standalone JSON policy, `IoTDataPlane` sid at
~line 34.

1. **Replace the single sid with four statements** (or one statement
   with four resources correlated to actions by data type — the concrete
   choice below is the four-statement shape for readability):
   1. `iot:Connect` on `arn:aws:iot:*:*:client/dda-*`.
   2. `iot:Publish` on `arn:aws:iot:*:*:topic/dda/*`.
   3. `iot:Subscribe` on `arn:aws:iot:*:*:topicfilter/dda/*`.
   4. `iot:Receive` on `arn:aws:iot:*:*:topic/dda/*`.
2. **Preserve `GreengrassComponentDownload`, `CloudWatchLogsUpload`,
   `AssumeDataAccountRole`, `GreengrassConnectivity` byte-for-byte.**

#### I16 — `README_main.md` (Req 2.16) — narrowed `dda-build-policy` example

**Function**: The `dda-build-policy` example JSON code fence at ~line 232.

1. **Replace the JSON body** with the same narrowed shape used by the
   `launch-arm64-build-server.sh` inline policy after I13 / I14: specific
   IoT actions (thing / job / endpoint split), specific Greengrass v2
   actions, S3 scoped to `dda-component-*` and `dda-inference-results-*`
   with `s3:ListAllMyBuckets` in its own statement, and the same
   `EC2Permissions` / `CloudWatchLogsPermissions` /
   `CloudWatchMetricsPermissions` / `ECRPermissions` sids the shell
   installer emits.
2. **Preserve the surrounding prose byte-for-byte** — the paragraph
   describing the policy purpose, the "replace `[AWS account id]` with
   your account ID" instruction, and the numbered step context (`1.`
   heading, follow-up `2.` and `3.` steps) are unchanged.

#### I17 — `README_main.md` (Req 2.17) — narrowed `dda-greengrass-policy` example

**Function**: The `dda-greengrass-policy` example JSON code fence at
~line 256.

1. **Replace the JSON body** with the same narrowed shape used by
   `create-edge-device-iam-role.sh` after I10 / I11 / I12 and by
   `edge-device-iam-policy.json` after I15: specific Greengrass v2
   edge-device actions; IoT actions scoped by resource type against
   `dda-*` clients / things and `dda/*` topics; S3 scoped to
   `dda-component-*` and `dda-inference-results-*`. Keep the CloudWatch
   Logs statement (`arn:aws:logs:*:*:*`) as-is.
2. **Preserve the surrounding prose byte-for-byte** — the paragraph
   describing when to attach the policy and the "Attach S3 permissions
   for component downloads" note are unchanged.

#### I18 — Repo audit gate (Req 2.18) — `test/backend-test/security/iam_audit.py`

Add a companion audit module at
`test/backend-test/security/iam_audit.py` and wire it into the same CI
gate the sibling specs land in `build-custom.sh`. It greps the twelve
in-scope files for the four bug-condition patterns and asserts zero
disallowed hits after the fix (minus documented exceptions). Details in
the Testing Strategy → Repo-audit design section below.

### Ordering and risk (five waves)

**Wave 1 — README examples (I16, I17)** — documentation-only, zero
runtime blast radius on live infrastructure; customers who copy the
policy today see a change on their next install. Preservation is
"README prose byte-for-byte identical apart from the JSON code fences".
Verified by `jq` diff of the extracted JSON code fences against the
narrowed baseline.

**Wave 2 — Standalone JSON policy template (I15)** — the edge-device
policy JSON is applied at device provisioning time. Changing the file
changes what future devices get; existing devices are unaffected until
they re-run `attach-role-policy`. Preservation is "the four preserved
sids byte-for-byte identical; `IoTDataPlane` split by resource type".
Verified by `jq` comparison.

**Wave 3 — Shell installers (I7, I8, I9, I10, I11, I12, I13, I14)** —
inline JSON heredocs in customer-run scripts. Re-running the script
updates the role policy; the impact radius is one AWS account per
script run. Preservation is "every non-modified heredoc byte-for-byte
identical; modified heredocs' non-modified sids byte-for-byte
identical". Verified by `jq` comparison of the heredoc-extracted JSON.
Group them together because they share the same assertion shape and
have no coupling to the CDK stack risk.

**Wave 4 — CDK non-`sts:AssumeRole` fixes (I1, I2, I3, I4)** — synth-diff-
verifiable `PolicyStatement` changes on `compute-stack.ts` (I1, I2) and
`usecase-account-stack.ts` (I3, I4). Land these after the installer wave
so the tag-conditioned statements (I3, I4) can be validated against the
same tag/naming-convention baseline the installers established.
Preservation is "the emitted CloudFormation template equals the pre-fix
baseline except in the enumerated statements". Verified by capturing
`cdk.out/*.template.json` before the fix, then diffing after.

**Wave 5 — CDK `sts:AssumeRole` fixes (I5, I6) + audit gate (I18)** —
the highest-blast-radius wave. They change what accounts the portal
Lambdas will assume roles into: mis-configuring
`trustedUseCaseAccountIds` breaks every legitimate cross-account portal
flow (Ground Truth labeling job monitoring, cross-account training).
Land last with the audit gate green so a regression to a wildcard
account cannot silently sneak back in. Deploy-time verification
requires re-running `cdk deploy` on the affected stacks and executing
an end-to-end portal smoke test (create a labeling job, run a training
job cross-account, edge-device component pull, inference-results
upload). The CI-runnable audit (I18) is a necessary but not sufficient
check; a green audit does not replace a portal-flow smoke test.

**Highest-risk areas to watch:**
- The I5 / I6 `trustedUseCaseAccountIds` sourcing must be correct at
  synth time. Empty list is a synth-time error (design assumption); the
  fix DOES NOT fall back to a wildcard account under any circumstance.
- The I4 tag `Condition` must be enforced by AWS at bucket-tag
  evaluation time; every currently-working legitimate customer bucket
  MUST carry the `dda-portal:managed=true` tag before the fix lands, or
  cross-account list/read/write against it will start denying. The
  deployment runbook must include a "tag your buckets first" step.
- The I1 split into per-service statements risks dropping an action a
  portal Lambda actually uses. The full action list must be preserved
  across the split (verified by summing actions across the eight
  post-split statements and comparing to the pre-fix combined
  statement).
- The I2 `s3:ListAllMyBuckets` isolation must land in its own statement;
  bundling it back with the tagged-bucket wildcard would either widen
  the wildcard back or drop `ListAllMyBuckets`.

## Testing Strategy

### Validation Approach

Two phases: first surface counterexamples that demonstrate the wildcard /
service-wildcard / wildcard-account / unenforced-tag patterns on the
**unfixed** tree (repo audit + `cdk synth` baseline + JSON-AST inspection),
then verify the fix **scopes / enumerates / bounds / enforces / documents**
every buggy statement (Fix Checking) and **preserves** behavior for every
legitimate input (Preservation Checking, `F(X) = F'(X)`). Property-based
testing (Hypothesis — the repo already vendors `.hypothesis/`) is emphasized
where the input domain is generatable: DDA-managed vs non-DDA resource ARNs
(should be allowed vs denied by the fixed policies), and trusted vs
untrusted account IDs (should succeed vs fail on `sts:AssumeRole`).

### Repo-audit design (Req 2.18)

**Decision: add a companion `test/backend-test/security/iam_audit.py`**
rather than extend the sibling `repo_audit.py` or `secrets_audit.py`.
Rationale (least-duplicative option):

- The three gates own different patterns (this one: `resources: ['*']`
  on scopable CDK actions, service-wildcard actions, wildcard-account
  `sts:AssumeRole`, unenforced tag conditions; the sibling
  `repo_audit.py`: subprocess interpolation, SSM f-strings, unsafe
  deserializers; the sibling `secrets_audit.py`: full-event log,
  credential interpolation, un-annotated `verify_signature=False`) and
  different in-scope file sets. Editing an existing gate would entangle
  three specs' assertions and risk regressing two already-green gates.
- To avoid duplication, `iam_audit.py` **imports the proven low-level
  helpers** from the sibling modules — `REPO_ROOT`, `EXCLUDE_DIRS`,
  `EXCLUDED_PATH_SUBSTRING`, `Hit`, `_grep`, `_parse_line`,
  `_is_comment_line`, `_has_nosem` — and defines only its own
  `AUDIT_PATTERNS`, `IN_SCOPE_FILES`, and precise `_is_disallowed`.
  This mirrors the sibling modules' two-layer shape: a raw
  `run_audit()` broad enumeration (non-empty on the unfixed tree, used
  by the exploration test) and a precise `disallowed_hits()` gate
  (zero after fix, minus documented exceptions).

**In-scope files** (`IN_SCOPE_FILES`, relative to `REPO_ROOT`) — the twelve
real source paths this spec owns, excluding vendored/generated copies
(`cdk.out/asset.*`, any duplicate under a build tree):
- `edge-cv-portal/infrastructure/lib/compute-stack.ts` (I1, I2)
- `edge-cv-portal/infrastructure/lib/usecase-account-stack.ts` (I3, I4)
- `edge-cv-portal/infrastructure/lib/labeling-workflow-stack.ts` (I5)
- `edge-cv-portal/infrastructure/lib/training-workflow-stack.ts` (I6)
- `edge-cv-portal/deploy-account-role.sh` (I7, I8, I9)
- `station_install/create-edge-device-iam-role.sh` (I10, I11, I12)
- `edge-cv-portal/launch-arm64-build-server.sh` (I13, I14)
- `station_install/edge-device-iam-policy.json` (I15)
- `README_main.md` (I16, I17)

**Precise gate semantics** (per category; a hit is *disallowed* only when
it is in `IN_SCOPE_FILES`, is not a comment line, carries no
`# nosec` / adjacent-comment-marker, and matches the rule):

- **`cdk_wildcard_resource`** — a CDK `PolicyStatement` whose
  `resources: ['*']` (or the string literal on its own line) sits within
  a `new iam.PolicyStatement({...})` block whose `actions` list includes
  a scopable action. The gate uses two regexes correlated by
  file+block: `new iam.PolicyStatement\(` opens a block; the block ends
  at the matching `}));`; within the block, `resources:\s*\[\s*['\"]\*['\"]`
  is disallowed unless the block's `actions` list is exclusively in the
  documented unscopable set (`sagemaker:ListWorkteams`,
  `sts:GetCallerIdentity`, `cloudwatch:PutMetricData`,
  `logs:DescribeLogGroups`, `ecr:GetAuthorizationToken`,
  `tag:GetResources`, `resourcegroupstaggingapi:GetResources`,
  `iot:DescribeEndpoint`, `s3:ListAllMyBuckets`) OR a
  `// nosec: iam-resource-wildcard — reason` marker sits immediately
  above the `resources:` line. After I1, I2 the disallowed hits in
  `compute-stack.ts` are gone.
- **`json_resource_wildcard`** — in the in-scope JSON code / heredocs
  (`edge-cv-portal/deploy-account-role.sh`,
  `station_install/create-edge-device-iam-role.sh`,
  `edge-cv-portal/launch-arm64-build-server.sh`,
  `station_install/edge-device-iam-policy.json`, and the extracted
  README JSON code fences), a statement whose `"Resource"` value is
  `"*"` or `["arn:aws:...*"]` where the action list is scopable.
  Detected via JSON-AST walk with `jq`: for each `Statement[]` element,
  if `.Resource == "*"` or `.Resource | contains(["arn:aws:s3:::*"])`
  or similar, AND `.Action` overlaps a scopable action per an embedded
  map, then hit. Documented exceptions bypass via a `# nosec` shell
  comment on the enclosing heredoc line OR a JSON-adjacent `"//":
  "unscopable — reason"` sibling key. After I7–I15, I16, I17 the
  disallowed hits are gone.
- **`service_wildcard_action`** — `greengrass:*`, `greengrassv2:*`,
  `iot:*`, `s3:*` in an `Action` list (JSON) or an `actions:` list
  (CDK). Regex: `['\"](greengrass|greengrassv2|iot|s3):\*['\"]`.
  Disallowed unconditionally in in-scope files. After I10, I11, I13,
  I16, I17 the disallowed hits are gone.
- **`assume_role_wildcard_account`** — an `sts:AssumeRole` statement
  whose `resources` include `arn:aws:iam::\*:role/...` or `*`. Detected
  via correlated regex: statement contains `sts:AssumeRole` (in
  `actions`/`Action`) AND `resources` / `Resource` matches
  `arn:aws:iam::\\*:role/` or is `"*"` / `['*']`. Disallowed unless a
  `// nosec: assume-role-account-wildcard — reason` marker is present
  on the resource line. After I5, I6 the disallowed hits are gone.

**Scoping precision** (mirroring the sibling gates): the gate is asserted
only over `IN_SCOPE_FILES`, so it does NOT match:
- Vendored/generated copies under `cdk.out/asset.*` (excluded via
  `EXCLUDED_PATH_SUBSTRING`).
- Files owned by other specs (the sibling security batches, unrelated
  CDK stacks, backend Lambda code).
- Non-authoritative documentation outside `README_main.md`.
- Test fixtures that intentionally embed a bug-condition string as a
  test input (none exist yet for this spec; if added later, a
  `# nosec: iam-audit-test-fixture — reason` marker would allow them).

Precision + this scoping (not a hard-coded line list) is what lets the
gate still FAIL if a wildcard resource, a service-wildcard action, a
wildcard-account `sts:AssumeRole`, or an unenforced tag condition is
reintroduced into any real fixed source file.

**Two-layer API** (matching sibling gates):
- `run_audit()` — raw broad enumeration; no scoping or exception
  handling; returns every matched hit. Non-empty on the unfixed tree
  (used by the exploration test to demonstrate counterexamples).
- `disallowed_hits()` — precise post-fix gate; applies `IN_SCOPE_FILES`
  scoping, `_has_nosem` / adjacent-comment exception handling, and the
  documented-unscopable-action allowlist. Returns `[]` after the fix.
  Non-zero exit if any element remains.

**CI wiring**: add the new gate next to the existing sibling gates in
`build-custom.sh` (the "Security … audit gate" block, ~lines 236–241),
under `set -e`, so a non-zero exit fails the build:
```sh
echo "Running security IAM/authorization audit gate..."
python${PYTHON_VERSION} test/backend-test/security/iam_audit.py
python${PYTHON_VERSION} -m pytest \
  test/backend-test/security/test_iam_bug_condition_exploration.py -v
python${PYTHON_VERSION} -m pytest \
  test/backend-test/security/preservation -p no:cacheprovider --noconftest -v
echo "Security IAM/authorization audit gate passed."
```

### `cdk synth` baseline for I1–I6

The CDK stacks are verifiable via `cdk synth` output diff. The fix MUST
NOT change any statement in the emitted CloudFormation
(`edge-cv-portal/infrastructure/cdk.out/*.template.json`) other than the
intended ones. The design captures a pre-fix baseline and diffs against
it after each wave.

**Baseline capture** (before Wave 4 lands):
1. Check out the pre-fix commit; run
   `cd edge-cv-portal/infrastructure && npm ci && npx cdk synth
   --all --context ...` with a canonical fixture context
   (`portalAccountId=111111111111`, `externalId=fixture-eid`,
   `trustedUseCaseAccountIds=222222222222,333333333333`, region
   `us-east-1`).
2. Copy `cdk.out/EdgeCVPortalComputeStack.template.json`,
   `cdk.out/DDAPortalUseCaseAccountStack.template.json`,
   `cdk.out/LabelingWorkflowStack.template.json` (or the equivalent
   nested-stack file names), and `cdk.out/TrainingWorkflowStack.template.json`
   to `test/backend-test/security/baselines/iam_baseline_<stack>.json`.
3. Commit the baselines alongside the test that consumes them.

**Baseline consumer** (`test/backend-test/security/test_iam_cdk_synth_preservation.py`):
1. Runs `cdk synth` in a subprocess against the same fixture context.
2. Reads the emitted template JSON.
3. For each of the four stacks, loads the paired baseline and compares:
   - **I1 preserved statements**: every `PolicyStatement` in
     `compute-stack.ts` that is NOT the combined I1 statement or the
     I2 S3 statement — the `useCasesHandler` bucket-scoped grants, the
     `devicesHandler` Secure Tunneling grant, the Cognito auth grant,
     `sharedComponentsHandler` grants, `packagingHandler` grants, and
     every other `addToRolePolicy` call — MUST be byte-for-byte
     identical in the emitted template.
   - **I3 / I4 preserved sids**: in
     `DDAPortalUseCaseAccountStack.template.json`, every sid that is
     NOT the Ground Truth S3 statement (I3) or the `S3BucketAccess` /
     `S3ObjectAccess` sids (I4) — including `SageMakerTraining`,
     `SageMakerCompilation`, `SageMakerAlgorithm`,
     `GroundTruthLabelingV2`, `GroundTruthWorkteams`,
     `ResourceTaggingAccess`, `S3SageMakerAccess`, `CloudWatchLogs`,
     `CrossAccountDataBucketRead`, `CrossAccountDataBucketWrite` — MUST
     be byte-for-byte identical.
   - **I5 / I6 preserved rest**: in `LabelingWorkflowStack.template.json`
     and `TrainingWorkflowStack.template.json`, every EventBridge rule,
     Lambda function, and CfnOutput MUST be byte-for-byte identical
     against the baseline; ONLY the `sts:AssumeRole`
     `PolicyStatement`'s `Resource` array changes (from a wildcard to
     the mapped `trustedUseCaseAccountIds` list).
4. Any drift outside the enumerated I1–I6 statements fails the test.

The baseline JSON files are the ground truth; the fixture context makes
the diff deterministic (no timestamps, no account-id substitutions). If
CDK's synth changes the emitted asset hashes for unrelated reasons (a
Lambda code bump, a library upgrade), the baseline is regenerated in a
separate, reviewed commit — this test is the discipline that keeps
"unrelated bump" honest.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate each wildcard /
service-wildcard / wildcard-account / unenforced-tag pattern BEFORE the
fix and confirm/refute the root-cause analysis. If refuted,
re-hypothesize.

**Test Plan**: Run `iam_audit.run_audit()` to enumerate every hit across
the in-scope files, and add targeted tests that observe the
over-permission on unfixed code.

**Test Cases** (`test/backend-test/security/test_iam_bug_condition_exploration.py`):
1. **CDK wildcard resource (I1, I2)**: `iam_audit.run_audit()` returns
   hits on `compute-stack.ts` for the combined statement and the
   portal S3 statement — the raw enumeration observes
   `resources: ['*']` inside a `PolicyStatement({...})` block whose
   `actions:` list contains scopable actions (counterexample). After
   the fix these hits are gone (except the isolated unscopable-action
   statements, which carry documented markers).
2. **CDK wildcard resource — Ground Truth (I3)**: hit on
   `usecase-account-stack.ts` for the `resources: ['arn:aws:s3:::*',
   'arn:aws:s3:::*/*']` block on the Ground Truth role — after fix,
   the block carries the tag `Condition` and the sibling `sagemaker-*`
   allowlist.
3. **Unenforced tag (I4)**: parse the `S3BucketAccess` and
   `S3ObjectAccess` sids from `usecase-account-stack.ts` and assert on
   unfixed code that neither has a `conditions:` block, despite the
   two lines above declaring "Buckets must be tagged with
   `'dda-portal:managed' = 'true'`". After the fix both have the
   `Condition`.
4. **Wildcard-account `sts:AssumeRole` (I5, I6)**: hits for
   `labeling-workflow-stack.ts` (`arn:aws:iam::*:role/...`) and
   `training-workflow-stack.ts` (`resources: ['*']`) on
   `sts:AssumeRole` statements — after fix, both reference the
   mapped `trustedUseCaseAccountIds` list.
5. **Service-wildcard actions (I10, I11, I13, I16, I17)**: hits for
   `greengrass:*`, `greengrassv2:*`, `iot:*` in `create-edge-device-iam-role.sh`,
   `launch-arm64-build-server.sh`, and `README_main.md` — after fix,
   replaced by the enumerable subset.
6. **Substring bucket match (I9)**: hit for `arn:aws:s3:::*-dda-*` in
   the `deploy-account-role.sh` `AllowDDABucketPatternAccess` sid — after
   fix, only `dda-*` remains.
7. **JSON `"Resource": "*"` on scopable action (I15)**: hit for the
   `IoTDataPlane` sid in `edge-device-iam-policy.json` — after fix,
   scoped by IoT resource type.

**Repo-audit grep patterns** (must be non-empty on unfixed, zero
disallowed hits after fix — minus documented exceptions), scoped to
`IN_SCOPE_FILES`:
- CDK wildcard-resource with scopable action:
  `new iam\.PolicyStatement\(` opening a block that contains
  `resources:\s*\[\s*['\"]\*['\"]` AND whose `actions:` list
  intersects the scopable set.
- JSON wildcard-resource with scopable action: JSON-AST walk over
  extracted `Statement[]` where `.Resource == "*"` or
  `.Resource | any(contains("arn:aws:...:*"))` AND `.Action` is
  scopable.
- Service-wildcard action:
  `['\"](greengrass|greengrassv2|iot|s3):\*['\"]` in in-scope files.
- Wildcard-account `sts:AssumeRole`: correlated regex —
  `sts:AssumeRole` in `actions` AND
  `arn:aws:iam::\*:role/` in `resources`, OR `resources:\s*\[\s*['\"]\*['\"]`
  alongside `sts:AssumeRole` in the same block.
- Substring bucket pattern: `arn:aws:s3:::\*-dda-\*` in in-scope files.
- Unenforced tag condition: heuristic — a `PolicyStatement` block whose
  comment (2 lines up) contains the words `tag` and `dda-portal:managed`
  but whose block does not contain `conditions:`. This is enumeration-
  only (fires the exploration report, not the precise gate).

**Expected Counterexamples**:
- Non-empty `iam_audit.run_audit()` hits across every category (I1–I17).
- `cdk synth` output for the pre-fix stacks contains `PolicyStatement`s
  with `Resource: '*'` and `arn:aws:iam::*:role/...` — captured as the
  baseline against which the fixed stacks are diffed.
- `jq` extraction of the shell installers' inline JSON heredocs and the
  README code fences yields `Statement[]` elements with `"Resource":
  "*"` and `"Action": ["greengrass:*", "iot:*"]`.

### Fix Checking

**Goal**: For all inputs where the bug condition holds, the fixed code
scopes / enumerates / bounds / enforces / documents.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := fixedFunction(input)
  ASSERT scoped(result) AND enumeratedActions(result)
     AND boundedAccounts(result) AND documentedException(result) WHEN unscopable
END FOR
```

Concretely:
- I1: post-fix `compute-stack.ts` emits eight per-service statements
  each with resource ARNs matching the naming-convention patterns;
  `ListWorkteams`, `DescribeEndpoint`, and `DescribeLogGroups`-class
  are isolated in their own statements with code comments; the
  `iam:PassRole` resource is `arn:aws:iam::*:role/DDA*Role` with the
  `iam:PassedToService=sagemaker.amazonaws.com` condition preserved
  (Req 2.1).
- I2: post-fix `compute-stack.ts` portal S3 grant is scoped to
  `props.portalArtifactsBucket.bucketArn` (+ object children), the
  tagged-bucket wildcard carries the `dda-portal:managed=true`
  Condition, and `s3:ListAllMyBuckets` is isolated with a code comment
  (Req 2.2).
- I3: post-fix `usecase-account-stack.ts` Ground Truth S3 grant is
  split into a tag-conditioned statement (on
  `dda-portal:managed=true`) and an unconditional `sagemaker-*`
  allowlist (Req 2.3).
- I4: post-fix `S3BucketAccess` and `S3ObjectAccess` sids both carry
  the `Condition` block (Req 2.4).
- I5, I6: post-fix `labeling-workflow-stack.ts` and
  `training-workflow-stack.ts` reference `props.trustedUseCaseAccountIds`
  mapped to `arn:aws:iam::${id}:role/DDAPortalAccessRole`; empty list
  is a synth-time error (Req 2.5, 2.6).
- I7–I9: post-fix `deploy-account-role.sh` inline JSON has narrowed
  `Resource` lists (`dda-*`, `sagemaker-*`, `dda-inference-results-*`
  as appropriate); the `*-dda-*` substring entries are removed (Req
  2.7, 2.8, 2.9).
- I10–I12: post-fix `create-edge-device-iam-role.sh` `Greengrass`
  sids enumerate specific v2 actions; `IoT` sids split by resource
  type (`client/dda-*`, `topic/dda/*`, `topicfilter/dda/*`, `thing/dda-*`,
  `DescribeEndpoint` isolated); `S3` sid narrowed to
  `dda-component-*` and `dda-inference-results-*` (Req 2.10, 2.11,
  2.12).
- I13, I14: post-fix `launch-arm64-build-server.sh` `IoT` sids split by
  resource type; `S3` sid scoped to `dda-component-*` and
  `dda-inference-results-*` with `ListAllMyBuckets` isolated (Req
  2.13, 2.14).
- I15: post-fix `edge-device-iam-policy.json` `IoTDataPlane` sid split
  by resource type (Req 2.15).
- I16, I17: post-fix `README_main.md` example JSONs match the narrowed
  shape from the shell installers (Req 2.16, 2.17).
- I18: post-fix `iam_audit.disallowed_hits()` returns `[]` (Req 2.18).

### Preservation Checking

**Goal**: For all inputs where the bug condition does NOT hold, the
fixed code produces the same result as the original — `F(X) = F'(X)`.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT originalFunction(input) = fixedFunction(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended where the
domain is generatable (resource ARNs matching / not matching the naming
conventions, tagged vs untagged buckets, trusted vs untrusted account
IDs); capture baseline behavior on the **unfixed** code first, then
assert the fixed code matches.

**Property-based test plans:**
- **PBT 1 — DDA-managed vs non-DDA resource ARNs**: generate
  `(action, resource_arn)` pairs where `action` is drawn from the
  action set of a given fixed policy and `resource_arn` is either (a)
  a `dda-*`/`dda/*`/`sagemaker-*`/`dda-component-*`/
  `dda-inference-results-*` ARN, or (b) a random non-DDA ARN.
  Invariants: for every (a) pair, the fixed policy allows; for every
  (b) pair, the fixed policy denies. Compare against the unfixed
  policy's allow set (which allows both) — the DIFFERENCE is exactly
  the non-DDA resource ARNs, and every DDA-ARN pair is preserved.
- **PBT 2 — Tagged vs untagged buckets (I3, I4)**: generate bucket
  states of the form `{ arn, tags: {"dda-portal:managed": <maybe>}
  }`. Invariants: fixed I3 / I4 policies allow the intended actions
  iff `tags["dda-portal:managed"] == "true"` OR the bucket is
  `sagemaker-*`; unfixed policy allows regardless of tag. The
  DIFFERENCE is exactly untagged non-`sagemaker-*` buckets.
- **PBT 3 — Trusted vs untrusted account IDs (I5, I6)**: generate a
  set `trusted = {A1, A2, ...}` and a probe account `probe` drawn
  either from `trusted` or from its complement. Invariants: the
  fixed I5 / I6 policies allow `sts:AssumeRole` on
  `arn:aws:iam::${probe}:role/DDAPortalAccessRole` iff `probe in
  trusted`; the unfixed policy allows for any `probe`. The DIFFERENCE
  is exactly `probe not in trusted`. This PBT also asserts empty
  `trusted` is a synth-time error.
- **PBT 4 — Enumerable IoT action subset (I11, I13, I15)**: generate
  IoT actions from the full IoT API set and split by whether the
  action is in the edge-device / build-server / data-plane exercised
  subset. Invariants: fixed policies allow iff the action is in the
  subset AND the resource matches the appropriate prefix; unfixed
  `iot:*` policies allow regardless. The DIFFERENCE is exactly the
  non-exercised actions.

**Example-based preservation cases:**
1. **I1 legitimate flows**: `useCasesHandler` calls `sagemaker:
   CreateTrainingJob` on `arn:aws:sagemaker:us-east-1:111:training-job/
   dda-usecase-001` (allowed by fixed I1); calls `iot:UpdateThingShadow`
   on `arn:aws:iot:us-east-1:111:thing/dda-thing-002` (allowed);
   calls `execute-api:Invoke` on the portal's own API Gateway ARN
   (allowed).
2. **I2 legitimate flows**: `useCasesHandler` reads / writes
   `props.portalArtifactsBucket` (allowed); lists all buckets
   (allowed via isolated `ListAllMyBuckets` statement); accesses a
   customer bucket tagged `dda-portal:managed=true` (allowed via
   tag-conditioned statement); accesses an untagged non-portal
   bucket (DENIED — this is the DIFFERENCE and is intended).
3. **I3 Ground Truth**: input+output buckets tagged
   `dda-portal:managed=true` (allowed); `sagemaker-*` buckets
   (allowed); non-tagged customer buckets (DENIED — this is the
   DIFFERENCE and is called out in the deployment runbook).
4. **I4 cross-account**: the portal Lambda assumes
   `DDAPortalAccessRole` in a trusted UseCase account and accesses a
   tagged bucket (allowed); untagged bucket (DENIED — DIFFERENCE);
   the `S3SageMakerAccess`, `ResourceTaggingAccess`, and every other
   sid in the file behave byte-for-byte identically (verified by
   `cdk synth` template diff).
5. **I5, I6 cross-account assume-role**: `LabelingMonitorFunction` /
   `assumeRoleFunction` succeed against accounts in
   `trustedUseCaseAccountIds` (allowed); attempts against
   non-trusted accounts DENIED (DIFFERENCE); the runtime `ExternalId`
   check inside the training assume-role Lambda handler is
   preserved.
6. **I7–I14 installer flows**: re-running each installer script
   emits inline JSON policies that grant the same
   `(Action, Resource)` pairs against `dda-*` / `sagemaker-*` /
   `dda-component-*` / `dda-inference-results-*` resources; access to
   non-DDA-prefixed resources DENIED (DIFFERENCE).
7. **I15 edge-device JSON**: the four preserved sids emit
   byte-for-byte identical JSON output; `IoTDataPlane` split allows
   `dda-*` clients / `dda/*` topics only (DIFFERENCE for non-DDA
   topics).
8. **I16, I17 README**: `README_main.md` diff shows only the two JSON
   code fences changed; surrounding prose byte-for-byte identical.
9. **Out-of-scope untouched (Req 3.18)**: `cdk.out/asset.*`,
   `security-injection-deserialization-fixes` files (findings #1–#8),
   `security-secrets-credentials-jwt-fixes` files (S1–S9), and every
   other spec's files are unchanged.

### Unit Tests

- I1: `cdk synth` output for `EdgeCVPortalComputeStack` contains eight
  post-split statements with the expected `Action` / `Resource`
  shapes; the pre-fix combined statement no longer appears; the
  `iam:PassRole` statement's `Resource` is `arn:aws:iam::*:role/DDA*Role`
  and its condition is preserved.
- I2: `cdk synth` output contains a `props.portalArtifactsBucket`-scoped
  S3 statement, a tag-conditioned wildcard statement, and an isolated
  `ListAllMyBuckets` statement.
- I3: `DDAPortalUseCaseAccountStack` synth contains the Ground Truth
  tag-conditioned statement AND the `sagemaker-*` allowlist; the
  original combined `arn:aws:s3:::*` statement is gone.
- I4: `S3BucketAccess` and `S3ObjectAccess` sids each have a
  `Condition: StringEquals { aws:ResourceTag/dda-portal:managed: 'true' }`
  block; all other sids byte-for-byte identical vs baseline.
- I5, I6: with `trustedUseCaseAccountIds = ['111', '222']`, the
  `sts:AssumeRole` resource array is
  `['arn:aws:iam::111:role/DDAPortalAccessRole', 'arn:aws:iam::222:role/DDAPortalAccessRole']`;
  with `trustedUseCaseAccountIds = []`, `cdk synth` throws a
  synth-time error.
- I7–I14: `jq` extraction of each shell installer's inline JSON
  heredoc yields the expected narrowed policy; sibling sids
  byte-for-byte identical to a pre-fix golden.
- I15: `jq '.Statement[] | select(.Sid == "IoTDataPlane")'` yields
  four resource-type-split statements (or one statement with four
  resources per action); the four preserved sids byte-for-byte
  identical.
- I16, I17: extracted JSON code fences match the narrowed baseline;
  the surrounding markdown is unchanged (diff-verified).
- I18: `iam_audit.disallowed_hits() == []`; `run_audit()` excludes
  `cdk.out/asset.*` and any vendored duplicates.

### Property-Based Tests

- PBT 1 (DDA vs non-DDA ARNs) — generated ARNs and actions across all
  in-scope services; invariant: allow iff DDA-prefixed AND action in
  exercised set.
- PBT 2 (tagged vs untagged buckets) — generated `(bucket_arn, tags)`
  pairs; invariant: fixed I3/I4 allow iff
  `tags["dda-portal:managed"] == "true"` OR bucket is `sagemaker-*`.
- PBT 3 (trusted vs untrusted accounts) — generated
  `(trusted_set, probe_account)` pairs; invariant: fixed I5/I6 allow
  `sts:AssumeRole` iff `probe_account in trusted_set`; empty trusted
  set is a synth-time error.
- PBT 4 (IoT enumerable subset) — generated IoT actions; invariant:
  fixed I11/I13/I15 allow iff action is in the edge-device /
  build-server / data-plane exercised subset AND resource matches.

### Integration Tests

- **`cdk deploy` end-to-end smoke test (I1–I6, gated)**: deploy the
  four stacks with fixture context to a staging AWS account; run a
  scripted portal workflow that (a) creates a training job with a
  `dda-*` name, (b) triggers a Ground Truth labeling job with a
  `dda-portal:managed=true`-tagged input bucket, (c) triggers the
  `LabelingMonitorFunction` cross-account against a trusted UseCase
  account, (d) triggers the `assumeRoleFunction` with a valid
  `ExternalId` against a trusted account. Every step must succeed
  identically to the pre-fix baseline. Attempts (e) with a non-DDA
  training job name and (f) `assumeRoleFunction` against a non-trusted
  account must FAIL — these are the intended DIFFERENCEs. The smoke
  test is a **deployment-time gate** — the CI-runnable audit (I18) is
  a necessary but not sufficient check.
- **Installer replay (I7–I14)**: for each shell installer, re-run
  against a staging AWS account, then use `aws iam get-role-policy`
  to fetch the applied policy and `jq`-compare to the expected
  narrowed shape.
- **Edge-device provisioning (I15)**: apply the narrowed
  `edge-device-iam-policy.json` to a provisioned edge device and
  assert IoT connect / publish / subscribe / receive succeed on
  `dda-*` clients / `dda/*` topics; assert connect on a non-`dda-*`
  client fails.
- **README example follow-through (I16, I17)**: manually follow the
  updated README instructions in a staging account and assert the
  resulting `dda-build-policy` / `dda-greengrass-policy` role
  policies match the narrowed shape.
- **Audit gate in CI (I18)**: run `iam_audit.py` in `build-custom.sh`
  — it fails if any disallowed wildcard-resource, service-wildcard,
  wildcard-account, or unenforced-tag pattern reappears in in-scope
  source.

**Rollback plan** (per the bugfix.md commitments): each of I1–I17 is a
separate commit / task in the task breakdown. If a portal flow breaks
after deploy, the specific IAM statement that caused the regression can
be reverted independently — isolated to a single file's diff — without
touching the other 16 fixes or the sibling remediation branches
(`security-injection-deserialization-fixes`,
`security-secrets-credentials-jwt-fixes`).
