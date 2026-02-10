# Defect Detection Application (DDA) - Portal Edition

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=flat&logo=docker&logoColor=white)](https://www.docker.com/)

The Defect Detection Application (DDA) is an edge-deployed computer vision solution for quality assurance in discrete manufacturing environments. Originally developed by the AWS EdgeML service team, DDA is now available as an open-source project under the stewardship of the AWS Manufacturing TFC and Auto/Manufacturing IBU.

**This README covers the recommended Portal-based deployment. For manual deployment without the Portal, see [README_MANUAL_DEPLOYMENT.md](README_MANUAL_DEPLOYMENT.md).**

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Deployment](#deployment)
- [Features](#features)
- [Documentation](#documentation)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

## Overview

DDA provides real-time defect detection capabilities for manufacturing quality control using computer vision and machine learning. The system runs at the edge using AWS IoT Greengrass, enabling low-latency inference and reducing dependency on cloud connectivity.

### Key Benefits

- **Real-time Processing**: Sub-second inference times for immediate quality feedback
- **Edge Deployment**: Operates independently of cloud connectivity
- **Scalable Architecture**: Supports multiple camera inputs and production lines
- **ML Model Flexibility**: Compatible with various computer vision models
- **Manufacturing Integration**: RESTful APIs for integration with existing systems
- **Portal Management**: Centralized multi-tenant admin portal for managing all deployments

## Quick Start

### 1. Deploy Portal (Portal Account)

```bash
# Prerequisites
npm install -g aws-cdk
cd edge-cv-portal/infrastructure

# Bootstrap CDK (first time only)
cdk bootstrap

# Deploy infrastructure
cdk deploy --all
```

### 2. Configure Frontend with Deployment Values

After CDK deployment completes, update `config.json` with your actual deployment values:

```bash
cd edge-cv-portal

# Get the API Gateway URL from CDK outputs
API_URL=$(aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalComputeStack \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiUrl`].OutputValue' \
  --output text)

# Get Cognito User Pool details from CDK outputs
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalAuthStack \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' \
  --output text)

USER_POOL_CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalAuthStack \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolClientId`].OutputValue' \
  --output text)

# Update config.json with your values
cat > frontend/public/config.json << EOF
{
  "apiUrl": "$API_URL",
  "userPoolId": "$USER_POOL_ID",
  "userPoolClientId": "$USER_POOL_CLIENT_ID",
  "region": "us-east-1",
  "branding": {
    "applicationName": "Defect Detection Application Portal",
    "companyName": "Amazon Web Services",
    "logoUrl": "/logo.png",
    "faviconUrl": "/favicon.ico",
    "primaryColor": "#0073bb",
    "supportEmail": "support@yourcompany.com",
    "documentationUrl": "https://docs.yourcompany.com"
  }
}
EOF

echo "✓ config.json updated with deployment values"
```

### 3. Build and Deploy Frontend

```bash
./deploy-frontend.sh
```

**Output**: Portal URL (e.g., `https://d1r8hupkjbsjb1.cloudfront.net`)

### 4. Create Admin User

```bash
# Get User Pool ID from deployment outputs
USER_POOL_ID="<your-user-pool-id>"

# Create admin user
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
```

### 5. Deploy UseCase Account Role (UseCase Account)

```bash
# Bootstrap CDK in UseCase Account
cdk bootstrap aws://YOUR_USECASE_ACCOUNT_ID/us-east-1

# Deploy role
cd edge-cv-portal
./deploy-account-role.sh  # Select option 1
```

### 6. Create UseCase in Portal

1. Log in to Portal as admin
2. Go to Settings → UseCases
3. Click "Add UseCase"
4. Fill in Account ID, Role ARN, and External ID from step 4
5. Click "Create"

## Building Greengrass Components (ARM64)

To build and publish custom Greengrass components for ARM64 edge devices, you need an ARM64 build server. This section covers both automated and manual setup options.

### Prerequisites

Before launching a build server, you must create the required IAM role:

```bash
./create-build-server-iam-role.sh
```

This creates:
- IAM role: `dda-build-role`
- Instance profile: `dda-build-role`
- Permissions for Greengrass, IoT, S3, CloudWatch, and ECR

### Option 1: Automated Setup (Recommended)

The fastest way to get a build server running:

```bash
# Step 1: Create IAM role (one-time setup)
./create-build-server-iam-role.sh

# Step 2: Launch the EC2 instance
./launch-arm64-build-server.sh --key-name YOUR_KEY_NAME

# Step 3: Connect and setup
ssh -i ~/.ssh/YOUR_KEY_NAME.pem ubuntu@<PUBLIC_IP>
git clone <your-repo>
cd DefectDetectionApplication
./setup-build-server.sh

# Step 4: Build components
./gdk-component-build-and-publish.sh
```

### Build Server Setup Script

The `setup-build-server.sh` script automates all dependencies:

```bash
./setup-build-server.sh
```

**What it installs:**
- Docker (with daemon startup and permissions)
- Python 3.9 (from source on Ubuntu 18.04, PPA on others)
- AWS CLI and AWS CDK
- AWS Greengrass Development Kit (GDK)
- Node.js and npm

**Features:**
- Comprehensive error handling (errors vs warnings)
- Full logging with timestamps to `/tmp/setup-build-*.log`
- VERBOSE mode for debugging: `VERBOSE=1 ./setup-build-server.sh`
- Clear error summary and next steps

### Building Components

Once the build server is set up, build and publish components:

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

### Option 2: Manual Setup

If you prefer to set up the IAM role and EC2 instance manually, follow these steps. Otherwise, use the automated setup above.

#### Step 1: Create IAM Role

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

#### Step 2: Create Instance Profile

```bash
# Create instance profile
aws iam create-instance-profile --instance-profile-name dda-build-role

# Add role to profile
aws iam add-role-to-instance-profile \
  --instance-profile-name dda-build-role \
  --role-name dda-build-role
```

#### Step 3: Launch EC2 Instance

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

#### Step 4: Connect and Setup

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

### Build Server Specifications

| Aspect | Details |
|--------|---------|
| **Instance Type** | m6g.4xlarge (ARM64) |
| **AMI** | Ubuntu 18.04 ARM64 |
| **Volume Size** | 100 GB (gp3) |
| **Region** | us-east-1 (configurable) |
| **IAM Role** | dda-build-role |
| **SSH Access** | Port 22 (restrict in production) |

### Cleanup

To terminate the build server when done:

```bash
# Using instance ID
aws ec2 terminate-instances --instance-ids $INSTANCE_ID

# Or using tag
aws ec2 terminate-instances \
  --filters "Name=tag:Name,Values=dda-arm64-build-server"
```

## Launching Edge Devices

To deploy DDA to edge devices with AWS Greengrass, you need to launch EC2 instances configured as edge devices. This section covers both automated and manual setup options.

### Prerequisites


### Automated Setup (Recommended)

The fastest way to launch an edge device:

```bash
# Step 1: Launch the EC2 instance
./station_install/launch-edge-device.sh \
  --thing-name dda-edge-1 \
  --key-name YOUR_KEY_NAME \
  --cidr auto

# Step 2: Connect and setup
ssh -i ~/.ssh/YOUR_KEY_NAME.pem ubuntu@<PUBLIC_IP>
cd /tmp
git clone https://github.com/awslabs/DefectDetectionApplication.git dda
cd dda/station_install
sudo ./setup_station.sh us-east-1 dda-edge-1

# Step 3: Device appears in portal
# After setup completes, the device will appear in the DDA Portal
```

### Launch Options

```bash
# ARM64 device (default)
./station_install/launch-edge-device.sh \
  --thing-name dda-arm64-edge-1 \
  --key-name YOUR_KEY_NAME

# x86_64 device
./station_install/launch-edge-device.sh \
  --thing-name dda-x86-edge-1 \
  --key-name YOUR_KEY_NAME \
  --arch x86_64

# Custom instance type and volume size
./station_install/launch-edge-device.sh \
  --thing-name dda-edge-1 \
  --key-name YOUR_KEY_NAME \
  --instance-type m6g.2xlarge \
  --volume-size 50
```

### Cleanup

To terminate an edge device:

```bash
# Using instance ID
aws ec2 terminate-instances --instance-ids $INSTANCE_ID

# Or using tag
aws ec2 terminate-instances \
  --filters "Name=tag:Name,Values=dda-edge-1"
```

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

## Deployment

### Prerequisites

- AWS CLI configured
- Node.js 18+, Python 3.11+
- AWS CDK: `npm install -g aws-cdk`
- [AWS Marketplace subscription](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6) (in UseCase Account)

### AWS Account Setup (Portal Account)

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

### Step-by-Step Deployment

See [edge-cv-portal/README.md](edge-cv-portal/README.md) for complete deployment instructions including:

- Portal infrastructure deployment
- Frontend build and deployment
- UseCase account setup
- Data account setup (optional)
- Admin user creation
- Portal configuration

## Features

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
| **Devices** | Monitor IoT Greengrass devices |
| **Settings** | Portal configuration and Data Accounts management |

## ML Workflow

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

## Documentation

| Document | Description |
|----------|-------------|
| [edge-cv-portal/README.md](edge-cv-portal/README.md) | Complete Portal deployment guide |
| [edge-cv-portal/ADMIN_GUIDE.md](edge-cv-portal/ADMIN_GUIDE.md) | Portal administration guide |
| [edge-cv-portal/TUTORIAL_MULTI_ACCOUNT_WORKFLOW.md](edge-cv-portal/TUTORIAL_MULTI_ACCOUNT_WORKFLOW.md) | End-to-end tutorial |
| [edge-cv-portal/TUTORIAL_SEGMENTATION_MULTI_ACCOUNT.md](edge-cv-portal/TUTORIAL_SEGMENTATION_MULTI_ACCOUNT.md) | Segmentation training guide |
| [DDA_END_TO_END_ARCHITECTURE.md](DDA_END_TO_END_ARCHITECTURE.md) | Complete system architecture |
| [README_MANUAL_DEPLOYMENT.md](README_MANUAL_DEPLOYMENT.md) | Manual deployment without Portal |
| [BUILD_SYSTEM_FINAL_STATUS.md](BUILD_SYSTEM_FINAL_STATUS.md) | Build system improvements and status |
| [ANIMATED_PROGRESS_INDICATOR.md](ANIMATED_PROGRESS_INDICATOR.md) | Progress indicator implementation details |

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

- **Portal Issues**: See [edge-cv-portal/README.md](edge-cv-portal/README.md#troubleshooting)
- **Manual Deployment**: See [README_MANUAL_DEPLOYMENT.md](README_MANUAL_DEPLOYMENT.md#troubleshooting)
- **GitHub Issues**: Report bugs and feature requests via [GitHub Issues](https://github.com/aws-samples/defect-detection-application/issues)
- **Discussions**: Join the community discussions for questions and support
