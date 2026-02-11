#!/bin/bash
set -e

# Build and publish Greengrass components using GDK
# This script builds components and publishes them to the Greengrass component repository

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

echo "Building component for architecture: $ARCH"
echo "Component name: $COMPONENT_NAME"
echo "Using recipe: $RECIPE_FILE"
echo ""

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
gdk component build

echo "Publishing component..."
gdk component publish

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
    if bash build-inference-uploader.sh; then
        echo "✅ InferenceUploader component built and published successfully!"
    else
        echo "⚠ Warning: InferenceUploader build failed (you can run ./build-inference-uploader.sh later)"
    fi
else
    echo ""
    echo "ℹ You can build the InferenceUploader component later by running:"
    echo "  ./build-inference-uploader.sh"
fi
