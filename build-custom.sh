#!/bin/bash
# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -e

VERBOSE="${VERBOSE:-0}"
LOG_FILE="${LOG_FILE:-/tmp/build-custom-$(date +%s).log}"
ERRORS=()
WARNINGS=()

# Helper function to run commands with logging
run_cmd() {
    local cmd="$@"
    if [ "$VERBOSE" = "1" ]; then
        echo "[RUN] $cmd"
        eval "$cmd" | tee -a "$LOG_FILE"
    else
        echo "[RUN] $cmd"
        if ! eval "$cmd" >> "$LOG_FILE" 2>&1; then
            return 1
        fi
    fi
}

# Trap errors and show summary
trap 'show_summary' EXIT

show_summary() {
    echo ""
    if [ ${#ERRORS[@]} -gt 0 ]; then
        echo "❌ BUILD FAILED WITH ERRORS:"
        printf '%s\n' "${ERRORS[@]}"
        echo ""
    fi
    if [ ${#WARNINGS[@]} -gt 0 ]; then
        echo "⚠️  WARNINGS:"
        printf '%s\n' "${WARNINGS[@]}"
        echo ""
    fi
    if [ ${#ERRORS[@]} -eq 0 ] && [ ${#WARNINGS[@]} -eq 0 ]; then
        echo "✅ Build completed successfully!"
    fi
    echo "📋 Full log: $LOG_FILE"
}

# Validate arguments
if [ $# -ne 2 ]; then
  echo 1>&2 "Usage: $0 COMPONENT-NAME COMPONENT-VERSION"
  exit 3
fi

COMPONENT_NAME=$1
VERSION=$2
ARCHITECTURE=$(uname -m)
BUILDKIT_PROGRESS=plain
export BUILDKIT_PROGRESS

# Detect Ubuntu version
if [ -f /etc/lsb-release ]; then
    IMAGE_VER=$(grep "DISTRIB_RELEASE" /etc/lsb-release | cut -d'=' -f2)
else
    IMAGE_VER="18.04"
    WARNINGS+=("Could not detect Ubuntu version, defaulting to 18.04")
fi
export IMAGE_VER

echo "Building component: $COMPONENT_NAME"
echo "Version: $VERSION"
echo "Architecture: $ARCHITECTURE"
echo "Ubuntu version: $IMAGE_VER"
echo "Log file: $LOG_FILE"
echo ""

# Check if Docker daemon is running
echo "▶ Checking Docker daemon..."
if ! docker ps > /dev/null 2>&1; then
    ERRORS+=("Docker daemon is not running or not accessible")
    exit 1
fi
echo "✓ Docker daemon is running"
echo ""

# Ensure greengrass-build directory exists
echo "▶ Setting up build directories..."
mkdir -p ./greengrass-build/recipes
mkdir -p ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION
echo "✓ Build directories created"
echo ""

# Copy recipe to greengrass-build
echo "▶ Copying recipe..."
if [ -f recipe.yaml ]; then
    cp recipe.yaml ./greengrass-build/recipes
    echo "✓ Recipe copied"
else
    ERRORS+=("recipe.yaml not found")
    exit 1
fi
echo ""

# Create custom build directory
echo "▶ Preparing custom build directory..."
rm -rf ./custom-build
mkdir -p ./custom-build/$COMPONENT_NAME
echo "✓ Custom build directory created"
echo ""

# Build Docker images
echo "▶ Building Docker images..."
cd src

# Build edgemlsdk
echo "  ▶ Building edgemlsdk Docker image..."
if [ -d edgemlsdk ] && [ -f edgemlsdk/build.sh ]; then
    cd edgemlsdk/
    if run_cmd "./build.sh -p $(uname -m) -u $IMAGE_VER 3.9"; then
        echo "  ✓ edgemlsdk built successfully"
    else
        ERRORS+=("Failed to build edgemlsdk")
        exit 1
    fi
    cd ..
    
    # Extract dependencies from edgemlsdk image
    echo "  ▶ Extracting edgemlsdk dependencies..."
    mkdir -p backend/edgemlsdk
    cp -r edgemlsdk backend/edgemlsdk
    
    if id=$(docker create edgemlsdk 2>/dev/null); then
        echo "  ▶ Copying .deb files from edgemlsdk image..."
        docker cp $id:/debs/PanoramaSDK.deb $(pwd)/backend/edgemlsdk/ 2>/dev/null || WARNINGS+=("Could not copy PanoramaSDK.deb")
        docker cp $id:/debs/aws-c-iot.deb $(pwd)/backend/edgemlsdk/ 2>/dev/null || WARNINGS+=("Could not copy aws-c-iot.deb")
        docker cp $id:/debs/aws-crt-cpp.deb $(pwd)/backend/edgemlsdk/ 2>/dev/null || WARNINGS+=("Could not copy aws-crt-cpp.deb")
        docker cp $id:/debs/aws-iot-device-sdk-cpp-v2.deb $(pwd)/backend/edgemlsdk/ 2>/dev/null || WARNINGS+=("Could not copy aws-iot-device-sdk-cpp-v2.deb")
        docker cp $id:/debs/aws-sdk-cpp.deb $(pwd)/backend/edgemlsdk/ 2>/dev/null || WARNINGS+=("Could not copy aws-sdk-cpp.deb")
        docker cp $id:/debs/libgstreamer-plugins-base1.0-dev.deb $(pwd)/backend/edgemlsdk/ 2>/dev/null || WARNINGS+=("Could not copy libgstreamer-plugins-base1.0-dev.deb")
        docker cp $id:/debs/libgstreamer1.0-dev.deb $(pwd)/backend/edgemlsdk/ 2>/dev/null || WARNINGS+=("Could not copy libgstreamer1.0-dev.deb")
        docker cp $id:/debs/libgstreamer1.0.deb $(pwd)/backend/edgemlsdk/ 2>/dev/null || WARNINGS+=("Could not copy libgstreamer1.0.deb")
        docker cp $id:/debs/liborc-0.4-0.deb $(pwd)/backend/edgemlsdk/ 2>/dev/null || WARNINGS+=("Could not copy liborc-0.4-0.deb")
        docker cp $id:/debs/libstdc++6.deb $(pwd)/backend/edgemlsdk/ 2>/dev/null || WARNINGS+=("Could not copy libstdc++6.deb")
        docker cp $id:/debs/openssl.deb $(pwd)/backend/edgemlsdk/ 2>/dev/null || WARNINGS+=("Could not copy openssl.deb")
        docker cp $id:/debs/panorama.whl $(pwd)/backend/edgemlsdk/panorama-1.0-py3-none-any.whl 2>/dev/null || WARNINGS+=("Could not copy panorama.whl")
        docker cp $id:/debs/triton-core.deb $(pwd)/backend/edgemlsdk/ 2>/dev/null || WARNINGS+=("Could not copy triton-core.deb")
        docker cp $id:/debs/triton-python-backend.deb $(pwd)/backend/edgemlsdk/ 2>/dev/null || WARNINGS+=("Could not copy triton-python-backend.deb")
        docker cp $id:/tars/triton_installation_files.tar.gz $(pwd)/backend/edgemlsdk/ 2>/dev/null || WARNINGS+=("Could not copy triton_installation_files.tar.gz")
        docker rm -v $id > /dev/null 2>&1
        echo "  ✓ Dependencies extracted"
    else
        WARNINGS+=("Could not create container from edgemlsdk image")
    fi
else
    WARNINGS+=("edgemlsdk directory or build.sh not found, skipping edgemlsdk build")
fi
echo ""

# Build backend and frontend images
echo "  ▶ Building backend and frontend Docker images..."
if run_cmd "docker-compose --profile tegra --profile generic -f docker-compose.yaml build --build-arg OS=$IMAGE_VER"; then
    echo "  ✓ Docker images built successfully"
else
    ERRORS+=("Failed to build Docker images with docker-compose")
    exit 1
fi
echo ""

# Save Docker images as tar files
echo "▶ Saving Docker images as tarballs..."
if docker image inspect flask-app > /dev/null 2>&1; then
    if run_cmd "docker save --output ../custom-build/$COMPONENT_NAME/flask-app.tar flask-app"; then
        echo "✓ flask-app image saved"
    else
        ERRORS+=("Failed to save flask-app image")
        exit 1
    fi
else
    WARNINGS+=("flask-app image not found")
fi

if docker image inspect react-webapp > /dev/null 2>&1; then
    if run_cmd "docker save --output ../custom-build/$COMPONENT_NAME/react-webapp.tar react-webapp"; then
        echo "✓ react-webapp image saved"
    else
        ERRORS+=("Failed to save react-webapp image")
        exit 1
    fi
else
    WARNINGS+=("react-webapp image not found")
fi
echo ""

cd ..

# Include docker-compose.yaml in archive
echo "▶ Preparing component archive..."
cp src/docker-compose.yaml ./custom-build/$COMPONENT_NAME/

# Include empty directories for each image build context
mkdir -p ./custom-build/$COMPONENT_NAME/backend
mkdir -p ./custom-build/$COMPONENT_NAME/frontend
mkdir -p ./custom-build/$COMPONENT_NAME/host_scripts

# Include dio script that triggers output
if [ -f src/backend/triggers/outputs/dio.py ]; then
    cp src/backend/triggers/outputs/dio.py ./custom-build/$COMPONENT_NAME/
fi

# Include host scripts
if [ -d src/host_scripts ]; then
    cp -r src/host_scripts ./custom-build/$COMPONENT_NAME/
fi

echo "✓ Component files prepared"
echo ""

# Zip up archive
echo "▶ Creating component archive..."
if run_cmd "zip -r -X ./custom-build/$COMPONENT_NAME-$ARCHITECTURE.zip ./custom-build/$COMPONENT_NAME"; then
    echo "✓ Archive created: $COMPONENT_NAME-$ARCHITECTURE.zip"
else
    ERRORS+=("Failed to create component archive")
    exit 1
fi
echo ""

# Create placeholder zip files for other architectures
echo "▶ Creating placeholder archives for other architectures..."
for arch in "aarch64" "x86_64"; do
  if [ "$arch" != "$ARCHITECTURE" ]; then
    touch ./custom-build/$COMPONENT_NAME-$arch.zip
    echo "✓ Created placeholder: $COMPONENT_NAME-$arch.zip"
  fi
done
echo ""

# Copy archives to greengrass-build
echo "▶ Copying archives to greengrass-build..."
for arch in "aarch64" "x86_64"; do
  if [ -f ./custom-build/$COMPONENT_NAME-$arch.zip ]; then
    cp ./custom-build/$COMPONENT_NAME-$arch.zip ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION/
    echo "✓ Copied $COMPONENT_NAME-$arch.zip"
  fi
done
echo ""

echo "✅ Custom build completed successfully!"
