# Bugfix Requirements Document

## Introduction

During the Aug 14–15 nvargus driver defect on jetson-thor1 (JP7/Thor, driver 595.78), ALL
new CUDA context creation on the device failed (`cudaErrorDevicesUnavailable`, kernel
signature `Can't map dma attachment!` + NVRM Error(89) — the incident is documented in
the sibling spec `vllm-jp7-engine-cuda-init`, whose "Discriminating experiment
(context-limit probe, 2026-08-15)" and "Clean-window qwen load re-test (2026-08-15)"
subsections are the authoritative record and the motivation for this spec). Under that
device-wide GPU compute outage, all three deployed vision models (yolo_test,
rf-detr-seg-nano, cookies-segmentation) loaded to READY and served inference — on CPU.

The mechanism: the DDA ONNX inference engine (`OnnxRunner` in
`src/backend/dda_triton/resources_for_copy/inference_runtimes.py`, running inside each
model's Triton python-backend stub) builds its execution-provider list as
`CUDAExecutionProvider → CPUExecutionProvider` (via `__select_providers`; TensorRT is
opt-in). `ort.get_available_providers()` reports COMPILED-IN providers, so CUDA is
always "available" on GPU images even when the driver cannot create a context; when the
CUDA EP then fails to initialize inside `ort.InferenceSession(...)`, ONNX Runtime
silently falls back to the CPU EP and session creation succeeds. Each stub's failed CUDA
attempt emitted exactly one kernel `Can't map dma attachment!` + NVRM Error(89) pair
(observed 20:23:12–13 matching the three stub starts) — visible only in the device's
kernel log, never in DDA's own logs or status. The stub's `initialize()` returns, Triton
marks the model READY, and the feature-config status API reports READY exactly as it
does for healthy GPU operation. `nvidia-smi --query-compute-apps` was EMPTY with all
three models READY.

The impact: a production device ran a full day with a device-wide GPU compute outage
completely masked. "READY" hid it, and only the vLLM deployment failure (which has no
CPU fallback) exposed the problem. CPU fallback itself is a FEATURE (graceful
degradation keeps inference available); the defect is purely the lack of VISIBILITY —
the active execution provider is never captured after session creation
(`session.get_providers()` is never called), never logged beyond the pre-session
"loading ONNX model ... with providers [requested list]" line, and never surfaced in
any status payload an operator or monitor can query.

Scope guardrails: this is device-side code under `src/` (the ORT runner in the Triton
stub plus the model-status surface in the backend), so any fix requires LocalServer
component build(s) and on-hardware verification per `.kiro/steering/builds.md` before it
can be called done. All three Jetson targets are in blast radius: JP5, JP6, and JP7 all
build onnxruntime with the GPU providers (`ONNXRUNTIME_GPU=1`) and share the same
CUDA→CPU chain (the plain x86 CPU image requests CPU-only and is genuinely-CPU by
design). Build scheduling note only: this can ride a shared build cycle with other
pending device-side specs (e.g. cold-model-first-run-failure) — the specs themselves
stay independent. Cloud-side/portal display of the per-device GPU-fallback signal is a
stretch goal at most; the device-side truth signal (log + queryable status payload) is
the core of this spec, and whether an opt-in strict mode (fail the load instead of
falling back) should exist is an open question deferred to design, not assumed here.

## Bug Analysis

### Current Behavior (Defect)

When the CUDA execution provider cannot initialize (e.g. device-wide CUDA context
creation failure), ONNX models silently serve on CPU while every operator-facing
surface reports the same healthy state as GPU operation:

1.1 WHEN an ONNX model's Triton stub creates its `ort.InferenceSession` with the
default provider chain (CUDA → CPU) and the CUDA EP fails to initialize THEN the
session silently falls back to `CPUExecutionProvider`, the stub's `initialize()`
completes normally, and the model reaches READY with no DDA-visible signal that the GPU
was requested but not obtained (the only trace is a kernel-log NVRM error pair outside
DDA entirely)

1.2 WHEN an ONNX model is serving on the CPU fallback THEN the model-status surfaces
(`/feature-configurations`, `/feature-configurations/models/{name}/start`, Triton model
state) report exactly the same READY status and payload as healthy GPU operation — the
active execution provider is not captured (`session.get_providers()` is never called
after session creation), not logged, and not present in any status payload, so CPU
fallback is indistinguishable from GPU inference to any operator, API consumer, or
monitor

1.3 WHEN EVERY loaded ONNX model on a device is on CPU fallback (the signature of a
device-wide GPU outage, as on jetson-thor1 Aug 14–15) THEN the system reports all
models READY with no device-level indication that GPU inference capability is lost —
the outage is only discoverable indirectly (kernel logs, empty
`nvidia-smi --query-compute-apps`, or a workload with no CPU fallback failing)

### Expected Behavior (Correct)

2.1 WHEN an ONNX model's `ort.InferenceSession` is created THEN the system SHALL
capture the ACTIVE execution providers from the created session (via
`session.get_providers()`) and log them at INFO for every load, and WHEN the active
providers do not include the requested GPU provider (CUDA requested but the session is
CPU-only) THEN the system SHALL log a prominent WARNING for that model identifying the
requested chain, the active provider, and that inference will run degraded on CPU

2.2 WHEN a model-status surface reports an ONNX model (at minimum the
`/feature-configurations` list entries for Triton models) THEN the payload SHALL
include the model's active execution provider information (e.g. an additive field
carrying the active provider and/or a gpu-fallback flag) so that CPU fallback is
queryable and distinguishable from GPU operation by API consumers

2.3 WHEN a load-time GPU-fallback state is surfaced THEN it SHALL reflect the actual
session state established at session creation for the currently loaded stub instance
(a model reloaded after GPU recovery SHALL report GPU again; a model loaded during an
outage SHALL report the fallback until it is reloaded)

2.4 WHEN NO loaded GPU-chain ONNX model on the device has an active GPU provider (and
at least one such model is loaded) THEN the system SHALL surface a device-level
degraded-GPU signal through the model-status surface (exact shape decided in design)
so a device-wide GPU outage is visible as such rather than only as N individually
healthy-looking READY models

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the CUDA EP fails to initialize THEN the system SHALL CONTINUE TO fall back
to CPU and bring the model to READY serving correct inference results — graceful
degradation is a feature; visibility MUST NOT convert the fallback into a load failure
(any opt-in strict mode is a design-phase question, not part of this fix)

3.2 WHEN an ONNX model loads with a healthy GPU (CUDA EP active) THEN the system SHALL
CONTINUE TO load to READY and serve on GPU exactly as before, with no change to
provider selection order, session options, or inference numerics

3.3 WHEN a model targets CPU by design (manifest `device: "cpu"`, or a CPU-only image
such as the plain x86 build where the CPU EP is the requested provider) THEN the
system SHALL CONTINUE TO report READY with unchanged semantics and SHALL NOT be
flagged as degraded — a fallback signal applies only when a GPU provider was requested
but not obtained

3.4 WHEN existing consumers read the model-status payloads (`/feature-configurations`,
start/stop responses, portal frontend, shadow/status sync) THEN they SHALL CONTINUE TO
work unchanged — all new status information SHALL be additive fields only, with no
renames, removals, or type changes to existing fields

3.5 WHEN DLR or PyTorch runtime models, or vLLM models, load and serve THEN they SHALL
CONTINUE TO behave exactly as before — this fix touches the ONNX runner's session
introspection and the status surface only; the DLR/Torch runners and all vLLM paths
(including the sibling spec's territory) are out of scope

3.6 WHEN models load on JP5 or JP6 devices with healthy GPUs THEN inference behavior
SHALL CONTINUE TO be identical to today — all three JetPack targets share the ORT
CUDA→CPU chain, so the change must be verified to be behavior-preserving on every
target it ships to (per `.kiro/steering/builds.md`: component build + on-hardware
verification before done)

3.7 WHEN TensorRT is opted in via manifest `device: "tensorrt"` THEN the provider
chain (TRT → CUDA → CPU), engine-cache handling, and load behavior SHALL CONTINUE TO
work exactly as before, with the same visibility rules applied (active provider
captured and surfaced; degraded WARNING only when no GPU provider is active)
