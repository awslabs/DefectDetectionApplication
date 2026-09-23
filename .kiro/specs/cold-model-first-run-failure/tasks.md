# Implementation Plan

## Overview

Stop a workflow run against a converted-but-not-yet-`READY` model from failing
with a generic GStreamer error that names neither the model nor its state, and
from destroying its input. Confirmed twice on `jetson-thor1`: 2026-08-14 on the
classic `POST /workflows/{id}/run` path (3 models), and 2026-09-22 on the
deployed-workflow engine path (1 of 12 executions — the first after a backend
container restart).

Root cause, confirmed: `emltriton`'s `Initialize()` calls `LoadModel`, which only
**enqueues** (`triton_server.cpp:128-184`), then `CheckModelLoaded()` on the very
next line (`emltriton.cpp:29-30`). No wait exists anywhere. Nothing on either
Python path reads model state — the engine resolves model names from a
**filesystem listing** (`pipeline_executor.py:124-146`), so the reassuring
"Resolved workflow model ..." log line carries no readiness information. Load
state is per-process, so every backend restart re-opens the window.

**Scope, per design.md Decision 1: deliver the GATE, not a reconciler.** A
bounded wait for `READY` before the pipeline starts absorbs the cold window, and
because the gate runs before the pipeline the input is never moved to `failed/`
(Decision 7 — the fix falls out of the ordering). A Triton boot-time reconciler
mirroring `.kiro/specs/vllm-model-reload-after-backend-restart/` is explicitly
deferred to a follow-up: it only shortens the window, fixes none of the three
reported harms on its own, and is a feature-sized change. The gate's own wait
logging (Decision 5) is the data that decides whether it is ever needed.

**Hard constraints.** `emltriton.cpp` / `triton_server.cpp` are NOT modified —
the native check is correct, merely unguarded. `model_convertor.py`'s repository
layout and atomic publish are untouched (3.7). The vLLM path is untouched (3.10).
The empty-repo Triton-creation guard is reused, not bypassed (2.15, 3.12): the
gate calls `feature_configs_utils.triton_repo_has_models()` first, because
`TritonEdgeClient.get_instance()` creates the native server when absent and
standing it up against an empty repo has a documented hang. A `LOADING` model is
never re-kicked (`start_model_triton` 403s unless `UNKNOWN`/`UNAVAILABLE`).

**On-device rule.** `src/backend/` code. Not done until built and verified on
real hardware (task 6). The engine-path reproduction is deterministic: restart the
backend container, trigger once.

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "description": "Readiness fake, exploration on both paths, preservation of warm and genuinely-failing runs.", "tasks": ["1.1", "1.2", "1.3", "1.4"] },
    { "wave": 2, "description": "The shared readiness helper and its units.", "tasks": ["2.1", "2.2"] },
    { "wave": 3, "description": "Wire the gate into both run paths.", "tasks": ["3.1", "3.2"] },
    { "wave": 4, "description": "Gates: device suites, vLLM and feature-config suites untouched, preservation.", "tasks": ["4.1", "4.2"] },
    { "wave": 5, "description": "USER ACTION: build the JP7 LocalServer component.", "tasks": ["5"] },
    { "wave": 6, "description": "USER ACTION: hardware verification of both paths on jetson-thor1.", "tasks": ["6"] }
  ]
}
```

## Tasks

- [ ] 1. Confirm the bug on both paths, pin today's behaviour
  - [ ] 1.1 Triton readiness fake — the missing test primitive. No test anywhere
    fakes Triton readiness on a workflow path (`get_model_status` appears in no
    executor test), so build one under `test/backend-test/dda_triton/` following
    the shape of `test/backend-test/vllm_model_reload/fakes.py`: a substitutable
    `TritonEdgeClient` whose `get_model_status` returns a scripted sequence,
    recording every `start_triton_model` call so "kicked once, not re-kicked" is
    assertable. Record the interpreter and command that run
    `test/backend-test/workflow_engine` and `test/backend-test/dda_triton` green
    today, plus current pass/skip counts for the suites task 4.1 compares against.
    - _Requirements: 2.12_
  - [ ] 1.2 Exploration, ENGINE path (MUST FAIL on unfixed code):
    `test/backend-test/workflow_engine/test_cold_model_engine_exploration.py`.
    With the fake reporting `LOADING` and a `FakePipelineManager` injected via
    `_pipeline_manager_factory`, execute a Triton-backed document and assert the
    post-fix expectations — the execution does NOT reach `run_pipeline`, and its
    error names the model and its state. On `F` this fails in the diagnostic way:
    `run_pipeline` IS called and the recorded error is exactly
    `"Pipeline failed to change state to PLAYING, check logs above this."`
    Record that text verbatim in the outcome; it is the evidence for harm 1.18.
    - _Requirements: 1.13-1.18, 2.11, Property 3_
  - [ ] 1.3 Exploration, CLASSIC path (MUST FAIL on unfixed code): the same
    assertion for `POST /workflows/{id}/run`, plus the input-consumption half —
    on `F` the folder-source image is moved to
    `{INFERENCE_RESULTS_DIR}/{workflowId}/failed/` and the error carries "Source
    image file has been moved to"; post-fix it must be untouched in place.
    - _Requirements: 1.1-1.6, 2.1, 2.2, Property 1_
  - [ ] 1.4 Preservation (MUST PASS on unfixed code), observation-first, both
    paths: with the fake reporting `READY`, a warm execution produces a
    `run_pipeline` call with the byte-identical launch string and arguments, the
    same artifact layout and the same terminal status as `F` (captured as explicit
    expected values). Separately, a genuine pipeline failure — `READY` model,
    pipeline raises — keeps its existing error text AND its `failed/` move (3.2).
    A document with no `emltriton` element never consults Triton at all.
    - _Requirements: 3.2, 3.4, Property 2, Property 4_

- [ ] 2. The shared readiness helper
  - [ ] 2.1 New `src/backend/dda_triton/model_readiness.py` —
    `ensure_model_ready(model_name)` returning a structured outcome
    (`ready` / `failed(reason)`), implementing design.md Decisions 2-5:
    - empty-repo no-op via `feature_configs_utils.triton_repo_has_models()`
      BEFORE any `TritonEdgeClient.get_instance()` call (2.15, 3.12);
    - state read via `TritonEdgeClient.get_instance().get_model_status(...)`;
    - `READY` → immediate success, zero added latency on the warm path;
    - `UNKNOWN` → kick exactly one load through
      `feature_configs_utils.start_model_triton` (the same path the component
      Startup uses), then wait — a pure wait never converges from `UNKNOWN`;
    - `LOADING` → wait WITHOUT re-kicking;
    - `UNAVAILABLE` → fail fast, surfacing Triton's `reason` (the field commit
      `7812407` added);
    - `UNLOADING` → fail fast;
    - bounded poll following `model_convertor._wait_for_model_ready`'s shape
      (`:86-107`), with interval and budget as documented module constants —
      budget sized for the ONNX-on-Thor case from the original report (a first
      load can build a TensorRT engine for a ~300 MB model and take minutes, so a
      DLR-sized budget would time out on a healthy load);
    - failure message names the workflow model name, the resolved Triton name, the
      observed state, Triton's `reason` when present, and the elapsed wait;
    - any wait longer than one interval logs at INFO with elapsed time (Decision
      5 — this is the data that decides whether the deferred reconciler is needed).
    - _Requirements: 2.11, 2.12, 2.13, 2.15_
  - [ ] 2.2 Helper units in `test/backend-test/dda_triton/test_model_readiness.py`:
    one case per state in Decision 2's table; `UNKNOWN` kicks exactly once then
    polls; `LOADING` never kicks; `LOADING → READY` mid-poll succeeds; budget
    exhaustion produces the full message; `UNAVAILABLE` surfaces `reason`;
    empty repo no-ops without constructing the client; the client is never
    constructed when the repo is empty (assert on the fake, not just the return).
    - _Requirements: 2.11-2.15_

- [ ] 3. Wire the gate into both paths (design.md Decision 6 — one helper, two
  call sites, so the paths cannot drift)
  - [ ] 3.1 ENGINE path: `src/backend/workflow_engine/pipeline_executor.py`,
    in `execute()` immediately after `_resolve_model_names(document)`
    (`~:1669`): for each DISTINCT resolved `emltriton` model in the document,
    call the helper; on a non-ready outcome `_finish_failed` with the helper's
    message before the pipeline is built or the plugin scan runs. The execution
    row is already `running` (`:1767-1769`) — the wait must NOT introduce a status
    change (2.13). Documents with no `emltriton` element are untouched.
    Exploration 1.2 now passes; preservation 1.4 still passes.
    - _Requirements: 2.11, 2.13, 3.11, Property 3, Property 4_
  - [ ] 3.2 CLASSIC path: `src/backend/endpoints/workflow.py`
    `run_inference_for_stream` — call the same helper in the pre-flight, before
    the pipeline is built, and return an error naming the model and state instead
    of the generic pipeline error. Because the gate precedes the pipeline, the
    catch-all in `gst_pipeline_executor.execute_workflow_pipeline` is never
    reached for a cold model and the source image is never moved (Decision 7,
    Requirement 2.2) — assert that rather than adding a special case in the
    handler. Exploration 1.3 now passes.
    - _Requirements: 2.1, 2.2, 2.3, Property 1_

- [ ] 4. Gates
  - [ ] 4.1 `test/backend-test/workflow_engine`, `test/backend-test/dda_triton`
    and the classic-path suites green, at or better than task 1.1's counts.
    Explicitly confirm untouched and passing: `test/backend-test/vllm_model_reload/**`
    (3.10), `test/backend-test/utils/test_feature_configs_utils.py` (the `7812407`
    `UNAVAILABLE`+`reason` test), and the capture-routing suites that patch
    `_TRITON_MODEL_REPO` (`test_workflow_capture_routing.py`,
    `test_property_capture_routing.py`).
    - _Requirements: 3.1-3.12, Property 2, Property 4_
  - [ ] 4.2 Security preservation gate at or better than baseline. Grep
    `test/backend-test/security/baselines/` for every file this spec changes and
    rebaseline only what is genuinely pinned, with a note naming this task; do not
    assume either way. Move `edge-cv-portal/infrastructure/cdk.out` aside first if
    a portal deploy has regenerated it.
    - _Requirements: —_

- [ ] 5. USER ACTION — build the JP7 LocalServer component
  - Per `.kiro/steering/builds.md`: confirm no other component build is running,
    move `cdk.out` aside, run the guard suite green FIRST (it runs after the ~1 h
    compile, so a stale baseline wastes the whole build), then build
    `aws.edgeml.dda.LocalServer.arm64JP7`, one target at a time, never concurrently
    with a portal deploy. Log to `.gdk_build_jp7.log`.
  - JP7 is where both sightings happened and is required. The original report notes
    the failure class is runtime- and JetPack-agnostic (DLR on JP5/JP6 has a
    shorter window), so JP5/JP6 builds are worthwhile — each needs its own
    hardware pass.
    - _Requirements: 2.10_

- [ ] 6. USER ACTION — hardware verification on `jetson-thor1`
  - (a) **Engine path, the deterministic reproduction**: restart the LocalServer
    backend container, then immediately trigger a Triton-backed deployed workflow.
    The first run must either succeed after a wait that appears in the log with its
    elapsed time, or fail with a message naming the model and its state — never
    `"Pipeline failed to change state to PLAYING"`. Repeat for N runs and confirm
    0 failures (the 2026-09-22 measurement was 1 of 12).
  - (b) **Classic path**: same restart-then-run through
    `POST /workflows/{id}/run` with a folder image source, and confirm the source
    image is still in place afterwards — NOT moved to `failed/`.
  - (c) **Warm behaviour unchanged**: subsequent runs show no added latency and
    no new log noise.
  - (d) **Backend health**: no crash, no container restart, no crash-loop for a
    sustained period across the runs above.
  - (e) Record the observed cold-window durations from the gate's INFO logs. That
    number is the input to the deferred decision on the Triton boot-time
    reconciler (design.md Decision 1): a routinely long wait argues for building it.
  - Record the result as `verification-notes.md` in this spec, following the
    precedent in
    `.kiro/specs/static-image-camera-binding-and-pin-discoverability/`, and update
    `docs/detection-training-gap.md`'s bug 2 write-up to point at the outcome.
    - _Requirements: 2.10, 2.11, 2.13_
