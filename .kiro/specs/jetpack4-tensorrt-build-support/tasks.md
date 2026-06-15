# Implementation Plan (REVISED 2) — JP4.6 runtime TensorRT CSV injection

## Overview

> Root cause is now **device-verified** (see `PIVOT-FINDINGS.md` →
> "Device verification"). It is NOT a build artifact, NOT the Triton tensorrt
> backend, and NOT profile mis-selection. The deployed **1.0.116** image
> (python-only Triton + the CWD-independent lyra fix) is correct — **no image
> rebuild.**

Verified chain:
- The JP4.6 device correctly selects `DOCKER_PROFILE=tegra` and the
  `backend_tegra_gpu_enabled` container runs with `HostConfig.Runtime=nvidia`.
- BUT that service sets `runtime: nvidia` **without** `NVIDIA_VISIBLE_DEVICES` /
  `NVIDIA_DRIVER_CAPABILITIES`, so the L4T NVIDIA Container Runtime performs **no
  CSV injection**. `tensorrt.csv` lists `libnvinfer.so.8.2.1`, but it is never
  mounted in.
- `libnvinfer.so*` lives in `/usr/lib/aarch64-linux-gnu/` (NOT the `tegra/`
  subdir the compose volume bind-mounts), so CSV injection is the **only**
  mechanism that delivers it. CUDA still works via the explicit `tegra`/`cuda`
  bind mounts, which masked the gap.
- Result: the python+DLR `base_model` can't load `libnvinfer.so.8` →
  `cannot open shared object file` → stuck `LOADING`.

Fix: make the `tegra` service request GPU capabilities so CSV injection runs.
This is a `docker-compose.yaml` change (ships in the component's scripts artifact,
**not** the docker image) → redeploy/republish, no rebuild.

Execution-environment legend:
- 🟢 **repo-side** — committable from the build server.
- 🔴 **DEVICE-ONLY** — JP4.6 Xavier NX validation.

## Tasks

- [x] 1. 🔴 Diagnose the runtime gap (DONE — device-verified)
  - [x] 1.1 Confirmed `DOCKER_PROFILE=tegra`, container `Runtime=nvidia`, host is JP4.6 (L4T r32.7, TRT 8.2.1).
  - [x] 1.2 Confirmed `libnvinfer.so*` absent in-container despite `tensorrt.csv` listing it; `NVIDIA_VISIBLE_DEVICES`/`NVIDIA_DRIVER_CAPABILITIES` absent from the container env → CSV injection not running.

- [x] 2. 🟢 Add GPU-capability env to the `tegra` service so CSV injection runs (DONE — implemented)
  - [x] 2.1 In `src/docker-compose.yaml`, add `NVIDIA_VISIBLE_DEVICES=all` and `NVIDIA_DRIVER_CAPABILITIES=all` to `backend_tegra_gpu_enabled`'s `environment` (with an explanatory comment). The `generic` service is intentionally left unchanged.

- [ ] 3. 🔴 Confirm the NVIDIA runtime is in CSV mode on the device
  - [ ] 3.1 Check `/etc/nvidia-container-runtime/config.toml` for `mode = "csv"` (on-device diagnosis showed no matching line). On JP4/L4T r32 the CSV hook only injects in csv mode. If it is `auto`/unset and injection still doesn't run after task 4, set `mode = "csv"` (or rely on the task 6 fallback).

- [ ] 4. 🔴 Deliver the compose change to the device and recreate the container
  - [ ] 4.1 Fast device-side validation (no republish): apply the same two env vars to the deployed `docker-compose.yaml` under the component's `custom-build/.../` dir, then `docker compose --profile tegra ... up --no-build --force-recreate`. Confirm `ldconfig -p | grep nvinfer` now lists `libnvinfer.so.8` in the container.
  - [ ] 4.2 Durable delivery (build server): republish `aws.edgeml.dda.LocalServer.arm64` so the updated `docker-compose.yaml` ships in the component artifact — via `./publish-ecr-only.sh` (reuses the existing `flask-app` image; repackages the scripts/compose zip; bumps to 1.0.117). NOTE: ensure the staging `custom-build/.../docker-compose.yaml` reflects the repo edit before zipping. Then deploy the new version from the portal.

- [ ] 5. 🔴 Verify the fix end-to-end (via the app/Greengrass path, not a bare `docker exec ./tritonserver`)
  - [ ] 5.1 In-container `libnvinfer.so.8` resolvable (`ldconfig -p | grep nvinfer`).
  - [ ] 5.2 `base_model-bd-dda-test-segmentation` → `READY`; ensemble `model-bd-dda-test-segmentation` → `READY`; python `marshal_model-...` still `READY`; inference completes (no "waiting for Triton inference" hang).

- [ ] 6. 🟢/🔴 Fallback if CSV injection still does not deliver TensorRT (only if task 5 fails)
  - [ ] 6.1 Add an explicit bind mount of the host TensorRT libs to the `tegra` service (e.g. mount `/usr/lib/aarch64-linux-gnu` read-only to a non-shadowing path and add it to `LD_LIBRARY_PATH`, or mount the specific `libnvinfer.so*` files), so DLR can load `libnvinfer.so.8` regardless of CSV injection. Re-verify task 5.

- [ ] 7. 🟢 Close out
  - [ ] 7.1 Once verified on-device, finalize `design.md`/`bugfix.md` to the runtime CSV-injection root cause and mark the spec complete (device runtime/config fix; no image change).

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "description": "Diagnosis (done) and the compose env fix (done).", "tasks": ["1.1", "1.2", "2.1"] },
    { "wave": 2, "description": "Confirm runtime csv mode on the device.", "tasks": ["3.1"] },
    { "wave": 3, "description": "Deliver the compose change (device-edit validation and/or republish+deploy).", "tasks": ["4.1", "4.2"] },
    { "wave": 4, "description": "Verify libnvinfer resolves and models reach READY + inference.", "tasks": ["5.1", "5.2"] },
    { "wave": 5, "description": "Fallback bind-mount only if CSV injection still fails.", "tasks": ["6.1"] },
    { "wave": 6, "description": "Finalize docs and close the spec.", "tasks": ["7.1"] }
  ],
  "criticalPath": ["2.1", "4.1", "5.2", "7.1"]
}
```

## Notes
- No image rebuild; **1.0.116** image is correct. The fix is purely the `tegra`
  service env in `docker-compose.yaml` (ships in the component scripts artifact).
- `docker-compose.yaml` is delivered via the component zip, so the durable fix
  needs a republish (task 4.2); task 4.1 lets the device validate immediately.
- The reverted build approach (PBTs, spike) remains at commit `bdadb56`.
