#!/bin/bash
#
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

python3 -m pip install boto3
python3 -m pip install scikit-learn==1.0.2
python3 -m pip install dill
# Requirement(s) of LyraScienceProcessingUtils


arch="$(uname -m)"

# Install EdgeML SDK packages (skip conflicting GStreamer packages)
dpkg -i edgemlsdk/aws-c-iot.deb
dpkg -i edgemlsdk/aws-crt-cpp.deb
dpkg -i edgemlsdk/aws-iot-device-sdk-cpp-v2.deb
dpkg -i edgemlsdk/aws-sdk-cpp.deb
# Skip GStreamer packages - already installed in base system
# dpkg -i edgemlsdk/libgstreamer-plugins-base1.0-dev.deb
# dpkg -i edgemlsdk/libgstreamer-plugins-base1.0.deb
# dpkg -i edgemlsdk/libgstreamer1.0-dev.deb
# dpkg -i edgemlsdk/libgstreamer1.0.deb
dpkg -i edgemlsdk/liborc-0.4-0.deb
# dpkg -i edgemlsdk/libstdc++6.deb  # Skip - conflicts with gcc-13-base
dpkg -i edgemlsdk/openssl.deb
dpkg -i edgemlsdk/triton-core.deb
dpkg -i edgemlsdk/triton-python-backend.deb
dpkg -i edgemlsdk/PanoramaSDK.deb

# Extract Triton installation files
tar xvfz edgemlsdk/triton_installation_files.tar.gz -C /opt/

# Install Panorama wheel
python3 -m pip install edgemlsdk/panorama-1.0-py3-none-any.whl

# Patch triton stubs with correct install
LOCATION=$(find / -name libtritonserver.so | grep tritonserver/install/lib/stubs/libtritonserver.so)
if [ -n "$LOCATION" ]; then
    cp /opt/tritonserver/lib/libtritonserver.so "$LOCATION"
fi