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

version=$(<"version")
python_version=$(python3 --version 2>&1 | awk '{print $2}')
ubuntu_version=$(lsb_release -rs)
aws s3 cp *.deb s3://panorama-sdk-v2-artifacts/release/$version/$(uname -m)/$ubuntu_version/$python_version/
aws s3 cp *.deb s3://panorama-sdk-v2-artifacts/release/latest/$(uname -m)/$ubuntu_version/$python_version/PanoramaSDK.deb

aws s3 cp ./lib/python_package/dist/*.whl s3://panorama-sdk-v2-artifacts/release/$version/$(uname -m)/$ubuntu_version/$python_version/
aws s3 cp ./lib/python_package/dist/*.whl s3://panorama-sdk-v2-artifacts/release/latest/$(uname -m)/$ubuntu_version/$python_version/

if [ -d "./sphinx" ]; then
    major_minor=$(echo "$version" | cut -d'.' -f1,2)
    aws s3 sync ./sphinx s3://edgeml-sdk-docs/edgeml-sdk/v1/$major_minor/
fi