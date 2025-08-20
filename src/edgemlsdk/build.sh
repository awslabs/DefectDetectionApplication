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
 
 
echo "Begin building Docker image."
docker build --build-arg OS=$ubuntu --build-arg PLATFORM=$platform --build-arg PWSH_ARCH=$pwsh_arch --build-arg PYTHON_VERSION=$python -t edgemlsdk .
popd