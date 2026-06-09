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
elif [ -f /usr/local/cuda/version.json ]; then
    # CUDA 11+/12 (JetPack 6) dropped version.txt in favor of version.json and
    # the full toolkit (nvcc) is often not installed. Prefer the cuda_cudart
    # runtime version (e.g. 12.2.140) which matches nvcc-style output; fall back
    # to the top-level "cuda" SDK version.
    JETSON_CUDA=$(grep -A2 '"cuda_cudart"' /usr/local/cuda/version.json 2>/dev/null | grep -m1 '"version"' | sed -E 's/.*"version" *: *"([0-9]+\.[0-9]+\.[0-9]+).*/\1/')
    if [ -z "$JETSON_CUDA" ]; then
        JETSON_CUDA=$(grep -A3 '"cuda"' /usr/local/cuda/version.json 2>/dev/null | grep -m1 '"version"' | sed -E 's/.*"version" *: *"([0-9]+\.[0-9]+).*/\1/')
    fi
    if [ -n "$JETSON_CUDA" ]; then
        is_gpu=1
    else
        JETSON_CUDA="NOT_INSTALLED"
    fi
else
    # Fall back to the L4T CUDA runtime package (nvidia-l4t-cuda) which is
    # present on JetPack devices even without the full toolkit.
    JETSON_CUDA_PKG=$(dpkg -l 2>/dev/null | grep -m1 -E "cuda-cudart-[0-9]|nvidia-l4t-cuda")
    if [ -n "$JETSON_CUDA_PKG" ] && [ $has_dpkg -eq 1 ]; then
        JETSON_CUDA=$(echo "$JETSON_CUDA_PKG" | awk '{print $3}' | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n1)
        [ -n "$JETSON_CUDA" ] && is_gpu=1 || JETSON_CUDA="NOT_INSTALLED"
    else
        JETSON_CUDA="NOT_INSTALLED"
    fi
fi
echo "JETSON_CUDA=${JETSON_CUDA}" >> /tmp/.dda.env

# Extract cuDNN version
JETSON_CUDNN=$(dpkg -l 2>/dev/null | grep -m1 "libcudnn")
if [ ! -z "$JETSON_CUDNN" ] && [ $has_dpkg -eq 1 ]; then
    JETSON_CUDNN=$(echo $JETSON_CUDNN | sed 's/.*libcudnn[0-9] \([^ ]*\).*/\1/' | cut -d '-' -f1 )
else
    JETSON_CUDNN="NOT_INSTALLED"
fi

# Export NVIDIA CuDNN Library
echo "JETSON_CUDNN=${JETSON_CUDNN}" >> /tmp/.dda.env

JETSON_MODEL="UNKNOWN"
# Extract jetson model name
if [ -f /sys/firmware/devicetree/base/model ]; then
    JETSON_MODEL=$(tr -d '\0' < /sys/firmware/devicetree/base/model)
    JETSON_MODEL=${JETSON_MODEL// /_}
fi
echo "JETSON_MODEL=\"${JETSON_MODEL}\"" >> /tmp/.dda.env

# Extract jetson chip id
JETSON_CHIP_ID=""
if [ -f /sys/module/tegra_fuse/parameters/tegra_chip_id ]; then
    JETSON_CHIP_ID=$(cat /sys/module/tegra_fuse/parameters/tegra_chip_id)
    JETSON_CHIP_ID=${JETSON_CHIP_ID// /_}
else
    JETSON_CHIP_ID="NOT_AVAILABLE"
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
fi
echo "JETSON_L4T=${JETSON_L4T_RELEASE}.${JETSON_L4T_REVISION}" >> /tmp/.dda.env

# Check libnvinfer
JETSON_NVINFER=$(dpkg -l 2>/dev/null | grep -m1 "libnvinfer-bin")
if [ ! -z "$JETSON_NVINFER" ] && [ $has_dpkg -eq 1 ]; then
    JETSON_NVINFER=$(echo $JETSON_NVINFER | sed 's/.*libnvinfer-bin \([^ ]*\).*/\1/' )
else
    JETSON_NVINFER="NOT_INSTALLED"
fi
echo "JETSON_NVINFER=${JETSON_NVINFER}" >> /tmp/.dda.env

# Check for nvidia-container-toolkit
JETSON_CONTAINER_TOOLKIT=$(dpkg -l 2>/dev/null | grep -m1 "nvidia-container-toolkit")
if [ ! -z "$JETSON_CONTAINER_TOOLKIT" ] && [ $has_dpkg -eq 1 ]; then
    JETSON_CONTAINER_TOOLKIT=$(echo $JETSON_CONTAINER_TOOLKIT | sed 's/.*nvidia-container-toolkit \([^ ]*\).*/\1/' | cut -d '-' -f1 )
else
    JETSON_CONTAINER_TOOLKIT="NOT_INSTALLED"
fi
echo "JETSON_CONTAINER_TOOLKIT=${JETSON_CONTAINER_TOOLKIT}" >> /tmp/.dda.env

# Check for nvidia-container-runtime
JETSON_CONTAINER_RUNTIME=$(dpkg -l 2>/dev/null | grep -m1 "nvidia-container-runtime")
if [ ! -z "$JETSON_CONTAINER_RUNTIME" ] && [ $has_dpkg -eq 1 ]; then
    JETSON_CONTAINER_RUNTIME=$(echo $JETSON_CONTAINER_RUNTIME | sed 's/.*nvidia-container-runtime \([^ ]*\).*/\1/' | cut -d '-' -f1)
else
    JETSON_CONTAINER_RUNTIME="NOT_INSTALLED"
fi
echo "JETSON_CONTAINER_RUNTIME=${JETSON_CONTAINER_RUNTIME}" >> /tmp/.dda.env

# Extract TensorRT version
JETSON_TENSORRT=$(dpkg -l 2>/dev/null | grep -m1 " tensorrt ")
if [ ! -z "$JETSON_TENSORRT" ] && [ $has_dpkg -eq 1 ]; then
    JETSON_TENSORRT=$(echo $JETSON_TENSORRT | sed 's/.*tensorrt \([^ ]*\).*/\1/' | cut -d '-' -f1 )
elif [ $has_dpkg -eq 1 ]; then
    # JetPack 6 frequently ships TensorRT as the libnvinfer* packages without
    # the top-level `tensorrt` metapackage. Derive the TRT version from
    # libnvinfer-bin (preferred) or the libnvinfer runtime lib package.
    JETSON_TRT_PKG=$(dpkg -l 2>/dev/null | grep -m1 -E "^ii +libnvinfer-bin ")
    [ -z "$JETSON_TRT_PKG" ] && JETSON_TRT_PKG=$(dpkg -l 2>/dev/null | grep -m1 -E "^ii +libnvinfer[0-9]+ ")
    if [ -n "$JETSON_TRT_PKG" ]; then
        # Version looks like 8.6.2.3-1+cuda12.2 → take 8.6.2 (drop build/cuda suffix).
        JETSON_TENSORRT=$(echo "$JETSON_TRT_PKG" | awk '{print $3}' | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+' | head -n1)
        [ -z "$JETSON_TENSORRT" ] && JETSON_TENSORRT="NOT_INSTALLED"
    else
        JETSON_TENSORRT="NOT_INSTALLED"
    fi
else
    JETSON_TENSORRT="NOT_INSTALLED"
fi
echo "JETSON_TENSORRT=${JETSON_TENSORRT}" >> /tmp/.dda.env

# Profile decision is driven by HARDWARE presence, not by optional package
# version strings. The earlier version flipped is_gpu=0 whenever any of the
# informational dpkg lookups above failed to match (cuDNN, libnvinfer-bin,
# nvidia-container-toolkit/runtime, tensorrt), which incorrectly selected the
# 'generic' profile on working Jetsons.
#
# The authoritative signal that this host is a CUDA-capable Jetson is the L4T
# marker file /etc/nv_tegra_release, which is present on ALL JetPack releases
# (JP4 r32.x, JP5 r35.x, JP6 r36.x). We rely on that rather than on the exact
# CUDA toolkit layout (CUDA 12 on JP6 dropped /usr/local/cuda/version.txt, and
# nvcc is only present with the full toolkit) or on a specific driver path
# (/usr/lib/aarch64-linux-gnu/tegra/libcuda.so* moved on JP6). GPU access inside
# the container is provided by the NVIDIA Container Runtime (`runtime: nvidia`
# in docker-compose), which injects the correct driver libraries for whatever
# JetPack version is running, so we don't need the driver libs at a fixed path
# on the host to choose the tegra profile.
#
# Fallbacks: also treat the host as GPU-capable if CUDA was detected (is_gpu==1
# from nvcc/version.txt above) or if libcuda.so* exists in the known JP4/JP5
# Tegra driver dir — so non-standard installs still resolve correctly.
TEGRA_DRIVER_DIR="/usr/lib/aarch64-linux-gnu/tegra"
if [ "$arch" = "aarch64" ] && { \
        [ -f /etc/nv_tegra_release ] || \
        [ "$is_gpu" -eq 1 ] || \
        ls "$TEGRA_DRIVER_DIR"/libcuda.so* >/dev/null 2>&1; }; then
    is_gpu=1
else
    # Not a CUDA-capable aarch64 Jetson.
    is_gpu=0
fi

# Use the GPU (tegra) profile when this is an aarch64 Jetson with CUDA present.
# This covers both JetPack 4 (Xavier, L4T r32.x) and JetPack 5 (Orin, L4T r35.x);
# the JP5-specific backend image is selected at build time (Dockerfile.jp5), while
# the runtime profile is the same `tegra` profile that mounts the CUDA libraries.
# NOTE: Orin is intentionally NOT disabled here — the old "disable gpu for orin"
# guard was for a bug that no longer applies, and disabling it left the container
# without the CUDA mounts.
if [ $is_gpu -eq 1 ] && [ $arch = "aarch64" ]; then
    echo DOCKER_PROFILE='tegra' >> /tmp/.dda.env
else
    echo DOCKER_PROFILE='generic' >> /tmp/.dda.env
fi
