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
  - [Step 5: Deploy UseCase Account and Create UseCase](#step-5-deploy-usecase-account-and-create-usecase)
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

DDA supports both single-account and multi-account architectures:

**Single-Account** (Recommended for Getting Started)
- All components in one AWS account
- Simplest setup and management
- Use `arn:aws:iam::YOUR_ACCOUNT_ID:root` as Role ARN when creating UseCase

**Multi-Account** (Recommended for Production)
- Separates Portal, UseCase, and Data accounts
- Better security and governance
- Requires cross-account IAM roles (created by `deploy-account-role.sh`)

#### Architecture Components

**Portal Account** - Central management hub
- DDA Portal web interface
- User authentication and RBAC
- Training and compilation job orchestration
- Portal configuration and audit logs

**UseCase Account** - ML workflow execution
- SageMaker training jobs
- Model compilation for edge deployment
- Greengrass component management
- Trained models and compiled artifacts
- Can be the same as Portal Account for single-account setups

**Data Account** (Optional) - Centralized data storage
- Training datasets
- Inference results from edge devices
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

#### 2.1 Frontend Configuration (config.json)

The frontend requires a configuration file at `edge-cv-portal/frontend/public/config.json` that contains your Cognito and API Gateway details.

**Configuration Required:**

You need to update the configuration file with your Cognito and API Gateway values:

1. Get the required values from CDK outputs:
   ```bash
   # Get Cognito User Pool ID
   aws cognito-idp list-user-pools --max-results 10 --region us-east-1 \
     --query 'UserPools[?Name==`dda-portal-users`].Id' --output text
   
   # Get Cognito Client ID
   aws cognito-idp list-user-pool-clients \
     --user-pool-id <USER_POOL_ID> \
     --region us-east-1 \
     --query 'UserPoolClients[0].ClientId' --output text
   
   # Get API Gateway endpoint
   aws apigateway get-rest-apis --region us-east-1 \
     --query 'items[?name==`DDAPortalAPI`].id' --output text
   ```

2. Edit `edge-cv-portal/frontend/public/config.json`:
   ```json
   {
     "apiUrl": "https://<API_GATEWAY_ID>.execute-api.us-east-1.amazonaws.com/prod",
     "userPoolId": "us-east-1_<USER_POOL_ID>",
     "userPoolClientId": "<CLIENT_ID>",
     "region": "us-east-1"
   }
   ```

**Configuration Fields:**
- `apiUrl` - API Gateway endpoint (without trailing slash)
- `userPoolId` - Cognito user pool ID
- `userPoolClientId` - Cognito app client ID
- `region` - AWS region (default: us-east-1)

#### 2.2 Build and Deploy Frontend

Now build and deploy the React frontend to CloudFront:

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

#### 3.1 Shared Components

When you deploy the portal infrastructure, the following Greengrass components are automatically provisioned and shared with UseCase accounts:

- **LocalServer** - Core DDA inference component that runs on edge devices
- **InferenceUploader** - Optional component for uploading inference results to S3

These shared components are available in the UseCase account's Greengrass component repository and can be deployed to edge devices without additional setup. They are automatically shared when you create a UseCase in the portal.

**Important**: The UseCase creation step also provisions the `DDAPortalComponentAccessPolicy` that edge devices require. Without this policy, devices will fail with "DDAPortalComponentAccessPolicy not found" error during provisioning.

#### 3.2 Create Portal Admin User

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

# Set custom:role attribute (required for API access)
aws cognito-idp admin-update-user-attributes \
  --user-pool-id $USER_POOL_ID \
  --username admin \
  --user-attributes Name=custom:role,Value=PortalAdmin \
  --region $REGION
```

#### 3.3 Verify Portal Access

1. Open the portal URL in your browser
2. Login with admin credentials
3. Navigate to **Settings** to verify configuration
4. Check that you can access the main dashboard

### Step 4: Build DDA Application (Build Server)

The DDA application is the core Greengrass component that runs inference on edge devices. Before deploying to devices, you must build and publish this component for your target architecture (ARM64 or x86-64).

#### 4.1 Prerequisites

The build server requires:
- EC2 instance (Ubuntu 20.04 or later, t3.large or larger recommended)
- AWS CLI configured with credentials
- Docker installed
- Git installed

The IAM role and permissions are automatically created by the build script when you run `gdk-component-build-and-publish.sh`.

#### 4.2 Build and Publish DDA Application

The fastest way to get a build server running and build the DDA application:

```bash
# Step 1: Launch the EC2 instance
./launch-arm64-build-server.sh --key-name YOUR_KEY_NAME

# Step 2: Connect and setup
ssh -i ~/.ssh/YOUR_KEY_NAME.pem ubuntu@<PUBLIC_IP>
git clone https://github.com/awslabs/DefectDetectionApplication.git
cd DefectDetectionApplication
./setup-build-server.sh

# Step 3: Build and publish components
./gdk-component-build-and-publish.sh
```

**Launch script options:**
- `--key-name KEY` (required) - SSH key pair name
- `--subnet-id SUBNET` - Subnet ID for VPC selection (default: uses default VPC)
- `--security-group-id SG` - Security group ID (default: creates new one)
- `--instance-type TYPE` - EC2 instance type (default: m6g.4xlarge)
- `--volume-size SIZE` - Root volume size in GB (default: 100)
- `--region REGION` - AWS region (default: us-east-1)
- `--iam-profile PROFILE` - IAM instance profile name (default: dda-build-role)

**Example with custom VPC:**
```bash
./launch-arm64-build-server.sh --key-name YOUR_KEY_NAME --subnet-id subnet-12345678
```

**Example with pre-created IAM role:**
If your IAM role was created outside the script (e.g., by your infrastructure team):
```bash
./launch-arm64-build-server.sh --key-name YOUR_KEY_NAME --iam-profile my-existing-build-role
```

**Required IAM permissions:**
If creating the IAM role manually, ensure it has these permissions:
- Greengrass: `greengrass:*`
- IoT: `iot:*`
- S3: `s3:*` (or scoped to specific buckets)
- EC2: `ec2:Describe*`
- CloudWatch Logs: `logs:*`
- CloudWatch Metrics: `cloudwatch:PutMetricData`
- ECR: `ecr:GetAuthorizationToken`, `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`

**What the build script does:**
- Creates the required IAM role (`dda-build-role`) if it doesn't exist
- Sets up instance profile with permissions for Greengrass, IoT, S3, CloudWatch, and ECR
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

#### 4.3 Manual Setup (Optional)

<details>
<summary><strong>Click to expand manual setup instructions</strong></summary>

If you prefer to set up the IAM role and EC2 instance manually, follow these steps. Otherwise, use the automated setup above.

##### 4.4.1 Create IAM Role

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

##### 4.4.2 Create Instance Profile

```bash
# Create instance profile
aws iam create-instance-profile --instance-profile-name dda-build-role

# Add role to profile
aws iam add-role-to-instance-profile \
  --instance-profile-name dda-build-role \
  --role-name dda-build-role
```

##### 4.4.3 Launch EC2 Instance

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

##### 4.4.4 Connect and Setup

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

</details>

#### 4.5 Build Server Specifications

| Aspect | Details |
|--------|---------|
| **Instance Type** | m6g.4xlarge (ARM64) |
| **AMI** | Ubuntu 18.04 ARM64 |
| **Volume Size** | 100 GB (gp3) |
| **Region** | us-east-1 (configurable) |
| **IAM Role** | dda-build-role |
| **SSH Access** | Port 22 (restrict in production) |

### Step 5: Create UseCase

A UseCase represents a specific defect detection application instance. Before deploying to edge devices, you need to create a UseCase in the portal.

#### 5.1 Set Up IAM Roles (if needed)

For multi-account setups, you need to create cross-account IAM roles. Run the deployment script:

```bash
cd edge-cv-portal
./deploy-account-role.sh
```

The script presents an interactive menu:
- **Option 1**: Single-account setup (creates `DDASageMakerExecutionRole`)
- **Option 2**: Multi-account setup (creates `DDAPortalAccessRole` in UseCase account)

For multi-account setups, save the Role ARN output - you'll need it in the next step.

> **Note**: Single-account setups can skip this step. The portal auto-detects your account and uses default roles.

#### 5.2 Create UseCase in Portal

1. Go to **UseCases** → **Create UseCase**
2. Fill in the form:
   - **UseCase Name**: Name for your use case (e.g., "Cookie Defect Detection")
   - **Description**: Brief description
   - **S3 Bucket**: S3 bucket for training datasets, models, and results
   - **Role ARN** (multi-account only): Role ARN from Step 5.1
   - **Data Account Role ARN** (optional): For separate data storage account
3. Click **Create**

**What this does:**
- Creates UseCase in the portal
- Provisions Greengrass components
- Creates `DDAPortalComponentAccessPolicy` (required by edge devices)
- Sets up S3 buckets for training data and models

### Step 6: Setting Up Edge Servers

After building the DDA application, set up edge servers to run the application. Edge servers are devices configured with AWS IoT Greengrass to run the DDA application and inference workloads.

**Prerequisites:**
- DDA application built and published (from Step 4)
- AWS IoT Greengrass v2 installed on edge device
- Device credentials configured
- For multi-account setups: edge servers should be in the UseCase account

**Testing with EC2 Instances (Development/Testing Only)**

For testing and development, launch EC2 instances as edge devices:

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

## Labeling Features

The DDA Portal provides comprehensive Ground Truth labeling capabilities for creating training datasets. You can create labeling jobs for classification and segmentation tasks, with automatic manifest generation and transformation.

### Labeling Job Types

The portal supports three labeling task types:

| Task Type | Use Case | Output |
|-----------|----------|--------|
| **Image Classification** | Binary or multi-class defect detection | Class labels (0, 1, 2, ...) |
| **Semantic Segmentation** | Pixel-level defect localization | Segmentation masks with color-coded regions |

### Creating a Labeling Job

**Step 1: Prepare and Upload Images**

Upload images to S3 in your UseCase account. The S3 structure is flexible - organize however makes sense for your data:

```bash
# Create S3 bucket for images
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="dda-labeling-images-${ACCOUNT_ID}"
aws s3 mb s3://${BUCKET}

# Upload images to your preferred folder structure
# Examples:
aws s3 sync ./my-images/ s3://${BUCKET}/images/
# or
aws s3 sync ./my-images/ s3://${BUCKET}/project-1/training-images/
# or
aws s3 sync ./my-images/ s3://${BUCKET}/cookies/dataset-files/training-images/
```

**Step 2: Create Labeling Job in Portal**

1. Go to **Labeling** → **Create Labeling Job**
2. Fill in job details:
   - **Job Name**: Name for your labeling job (e.g., "Cookie Defects Q1")
   - **S3 URI**: S3 prefix pointing to your image folder
     - This should be the folder containing your image files (not parent directories)
     - Examples: `s3://bucket/images/`, `s3://bucket/project-1/training-images/`, `s3://bucket/cookies/dataset-files/training-images/`
     - The portal will list all images in this prefix
   - **Task Type**: Select Classification or Segmentation
   - **Label Categories**: Comma-separated list of classes (e.g., "normal, cracked, dented")
   - **Workforce**: Select Private (your team) or Public (Amazon Mechanical Turk)
   - **Instructions**: Detailed labeling instructions for workers
3. Click **Create**

**What Happens:**
- Portal lists all images in the S3 prefix
- Automatically generates Ground Truth manifest (JSONL format)
- Creates Ground Truth labeling job
- Workers can start labeling immediately

> **Important**: Creating a labeling job in the DDA Portal only creates an entry in AWS SageMaker Ground Truth. You must complete the actual labeling work through the **Ground Truth UI** (not the DDA Portal). Workers will label images in Ground Truth, and once labeling is complete, the manifest will be available for training in the DDA Portal.

**S3 URI Tips:**

- **Point to the image folder**: The S3 URI should be the folder containing your actual image files
- **Avoid parent directories**: Don't point to a parent folder that contains multiple subfolders
- **Use trailing slash**: Include the trailing `/` in your S3 URI (e.g., `s3://bucket/images/`)
- **Flat or nested**: Your images can be directly in the folder or in subfolders - the portal will find them recursively

### Segmentation with Masks

For semantic segmentation tasks, you can provide pre-drawn masks to guide workers or use them as reference:

**Creating Segmentation Job with Masks:**

1. Go to **Labeling** → **Create Labeling Job**
2. Select **Semantic Segmentation** as task type
3. Set **Dataset Prefix**: S3 prefix containing your training images
4. Set **Mask Prefix**: S3 prefix containing your mask images (optional)
   - Masks should be in a separate folder from images
   - Filenames must match image filenames (e.g., `image-1.jpg` → `image-1.png`)
5. Configure label categories and workforce
6. Click **Create**

**Mask Format:**

Masks are PNG files with pixel colors representing classes:
- `#23A436` (green) - Defect/anomaly region
- `#FFFFFF` (white) - Background/normal region
- Filenames must match image filenames (e.g., `image-1.jpg` → `image-1.png`)

**Example Mask Creation (Python):**

```python
from PIL import Image
import numpy as np

# Create mask image (same size as original image)
width, height = 640, 480
mask = Image.new('RGB', (width, height), color='white')  # White background

# Draw defect region in green
pixels = mask.load()
for x in range(100, 200):
    for y in range(150, 250):
        pixels[x, y] = (35, 164, 54)  # Green (#23A436)

# Save mask
mask.save('image-1.png')
```

### Monitoring Labeling Progress

1. Go to **Labeling** → **Labeling Jobs**
2. Select your job to see:
   - **Status**: InProgress, Completed, Failed, Stopped
   - **Progress**: Percentage of images labeled
   - **Labeled Count**: Number of images completed
   - **Created By**: User who created the job
   - **Created At**: Job creation timestamp

**Status Meanings:**
- **InProgress**: Workers are actively labeling
- **Completed**: All images labeled and consolidated
- **Failed**: Job encountered an error
- **Stopped**: Job was manually stopped

### Manifest Transformation

Ground Truth creates manifests with job-specific attribute names that need to be transformed to DDA-compatible format before training. The portal automatically detects and transforms these manifests during the training job creation process.

**Why Transform?**

Ground Truth creates manifests with job-specific attribute names (e.g., `my-labeling-job`, `my-labeling-job-metadata`). The DDA model expects standardized names (`anomaly-label`, `anomaly-label-metadata`). The transformer automatically converts these.

**Automatic Transformation in Training Workflow:**

1. Go to **Training** → **Create Training Job**
2. Select your labeling job or pre-labeled dataset
3. If the manifest is in Ground Truth format, a warning alert appears
4. Click **Transform Manifest Now** button
5. Portal converts to DDA format automatically
6. Transformed manifest is used for training
7. Transformed manifest is saved to S3 with `-dda` suffix

**What Gets Transformed:**

**Classification Input (Ground Truth):**
```json
{
  "source-ref": "s3://bucket/image.jpg",
  "my-job": 1,
  "my-job-metadata": {"class-name": "defect"}
}
```

**Classification Output (DDA):**
```json
{
  "source-ref": "s3://bucket/image.jpg",
  "anomaly-label": 1,
  "anomaly-label-metadata": {"class-name": "defect"}
}
```

**Segmentation Input (Ground Truth):**
```json
{
  "source-ref": "s3://bucket/image.jpg",
  "my-job": 1,
  "my-job-metadata": {"class-name": "defect"},
  "my-job-ref": "s3://bucket/masks/image.png",
  "my-job-ref-metadata": {"class-name": "defect"}
}
```

**Segmentation Output (DDA):**
```json
{
  "source-ref": "s3://bucket/image.jpg",
  "anomaly-label": 1,
  "anomaly-label-metadata": {"class-name": "defect"},
  "anomaly-mask-ref": "s3://bucket/masks/image.png",
  "anomaly-mask-ref-metadata": {"class-name": "defect"}
}
```

### Using Labeled Data for Training

After transformation, use the labeled data to train models:

1. Go to **Training** → **Create Training Job**
2. Select **Ground Truth Labeling Job** as source
3. Choose your transformed labeling job
4. Configure training parameters
5. Click **Start Training**

### Labeling Best Practices

**Image Quality:**
- Use consistent lighting and camera angles
- Ensure images are clear and in focus
- Include diverse examples of defects
- Aim for 50-100 images per defect type minimum

**Label Consistency:**
- Provide clear, detailed labeling instructions
- Use consistent terminology
- Include example images in instructions
- Consider using multiple workers per image for consensus

**Defect Coverage:**
- Include normal/good examples (important for model accuracy)
- Capture various defect types and severities
- Include edge cases and borderline examples
- Aim for balanced dataset (similar counts per class)

**Segmentation Masks:**
- Draw masks precisely around defect boundaries
- Use consistent color coding across all masks
- Include masks for all defect images
- For normal images, use white background only

### Troubleshooting Labeling

**Issue: "No images found in the specified prefix"**
- Verify S3 prefix is correct
- Check that images are in S3 (not local)
- Ensure image format is supported (.jpg, .png, .bmp, .tiff)
- Verify IAM permissions allow S3 access

**Issue: "The UI template located at s3://sagemaker-{region}-{account}/ground-truth-labeling-templates/... can't be accessed"**

This error occurs when the `DDASageMakerExecutionRole` doesn't have permission to access AWS's Ground Truth templates. The role needs a trust policy that allows AWS's SageMaker account to assume it.

**Solution:**
1. Re-run the deployment script to update the role with the correct trust policy:
   ```bash
   cd edge-cv-portal
   ./deploy-account-role.sh
   ```
2. Select **Option 1** (Single Account) when prompted
3. The script will:
   - Detect your AWS region
   - Add the correct SageMaker account ID for your region to the trust policy
   - Update the `DDASageMakerExecutionRole` with the new trust policy
4. Try creating the labeling job again

**Why this happens:**
- Ground Truth templates are stored in AWS-managed S3 buckets in each region
- Each region has a different AWS SageMaker account ID that owns these templates
- The role must trust that account to access the templates
- If the role was created before this fix, it won't have the correct trust policy

**Supported regions and their SageMaker account IDs:**
- us-east-1: 432418664414
- us-west-2: 246618743249
- eu-west-1: 685385470294
- eu-central-1: 492215442770
- ap-northeast-1: 501404014126
- ap-southeast-1: 114774131450
- ap-southeast-2: 783357319266

**Issue: "Labeling job creation failed"**
- Check that workteam exists in SageMaker Ground Truth
- Verify workforce ARN is correct
- Ensure S3 bucket has proper permissions
- Check CloudWatch Logs for detailed error

**Issue: "Manifest transformation failed"**
- Verify labeling job is completed
- Check that manifest file exists in S3
- Ensure manifest is valid JSONL format
- Try transforming again (may be temporary issue)

**Issue: "Cannot train with labeled data"**
- Verify manifest was transformed (not original Ground Truth manifest)
- Check that transformed manifest URI is correct
- Ensure all images referenced in manifest exist in S3
- Verify label values are numeric (0, 1, 2, etc.)

## ML Workflow

The DDA Portal supports two paths for training models:

**Path A: Ground Truth Labeling** (Recommended for production) - Create custom labeled datasets using Amazon SageMaker Ground Truth
**Path B: Pre-Labeled Datasets** (Quick start for testing) - Use existing labeled data without creating labeling jobs

### Path A: Ground Truth Labeling Workflow

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

3. **Create Training Job** ⚠️ **Manifest transformation happens here**
   - Go to **Training** → **Create Training Job**
   - Select your completed labeling job
   - Portal automatically detects manifest format
   - If Ground Truth format is detected, a warning alert appears
   - Click "Transform Manifest Now" button to convert to DDA format
   - Configure training parameters (model type, instance type, runtime)
   - Click "Start Training"

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

4. **Monitor Training**
   - Go to **Training** → Select your training job
   - Monitor progress and logs
   - Wait for training to complete

5. **Compile Model**
   - Go to **Models** → Select trained model
   - Click "Compile"
   - Select target architecture (ARM64 or x86)
   - Wait for compilation to complete

6. **Deploy to Device**
   - Go to **Deployments** → **Create Deployment**
   - Select device or device group
   - Add compiled model component
   - Monitor deployment status
   - Optionally enable Inference Uploader for S3 sync
   - Deploy

### Path B: Pre-Labeled Datasets (Quick Start)

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

**Note**: Pre-labeled datasets are useful for quick testing, prototyping, and demos. For production models, use **Path A: Ground Truth Labeling** to create custom labeled data specific to your manufacturing environment.

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

The manifest transformation happens automatically during the training job creation process:

1. Go to **Training** → **Create Training Job**
2. Select your labeling job or pre-labeled dataset
3. If the manifest is in Ground Truth format, a warning alert appears
4. Click **Transform Manifest Now** button
5. Portal converts to DDA format automatically
6. Transformed manifest is saved to S3 with `-dda` suffix
7. Transformed manifest is automatically used for training

## Training

The DDA Portal provides a streamlined training workflow for creating SageMaker training jobs with your labeled data. The training interface validates manifests, detects format issues, and provides helpful guidance throughout the process.

### Creating a Training Job

**Step 1: Navigate to Training**

1. Go to **Training** → **Create Training Job**
2. You'll see a form with required fields and helpful validation hints

**Step 2: Select Use Case**

- Choose the use case where your training data is stored
- The use case determines which S3 bucket and resources are used

**Step 3: Configure Model**

- **Model Name**: Name for your trained model (e.g., "defect-detector-line1")
  - Can only contain letters, numbers, and hyphens
  - Used to identify the model in deployments
- **Model Version**: Version number for tracking (e.g., "1.0.0")
  - For tracking only, not used in training job name
- **Model Type**: Choose between:
  - **Classification**: Binary or multi-class defect detection
  - **Classification (Robust)**: Enhanced classification model
  - **Segmentation**: Pixel-level defect localization
  - **Segmentation (Robust)**: Enhanced segmentation model

**Step 4: Select Dataset Source**

Choose how to provide your training data:

**Option A: Ground Truth Labeling Job**
- Use output from a completed labeling job
- Portal automatically detects manifest format
- If Ground Truth format is detected, you can transform it on-demand
- Supports both classification and segmentation tasks

**Option B: Pre-Labeled Dataset**
- Use existing labeled data registered in the portal
- No transformation needed (must already be in DDA format)
- Useful for quick testing or using public datasets

### Manifest Format Detection and Transformation

The portal automatically detects your manifest format and helps you fix issues before training:

**Automatic Detection:**
- When you select a dataset, the portal checks the manifest format
- Detects Ground Truth format (job-specific attribute names)
- Detects DDA format (standardized attribute names)

**Ground Truth Format Detected:**
If your manifest uses Ground Truth format (e.g., `my-job`, `my-job-metadata`):
1. A warning alert appears
2. Click **Transform Manifest Now** button
3. Portal converts to DDA format automatically
4. Transformed manifest is used for training

**Why Transform?**
- Ground Truth creates manifests with job-specific attribute names
- AWS Marketplace model requires standardized DDA attribute names
- Transformation renames attributes automatically:
  - `{job-name}` → `anomaly-label`
  - `{job-name}-metadata` → `anomaly-label-metadata`
  - For segmentation: `{job-name}-ref` → `anomaly-mask-ref`

**DDA Format Ready:**
If your manifest is already in DDA format:
- Green success alert appears
- No transformation needed
- Ready to proceed with training

### Form Validation

The portal validates your form before allowing training to start:

**Required Fields:**
- Use Case
- Model Name
- Model Version
- Dataset selection (Ground Truth job or Pre-Labeled dataset)
- Model Type
- Instance Type

**Validation Hints:**
- Missing required fields are listed in a warning alert at the top
- Each field shows inline error messages if invalid
- "Start Training" button is disabled until all fields are valid
- Helpful error messages guide you to fix issues

**Model Name Validation:**
- Can only contain letters, numbers, and hyphens
- Inline error appears if invalid characters are used
- Example valid names: `defect-detector`, `model-v1`, `line1-classifier`

### Compute Configuration

**Instance Type:**
- **ml.g4dn.2xlarge** (GPU - Recommended): Fastest training, higher cost
- **ml.p3.2xlarge** (GPU - High Performance): Very fast training, highest cost
- **ml.m5.xlarge** (CPU - Budget): Slower training, lowest cost

**Max Runtime:**
- Maximum training time in seconds
- Default: 3600 seconds (1 hour) for classification
- Default: 7200 seconds (2 hours) for segmentation
- Typical training takes 2-4 hours depending on dataset size
- If training fails with "MaxRuntimeExceeded", increase this value
- Recommended: 14400-21600 seconds (4-6 hours) for production datasets

### Post-Training Options

**Auto-Compile:**
- Enable to automatically compile model after training completes
- Compilation optimizes model for edge deployment

**Compilation Targets:**
- **x86_64 CPU**: Intel/AMD processors
- **ARM64**: Standard ARM processors
- **Jetson Xavier**: ARM64 with NVIDIA GPU

Select which platforms you want to deploy to. Compiled models are automatically packaged as Greengrass components.

### Training Summary

Before submitting, review the summary showing:
- Model name and version
- Model type
- Instance type
- Dataset source and selection
- Training and output buckets
- Compilation targets (if enabled)

### Starting Training

1. Verify all required fields are filled
2. Review the summary
3. Click **Start Training**
4. Portal creates SageMaker training job
5. Redirects to Training page to monitor progress

### Monitoring Training

1. Go to **Training** page
2. Select your training job
3. View status and progress:
   - **InProgress**: Training is running
   - **Completed**: Training finished successfully
   - **Failed**: Training encountered an error
   - **Stopped**: Training was manually stopped

### Training Best Practices

**Dataset Size:**
- Minimum: 50-100 images per class
- Recommended: 200-500 images per class
- Larger datasets generally produce better models

**Class Balance:**
- Try to have similar number of images per class
- Imbalanced datasets can bias the model
- Include normal/good examples (important for accuracy)

**Image Quality:**
- Use consistent lighting and camera angles
- Ensure images are clear and in focus
- Include diverse examples of defects
- Capture various defect types and severities

**Training Time:**
- Classification: 1-2 hours typical
- Segmentation: 2-4 hours typical
- Larger datasets take longer
- GPU instances are significantly faster than CPU

**Cost Estimation:**
- ml.g4dn.2xlarge: ~$0.35/hour
- ml.p3.2xlarge: ~$3.06/hour
- ml.m5.xlarge: ~$0.096/hour
- Typical training cost: $1-10 depending on instance and duration

### Troubleshooting Training

**Issue: "Start Training" button is greyed out**
- Check the warning alert for missing required fields
- Ensure all fields are filled correctly
- Verify dataset selection is valid

**Issue: "Manifest validation failed"**
- Check that manifest is in DDA format
- If Ground Truth format, use Transform Manifest button
- Verify all images referenced in manifest exist in S3
- Ensure label values are numeric (0, 1, 2, etc.)

**Issue: Training job fails with "MaxRuntimeExceeded"**
- Increase Max Runtime value
- Try a faster instance type (GPU instead of CPU)
- Reduce dataset size for testing

**Issue: Training job fails with permission errors**
- Verify IAM role has S3 access to training data bucket
- Check that role has SageMaker permissions
- Verify bucket is tagged with `dda-portal:managed=true`

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

### Registering a Pre-Labeled Dataset

**Step 1: Prepare Your Dataset**

Organize your dataset in S3 with a manifest file in JSONL format:

```
s3://your-bucket/
├── images/
│   ├── normal-1.jpg
│   ├── normal-2.jpg
│   ├── defect-1.jpg
│   └── defect-2.jpg
└── manifest.jsonl
```

**Step 2: Create Manifest File**

The manifest file must be in JSONL format (one JSON object per line). Each line represents one image with its label:

**For Classification:**
```json
{"source-ref": "s3://your-bucket/images/normal-1.jpg", "class": "normal"}
{"source-ref": "s3://your-bucket/images/normal-2.jpg", "class": "normal"}
{"source-ref": "s3://your-bucket/images/defect-1.jpg", "class": "defect"}
{"source-ref": "s3://your-bucket/images/defect-2.jpg", "class": "defect"}
```

**For Segmentation:**
```json
{"source-ref": "s3://your-bucket/images/image-1.jpg", "anomaly-mask-ref": "s3://your-bucket/masks/image-1.png"}
{"source-ref": "s3://your-bucket/images/image-2.jpg", "anomaly-mask-ref": "s3://your-bucket/masks/image-2.png"}
```

**Step 3: Upload to S3**

```bash
# Upload images
aws s3 sync ./images/ s3://your-bucket/images/

# Upload manifest
aws s3 cp manifest.jsonl s3://your-bucket/manifest.jsonl
```

**Step 4: Register Dataset in Portal**

1. Go to **Data Management** → **Pre-Labeled Datasets**
2. Click **Register Dataset**
3. Fill in the form:
   - **Dataset Name**: Name for your dataset (e.g., "Cookie Defects Q1")
   - **Manifest S3 URI**: Full S3 path to manifest file (e.g., `s3://your-bucket/manifest.jsonl`)
   - **Task Type**: Select Classification or Segmentation
   - **Description**: Optional description of the dataset
4. Click **Register**

**Step 5: Use Dataset for Training**

1. Go to **Training** → **Create Training Job**
2. Select **Pre-Labeled Dataset** as the data source
3. Choose your registered dataset from the dropdown
4. Configure training parameters
5. Click **Create Training Job**

### Pre-Labeled Dataset Format Requirements

**Manifest File (JSONL):**
- One JSON object per line
- Each line represents one image
- Required fields:
  - `source-ref`: Full S3 URI to the image
  - `class`: Label for classification tasks
  - `anomaly-mask-ref`: S3 URI to mask for segmentation tasks

**Image Files:**
- Supported formats: JPG, PNG, BMP, TIFF
- Recommended size: 1024x1024 or larger
- Organized in S3 with clear folder structure

**Manifest Location:**
- Must be in S3 bucket accessible by your UseCase account
- Can be in same bucket as images or separate bucket
- Recommended: `s3://bucket/manifest.jsonl` at root level

### AWS Open Cookie Dataset

The AWS Open Cookie Dataset is a publicly available dataset for defect detection that you can use with DDA:

#### Understanding S3 Bucket and S3 Prefix

Before creating your use case, you need to understand how DDA uses S3 storage:

- **S3 Bucket**: The root container in Amazon S3 where all your data will be stored. This includes training datasets, labeled data, trained models, and inference results. The bucket name must be globally unique across AWS.
  - Example: `dda-cookie-dataset`

- **S3 Prefix**: An optional folder path within the bucket to organize your data. Think of it like a folder structure. If you don't specify a prefix, data is stored at the bucket root.
  - Example: `datasets/` stores data in a "datasets" folder
  - Example: `project-1/training-data/` creates nested folders
  - Useful for organizing multiple use cases or projects within a single bucket

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

# Tag bucket as managed by DDA Portal
aws s3api put-bucket-tagging \
  --bucket ${BUCKET_NAME} \
  --tagging 'TagSet=[{Key=ManagedBy,Value=DDAPortal}]' \
  --region us-east-1
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

# Tag bucket as managed by DDA Portal
aws s3api put-bucket-tagging \
  --bucket ${BUCKET_NAME} \
  --tagging 'TagSet=[{Key=ManagedBy,Value=DDAPortal}]' \
  --region us-east-1
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

<details>
<summary><strong>Click to expand Inference Uploader setup</strong></summary>

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

</details>

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
