# Model GPU-Fallback Visibility Bugfix Design

## Overview

During the Aug 14–15 device-wide CUDA outage on jetson-thor1 (JP7/Thor, driver
595.78 — incident record in the sibling spec `vllm-jp7-engine-cuda-init`), all
three deployed ONNX vision models silently served inference on CPU for a full
day while every operator-facing surface reported the same healthy READY state
as GPU operation. The mechanism is ONNX Runtime's by-design graceful fallback:
`OnnxRunner` requests `CUDAExecutionProvider → CPUExecutionProvider`, and when
the CUDA EP fails to initialize inside `ort.InferenceSession(...)`, ORT falls
back to the CPU EP and session creation succeeds. The fallback is a FEATURE;
the defect is pure lack of VISIBILITY — the active execution provider is never
captured after session creation (`session.get_providers()` is never called),
never logged, and never present in any status payload.

The fix has two legs, per the binding user decision (portal display IN scope):

- **Device leg (the core):** `OnnxRunner` captures the ACTIVE providers from
  the created session, logs them at INFO on every load and a prominent WARNING
  when a GPU provider was requested but not obtained, and writes an atomic
  **Active_Provider_Record** sidecar JSON into the model's Triton version
  directory (the stub→backend channel, Decision 1). The backend merges that
  record into each Triton model's `/feature-configurations` entry as an
  additive `defaultConfiguration.executionProviderInfo` field (the vLLM
  `failureReason` precedent), computes a device-level degraded-GPU signal
  ("≥1 GPU-chain model loaded, none has an active GPU provider") surfaced via
  a new additive `GET /feature-configurations/gpu-status` endpoint (Decision
  2), and reports the whole snapshot into a new reported-only named shadow
  `dda-model-status` (Decision 4).
- **Cloud/portal leg (additive display):** the portal's deployment creation
  adds `dda-model-status` to the ShadowManager synchronize list (the exact
  mechanism that already syncs `dda-camera-registry`), `GET /devices/{id}`
  reads the shadow on demand through the existing assumed-role iot-data
  pattern, and DeviceDetail renders a "Deployed models" panel with a
  per-model "CPU fallback" badge and a device-level degraded-GPU alert.
  Absence of the signal (older device software) renders exactly as today.

Two explicit design dispositions: **no opt-in strict mode ships in this spec**
(Decision 3 — fallback stays functional per requirement 3.1; strict mode is a
behavior change, not a visibility fix, and is deferred), and **sidecar absence
means "no information"** everywhere (Decision 6) — because the per-model copy
of `inference_runtimes.py` only refreshes when the model component's Greengrass
Startup re-runs `model_convertor.py`, models converted before this fix simply
show no provider info (today's behavior) until their next restart/reboot.

Blast radius: JP5, JP6, and JP7 all build onnxruntime with the GPU providers
and share the CUDA→CPU chain — full sequential component builds plus
on-hardware verification per `.kiro/steering/builds.md`; the plain x86 image is
genuinely-CPU by design and must be preservation-verified (never flagged
degraded). No preservation-tracked file is touched (verified — no baseline
rebaselines needed), no recipe changes (the shadow ACL is already wildcard),
and no new Python dependencies. The portal leg ships via a portal deploy that
must be sequenced strictly around the component builds (builds.md: never
portal-deploy while a component build runs).

## Glossary

- **Bug_Condition (C)**: an ONNX model whose Triton stub created its
  `ort.InferenceSession` with a GPU provider in the requested chain but whose
  session came up without any active GPU provider (silent CPU fallback), with
  no DDA-visible signal in logs, status payloads, or the portal
- **Property (P)**: the desired behavior — the active providers are captured
  and logged on every load, a fallback is prominently warned, queryable
  per-model and device-level through the status surfaces, and visible in the
  portal; while the fallback itself continues to work
- **Preservation**: fallback-to-CPU still succeeds and serves; healthy-GPU,
  CPU-by-design, TensorRT-opt-in, DLR/Torch, and vLLM behavior unchanged;
  all existing status payload fields unchanged (additive only)
- **OnnxRunner**: the ONNX engine in
  `src/backend/dda_triton/resources_for_copy/inference_runtimes.py`; builds
  the provider chain in `__select_providers` and creates the
  `ort.InferenceSession`. Runs INSIDE the Triton python-backend stub process,
  not the backend app process
- **Triton python-backend stub**: the per-model process Triton launches to run
  `model.py` (`lfv_model_template.py`); one stub per loaded model; its only
  shared surface with the backend is the filesystem and Triton's model state
- **Execution provider (EP)**: an ORT compute backend;
  `ort.get_available_providers()` lists COMPILED-IN providers (CUDA is always
  "available" on GPU images even when the driver cannot create a context);
  `session.get_providers()` lists the ACTIVE providers of a created session
- **GPU provider set**: `{CUDAExecutionProvider, TensorrtExecutionProvider}`
- **GPU-chain model**: a loaded ONNX model whose requested provider chain
  contained at least one GPU provider (`gpuRequested: true` in its record)
- **Active_Provider_Record**: the sidecar `dda_active_providers.json` the
  fixed `OnnxRunner` writes atomically into the model VERSION directory —
  per-stage requested/active providers plus the model-level `gpuRequested` /
  `gpuActive` aggregate; the stub→backend channel (Decision 1)
- **Model version directory**: `TRITON_MODEL_DIR/base_{model}/{version}/` —
  the real (non-symlinked) directory holding `model.py`, the per-model copy of
  `inference_runtimes.py`, and (fixed) the Active_Provider_Record; readable by
  the backend (same container filesystem under `/aws_dda`)
- **Per-model runner copy propagation**: `model_convertor.py` copies
  `inference_runtimes.py` into each model's version dir at conversion time;
  conversion re-runs on every MODEL component Greengrass Startup (reboot,
  Nucleus restart, model redeploy) from `/aws_dda/resources_for_copy`, which
  `triton_setup.cp_model_conversion_files()` full-tree re-syncs on every
  backend container start. A LocalServer-only deploy does NOT refresh the
  per-model copies (Decision 6)
- **`executionProviderInfo`**: the additive field merged into a Triton entry's
  `defaultConfiguration` in `/feature-configurations` (precedent: the vLLM
  `failureReason` additive field)
- **Device degraded-GPU signal**: `gpuDegraded = (≥1 GPU-chain model with a
  record is loaded) AND (none of them has gpuActive)` — requirement 2.4
- **`dda-model-status` shadow**: new reported-only named shadow carrying the
  per-model provider snapshot + device degraded flag; synced by ShadowManager
  exactly like `dda-camera-registry` (portal `deployments.py` auto-include)
- **`get_features_triton`**: `src/backend/utils/feature_configs_utils.py` —
  builds the `/feature-configurations` Triton entries from
  `TritonEdgeClient.list_triton_models()` (panorama `mlops` state only: name +
  READY/UNAVAILABLE; it carries NO provider information, which is why the
  sidecar channel exists)
- **jetson-thor1**: JP7/Thor verification device — the incident device

## Bug Details

### Bug Condition

The bug manifests whenever the CUDA (or TensorRT) EP cannot initialize during
`ort.InferenceSession` creation in a Triton stub: ORT silently strips the GPU
provider, the session comes up CPU-only, the stub's `initialize()` returns
normally, Triton marks the model READY, and every DDA surface — the stub log
(which only logs the REQUESTED chain, pre-session), `/feature-configurations`,
start/stop responses, the portal — reports exactly the healthy-GPU state. On
jetson-thor1 the only trace was one kernel `Can't map dma attachment!` + NVRM
Error(89) pair per stub start, outside DDA entirely, and
`nvidia-smi --query-compute-apps` was EMPTY with all three models READY.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type OnnxModelLoad
         { requestedProviders: list,   // chain passed to InferenceSession
           activeProviders: list,      // session.get_providers() after create
           loadSucceeded: boolean }
  OUTPUT: boolean

  GPU_SET = {CUDAExecutionProvider, TensorrtExecutionProvider}

  RETURN X.loadSucceeded
         AND (GPU_SET ∩ names(X.requestedProviders)) ≠ ∅   // GPU was requested
         AND (GPU_SET ∩ X.activeProviders) = ∅             // none was obtained
         // and on the unfixed tree: no warning logged, no record written,
         // no status field, no device-level signal — indistinguishable
         // from healthy GPU operation
END FUNCTION
```

On the unfixed tree every such load satisfies C(X): `session.get_providers()`
is never called, so `activeProviders` is not even observed; the INFO line
"loading ONNX model ... with providers [...]" logs the requested chain before
session creation and nothing after.

### Examples

- **jetson-thor1 Aug 14–15 (the motivating incident)**: device-wide CUDA
  context creation failure; yolo_test, rf-detr-seg-nano, and
  cookies-segmentation all loaded READY on CPU at 20:23:12–13 (three kernel
  Error(89) pairs matching the three stub starts); a production device ran a
  full day with GPU inference capability lost and zero DDA-visible signal.
  Expected: three per-model WARNINGs, three `gpuFallback: true` status
  entries, one device-level degraded-GPU signal, and a portal badge.
- **Single-model fallback (partial outage)**: one model's CUDA EP fails (e.g.
  OOM at init) while others hold GPU. Expected: that model warns and reports
  `gpuActive: false`; the device-level signal stays off (some GPU-chain model
  still has GPU).
- **Recovery reload (2.3)**: a model loaded during the outage reports the
  fallback until reloaded; after GPU recovery and a model restart, the new
  stub instance rewrites the record and the model reports GPU again.
- **Edge case — CPU by design (3.3)**: manifest `device: "cpu"`, or the plain
  x86 image where CUDA is not compiled in (the chain `__select_providers`
  returns is CPU-only). `gpuRequested` is false — computed from the CHOSEN
  chain, not the manifest — so no warning, no fallback flag, no degraded
  contribution. Not a bug condition.
- **Edge case — TensorRT opt-in (3.7)**: chain TRT → CUDA → CPU. If the
  session comes up on CUDA (TRT failed), a GPU provider is still active —
  degraded WARNING does NOT fire (visibility rule: warn only when NO GPU
  provider is active); the record still shows the full requested/active lists.
- **Edge case — pre-fix per-model runner copy (Decision 6)**: a model whose
  version dir still holds the old `inference_runtimes.py` writes no record.
  Expected: no `executionProviderInfo` field, excluded from the degraded
  computation, portal renders as today — never a false signal.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- CPU fallback keeps working (3.1): when the CUDA EP fails to initialize the
  model SHALL still reach READY and serve correct results. Visibility code
  (introspection, logging, sidecar write) is failure-isolated — a sidecar
  write error must never fail the load.
- Healthy-GPU loads (3.2): identical provider selection order, session
  options (thread counts), and inference numerics. The only new session
  interaction is the read-only `session.get_providers()` call.
- CPU-by-design models (3.3): manifest `device: "cpu"` and the plain x86
  CPU-only image continue to report READY with unchanged semantics and are
  never flagged degraded.
- Existing status consumers (3.4): `/feature-configurations`, start/stop
  responses, portal frontend, shadow sync — all new information is additive
  (`executionProviderInfo` inside `defaultConfiguration`, a NEW endpoint, a
  NEW named shadow, a NEW portal panel); no renames, removals, or type
  changes.
- DLR / PyTorch / vLLM paths (3.5): `DlrRunner`, `TorchRunner`,
  `get_features_vllm`, and everything in the sibling vLLM spec's territory
  are untouched; only `OnnxRunner` and the status surface change.
- JP5/JP6 healthy-GPU behavior (3.6): the change is behavior-preserving on
  every target it ships to; verified per builds.md (component build +
  on-hardware verification per arch).
- TensorRT opt-in (3.7): chain construction, engine-cache handling, and load
  behavior unchanged; the same visibility rules apply.

**Scope:**
All inputs that do NOT involve an ONNX session where a GPU provider was
requested but not obtained are completely unaffected: DLR/Torch/vLLM models,
CPU-by-design ONNX models, healthy-GPU loads (which gain only an INFO log line
and a record showing `gpuActive: true`), every existing status payload field,
and every device whose models predate the fix (no record → no new fields).

## Hypothesized Root Cause

> Not a hypothesis: the incident investigation (sibling spec
> `vllm-jp7-engine-cuda-init`) and the code reading in bugfix.md established
> the causes directly. Section header kept per the bugfix design format.

1. **The active provider is never captured** (defect 1.1): `OnnxRunner`
   logs the requested chain BEFORE `ort.InferenceSession(...)` and never calls
   `session.get_providers()` after. ORT's CUDA-EP-init failure path logs to
   the stub's own stderr (Triton-managed, effectively invisible) and returns a
   working CPU session — by design.
2. **The status surface has no channel to the stub** (defect 1.2): the
   backend's model status comes from `TritonEdgeClient.list_triton_models()`
   → panorama `mlops` → Triton model state, which is only
   READY/UNAVAILABLE/etc. The provider information exists solely inside the
   stub process; nothing exports it, so `get_features_triton` could not
   surface it even if it wanted to.
3. **No aggregation exists** (defect 1.3): with no per-model signal there is
   nothing to aggregate into a device-level "GPU inference capability lost"
   signal; N silently-degraded models look like N healthy models.

## Design Decisions

### Decision 1 — Stub→backend channel: atomic sidecar JSON in the model version directory

**Decision:** the fixed `OnnxRunner` writes `dda_active_providers.json` into
the model VERSION directory (`os.path.dirname(model_dir)` — the runner's
`model_dir` is the stage subdirectory, so its parent is the version dir that
already holds `model.py` and the per-model `inference_runtimes.py`). Written
via temp-file + `os.replace` (atomic; readers never see a torn file), merged
read-modify-write per stage (stages initialize sequentially inside one stub
process — no concurrency), wrapped in try/except so a write failure logs a
warning and never fails the load (3.1). The backend reads the record from
`TRITON_MODEL_DIR/base_{model}/{maxIntVersion}/` — the backend and Triton
share the container filesystem (the backend itself stands Triton up via
`mlops.create_triton_inference_server`), so this is a plain local file read.

**Rejected alternatives:**
- **Triton model metadata/config surface:** the backend reads Triton state
  through panorama `mlops` (`list_models()` / `model_metadata()`), whose
  payload is fixed (name + state). The python-backend stub has no API to
  inject custom metadata into that surface, and extending `mlops` is out of
  reach entirely.
- **Parsing stub logs:** the stub's stdout/stderr goes through Triton's
  process management; scraping it is fragile (rotation, format drift,
  multi-model interleaving) and gives no structured per-model state. The
  WARNING log is still required by 2.1, but as a signal for humans, not as
  the machine channel.
- **A Triton inference call / extra output tensor to query the stub:** would
  change the model's tensor contract or ensemble wiring — exactly the
  inference-path churn requirements 3.4/3.5 forbid, for a status read.
- **Writing into the stage directory:** the stage dirs are SYMLINKS into the
  deployed Greengrass model artifact directory (`create_sym_links`); writing
  there pollutes the component artifacts. The version dir is a real directory
  created by `model_convertor.py`.

Freshness semantics fall out naturally (2.3): the record is rewritten by every
stub `initialize()`, i.e. it always reflects the currently loaded instance; a
reload after GPU recovery rewrites it with `gpuActive: true`; a new model
version gets a new version dir with no stale record.

**Record shape** (constants duplicated in the writer and the reader — the
stub cannot import backend modules, mirroring the existing "keep in sync"
convention between the templates):

```json
{
  "modelId": "base_yolo_test_1",
  "runtime": "onnx",
  "stages": {
    "<stage_type>": {
      "requestedProviders": ["CUDAExecutionProvider", "CPUExecutionProvider"],
      "activeProviders": ["CPUExecutionProvider"],
      "gpuRequested": true,
      "gpuActive": false
    }
  },
  "gpuRequested": true,
  "gpuActive": false,
  "updatedAt": "2026-08-15T20:23:13Z"
}
```

Aggregation: `gpuRequested` = any stage requested a GPU provider (requested
entries normalized — TensorRT rides as a `(name, options)` tuple in the
chain); `gpuActive` = every GPU-requesting stage obtained a GPU provider (a
single fallen-back stage makes the model degraded). A GPU provider "obtained"
means ANY member of the GPU provider set is active — TRT-requested models
that land on CUDA are not degraded (3.7 example above).

### Decision 2 — Status surface shape: additive per-model field + new device-level endpoint

**Decision:** two additive surfaces.

1. **Per-model (2.2):** `get_features_triton` merges the record into each
   Triton entry's `defaultConfiguration` as `executionProviderInfo`
   (`requestedProviders`, `activeProviders`, `gpuRequested`, `gpuActive`,
   derived `gpuFallback = gpuRequested AND NOT gpuActive`, `updatedAt`).
   Exact precedent: the vLLM `failureReason` additive field in
   `defaultConfiguration`. No record → no field (Decision 6).
2. **Device-level (2.4):** a new `GET /feature-configurations/gpu-status`
   endpoint returning
   `{"gpuDegraded": bool, "gpuChainModels": N, "gpuActiveModels": M, "models": {name: {...}}, "updatedAt": ...}`.
   The same helper feeds the shadow reporter (Decision 4) and logs a
   transition WARNING ("DEVICE GPU DEGRADED: N GPU-chain ONNX models loaded,
   none has an active GPU provider") when the device enters the degraded
   state, INFO on recovery.

**Rejected alternatives:**
- **Top-level field on `/feature-configurations`:** the response is a
  `RootModel` LIST (`ListFeatureConfigsResponse.root: List[...]`) — adding a
  device-level field means changing the response shape, breaking 3.4.
- **Duplicating a `deviceGpuDegraded` flag into every model entry:** noisy,
  ambiguous (which entry is authoritative?), and misrepresents a device-level
  fact as a per-model one.
- **Only deriving it client-side:** 2.4 requires the SYSTEM to surface the
  signal; making every consumer re-implement the aggregation invites drift
  (and the shadow/portal leg needs the server-side computation anyway).

### Decision 3 — Opt-in strict mode: NOT in this spec (deferred)

**Decision:** no strict mode (fail the load instead of falling back) ships in
this spec, in any form — no manifest key, no env knob.

**Rationale:**
- Requirement 3.1 is categorical: graceful degradation is a feature and
  visibility MUST NOT convert the fallback into a load failure. Strict mode
  is a new BEHAVIOR (a new failure path through stub `initialize()`, Triton
  load-state handling, model_convertor's start/retry loop, and autostart),
  not a visibility fix — it belongs in its own spec with its own blast
  radius, failure-mode analysis, and per-target hardware verification.
- The operational need strict mode serves ("don't run degraded silently") is
  exactly what this spec provides by other means: once the fallback is
  loudly logged, queryable, aggregated, and portal-visible, an operator or a
  monitor can react deliberately — including choosing to keep serving on CPU,
  which the incident showed is often the right call (inference stayed
  available all day).
- If field experience later shows a genuine need (e.g. a workload where CPU
  results are unacceptable), a follow-up spec can add a per-model manifest
  opt-in on top of this spec's introspection point, which is the natural
  place to raise. Recorded as a deferral, not a rejection.

### Decision 4 — Portal transport: reported-only `dda-model-status` named shadow, read on demand

**Decision:** the device reports the model-status snapshot into a new named
shadow `dda-model-status` (reported state only — there is no desired state and
no delta handling; this is one-way telemetry). ShadowManager mirrors it to IoT
Core via the portal's existing auto-include `synchronize` configuration in
`deployments.py` (the same list that carries `dda-camera-registry` /
`dda-camera-bindings` — verified: without a synchronize entry a named shadow
never leaves the device). Portal-side, `GET /devices/{id}` reads the shadow on
demand via the existing assumed-role `get_usecase_client('iot-data', ...)`
pattern (`camera_registry.py` refresh precedent) and returns it as an additive
`model_status` field, `null`-tolerant when the shadow does not exist.

**Shadow document (reported):**
```json
{
  "models": {
    "yolo_test": {"status": "READY", "runtime": "onnx",
                   "gpuRequested": true, "gpuActive": false}
  },
  "gpuDegraded": true,
  "gpuChainModels": 3,
  "gpuActiveModels": 0,
  "updatedAt": "2026-08-15T20:25:01Z"
}
```

**Rejected alternatives:**
- **SQS ingest + DynamoDB projection (full camera-registry-sync shape):** the
  camera registry needs conflict resolution, portal-originated mutations, and
  a queryable inventory — none of which applies to one-way, low-cardinality
  telemetry. On-demand GetThingShadow from the device detail page is the
  entire read requirement; no IoT topic rule, queue, table, or reducer.
- **Portal calls the device's local API:** there is no portal→device HTTP
  path (devices are behind Greengrass; the SSH tunnel is operator-interactive).
- **Riding Greengrass component health:** component lifecycle state cannot
  carry model-level payloads.

Access control verified: the LocalServer recipes already grant
`aws.greengrass#UpdateThingShadow` on `$aws/things/*/shadow/name/*` (wildcard —
checked in `recipe-arm64-jp7.yaml`; all targets identical), so **no recipe
edits and no recipe-golden churn**. The device IoT policy created by
`device_provisioning.py` already carries `iot:UpdateThingShadow` for the
ShadowManager HTTP sync path (its comment enumerates the camera shadows; the
policy actions are shadow-name-generic — re-verify at implementation). Known
limitation, documented: the ShadowManager synchronize list only reaches a
device with the NEXT portal-created deployment — which is exactly the
deployment that ships this component version, so the sync config and the
reporter arrive together.

### Decision 5 — Reporter trigger: piggyback on feature-config reads, debounced, failure-isolated

**Decision:** the shadow reporter is invoked from the status read path (the
`/feature-configurations` and `/gpu-status` handlers hand it the snapshot they
just computed). It compares the canonical snapshot against the last written
one and, when changed (subject to a minimum-interval debounce, ~30 s), writes
the shadow via the existing `IoTShadowAccessor` on a short-lived background
thread. Every failure is caught and logged — a shadow problem never affects
the endpoint response (the vLLM isolation precedent in
`get_features_vllm`).

**Rationale and rejected alternative (dedicated poller thread):** a
camera-sync-style daemon agent would need its own Triton access, and standing
up the Triton server from a background thread re-introduces the empty-repo
hang class the endpoint deliberately guards against
(`triton_repo_has_models`). The read path is naturally well-fed at exactly the
moments that matter: `model_convertor.start_model` polls
`/feature-configurations` during every model load (the incident's stub starts
were driven by this exact loop), model autostart polls it, and the station UI
polls it continuously in production. Accepted limitation, stated honestly: on
a device where nothing polls the endpoint, the shadow goes stale — the
device-side truth (logs, sidecar, local API) is unaffected, and the portal
shows the last reported snapshot with its `updatedAt` timestamp visible.

### Decision 6 — Pre-fix per-model runner copies: absence-tolerant by construction

**Decision:** every consumer treats a missing/corrupt Active_Provider_Record
as "no information": no `executionProviderInfo` field, no contribution to the
degraded computation (in either direction), no portal badge — byte-for-byte
today's behavior.

**Rationale (the propagation reality, verified in code):**
`inference_runtimes.py` reaches a model in two hops. Hop 1: on backend
container start, `triton_setup.cp_model_conversion_files()` full-tree
re-syncs `/aws_dda/resources_for_copy` (and `/aws_dda/model_convertor.py`)
from the new image (`dirs_exist_ok=True` — the drift-proof re-sync). Hop 2:
the MODEL component's Greengrass Startup lifecycle runs
`python3 /aws_dda/model_convertor.py ...`, whose
`_create_base_model_structure` copies `inference_runtimes.py` from
`resources_for_copy` into the model's version dir. Hop 2 runs on model
component (re)start — device reboot, Nucleus restart, model redeploy — NOT on
a LocalServer-only deployment (Greengrass restarts only changed components).
So after deploying this fix, already-deployed models keep the old runner (and
write no record) until their next restart; on a reboot the model Startup can
even race the backend's re-sync and stage the old runner once more,
self-healing on the following restart. Absence-tolerance makes every one of
these states safe and signal-free rather than wrong. The on-hardware
verification session restarts the model components explicitly so the signal
is live immediately (and operators can do the same fleet-wide via reboot or
model restart).

## Correctness Properties

Property 1: Bug Condition - Silent CPU Fallback Becomes Visible

_For any_ ONNX model load where the bug condition holds (isBugCondition
returns true — a GPU provider was in the requested chain and the created
session has no active GPU provider), the fixed OnnxRunner SHALL capture the
active providers via `session.get_providers()`, log them at INFO, log a
prominent WARNING identifying the requested chain, the active provider(s), and
that inference will run degraded on CPU, and write an Active_Provider_Record
with `gpuRequested: true, gpuActive: false`; the fixed status surface SHALL
expose that record as `defaultConfiguration.executionProviderInfo` on the
model's `/feature-configurations` entry (distinguishable from GPU operation);
and healthy loads SHALL likewise log and record their active providers
(`gpuActive: true`) with no WARNING.

**Validates: Requirements 2.1, 2.2**

Property 2: Preservation - Everything Outside the Visibility Surface Is Unchanged

_For any_ input where the bug condition does NOT hold — CPU-fallback loads
still completing to READY (visibility never converts fallback into failure,
including when the sidecar write itself fails), healthy-GPU loads (identical
provider chain construction for every `device` value and availability set,
identical session options), CPU-by-design models and the plain x86 image
(never flagged), existing `/feature-configurations` payload fields (additive
only; entries without a record byte-identical to today, vLLM entries
untouched), DLR/Torch/vLLM paths, and the TensorRT opt-in chain — the fixed
code SHALL produce the same result as the original code.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7**

Property 3: Fix Checking - The Record Tracks the Loaded Instance

_For any_ sequence of loads of the same model (e.g. fallback load during an
outage, then reload after GPU recovery), the Active_Provider_Record and the
status surfaces SHALL reflect the session state of the CURRENTLY loaded stub
instance: each `initialize()` rewrites the record atomically (a reader never
observes a torn file), a post-recovery reload reports GPU again, and a model
loaded during an outage reports the fallback until it is reloaded.

**Validates: Requirements 2.3**

Property 4: Fix Checking - Device-Level Aggregation and Portal Display

_For any_ set of per-model records, the device-level signal SHALL be
`gpuDegraded = true` if and only if at least one loaded GPU-chain model has a
record and NO recorded GPU-chain model has an active GPU provider (models
without records and CPU-by-design models contribute nothing in either
direction), surfaced through `GET /feature-configurations/gpu-status`, the
`dda-model-status` shadow, and a transition WARNING log; and _for any_ device
shadow state the portal SHALL render additively — degraded alert + per-model
CPU-fallback badges when reported degraded, provider badges when healthy,
and EXACTLY today's rendering when the shadow or the signal is absent.

**Validates: Requirements 2.4, 2.5**

## Fix Implementation

### Changes Required

**File 1 — `src/backend/dda_triton/resources_for_copy/inference_runtimes.py` (edit; device leg, stub side)**

In `OnnxRunner.__init__`, immediately after session creation:

```python
self.__session = ort.InferenceSession(...)          # unchanged
active = list(self.__session.get_providers())        # NEW: introspection only
requested_names = _provider_names(providers)         # tuple-safe (TRT entries)
gpu_requested = bool(GPU_PROVIDERS & set(requested_names))
gpu_active = bool(GPU_PROVIDERS & set(active))
log.info(f"{model_path}: ONNX session for {model_id} active providers {active}")
if gpu_requested and not gpu_active:
    log.warning(
        f"{model_path}: GPU FALLBACK for {model_id} — requested "
        f"{requested_names} but session is running on {active}; inference "
        f"will run DEGRADED on CPU until the model is reloaded with a "
        f"working GPU (spec: model-gpu-fallback-visibility)"
    )
_write_active_provider_record(...)                   # NEW: atomic, isolated
```

Module additions: `GPU_PROVIDERS = {"CUDAExecutionProvider",
"TensorrtExecutionProvider"}`, `ACTIVE_PROVIDER_RECORD = "dda_active_providers.json"`,
`_provider_names()` (normalizes `(name, options)` tuples), and
`_write_active_provider_record(model_dir, stage_record)` — resolves the
version dir as `os.path.dirname(model_dir)`, read-merges the existing record
(per-stage key = `os.path.basename(model_dir)`), recomputes the model-level
`gpuRequested`/`gpuActive` aggregate, and writes via `tempfile` +
`os.replace`. The ENTIRE visibility block is wrapped so any exception logs a
warning and the constructor proceeds (3.1). `__select_providers`, session
options, and the DLR/Torch runners are byte-identical.

**File 2 — NEW `src/backend/dda_triton/provider_visibility.py` (device leg, backend side)**

The backend-side reader and aggregator (constants kept in sync with File 1 —
the stub cannot import backend modules, mirroring the existing
template "keep in sync" convention):

- `read_active_provider_record(model_name) -> dict | None`: resolves
  `TRITON_MODEL_DIR/base_{model_name}/`, picks the highest integer version
  directory, loads `dda_active_providers.json`; returns `None` on
  missing/corrupt/unreadable (Decision 6), never raises.
- `execution_provider_info(record) -> dict`: the additive
  `executionProviderInfo` payload (requested/active lists, `gpuRequested`,
  `gpuActive`, derived `gpuFallback`, `updatedAt`).
- `device_gpu_status(records: dict[str, record|None], statuses) -> dict`: the
  Property 4 aggregation (`gpuDegraded`, `gpuChainModels`,
  `gpuActiveModels`, per-model map), plus module-level transition state that
  logs the WARNING on entering degraded and INFO on recovery.

**File 3 — `src/backend/utils/feature_configs_utils.py` (edit)**

In `get_features_triton`'s per-model loop, after `default_configs_dict` is
built: `record = read_active_provider_record(model_id)`; if a record exists,
`default_configs_dict["executionProviderInfo"] = execution_provider_info(record)`.
Isolation: wrapped so a reader bug degrades to "no field", never a 500. vLLM
and LFV paths untouched.

**File 4 — `src/backend/endpoints/feature_config.py` (edit; additive endpoint)**

`GET /feature-configurations/gpu-status`: guards exactly like the list route
(`get_is_triton()`, `triton_repo_has_models()` — degraded computation without
standing up Triton when the repo is empty returns the empty/non-degraded
shape), collects records for the non-base/marshal Triton models, returns
`device_gpu_status(...)`. Both this handler and `list_feature_configs` hand
their snapshot to the shadow reporter (File 5) after responding data is
computed. Route order note: registered BEFORE the
`/feature-configurations/models/{modelName}/...` routes cannot collide
(`gpu-status` has no `/models/` segment); still verified in tests.

**File 5 — NEW `src/backend/utils/model_status_shadow.py` (device leg, portal transport)**

`MODEL_STATUS_SHADOW_NAME = "dda-model-status"`. `report(snapshot)`: canonical
JSON compare against the last written snapshot; if changed and ≥30 s since the
last write, spawn a daemon thread that calls
`server_setup.iot_shadow_accessor.update_thing_shadow(thing_name,
MODEL_STATUS_SHADOW_NAME, {"reported": snapshot})` (thing name from
`AWS_IOT_THING_NAME`, the camera-sync convention). Single in-flight write
(lock + flag), every exception logged and swallowed (Decision 5). No desired
state and no delta subscription exist for this shadow.

**File 6 — `edge-cv-portal/backend/functions/deployments.py` (edit; cloud leg)**

`MODEL_STATUS_SHADOW_NAME = 'dda-model-status'` added next to the camera
shadow constants, appended to the auto-include ShadowManager
`synchronize.coreThing.namedShadows` list, and named in the `auto_included`
reason string. Comment updated to note it carries the model GPU-fallback
status (spec: model-gpu-fallback-visibility).

**File 7 — `edge-cv-portal/backend/functions/devices.py` (edit; cloud leg)**

In the single-device GET handler, after the Greengrass status assembly: read
the `dda-model-status` shadow through the use-case-scoped iot-data client
(`get_usecase_client('iot-data', ...)` — the `camera_registry.py` refresh
pattern) and attach the reported document as an additive `model_status` field;
`ResourceNotFoundException`/any error → `model_status: None` (portal renders
as today). No list-route change (no per-device shadow fan-out on the fleet
list).

**File 8 — `edge-cv-portal/frontend/src/pages/DeviceDetail.tsx` (edit; cloud leg)**

Additive "Deployed models" container, rendered ONLY when
`device.model_status?.models` is non-empty: a device-level Cloudscape
`Alert type="warning"` when `gpuDegraded` ("GPU inference degraded — N
GPU-chain models loaded, none has an active GPU provider; models are serving
on CPU fallback", with the shadow `updatedAt`); a per-model table/list with
name, status, and a provider badge — green "GPU" (`gpuActive`), red
"CPU fallback" (`gpuRequested && !gpuActive`), neutral "CPU" (not
`gpuRequested`), no badge when provider info is absent for that model. The
`Device` type in the API service gains the optional `model_status` field.

**File 9 — NEW test suite `test/backend-test/model_gpu_fallback_visibility/`**

Exploration, fix-check, and preservation tests (see Testing Strategy), all
host-runnable with a fake `ort` module and fake shadow accessor.

**Explicitly NOT changed:** `__select_providers` and the DLR/Torch runners,
`lfv_model_template.py` (the runner writes the record itself — the template
stays out of it), `model_convertor.py`, `triton_setup.py`,
`triton_edge_client.py`, all vLLM modules, all recipes (shadow ACL already
wildcard), `src/docker-compose.yaml`, all Dockerfiles,
`src/backend/requirements.txt` (no new dependencies), and therefore **no
security-preservation baseline rebaselines** (verified: the gate pins
docker-compose/Dockerfiles/requirements/recipes/setup_station.sh — none
touched; no baseline pins `inference_runtimes.py` or
`feature_configs_utils.py` content).

## Cross-Spec Documentation Consistency

| Document | Relationship to this fix | Action |
|---|---|---|
| `.kiro/specs/vllm-jp7-engine-cuda-init/bugfix.md` | Authoritative incident record (context-limit probe, clean-window re-test) motivating this spec; its territory (vLLM engine init) is explicitly out of scope here (3.5) | No change — remains the incident-evidence authority |
| `.kiro/specs/csi-nvargus-optional/bugfix.md` + `design.md` | Complementary mitigation: removes the outage trigger and auto-recovers the daemon, host-side; its design names this spec as the visibility complement ("while ORT reports READY on CPU"). Its Session A deliberately reproduces the degraded state — the shared hardware-validation window for THIS spec's real bug condition (see Verification) | No change to its documents; bundling noted in both verification plans |
| `.kiro/specs/model-gpu-fallback-visibility/bugfix.md` | Requirements for this design; amended in the design phase with clause 2.5 (portal leg in scope per binding user decision) + amendment note | Amendment applied with this design (2.5) |
| `docs/multi-runtime-inference.md` | Documents the runner contract and the ONNX engine; the provider-selection contract is unchanged, but the runner now emits the Active_Provider_Record | Update after fix: one subsection on the record + visibility semantics (task) |
| `.kiro/steering/builds.md` | Process authority: sequential builds, security gate, on-hardware verification, portal-deploy-vs-build sequencing | No change; this design's rollout complies |
| `edge-cv-portal/DESIGN_OVERVIEW.md` | Portal architecture overview; gains the `dda-model-status` shadow in whatever section lists the named shadows (if any) | Check at implementation; update only if it enumerates shadows |

## Deployment and On-Hardware Verification

### Rollout shape and scheduling

1. **One build at a time** (`pgrep -af "gdk component build"` /
   `pgrep -af "build-custom.sh"` before dispatching). **Bundling note
   (scheduling only):** this change can ride the same component build cycle as
   the pending `csi-nvargus-optional` builds — one version bump per target,
   and the two specs' verification sessions share the degraded-state window.
   The specs remain independent.
2. **Pre-build gate (builds.md):** no preservation-tracked file changes in
   this spec, so no rebaselines are expected — still run the guard suite
   (`test_preservation_out_of_scope_guard.py`,
   `test_preservation_secrets_out_of_scope_guard.py`) and confirm green;
   move `cdk.out` aside; **no portal deploy while any build runs.**
3. **Build order:** JP7 first (`aws.edgeml.dda.LocalServer.arm64JP7`, log to
   `.gdk_build_jp7.log`) — jetson-thor1 is the verification device; JP5/JP6
   sequentially after (swap `gdk-config.json` per builds.md).
4. **Portal deploy (the cloud leg — Files 6–8) runs strictly AFTER all
   component builds finish** (builds.md rule: portal deploys regenerate
   `cdk.out` mid-build and fail the security gate). Sequence: builds → portal
   deploy → move fresh `cdk.out` aside before any FUTURE build.
5. **Deployment ordering note:** the portal deploy should land before (or
   with) the device deployment that ships this component version, so the
   deployment-creation path already includes `dda-model-status` in the
   ShadowManager synchronize list when the new component rolls out.

### Session A — jetson-thor1 (JP7): healthy path, fallback path, portal leg

1. **Deploy the JP7 component via the portal** (this also delivers the
   ShadowManager synchronize config). Backend healthy, models READY.
2. **Refresh per-model runner copies** (Decision 6): restart the model
   components (or reboot) so `model_convertor.py` re-stages
   `inference_runtimes.py`; confirm `dda_active_providers.json` appears in
   each `base_*/{version}/` dir.
3. **Healthy-GPU verification (3.2, 3.6):** stub logs show the INFO active
   providers line with CUDA and NO warning; `/feature-configurations` entries
   carry `executionProviderInfo.gpuActive: true`; `gpu-status` reports
   non-degraded; `nvidia-smi --query-compute-apps` shows the stubs (the
   incident's tell, now in the affirmative); inference results unchanged.
4. **Shadow + portal verification (2.5):** `dda-model-status` reported state
   visible in IoT Core; portal DeviceDetail shows the models panel with green
   GPU badges; a device WITHOUT the new component still renders today's page
   (absence leg).
5. **Real bug-condition validation (2.1–2.4) — shared window with
   csi-nvargus-optional Session A:** during that session's deliberate
   degraded-state reproduction (device-wide CUDA context creation failing),
   restart one vision model component. Assert: stub WARNING logged; record
   `gpuActive: false`; `executionProviderInfo.gpuFallback: true`;
   `gpu-status` still non-degraded while other models hold GPU (partial
   case) or degraded once all are reloaded degraded; shadow updated; portal
   shows the CPU-fallback badge and (full case) the degraded alert. If the
   degraded state does not reproduce in a bounded window, record that
   honestly — the fallback leg then rests on the host-side fake-ort tests
   plus a CUDA-EP-failure simulation if one can be safely arranged (e.g.
   `CUDA_VISIBLE_DEVICES=""` injected into a test model's stub env is NOT
   currently plumbed; do not improvise device-breaking experiments).
6. **Recovery reload (2.3):** after recovery (watchdog/manual daemon
   restart), restart the model component; assert the record and all surfaces
   report GPU again.
7. **Sustained health per builds.md:** no crash-loop, no container restart,
   status endpoints stable, no shadow write storms (debounce holding), for a
   sustained period.

### x86 / CPU-by-design smoke (3.3)

On an x86 CPU-only station (or the flask-app container harness where real
hardware is unavailable — recorded honestly if so): models load READY, record
shows `gpuRequested: false`, no WARNING, no fallback flag, `gpu-status`
non-degraded with `gpuChainModels: 0`, portal shows neutral "CPU" badges.

### JP5 / JP6 follow-on

Per builds.md's every-arch rule: after their sequential builds, an on-device
smoke on each arch — healthy-GPU INFO line + `gpuActive: true` +
non-degraded `gpu-status` + unchanged inference behavior. The code is
target-independent Python, so JP7's full session plus these smokes is the
honest coverage claim.

## Testing Strategy

### Validation Approach

Two phases per the bugfix methodology: exploration tests written to assert the
FIXED expectation run first on the UNFIXED tree and FAIL (confirming the bug),
then become the fix-check suite; preservation tests PASS on the unfixed tree
and must keep passing. Suite: `test/backend-test/model_gpu_fallback_visibility/`.
Everything is host-runnable with a **fake `ort` module** (controllable
`get_available_providers()` and an `InferenceSession` fake whose
`get_providers()` returns a configured active list — the mechanism by which
CPU fallback is SIMULATED host-side) and a fake shadow accessor; hypothesis
(already a test dependency) drives the property-based tests.

### Exploratory Bug Condition Checking

**Goal**: surface counterexamples demonstrating the bug on UNFIXED code.

**Test cases (all FAIL on unfixed code — this confirms the bug):**
1. **No fallback warning**: construct `OnnxRunner` against the fake ort with
   CUDA in the available set but a CPU-only session; assert a WARNING record
   naming the requested chain and the active provider — unfixed code logs
   nothing after session creation.
2. **No record written**: same load; assert `dda_active_providers.json`
   exists in the version dir with `gpuRequested: true, gpuActive: false` —
   unfixed code writes nothing.
3. **Status surface blind**: seed a version dir with a record, run
   `get_features_triton` against a fake triton_server; assert the entry
   carries `executionProviderInfo` — unfixed entries never do.
4. **No device-level signal**: assert `provider_visibility.device_gpu_status`
   exists and reports degraded for an all-fallback record set — the module
   does not exist on unfixed code.

**Expected counterexamples**: the unfixed `OnnxRunner.__init__` body between
session creation and the input-name read (no `get_providers()` call — the
textual fingerprint of defect 1.1), and unfixed `get_features_triton` output
where a CPU-fallback model's entry is byte-identical to a healthy one
(defect 1.2).

### Fix Checking

**Goal**: for all inputs where the bug condition holds, the fixed code
produces the expected behavior.

**Pseudocode:**
```
FOR ALL load WHERE isBugCondition(load) DO
  result := OnnxRunner_fixed(load)
  ASSERT warningLogged(result) AND recordWritten(result)
         AND statusSurfaced(result) AND fallbackStillServes(result)
END FOR
```

**Test cases (the exploration suite, now passing, PLUS):**
1. **Record lifecycle (Property 3)**: fallback load then healthy reload of
   the same model dir → record transitions `gpuActive` false→true; atomic
   write asserted (record only ever appears via rename; a reader thread
   never observes invalid JSON during rewrites).
2. **Failure isolation (3.1)**: read-only version dir → load completes, a
   warning notes the record write failure, runner serves inference.
3. **Multi-stage aggregation**: two stages, one on GPU and one fallen back →
   model-level `gpuActive: false`; both stage records present.
4. **TRT normalization (3.7)**: requested chain with the
   `("TensorrtExecutionProvider", {...})` tuple → `gpuRequested: true`;
   active = CUDA-only → `gpuActive: true`, NO warning.
5. **Device aggregation (Property 4), property-based**: _for any_ generated
   map of records (gpuRequested/gpuActive combinations, Nones for absent
   records), `gpuDegraded` is true iff ≥1 recorded GPU-chain model exists
   and none is gpuActive; transition WARNING fires exactly on entering
   degraded.
6. **Endpoint**: `gpu-status` route returns the aggregate; empty repo /
   no-Triton guards return the non-degraded empty shape without standing up
   a server.
7. **Shadow reporter**: fake accessor — write on first snapshot, no write on
   identical snapshot, debounced write on change, exception swallowed;
   payload matches the documented document shape.
8. **Portal (cloud leg)**: devices.py handler unit test — shadow present →
   additive `model_status`; `ResourceNotFoundException` → `model_status:
   None` with the rest of the response unchanged. DeviceDetail component
   test — degraded shadow → alert + red badge; healthy → green badge; absent
   → panel not rendered (today's DOM).

### Preservation Checking

**Goal**: for all inputs where the bug condition does NOT hold, fixed behavior
equals unfixed behavior.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT F(input) = F'(input)
END FOR
```

**Test plan (observation-first — capture unfixed behavior, assert it holds
after the fix; these PASS on unfixed code):**
1. **Provider chain identity (3.2, 3.3, 3.7), property-based**: _for any_
   `device` value (`None`, `"cpu"`, `"gpu"`, `"cuda"`, `"tensorrt"`, `"trt"`,
   arbitrary case) and _any_ available-provider set, the fixed
   `__select_providers` returns the exact chain the unfixed one returns
   (captured as a reference implementation from the unfixed code).
2. **Session construction identity (3.2)**: fake ort records
   `InferenceSession` call args — model path, `sess_options` thread counts,
   providers list are unchanged; `get_providers()` is the ONLY new session
   interaction.
3. **Fallback still serves (3.1)**: CPU-only session load completes to a
   working runner on both trees.
4. **Status payload identity without records (3.4, 3.5), property-based**:
   _for any_ fake Triton model list (including base_/marshal_ filtering and
   vLLM entries), `get_features_triton` output with no sidecar files present
   is deep-equal to the unfixed output; with records present, removing the
   `executionProviderInfo` key restores deep-equality (additive-only,
   executable).
5. **CPU-by-design never flagged (3.3)**: `device: "cpu"` and
   CUDA-unavailable chains → `gpuRequested: false`, no warning, no degraded
   contribution.
6. **DLR/Torch untouched (3.5)**: module-level assertion that `DlrRunner` /
   `TorchRunner` bodies are unchanged from the unfixed tree (hash pin in the
   suite), and `make_runner` dispatch behavior identical.
7. **Portal absence leg (2.5/3.4)**: devices.py response for a device with no
   shadow is deep-equal to the unfixed response minus the additive
   `model_status: None` key; DeviceDetail with `model_status` absent renders
   the unfixed DOM.

### Unit Tests

- Version-dir picking (numeric max, ignores non-numeric entries), missing
  base dir, corrupt/empty JSON, permission errors → `None`.
- `_provider_names` tuple/string normalization.
- Debounce arithmetic and in-flight write exclusion in the shadow reporter.
- gpu-status route registration does not shadow the
  `/models/{name}/start|stop` routes.

### Property-Based Tests

- Fix-check 5 (device aggregation) and preservation 1 and 4 above — the
  strongest guarantees in the suite (hypothesis).
- _For any_ requested/active provider list pair, `gpuFallback` ==
  `gpuRequested AND NOT gpuActive` and the record round-trips through
  write→read unchanged.

### Integration Tests

- End-to-end through the real staging layout: build a fake
  `base_model/{version}/` tree the way `model_convertor` does, run the fixed
  runner (fake ort) inside it, then `get_features_triton` +
  `device_gpu_status` + shadow reporter over the same tree — the full device
  leg without Triton.
- On-hardware Session A (above) is the real integration tier.

### Honesty Guard — what host tests CANNOT prove

- **Real ORT CUDA-EP init failure**: host-side there is no GPU and no
  ORT-CUDA execution — every host test SIMULATES fallback through the fake
  ort's `get_providers()`. That real ORT strips the CUDA EP exactly this way
  is evidenced by the incident itself and validated on hardware only
  (Session A step 5, in the shared degraded-state window; recorded honestly
  if the window cannot be arranged).
- **The stub process environment**: that the record lands where the backend
  reads it on a real device (paths, permissions, the Triton stub's CWD and
  user) — Session A steps 2–3.
- **ShadowManager sync end-to-end**: local shadow → IoT Core mirroring under
  the auto-included synchronize config — Session A step 4.
- **Greengrass IPC shadow writes from the backend container** — fake accessor
  host-side; real IPC on device only.
- **JP5/JP6 parity**: identical Python, but per builds.md not "done" until
  their on-device smokes run.
- **Portal IAM in a real use-case account**: the assumed-role iot-data
  GetThingShadow for the new shadow name — verified at portal deploy.
