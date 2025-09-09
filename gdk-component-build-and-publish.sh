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

# Backup original files
cp gdk-config.json gdk-config.json.backup
cp recipe.yaml recipe.yaml.backup

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

# Restore original files
mv gdk-config.json.backup gdk-config.json
mv recipe.yaml.backup recipe.yaml

echo "Component ${COMPONENT_NAME} built and published successfully!"