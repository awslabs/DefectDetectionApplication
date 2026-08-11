# scripts/build-target-derivation.sh
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
#
# Sourceable build-target derivation helper (spec: jetpack7-support).
#
# This file is the single source of truth for the component-name -> build
# target derivation used by build-custom.sh. It is extracted into its own
# sourceable file so the derivation is testable (property tests source this
# file and call the functions directly) without running a build.
#
#   source scripts/build-target-derivation.sh
#   derive_build_target "$COMPONENT_NAME"   # sets/exports the target vars
#   resolve_onnxruntime_gpu                 # sets/exports ONNXRUNTIME_GPU
#
# Derivation table (token containment on the component name; first match in
# the chain wins, preserving the original build-custom.sh if/elif precedence):
#
#   token    | flags            | JETPACK_ARG | BACKEND_DOCKERFILE       | ORT GPU default
#   ---------+------------------+-------------+--------------------------+----------------
#   JP6      | IS_JP6=1         | "6"         | Dockerfile.jp6           | 1
#   JP5      | IS_JP5=1         | "5"         | Dockerfile.jp5           | 1
#   JP7      | IS_JP7=1         | "7"         | Dockerfile.jp7           | 1
#   Nvidia   | IS_X86_NVIDIA=1  | ""          | Dockerfile.x86_64_nvidia | 1
#   (none)   | all 0            | ""          | Dockerfile               | 0 (forced)
#
# JP5/JP6/Nvidia rows and the no-token default row are byte-for-byte the
# semantics previously inlined in build-custom.sh; JP7 is the added row.

# Determine the JetPack target from the component name.
# JP5 components are named with "JP5" (e.g. aws.edgeml.dda.LocalServer.arm64JP5),
# JP6 components with "JP6" (e.g. aws.edgeml.dda.LocalServer.arm64JP6),
# JP7 components with "JP7" (e.g. aws.edgeml.dda.LocalServer.arm64JP7).
# x86 NVIDIA components are named with "Nvidia"
# (e.g. aws.edgeml.dda.LocalServer.amd64Nvidia): x86_64 hosts with the NVIDIA
# GPU runtime — CUDA-based backend image + GPU onnxruntime, no JetPack.
#
# Also selects the backend Dockerfile: JP5 uses an L4T r35.x base, JP6 an
# L4T r36.x base, JP7 a CUDA 13.0 Ubuntu 24.04 arm64 base (Thor / L4T r38.x),
# x86 NVIDIA a CUDA x86 base (Dockerfile.x86_64_nvidia), and everything else
# the default CPU-only Dockerfile.
derive_build_target() {
  local component_name="$1"

  IS_JP5=0
  IS_JP6=0
  IS_JP7=0
  IS_X86_NVIDIA=0
  JETPACK_ARG=""

  # Token containment, first match wins. `case "$name" in *JP6*)` is the same
  # substring test as the original `echo "$name" | grep -q "JP6"` (none of the
  # tokens contain regex metacharacters) but is robust for arbitrary strings.
  case "$component_name" in
    *JP6*)
      IS_JP6=1
      JETPACK_ARG="6"
      export BACKEND_DOCKERFILE="Dockerfile.jp6"
      ;;
    *JP5*)
      IS_JP5=1
      JETPACK_ARG="5"
      export BACKEND_DOCKERFILE="Dockerfile.jp5"
      ;;
    *JP7*)
      IS_JP7=1
      JETPACK_ARG="7"
      export BACKEND_DOCKERFILE="Dockerfile.jp7"
      ;;
    *Nvidia*)
      IS_X86_NVIDIA=1
      export BACKEND_DOCKERFILE="Dockerfile.x86_64_nvidia"
      ;;
    *)
      export BACKEND_DOCKERFILE="Dockerfile"
      ;;
  esac
}

# ── GPU ONNX Runtime (JP5/JP6/JP7/x86 NVIDIA) ───────────────────────────────
# The OnnxRunner uses a GPU (CUDA/TensorRT) onnxruntime in the backend image.
# GPU is enabled by default on JetPack 5, 6 and 7 (built from source, ~1-2h,
# can be turned off for a fast CPU-only build with ONNXRUNTIME_GPU=0) and on
# the x86 NVIDIA target (Dockerfile.x86_64_nvidia installs the prebuilt x86_64
# onnxruntime-gpu wheel — PyPI ships GPU wheels for x86_64 only, so no source
# build is needed there). JetPack 4 stays CPU-only (its native python 3.6 has
# no compatible build path) and plain x86 uses the CPU wheel.
#
# Must be called after derive_build_target (reads the IS_* flags). Honors the
# existing ONNXRUNTIME_GPU env opt-out on GPU targets (ONNXRUNTIME_GPU=0 in
# the environment overrides the default of 1); non-GPU targets are forced to
# 0 regardless of the environment, exactly as before.
resolve_onnxruntime_gpu() {
  if [ "$IS_JP6" = "1" ] || [ "$IS_JP5" = "1" ] || [ "$IS_JP7" = "1" ] || [ "$IS_X86_NVIDIA" = "1" ]; then
    export ONNXRUNTIME_GPU="${ONNXRUNTIME_GPU:-1}"
  else
    export ONNXRUNTIME_GPU=0
  fi
}
