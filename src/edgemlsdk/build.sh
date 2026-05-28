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
ubuntu=$(grep "DISTRIB_RELEASE" /etc/lsb-release | cut -d'=' -f2)
jetpack=""
BUILDKIT_PROGRESS=plain
export BUILDKIT_PROGRESS
export ubuntu

echo "Ubuntu version: $ubuntu"
while getopts p:y:u:j: flag
do
    case "${flag}" in
        p) platform=${OPTARG};;
        y) python=${OPTARG};;
        u) ubuntu=${OPTARG};;
        j) jetpack=${OPTARG};;
    esac
done

echo "Platform=$platform"
echo "Python=$python"
echo "Ubuntu=$ubuntu"
echo "JetPack=$jetpack"

if [ $platform = "x86_64" ]; then
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

# Select Dockerfile based on JetPack version
if [ "$jetpack" = "5" ]; then
    DOCKERFILE="Dockerfile.jp5"
    echo "Using JP5 Dockerfile (l4t-jetpack:r35.4.1 base, native build)"
else
    DOCKERFILE="Dockerfile"
    echo "Using standard Dockerfile (Ubuntu ${ubuntu} base)"
fi

echo "Begin building Docker image. For OS=$ubuntu platform=$platform arch=$pwsh_arch dockerfile=$DOCKERFILE"

# Build the edgemlsdk image
docker build \
    --build-arg OS=$ubuntu \
    --build-arg PLATFORM=$platform \
    --build-arg PWSH_ARCH=$pwsh_arch \
    --build-arg PYTHON_VERSION=$python \
    -f $DOCKERFILE \
    -t edgemlsdk .

# Extract debs/tars from the built image using docker cp
mkdir -p $rootDir/extracted-debs/debs $rootDir/extracted-debs/tars
CONTAINER_ID=$(docker create edgemlsdk /bin/true)
docker cp $CONTAINER_ID:/debs/. $rootDir/extracted-debs/debs/
docker cp $CONTAINER_ID:/tars/. $rootDir/extracted-debs/tars/
docker rm $CONTAINER_ID

echo "Extraction complete. Checking extracted files..."
ls -la $rootDir/extracted-debs/debs/ 2>/dev/null || echo "WARNING: No debs found in extracted-debs/"
ls -la $rootDir/extracted-debs/tars/ 2>/dev/null || echo "WARNING: No tars found in extracted-debs/"

# Cache the openssl deb for future builds
if ls $rootDir/extracted-debs/debs/openssl*.deb 1>/dev/null 2>&1; then
    mkdir -p $rootDir/cached-debs
    cp $rootDir/extracted-debs/debs/openssl*.deb $rootDir/cached-debs/
    echo "Cached openssl deb to $rootDir/cached-debs/ for future builds"
fi

popd
