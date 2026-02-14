#!/bin/bash

# Edge CV Portal - Frontend Deployment Script

set -e

# Capture the script's directory at the start (before any cd commands)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=========================================="
echo "DDA - Frontend Deployment"
echo "=========================================="
echo ""

# Get the bucket name from CloudFormation
BUCKET_NAME=$(aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalFrontendStack \
  --query 'Stacks[0].Outputs[?OutputKey==`FrontendBucketName`].OutputValue' \
  --output text)

DISTRIBUTION_ID=$(aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalFrontendStack \
  --query 'Stacks[0].Outputs[?OutputKey==`DistributionId`].OutputValue' \
  --output text)

echo "Bucket: $BUCKET_NAME"
echo "Distribution: $DISTRIBUTION_ID"
echo ""

# Navigate to frontend directory
cd "$SCRIPT_DIR/frontend"

# Install dependencies
echo "Step 1: Installing dependencies..."
npm install

# Build the application
echo "Step 2: Building application..."
npm run build

# Deploy to S3
echo "Step 3: Deploying to S3..."
aws s3 sync dist/ s3://$BUCKET_NAME/ --delete

# Invalidate CloudFront cache
echo "Step 4: Invalidating CloudFront cache..."
aws cloudfront create-invalidation \
  --distribution-id $DISTRIBUTION_ID \
  --paths "/*" \
  --query 'Invalidation.Id' \
  --output text

# Get CloudFront URL from stack outputs
CLOUDFRONT_URL=$(aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalFrontendStack \
  --query 'Stacks[0].Outputs[?OutputKey==`DistributionDomainName`].OutputValue' \
  --output text)

# Step 5: Update cdk.json with CloudFront domain for auto-CORS configuration
echo ""
echo "Step 5: Configuring CloudFront domain for auto-CORS..."
cd "$SCRIPT_DIR/infrastructure"

# Check if jq is available
if command -v jq &> /dev/null; then
  # Use jq to update cdk.json
  CURRENT_DOMAIN=$(jq -r '.context.cloudFrontDomain // empty' cdk.json)
  if [ "$CURRENT_DOMAIN" != "$CLOUDFRONT_URL" ]; then
    echo "Updating cdk.json with CloudFront domain: $CLOUDFRONT_URL"
    jq --arg domain "$CLOUDFRONT_URL" '.context.cloudFrontDomain = $domain' cdk.json > cdk.json.tmp && mv cdk.json.tmp cdk.json
    
    # Redeploy compute stack to update Lambda environment variable
    echo "Step 6: Redeploying backend with CloudFront domain..."
    npm run build
    npx cdk deploy EdgeCVPortalComputeStack --require-approval never
    echo "Backend updated with CloudFront domain for auto-CORS configuration."
  else
    echo "CloudFront domain already configured in cdk.json"
  fi
else
  echo "WARNING: jq not installed. Please manually add cloudFrontDomain to cdk.json:"
  echo "  \"cloudFrontDomain\": \"$CLOUDFRONT_URL\""
  echo ""
  echo "Then run: cd infrastructure && cdk deploy EdgeCVPortalComputeStack"
fi

echo ""
echo "=========================================="
echo "Frontend Deployment Complete!"
echo "=========================================="
echo ""
echo "Access your portal at:"
echo "https://$CLOUDFRONT_URL"
echo ""
echo "Note: CloudFront cache invalidation may take a few minutes."
echo "=========================================="
