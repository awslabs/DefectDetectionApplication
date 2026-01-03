#!/bin/bash
set -e

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

# Get the component ARN and tag it for portal visibility
echo "Tagging component for DDA Portal visibility..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region || echo "us-east-1")

# Get the latest version of the component
LATEST_VERSION=$(aws greengrassv2 list-component-versions \
  --arn "arn:aws:greengrass:${REGION}:${ACCOUNT_ID}:components:${COMPONENT_NAME}" \
  --query 'componentVersions[0].componentVersion' \
  --output text 2>/dev/null || echo "")

if [ -n "$LATEST_VERSION" ] && [ "$LATEST_VERSION" != "None" ]; then
  COMPONENT_ARN="arn:aws:greengrass:${REGION}:${ACCOUNT_ID}:components:${COMPONENT_NAME}:versions:${LATEST_VERSION}"
  
  echo "Tagging component: $COMPONENT_ARN"
  aws greengrassv2 tag-resource \
    --resource-arn "$COMPONENT_ARN" \
    --tags "dda-portal:managed=true" \
           "dda-portal:component-type=local-server" \
           "dda-portal:architecture=${ARCH}"
  
  echo "Component tagged successfully!"
else
  echo "Warning: Could not determine component version for tagging"
fi

echo "Component ${COMPONENT_NAME} built and published successfully!"