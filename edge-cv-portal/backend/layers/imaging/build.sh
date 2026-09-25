#!/bin/bash

# Build script for the imaging Lambda Layer (synthetic-defect-data-generation)
# Bundles Pillow for the SyntheticDataHandler's image decode/diff path
# (bbox_from_diff auto-annotation fallback).
#
# Same convention as the sibling jwt layer: run this script before deploying
# so the python/ directory exists when the CDK asset is packaged.

set -e

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Building imaging Lambda Layer..."

mkdir -p python

# Pillow ships native extensions; force the manylinux wheel matching the
# Lambda runtime (Python 3.12, x86_64) so the layer works regardless of the
# build host. Pillow >= 12 publishes only manylinux_2_28 wheels, which need
# the python3.12 runtime (Amazon Linux 2023); the layer's consumers run on it.
pip install -r requirements.txt -t python/ \
    --platform manylinux_2_28_x86_64 \
    --implementation cp \
    --python-version 3.12 \
    --only-binary=:all:

echo "Imaging Lambda Layer built successfully!"
echo "Dependencies installed in python/ directory"
