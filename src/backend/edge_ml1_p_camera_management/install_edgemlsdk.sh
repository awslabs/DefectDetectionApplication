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

python3.9 -m pip install boto3
python3.9 -m pip install scikit-learn==1.0.2
python3.9 -m pip install dill
#python3.9 -m pip install edge_ml1_p_camera_management/wheels/LyraAnomaliesMaskUtils-1.0-py3-none-any.whl
#python3.9 -m pip install edge_ml1_p_camera_management/wheels/LyraScienceProcessingUtils-1.0-py3-none-any.whl
# Requirement(s) of LyraScienceProcessingUtils


arch="$(uname -m)"

# Install EdgeML SDK packages (skip conflicting GStreamer packages)
# Check if edgemlsdk directory exists and has .deb files
if [ -d edgemlsdk ] && [ -f edgemlsdk/aws-c-iot.deb ]; then
    echo "Installing EdgeML SDK dependencies..."
    dpkg -i edgemlsdk/aws-c-iot.deb || echo "Warning: Failed to install aws-c-iot.deb"
    dpkg -i edgemlsdk/aws-crt-cpp.deb || echo "Warning: Failed to install aws-crt-cpp.deb"
    dpkg -i edgemlsdk/aws-iot-device-sdk-cpp-v2.deb || echo "Warning: Failed to install aws-iot-device-sdk-cpp-v2.deb"
    dpkg -i edgemlsdk/aws-sdk-cpp.deb || echo "Warning: Failed to install aws-sdk-cpp.deb"
    # Skip GStreamer packages - already installed in base system
    # dpkg -i edgemlsdk/libgstreamer-plugins-base1.0-dev.deb
    # dpkg -i edgemlsdk/libgstreamer-plugins-base1.0.deb
    # dpkg -i edgemlsdk/libgstreamer1.0-dev.deb
    # dpkg -i edgemlsdk/libgstreamer1.0.deb
    dpkg -i edgemlsdk/liborc-0.4-0.deb || echo "Warning: Failed to install liborc-0.4-0.deb"
    # dpkg -i edgemlsdk/libstdc++6.deb  # Skip - conflicts with gcc-13-base
    dpkg -i edgemlsdk/openssl.deb || echo "Warning: Failed to install openssl.deb"
    dpkg -i edgemlsdk/triton-core.deb || echo "Warning: Failed to install triton-core.deb"
    dpkg -i edgemlsdk/triton-python-backend.deb || echo "Warning: Failed to install triton-python-backend.deb"
    dpkg -i edgemlsdk/PanoramaSDK.deb || echo "Warning: Failed to install PanoramaSDK.deb"

    # Extract Triton installation files if they exist
    if [ -f edgemlsdk/triton_installation_files.tar.gz ]; then
        tar xvfz edgemlsdk/triton_installation_files.tar.gz -C /opt/ || echo "Warning: Failed to extract triton_installation_files.tar.gz"
    fi

    # Install Panorama wheel if it exists
    if [ -f edgemlsdk/panorama-1.0-py3-none-any.whl ]; then
        python3.9 -m pip install edgemlsdk/panorama-1.0-py3-none-any.whl || echo "Warning: Failed to install panorama wheel"
    fi

    # Patch triton stubs with correct install if available
    LOCATION=$(find / -name libtritonserver.so 2>/dev/null | grep tritonserver/install/lib/stubs/libtritonserver.so | head -1)
    if [ -n "$LOCATION" ] && [ -f /opt/tritonserver/lib/libtritonserver.so ]; then
        cp /opt/tritonserver/lib/libtritonserver.so "$LOCATION" || echo "Warning: Failed to patch triton stubs"
    fi
else
    echo "EdgeML SDK dependencies not found. Skipping EdgeML SDK installation."
    echo "This is expected if edgemlsdk was not built. The application will run without EdgeML SDK support."
fi