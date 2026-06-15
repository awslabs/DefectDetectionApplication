# JP4 TensorRT build-support — investigation pivot (root-cause correction)

**Status:** the original fix approach (compile the Triton `tensorrt` backend in the
from-source build — "Option C") was implemented, hit a hard blocker, and was
**reverted** (revert of `bdadb56`). The spec needs re-hypothesis before any further
implementation. The build is back to the known-working **python-backend-only**
Triton v2.45.0 configuration.

## Why Option C was abandoned

1. **CUDA 10.2 cannot compile Triton v2.45.0's GPU code.** The build failed with
   `nvcc fatal: Value 'c++17' is not defined for option 'std'` while compiling a
   CUDA device object (`kernel_library_new_generated_kernel.cu`). CUDA 10.2's nvcc
   supports at most C++14; the modern Triton GPU/backend build requires C++17. A
   secondary failure (`caffe2plan.cc: NvCaffeParser.h: No such file`) is a missing
   deprecated Caffe-parser dev header. Both stem from compiling a 2024-era Triton
   GPU backend against the JP4.6 (L4T r32.7) CUDA 10.2 toolchain.

2. **The `tensorrt` backend was NEVER part of this build** — git evidence:
   the pre-Python-3.11 edgemlsdk Dockerfile (commit `55f1482`) built Triton
   **v2.45.0 with `--backend python` only** (no `--enable-gpu`, no
   `--backend tensorrt`), staging only `.../backends/python`. The Python-3.11
   migration (`bef33ec`) kept that unchanged. There is no `triton-tensorrt-backend.deb`
   or prebuilt tensorrt backend anywhere. So "how did JP4 work before?" → it never
   built the tensorrt backend and never hit this wall.

## Corrected root-cause hypothesis (for re-hypothesis)

The JP4 segmentation `base_model` is a **python-backend** model. Its `model.py`
runs the Neo-compiled engine via **DLR** (`libdlr.so`), which links
`libnvinfer.so.8` **at runtime**. The observed device failure was:

```
OSError: libdlr.so could not be loaded: libnvinfer.so.8: cannot open shared object file
```

This is a **runtime TensorRT-library availability** problem on the device
(`libnvinfer.so.8` is host-injected via the NVIDIA Container Runtime / `tensorrt.csv`),
NOT a missing Triton `tensorrt` backend and NOT a build problem. The image does not
need (and never had) the Triton `tensorrt` backend for this path.

## Recommended next steps (device-side, DEVICE-ONLY)

The deployed **1.0.116** image (python-only Triton + the CWD-independent lyra import
fix) is the correct build. The remaining fix is on the JP4.6 device:

- Confirm the container runs with `--runtime nvidia` (or the Greengrass/compose
  equivalent) so the host JetPack TensorRT is injected.
- Confirm `tensorrt.csv` injects `libnvinfer.so.8(.2.1)` and that it is on the
  loader path for the python+DLR stub (`ldconfig -p | grep nvinfer`,
  `find / -name 'libnvinfer.so.8*'`).
- Confirm the device JetPack provides TensorRT 8.x (`libnvinfer.so.8`), matching
  the models' Neo compile target (TRT 8.2.1). An older JP4 (TRT 7 → `libnvinfer.so.7`)
  would not satisfy `libnvinfer.so.8`.

The routing/preservation PBT tests and the spike findings from the abandoned approach
are in git history at commit `bdadb56` if needed for reference.

---

## Device verification (refines the root cause; rules out profile mis-selection)

On-device diagnosis on the JP4.6 Xavier NX (running component `aws.edgeml.dda.LocalServer.arm64` 1.0.116, RUNNING) confirms the runtime-injection hypothesis **and** narrows it: the failure is NOT profile mis-selection — the device is correctly on the `tegra` profile — it is that the NVIDIA Container Runtime CSV injection is not delivering TensorRT into the container.

### Verified facts
- Profile selection is correct: `/tmp/.dda.env` has `DOCKER_PROFILE=tegra` (host detected as Jetson: `JETSON_L4T=32.7.6`, `JETSON_CUDA=10.2.300`, `JETSON_TENSORRT=8.2.1.9`, `JETSON_NVINFER=8.2.1-1+cuda10.2`). `get_nvidia_libs_versions.sh` correctly set `is_gpu=1`, `arch=aarch64` → `tegra`.
- The running container is the `tegra` service with the nvidia runtime: `service=backend_tegra_gpu_enabled`, `HostConfig.Runtime=nvidia`.
- Despite that, TensorRT is absent inside the container: `ldconfig -p | grep nvinfer` → nothing; `find / -name 'libnvinfer.so*'` → nothing; `/usr/lib/aarch64-linux-gnu/libnvinfer.so*` → absent. (`import tensorrt` also fails, but that is irrelevant — the model path is python+DLR/C, not the TensorRT Python bindings.)
- On the host, `libnvinfer.so{,.8,.8.2.1}` live in `/usr/lib/aarch64-linux-gnu/` — NOT under the `tegra/` subdirectory.
- `tensorrt.csv` (under `/etc/nvidia-container-runtime/host-files-for-container.d/`) DOES list `libnvinfer.so.8.2.1` (+ plugin/onnxparser libs and symlinks), and `nvidia` is the default docker runtime.
- The `backend_tegra_gpu_enabled` service sets `runtime: nvidia` but does NOT set `NVIDIA_VISIBLE_DEVICES` or `NVIDIA_DRIVER_CAPABILITIES` (confirmed absent in the container's `Config.Env`).

### Analysis
Two independent mechanisms deliver host GPU libraries into the container, covering different libs:
1. **Explicit docker-compose bind mounts** — `/usr/local/cuda` and `/usr/lib/aarch64-linux-gnu/tegra`. These deliver CUDA + the Tegra driver libs.
2. **NVIDIA Container Runtime CSV injection** (L4T r32 path) — bind-mounts the files listed in `*.csv` (incl. `tensorrt.csv` → `libnvinfer.so.8.2.1`). This is the ONLY mechanism that delivers TensorRT, because `libnvinfer.so*` lives in `/usr/lib/aarch64-linux-gnu/` (not in the `tegra/` subdir that the compose volume mounts).

The CSV injection is not running for this container. The L4T CSV hook only performs its mounts when the container requests GPU capabilities via `NVIDIA_VISIBLE_DEVICES` (+ `NVIDIA_DRIVER_CAPABILITIES`); the `tegra` service does not set these, so the runtime injects nothing from the CSVs. CUDA still works because it arrives via the explicit compose bind mounts — which masks the broken CSV injection. (Secondary contributor: the runtime `mode` could not be confirmed as `csv` — `config.toml` had no matching `mode`/`csv` line — so the runtime may not resolve to csv mode either.)

Consequence: `base_model-...-segmentation` is python-backend + DLR; `libdlr.so` needs `libnvinfer.so.8` at runtime, it is absent → `libnvinfer.so.8: cannot open shared object file` → `base_model` stuck `LOADING` → inference hangs. No Triton `tensorrt` backend is involved.

### Corrected fix direction (device/compose side — no image rebuild)
- Set `NVIDIA_VISIBLE_DEVICES=all` and `NVIDIA_DRIVER_CAPABILITIES=all` on the `backend_tegra_gpu_enabled` service so the CSV plugin injects `tensorrt.csv` (and cuda/cudnn) libs; confirm `/etc/nvidia-container-runtime/config.toml` has `mode = "csv"`.
- Fallback: explicitly bind-mount `/usr/lib/aarch64-linux-gnu/libnvinfer.so*` into the container (like the `tegra` dir) so DLR can load it regardless of CSV injection.
- Verify after the change with `ldconfig -p | grep nvinfer` and `find / -name 'libnvinfer.so.8*'` inside the container, then confirm `base_model` reaches `READY` and inference completes.

### Status of the build-target theory
Superseded and consistent with the abandonment of Option C above: the model does not use the Triton `tensorrt` backend, so no build/Dockerfile change is required for this defect. The `tegra` vs `generic` *build target* (Dockerfile selection) is unrelated to this runtime-injection failure.
