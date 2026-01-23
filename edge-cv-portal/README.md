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

## Quick Deploy

```bash
# 1. Deploy Portal (in Portal Account)
cd infrastructure && cdk deploy --all

# 2. (Optional) Enable automatic CORS configuration
# After first deployment, get your CloudFront domain and redeploy:
cdk deploy --all -c cloudFrontDomain=YOUR_CLOUDFRONT_DOMAIN.cloudfront.net

# 3. Deploy UseCase Role (in UseCase Account)
./deploy-account-role.sh  # Select option 1

# 4. (Optional) Deploy Data Account Role (if using separate Data Account)
./deploy-account-role.sh  # Select option 2
# Then register in portal: Settings → Data Accounts → Add Data Account

# 5. Create UseCase in portal with Role ARN + External ID
#    The following are automatically configured during onboarding:
#    - Bucket policy (for SageMaker cross-account access)
#    - CORS (for browser uploads)
#    - Bucket tagging (dda-portal:managed=true)
#    - Data Account dropdown selection (if registered)
```

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
git clone <repo-url>
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
├── sync-labeling-status.sh # Manual status sync utility
└── ADMIN_GUIDE.md     # Full documentation
```

## Prerequisites

- AWS CLI configured
- Node.js 18+, Python 3.11+
- AWS CDK: `npm install -g aws-cdk`
- [AWS Marketplace subscription](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6) (in UseCase Account)

## Tutorials

### Getting Started
1. [Multi-Account Workflow](TUTORIAL_MULTI_ACCOUNT_WORKFLOW.md) - Complete end-to-end guide
2. [Segmentation Training](TUTORIAL_SEGMENTATION_MULTI_ACCOUNT.md) - Pixel-level defect detection

### Key Features
- [Data Accounts Management](DATA_ACCOUNTS_DEPLOYMENT.md) - Centralized credential management
- [Manifest Transformation](MANIFEST_VALIDATION_FEATURE.md) - Ground Truth to DDA format
- [Inference Uploader](INFERENCE_UPLOADER_CONFIG.md) - Configurable S3 sync

## Support

Check CloudWatch Logs: `/aws/lambda/EdgeCVPortal*`

## Version

Current: v2.0.0 (January 2026)

See [CHANGELOG.md](CHANGELOG.md) for release history.
