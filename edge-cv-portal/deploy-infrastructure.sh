#!/bin/bash

# Deploy Edge CV Portal Infrastructure
# This script deploys the CDK infrastructure with the fixed shared_utils.py

set -e

# Get AWS account ID
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=$(aws configure get region 2>/dev/null)
if [ -z "$AWS_REGION" ]; then
    echo "❌ ERROR: No AWS region configured."
    echo "   Run: aws configure set region us-east-1"
    echo "   Or set: export AWS_DEFAULT_REGION=us-east-1"
    exit 1
fi

echo "🚀 Starting Edge CV Portal Infrastructure Deployment..."
echo "📍 AWS Account: $AWS_ACCOUNT_ID | Region: $AWS_REGION"
echo ""

# Change to infrastructure directory
cd infrastructure

echo "📦 Installing dependencies..."
npm ci

echo "🔨 Building TypeScript..."
npm run build

echo "🧹 Clearing CDK cache to force layer update..."
rm -rf cdk.out

echo "🚀 Deploying CDK stacks with forced updates..."

# If frontend stack already exists, pass CloudFront domain for auto-CORS
CLOUDFRONT_URL=$(aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalFrontendStack \
  --query 'Stacks[0].Outputs[?OutputKey==`DistributionDomainName`].OutputValue' \
  --output text 2>/dev/null || echo "")

CDK_CONTEXT_ARGS=""
if [ -n "$CLOUDFRONT_URL" ] && [ "$CLOUDFRONT_URL" != "None" ]; then
  echo "📡 Found existing CloudFront domain: $CLOUDFRONT_URL"
  CDK_CONTEXT_ARGS="-c cloudFrontDomain=$CLOUDFRONT_URL"
fi

# Portal_Identity registry enforcement (portal-jwt-role-privilege-escalation
# Req 2.4). Off unless the operator asks for it, matching the CDK helper's
# default-OFF resolution: with enforcement on and a registry row missing, the
# portal denies that principal everything, including the bootstrap `admin`.
# Flip it only AFTER backfill_portal_registry.py has been applied and the
# accounts verified:
#
#   PORTAL_REGISTRY_ENFORCED=true ./deploy-infrastructure.sh
#
# Any value the shared layer accepts as true (1/true/yes/on/enabled) is passed
# through; anything else (including unset) deploys with enforcement off.
if [ -n "$PORTAL_REGISTRY_ENFORCED" ]; then
  echo "🔐 Portal_Identity enforcement requested: PORTAL_REGISTRY_ENFORCED=$PORTAL_REGISTRY_ENFORCED"
  CDK_CONTEXT_ARGS="$CDK_CONTEXT_ARGS -c portalRegistryEnforced=$PORTAL_REGISTRY_ENFORCED"
fi

# The project-local CLI (devDependency aws-cdk, installed by `npm ci` above):
# aws-cdk-lib emits a cloud-assembly schema that an older global `cdk` on
# PATH cannot read ("Cloud assembly schema version mismatch").
npx cdk deploy --all --require-approval never --force $CDK_CONTEXT_ARGS

echo "✅ Deployment completed successfully!"
echo ""
echo "📝 Next steps:"
echo "1. Test the API endpoints to ensure 502 errors are resolved"
echo "2. Check that use cases appear in the dropdown"
echo "3. Verify RBAC functionality is working"
echo ""
echo "🔍 To test the use cases API:"
echo "curl -H \"Authorization: Bearer YOUR_TOKEN\" https://xg0yibkeh2.execute-api.us-east-1.amazonaws.com/v1/usecases"