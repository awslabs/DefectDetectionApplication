# Single-Account UseCase Setup

This document describes the simplified single-account setup for creating UseCases without needing to manage cross-account IAM roles.

## What Changed

### Backend (usecases.py)

The `create_usecase` function now supports two modes:

1. **Single-Account Setup** (Simplified)
   - Only requires: `name` and `s3_bucket`
   - Automatically detects AWS account ID from STS
   - Uses default SageMaker role: `arn:aws:iam::ACCOUNT_ID:role/SageMakerExecutionRole`
   - Uses account root as cross-account role: `arn:aws:iam::ACCOUNT_ID:root`

2. **Multi-Account Setup** (Enterprise)
   - Requires: `name`, `account_id`, `s3_bucket`, `cross_account_role_arn`, `sagemaker_execution_role_arn`
   - Allows cross-account access with explicit role ARNs
   - Supports separate Data Account for centralized training data

### How It Works

When creating a UseCase via API:

```bash
# Single-account (simplified)
curl -X POST https://api.../v1/usecases \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "name": "Cookie Defect Detection",
    "s3_bucket": "my-dda-data"
  }'

# Multi-account (enterprise)
curl -X POST https://api.../v1/usecases \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "name": "Cookie Defect Detection",
    "account_id": "123456789012",
    "s3_bucket": "my-dda-data",
    "cross_account_role_arn": "arn:aws:iam::123456789012:role/DDAPortalAccessRole",
    "sagemaker_execution_role_arn": "arn:aws:iam::123456789012:role/SageMakerExecutionRole"
  }'
```

## Frontend Changes (TODO)

The frontend UseCaseOnboarding wizard should be updated to:

1. Add a "Setup Type" selection step:
   - Single Account (Recommended for getting started)
   - Multi-Account (For production with cross-account access)

2. For Single-Account flow:
   - Step 1: Basic Info (Name, Description)
   - Step 2: S3 Storage (Bucket, Prefix)
   - Step 3: Review & Create

3. For Multi-Account flow:
   - Keep existing steps with all role ARN fields

## Testing Single-Account Setup

```bash
# 1. Get auth token
TOKEN=$(aws cognito-idp admin-initiate-auth \
  --user-pool-id us-east-1_YOUR_POOL_ID \
  --client-id YOUR_CLIENT_ID \
  --auth-flow ADMIN_NO_SRP_AUTH \
  --auth-parameters USERNAME=testadmin,PASSWORD=TestAdmin123! \
  --query 'AuthenticationResult.IdToken' \
  --output text)

# 2. Create UseCase (single-account)
curl -X POST https://YOUR_API_URL/v1/usecases \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test UseCase",
    "s3_bucket": "my-test-bucket"
  }'

# 3. Verify UseCase was created
curl -X GET https://YOUR_API_URL/v1/usecases \
  -H "Authorization: Bearer $TOKEN"
```

## Benefits

- **Simpler Onboarding**: No need to create IAM roles or manage cross-account access
- **Faster Setup**: Get started in minutes instead of hours
- **Lower Complexity**: Fewer configuration steps and potential failure points
- **Backward Compatible**: Multi-account setup still works for enterprise deployments

## Limitations

Single-account setup is best for:
- Development and testing
- Small teams in a single AWS account
- Getting started quickly

For production deployments with multiple teams or accounts, use multi-account setup.
