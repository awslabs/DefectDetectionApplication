#!/bin/bash

# Deploy compilation events infrastructure
# This adds the missing EventBridge rule and Lambda handler for automatic Greengrass component creation

echo "🚀 Deploying compilation events infrastructure..."

# Navigate to infrastructure directory
cd infrastructure

# Deploy the compute stack with the new compilation events handler
echo "📦 Deploying compute stack with compilation events..."
npx cdk deploy EdgeCVPortalComputeStack --require-approval never

if [ $? -eq 0 ]; then
    echo "✅ Compilation events infrastructure deployed successfully!"
    echo ""
    echo "🔧 What was added:"
    echo "  • Compilation Events Lambda Handler"
    echo "  • EventBridge Rule for SageMaker Compilation Job State Changes"
    echo "  • Automatic Greengrass component creation when compilation completes"
    echo ""
    echo "🎯 Now when a compilation job completes:"
    echo "  1. EventBridge captures the completion event"
    echo "  2. Compilation Events Lambda updates the job status"
    echo "  3. Greengrass Publish Lambda is triggered automatically"
    echo "  4. A new Greengrass component is created with the compiled model"
    echo ""
    echo "🧪 Test by starting a new compilation job from the UI!"
else
    echo "❌ Deployment failed!"
    exit 1
fi