#!/bin/bash

# Build and publish Greengrass components using GDK
# This script builds components and publishes them to the Greengrass component repository

set -e

echo "Building and publishing Greengrass components..."

# Build LocalServer components for different architectures
for arch in arm64 amd64; do
    echo "Building LocalServer for $arch..."
    gdk component build --architecture $arch
    gdk component publish --architecture $arch
done

echo "Components published successfully!"
