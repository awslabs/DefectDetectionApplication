# NVIDIA Bug Report Draft

> DRAFT for filing on the NVIDIA developer forums / NVBug. All facts sourced from the
> on-device evidence chain in `bugfix.md` (Re-hypothesis chain, 2026-08-15 sessions).
> Not yet filed. Timestamps are device-local (CDT) as recorded in journalctl.

## Title

Thor/JP7: nvargus-daemon enters persistent state blocking ALL new CUDA context
creation device-wide (dmabuf import Error 89)

## Platform

- Device: Jetson Thor (hostname jetson-thor1)
- JetPack 7, NVIDIA driver 595.78
- CUDA 13.x userspace (system ptxas is CUDA 13.2, V13.2.78)
- Observed via both the CUDA driver API (`cuInit`/`cuCtxCreate`) and the runtime API
  (`cudaSetDevice`/`cudaFree`), host-side and inside containers. Application stack
  where first noticed: PyTorch/vLLM (vLLM 0.11.3.dev0 source build, sm_110) and ONNX
  Runtime — versions are incidental; the failure reproduces with bare
  `cuCtxCreate`/`cudaSetDevice` probes with no framework involved.

## Summary

After nvargus/CSI ISP capture activity, nvargus-daemon holds a state in which every
NEW CUDA context creation on the device fails, while pre-existing contexts keep
working. In the degraded state:

- `cuCtxCreate` fails with `CUDA_ERROR_OPERATING_SYSTEM` (304) — for an unprivileged
  user AND for root — while `cuInit` still returns 0.
- Runtime API `cudaSetDevice(0)` / `cudaFree(0)` in a fresh process return
  `cudaErrorDevicesUnavailable` (46); PyTorch surfaces it as
  `torch.AcceleratorError: CUDA error: CUDA-capable device(s) is/are busy or
  unavailable`.
- Pre-existing graphics contexts (Xorg, gnome-shell, created before onset) continue
  to work throughout.
- `systemctl restart nvargus-daemon` clears the state instantly — no reboot needed.

## Timeline / onset

- Boot: Aug 14 11:22:50. Onset is a discrete event: **Aug 14 17:17:31** — the first
  kernel Error(89) line appears, interleaved 1:1 with a `gst-launch` nvargus/CSI ISP
  capture loop that was running at that moment (`tegra194-isp5 ... ISP capture setup
  complete` every ~0.5 s).
- The degraded state then persists with zero load: with no camera pipeline running
  and spontaneous Error(89) at 0 over a quiet 5-minute observation window, a fresh
  CUDA-init probe still failed.
- nvargus itself was also degraded in this state: `CameraProvider failed to
  initialize` with SCF Error 0x00000002 (`CudaService startService`), logged Aug 15
  14:42:57.

## Kernel signature

Every failed context creation appends exactly one pair of lines:

```
Can't map dma attachment!
NVRM: GPU0 osCreateOsDescriptorFromFileHandle: Error (89) while trying to import fd!
```

journalctl counted **200,273+ occurrences** since onset (later reads: 200,288, still
accruing 1:1 per failed probe until recovery). An earlier much lower dmesg count
(~455) was an artifact of dmesg ring-buffer rotation.

## Evidence the failure is context-creation-specific and process-independent

- Completely fresh processes fail: probes run inside a container (`docker exec
  python3 -c "import torch; torch.cuda.set_device(0)"`) and host-side, as
  unprivileged user and as root, all fail identically.
- `cuInit` succeeds (returns 0, opens the `/dev/nvidia*`/`nvidia-uvm` fds) while
  `cuCtxCreate` fails — the block is specifically at context creation.
- Zero CUDA compute contexts existed at failure time: `nvidia-smi
  --query-compute-apps` returned EMPTY while the probe failed (only pre-onset
  Xorg/gnome-shell graphics contexts present), and every candidate process had 0
  `nvidia-uvm` memory mappings. A stepwise probe at 0, 1, and 3 loaded model
  contexts failed identically at every count — there is no context-count threshold.
- The failure is independent of process count, process ancestry, and container
  boundary. (A fork-after-CUDA-init application hypothesis was explicitly tested and
  refuted: switching the affected app to spawn changed nothing.)
- Application processes that "kept working" had in fact silently fallen back to CPU
  (ONNX Runtime provider chain CUDA → CPU); each of their startup attempts emitted
  its own Error(89) pair — they tried CUDA and failed like everything else.

## Recovery

`systemctl restart nvargus-daemon` (Aug 15 15:57:03 CDT, old daemon PID 2568 running
since Aug 13, new PID 2973277):

- A fresh-process CUDA-init probe succeeded SECONDS later on the first attempt, and
  again with a real device tensor allocation (`torch.zeros(4, device="cuda")`).
- The kernel Error(89) count froze at 200,288 — zero new lines from the restart
  onward, across an entire subsequent session including a full vLLM engine
  initialization (16.6 GiB weight load) and ONNX model reloads onto GPU.
- Reboot was never needed and remains unexercised as a discriminator. The state is
  therefore held by the nvargus-daemon process itself, not by persistent
  kernel/driver state.

## Impact

Production-visible, device-wide GPU compute outage, partially masked by CPU
fallbacks:

- Blocked vLLM model deployment entirely: the engine core subprocess died at
  `torch.cuda.set_device`, the model component went BROKEN, and Greengrass rolled
  back the whole deployment (taking three healthy vision models with it).
- Silently degraded ONNX inference to CPU across all models on the device — models
  reported READY while no GPU compute context existed anywhere, hiding the outage.

## Reproduction status

The trigger correlation is strong (first Error(89) interleaved 1:1 with the
nvargus/CSI ISP capture loop at onset; recovery exactly at daemon restart), but a
deliberate reproduction — running the CSI capture loop to re-enter the degraded
state, confirming the failure signature, then clearing it with a daemon restart —
has NOT yet been performed. We can run that confirmation pass on request and attach
full journalctl/nvargus logs from it.

## Questions

1. Is this a known issue on Thor/JetPack 7 (driver 595.78)? Is there an existing
   NVBug for Argus/ISP dmabuf-import poisoning of device-wide CUDA context creation?
2. Is a fix available or planned in a newer driver / JetPack release?
3. Is there a recommended mitigation beyond restarting nvargus-daemon (e.g. a
   configuration that prevents the daemon from entering this state, or a way to
   detect it proactively)?
