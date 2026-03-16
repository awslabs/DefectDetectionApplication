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

# Reading CUDA and TensorRT versions based on https://github.com/jetsonhacks/jetsonUtilities
# Read CUDA version
arch=$(uname -m)
is_gpu=0
has_dpkg=1
if ! command -v dpkg &> /dev/null
then
    has_dpkg=0
fi

if [ -f /usr/local/cuda/bin/nvcc ]; then
    JETSON_CUDA=$(/usr/local/cuda/bin/nvcc --version | egrep -o "V[0-9]+.[0-9]+.[0-9]+" | cut -c2-)
    is_gpu=1
elif [ -f /usr/local/cuda/version.txt ]; then
    JETSON_CUDA=$(cat /usr/local/cuda/version.txt | sed 's/\CUDA Version //g')
    is_gpu=1
else
    JETSON_CUDA="NOT_INSTALLED"
fi
echo "JETSON_CUDA=${JETSON_CUDA}" >> /tmp/.dda.env

# Extract cuDNN version
JETSON_CUDNN=$(dpkg -l 2>/dev/null | grep -m1 "libcudnn")
if [ ! -z "$JETSON_CUDNN" ] && [ $has_dpkg -eq 1 ]; then
    JETSON_CUDNN=$(echo $JETSON_CUDNN | sed 's/.*libcudnn[0-9] \([^ ]*\).*/\1/' | cut -d '-' -f1 )
else
    JETSON_CUDNN="NOT_INSTALLED"
    is_gpu=0
fi

# Export NVIDIA CuDNN Library
echo "JETSON_CUDNN=${JETSON_CUDNN}" >> /tmp/.dda.env

JETSON_MODEL="UNKNOWN"
# Extract jetson model name
if [ -f /sys/firmware/devicetree/base/model ]; then
    JETSON_MODEL=$(tr -d '\0' < /sys/firmware/devicetree/base/model)
    JETSON_MODEL=${JETSON_MODEL// /_}
else
    is_gpu=0
fi
echo "JETSON_MODEL=\"${JETSON_MODEL}\"" >> /tmp/.dda.env

# Extract jetson chip id
JETSON_CHIP_ID=""
if [ -f /sys/module/tegra_fuse/parameters/tegra_chip_id ]; then
    JETSON_CHIP_ID=$(cat /sys/module/tegra_fuse/parameters/tegra_chip_id)
    JETSON_CHIP_ID=${JETSON_CHIP_ID// /_}
else
    JETSON_CHIP_ID="NOT_AVAILABLE"
    is_gpu=0
fi
echo "JETSON_CHIP_ID=\"${JETSON_CHIP_ID}\"" >> /tmp/.dda.env

# Ectract type board
JETSON_SOC=""
if [ -f /proc/device-tree/compatible ]; then
    # Extract the last part of name
    JETSON_SOC=$(tr -d '\0' < /proc/device-tree/compatible | sed -e 's/.*,//')
    JETSON_SOC=${JETSON_SOC// /_}
else
    JETSON_SOC="NOT_AVAILABLE"
    is_gpu=0
fi
echo "JETSON_SOC=\"${JETSON_SOC}\"" >> /tmp/.dda.env

if [ -f /etc/nv_tegra_release ]; then
    # L4T string
    # First line on /etc/nv_tegra_release 
    # - "# R28 (release), REVISION: 2.1, GCID: 11272647, BOARD: t186ref, EABI: aarch64, DATE: Thu May 17 07:29:06 UTC 2018"
    JETSON_L4T_STRING=$(head -n 1 /etc/nv_tegra_release)
    # Load release and revision
    JETSON_L4T_RELEASE=$(echo $JETSON_L4T_STRING | cut -f 2 -d ' ' | grep -Po '(?<=R)[^;]+')
    JETSON_L4T_REVISION=$(echo $JETSON_L4T_STRING | cut -f 2 -d ',' | grep -Po '(?<=REVISION: )[^;]+')
else
    # Load release and revision
    JETSON_L4T_RELEASE="N"
    JETSON_L4T_REVISION="N.N"
    is_gpu=0
fi
echo "JETSON_L4T=${JETSON_L4T_RELEASE}.${JETSON_L4T_REVISION}" >> /tmp/.dda.env

# Check libnvinfer
JETSON_NVINFER=$(dpkg -l 2>/dev/null | grep -m1 "libnvinfer-bin")
if [ ! -z "$JETSON_NVINFER" ] && [ $has_dpkg -eq 1 ]; then
    JETSON_NVINFER=$(echo $JETSON_NVINFER | sed 's/.*libnvinfer-bin \([^ ]*\).*/\1/' )
else
    JETSON_NVINFER="NOT_INSTALLED"
    is_gpu=0
fi
echo "JETSON_NVINFER=${JETSON_NVINFER}" >> /tmp/.dda.env

# Check for nvidia-container-toolkit
JETSON_CONTAINER_TOOLKIT=$(dpkg -l 2>/dev/null | grep -m1 "nvidia-container-toolkit")
if [ ! -z "$JETSON_CONTAINER_TOOLKIT" ] && [ $has_dpkg -eq 1 ]; then
    JETSON_CONTAINER_TOOLKIT=$(echo $JETSON_CONTAINER_TOOLKIT | sed 's/.*nvidia-container-toolkit \([^ ]*\).*/\1/' | cut -d '-' -f1 )
else
    JETSON_CONTAINER_TOOLKIT="NOT_INSTALLED"
    is_gpu=0
fi
echo "JETSON_CONTAINER_TOOLKIT=${JETSON_CONTAINER_TOOLKIT}" >> /tmp/.dda.env

# Check for nvidia-container-runtime
JETSON_CONTAINER_RUNTIME=$(dpkg -l 2>/dev/null | grep -m1 "nvidia-container-runtime")
if [ ! -z "$JETSON_CONTAINER_RUNTIME" ] && [ $has_dpkg -eq 1 ]; then
    JETSON_CONTAINER_RUNTIME=$(echo $JETSON_CONTAINER_RUNTIME | sed 's/.*nvidia-container-runtime \([^ ]*\).*/\1/' | cut -d '-' -f1)
else
    JETSON_CONTAINER_RUNTIME="NOT_INSTALLED"
    is_gpu=0
fi
echo "JETSON_CONTAINER_RUNTIME=${JETSON_CONTAINER_RUNTIME}" >> /tmp/.dda.env

# Extract TensorRT version
JETSON_TENSORRT=$(dpkg -l 2>/dev/null | grep -m1 " tensorrt ")
if [ ! -z "$JETSON_TENSORRT" ] && [ $has_dpkg -eq 1 ]; then
    JETSON_TENSORRT=$(echo $JETSON_TENSORRT | sed 's/.*tensorrt \([^ ]*\).*/\1/' | cut -d '-' -f1 )
else
    JETSON_TENSORRT="NOT_INSTALLED"
    is_gpu=0
fi
echo "JETSON_TENSORRT=${JETSON_TENSORRT}" >> /tmp/.dda.env

#Disable gpu for orin container , this file found only on orin devices for now.
if [ -f /sys/devices/soc0/soc_id ]; then
    is_gpu=0
fi
#Use gpu profile based on architecture and CUDA presence
if [ $is_gpu -eq 1 ] && [ $arch = "aarch64" ]; then
    echo DOCKER_PROFILE='tegra' >> /tmp/.dda.env
elif [ $is_gpu -eq 1 ] && [ $arch = "x86_64" ]; then
    echo DOCKER_PROFILE='x86_cuda' >> /tmp/.dda.env
else
    echo DOCKER_PROFILE='generic' >> /tmp/.dda.env
fi
