#!/bin/bash

# Edge CV Portal - Frontend Deployment Script

set -e

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
cd "$(dirname "$0")/frontend"

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

echo ""
echo "=========================================="
echo "Frontend Deployment Complete!"
echo "=========================================="
echo ""
echo "Access your portal at:"
echo "https://$CLOUDFRONT_URL"
echo ""
echo "Login credentials:"
echo "Username: testadmin"
echo "Password: TestAdmin123!"
echo ""
echo "Note: CloudFront cache invalidation may take a few minutes."
echo "=========================================="
