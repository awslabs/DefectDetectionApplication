#!/bin/bash

# Build workflow_core Lambda layer
# Packages the workflow_core package (node catalog, serializer, validator,
# compiler) and its dependencies for Lambda functions

set -e

LAYER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$LAYER_DIR/build"
PYTHON_DIR="$BUILD_DIR/python"

echo "Building workflow_core Lambda layer..."

# Clean and create build directory
rm -rf "$BUILD_DIR" "$LAYER_DIR/layer.zip"
mkdir -p "$PYTHON_DIR"

# Copy the workflow_core package (exclude caches)
cp -r "$LAYER_DIR/python/workflow_core" "$PYTHON_DIR/"
find "$PYTHON_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Install dependencies targeting the Lambda runtime platform (x86_64,
# Python 3.11) so native wheels are correct regardless of the build host.
if [ -f "$LAYER_DIR/requirements.txt" ]; then
    python3 -m pip install -r "$LAYER_DIR/requirements.txt" -t "$PYTHON_DIR/" \
        --platform manylinux2014_x86_64 \
        --implementation cp \
        --python-version 3.11 \
        --only-binary=:all: \
        --upgrade
fi

# Create zip file
cd "$BUILD_DIR"
zip -r ../layer.zip . > /dev/null

echo "Layer built successfully: $LAYER_DIR/layer.zip"
