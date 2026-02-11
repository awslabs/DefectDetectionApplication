#!/bin/bash

# Deploy Edge CV Portal Infrastructure
# This script deploys the CDK infrastructure with the fixed shared_utils.py

set -e

# Get AWS account ID
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=$(aws configure get region || echo "us-east-1")

echo "🚀 Starting Edge CV Portal Infrastructure Deployment..."
echo "📍 AWS Account: $AWS_ACCOUNT_ID | Region: $AWS_REGION"
echo ""

# Change to infrastructure directory
cd infrastructure

echo "📦 Installing dependencies..."
npm install

echo "🔨 Building TypeScript..."
npm run build

echo "🧹 Clearing CDK cache to force layer update..."
rm -rf cdk.out

echo "🚀 Deploying CDK stacks with forced updates..."
cdk deploy --all --require-approval never --force

echo "✅ Deployment completed successfully!"
echo ""
echo "📝 Next steps:"
echo "1. Test the API endpoints to ensure 502 errors are resolved"
echo "2. Check that use cases appear in the dropdown"
echo "3. Verify RBAC functionality is working"
echo ""
echo "🔍 To test the use cases API:"
echo "curl -H \"Authorization: Bearer YOUR_TOKEN\" https://xg0yibkeh2.execute-api.us-east-1.amazonaws.com/v1/usecases"