# Defect Detection Application (DDA) - Portal Edition

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=flat&logo=docker&logoColor=white)](https://www.docker.com/)

The Defect Detection Application (DDA) is an edge-deployed computer vision solution for quality assurance in discrete manufacturing environments. Originally developed by the AWS EdgeML service team, DDA is now available as an open-source project under the stewardship of the AWS Manufacturing TFC and Auto/Manufacturing IBU.

## Table of Contents

- [Overview](#overview)
- [Deployment](#deployment)
  - [Step 1: Deploy Portal Infrastructure](#step-1-deploy-portal-infrastructure-portal-account)
  - [Step 2: Build and Deploy Frontend](#step-2-build-and-deploy-frontend)
  - [Step 3: Post-Deployment Setup](#step-3-post-deployment-setup)
  - [Step 4: Build DDA Application](#step-4-build-dda-application-build-server)
  - [Step 5: Set Up Accounts](#step-5-set-up-accounts)
  - [Step 6: Create UseCase](#step-6-create-usecase)
  - [Step 7: Setting Up Edge Servers](#step-7-setting-up-edge-servers)
- [Deploy DDA Application to Edge Device](#deploy-dda-application-to-edge-device)
- [Deployments](#deployments)
- [Devices](#devices)
- [Using the Portal](#using-the-portal)
- [ML Workflow](#ml-workflow)
- [Optional Datasets](#optional-datasets-before-model-training)
- [Inference Results Upload](#inference-results-upload-optional)
- [Edge Device Management](#edge-device-management)
- [Troubleshooting](#troubleshooting)
- [Project Structure](#project-structure)
- [Contributing](#contributing)
- [License](#license)
- [Support](#support)

## Overview

DDA provides real-time defect detection capabilities for manufacturing quality control using computer vision and machine learning. The system runs at the edge using AWS IoT Greengrass, enabling low-latency inference and reducing dependency on cloud connectivity.

### Key Benefits

- **Real-time Processing**: Sub-second inference times for immediate quality feedback
- **Edge Deployment**: Operates independently of cloud connectivity
- **Scalable Architecture**: Supports multiple camera inputs and production lines
- **ML Model Flexibility**: Compatible with various computer vision models
- **Manufacturing Integration**: RESTful APIs for integration with existing systems
- **Portal Management**: Centralized multi-tenant admin portal for managing all deployments

### Portal Features

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
| **Inference Uploader** | Automatically upload inference results from edge devices to S3 |
| **Devices** | Monitor IoT Greengrass devices |
| **Settings** | Portal configuration and Data Accounts management (PortalAdmin only) |

## Deployment

### Architecture Overview

DDA supports both single-account and multi-account architectures:

**Single-Account** (Recommended for Getting Started)
- All components in one AWS account
- Simplest setup and management
- Requires running `./deploy-account-role.sh` (select Option 1) to create the `DDASageMakerExecutionRole`

**Multi-Account** (Recommended for Production)
- Separates Portal, UseCase, and Data accounts
- Better security and governance
- Requires cross-account IAM roles (created by `deploy-account-role.sh`)

#### Architecture Components

```
Portal Account          UseCase Account         Data Account (Optional)
┌──────────────┐       ┌──────────────┐        ┌──────────────┐
│ CloudFront   │       │ SageMaker    │        │ S3 Buckets   │
│ API Gateway  │──────▶│ Greengrass   │◀──────▶│ (Training    │
│ Cognito      │ STS   │ IoT Core     │        │  Data)       │
│ DynamoDB     │       │ S3 Buckets   │        │              │
└──────────────┘       └──────────────┘        └──────────────┘
```

**Portal Account** - Central management hub
- DDA Portal web interface, user authentication and RBAC
- Training and compilation job orchestration

**UseCase Account** - ML workflow execution
- SageMaker training jobs, model compilation for edge deployment
- Greengrass component management
- Can be the same as Portal Account for single-account setups

**Data Account** (Optional) - Centralized data storage
- Training datasets and inference results from edge devices

### Prerequisites

- AWS CLI configured
- Node.js 18+, Python 3.11+
- AWS CDK: `npm install -g aws-cdk`
- [AWS Marketplace subscription](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6) (in UseCase Account — see below)

> **Important: AWS Marketplace Subscription Required**
> 
> The DDA training algorithm is an AWS Marketplace product. You must subscribe **in the UseCase Account** (the account where SageMaker runs) before creating training jobs.
> 
> 1. Sign in to the **UseCase Account** AWS Console
> 2. Go to [AWS Marketplace - DDA Algorithm](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6)
> 3. Click **Continue to Subscribe** → **Accept Terms**
> 4. Wait for the subscription to become active (usually a few minutes)
> 
> For multi-account setups, the subscription must be in the UseCase Account (where SageMaker runs), not the Portal Account or Data Account.

### Step 1: Deploy Portal Infrastructure (Portal Account)

#### 1.1 Configure CloudWatch Logs for API Gateway

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

# 3. Get the role ARN and set in API Gateway
ROLE_ARN=$(aws iam get-role --role-name APIGatewayCloudWatchLogsRole --query 'Role.Arn' --output text)
aws apigateway update-account \
  --patch-operations op=replace,path=/cloudwatchRoleArn,value=$ROLE_ARN \
  --region us-east-1
```

> **Note**: This is a one-time setup per AWS account.

#### 1.2 Deploy Portal Infrastructure with CDK

```bash
cd edge-cv-portal

# First time only: bootstrap CDK in your account
cd infrastructure
npm install
npx cdk bootstrap
cd ..

# Deploy all stacks
./deploy-infrastructure.sh
```

> **Note**: `cdk bootstrap` must be run from `edge-cv-portal/infrastructure/` where `cdk.json` lives. The `deploy-infrastructure.sh` script handles `npm install` and `cdk deploy` for you after that.

**What gets deployed:** CloudFront, API Gateway, Cognito, DynamoDB, Lambda functions, S3 buckets, IAM roles.

**Save the CDK outputs:** `PortalApiEndpoint`, `PortalFrontendUrl`, `CognitoUserPoolId`, `CognitoClientId`.

### Step 2: Build and Deploy Frontend

```bash
cd edge-cv-portal
./deploy-frontend.sh
```

This automatically generates `config.json` from CDK stack outputs, builds the React app, uploads to S3, and invalidates CloudFront cache.

### Step 3: Post-Deployment Setup

#### 3.1 Shared Components

When you deploy the portal infrastructure, the following Greengrass components are automatically provisioned and shared with UseCase accounts:

- **LocalServer** - Core DDA inference component
- **InferenceUploader** - Optional component for uploading inference results to S3

These are automatically shared when you create a UseCase in the portal. The UseCase creation also provisions the `DDAPortalComponentAccessPolicy` required by edge devices.

#### 3.2 Create Portal Admin User

```bash
# Get User Pool ID from CDK output (or use the value from config.json)
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalAuthStack \
  --query 'Stacks[0].Outputs[?OutputKey==`AuthConfig`].OutputValue' \
  --output text --region us-east-1 | python3 -c "import sys,json; print(json.load(sys.stdin)['userPoolId'])")
REGION="us-east-1"

aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username admin \
  --message-action SUPPRESS \
  --temporary-password TempPassword123! \
  --region $REGION

aws cognito-idp admin-set-user-password \
  --user-pool-id $USER_POOL_ID \
  --username admin \
  --password YourSecurePassword123! \
  --permanent \
  --region $REGION

aws cognito-idp admin-update-user-attributes \
  --user-pool-id $USER_POOL_ID \
  --username admin \
  --user-attributes Name=custom:role,Value=PortalAdmin \
  --region $REGION
```

### Step 4: Build DDA Application (Build Server)

The DDA application is the core Greengrass component that runs inference on edge devices.

#### 4.1 Build and Publish

Before launching the build server, find your public IP to configure SSH access in the security group:

```bash
curl -L https://ifconfig.me
```

Then launch and connect:

```bash
# Launch build server (from edge-cv-portal directory)
cd edge-cv-portal
./launch-arm64-build-server.sh --key-name YOUR_KEY_NAME

# Set permissions on your key pair
chmod 400 ~/.ssh/YOUR_KEY_NAME.pem

# Connect to the build server
ssh -i ~/.ssh/YOUR_KEY_NAME.pem ubuntu@<PUBLIC_IP>

# Clone the repo and build
mkdir -p ~/workspace
cd ~/workspace
git clone https://github.com/awslabs/DefectDetectionApplication.git
cd DefectDetectionApplication
./setup-build-server.sh
./gdk-component-build-and-publish.sh
```

**Launch script options:**
- `--key-name KEY` (required) - SSH key pair name
- `--subnet-id SUBNET` - Subnet ID for VPC selection
- `--security-group-id SG` - Security group ID
- `--instance-type TYPE` - EC2 instance type (default: m6g.4xlarge)
- `--volume-size SIZE` - Root volume size in GB (default: 100)
- `--region REGION` - AWS region (default: us-east-1)
- `--iam-profile PROFILE` - IAM instance profile name (default: dda-build-role, auto-created if missing)

**What the build script does:**
- Creates IAM role (`dda-build-role`) if needed
- Detects architecture (x86_64 or aarch64)
- Builds Docker images (backend and frontend)
- Publishes to AWS Greengrass component repository

**Debugging:** `VERBOSE=1 ./gdk-component-build-and-publish.sh`

> **Notice**: Stop the EC2 build server after building to avoid unnecessary costs.

<details>
<summary><strong>Manual Setup (Optional)</strong></summary>

If you prefer manual IAM role and EC2 setup, create a role with trust policy for `ec2.amazonaws.com` and attach permissions for Greengrass, IoT, S3, EC2, CloudWatch Logs, CloudWatch Metrics, and ECR. See the inline policy in the collapsed section of the original documentation.

</details>

### Step 5: Set Up Accounts

#### 5.1 UseCase Account (Required)

```bash
cd edge-cv-portal
./deploy-account-role.sh
```

- **Option 1**: Single-account setup (creates `DDASageMakerExecutionRole`)
- **Option 2**: Multi-account UseCase account (creates `DDAPortalAccessRole` + `DDASageMakerExecutionRole`)

Save the generated `usecase-account-*-config.txt` file — you'll upload it when creating a UseCase.

#### 5.2 Data Account (Optional, for multi-account)

If your training data lives in a separate AWS account:

```bash
cd edge-cv-portal
./deploy-account-role.sh
# Select option 3: Data Account
```

Save the generated `data-account-*-config.txt` file, then register it in the portal:

1. Go to **Settings** → **Data Accounts** → **Add Data Account**
2. Upload the `data-account-*-config.txt` file to auto-fill the fields
3. Enter a name and click **Register**

This only needs to be done once per data account. All future UseCases can select it from a dropdown.

### Step 6: Create UseCase

1. Go to **UseCases** → **Create UseCase**
2. Choose setup type (Single Account or Multi-Account)
3. For multi-account: upload `usecase-account-*-config.txt` to auto-fill role details
4. Configure data account (select from registered accounts or enter manually)
5. Click **Create**

This provisions Greengrass components, creates `DDAPortalComponentAccessPolicy`, and sets up S3 buckets.

### Step 7: Setting Up Edge Servers

Edge device setup is a manual process. After building the DDA application, provision edge servers with AWS IoT Greengrass.

**Prerequisites:**
- DDA application built and published (Step 4)
- Device credentials configured using `station_install/edge-device-iam-policy.json` (see [Edge Device IAM Permissions](#edge-device-iam-permissions) below)

#### Testing with EC2 Instances

```bash
# Launch edge device (from the station_install directory)
cd station_install
./launch-edge-device.sh \
  -n dda-edge-1 \
  -k YOUR_KEY_NAME \
  -c auto

# Set permissions on your key pair
chmod 400 ~/.ssh/YOUR_KEY_NAME.pem

# Connect and setup
ssh -i ~/.ssh/YOUR_KEY_NAME.pem ubuntu@<PUBLIC_IP>
scp -r -i ~/.ssh/YOUR_KEY_NAME.pem ./station_install ubuntu@<PUBLIC_IP>:/tmp/
cd /tmp/station_install
sudo ./setup_station.sh us-east-1 dda-edge-1
```

#### Production Deployment (Jetson Devices)

```bash
# Copy station_install to Jetson device
scp -r ./station_install <jetson-user>@<jetson-ip>:/tmp/

# SSH and run setup
ssh <jetson-user>@<jetson-ip>
cd /tmp/station_install
sudo ./setup_station.sh <region> <device-name>
```

The setup script installs system dependencies (Java, Python 3.9, Docker, GStreamer), installs Greengrass Core, provisions the device as an IoT Thing, configures IAM roles, and sets up directory structure.

#### Edge Device IAM Permissions

Edge devices need IAM permissions for S3, CloudWatch Logs, cross-account access, IoT Core, and Greengrass. Use the policy in `station_install/edge-device-iam-policy.json`:

```bash
aws iam create-user --user-name dda-edge-device
aws iam put-user-policy \
  --user-name dda-edge-device \
  --policy-name DDAEdgeDevicePolicy \
  --policy-document file://station_install/edge-device-iam-policy.json
aws iam create-access-key --user-name dda-edge-device
```

## Deploy DDA Application to Edge Device

> **Alert**: The DDA application (LocalServer component) must be deployed before any other components or models.

All deployments are managed through the portal:

1. Go to **Deployments** → **Create Deployment**
2. Select your edge device or device group
3. Add `aws.edgeml.dda.LocalServer.<arch>` (arm64 or x86_64)
4. Click **Deploy**
5. Verify on **Devices** page → select device → **Components** tab

## Deployments

Deployments follow a two-step process, all managed through the portal:

### Step 1: Deploy DDA LocalServer (Infrastructure)

1. **Deployments** → **Create Deployment** → Add `aws.edgeml.dda.LocalServer.<arch>` → **Deploy**

### Step 2: Deploy Trained ML Models

1. **Deployments** → **Create Deployment** → Add your compiled model component
2. Optionally enable **Inference Uploader** for S3 sync
3. **Deploy**

### Deployment Options

- **Specific Devices**: Deploy to individual devices (recommended for testing)
- **Thing Groups**: Deploy to all devices in an IoT thing group (recommended for production)
- **Auto-Included**: `aws.greengrass.Nucleus` and `aws.greengrass.LogManager` are automatically included

### Monitoring Deployments

Go to **Deployments** page → select deployment to see status (InProgress, Succeeded, Failed, Cancelled), target, components, and per-device status.

## Devices

### Device Management (via Portal)

- **Devices** page shows all registered devices with status, platform, architecture, last seen
- Click a device for: Overview, Components, Deployments, Logs, Diagnostics
- Logs are sent to CloudWatch and viewable in the portal
- Diagnostics show connectivity, Greengrass status, component health, storage, memory

## Using the Portal

### Simplified Workflow

```
1. Labeling     → Create labeling job → Complete in Ground Truth UI
2. Transform    → Click "Transform Manifest" (auto-fills URIs)
3. Training     → Select job (shows ✓ Transformed) → Start training
4. Compilation  → Select target architecture (ARM64/x86)
5. Deployment   → Create deployment → Optional: Inference Uploader
```

### Data Management

- Upload and organize training images to S3
- Browse datasets with image counts
- Register pre-labeled datasets for quick testing

### Labeling

- Create Ground Truth labeling jobs (classification & segmentation)
- Status auto-syncs from SageMaker
- Supports pre-drawn masks for segmentation guidance

> **Important**: Labeling work is completed in the Ground Truth UI, not the DDA Portal.

### Manifest Transformation

Ground Truth creates manifests with job-specific attribute names. The portal auto-transforms them to DDA format during training job creation:

- `{job-name}` → `anomaly-label`
- `{job-name}-metadata` → `anomaly-label-metadata`
- `{job-name}-ref` → `anomaly-mask-ref` (segmentation)

### Training

- Select Ground Truth job or pre-labeled dataset
- Auto-detect and transform manifests
- Configure model type (Classification, Segmentation, Robust variants)
- Instance types: ml.g4dn.2xlarge (GPU), ml.p3.2xlarge (GPU), ml.m5.xlarge (CPU)
- Optional auto-compile after training

### Compilation

- Compile trained models for edge deployment
- Targets: x86_64 CPU, ARM64, Jetson Xavier
- Compiled models are packaged as Greengrass components

## ML Workflow

### Path A: Ground Truth Labeling (Recommended)

1. **Create Labeling Job** → Upload images, configure task type
2. **Complete Labeling** → Workers label in Ground Truth UI
3. **Create Training Job** → Transform manifest → Start training
4. **Compile Model** → Select target architecture
5. **Deploy to Device** → Via portal Deployments page

### Path B: Pre-Labeled Datasets (Quick Start)

1. **Register Dataset** → Data Management → Pre-Labeled Datasets
2. **Create Training Job** → Select dataset → Start training
3. **Compile Model** → Select target architecture
4. **Deploy to Device** → Via portal Deployments page

## Optional Datasets (Before Model Training)

Pre-labeled datasets let you train models without creating labeling jobs. Register them in **Data Management** → **Pre-Labeled Datasets**.

For detailed instructions on Cookie and Alien sample datasets, see [datasets/README.md](./datasets/README.md).

## Inference Results Upload (Optional)

The **Inference Uploader** component enables edge devices to automatically upload inference results to S3.

- Automatically provisioned to UseCase accounts during onboarding
- Enable per deployment: add `aws.edgeml.dda.InferenceUploader` component
- Configurable upload interval (10s to daily)
- Results organized as: `s3://bucket/{usecase-id}/{device-id}/{model-id}/YYYY/MM/DD/`

See [INFERENCE_UPLOADER_SETUP.md](INFERENCE_UPLOADER_SETUP.md) for detailed configuration.

## Edge Device Management

Edge device provisioning and management is a manual process. The portal handles deployments, but device setup requires SSH access.

### Directory Structure on Device

```
/aws_dda/
├── greengrass/v2/              # Greengrass installation
│   ├── config/
│   ├── logs/
│   └── packages/
├── image-capture/              # Captured images from camera
├── inference-results/          # Inference results
├── em_agent/                   # Edge Manager Agent
└── check-cloudwatch-logging.sh # Diagnostics script
```

### Common Device Operations

```bash
# Check Greengrass status
sudo systemctl status greengrass

# Restart Greengrass
sudo systemctl restart greengrass

# View Greengrass logs
tail -f /aws_dda/greengrass/v2/logs/aws.greengrass.Nucleus.log

# View LocalServer logs
tail -f /aws_dda/greengrass/v2/logs/aws.edgeml.dda.LocalServer.log

# Check deployments
aws greengrassv2 list-effective-deployments \
  --core-device-thing-name your-device-name \
  --region us-east-1
```

### Testing Inference (Without Camera - Folder Mode)

Copy test images to the device and run inference directly:

```bash
# Copy images to device
scp -i "device-key.pem" -r ./test-images/* ubuntu@device-ip:/aws_dda/image-capture/<folder-name>

# Test classification
curl -X POST http://localhost:5000/api/v1/inference \
  -F "image=@/aws_dda/image-capture/test-image.jpg" \
  -F "model_name=model-cookie-class"

# Test segmentation
curl -X POST http://localhost:5000/api/v1/inference \
  -F "image=@/aws_dda/image-capture/test-image.jpg" \
  -F "model_name=model-cookie-segmentation"
```

**Batch test all images:**

```bash
for image in /aws_dda/image-capture/*.jpg; do
  echo "Testing: $(basename $image)"
  curl -s -X POST http://localhost:5000/api/v1/inference \
    -F "image=@$image" \
    -F "model_name=model-cookie-class" | python3 -m json.tool
done
```

### Device Health Monitoring

```bash
# Check disk space
df -h /aws_dda/

# Check memory
free -h

# Check network connectivity
aws sts get-caller-identity

# CloudWatch diagnostics
/aws_dda/check-cloudwatch-logging.sh us-east-1

# Search logs for errors
grep -i "error\|failed\|exception" /aws_dda/greengrass/v2/logs/*.log
```

## Troubleshooting

### Portal Deployment Issues

**CDK Bootstrap Error** (`SSM parameter /cdk-bootstrap/hnb659fds/version not found`):
```bash
cdk bootstrap aws://YOUR_ACCOUNT_ID/YOUR_REGION
```

**CloudWatch Logs Error** (`CloudWatch Logs role ARN must be set`):
Follow the API Gateway CloudWatch setup in Step 1.1.

**Frontend Not Accessible** (S3 "NoSuchKey" errors):
```bash
./deploy-frontend.sh  # Re-deploy frontend
```

### Edge Device Issues

**Device Provisioning Fails** (`DDAPortalComponentAccessPolicy does not exist`):
Create a UseCase in the portal first — it provisions this policy.

**Greengrass Won't Start:**
```bash
tail -f /aws_dda/greengrass/v2/logs/aws.greengrass.Nucleus.log
sudo systemctl restart greengrass
```

**S3 Access Denied** (`S3_HEAD_OBJECT_ACCESS_DENIED`):
Verify device role has `DDAPortalComponentAccessPolicy` attached:
```bash
aws iam list-attached-role-policies --role-name GreengrassV2TokenExchangeRole
```

**Deployment Fails** (`COMPONENT_VERSION_REQUIREMENTS_NOT_MET`):
- Verify component exists in your account
- Check architecture matches (arm64 vs x86_64)

**Inference Endpoint Not Responding:**
```bash
ps aux | grep LocalServer
tail -f /aws_dda/greengrass/v2/logs/aws.edgeml.dda.LocalServer.log
netstat -tlnp | grep 5000
```

**Docker Permission Errors:**
```bash
sudo usermod -aG docker ggc_user
sudo systemctl restart greengrass
```

**Frontend Not Accessible on Device** (port 3000):
```bash
docker ps | grep frontend
# Use SSH tunnel: ssh -i "key.pem" -L 3000:localhost:3000 ubuntu@device-ip
```

**GStreamer Pipeline Issues:**
```bash
export GST_DEBUG=3
v4l2-ctl --list-devices
gst-launch-1.0 v4l2src device=/dev/video0 ! autovideosink
```

### CloudWatch LogManager Issues

The portal auto-includes `aws.greengrass.LogManager` in deployments to upload device logs to CloudWatch. Logs are organized as:

```
CloudWatch Log Groups:
  /aws/greengrass/GreengrassSystemComponent/{region}/System     # Greengrass system logs
  /aws/greengrass/UserComponent/{region}/{component-name}       # Per-component logs

Log Streams (per device):
  /YYYY/MM/DD/thing/{thing-name}
```

**"Components detected but no logs yet"** in the portal:
- LogManager uploads every 5 minutes — wait for the first upload cycle
- Verify LogManager is installed: `ls /aws_dda/greengrass/v2/logs/aws.greengrass.LogManager.log`
- If no LogManager log file exists, redeploy from the portal (LogManager is auto-included)

**LogManager installed but no CloudWatch log groups:**
```bash
# Check LogManager config has uploadToCloudWatch enabled
grep -A 80 "aws.greengrass.LogManager" /aws_dda/greengrass/v2/config/effectiveConfig.yaml

# Check LogManager logs for errors
tail -30 /aws_dda/greengrass/v2/logs/aws.greengrass.LogManager.log

# Verify device role has CloudWatch Logs permissions
aws iam list-attached-role-policies --role-name GreengrassV2TokenExchangeRole
```

**LogManager running but portal still shows no logs:**
- The portal searches for log streams matching `/thing/{device-id}` — verify your device's thing name matches
- Check CloudWatch directly: `aws logs describe-log-groups --log-group-name-prefix "/aws/greengrass" --region us-east-1`
- Redeploy from the portal to ensure LogManager config includes all component names

### Training Issues

**"Caller is not subscribed to the marketplace offering"**: Subscribe to the DDA algorithm in the **UseCase Account** (not Portal Account):
1. Sign in to the UseCase Account console
2. Go to [AWS Marketplace - DDA Algorithm](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6)
3. Click **Continue to Subscribe** → **Accept Terms**
4. Wait a few minutes for activation, then retry

**"Manifest validation failed"**: Transform the manifest first using the portal's Transform Manifest button.

**"MaxRuntimeExceeded"**: Increase Max Runtime (recommend 14400-21600s for production datasets).

**Labeling job creation fails** (`UI template can't be accessed`):
```bash
cd edge-cv-portal
./deploy-account-role.sh  # Select Option 1 to update trust policy
```

## Project Structure

```
defect-detection-application/
├── edge-cv-portal/              # Portal application
│   ├── infrastructure/          # AWS CDK stacks
│   ├── backend/                 # Lambda functions (Python)
│   │   ├── functions/           # Lambda handlers
│   │   └── layers/shared/       # Shared utilities
│   ├── frontend/                # React app (TypeScript)
│   │   ├── src/pages/           # Page components
│   │   ├── src/components/      # Shared components
│   │   └── src/services/        # API services
│   ├── deploy-account-role.sh   # Account setup
│   ├── launch-arm64-build-server.sh  # Build server launcher
│   └── deploy-frontend.sh       # Frontend deployment
├── src/                         # DDA edge application
│   ├── backend/                 # Python Flask backend
│   ├── frontend/                # React web interface
│   └── edgemlsdk/               # ML inference SDK
├── station_install/             # Edge device installation
│   ├── setup_station.sh         # Device provisioning script
│   ├── launch-edge-device.sh    # EC2 edge device launcher
│   └── edge-device-iam-policy.json
├── datasets/                    # Sample datasets
├── build-custom.sh              # Custom build logic
├── gdk-component-build-and-publish.sh
└── setup-build-server.sh
```

## Documentation

| Document | Description |
|----------|-------------|
| [edge-cv-portal/ADMIN_GUIDE.md](edge-cv-portal/ADMIN_GUIDE.md) | Portal administration guide |
| [edge-cv-portal/SHARED_COMPONENTS.md](edge-cv-portal/SHARED_COMPONENTS.md) | Shared Greengrass components |
| [INFERENCE_UPLOADER_SETUP.md](INFERENCE_UPLOADER_SETUP.md) | Inference Uploader setup |
| [datasets/README.md](datasets/README.md) | Sample datasets guide |

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

This project adheres to the [Amazon Open Source Code of Conduct](CODE_OF_CONDUCT.md).

## License

This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.

## Support

- **GitHub Issues**: Report bugs and feature requests via [GitHub Issues](https://github.com/aws-samples/defect-detection-application/issues)
- **Discussions**: Join the community discussions for questions and support
