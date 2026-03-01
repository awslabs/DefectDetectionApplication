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

# Auto-generate config.json from CDK stack outputs
echo "Step 1: Generating config.json from CDK outputs..."

AUTH_CONFIG=$(aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalAuthStack \
  --query 'Stacks[0].Outputs[?OutputKey==`AuthConfig`].OutputValue' \
  --output text 2>/dev/null || echo "")

API_URL=$(aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalComputeStack \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiUrl`].OutputValue' \
  --output text 2>/dev/null || echo "")

if [ -n "$AUTH_CONFIG" ] && [ -n "$API_URL" ] && [ "$AUTH_CONFIG" != "None" ] && [ "$API_URL" != "None" ]; then
  USER_POOL_ID=$(echo "$AUTH_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin)['userPoolId'])")
  CLIENT_ID=$(echo "$AUTH_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin)['userPoolWebClientId'])")
  REGION=$(echo "$AUTH_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin)['region'])")

  # Remove trailing slash from API URL if present
  API_URL="${API_URL%/}"

  # Merge with existing config.json to preserve branding/custom fields
  if [ -f public/config.json ]; then
    python3 -c "
import json, sys
with open('public/config.json') as f:
    existing = json.load(f)
existing['apiUrl'] = '${API_URL}'
existing['userPoolId'] = '${USER_POOL_ID}'
existing['userPoolClientId'] = '${CLIENT_ID}'
existing['region'] = '${REGION}'
with open('public/config.json', 'w') as f:
    json.dump(existing, f, indent=2)
    f.write('\n')
"
    echo "✅ config.json updated (branding preserved):"
  else
    cat > public/config.json << EOF
{
  "apiUrl": "$API_URL",
  "userPoolId": "$USER_POOL_ID",
  "userPoolClientId": "$CLIENT_ID",
  "region": "$REGION"
}
EOF
    echo "✅ config.json created:"
  fi
  echo "   API URL:      $API_URL"
  echo "   User Pool:    $USER_POOL_ID"
  echo "   Client ID:    $CLIENT_ID"
  echo "   Region:       $REGION"
else
  echo "⚠️  Could not read CDK outputs. Using existing config.json."
  echo "   Make sure infrastructure is deployed first (./deploy-infrastructure.sh)"
  if [ ! -f public/config.json ]; then
    echo "❌ ERROR: public/config.json does not exist and could not be generated."
    echo "   Deploy infrastructure first, then re-run this script."
    exit 1
  fi
fi
echo ""

# Install dependencies
echo "Step 2: Installing dependencies..."
npm install

# Build the application
echo "Step 3: Building application..."
npm run build

# Deploy to S3
echo "Step 4: Deploying to S3..."
aws s3 sync dist/ s3://$BUCKET_NAME/ --delete

# Invalidate CloudFront cache
echo "Step 5: Invalidating CloudFront cache..."
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

# Step 6: Update cdk.json with CloudFront domain for auto-CORS configuration
echo ""
echo "Step 6: Configuring CloudFront domain for auto-CORS..."
cd "$SCRIPT_DIR/infrastructure"

# Check if jq is available
if command -v jq &> /dev/null; then
  # Use jq to update cdk.json
  CURRENT_DOMAIN=$(jq -r '.context.cloudFrontDomain // empty' cdk.json)
  if [ "$CURRENT_DOMAIN" != "$CLOUDFRONT_URL" ]; then
    echo "Updating cdk.json with CloudFront domain: $CLOUDFRONT_URL"
    jq --arg domain "$CLOUDFRONT_URL" '.context.cloudFrontDomain = $domain' cdk.json > cdk.json.tmp && mv cdk.json.tmp cdk.json
    
    # Redeploy compute stack to update Lambda environment variable
    echo "Step 7: Redeploying backend with CloudFront domain..."
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
