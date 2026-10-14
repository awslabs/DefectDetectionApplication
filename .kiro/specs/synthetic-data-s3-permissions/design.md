# Synthetic Data S3 Permissions Bugfix Design

## Overview

Synthetic data generation fails at the generate step in single-account portal deployments with `AccessDenied` on `s3:GetObject` (verified live on account 164152369890, us-east-1). The `SyntheticDataHandlerRole` created by `edge-cv-portal/infrastructure/lib/synthetic-data-stack.ts` carries DynamoDB, Bedrock, STS, Lambda self-invoke, and SageMaker grants but no S3 statements at all. In single-account setups, `shared_utils.assume_usecase_role` falls back to the Lambda execution role's credentials, so the generation worker's S3 data access is denied on its first read. Cross-account setups are unaffected because the assumed `DDAPortalAccessRole` carries the S3 permissions.

The fix mirrors an already-reviewed pattern: the compute stack's `createLambdaRole` (`edge-cv-portal/infrastructure/lib/compute-stack.ts`, ~lines 557–612) grants data-plane-only S3 access scoped by the optional `dataBucketAllowlist` context, deliberately excluding all control-plane actions and the `aws:ResourceTag` condition (a documented past regression — S3 does not honor it for bucket-level actions). We add the same optional `dataBucketAllowlist?: string[]` prop to `SyntheticDataStackProps`, replicate the compute stack's allowlist normalization and the two data-plane policy statements onto `handlerRole`, and pass the already-parsed allowlist through from `bin/app.ts`. The change is additive: no existing grant on the handler role is touched, and the compute stack is not modified.

## Glossary

- **Bug_Condition (C)**: A synthetic generation task running in a single-account setup that requires S3 data-plane access to the use case data bucket — the worker's S3 calls run under the Lambda execution role, which currently has no S3 statement.
- **Property (P)**: The synthesized `SyntheticDataHandlerRole` allows the S3 data-plane actions the generation worker needs: `s3:GetObject`/`s3:PutObject` at the object level and `s3:ListBucket`/`s3:GetBucketLocation`/`s3:GetBucketTagging` at the bucket level, scoped by the allowlist when configured.
- **Preservation**: All existing grants on the handler role (DynamoDB, Bedrock, STS AssumeRole, Lambda self-invoke, SageMaker + PassRole), the absence of S3 control-plane actions, the cross-account access path, and the compute stack's role output must remain unchanged.
- **SyntheticDataHandlerRole**: The IAM role in `synthetic-data-stack.ts` assumed by the `dda-synthetic-data-handler` Lambda (API routing + async generation worker).
- **createLambdaRole**: The private method in `compute-stack.ts` that builds the shared portal Lambda roles; its data-plane grant block (~lines 557–612) is the reviewed pattern this fix replicates.
- **dataBucketAllowlist**: Optional CDK context (`-c dataBucketAllowlist=bucket-a,bucket-b`), parsed in `bin/app.ts` into a string array. Entries may be bare bucket names or `arn:aws:s3:::name` ARNs; empty/unset means all buckets (`arn:aws:s3:::*`) on the data plane.
- **Single-account setup**: A deployment where the use case data lives in the portal account itself; the cross-account role ARN resolves to the account root and `shared_utils.assume_usecase_role` falls back to the Lambda's own execution-role credentials.
- **Data-plane actions**: Object and bucket read/write operations (`s3:GetObject`, `s3:PutObject`, `s3:ListBucket`, `s3:GetBucketLocation`, `s3:GetBucketTagging`) as opposed to control-plane actions (`s3:PutBucketPolicy`, ACLs, `s3:DeleteBucket`, `s3:PutBucketTagging`, etc.), which are never granted.

## Bug Details

### Bug Condition

The bug manifests when a synthetic generation task runs in a single-account setup and the worker needs S3 data-plane access to the use case data bucket. Because `SyntheticDataHandlerRole` contains no S3 statement, every such call — reading source images (`s3:GetObject`), writing generated previews or the ETag-conditional manifest append (`s3:PutObject`), and bucket-level operations (`s3:ListBucket`, `s3:GetBucketLocation`, `s3:GetBucketTagging`) — is denied.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type SyntheticGenerationTask
  OUTPUT: boolean

  // Single-account setup: assume_usecase_role returns default-credential
  // markers, so S3 access runs under the Lambda execution role — which
  // currently has no S3 statement at all.
  RETURN X.setup = SINGLE_ACCOUNT
         AND X.requiresS3DataPlaneAccess(useCaseDataBucket)
END FUNCTION
```

### Examples

- **Verified counterexample (live)**: a single-account generate request on account 164152369890 whose worker calls `s3:GetObject` on `arn:aws:s3:::ryvan-cookies/training-images/anomaly-12.jpg` — denied: "no identity-based policy allows the s3:GetObject action" on `EdgeCVPortalSyntheticData-SyntheticDataHandlerRoleA-*`. Expected: the image is read and generation proceeds.
- **Preview write**: after Bedrock generates a variation, the worker writes the preview image to the use case data bucket — denied `s3:PutObject`. Expected: preview persists and appears in the session.
- **Manifest append at integration**: the integrate step performs an ETag-conditional `s3:PutObject` of the manifest — denied. Expected: manifest updated with the approved images.
- **Static synth-time view**: `Template.fromStack(SyntheticDataStack)` shows no IAM policy statement with any `s3:*` action attached to the handler role — the structural form of the bug that the exploration test asserts against.
- **Edge case (cross-account, not buggy)**: the same generate request in a cross-account setup succeeds because S3 access flows through the assumed `DDAPortalAccessRole` — this path must remain untouched.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- All existing grants on `SyntheticDataHandlerRole`: DynamoDB (own tables read/write; usecases/user-roles/settings read; audit write; training-jobs read/write), Bedrock `InvokeModel` + `ListFoundationModels`, `sts:AssumeRole` on `DDAPortalAccessRole` in the trusted use case accounts, `lambda:InvokeFunction` self-invoke on the fixed handler function ARN, SageMaker training-job actions, and `iam:PassRole` with the `iam:PassedToService: sagemaker.amazonaws.com` condition (Req 3.1)
- No S3 control-plane permissions anywhere on the role — no `s3:PutBucketPolicy`, ACL actions, `s3:DeleteBucket`, `s3:PutBucketTagging`, or similar (Req 3.2)
- Cross-account setups continue to access the use case data bucket via the assumed `DDAPortalAccessRole` credentials (Req 3.3)
- The compute stack's `createLambdaRole` produces byte-identical policy statements — this fix does not touch `compute-stack.ts` (Req 3.4)
- Per-task generation failures continue to create the session and record the failure on the preview (Req 3.5)

**Scope:**
All inputs that do NOT involve single-account S3 data-plane access from the synthetic data handler should be completely unaffected by this fix. This includes:
- Cross-account generation tasks (S3 via assumed role)
- All non-S3 handler operations (DynamoDB reads/writes, Bedrock calls, SageMaker retrain, self-invocation)
- Every other stack in the CDK app, in particular the compute stack's Lambda roles

**Note:** The expected correct behavior for buggy inputs is defined in the Correctness Properties section (Property 1). This section covers what must NOT change.

## Hypothesized Root Cause

The root cause is established, not merely hypothesized — it is a straightforward omission confirmed by reading the stack and by the live error:

1. **Missing S3 grants on the handler role**: `synthetic-data-stack.ts` was written as a deliberately narrow role ("only the tables and services this feature uses") and simply never received S3 statements. The feature was validated in a context where the cross-account path (assumed `DDAPortalAccessRole`) supplied S3 access, masking the gap.
   - The role carries DynamoDB, Bedrock, STS, Lambda, and SageMaker grants — grep for `s3:` in the file returns nothing.
   - The live `AccessDenied` names the handler role directly and states no identity-based policy allows `s3:GetObject`.

2. **Missing prop plumbing**: `SyntheticDataStackProps` has no `dataBucketAllowlist` field, and `bin/app.ts` does not pass the already-parsed `dataBucketAllowlist` array to the `SyntheticDataStack` instantiation (it passes it to the ComputeStack only). Even if grants existed, they could not be allowlist-scoped.

3. **Why single-account only**: `shared_utils.assume_usecase_role` detects the single-account case and falls back to default (execution-role) credentials, so the worker's boto3 S3 client runs as the handler role. Cross-account setups get credentials from the assumed `DDAPortalAccessRole`, which carries its own S3 permissions.

## Correctness Properties

Property 1: Bug Condition - Handler Role Carries S3 Data-Plane Grants

_For any_ synthesis of the `SyntheticDataStack` (with any valid `dataBucketAllowlist`, including unset/empty), the fixed stack SHALL attach to the handler role an object-level statement allowing `s3:GetObject` and `s3:PutObject` on `<bucketArn>/*` for each normalized allowlist bucket (or `arn:aws:s3:::*/*` when the allowlist is empty), and a bucket-level statement allowing `s3:ListBucket`, `s3:GetBucketLocation`, and `s3:GetBucketTagging` on the normalized bucket ARNs (or `arn:aws:s3:::*` when empty), with no `aws:ResourceTag` condition and using the same name-or-ARN normalization as the compute stack's `createLambdaRole` — so that single-account generation tasks succeed under execution-role credentials.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - Existing Grants and Non-Buggy Paths Unchanged

_For any_ input where the bug condition does NOT hold (cross-account tasks, non-S3 handler operations, and every other stack), the fixed code SHALL produce the same result as the original code: all pre-existing statements on `SyntheticDataHandlerRole` (DynamoDB, Bedrock, STS AssumeRole on `DDAPortalAccessRole`, Lambda self-invoke, SageMaker + PassRole condition) remain present and unchanged, no S3 control-plane action appears anywhere on the role, the cross-account access path still flows through the assumed `DDAPortalAccessRole`, and the compute stack's Lambda role policies are byte-identical to before the fix.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

## Fix Implementation

### Changes Required

**File**: `edge-cv-portal/infrastructure/lib/synthetic-data-stack.ts`

**Specific Changes**:

1. **Add the optional prop**: Extend `SyntheticDataStackProps` with `dataBucketAllowlist?: string[]`, documented the same way as the ComputeStack's prop (bare names or `arn:aws:s3:::name` ARNs; empty/unset ⇒ all buckets on the data plane; control plane never granted).

2. **Replicate the data-plane grant block onto `handlerRole`** (after the existing STS/Lambda/SageMaker statements), mirroring `createLambdaRole` ~lines 557–612:
   - Normalize the allowlist: filter empty entries; each entry that starts with `arn:aws:s3:::` has any trailing `/*` or `/` stripped to the canonical bucket ARN, otherwise it is prefixed to `arn:aws:s3:::${name}`; an empty allowlist yields `['arn:aws:s3:::*']`.
   - `bucketLevelResources` = the bucket ARNs; `objectLevelResources` = each ARN + `/*`.
   - Add two `iam.PolicyStatement`s: bucket-level (`s3:ListBucket`, `s3:GetBucketLocation`, `s3:GetBucketTagging` on `bucketLevelResources`) and object-level (`s3:GetObject`, `s3:PutObject` on `objectLevelResources`).
   - NO control-plane actions and NO `aws:ResourceTag` condition — include the explanatory comment mirroring the compute stack's (documented past regression: S3 does not honor `aws:ResourceTag` for bucket-level actions, so the earlier condition silently denied all data-bucket access).

3. **Duplicate rather than extract the normalization**: the compute stack's block lives inside a private method (`createLambdaRole`) of another stack, so extraction would mean refactoring `compute-stack.ts` — exactly the file Requirement 3.4 says must produce unchanged output. Duplicating ~15 lines that visibly match the reviewed pattern is the surgical choice; the tradeoff (two copies to keep in sync) is accepted for this bugfix and can be revisited if a third consumer appears.

**File**: `edge-cv-portal/infrastructure/bin/app.ts`

4. **Pass the allowlist through**: add `dataBucketAllowlist,` (the already-parsed array at ~line 112) to the `SyntheticDataStack` instantiation props. No new parsing — the same array the ComputeStack receives.

**Post-fix live verification** (orchestrator task, outside this repo's test suite): deploy the infrastructure (`EdgeCVPortalSyntheticDataStack`) and re-run a generation session on the affected single-account portal to confirm the `AccessDenied` is gone end to end.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on the unfixed stack, then verify the fix works correctly and preserves existing behavior. All static checks are CDK assertions tests (jest, `Template.fromStack`) in `edge-cv-portal/infrastructure/test/`, following the conventions of `workflow-manager-gaps-infra.test.ts`: synthesize once in `beforeAll` with a generous timeout (asset staging is expensive), locate resources via `template.findResources`, and assert on the raw CloudFormation properties. The `SyntheticDataStack` throws at synth time without a non-empty `trustedUseCaseAccountIds`, so tests must pass it (and the other required props: tables from a `StorageStack`, a Cognito user pool, rest API ids, stage name) in props.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write a CDK assertions test that synthesizes `SyntheticDataStack` and collects every IAM policy statement attached to the handler role (inline policies whose `Roles` reference the `SyntheticDataHandlerRole` logical id), then asserts the object-level (`s3:GetObject`/`s3:PutObject`) and bucket-level (`s3:ListBucket`/`s3:GetBucketLocation`/`s3:GetBucketTagging`) statements are present. Run on the UNFIXED code to observe the failure.

**Test Cases**:
1. **Object-level statement present**: the handler role's policies contain a statement allowing `s3:GetObject` and `s3:PutObject` on object-level resources (will fail on unfixed code — no S3 statement exists)
2. **Bucket-level statement present**: the handler role's policies contain a statement allowing `s3:ListBucket`, `s3:GetBucketLocation`, `s3:GetBucketTagging` on bucket-level resources (will fail on unfixed code)

**Expected Counterexamples**:
- The synthesized template contains zero statements with any `s3:*` action attached to the handler role
- Possible causes confirmed/refuted: missing grants in `synthetic-data-stack.ts` (expected), missing prop plumbing from `bin/app.ts` (expected)

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL allowlist IN {unset, [], [bare names], [ARNs], [mixed, with trailing /*]} DO
  template := Template.fromStack(SyntheticDataStack(props with allowlist))
  ASSERT handlerRole allows s3:GetObject, s3:PutObject on objectLevelResources(allowlist)
  ASSERT handlerRole allows s3:ListBucket, s3:GetBucketLocation, s3:GetBucketTagging
         on bucketLevelResources(allowlist)
  ASSERT no aws:ResourceTag condition on either statement
END FOR
```

Concretely: the exploration tests above flip to passing after the fix (default/empty allowlist ⇒ `arn:aws:s3:::*` and `arn:aws:s3:::*/*`), plus an allowlist-scoping test that synthesizes with `dataBucketAllowlist: ['bucket-a', 'arn:aws:s3:::bucket-b']` and asserts the statements' resources are exactly `arn:aws:s3:::bucket-a`, `arn:aws:s3:::bucket-b` (bucket level) and their `/*` forms (object level) — verifying the name-or-ARN normalization matches the compute stack's (Req 2.4).

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
  // Concretely: all non-S3 statements on SyntheticDataHandlerRole are
  // unchanged; no S3 control-plane actions appear anywhere on the role;
  // cross-account access still flows through DDAPortalAccessRole;
  // compute-stack roles are untouched.
END FOR
```

**Testing Approach**: The "input domain" here is the synthesized CloudFormation template, which is deterministic at synth time, so exhaustive static assertions over the template take the role property-based testing usually plays: they check the preservation invariants across ALL statements on the role (e.g., "no statement anywhere on the role contains a control-plane S3 action"), not just hand-picked examples. This gives the same all-inputs guarantee for the synth-time behavior.

**Test Plan**: Observe the UNFIXED stack's handler-role statements first (they are fully specified in `synthetic-data-stack.ts`), then write assertions capturing that inventory so the fixed stack must still produce it.

**Test Cases**:
1. **Existing statements preserved**: the handler role still carries Bedrock `InvokeModel` (foundation-model + inference-profile ARNs) and `ListFoundationModels`, `sts:AssumeRole` scoped to `arn:aws:iam::<trusted>:role/DDAPortalAccessRole`, `lambda:InvokeFunction` on the fixed `dda-synthetic-data-handler` function ARN, the SageMaker training-job actions, and `iam:PassRole` with the `sagemaker.amazonaws.com` PassedToService condition (Req 3.1)
2. **No control-plane S3 anywhere**: iterate every statement in every policy attached to the handler role and assert no action matches `s3:PutBucketPolicy`, `s3:PutBucketAcl`, `s3:PutObjectAcl`, `s3:DeleteBucket`, `s3:PutBucketTagging`, or other control-plane patterns; also assert the new S3 statements carry no `Condition` (Req 3.2, 2.5)
3. **Cross-account path untouched**: the `sts:AssumeRole` statement on `DDAPortalAccessRole` is unchanged — same actions, same resources (Req 3.3; the runtime cross-account flow itself is covered by the live verification task)
4. **Compute stack untouched**: `git diff` shows no change to `compute-stack.ts`, and (optionally) the existing compute-stack test suite still passes, confirming `createLambdaRole` output is unchanged (Req 3.4)

### Unit Tests

- Exploration/fix: handler role carries the object-level and bucket-level S3 data-plane statements (default allowlist)
- Fix: allowlist scoping — synth with a mixed name/ARN allowlist and assert exact normalized resources on both statements
- Preservation: existing statement inventory (Bedrock, STS, self-invoke, SageMaker, PassRole) still present and unchanged
- Preservation: no S3 control-plane action and no `aws:ResourceTag` condition anywhere on the role

### Property-Based Tests

- Not applicable as a randomized-input suite: the behavior under test is deterministic synth-time template generation. The preservation tests achieve the equivalent all-inputs guarantee by quantifying over ALL statements in the synthesized template (universal assertions such as "no statement on the role contains a control-plane S3 action") rather than sampling examples.

### Integration Tests

- Build gate: `npx tsc` and `npx jest` in `edge-cv-portal/infrastructure/` (the package's `test` script is plain `jest`); tests construct the stacks directly with required props (non-empty `trustedUseCaseAccountIds`) rather than relying on `cdk synth` context
- Post-fix live verification (orchestrator task): deploy the infrastructure and re-run a generation session on the affected single-account portal; confirm source-image reads, preview writes, and the integrate-step manifest append succeed with no `AccessDenied`
