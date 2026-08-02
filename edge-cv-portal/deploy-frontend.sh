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
npm ci

# Build the application
echo "Step 3: Building application..."
npm run build

# Deploy to S3.
# Hashed assets are immutable and safe to cache for a year; the entry
# point (index.html) and runtime config (config.json) must always be
# revalidated so browsers pick up new deployments immediately instead of
# holding a stale index.html that references deleted hashed chunks.
echo "Step 4: Deploying to S3..."
aws s3 sync dist/ s3://$BUCKET_NAME/ --delete \
  --exclude "index.html" --exclude "config.json" \
  --cache-control "public, max-age=31536000, immutable"
aws s3 cp dist/index.html s3://$BUCKET_NAME/index.html \
  --cache-control "no-cache"
if [ -f dist/config.json ]; then
  aws s3 cp dist/config.json s3://$BUCKET_NAME/config.json \
    --cache-control "no-cache"
fi

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

# Step 6: Update Lambda env with CloudFront domain for auto-CORS configuration
echo ""
echo "Step 6: Deploying backend with CloudFront domain for auto-CORS..."
cd "$SCRIPT_DIR/infrastructure"

# The ComputeStack constructor refuses to synth an sts:AssumeRole grant on a
# wildcard account, so trustedUseCaseAccountIds must be supplied. Default to
# the deploying account (single-account setup); override via
# TRUSTED_USECASE_ACCOUNT_IDS for cross-account. Same pattern as
# infrastructure/deploy_portal_fixes.sh.

# Resolve + export credentials for CDK's JS SDK (guarded so failure is non-fatal).
if _CREDS_ENV=$(aws configure export-credentials --format env 2>/dev/null | grep -v AWS_CREDENTIAL_EXPIRATION); then
  eval "$_CREDS_ENV"
  unset _CREDS_ENV
fi

CDK_REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-us-east-1}}"
ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null)}"
if [ -z "$ACCOUNT" ] || [ "$ACCOUNT" = "None" ]; then
  echo "❌ ERROR: could not resolve AWS account. Check credentials (aws sts get-caller-identity)." >&2
  exit 1
fi
export CDK_DEFAULT_ACCOUNT="$ACCOUNT"
export CDK_DEFAULT_REGION="$CDK_REGION"
export AWS_REGION="$CDK_REGION"

TRUSTED="${TRUSTED_USECASE_ACCOUNT_IDS:-$ACCOUNT}"

echo "Redeploying compute stack with CloudFront domain: $CLOUDFRONT_URL"
echo "   Trusted UseCase accounts: $TRUSTED"
npm run build
npx cdk deploy EdgeCVPortalComputeStack --require-approval never \
  -c cloudFrontDomain="$CLOUDFRONT_URL" \
  -c "trustedUseCaseAccountIds=$TRUSTED" \
  -c "dataBucketAllowlist=${DATA_BUCKET_ALLOWLIST:-}"
echo "✅ Backend updated with CloudFront domain for auto-CORS configuration."

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
