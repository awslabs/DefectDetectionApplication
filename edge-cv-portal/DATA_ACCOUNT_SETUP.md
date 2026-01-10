# Data Account Configuration Guide

This guide explains how to configure data storage for the DDA Portal across different account scenarios.

## Overview

The DDA Portal supports three deployment scenarios for training data storage:

| Scenario | Description | Complexity | Use When |
|----------|-------------|------------|----------|
| **Same Account** | Data stored in UseCase Account | Simple | Single team, no data isolation needed |
| **Separate Data Account** | Data in dedicated account | Medium | Centralized data lake, multiple usecases share data |
| **All-in-One** | Portal = UseCase = Data | Simplest | Development, POC, single-user setups |

## Key Feature: Automatic Bucket Policy Configuration

When using a **Separate Data Account**, the portal **automatically configures** the S3 bucket policy during UseCase onboarding. This means:

- ✅ No manual bucket policy setup required
- ✅ No need to run CDK with `dataBucketNames` parameter
- ✅ Just deploy the Data Account role and onboard the UseCase
- ✅ Portal adds the necessary policy statements to allow SageMaker access

## Quick Reference

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           SCENARIO COMPARISON                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  SAME ACCOUNT (Recommended for most users)                                  │
│  ─────────────────────────────────────────                                  │
│  Portal Account ──────► UseCase Account                                     │
│                         ├── SageMaker Training                              │
│                         ├── Ground Truth Labeling                           │
│                         ├── Greengrass Devices                              │
│                         └── S3 Training Data ◄── Data stored here           │
│                                                                             │
│  Steps: 1. Deploy UseCase stack  2. Onboard UseCase (select "Same Account") │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  SEPARATE DATA ACCOUNT (For enterprise/multi-tenant)                        │
│  ───────────────────────────────────────────────────                        │
│  Portal Account ──────► UseCase Account                                     │
│                         ├── SageMaker Training ─────┐                       │
│                         ├── Ground Truth Labeling   │ reads from            │
│                         └── Greengrass Devices      │                       │
│                                                     ▼                       │
│                         Data Account ◄──────────────┘                       │
│                         └── S3 Training Data                                │
│                                                                             │
│  Steps: 1. Deploy UseCase stack  2. Deploy Data stack  3. Onboard UseCase   │
│         (Bucket policy is automatically configured during step 3!)          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Scenario 1: Same Account (Data in UseCase Account)

**Best for**: Most deployments where data isolation between accounts isn't required.

### How It Works

- Training data is stored in S3 buckets within the UseCase Account
- SageMaker training jobs read data from the same account
- No cross-account bucket policies needed
- Simplest setup with fewest moving parts

### Setup Steps

#### Step 1: Deploy UseCase Account Stack

In the **UseCase Account**, run:

```bash
cd edge-cv-portal
./deploy-account-role.sh
# Select option 1: UseCase Account
# Enter Portal Account ID when prompted
```

Save the outputs:
- `Role ARN`
- `External ID`
- `SageMaker Execution Role ARN`

#### Step 2: Create S3 Bucket for Training Data

```bash
# Create bucket
aws s3 mb s3://my-training-data-bucket

# Tag for portal discovery
aws s3api put-bucket-tagging \
  --bucket my-training-data-bucket \
  --tagging 'TagSet=[{Key=dda-portal:managed,Value=true}]'

# Configure CORS for browser uploads (replace CLOUDFRONT_DOMAIN)
aws s3api put-bucket-cors --bucket my-training-data-bucket --cors-configuration '{
  "CORSRules": [{
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
    "AllowedOrigins": ["https://YOUR_CLOUDFRONT_DOMAIN"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }]
}'
```

#### Step 3: Onboard UseCase in Portal

1. Go to **Use Cases** → **Create Use Case**
2. Complete the wizard:
   - Enter UseCase Account details (Role ARN, External ID)
   - Enter S3 bucket name
3. In the **Data Account Configuration** step:
   - Select **"Same as UseCase Account"** (default)
4. Complete the wizard

### What Happens Behind the Scenes

When you select "Same as UseCase Account":
- The portal stores `data_account_id = account_id`
- Labeling jobs read images from the UseCase Account bucket
- Training jobs access data using the SageMaker execution role (same account)
- No additional role assumptions needed

---

## Scenario 2: Separate Data Account

**Best for**: Enterprise deployments where:
- Training data is centralized in a data lake
- Multiple UseCases share the same data
- Data governance requires separate account isolation

### How It Works

- Training data is stored in a dedicated Data Account
- Portal assumes a role in Data Account to list/browse data
- **Bucket policy is automatically updated** during usecase onboarding to allow SageMaker access
- SageMaker in UseCase Account reads data via bucket policy

### Setup Steps

#### Step 1: Deploy UseCase Account Stack

In the **UseCase Account**, run:

```bash
cd edge-cv-portal
./deploy-account-role.sh
# Select option 1: UseCase Account
# Enter Portal Account ID when prompted
```

Save the outputs:
- `Role ARN`
- `External ID`
- `SageMaker Execution Role ARN`

#### Step 2: Deploy Data Account Role

In the **Data Account**, generate an external ID first (this is required for production security):

```bash
# Generate a secure external ID
EXTERNAL_ID=$(uuidgen)
echo "Save this External ID securely: $EXTERNAL_ID"
```

Then deploy the Data Account stack:

```bash
cd edge-cv-portal/infrastructure

# Deploy with required parameters
npx cdk deploy -a "npx ts-node bin/data-account-app.ts" \
  -c portalAccountId=YOUR_PORTAL_ACCOUNT_ID \
  -c usecaseAccountIds=YOUR_USECASE_ACCOUNT_ID \
  -c externalId=$EXTERNAL_ID
```

**⚠️ IMPORTANT**: Save the External ID securely! You will need it when onboarding UseCases.

This creates a role that allows the Portal to:
- Browse S3 buckets and list objects
- **Automatically update bucket policies** for SageMaker access
- **Automatically configure CORS** for browser uploads

**Important**: The Data Account role includes these S3 permissions for automatic configuration:
- `s3:GetBucketPolicy` / `s3:PutBucketPolicy` - for SageMaker access
- `s3:GetBucketCors` / `s3:PutBucketCors` - for browser uploads

Save the outputs:
- `Portal Access Role ARN`
- `External ID` (the one you generated)

> **Note**: You do NOT need to specify bucket names during deployment. The bucket policy and CORS are automatically configured when you onboard the UseCase in the portal.

#### Step 3: Tag Data Bucket (CORS is auto-configured)

```bash
# Tag for portal discovery
aws s3api put-bucket-tagging \
  --bucket your-data-bucket \
  --tagging 'TagSet=[{Key=dda-portal:managed,Value=true}]'
```

> **Note**: CORS is automatically configured during UseCase onboarding. No manual CORS setup needed!

#### Step 4: Onboard UseCase in Portal

1. Go to **Use Cases** → **Create Use Case**
2. Complete the wizard:
   - Enter UseCase Account details
   - Enter UseCase S3 bucket (for outputs/manifests)
3. In the **Data Account Configuration** step:
   - Select **"Separate Data Account"**
   - Enter Data Account ID
   - Enter Data Account Role ARN (from Step 2)
   - Enter Data Account External ID (**required** - the UUID you generated in Step 2)
   - Enter Data S3 Bucket name
4. Complete the wizard

**The portal will automatically:**
- Configure CORS on the Data Account bucket for browser uploads
- Update the Data Account bucket policy to allow the UseCase's SageMaker role to read
- Tag the bucket for portal discovery
- Provision shared Greengrass components to the UseCase Account

### What Happens Behind the Scenes

When you configure a separate Data Account:

1. **During Onboarding**:
   - Portal assumes Data Account role
   - Adds bucket policy statements allowing UseCase's `DDASageMakerExecutionRole` to read
   - Policy is idempotent (won't duplicate if already exists)

2. **Labeling Jobs**:
   - Portal assumes Data Account role to list images
   - Creates manifest file in UseCase Account
   - Manifest references images in Data Account bucket

3. **Training Jobs**:
   - SageMaker runs in UseCase Account
   - Reads manifest from UseCase Account bucket
   - Reads images from Data Account bucket (via bucket policy)
   - Writes model artifacts to UseCase Account bucket

4. **Bucket Policy** (automatically created):
   ```json
   {
     "Sid": "AllowSageMakerRead-USECASE_ACCOUNT_ID",
     "Effect": "Allow",
     "Principal": {
       "AWS": "arn:aws:iam::USECASE_ACCOUNT:role/DDASageMakerExecutionRole"
     },
     "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectTagging"],
     "Resource": "arn:aws:s3:::data-bucket/*"
   }
   ```

---

## Scenario 3: All-in-One (Single Account)

**Best for**: Development, POC, or single-user setups.

### How It Works

- Portal, UseCase, and Data all in the same AWS account
- Simplest possible setup
- No cross-account roles needed (but still deployed for consistency)

### Setup Steps

#### Step 1: Deploy Portal

```bash
cd edge-cv-portal/infrastructure
npm install
cdk deploy --all
```

#### Step 2: Deploy UseCase Stack (Same Account)

```bash
./deploy-account-role.sh
# Select option 1: UseCase Account
# Enter the SAME account ID as Portal
```

#### Step 3: Onboard UseCase

1. Create UseCase in portal
2. Select **"Same as UseCase Account"** for data
3. Use the same S3 bucket for everything

---

## Troubleshooting

### SageMaker Training Fails with "Access Denied" to Data Bucket

**Cause**: Bucket policy not configured for cross-account access.

**Solution**: 
1. Check if bucket policy was configured during onboarding:
   ```bash
   aws dynamodb get-item \
     --table-name edge-cv-portal-usecases \
     --key '{"usecase_id": {"S": "YOUR_ID"}}' \
     --query 'Item.data_bucket_policy_result'
   ```

2. If status is "failed", check the error message. Common issues:
   - **Access Denied**: Data Account role needs `s3:GetBucketPolicy` and `s3:PutBucketPolicy`
   - **NoSuchBucket**: Bucket name is incorrect

3. Manually verify/add bucket policy:
   ```bash
   # In Data Account
   aws s3api get-bucket-policy --bucket YOUR_BUCKET --query Policy --output text | jq .
   ```

### Automatic Bucket Policy Update Failed

**Symptom**: UseCase created but `data_bucket_policy_configured` is false.

**Cause**: Data Account role lacks S3 bucket policy permissions.

**Solution**: Add these permissions to the Data Account role:
```json
{
  "Effect": "Allow",
  "Action": [
    "s3:GetBucketPolicy",
    "s3:PutBucketPolicy"
  ],
  "Resource": "arn:aws:s3:::YOUR_DATA_BUCKET"
}
```

Then re-onboard the UseCase or manually add the bucket policy.

### Labeling Job Can't List Images from Data Account

**Cause**: Portal can't assume Data Account role.

**Solution**: 
1. Verify `data_account_role_arn` is correct in UseCase configuration
2. Check External ID matches between DynamoDB and IAM role
3. Verify role trust policy includes Portal Account

### "No images found" When Creating Labeling Job

**Cause**: Wrong bucket or prefix configured.

**Solution**:
1. Verify `data_s3_bucket` is correct
2. Check `data_s3_prefix` points to folder with images
3. Ensure images have supported extensions (.jpg, .jpeg, .png)

### Manifest References Wrong Bucket

**Cause**: Data Account configuration mismatch.

**Solution**: Check UseCase configuration in DynamoDB:

```bash
aws dynamodb get-item \
  --table-name edge-cv-portal-usecases \
  --key '{"usecase_id": {"S": "YOUR_USECASE_ID"}}' \
  --query 'Item.{data_account_id:data_account_id.S, data_s3_bucket:data_s3_bucket.S}'
```

---

## Migration Guide

### Migrating Existing UseCase to Separate Data Account

1. **Deploy Data Account Stack** (see Scenario 2, Step 2)

2. **Update UseCase Configuration**:
   ```bash
   aws dynamodb update-item \
     --table-name edge-cv-portal-usecases \
     --key '{"usecase_id": {"S": "YOUR_USECASE_ID"}}' \
     --update-expression "SET data_account_id = :dai, data_account_role_arn = :dar, data_account_external_id = :dae, data_s3_bucket = :dsb" \
     --expression-attribute-values '{
       ":dai": {"S": "DATA_ACCOUNT_ID"},
       ":dar": {"S": "arn:aws:iam::DATA_ACCOUNT_ID:role/DDAPortalDataAccessRole"},
       ":dae": {"S": "EXTERNAL_ID"},
       ":dsb": {"S": "data-bucket-name"}
     }'
   ```

3. **Copy Training Data** (if needed):
   ```bash
   aws s3 sync s3://old-usecase-bucket/datasets/ s3://new-data-bucket/datasets/
   ```

4. **Update Existing Manifests** (if any):
   - Manifests reference S3 URIs
   - May need to update `source-ref` paths if bucket changed

---

## Best Practices

### Security

1. **Always use External IDs** for cross-account roles (required for production)
   - Prevents "confused deputy" attacks
   - Generate unique UUIDs for each Data Account deployment
   - Store External IDs securely (e.g., AWS Secrets Manager)

2. **Principle of Least Privilege**
   - Data Account role only has permissions needed for portal operations
   - SageMaker access is granted via bucket policy, not role assumption

3. **Audit Cross-Account Access**
   - Enable CloudTrail in all accounts
   - Monitor `AssumeRole` events for the Data Account role

### Architecture

4. **Use Separate Data Account** when:
   - Multiple teams share training data
   - Data governance requires isolation
   - You have a centralized data lake

5. **Use Same Account** when:
   - Single team/project
   - Simpler operations preferred
   - No data sharing requirements

### Operations

6. **Always tag buckets** with `dda-portal:managed=true`

7. **CORS is auto-configured** - no manual setup needed when using Data Account

8. **Document your account mapping** for team reference:
   ```
   Portal Account:  164152369890  (hosts the portal)
   UseCase Account: 198226511894  (runs SageMaker, Greengrass)
   Data Account:    814373574263  (stores training data)
   External ID:     xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (keep secret!)
   ```
