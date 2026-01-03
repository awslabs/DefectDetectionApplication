#!/bin/bash
# Build script for shared Lambda layer
# Installs Python dependencies into the layer for Linux (Lambda runtime)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_DIR="$SCRIPT_DIR/python"

echo "Installing dependencies into $PYTHON_DIR for Linux (Lambda runtime)..."

# Install PyYAML for Linux platform (manylinux)
pip install PyYAML -t "$PYTHON_DIR" --upgrade --no-cache-dir --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.11

echo "Dependencies installed successfully!"
echo "Contents of $PYTHON_DIR:"
ls -la "$PYTHON_DIR"
