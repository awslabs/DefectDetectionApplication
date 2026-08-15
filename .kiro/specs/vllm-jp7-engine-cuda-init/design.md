# vLLM JP7 Engine CUDA Init Bugfix Design

> **⚠️ RE-SCOPED (2026-08-15).** The fork-after-CUDA-init analysis in this
> Overview was REFUTED on hardware and the original CUDA-init failure was
> proven ENVIRONMENTAL (nvargus/Argus driver defect — see bugfix.md
> "Re-hypothesis"). The design's authoritative sections are now
> **"Validated Root Cause and Fix (Re-scope, 2026-08-15)"**, the reworked
> **Correctness Properties**, and the reworked **Testing Strategy** below.
> The Overview, Glossary, Bug Details, Hypothesized Root Cause, and the
> original Fix Implementation content are retained unmodified for the
> record per house style.

## Overview

On JP7/Thor, every vLLM model load dies before the engine exists: the V1
EngineCore subprocess (`EngineCore_DP0`) is **forked** from the LocalServer
backend process, whose process tree has already initialized CUDA (in-process
ONNX/Triton vision inference, plus the runtime manager's own memory-reclaim
handler). A forked child re-initializing CUDA on Jetson/Tegra gets exactly the
observed `torch.AcceleratorError: cudaErrorDevicesUnavailable` at
`torch.cuda.set_device`, the model latches FAILED, the load endpoint answers
409, the model component exits 1 on every Greengrass retry, goes BROKEN, and
the whole deployment rolls back — taking three healthy vision models with it
(deployment aebc9d9a, `FAILED_ROLLBACK_COMPLETE`).

The root-cause chain is now confirmed against the pinned vLLM source
(v0.11.2, the JP7 image's from-source build):

1. `AsyncLLM` launches EngineCore via `CoreEngineProcManager`
   (`vllm/v1/engine/utils.py`), which names the process
   `EngineCore_DP{n}` — exactly the process observed dying on-device — and
   takes its multiprocessing context from `get_mp_context()`
   (`vllm/utils/system_utils.py`).
2. `get_mp_context()` honors `VLLM_WORKER_MULTIPROC_METHOD` first: when the
   env var is `spawn`, `_maybe_force_spawn()` returns immediately and the
   spawn context is used. When unset, the default is **fork**.
3. vLLM's own fork-safety guard, `cuda_is_initialized()`, only sees
   **torch-level** CUDA state (`torch.cuda.is_initialized()`). It is blind to
   driver-level `cuInit` performed by other libraries in the same process —
   onnxruntime's CUDA execution provider, the embedded Triton, or torch's own
   `torch.cuda.is_available()` device-count probe. Upstream vLLM issue #32611
   documents this exact detection gap (there triggered via pynvml). So the
   guard never forces spawn for us, and fork wins.

The fix is therefore two-sided and deliberately minimal:

1. **Declare spawn for the JP7 image** — `ENV VLLM_WORKER_MULTIPROC_METHOD=spawn`
   in `src/backend/Dockerfile.jp7`, mirroring the existing per-image engine
   contract convention (`Dockerfile.jp6` pins `ENV VLLM_USE_V1=0` the same
   way). Every EngineCore launch then starts from a CUDA-clean spawned
   interpreter regardless of what the parent has done with the GPU. JP6 and
   JP5 images are untouched by construction.
2. **Stop the manager poisoning its own parent process** — `_reclaim_gpu_memory`
   currently calls `torch.cuda.is_available()` (a driver-initializing probe)
   on every load failure and unload. Gate reclaim on
   `torch.cuda.is_initialized()` (a pure state read) instead: on JP6's V0
   in-process engine, torch is initialized whenever there is engine memory to
   reclaim, so reclaim behavior is preserved; on JP7's V1 subprocess engine,
   the parent's torch never held the engine memory, so the call was a no-op
   placebo that only initialized parent CUDA — guaranteeing bug condition
   re-entry on every retry (defect 1.3).

`vllm_model_prep.py` needs **no change**: its retry semantics were already
correct (an authoritative 409 exits 1, Greengrass restarts the component, the
manager genuinely re-attempts a FAILED model's load). The retries could never
recover only because every attempt forked from the same poisoned parent; with
spawn, each retry launches a fresh CUDA-clean engine child (defect 1.2 /
expected behavior 2.3).

The deployment blast radius (defect 1.4) is a Greengrass policy question, not
a device-code defect: `deployments.py` (cloud side) already exposes
`failureHandlingPolicy: ROLLBACK | DO_NOTHING` via `rollout_config.auto_rollback`
per deployment, and Greengrass offers no per-component policy. This spec
removes the trigger (the vLLM component now deploys cleanly); whether the
portal should default or surface `DO_NOTHING` for mixed-model deployments is
explicitly deferred to a follow-up cloud-side spec, keeping this spec
device-only per the requirements' scope guardrail.

Because both changed files are baked into the LocalServer image, the fix
requires one JP7 component build (~1-2h) and on-hardware verification on
jetson-thor1 per `.kiro/steering/builds.md` — with a cheap pre-build
hot-patch validation on the device so the 1-2h build is only dispatched once
the fix is already demonstrated working.

## Glossary

- **Bug_Condition (C)**: a vLLM engine-core launch on a JP7 device performed
  with the fork start method from a CUDA-initialized parent process — the
  condition under which EngineCore dies at `torch.cuda.set_device` with
  `cudaErrorDevicesUnavailable`
- **Property (P)**: the desired behavior — the engine core process starts in
  a CUDA-clean context and the model reaches READY, regardless of the parent
  process's prior GPU activity
- **Preservation**: JP6 V0 in-process engine behavior (including memory
  reclaim after failures/unloads and the KV-cache OOM recovery cycle), JP5's
  vLLM-free startup, JP7 vision/ONNX inference, failure containment, the 409
  state-info contract, and `vllm_model_prep.py` staging/cleanup semantics
- **EngineCore / `EngineCore_DP0`**: vLLM V1's engine subprocess, launched by
  `CoreEngineProcManager` (`vllm/v1/engine/utils.py`) from within
  `AsyncLLM.from_engine_args` — the process observed dying on-device
- **`get_mp_context()`**: vLLM's multiprocessing-context selector
  (`vllm/utils/system_utils.py`): `VLLM_WORKER_MULTIPROC_METHOD` when set
  (early-return for `spawn`), else fork by default with a torch-level-only
  auto-spawn guard (`cuda_is_initialized()`)
- **fork-after-CUDA-init**: forking a process after the CUDA driver has been
  initialized in it (`cuInit` — by torch, onnxruntime, or any other library);
  the forked child cannot re-initialize CUDA on Jetson/Tegra and fails with
  `cudaErrorDevicesUnavailable`
- **spawn**: the multiprocessing start method that launches a fresh Python
  interpreter; the child re-imports the parent's `__main__` (app.py) under
  the name `__mp_main__`, so app.py's `if __name__ == "__main__"` guard keeps
  all startup side effects (Triton setup, uvicorn, engine start) out of the
  child — only the module-level app construction is re-executed
- **`VllmRuntimeManager`**: `src/backend/vllm_runtime/manager.py` — owns every
  vLLM model on the device, runs **in-process** inside the backend (uvicorn
  daemon thread via `app.py::start_vllm_runtime`), with injectable
  `engine_factory` / `sampling_params_factory` for GPU-free tests
- **`_reclaim_gpu_memory`**: the manager's best-effort CUDA memory release on
  `_fail()` and `unload()`; today it probes `torch.cuda.is_available()`,
  which driver-initializes CUDA in the parent backend process
- **`vllm_model_prep.py`**: `src/backend/dda_triton/vllm_model_prep.py` — the
  model component's Startup/Shutdown script: validate → stage → POST load,
  exit non-zero on authoritative failure so Greengrass retries
- **V0 / V1 engine**: JP6 pins vLLM 0.9.3 with `VLLM_USE_V1=0` (in-process
  classic engine, no CUDA subprocess); JP7 builds vLLM v0.11.2 from source
  (V1 only — the classic API is a build-script compatibility subclass of the
  V1 `AsyncLLM`, so `from_engine_args` launches EngineCore as a subprocess)

## Bug Details

### Bug Condition

The bug manifests when a vLLM engine core is launched on a JP7 device: the
launch inherits the container's default `fork` start method (no
`VLLM_WORKER_MULTIPROC_METHOD` is set anywhere in the JP7 image, compose
service, or component recipe), and the backend parent process has initialized
CUDA at the driver level — through vision/ONNX inference, a prior engine
launch attempt, or the manager's own `_reclaim_gpu_memory` (which runs inside
`_fail()`, so the very first load failure guarantees the condition for every
subsequent attempt).

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type EngineCoreLaunch
         { device: JetPackTarget, startMethod: fork | spawn,
           parentCudaInitialized: boolean }
  OUTPUT: boolean

  RETURN input.device = JP7                     // V1 engine: subprocess launch
         AND input.startMethod = fork           // no spawn declared anywhere
         AND input.parentCudaInitialized        // ONNX/Triton inference, prior
                                                // attempt, or reclaim handler
END FUNCTION
```

On the unfixed tree, `startMethod` is always `fork` (nothing sets the env var
and vLLM's auto-guard cannot see non-torch CUDA init), and
`parentCudaInitialized` is true in every realistic deployment (the three
vision models were serving; and even on an otherwise idle GPU, the first
failure's reclaim call poisons the parent for all retries) — so every JP7
engine launch satisfies C(X).

### Examples

- **The live incident**: deploy `model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp7`
  v1.0.0 to jetson-thor1 alongside three vision models → Startup POSTs
  `/v2/repository/models/qwen3-vl-8b-instruct/load` → EngineCore_DP0 dies at
  `torch.cuda.set_device` with `cudaErrorDevicesUnavailable` → 409
  `{"state":"FAILED","reason":"Engine core initialization failed..."}` →
  component exits 1, exhausts retries, BROKEN → deployment aebc9d9a
  `FAILED_ROLLBACK_COMPLETE`, three healthy vision models removed.
  Expected: model STAGED → LOADING → READY, HTTP 200, vision models untouched.
- **Idle-GPU reproduction** (rules out contention): backend container
  restarted, GPU idle, manual load request → identical EngineCore death.
  Expected: READY. This is what pins the cause on process state, not GPU load.
- **Retry futility** (defect 1.2): each Greengrass restart of the component
  re-drives the load; the manager genuinely re-attempts, but the new
  EngineCore forks from the same CUDA-initialized parent → identical failure
  every time, even after the triggering condition (if transient) cleared.
  Expected: a genuine, uncontaminated re-attempt per retry (2.3).
- **Self-poisoning** (defect 1.3): even a parent that had never touched CUDA
  fails permanently after one failed load — `_fail()` →
  `_reclaim_gpu_memory()` → `torch.cuda.is_available()` → driver-level CUDA
  init in the parent → every later fork is poisoned. Expected: the failure
  handler must not initialize CUDA in the parent.
- **Edge case — JP6 load (must NOT change)**: V0 engine is constructed
  in-process; no subprocess, no fork hazard; `_reclaim_gpu_memory` finds
  torch CUDA initialized (the engine lived in-process) and must still
  `empty_cache()` — this is the validated KV-cache OOM recovery substrate
  (3.1, 3.6).

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- JP6 (V0 in-process engine, vllm 0.9.3, `VLLM_USE_V1=0`): model loads to
  READY, generate/generate_stream serve exactly as before; memory reclaim
  after failure/unload still runs (torch is initialized in-process whenever
  engine memory exists); the KV-cache OOM unload → reload recovery still
  works (3.1, 3.6)
- JP5 (`VLLM_ENABLE=0`): pre-vLLM startup sequence, no vLLM runtime, no
  behavior change (3.2)
- JP7 vision/ONNX/Triton inference from the same container, with or without a
  vLLM model present (3.3)
- Failure containment: a vLLM startup or load failure never takes down the
  backend, the vision stack, or other loaded models; only the failing model
  transitions to FAILED with its reason retained (3.4)
- `vllm_model_prep.py`: idempotent Shutdown cleanup (3.5), the single
  unload → reload KV-cache recovery cycle and fail-fast sizing hint (3.6),
  and the whole file byte-identical (no changes needed)
- The 409 state-info contract (`state`, `reason`) for LOADING / FAILED /
  UNKNOWN models that Text_Generation_API, output bindings, and
  `vllm_model_prep.py` rely on (3.7)
- The manager's per-model state machine, lock discipline, injectable
  factories, multimodal path, and every existing test under
  `test/backend-test/vllm_runtime/`, `test/backend-test/vllm_runtime_tests/`,
  `test/backend-test/text_generation/`, and the health gating under
  `test/backend-test/deploy_reliability/`

**Scope:**
All inputs that do NOT involve a JP7 engine-core launch are completely
unaffected. This includes:
- Every JP6/JP5 code path (their images gain no new env var; the manager
  change is behavior-preserving where torch CUDA is already initialized)
- Every vision-model deployment and inference request on any target
- Every vLLM request-serving path after READY (generate, streaming,
  multimodal) — the fix touches only launch context and failure-handler
  hygiene

## Hypothesized Root Cause

Confirmed against the pinned vLLM v0.11.2 source and two live on-device
reproductions (one on an idle GPU); the exploration phase still pins each leg
before the fix lands:

1. **Fork inherited by default** (primary): `CoreEngineProcManager` takes its
   context from `get_mp_context()`, which defaults to fork when
   `VLLM_WORKER_MULTIPROC_METHOD` is unset — and nothing in the JP7 image,
   compose service, or component recipe sets it. The container's start method
   was verified `fork` in-container.

2. **vLLM's auto-spawn guard is torch-blind**: `_maybe_force_spawn()` checks
   `cuda_is_initialized()` — i.e. `torch.cuda.is_initialized()` — which is
   false even while the process holds a driver-level CUDA context created by
   onnxruntime/Triton vision inference or by `torch.cuda.is_available()`
   probes. Upstream issue vllm-project/vllm#32611 documents the same gap. So
   the guard never rescues us.

3. **The manager guarantees the poisoned-parent condition** (defect 1.3):
   `_fail()` → `_reclaim_gpu_memory()` → `torch.cuda.is_available()` performs
   driver-level CUDA init in the backend parent on the very first failure, so
   even a hypothetically CUDA-clean parent stays poisoned for every retry.
   On JP7/V1 the reclaim is also pointless: the engine memory lives in the
   (dead) child, not the parent.

4. **JP7-only exposure**: JP6 pins the V0 in-process engine (`VLLM_USE_V1=0`,
   vllm 0.9.3 — no CUDA subprocess, nothing to fork); JP5 ships without vLLM
   (`VLLM_ENABLE=0`). The JP7 image's classic-API shim
   (`install_vllm_gpu.sh`: `AsyncLLMEngine` subclasses the V1 `AsyncLLM`)
   means the manager's unchanged `from_engine_args` call transparently became
   a subprocess launch on JP7 — the fork hazard arrived with the engine
   architecture, not with any manager change.

5. **Cascade amplifiers** (named, not fixed here): the Greengrass deployment
   `failureHandlingPolicy` defaults to ROLLBACK, so one BROKEN model
   component removes every sibling (defect 1.4; `deployments.py` already
   supports `DO_NOTHING` via `rollout_config.auto_rollback` — portal-side
   exposure deferred to a follow-up cloud-side spec). The dmesg `NVRM ...
   osCreateOsDescriptorFromFileHandle: Error (89)` spam observed during the
   degraded period is driver noise independent of this bug (the reproduction
   succeeded identically after it cleared); the on-hardware verification
   records whether it recurs, for a potential NVIDIA report, and nothing in
   this fix depends on it.

## Validated Root Cause and Fix (Re-scope, 2026-08-15)

All evidence in this section is on-device validated on jetson-thor1 and
recorded in bugfix.md's Re-hypothesis subsections (the authoritative
chronology).

### What the original failure actually was (environmental, out of code scope)

The `cudaErrorDevicesUnavailable` death at `torch.cuda.set_device` — the
failure this spec was opened for, reproduced twice including on an idle GPU —
was an **nvargus/Argus driver defect on Thor/JP7 (driver 595.78)**: from Aug 14
17:17:31 (coincident with nvargus/ISP CSI capture activity) the daemon held a
poisoned state in which ALL new CUDA context creation failed device-wide
(kernel signature `Can't map dma attachment!` + NVRM Error(89), 1:1 per failed
context creation, 200k+ occurrences), while pre-existing contexts kept working.
`systemctl restart nvargus-daemon` cleared it instantly. Every prior
reproduction — including deployment aebc9d9a itself and the task 3 spawn
hot-patch test — ran inside that degraded window. Outside it, the engine
passes CUDA init flawlessly from the same backend process tree, fork default
and all. **The spawn ENV (old fix step 1) is therefore mooted**; the Argus
defect goes to an NVIDIA bug report (follow-up (a), bugfix.md Scope
Disposition), not to LocalServer code.

### The real image defect: triton's bundled ptxas cannot target sm_110a

Validated root-cause chain (clean-window re-test + hot-patch validation,
2026-08-15):

1. The engine core initializes CUDA, resolves
   `Qwen3VLForConditionalGeneration`, selects the FLASH_ATTN backend, and
   loads weights (16.6 GiB) without incident.
2. During `determine_available_memory` → `profile_run`, the vision encoder's
   rotary-embedding kernel (`vllm/vllm_flash_attn/ops/triton/rotary.py`) —
   like any Triton-JIT path — is compiled: triton emits PTX, then shells out
   to **its own BUNDLED ptxas, which is CUDA 12.8 (V12.8.93)**.
3. Thor's compute architecture is `sm_110a` (CUDA 13.x-era); ptxas 12.8 does
   not know it: ``ptxas fatal : Value 'sm_110a' is not defined for option
   'gpu-name'`` → `triton.runtime.errors.PTXASError`.
4. The engine dies, the model latches FAILED ("Engine core initialization
   failed"), the load endpoint answers 409, the component exits 1, Greengrass
   retries deterministically hit the same codegen failure, the component goes
   BROKEN, and the deployment rolls back.

This hits **any vLLM model whose execution path JIT-compiles a Triton
kernel** — it is a JP7-image defect, not a model-specific one.

### Fix step 1 (primary, validated): `TRITON_PTXAS_PATH` in Dockerfile.jp7

Add to `src/backend/Dockerfile.jp7`, adjacent to the vLLM from-source build
layer (the layer carrying the "no `ENV VLLM_USE_V1` here" engine-contract
note):

```dockerfile
# Triton (the torch/vLLM JIT compiler, pinned triton==3.5.0 above) ships its
# own BUNDLED ptxas, which is CUDA 12.8 (V12.8.93) and rejects Thor's
# sm_110a ("ptxas fatal : Value 'sm_110a' is not defined for option
# 'gpu-name'"): any vLLM model whose execution path JIT-compiles a Triton
# kernel dies with PTXASError during the engine's profile_run
# (spec: vllm-jp7-engine-cuda-init). Point triton at the image's system
# CUDA 13.x ptxas instead, which accepts sm_110a. Validated on-device
# (jetson-thor1, 2026-08-15): with this env the qwen3-vl-8b engine
# profiles, sizes its KV cache, reaches READY, and serves, coexisting with
# the vision models on GPU. JP6 (vllm 0.9.3 / V0, cu122 stack) and JP5
# (no vLLM) take no analogous env var.
ENV TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas
```

Placement rationale (mirrors the old step 1's reasoning):
- **Not `src/docker-compose.yaml`**: shared across targets — would leak onto
  JP6's backend service (preservation 3.8).
- **Not the recipes**: build artifacts of the component, not image contracts.
- **Dockerfile ENV is JP7-image-scoped by construction**, exactly how
  `Dockerfile.jp6` scopes `ENV VLLM_USE_V1=0`.

**Risk/limitation (stated honestly)**: `TRITON_PTXAS_PATH` points triton at a
NEWER ptxas than its bundled CUDA 12.8 runtime. In this image that is the
right pairing: the Dockerfile pins the base at CUDA 13.0.2 (nvcc verified
13.0 in-build; the deployed image observed on-device reports system ptxas
CUDA 13.2 V13.2.78 — both 13.x, both accept `sm_110a`), and torch/vLLM are
themselves built against CUDA 13.x (`torch==2.9.0+cu130`, vLLM compiled
from source for sm_110 in-image). Cross-version PTX→cubin assembly is the
normal CUDA compatibility direction (newer ptxas assembling older-ISA PTX),
and the pairing is validated end-to-end on hardware: profile pass completed,
40.48 GiB KV cache sized, READY, generate served. Note for the acceptance
check: `sm_110a` requires PTX ISA ≥ 9.0 (`.version 9.0` compiles; `.version
8.0` is rejected).

### Fix step 2 (optional hardening, kept): reclaim without initializing CUDA

Defect 1.3 is real even though it was not the root cause: the manager's
failure handler driver-initializes CUDA in the parent backend process. Carry
over the old step 2 unchanged — in
`src/backend/vllm_runtime/manager.py::_reclaim_gpu_memory`, replace the
driver-initializing probe with a pure state read:

```python
# BEFORE (driver-initializes CUDA in the parent on every failure/unload)
if torch.cuda.is_available():
    torch.cuda.empty_cache()

# AFTER (pure state read; empty_cache only where torch CUDA already lives)
if torch.cuda.is_initialized():
    torch.cuda.empty_cache()
```

- `torch.cuda.is_initialized()` never initializes CUDA; `empty_cache()` is
  only meaningful when torch's allocator is initialized in **this** process —
  exactly the JP6/V0 in-process case (preserved) and never the JP7/V1 case
  (engine memory lives in the child).
- Keep the lazy `import torch`, the `ImportError` return, the broad exception
  swallow, and the log lines exactly as they are (Property 2); update the
  docstring with the invariant: *reclaim must never be the first CUDA touch
  in a process*.
- Correct the stale module docstring in passing: "(JetPack 6)" →
  "(JetPack 6 / JetPack 7)".
- Existing call sites (`_fail`, `unload`) untouched, so
  `test_manager_memory_reclaim.py`'s call-site tests pass unchanged.

### Fix step 3: rebaseline the masked Dockerfile.jp7 golden

Step 1 changes a preservation-tracked file. Per `.kiro/steering/builds.md`,
update `docker_baseline_backend_Dockerfile.jp7_masked.txt` in the same
commit and re-run the preservation suite in the flask-app container before
dispatching the build. `Dockerfile.jp7` has no sha256 entry in
`docker_baseline_out_of_scope.json`, so only the masked golden changes.

### No other code changes

`vllm_model_prep.py` stays byte-identical (its retry loop is correct);
`vllm_runtime/server.py`, `app.py`, compose, recipes, JP6/JP5 Dockerfiles
untouched. Cloud side untouched (defect 1.4 deferred — Scope Disposition
follow-up (d)).

## Correctness Properties

> Reworked at the 2026-08-15 re-scope. Property 1 now targets the validated
> ptxas defect; Property 2 gains the JP6/JP5 no-new-env clause (3.8);
> Property 3 (reclaim hygiene) is unchanged.

Property 1: Bug Condition - JP7 Triton-JIT Compilation Succeeds for sm_110a

_For any_ vLLM engine-core initialization on a JP7 device whose profile run
JIT-compiles a Triton kernel (the bug condition: triton's bundled CUDA 12.8
ptxas cannot codegen for Thor's `sm_110a`), the fixed configuration SHALL
cause the compilation to use the image's system CUDA 13.x ptxas — the JP7
image declares `ENV TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas`, which
triton honors for its PTX assembly step — so the profile run completes
without `PTXASError` and a staged, correctly-packaged model transitions
STAGED → LOADING → READY with HTTP 200, on the first attempt and on every
retry. Config leg (the ENV is declared exactly once in Dockerfile.jp7 and
nowhere else) is testable GPU-free; the behavioral leg is validated on
hardware (hot-patch validation done 2026-08-15; built-component acceptance
in task 10).

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Property 2: Preservation - Non-JP7-Launch Behavior Is Unchanged

_For any_ input where the bug condition does NOT hold (every JP6 V0 load and
serve path, JP5's vLLM-free startup, JP7 vision/ONNX inference, failure
containment, the 409 state-info contract, staging/cleanup, and memory reclaim
in any process whose torch CUDA is already initialized), the fixed code SHALL
produce the same result as the original code — including that
`_reclaim_gpu_memory` still calls `torch.cuda.empty_cache()` whenever
`torch.cuda.is_initialized()` is true, preserving JP6's validated post-failure
memory recovery — and the JP6/JP5 images SHALL gain NO new env var
(no `TRITON_PTXAS_PATH` anywhere outside Dockerfile.jp7).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**

Property 3: Fix Checking - The Failure Handler Never Initializes CUDA

_For any_ failure or unload handled by the fixed `VllmRuntimeManager` in a
process whose torch CUDA is NOT initialized, `_reclaim_gpu_memory` SHALL
perform no CUDA-initializing torch call (`torch.cuda.is_available()` or any
other driver-initializing probe) and SHALL swallow every torch error exactly
as before — so the manager itself can never re-create the bug condition's
`parentCudaInitialized` leg, and a load re-attempt after a failure starts
from an uncontaminated parent. (Post-re-scope status: this is the OPTIONAL
HARDENING for hygiene defect 1.3 — the reworked requirement 2.3 carries the
hardening clause.)

**Validates: Requirements 2.3**

## Fix Implementation

> **⚠️ REFUTED ON HARDWARE (2026-08-15, jetson-thor1 — tasks.md task 3) —
> RE-SCOPE COMPLETE.** The spawn fix direction below was validated by
> hot-patch on the device and **did NOT fix the bug**: with
> `VLLM_WORKER_MULTIPROC_METHOD=spawn` verifiably active (spawn's module
> re-import observed in the `EngineCore_DP0` child's log), the engine child
> died at the identical location (`torch.cuda.set_device` →
> `cudaErrorDevicesUnavailable`, HTTP 409). The subsequent discriminators
> (bugfix.md Re-hypothesis chain) proved that failure ENVIRONMENTAL
> (nvargus/Argus driver defect, cleared by a daemon restart) and surfaced the
> real image defect: triton's bundled CUDA 12.8 ptxas rejects Thor's
> `sm_110a`. **The re-scope is COMPLETE — the authoritative fix is now
> "Validated Root Cause and Fix (Re-scope, 2026-08-15)" above** (fix step 1:
> `ENV TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas` in Dockerfile.jp7,
> hot-patch validated; fix step 2: the reclaim hygiene hardening below, which
> survives unchanged; fix step 3: the masked-golden rebaseline). Step 1
> (spawn ENV) below is MOOTED and must NOT be implemented. The content below
> is retained unmodified for the record.

### Changes Required

Assuming our root cause analysis is correct (the exploration phase confirms it
first, including a pre-build on-device hot-patch validation):

**Step 1 — `src/backend/Dockerfile.jp7`: declare spawn for the engine launch**

Add, adjacent to the vLLM from-source build layer (which already carries the
"no `ENV VLLM_USE_V1` here" engine-contract note):

```dockerfile
# vLLM V1 launches EngineCore as a subprocess (CoreEngineProcManager →
# get_mp_context()). The default start method is fork, and vLLM's auto-spawn
# guard only detects torch-level CUDA init — it is blind to the driver-level
# CUDA context this backend already holds (onnxruntime/Triton vision
# inference in-process). A forked EngineCore re-initializing CUDA on Tegra
# dies at torch.cuda.set_device with cudaErrorDevicesUnavailable
# (spec: vllm-jp7-engine-cuda-init). Spawn starts the engine from a fresh,
# CUDA-clean interpreter; app.py's __main__ guard keeps startup side effects
# out of the re-imported child. JP6 pins the V0 in-process engine instead
# (Dockerfile.jp6: ENV VLLM_USE_V1=0) and takes no analogous env var.
ENV VLLM_WORKER_MULTIPROC_METHOD=spawn
```

Rationale for this placement over the alternatives:
- **Not `src/docker-compose.yaml`**: the compose file is shared across
  targets; an env there would leak onto JP6's backend service. The Dockerfile
  ENV is JP7-image-scoped by construction, mirroring how JP6 scopes
  `VLLM_USE_V1=0`.
- **Not code-level `os.environ.setdefault` in the manager**: the manager is
  shared code running on JP6 too; gating it on runtime probes couples the
  manager to per-image build knowledge that the Dockerfiles already own
  declaratively.
- **Not an out-of-process runtime manager**: moving the manager out of the
  backend process would fix the fork hazard too, but requires re-plumbing the
  Text_Generation_API router, the feature-config status merge, and health
  gating through IPC — a large refactor with its own failure modes, and
  unnecessary once EngineCore itself spawns clean. Rejected as non-minimal.

Spawn-safety in this deployment shape (verified in source): the spawn child
re-imports app.py as `__mp_main__`; all startup side effects (Triton setup,
DB migration, uvicorn binds, `start_vllm_runtime`) sit under
`if __name__ == "__main__":`, so the child only re-executes module-level app
construction. vLLM's spawn target (`EngineCoreProc.run_engine_core` with
msgspec-serialized config) is vLLM's own supported spawn path.

**Step 2 — `src/backend/vllm_runtime/manager.py`: reclaim without initializing CUDA**

In `_reclaim_gpu_memory`, replace the driver-initializing probe with a pure
state read:

```python
# BEFORE (poisons the parent: is_available() driver-initializes CUDA)
if torch.cuda.is_available():
    torch.cuda.empty_cache()

# AFTER (pure state read; empty_cache only where torch CUDA already lives)
if torch.cuda.is_initialized():
    torch.cuda.empty_cache()
```

- `torch.cuda.is_initialized()` never initializes CUDA; `empty_cache()` is
  only meaningful when torch's allocator is initialized in **this** process,
  which is exactly the JP6/V0 in-process case (preserved) and never the
  JP7/V1 case (engine memory lives in the child).
- Keep the lazy `import torch`, the `ImportError` return, the broad
  exception swallow, and the log lines exactly as they are (Property 2);
  update the docstring to state the invariant: *reclaim must never be the
  first CUDA touch in a process* (defect 1.3).
- Existing call sites (`_fail`, `unload`) are untouched, so
  `test_manager_memory_reclaim.py`'s call-site tests pass unchanged.

**Step 3 — `test/backend-test/security/baselines/`: rebaseline the intended Dockerfile.jp7 change**

Step 1 changes a preservation-tracked file. Per `.kiro/steering/builds.md`,
update `docker_baseline_backend_Dockerfile.jp7_masked.txt` (the masked
non-`FROM` golden that `test_preservation_docker_masked_bytes.py` pins) in the
same commit, and re-run the preservation suite in the flask-app container to
confirm green BEFORE dispatching the build. `Dockerfile.jp7` has no sha256
entry in `docker_baseline_out_of_scope.json` (only the JP5 `Dockerfile`,
compose, edgemlsdk, frontend), so only the masked golden needs rebaselining.

**Step 4 — no other code changes**

- `vllm_model_prep.py`: byte-identical. Its authoritative-409 → exit 1 →
  Greengrass-retry loop is correct; with steps 1-2 each retry is a genuine
  re-attempt (2.3).
- `vllm_runtime/server.py`, `app.py`, compose, recipes: untouched.
- Cloud side: untouched (scope guardrail). The `failureHandlingPolicy`
  exposure question (defect 1.4) is deferred to a follow-up cloud-side spec.

## Cross-Spec Documentation Consistency

| Document | Affected claim | Amendment |
|---|---|---|
| `.kiro/specs/vllm-multi-arch-publish-conflict/` (tasks.md, open on-hardware verification) | Its deferred JP7 deploy test "anticipated exactly this test" — the deploy it was waiting on is the one that failed here | Append a note: the on-hardware verification is fulfilled by `.kiro/specs/vllm-jp7-engine-cuda-init/`'s jetson-thor1 deployment (cloud publish/packaging confirmed working; the device-side failure it surfaced is fixed by this spec) |
| `.kiro/specs/jp7-vllm-enablement/design.md` | Documents the JP7 vLLM engine/toolchain contract (triton==3.5.0 pinned with the torch cu130 stack; vLLM compiled for sm_110) without noting that triton's BUNDLED ptxas (CUDA 12.8) cannot codegen for Thor's `sm_110a` | Append a note: the JP7 image declares `ENV TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas` (added by `.kiro/specs/vllm-jp7-engine-cuda-init/`) so Triton-JIT kernel compilation uses the image's system CUDA 13.x ptxas — without it any vLLM model whose execution path JIT-compiles a Triton kernel dies with PTXASError during profile_run |
| `src/backend/vllm_runtime/manager.py` module docstring | "the ``vllm`` package only exists on vLLM-capable images (JetPack 6)" — stale since JP7 enablement | Correct in passing during step 2 to "(JetPack 6 / JetPack 7)"; no semantic change |
| `.kiro/specs/cold-model-first-run-failure/` | Also requires a JP7 LocalServer build when implemented | No amendment to the spec itself (do NOT couple them); this spec's build task notes the two fixes MAY share one JP7 build cycle if the user sequences them together |

## Deployment and On-Hardware Verification

Per `.kiro/steering/builds.md` — one build at a time, security gate
pre-checked, no portal deploys mid-build, and an on-device change is not
"done" until verified on real hardware from a built+deployed component.

1. **Pre-build hot-patch validation on jetson-thor1 — ALREADY DONE
   (2026-08-15)**: `TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas` was
   hot-patched into the backend environment, the qwen load answered HTTP 200
   on the first attempt, the profile pass completed (40.48 GiB KV cache),
   the model reached READY coexisting with the three vision stubs on GPU,
   and a generate request served — then the hot-patch was fully reverted
   (see bugfix.md "TRITON_PTXAS_PATH hot-patch validation"). The 1-2h build
   is dispatched only against this already-demonstrated fix; the built
   component must still prove itself from a clean deployment (step 4). The
   17 GB HF weight cache left on the device makes the acceptance load skip
   the download.
2. **Pre-build gate** (builds.md checklist): rebaseline
   `docker_baseline_backend_Dockerfile.jp7_masked.txt`; run the preservation
   suite in the flask-app container and the two out-of-scope guard tests;
   move `cdk.out` aside; confirm no build is running.
3. **JP7 build**: swap `gdk-config.json` to
   `aws.edgeml.dda.LocalServer.arm64JP7`, `gdk component build`, log to
   `.gdk_build_jp7.log` (~1-2h). NOTE: the cold-model-first-run-failure spec
   will also need a JP7 build when implemented — if the user sequences both
   fixes together, they may share this one build cycle (the specs stay
   independent either way).
4. **Deploy to jetson-thor1 and verify the original failing scenario (2.4)**:
   deploy the new LocalServer.arm64JP7 together with
   `model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp7` AND the three vision
   model components (the deployment shape of aebc9d9a). Acceptance: the
   deployment COMPLETES (no rollback), qwen reaches READY and answers a
   generate request, all three vision models remain deployed and serving at
   their verified latency (3.3), and the backend stays healthy for a
   sustained period (no crash-loop, no container restart).
5. **JP6 regression (3.1)**: the manager change rides the NEXT JP6 build —
   the currently deployed JP6 image is untouched by this spec, so the
   on-device JP6 regression risk is zero until then. Required now: the full
   existing vLLM suites green in the flask-app container (JP6 uses
   python3.10 — the container command's interpreter shim picks it), plus a
   spot check that the JP6 device's deployed vLLM model
   (qwen on ryanorinagxdevkithomelabjp622) is still READY and generating.
   An optional JP6 build+deploy to prove the shared manager change on JP6
   hardware ahead of its next scheduled build is offered as a user decision
   in tasks.md (it costs a second 1-2h cycle and must be sequenced after the
   JP7 build).

## Testing Strategy

### Validation Approach

> Reworked at the 2026-08-15 re-scope: the exploration suite
> (`test_exploration_fork_cuda_init.py` — the filename is historical, kept
> to preserve the task 1 record; task 4.4 adds a module-docstring note) is
> re-pointed at the ptxas defect.

Two-phase: first pin the bug condition (config assertions plus a GPU-free
behavioral test of the reclaim-hygiene leg; the decisive behavioral evidence
is the on-device chronology in bugfix.md, already gathered), then verify the
fix and preservation. **Honesty note**: no GPU-free test executes ptxas or
CUDA — the behavioral claim (system ptxas → profile run completes → READY)
is validated on hardware: the hot-patch validation (DONE 2026-08-15) and the
built-component acceptance (task 10, requirement 2.4). The container-level
tests pin everything that is pinnable without a GPU: the causal
configuration (the ENV declaration and its scoping), the reclaim hygiene
behavior, and the preserved behaviors.

### Exploratory Bug Condition Checking

**Goal**: Pin the bug condition on UNFIXED code. (Historical note: the
original suite's case 1 asserted the spawn ENV; the on-device chronology
refuted that direction and the suite is re-scoped by task 4.4 to the
validated ptxas fix. Cases 2 and 3 carry over unchanged in intent.)

**Test Plan**: suite `test/backend-test/vllm_jp7_engine_cuda_init/`
(following the sibling `test/backend-test/vllm_runtime/` convention:
`sys.path.insert` shim to `src/backend`, runnable in the flask-app
container). Cases 1–2 fail on the unfixed tree; all pass on the fixed tree.

**Test Cases**:
1. **TRITON_PTXAS_PATH is declared for JP7** (config-level): `Dockerfile.jp7`
   contains exactly one `ENV TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas`
   line (FAILS on unfixed code — the line is absent, which IS the bug
   condition: triton falls back to its bundled CUDA 12.8 ptxas, which cannot
   codegen for `sm_110a`); the same test documents the input surface by
   asserting no other config source (`src/docker-compose.yaml`, recipe
   variants) sets the variable (compose is shared with JP6 — preservation 3.8)
2. **The failure handler does not initialize CUDA** (behavioral, GPU-free,
   the defect 1.3 hardening): inject a fake `torch` into `sys.modules` whose
   `cuda.is_initialized()` returns False and which records every `cuda.*`
   call; drive `VllmRuntimeManager._fail` via a load whose engine factory
   raises; assert no CUDA-initializing call (`is_available`) was made (FAILS
   on unfixed code — `is_available` is called, the defect 1.3 counterexample)
3. **JP6/JP5 engine contracts untouched** (documents F(X), PASSES on unfixed
   code, must NOT be inverted): `Dockerfile.jp6` pins `ENV VLLM_USE_V1=0`
   and contains NO `VLLM_WORKER_MULTIPROC_METHOD` and NO `TRITON_PTXAS_PATH`;
   `Dockerfile.jp5` keeps `ARG VLLM_ENABLE=0` and gains neither variable (3.8)
4. **On-device reproduction — DONE (2026-08-15, bugfix.md clean-window
   re-test)**: outside the Argus-degraded window, the qwen load passed CUDA
   init and weight load, then died in `profile_run` with
   `triton.runtime.errors.PTXASError` (``ptxas fatal : Value 'sm_110a' is
   not defined for option 'gpu-name'``), model FAILED, HTTP 409

**Expected Counterexamples**:
- Case 1: no `TRITON_PTXAS_PATH` declaration anywhere in the JP7
  image/compose/recipes — triton uses its bundled CUDA 12.8 ptxas
- Case 2: `torch.cuda.is_available()` invoked from `_fail()` in a process
  whose torch CUDA was never initialized
- Case 4: `EngineCore_DP0` death at `profile_run` with PTXASError/`sm_110a`,
  model FAILED, HTTP 409 (observed and recorded)

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed
configuration produces the expected behavior.

**Pseudocode:**
```
FOR ALL launch WHERE isBugCondition_ptxas(launch) DO
  // launch = a JP7 engine-core init whose profile run JIT-compiles a
  // Triton kernel. Fixed image: TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas
  // => triton assembles PTX with the system CUDA 13.x ptxas, which accepts
  // sm_110a, so the profile run completes
  result := engineCoreInit_fixed(launch)
  ASSERT result.ptxasUsed = systemCuda13Ptxas                    // config leg
  ASSERT result.modelState = READY AND result.httpStatus = 200   // on-hardware
END FOR
```

Container-level: re-run the exploration suite — cases 1 and 2 now PASS (the
`TRITON_PTXAS_PATH` declaration exists; the failure handler makes no
CUDA-initializing call), case 3 still passes. Property 3 is additionally
encoded
property-based (Hypothesis, `test_property_*` naming, no hardcoded
`max_examples`): for generated fake-torch states with
`is_initialized() = False` (crossed with `empty_cache` raising or not, torch
importable or not), the fixed reclaim performs no CUDA-initializing call and
never raises. On-hardware: the hot-patch validation (DONE 2026-08-15) plus
deployment step 4 above are the authoritative Property 1 validation (2.1,
2.2, 2.4 — profile run completes with zero ptxas errors, model READY,
generate serves); the retry-recovery claim (2.3) is validated by observing
that the deployed component's load succeeds on its first genuine attempt
(the deterministic codegen failure is gone).

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold,
the fixed code produces the same result as the original code.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT vllmRuntime_original(input) = vllmRuntime_fixed(input)
END FOR
```

**Testing Approach**: property-based testing for the reclaim behavior
(Hypothesis generates torch states across the input domain and catches edge
cases manual tests would miss); byte-level/suite-level identity for
everything else.

**Test Plan**: observe on UNFIXED code first, then encode:
1. **Reclaim-when-initialized preservation** (property-based): for generated
   fake-torch states with `is_initialized() = True`, the fixed reclaim calls
   `empty_cache()` exactly as the original did (observed on unfixed code with
   the same fake: `is_available() = True` → `empty_cache()` called) — the
   JP6/V0 recovery substrate (3.1, 3.6)
2. **Existing suites byte-identical and green**: the full
   `test/backend-test/vllm_runtime/`, `vllm_runtime_tests/`,
   `text_generation/`, `api-endpoints/` (vLLM-related), and
   `deploy_reliability/` health/exit-structure suites pass unchanged —
   covering the state machine, 409 contract, failure containment, multimodal
   path, and `vllm_model_prep.py` semantics (3.4, 3.5, 3.6, 3.7)
3. **`vllm_model_prep.py` untouched**: assert byte-identity to git HEAD in
   the checkpoint (no code change is part of this fix)
4. **Image contract preservation** (source-level): exploration case 3 (JP6
   `VLLM_USE_V1=0`, no multiproc var, no `TRITON_PTXAS_PATH`; JP5
   `VLLM_ENABLE=0`, neither variable) keeps passing (3.1, 3.2, 3.8)
5. **Security preservation gate**: the masked Dockerfile.jp7 golden is
   rebaselined for the intended ENV addition; the full preservation suite
   runs green in the flask-app container before the build (builds.md)

### Unit Tests

- `_reclaim_gpu_memory` with fake torch: `is_initialized() = False` → no
  CUDA call; `is_initialized() = True` → `empty_cache()` called; torch
  missing → silent return; `empty_cache()` raising → swallowed and logged
  (the existing `test_manager_memory_reclaim.py` call-site tests continue to
  cover WHEN reclaim runs)
- Dockerfile assertions: JP7 `TRITON_PTXAS_PATH` ENV present exactly once;
  JP6/JP5 contracts unchanged (no new env var — 3.8)

### Property-Based Tests

- Property 3 fix-check: generated fake-torch states with uninitialized torch
  CUDA → the fixed reclaim never makes a CUDA-initializing call and never
  raises
- Property 2 preservation: generated fake-torch states with initialized torch
  CUDA → fixed reclaim behavior identical to observed unfixed behavior
  (`empty_cache` called, errors swallowed)

### Integration Tests

- Existing manager/server integration suites (load → READY with fake engine,
  load failure → 409 with reason, unload idempotence, generate/stream) rerun
  unchanged in the flask-app container
- On-hardware (USER ACTION): the full deployment scenario of 2.4 on
  jetson-thor1 (qwen READY beside three healthy vision models, sustained
  backend health), plus the JP6 spot check of step 5 in the deployment plan

### Test Commands

Device-side `src/` code tests run in the flask-app container per
`.kiro/steering/builds.md` (NOT the portal venv; the interpreter shim picks
python3.11/JP5 or python3.10/JP6 image builds):

```
docker run --rm -v "$(pwd)":/repo -w /repo \
  -e PYTHONPATH=/repo/src/backend:/repo/test/backend-test \
  flask-app:latest bash -lc \
  'PY=$(command -v python3.11 || command -v python3.10); \
   $PY -m pip install --no-cache-dir --quiet pytest sarge testfixtures hypothesis; \
   $PY -m pytest test/backend-test/vllm_jp7_engine_cuda_init \
       test/backend-test/vllm_runtime \
       test/backend-test/vllm_runtime_tests -q -p no:cacheprovider'
```

Security preservation gate (pre-build, after rebaselining):

```
python3 -m pytest \
  test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py \
  test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py \
  -p no:cacheprovider --noconftest -q
```

plus the full preservation suite in the flask-app container (builds.md).
