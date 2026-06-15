# Implementation Plan (REVISED) — JP4.6 runtime TensorRT availability

## Overview

> **This plan supersedes the original "compile the Triton tensorrt backend"
> approach, which was reverted (see `PIVOT-FINDINGS.md`).** Git history proved the
> `tensorrt` backend was never part of this build (always `--backend python`), and
> the modern Triton GPU build cannot compile on CUDA 10.2 (nvcc has no C++17).
> The JP4 segmentation `base_model` is a **python-backend** model that runs the
> Neo engine via **DLR** (`libdlr.so` → `libnvinfer.so.8` **at runtime**). The
> deployed **1.0.116** image (python-only Triton + the CWD-independent lyra import
> fix) is the correct artifact. The remaining work is a **runtime / host-injection**
> fix on the JP4.6 Xavier NX — no image rebuild.

The device launches the backend via the component recipe:

```
export $(grep -v '^#' /tmp/.dda.env | xargs)
docker compose --profile $DOCKER_PROFILE -f .../docker-compose.yaml up --no-build
```

`DOCKER_PROFILE` is written to `/tmp/.dda.env` by
`src/host_scripts/get_nvidia_libs_versions.sh`:
- `tegra` → service `backend_tegra_gpu_enabled` (`runtime: nvidia` + Tegra/CUDA
  mounts) → the NVIDIA Container Runtime injects host TensorRT
  (`libnvinfer.so.8.2.1` via `tensorrt.csv`).
- `generic` → service `backend_generic` (**no `runtime: nvidia`, no GPU mounts**)
  → host TensorRT NOT injected → `libdlr.so` cannot resolve `libnvinfer.so.8`
  → `base_model` fails to load → ensemble stuck `LOADING`.

Execution-environment legend:
- 🔴 **DEVICE-ONLY** — JP4.6 Xavier NX. All diagnosis/fix below runs here.
- 🟢 **repo-side** — small code/doc change committable from the build server.

## Tasks

- [ ] 1. 🔴 Capture the runtime state on the device (diagnosis-first; no changes yet)
  - [ ] 1.1 Identify the running backend container and selected profile
    - `docker ps --format '{{.ID}} {{.Image}} {{.Names}}'` (expect `flask-app`).
    - `cat /tmp/.dda.env` and `grep DOCKER_PROFILE /tmp/.dda.env`.
    - `docker inspect <cid> --format '{{.HostConfig.Runtime}}'` (expect `nvidia` for the GPU path).
    - `ls -l /etc/nv_tegra_release; cat /etc/nv_tegra_release` (JP4.6 = L4T r32.7).
  - [ ] 1.2 Inspect TensorRT availability in the container and on the host
    - In container: `docker exec <cid> bash -lc 'echo "$LD_LIBRARY_PATH"; ldconfig -p | grep -i nvinfer; find / -name "libnvinfer.so.8*" 2>/dev/null'`
    - On host: `ls -l /usr/lib/aarch64-linux-gnu/libnvinfer.so*` and `grep -ri nvinfer /etc/nvidia-container-runtime/host-files-for-container.d/ 2>/dev/null`
    - Classify the gap: (a) profile=generic, (b) CSV missing nvinfer, or (c) injected-but-not-on-loader-path.

- [ ] 2. 🔴 Apply the fix for the classified gap
  - [ ] 2.1 If `DOCKER_PROFILE=generic` on this JP4.6 device (wrong profile)
    - Re-run `sudo bash src/host_scripts/get_nvidia_libs_versions.sh; cat /tmp/.dda.env`. The selector sets `tegra` when aarch64 AND (`/etc/nv_tegra_release` present OR libcuda in the Tegra dir). If the marker is missing on this image, that's why it fell back to `generic`.
    - Make the selector resolve `tegra` on this device, redeploy/restart so the recipe brings the stack up under `--profile tegra`, then re-verify runtime=`nvidia` and `libnvinfer.so.8` present.
  - [ ] 2.2 If profile is `tegra`/runtime `nvidia` but `libnvinfer.so.8` is absent (CSV not injecting it)
    - Inspect `/etc/nvidia-container-runtime/host-files-for-container.d/*.csv` for `libnvinfer` lines (see `NVIDIA_CSI_SETUP.md` / `NVIDIA_CSI_SETTINGS_FIX.md` / `NVIDIA_CSI_EXPOSURE_TROUBLESHOOTING.md`).
    - Ensure `tensorrt.csv` lists `libnvinfer.so.8.2.1` (+ plugin/onnxparsers); recreate the container so injection re-runs.
  - [ ] 2.3 If `libnvinfer.so.8` IS present but DLR still can't load it (not on loader path)
    - Ensure the injected dir (typically `/usr/lib/aarch64-linux-gnu`) is on the loader path for the Triton python stub (add to the `tegra` service `LD_LIBRARY_PATH` in `docker-compose.yaml`, or `ldconfig` after injection). This sub-case may need a small 🟢 repo change.

- [ ] 3. 🔴 Confirm the fix end-to-end on the device
  - [ ] 3.1 With 1.0.116 deployed and the gap closed, validate via the app/Greengrass path (NOT a bare `docker exec ./tritonserver`, which lacks the tegra/nvidia-runtime env)
    - `base_model-bd-dda-test-segmentation` reaches `READY` (DLR loads `libnvinfer.so.8`).
    - the ensemble `model-bd-dda-test-segmentation` reaches `READY`.
    - the python-backend `marshal_model-...` still `READY` (preservation).
    - inference completes (no more "waiting for Triton inference" hang).

- [ ] 4. 🟢 Close out
  - [ ] 4.1 If a repo change was required (selector fallback or compose `LD_LIBRARY_PATH`), commit it and finalize `design.md`/`bugfix.md` to the runtime root cause; otherwise close the spec as "device runtime/config fix, no image change."

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "description": "Diagnose the device runtime state (no changes).", "tasks": ["1.1", "1.2"] },
    { "wave": 2, "description": "Apply the fix for whichever gap task 1 classified (exactly one of 2.1/2.2/2.3).", "tasks": ["2.1", "2.2", "2.3"] },
    { "wave": 3, "description": "Validate model READY + inference via the app path.", "tasks": ["3.1"] },
    { "wave": 4, "description": "Commit any repo change and close the spec.", "tasks": ["4.1"] }
  ],
  "criticalPath": ["1.1", "1.2", "2.1", "3.1", "4.1"]
}
```

## Notes
- No image rebuild is required; **1.0.116** is the correct python-only image.
- The original build-the-tensorrt-backend artifacts (routing/preservation PBTs,
  spike findings) remain in git history at commit `bdadb56`.
- This is fundamentally a JP4.6 **runtime GPU-enablement** issue (right compose
  profile + nvidia runtime + TensorRT CSV injection), not a build artifact gap.
