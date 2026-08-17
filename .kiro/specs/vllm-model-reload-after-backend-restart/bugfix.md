# Bugfix Requirements Document

## Introduction

On jetson-thor1 (JP7/Thor, LocalServer.arm64JP7 1.0.6) the user-visible symptom was
"qwen3-vl-8b-instruct is stuck loading on thor1". The live-device diagnosis
(2026-08-16 22:30–22:45Z, verified evidence) showed the model is NOT stuck loading —
it loaded successfully once and was then silently lost: a later LocalServer backend
container restart destroyed the in-process vLLM runtime, and nothing on the device
ever re-issues the load. The Greengrass model component
`model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp7` v1.0.0 keeps reporting RUNNING
and the backend reports healthy while the runtime holds zero models
(`gpuActiveModels: 0, models: {}`).

**The core defect this spec targets:** the vLLM model component's lifecycle issues
the model load exactly once, in its Startup script
(`src/backend/dda_triton/vllm_model_prep.py`, invoked by the recipe generated in
`edge-cv-portal/backend/functions/greengrass_publish.py`). Once Startup exits 0, no
component, script, or backend code ever re-asserts the load. The loaded engine lives
in the backend process (`VllmRuntimeManager` in
`src/backend/vllm_runtime/manager.py`, started by `app.py`'s
`start_vllm_runtime()`), so ANY backend restart — crash, abort, compose
`restart: unless-stopped` recovery, deployment restart — silently converts a READY
model into "unloaded forever while every dashboard shows RUNNING/healthy". The
staged repository survives on disk (`VLLM_MODEL_DIR =
/aws_dda/dda_triton/vllm_model_repo`), but the recreated manager starts with an
empty model table and never scans-and-reloads it. Code details that pin this:

- `vllm_model_prep.py`'s own `LOAD_UNREACHABLE` diagnostic claims the model "stays
  staged for the next LocalServer start" — implying an auto-load-on-start that does
  not exist anywhere in the codebase.
- `feature_configs_utils._VLLM_STATUS_MAP` maps STAGED→"LOADING" on the documented
  assumption that "STAGED models have their load request on the way" — exactly the
  assumption this bug breaks. After the backend restart, the staged-but-dropped
  model reports "LOADING" indefinitely through the feature-config API and shadow
  sync.
- The manager's `_ready_engine` raises `ModelUnavailableError` for any non-READY
  model; there is no lazy load. Generate requests answer HTTP 409 forever. Workflow
  LLM output bindings (`workflow_engine/output_bindings.py`) poll the 409-loading
  state for a bounded 240 s (`LLM_LOADING_BUDGET_SEC`) and then fail terminally.
- By contrast, the embedded vision Triton (`TritonEdgeClient`) is recreated over its
  persistent model repository on every backend start, and vision models can be
  (re)started on demand (`start_model_triton` on UNKNOWN/UNAVAILABLE models); the
  same silent-unload incident window (Aug 14–15) ended for ONNX/Triton models via
  reload on the next request/start — for vLLM models there is no equivalent path.

### Incident record (jetson-thor1, 2026-08-16 — verified live-device evidence)

Failure chain observed:

1. **21:51:26Z** — first load attempt died with `torch.AcceleratorError: CUDA
   error: CUDA-capable device(s) is/are busy or unavailable` during the nvargus
   Error(89) degraded window (kernel: `Can't map dma attachment!` +
   `osCreateOsDescriptorFromFileHandle(): Error (89)`, 323,543 occurrences since
   Aug 14; cleared by an nvargus-daemon restart at 21:51:30Z). This leg is covered
   by the sibling specs `csi-nvargus-optional` (watchdog) and
   `vllm-jp7-engine-cuda-init` — NOT this spec.
2. **21:52:32Z** — the component retry loaded the model successfully ("Model
   'qwen3-vl-8b-instruct' loaded successfully!", KV cache 37.20 GiB). Startup
   exited 0; component RUNNING.
3. **21:55:00, 21:59:33, 22:23:36, 22:25:38, 22:33:10Z** — the JP7 backend
   container was killed 5x by the awscrt fatal abort (`Fatal error condition ...
   event_stream_rpc_client.c:961: ref_count != 0 && "Continuation ref count has
   gone negative"`; RestartCount=5). Each abort destroys the in-process vLLM
   runtime. The awscrt abort itself is pre-existing and explicitly OUT of scope
   (tracked as a known follow-up).
4. **22:33:11Z →** — the current backend is up and healthy with an EMPTY model
   runtime: `gpuActiveModels: 0, models: {}`, while the model component reports
   RUNNING. The model stays gone until a human intervenes (component restart or
   redeployment).

The same defect bit during the Aug 14–15 incident: models silently unloaded until
the next lazy request — which exists for ONNX/Triton and does NOT exist for vLLM.

**Aggravator (candidate companion requirement — user may split into its own
spec):** a STALE JP6 LocalServer deployment (`aws.edgeml.dda.LocalServer.arm64JP6`
v1.0.59) is deployed ALONGSIDE JP7 on this Thor device. All LocalServer backends
run `network_mode: host` (`src/docker-compose.yaml`), so both lineages contend for
the same loopback ports; the JP6 backend crash-loops (RestartCount=61) on
`[Errno 98] address already in use ('127.0.0.1', 8901)` (JP7 owns 8901, the vLLM
runtime port) and logs arch-mismatch artifact rejections. Its restart churn hammers
Greengrass IPC every ~60 s — the plausible trigger environment for the awscrt
aborts that killed the JP7 backend. How both lineages got deployed to one thing
(possibly two overlapping thing-group/thing deployments) needs a look at the
thing's deployment history; that operational question is recorded here but the
code-side requirement below covers only the device's behavior under the conflict.

**Explicitly out of scope:** the awscrt refcount abort itself (pre-existing, known
follow-up); the nvargus driver defect (covered by `csi-nvargus-optional` +
`vllm-jp7-engine-cuda-init`).

## Bug Analysis

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X — a device state observation (one vLLM model on one device)
  OUTPUT: boolean

  RETURN X.model.runtime = "vllm"
     AND X.model.staged_repo_present            // VLLM_MODEL_DIR/{name} on disk
     AND X.model.component_state = RUNNING       // Startup exited 0, no --cleanup
     AND X.backend_restart_occurred_after_load   // in-process engine destroyed
     AND NOT X.load_reissued                     // no reconciliation, no lazy load
END FUNCTION
```

For every X where `isBugCondition(X)` holds, the unfixed system leaves the model
unloaded indefinitely (generate → 409 forever, workflow LLM nodes fail terminally
after the 240 s poll budget) while reporting the component RUNNING and the model
"LOADING". The fixed system SHALL make `NOT X.load_reissued` unreachable: a load is
re-driven for every staged, desired model after a backend restart (Expected
Behavior 2.1–2.3). Note the contrast domain for preservation: observations where
the model was removed by `--cleanup` (repo unstaged), where the model is
ONNX/Triton, or where no backend restart occurred are ¬C(X) and must behave
exactly as today.

### Current Behavior (Defect)

The vLLM model load is a one-shot Startup action with no reconciliation; a backend
restart silently and permanently drops every loaded vLLM model:

1.1 WHEN the LocalServer backend process exits after a vLLM model reached READY
(awscrt abort, crash, OOM-kill, compose `restart: unless-stopped` recovery,
deployment-driven container recreation) THEN the system destroys the in-process
vLLM runtime and all loaded engines with it, and the recreated backend starts a
fresh `VllmRuntimeManager` with an empty model table — the still-staged repository
under `VLLM_MODEL_DIR/{model_name}` is never scanned-and-reloaded and no load
request is ever re-issued (observed: jetson-thor1 22:33:11Z, healthy backend,
`gpuActiveModels: 0, models: {}`)

1.2 WHEN the model component's Startup script has already exited 0 THEN the system
keeps the Greengrass component in RUNNING state regardless of whether its model is
actually loaded — the component lifecycle (Startup: `vllm_model_prep.py` prepare;
Shutdown: `--cleanup`) contains no mechanism that observes the backend restart or
re-asserts the load, so component state and dashboards report healthy while the
model is gone (observed: component RUNNING for 5 backend kills in a row)

1.3 WHEN a generate request (Text_Generation_API, workflow LLM output binding, or
the runtime's `/v2/models/{m}/generate`) names the dropped model THEN the system
answers HTTP 409 with the model's non-READY state on every request indefinitely —
`_ready_engine` performs no lazy load for a staged repository (unlike ONNX/Triton
vision models, which can be re-started on demand after a backend restart) — and
workflow LLM nodes exhaust their bounded 409-loading poll budget (240 s) and fail
terminally

1.4 WHEN the dropped model's staged repository is still on disk THEN the system
reports the model as "LOADING" through the feature-config API and shadow sync
indefinitely (`_VLLM_STATUS_MAP` maps STAGED→LOADING on the now-broken assumption
that a staged model's load request "is on the way"), so every dashboard shows a
permanently-loading-but-healthy model instead of an unloaded one

1.5 (aggravator — candidate to split into its own spec) WHEN a second LocalServer
lineage is deployed alongside the device's native lineage (observed: stale JP6
LocalServer.arm64JP6 v1.0.59 beside JP7 on Thor) THEN the losing backend crash-loops
indefinitely on `[Errno 98] address already in use ('127.0.0.1', 8901)` under
`network_mode: host` (RestartCount=61, one restart per ~60 s), hammering Greengrass
IPC with connect/disconnect churn — the plausible trigger environment for the awscrt
aborts that restart the winning backend and trigger 1.1 — with no diagnostic naming
the lineage conflict

### Expected Behavior (Correct)

2.1 WHEN the LocalServer backend restarts while one or more vLLM models have staged
repositories under `VLLM_MODEL_DIR` whose model components are RUNNING (i.e. staged
by a Startup that succeeded and not removed by a Shutdown `--cleanup`) THEN the
system SHALL re-establish the loaded state of each such model automatically — without
manual intervention, component restart, or redeployment — driving each model back
through LOADING to READY (or to FAILED with the retained backend reason if the
reload genuinely fails)

2.2 WHEN a generate request names a staged-but-not-yet-reloaded vLLM model during
the post-restart reconciliation window THEN the system SHALL answer with the
existing 409 state-info mapping reflecting a load genuinely in progress (so the
workflow LLM binding's bounded 409-loading poll can ride through the reload), and
once the reload completes the same request path SHALL serve normally

2.3 WHEN the device model-status mechanisms (feature-config API, shadow sync)
report a vLLM model THEN the reported status SHALL reflect reality within a bounded
time: a model whose engine was lost and whose reload is pending or in progress
SHALL be reported as loading only while a load is actually being driven, and a model
whose reload has terminally failed SHALL be reported FAILED with the retained
reason — never indefinitely "LOADING" with no load in flight

2.4 WHEN the model component's Shutdown (`vllm_model_prep.py --cleanup`) has
unloaded a model and removed its staged repository THEN the system SHALL NOT
resurrect that model on any subsequent backend restart (reconciliation applies only
to models that remain staged/desired)

2.5 (aggravator — candidate to split into its own spec) WHEN a LocalServer backend
cannot bind a required loopback port because another LocalServer lineage on the
same device owns it THEN the system SHALL surface an explicit diagnostic naming the
port and the lineage/coexistence conflict (rather than a bare `[Errno 98]`
crash-loop), and SHALL avoid unbounded ~60 s restart churn against Greengrass IPC

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a vLLM model component deploys for the first time THEN the system SHALL
CONTINUE TO run the existing Startup sequence unchanged: validate the unarchived
repository, rewrite the S3 weights sentinel, stage atomically into
`VLLM_MODEL_DIR`, and request the load with the existing retry/backoff,
exit-code, and diagnostic semantics (validation defect → exit 1;
`LOAD_UNREACHABLE` → exit 1 with the backend-container diagnostic;
`LOAD_HTTP_ERROR` → exit 1 with the authoritative failure log; KV-cache OOM →
single unload→reload recovery cycle)

3.2 WHEN a vLLM model load or serve fails THEN the system SHALL CONTINUE TO
isolate the failure to that model (STAGED → LOADING → READY | FAILED with retained
reason), never touching another engine, and SHALL NOT convert a terminal FAILED
state into an unbounded automatic retry storm

3.3 WHEN ONNX/Triton vision models are deployed, converted, loaded, stopped, or
re-warmed after a backend restart THEN the system SHALL CONTINUE TO behave exactly
as before — the embedded vision Triton, its model repository, `model_convertor.py`,
and the feature-config start/stop paths are untouched

3.4 WHEN a generate request names a READY vLLM model THEN the system SHALL
CONTINUE TO serve it with the existing request validation, 409/422/502 error
mappings, streaming semantics, and multimodal handling unchanged

3.5 WHEN an operator unloads a vLLM model through the model-control endpoint
(`POST /v2/repository/models/{m}/unload`) THEN the system SHALL CONTINUE TO honor
the unload idempotently (how long an explicit unload holds against reconciliation
of a still-staged repository is a design decision to be settled in the design
phase, but the unload call itself must keep succeeding and freeing the engine)

3.6 WHEN the fix runs on vLLM-free images (JP5, x86 variants without the vllm
wheel) THEN the system SHALL CONTINUE TO start with the exact pre-feature sequence
— no vLLM runtime, no reconciliation activity, no new failure modes

3.7 WHEN a vLLM model component's lifecycle runs THEN the system SHALL CONTINUE TO
never restart LocalServer from a model component lifecycle, and Greengrass
component lifecycle semantics for existing components SHALL remain valid (no
Startup-script semantics change that would break deployed component recipes)

3.8 WHEN the single-lineage (normal) deployment runs THEN the system SHALL
CONTINUE TO bind the vLLM runtime to 127.0.0.1:8901 (env-overridable
`VLLM_RUNTIME_PORT`) and the backend's existing ports exactly as before — the
aggravator diagnostic (2.5) changes behavior only in the port-conflict case

## Addendum (2026-08-16/17 — verified live checks 23:28–00:00Z)

Verified provenance and remediation findings from the live session following the
jetson-thor1 revision-9 thing deployment (`e3b3c7ce-40c3-4d27-93d3-3033b4da885a`,
COMPLETED 23:28Z). The requirements above stand as written; this addendum refines
the incident record and the aggravator's characterization. The spec does NOT
advance past requirements with this edit.

### (a) Provenance correction: the stale JP6 came from a PRIOR thing-deployment revision, not a local deployment

The stale `aws.edgeml.dda.LocalServer.arm64JP6` v1.0.59 was NOT a local
deployment — it was carried by a PRIOR revision of the thing deployment.
Revision 9 removed it. greengrass.log verbatim: `23:29:44.707Z`
DeploymentConfigMerger `"Removing services {service-to-remove=[model-vllm-qwen3-vl-8b-instruct, aws.edgeml.dda.LocalServer.arm64JP6]}"`;
the JP6 service reached FINISHED at 23:29:47Z; component store delete at
23:29:49Z.

### (b) Pre-removal IPC churn — the suspected awscrt-abort trigger environment

Before its removal, the JP6 principal generated IPC authorization failures at
~60 s intervals: `"Principal aws.edgeml.dda.LocalServer.arm64JP6 is not
authorized to perform GetThingShadow/SubscribeToIoTCore/ListComponents"`. This
is the suspected trigger environment for the awscrt aborts that killed the JP7
backend (incident step 3 above). The awscrt refcount abort itself remains out
of scope as recorded.

### (c) Remediation outcome (verified)

After the JP6 removal plus a model component restart (Startup 23:45:47Z, exit 0
at 23:46:35Z): qwen READY at 23:47Z; a generate request served in 64.8 s;
backend RestartCount frozen at 4 through a 12-minute watch window; zero new
awscrt aborts; zero Error(89); `gpu-status` reported `gpuDegraded=false` with
3/3 vision models `gpuActive`.

### (d) Factual correction

The device's Greengrass root is `/aws_dda/greengrass/v2` (not `/greengrass/v2`).

### Aggravator requirement note (1.5 / 2.5)

The coexistence window arises from thing-deployment REVISION transitions: a
revision that drops a lineage cleans it up correctly (as revision 9 did here) —
the bug window is while a revision still lists BOTH lineages, or during
platform re-variant transitions. The missing-diagnostic requirement (2.5's
explicit port/lineage-conflict diagnostic instead of a bare `[Errno 98]`
crash-loop) stands as written.

---

## Amendment (2026-08-17): aggravator root cause found (portal packaging leg); awscrt trigger theory refuted

Tonight's completed device remediation (2026-08-17 00:00Z, verified live-device
and account evidence) closes the two open operational questions in this
document and reshapes the aggravator legs. The original text above is retained
unmodified per house style; the corrections below supersede it where they
conflict.

### A1. Corrected aggravator mechanism — the dual lineage was TRANSITIVE, not two deployments

The Introduction's hypothesis ("possibly two overlapping thing-group/thing
deployments") is **refuted**. jetson-thor1 has exactly ONE thing-level
deployment ("jetson-thor1-platform-variant-jp7", id `bd65402c`,
portal-managed). The JP6 stack arrived transitively through the deployment's
own dependency closure:

- root workflow component `dda.workflow.421f8233:7.0.0` carries a HARD
  dependency on the NON-SUFFIXED model component
  `model-vllm-qwen3-vl-8b-instruct` (v1.0.0, a JP6-era artifact),
- which itself HARD-depends on `aws.edgeml.dda.LocalServer.arm64JP6 >=1.0.0`,
- so the JP7 deployment dragged `LocalServer.arm64JP6` 1.0.59 onto the Thor.
  Workflow v6.0.0 carries the same stale non-suffixed dependency.

**The portal-side bug:** the workflow packaging path emitted a
non-platform-suffixed vLLM model dependency even though the platform-suffixed
component (`model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp7`) exists and was
already a root of the same deployment. Code reading pins the emission path:
`edge-cv-portal/backend/functions/workflow_packaging.py`'s
`resolve_model_components` short-circuits on the vLLM singular
`published_component` map and hands its `component_name` — which
`greengrass_publish.py` deliberately keeps as the UNSUFFIXED base name
(`model-vllm-{safe_model_name}`, the `component_name-index` GSI key for legacy
readers) — straight to `model_component_dependencies`, which emits it verbatim
as the HARD dependency. Every vLLM-referencing workflow packaged on this path
gets an arch-agnostic model dependency; on any account where a stale artifact
exists in Greengrass under that base name, the deployment closure silently
drags a wrong-arch LocalServer lineage onto the device. This is now a
**first-class defect leg of this spec** (clauses 1.6 / 2.6 / 3.9 below).

**Remediation already applied in the account (operational, not the code
fix):** workflow 7.0.1 published with the dependency repointed to the
platform-suffixed component; deployment revision `e3b3c7ce` applied; the
device is clean (JP6 stack fully removed).

**Scope disposition:** the device-side port-conflict diagnostic legs 1.5 / 2.5
remain a candidate split into their own spec (defense-in-depth; the coexistence
scenario is now prevented at its source by 2.6) and are NOT implemented by this
spec. The portal packaging leg (1.6 / 2.6) IS in scope.

### A2. New defect clauses (portal packaging leg)

Appended to Current Behavior (Defect):

1.6 WHEN the portal packages a workflow that references a vLLM model (an
`llm_inference` model_ref) THEN the workflow packaging path emits a HARD
ComponentDependencies entry naming the record's UNSUFFIXED base component name
(`model-vllm-{safe_model_name}`, arch-agnostic) instead of the
platform-suffixed Per_JetPack_Component matching the workflow's target
architecture — and WHEN a stale artifact exists in Greengrass under that base
name (observed: `model-vllm-qwen3-vl-8b-instruct` v1.0.0, JP6-era,
HARD-depending on `aws.edgeml.dda.LocalServer.arm64JP6 >=1.0.0`) THEN the
deployment's dependency closure silently installs a wrong-arch LocalServer
lineage on the device (observed: LocalServer.arm64JP6 1.0.59 beside JP7 on the
Thor, via workflow 7.0.0 and 6.0.0)

Appended to Expected Behavior (Correct):

2.6 WHEN the portal packages a workflow that references a vLLM model THEN
workflow packaging SHALL resolve the reference to the platform-suffixed
Per_JetPack_Component(s) matching the workflow's selected target
architecture(s) and SHALL emit only platform-suffixed model component
dependencies; and WHEN no platform-suffixed published component covers a
selected architecture — including legacy records that carry only the
unsuffixed base name — THEN packaging SHALL fail closed with an error naming
the model and the uncovered architecture. It SHALL NEVER emit the unsuffixed
base component name as a dependency. (Whether packaging additionally
introspects the model component's own LocalServer dependency for an
arch-contradiction guard is a design decision — settled in design.md.)

Appended to Unchanged Behavior (Regression Prevention):

3.9 WHEN the portal packages a workflow that references only vision models,
plugins, or no models at all THEN workflow packaging SHALL CONTINUE TO resolve
and emit its ComponentDependencies exactly as before — vision per-target
resolution with the Defect F single-variant omission and Defect G fail-closed
coverage semantics, `dda.plugin.*` pinning, and the LocalServer single-variant
discipline all unchanged

### A3. awscrt trigger theory REFUTED — incident record corrected

The original record (Incident step 3 and the 1.5 aggravator framing) treats
the stale-JP6 restart churn as "the plausible trigger environment" for the
awscrt fatal aborts. **Refuted by tonight's evidence:** after the JP6 stack was
fully removed, the awscrt "Continuation ref count has gone negative" abort
fired 3 MORE times (23:32:48, 23:35:20, 23:38:53Z), each ~45–100 s after a
vLLM engine spawn, each killing a just-loaded model. The aborts correlated
with Greengrass IPC churn from overlapping deployment/component restarts and
stopped once the churn settled — the 4th load attempt (23:46Z) was stable:
READY 14+ minutes, generate smoke passed. So the JP6 port conflict was an
aggravator of IPC churn but NOT the abort's necessary trigger; ANY deployment
churn can re-fire it.

The awscrt abort itself STAYS explicitly out of scope (pre-existing, known
follow-up). But the corrected record **strengthens the case for the core fix
(2.1 reconciliation)**: on the unfixed system, every abort occurrence — and
they re-fire on ordinary deployment churn, with or without a lineage conflict
— silently drops every loaded vLLM model until a human intervenes.

### A4. Manual intervention observed — exactly what 2.1 eliminates

The qwen model reached READY tonight only because a HUMAN restarted the model
component after the churn settled. That restart re-ran the component's Startup
(`vllm_model_prep.py` prepare → load request) — the manual re-drive of the
load that requirement 2.1's automatic reconciliation exists to make
unnecessary.

### Additional awscrt evidence (2026-08-17 01:22/01:30Z)

The awscrt refcount abort fired twice more during the 1.0.7 deployment window
on thor1 (verbatim `Fatal error condition ... event_stream_rpc_client.c:961:
ref_count != 0 ...` preceded by `SystemError: null argument to internal
routine`), each immediately after IoT shadow IPC activity — one after a
gpu-status GET at 01:22:54Z, one after a camera-registry
UpdateThingShadowResponse at 01:28:08Z. RestartCount 2, auto-recovered, quiet
for 12+ min after. Strengthens the shadow-IPC-traffic correlation for the
out-of-scope awscrt follow-up.
