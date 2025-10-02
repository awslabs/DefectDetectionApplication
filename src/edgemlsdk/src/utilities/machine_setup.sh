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

if [ "$EUID" -ne 0 ]; then
    do_sudo=sudo;
else
    do_sudo=;
fi

check_and_install_package() {
    local package_name=$1
    if ! dpkg -s "$package_name" >/dev/null 2>&1; then
        echo "Package '$package_name' is not installed. Installing..."
        $do_sudo apt-get install -y "$package_name"
        if [ $? -eq 0 ]; then
            echo "Package '$package_name' has been successfully installed."
        else
            echo "Failed to install package '$package_name'."
        fi
    else
        echo "Package '$package_name' is already installed."
    fi
}

check_and_install_python_module() {
    local module_name=$1
    if ! python -c "import $module_name" >/dev/null 2>&1; then
        echo "Python module '$module_name' is not installed. Installing..."
        python3 -m pip install "$module_name"
        if [ $? -eq 0 ]; then
            echo "Python module '$module_name' has been successfully installed."
        else
            echo "Failed to install Python module '$module_name'."
        fi
    else
        echo "Python module '$module_name' is already installed."
    fi

    source ~/.profile
}

install_cmake()
{
    if ! dpkg -s "cmake" >/dev/null 2>&1; then
        . /etc/os-release
        if [ $VERSION_ID = "18.04" ]; then
            apt-get clean all;
            apt-get install gpg wget -y;
            wget -O - https://apt.kitware.com/keys/kitware-archive-latest.asc 2>/dev/null | gpg --dearmor - | tee /etc/apt/trusted.gpg.d/kitware.gpg >/dev/null;
            apt-add-repository "deb https://apt.kitware.com/ubuntu/ $(lsb_release -cs) main";
            apt-key adv --recv-keys --keyserver hkp://keyserver.ubuntu.com:80  6AF7F09730B3F0A4;
	    apt-key adv --keyserver keyserver.ubuntu.com --recv-keys 16FAAD7AF99A65E2
            apt-get update;
            apt-get install kitware-archive-keyring;
            rm /etc/apt/trusted.gpg.d/kitware.gpg;
            apt-get update;
            apt-get install cmake -y;
        else
            check_and_install_package cmake;
        fi
    else
        echo "CMake is already installed"
    fi
}

install_powershell()
{
    if [ $(uname -m) = "x86_64" ];
    then
        pwsh_arch="x64"
    else
        pwsh_arch="arm64"
    fi

    if command -v pwsh > /dev/null 2>&1; then
        echo "Powershell already installed"
    else
        curl -L -o /tmp/powershell.tar.gz https://github.com/PowerShell/PowerShell/releases/download/v7.2.3/powershell-7.2.3-linux-$pwsh_arch.tar.gz
        $do_sudo mkdir -p /opt/microsoft/powershell/7
        $do_sudo tar zxf /tmp/powershell.tar.gz -C /opt/microsoft/powershell/7
        $do_sudo chmod +x /opt/microsoft/powershell/7/pwsh
        $do_sudo ln -s /opt/microsoft/powershell/7/pwsh /usr/bin/pwsh
    fi
}

install_swig()
{
    if command -v swig > /dev/null 2>&1; then
       echo "Swig already installed"
    else
        mkdir ~/swig
        cd ~/swig
        wget http://prdownloads.sourceforge.net/swig/swig-4.0.2.tar.gz
        mkdir swig
        tar -xzvf swig-4.0.2.tar.gz -C ./swig
        rm swig-4.0.2.tar.gz
        cd ./swig/swig-4.0.2
        ./configure
        make
        $do_sudo make install
    fi
}

apt-get update

# Install Packages
check_and_install_package software-properties-common
check_and_install_package lsb-release
check_and_install_package curl
check_and_install_package libcurl4-openssl-dev
check_and_install_package libssl-dev
check_and_install_package python3
check_and_install_package libgstreamer1.0-dev
check_and_install_package libgstreamer-plugins-base1.0-dev
check_and_install_package libgstreamer-plugins-bad1.0-dev
check_and_install_package gstreamer1.0-plugins-base
check_and_install_package gstreamer1.0-plugins-good
check_and_install_package gstreamer1.0-plugins-bad
check_and_install_package gstreamer1.0-plugins-ugly
check_and_install_package gstreamer1.0-libav
check_and_install_package gstreamer1.0-tools 
check_and_install_package gstreamer1.0-x
check_and_install_package gstreamer1.0-alsa
check_and_install_package gstreamer1.0-gl
check_and_install_package gstreamer1.0-gtk3
check_and_install_package gstreamer1.0-qt5
check_and_install_package libgstrtspserver-1.0-dev
check_and_install_package libgirepository1.0-dev
check_and_install_package doxygen
check_and_install_package python3-sphinx
check_and_install_package python3-pip
check_and_install_package ninja-build
install_cmake
install_powershell
install_swig

# Install Python Modules
check_and_install_python_module conan
check_and_install_python_module sphinx-toolbox
check_and_install_python_module breathe
check_and_install_python_module myst_parser
check_and_install_python_module boto3
check_and_install_python_module awscrt

# Install CBS-CLI and configure (Will give errors if already run, can be ignored)
toolbox registry add s3://cbs-toolbox-498039791012-us-west-2/tools.json
toolbox install cbs-cli
cbs_configure
conan profile detect
