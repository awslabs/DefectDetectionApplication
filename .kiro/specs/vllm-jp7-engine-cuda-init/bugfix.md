# Bugfix Requirements Document

## Introduction

Deploying the vLLM model component `model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp7`
(v1.0.0) to a JetPack 7 / Thor device (jetson-thor1, LocalServer.arm64JP7 1.0.4) breaks
the entire Greengrass deployment. The component's Startup requests a model load from the
companion vLLM runtime (127.0.0.1:8901 inside the backend container) and receives HTTP
409 `{"name":"qwen3-vl-8b-instruct","state":"FAILED","reason":"Engine core initialization
failed. See root cause above. Failed core proc(s): {}"}`. The component exits 1, exhausts
its Greengrass retries, goes BROKEN, and Greengrass rolls back the whole deployment —
taking three healthy vision models down with it (deployment aebc9d9a,
`FAILED_ROLLBACK_COMPLETE`).

Live-captured root failure (reproduced twice on-device, once with the GPU fully idle):
the vLLM V1 EngineCore subprocess (`EngineCore_DP0`), launched by the runtime manager
inside the backend container, dies at `torch.cuda.set_device` with
`torch.AcceleratorError: CUDA error: CUDA-capable device(s) is/are busy or unavailable
(cudaErrorDevicesUnavailable)`. vLLM is the JP7 image's source build
(0.11.3.dev0+g275de3417.d20260814, sm_110); engine args staged:
`gpu_memory_utilization=0.5`, `dtype=auto`, `max_model_len=2048`,
`tensor_parallel_size=1`, `enforce_eager=true`.

Ruled out on-device: GPU contention (reproduced on an idle GPU, Compute Mode Default),
model architecture support (Qwen3VLForConditionalGeneration is in the image's vLLM
registry), network/HF access (config metadata cached; the engine dies before weight
download), disk, memory, and container OOM. The main backend process CAN use CUDA in the
same container (ONNX/Triton vision inference verified working the same day).

Primary hypothesis (to be confirmed or refuted by the exploration phase, NOT yet
established fact): fork-after-CUDA-init. The vLLM runtime manager lives inside the
backend container process tree; the container's multiprocessing start method is `fork`
(verified in-container); no `VLLM_WORKER_MULTIPROC_METHOD` is set anywhere in the JP7
image, compose service, or component recipe; and the parent process tree has initialized
CUDA before the engine launch (GPU inference activity, plus the manager's own failure
handler touches `torch.cuda` — "Reclaimed cached CUDA memory after unload/failure"). A
forked child re-initializing CUDA on Jetson/Tegra yields exactly
`cudaErrorDevicesUnavailable`. This exposure is JP7-specific: JP6 pins the V0 in-process
engine (`VLLM_USE_V1=0`, vllm 0.9.3 — no CUDA subprocess) and JP5 ships without vLLM
(`VLLM_ENABLE=0`), so both must be preserved unchanged.

Supporting observation to record for exploration (may be unrelated): during the degraded
period the same morning, the Thor's dmesg spammed `NVRM: GPU0
osCreateOsDescriptorFromFileHandle: Error (89) while trying to import fd!` (~1.3/s, 456+
occurrences) and even host-side `cuCtxCreate` failed for an unprivileged user
(`CUDA_ERROR_OPERATING_SYSTEM`). After the backend container restarted, the manual
reproduction still failed identically on an idle GPU, so the fork hypothesis stands
independent of that driver noise — but the exploration should determine whether the NVRM
spam is a distinct JP7 driver issue worth its own report to NVIDIA.

Scope guardrails: cloud-side publish/packaging is NOT the bug (the component deployed and
its lifecycle ran correctly — that path was fixed by the sibling spec
vllm-multi-arch-publish-conflict, whose open on-hardware verification anticipated exactly
this test). The vision/ONNX paths are untouched. Any fix is on-device code (`src/`) and
therefore requires a JP7 LocalServer component build (~1-2h, one at a time, security gate
pre-checked per `.kiro/steering/builds.md`) plus on-hardware verification on jetson-thor1
— deploying the qwen component cleanly WITH the three vision models present and healthy
(the original failing scenario) — and a JP5 or JP6 vLLM regression check before the fix
is called done.

### Re-hypothesis (task 3 outcome, 2026-08-15)

The primary hypothesis above (fork-after-CUDA-init) was put to the decisive on-device
test per tasks.md task 3 and was **REFUTED as the root cause**. With
`VLLM_WORKER_MULTIPROC_METHOD=spawn` hot-patched into the backend environment on
jetson-thor1 (spawn verifiably active — the `EngineCore_DP0` child's log showed the
backend app's module-level initialization re-executing under the child's pid prefix,
which only happens under spawn's module re-import), the engine child died at the
IDENTICAL location: `torch.cuda.set_device` →
`cudaErrorDevicesUnavailable`, HTTP 409, same reason string. The original hypothesis
text above is retained per house style; this subsection records the refutation. The
plan is HALTED at tasks.md wave 2 pending re-hypothesis; no fix code has landed.

Discriminating evidence gathered immediately after (same session):

- With the backend freshly restarted and 3 Triton python stubs (the three vision
  models' per-model processes) holding nvgpu fds in the container, a COMPLETELY FRESH
  process (`docker exec python3` → `torch.cuda.set_device`) fails with the same
  `cudaErrorDevicesUnavailable`. Process ancestry is irrelevant — brand-new processes
  cannot create a CUDA context while the stubs hold theirs.
- Earlier same-day evidence consistent with this: host-side `cuCtxCreate` failed
  (`CUDA_ERROR_OPERATING_SYSTEM`, 304) while the stubs were loaded; existing CUDA
  contexts (the vision models) keep working throughout; dmesg NVRM
  `osCreateOsDescriptorFromFileHandle: Error (89) while trying to import fd` spam
  observed during degraded periods (count stable at ~455 now, not growing).
- CloudWatch has NO log history for the qwen component before today — there is no
  evidence qwen ever loaded successfully on this device (it may never have worked on
  Thor).

**New leading hypothesis**: a concurrent CUDA context/channel limit (or driver defect)
on Thor/JP7's iGPU — the device appears unable to create a NEW CUDA context once N
contexts exist (N appears to be around backend + 3 Triton stubs + desktop). The
discriminating experiment has NOT yet been run because it requires user consent (it
stops live vision models): unload the vision models one at a time and find the exact
context count at which a fresh process can initialize CUDA again.

Consequence for the fix direction: the spawn ENV (design step 1) alone will not fix the
bug and tasks 4.x are invalidated in their current form; the reclaim hygiene change
(design step 2 / task 4.2, defect 1.3) remains a valid hardening independent of the
root cause.

#### Discriminating experiment (context-limit probe, 2026-08-15)

Run with user consent on jetson-thor1 (protocol: census GPU-fd holders, probe
fresh-process CUDA init at each vision-model count, unload stepwise until the probe
succeeds). The stepwise unload was mooted immediately: **the probe fails at ZERO loaded
model contexts**, so there is no count threshold to find.

Census/probe table (probe = `docker exec <backend> python3 -c "import torch;
torch.cuda.set_device(0)"`; census = host processes holding `/dev/nvidia0|nvidiactl`
fds):

| Step | Vision models loaded | GPU-fd holders | Fresh-init probe |
|---|---|---|---|
| Baseline (models cold after backend restart) | 0 | 6 (nvargus-daemon, Xorg, gnome-shell, mutter, gnome-software, xdg-desktop-portal) | **FAIL** `cudaErrorDevicesUnavailable` |
| yolo_test warmed to READY | 1 Triton stub | 7 | **FAIL** (identical) |
| all three warmed to READY | 3 Triton stubs | 9 | **FAIL** (identical) |

Also at baseline: host-side `cuCtxCreate` fails as BOTH unprivileged user and **root**
(CUDA_ERROR_OPERATING_SYSTEM, 304) while `cuInit` returns 0; runtime-API
`cudaSetDevice(0)`/`cudaFree(0)` in a fresh container process return 46
(devices unavailable). The bidirectional reload confirmation and the two-simultaneous-
probe discriminator were N/A — the probe never succeeded at any count.

**Verdict: the concurrent context/channel-LIMIT hypothesis is REFUTED as stated.**
There is no N. New CUDA context creation fails device-wide regardless of how many
model contexts exist, including at zero.

Corrections to the prior evidence base uncovered by the experiment:

- **The fd census over-counts contexts.** `cuInit` alone opens the `/dev/nvidia*` /
  `nvidia-uvm` fds and it still succeeds; only `cuCtxCreate` fails. Holding nvgpu/nvidia
  fds does not mean holding a CUDA context.
- **No live CUDA compute context exists anywhere on the device.**
  `nvidia-smi --query-compute-apps` returns EMPTY with all three vision models READY
  (only Xorg/gnome-shell graphics contexts, created before the failure onset, appear),
  and every census process — including all three Triton stubs — has 0 `nvidia-uvm`
  memory mappings.
- **"The vision models keep working" was misleading: they load with silent CPU
  fallback.** The DDA ORT provider chain is `CUDA → CPU`
  (`inference_runtimes.py`), so the stubs reach READY without GPU. Each stub load
  emitted its own kernel `Can't map dma attachment!` + NVRM Error(89) pair (observed
  at 20:23:12–13 matching the three stub starts) — the stubs TRIED CUDA, failed
  identically, and fell back.
- **The Error(89) spam is the failure, 1:1.** Every failed context creation appends
  exactly one `Can't map dma attachment!` + `NVRM: GPU0
  osCreateOsDescriptorFromFileHandle: Error (89)` pair. journalctl since boot counts
  **200,273** occurrences (the earlier "stable ~455" reading was an artifact of dmesg
  ring-buffer rotation — the absolute dmesg count even DECREASES as wifi-driver spam
  rotates old lines out).
- **Onset is a discrete event: Aug 14 17:17:31** (boot was Aug 14 11:22:50). The first
  Error(89) appears interleaved 1:1 with a `gst-launch` nvargus/CSI **ISP capture loop**
  (`tegra194-isp5 ... ISP capture setup complete` every ~0.5 s) that was running at that
  moment — consistent with the CSI camera experiments documented in the repo's
  NVIDIA_CSI_* notes. The degraded state then persists with no load: at experiment time
  no camera pipeline was running, spontaneous Error(89) was 0 over a quiet 5-minute
  window, and the probe still failed. nvargus itself is now degraded too
  (`CameraProvider failed to initialize`, SCF Error 0x00000002, 14:42:57).

**Resulting hypothesis (v3): a persistent driver-level degraded state on Thor/JP7
(driver 595.78), entered on Aug 14 17:17:31 coincident with nvargus/ISP CSI capture
activity, in which ALL new CUDA context creation fails** (`cuCtxCreate` →
CUDA_ERROR_OPERATING_SYSTEM host-side / `cudaErrorDevicesUnavailable` runtime-side,
kernel signature `Can't map dma attachment!` + NVRM Error(89)) **while pre-existing
graphics contexts keep working. Process count, ancestry, and container boundaries are
all irrelevant.** This also retro-explains the task 3 refutation (the spawn hot-patch
test ran inside the degraded window) and possibly the original aebc9d9a failure itself.
Likely recovery: reboot (untested); cheap intermediate discriminator: restart
`nvargus-daemon` and re-probe (untested, needs consent — touches a system service).
Strong candidate for an NVIDIA bug report (Argus/ISP dmabuf import poisoning
driver-wide context creation).

Fix-direction consequence: the root cause is most likely NOT a LocalServer code defect
at all. Next discriminators, in order of cost: (1) `systemctl restart nvargus-daemon`
+ re-probe; (2) reboot + immediately probe fresh CUDA init and re-run the qwen load on
a clean boot BEFORE any camera/ISP activity; (3) if clean-boot qwen works, reproduce
the degraded state deliberately with the CSI capture loop to pin the trigger for the
NVIDIA report. Separately, the vision models' silent CPU fallback (READY without GPU)
deserves its own visibility/alerting consideration — "READY" currently hides a
device-wide GPU outage.

Device state after the experiment: all three vision models restored to READY (stubs
present in the census), backend container healthy and answering HTTP 200. Note: the
backend container restarted once mid-experiment from the PRE-EXISTING `awscrt`
"Continuation ref count has gone negative" crash loop (RestartCount=6, aborts visible
at multiple earlier startups; not triggered by the read-only probes).

##### nvargus-daemon restart discriminator (2026-08-15)

Run with user consent (restart of the nvargus-daemon system service authorized;
reboot NOT authorized). Result: **CUDA context creation recovered immediately —
Argus is PINNED as the holder of the poisoned state.**

- Pre-restart baseline: in-container fresh-init probe (`docker exec <backend>
  python3 -c "import torch; torch.cuda.set_device(0)"`) FAILED with the identical
  `cudaErrorDevicesUnavailable`; kernel Error(89) count 200,288 (up from 200,273 —
  still accruing 1:1 per probe); nvargus-daemon PID 2568, running since Aug 13
  15:51 CDT, last log lines still the 14:42:57 SCF `CudaService startService`
  Error 0x00000002 (CameraProvider degraded).
- Restart: `systemctl restart nvargus-daemon` at 15:57:03 CDT → clean start, new
  PID 2973277, "Listening for connections...", no SCF errors on startup
  (CameraProvider init is lazy — full recovery unverified until a camera client
  connects).
- Post-restart probe 1 (seconds later, same in-container command): **`CUDA INIT
  OK`** — SUCCESS on the first attempt, no grace period needed.
- Post-restart probe 2 (repeatability, with a real device tensor allocation):
  **SUCCESS** (`torch.zeros(4, device="cuda")` allocated and summed).
- Error(89) count after BOTH probes: **200,288 — zero new lines.** The 1:1
  failure signature stopped exactly with the restart.
- `nvidia-smi --query-compute-apps` still empty (expected — probes are
  short-lived; vision models remain on their silent ORT CPU fallback from the
  degraded window).
- Backend untouched and healthy throughout: HTTP 200 before and after, container
  NOT restarted (same StartedAt 20:52:48Z, RestartCount 8 before and after —
  the increment from 6 to 8 predates this experiment, the pre-existing awscrt
  crash loop).

**Verdict: hypothesis v3 CONFIRMED and sharpened — the degraded state is held by
the nvargus-daemon process itself, not by persistent kernel/driver state. A
plain service restart (no reboot) fully restores device-wide CUDA context
creation.** This completes the Argus pin for the NVIDIA bug report:
nvargus/ISP CSI capture activity (onset Aug 14 17:17:31) put the daemon into a
state that blocked ALL new CUDA context creation device-wide
(dmabuf-import poisoning), and killing the daemon released it. Reboot is NOT
required as a discriminator and remains unexercised.

Follow-ups (user decisions, not performed):
- The three vision models are still READY on CPU fallback; a stub reload or
  backend restart is needed for them to pick the GPU back up. Not done — left
  to the user.
- The qwen vLLM load should now be re-attempted (the original bug) — the
  device can create CUDA contexts again, so the aebc9d9a failure scenario is
  finally testable outside the degraded window. This likely moots the
  LocalServer code-fix direction entirely (pending that re-test).
- Reproduce deliberately (CSI capture loop → degraded → nvargus restart →
  recovered) to complete the NVIDIA report evidence chain.

##### Clean-window qwen load re-test (2026-08-15)

Run with user consent (vision-model reloads + manual qwen staging/load
authorized; reboot NOT authorized). This is the decisive re-test the previous
subsection anticipated: the first-ever qwen load attempt OUTSIDE the degraded
window.

Vision models back onto GPU (Phase A). The backend container had restarted
again (RestartCount 8→9 during the session, both from the PRE-EXISTING awscrt
"Continuation ref count has gone negative" crash loop — not our probes), so
the CPU-fallback stubs were already gone and all three models were cold
(UNKNOWN). Fresh-init probe in the container: **CUDA INIT OK**. Each model
started via `GET /feature-configurations/models/{name}/start` (port 5000 — the
vision Triton is in-process via panorama mlops; there is no HTTP repository
endpoint for it):

| Model | Status | GPU (nvidia-smi compute apps) |
|---|---|---|
| yolo_test | READY | stub visible, 340 MiB |
| rf-detr-seg-nano | READY | stub visible, ~530 MiB |
| cookies-segmentation | READY | stub visible, 788–1296 MiB |

Zero new Error(89) lines (count 200,288 before and after). The "silent CPU
fallback" state from the degraded window is fully cleared — this is the first
time the stubs appear in `nvidia-smi --query-compute-apps` since the Aug 14
onset.

Qwen load outside the degraded window (Phase B). Staged exactly as task 3 did
(`python3 /aws_dda/vllm_model_prep.py --unarchived_repo_path /tmp/qwen_debug
--model_name qwen3-vl-8b-instruct ...` — the prior session's repository
template with the identical engine args survived on the device; artifact
dirs under greengrass were empty post-rollback). Outcome:

- **The original failure is GONE.** `EngineCore_DP0` (pid 329) sailed past
  `torch.cuda.set_device` — the exact line it died on twice inside the
  degraded window — initialized NCCL, resolved Qwen3VLForConditionalGeneration,
  selected the FLASH_ATTN backend, downloaded the weights (134 s, 17 GB now
  persisted in `/aws_dda/hf_cache`), and loaded them: "Model loading took
  16.6397 GiB memory and 163.95 seconds".
- **A second, DISTINCT failure mode then surfaced** during the engine's
  memory-profiling forward pass (`determine_available_memory` →
  `profile_run` → the vision encoder's rotary-embedding Triton-JIT kernel,
  `vllm/vllm_flash_attn/ops/triton/rotary.py`):
  `triton.runtime.errors.PTXASError: PTXAS error: Internal Triton PTX codegen
  error` — ``ptxas fatal : Value 'sm_110a' is not defined for option
  'gpu-name'``. Model → FAILED, HTTP 409, the same outer reason string
  ("Engine core initialization failed") as before. Zero kernel Error(89)
  lines — this failure has a completely different signature.
- Discriminator run in the same session: the Triton compiler's **bundled**
  ptxas is CUDA **12.8** (V12.8.93) and rejects `sm_110a`; the container's
  system `/usr/local/cuda/bin/ptxas` is CUDA **13.2** (V13.2.78) and accepts
  the flag. Thor's sm_110a is simply newer than the ptxas that triton ships.
  Obvious fix candidate: point Triton at the system assembler
  (`TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas`, e.g. as a Dockerfile.jp7
  ENV) — NOT tested this session (no permanent device edits authorized).

Memory observations: model load peaked around 30 GB system-used of 125 GB
unified memory, no swap; after unload/cleanup 15–21 GB used, ~104 GB
available. The step-8 coexistence check was preempted: the awscrt restart had
already unloaded the vision models BEFORE the qwen load started, so qwen ran
against a cold vision stack (coexistence under gpu_memory_utilization=0.5
remains unproven — it belongs to task 10's acceptance run anyway).

Cleanup/end state: `vllm_model_prep.py --cleanup` ran (unload 200, staged
repo removed); all three vision models reloaded to READY **on GPU** (stubs in
nvidia-smi); backend HTTP 200, healthy; fresh-init probe still CUDA INIT OK;
final Error(89) count **200,288 — zero new lines for the entire session**.
The 17 GB HF weight cache was left in place (it makes the next load attempt
skip the download).

**Verdict on the fix direction:**

1. **The original bug (cudaErrorDevicesUnavailable) is CONFIRMED
   ENVIRONMENTAL** — the nvargus/Argus driver defect (hypothesis v3). Every
   prior reproduction, including deployment aebc9d9a itself, ran inside the
   degraded window. Outside it the engine initializes CUDA flawlessly from
   the same backend process tree, fork default and all. **Task 4.1 (spawn
   ENV) is mooted as a fix; task 4.2 (reclaim hygiene) survives only as
   optional hardening.** The NVIDIA bug report (Argus dmabuf-import
   poisoning) and the silent-CPU-fallback visibility follow-up stand.
2. **BUT the spec cannot close as "no code fix needed": qwen still cannot
   reach READY on Thor** due to the newly-surfaced, real, deterministic
   image defect — triton's bundled CUDA 12.8 ptxas cannot codegen for
   sm_110a. This is a JP7-image-level bug (likely a one-line
   `TRITON_PTXAS_PATH` ENV in Dockerfile.jp7 + the usual golden rebaseline +
   JP7 build), hit by any vLLM model whose execution path JIT-compiles a
   Triton kernel. Recommended: re-scope this spec (or open a sibling) around
   the ptxas defect; requirement 2.4 (qwen READY alongside the three vision
   models) remains the acceptance bar and is still unmet.

##### TRITON_PTXAS_PATH hot-patch validation (2026-08-15)

Run with standing user consent (hot-patch + container restarts authorized;
reboot not needed). This validates the fix candidate the clean-window re-test
identified. **Outcome: VALIDATED ON THE FIRST ITERATION — no further knobs
were needed.**

Sanity (pre-patch): fresh-init CUDA probe in the backend container `CUDA INIT
OK`; Error(89) baseline 200,288; backend healthy; all three vision models
READY on GPU (stubs 340/530/788 MiB in `nvidia-smi --query-compute-apps` —
the backend auto-warms them on container start; the container had restarted
once more from the pre-existing awscrt crash loop, RestartCount 9→ recreated
below). No nvargus degraded-state recurrence at any point this session.

Target verification: the container's `/usr/local/cuda/bin/ptxas` is CUDA
13.2 (V13.2.78) and **compiles a real PTX stub for sm_110a**
(`.version 9.0` / `.target sm_110a` → cubin OK; note `.version 8.0` is
rejected for that target, so the acceptance check must use PTX ISA ≥ 9.0).

Hot-patch mechanism: on-device compose backed up, then
`TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas` added to the backend service
`environment` (the sed keyed on the `HF_HOME` line, so the var also landed in
the two other service blocks that carry HF_HOME — harmless for a validation,
fully reverted after). Component restarted via greengrass-cli; env verified
in-container via `printenv`; backend healthy (HTTP 200 on /health).

Qwen load with the patch active (staged from the surviving `/tmp/qwen_debug`
template, weights from the 17 GB HF cache):

- Load request answered **HTTP 200 on the FIRST attempt** ("Model
  'qwen3-vl-8b-instruct' loaded successfully!", prep exit 0).
- `EngineCore_DP0` (pid 957): weights loaded in 13.76 s (cache hit; no
  download), "Model loading took 16.6397 GiB"; **the memory-profiling
  forward pass — the exact step that died with PTXASError — completed**:
  "Available KV cache memory: 40.48 GiB", "GPU KV cache size: 294,752
  tokens", "init engine (profile, create kv cache, warmup model) took
  29.03 seconds", then **"vLLM model 'qwen3-vl-8b-instruct' is READY"**
  (21:37:48Z). Zero ptxas/PTXAS errors anywhere in the log.
- **Coexistence (closest pre-build approximation of requirement 2.4):**
  `nvidia-smi` concurrently showed all three vision stubs (340/788/532 MiB)
  AND `VLLM::EngineCore` at **59,663 MiB** while qwen was READY. System
  memory peaked ~72 GB used of 122 GB unified, no swap pressure.
- **Generate proof:** `POST /v2/models/qwen3-vl-8b-instruct/generate` with a
  text-only prompt → **HTTP 200** with real `text_output` (the model
  answered; content quality irrelevant — serving is proven). Note the first
  generate needs a generous client timeout (a 60 s curl timed out during
  first-request warmup; a retry within a 300 s budget succeeded).

Nuisance (noted, not caused by the patch): AFTER the generate proof
completed, the backend container restarted again from the pre-existing awscrt
"Continuation ref count has gone negative" abort (signature confirmed in the
log), which unloaded qwen and the stubs. All validation evidence had already
been captured; no re-run was needed.

Cleanup/end state: qwen unloaded via `--cleanup` (HTTP 200, staged repo
removed); compose restored from backup and backup deleted (**0 occurrences
of TRITON_PTXAS_PATH**, env verifiably absent from the recreated container);
all three vision models READY on GPU again (stubs 340/532/784 MiB);
fresh-init probe `CUDA INIT OK`; backend HTTP 200; final Error(89) count
**200,288 — zero new lines for the entire session**; HF cache (17 GB) and
the `/tmp/qwen_debug` template left in place; session temp files removed.

**Confirmed fix direction for the re-scope:** one-line
`ENV TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas` in
`src/backend/Dockerfile.jp7` (+ the masked-golden rebaseline + the JP7
LocalServer build + the task-10-shape on-hardware acceptance). No other
knobs (triton env, gpu_memory_utilization, eager) were needed — with the
system ptxas the engine profiles, sizes a 40 GiB KV cache under
`gpu_memory_utilization=0.5`, reaches READY, and serves, coexisting with the
three vision models on GPU. Requirement 2.4 is now demonstrated achievable
pre-build; the durable fix must still come from the built image, not the
hot-patched device.

## Bug Analysis

> **Re-scoped 2026-08-15** around the VALIDATED root cause (see the Re-hypothesis
> evidence chain above, which is the authoritative record). The original
> `cudaErrorDevicesUnavailable` failure was ENVIRONMENTAL — an nvargus/Argus driver
> defect on Thor/JP7 (driver 595.78) blocked ALL new CUDA context creation device-wide
> and was cleared by restarting nvargus-daemon. The code-fixable defect this spec now
> addresses is the JP7 image defect surfaced by the clean-window re-test: triton's
> BUNDLED ptxas (CUDA 12.8) cannot codegen for Thor's `sm_110a`, so any vLLM model
> whose execution path JIT-compiles a Triton kernel dies with `PTXASError` during the
> engine's profile run. Requirement numbering below is preserved (1.1–1.4, 2.1–2.4,
> 3.1–3.7) with reworked content; 3.8 is new.

### Current Behavior (Defect)

When a vLLM model load is requested on a JP7/Thor device, the engine core never
finishes initializing and the failure cascades from one model component to the whole
deployment:

1.1 WHEN a load of a staged vLLM model whose execution path JIT-compiles any Triton
kernel is requested on a JP7 device (via `POST /v2/repository/models/{name}/load` on
the runtime at 127.0.0.1:8901) THEN the vLLM V1 EngineCore subprocess passes CUDA init
and weight load but dies during the memory-profiling forward pass
(`determine_available_memory` → `profile_run` → e.g. the vision encoder's
rotary-embedding Triton-JIT kernel) with `triton.runtime.errors.PTXASError` — triton's
BUNDLED ptxas (CUDA 12.8, V12.8.93) rejects Thor's `sm_110a` (``ptxas fatal : Value
'sm_110a' is not defined for option 'gpu-name'``) — the model transitions to FAILED
with reason "Engine core initialization failed", and the load endpoint answers HTTP
409 (validated on jetson-thor1, clean-window re-test 2026-08-15)

1.2 WHEN the model component's Startup (`vllm_model_prep.py`) receives the authoritative
409 THEN it exits 1, Greengrass restarts the component, and each retry's load request
fails identically — the failure is DETERMINISTIC (every attempt recompiles the same
kernel with the same bundled ptxas), so the component can never recover within a
deployment attempt

1.3 (hygiene hardening, not the root cause) WHEN an engine load fails THEN the runtime
manager's failure handling itself touches CUDA in the parent backend process
(`torch.cuda.is_available()` / `torch.cuda.empty_cache()` during memory reclaim),
driver-initializing CUDA in the parent on every failure. This is a real hygiene defect
kept in scope as OPTIONAL HARDENING: it was the suspected poisoning leg of the refuted
fork hypothesis, and on JP7/V1 the reclaim is a no-op anyway (engine memory lives in
the dead child, not the parent)

1.4 WHEN the vLLM model component exhausts its retries and goes BROKEN THEN Greengrass
rolls back the ENTIRE deployment, removing the healthy vision model components that
deployed alongside it (observed: deployment aebc9d9a,
`FAILED_ROLLBACK_COMPLETE: Service model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp7 in
broken state after deployment`) — deferred to a follow-up cloud-side spec (see Scope
Disposition)

### Expected Behavior (Correct)

2.1 WHEN a load of a staged, correctly-packaged vLLM model is requested on a JP7 device
THEN the system SHALL complete engine core initialization INCLUDING the memory-profiling
forward pass — every Triton-JIT kernel on the model's execution path SHALL compile
successfully for Thor's `sm_110a` (the JP7 backend environment declares
`TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas`, pointing triton at the image's system
CUDA 13.x ptxas, which accepts `sm_110a`) — and transition the model STAGED -> LOADING
-> READY, answering the load request with HTTP 200

2.2 WHEN the engine's profile run reaches any Triton-JIT compilation on a JP7 device
THEN the PTX assembly step SHALL use the image's system ptxas (CUDA 13.x, `sm_110a`
capable) rather than triton's bundled CUDA 12.8 ptxas — no `PTXASError` for
architecture-name rejection SHALL occur on any load attempt, first or retried

2.3 WHEN an engine core initialization does fail for a transient cause THEN a subsequent
load request for the same model SHALL perform a genuine re-attempt that can succeed once
the triggering condition has cleared (the deterministic ptxas codegen failure removed,
the Greengrass-driven retry loop is again capable of recovering from genuinely
transient causes; as hardening, the failure handler SHALL NOT driver-initialize CUDA in
the parent backend process)

2.4 WHEN the qwen3-vl-8b-instruct component is deployed to jetson-thor1 together with the
three vision model components THEN the system SHALL bring the vLLM model to READY with
all three vision models remaining deployed and healthy (the original failing scenario,
required for on-hardware verification)

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a vLLM model is loaded on a JP6 device (V0 in-process engine, `VLLM_USE_V1=0`,
vllm 0.9.3) THEN the system SHALL CONTINUE TO load the model to READY and serve
generate/generate_stream requests exactly as before

3.2 WHEN a JP5 device runs LocalServer (vLLM disabled, `VLLM_ENABLE=0`) THEN the system
SHALL CONTINUE TO start with the pre-vLLM startup sequence, with no vLLM runtime and no
behavior change

3.3 WHEN vision/ONNX/Triton inference runs on a JP7 device (with or without a vLLM model
present) THEN the system SHALL CONTINUE TO serve GPU inference correctly from the same
container (e.g. cookies-segmentation at its verified latency)

3.4 WHEN vLLM runtime startup or a vLLM model load fails THEN the system SHALL CONTINUE
TO contain the failure: the backend stays healthy, the vision stack and every other
loaded model are untouched, and only the failing model transitions to FAILED with its
reason retained

3.5 WHEN the model component's Shutdown runs (`vllm_model_prep.py --cleanup`) THEN the
system SHALL CONTINUE TO unload the model and remove the staged repository idempotently

3.6 WHEN a load fails with a KV-cache out-of-memory reason THEN the system SHALL CONTINUE
TO run the single unload -> reload recovery cycle in `vllm_model_prep.py`, and a genuinely
oversized model SHALL CONTINUE TO fail fast with the sizing remediation hint

3.7 WHEN a load request targets a model that is genuinely LOADING, FAILED, or UNKNOWN
THEN the system SHALL CONTINUE TO answer 409 with the state-info body (`state`, `reason`)
that callers (Text_Generation_API, output bindings, `vllm_model_prep.py`) rely on to
distinguish a warming model from a failed one

3.8 WHEN the JP6 or JP5 images are built THEN they SHALL CONTINUE TO contain NO
`TRITON_PTXAS_PATH` declaration — the env var is JP7-image-scoped by construction
(JP6's vllm 0.9.3 / V0 stack is untouched; JP5 ships without vLLM, `VLLM_ENABLE=0`)

### Scope Disposition (post-validation, 2026-08-15)

The original failure this spec was opened for (`EngineCore` death at
`torch.cuda.set_device` with `cudaErrorDevicesUnavailable`) is CONFIRMED ENVIRONMENTAL:
an nvargus/Argus driver defect on Thor/JP7 (driver 595.78) that blocked ALL new CUDA
context creation device-wide, cleared by `systemctl restart nvargus-daemon`. It is NOT
a LocalServer code defect; the spawn ENV fix direction is mooted. This spec now fixes:

1. **The ptxas defect (primary, validated)**: `ENV
   TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas` in `src/backend/Dockerfile.jp7` —
   hot-patch validated on jetson-thor1 (qwen READY, 40.48 GiB KV cache, generate
   served, coexisting with the three vision models on GPU).
2. **The reclaim hygiene hardening (secondary, optional)**: gate
   `_reclaim_gpu_memory` on `torch.cuda.is_initialized()` (defect 1.3 — real, cheap,
   already designed and test-scaffolded; just not the root cause).

Named follow-ups OUTSIDE this spec:

- (a) **NVIDIA bug report** for the Argus dmabuf-import poisoning (evidence chain
  complete except the deliberate reproduction: CSI capture loop → degraded →
  nvargus restart → recovered). Draft ready for filing:
  `nvidia-bug-report-draft.md` (this spec directory, 2026-08-15).
- (b) **Silent ORT CPU-fallback visibility**: the DDA ORT provider chain `CUDA → CPU`
  lets vision models reach READY without GPU, so READY currently hides a device-wide
  GPU outage — candidate follow-up spec.
- (c) **Pre-existing awscrt crash loop** ("Continuation ref count has gone negative")
  observed repeatedly on jetson-thor1 (RestartCount grew 6→10 across sessions) —
  pre-existing, separate issue.
- (d) **Deployment rollback blast radius** (defect 1.4, `failureHandlingPolicy`
  exposure) — already-deferred cloud-side spec.
