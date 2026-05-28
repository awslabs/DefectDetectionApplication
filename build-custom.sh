#!/bin/bash
set -e
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

if [ $# -ne 3 ]; then
  echo 1>&2 "Usage: $0 COMPONENT-NAME COMPONENT-VERSION ARCH"
  exit 3
fi

COMPONENT_NAME=$1
VERSION=$2
ARCHITECTURE=$3

BUILDKIT_PROGRESS=plain
export BUILDKIT_PROGRESS

# Detect Ubuntu version from host
IMAGE_VER=$(grep "DISTRIB_RELEASE" /etc/lsb-release | cut -d'=' -f2)
export IMAGE_VER
echo "Ubuntu version: $IMAGE_VER"

# Determine if this is a JP5 build based on component name
IS_JP5=0
if echo "$COMPONENT_NAME" | grep -q "JP5"; then
    IS_JP5=1
fi

echo "Architecture: $ARCHITECTURE"
echo "JetPack 5: $IS_JP5"

# copy recipe to greengrass-build
cp recipe.yaml ./greengrass-build/recipes

# create custom build directory
rm -rf ./custom-build
mkdir -p ./custom-build/$COMPONENT_NAME

# build Docker images
cd src

# edgemlsdk build
cd edgemlsdk/
if [ "$IS_JP5" = "1" ]; then
    ./build.sh -p $ARCHITECTURE -u $IMAGE_VER -y 3.9 -j 5
else
    ./build.sh -p $ARCHITECTURE -u $IMAGE_VER -y 3.9
fi
cd ..

# Copy edgemlsdk artifacts to backend build context
mkdir -p backend/edgemlsdk
# Clean stale debs from previous builds
rm -f backend/edgemlsdk/*.deb backend/edgemlsdk/*.whl backend/edgemlsdk/*.tar.gz

# Copy debs/tars from extracted-debs directory (populated by build.sh)
EXTRACTED_DIR=$(pwd)/edgemlsdk/extracted-debs
if [ ! -d "$EXTRACTED_DIR/debs" ]; then
    echo "ERROR: extracted-debs/debs not found at $EXTRACTED_DIR"
    echo "The edgemlsdk build.sh should have extracted debs to this directory."
    exit 1
fi
echo "Copying debs from $EXTRACTED_DIR..."
cp $EXTRACTED_DIR/debs/PanoramaSDK.deb $(pwd)/backend/edgemlsdk/
cp $EXTRACTED_DIR/debs/aws-c-iot.deb $(pwd)/backend/edgemlsdk/
cp $EXTRACTED_DIR/debs/aws-crt-cpp.deb $(pwd)/backend/edgemlsdk/
cp $EXTRACTED_DIR/debs/aws-iot-device-sdk-cpp-v2.deb $(pwd)/backend/edgemlsdk/
cp $EXTRACTED_DIR/debs/aws-sdk-cpp.deb $(pwd)/backend/edgemlsdk/
cp $EXTRACTED_DIR/debs/libgstreamer-plugins-base1.0-dev.deb $(pwd)/backend/edgemlsdk/
cp $EXTRACTED_DIR/debs/libgstreamer1.0-dev.deb $(pwd)/backend/edgemlsdk/
cp $EXTRACTED_DIR/debs/libgstreamer1.0.deb $(pwd)/backend/edgemlsdk/
cp $EXTRACTED_DIR/debs/liborc-0.4-0.deb $(pwd)/backend/edgemlsdk/
cp $EXTRACTED_DIR/debs/libstdc++6.deb $(pwd)/backend/edgemlsdk/
cp $EXTRACTED_DIR/debs/openssl.deb $(pwd)/backend/edgemlsdk/
cp $EXTRACTED_DIR/debs/panorama.whl $(pwd)/backend/edgemlsdk/panorama-1.0-py3-none-any.whl
cp $EXTRACTED_DIR/debs/triton-core.deb $(pwd)/backend/edgemlsdk/
cp $EXTRACTED_DIR/debs/triton-python-backend.deb $(pwd)/backend/edgemlsdk/
cp $EXTRACTED_DIR/tars/triton_installation_files.tar.gz $(pwd)/backend/edgemlsdk/
echo "Done copying binaries"

# Verify all required artifacts are present
for f in aws-c-iot.deb aws-crt-cpp.deb aws-iot-device-sdk-cpp-v2.deb aws-sdk-cpp.deb \
         PanoramaSDK.deb openssl.deb liborc-0.4-0.deb triton-core.deb \
         triton-python-backend.deb panorama-1.0-py3-none-any.whl triton_installation_files.tar.gz; do
    if [ ! -f "$(pwd)/backend/edgemlsdk/$f" ]; then
        echo "ERROR: Required file backend/edgemlsdk/$f not found after copy"
        exit 1
    fi
done
echo "All required edgemlsdk artifacts verified"

# Build backend and frontend Docker images
# Select the correct Dockerfile for the backend
if [ "$IS_JP5" = "1" ]; then
    export BACKEND_DOCKERFILE="Dockerfile.jp5"
else
    export BACKEND_DOCKERFILE="Dockerfile"
fi
export OS=$IMAGE_VER

echo "Building backend with $BACKEND_DOCKERFILE..."
docker-compose --profile generic -f docker-compose.yaml build --no-cache
docker-compose --profile tegra -f docker-compose.yaml build --no-cache
cd ..

# save Docker images separately for Greengrass (each artifact must be < 2GB)
echo "Saving docker images as separate artifacts..."
docker save react-webapp | gzip > ./custom-build/$COMPONENT_NAME/react-webapp.tar.gz

FLASK_TAR="./custom-build/$COMPONENT_NAME/flask-app.tar"
docker save flask-app --output "$FLASK_TAR"
FLASK_SIZE=$(stat --format=%s "$FLASK_TAR")
echo "flask-app.tar size: $(numfmt --to=iec $FLASK_SIZE)"

echo "Image sizes:"
ls -lh ./custom-build/$COMPONENT_NAME/*.tar* 

# include docker-compose.yaml in archive
cp src/docker-compose.yaml ./custom-build/$COMPONENT_NAME/

# include empty directories for each image build context
mkdir -p ./custom-build/$COMPONENT_NAME/backend
mkdir -p ./custom-build/$COMPONENT_NAME/frontend
mkdir -p ./custom-build/$COMPONENT_NAME/host_scripts
mkdir -p ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION/

# include dio script that triggers output
cp src/backend/triggers/outputs/dio.py ./custom-build/$COMPONENT_NAME/
cp -r src/host_scripts ./custom-build/$COMPONENT_NAME/

# Package as separate artifacts to stay under 2GB Greengrass limit
# Artifact 1: backend Docker image (large)
echo "Creating backend artifact..."
zip -r -X ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION/$COMPONENT_NAME-backend-$ARCHITECTURE.zip \
    ./custom-build/$COMPONENT_NAME/flask-app.tar

# Artifact 2: frontend image + scripts + compose (small)
echo "Creating frontend+scripts artifact..."
zip -r -X ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION/$COMPONENT_NAME-app-$ARCHITECTURE.zip \
    ./custom-build/$COMPONENT_NAME/react-webapp.tar.gz \
    ./custom-build/$COMPONENT_NAME/docker-compose.yaml \
    ./custom-build/$COMPONENT_NAME/host_scripts \
    ./custom-build/$COMPONENT_NAME/dio.py \
    ./custom-build/$COMPONENT_NAME/backend \
    ./custom-build/$COMPONENT_NAME/frontend

echo "Artifact sizes:"
ls -lh ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION/*.zip

# Check if any artifact exceeds 2GB
for zipfile in ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION/*.zip; do
    SIZE=$(stat --format=%s "$zipfile")
    if [ "$SIZE" -gt 2147483648 ]; then
        echo "NOTE: $zipfile is $(numfmt --to=iec $SIZE) - exceeds 2GB Greengrass artifact limit."
        echo "The publish script will use ECR-based deployment instead."
    fi
done
