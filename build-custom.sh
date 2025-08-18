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

if [ $# -ne 2 ]; then
  echo 1>&2 "Usage: $0 COMPONENT-NAME COMPONENT-VERSION"
  exit 3
fi

COMPONENT_NAME=$1
VERSION=$2
ARCHITECTURE=`uname -m`
# change to 20.04 or 18.04
# TODO add 20.04 for JP5
#IMAGE_VER="18.04"
IMAGE_VER="20.04"

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
./build.sh -p $(uname -m) -u $IMAGE_VER 3.9
cd ..
echo copying $id
id=$(docker create edgemlsdk)
docker cp $id:/debs/PanoramaSDK.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/aws-c-iot.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/aws-crt-cpp.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/aws-iot-device-sdk-cpp-v2.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/aws-sdk-cpp.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/libgstreamer-plugins-base1.0-dev.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/libgstreamer1.0-dev.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/libgstreamer1.0.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/liborc-0.4-0.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/libstdc++6.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/openssl.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/panorama.whl $(pwd)/backend/edgemlsdk/panorama-1.0-py3-none-any.whl
docker cp $id:/debs/triton-core.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/debs/triton-python-backend.deb $(pwd)/backend/edgemlsdk/
docker cp $id:/tars/triton_installation_files.tar.gz  $(pwd)/backend/edgemlsdk/
docker rm -v $id
echo done copying binaries
# rest of the application
docker-compose --profile tegra --profile generic -f docker-compose.yaml build --no-cache
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
for arch in "aarch64" "x86_64"; do
  touch $COMPONENT_NAME-$arch.zip
  mv $COMPONENT_NAME-$arch.zip ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION/
done

# copy archive to greengrass-build
cp ./custom-build/$COMPONENT_NAME-$ARCHITECTURE.zip ./greengrass-build/artifacts/$COMPONENT_NAME/$VERSION/