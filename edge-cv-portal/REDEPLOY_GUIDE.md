# Full Redeploy Guide

This guide walks through a complete redeploy of the DDA Portal from scratch.

## Prerequisites

- AWS CLI configured with appropriate credentials
- Node.js 18+ installed
- CDK CLI installed: `npm install -g aws-cdk`

## Step 1: Clean Up Old Infrastructure (Optional but Recommended)

If you want a completely fresh start, delete the old stacks:

```bash
cd DefectDetectionApplication/edge-cv-portal/infrastructure

# Delete frontend stack
aws cloudformation delete-stack --stack-name EdgeCVPortalFrontendStack --region us-east-1

# Delete compute stack
aws cloudformation delete-stack --stack-name EdgeCVPortalComputeStack --region us-east-1

# Delete storage stack
aws cloudformation delete-stack --stack-name EdgeCVPortalStorageStack --region us-east-1

# Delete auth stack
aws cloudformation delete-stack --stack-name EdgeCVPortalAuthStack --region us-east-1

# Wait for deletion to complete (5-10 minutes)
aws cloudformation wait stack-delete-complete --stack-name EdgeCVPortalAuthStack --region us-east-1
```

## Step 2: Deploy Infrastructure

```bash
cd DefectDetectionApplication/edge-cv-portal/infrastructure

# Install dependencies
npm install

# Deploy all stacks
cdk deploy --all --require-approval never
```

This will deploy:
- **EdgeCVPortalAuthStack** - Cognito User Pool and authentication
- **EdgeCVPortalStorageStack** - DynamoDB tables and S3 buckets
- **EdgeCVPortalComputeStack** - Lambda functions and API Gateway
- **EdgeCVPortalFrontendStack** - CloudFront and S3 frontend hosting

**Save the outputs** - you'll need them for the next steps.

## Step 3: Create Portal Admin User

After infrastructure deployment, create an admin user:

```bash
# Get the User Pool ID from CDK outputs
USER_POOL_ID="us-east-1_YOUR_POOL_ID"
REGION="us-east-1"

# Create admin user
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username testadmin \
  --message-action SUPPRESS \
  --temporary-password TempPassword123! \
  --region $REGION

# Set permanent password
aws cognito-idp admin-set-user-password \
  --user-pool-id $USER_POOL_ID \
  --username testadmin \
  --password TestAdmin123! \
  --permanent \
  --region $REGION

# Set user role attribute (CRITICAL for API access)
aws cognito-idp admin-update-user-attributes \
  --user-pool-id $USER_POOL_ID \
  --username testadmin \
  --user-attributes Name=custom:role,Value=PortalAdmin \
  --region $REGION
```

## Step 4: Deploy Frontend

```bash
cd DefectDetectionApplication/edge-cv-portal

# Build and deploy frontend
./deploy-frontend.sh
```

This will:
- Build the React application
- Upload to S3
- Invalidate CloudFront cache

## Step 5: Test Portal Access

1. Get the CloudFront URL from CDK outputs
2. Open in browser
3. Login with credentials:
   - Username: `testadmin`
   - Password: `TestAdmin123!`
4. Verify you can access the dashboard and make API calls

## Troubleshooting

### 401 Unauthorized Errors

If you get 401 errors when making API calls:

1. **Verify the user has the `custom:role` attribute:**
   ```bash
   aws cognito-idp admin-get-user \
     --user-pool-id $USER_POOL_ID \
     --username testadmin \
     --region us-east-1
   ```
   Should show: `"Name": "custom:role", "Value": "PortalAdmin"`

2. **Check API Gateway access logs:**
   ```bash
   aws logs tail /aws/apigateway/edge-cv-portal-access --follow --region us-east-1
   ```

3. **Verify config.json has correct credentials:**
   ```bash
   cat edge-cv-portal/frontend/public/config.json
   ```

### Token Not Being Stored

If the token is not being stored in localStorage:

1. Open browser DevTools → Application → Local Storage
2. Look for key `idToken`
3. If missing, check browser console for errors during login

### CORS Errors

If you see CORS errors in the browser console:

1. The API Gateway should have CORS configured
2. Check that the frontend CloudFront domain is allowed
3. Verify the Authorization header is being sent

## Rollback

If something goes wrong, you can rollback to the previous version:

```bash
# Revert all changes
git checkout .

# Redeploy with previous code
cdk deploy --all --require-approval never
```
