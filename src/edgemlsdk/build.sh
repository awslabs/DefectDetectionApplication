#!/bin/bash
#
#
# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#

platform=$(uname -m)
python=3.9
ubuntu=20.04
ubuntu=$(grep "DISTRIB_RELEASE" /etc/lsb-release | cut -d'=' -f2)
BUILDKIT_PROGRESS=plain
export BUILDKIT_PROGRESS
# Export as environment variable
export ubuntu

echo "Ubuntu version: $UBUNTU_VERSION" 
while getopts p:y:u: flag
do
    case "${flag}" in
        p) platform=${OPTARG};;
        y) python=${OPTARG};;
        u) ubuntu=${OPTARG};;
    esac
done
 
echo "Platform=$platform"
echo "Python=$python"
echo "Ubuntu=$ubuntu"
 
if [ $platform = "x86_64" ];
then
    pwsh_arch="x64"
else
    pwsh_arch="arm64"
fi
 
rootDir="$( cd -- "$( dirname -- "${BASH_SOURCE[0]:-$0}"; )" &> /dev/null && pwd 2> /dev/null; )";
pushd $rootDir

# Clean previous extraction
rm -rf $rootDir/extracted-debs

# Ensure cached-debs directory exists (may be empty on first build)
mkdir -p $rootDir/cached-debs

echo "Begin building Docker image. For OS=$ubuntu platform=$platform arch=$pwsh_arch"

# Step 1: Build the full edgemlsdk image (cached by buildx)
docker buildx build --platform linux/arm64 \
    --build-arg OS=$ubuntu \
    --build-arg PLATFORM=$platform \
    --build-arg PWSH_ARCH=$pwsh_arch \
    --build-arg PYTHON_VERSION=$python \
    -t edgemlsdk .

# Step 2: Extract debs/tars from the built image using docker cp
# Using docker cp instead of --output type=local to avoid lchown permission errors
# Note: we create from the builder stage since the extractor stage (FROM scratch) has no command
mkdir -p $rootDir/extracted-debs/debs $rootDir/extracted-debs/tars
CONTAINER_ID=$(docker create --platform linux/arm64 edgemlsdk /bin/true)
docker cp $CONTAINER_ID:/debs/. $rootDir/extracted-debs/debs/
docker cp $CONTAINER_ID:/tars/. $rootDir/extracted-debs/tars/
docker rm $CONTAINER_ID

echo "Extraction complete. Checking extracted files..."
ls -la $rootDir/extracted-debs/debs/ 2>/dev/null || echo "WARNING: No debs found in extracted-debs/"
ls -la $rootDir/extracted-debs/tars/ 2>/dev/null || echo "WARNING: No tars found in extracted-debs/"

# Cache the openssl deb for future builds (skips the slow QEMU compilation next time)
if ls $rootDir/extracted-debs/debs/openssl*.deb 1>/dev/null 2>&1; then
    mkdir -p $rootDir/cached-debs
    cp $rootDir/extracted-debs/debs/openssl*.deb $rootDir/cached-debs/
    echo "Cached openssl deb to $rootDir/cached-debs/ for future builds"
fi

popd
