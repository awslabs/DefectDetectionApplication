# Implementation Plan: Node Execution Timing

## Overview

Add per-node execution timing to deployed-workflow runs: the `NodeStatusCollector` owns all timing state (lifecycle timing via a centralized `_set_status` write path, invocation durations via `record_invocation_duration`), the executor threads a `duration_sink` into binding processors, persists contained mid-run snapshots, and emits timing log lines in `execute()`'s `finally` block; the endpoint passes `durationMs` through with zero code change; the frontend formats and renders the duration on terminal node cards. All timing paths are contained per the R8.5 discipline — a timing failure never changes a run's outcome.

**Execution environment notes (for task executors):**

- **Backend tests** run on the host with `PYTHONPATH=src/backend:test/backend-test` using the venv at `/tmp/kiro-test-venv`. If the venv is gone, recreate it: `python3 -m venv /tmp/kiro-test-venv && /tmp/kiro-test-venv/bin/pip install pytest hypothesis sarge testfixtures`. Run e.g. `PYTHONPATH=src/backend:test/backend-test /tmp/kiro-test-venv/bin/python -m pytest test/backend-test/workflow_engine -q -p no:cacheprovider`.
- **Frontend tests**: there is NO Node runtime on the host. Use a `node:20-alpine` container via podman with the project's own `node_modules` (pattern from the previous spec), e.g. `podman run --rm -v "$(pwd)/src/frontend":/app -w /app -e CI=true node:20-alpine npx react-scripts test --watchAll=false`. The suite is jest via react-scripts (NOT vitest); fast-check 3.23.2 is already a devDependency.
- **Property tests**: hypothesis with at least 100 examples (backend), fast-check with `numRuns: 100` (frontend). Each property test implements exactly one design property and carries the tag comment `Feature: node-execution-timing, Property N: <property text>`.

## Tasks

- [x] 1. NodeStatusCollector timing (src/backend/workflow_engine/node_status.py)
  - [x] 1.1 Centralize status writes and capture lifecycle timing
    - Add internal state `_running_since: Dict[str, float]`, `_durations_ms: Dict[str, int]`, `_invocation_durations_ms: Dict[str, int]`
    - Add private `_set_status(node_id, status)` performing the identical `self._statuses[node_id] = status` assignment plus contained timing capture: first entry into `running` records `time.monotonic()`; first entry into a terminal state records `max(0, round((now - start) * 1000))` only if the node ran and has no duration yet; later transitions never overwrite; a node terminal without ever running records nothing; timing capture wrapped in try/except so the status assignment always stands
    - Route all five mutation paths (`sink`, `mark_running_all`, `mark_success_all`, `mark_failure`, `finalize`) through `_set_status`, keeping their exact signatures, guards, and transition rules
    - _Requirements: 1.1, 1.2, 1.5, 1.6, 1.7, 5.1_

  - [x] 1.2 Add invocation durations and durationMs serialization
    - Add `record_invocation_duration(node_id, duration_ms)`: contained — ignores None/untracked node ids and negative or non-numeric values; stores `int(round(duration_ms))`; first recorded value wins (idempotent per node per run)
    - Add `duration_ms_of(node_id)` returning the value `to_map()` would serialize (invocation value first, else lifecycle value, else None)
    - Extend `to_map()` to add the additive `durationMs` field (non-negative int, invocation precedence) without touching `status`/`detail`
    - _Requirements: 1.3, 1.4, 1.8, 2.1, 2.3, 2.4_

  - [x] 1.3 Add module-level format_duration_ms helper
    - `format_duration_ms(duration_ms)`: `< 1000` → `"<n> ms"` (`"0 ms"` for 0); `>= 1000` → seconds with exactly one decimal place rounded to the nearest tenth, e.g. `"3.4 s"`
    - _Requirements: 3.3, 3.4, 4.2_

  - [ ]* 1.4 Write property test for lifecycle timing model
    - **Property 1: Lifecycle timing model**
    - Generated transition-event sequences (sink running/warning, mark_running_all, mark_success_all, mark_failure, finalize) with an injected fake monotonic clock (monkeypatched `time.monotonic`); assert serialized `durationMs` equals first-running→first-terminal interval, absent when the node never ran, and unchanged by all later events
    - New file in test/backend-test/workflow_engine/ (test_property_* naming), hypothesis, min 100 examples
    - **Validates: Requirements 1.1, 1.2, 1.6**

  - [ ]* 1.5 Write property test for invocation duration precedence
    - **Property 2: Invocation duration precedence**
    - Any interleaving of `record_invocation_duration` with lifecycle transition events (before or after the first terminal transition); serialized `durationMs` equals the invocation duration
    - **Validates: Requirements 1.3, 1.4**

  - [ ]* 1.6 Write model-based property test for transition non-regression
    - **Property 5: Transition non-regression (model-based)**
    - Compare per-node status after every event against a minimal status-only reference model of the pre-feature transition semantics (same guards, ordering, finalize resolution)
    - **Validates: Requirements 5.1, 5.3**

  - [ ]* 1.7 Write property test for timing error containment
    - **Property 6: Timing error containment**
    - Transition sequences with a monotonic clock / recording step that raises at arbitrary points: no error propagates, no partial `durationMs` recorded, per-node statuses at every step identical to the error-free run
    - **Validates: Requirements 1.7, 5.4**

  - [ ]* 1.8 Write property test for serialization round trip and shape preservation
    - **Property 4: Serialization round trip and shape preservation**
    - Generated collector states round-tripped through `to_json()` → `run_artifacts.parse_node_status`; `status`/`detail` exactly pre-feature; `durationMs` present as non-negative int iff recorded; generated pre-feature maps (no timing keys) parse unchanged
    - **Validates: Requirements 1.5, 1.8, 2.1, 2.2, 2.3, 2.4, 2.6, 5.2**

  - [ ]* 1.9 Write property test for backend duration formatting
    - **Property 7: Duration formatting** (backend `format_duration_ms`)
    - Generated non-negative integer ms values; `< 1000` → whole-ms `" ms"` form (`"0 ms"` for zero); `>= 1000` → one-decimal nearest-tenth `" s"` form
    - **Validates: Requirements 3.3, 3.4, 4.2**

- [x] 2. Binding processor invocation timing (src/backend/workflow_engine/output_bindings.py)
  - [x] 2.1 Add duration_sink measurement to the three processors
    - Add optional trailing keyword `duration_sink: Optional[Callable[[Optional[str], float], None]] = None` to `BedrockInferenceProcessor.process`, `LlmInferenceProcessor.process`, and `OutputBindingProcessor.process` (default None → behavior byte-identical to today)
    - Wrap each actual invocation (`_run_one` calls; output-binding runner call after gating checks) in a monotonic measurement reported in `try/finally`, so error-terminated invocations are timed and raises propagate exactly as today; gated-out/skipped and filter/conditional bindings report nothing
    - Add shared private `_emit_duration(duration_sink, node_id, elapsed_ms)` mirroring `_emit_detail`: None sink is a no-op; a raising sink is caught and logged at debug, never affecting the binding outcome
    - _Requirements: 1.3, 1.7_

  - [ ]* 2.2 Write property test for binding invocation timing
    - **Property 3: Binding invocations are timed on success and on error**
    - Generated executor-binding documents with stub runners/clients that randomly succeed or raise; each processor reports exactly one `(nodeId, elapsed_ms)` per binding actually invoked (including error-terminated), none for skipped bindings; a None or raising sink never changes the processor outcome
    - **Validates: Requirements 1.3, 1.7**

- [x] 3. WorkflowExecutor wiring (src/backend/workflow_engine/pipeline_executor.py)
  - [x] 3.1 Thread duration_sink into processor invocations
    - Initialize `collector = None` next to `log_capture` at the top of `execute()` so the `finally` block can reference it on every path
    - Add static `_duration_sink(collector)` mirroring `_status_sink`: maps `(node_id, elapsed_ms)` to `collector.record_invocation_duration`, or None when there is no collector
    - Pass `duration_sink=` into the Bedrock and LLM `process(...)` calls only when the processor signature accepts the keyword; generalize `_handler_accepts_detail_sink` to `_handler_accepts_keyword(handler, name)` and thread `duration_sink` through `_run_post_run_handler` / `_invoke_post_run_handler` exactly as `detail_sink` is threaded today
    - _Requirements: 1.3, 1.4, 5.2_

  - [x] 3.2 Add contained mid-run node-status snapshots
    - Add `_persist_node_status_snapshot(session, execution, collector)`: writes `collector.to_json()` to `execution.node_status_json` and commits WITHOUT finalizing or changing any status; best-effort — any error is logged (debug), session rollback best-effort, run proceeds
    - Call it at executor-thread checkpoints: after `run_pipeline` returns successfully, after the Bedrock block, after `_mark_llm_outcomes`, and after the post-run handler returns; the terminal `_persist_node_status` remains the authoritative last write, unchanged
    - _Requirements: 2.5, 5.4_

  - [x] 3.3 Emit timing log lines in execute()'s finally block
    - Add `_emit_timing_logs(collector)`: exactly one `logger.info` line per node with a recorded duration — `'Node <nodeId> took <formatted>'` using `format_duration_ms` and `duration_ms_of`, in collector insertion order; each line in its own try/except so one failure cannot suppress the rest; a None collector emits nothing
    - Call it at the top of the `finally` block, before `log_capture.stop()`, so lines land inside the capture window exactly once on every terminal path
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [ ]* 3.4 Write property test for run-log timing emission
    - **Property 9: Run-log timing emission**
    - Generated collector states with a logger capture and per-node injected emission failures: exactly one line per node with a recorded duration whose emission did not raise, none for nodes without durations, each line states nodeId and the Property-7-formatted duration, no error propagates
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4**

  - [ ]* 3.5 Write example test for mid-run snapshot availability
    - Drive `WorkflowExecutor.execute()` with the existing stubbed-session/manager test harness; assert `node_status_json` snapshots containing `durationMs` for terminal nodes are committed before the terminal persist
    - _Requirements: 2.5_

  - [ ]* 3.6 Write example test for capture-window placement of timing lines
    - Executor-flow test asserting the timing lines are present in the captured run log file (RunLogCapture active) on both the success path and a failure path
    - _Requirements: 4.1, 4.3_

- [ ] 4. Endpoint pass-through pinning (no code change in api.py / run_artifacts.py)
  - [ ]* 4.1 Write example tests pinning the verbatim pass-through
    - Node-status endpoint returns `durationMs` verbatim alongside `status`/`detail`; entries without a duration have no timing field; a pre-feature `node_status_json` row (no timing fields) is returned without error and without timing fields
    - _Requirements: 2.2, 2.3, 2.6, 5.2_

- [x] 5. Checkpoint - Backend suite green
  - Run the backend workflow_engine suite: `PYTHONPATH=src/backend:test/backend-test /tmp/kiro-test-venv/bin/python -m pytest test/backend-test/workflow_engine -q -p no:cacheprovider` (recreate the venv per the environment notes if missing). Ensure all tests pass with no modified pre-existing assertions, ask the user if questions arise.
  - _Requirements: 5.5_

- [x] 6. Frontend timing display (src/frontend)
  - [x] 6.1 Add durationMs to the node-status entry type
    - In `api/WorkflowRegistrationAPI.ts`, add optional `durationMs?: number` to the node-status entry interface (additive)
    - _Requirements: 2.1, 2.4_

  - [x] 6.2 Create the durationFormat helper
    - New pure, dependency-free `components/deployed-workflow/graph/durationFormat.ts` exporting `formatDuration(durationMs: unknown): string | null`: null for non-numbers, NaN/Infinity, negatives; `Math.round(ms) < 1000` → `` `${rounded} ms` `` (including `"0 ms"`); else `` `${(ms / 1000).toFixed(1)} s` ``
    - _Requirements: 3.3, 3.4, 3.8_

  - [ ]* 6.3 Write property test for frontend duration formatting
    - **Property 7: Duration formatting** (frontend `formatDuration`)
    - `durationFormat.property.test.ts` with fast-check (`numRuns: 100`): generated non-negative ints follow the ms/s rules; generated negatives/non-numbers return null
    - **Validates: Requirements 3.3, 3.4, 4.2**

  - [x] 6.4 Set durationText in the node visual
    - In `graphGeometry.ts`, add optional `durationText?: string` to `NodeVisual`; `nodeVisual()` sets it only when the resolved status is terminal (`success`/`warning`/`failure`) AND `formatDuration(entry?.durationMs)` returns non-null
    - _Requirements: 3.1, 3.2, 3.5, 3.6, 3.8_

  - [ ]* 6.5 Write property test for node card timing display
    - **Property 8: Node card timing display**
    - `nodeVisual` over generated status entries (arbitrary statuses × arbitrary `durationMs` values including negatives, NaN, strings, absent): duration text included iff status is terminal and `durationMs` is a non-negative finite number, and equals `formatDuration(durationMs)`; all other cases render with existing coloring/detail and no timing element
    - **Validates: Requirements 3.1, 3.2, 3.5, 3.6, 3.8**

  - [x] 6.6 Render the timing span in RunStatusGraph
    - In `RunStatusGraph.tsx`, render `visual.durationText` next to the status line when present as a span with `data-testid={`node-duration-${visual.id}`}` (fontSize 12, color #5f6b7a, fontWeight 400); no polling changes — `nodeVisual` recomputes on every react-query poll result
    - _Requirements: 3.1, 3.2, 3.7_

  - [ ]* 6.7 Write rendered-graph example tests
    - React Testing Library, following `RunStatusGraph.preview.test.tsx` conventions: success node with `durationMs` shows `node-duration-<id>` including the `"0 ms"` case (R3.1, R3.4); rerender with an updated mocked node-status query result makes a newly arrived `durationMs` appear without reload (R3.7); no timing element for running nodes or terminal nodes without the field (R3.5, R3.6)
    - _Requirements: 3.1, 3.4, 3.5, 3.6, 3.7_

- [x] 7. Checkpoint - Frontend suite green
  - Run the frontend suite in a node:20-alpine container via podman using the project's own node_modules (no Node on host): `podman run --rm -v "$(pwd)/src/frontend":/app -w /app -e CI=true node:20-alpine npx react-scripts test --watchAll=false`. Ensure all tests pass (including the existing preservation tests) with no modified pre-existing assertions, ask the user if questions arise.
  - _Requirements: 5.5_

- [ ] 8. On-device verification (required before commit — builds.md gate)
  - [ ] 8.1 Verify end-to-end on jetson-thor1 (JP7) from a real built+deployed component
    - The build dispatch is user-coordinated: builds run strictly one at a time per the builds steering (check `pgrep -af "gdk component build"` / `pgrep -af "build-custom.sh"` first), move `edge-cv-portal/infrastructure/cdk.out` aside, and confirm the security preservation guard suite is green before starting — coordinate with the user before dispatching `gdk component build` for `aws.edgeml.dda.LocalServer.arm64JP7`
    - After deploy to jetson-thor1, run a deployed workflow containing both node kinds (a pipeline path plus at least one executor binding, e.g. llm_inference or mqtt_publish) and verify: timings appear on terminal nodes in the Run_Status_Graph live during the run (polling) and after completion; the node-status endpoint returns `durationMs`; exactly one timing line per timed node appears in "View run log"; a pre-feature execution's run view still renders without error; the backend stays healthy for a sustained period (no crash/restart loop)
    - Only then commit, stating what was verified on which device
    - _Requirements: 2.2, 2.5, 2.6, 3.1, 3.7, 4.3, 5.3_

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for faster MVP, but Requirement 5.5 requires each code change to be covered by at least one new test in the same layer — skipping them defers that coverage.
- No code change is made to `api.py` / `run_artifacts.py` (task 4 only pins the existing pass-through) and no preservation-tracked files (docker-compose, Dockerfiles, requirements.txt, recipes, setup_station.sh) are touched, so no security-gate baseline updates are expected.
- Property tests use hypothesis (backend, min 100 examples) and fast-check 3.23.2 (frontend, `numRuns: 100`); each implements exactly one design property with the tag comment `Feature: node-execution-timing, Property N: <property text>`.
- Frontend tests run under jest via react-scripts (NOT vitest), inside a node:20-alpine podman container since the host has no Node runtime.
- Checkpoints ensure incremental validation; the existing backend and frontend suites must pass with zero failures and no modified pre-existing assertions (R5.5).

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "6.1", "6.2"] },
    { "id": 1, "tasks": ["1.2", "1.6", "2.2", "6.3", "6.4"] },
    { "id": 2, "tasks": ["1.3", "1.4", "1.5", "1.7", "1.8", "3.1", "4.1", "6.5", "6.6"] },
    { "id": 3, "tasks": ["1.9", "3.2", "6.7"] },
    { "id": 4, "tasks": ["3.3", "3.5"] },
    { "id": 5, "tasks": ["3.4", "3.6"] },
    { "id": 6, "tasks": ["8.1"] }
  ]
}
```
