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
