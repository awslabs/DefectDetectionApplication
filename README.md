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
  - [Step 4: Deploy UseCase Account and Create UseCase](#step-4-deploy-usecase-account-and-create-usecase)
  - [Step 5: Build DDA Application](#step-5-build-dda-application-build-server)
  - [Step 6: Setting Up Edge Servers](#step-6-setting-up-edge-servers)
- [Features](#features)
- [ML Workflow](#ml-workflow)
- [Optional Datasets](#optional-datasets-before-model-training)
- [Inference Results Upload](#inference-results-upload-optional)
- [Launching Edge Devices](#launching-edge-devices)
- [Documentation](#documentation)
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

## Deployment

### Architecture Overview

DDA supports both single-account and multi-account architectures to fit different organizational needs.

#### Single-Account Architecture (Recommended for Getting Started)

All components run in a single AWS account. Simplest to set up and manage:

```
Single AWS Account
┌────────────────────────────────────────┐
│ Portal + UseCase + Data (Optional)     │
│                                        │
│ ┌──────────────┐  ┌──────────────┐   │
│ │ Portal       │  │ SageMaker    │   │
│ │ - CloudFront │  │ - Training   │   │
│ │ - API Gateway│  │ - Compilation│   │
│ │ - Cognito    │  │ - Greengrass │   │
│ │ - DynamoDB   │  │ - S3 Buckets │   │
│ └──────────────┘  └──────────────┘   │
└────────────────────────────────────────┘
```

**Setup**: Use `arn:aws:iam::YOUR_ACCOUNT_ID:root` as Role ARN when creating UseCase

#### Multi-Account Architecture (Recommended for Production)

Separates Portal, UseCase, and Data accounts for better security and governance:

```
Portal Account          UseCase Account         Data Account (Optional)
┌──────────────┐       ┌──────────────┐        ┌──────────────┐
│ CloudFront   │       │ SageMaker    │        │ S3 Buckets   │
│ API Gateway  │──────▶│ Greengrass   │◀──────▶│ (Training    │
│ Cognito      │ STS   │ IoT Core     │        │  Data)       │
│ DynamoDB     │       │ S3 Buckets   │        │              │
└──────────────┘       └──────────────┘        └──────────────┘
```

**Setup**: Run `deploy-account-role.sh` in UseCase and Data accounts to create cross-account roles

#### Architecture Components

**Portal Account** - Central management hub
- Hosts the DDA Portal web interface
- Manages users, authentication, and RBAC
- Orchestrates training and compilation jobs
- Stores portal configuration and audit logs

**UseCase Account** - ML workflow execution
- Runs SageMaker training jobs
- Compiles models for edge deployment
- Manages Greengrass components
- Stores trained models and compiled artifacts
- Can be the same as Portal Account for single-account setups

**Data Account** (Optional) - Centralized data storage
- Stores training datasets
- Stores inference results from edge devices
- Separate from UseCase Account for data governance
- Optional - data can be stored in UseCase Account instead

### Prerequisites

- AWS CLI configured
- Node.js 18+, Python 3.11+
- AWS CDK: `npm install -g aws-cdk`
- [AWS Marketplace subscription](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6) (in UseCase Account)

### Step 1: Deploy Portal Infrastructure (Portal Account)

#### 1.1 Configure CloudWatch Logs for API Gateway

Before deploying, configure CloudWatch Logs for API Gateway:

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

# 5. Verify
aws apigateway get-account --region us-east-1
```

#### 1.2 Deploy Portal Infrastructure with CDK

Deploy the portal infrastructure to your AWS account:

```bash
cd edge-cv-portal/infrastructure
npm install
cdk bootstrap  # First time only

# Deploy the portal stack
cdk deploy DDAPortalStack --require-approval never
```

**What gets deployed:**
- CloudFront distribution for frontend
- API Gateway for backend APIs
- Cognito user pool for authentication
- DynamoDB tables for portal data
- Lambda functions for backend logic
- S3 buckets for artifacts and data
- IAM roles and policies

**Output**: The CDK deployment will output important values like:
- `PortalApiEndpoint` - API Gateway endpoint URL
- `PortalFrontendUrl` - CloudFront distribution URL
- `CognitoUserPoolId` - Cognito user pool ID
- `CognitoClientId` - Cognito app client ID

Save these values for the next steps.

### Step 2: Build and Deploy Frontend

Build and deploy the React frontend to CloudFront:

```bash
cd edge-cv-portal

# Build the frontend
./deploy-frontend.sh
```

**What this does:**
- Builds React application with production optimizations
- Uploads built files to S3 bucket
- Invalidates CloudFront cache
- Frontend becomes accessible at the CloudFront URL from Step 1

**Verification:**
- Open the CloudFront URL in your browser
- You should see the DDA Portal login page
- Login with the Cognito credentials you created

### Step 3: Post-Deployment Setup

After deploying the portal infrastructure and frontend, complete these setup tasks:

#### 3.1 Create Portal Admin User

Create the initial admin user in Cognito:

```bash
# Get Cognito user pool ID from CDK outputs
USER_POOL_ID="<from CDK output>"
REGION="us-east-1"  # or your region

# Create admin user
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username admin \
  --message-action SUPPRESS \
  --temporary-password TempPassword123! \
  --region $REGION

# Set permanent password
aws cognito-idp admin-set-user-password \
  --user-pool-id $USER_POOL_ID \
  --username admin \
  --password YourSecurePassword123! \
  --permanent \
  --region $REGION
```

#### 3.2 Verify Portal Access

1. Open the portal URL in your browser
2. Login with admin credentials
3. Navigate to **Settings** to verify configuration
4. Check that you can access the main dashboard

### Step 4: Deploy UseCase Account and Create UseCase

Before building the DDA application, you need to set up a UseCase account and create a UseCase in the portal. A UseCase represents a specific defect detection application instance. This step is critical because edge devices need the `DDAPortalComponentAccessPolicy` that gets created when the UseCase account stack is deployed.

#### 4.1 Deploy UseCase Account Role (Multi-Account Only)

For multi-account setups, deploy the UseCase account role to enable cross-account access from the Portal account.

**For Single-Account Setup**: Skip this step and use `arn:aws:iam::YOUR_ACCOUNT_ID:root` as the Role ARN when creating a UseCase.

**For Multi-Account Setup**:

In the **UseCase Account**, run:

```bash
cd edge-cv-portal
./deploy-account-role.sh
```

**What this does:**
- Creates `DDAPortalAccessRole` in the UseCase account
- Grants permissions for SageMaker, Greengrass, IoT Core, and S3
- Outputs the role ARN for use in the portal

**Save the Role ARN** - You'll need this when creating a UseCase in the portal.

#### 4.2 Create UseCase in Portal

1. Go to **UseCases** page in the portal
2. Click **Create UseCase**
3. Fill in the form:
   - **UseCase Name**: Name for your use case (e.g., "Cookie Defect Detection")
   - **Description**: Brief description
   - **Role ARN**: 
     - Single-account: `arn:aws:iam::YOUR_ACCOUNT_ID:root`
     - Multi-account: Role ARN from Step 4.1
   - **Data Account Role ARN** (optional):
     - If using separate Data account: Role ARN from Step 6
     - Otherwise: Leave blank
4. Click **Create**

**What this does:**
- Creates a new UseCase in the portal
- Provisions Greengrass components for the UseCase
- Creates `DDAPortalComponentAccessPolicy` (required by edge devices)
- Sets up S3 buckets for training data and models
- Enables the UseCase for training and deployment workflows
- Automatically shares portal components (LocalServer, InferenceUploader) with the UseCase account

**Shared Components:**

When you create a UseCase, the portal automatically shares essential Greengrass components with the UseCase account:
- **LocalServer** - Core DDA inference component
- **InferenceUploader** - Optional component for uploading inference results to S3

These shared components are available in the UseCase account's Greengrass component repository and can be deployed to edge devices without additional setup.

**Important**: The UseCase creation step provisions the `DDAPortalComponentAccessPolicy` that edge devices require. Without this, devices will fail with "DDAPortalComponentAccessPolicy not found" error during provisioning.

### Step 5: Build DDA Application (Build Server)

The DDA application is the core Greengrass component that runs inference on edge devices. Before deploying to devices, you must build and publish this component for your target architecture (ARM64 or x86-64).

#### 5.1 Prerequisites

Before launching a build server, you must create the required IAM role:

```bash
./create-build-server-iam-role.sh
```

This creates:
- IAM role: `dda-build-role`
- Instance profile: `dda-build-role`
- Permissions for Greengrass, IoT, S3, CloudWatch, and ECR

#### 5.2 Automated Setup (Recommended)

The fastest way to get a build server running:

```bash
# Step 1: Create IAM role (one-time setup)
./create-build-server-iam-role.sh

# Step 2: Launch the EC2 instance
./launch-arm64-build-server.sh --key-name YOUR_KEY_NAME

# Step 3: Connect and setup
ssh -i ~/.ssh/YOUR_KEY_NAME.pem ubuntu@<PUBLIC_IP>
git clone https://github.com/awslabs/DefectDetectionApplication.git
cd DefectDetectionApplication
./setup-build-server.sh

# Step 4: Build components
./gdk-component-build-and-publish.sh
```

#### 5.3 Build and Publish DDA Application

Once the build server is set up, build and publish the DDA application:

```bash
./gdk-component-build-and-publish.sh
```

**What it does:**
- Detects system architecture (x86_64 or aarch64)
- Selects appropriate recipe file (recipe-amd64.yaml or recipe-arm64.yaml)
- Generates dynamic GDK configuration
- Builds Docker images (backend and frontend)
- Creates component archive
- Publishes to AWS Greengrass component repository

**Features:**
- Animated progress indicators showing current task
- Full logging with timestamps
- VERBOSE mode for debugging: `VERBOSE=1 ./gdk-component-build-and-publish.sh`
- Clear error summary and component name in success message

**Example output:**
```
Building and publishing Greengrass components...
Log file: /tmp/gdk-build-1770652049.log

▶ Detecting system architecture...
✓ Architecture: aarch64 (arm64)
  Component name: aws.edgeml.dda.LocalServer.arm64
  Recipe file: recipe-arm64.yaml

▶ Building component...
  ⠋ Building Docker images...
  ✓ Building Docker images...

▶ Publishing component...
  ⠋ Uploading to AWS...
  ✓ Uploading to AWS...

✅ Component aws.edgeml.dda.LocalServer.arm64 built and published successfully!
```

**Debugging:**
If the build fails, check the log file shown in the output. For more details, run with VERBOSE mode:
```bash
VERBOSE=1 ./gdk-component-build-and-publish.sh
```

> **Notice**: After the component is successfully built and published, stop the EC2 build server instance to avoid incurring unnecessary costs. You can restart it later if you need to rebuild components for a different architecture or make updates.

#### 5.4 Manual Setup (Optional)

If you prefer to set up the IAM role and EC2 instance manually, follow these steps. Otherwise, use the automated setup above.

##### 5.4.1 Create IAM Role

Create a role with the following trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "ec2.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

Attach these policies:

1. **AWS Managed Policy**: `AmazonSSMManagedInstanceCore`

2. **Inline Policy** (create new):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GreengrassPermissions",
      "Effect": "Allow",
      "Action": ["greengrass:*"],
      "Resource": "*"
    },
    {
      "Sid": "IoTPermissions",
      "Effect": "Allow",
      "Action": ["iot:*"],
      "Resource": "*"
    },
    {
      "Sid": "S3Permissions",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:GetBucketLocation",
        "s3:PutBucketVersioning",
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket",
        "s3:DeleteObject",
        "s3:GetBucketVersioning",
        "s3:ListBucketVersions"
      ],
      "Resource": "*"
    },
    {
      "Sid": "EC2Permissions",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeImages",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSubnets",
        "ec2:DescribeVpcs",
        "ec2:DescribeKeyPairs",
        "ec2:DescribeTags"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchLogsPermissions",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    },
    {
      "Sid": "CloudWatchMetricsPermissions",
      "Effect": "Allow",
      "Action": ["cloudwatch:PutMetricData"],
      "Resource": "*"
    },
    {
      "Sid": "ECRPermissions",
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": "*"
    }
  ]
}
```

##### 5.4.2 Create Instance Profile

```bash
# Create instance profile
aws iam create-instance-profile --instance-profile-name dda-build-role

# Add role to profile
aws iam add-role-to-instance-profile \
  --instance-profile-name dda-build-role \
  --role-name dda-build-role
```

##### 5.4.3 Launch EC2 Instance

```bash
# Find latest Ubuntu 18.04 ARM64 AMI
AMI_ID=$(aws ec2 describe-images \
  --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-arm64-server-*" \
            "Name=state,Values=available" \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
  --output text)

# Create security group
SG_ID=$(aws ec2 create-security-group \
  --group-name dda-build-sg \
  --description "Security group for DDA build server" \
  --query 'GroupId' \
  --output text)

# Allow SSH
aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp \
  --port 22 \
  --cidr 0.0.0.0/0

# Launch instance
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id $AMI_ID \
  --instance-type m6g.4xlarge \
  --key-name YOUR_KEY_NAME \
  --security-group-ids $SG_ID \
  --iam-instance-profile Name=dda-build-role \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=100,VolumeType=gp3,DeleteOnTermination=true}' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=dda-arm64-build-server}]' \
  --query 'Instances[0].InstanceId' \
  --output text)

# Wait for instance to be running
aws ec2 wait instance-running --instance-ids $INSTANCE_ID

# Get public IP
PUBLIC_IP=$(aws ec2 describe-instances \
  --instance-ids $INSTANCE_ID \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --output text)

echo "Instance launched: $INSTANCE_ID"
echo "Public IP: $PUBLIC_IP"
```

##### 5.4.4 Connect and Setup

```bash
# Connect to instance
ssh -i ~/.ssh/YOUR_KEY_NAME.pem ubuntu@$PUBLIC_IP

# Clone repository
git clone https://github.com/awslabs/DefectDetectionApplication.git
cd DefectDetectionApplication

# Run setup script
./setup-build-server.sh

# Build components
./gdk-component-build-and-publish.sh
```

#### 5.5 Build Server Specifications

| Aspect | Details |
|--------|---------|
| **Instance Type** | m6g.4xlarge (ARM64) |
| **AMI** | Ubuntu 18.04 ARM64 |
| **Volume Size** | 100 GB (gp3) |
| **Region** | us-east-1 (configurable) |
| **IAM Role** | dda-build-role |
| **SSH Access** | Port 22 (restrict in production) |

### Step 6: Setting Up Edge Servers

After building the DDA application, you need to set up edge servers to run the application. Edge servers are devices configured with AWS IoT Greengrass to run the DDA application and inference workloads.

**Prerequisites:**
- DDA application built and published (from Step 5.3)
- AWS IoT Greengrass v2 installed on edge device
- Device credentials configured

**Account Selection:**
- **Single-Account Setup**: Launch edge servers in the same account as the portal
- **Multi-Account Setup**: Launch edge servers in the **UseCase Account** (the account where you deployed the UseCase Account Role in Step 4.1)

**Testing with EC2 Instances (Development/Testing Only)**

For testing and development, you can launch EC2 instances as edge devices:

```bash
# Launch an edge device
./station_install/launch-edge-device.sh \
  --thing-name dda-edge-1 \
  --key-name YOUR_KEY_NAME

# Connect to the device
ssh -i ~/.ssh/YOUR_KEY_NAME.pem ubuntu@<PUBLIC_IP>

# Copy station_install folder to the device
scp -r -i ~/.ssh/YOUR_KEY_NAME.pem ./station_install ubuntu@<PUBLIC_IP>:/tmp/

# Setup Greengrass and DDA
cd /tmp/station_install
sudo ./setup_station.sh us-east-1 dda-edge-1
```

**Production Deployment with Jetson Devices**

In production environments, you would deploy DDA to NVIDIA Jetson devices (Jetson Nano, Jetson Xavier, etc.) instead of EC2 instances. The setup process is the same:

1. Copy the station_install folder to your Jetson device
2. Set up IAM permissions (see section below)
3. Run the setup script:

```bash
# Copy station_install folder to your Jetson device
scp -r ./station_install <jetson-user>@<jetson-ip>:/tmp/

# SSH into the Jetson device
ssh <jetson-user>@<jetson-ip>

# Run the setup script
cd /tmp/station_install
sudo ./setup_station.sh <region> <device-name>

# Example:
sudo ./setup_station.sh us-east-1 dda-jetson-device-1
```

4. Device will appear in the DDA Portal after setup completes

## Edge Device IAM Permissions

> **Important**: Edge device IAM permissions must be configured before running setup_station.sh. The setup script requires these credentials to access AWS services.

Edge devices need IAM permissions to access S3, CloudWatch Logs, and cross-account resources. Use the policy in `station_install/edge-device-iam-policy.json`:

**What the policy grants:**
- **S3 Access**: Download Greengrass components and upload inference results
- **CloudWatch Logs**: Send component logs to CloudWatch
- **Cross-Account Access**: Assume roles in other AWS accounts (for multi-account setups)
- **IoT Core**: Connect and publish device telemetry
- **Greengrass**: Get deployment status and connectivity info

**Cross-Account Support:**
The policy includes `sts:AssumeRole` permission for `dda-cross-account-role`, which enables:
- Devices in UseCase Account to access S3 buckets in Data Account
- Devices to upload inference results to cross-account S3 buckets
- Multi-account deployments where components and data are in different accounts

**How to apply:**
1. Create an IAM user or role for edge device credentials
2. Attach the policy from `station_install/edge-device-iam-policy.json`
3. Generate access keys for the device
4. Configure device with these credentials during setup

**Example:**
```bash
# Create IAM user for edge devices
aws iam create-user --user-name dda-edge-device

# Attach policy
aws iam put-user-policy \
  --user-name dda-edge-device \
  --policy-name DDAEdgeDevicePolicy \
  --policy-document file://station_install/edge-device-iam-policy.json

# Generate access keys
aws iam create-access-key --user-name dda-edge-device
```

**Configure Device with Credentials:**

When running setup_station.sh, provide the IAM credentials:

```bash
# The setup script will prompt for AWS credentials
# Provide the access key and secret key from the IAM user created above
sudo ./setup_station.sh us-east-1 dda-edge-device-1
```

**What the Setup Does:**
- Installs AWS IoT Greengrass v2
- Configures device certificates and connectivity
- Sets up DDA application runtime environment
- Registers device with AWS IoT Core
- Device appears in DDA Portal after setup completes

**Next Steps:**
Once the edge device is set up and appears in the portal:
1. Go to **Devices** page in portal
2. Verify device is online
3. Deploy the DDA application (see section below)
4. Create deployments to push models and components to the device
5. Monitor inference results in real-time

## Deploy DDA Application to Edge Device

> **Alert**: The DDA application (LocalServer component) must be deployed to edge devices before any other components or models can be deployed. This is a prerequisite for all subsequent deployments.

After your edge device is set up and appears in the portal, you need to deploy the DDA application to enable inference capabilities.

**Deploy DDA Application:**

1. Go to **Deployments** → **Create Deployment**
2. Select your edge device or device group
3. Click **Add Component**
4. Search for and select `aws.edgeml.dda.LocalServer.<arch>` (where `<arch>` is your device architecture: `arm64` or `x86_64`)
5. Click **Deploy**
6. Wait for deployment to complete (status will show "Succeeded")

**Verify Deployment:**

1. Go to **Devices** page
2. Select your device
3. Check **Components** tab to verify LocalServer is deployed and running
4. Check **Logs** tab for any errors

**What Gets Deployed:**

- AWS IoT Greengrass LocalServer component
- DDA inference runtime environment
- Camera and inference pipeline configuration
- Model serving infrastructure

Once the DDA application is deployed, you can proceed with deploying trained models and other components to the device.

## Portal Features

Once you've completed the deployment steps above and logged into the DDA Portal, you'll have access to the following features for managing your defect detection workflows:

| Feature | Description |
|---------|-------------|
| **Data Management** | Browse & upload training images to S3 |
| **Data Accounts** | Centralized management of cross-account data access |
| **Labeling** | Create Ground Truth labeling jobs (classification & segmentation) |
| **Manifest Transformation** | Auto-transform Ground Truth manifests to DDA-compatible format |
| **Training** | SageMaker training with AWS Marketplace algorithm |
| **Compilation** | Compile models for edge (x86-64, ARM64) |
| **Components** | Manage Greengrass components |
| **Deployments** | Deploy models to edge devices with optional Inference Uploader |
| **Inference Uploader** | Automatically upload inference results (images & metadata) from edge devices to S3 |
| **Devices** | Monitor IoT Greengrass devices |
| **Settings** | Portal configuration and Data Accounts management |

## Using the Portal

The DDA Portal provides a complete workflow for managing your defect detection application. You can use the portal for:

- **Data Management** - Upload and organize training datasets
- **Labeling** - Create labeling jobs and manage labeled data
- **Training** - Train models using your labeled datasets
- **Deployment** - Deploy trained models to edge devices

### Data Management

The Data Management section allows you to upload and organize training images for your defect detection models.

**Uploading Training Data:**

1. Go to **Data Management** → **Upload Data**
2. Select images from your local machine or drag and drop
3. Images are uploaded to S3 in your UseCase account
4. Organize images by defect type or production line for easier labeling

**Browsing Datasets:**

1. Go to **Data Management** → **Dataset Browser**
2. View all uploaded datasets
3. See image counts and organization structure
4. Filter by date or custom tags

**Pre-Labeled Datasets:**

For quick testing and prototyping, you can register pre-labeled datasets without creating labeling jobs:

1. Go to **Data Management** → **Pre-Labeled Datasets**
2. Click **Register Dataset**
3. Provide S3 manifest URI and dataset name
4. Use the dataset directly in training jobs

See [Optional Datasets](#optional-datasets-before-model-training) section below for Cookie and Alien dataset setup examples.

## ML Workflow

The standard workflow for training a defect detection model using Amazon SageMaker Ground Truth labeling and Amazon SageMaker:

1. **Create Labeling Job**
   - Go to **Labeling** → **Create Labeling Job**
   - Upload images to S3
   - Configure labeling task (classification or segmentation)
   - Assign to workforce (private or public)

2. **Complete Labeling**
   - Workers label images in Ground Truth
   - Monitor progress in portal
   - Status auto-syncs to "Completed" when done

3. **Transform Manifest** ⚠️ **Required before training**
   - Go to **Labeling** → Select completed job
   - Click "Transform Manifest" button
   - Converts Ground Truth manifest to DDA-compatible format
   - Supports both classification and segmentation tasks
   - Creates DDA-compatible manifest automatically

   **Why Transform is Required:**
   Ground Truth creates manifests with job-specific attribute names that the DDA model cannot recognize. The transformation step converts these to standardized DDA format required for training.

   **What Transform Does:**
   - Renames Ground Truth attributes to DDA standard names:
     - `{job-name}` → `anomaly-label`
     - `{job-name}-metadata` → `anomaly-label-metadata`
     - For segmentation: `{job-name}-ref` → `anomaly-mask-ref`
   - Validates manifest format (JSONL)
   - Preserves all image references and metadata
   - Stores transformed manifest in S3 for training

4. **Create Training Job**
   - Go to **Training** → **Create Training Job**
   - Select transformed Ground Truth job
   - Configure training parameters
   - Click "Start Training"

5. **Compile Model**
   - Go to **Models** → Select trained model
   - Click "Compile"
   - Select target architecture (ARM64 or x86)
   - Wait for compilation to complete

6. **Deploy to Device**
   - Go to **Deployments** → **Create Deployment**
   - Select device or device group
   - Add compiled model component
   - Optionally enable Inference Uploader for S3 sync
   - Deploy

### Quick Start with Pre-Labeled Datasets (Demo/Dev Only)

To skip the labeling phase for testing and development, you can use pre-labeled datasets:

1. **Register Pre-Labeled Dataset**
   - Go to **Data Management** → **Pre-Labeled Datasets**
   - Click **Register Dataset**
   - Provide S3 manifest URI and dataset name
   - See [Optional Datasets](#optional-datasets-before-model-training) section below for Cookie and Alien dataset setup

2. **Create Training Job**
   - Go to **Training** → **Create Training Job**
   - Select **Pre-Labeled Dataset** as source
   - Choose your registered dataset
   - Configure training parameters
   - Click "Start Training"

3. **Compile Model**
   - Go to **Models** → Select trained model
   - Click "Compile"
   - Select target architecture (ARM64 or x86)
   - Wait for compilation to complete

4. **Deploy to Device**
   - Go to **Deployments** → **Create Deployment**
   - Select device or device group
   - Add compiled model component
   - Optionally enable Inference Uploader for S3 sync
   - Deploy

**Note**: Pre-labeled datasets are useful for quick testing and demos. For production models, use the full Ground Truth labeling workflow above to create custom labeled data specific to your manufacturing environment.

## Manifest Transformation

The **Manifest Transformer** converts Ground Truth labeling job outputs to DDA-compatible format. This is required before training with the AWS Marketplace algorithm.

### Why Transform?

Ground Truth creates manifests with job-specific attribute names (e.g., `my-labeling-job`, `my-labeling-job-metadata`). The DDA model expects standardized attribute names (`anomaly-label`, `anomaly-label-metadata`). The transformer automatically renames these attributes.

### Supported Task Types

- **Classification** - Binary or multi-class defect detection
- **Segmentation** - Pixel-level defect localization with masks

### How It Works

**Input Manifest (Ground Truth format):**
```json
{"source-ref": "s3://bucket/image.jpg", "my-job": 1, "my-job-metadata": {"class-name": "defect"}}
```

**Output Manifest (DDA format):**
```json
{"source-ref": "s3://bucket/image.jpg", "anomaly-label": 1, "anomaly-label-metadata": {"class-name": "defect"}}
```

**For Segmentation:**
```json
{
  "source-ref": "s3://bucket/image.jpg",
  "anomaly-label": 1,
  "anomaly-label-metadata": {"class-name": "defect"},
  "anomaly-mask-ref": "s3://bucket/masks/image-mask.png",
  "anomaly-mask-ref-metadata": {"class-name": "defect"}
}
```

### Transformation Process

1. **Detect Attributes** - Automatically identifies Ground Truth attribute names from manifest
2. **Validate Format** - Ensures manifest is valid JSONL (one JSON object per line)
3. **Transform Entries** - Renames attributes to DDA standard names
4. **Preserve Data** - Keeps all image references and metadata intact
5. **Upload Result** - Stores transformed manifest in S3 with `-dda` suffix

### Manifest Validation

Before training, the portal validates manifests for AWS Marketplace model compatibility:

**Required Attributes:**
- `source-ref` (string) - S3 URI of image
- `anomaly-label` (number) - Label value (0 or 1 for binary, 0-N for multi-class)
- `anomaly-label-metadata` (object) - Label metadata

**For Segmentation:**
- `anomaly-mask-ref` (string) - S3 URI of segmentation mask
- `anomaly-mask-ref-metadata` (object) - Mask metadata

**Validation Errors:**
If validation fails, you'll see a helpful error message:
```
Manifest validation failed
Missing required attributes: anomaly-label, anomaly-label-metadata. 
This appears to be a Ground Truth manifest that needs transformation.
Suggestion: Use the Manifest Transformer tool to convert your Ground Truth manifest to DDA-compatible format
```

### Using the Transformer

1. Go to **Labeling** → Select completed job
2. Click **Actions** → **Transform Manifest**
3. Modal opens with transformation options
4. Select task type (Classification or Segmentation)
5. Click **Transform**
6. Transformed manifest is saved to S3
7. Use transformed manifest URI in training job

## Optional Datasets (Before Model Training)

Pre-labeled datasets allow you to train models without creating labeling jobs. This is useful for:
- Testing and prototyping
- Using publicly available datasets (Cookie, Alien)
- Combining multiple datasets
- Quick model evaluation
- Combining multiple datasets for training
- Bootstrapping models with existing labeled data

### Using Pre-Labeled Datasets

The portal includes a **Pre-Labeled Datasets** feature that allows you to:

1. **Browse Available Datasets**
   - Go to **Data Management** → **Pre-Labeled Datasets**
   - View datasets available in your usecase account
   - See dataset statistics (image count, classes, etc.)

2. **Upload Your Own Dataset**
   - Prepare dataset in S3 with manifest file
   - Manifest format: JSONL with image URIs and labels
   - Upload to S3 bucket in your usecase account
   - Register in portal via Pre-Labeled Datasets page

3. **Use Dataset for Training**
   - Go to **Training** → **Create Training Job**
   - Select "Pre-Labeled Dataset" instead of Ground Truth job
   - Choose dataset from dropdown
   - Start training immediately (no labeling required)

### AWS Open Cookie Dataset

The AWS Open Cookie Dataset is a publicly available dataset for defect detection that you can use with DDA:

**Step 1: Clone the AWS Lookout for Vision repository**
```bash
git clone https://github.com/aws-samples/amazon-lookout-for-vision.git
cd amazon-lookout-for-vision
```

**Step 2: Create S3 bucket for dataset**
```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET_NAME="dda-cookie-dataset-${ACCOUNT_ID}"
aws s3 mb s3://${BUCKET_NAME}
```

**Step 3: Upload cookie dataset to S3**
```bash
# Navigate to the datasets directory in the cloned repo
cd datasets/cookies

# Upload to S3
aws s3 sync . s3://${BUCKET_NAME}/cookies/
```

**Step 4: Create manifest file**

Create a manifest file (`manifest.jsonl`) in S3 with entries for each image:

```bash
# Example manifest format (one line per image)
{"source-ref": "s3://dda-cookie-dataset-ACCOUNT_ID/cookies/normal/image1.jpg", "class": "normal"}
{"source-ref": "s3://dda-cookie-dataset-ACCOUNT_ID/cookies/defect/image2.jpg", "class": "defect"}
```

Upload manifest to S3:
```bash
aws s3 cp manifest.jsonl s3://${BUCKET_NAME}/manifest.jsonl
```

**Step 5: Register in Portal**

1. Go to **Data Management** → **Pre-Labeled Datasets**
2. Click **Register Dataset**
3. Fill in:
   - **Dataset Name**: "Cookie Defect Detection"
   - **S3 Manifest URI**: `s3://dda-cookie-dataset-ACCOUNT_ID/manifest.jsonl`
   - **Description**: "AWS Open Cookie Dataset for defect detection"
4. Click **Register**

**Step 6: Train Model with Cookie Dataset**

1. Go to **Training** → **Create Training Job**
2. Select **Pre-Labeled Dataset** as source
3. Choose "Cookie Defect Detection" from dropdown
4. Configure training parameters
5. Click **Start Training**

### AWS Alien Dataset

The AWS Alien Dataset is another publicly available dataset for defect detection featuring toy alien figurines with various defects. This dataset is useful for testing anomaly detection and localization capabilities:

**Step 1: Clone the AWS Lookout for Vision repository**
```bash
git clone https://github.com/aws-samples/amazon-lookout-for-vision.git
cd amazon-lookout-for-vision
```

**Step 2: Create S3 bucket for dataset**
```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET_NAME="dda-alien-dataset-${ACCOUNT_ID}"
aws s3 mb s3://${BUCKET_NAME}
```

**Step 3: Upload alien dataset to S3**
```bash
# Navigate to the datasets directory in the cloned repo
cd datasets/aliens

# Upload to S3
aws s3 sync . s3://${BUCKET_NAME}/aliens/
```

**Step 4: Create manifest file**

Create a manifest file (`manifest.jsonl`) in S3 with entries for each image:

```bash
# Example manifest format (one line per image)
{"source-ref": "s3://dda-alien-dataset-ACCOUNT_ID/aliens/normal/alien1.jpg", "class": "normal"}
{"source-ref": "s3://dda-alien-dataset-ACCOUNT_ID/aliens/defect/alien2.jpg", "class": "defect"}
```

Upload manifest to S3:
```bash
aws s3 cp manifest.jsonl s3://${BUCKET_NAME}/manifest.jsonl
```

**Step 5: Register in Portal**

1. Go to **Data Management** → **Pre-Labeled Datasets**
2. Click **Register Dataset**
3. Fill in:
   - **Dataset Name**: "Alien Defect Detection"
   - **S3 Manifest URI**: `s3://dda-alien-dataset-ACCOUNT_ID/manifest.jsonl`
   - **Description**: "AWS Alien Dataset for anomaly detection and localization"
4. Click **Register**

**Step 6: Train Model with Alien Dataset**

1. Go to **Training** → **Create Training Job**
2. Select **Pre-Labeled Dataset** as source
3. Choose "Alien Defect Detection" from dropdown
4. Configure training parameters
5. Click **Start Training**

### Dataset Format Requirements

Pre-labeled datasets must follow this structure:

**Manifest File (JSONL format):**
```json
{"source-ref": "s3://bucket/path/image1.jpg", "class": "normal"}
{"source-ref": "s3://bucket/path/image2.jpg", "class": "defect"}
```

**For Multi-Class Classification:**
```json
{"source-ref": "s3://bucket/image1.jpg", "class": "class1"}
{"source-ref": "s3://bucket/image2.jpg", "class": "class2"}
{"source-ref": "s3://bucket/image3.jpg", "class": "class3"}
```

**For Segmentation (Bounding Boxes):**
```json
{
  "source-ref": "s3://bucket/image1.jpg",
  "annotations": [
    {"class": "defect", "x": 100, "y": 150, "width": 50, "height": 60}
  ]
}
```

### Benefits of Pre-Labeled Datasets

| Benefit | Description |
|---------|-------------|
| **Speed** | Skip labeling phase, train immediately |
| **Cost** | No Ground Truth labeling costs |
| **Flexibility** | Use any labeled data source |
| **Experimentation** | Quick model prototyping |
| **Combination** | Mix multiple datasets for training |

## Inference Results Upload (Optional)

The **Inference Uploader** component enables edge devices to automatically upload inference results to S3 for centralized storage, analysis, and monitoring.

### What is Inference Uploader?

The Inference Uploader is an optional Greengrass component that:
- Monitors inference results on edge devices (`/aws_dda/inference-results/`)
- Automatically uploads images (.jpg, .png) and metadata (.jsonl) to S3
- Organizes results by usecase, device, and model
- Manages local file retention and cleanup
- Provides CloudWatch logging for monitoring

### Automatic Provisioning

The Inference Uploader component is **automatically provisioned** to all usecase accounts during onboarding:

1. **New Usecases**: Component is automatically shared when you create a usecase in the portal
2. **Existing Usecases**: Component is available for deployment (use "Update All Usecases" button to refresh)

### Deployment

To enable Inference Uploader on a device:

1. Go to **Deployments** → **Create Deployment**
2. Select your device/group
3. Add components:
   - `aws.edgeml.dda.LocalServer.{arch}` (required)
   - `aws.edgeml.dda.InferenceUploader` (optional)
   - Your model component
4. Configure Inference Uploader:
   ```json
   {
     "s3Bucket": "dda-inference-results-{account-id}",
     "s3Prefix": "{usecase-id}/{device-id}",
     "uploadIntervalSeconds": 300,
     "batchSize": 100,
     "localRetentionDays": 7,
     "uploadImages": true,
     "uploadMetadata": true
   }
   ```
5. Deploy


### S3 Structure

Results are organized as:
```
s3://dda-inference-results-{account}/
  └─ {usecase-id}/
     └─ {device-id}/
        └─ {model-id}/
           └─ YYYY/MM/DD/
              ├─ {event-id}.jpg
              └─ {event-id}.jsonl
```

### Monitoring

- **Component Logs**: CloudWatch Logs at `/aws/greengrass/UserComponent/{region}/{device-name}/aws.edgeml.dda.InferenceUploader`
- **Upload Status**: Check S3 bucket for uploaded files
- **Device Logs**: SSH to device and check `/aws_dda/greengrass/v2/logs/aws.edgeml.dda.InferenceUploader.log`

For detailed setup and troubleshooting, see [INFERENCE_UPLOADER_SETUP.md](INFERENCE_UPLOADER_SETUP.md).

## Documentation

| Document | Description |
|----------|-------------|
| [edge-cv-portal/ADMIN_GUIDE.md](edge-cv-portal/ADMIN_GUIDE.md) | Portal administration guide |
| [edge-cv-portal/TUTORIAL_MULTI_ACCOUNT_WORKFLOW.md](edge-cv-portal/TUTORIAL_MULTI_ACCOUNT_WORKFLOW.md) | End-to-end multi-account workflow tutorial |
| [edge-cv-portal/TUTORIAL_SEGMENTATION_MULTI_ACCOUNT.md](edge-cv-portal/TUTORIAL_SEGMENTATION_MULTI_ACCOUNT.md) | Segmentation model training guide |
| [edge-cv-portal/DESIGN_OVERVIEW.md](edge-cv-portal/DESIGN_OVERVIEW.md) | Portal architecture and design overview |
| [edge-cv-portal/SHARED_COMPONENTS.md](edge-cv-portal/SHARED_COMPONENTS.md) | Shared Greengrass components documentation |
| [INFERENCE_UPLOADER_SETUP.md](INFERENCE_UPLOADER_SETUP.md) | Inference Uploader component setup and configuration |

## Troubleshooting

### CDK Bootstrap Error

If you see: `SSM parameter /cdk-bootstrap/hnb659fds/version not found`

**Solution**: Bootstrap the account first:
```bash
cdk bootstrap aws://YOUR_ACCOUNT_ID/YOUR_REGION
```

### CloudWatch Logs Error

If you see: `CloudWatch Logs role ARN must be set in account settings`

**Solution**: Follow the AWS Account Setup section above.

### Role ARN Not Populated

If the config file has empty Role ARN fields:

1. Verify CDK deployment succeeded
2. Check stack outputs: `aws cloudformation describe-stacks --stack-name DDAPortalUseCaseAccountStack --query 'Stacks[0].Outputs'`
3. Re-run the deploy script: `./deploy-account-role.sh`

### Frontend Not Accessible

If you see S3 "NoSuchKey" errors:

1. Verify frontend was deployed: `aws s3 ls s3://dda-portal-frontend-YOUR_ACCOUNT_ID/`
2. Re-run frontend deployment: `./deploy-frontend.sh`
3. Wait for CloudFront cache invalidation (up to 5 minutes)

## Project Structure

```
defect-detection-application/
├── edge-cv-portal/              # Portal application
│   ├── infrastructure/          # AWS CDK stacks
│   ├── backend/                 # Lambda functions
│   ├── frontend/                # React app
│   ├── deploy-account-role.sh   # Account setup
│   ├── deploy-frontend.sh       # Frontend deployment
│   └── README.md                # Portal documentation
├── src/                         # DDA edge application
│   ├── backend/                 # Python Flask backend
│   ├── frontend/                # React web interface
│   └── edgemlsdk/               # ML inference SDK
├── station_install/             # Edge device installation
├── test/                        # Test suites
├── DDA_END_TO_END_ARCHITECTURE.md
├── README_MANUAL_DEPLOYMENT.md
└── README.md                    # This file
```

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

### Development Workflow

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test for new functionality
5. Submit a pull request

### Code of Conduct

This project adheres to the [Amazon Open Source Code of Conduct](CODE_OF_CONDUCT.md).

## License

This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.

## Support


- **GitHub Issues**: Report bugs and feature requests via [GitHub Issues](https://github.com/aws-samples/defect-detection-application/issues)
- **Discussions**: Join the community discussions for questions and support
