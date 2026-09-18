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
8. [User Roles and Permissions](#user-roles-and-permissions)
9. [Portal Users and Privilege (Portal_Identity Registry)](#portal-users-and-privilege-portal_identity-registry)

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

The **first** PortalAdmin is provisioned by hand, because nobody can sign in to
the portal yet. This is the one legitimate exception to
"[provision users through the portal only](#provisioning-a-user--through-the-portal-only)":
the Cognito account alone grants nothing — the Portal_Identity row in
`dda-portal-user-roles` is what makes it a PortalAdmin. Create every subsequent
account in the portal's **User Manager**.

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

# Assign PortalAdmin role: the global Portal_Identity row is what grants it
aws dynamodb put-item \
  --table-name dda-portal-user-roles \
  --item "{
    \"user_id\": {\"S\": \"$USER_SUB\"},
    \"usecase_id\": {\"S\": \"global\"},
    \"role\": {\"S\": \"PortalAdmin\"},
    \"status\": {\"S\": \"enabled\"},
    \"username\": {\"S\": \"admin\"},
    \"email\": {\"S\": \"admin@company.com\"},
    \"assigned_at\": {\"N\": \"$(date +%s)000\"},
    \"assigned_by\": {\"S\": \"system\"}
  }"
```

> Setting `custom:role` on the Cognito account is **not** required and grants
> nothing — see
> [Portal Users and Privilege](#portal-users-and-privilege-portal_identity-registry).

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

### Complete Onboarding Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    USECASE ONBOARDING FLOW                               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  1. Deploy UseCaseAccountStack (in UseCase Account)                     │
│     └─> Creates: DDAPortalAccessRole, DDASageMakerExecutionRole,        │
│                  DDAPortalComponentAccessPolicy                          │
│                                                                          │
│  2. (Optional) Deploy DataAccountStack (in Data Account)                │
│     └─> Creates: DDAPortalDataAccessRole                                │
│                                                                          │
│  3. Create UseCase in Portal UI                                         │
│     └─> Stores: Account IDs, Role ARNs, External IDs                    │
│                                                                          │
│  4. Provision Shared Components (in Portal)                             │
│     └─> Creates: Greengrass components in UseCase Account               │
│     └─> Updates: S3 bucket policy for cross-account access              │
│                                                                          │
│  5. Setup Edge Device (on physical device)                              │
│     └─> Creates: GreengrassV2TokenExchangeRole                          │
│     └─> Attaches: DDAPortalComponentAccessPolicy                        │
│     └─> Tags: Device for portal discovery                               │
│                                                                          │
│  6. Deploy to Device (in Portal)                                        │
│     └─> Device downloads artifacts from Portal's S3 bucket              │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### Step 1: Create Cognito Users

Create each user in the portal's **User Manager** (sign in as a PortalAdmin →
user menu top-right → **User Manager** → **Create user**). That path writes both
the Cognito account and its Portal_Identity registry row, which is what grants
portal access — see
[Portal Users and Privilege](#portal-users-and-privilege-portal_identity-registry).

Creating the account with the CLI instead leaves it with **no registry row**, so
it can sign in and is then denied every route:

```bash
# Not sufficient on its own — the portal User Manager is the supported path
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

Assign roles in the portal: **Use Cases** → **Actions** → **Manage Team** for
per-UseCase roles, or the **User Manager** for the account-level (`global`) role.
Both write the Portal_Identity registry, which is what the API resolves
privilege from.

The equivalent direct write, for scripted onboarding:

```bash
# Get user's sub
USER_SUB=$(aws cognito-idp admin-get-user \
  --user-pool-id $USER_POOL_ID \
  --username user@customer.com \
  --query 'UserAttributes[?Name==`sub`].Value' --output text)

# Assign to usecase (usecase_id='global' would set the account-level role)
aws dynamodb put-item \
  --table-name dda-portal-user-roles \
  --item "{
    \"user_id\": {\"S\": \"$USER_SUB\"},
    \"usecase_id\": {\"S\": \"<USECASE_ID>\"},
    \"role\": {\"S\": \"UseCaseAdmin\"},
    \"status\": {\"S\": \"enabled\"},
    \"username\": {\"S\": \"user@customer.com\"},
    \"email\": {\"S\": \"user@customer.com\"},
    \"assigned_at\": {\"N\": \"$(date +%s)000\"},
    \"assigned_by\": {\"S\": \"admin\"}
  }"
```

> A per-UseCase row does **not** replace the account's `global` row: with
> enforcement on, an account with no enabled `global` row is denied everywhere.
> See [Portal Users and Privilege](#portal-users-and-privilege-portal_identity-registry).

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
- Creates `GreengrassV2TokenExchangeRole` for device credentials
- Attaches `DDAPortalComponentAccessPolicy` for cross-account S3 access and ECR image pulls
- Tags the Greengrass Core Device with `dda-portal:managed=true` for portal discovery

**Prerequisites:**
1. Deploy `UseCaseAccountStack` first (creates `DDAPortalComponentAccessPolicy`)
2. AWS CLI configured on the device with UseCase Account credentials

**Setup Command:**
```bash
cd station_install
sudo ./setup_station.sh <aws-region> <thing-name>

# Example:
sudo ./setup_station.sh us-east-1 manufacturing-line-1-device
```

**For Existing Devices** (set up before portal tagging):
```bash
# Tag the Greengrass Core Device (not IoT Thing)
aws greengrassv2 tag-resource \
  --resource-arn arn:aws:greengrass:REGION:ACCOUNT:coreDevices:THING_NAME \
  --tags "dda-portal:managed=true"

# Attach the component access policy
aws iam attach-role-policy \
  --role-name GreengrassV2TokenExchangeRole \
  --policy-arn "arn:aws:iam::ACCOUNT:policy/DDAPortalComponentAccessPolicy"
```

### Deployments
- Create Greengrass deployments
- Target specific devices or groups
- Monitor deployment status

---

## Troubleshooting

### "Insufficient permissions" (403) for a user who should have access

**Cause**: with registry enforcement on, the account has no **enabled** global
Portal_Identity row (`dda-portal-user-roles`, `usecase_id='global'`). A Cognito
`custom:role` attribute grants nothing. The handler log shows
`Registry denial: user <sub> has no enabled Portal_Identity …` and the audit
entry records `identity_source=absent`.

**Fix**: provision the account in the portal's **User Manager**, or re-run
`backfill_portal_registry.py --apply`. Full procedure, including how to read the
row and how to turn enforcement off again:
[Portal Users and Privilege](#portal-users-and-privilege-portal_identity-registry).

A 500 `Authorization check failed` is a different problem — the registry lookup
itself failed (check the table's health), and is deliberately not reported as a
permission denial.

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
aws greengrassv2 tag-resource \
  --resource-arn arn:aws:greengrass:REGION:ACCOUNT:coreDevices:THING_NAME \
  --tags "dda-portal:managed=true"
```
3. Verify the device is a Greengrass Core Device (not just an IoT Thing)

### Deployment Fails with "S3 Access Denied" on Device

**Symptom**: Deployment shows `FAILED_NO_STATE_CHANGE` with "S3 HeadObject returns 403 Access Denied".

**Cause**: The device's `GreengrassV2TokenExchangeRole` doesn't have permission to access the Portal Account's component bucket.

**Fix**:
1. Verify `DDAPortalComponentAccessPolicy` exists in the UseCase Account:
```bash
aws iam get-policy --policy-arn "arn:aws:iam::USECASE_ACCOUNT:policy/DDAPortalComponentAccessPolicy"
```

2. If missing, redeploy `UseCaseAccountStack`:
```bash
cd edge-cv-portal/infrastructure
npm run build
rm -rf cdk.out
cdk deploy -a "npx ts-node bin/usecase-account-app.ts" \
  -c portalAccountId=PORTAL_ACCOUNT_ID \
  -c externalId=YOUR_EXTERNAL_ID \
  --require-approval never
```

3. Attach the policy to the device role:
```bash
aws iam attach-role-policy \
  --role-name GreengrassV2TokenExchangeRole \
  --policy-arn "arn:aws:iam::USECASE_ACCOUNT:policy/DDAPortalComponentAccessPolicy"
```

4. Verify the Portal's component bucket policy allows the UseCase Account:
```bash
aws s3api get-bucket-policy --bucket dda-component-REGION-PORTAL_ACCOUNT
```

The policy should include both `GreengrassV2TokenExchangeRole` and `greengrass.amazonaws.com` service principal.

### Component Provisioning Fails with "Artifact Cannot Be Accessed"

**Symptom**: Re-provisioning shared components fails with "Specified artifact resource cannot be accessed".

**Cause**: The Greengrass service can't validate the S3 artifact during `CreateComponentVersion`.

**Fix**: Update the Portal's component bucket policy to include the Greengrass service principal:
```bash
# This is automatically done during provisioning, but if it fails:
aws s3api put-bucket-policy --bucket dda-component-REGION-PORTAL_ACCOUNT --policy '{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowUseCaseAccountsGreengrassAccess",
      "Effect": "Allow",
      "Principal": {
        "AWS": [
          "arn:aws:iam::USECASE_ACCOUNT:role/DDAPortalAccessRole",
          "arn:aws:iam::USECASE_ACCOUNT:role/GreengrassV2TokenExchangeRole"
        ]
      },
      "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:GetBucketLocation"],
      "Resource": [
        "arn:aws:s3:::dda-component-REGION-PORTAL_ACCOUNT",
        "arn:aws:s3:::dda-component-REGION-PORTAL_ACCOUNT/*"
      ]
    },
    {
      "Sid": "AllowGreengrassServiceAccess",
      "Effect": "Allow",
      "Principal": {
        "Service": "greengrass.amazonaws.com"
      },
      "Action": ["s3:GetObject", "s3:GetBucketLocation"],
      "Resource": [
        "arn:aws:s3:::dda-component-REGION-PORTAL_ACCOUNT",
        "arn:aws:s3:::dda-component-REGION-PORTAL_ACCOUNT/*"
      ],
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": ["PORTAL_ACCOUNT", "USECASE_ACCOUNT"]
        }
      }
    }
  ]
}'
```

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

The portal uses a two-layer permission model, both layers held in the
Portal_Identity registry (`dda-portal-user-roles`):

1. **Account-level role** (`usecase_id='global'`): determines global capabilities
2. **UseCase assignment** (`usecase_id=<usecase>`): determines which usecases a
   user can access, and overrides the global role within that usecase

The Cognito / SSO `custom:role` attribute is descriptive metadata and is **not**
a privilege source — see
[Portal Users and Privilege](#portal-users-and-privilege-portal_identity-registry).

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

### Setting User Roles

Set roles through the portal's **User Manager** (see
[Portal Users and Privilege](#portal-users-and-privilege-portal_identity-registry)).
The User Manager writes both the Cognito `custom:role` attribute **and** the
Portal_Identity registry row, and the registry row is what grants privilege.

Valid role values: `PortalAdmin`, `UseCaseAdmin`, `DataScientist`, `Operator`, `Viewer`, `DataLabeler`

> **Do not** grant a role with `aws cognito-idp admin-update-user-attributes
> --user-attributes Name=custom:role,...`. With registry enforcement on, that
> attribute grants **nothing** — it is descriptive metadata that is recorded in
> the audit trail and never used as a privilege source. An account whose only
> "role" is a `custom:role` attribute is denied every route with HTTP 403.

---

## Portal Users and Privilege (Portal_Identity Registry)

This section is the operator runbook for the portal's authorization model after
the JWT-role privilege-escalation fix
(`.kiro/specs/portal-jwt-role-privilege-escalation`). Read it before creating
users, changing roles, or enabling enforcement.

### The registry is the authority

Portal privilege comes from a **Portal_Identity row** in the DynamoDB table
`dda-portal-user-roles`, keyed (`user_id` = Cognito `sub`, `usecase_id`):

| Attribute | Meaning |
|-----------|---------|
| `user_id` | The account's Cognito `sub` |
| `usecase_id` | `global` = the account-level entry; any other value = a UseCase assignment |
| `role` | `PortalAdmin` / `UseCaseAdmin` / `DataScientist` / `Operator` / `Viewer` / `DataLabeler` |
| `status` | `enabled` or `disabled` (a row written before this fix carries none and is treated as enabled) |
| `username`, `email` | Human-readable attribution, captured at write time |
| `assigned_by`, `assigned_at` | Who provisioned the row and when (`backfill` for rows the backfill script wrote) |

Resolution on every request (`shared_utils.RBACManager.get_user_role`):

1. No **enabled global** row for the caller's `sub` → **no role** → HTTP 403
   `Insufficient permissions`, audited with `identity_source=absent` and the
   claimed `custom:role`.
2. An enabled global row → that row's role.
3. An enabled UseCase row, when the request is scoped to that UseCase →
   that row's role, overriding the global role (unchanged Team Management
   precedence).
4. The registry lookup **failing** (DynamoDB error) → HTTP 500
   `Authorization check failed`, audited `result='failure'`. An outage is never
   recorded as a permission decision and never downgrades the caller to
   `Viewer`.

The token's `custom:role` claim is **Claimed_Role**: descriptive metadata only.
It is recorded in audit entries (a mismatch between claim and registry role is
itself a signal) and never grants anything. This is the fix: a single
`cognito-idp:AdminCreateUser` call carrying `custom:role=PortalAdmin` no longer
mints a privileged portal principal, because that account has no registry row.

Every audit entry in `dda-portal-audit-log` now also carries `username`,
`email`, `source_ip`, `user_agent`, and `identity_source`, captured from the
request at write time, so deleting the Cognito account afterwards cannot erase
who acted.

> The User Manager's account list shows each account's Cognito `custom:role`
> attribute, not its registry role. For accounts provisioned through the portal
> the two agree. To read what actually decides privilege, query the registry
> (see [Verifying an account](#verifying-an-account)).

### Provisioning a user — through the portal only

Use the portal's **User Manager** (sign in as a PortalAdmin → user menu
top-right → **User Manager**, route `/admin/user-manager`). It is the only path
that keeps Cognito and the registry in step:

| Action | Cognito | Registry (`usecase_id='global'`) |
|--------|---------|----------------------------------|
| Create account | `AdminCreateUser` (+ attributes) | row written with `role`, `username`, `email`, `status='enabled'` |
| Change role | `custom:role` updated | `role` updated — **this** is the value that takes effect |
| Disable / enable | `AdminDisableUser` / `AdminEnableUser` | `status` set `disabled` / `enabled` |
| Delete | `AdminDeleteUser` | global row removed, then every per-UseCase row |

Notes:

- If the Cognito account is created but the registry write fails, the API
  answers **502** and records the partial state in the audit trail. The
  account is inert (no registry row → denied), so the safe recovery is to
  delete it in the User Manager and create it again.
- **Disabling** matters: Cognito's own disable only blocks new sign-ins, so an
  already-issued token keeps working until it expires. The registry row's
  `status=disabled` is what stops that token on its **next request**. The same
  is true of deletion.
- Per-UseCase grants keep being made through **Manage Team** on the UseCases
  page; they write the same table with the UseCase's id as `usecase_id`.
- Creating users with `aws cognito-idp admin-create-user` is **not**
  provisioning. Such an account can sign in and gets a valid token, and is
  then denied every route. The only exception is the bootstrap admin, below.

#### Bootstrapping the first PortalAdmin

The first PortalAdmin cannot be created through the portal (nobody can sign in
yet), so it is provisioned by hand — Cognito account plus registry row. See
[Create Admin User](#create-admin-user). Every subsequent account should go
through the User Manager.

### Backfilling the registry (before enforcement)

The registry was historically populated only by Team Management's per-UseCase
grants, so an existing portal has almost no global rows. Enabling enforcement
first would deny **every** user, including the bootstrap `admin`. Run the
backfill first: `edge-cv-portal/backfill_portal_registry.py` writes the global
row each existing enabled pool account needs, carrying the role that account
effectively has today (its `custom:role`, or `Viewer` when absent), so enabling
enforcement changes nobody's access.

It is **dry run by default**, idempotent, and re-runnable: it never overwrites
an existing row (conditional write), skips **disabled** Cognito accounts so they
stay denied, and touches only `usecase_id='global'` rows.

```bash
cd edge-cv-portal

# 1. Review the plan — writes nothing
./backfill_portal_registry.py \
  --user-pool-id us-east-1_XXXXXXXXX --region us-east-1

# 2. Apply, after reviewing every line of the plan
./backfill_portal_registry.py \
  --user-pool-id us-east-1_XXXXXXXXX --region us-east-1 --apply

# 3. Re-run the dry run: every account should now report `exists`
./backfill_portal_registry.py \
  --user-pool-id us-east-1_XXXXXXXXX --region us-east-1
```

Flags: `--table-name` (default `$USER_ROLES_TABLE`, else
`dda-portal-user-roles`), `--region`, `--page-size`. Credentials need
`cognito-idp:ListUsers` on the pool and `dynamodb:GetItem`/`PutItem` on the
table. Exit status: 0 = run completed, 1 = at least one account could not be
processed, 2 = usage error.

What to check in the plan before applying:

- Every account you expect is listed, and the `role=` column matches the access
  that account should keep. `claim=<none>` accounts are backfilled as `Viewer`.
- `exists` lines say the row was left unchanged; a
  `(differs from claim …)` note means the registry and the attribute disagree —
  the registry wins, which is the point, but it is worth a look.
- `skip-disabled` accounts stay denied by design. Re-enable them in the
  User Manager if that is wrong.
- `error` lines mean that account could not be processed (the registry read or
  write failed) and still has **no** row: fix the cause and re-run.

### The enforcement flag

`PORTAL_REGISTRY_ENFORCED` is the single switch (design Decision 4). It is a
Lambda environment variable on every portal handler that resolves roles, and it
**defaults to off**.

| Flag | Behaviour |
|------|-----------|
| off (default, `false`) | Legacy resolution — the `custom:role` claim still grants — **plus** a `would_deny` WARNING naming every request enforcement would have denied. A dry run against real traffic. |
| on (`true`) | The registry is authoritative, per [The registry is the authority](#the-registry-is-the-authority). |

Deploy with it on (only after the backfill):

```bash
cd edge-cv-portal
PORTAL_REGISTRY_ENFORCED=true ./deploy-infrastructure.sh
```

The script passes this through as the CDK context value
`-c portalRegistryEnforced=true`. Accepted affirmatives are `1`, `true`, `yes`,
`on`, `enabled` (trimmed, case-insensitive) — exactly what the Lambda layer
accepts, so a value that reads "on" at deploy time can never land as "off" in
the handler. Anything else, including unset, deploys enforcement **off**;
`cdk.json` deliberately does not carry the key, so a flag-less deploy is always
off. To turn enforcement back off, redeploy without the variable.

**Size the gap before flipping.** With the flag off, mine the dry-run WARNINGs
from the deployed logs — each one names a principal the backfill has not
covered:

```bash
# Log group names are CDK-generated (EdgeCVPortal<Stack>-<Handler><hash>);
# sweep every portal handler rather than guessing one.
for LG in $(aws logs describe-log-groups --region us-east-1 \
  --log-group-name-prefix /aws/lambda/EdgeCVPortal \
  --query 'logGroups[].logGroupName' --output text); do
  aws logs filter-log-events --region us-east-1 --log-group-name "$LG" \
    --filter-pattern would_deny --start-time $(( ($(date +%s) - 86400) * 1000 )) \
    --query 'events[].message' --output text
done
```

Each line carries the `sub`, the scope, `claimed_role`, `claimed_username`, and
the `legacy_role` the caller is getting today. A quiet log across all portal
handlers, over a period that covers your real users, is the signal that
enforcement is safe to enable. (`would_deny undetermined` lines mean the
registry lookup itself failed — fix that first; they say nothing about
coverage.)

### Rollout order

1. Deploy with the flag **off** and confirm every portal handler shows
   `PORTAL_REGISTRY_ENFORCED=false`.
2. Backfill: dry run → review → `--apply` → dry run again (all `exists`).
3. Watch the `would_deny` WARNINGs until they stop for real users.
4. Verify each known account resolves the role you expect (below).
5. Redeploy with `PORTAL_REGISTRY_ENFORCED=true`.
6. Re-verify: the bootstrap `admin` can still reach the User Manager, build
   operators can still `POST /builds`, and a UseCase member still sees their
   UseCase.
7. Confirm the hole is closed: create a throwaway pool account with
   `custom:role=PortalAdmin` and **no** registry row, present its token to
   `POST /builds` — expect **403** — then delete the account and confirm the
   denial's audit row still names its username and email.

If enforcement locks someone out, the fastest fix is to add their global row
(User Manager, or the backfill script re-run for a newly created account).
Redeploying with the flag unset restores the previous behaviour wholesale.

### Verifying an account

```bash
USER_POOL_ID="us-east-1_XXXXXXXXX"
USERNAME="user@company.com"

USER_SUB=$(aws cognito-idp admin-get-user \
  --user-pool-id "$USER_POOL_ID" --username "$USERNAME" \
  --query 'UserAttributes[?Name==`sub`].Value' --output text)

# The row that decides privilege (global scope)
aws dynamodb get-item --table-name dda-portal-user-roles \
  --key "{\"user_id\":{\"S\":\"$USER_SUB\"},\"usecase_id\":{\"S\":\"global\"}}"

# Every row for the account, global plus per-UseCase
aws dynamodb query --table-name dda-portal-user-roles \
  --key-condition-expression 'user_id = :u' \
  --expression-attribute-values "{\":u\":{\"S\":\"$USER_SUB\"}}"
```

An account is provisioned when the `global` row exists, `status` is `enabled`
(or absent), and `role` names the intended role. Unexpected 403s under
enforcement are almost always a missing or `disabled` global row; the handler
log records `Registry denial: user <sub> has no enabled Portal_Identity …` and
the audit entry records `identity_source=absent`.

### Defense in depth and detection — not the fix

Two further controls ship with this fix. **Neither prevents the incident**, and
neither should be mistaken for the actual control, which is the registry
enforced in the shared RBAC layer:

- **SRP-only app client.** The `dda-portal-client` app client no longer enables
  the `USER_PASSWORD_AUTH` or `ADMIN_USER_PASSWORD_AUTH` (`ADMIN_NO_SRP_AUTH`)
  flows, so a password issued out of band with `AdminSetUserPassword` cannot be
  exchanged for portal tokens through those flows. This is **defense in depth
  only**: an actor holding `cognito-idp:UpdateUserPoolClient` can simply
  re-enable the flows, and SRP itself works with an admin-set password.
  Browser sign-in, the new-password challenge, forgot-password and token
  refresh are unaffected (Amplify signs in with SRP by default; the portal's
  refresh uses `REFRESH_TOKEN_AUTH`, which Cognito always allows).
- **Out-of-band Cognito administration alerts.** An EventBridge rule
  `dda-portal-cognito-admin-activity` matches CloudTrail `AdminCreateUser`,
  `AdminSetUserPassword`, `AdminUpdateUserAttributes`, `AdminAddUserToGroup`,
  `AdminEnableUser`, `AdminDisableUser` and `AdminDeleteUser` on **this** user
  pool from any caller other than the portal's User Manager Lambda role, and
  publishes to the SNS topic `dda-portal-cognito-admin-alerts` (stack output
  `CognitoAdminActivityTopicArn`). The message carries the event name, caller
  ARN, source IP, user agent, event time, pool id, region and CloudTrail
  `eventID`. This is **detection only**: it prevents nothing, and an actor with
  `events:DisableRule` can silence it.

  Two operator steps are required for it to work:

  ```bash
  # 1. Subscribe someone (subscriptions are managed out of band)
  aws sns subscribe --region us-east-1 \
    --topic-arn "$(aws cloudformation describe-stacks \
      --stack-name EdgeCVPortalComputeStack --region us-east-1 \
      --query 'Stacks[0].Outputs[?OutputKey==`CognitoAdminActivityTopicArn`].OutputValue' \
      --output text)" \
    --protocol email --notification-endpoint oncall@company.com

  # 2. Confirm a CloudTrail trail logs management events in this region —
  #    without one, "AWS API Call via CloudTrail" events never reach
  #    EventBridge and the rule stays silent. The portal does not create a
  #    trail (an account/organization trail normally already exists; a second
  #    one would duplicate delivery and cost).
  aws cloudtrail describe-trails --region us-east-1 \
    --query 'trailList[].{Name:Name,Multi:IsMultiRegionTrail,Home:HomeRegion}'
  ```

**The complementary preventive control lives outside this repo.** Anyone who
can call `cognito-idp:Admin*` on the pool can still create accounts, set
passwords and delete users — they just cannot obtain portal privilege that way,
and the actions are attributable and alerted. Restricting `cognito-idp:Admin*`
to the portal's own User Manager role via an **SCP or a permission boundary**
is the control that stops the calls themselves; it is an operational task for
whoever governs the AWS account, and this repo cannot enforce it. Likewise,
anyone able to edit IAM, CloudFormation or the Lambda code can grant themselves
anything — that is an explicit non-goal of the fix.

#### Known caveat

The `/admin/*` User Manager routes are additionally gated by
`user_admin.require_portal_admin`, which reads the `custom:role` **claim**
rather than the registry, so under enforcement an unprovisioned principal
claiming `PortalAdmin` can still reach those routes (all its other portal access
is denied). Treat `cognito-idp:AdminUpdateUserAttributes` on the pool as
User-Manager-equivalent privilege until that check is moved onto the registry,
and keep the alerting above subscribed.

---

## Support

- **Logs**: CloudWatch Logs under `/aws/lambda/EdgeCVPortal*`
- **API Errors**: Check browser DevTools Network tab
- **Infrastructure**: `cdk diff` to see pending changes
