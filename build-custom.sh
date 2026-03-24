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

if [ $# -ne 3 ]; then
  echo 1>&2 "Usage: $0 COMPONENT-NAME COMPONENT-VERSIONi ARCH"
  exit 3
fi

COMPONENT_NAME=$1
VERSION=$2

ARCHITECTURE=`uname -m`
ARCHITECTURE=$3
# change to 20.04 or 18.04
# TODO add 20.04 for JP5
IMAGE_VER="18.04"
#IMAGE_VER="20.04"
BUILDKIT_PROGRESS=plain
export BUILDKIT_PROGRESS
IMAGE_VER=$(grep "DISTRIB_RELEASE" /etc/lsb-release | cut -d'=' -f2)

# Export as environment variable
export IMAGE_VER 

echo "Ubuntu version: $IMAGE_VER"
# copy recipe to greengrass-build
cp recipe.yaml ./greengrass-build/recipes

# create custom build directory
rm -rf ./custom-build
mkdir -p ./custom-build/$COMPONENT_NAME

# build Docker images
# to save build time, remove "--no-cache" parameter
cd src
#edgemlsdk
cd edgemlsdk/
./build.sh -p $ARCHITECTURE -u $IMAGE_VER 3.9 || { echo "edgemlsdk build failed"; exit 1; }
cd ..
mkdir -p backend/edgemlsdk
cp -r edgemlsdk backend/edgemlsdk
# Clean stale debs from previous builds
rm -f backend/edgemlsdk/*.deb backend/edgemlsdk/*.whl backend/edgemlsdk/*.tar.gz
# Copy debs/tars from extracted-debs directory (populated by build.sh --output type=local)
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
echo done copying binaries
# rest of the application - build sequentially to avoid OOM during compilation
docker-compose --profile generic -f docker-compose.yaml build --build-arg OS=$IMAGE_VER --no-cache
docker-compose --profile tegra -f docker-compose.yaml build --build-arg OS=$IMAGE_VER --no-cache
cd ..
# save Docker images as tar
echo "save docker images as tarvballs"
docker save --output ./custom-build/$COMPONENT_NAME/flask-app.tar flask-app
docker save --output ./custom-build/$COMPONENT_NAME/react-webapp.tar react-webapp

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

# zip up archive
zip -r -X ./custom-build/$COMPONENT_NAME-$ARCHITECTURE.zip ./custom-build/$COMPONENT_NAME

# dev test, create temp zip file for supported architecture not in development
#for arch in "aarch64" "x86_64"; do
touch $COMPONENT_NAME-aarch64.zip
mv $COMPONENT_NAME-aarch64.zip ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION/
touch $COMPONENT_NAME-x86_64.zip
mv $COMPONENT_NAME-x86_64.zip ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION/
#done

# copy archive to greengrass-build
cp ./custom-build/$COMPONENT_NAME-$ARCHITECTURE.zip ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION/
