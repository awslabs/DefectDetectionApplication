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

### 2. Build and Deploy Frontend

```bash
cd edge-cv-portal
./deploy-frontend.sh
```

**Output**: Portal URL (e.g., `https://d1r8hupkjbsjb1.cloudfront.net`)

### 3. Create Admin User

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

### 4. Deploy UseCase Account Role (UseCase Account)

```bash
# Bootstrap CDK in UseCase Account
cdk bootstrap aws://YOUR_USECASE_ACCOUNT_ID/us-east-1

# Deploy role
cd edge-cv-portal
./deploy-account-role.sh  # Select option 1
```

### 5. Create UseCase in Portal

1. Log in to Portal as admin
2. Go to Settings → UseCases
3. Click "Add UseCase"
4. Fill in Account ID, Role ARN, and External ID from step 4
5. Click "Create"

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
