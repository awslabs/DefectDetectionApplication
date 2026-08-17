# Bugfix Requirements Document

## Introduction

Synthetic data generation fails at the generate step in single-account portal deployments with an S3 `AccessDenied` error. Verified on live account 164152369890 (us-east-1):

```
An error occurred (AccessDenied) when calling the GetObject operation: User:
arn:aws:sts::164152369890:assumed-role/EdgeCVPortalSyntheticData-SyntheticDataHandlerRoleA-ObZAyr8XI3GD/dda-synthetic-data-handler
is not authorized to perform: s3:GetObject on resource:
arn:aws:s3:::ryvan-cookies/training-images/anomaly-12.jpg because no
identity-based policy allows the s3:GetObject action
```

The `SyntheticDataHandlerRole` created by `edge-cv-portal/infrastructure/lib/synthetic-data-stack.ts` is a least-privilege role carrying DynamoDB, Bedrock, STS (`sts:AssumeRole` on `DDAPortalAccessRole` in trusted use case accounts), Lambda self-invoke, and SageMaker grants — but no S3 permissions at all. In single-account setups the cross-account role ARN resolves to the account root, `shared_utils.assume_usecase_role` falls back to default (execution-role) credentials, and the generation worker's S3 access runs under the Lambda execution role, which is denied on its first `s3:GetObject`. Cross-account setups are unaffected (the assumed `DDAPortalAccessRole` carries the S3 permissions), which is why the gap was not caught earlier.

The generation worker needs data-plane S3 access to the use case data bucket: `s3:GetObject` (read source images — the failing call), `s3:PutObject` (write generated preview images and the ETag-conditional manifest append at integration), and bucket-level list/location/tagging reads. The compute stack's `createLambdaRole` in `edge-cv-portal/infrastructure/lib/compute-stack.ts` already implements the reviewed pattern for exactly this need: data-plane-only grants scoped by the optional `dataBucketAllowlist` context, deliberately excluding all control-plane actions and the `aws:ResourceTag` condition (which S3 does not honor for bucket-level actions — a documented past regression). The fix is to apply that same pattern to the synthetic data handler role, security-sensitive only in that it must mirror the existing reviewed grant shape rather than invent new policy.

## Bug Analysis

### Current Behavior (Defect)

The synthetic data handler role has no S3 grants, so any generation task that must use the Lambda's own credentials for S3 data access is denied.

1.1 WHEN a synthetic generation task runs in a single-account setup and the worker reads a source image from the use case data bucket THEN the system fails with `AccessDenied` on `s3:GetObject` because the `SyntheticDataHandlerRole` policy set contains no S3 statement

1.2 WHEN a synthetic generation task runs in a single-account setup and the worker attempts to write a generated preview image or the manifest to the use case data bucket THEN the system is denied `s3:PutObject` because the `SyntheticDataHandlerRole` policy set contains no S3 statement

1.3 WHEN a synthetic generation task runs in a single-account setup and the worker performs bucket-level operations on the use case data bucket (list, location, tagging reads) THEN the system is denied because the `SyntheticDataHandlerRole` policy set contains no S3 statement

1.4 WHEN the portal is deployed with the `dataBucketAllowlist` context THEN the system does not apply the allowlist to the synthetic data handler role because `SyntheticDataStackProps` does not receive `dataBucketAllowlist` from `bin/app.ts`

### Expected Behavior (Correct)

The handler role carries the same data-plane-only, allowlist-scopable S3 grant pattern as the compute stack's shared Lambda roles.

2.1 WHEN a synthetic generation task runs in a single-account setup and the worker reads a source image from the use case data bucket THEN the system SHALL allow `s3:GetObject` on the object-level resources (`<bucketArn>/*` for each allowlist bucket, or `arn:aws:s3:::*/*` when the allowlist is empty)

2.2 WHEN a synthetic generation task runs in a single-account setup and the worker writes a generated preview image or performs the ETag-conditional manifest append THEN the system SHALL allow `s3:PutObject` on the same object-level resources

2.3 WHEN a synthetic generation task runs in a single-account setup and the worker performs bucket-level operations THEN the system SHALL allow `s3:ListBucket`, `s3:GetBucketLocation`, and `s3:GetBucketTagging` on the bucket-level resources (the allowlist bucket ARNs, or `arn:aws:s3:::*` when the allowlist is empty)

2.4 WHEN the portal is deployed with the `dataBucketAllowlist` context THEN the system SHALL pass the parsed allowlist through `SyntheticDataStackProps` and scope the synthetic data handler role's S3 data-plane grants to exactly those buckets, using the same name-or-ARN normalization as the compute stack's `createLambdaRole`

2.5 WHEN the S3 grants are added to the handler role THEN the system SHALL grant data-plane actions only, with no `aws:ResourceTag` condition on the statements (S3 does not honor it for bucket-level actions — the documented past regression in `compute-stack.ts`)

### Unchanged Behavior (Regression Prevention)

The fix is additive to the handler role's S3 posture only; every other grant and the cross-account path stay exactly as they are.

3.1 WHEN the synthetic data stack is synthesized THEN the `SyntheticDataHandlerRole` SHALL CONTINUE TO carry its existing grants unchanged: DynamoDB table grants (own tables read/write; usecases/user-roles/settings read; audit write; training-jobs read/write), Bedrock `InvokeModel`/`ListFoundationModels`, `sts:AssumeRole` on `DDAPortalAccessRole` in the trusted use case accounts, Lambda self-invoke on the handler function ARN, SageMaker training-job actions, and `iam:PassRole` with the `sagemaker.amazonaws.com` condition

3.2 WHEN the synthetic data stack is synthesized THEN the `SyntheticDataHandlerRole` SHALL CONTINUE TO have no S3 control-plane permissions (no `s3:PutBucketPolicy`, ACL actions, `s3:DeleteBucket`, `s3:PutBucketTagging`, or similar)

3.3 WHEN a synthetic generation task runs in a cross-account setup THEN the system SHALL CONTINUE TO access the use case data bucket via the assumed `DDAPortalAccessRole` credentials, unchanged by this fix

3.4 WHEN the compute stack is synthesized THEN the compute stack's Lambda roles (`createLambdaRole`) SHALL CONTINUE TO produce the same policy statements as before, unchanged by this fix

3.5 WHEN a per-task generation failure occurs THEN the system SHALL CONTINUE TO create the session and record the failure on the preview (existing error surfacing), unchanged by this fix

## Bug Condition

**Bug Condition Function** — identifies inputs that trigger the bug:

```pascal
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

**Property Specification** — defines correct behavior for buggy inputs:

```pascal
// Property: Fix Checking — S3 data-plane access under execution-role credentials
FOR ALL X WHERE isBugCondition(X) DO
  role ← synthesize(SyntheticDataStack).handlerRole
  ASSERT role allows s3:GetObject, s3:PutObject on objectLevelResources(allowlist)
  ASSERT role allows s3:ListBucket, s3:GetBucketLocation, s3:GetBucketTagging
         on bucketLevelResources(allowlist)
END FOR
```

**Preservation Goal**:

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
  // Concretely: all non-S3 statements on SyntheticDataHandlerRole are
  // byte-identical; no S3 control-plane actions appear anywhere on the
  // role; cross-account access still flows through DDAPortalAccessRole;
  // compute-stack roles are untouched.
END FOR
```

**Counterexample** (verified on live account 164152369890, us-east-1): a single-account generate request whose worker calls `s3:GetObject` on `arn:aws:s3:::ryvan-cookies/training-images/anomaly-12.jpg` — denied because no identity-based policy on the handler role allows the action.
