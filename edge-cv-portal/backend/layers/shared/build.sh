#!/bin/bash

# Build shared Lambda layer
# Packages shared utilities and dependencies for Lambda functions

set -e

LAYER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$LAYER_DIR/build"
PYTHON_DIR="$BUILD_DIR/python"

echo "Building shared Lambda layer..."

# Create build directory
mkdir -p "$PYTHON_DIR"

# Copy shared utilities
cp "$LAYER_DIR/python"/*.py "$PYTHON_DIR/" 2>/dev/null || true

# Install dependencies
if [ -f "$LAYER_DIR/requirements.txt" ]; then
    pip install -r "$LAYER_DIR/requirements.txt" -t "$PYTHON_DIR/" --upgrade
fi

# Create zip file
cd "$BUILD_DIR"
zip -r ../layer.zip . > /dev/null

echo "Layer built successfully: $LAYER_DIR/layer.zip"
