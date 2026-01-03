#!/bin/bash

# Build script for JWT Lambda Layer
# This script installs Python dependencies for JWT validation

set -e

echo "Building JWT Lambda Layer..."

# Create python directory if it doesn't exist
mkdir -p python

# Install dependencies
pip install -r requirements.txt -t python/

echo "JWT Lambda Layer built successfully!"
echo "Dependencies installed in python/ directory"