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

pushd $pwd
mkdir ~/aws_dependencies
cd ~/aws_dependencies

arch=$(uname -m)
git clone --recurse-submodules https://github.com/awslabs/aws-crt-cpp.git
cd aws-crt-cpp
git checkout 6158ecefd0e0d53b2cf5e9d09f8c42f57e741e35
mkdir build
cd build
cmake -GNinja -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=/usr/ -DCMAKE_INSTALL_PREFIX=/usr/ -DUSE_OPENSSL=ON -DBUILD_SHARED_LIBS=ON ../
sudo ninja install

cd ~/aws_dependencies
git clone --recurse-submodules https://github.com/aws/aws-sdk-cpp
cd aws-sdk-cpp
git checkout c63eb9e5f059ae2595cae8c79732c6938ca36b7d
mkdir build
cd build
cmake -GNinja -DCMAKE_MODULE_PATH=/usr/lib/$arch-linux-gnu/cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=/usr/ -DCMAKE_INSTALL_PREFIX=/usr/ -DBUILD_DEPS=OFF -DBUILD_ONLY="s3;sts;secretsmanager;sagemaker-edge;transfer;iot;iot-data" ../
sudo ninja install

cd ~/aws_dependencies
git clone --recurse-submodules https://github.com/awslabs/aws-c-iot.git
cd aws-c-iot
git checkout 6e59b4660fecbc20c74493aee7da8a7b486b2966
mkdir build
cd build
cmake -GNinja -DCMAKE_PREFIX_PATH=/usr/ -DCMAKE_INSTALL_PREFIX=/usr/ -DBUILD_SHARED_LIBS=ON -DCMAKE_BUILD_TYPE="Release" ../
sudo ninja install

cd ~/aws_dependencies
git clone --recursive https://github.com/aws/aws-iot-device-sdk-cpp-v2.git
cd aws-iot-device-sdk-cpp-v2
git checkout f7cbf0982d2f23902406a0ba696a1a900b732a7e
git submodule update --init --recursive
mkdir build
cd build
cmake -GNinja -DCMAKE_MODULE_PATH=/usr/lib/$arch-linux-gnu/cmake -DCMAKE_INSTALL_PREFIX=/usr/ -DBUILD_SHARED_LIBS=ON -DBUILD_DEPS=OFF -DCMAKE_BUILD_TYPE="Release" -DBUILD_TESTING=OFF ../
sudo ninja install
popd