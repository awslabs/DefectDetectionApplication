# Task 3.1 SPIKE — Findings (BUILD-SERVER-ONLY)

**Decision gate verdict: 🟢 GREEN — the Triton `tensorrt` backend CAN be compiled
on this toolchain.** Every dev dependency is installable from a single reachable
repo, the Triton `v2.45.0` build wiring is confirmed, and all paths are verified.
The design's representative wiring is correct with two refinements noted below.

## Build host actually used for the spike

- AWS EC2 **Graviton (aarch64)**, `ip-172-31-38-82`, kernel `5.4.0-1154-aws`.
- Ubuntu **18.04.6 LTS** (bionic), matches the generic Dockerfile `OS=18.04`.
- Docker 24.0.2 available.
- **NOT a Jetson/Tegra device**: no `/etc/nv_tegra_release`, no L4T apt source,
  no CUDA, no TensorRT, no `libnvinfer` present, and `nvidia-l4t-*` packages are
  absent. So the dev toolchain must be sourced from the NVIDIA L4T apt repo at
  build time (option "add the L4T apt repo", NOT "preconfigured JP4.6 host").

## (a) Dev-package install commands + repo/key wiring  — CONFIRMED

The **NVIDIA L4T r32.7 `common`** apt repo is reachable from this build server
and is signed (detached `Release.gpg` present). The `common` dist carries the
full CUDA 10.2 + TensorRT 8.2.1 dev set; the `t194` dist is **not** needed.

Repo + key (add inside the Dockerfile, gated on `ENABLE_TENSORRT_BACKEND=1`):

```bash
# GPG key (ASCII-armored, 5409 bytes, verified downloadable)
wget -qO - https://repo.download.nvidia.com/jetson/jetson-ota-public.asc \
  | gpg --dearmor | tee /etc/apt/trusted.gpg.d/jetson-ota-public.gpg >/dev/null
# Repo (dist = r32.7, component = main)
echo "deb https://repo.download.nvidia.com/jetson/common r32.7 main" \
  > /etc/apt/sources.list.d/nvidia-l4t.list
apt-get update
```

Install (CONFIRMED present, exact versions in (b)). Two equally valid options:

```bash
# Minimal (recommended — avoids cuda-documentation/cuda-samples bloat):
apt-get install -y --no-install-recommends \
  cuda-nvcc-10-2 cuda-cudart-dev-10-2 cuda-libraries-dev-10-2 \
  libnvinfer-dev libnvinfer-plugin-dev libnvonnxparsers-dev
```
```bash
# Or the design's full-toolkit form (also works, larger image):
apt-get install -y \
  cuda-toolkit-10-2 \
  libnvinfer-dev libnvinfer-plugin-dev libnvonnxparsers-dev
```

**Dependency-closure de-risk (key result):** the entire transitive closure
(`libnvinfer8`, `libnvinfer-plugin8`, `libnvonnxparsers8`, `libcudnn8`,
`libcudnn8-dev`, `libcublas10`, `libcublas-dev`, `cuda-nvrtc[-dev]-10-2`,
`cuda-cudart[-dev]-10-2`, `cuda-driver-dev-10-2`, etc.) resolves **entirely
within the `common` r32.7 repo**, and there are **zero `nvidia-l4t-*`
dependencies anywhere in that repo** (`grep -c nvidia-l4t Packages` = 0). So the
dev headers/libs install cleanly into a plain Ubuntu 18.04 aarch64 image with no
Tegra-runtime packages required at build time. (Runtime `libnvinfer.so.8.2.1` is
still host-injected on the device via `tensorrt.csv` as designed.)

## (b) Exact dev-package names + versions  — CONFIRMED

| package | version |
|---|---|
| `libnvinfer-dev` | `8.2.1-1+cuda10.2` |
| `libnvinfer-plugin-dev` | `8.2.1-1+cuda10.2` |
| `libnvonnxparsers-dev` | `8.2.1-1+cuda10.2` |
| `libnvinfer8` | `8.2.1-1+cuda10.2` |
| `tensorrt` (meta) | `8.2.1.9-1+cuda10.2` |
| `cuda-toolkit-10-2` | `10.2.460-1` |
| `cuda-nvcc-10-2` | `10.2.300-1` |
| `cuda-cudart-dev-10-2` | `10.2.300-1` |
| `cuda-libraries-dev-10-2` | `10.2.460-1` |
| `libcudnn8` / `libcudnn8-dev` | `8.2.1.32-1+cuda10.2` |
| `libcublas10` / `libcublas-dev` | `10.2.3.300-1` |

This is exactly TensorRT 8.2.1 + CUDA 10.2 as the design assumed.

**Verified install locations (from deb file lists, no install performed):**
- TensorRT headers: `/usr/include/aarch64-linux-gnu/NvInfer.h`
- TensorRT lib: `/usr/lib/aarch64-linux-gnu/libnvinfer.so` → `libnvinfer.so.8.2.1`
- nvcc / CUDA root: `/usr/local/cuda-10.2/bin/nvcc` (toolkit root `/usr/local/cuda-10.2`)

## (c) build.py / cmake args to add  — CONFIRMED against Triton `v2.45.0`

Verified directly against `server@v2.45.0/build.py` and
`tensorrt_backend@r24.04/CMakeLists.txt` (the backend branch build.py clones for
v2.45.0 via `backend_repo("tensorrt")` → `tensorrt_backend`).

- `--enable-gpu` is a real flag → sets `TRITON_ENABLE_GPU=ON` for core **and**
  every backend. **Mandatory:** the tensorrt backend CMake hard-fails otherwise
  (`FATAL_ERROR "TensorRT backend requires TRITON_ENABLE_GPU=1"`).
- `--backend tensorrt` → clones+builds `tensorrt_backend` (needs build-time
  network, same as the existing `server` clone).
- The tensorrt backend's CMake exposes exactly these cache vars (design's names
  are correct):
  - `TRITON_TENSORRT_INCLUDE_PATHS` (added as PRIVATE include dir)
  - `TRITON_TENSORRT_LIB_PATHS` (added as `-L` link dirs)
  - It also does `find_library(NVINFER_LIBRARY NAMES nvinfer)` +
    `find_library(NVINFER_PLUGIN_LIBRARY NAMES nvinfer_plugin)` and
    `find_package(CUDAToolkit REQUIRED)`.
- **On Linux, `build.py`'s `tensorrt_cmake_args()` passes NO TensorRT path args**
  (it only sets them on Windows). Discovery therefore relies on standard system
  paths — which the L4T debs satisfy (`/usr/lib/aarch64-linux-gnu`,
  `/usr/include/aarch64-linux-gnu`). Passing the paths explicitly is cheap
  insurance and recommended.
- Arg format CONFIRMED: `--extra-backend-cmake-arg=<backend>:<NAME>=<VALUE>` and
  `--extra-core-cmake-arg=<NAME>=<VALUE>`.

**Concrete TRT_ARGS to append to the existing `build.py` call when enabled:**

```
--enable-gpu \
--backend tensorrt \
--extra-backend-cmake-arg=tensorrt:TRITON_TENSORRT_INCLUDE_PATHS=/usr/include/aarch64-linux-gnu \
--extra-backend-cmake-arg=tensorrt:TRITON_TENSORRT_LIB_PATHS=/usr/lib/aarch64-linux-gnu
```

Plus make CUDA discoverable for BOTH the core (TRITON_ENABLE_GPU=ON →
`find_package(CUDAToolkit)`) and the backend. Cleanest is to export before the
`build.py` line (only when enabled):

```
export PATH=/usr/local/cuda-10.2/bin:$PATH
export CUDAToolkit_ROOT=/usr/local/cuda-10.2   # FindCUDAToolkit hint (CMake 3.21)
```

The existing core cmake args (PYBIND11/PYTHON_*, `CMAKE_POLICY_VERSION_MINIMUM`,
`TRITON_ENABLE_ENSEMBLE`) and `--backend python` are **kept unchanged**. When
`ENABLE_TENSORRT_BACKEND=0`, `TRT_ARGS` is empty and the call is byte-for-byte
today's (x86_64 preserved).

### ⚠️ Two refinements for 3.2 to confirm during the actual compile (5.1)

1. **Memory tracker / iGPU.** `target_platform()` returns `platform.system()`
   = `"linux"` on this non-Jetson host (it does NOT auto-detect iGPU). With
   `--enable-gpu` on `linux`, build.py sets `TRITON_ENABLE_MEMORY_TRACKER=ON`
   for the tensorrt backend. The Jetson-correct behavior disables it. If the
   compile/runtime errors on the memory tracker, the fix is to add
   `--extra-backend-cmake-arg=tensorrt:TRITON_ENABLE_MEMORY_TRACKER=OFF`
   (preferred — surgical) **or** build with `--target-platform igpu` (broader,
   also flips other backends/core to the Jetson path). Start without it; add the
   surgical OFF only if the memory-tracker link fails. This is the single most
   likely compile snag and is contained.
2. **Build-time network.** `--backend tensorrt` clones `tensorrt_backend`
   (r24.04) at build time; ensure the build server has GitHub egress (it already
   clones `server`, so this is expected to be fine).

## (d) Staging source path  — CONFIRMED

build.py builds each backend under `build/<be>/install/backends/<be>`
(verified: `repo_install_dir = build_dir/<be>/install`, backend lands in
`repo_install_dir/backends/<be>`). The existing python move proves the pattern:

```
# existing (python):  build/python/install/backends/python  -> tritonserver/install/backends
# new (tensorrt):     build/tensorrt/install/backends/tensorrt -> tritonserver/install/backends
```

So the gated staging move for 3.2 is (mirroring the existing python move's
destination exactly):

```dockerfile
RUN mv /dependencies/server/build/python/install/backends/python \
       /dependencies/server/build/tritonserver/install/backends && \
    if [ "$ENABLE_TENSORRT_BACKEND" = "1" ]; then \
      mv /dependencies/server/build/tensorrt/install/backends/tensorrt \
         /dependencies/server/build/tritonserver/install/backends ; \
    fi
```

Result: `tritonserver/install/backends/tensorrt` (containing
`libtriton_tensorrt.so`), carried through by the existing
`triton_installation_files.tar.gz` packaging unchanged → lands at
`/opt/tritonserver/backends/tensorrt` on the device. No new backend Dockerfile.

## Notes confirming the rest of the design

- The two in-repo patches (`edgeml-triton-server.diff`, `edgeml-triton-core.diff`)
  only add `-static-libstdc++` and skip core unit tests — orthogonal to GPU/
  TensorRT. Retain unchanged.
- No full image build was run (per task constraint). Findings come from repo
  reachability checks, deb metadata/file-list inspection, and reading the
  pinned `v2.45.0` build.py + `r24.04` tensorrt_backend CMakeLists.
