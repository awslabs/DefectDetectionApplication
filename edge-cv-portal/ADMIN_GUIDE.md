# DDA Portal - Administrator Guide

Complete guide for deploying, configuring, and managing the Defect Detection Application (DDA) Portal.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Initial Portal Deployment](#initial-portal-deployment)
3. [Account Setup](#account-setup)
4. [Onboarding a New Customer](#onboarding-a-new-customer)
5. [Creating a UseCase](#creating-a-usecase)
6. [Portal Features](#portal-features)
7. [Troubleshooting](#troubleshooting)

**Related Guides:**
- [DATA_ACCOUNT_SETUP.md](DATA_ACCOUNT_SETUP.md) - Detailed data account configuration scenarios
- [SHARED_COMPONENTS.md](SHARED_COMPONENTS.md) - Greengrass component provisioning
- [DEPLOYMENT.md](DEPLOYMENT.md) - Quick deployment reference

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         PORTAL ACCOUNT                                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │
│  │  CloudFront │  │ API Gateway │  │   Cognito   │  │  DynamoDB   │    │
│  │  (Frontend) │  │   (REST)    │  │   (Auth)    │  │  (Storage)  │    │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘    │
│         │                │                                              │
│         └────────────────┼──────────────────────────────────────────────┤
│                          │                                              │
│                    Lambda Functions                                     │
│                          │                                              │
└──────────────────────────┼──────────────────────────────────────────────┘
                           │
            ┌──────────────┴──────────────┐
            │      AssumeRole (STS)       │
            │      with External ID       │
            ▼                             ▼
┌─────────────────────────────┐  ┌─────────────────────────────┐
│     USECASE ACCOUNT         │  │     DATA ACCOUNT (Optional) │
│  ┌─────────────────────┐    │  │  ┌─────────────────────┐    │
│  │ DDAPortalAccessRole │    │  │  │  DDADataAccessRole  │    │
│  └─────────────────────┘    │  │  └─────────────────────┘    │
│                             │  │                             │
│  • SageMaker Training       │  │  • S3 Training Data         │
│  • Ground Truth Labeling    │  │  • Tagged Buckets           │
│  • Greengrass Components    │  │                             │
│  • IoT Core Devices         │  │                             │
│  • S3 Buckets (if no Data)  │  │                             │
└─────────────────────────────┘  └─────────────────────────────┘
```

### Account Types

| Account | Purpose | Required |
|---------|---------|----------|
| **Portal Account** | Hosts the portal infrastructure (API, frontend, auth) | Yes |
| **UseCase Account** | Runs ML workloads (training, labeling, deployments) | Yes |
| **Data Account** | Stores training data in isolated S3 buckets | Optional |

**Flexibility**: All three can be the same AWS account for simple deployments, or separate accounts for enterprise isolation.

### Resource Scoping Model

The portal uses two different scoping models depending on where data is stored:

| Resource Type | Storage | Scoping | Notes |
|---------------|---------|---------|-------|
| Training Jobs | Portal DynamoDB | Per UseCase ID | Strictly isolated by `usecase_id` |
| Labeling Jobs | Portal DynamoDB | Per UseCase ID | Strictly isolated by `usecase_id` |
| Pre-labeled Datasets | Portal DynamoDB | Per UseCase ID | Strictly isolated by `usecase_id` |
| S3 Buckets | UseCase/Data Account | Per AWS Account | All buckets tagged `dda-portal:managed=true` |
| Greengrass Components | UseCase Account | Per AWS Account | All components tagged `dda-portal:managed=true` |
| IoT Devices | UseCase Account | Per AWS Account | All devices tagged `dda-portal:managed=true` |

**Important**: For AWS resources (S3, Components, Devices), selecting a UseCase in the portal determines which AWS account to query via cross-account role. All portal-managed resources in that account will be visible.

**Recommendation**: Use **one UseCase per AWS account** for clear resource isolation. If you need multiple use cases, use separate AWS accounts.

> **Future Enhancement**: UseCase-level isolation for AWS resources can be added by filtering on `dda-portal:usecase-id` tag. Components already include this tag; devices and buckets would need to be updated.

---

## Initial Portal Deployment

### Prerequisites

- AWS CLI configured with admin credentials
- Node.js 18+, Python 3.11+
- AWS CDK: `npm install -g aws-cdk`

### Deploy Portal Infrastructure

```bash
cd edge-cv-portal/infrastructure
npm install
cdk bootstrap  # First time only
cdk deploy --all --require-approval never
```

**Outputs to save:**
- `ApiUrl` - Backend API endpoint
- `UserPoolId` - Cognito User Pool ID
- `UserPoolClientId` - Cognito Client ID
- `DistributionDomainName` - CloudFront URL

### Configure Frontend

```bash
cd ../frontend
npm install

# Create config with CDK outputs
cat > public/config.json << EOF
{
  "apiUrl": "<API_URL>",
  "userPoolId": "<USER_POOL_ID>",
  "userPoolClientId": "<CLIENT_ID>",
  "region": "us-east-1"
}
EOF

npm run build
./deploy-frontend.sh
```

### Create Admin User

```bash
USER_POOL_ID="<your-user-pool-id>"

# Create user
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username admin \
  --user-attributes Name=email,Value=admin@company.com \
  --temporary-password TempPass123!

# Set permanent password
aws cognito-idp admin-set-user-password \
  --user-pool-id $USER_POOL_ID \
  --username admin \
  --password YourSecurePassword123! \
  --permanent

# Get user sub for role assignment
USER_SUB=$(aws cognito-idp admin-get-user \
  --user-pool-id $USER_POOL_ID \
  --username admin \
  --query 'UserAttributes[?Name==`sub`].Value' --output text)

# Assign PortalAdmin role
aws dynamodb put-item \
  --table-name edge-cv-portal-user-roles \
  --item "{
    \"user_id\": {\"S\": \"$USER_SUB\"},
    \"usecase_id\": {\"S\": \"global\"},
    \"role\": {\"S\": \"PortalAdmin\"},
    \"assigned_at\": {\"N\": \"$(date +%s)000\"},
    \"assigned_by\": {\"S\": \"system\"}
  }"
```

---

## Account Setup

### UseCase Account Setup

Run in the **UseCase Account** (where ML workloads will run):

```bash
cd edge-cv-portal
./deploy-account-role.sh
```

Select option `1` for UseCase Account Role.

**What gets created:**
- `DDAPortalAccessRole` - Cross-account access role
- `DDASageMakerExecutionRole` - For training jobs
- `DDAGroundTruthExecutionRole` - For labeling jobs

**Save these outputs:**
- Role ARN
- External ID (generated automatically)
- SageMaker Execution Role ARN

### Data Account Setup (Optional)

If storing training data in a separate account:

```bash
cd edge-cv-portal
./deploy-account-role.sh
```

Select option `2` for Data Account Role.

**What gets created:**
- `DDAPortalDataAccessRole` - For Portal to browse data and update bucket policies
- `DDASageMakerDataAccessRole` - For SageMaker cross-account access

**Save these outputs:**
- Portal Access Role ARN
- External ID

**Note**: The bucket policy for SageMaker access is **automatically configured** when you onboard the UseCase in the portal. The Portal assumes the Data Account role and adds the necessary bucket policy statements.

> **📖 See [DATA_ACCOUNT_SETUP.md](DATA_ACCOUNT_SETUP.md) for detailed scenarios and step-by-step guides.**

### Tag S3 Buckets

The portal uses tag-based access. Tag each bucket:

```bash
aws s3api put-bucket-tagging \
  --bucket YOUR_BUCKET_NAME \
  --tagging 'TagSet=[{Key=dda-portal:managed,Value=true}]'
```

### Configure S3 Bucket CORS (Required for Uploads)

**Important**: Tagging alone is not enough. To upload files from the portal, you must also configure CORS on each bucket.

#### Why CORS is Required
The portal frontend (running in your browser) uploads files directly to S3 using presigned URLs. This is a "cross-origin" request (from CloudFront to S3), which S3 blocks by default.

#### Option 1: AWS Console (Recommended for Non-Technical Users)

1. Go to **S3** in AWS Console
2. Select your bucket
3. Go to **Permissions** tab
4. Scroll to **Cross-origin resource sharing (CORS)** → Click **Edit**
5. Paste this configuration (replace `YOUR_CLOUDFRONT_DOMAIN`):

```json
[
  {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
    "AllowedOrigins": ["https://YOUR_CLOUDFRONT_DOMAIN"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }
]
```

6. Click **Save changes**

#### Option 2: AWS CLI

```bash
# Replace values
BUCKET_NAME="your-bucket-name"
CLOUDFRONT_DOMAIN="d3qeryypza4i9i.cloudfront.net"

aws s3api put-bucket-cors --bucket $BUCKET_NAME --cors-configuration '{
  "CORSRules": [{
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
    "AllowedOrigins": ["https://'"$CLOUDFRONT_DOMAIN"'"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }]
}'
```

#### Option 3: Helper Script

```bash
cd edge-cv-portal
./configure-bucket-cors.sh YOUR_BUCKET_NAME YOUR_CLOUDFRONT_DOMAIN
```

#### Verify CORS Configuration

```bash
aws s3api get-bucket-cors --bucket YOUR_BUCKET_NAME
```

### AWS Marketplace Subscription

**Required in UseCase Account** before training:

1. Go to [Computer Vision Defect Detection Model](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6)
2. Click **Continue to Subscribe** → **Accept Offer**
3. Wait for activation (~2 minutes)

Verify:
```bash
aws sagemaker list-algorithms --name-contains "computer-vision-defect-detection"
```

---

## Onboarding a New Customer

### Step 1: Create Cognito Users

For each user in the customer organization:

```bash
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username user@customer.com \
  --user-attributes Name=email,Value=user@customer.com \
  --temporary-password TempPass123!
```

### Step 2: Deploy UseCase Account Role

Have the customer run in their AWS account:

```bash
./deploy-account-role.sh
# Select option 1 (UseCase Account)
# Enter Portal Account ID when prompted
```

### Step 3: Create UseCase in Portal

1. Login to portal as PortalAdmin
2. Go to **Use Cases** → **Create Use Case**
3. Enter:
   - **Name**: Customer project name
   - **AWS Account ID**: Customer's UseCase Account ID
   - **Cross-Account Role ARN**: From deploy script output
   - **External ID**: From deploy script output
   - **SageMaker Execution Role ARN**: From deploy script output

### Step 4: Assign User Roles

```bash
# Get user's sub
USER_SUB=$(aws cognito-idp admin-get-user \
  --user-pool-id $USER_POOL_ID \
  --username user@customer.com \
  --query 'UserAttributes[?Name==`sub`].Value' --output text)

# Assign to usecase
aws dynamodb put-item \
  --table-name edge-cv-portal-user-roles \
  --item "{
    \"user_id\": {\"S\": \"$USER_SUB\"},
    \"usecase_id\": {\"S\": \"<USECASE_ID>\"},
    \"role\": {\"S\": \"UseCaseAdmin\"},
    \"assigned_at\": {\"N\": \"$(date +%s)000\"},
    \"assigned_by\": {\"S\": \"admin\"}
  }"
```

### Step 5: Tag Customer's S3 Buckets

Customer runs in their account:
```bash
# Tag bucket for portal access
aws s3api put-bucket-tagging \
  --bucket training-data-bucket \
  --tagging 'TagSet=[{Key=dda-portal:managed,Value=true}]'

# Configure CORS for uploads (replace CLOUDFRONT_DOMAIN)
aws s3api put-bucket-cors --bucket training-data-bucket --cors-configuration '{
  "CORSRules": [{
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
    "AllowedOrigins": ["https://CLOUDFRONT_DOMAIN"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }]
}'
```

**Note**: Both tagging AND CORS are required. Tagging enables IAM access; CORS enables browser uploads.

---

## Creating a UseCase

### Via Portal UI

1. Navigate to **Use Cases** → **Create Use Case**
2. Fill in the wizard:
   - **Basic Info**: Name, description, cost center
   - **AWS Account**: Role ARN, External ID, SageMaker Role ARN
   - **S3 Storage**: Bucket name and prefix for outputs
   - **Data Account Configuration**: Choose where training data is stored

#### Data Account Options

| Option | When to Use | What Happens |
|--------|-------------|--------------|
| **Same as UseCase Account** | Data is in the same account as SageMaker | No extra role assumption; simplest setup |
| **Separate Data Account** | Data is in a centralized data lake | Portal assumes Data Account role; SageMaker uses bucket policy |

**For Separate Data Account**, you'll need:
- Data Account ID
- Data Account Role ARN (`DDAPortalDataAccessRole`)
- Data Account External ID
- Data S3 Bucket name

> **📖 See [DATA_ACCOUNT_SETUP.md](DATA_ACCOUNT_SETUP.md) for detailed setup instructions.

### Via API

```bash
curl -X POST "$API_URL/usecases" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Manufacturing Line 1",
    "account_id": "123456789012",
    "cross_account_role_arn": "arn:aws:iam::123456789012:role/DDAPortalAccessRole",
    "external_id": "UUID-FROM-DEPLOY-SCRIPT",
    "sagemaker_execution_role_arn": "arn:aws:iam::123456789012:role/DDASageMakerExecutionRole"
  }'
```

### Update DynamoDB External ID

If you regenerate the External ID:

```bash
aws dynamodb update-item \
  --table-name edge-cv-portal-usecases \
  --key '{"usecase_id": {"S": "<USECASE_ID>"}}' \
  --update-expression "SET external_id = :eid" \
  --expression-attribute-values '{":eid": {"S": "<NEW_EXTERNAL_ID>"}}'
```

---

## Portal Features

### Data Management
- Browse S3 buckets tagged with `dda-portal:managed=true`
- Upload training images via presigned URLs
- Organize data into folders

### Labeling (Ground Truth)
- Create bounding box labeling jobs
- Monitor labeling progress
- Use pre-labeled datasets for quick starts

### Training
- Start SageMaker training jobs using Marketplace algorithm
- Monitor training progress and metrics
- View training logs

### Model Compilation
- Compile models for edge devices (x86-64, ARM64)
- Target CPU or GPU inference
- Automatic Greengrass component creation

### Greengrass Components
- View portal-created components (tagged `dda-portal:managed=true`)
- Component versioning
- Deploy to edge devices

### Device Management
- View portal-managed Greengrass core devices (tagged `dda-portal:managed=true`)
- Monitor device status, installed components, and deployments
- Devices must be set up using `setup_station.sh` script

### Device Setup

Devices are registered using the `setup_station.sh` script in the `station_install/` folder. This script:
- Installs Python 3.9, Java, Docker, and dependencies
- Downloads and installs AWS IoT Greengrass Core v2
- Creates an IoT Thing and provisions certificates
- Tags the IoT Thing with `dda-portal:managed=true` for portal discovery

**Setup Command:**
```bash
cd station_install
sudo ./setup_station.sh <aws-region> <thing-name>

# Example:
sudo ./setup_station.sh us-east-1 manufacturing-line-1-device
```

**For Existing Devices** (set up before portal tagging):
```bash
aws iot tag-resource \
  --resource-arn arn:aws:iot:REGION:ACCOUNT:thing/THING_NAME \
  --tags "Key=dda-portal:managed,Value=true"
```

### Deployments
- Create Greengrass deployments
- Target specific devices or groups
- Monitor deployment status

---

## Troubleshooting

### "Access Denied" on Cross-Account Operations

**Cause**: External ID mismatch between IAM role and DynamoDB.

**Fix**:
```bash
# Check current DynamoDB value
aws dynamodb get-item \
  --table-name edge-cv-portal-usecases \
  --key '{"usecase_id": {"S": "<ID>"}}'

# Update to match IAM role
aws dynamodb update-item \
  --table-name edge-cv-portal-usecases \
  --key '{"usecase_id": {"S": "<ID>"}}' \
  --update-expression "SET external_id = :eid" \
  --expression-attribute-values '{":eid": {"S": "<CORRECT_EXTERNAL_ID>"}}'
```

### "Algorithm does not exist" on Training

**Cause**: AWS Marketplace subscription not active in UseCase Account.

**Fix**: Subscribe to the algorithm in the UseCase Account (not Portal Account).

### Components Not Showing

**Cause**: Components not tagged with `dda-portal:managed=true`.

**Fix**: Only components created through the portal are shown. Create a new component via the training → compilation workflow.

### Devices Not Showing

**Cause**: Devices not tagged with `dda-portal:managed=true` or not set up via `setup_station.sh`.

**Fix**: 
1. Ensure the device was set up using `setup_station.sh` (which auto-tags)
2. For existing devices, manually tag them:
```bash
aws iot tag-resource \
  --resource-arn arn:aws:iot:REGION:ACCOUNT:thing/THING_NAME \
  --tags "Key=dda-portal:managed,Value=true"
```
3. Verify the device is a Greengrass Core Device (not just an IoT Thing)

### S3 Buckets Not Showing

**Cause**: Bucket not tagged.

**Fix**:
```bash
aws s3api put-bucket-tagging \
  --bucket BUCKET_NAME \
  --tagging 'TagSet=[{Key=dda-portal:managed,Value=true}]'
```

### CORS Error When Uploading Files

**Symptom**: Browser console shows `No 'Access-Control-Allow-Origin' header` error.

**Cause**: S3 bucket CORS not configured for portal uploads.

**Fix**: Configure CORS on the bucket (see [Configure S3 Bucket CORS](#configure-s3-bucket-cors-required-for-uploads) section above).

Quick CLI fix:
```bash
BUCKET="your-bucket"
DOMAIN="your-cloudfront-domain.cloudfront.net"

aws s3api put-bucket-cors --bucket $BUCKET --cors-configuration '{
  "CORSRules": [{
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
    "AllowedOrigins": ["https://'"$DOMAIN"'"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }]
}'
```

### Training Job Fails with "Access Denied" to Data Bucket

**Symptom**: SageMaker training job fails with S3 access denied error.

**Cause**: When using a separate Data Account, the bucket policy doesn't allow the UseCase Account's SageMaker role.

**Fix**: Deploy (or redeploy) the Data Account stack with the bucket name:

```bash
cd edge-cv-portal/infrastructure
cdk deploy -a "npx ts-node bin/data-account-app.ts" \
  -c portalAccountId=PORTAL_ID \
  -c usecaseAccountIds=USECASE_ID \
  -c dataBucketNames=your-data-bucket
```

This creates a bucket policy allowing `DDASageMakerExecutionRole` from the UseCase Account to read from the bucket.

### Labeling Job Can't Find Images in Data Account

**Symptom**: "No images found" when creating labeling job, but images exist.

**Cause**: Portal can't assume Data Account role or wrong bucket configured.

**Fix**:
1. Verify UseCase has correct `data_account_role_arn` and `data_s3_bucket`:
   ```bash
   aws dynamodb get-item \
     --table-name edge-cv-portal-usecases \
     --key '{"usecase_id": {"S": "YOUR_ID"}}' \
     --query 'Item.{data_account_id:data_account_id.S, data_s3_bucket:data_s3_bucket.S, data_account_role_arn:data_account_role_arn.S}'
   ```

2. Verify External ID matches between DynamoDB and IAM role trust policy

3. Test role assumption manually:
   ```bash
   aws sts assume-role \
     --role-arn arn:aws:iam::DATA_ACCOUNT:role/DDAPortalDataAccessRole \
     --role-session-name test \
     --external-id YOUR_EXTERNAL_ID
   ```

> **📖 See [DATA_ACCOUNT_SETUP.md](DATA_ACCOUNT_SETUP.md) for complete troubleshooting guide.

### Lambda Timeout on Large Operations

**Cause**: Cross-account API calls taking too long.

**Fix**: Check CloudWatch logs for the specific Lambda function:
```bash
aws logs tail /aws/lambda/EdgeCVPortalComputeStack-ComponentsHandler --follow
```

---

## User Roles and Permissions

### Role Hierarchy

The portal uses a two-layer permission model:

1. **IDP Role (from Cognito/SSO)**: Determines global capabilities
2. **UseCase Assignment (from DynamoDB)**: Determines which usecases a user can access

### Available Roles

| Role | Scope | Description |
|------|-------|-------------|
| **PortalAdmin** | Global | Super user with full access to all usecases and user management |
| **UseCaseAdmin** | Per-UseCase | Full access within assigned usecases, can manage team members |
| **DataScientist** | Per-UseCase | Can create labeling jobs, training jobs, and manage models |
| **Operator** | Per-UseCase | Can create deployments, manage devices, view logs |
| **Viewer** | Per-UseCase | Read-only access to view usecases, jobs, models, deployments |

### Permission Matrix

| Action | PortalAdmin | UseCaseAdmin | DataScientist | Operator | Viewer |
|--------|:-----------:|:------------:|:-------------:|:--------:|:------:|
| **UseCase Management** |
| Create UseCase | ✅ | ✅ | ✅ | ✅ | ✅ |
| View All UseCases | ✅ | ❌ | ❌ | ❌ | ❌ |
| View Assigned UseCases | ✅ | ✅ | ✅ | ✅ | ✅ |
| Update UseCase | ✅ | ✅ | ❌ | ❌ | ❌ |
| Delete UseCase | ✅ | ❌ | ❌ | ❌ | ❌ |
| Manage Team Members | ✅ | ✅ | ❌ | ❌ | ❌ |
| **Labeling** |
| Create Labeling Job | ✅ | ✅ | ✅ | ❌ | ❌ |
| View Labeling Jobs | ✅ | ✅ | ✅ | ✅ | ✅ |
| Delete Labeling Job | ✅ | ✅ | ✅ | ❌ | ❌ |
| **Training** |
| Create Training Job | ✅ | ✅ | ✅ | ❌ | ❌ |
| View Training Jobs | ✅ | ✅ | ✅ | ✅ | ✅ |
| Stop Training Job | ✅ | ✅ | ✅ | ❌ | ❌ |
| **Models** |
| View Models | ✅ | ✅ | ✅ | ✅ | ✅ |
| Compile Model | ✅ | ✅ | ✅ | ❌ | ❌ |
| Package Model | ✅ | ✅ | ✅ | ❌ | ❌ |
| Publish Component | ✅ | ✅ | ✅ | ❌ | ❌ |
| Delete Model | ✅ | ✅ | ✅ | ❌ | ❌ |
| **Deployments** |
| Create Deployment | ✅ | ✅ | ❌ | ✅ | ❌ |
| View Deployments | ✅ | ✅ | ✅ | ✅ | ✅ |
| Cancel Deployment | ✅ | ✅ | ❌ | ✅ | ❌ |
| **Devices** |
| View Devices | ✅ | ✅ | ✅ | ✅ | ✅ |
| Restart Greengrass | ✅ | ✅ | ❌ | ✅ | ❌ |
| Reboot Device | ✅ | ✅ | ❌ | ✅ | ❌ |
| Browse Files | ✅ | ✅ | ❌ | ✅ | ❌ |
| View Logs | ✅ | ✅ | ✅ | ✅ | ✅ |
| Update Config | ✅ | ✅ | ❌ | ✅ | ❌ |

### Self-Service UseCase Creation

Any authenticated user can create a new usecase. When a user creates a usecase:
1. The usecase is created in DynamoDB
2. The creator is automatically assigned as **UseCaseAdmin** for that usecase
3. The creator can then add other team members via "Manage Team"

### Managing Team Members

UseCaseAdmins and PortalAdmins can manage team members for their usecases:

1. Go to **Use Cases** page
2. Click **Actions** → **Manage Team** for the usecase
3. Add users by email and assign a role
4. Remove users as needed

### PortalAdmin as Fallback

PortalAdmins have global access to all usecases. This ensures:
- No usecase becomes orphaned if the UseCaseAdmin leaves
- PortalAdmins can reassign admins when needed
- Emergency access is always available

### Setting User Roles in Cognito

To set a user's IDP role (for PortalAdmin access):

```bash
# Set custom:role attribute
aws cognito-idp admin-update-user-attributes \
  --user-pool-id us-east-1_jBJ4LzuQ8 \
  --username USERNAME \
  --user-attributes Name=custom:role,Value=PortalAdmin
```

Valid role values: `PortalAdmin`, `UseCaseAdmin`, `DataScientist`, `Operator`, `Viewer`

---

## Support

- **Logs**: CloudWatch Logs under `/aws/lambda/EdgeCVPortal*`
- **API Errors**: Check browser DevTools Network tab
- **Infrastructure**: `cdk diff` to see pending changes
