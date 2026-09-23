# Bugfix Requirements Document

## Introduction

A freshly deployed model component shows state `UNKNOWN` in the portal, and
the FIRST workflow run that references it fails with the exact user-visible
error:

> "Error running workflow: The server is unable to process the request because
> of a pipeline processing error. Error: 'Pipeline failed to change state to
> PLAYING, check logs above this. : Source image file has been moved to
> /aws_dda/inference-results/atubh5ft/failed/bus.jpg' Check the pipeline and
> retry again."

After the failure the model load completes in the background, the state moves
to `READY`, and every subsequent run works. Observed live on 2026-08-14 on
`jetson-thor1` (JP7/Thor) during on-hardware verification of the deployed
`onnx-jetson-publish-packaging` spec, and reproduced on three models the same
day: `cookies-segmentation` (compiled ONNX), `yolo-test` and `rf-detr` (BYO
ONNX). The failure class is runtime- and JetPack-agnostic: DLR models on
JP5/JP6 have the same window, just shorter; ONNX models on Thor make it
near-certain because the first `OnnxRunner` load can build a TensorRT engine
for a ~300 MB model, taking minutes.

The failure has three compounding harms:

1. **The error is a lie by omission.** The operator is told to "check the
   pipeline and retry" — a generic GStreamer pipeline failure — when the
   actual cause is that the model is still converting/loading. An operator
   will chase a phantom pipeline bug ("the optics of it don't look good").
   This resembles the historical Triton model-load race incident recorded in
   `.kiro/steering/builds.md`.
2. **The failure consumes the input.** For folder image sources, the
   catch-all failure handler moves the source image into the workflow's
   `failed/` directory — a transient not-ready condition destroys a valid
   input as if it were corrupt.
3. **The cold state is silent and unexplained.** Nothing in the portal or the
   error links the failed run to the model's load state at failure time.

**Root cause chain (verified in code).** The workflow-run path never consults
model state; the deployment-time preload hook exists but every one of its
failure modes is silent; and the failure handler treats all pipeline errors
as input errors:

- `src/backend/endpoints/workflow.py` `run_inference_for_stream`
  (`POST /workflows/{workflow_id}/run`, ~line 143): the only precondition
  checked is `validate_workflow_requirements` (image sources present, ~line
  74) and the mere *presence* of `featureConfigurations`. Model state is never
  read before the pipeline is built and started.
- `src/backend/gstreamer/pipeline_builder.py` `_add_inference_plugins`
  (~line 185): the pipeline embeds the `emltriton` element with `model-repo`,
  `server-path`, and the model name. Loading is lazy: the element resolves
  the model against the in-process Triton core when the pipeline starts, so
  the first run against a cold model triggers (or collides with) the actual
  load.
- `src/backend/gstreamer/gst_pipeline.py` `run_pipeline` (~line 200):
  `pipeline.set_state(Gst.State.PLAYING)` returns `FAILURE` synchronously
  when `emltriton` cannot bring the model up within its startup window, and
  raises `PipelineExecutionException("Pipeline failed to change state to
  PLAYING, check logs above this.")` (~line 225) — the bus-drain enrichment
  found no detail in the observed instance.
- `src/backend/gstreamer/gst_pipeline_executor.py` `execute_workflow_pipeline`
  (~line 207): the `except` branch moves the folder-source image to
  `failed/` for ANY pipeline exception and appends "Source image file has
  been moved to {path}" to the exception args — no distinction between a
  genuinely bad input and a transiently unavailable model.
- `src/backend/exceptions/handlers/exception_handlers.py`
  `pipeline_execution_exception_handler` (~line 94) wraps everything in the
  generic "pipeline processing error … Check the pipeline and retry again."
- **The deployment-time preload hook already exists — and fails silently.**
  (Surprise versus the initial "conversion happens lazily on first run"
  hypothesis: the load IS kicked at deployment; it just never sticks and
  nobody notices.) The model component's Greengrass `Startup` runs
  `model_convertor.py` (recipe generated in
  `edge-cv-portal/backend/functions/greengrass_publish.py`, ~line 277,
  Startup `Timeout: 1800`), which after conversion calls
  `start_model(model_name)` (`src/backend/dda_triton/model_convertor.py`
  `__main__`, ~line 788). Silent failure modes:
  - The `start_model` return value is **ignored** — a `False` (never loaded,
    never READY) still exits 0, the component reports healthy, and the model
    stays `UNKNOWN` (~line 788).
  - `wait_for_server` (`src/backend/dda_triton/model_autostart_utils.py`,
    ~line 53) gives up after ~93 s; during a Greengrass deployment window the
    LocalServer backend may be restarting, so no load is ever enqueued.
  - `START_MODEL_READY_TIMEOUT_S = 120` with 3 attempts
    (`model_convertor.py` ~line 62) is sized for DLR ("tens of seconds" per
    its own comment); an ONNX first load that builds a TensorRT engine takes
    minutes, so `start_model` gives up even when the load is progressing.
  - Loaded state does not survive a LocalServer restart: the Triton server is
    an in-process, lazily created singleton
    (`src/backend/dda_triton/triton_edge_client.py`, `TritonEdgeClient`),
    and nothing reloads previously READY models at boot — they revert to
    `UNKNOWN` until something (portal start button, model component restart,
    or the first inference) loads them again.
- **State visibility**: `/feature-configurations`
  (`src/backend/endpoints/feature_config.py` ~line 85 →
  `utils/feature_configs_utils.py` `get_features_triton`) passes through the
  raw Triton state token (`UNKNOWN` default in
  `triton_edge_client.py::list_triton_models`). The portal shows the bare
  token with no lifecycle semantics (converting/loading/ready) and no
  guidance.

**Mechanism note (hypothesis flagged).** `emltriton` is a closed-source
plugin (NeoAgentSmith); the exact interaction between its startup deadline
and the in-progress load is inferred from observed behavior (state-change
FAILURE on cold model; READY after background load completes; warm runs
succeed). The exploration phase must confirm the precise failure point before
the fix mechanism is finalized in design.

**Sibling coordination.**
- `.kiro/specs/onnx-jetson-publish-packaging/` (deployed) is the *discovery
  context only*: it delivered the per-JetPack ONNX components whose
  on-hardware verification surfaced this bug. Nothing in that spec's
  packaging/publish contracts is touched here.
- `.kiro/specs/onnx-compile-error-diagnostics/` is cloud-side (portal
  compilation status); no overlap. This spec composes with nothing in flight
  on the cloud side.
- This is **ON-DEVICE code** (`src/` LocalServer backend). Per
  `.kiro/steering/builds.md`, any fix requires building the affected
  LocalServer component (~1–2 h per target, strictly one build at a time,
  security-preservation gate pre-checked) and verifying on real hardware for
  every JetPack the change touches (JP5/JP6/JP7) before it can be called
  done. Unit and container tests are necessary but NOT sufficient.

**Non-goals.** This spec does not change the `emltriton` plugin itself, does
not change model conversion/packaging outputs (`model_convertor.py`'s Triton
repository layout and atomic publish are untouched), does not touch the vLLM
model path, does not change cloud-side packaging/publish/compilation, and
does not add a Neo `jetson-xavier-jp7` compile target.

## Bug Analysis

### Current Behavior (Defect)

**Defect 1 — the workflow-run path never consults model state**

1.1 WHEN `POST /workflows/{workflow_id}/run` is invoked for a workflow with
`featureConfigurations` THEN the system checks only that image sources exist
and that feature configurations are present, and builds and starts the
GStreamer pipeline without reading the referenced model's state

1.2 WHEN the pipeline starts while the referenced model is not READY (state
`UNKNOWN`, or loading/converting in progress) THEN the system's
`pipeline.set_state(PLAYING)` fails and raises
`PipelineExecutionException("Pipeline failed to change state to PLAYING,
check logs above this.")`

1.3 WHEN that exception reaches `pipeline_execution_exception_handler` THEN
the system returns the generic "pipeline processing error … Check the
pipeline and retry again" message, which names neither the model nor its
loading state, steering the operator toward a phantom pipeline bug

1.4 WHEN the first (failed) run has triggered or collided with the model
load THEN the system completes the load in the background, moves the model to
READY, and every subsequent run succeeds — making the first-run failure look
like a flaky pipeline rather than a deterministic cold-model condition

**Defect 2 — the failure consumes the input**

1.5 WHEN a folder-source workflow run fails for ANY reason — including the
transient cold-model condition — THEN the system's catch-all in
`execute_workflow_pipeline` moves the source image to
`{INFERENCE_RESULTS_DIR}/{workflowId}/failed/` and appends "Source image file
has been moved to {path}" to the error

1.6 WHEN the model becomes READY moments later THEN the system cannot
reprocess the consumed input — the valid source image sits in `failed/` as if
it were corrupt, and the operator must manually restore it

**Defect 3 — the deployment-time preload hook fails silently**

1.7 WHEN the model component's Greengrass Startup runs `model_convertor.py`
and `start_model` returns `False` (model never confirmed READY) THEN the
system ignores the return value, exits 0, and the component reports healthy
while the model remains `UNKNOWN`

1.8 WHEN the LocalServer backend is unreachable during the deployment window
(container restarting) THEN the system's `wait_for_server` gives up after
~93 s, no load is ever enqueued, and the failure is logged only inside the
model component's log

1.9 WHEN an ONNX model's first load must build a TensorRT engine (minutes
for a ~300 MB model on Thor) THEN the system's `start_model` readiness window
(3 attempts × 120 s, sized for DLR's tens of seconds) expires and gives up
even though the load may still be progressing

1.10 WHEN the LocalServer backend restarts THEN the system loses all
in-process Triton load state, previously READY models revert to `UNKNOWN`,
and nothing reloads them at boot — recreating the cold-model window on every
backend restart, not just on fresh deployment

**Defect 4 — the model state lifecycle is invisible and unexplained**

1.11 WHEN the portal reads model state through `/feature-configurations` THEN
the system passes through the raw Triton token (`UNKNOWN`) with no lifecycle
semantics — the operator cannot distinguish "never loaded", "converting/
loading right now", and "broken"

1.12 WHEN a workflow run fails while the referenced model is not READY THEN
the system records no signal linking the failure to the model's state at
failure time, in either the error payload or the run artifacts

**Defect 4 — the same failure class on the deployed-workflow engine path
(second hardware sighting, 2026-09-22, `jetson-thor1`, LocalServer `arm64JP7`
1.0.43)**

Recorded while verifying task 11 of
`.kiro/specs/rfdetr-training-and-transfer-learning/`: of twelve triggered
executions of one workflow, **eleven succeeded and exactly one failed — the
first one after a backend container restart**. Device log for the failing run,
all inside one second:

```
17:50:36 Resolved workflow model 'blue-plate-rfdetr-small' to deployed Triton model 'model-blue-plate-rfdetr-small-jetson-xavier-jp7'
17:50:36 Scanned workflow plugin directory /usr/lib/panoramagst (registry changed: True)
17:50:36 Setting pipeline to PLAYING state
17:50:36 [emltriton.cpp:196] [CHECKIF status != "READY"]: Model is not ready for inference, wait till it is in READY state ...
17:50:36 [emltriton.cpp:31]  [CHECKHR CheckModelLoaded() (Unknown error)]
17:50:36 [emltriton.cpp:435] Failed to initialize underlying triton server
17:50:36 Pipeline failed to change state to PLAYING, check logs above this.
```

A later, successful run logged the contrast: `[triton_server.cpp:137] Model
... is already loaded` then `[triton_server.cpp:389] Model ... status is READY`.

1.13 WHEN the workflow engine resolves a `model_inference` node's model name
THEN the system consults **only the filesystem** —
`_loaded_ensemble_models()` lists `_TRITON_MODEL_REPO`
(`workflow_engine/pipeline_executor.py:124-146`, resolution at `:219-244`,
logged at `:1391-1396`) — and its own docstring states this is deliberate, so
"resolution never stalls on server state". A directory exists from the moment
the model component staged its artifacts, so **the "Resolved workflow model"
log line carries no readiness information whatsoever**

1.14 WHEN the engine starts the pipeline THEN the system performs no model
readiness check of any kind: `_preflight` checks only that the registration
exists and is registered (`pipeline_executor.py:3441-3453`), and
`_preflight_pipeline_factories` checks only GStreamer element-factory presence
(`:2612-2660`); `run_pipeline` is then called directly (`:1866`, `:1874`)

1.15 WHEN the pipeline transitions `NULL → READY` THEN the system's only
readiness check is native and synchronous with **zero wait**: `emltriton`'s
`Initialize()` calls `_server->LoadModel()` — which merely *enqueues*
(`edgemlsdk .../triton_server.cpp:128-184`) — and then `CheckModelLoaded()` on
the very next line (`emltriton.cpp:29-30`, check at `:193-198`), so a model
whose asynchronous load is still in flight fails instantly and
`change_state` returns `GST_STATE_CHANGE_FAILURE` (`emltriton.cpp:424-441`)

1.16 WHEN the backend container restarts THEN the system loses all in-process
Triton load state — the state map lives only in the backend process — while the
model components' `model_convertor.py` loads are asynchronous and in flight, so
**every backend restart re-opens the cold window** for every model. This is the
engine-path instance of Defect 1.10

1.17 WHEN that run fails THEN the system records it once and never retries:
`_finish_failed` sets `status='failed'` with the error and the failing node
(`pipeline_executor.py:3483-3496`) and nothing re-dispatches, so a transient
not-ready condition is indistinguishable from a real pipeline defect in the run
history

1.18 WHEN an operator reads the failure THEN the system's message is
`"Pipeline failed to change state to PLAYING, check logs above this."` — naming
neither the model nor its state — because `emltriton` failed the state change
without posting a bus ERROR for `gst_pipeline.py`'s drain to enrich
(`gstreamer/gst_pipeline.py:210-243`). This is Defect 1.3's message on a
different path

**Correlating signal, not a cause.** The failing run logged `registry changed:
True` and successful runs `False`. That flag comes from
`workflow_engine/gst_plugins.py:_scan_registry` (`:278-307`) and means only
that this process had not yet scanned the workflow plugin directory — i.e. it is
a reliable fingerprint for "first execution of this backend process" and is
unrelated to Triton loading.

### Expected Behavior (Correct)

**Fix 1 — a run against a cold model has a defined, honest outcome**

The design decides the mechanism; the requirements capture the acceptable
outcome space. Exactly one of 2.1(a)/2.1(b) fires per run, deterministically.

2.1 WHEN a workflow run references a model that is not READY and the model is
loadable (converted or converting, not terminally FAILED) THEN the system
SHALL either (a) wait — bounded, with a deadline sized for the model's
runtime class (ONNX first loads take minutes) — for the model to reach READY
and then proceed with the run exactly as a warm run, or (b) fail fast with an
explicit, actionable error that names the model and its state (e.g. "model
{name} is still converting/loading (state {state}); retry shortly") — and the
chosen behavior SHALL be deterministic and documented

2.2 WHEN the system fails fast under 2.1(b) for a folder-source workflow THEN
it SHALL NOT move, delete, or otherwise consume the source image — the input
SHALL remain byte-identical in place so the retry processes it

2.3 WHEN the system fails fast under 2.1(b) THEN the returned error SHALL be
distinguishable from a genuine pipeline failure (distinct message naming the
model and state, not the generic "Check the pipeline and retry again" text)

2.4 WHEN a workflow run fails for a genuine pipeline reason while the model
IS READY THEN the system SHALL report it through the existing pipeline-error
path unchanged (see 3.2)

**Fix 2 — the deployment-time preload is honest and sized correctly**

2.5 WHEN the model component Startup's `start_model` cannot confirm the model
READY THEN the system SHALL surface that outcome instead of silently exiting
0 — at minimum the component's startup outcome SHALL reflect it in a way an
operator can find without reading raw logs (the design decides between
failing the Startup, degraded component status, or an explicit device-side
state record)

2.6 WHEN a model's first load legitimately takes minutes (ONNX TensorRT
engine build) THEN the system SHALL NOT misclassify the in-progress load as a
failure — the readiness window SHALL accommodate the runtime class, or the
system SHALL report the model in an explicit in-progress state that the run
path (2.1) and the portal (2.8) handle

2.7 WHEN the LocalServer backend restarts THEN the system SHALL either
automatically restore previously READY models to READY (reload at boot) or
report them in an explicit non-READY state that the run path handles per 2.1
— a backend restart SHALL NOT silently recreate the misleading first-run
failure

**Fix 3 — the lifecycle is visible to the operator**

2.8 WHEN a model is anywhere in its load lifecycle THEN the system SHALL
expose a state the portal can render meaningfully — at minimum
distinguishing not-loaded, loading/converting-in-progress, READY, and FAILED
— through the existing device model-status mechanism (`/feature-
configurations` and its shadow sync)

2.9 WHEN a workflow run fails while the referenced model is not READY THEN
the system SHALL record the model's state at failure time in the error
surfaced to the caller (satisfied by 2.1(b)'s message when failing fast, and
by the wait-timeout error when 2.1(a)'s bounded wait expires)

**Fix 4 — on-device verification is part of done**

2.10 WHEN the fix is implemented THEN it SHALL be verified on real hardware
per `.kiro/steering/builds.md` before being called done: build the LocalServer
component for every JetPack the change touches (strictly one build at a time,
~1–2 h each, security-preservation gate pre-checked), deploy to a matching
device, deploy a fresh model component, and confirm (a) the cold-model
first-run outcome matches 2.1, (b) the source image survives per 2.2, (c) the
portal state lifecycle per 2.8, and (d) warm runs are unchanged per 3.1 — on
JP7 (`jetson-thor1`, ONNX) and on at least one of JP5/JP6 (DLR) if the shared
run path is touched

2.11 WHEN the deployed-workflow engine is about to start a pipeline containing
an `emltriton` element THEN the system SHALL consult the referenced model's
state before the pipeline is started, and SHALL either wait a bounded time for
`READY` or fail the execution with an error naming the model and its state —
the same choice 2.1 offers the classic path, applied to the engine path
(Defect 4, promoting Requirement 3.11)

2.12 WHEN the engine gates on model state THEN the system SHALL read it through
the in-process API rather than inferring it from the filesystem:
`TritonEdgeClient.get_instance().get_model_status(resolved_name)`
(`dda_triton/triton_edge_client.py:142`) returns one of `UNKNOWN`, `LOADING`,
`READY`, `UNLOADING`, `UNAVAILABLE`. The design SHALL account for three
properties of that vocabulary: `UNKNOWN` means "this process has never been
asked to load it" and so never converges without a load being kicked;
`UNAVAILABLE` carries a `reason` and is terminal (added by commit `7812407`,
`triton_server.cpp:186-206`); and the existing `start_model` route **403s**
unless the state is `UNKNOWN` or `UNAVAILABLE`, so a `LOADING` model must not
be re-started (`utils/feature_configs_utils.py:279-300`)

2.13 WHEN the engine waits for a model THEN the wait SHALL be bounded and the
execution row SHALL remain `running` for its duration. Two in-repo precedents
SHALL be followed rather than inventing a third: the LLM output binding's
poll on `409 {"state": "loading"}` (`workflow_engine/output_bindings.py:2646-2660`,
5 s interval, 240 s budget) and `model_convertor.py`'s
`_wait_for_model_ready` (`:86-107`, 3 s interval, `START_MODEL_READY_TIMEOUT_S`
budget, short-circuiting on `UNAVAILABLE`/`FAILED`). Retrying the whole
execution has **no** precedent and SHALL NOT be introduced by this spec

2.14 WHEN the LocalServer backend starts THEN the design SHALL consider
shrinking the cold window itself rather than only tolerating it, by mirroring
the implemented pattern in
`.kiro/specs/vllm-model-reload-after-backend-restart/`: a boot-time reconciler
(`vllm_runtime/reconciler.py`, started from `app.py:341-342`) that re-drives
each staged model's load **through the same endpoint the component Startup
uses**, with bounded per-model retries, per-model failure isolation, an
unload tombstone so explicit unloads stay honored, and truthful statuses
instead of an eternal `LOADING`. The Triton analogue re-drives the `model-*`
directories found in `/aws_dda/dda_triton/triton_model_repo`. A gate (2.11) and
a reconciler (2.14) address different halves — the gate makes the cold window
honest, the reconciler makes it short — and the design SHALL state which it
delivers, and why, rather than silently choosing one

2.15 WHEN the empty-repo case is reached THEN any new readiness call SHALL
reuse the existing `feature_configs_utils.triton_repo_has_models()` guard,
because `TritonEdgeClient.get_instance()` creates the native server if absent
and standing Triton up against an empty repository has a documented hang
(consistent with Requirement 3.12)

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a workflow run references a model that IS READY (warm model) THEN
the system SHALL CONTINUE TO execute the pipeline, produce inference results,
and return the response byte-identically to today — for folder, camera,
ICAM, and NVIDIA CSI image sources alike

3.2 WHEN a workflow run fails for a genuine pipeline reason unrelated to
model load state (bad camera, invalid pipeline graph, GStreamer element
error, Triton inference error on a READY model) THEN the system SHALL
CONTINUE TO raise the existing `PipelineExecutionException` path with its
existing message enrichment (bus-drain detail), and for folder sources SHALL
CONTINUE TO move the source image to `failed/` exactly as today

3.3 WHEN a folder-source image is zero bytes THEN the system SHALL CONTINUE
TO move it to `failed/` and raise the existing 422 "source image file
corruption" error before any pipeline is built

3.4 WHEN a successful run completes THEN the system SHALL CONTINUE TO produce
the identical inference-results layout (capture id naming, output files,
`failed/` untouched, folder-source cleanup of the processed image) and the
identical response schema, including `returnPartialResultsEarly` semantics

3.5 WHEN a workflow has no `featureConfigurations` (capture-only) THEN the
system SHALL CONTINUE TO run the capture-task path unchanged — no model-state
check applies

3.6 WHEN DIO-triggered runs (`digital_input_thread_manager` /
`digital_input_process_manager`) execute against a READY model THEN the
system SHALL CONTINUE TO behave exactly as today

3.7 WHEN `model_convertor.py` converts a model THEN the system SHALL CONTINUE
TO produce the identical Triton repository layout (base/marshal/ensemble,
atomic staging publish, config.pbtxt written last) — conversion output is not
touched by this fix

3.8 WHEN existing callers use `/feature-configurations/models/{m}/start` and
`/stop` THEN the system SHALL CONTINUE TO honor their current contracts —
in particular `start_model`'s reliance on 403 meaning "a load is already in
flight" (start allowed only from UNKNOWN/UNAVAILABLE, stop only from READY)
— for any state vocabulary change, every device-side caller SHALL be updated
in lockstep within this fix

3.9 WHEN DLR models on JP5/JP6 load within today's windows THEN the system
SHALL CONTINUE TO reach READY at deployment time exactly as today — sizing
changes for ONNX SHALL NOT slow down or destabilize the DLR path

3.10 WHEN vLLM models are staged/loaded (`vllm_model_prep.py`, vLLM runtime
manager, `VllmModel` feature-config entries) THEN the system SHALL CONTINUE
TO behave identically — the vLLM path is out of scope

3.11 WHEN the deployed-workflow engine runs a pipeline THEN its existing
behavior SHALL CONTINUE unchanged **except** for the model-state gate this
spec now requires on that path as well — see Defect 4 and Requirements 2.11
to 2.14 below. This criterion originally deferred the engine path ("MAY be
covered, but never regressed"); the 2026-09-22 hardware evidence promoted it
to in-scope, because the engine path is where the failure was next observed
and it is now the primary deployed run path

3.12 WHEN the LocalServer boots with an empty Triton model repository THEN
the system SHALL CONTINUE TO skip Triton server creation on
`/feature-configurations` (the empty-repo hang guard in
`feature_configs_utils.triton_repo_has_models`)

### Bug Conditions and Properties

**Key definitions.** `F` is the current (unfixed) code; `F'` is the fixed
code. `run(X)` is one invocation of the workflow-run path for request `X`.
`modelState(m)` is the device-reported Triton state for model `m`.
`loadable(m)` is true when `m` is converted (Triton repo entries exist) and
not terminally FAILED. `sourceImage(X)` is the folder-source input file
selected for the run, when the workflow uses a folder source.

#### Bug condition — a run against a cold, loadable model

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type WorkflowRunRequest
  OUTPUT: boolean

  RETURN X.workflow.featureConfigurations ≠ ∅
     AND modelState(modelOf(X)) ≠ READY
     AND loadable(modelOf(X))
END FUNCTION
```

```pascal
// Property 1: Fix Checking - cold-model runs have a defined, honest,
// non-destructive outcome
FOR ALL X WHERE isBugCondition(X) DO
  result ← run'(X)
  ASSERT (result IS Success AND modelState'(modelOf(X)) = READY)   // 2.1(a)
      OR (result IS ColdModelError
          AND result.message NAMES modelOf(X)
          AND result.message NAMES modelState(modelOf(X))
          AND result.message ≠ genericPipelineError)               // 2.1(b), 2.3
  ASSERT usesFolderSource(X) AND result IS ColdModelError
     IMPLIES sourceImage(X) unchanged AND NOT movedToFailed(sourceImage(X))  // 2.2
END FOR
```

#### Preservation — warm runs and genuine failures are untouched

```pascal
// Property 2: Preservation Checking - for all non-cold-model inputs the
// fixed system behaves identically to the original
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT run(X) = run'(X)          // response, artifacts, error path
  ASSERT genuinePipelineFailure(X)
     IMPLIES errorPath'(X) = errorPath(X)               // 3.2, incl. failed/ move
  ASSERT successfulRun(X)
     IMPLIES resultsLayout'(X) = resultsLayout(X)       // 3.4
END FOR
```

#### Engine-path condition — the first run of a backend process

The 2026-09-22 sighting gives the bug condition a sharper, reproducible form on
the engine path: it is not "a freshly deployed model" but **"the first
execution after the backend process started"**, for any model whose load is
still in flight.

```pascal
FUNCTION isEngineBugCondition(E)
  INPUT: E of type WorkflowExecution
  OUTPUT: boolean

  RETURN hasTritonElement(documentOf(E))
     AND modelState(modelOf(E)) ≠ READY
     AND loadable(modelOf(E))
END FUNCTION
```

```pascal
// Property 3: Fix Checking - engine executions against a not-yet-ready model
// have a defined, honest outcome, and no run is silently lost
FOR ALL E WHERE isEngineBugCondition(E) DO
  result ← execute'(E)
  ASSERT (result IS Success AND modelState'(modelOf(E)) = READY)      // 2.11(a)
      OR (result IS ColdModelError
          AND result.error NAMES modelOf(E)
          AND result.error NAMES modelState(modelOf(E))
          AND result.error ≠ "Pipeline failed to change state to PLAYING")  // 2.11(b), 1.18
  ASSERT waitDuration(result) ≤ configuredBudget                       // 2.13
  ASSERT executionRow'(E).status ∈ {running, succeeded, failed}        // 2.13
END FOR
```

```pascal
// Property 4: Preservation - warm engine runs are byte-identical
FOR ALL E WHERE NOT isEngineBugCondition(E) DO
  ASSERT runPipelineCalls'(E) = runPipelineCalls(E)   // same launch string, same args
  ASSERT executionOutcome'(E) = executionOutcome(E)
  ASSERT artifactLayout'(E) = artifactLayout(E)
END FOR
```

`runPipelineCalls` is observable through the existing `FakePipelineManager`
doubles injected via `_pipeline_manager_factory`
(`test/backend-test/output_bindings_fixes/executor_harness.py:89-101`,
`test/backend-test/workflow_engine/test_workflow_aravis_executor.py:121-131`),
which also make "the gate ran before `run_pipeline`" and "`run_pipeline` was
never called" directly assertable. No existing test fakes Triton readiness on
the workflow path — `get_model_status` appears in no executor test — so a new
fake is required; the closest models to copy are the vLLM reload fakes at
`test/backend-test/vllm_model_reload/fakes.py`.

**Note on exploration order (bugfix methodology).** The exploration test for
Property 1 MUST be written and run against UNFIXED code first — it is
expected to FAIL, confirming the bug and pinning the exact failure point
(the `emltriton` cold-start interaction was hypothesis-flagged above and is now
confirmed: `Initialize()` enqueues the load and checks readiness on the next
line, `emltriton.cpp:29-30`). Property 3's exploration test likewise MUST fail
against unfixed code. Preservation tests for Properties 2 and 4 MUST be written
observation-first against UNFIXED code and PASS before any fix lands. Final
validation of all four properties is on real hardware per 2.10 — and for the
engine path the reproduction is cheap and deterministic: restart the backend
container, trigger the workflow once, observe the failure; today that is
`1 of 12` runs, and after the fix it must be `0 of 12`.

---

## Revision history

- **2026-08-14** — original report, classic workflow-run path
  (`POST /workflows/{id}/run`), `jetson-thor1`, three models.
- **2026-09-22** — second hardware sighting on the **deployed-workflow engine**
  path during task 11 of `.kiro/specs/rfdetr-training-and-transfer-learning/`
  (1 of 12 executions failed, the first after a backend container restart).
  Added Defect 4 (1.13-1.18), Requirements 2.11-2.15, Properties 3 and 4, and
  promoted Requirement 3.11 from "MAY be covered" to in-scope. The root cause
  is now confirmed rather than inferred. Full record in
  `docs/detection-training-gap.md` §"On-device verification (2026-09-22)".
  Still requirements-only: no design.md and no tasks.md exist, so **nothing in
  this spec is implemented**.
