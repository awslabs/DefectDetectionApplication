# DDA Portal - Deployment Guide

## Architecture Overview

The DDA Portal uses a multi-account architecture:

| Account Type | Purpose |
|-------------|---------|
| **Portal Account** | Hosts the portal infrastructure (API, frontend, databases) |
| **UseCase Account(s)** | Where Greengrass devices and SageMaker training run |
| **Data Account** (optional) | Centralized training data storage, shared by multiple usecases |

## Prerequisites

- AWS CLI configured
- Node.js 18+
- Python 3.11+
- AWS CDK CLI: `npm install -g aws-cdk`

---

## Step 1: Deploy Portal Account

```bash
cd edge-cv-portal

# Build shared Lambda layer
cd backend/layers/shared && ./build.sh && cd ../../..

# Deploy infrastructure
cd infrastructure
npm install
npm run build
cdk bootstrap  # Only needed once per account/region
cdk deploy --all --require-approval never
cd ..

# Build and deploy frontend
cd frontend
npm install
npm run build
cd ..
./deploy-frontend.sh
```

**Save the outputs:**
- `FrontendUrl` - Portal URL (CloudFront domain)
- `ApiUrl` - API endpoint
- `UserPoolId` - Cognito User Pool ID
- `UserPoolClientId` - Cognito Client ID

### Enable Automatic CORS Configuration (Recommended)

After the first deployment, redeploy with your CloudFront domain to enable automatic CORS configuration on Data Account buckets:

```bash
cd infrastructure
cdk deploy --all -c cloudFrontDomain=YOUR_CLOUDFRONT_DOMAIN.cloudfront.net
```

This allows the portal to automatically configure CORS when you onboard a UseCase with a separate Data Account.

### Configure Frontend

Update `frontend/public/config.json` with the CDK outputs:

```json
{
  "apiUrl": "YOUR_API_URL",
  "userPoolId": "YOUR_USER_POOL_ID",
  "userPoolClientId": "YOUR_USER_POOL_CLIENT_ID",
  "region": "us-east-1"
}
```

Then rebuild and deploy the frontend:

```bash
cd frontend && npm run build && cd ..
./deploy-frontend.sh
```

---

## Step 2: Deploy UseCase Account Role

For each UseCase account, deploy the cross-account access role:

```bash
# Switch AWS credentials to UseCase account
cd edge-cv-portal
./deploy-account-role.sh
```

Select option **1 (UseCase Account)** and provide:
- Portal Account ID

**Save the outputs:**
- Role ARN
- SageMaker Execution Role ARN
- External ID

---

## Step 3: Configure Data Account (Choose One)

### Option A: Same Account (Recommended for most users)

If training data is stored in the **same account** as the UseCase:

1. During UseCase onboarding in the portal, select **"Same as UseCase Account"**
2. No additional deployment needed
3. SageMaker accesses data using its execution role (same account)

### Option B: Separate Data Account

If training data is in a **different account** (e.g., centralized data lake):

```bash
# Switch AWS credentials to Data account
cd edge-cv-portal
./deploy-account-role.sh
```

Select option **2 (Data Account)** and provide:
- Portal Account ID
- UseCase Account ID(s)

**Save the outputs:**
- Portal Access Role ARN
- External ID

**Note**: The bucket policy for SageMaker access is **automatically configured** when you onboard the UseCase in the portal. No manual bucket policy setup required!

> **📖 See [DATA_ACCOUNT_SETUP.md](DATA_ACCOUNT_SETUP.md) for detailed scenarios and troubleshooting.**

---

## Step 4: Tag S3 Buckets

In the Data account (or UseCase account if not using separate Data account):

```bash
aws s3api put-bucket-tagging --bucket YOUR_BUCKET_NAME \
  --tagging 'TagSet=[{Key=dda-portal:managed,Value=true}]'
```

---

## Step 5: Create Initial Admin User

```bash
# Get User Pool ID from CDK outputs
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalAuthStack \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' \
  --output text)

# Create admin user
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username admin \
  --user-attributes Name=email,Value=admin@example.com \
  --temporary-password TempPassword123!
```

---

## Step 6: Access the Portal

1. Open the `FrontendUrl` from Step 1
2. Login with admin credentials
3. Create a UseCase using the role ARNs and external IDs from Steps 2-3

---

## Updating the Frontend

After code changes:

```bash
cd edge-cv-portal/frontend
npm run build
cd ..
./deploy-frontend.sh
```

---

## Cleanup

```bash
# Portal account
cd edge-cv-portal/infrastructure
cdk destroy --all

# UseCase account (switch credentials first)
aws cloudformation delete-stack --stack-name DDAPortalUseCaseAccountStack

# Data account (switch credentials first)
aws cloudformation delete-stack --stack-name DDAPortalDataAccountStack
```

---

## Troubleshooting

### Frontend shows "ERR_NAME_NOT_RESOLVED" or API errors
The API Gateway URL in `frontend/public/config.json` is outdated. Get the current URL:

```bash
aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalComputeStack \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiUrl`].OutputValue' \
  --output text
```

Update `config.json` with the new URL, rebuild and redeploy the frontend.

### Frontend shows "NoSuchKey" error
Run `./deploy-frontend.sh` to upload frontend files to S3.

### Cross-account role assumption fails
- Verify External ID matches
- Check role trust policy includes Portal account
- Ensure role exists in target account

### SageMaker can't access Data account

**Cause**: Bucket policy not configured for cross-account access.

**Fix**: The bucket policy should be automatically configured during UseCase onboarding. Check the UseCase in DynamoDB:

```bash
aws dynamodb get-item \
  --table-name edge-cv-portal-usecases \
  --key '{"usecase_id": {"S": "YOUR_USECASE_ID"}}' \
  --query 'Item.data_bucket_policy_result'
```

If the status is "failed", ensure the Data Account role has `s3:GetBucketPolicy` and `s3:PutBucketPolicy` permissions.

> **📖 See [DATA_ACCOUNT_SETUP.md](DATA_ACCOUNT_SETUP.md) for complete data account configuration guide.**
