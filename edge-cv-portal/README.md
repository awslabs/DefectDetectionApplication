# Defect Detection Application (DDA) Portal

Multi-tenant admin portal for managing computer vision defect detection workloads on AWS edge devices.

## 🆕 What's New

### Latest Features
- **Manifest Transformation**: Auto-transform Ground Truth manifests to DDA format with dropdown selection
- **Labeling Job Status Sync**: Real-time status updates from SageMaker Ground Truth
- **Smart Training Setup**: Auto-detect transformed manifests with visual indicators (✓/⚠️)
- **Data Accounts Management**: Centralized credential management with dropdown selection
- **Inference Uploader**: Optional, configurable S3 upload (immediate to daily intervals)
- **Manifest Validation**: Pre-training validation with helpful error messages

See [CHANGELOG.md](CHANGELOG.md) for complete version history.

## Quick Links

| Document | Description |
|----------|-------------|
| [CHANGELOG.md](CHANGELOG.md) | Version history and release notes |
| [TUTORIAL_MULTI_ACCOUNT_WORKFLOW.md](TUTORIAL_MULTI_ACCOUNT_WORKFLOW.md) | Complete end-to-end tutorial |
| [TUTORIAL_SEGMENTATION_MULTI_ACCOUNT.md](TUTORIAL_SEGMENTATION_MULTI_ACCOUNT.md) | Segmentation model training guide |
| [ADMIN_GUIDE.md](ADMIN_GUIDE.md) | Complete deployment & administration guide |
| [DATA_ACCOUNTS_DEPLOYMENT.md](DATA_ACCOUNTS_DEPLOYMENT.md) | Data Accounts Management feature guide |
| [MANIFEST_VALIDATION_FEATURE.md](MANIFEST_VALIDATION_FEATURE.md) | Manifest validation and transformation |
| [INFERENCE_UPLOADER_CONFIG.md](INFERENCE_UPLOADER_CONFIG.md) | Inference Uploader configuration options |
| [SHARED_COMPONENTS.md](SHARED_COMPONENTS.md) | Greengrass component provisioning |
| [COST_ESTIMATE.md](COST_ESTIMATE.md) | Cost breakdown for Portal and UseCase accounts |

## Features

| Feature | Description |
|---------|-------------|
| **Data Management** | Browse & upload training images to S3 |
| **Data Accounts** | Centralized management of cross-account data access with dropdown selection |
| **Labeling** | Create Ground Truth labeling jobs (classification & segmentation) |
| **Manifest Transformation** | Auto-transform Ground Truth manifests to DDA-compatible format |
| **Training** | SageMaker training with AWS Marketplace algorithm and manifest validation |
| **Compilation** | Compile models for edge (x86-64, ARM64) |
| **Components** | Manage Greengrass components |
| **Deployments** | Deploy models to edge devices with optional Inference Uploader |
| **Devices** | Monitor IoT Greengrass devices |
| **Settings** | Portal configuration and Data Accounts management (PortalAdmin only) |

## Simplified Workflow

### From Labeling to Deployment in 5 Steps

```
1. Labeling
   ↓ Create labeling job → Complete labeling
   ↓ Status auto-syncs to "Completed"
   
2. Transform Manifest
   ↓ Click "Transform Manifest" button
   ↓ Select job from dropdown (auto-fills URIs)
   ↓ Click "Transform" → Creates DDA-compatible manifest
   
3. Training
   ↓ Click "Create Training Job"
   ↓ Select Ground Truth job (shows ✓ Transformed)
   ↓ Manifest auto-validated → Start training
   
4. Compilation
   ↓ Select target architecture (ARM64/x86)
   ↓ Compile model for edge deployment
   
5. Deployment
   ↓ Create deployment to device/group
   ↓ Optional: Enable Inference Uploader for S3 sync
```

**Key Improvements:**
- **Dropdown-driven**: No manual S3 URI entry
- **Auto-fill**: Manifests, credentials, configurations
- **Validation**: Catches issues before training
- **Visual feedback**: ✓ Transformed, ⚠️ Not transformed
- **Status sync**: Real-time updates from SageMaker

## Architecture

```
Portal Account          UseCase Account         Data Account (Optional)
┌──────────────┐       ┌──────────────┐        ┌──────────────┐
│ CloudFront   │       │ SageMaker    │        │ S3 Buckets   │
│ API Gateway  │──────▶│ Greengrass   │◀──────▶│ (Training    │
│ Cognito      │ STS   │ IoT Core     │        │  Data)       │
│ DynamoDB     │       │ S3 Buckets   │        │              │
└──────────────┘       └──────────────┘        └──────────────┘
```

## Prerequisites

- AWS CLI configured
- Node.js 18+, Python 3.11+
- AWS CDK: `npm install -g aws-cdk`
- [AWS Marketplace subscription](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6) (in UseCase Account)

### AWS Account Setup (Portal Account)

Before deploying the Portal, configure CloudWatch Logs for API Gateway:

```bash
# 1. Create IAM role for API Gateway CloudWatch Logs
aws iam create-role \
  --role-name APIGatewayCloudWatchLogsRole \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "apigateway.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

# 2. Attach CloudWatch Logs policy
aws iam attach-role-policy \
  --role-name APIGatewayCloudWatchLogsRole \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchLogsFullAccess

# 3. Get the role ARN
ROLE_ARN=$(aws iam get-role --role-name APIGatewayCloudWatchLogsRole --query 'Role.Arn' --output text)

# 4. Set in API Gateway account settings
aws apigateway update-account \
  --patch-operations op=replace,path=/cloudwatchRoleArn,value=$ROLE_ARN \
  --region us-east-1

# 5. Verify (should show cloudwatchRoleArn in output)
aws apigateway get-account --region us-east-1
```

**Note**: This is a one-time setup per AWS account. If the role already exists, skip to step 4.

## Deployment

### Step 1: Deploy Portal Infrastructure (Portal Account)

**Prerequisites:**
- AWS CDK installed: `npm install -g aws-cdk`
- Node.js 14+ installed
- AWS credentials configured for Portal Account

**Bootstrap CDK (first time only):**

```bash
cd edge-cv-portal/infrastructure
cdk bootstrap
```

**Deploy Portal Infrastructure:**

```bash
cd edge-cv-portal/infrastructure
cdk deploy --all
```

This deploys:
- Frontend (React UI via CloudFront + S3)
- Backend APIs (Lambda + API Gateway)
- Authentication (Cognito)
- Database (DynamoDB)
- Storage (S3 buckets)
- Event processing (EventBridge)

### Step 2: Build and Deploy Frontend

The frontend React app must be built and uploaded to S3:

```bash
cd edge-cv-portal
./deploy-frontend.sh
```

This script:
- Installs dependencies
- Builds the production bundle
- Syncs to S3
- Invalidates CloudFront cache
- Configures CORS for cross-account access

**Output:** Portal URL will be displayed (e.g., `https://d1r8hupkjbsjb1.cloudfront.net`)

**Note**: Without this step, you'll see S3 "NoSuchKey" errors when accessing the CloudFront URL.

### Step 3: Post-Deployment Setup

After deployment completes:

1. **Access Portal**: Open the CloudFront URL in your browser
2. **Create Admin User**: Follow [ADMIN_GUIDE.md - Create Admin User](ADMIN_GUIDE.md#create-admin-user) to create your first admin account
3. **Onboard UseCase Accounts**: Use the Portal UI to add UseCase accounts and configure cross-account access

### Step 4: Deploy UseCase Account Role (UseCase Account)

**Bootstrap CDK in UseCase Account (first time only):**

```bash
cdk bootstrap aws://YOUR_USECASE_ACCOUNT_ID/us-east-1
```

**Deploy UseCase Role:**

```bash
cd edge-cv-portal
./deploy-account-role.sh  # Select option 1
```

This creates:
- IAM role for Portal to assume
- SageMaker execution role for training/compilation
- S3 access policies for model artifacts
- Greengrass device policies

**Output**: The script saves configuration to `usecase-account-YOUR_ACCOUNT_ID-config.txt` with:
- Role ARN
- SageMaker Execution Role ARN
- External ID
- Account ID

Use these values when creating the UseCase in the Portal.

### Step 5: (Optional) Deploy Data Account Role (Data Account)

If using a separate Data Account for training data:

**Bootstrap CDK in Data Account (first time only):**

```bash
cdk bootstrap aws://YOUR_DATA_ACCOUNT_ID/us-east-1
```

**Deploy Data Account Role:**

```bash
cd edge-cv-portal
./deploy-account-role.sh  # Select option 2
```

This creates:
- Portal access role
- SageMaker access role
- S3 bucket policies (auto-configured by Portal)

**Register in Portal** (optional but recommended for dropdown feature):
1. Log in as PortalAdmin
2. Go to Settings → Data Accounts
3. Click "Add Data Account"
4. Upload the generated config file
5. Fill in bucket details and click "Register"

Then when creating UseCases, you can select from a dropdown instead of manual entry.

### Step 6: Create UseCase in Portal

1. Log in to Portal as PortalAdmin
2. Go to Settings → UseCases
3. Click "Add UseCase"
4. Fill in:
   - UseCase Name
   - Account ID (from config file)
   - Role ARN (from config file)
   - External ID (from config file)
   - (Optional) Data Account (if registered)
5. Click "Create"

The Portal automatically configures:
- S3 bucket policies for SageMaker cross-account access
- CORS for browser uploads
- Bucket tagging for management
- EventBridge forwarding for job status updates

## Configuration Options

### Inference Uploader (Optional)

The Inference Uploader is **disabled by default**. Enable per UseCase in DynamoDB:

```json
{
  "enable_inference_uploader": true,
  "inference_uploader_interval_seconds": 300,  // 5 minutes (default)
  "inference_uploader_s3_bucket": "custom-bucket",  // Optional
  "inference_uploader_batch_size": 100,  // Default
  "inference_uploader_retention_days": 7  // Default
}
```

**Common intervals:**
- `10` - Immediate (every 10 seconds)
- `300` - Every 5 minutes (default)
- `3600` - Hourly
- `86400` - Daily

See [INFERENCE_UPLOADER_CONFIG.md](INFERENCE_UPLOADER_CONFIG.md) for complete configuration options.

## Build Server Setup

The DDA LocalServer component must be built on an architecture matching your edge devices.

### Launch ARM64 Build Server

```bash
# From the main DefectDetectionApplication directory
cd ..

# Quick launch (creates new security group)
./launch-arm64-build-server.sh --key-name your-key-name

# Enterprise (use existing infrastructure)
./launch-arm64-build-server.sh \
  --key-name your-key-name \
  --security-group-id sg-xxxxxxxx \
  --subnet-id subnet-xxxxxxxx \
  --iam-profile dda-build-role

# Preview without creating
./launch-arm64-build-server.sh --key-name your-key-name --dry-run
```

### Build and Publish Component

```bash
# SSH to build server
ssh -i "your-key.pem" ubuntu@<build-server-ip>

# Clone and setup
git clone https://github.com/awslabs/DefectDetectionApplication.git
cd DefectDetectionApplication
./setup-build-server.sh

# Build ARM64 component
./gdk-component-build-and-publish.sh
```

See main [README.md](../README.md) for complete build server options.

## Project Structure

```
edge-cv-portal/
├── infrastructure/     # AWS CDK stacks
├── backend/           # Lambda functions (Python)
│   └── functions/
│       ├── labeling.py          # Labeling & manifest transformation
│       ├── training.py          # Training with validation
│       ├── data_accounts.py     # Data Accounts management
│       └── deployments.py       # Deployments with Inference Uploader
├── frontend/          # React app (TypeScript)
│   ├── pages/
│   │   ├── Labeling.tsx         # Labeling jobs list
│   │   ├── TransformManifest.tsx # Manifest transformation
│   │   ├── CreateTraining.tsx   # Training with job selector
│   │   └── Settings.tsx         # Data Accounts management
│   └── components/
│       └── ManifestTransformer.tsx # Transformation UI
├── deploy-account-role.sh  # UseCase/Data account setup
├── deploy-frontend.sh      # Frontend build and deployment
├── sync-labeling-status.sh # Manual status sync utility
└── ADMIN_GUIDE.md          # Full documentation
```

## Tutorials

### Getting Started
1. [Multi-Account Workflow](TUTORIAL_MULTI_ACCOUNT_WORKFLOW.md) - Complete end-to-end guide
2. [Segmentation Training](TUTORIAL_SEGMENTATION_MULTI_ACCOUNT.md) - Pixel-level defect detection

### Key Features
- [Data Accounts Management](DATA_ACCOUNTS_DEPLOYMENT.md) - Centralized credential management
- [Manifest Transformation](MANIFEST_VALIDATION_FEATURE.md) - Ground Truth to DDA format
- [Inference Uploader](INFERENCE_UPLOADER_CONFIG.md) - Configurable S3 sync

## Troubleshooting

### CDK Bootstrap Error

If you see: `SSM parameter /cdk-bootstrap/hnb659fds/version not found`

**Solution**: Bootstrap the account first:
```bash
cdk bootstrap aws://YOUR_ACCOUNT_ID/YOUR_REGION
```

### CloudWatch Logs Error

If you see: `CloudWatch Logs role ARN must be set in account settings`

**Solution**: Follow the AWS Account Setup section above to create and configure the CloudWatch Logs role.

### Role ARN Not Populated

If the config file has empty Role ARN and SageMaker Role ARN fields:

1. Verify CDK deployment succeeded: `aws cloudformation list-stacks --query 'StackSummaries[?StackName==`DDAPortalUseCaseAccountStack`]'`
2. Check stack outputs: `aws cloudformation describe-stacks --stack-name DDAPortalUseCaseAccountStack --query 'Stacks[0].Outputs'`
3. Re-run the deploy script: `./deploy-account-role.sh`

### Frontend Not Accessible

If you see S3 "NoSuchKey" errors:

1. Verify frontend was deployed: `aws s3 ls s3://dda-portal-frontend-YOUR_ACCOUNT_ID/`
2. Re-run frontend deployment: `./deploy-frontend.sh`
3. Wait for CloudFront cache invalidation (up to 5 minutes)

## Support

Check CloudWatch Logs: `/aws/lambda/EdgeCVPortal*`

## Version

Current: v2.0.0 (January 2026)

See [CHANGELOG.md](CHANGELOG.md) for release history.
