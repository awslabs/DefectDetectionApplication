# Defect Detection Application (DDA) Portal

Multi-tenant admin portal for managing computer vision defect detection workloads on AWS edge devices.

## Quick Links

| Document | Description |
|----------|-------------|
| [ADMIN_GUIDE.md](ADMIN_GUIDE.md) | Complete deployment & administration guide |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Quick deployment reference |
| [DATA_ACCOUNT_SETUP.md](DATA_ACCOUNT_SETUP.md) | Data account configuration scenarios |
| [SHARED_COMPONENTS.md](SHARED_COMPONENTS.md) | Greengrass component provisioning |

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

# 2. (Optional) Enable automatic CORS configuration
# After first deployment, get your CloudFront domain and redeploy:
cdk deploy --all -c cloudFrontDomain=YOUR_CLOUDFRONT_DOMAIN.cloudfront.net

# 3. Deploy UseCase Role (in UseCase Account)
./deploy-account-role.sh  # Select option 1

# 4. (Optional) Deploy Data Account Role (if using separate Data Account)
./deploy-account-role.sh  # Select option 2

# 5. Create UseCase in portal with Role ARN + External ID
#    The following are automatically configured during onboarding:
#    - Bucket policy (for SageMaker cross-account access)
#    - CORS (for browser uploads)
#    - Bucket tagging (dda-portal:managed=true)
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
