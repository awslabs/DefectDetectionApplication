#!/bin/bash
set -e
set -o pipefail

# Build and publish Greengrass components using GDK
# This script builds components and publishes them to the Greengrass component repository

# Step tracking
STEP=0
TOTAL_STEPS=7
START_TIME=$(date +%s)

print_step() {
    STEP=$((STEP + 1))
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "[$STEP/$TOTAL_STEPS] $1"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# Get architecture and determine recipe file
ARCH=$(uname -m)
case $ARCH in
    x86_64)
        RECIPE_FILE="recipe-amd64.yaml"
        COMPONENT_NAME="aws.edgeml.dda.LocalServer.amd64"
        ;;
    aarch64)
        RECIPE_FILE="recipe-arm64.yaml"
        COMPONENT_NAME="aws.edgeml.dda.LocalServer.arm64"
        ;;
    *)
        echo "Unsupported architecture: $ARCH"
        exit 1
        ;;
esac

print_step "Detecting architecture and preparing configuration"
echo "Architecture: $ARCH"
echo "Component name: $COMPONENT_NAME"
echo "Recipe file: $RECIPE_FILE"

# Use architecture-specific recipe
cp $RECIPE_FILE recipe.yaml

# Create gdk-config.json with architecture-specific component name
cat > gdk-config.json << EOF
{
  "component": {
    "${COMPONENT_NAME}": {
      "author": "Amazon",
      "version": "NEXT_PATCH",
      "build": {
        "build_system": "custom",
        "custom_build_command": [
          "bash",
          "build-custom.sh",
          "${COMPONENT_NAME}",
          "NEXT_PATCH"
        ]
      },
      "publish": {
        "bucket": "dda-component",
        "region": "us-east-1"
      }
    }
  },
  "gdk_version": "1.0.0"
}
EOF

# Clean GDK cache and build directories
rm -rf greengrass-build/
rm -rf .gdk/

# Build and publish component
echo "Building component..."
BUILD_LOG="/tmp/gdk-build-$(date +%s).log"
echo "Build log: $BUILD_LOG"
echo ""

# Run build with real-time output and log capture
if gdk component build 2>&1 | tee "$BUILD_LOG"; then
    echo ""
    echo "✓ Component built successfully"
else
    BUILD_EXIT_CODE=${PIPESTATUS[0]}
    echo ""
    echo "✗ Component build failed (exit code: $BUILD_EXIT_CODE)"
    echo ""
    echo "Last 50 lines of build log:"
    echo "---"
    tail -50 "$BUILD_LOG"
    echo "---"
    echo ""
    echo "Full log saved to: $BUILD_LOG"
    exit 1
fi

echo ""
echo "Publishing component..."
PUBLISH_LOG="/tmp/gdk-publish-$(date +%s).log"
echo "Publish log: $PUBLISH_LOG"
echo ""

if gdk component publish 2>&1 | tee "$PUBLISH_LOG"; then
    echo ""
    echo "✓ Component published successfully"
else
    PUBLISH_EXIT_CODE=${PIPESTATUS[0]}
    echo ""
    echo "✗ Component publish failed (exit code: $PUBLISH_EXIT_CODE)"
    echo ""
    echo "Last 50 lines of publish log:"
    echo "---"
    tail -50 "$PUBLISH_LOG"
    echo "---"
    echo ""
    echo "Full log saved to: $PUBLISH_LOG"
    exit 1
fi

echo ""
echo "Component ${COMPONENT_NAME} built and published successfully!"
echo ""

# Tag the published component with dda-portal:managed=true
echo "Tagging component for portal discovery..."

REGION=$(aws configure get region || echo "us-east-1")
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

COMPONENT_ARN=$(aws greengrassv2 list-components \
    --scope PRIVATE \
    --region $REGION \
    --query "components[?componentName=='${COMPONENT_NAME}'].arn | [0]" \
    --output text 2>/dev/null)

if [ -n "$COMPONENT_ARN" ] && [ "$COMPONENT_ARN" != "None" ]; then
    echo "Found component ARN: $COMPONENT_ARN"
    
    if aws greengrassv2 tag-resource \
        --resource-arn "$COMPONENT_ARN" \
        --tags "dda-portal:managed=true" \
        --region $REGION 2>/dev/null; then
        echo "✓ Component tagged successfully"
    else
        echo "⚠ Warning: Could not tag component (this is non-critical)"
    fi
else
    echo "⚠ Warning: Could not find component ARN for tagging (this is non-critical)"
fi

echo ""

# Ask if user wants to build InferenceUploader component
echo "Optional: Build InferenceUploader component?"
echo ""
echo "The InferenceUploader component enables edge devices to automatically"
echo "upload inference results (images and metadata) to S3 for centralized storage."
echo ""
read -p "Build and publish InferenceUploader component now? (y/n): " BUILD_INFERENCE_UPLOADER

if [ "$BUILD_INFERENCE_UPLOADER" = "y" ] || [ "$BUILD_INFERENCE_UPLOADER" = "Y" ]; then
    echo ""
    echo "Building InferenceUploader component..."
    INFERENCE_LOG="/tmp/inference-uploader-build-$(date +%s).log"
    echo "Build log: $INFERENCE_LOG"
    echo ""
    
    if bash build-inference-uploader.sh 2>&1 | tee "$INFERENCE_LOG"; then
        echo ""
        echo "✅ InferenceUploader component built and published successfully!"
    else
        INFERENCE_EXIT_CODE=${PIPESTATUS[0]}
        echo ""
        echo "✗ InferenceUploader build failed (exit code: $INFERENCE_EXIT_CODE)"
        echo ""
        echo "Last 50 lines of build log:"
        echo "---"
        tail -50 "$INFERENCE_LOG"
        echo "---"
        echo ""
        echo "Full log saved to: $INFERENCE_LOG"
        echo ""
        echo "You can run ./build-inference-uploader.sh later to retry"
    fi
else
    echo ""
    echo "ℹ You can build the InferenceUploader component later by running:"
    echo "  ./build-inference-uploader.sh"
fi
