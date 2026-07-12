#!/bin/bash
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
# Build onnxruntime-gpu from source for the OnnxRunner inference engine
# (dda_triton/resources_for_copy/inference_runtimes.py), with the CUDA and
# TensorRT execution providers, against the container's DDA python (PYTHON_VERSION, default 3.11).
#
# WHY FROM SOURCE: NVIDIA's prebuilt Jetson onnxruntime-gpu wheels target each
# JetPack's *native* python (3.8 on JP5/r35, 3.10 on JP6/r36), not the 3.9 the
# DDA backend container is standardized on. PyPI's onnxruntime-gpu wheels are
# x86_64-only. So a GPU build for aarch64 + the container cp3xx must be compiled here. The
# build links against the CUDA/cuDNN/TensorRT that ship in the l4t-jetpack base
# image, so the resulting wheel matches the device runtime.
#
# This is a LONG build (~1-2 h, RAM-hungry). It is gated in the Dockerfiles by
# the ONNXRUNTIME_GPU build-arg so routine/CPU builds stay fast.
#
# Required env:
#   JETPACK_MAJOR        5 | 6   (selects ORT version + CUDA arch defaults)
# Optional env (override the per-JetPack defaults):
#   ONNXRUNTIME_VERSION  git tag of onnxruntime to build (e.g. v1.16.3)
#   CUDA_ARCHITECTURES   semicolon list (e.g. "72;87")
#   CUDA_HOME            CUDA toolkit root (default /usr/local/cuda)
#   ORT_BUILD_JOBS       parallel build jobs (default: nproc, capped)

set -e

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Build against the DDA container interpreter. After update-alternatives, the
# python3 alternative points at PYTHON_VERSION (default 3.11), so the produced
# wheel is tagged for whatever python the container actually runs (cp3xx).
PYBIN="${PYBIN:-python3}"
JETPACK_MAJOR="${JETPACK_MAJOR:-}"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"

# ── Per-JetPack defaults (verified pairings) ───────────────────────────────
#  JP5 (L4T r35): CUDA 11.4, TensorRT 8.5.2 -> onnxruntime 1.16.3.
#                 Xavier (sm_72) + Orin (sm_87).
#  JP6 (L4T r36): CUDA 12.2, TensorRT 8.6   -> onnxruntime 1.17.1
#                 (first series with solid CUDA-12 support). Orin (sm_87).
case "$JETPACK_MAJOR" in
    5)
        ONNXRUNTIME_VERSION="${ONNXRUNTIME_VERSION:-v1.16.3}"
        CUDA_ARCHITECTURES="${CUDA_ARCHITECTURES:-72;87}"
        ;;
    6)
        ONNXRUNTIME_VERSION="${ONNXRUNTIME_VERSION:-v1.17.1}"
        CUDA_ARCHITECTURES="${CUDA_ARCHITECTURES:-87}"
        ;;
    *)
        echo "ERROR: JETPACK_MAJOR must be 5 or 6 (got '${JETPACK_MAJOR}')." >&2
        echo "       GPU onnxruntime is not supported on JetPack 4." >&2
        exit 1
        ;;
esac

# Cap parallelism: the ORT/CUDA compile is memory-heavy and OOMs with too many
# jobs on Jetson-class RAM.
NPROC=$(nproc)
ORT_BUILD_JOBS="${ORT_BUILD_JOBS:-$(( NPROC > 6 ? 6 : NPROC ))}"

echo "=========================================================="
echo " Building onnxruntime-gpu from source"
echo "   JetPack major     : ${JETPACK_MAJOR}"
echo "   onnxruntime tag   : ${ONNXRUNTIME_VERSION}"
echo "   CUDA home         : ${CUDA_HOME}"
echo "   CUDA architectures: ${CUDA_ARCHITECTURES}"
echo "   python            : $(${PYBIN} --version 2>&1)"
echo "   build jobs        : ${ORT_BUILD_JOBS}"
echo "=========================================================="

if [ ! -d "${CUDA_HOME}" ]; then
    echo "ERROR: CUDA toolkit not found at ${CUDA_HOME}. The l4t-jetpack base" >&2
    echo "       image is required (it ships CUDA/cuDNN/TensorRT dev files)." >&2
    exit 1
fi

# ── Locate cuDNN and TensorRT (Jetson multiarch layout) ────────────────────
# On Jetson the libs live in /usr/lib/aarch64-linux-gnu and headers in
# /usr/include/aarch64-linux-gnu; onnxruntime's build expects a "home" dir that
# contains both lib/ and include/. /usr is the canonical root that satisfies it.
CUDNN_HOME="${CUDNN_HOME:-/usr}"
TENSORRT_HOME="${TENSORRT_HOME:-/usr}"

# Sanity-check that the TensorRT dev headers are present (a header-less runtime
# base image would fail deep into the build instead of here).
if ! ls /usr/include/aarch64-linux-gnu/NvInfer.h >/dev/null 2>&1 \
   && ! ls /usr/include/NvInfer.h >/dev/null 2>&1; then
    echo "ERROR: TensorRT headers (NvInfer.h) not found. The base image must" >&2
    echo "       include the TensorRT dev package (l4t-jetpack does)." >&2
    exit 1
fi

# ── Build dependencies ─────────────────────────────────────────────────────
# Derive the dev-headers package from the actual container interpreter
# (e.g. python3.11 -> libpython3.11-dev) rather than hardcoding a version. The
# Dockerfile already installs python${PYTHON_VERSION}-dev; this is a safety net.
PY_MM=$(${PYBIN} -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")
echo "Installing build dependencies (python ${PY_MM})..."
apt-get update -y
apt-get install -y --no-install-recommends \
    git build-essential cmake wget ca-certificates || {
    echo "Failed to install build dependencies" >&2
    exit 1
}
if [ -n "${PY_MM}" ]; then
    apt-get install -y --no-install-recommends "libpython${PY_MM}-dev" || \
        echo "WARNING: libpython${PY_MM}-dev not available via apt; relying on Dockerfile-provided dev headers"
fi

# ── CUDA host-compiler selection ───────────────────────────────────────────
# CUDA 11.4's nvcc (JetPack 5) does NOT support GCC 11 as the host compiler:
# compiling a .cu against GCC 11's libstdc++ fails with
# "std_function.h: parameter packs not expanded with '...'". The JP5 base image
# defaults gcc to gcc-11, so for JetPack 5 we install gcc-10 and point nvcc at
# it via CMAKE_CUDA_HOST_COMPILER, leaving the image's host gcc-11 untouched
# (only nvcc's host compiler changes). JetPack 6 (CUDA 12.2) supports gcc-11,
# so it needs no override.
CUDA_HOST_COMPILER_DEFINE=""
if [ "${JETPACK_MAJOR}" = "5" ]; then
    echo "JetPack 5 / CUDA 11.4: installing gcc-10 for nvcc host compiler"
    apt-get install -y --no-install-recommends gcc-10 g++-10 || {
        echo "Failed to install gcc-10/g++-10 for the CUDA host compiler" >&2
        exit 1
    }
    CUDA_HOST_COMPILER_DEFINE="CMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-10"
fi

# onnxruntime's build driver needs CMake, but NOT CMake 4.x: ORT 1.16/1.17
# vendor third-party projects (e.g. google_nsync) that declare
# cmake_minimum_required < 3.5, and CMake 4 removed that compatibility, failing
# configure with "Compatibility with CMake < 3.5 has been removed". Pin to the
# 3.x line. (We also pass -DCMAKE_POLICY_VERSION_MINIMUM=3.5 below as a belt-
# and-suspenders for any sub-project that still trips the deprecation.)
${PYBIN} -m pip install --no-cache-dir "cmake>=3.26,<4" packaging wheel

WORK_DIR="${WORK_DIR:-/tmp/ort-build}"
rm -rf "${WORK_DIR}"
mkdir -p "${WORK_DIR}"
cd "${WORK_DIR}"

echo "Cloning onnxruntime ${ONNXRUNTIME_VERSION}..."
git clone --recursive --depth 1 --branch "${ONNXRUNTIME_VERSION}" \
    https://github.com/microsoft/onnxruntime.git
cd onnxruntime

# ── Fix eigen FetchContent SHA1 drift ──────────────────────────────────────
# ORT pins a SHA1 for the eigen archive in cmake/deps.txt, but GitLab
# re-compresses its -/archive/ zips over time, so the served bytes (hence the
# SHA1) no longer match the pinned value and the build fails with
# "Hash mismatch, removing... Each download failed!". Rather than hardcode a
# replacement hash (which drifts again and differs per ORT version), download
# the pinned URL, recompute its actual SHA1, and rewrite the eigen line in
# deps.txt to match. Self-correcting across versions/JetPacks.
if [ -f cmake/deps.txt ]; then
    # Match the DATA line (starts with "eigen;"), not the explanatory comments
    # above it that also mention the eigen URL.
    EIGEN_LINE=$(grep -E '^eigen;' cmake/deps.txt | head -1 || true)
    if [ -n "${EIGEN_LINE}" ]; then
        EIGEN_NAME=$(echo "${EIGEN_LINE}" | cut -d';' -f1)
        EIGEN_URL=$(echo "${EIGEN_LINE}" | cut -d';' -f2)
        echo "Re-pinning eigen hash from ${EIGEN_URL}"
        if wget -q -O /tmp/eigen_dep.zip "${EIGEN_URL}"; then
            EIGEN_SHA=$(sha1sum /tmp/eigen_dep.zip | cut -d' ' -f1)
            rm -f /tmp/eigen_dep.zip
            sed -i "s|^${EIGEN_NAME};.*|${EIGEN_NAME};${EIGEN_URL};${EIGEN_SHA}|" cmake/deps.txt
            echo "eigen re-pinned to SHA1 ${EIGEN_SHA}"
        else
            echo "WARNING: could not pre-fetch eigen to re-pin its hash; build may fail." >&2
        fi
    fi
fi

# ── Build: Release wheel with CUDA + TensorRT execution providers ──────────
# --build_wheel produces a pip-installable .whl tagged for the active python
# (cp3xx for the container python). CMAKE_CUDA_ARCHITECTURES restricts codegen to the target Jetson SoC(s)
# to keep build time/size down.
echo "Building (this is the long step)..."
./build.sh \
    --config Release \
    --update --build \
    --parallel "${ORT_BUILD_JOBS}" \
    --build_wheel \
    --skip_tests \
    --allow_running_as_root \
    --use_cuda --cuda_home "${CUDA_HOME}" --cudnn_home "${CUDNN_HOME}" \
    --use_tensorrt --tensorrt_home "${TENSORRT_HOME}" \
    --cmake_extra_defines \
        CMAKE_CUDA_ARCHITECTURES="${CUDA_ARCHITECTURES}" \
        CMAKE_POLICY_VERSION_MINIMUM=3.5 \
        ${CUDA_HOST_COMPILER_DEFINE} \
        onnxruntime_BUILD_UNIT_TESTS=OFF

# ── Install the produced wheel into python3.9 ──────────────────────────────
WHL=$(ls build/Linux/Release/dist/onnxruntime_gpu-*-cp*-*aarch64*.whl 2>/dev/null | head -1)
if [ -z "${WHL}" ]; then
    echo "ERROR: build produced no aarch64 onnxruntime_gpu wheel." >&2
    ls -R build/Linux/Release/dist/ 2>/dev/null || true
    exit 1
fi
echo "Installing ${WHL}"
# Stage a copy of the built wheel outside the (soon-deleted) build tree so a
# successful long build is preserved in the image for inspection/reuse.
mkdir -p /opt/onnxruntime-wheels
cp "${WHL}" /opt/onnxruntime-wheels/ || true
# Remove any CPU onnxruntime first so the GPU build is authoritative (the two
# packages must not coexist).
${PYBIN} -m pip uninstall -y onnxruntime onnxruntime-gpu >/dev/null 2>&1 || true
${PYBIN} -m pip install --no-cache-dir "${WHL}"

# ── Verify the CUDA/TensorRT providers are present ─────────────────────────
# Run from a neutral directory: the onnxruntime source tree contains an
# `onnxruntime/` package dir that would shadow the *installed* package (whose
# compiled onnxruntime.capi lives in site-packages), causing a spurious
# "No module named 'onnxruntime.capi'". cd / avoids the shadow.
cd /
${PYBIN} - <<'PYEOF'
import onnxruntime as ort
provs = ort.get_available_providers()
print("onnxruntime", ort.__version__, "providers:", provs)
need = {"CUDAExecutionProvider", "TensorrtExecutionProvider"}
missing = need - set(provs)
if missing:
    raise SystemExit(f"ERROR: GPU providers missing from build: {missing}")
print("GPU execution providers present.")
PYEOF

# Clean up the (large) build tree to keep the image small.
cd /
rm -rf "${WORK_DIR}"
echo "onnxruntime-gpu installation completed successfully"
