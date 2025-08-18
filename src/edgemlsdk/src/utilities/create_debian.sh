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

arch=$(uname -m)
if test "$arch" = "aarch64"; then
    architecture="arm64"
else
    architecture="amd64"
fi
version=$(<"version")

# Check if the include and lib directories exist
if [ ! -d "include" ] || [ ! -d "lib" ]; then
  echo "Error: Cannot find include or lib directory."
  exit 1
fi

# Create the package directory structure
pkg_name="Panorama"
pkg_dir="${pkg_name}_$version"
mkdir -p "${pkg_dir}/DEBIAN"
mkdir -p "${pkg_dir}/usr/include"
mkdir -p "${pkg_dir}/usr/lib"
mkdir -p "${pkg_dir}/usr/bin"

# Copy header files to the package directory
cp -r include/* "${pkg_dir}/usr/include"

# Copy library files to the package directory
cp -r lib/* "${pkg_dir}/usr/lib"

# Copy executables to the bin directory
cp -r bin/* "${pkg_dir}/usr/bin"

# Create the control file
cat << EOF > "${pkg_dir}/DEBIAN/control"
Package: ${pkg_name}
Version: $version
Section: libs
Priority: optional
Architecture: $architecture
Maintainer: Aws Panorama
Description: SDK for Panorama
EOF

# Set correct permissions for the control file
chmod 755 "${pkg_dir}/DEBIAN/control"

# Build the Debian package
dpkg-deb --build "${pkg_dir}"

echo "Debian package created: ${pkg_name}_$version.deb"
