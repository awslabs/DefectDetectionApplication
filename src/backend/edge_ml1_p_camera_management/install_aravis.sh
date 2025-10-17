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

set -e  # Exit on any error

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
cd $SCRIPT_DIR

# echo "Adding Debian repository..."
# echo "deb http://archive.debian.org/debian buster-backports main" >> /etc/apt/sources.list || {
#     echo "Failed to add repository"
#     exit 1
# }

echo "Updating package lists..."
apt-get update -y || {
    echo "Failed to update package lists"
    exit 1
}

echo "Installing wget and build tools..."
apt-get install -y --no-install-recommends wget build-essential ninja-build meson || {
    echo "Failed to install build tools"
    exit 1
}

echo "Downloading Aravis..."
wget https://github.com/AravisProject/aravis/releases/download/0.8.35/aravis-0.8.35.tar.xz || {
    echo "Failed to download Aravis"
    exit 1
}

echo "Extracting Aravis..."
tar xf aravis-0.8.35.tar.xz || {
    echo "Failed to extract Aravis"
    exit 1
}

echo "Installing dependencies..."
apt install -y --no-install-recommends gstreamer1.0-plugins-bad build-essential ninja-build \
    libxml2-dev libglib2.0-dev libusb-1.0-0-dev gobject-introspection \
    libgtk-3-dev gtk-doc-tools xsltproc libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev gstreamer1.0-plugins-good \
    libgirepository1.0-dev gettext pkg-config libcairo2-dev || {
    echo "Failed to install dependencies"
    exit 1
}

apt update -y
ldconfig

echo "Building Aravis..."
cd aravis-0.8.35 || {
    echo "Failed to enter aravis directory"
    exit 1
}

meson setup -Dprefix=/usr build || {
    echo "Failed to setup meson build"
    exit 1
}

cd build || {
    echo "Failed to enter build directory"
    exit 1
}

ninja || {
    echo "Failed to build with ninja"
    exit 1
}

ninja install || {
    echo "Failed to install with ninja"
    exit 1
}

ldconfig
echo "Aravis installation completed successfully"
