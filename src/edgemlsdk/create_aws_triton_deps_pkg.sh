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

CreateDebFromInstallManifest(){
    package=$1
    version=$2
    if test $(uname -m) = "aarch64"; then
        arch="arm64"
    else
        arch="amd64"
    fi

    mkdir -p $package/DEBIAN

    touch $package/DEBIAN/control
    echo "Package: "$package > $package/DEBIAN/control
    echo "Version: "$version >> $package/DEBIAN/control
    echo "Section: base" >> $package/DEBIAN/control
    echo "Priority: optional" >> $package/DEBIAN/control
    echo "Architecture: "$arch >> $package/DEBIAN/control
    echo "Maintainer: EdgeML SDK Devs" >> $package/DEBIAN/control
    echo "Description: Precompiled "$package" libraries used for building the Panorama V2 SDK" >> $package/DEBIAN/control
    
    rsync -arR --files-from=$3 / ./$package
    dpkg-deb --build $package
}

CreateDebFromInstallManifest aws-crt-cpp 0.19.7 /dependencies/aws-crt-cpp/build/install_manifest.txt
CreateDebFromInstallManifest aws-sdk-cpp 1.11.21 /dependencies/aws-sdk-cpp/build/install_manifest.txt
CreateDebFromInstallManifest aws-c-iot 0.1.11 /dependencies/aws-c-iot/build/install_manifest.txt
CreateDebFromInstallManifest aws-iot-device-sdk-cpp-v2 1.20.3 /dependencies/aws-iot-device-sdk-cpp-v2/build/install_manifest.txt
CreateDebFromInstallManifest triton-core 2.45.0 /dependencies/server/build/tritonserver/build/install_manifest.txt
CreateDebFromInstallManifest triton-python-backend 2.45.0 /dependencies/server/build/python/build/install_manifest.txt