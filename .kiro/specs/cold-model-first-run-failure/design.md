# Cold Model First Run Failure — Bugfix Design

## Overview

A workflow run against a model that is converted but not yet `READY` fails with
a generic GStreamer error that names neither the model nor its state, and — on
the classic path — destroys the input image by moving it to `failed/`. Confirmed
on two independent paths and two dates:

| Date | Path | Evidence |
|---|---|---|
| 2026-08-14 | classic `POST /workflows/{id}/run` | 3 models on `jetson-thor1`, all first-run failures, all fine afterwards |
| 2026-09-22 | deployed-workflow engine | 1 of 12 executions failed — the first after a backend container restart (`emltriton.cpp:196` / `:31` / `:435`) |

The root cause is now confirmed rather than inferred, and it is one line of
C++: `emltriton`'s `Initialize()` calls `_server->LoadModel()`, which **only
enqueues** (`triton_server.cpp:128-184`), then calls `CheckModelLoaded()` on the
very next line (`emltriton.cpp:29-30`). There is no wait anywhere on either
path. `GetModelStatus` returns `LOADING` (or `UNKNOWN`), the check fails, and
`change_state` returns `GST_STATE_CHANGE_FAILURE` (`emltriton.cpp:424-441`).

Because Triton load state lives **only in the backend process**, every backend
restart re-opens the window for every model. That makes the engine-path bug
condition sharp and cheap to reproduce: *the first execution after the backend
process starts*.

## Glossary

- **Cold model** — converted (a `model-*` directory exists in
  `/aws_dda/dda_triton/triton_model_repo`) but `GetModelStatus` ≠ `READY`.
- **Classic path** — `endpoints/workflow.py::run_inference_for_stream` →
  `gstreamer/pipeline_builder` → `gst_pipeline_executor.execute_workflow_pipeline`.
- **Engine path** — `workflow_engine/pipeline_executor.py::execute` for a
  deployed Workflow_Component.
- **Gate** — a readiness check before the pipeline is started, which either waits
  a bounded time for `READY` or fails with a named reason.
- **Reconciler** — a boot-time component that re-drives staged model loads so the
  cold window is short, mirroring
  `.kiro/specs/vllm-model-reload-after-backend-restart/`.

## Bug Details

### Bug condition

`isBugCondition(X)` (classic) and `isEngineBugCondition(E)` (engine) in
bugfix.md. Both reduce to: the document contains an `emltriton` element, the
model is loadable, and its state is not `READY`.

### Why "Resolved workflow model ..." is not reassurance

The engine resolves the model name from a **filesystem listing**
(`pipeline_executor.py:124-146`, `:219-244`), and its docstring says so
deliberately, "so model-name resolution can never stall on server state". A
directory exists from the moment the component staged its artifacts. The log
line that reads like a successful lookup therefore carries zero readiness
information — which is exactly why the failure looks inexplicable in the logs.

### The complete pre-flight set today

Engine path: `_preflight` checks the registration exists and is registered
(`pipeline_executor.py:3441-3453`); `_preflight_pipeline_factories` checks
GStreamer element-factory presence (`:2612-2660`). Classic path:
`validate_workflow_requirements` checks image sources exist. **Neither reads
model state.** No poll, no retry, no backoff on either path.

## Expected Behavior

bugfix.md 2.1-2.10 (classic) and 2.11-2.15 (engine). The union, in one line:
a cold-model run either succeeds after a bounded wait or fails with an error
naming the model and its state, and never consumes its input.

### Preservation Requirements

bugfix.md 3.1-3.12. The load-bearing ones for this design:

- **3.2** a genuine pipeline failure keeps its existing error path, `failed/`
  move included.
- **3.7** `model_convertor.py`'s Triton repository layout and atomic publish are
  untouched.
- **3.10** the vLLM path is untouched.
- **3.12** the empty-repo Triton-creation guard stays.

## Hypothesized Root Cause

No longer hypothesized. Confirmed:

1. `emltriton.cpp:29-30` — enqueue then immediately check.
2. `triton_server.cpp:128-184` — `LoadModel` writes `state: "LOADING"` and
   returns; a worker thread does the real load (`:212-267`).
3. `triton_server.cpp:378-391` — `GetModelStatus` returns the stored state, or
   `"UNKNOWN"` when this process has never been asked to load the model.
4. Load state is per-process, so a restart resets it while the model components'
   `model_convertor.py` loads are asynchronous and in flight.

The 2026-08-14 report's hypothesis flag on "the exact interaction between
`emltriton`'s startup deadline and the in-progress load" is resolved: there is no
deadline. There is no wait at all.

## Design Decisions

### Decision 1: Deliver the gate; do NOT deliver a reconciler in this spec

bugfix.md 2.14 requires this choice to be stated with reasons.

**Chosen: the gate (2.11-2.13) on both paths, plus the input-preservation fix
(2.2). The Triton boot-time reconciler is explicitly deferred.**

Reasoning:

- The gate is **sufficient for correctness**. A bounded wait for `READY` absorbs
  the cold window entirely from the operator's point of view: the run succeeds,
  just later. The reconciler only makes the window *shorter* — it is a latency
  optimisation once the gate exists, not a correctness fix.
- The gate is where all three harms in bugfix.md's Introduction live (a lying
  error, a consumed input, an unexplained cold state). A reconciler fixes none of
  them on its own: a run that arrives during the reconciler's own window still
  fails exactly as today.
- The reconciler is a new background component with its own failure modes —
  ordering, per-model isolation, unload tombstones so an explicit unload is not
  resurrected, retry budgets. The vLLM spec needed all of that
  (`vllm_runtime/reconciler.py`), and reproducing it for Triton is a feature-sized
  change that would dominate a bugfix.
- Scoping it out keeps this spec's device blast radius to two readable
  guards, which matters because every iteration costs a ~1-2 h component build
  plus a hardware pass.

Recorded as the follow-up: "Triton boot-time model reconciler", mirroring
`vllm-model-reload-after-backend-restart`, re-driving the `model-*` directories
in the repo **through the same endpoint the component Startup uses** rather than
the in-process API. Worth doing if the gate's waits prove long in practice; the
gate's own logging (Decision 5) is what will tell us.

### Decision 2: Read state through `TritonEdgeClient.get_model_status`, and handle the vocabulary honestly

The workflow engine runs in the same process as the Triton singleton
(`app.py:406` creates it, `:419` starts the engine), so the gate is an in-process
call: `TritonEdgeClient.get_instance().get_model_status(resolved_name)`
(`dda_triton/triton_edge_client.py:142`).

The vocabulary has three traps, all of which the gate must respect (2.12):

| State | Meaning | Gate behaviour |
|---|---|---|
| `READY` | loaded | proceed immediately — the warm path, zero added latency |
| `LOADING` | a load is in flight | wait, do **not** re-request the load |
| `UNKNOWN` | this process has never been asked to load it | kick one load, then wait — a pure wait never converges |
| `UNAVAILABLE` | terminal failure, carries `reason` | fail fast, surface the reason |
| `UNLOADING` | being unloaded | fail fast; waiting for a model on its way out is wrong |

`UNKNOWN` is the state a restarted backend sees, so the kick is the part that
makes the gate work after a restart rather than merely reporting the failure
politely. The kick must go through the same path the component Startup uses
(`feature_configs_utils.start_model_triton`, which 403s unless the state is
`UNKNOWN`/`UNAVAILABLE` — hence never kicking a `LOADING` model).

### Decision 3: Reuse the existing empty-repo guard

`TritonEdgeClient.get_instance()` **creates** the native server when absent, and
standing Triton up against an empty repository has a documented hang. The gate
therefore calls `feature_configs_utils.triton_repo_has_models()` first and
no-ops when the repo is empty — the same guard `/feature-configurations` uses
(2.15, consistent with 3.12). A document with no `emltriton` element skips the
gate entirely, so non-Triton workflows pay nothing.

### Decision 4: Bounded wait, sized per runtime class, following existing precedent

No new waiting machinery (2.13). Two in-repo precedents, both reused rather than
re-invented:

- the LLM output binding's loading poll — 5 s interval, 240 s budget
  (`workflow_engine/output_bindings.py:2646-2660`);
- `model_convertor.py::_wait_for_model_ready` — 3 s interval,
  `START_MODEL_READY_TIMEOUT_S` budget, short-circuiting on
  `UNAVAILABLE`/`FAILED` (`:86-107`).

The gate takes the second's shape (it is already about Triton) with a budget
sized for the ONNX-on-Thor case that motivated the original report: a first ONNX
load can build a TensorRT engine for a ~300 MB model and take minutes, so a
DLR-sized budget would time out on a load that is progressing normally. The
budget is a module constant, documented against that case, not a magic number
inline.

The execution row stays `running` for the wait's duration (2.13); it is already
set to `running` before the pipeline starts (`pipeline_executor.py:1767-1769`),
so this requires no change — it requires *not* introducing a status change.

### Decision 5: The error names the model and its state, and the wait is logged

The generic message is half the reported harm (1.3, 1.18). On timeout or a
terminal state the gate fails the execution with a message naming the model, the
resolved Triton model name, the observed state, the `reason` when Triton supplied
one, and the elapsed wait — so the run history distinguishes "the model was
loading" from "the pipeline is broken". Every wait longer than one poll interval
logs at INFO with the elapsed time, which is also the data that decides whether
Decision 1's deferred reconciler is ever needed.

### Decision 6: Gate both paths from one helper

The two paths have different pre-flight structures but need identical semantics,
and bugfix.md 3.11 now puts both in scope. One helper — state read, kick,
bounded wait, structured outcome — called from the engine's `execute()` after
`_resolve_model_names` (`pipeline_executor.py:1669`) and from the classic path's
pre-flight before the pipeline is built. Duplicating the logic would let the two
paths drift, which is how the engine path ended up uncovered in the first place.

### Decision 7: Do not consume the input on a cold-model failure

Requirement 2.2 / defect 1.5-1.6. The classic path's catch-all moves the
folder-source image to `failed/` for **any** pipeline exception. The gate runs
*before* the pipeline, so a cold-model failure never reaches that handler and the
input is untouched — the fix falls out of the ordering rather than needing a
special case. The engine path's equivalent (`_relocate_failed_folder_frames`,
`pipeline_executor.py:1889`) is likewise not reached. A genuine pipeline failure
still takes the existing path, `failed/` move included (3.2).

## Correctness Properties

bugfix.md Properties 1-4. Mapping:

| Property | Statement | Artifact |
|---|---|---|
| 1 | classic cold-model runs: honest outcome, input preserved | exploration + fix-check on the classic path |
| 2 | classic warm runs and genuine failures byte-identical | preservation suite, observation-first |
| 3 | engine cold-model executions: honest outcome, bounded wait, row never stuck | exploration + fix-check on the engine path |
| 4 | engine warm runs byte-identical, same `run_pipeline` call | `FakePipelineManager` doubles |

Property 4's observability already exists: the `FakePipelineManager` injected via
`_pipeline_manager_factory`
(`test/backend-test/output_bindings_fixes/executor_harness.py:89-101`,
`test/backend-test/workflow_engine/test_workflow_aravis_executor.py:121-131`)
records every `run_pipeline` call, which makes both "the gate ran before
`run_pipeline`" and "`run_pipeline` was never called" directly assertable.

No test anywhere fakes Triton readiness on the workflow path — `get_model_status`
appears in no executor test — so a new fake is required. The closest models to
copy are `test/backend-test/vllm_model_reload/fakes.py`.

## Fix Implementation

### Changes Required

1. **New helper** (module TBD in tasks — `dda_triton/model_readiness.py` is the
   natural home, beside the client it wraps): `ensure_model_ready(model_name)` →
   structured outcome (`ready` / `failed(reason)`), implementing Decisions 2, 3,
   4 and 5. Pure orchestration over `TritonEdgeClient` and
   `feature_configs_utils`; no new dependency.
2. **`src/backend/workflow_engine/pipeline_executor.py`** — call the helper after
   `_resolve_model_names` (`:1669`) for each distinct resolved `emltriton` model,
   and on a non-ready outcome `_finish_failed` with the helper's message before
   the pipeline is built. Execution row stays `running` during the wait.
3. **`src/backend/endpoints/workflow.py`** (classic path) — call the same helper
   in the pre-flight, before the pipeline is built, returning an error that names
   the model and state rather than the generic pipeline error.

`emltriton.cpp` and `triton_server.cpp` are **not** modified: the native check is
correct, it is just unguarded by anything that waits. Keeping the SDK out of
scope also keeps this a LocalServer-only build.

### Not changed

`model_convertor.py`'s repository layout and atomic publish (3.7), the vLLM path
(3.10), the `dda-model-status` shadow and its reporter, Neo/packaging/publish,
and the Portal.

## Testing Strategy

Bugfix order: exploration (fails on `F`) → preservation (passes on `F`) → fix →
both pass → hardware.

- **Exploration**, engine path: with a fake whose `get_model_status` returns
  `LOADING`, an execution fails today with "Pipeline failed to change state to
  PLAYING" and `run_pipeline` IS called. Must fail on `F'` expectations.
- **Exploration**, classic path: the same, plus the folder-source image is moved
  to `failed/`.
- **Preservation**: warm executions (`READY`) produce an identical
  `run_pipeline` call and identical artifacts; a genuine pipeline failure keeps
  its message and its `failed/` move.
- **Helper units**: each state in Decision 2's table, the kick-once-then-wait
  behaviour for `UNKNOWN`, the timeout message content, the empty-repo no-op, and
  that a `LOADING` model is never re-kicked.
- **Untouched and must stay green**: `test/backend-test/vllm_model_reload/**`
  (3.10), `test/backend-test/utils/test_feature_configs_utils.py` (the `7812407`
  UNAVAILABLE/`reason` test), and the capture-routing suites that patch
  `_TRITON_MODEL_REPO`.

## Hardware Verification

Required by 2.10 and `.kiro/steering/builds.md`. The engine-path reproduction is
deterministic and cheap, which the original report's was not: restart the
LocalServer backend container, trigger a Triton-backed workflow once, observe.
Today that fails (measured 1 of 12 on 2026-09-22); after the fix it must be 0 of
N, with the first run either succeeding after a logged wait or failing with a
message naming the model and its state. The classic path is verified the same way
through `POST /workflows/{id}/run`, additionally confirming the folder-source
image is still in place afterwards. JP7 `jetson-thor1` is where both sightings
happened; any other arch built needs its own pass.
