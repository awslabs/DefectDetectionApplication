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

- [x] 3. 🔴 Confirm the NVIDIA runtime is in CSV mode on the device
  - [x] 3.1 DEVICE RESULT: `config.toml` had **no explicit `mode` line**, yet after adding the capability env vars the CSV injection ran and delivered `libnvinfer.so{,.8,.8.2.1}` + plugins — so the default mode resolves to csv on this JP4.6 host. **No `config.toml` change needed.**

- [x] 4. 🔴 Deliver the compose change to the device and recreate the container
  - [x] 4.1 DEVICE-VALIDATED (quick test): added the two env vars to the live 1.0.116 deployed compose, restarted the component via `greengrass-cli component restart`, and confirmed in the new container: `ldconfig -p | grep nvinfer` lists `libnvinfer.so.8` and `/usr/lib/aarch64-linux-gnu/libnvinfer.so* -> libnvinfer.so.8.2.1` are present (fresh mounts). The env-var fix works.
  - [x] 4.2 Durable delivery (build server): republished `aws.edgeml.dda.LocalServer.arm64` **1.0.117** via `./publish-ecr-only.sh` (image reused from 1.0.116 — byte-identical id `221c20480cb7`; only the scripts/compose zip changed). Registered + tagged `dda-portal:managed=true`. **Deploy 1.0.117 from the portal**, then run task 5 against it.

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
