# Defect Detection Application (DDA) Portal

Multi-tenant admin portal for managing computer vision defect detection workloads on AWS edge devices.

## Quick Links

| Document | Description |
|----------|-------------|
| [ADMIN_GUIDE.md](ADMIN_GUIDE.md) | Complete deployment & administration guide |
| [QUICKSTART.md](QUICKSTART.md) | Quick start for developers |
| [USECASE_ACCOUNT_SETUP.md](USECASE_ACCOUNT_SETUP.md) | Detailed UseCase Account setup |

## Features

| Feature | Description |
|---------|-------------|
| **Data Management** | Browse & upload training images to S3 |
| **Labeling** | Create Ground Truth bounding box jobs |
| **Training** | SageMaker training with Marketplace algorithm |
| **Compilation** | Compile models for edge (x86-64, ARM64) |
| **Components** | Manage Greengrass components |
| **Deployments** | Deploy models to edge devices |
| **Devices** | Monitor IoT Greengrass devices |

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

# 2. Deploy UseCase Role (in UseCase Account)
./deploy-account-role.sh  # Select option 1

# 3. Tag S3 buckets for portal access
aws s3api put-bucket-tagging --bucket YOUR_BUCKET \
  --tagging 'TagSet=[{Key=dda-portal:managed,Value=true}]'

# 4. Create UseCase in portal with Role ARN + External ID
```

## Project Structure

```
edge-cv-portal/
├── infrastructure/     # AWS CDK stacks
├── backend/           # Lambda functions (Python)
├── frontend/          # React app (TypeScript)
├── deploy-account-role.sh  # UseCase/Data account setup
└── ADMIN_GUIDE.md     # Full documentation
```

## Prerequisites

- AWS CLI configured
- Node.js 18+, Python 3.11+
- AWS CDK: `npm install -g aws-cdk`
- [AWS Marketplace subscription](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6) (in UseCase Account)

## Support

Check CloudWatch Logs: `/aws/lambda/EdgeCVPortal*`
