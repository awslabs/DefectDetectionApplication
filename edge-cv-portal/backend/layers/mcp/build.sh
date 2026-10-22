#!/bin/bash
# Build script for the MCP Lambda Layer (Portal_MCP_Server)
# Installs the MCP Python SDK and the ASGI adapter for the python3.11 Lambda
# runtime (x86_64): binary wheels only, resolved for CPython 3.11 regardless of
# the interpreter running pip.
set -e

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "Building MCP Lambda Layer..."

# Create python directory if it doesn't exist
mkdir -p python

# Install dependencies
pip install --platform manylinux2014_x86_64 --only-binary=:all: \
    --python-version 3.11 --implementation cp \
    -r requirements.txt -t python/

echo "MCP Lambda Layer built successfully!"
echo "Dependencies installed in python/ directory"
