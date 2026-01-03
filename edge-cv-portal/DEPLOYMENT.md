# Defect Detection Application (DDA) - Deployment Guide

## Prerequisites

- AWS CLI configured with appropriate credentials
- Node.js 18+ installed
- Python 3.11+ installed
- AWS CDK CLI installed: `npm install -g aws-cdk`

## Step 1: Deploy Infrastructure

```bash
cd infrastructure
npm install
npm run build
cdk bootstrap  # Only needed once per account/region
cdk deploy --all
```

This will deploy:
- Cognito User Pool for authentication
- DynamoDB tables for data storage
- Lambda functions for API handlers
- API Gateway for REST API
- CloudFront distribution for frontend hosting

**Note the outputs** from the deployment, especially:
- UserPoolId
- UserPoolClientId
- ApiUrl
- DistributionDomainName

## Step 2: Build and Deploy Frontend

```bash
cd ../frontend
npm install

# Create config.json with values from CDK outputs
cat > public/config.json << EOF
{
  "apiUrl": "<API_URL_FROM_CDK_OUTPUT>",
  "userPoolId": "<USER_POOL_ID_FROM_CDK_OUTPUT>",
  "userPoolClientId": "<USER_POOL_CLIENT_ID_FROM_CDK_OUTPUT>",
  "region": "us-east-1"
}
EOF

# Build the frontend
npm run build

# Deploy to S3
aws s3 sync dist/ s3://<FRONTEND_BUCKET_NAME>/ --delete

# Invalidate CloudFront cache
aws cloudfront create-invalidation \
  --distribution-id <DISTRIBUTION_ID> \
  --paths "/*"
```

## Step 3: Create Initial Users

```bash
# Create a PortalAdmin user
aws cognito-idp admin-create-user \
  --user-pool-id <USER_POOL_ID> \
  --username admin \
  --user-attributes Name=email,Value=admin@example.com \
  --temporary-password TempPassword123! \
  --message-action SUPPRESS

# Set custom attribute for role
aws cognito-idp admin-update-user-attributes \
  --user-pool-id <USER_POOL_ID> \
  --username admin \
  --user-attributes Name=custom:role,Value=PortalAdmin

# Create UserRoles entry for super user access
aws dynamodb put-item \
  --table-name dda-portal-user-roles \
  --item '{
    "user_id": {"S": "<USER_SUB_FROM_COGNITO>"},
    "usecase_id": {"S": "global"},
    "role": {"S": "PortalAdmin"},
    "assigned_at": {"N": "'$(date +%s)'000"},
    "assigned_by": {"S": "system"}
  }'
```

## Step 4: Access the Portal

Navigate to: `https://<DISTRIBUTION_DOMAIN_NAME>`

Login with:
- Username: `admin`
- Password: `TempPassword123!` (you'll be prompted to change it)

## MVP Features

This MVP includes:

✅ **Infrastructure**
- Cognito User Pool (SSO integration ready)
- DynamoDB tables (UseCases, UserRoles, Devices, AuditLog)
- Lambda functions (Auth, UseCases, Devices handlers)
- API Gateway with JWT authorization
- CloudFront + S3 for frontend hosting

✅ **Frontend**
- React app with TypeScript
- CloudScape Design System
- Dashboard with metrics
- Use Case management (CRUD)
- Device inventory viewing
- Mock authentication (ready for Cognito integration)

✅ **Backend**
- RESTful API with RBAC
- Super user (PortalAdmin) support
- Audit logging
- Cross-account role validation

## Next Steps

To extend this MVP:

1. **Complete Authentication**: Integrate AWS Amplify for full Cognito auth
2. **Add Labeling**: Implement Ground Truth job creation
3. **Add Training**: Implement SageMaker training workflows
4. **Add Deployments**: Implement Greengrass deployment management
5. **Add Device Control**: Implement IoT Jobs for device management
6. **Add Real-time Updates**: Implement WebSocket API

## Cleanup

To remove all resources:

```bash
cd infrastructure
cdk destroy --all
```

## Troubleshooting

### Lambda Functions Not Working

Check CloudWatch Logs:
```bash
aws logs tail /aws/lambda/DDAPortalComputeStack-UseCasesHandler --follow
```

### API Gateway 403 Errors

Verify Cognito token is being sent:
- Check browser DevTools Network tab
- Ensure Authorization header is present
- Verify token hasn't expired

### Frontend Not Loading

- Check CloudFront distribution status
- Verify S3 bucket has correct files
- Check browser console for errors
- Verify config.json has correct values

## Support

For issues or questions, refer to the design document at `.kiro/specs/edge-cv-admin-portal/design.md`
