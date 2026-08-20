# Bugfix Requirements Document

## Introduction

`qwen2-5-vl-7b-instruct-awq` (component `model-vllm-qwen2-5-vl-7b-instruct-awq`
2.0.0) cannot load on `aws.edgeml.dda.LocalServer.arm64JP6` **1.0.61**, while the
IDENTICAL model with the IDENTICAL staged engine args loads and serves on
**1.0.59**. Every JP6 device that takes the new LocalServer therefore loses its
vLLM model: the model component goes BROKEN, the deployment fails, and Greengrass
rolls back (revision 73 on `ryanorinagxdevkithomelabjp622` →
`FAILED_ROLLBACK_COMPLETE`). This spec owns that defect and the packaging-time
sizing model that let the configuration ship in the first place.

The failure is a **KV-cache budget** failure, not a weights failure. The weights
load fine (`Model loading took 6.47 GiB`); vLLM then computes a negative — or
sub-block — remainder for KV cache inside the configured
`gpu_memory_utilization = 0.4` and answers HTTP 409
`{"state":"FAILED","reason":"No available memory for the cache blocks. Try
increasing gpu_memory_utilization when initializing the engine."}`.

Two findings reframe the defect and are the reason this spec is broader than "bump
one number":

1. **The publish-time gate is unsound, not merely mis-tuned.**
   `edge-cv-portal/backend/functions/vllm_fit_check.py` decides
   `fits = gpu_memory_utilization * DEVICE_MEMORY_PROFILE_BYTES[arch] >=
   weight_estimate + MINIMUM_KV_CACHE_BYTES`. For this model that is
   `0.4 × 30 GiB = 12.00 GiB` budget vs `6.5 + 1 = 7.5 GiB` required — it PASSES
   with a claimed 4.5 GiB of slack. The device's own profiling for the same load
   reports the remainder as **−7.83 GiB** (failing attempt) and **+0.65 GiB**
   (succeeding attempt). The formula never subtracts the two terms that actually
   consume the budget: vLLM's **activation/profiling peak** (measured **4.92 GiB**
   for this model) and the **non-torch / co-tenant residency** vLLM attributes to
   the budget (measured **−0.05 GiB to +8.29 GiB** on the same device minutes
   apart). Note the honest nuance: the `MINIMUM_KV_CACHE_BYTES = 1 GiB` floor is
   NOT the main error — 0.65 GiB of KV served this model at 2.95x concurrency for
   4096 tokens. The error is that a ~4.9 GiB activation term and a 0–8.3 GiB
   co-tenant term are absent from the model entirely, and that
   `gpu_memory_utilization` is a fraction of TOTAL device memory on a device where
   other consumers already hold ~6 GB.
2. **JP6 is marginal on BOTH versions; 1.0.59 only survives by retrying.** On the
   live 1.0.59 device (read-only evidence collected 2026-08-17 22:xxZ) the FIRST
   load attempt of the current backend life failed with the exact same
   `No available memory for the cache blocks` error at 22:12:16Z, and only the
   prep's validated KV-OOM unload→reload recovery reached READY at 22:16:15Z with
   0.65 GiB of KV cache. "1.0.59 works" means "1.0.59 works on the second try,
   with 0.65 GiB of headroom, when the co-tenant residency happens to be low at
   profiling time." The load outcome is a function of transient device state.

**Success condition (both halves are required).** The published vLLM model loads
and serves on JP6 devices AND the co-resident ONNX GPU models
(`model-cookies-binary-jetson-xavier-jp6` 10.0.0,
`model-rf-detr-seg-nano-jetson-xavier-jp6` 8.0.0,
`model-yolo-test-jetson-xavier-jp6` 8.0.0 — all RUNNING on the same unified
memory) keep working. "Raise `gpu_memory_utilization`" is therefore a **hazard,
not a fix**: on a shared unified-memory device a larger fraction of TOTAL memory
silently overlaps memory the ONNX models already hold. Both the portal fit check
and the device-side prep currently tell operators to raise it
(`vllm_fit_check.evaluate_fit` message: "Remediation: raise
gpu_memory_utilization …"; `vllm_model_prep.py`: "RAISE 'gpu_memory_utilization'
in the model's engine configuration").

### Ownership boundaries (stated explicitly so sibling specs stay clean)

- **`vllm-model-reload-after-backend-restart`** owns the reconciler-vs-deployment
  race (its task 12 OUTCOME blocks are the primary evidence source for this spec).
  That race is CONFIRMED and separately mitigated; it is **NOT this spec's
  defect**. The decisive separation: after the pre-clean + clean backend restart
  the reconciler logged `vLLM reconciler: no staged models awaiting reload;
  nothing to do` and the single clean load STILL failed on KV cache. This spec
  must not re-litigate or modify the reconciler.
- **`vllm-sizing-and-packaging-errors`** owns `vllm_fit_check.py`, the
  `Device_Memory_Profile`/`Minimum_KV_Cache` model (its Requirement 3.8 mandates
  the `arm64_jp6` → 30 GiB entry), and the device-side remediation text (its
  Requirements 4.2, 3.9 mandate the "raise, never lower" direction). This spec
  **revises that model and that remediation direction** for the failure mode where
  weights fit but activation peak plus co-tenancy do not. Its original incident
  (weights alone exceeding the budget) keeps its correct remediation.
- **`vllm-jp7-engine-cuda-init`** owns the JP7 CUDA-init history
  (`cudaErrorDevicesUnavailable`, nvargus/Argus driver defect, NVIDIA bug-report
  draft). The JP6 `NVML_SUCCESS == r INTERNAL ASSERT FAILED at
  "/opt/pytorch/c10/cuda/CUDACachingAllocator.cpp":1131` seen here is a
  **different symptom** from that spec's class and is carried as its own
  unresolved bug condition below (1.6).
- **`model-gpu-fallback-visibility`** owns GPU status surfaces / ONNX CPU-fallback
  visibility. If starving the ONNX models is ever observed, that spec's signal is
  the reporting channel; this spec's job is to not cause it.
- **JP7 is not in scope as a defect.** On thor1 (`LocalServer.arm64JP7` 1.0.8,
  Thor, ~128 GB) the same vLLM generation loads `qwen3-vl-8b-instruct` with
  `Available KV cache memory: 36.34 GiB` / `GPU KV cache size: 264,592 tokens`.
  The new vLLM is fine where the budget is ample; JP6's budget is not. JP7 must be
  preserved unchanged.

### What the repo can and cannot settle about 1.0.59 → 1.0.61

- **The JP6 vLLM/torch pins did NOT move.** `src/backend/Dockerfile.jp6` pins
  `VLLM_SPEC="vllm==0.9.3+cu126"`, `torch==2.8.0`, `triton==3.6.0`,
  `torchvision==0.23.0`, `ENV VLLM_USE_V1=0`, and was last modified
  **2026-08-09** (`e48d5d6`, the VL enablement fixes) — before both builds.
  `src/backend/requirements.txt` is untouched in the window. Verified in the
  running 1.0.59 container: `vllm 0.9.3 / torch 2.8.0 / cuda 12.6`. There is **no
  JP6 vLLM version bump toward thor1's `v0.11.3.dev0+g275de3417.d20260816`** — the
  new vLLM generation is the JP7 image's from-source build
  (`Dockerfile.jp7`, `VLLM_VERSION=v0.11.2`). The user-supplied hypothesis of a
  version bump on JP6 is therefore not supported by the repo.
- **One in-repo change in the delta does move the memory profile of this exact
  model class.** Commit `086c251` (2026-08-16, "VLM/LLM node: Bedrock parity —
  anomaly mode + reference image", ancestor of `652c7bf` which built 1.0.61) added
  to `src/backend/vllm_runtime/manager.py`:

  ```python
  # vLLM's default caps images per prompt at 1; two-image reference
  # generation needs 2 (vlm-anomaly-reference-parity Requirement
  # 6.6). setdefault: an explicit model.json value wins unchanged,
  # and the arg is a standard EngineArgs field, harmless for
  # text-only models.
  engine_args.setdefault("limit_mm_per_prompt", {"image": 2})
  ```

  The staged `model.json` on the device does NOT set `limit_mm_per_prompt`, so the
  default applies. Confirmed read-only in the running 1.0.59 container:
  `grep -c limit_mm_per_prompt /vllm_runtime/manager.py` → **0**. So 1.0.61 asks a
  vision-language engine to profile for **two** images per prompt where 1.0.59
  profiled for one, inside an unchanged budget, with the activation/profiling peak
  already at 4.92 GiB of an 11.98 GiB budget. This is the leading candidate cause
  of the version-to-version regression; it is a hypothesis about magnitude, not a
  measured 1.0.61 number (see the honesty guard).
- **What the repo cannot settle**: the exact resolved dependency set inside the
  1.0.61 image (`transformers>=4.51.1,<5` and `numpy>=1.26,<2` float, all PyPI
  transitive deps float, and the Jetson index can republish a wheel under the same
  version string), and the actual profiling numbers 1.0.61 produced. The 1.0.61
  image was pruned from the device but can be re-pulled for inspection from
  `164152369890.dkr.ecr.us-east-1.amazonaws.com/dda/flask-app:1.0.61`; a
  `pip freeze` diff against the on-device 1.0.59 image plus a
  `limit_mm_per_prompt` grep settles both questions definitively.

### Honesty guard

Host tests cannot load a real vLLM engine, allocate GPU memory, or reproduce
Jetson unified-memory accounting. What IS host-testable (pure math + moto): the
`vllm_fit_check.py` sizing/gate logic, the publish gate's fail/pass/override
branches, the engine-arg authoring and staging path, and any new device-side
preflight computation exercised over injected memory readings. Every clause below
that can only be proven on hardware is labelled **[HARDWARE]** and belongs to a
later USER ACTION task. All device evidence in this document was collected
**read-only** (`docker logs`, `docker exec` greps, `free`, `ps`, GET endpoints,
`greengrass-cli component list`); the device was left HEALTHY on 1.0.59 with the
model READY (`/v2/repository/index` → `[{"name":"qwen2-5-vl-7b-instruct-awq",
"state":"READY"}]`), and nothing on it was changed. Per
`.kiro/steering/builds.md` any device-side fix requires a LocalServer build and
on-hardware verification before it is done.

### Incident Record (verbatim evidence)

**Device**: `ryanorinagxdevkithomelabjp622` — Jetson Orin AGX devkit, JetPack 6,
`# R36 (release), REVISION: 5.0, GCID: 43688277, BOARD: generic, EABI: aarch64,
DATE: Fri Jan 16 03:50:45 UTC 2026`. `free -g` total **29** GB (swap 14 GB).
Co-resident GPU workloads, all RUNNING: `model-vllm-qwen2-5-vl-7b-instruct-awq`
2.0.0, `model-cookies-binary-jetson-xavier-jp6` 10.0.0,
`model-rf-detr-seg-nano-jetson-xavier-jp6` 8.0.0,
`model-yolo-test-jetson-xavier-jp6` 8.0.0.

**Staged engine args, verbatim** (`/aws_dda/dda_triton/vllm_model_repo/qwen2-5-vl-7b-instruct-awq/1/model.json`):

```json
{
  "dtype": "auto",
  "max_model_len": 4096,
  "gpu_memory_utilization": 0.4,
  "enforce_eager": true,
  "tensor_parallel_size": 1,
  "model": "Qwen/Qwen2.5-VL-7B-Instruct-AWQ"
}
```

**Clean-system proof on 1.0.61** (2026-08-17, from the sibling spec's task 12
OUTCOME — rules out the reconciler race):

- Backend container restarted 21:52:12Z → `free -g`: **6 GB used / 23 GB free**;
  reconciler logged `vLLM reconciler: no staged models awaiting reload; nothing to
  do` (no competing engine).
- Single clean load (component restart 21:53:09Z): weights fine — `Model loading
  took 6.47 GiB and ~9.5 s` — then FAILED 21:59:50Z, HTTP 409
  `{"state":"FAILED","reason":"No available memory for the cache blocks. Try
  increasing gpu_memory_utilization when initializing the engine."}`.
- Earlier attempts on the same image threw
  `NVML_SUCCESS == r INTERNAL ASSERT FAILED at
  "/opt/pytorch/c10/cuda/CUDACachingAllocator.cpp":1131` instead (13:36:30Z,
  13:39:38Z, 21:44Z).
- Stranded-allocation cascade: after three failed loads the device sat at
  **26 GB used / 3 GB free with NO model loaded**; stopping the component freed
  ~10 GB and only a backend container restart reclaimed the rest (`6 GB used /
  23 GB free`).
- Runtime server unresponsive for the whole engine construction:
  `/v2/repository/index` returned EMPTY for ~12 min during the 21:53Z load
  (independently corroborated by a 300 s `read timeout=300` on the Shutdown unload
  POST at 13:31:18Z).
- Deployment outcome: revision 73 (`382558d2`, 1.0.59 → 1.0.61) →
  `FAILED_ROLLBACK_COMPLETE`; device restored by revision
  `8b697b31-f5cf-4d09-8a58-70b3cc0afb96` pinning 1.0.59, COMPLETED 22:16:53Z;
  `generate` 200 in 1.86 s then 0.87 s.

**The budget arithmetic, verbatim from the live 1.0.59 device** (backend container
life started 22:06:58Z; these lines are vLLM's own profiling output):

```
22:07:58  INFO [model_runner.py:1115] Model loading took 6.5935 GiB and 7.243515 seconds
22:12:16  INFO [worker.py:295] Memory profiling takes 257.11 seconds
22:12:16  INFO [worker.py:295] model weights take 6.59GiB; non_torch_memory takes 8.29GiB;
                               PyTorch activation peak memory takes 4.93GiB;
                               the rest of the memory reserved for KV Cache is -7.83GiB.
22:12:16  INFO [executor_base.py:118] Maximum concurrency for 4096 tokens per request: 0.00x
22:12:16.774710Z [error] vLLM model 'qwen2-5-vl-7b-instruct-awq' failed: No available memory
                         for the cache blocks. Try increasing `gpu_memory_utilization` when
                         initializing the engine. [vllm_runtime.manager]
22:12:17-18 [info] Reclaimed cached CUDA memory after unload/failure of vLLM model … (x2)
22:12:18.302061Z [info] vLLM model 'qwen2-5-vl-7b-instruct-awq' unloaded
22:12:18.325723Z [info] Loading vLLM model 'qwen2-5-vl-7b-instruct-awq'
22:12:30  INFO [model_runner.py:1115] Model loading took 6.4689 GiB and 8.315042 seconds
22:16:12  INFO [worker.py:295] Memory profiling takes 221.80 seconds
22:16:12  INFO [worker.py:295] model weights take 6.47GiB; non_torch_memory takes -0.05GiB;
                               PyTorch activation peak memory takes 4.92GiB;
                               the rest of the memory reserved for KV Cache is 0.65GiB.
22:16:13  INFO [executor_base.py:118] Maximum concurrency for 4096 tokens per request: 2.95x
22:16:15  INFO [llm_engine.py:424] init engine (profile, create kv cache, warmup model) took 225.02 seconds
22:16:15.491826Z [info] vLLM model 'qwen2-5-vl-7b-instruct-awq' is READY
```

Derived facts: the four terms sum to the budget, so vLLM's total is ≈ 29.95 GiB
and the `0.4` budget is ≈ **11.98 GiB** — the `arm64_jp6` profile's 30 GiB is a
fair figure for TOTAL memory but is documented and used as "usable". Of that
budget, weights take 6.47 GiB and the activation/profiling peak takes 4.92 GiB,
leaving 0.65 GiB — while the fit check's model predicted 12.00 − 7.50 = 4.50 GiB
of slack. The non-torch term swung 8.34 GiB between two attempts four minutes
apart on the same device, and `Reclaimed cached CUDA memory` (the manager's
`_reclaim_gpu_memory`) is what cleared it — that reclaim path does NOT work across
the NVML-assert path (see 1.5).

**Co-tenancy, measured** (`ps -eo rss`, same device, model loaded): `python3
app.py` 18,564,980 KB; Triton python-backend stubs
`base_model-cookie…` 3,909,200 KB, `base_model-rf-det…` 1,030,612 KB,
`base_model-yolo-te…` 921,184 KB — ≈ **5.7 GiB in the three ONNX stubs alone**,
which is the ~6 GB resident before vLLM starts, and which vLLM's budget fraction
does not know about.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a vLLM model is published for `arm64_jp6` whose estimated weights plus
the 1 GiB `MINIMUM_KV_CACHE_BYTES` fit inside `gpu_memory_utilization ×
DEVICE_MEMORY_PROFILE_BYTES['arm64_jp6']` (30 GiB) THEN the Fit_Check PASSES and
the publish proceeds even when the model provably cannot load on the target
device, because the computation omits vLLM's activation/profiling peak (measured
4.92 GiB for this model, ~41% of the budget) and omits every other resident
consumer of the same unified memory (measured ~5.7 GiB of ONNX Triton stubs plus
backend/frontend containers) — observed: `0.4 × 30 GiB = 12.00 GiB ≥ 6.5 + 1 =
7.5 GiB` PASSES while the device computes the KV remainder as −7.83 GiB

1.2 WHEN the Fit_Check budget is computed as `gpu_memory_utilization ×
profile[arch]` THEN it is treated as memory available to this model, although
`gpu_memory_utilization` is a fraction of TOTAL device memory and, on a Jetson
unified-memory device with co-resident ONNX GPU models, that budget silently
overlaps memory other consumers already hold — nothing in the pipeline accounts
for co-tenancy, so the same published configuration is feasible or infeasible
depending on what else is resident at engine-profiling time

1.3 WHEN a vLLM load fails for insufficient KV-cache memory THEN both the
portal Fit_Check message ("Remediation: raise gpu_memory_utilization …") and the
device-side prep ERROR line ("RAISE 'gpu_memory_utilization' in the model's engine
configuration") instruct the operator to raise the fraction, which on a shared
unified-memory device grows this model's claim on memory the co-resident ONNX GPU
models are using — the guidance can convert a single broken model into a broken
vision stack, and no surface warns of that trade-off

1.4 WHEN LocalServer.arm64JP6 1.0.61 loads a vision-language model whose staged
`model.json` does not set `limit_mm_per_prompt` THEN the runtime forces
`limit_mm_per_prompt = {"image": 2}` (`vllm_runtime/manager.py`, commit
`086c251`), doubling the images the engine must profile for versus 1.0.59 (whose
container provably lacks the setting) inside an unchanged
`gpu_memory_utilization = 0.4` budget whose activation peak is already 4.92 GiB —
and neither the publish-time Fit_Check nor any device-side check accounts for the
multimodal profiling cost this implies **[HARDWARE: the 1.0.61 activation-peak
number itself is only measurable on device or from the re-pulled image]**

1.5 WHEN a vLLM load fails on the
`NVML_SUCCESS == r INTERNAL ASSERT FAILED at
"/opt/pytorch/c10/cuda/CUDACachingAllocator.cpp":1131` path THEN the allocations
of the failed attempt are NOT reclaimed — the prep's validated KV-OOM
unload→reload recovery and the manager's `_reclaim_gpu_memory` do not clear it, so
each subsequent attempt starts with less memory than the last (observed: three
failed loads → **26 GB used / 3 GB free with NO model loaded**; stopping the
component freed ~10 GB; only a backend container restart returned the device to
`6 GB used / 23 GB free`) — one marginal failure becomes a guaranteed cascade in
which every retry, and every other GPU consumer on the device, is starved

1.6 WHEN a load attempt follows a previous failed attempt in the same backend
life THEN the failure surfaces as the NVML allocator INTERNAL ASSERT rather than
the KV-cache message (observed 13:36:30Z, 13:39:38Z, 21:44Z), while the single
clean-system attempt surfaced the KV-cache message (21:59:50Z) — the two are
currently indistinguishable to any consumer, and whether the assert is a symptom
of the same exhaustion (torch's allocator querying device memory in a starved
state) or a distinct CUDA/NVML fault is UNRESOLVED; it is a different symptom from
the `cudaErrorDevicesUnavailable` class owned by `vllm-jp7-engine-cuda-init`
**[HARDWARE]**

1.7 WHEN the same model and engine args are loaded twice minutes apart on the
same device THEN the outcome differs (first attempt FAILED with a −7.83 GiB
remainder at 22:12:16Z, retry READY with a +0.65 GiB remainder at 22:16:15Z),
because the non-torch/co-tenant term vLLM charges against the budget swung
8.34 GiB between the attempts — the "working" 1.0.59 configuration is not
reliable, it is one retry and 0.65 GiB from failing, and nothing in the pipeline
or on the device reports that a load succeeded with margin this thin

1.8 WHEN the publish-time Fit_Check fails THEN the publish is blocked only if it
fails for EVERY supported Target_Architecture
(`greengrass_publish.py`: `every_arch_fails = all(not finding.fits …)`), so a
configuration that is infeasible on `arm64_jp6` but feasible on `arm64_jp7`
publishes with at most a warning — the per-architecture verdict never gates the
architecture it applies to

1.9 WHEN a JP6 device takes the new LocalServer while its published vLLM model
cannot load THEN the model component exhausts its Startup retries and goes BROKEN,
the whole deployment fails and rolls back (`FAILED_ROLLBACK_COMPLETE`, revision
73), and the device is left on the previous LocalServer — so a single mis-sized
vLLM model blocks every other change in that deployment for that device, and the
latest cloud deployment for the target remains the FAILED revision that a future
portal revision would preload from

1.10 WHEN engine args are decided at authoring/publish time THEN they are staged
and consumed verbatim with no adaptation to the device's ACTUAL free memory:
`model_import.ENGINE_DEFAULTS` resolves the record, packaging copies it into
`model.json`, `vllm_model_prep.py` stages it, `vllm_runtime/repository.py` parses
it, and `manager._default_engine_factory` passes it straight to
`AsyncEngineArgs(**engine_args)` — no code path anywhere reads free/total device
memory before requesting a load, so a device whose co-tenancy differs from the
authoring-time assumption has no way to fail early, cheaply, or informatively
(the failure costs ~4 min of profiling per attempt and blocks the runtime server's
event loop for the whole construction)

### Expected Behavior (Correct)

2.1 WHEN the Fit_Check evaluates a vLLM model for a Target_Architecture THEN it
SHALL model every term that consumes the `gpu_memory_utilization` budget on that
architecture — at minimum the weight estimate, an activation/profiling-peak
allowance, and the KV-cache floor — and SHALL fail the check when the sum exceeds
the budget, so that a configuration whose device-side KV remainder would be
negative (observed −7.83 GiB) cannot be reported as fitting with 4.5 GiB of slack

2.2 WHEN the Fit_Check computes an architecture's budget THEN it SHALL account
for memory held by other consumers of the same (unified) device memory — the
LocalServer backend/frontend containers and co-resident ONNX GPU models — so that
the verdict reflects memory actually available to the vLLM engine rather than a
fraction of TOTAL memory, and the profile entry SHALL be documented for what it is
(total vs usable vs available-to-vLLM), with the `arm64_jp6` figure reconciled
against the measured device reality (29 GB `free -g` total, ≈29.95 GiB as vLLM
sees it, ~6 GB resident before vLLM starts)

2.3 WHEN a Fit_Check or a device-side load failure reports insufficient KV-cache
memory THEN the remediation SHALL NOT advise raising `gpu_memory_utilization`
without stating the co-tenancy hazard, and SHALL offer the remediations that do
not starve co-resident models (reduce `max_model_len`, bound the multimodal limits,
choose a smaller/more quantized model, or free device memory) — raising the
fraction SHALL be presented only where headroom demonstrably exists after
accounting for other consumers

2.4 WHEN a vision-language model is loaded and its staged `model.json` does not
specify `limit_mm_per_prompt` THEN the effective multimodal limit SHALL be part
of the sized, authored configuration rather than an unbudgeted device-side
default — the two-image reference capability SHALL NOT silently enlarge the
profiling peak of an already-published model, and whichever value applies SHALL be
visible in the staged args and to the Fit_Check **[HARDWARE for the resulting
memory numbers]**

2.5 WHEN a vLLM load attempt fails for any reason THEN the system SHALL release
the failed attempt's device memory before the next attempt, or SHALL detect that
it cannot and refuse to retry into a starved device with a diagnostic naming the
condition — a failed load SHALL NOT leave the device at `26 GB used / 3 GB free
with no model loaded`, and SHALL NOT require a human-initiated backend container
restart to recover **[HARDWARE for the reclaim proof]**

2.6 WHEN the NVML allocator INTERNAL ASSERT path is hit THEN the system SHALL
report it distinguishably from the KV-cache exhaustion path (so operators and
sibling specs can tell an accounting fault from a budget fault), and this spec
SHALL record a determination — same-exhaustion symptom or distinct CUDA/NVML fault
— based on device evidence, cross-checked against
`vllm-jp7-engine-cuda-init`'s `cudaErrorDevicesUnavailable` class **[HARDWARE]**

2.7 WHEN a vLLM model reaches READY with a KV-cache remainder below the
configured floor (observed 0.65 GiB against a 1 GiB floor) THEN the system SHALL
surface that thin margin as a warning rather than presenting it as an
unqualified success, so a device that is one retry from failing is visible as such

2.8 WHEN the publish-time Fit_Check fails for a specific Target_Architecture
THEN the publish SHALL NOT ship a configuration that is infeasible for that
architecture as merely "warnings" — the per-architecture verdict SHALL gate the
architecture it applies to (an explicit override staying available and audited)

2.9 WHEN a device receives a vLLM model whose engine args cannot fit its ACTUAL
free memory THEN the system SHALL fail fast and informatively — before or instead
of a ~4 min engine construction that blocks the runtime server's event loop —
naming the measured available memory, the computed requirement, and the specific
engine setting to change; and it SHALL NOT take the entire Greengrass deployment
BROKEN → rolled back when the only unmet condition is this model's memory budget
**[HARDWARE for the end-to-end deployment outcome]**

2.10 WHEN the fix is in place THEN the published vLLM model SHALL load and serve
on JP6 devices AND the three co-resident ONNX GPU models SHALL keep serving on
GPU — both halves are required for success, and a fix that buys KV headroom by
starving the ONNX models SHALL be treated as a failure **[HARDWARE]**

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a vLLM model is published whose sizing genuinely fits its target
architecture THEN publish SHALL CONTINUE TO succeed with the existing response
shape, `fit_check` annotation, and audit events (`passed` / `warnings` /
`overridden` / `unverified` statuses preserved)

3.2 WHEN a Weight_Estimate cannot be determined (Hugging Face metadata or S3 head
failure) THEN the Fit_Check SHALL CONTINUE TO be skipped and reported as
`unverified` without blocking registration, update, or publish
(`vllm-sizing-and-packaging-errors` Requirement 3.4), and
`vllm_fit_check.estimate_weights` SHALL CONTINUE TO never raise out of its public
API and to stay stdlib-only with no AWS dependencies

3.3 WHEN a vLLM model record is registered or its engine configuration updated
THEN the Engine_Configuration contract SHALL CONTINUE TO hold end-to-end: the
`ENGINE_DEFAULTS` key set and values (`dtype=auto`,
`gpu_memory_utilization=0.5`, `max_model_len`, `tensor_parallel_size`,
`enforce_eager`), the fail-closed rejection of unknown keys and out-of-range
values with per-field findings, and verbatim propagation of the stored values into
the packaged `model.json`

3.4 WHEN `LocalServer.arm64JP7` loads a vLLM model on Thor THEN behavior SHALL
CONTINUE TO be unchanged — the same generation loads `qwen3-vl-8b-instruct` with
`Available KV cache memory: 36.34 GiB` / `GPU KV cache size: 264,592 tokens` under
`gpu_memory_utilization=0.5` while three vision models coexist on GPU; no JP7
sizing, engine-arg, or image behavior may regress

3.5 WHEN the image carries no vLLM wheel (JP5 default `VLLM_ENABLE=0`, x86) THEN
startup SHALL CONTINUE TO follow the byte-identical pre-feature sequence with no
new env knobs, no new imports, and no new failure modes

3.6 WHEN the three co-resident ONNX GPU models load and serve on JP6
(`model-cookies-binary-jetson-xavier-jp6`,
`model-rf-detr-seg-nano-jetson-xavier-jp6`,
`model-yolo-test-jetson-xavier-jp6`) THEN they SHALL CONTINUE TO load to READY on
GPU with unchanged inference behavior and unchanged memory footprint — no fix may
buy vLLM headroom at their expense

3.7 WHEN a backend restart leaves a staged vLLM model THEN the reconciler SHALL
CONTINUE TO behave exactly as the sibling spec validated it: one-shot scan,
sequential re-drive through the loopback load endpoint, bounded backoff
`(30, 120, 480)`, tombstone semantics, truthful status surfaces, and the no-op log
line `vLLM reconciler: no staged models awaiting reload; nothing to do` when
nothing is staged

3.8 WHEN the device-side prep stages and requests a load THEN its validated
lifecycle semantics SHALL CONTINUE TO hold: atomic staging, exit-code
classification (`LOAD_UNREACHABLE` → exit 1 with the authoritative log;
`LOAD_HTTP_ERROR` → exit **0** with that same authoritative log, unchanged in
content), the single KV-OOM unload→reload recovery per attempt, the
prominent ERROR line carrying model name, HTTP status, extracted reason, and the
staged `gpu_memory_utilization` / `max_model_len`, and idempotent
Shutdown/`--cleanup`

**AMENDED 2026-08-19 (operator-approved), same discipline as the S1-S4
amendments in `vllm-sizing-and-packaging-errors/requirements.md`.** Superseded
text, recorded VERBATIM:

> exit-code classification (`LOAD_UNREACHABLE` / `LOAD_HTTP_ERROR` → exit 1 with
> the authoritative log)

**New contract.** `LOAD_UNREACHABLE` keeps **exit 1**, because the runtime was
never reachable — the component genuinely started before the backend was ready,
so a component retry IS the recovery. Every **authoritative runtime answer**
(`LOAD_HTTP_ERROR`, and `LOAD_PREFLIGHT_REFUSED` before it) exits **0**, because
a MODEL failure is not a COMPONENT failure: the model is reported `FAILED` with
its reason through the unchanged model-status surfaces and the in-backend
reconciler owns the retries, while co-deployed components and the workflows that
depend on them stay available. `LOAD_OK` → 0 is unchanged. Nothing is quieter:
the prominent ERROR line keeps every element this clause pins.

**Evidence.** Three consecutive transient-DNS load failures on
`ryanorinagxdevkithomelabjp622` (`Failed to resolve 'huggingface.co'
([Errno -3] Temporary failure in name resolution)`) at **12:00:47Z /
12:02:09Z / 12:03:22Z**, each `Startup script exited. {exitCode=1}`; the third
drove `currentState=BROKEN`, which stranded `dda.workflow.0c7fe31a-…` 7.0.0 and
`dda.workflow.1f0b4c0c-…` 9.0.0 at `INSTALLED` and took the core device
**UNHEALTHY**. A transient name-resolution failure on an already-staged model
therefore cost the whole device, which is defect 1.9's mechanism reappearing
through a different door.

**Note on where this amendment landed.** The dispatch pointed at
`vllm-sizing-and-packaging-errors/requirements.md` (Requirement 4 criterion 2
area). That file carries **no** exit-code clause — `grep -c exit` over it
returns **0**, and its Requirement 4 criteria 1-4 cover the ERROR line, the
remediation order, unparseable bodies and the staged-args echo only. The clause
that reads "`LOAD_UNREACHABLE` / `LOAD_HTTP_ERROR` → exit 1" is **this**
document's 3.8, so the amendment was made here rather than inventing a
criterion in a file that never held one. No other clause in either file was
touched.

3.9 WHEN a text-only vLLM model is loaded THEN its memory profile and behavior
SHALL CONTINUE TO be unaffected by any multimodal-limit change, and the two-image
reference-generation capability (`vlm-anomaly-reference-parity`) SHALL CONTINUE TO
work for models sized for it — the fix may not silently remove the feature

3.10 WHEN vision (Triton/ONNX) model packaging, publishing, or workflow packaging
runs THEN it SHALL CONTINUE TO behave exactly as today, including the
per-architecture suffixed vLLM dependency resolution, Defect F omission-with-
warning, Defect G fail-closed coverage, plugin pinning, and the LocalServer
single-variant discipline

3.11 WHEN the device is on the currently healthy LocalServer 1.0.59 THEN it SHALL
CONTINUE TO serve `qwen2-5-vl-7b-instruct-awq` (READY, `generate` 200 in ~1.9 s
cold then ~0.9 s) until a fixed component is deliberately deployed — the
investigation itself must not disturb the device

### Explicitly NOT changed

- `src/backend/vllm_runtime/reconciler.py` and the reconciler wiring in
  `src/backend/app.py` — owned by `vllm-model-reload-after-backend-restart`; the
  race is confirmed and separately mitigated and is NOT this defect.
- `src/backend/vllm_runtime/repository.py` and the runtime server's routes and
  status mappings — this spec is a consumer of the existing load/unload contract.
- The tombstone contract (`.dda_explicit_unload`), `ModelState.UNLOADED`, and the
  feature-config / 409-category status maps.
- `src/backend/Dockerfile.jp7`, the JP7 from-source vLLM build
  (`VLLM_VERSION=v0.11.2`), `TRITON_PTXAS_PATH`, and every JP7 engine default.
- The JP5 (`VLLM_ENABLE=0`) and x86 images' vLLM-free inertness.
- The ONNX/Triton vision path: `model_convertor.py`, `inference_runtimes.py`, the
  vision model recipes, and the three JP6 ONNX model components.
- `vllm-jp7-engine-cuda-init`'s territory: the `cudaErrorDevicesUnavailable`
  nvargus/Argus driver finding, its NVIDIA bug-report draft, and the
  `VLLM_WORKER_MULTIPROC_METHOD` disposition.
- `model-gpu-fallback-visibility`'s GPU status surfaces (this spec must not
  duplicate them; it must avoid triggering the condition they report).
- The awscrt "Continuation ref count has gone negative" abort class and the thor1
  cold-first-generate (~60 s) finding — pre-existing, out of scope.
- Greengrass deployment/revision machinery, ShadowManager sync, and the portal
  build fleet.
- Any preservation-tracked file
  (`src/docker-compose.yaml`, `src/backend/Dockerfile*`, `src/frontend/Dockerfile`,
  `src/edgemlsdk/Dockerfile`, `src/backend/requirements.txt`, the recipe variants,
  `station_install/setup_station.sh`) unless a fix demonstrably requires it — in
  which case the security baseline is rebaselined in the same change per
  `.kiro/steering/builds.md`.
- No component build is started by this spec's requirements phase.

### Deriving the Bug Condition

The input is a publish-and-deploy attempt for a vLLM model on a JetPack device:

```pascal
RECORD LoadAttempt
  arch                    : TargetArchitecture      // e.g. arm64_jp6
  weights_bytes           : Integer                 // on-GPU weight footprint
  util                    : Real                    // gpu_memory_utilization
  max_model_len           : Integer
  mm_images_per_prompt    : Integer                 // effective limit_mm_per_prompt
  device_total_bytes      : Integer                 // as the engine sees it
  co_resident_bytes       : Integer                 // ONNX stubs + containers + OS
  activation_peak_bytes   : Integer                 // vLLM profiling peak
  prior_failed_attempt    : Boolean                 // same backend life
END RECORD

FUNCTION isBugCondition(X)
  INPUT: X of type LoadAttempt
  OUTPUT: boolean

  budget    := X.util * X.device_total_bytes
  // What the shipped Fit_Check believes is required:
  claimed   := X.weights_bytes + MINIMUM_KV_CACHE_BYTES        // 1 GiB
  // What the device actually charges against the same budget:
  actual    := X.weights_bytes + X.activation_peak_bytes
               + charged_non_torch(X.co_resident_bytes, X.prior_failed_attempt)
               + MINIMUM_KV_CACHE_BYTES

  // C1: the gate passes a configuration the device cannot load
  RETURN (budget >= claimed) AND (budget < actual)
END FUNCTION
```

Concrete counterexample from the incident (`qwen2-5-vl-7b-instruct-awq`,
`arm64_jp6`, `util = 0.4`):

```
budget  = 0.4 x 30 GiB (profile)      = 12.00 GiB   -> gate sees 4.50 GiB slack
claimed = 6.5 + 1.0                   =  7.50 GiB
actual  = 6.47 + 4.92 + [ -0.05 .. 8.29 ] + 1.0 = 12.34 .. 20.68 GiB
device-measured KV remainder          = -7.83 GiB (fail) / +0.65 GiB (retry)
```

Companion bug conditions, each with its own property:

```pascal
// C2: raising the fraction is charged against shared memory
isRemediationHazard(X) := (X.util_raised) AND
                          (X.util * X.device_total_bytes + X.co_resident_bytes
                           > X.device_total_bytes)

// C3: the multimodal default enlarges an already-published model's peak
isUnbudgetedMultimodal(X) := (model.json omits limit_mm_per_prompt)
                            AND (runtime forces mm_images_per_prompt = 2)
                            AND (model is vision-language)

// C4: a failed attempt strands allocations
isStrandedCascade(X) := X.prior_failed_attempt
                        AND NOT reclaimed(previous_attempt)

// C5: per-arch infeasibility ships because another arch fits
isPerArchEscape(X) := (NOT fits(X.arch)) AND (EXISTS a != X.arch : fits(a))
```

**Property: Fix Checking**

```pascal
FOR ALL X WHERE isBugCondition(X) DO
  verdict <- FitCheck'(X)
  ASSERT verdict.fits = FALSE
  ASSERT verdict.message NAMES the activation-peak and co-tenancy terms
  ASSERT verdict.remediation DOES NOT advise raising util without headroom proof
END FOR

FOR ALL X WHERE isPerArchEscape(X) DO
  ASSERT Publish'(X) is refused for X.arch (or explicitly overridden + audited)
END FOR

FOR ALL X WHERE isStrandedCascade(X) DO          // [HARDWARE]
  ASSERT device_free_after(X) >= device_free_before(X) - epsilon
      OR the retry is refused with a diagnostic naming the starved condition
END FOR
```

**Property: Preservation**

```pascal
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT FitCheck(X)  = FitCheck'(X)      // verdict, budget, required, message shape
  ASSERT Publish(X)   = Publish'(X)       // status, fit_check annotation, audit events
  ASSERT StagedArgs(X) = StagedArgs'(X)   // model.json byte-identical
  ASSERT JP7Load(X)   = JP7Load'(X)       // [HARDWARE]
  ASSERT OnnxLoad(X)  = OnnxLoad'(X)      // [HARDWARE]
END FOR
```

Where `F` is the current (unfixed) pipeline — `vllm_fit_check.evaluate_fit` +
`greengrass_publish` gate + `vllm_model_prep` staging + `VllmRuntimeManager.load`
— and `F'` is the fixed pipeline.
