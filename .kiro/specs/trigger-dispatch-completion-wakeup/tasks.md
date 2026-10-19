# Implementation Plan

## Overview

This plan makes the trigger dispatcher wake when a run ends instead of at its next 0.5 s re-read of
the execution row (design.md D1–D5):
- `executor.dispatch` gains an optional, keyword-only `on_complete` callback, called once at the end
  of the run thread;
- `default_run_starter` waits on a per-run `threading.Event` set by that callback;
- the execution row stays the release condition, and the 0.5 s re-read remains as the fallback.

Only `src/backend/workflow_engine/executor.py` and `src/backend/workflow_engine/trigger_runtime.py`
change.

The plan follows the repo's exploratory bugfix method:
1. Write exploration tests that must FAIL on the unfixed code.
2. Write preservation tests and record a baseline that must PASS on the unfixed code.
3. Apply the fix, then re-run both.
4. Run a checkpoint.
5. Build and verify on devices; these steps are owner-gated.

**On-device rule (builds.md).** This is `src/backend/` code that runs on the edge device. It is not
done until it is built, deployed and verified on real hardware: the MIC-730 first (JP5), then a JP6
device and an x86 device for non-regression. No implementation commit is made before task 7 passes.
The builds come from a local build-snapshot commit on a separate `wip/` branch (task 5), which is
not the implementation commit and is never pushed.

**Sequencing.** The owner schedules the builds after the `triton-model-cleanup-wedge` builds, because
only one component build may run at a time on a build host.

**Suite invocation (host).** Run it from the repository root. The root conftest copies
`test/backend-test/utils/inference_out_for_test/` relative to the current directory
(`test/backend-test/conftest.py:83–91`), so the suite errors when it is started elsewhere.

```
PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/ -q --continue-on-collection-errors
```

Host baseline with Python 3.9.5, at `795ce528` and again at `1fa52f3a` (which adds only a README
change): 1904 passed, 10 skipped and 1 collection error, in about 3.5 minutes. The error is
`test_mqtt_publish_retain.py`, which fails to collect because the Greengrass `awsiot` SDK is not
installed on the host. That error is pre-existing and may be ignored; any other failure may not.

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "2"], "description": "Against UNFIXED code: exploration tests (MUST FAIL) and the preservation baseline (MUST PASS). The two are independent, and both complete before any fix code is written." },
    { "wave": 2, "tasks": ["3"], "description": "The fix in executor.py and trigger_runtime.py, the unit and property tests for the new code, then exploration (now PASS) and preservation (still PASS). Depends on wave 1." },
    { "wave": 3, "tasks": ["4"], "description": "Checkpoint: the full workflow_engine suite, the flask-app container run, the security baseline grep and the guard pair. Depends on wave 2." },
    { "wave": 4, "tasks": ["5"], "description": "OWNER-GATED: the build base, the build snapshot, the pre-build checks and the JP5 LocalServer build on the aarch64 build host, after the triton-model-cleanup-wedge builds. Depends on wave 3." },
    { "wave": 5, "tasks": ["6"], "description": "OWNER-GATED: MIC-730 current-build measurement, deployment and measurement (V1-V10, V4x) against today's numbers. Depends on wave 4." },
    { "wave": 6, "tasks": ["7"], "description": "OWNER-GATED: the JP6 build (aarch64 host) and the x86 build (x86_64 host), one build at a time per host, and the non-regression checks. Depends on wave 5." },
    { "wave": 7, "tasks": ["8"], "description": "verification-notes.md and the local commit, with the owner's go-ahead. Depends on wave 6." }
  ]
}
```

## Tasks

- [ ] 1. Write the bug condition exploration tests (BEFORE the fix)
  - **Property 1: Bug Condition.** The run starter returns right after the run ends. **Property 2:
    Bug Condition.** `drop` accepts a firing that arrives after the run ended. **Property 3: Bug
    Condition.** A backlog drains at the run rate.
  - **CRITICAL:** these tests MUST FAIL on the unfixed code. The failure confirms the defect.
  - Create `test/backend-test/workflow_engine/test_dispatch_wakeup_exploration.py` with the header
    `# Feature: trigger-dispatch-completion-wakeup, Property N: ...` for each property.
  - Read first, and reuse rather than re-invent:
    - from `workflow_engine_test_utils.py`: `make_session_factory`, `write_artifact_set`,
      `VALID_COMPILED`;
    - from `test_trigger_lifecycle.py`: `StubTransportFactory`, `trigger_compiled`,
      `add_registration`, `wait_for`, `manager_stopper`;
    - from `test_workflow_engine_integration.py`: the `ScriptedPipelineManager` shape and the
      `join_execution_thread` helper;
    - from `test_trigger_metadata_persistence.py`: the patch of
      `pipeline_executor._WORKFLOW_CAPTURE_ROOT` to `tmp_path` and the `gst_plugins._scan_registry`
      patch;
    - from `test_property_trigger_context.py:429–438`: one file-backed database shared across
      Hypothesis examples, with rows isolated by a unique registration id per example.
  - Fake executor, registered with `executor.set_executor`:
    1. it opens its own session, marks the row `running` and sleeps d;
    2. it writes `completed` or `failed` with `finished_at`;
    3. its last line records `time.monotonic()` and sets a test Event.

    Always restore the previous executor in `finally`.
  - **E1 (Property 1, Hypothesis).** `@settings(max_examples=10, deadline=None)`. Draw d from
    10–300 ms and the outcome from {completed, failed}. Run
    `default_run_starter(reg, session_factory=sf, poll_interval=3.0)(activation)` on the test thread.
    Assert:
    - t_return − t_end ≤ 0.5 s (one-sided; on F' a first read that lands between the fake's
      terminal commit and its end timestamp returns a few µs before the end);
    - the row is settled at that moment.

    d starts at 10 ms, not 0: at d near 0 the fake can commit its terminal status before the
    starter's first read, so that example passes on F too, and a shrinking replay can make
    Hypothesis report `Flaky` instead of the late return. The run that ends before the first read
    is the "under a millisecond" edge case, which Property 5 covers.
    Expected on F: it returns about 3 s after the start, at the first 3 s re-read.
  - **E2 (Property 2, Hypothesis).** `@settings(max_examples=10, deadline=None)`. Draw d from
    10–60 ms (as in E1) and m from 100–150 ms. Use
    `WorkflowActivationCore(reg, policies=[TriggerPolicy("t", concurrency_policy="drop")], run_starter=default_run_starter(reg, session_factory=sf))`,
    which keeps the production 0.5 s fallback. Then:
    1. `start()`, then `fire("t", ctx1)`;
    2. wait for the fake's Event, then sleep m;
    3. `fire("t", ctx2)`.

    Assert that the second fire returns `True` and that its row reaches `completed`. Stop the core in
    `finally`. Expected on F: `False`, because the node is in flight until about 0.5 s.
  - **E3 (Property 3).** Use `concurrency_policy="queue"` and `queue_depth` 10, with 30 ms fake runs
    and the production 0.5 s fallback. Fire five times at once and assert that all five rows are
    `completed` within 1.0 s of the first fire. Expected on F: about 2.0 s, one 0.5 s re-read for each
    of the first four activations.
  - **E4 (integration).** Use the real `WorkflowExecutor`, through
    `register_workflow_executor(session_factory=sf, pipeline_manager_factory=lambda: manager)` and a
    scripted pipeline manager, with the capture root and the registry scan patched. Clear the hook
    afterwards with `executor.set_executor(None)`, as the `reset_executor_hook` fixture does. Add a
    real `TriggerSubscriptionManager` with:
    - `session_factory=sf`;
    - `mqtt_transport_factory=StubTransportFactory()`;
    - an artifact set built from `trigger_compiled(parameters={"concurrency_policy": "drop"})`;
    - a registered row;
    - `run_starter_factory=lambda rid: default_run_starter(rid, session_factory=sf, poll_interval=3.0)`.

    Run `on_registrations_changed()`, then:
    1. deliver #1 through the worker's `on_delivery`;
    2. wait for its row to be `completed`, then join its `workflow-execution-<id>` thread;
    3. sleep 0.1 s and deliver #2.

    Assert that `on_delivery` returns `True` and that both rows are `completed`. Expected on F:
    `False`, because the node is in flight until the 3 s re-read. If the real executor cannot run
    this document in the harness, record why in this task's outcome and keep E1–E3 as the gate. Do
    not weaken E4 into a fake-executor copy of E2.
  - Run
    `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/test_dispatch_wakeup_exploration.py -q`
    from the repository root. Confirm that E1–E4 FAIL for the expected reason (late return or
    discarded firing), not an import or harness error. Record each counterexample and the observed
    release lag in this task.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3_

- [ ] 2. Write the preservation tests and record the baseline (BEFORE the fix)
  - **Property 6: Preservation.** The starter never returns while the run is active. **Property 7:
    Preservation.** The other paths are unchanged.
  - **CRITICAL:** these tests MUST PASS on the unfixed code. Observe the values on F first, then
    encode them.
  - Create `test/backend-test/workflow_engine/test_dispatch_wakeup_preservation.py`:
    - **P1 (Hypothesis).** `@settings(max_examples=10, deadline=None)`. Draw h from 0–700 ms and the
      outcome. The fake holds the row in `running` for h, then takes
      `t_write = time.monotonic()` immediately before the terminal `session.commit()`. The starter
      keeps the production 0.5 s fallback. Assert that the starter returns no earlier than `t_write`
      and that the row is settled.
    - **P2.** The fake marks the row `running` and returns without settling it. With
      `poll_interval=0.2`, assert that the starter is still blocked after 1.2 s. Then write `failed`
      from the test and assert that the starter returns within 1.0 s.
    - **P3.** With no executor registered, the row stays `pending` and the starter returns within
      0.5 s.
    - **P4.** `executor.dispatch(id)` with no callback:
      - the run thread is named `workflow-execution-<id>` and `daemon` is `True`;
      - `True` is returned;
      - an executor exception is logged as "Workflow execution %s raised" (`caplog`) and does not
        propagate.

      With no executor, `False` is returned and "No workflow executor registered" is logged.
    - **P5.** Snapshot the names of the live threads. Start a `WorkflowActivationCore` whose
      `default_run_starter` uses a session factory that counts calls, and fire nothing for 1.0 s.
      Assert zero factory calls (no row insert, no row read) and no `workflow-execution-*` thread
      that is missing from the snapshot. Stop the core.
    - **P6.** The fake writes `failed` and then raises `RuntimeError`. Assert that the starter returns,
      that the row is `failed`, and that the hook logs the exception: wait up to 1 s for the
      "Workflow execution %s raised" record with the existing `wait_for` helper over
      `caplog.records` before asserting it. On F, a first read that lands between the fake's
      `failed` commit and its raise returns before the hook has logged.
    - **P7 (Property 7, stop during a run).** Use a `WorkflowActivationCore` with
      `TriggerPolicy("t", concurrency_policy="drop")` and
      `default_run_starter(reg, session_factory=sf)` (production 0.5 s fallback). The fake marks the
      row `running`, sleeps 0.8 s and writes `completed`. Then:
      1. `start()`, `fire("t", ctx)`, and wait until the registration's row reads `running`;
      2. find the `workflow-activation-dispatcher-<reg>` thread in `threading.enumerate()`;
      3. call `core.stop(timeout=5.0)` and time it.

      Assert that:
      - `stop` returns within 3.5 s (expected about 1.0 s on F and about 0.8 s on F'). The bound
        stays under the 5 s join timeout, so a hung join still fails;
      - the row reads `completed` when `stop` returns;
      - the dispatcher thread is no longer alive.
    - **P8 (Property 6, the run's tail, design.md D3).** The fake:
      1. marks the row `running` and holds it for 300 ms, so the starter's first read, right after
         the dispatch, sees `running` even on a slow CI host;
      2. commits `completed`, then sleeps 200 ms (the tail);
      3. takes `t_tail = time.monotonic()` as its last statement.

      With `poll_interval=3.0`, assert that the starter returns no earlier than `t_tail` and that
      the row is `completed`. Expected: about 3 s on F (the first re-read) and about 0.5 s on F'.
  - Run the preservation file plus the existing suites, and record the pass and skip counts:
    - `test_property_concurrency_policies.py` (Property 9);
    - `test_property_dispatch_ordering.py` (Property 10);
    - `test_property_or_semantics.py` (Property 11);
    - `test_property_trigger_context.py` (Property 13);
    - `test_trigger_lifecycle.py`;
    - `test_workflow_engine_api.py`;
    - `test_workflow_engine_integration.py`;
    - `test_workflow_continuous_runner.py`.
  - Record the full-suite baseline from the Overview on the current base. Re-measure it if the
    branch has moved.
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10_

- [ ] 3. Apply the fix

- [ ] 3.1 Add the completion callback to `src/backend/workflow_engine/executor.py`
  - Change the signature to
    `dispatch(execution_id: str, *, on_complete: Optional[Callable[[], None]] = None) -> bool`,
    keeping the annotations. `Callable` and `Optional` are already imported.
  - Raise `TypeError` for a non-callable `on_complete` before `get_executor()` and before any thread
    starts.
  - In `_run`, add a `finally` that calls `on_complete()` once after the executor returned or raised.
    Contain an exception from it with `logger.exception("Completion callback for workflow execution %s raised", ...)`.
  - Keep the thread name, `daemon=True`, the INFO line for no executor, the bool return values and the
    existing exception log unchanged.
  - Extend the `ExecutorCallback` comment (`:36–39`) with the synchronous contract.
  - Follow design.md, "Fix Implementation".
  - _Requirements: 2.1, 3.5_

- [ ] 3.2 Wait on the signal in `src/backend/workflow_engine/trigger_runtime.py`
  - Keep the existing annotated signatures, as design.md's snippets show them:
    - `default_run_starter(registration_id: str, session_factory: Optional[Callable] = None, poll_interval: float = DEFAULT_RUN_POLL_INTERVAL_SECONDS) -> Callable[[RunActivation], None]`;
    - the inner `run_starter(activation: RunActivation) -> None`;
    - `_wait_for_terminal_status(session_factory: Callable, execution_id: str, poll_interval: float, completed: Optional[threading.Event] = None, sleep: Callable[[float], None] = time.sleep) -> None`.
  - `default_run_starter`:
    - validate `0 < poll_interval <= threading.TIMEOUT_MAX` at construction (NaN fails the first
      comparison, infinity the second; `Event.wait` raises `OverflowError` for any larger timeout)
      and raise `ValueError` otherwise. Add no separate type check: a non-numeric value raises
      `TypeError` from the comparison;
    - create a `threading.Event` per run and call `executor.dispatch(execution_id, on_complete=completed.set)`;
    - pass `completed=completed` to the wait.

    The row insert is unchanged.
  - `_wait_for_terminal_status`, with the two new optional parameters:
    - at the top of each iteration, sample `signalled = completed.is_set()` BEFORE reading the row
      (design.md D5);
    - read the status inside the session and return when it is not in `ACTIVE_EXECUTION_STATUSES`;
    - if the signal was unset before this read, call `completed.wait(poll_interval)` and re-read;
    - if it was already set and the row is still active, log one WARNING (execution id, status,
      interval) and `sleep(poll_interval)` between reads.

    With `completed=None`, today's loop is unchanged.
  - Leave `DEFAULT_RUN_POLL_INTERVAL_SECONDS = 0.5` as it is and update its comment. Update the
    docstrings in the module (`:46–55`), `default_run_starter` (`:512–525`) and
    `_wait_for_terminal_status` (`:568–572`).
  - Add no import. The module stays importable with the standard library alone.
  - Do not touch `TriggerGate`, `ActivationQueue`, `ActivationDispatcher`, `WorkflowActivationCore`,
    the manager, the transports, `pipeline_executor.py`, `api.py` or `continuous_runner.py`.
  - _Requirements: 2.1, 2.2, 2.3, 2.5, 3.1, 3.7, 3.8, 3.9_

- [ ] 3.3 Write the unit and property tests for the new code
  - Create `test/backend-test/workflow_engine/test_property_dispatch_completion_signal.py`.
  - Inside `@given`, capture log records with a handler installed in the test body, as
    `captured_logs` does in `test_property_tuning_sample_export.py:272–295`. Hypothesis rejects the
    function-scoped `caplog` fixture there.
  - **Property 4 (Hypothesis).** `@settings(max_examples=10, deadline=None)`. Draw the executor
    behavior (returns, raises `RuntimeError`), the callback behavior (records, raises) and a duration
    of 0–20 ms. Assert:
    - `True` is returned;
    - the callback runs exactly once, after the executor finished (ordered through a shared list), on
      the `workflow-execution-<id>` thread;
    - a raising callback is contained and logged.

    Separate tests:
    - with no executor, `False` is returned and the callback is never called;
    - a non-callable callback raises `TypeError` and no thread starts.
  - **Property 5 (Hypothesis, no threads and no real sleeping; the profile's settings).**
    - A fake session factory replays a scripted list of statuses (`pending`, `running`, `completed`,
      `failed`, or `None` for a vanished row).
    - A fake Event flips to set at a scripted read index and records each `wait(timeout)`.
    - A recording `sleep` captures the fallback sleeps.

    Include scripts in which the signal is set between a read and the decision. Assert:
    - the wait returns right after the first settled read and never earlier;
    - each non-returning iteration makes exactly one blocking call with `timeout == poll_interval`:
      `wait` when the signal was unset before that iteration's read, `sleep` otherwise;
    - exactly one WARNING is logged if a read that started after the signal showed an active row,
      and none otherwise. That WARNING's message names the execution id and the status the read
      showed. A signal set between a read and the decision gives no WARNING;
    - with `completed=None`, the behavior is read, then sleep.
  - Construction tests: `default_run_starter` raises `ValueError` for `0`, `-0.1`, `float("nan")`,
    `float("inf")` and `1e10` (finite, but above `threading.TIMEOUT_MAX`), and `TypeError` for
    `None`.
  - _Requirements: 2.1, 2.5, 3.1, 3.9_

- [ ] 3.4 Verify the fix: re-run the exploration tests (they MUST now PASS)
  - Run `test_dispatch_wakeup_exploration.py`. E1–E4 pass. Record the observed release lags.
  - _Requirements: 2.1, 2.2, 2.3_

- [ ] 3.5 Verify preservation: re-run task 2 (it MUST still PASS, unchanged)
  - Run the preservation file and every suite listed in task 2. The counts must be at or above the
    task-2 baseline. No test may be modified or skipped.
  - _Requirements: 3.1–3.10_

- [ ] 4. Checkpoint
  - Run the full `test/backend-test/workflow_engine/` suite on the host from the repository root,
    with `--continue-on-collection-errors`. It must be at or above the baseline from the Overview, and
    the `awsiot` collection error in `test_mqtt_publish_retain.py` is the only error allowed.
  - Run the three new files and the trigger property suites (Properties 9, 10, 11 and 13) with
    `HYPOTHESIS_PROFILE=ci`. The real-time properties keep their explicit
    `@settings(max_examples=10, deadline=None)`.
  - Run the trigger, executor-hook and new suites inside the flask-app image, using the builds.md
    pattern (`PY=$(command -v python3.11 || command -v python3.10)`), and record `$PY --version`. The
    `flask-app` tag is shared across targets and holds whichever target was built last. If the
    interpreter is not 3.11, this run did not cover the JP5 interpreter: repeat it after task 5 has
    built the JP5 image.
  - Grep `test/backend-test/security/baselines/` for `trigger_runtime.py` and `executor.py`. At
    `1fa52f3a` neither is hash-tracked. If that has changed, rebaseline only what is pinned and name
    this task in the note.
  - Run the guard pair from builds.md and confirm it is green.
  - Public-repo hygiene of the diff: no device thing names, account ids, hostnames, bucket names, IoT
    endpoints or e-mail addresses in code, tests or comments.
  - _Requirements: 2.1–2.5, 3.1–3.10_

- [ ] 5. OWNER-GATED: build base, pre-build checks and the JP5 build
  - **Do not start without the owner's go-ahead.** The build runs after the `triton-model-cleanup-wedge`
    builds; never start two component builds at once on a build host.
  - **Build base.** For each test device (MIC-730, JP6, x86), record the commit its current
    LocalServer was built from, and list the device-side differences in both directions:
    `git log --oneline <device-commit>..HEAD -- src/` (changes the deployment would add) and
    `git log --oneline HEAD..<device-commit> -- src/` (changes it would remove). If either list is
    non-empty, report both to the owner before the build. The owner either picks another build
    base, or accepts the difference. If the owner accepts it, task 6's step 1 still measures the
    current build, and verification-notes.md lists the extra commits next to that device's
    before-and-after table. Do not rebase the branch.
  - **Build snapshot.** Before the first build, snapshot the uncommitted tree as a local commit on
    a separate branch, `wip/trigger-dispatch-completion-wakeup-verify`, and build every target
    (this task and task 7) from that commit. Record its id. It is a build snapshot, not the
    implementation commit, and it is not pushed. The task 8 commit must have the same `src/` tree
    as the snapshot (`git diff --quiet <snapshot> HEAD -- src/`).
  - On the aarch64 build host, in a checkout of the snapshot commit (`git rev-parse HEAD` prints
    its id):
    - check that no build is running: `pgrep -af "gdk component build"` and
      `pgrep -af "build-custom.sh"` both return nothing;
    - check that no Portal deploy is in progress, and start none during the build;
    - move `edge-cv-portal/infrastructure/cdk.out` aside;
    - run the guard pair green.
  - Build and publish `aws.edgeml.dda.LocalServer.arm64JP5` alone:
    `./portal-build.sh aarch64 5 2>&1 | tee .gdk_build_jp5.log`. Record the published version next
    to the snapshot id.
  - Restore the two files the script rewrites: `git checkout -- gdk-config.json recipe.yaml`.
  - If task 4's container run did not use Python 3.11, repeat it now against the new JP5 image.
  - _Requirements: 2.4_

- [ ] 6. OWNER-GATED: MIC-730 measurement, deployment and acceptance (design.md, "On-device verification")
  - **Background activity, every window before and after the deployment** (design.md, the
    background rule). The demo's clipper and live view run inside the backend container, and the
    demo prunes its runs on a schedule:
    - start each idle-CPU window at least 120 s after the last run of any earlier window or demo
      activity, and after the clipper's log shows its last clip published or, if no clip was open
      after the last run, 120 s after it;
    - run every window with the same live-view clients connected, preferably none;
    - pause a continuous workflow, if one runs, for each idle-CPU window and resume it right after
      (`.../continuous/pause`, `.../continuous/resume`). If it has not resumed, with its counters
      advancing, within 60 s, stop and report to the owner;
    - before the first window of step 1, record the schedule of every periodic job outside
      LocalServer that writes the backend database or the artifact volume: on the PPE demo, the
      clipper's prune (one interval, an hour by default, after the clipper starts, then every
      interval) and the daily scheduled prune. Start no window and no soak that a scheduled run
      could overlap, allowing for the job's longest run in its log, or wait until that run has
      finished. Check each job's log after every window and after the soak;
    - collect and derive each window before the next window starts. During a window, nothing else
      reads the device API apart from the helpers that were running in step 1.
  - **Trigger publisher (step 3), for every window before and after the deployment:**
    - each message is `{"seq": n, "ts": <host time.time() at publish>}`, published at
      t0 + (n − 1)/f on an absolute schedule, and logged on the host with its seq and ts;
    - the publisher that produced the baseline sends both fields on that schedule but logs only the
      seq. Add ts to its log line, or reconstruct ts_j from the schedule;
    - leave at least 10 s with no publisher running between windows.
  - **Before deploying, on the current build (step 1):**
    - record the LocalServer version, the commit it was built from and the backend `RestartCount`;
    - check the free space on `/aws_dda`. At least 10 GB is required; otherwise stop and report to
      the owner, and delete nothing;
    - record the periodic jobs and their next runs. If a demo trigger loop is running, record its
      rate and options, then stop it. Stopping a demo trigger loop can run one prune; start no
      window until its summary line is in the prune log;
    - measure idle CPU over 300 s from the backend container's cgroup, under the background rule,
      with no trigger publisher running;
    - measure the 1.8 Hz and the 5 Hz windows (steps 3–5, run logs included). They give the Today
      values of the acceptance table. The 5 Hz window's V4 quantity decides the separation rule; if
      the rule calls for it, also measure the f_x window on the current build;
    - compare the run p50 (node status) of both windows with the baseline's 258 ms. If either is
      more than 20 ms higher, report it to the owner before deploying: V3 and V5 assume the
      baseline's run duration.
  - **Deploy (step 2)** by revising only the LocalServer version, following the MIC-730 deployment
    safety steps:
    - stop every trigger publisher first, and stop the demo's clipper and live view cleanly. They run
      inside the backend container, which the deployment restarts or recreates;
    - trigger no run until the deployment is COMPLETED and every model component's Startup has
      finished (READY or "gave up");
    - record the backend container's id and `RestartCount` again. The deployment restarts or
      recreates the container, and V9 compares against these values;
    - run the in-image check before any window: `docker exec <backend> sha256sum` of the image's
      `workflow_engine/executor.py` and `workflow_engine/trigger_runtime.py` must equal the
      snapshot's files (task 5). A mismatch stops the session with a report to the owner;
    - kick the PPE model with one manual run and wait for READY. Record the kick run's outcome;
    - start one more manual run with `POST /workflows/registrations/{id}/trigger`; it must complete.
      This is the MIC-730 check of 3.5 (V11);
    - restart the clipper and the live view, so the windows after the deployment run under the load
      of step 1. Record the restart time: the clipper's prune schedule restarts with it.

    If a model stays LOADING for more than 10 minutes, with or without `Python model file not found`
    in the backend log, capture the evidence and stop. A backend restart needs the owner's go-ahead.
  - **Measure (step 3)** 70 s at 1.8 Hz, 70 s at 5 Hz, 70 s at f_x if the separation rule calls for
    it, and optionally 70 s at 20 Hz for the ceiling (informational only). Each window runs under
    the background rule: clear of every scheduled job, with the same live-view clients.
  - **Collect (step 4)** each window, before the next window starts, from
    `GET /workflows/registrations/{id}`, keeping `startedAt` in
    [window start − 1 s, window end + 5 s]. Never use
    `GET /workflows/registrations/{id}/executions`, which returns at most 50 runs. For each run, read:
    - `/workflows/executions/{executionId}/node-status`;
    - `.../metadata`: `payload_json` with seq and ts, and the receive timestamp;
    - `.../log`.
  - **Derive (step 5):**
    - c_k, gap_k, idle_k and e_k for each completed run, and completed runs/s as
      (n − 1) / (receive_last − receive_first);
    - at 1.8 Hz, attribute each drop: L, k and a_j = ts_j + L. A dispatcher drop is one with
      a_j > last_k + 0.05 s, or one with no earlier run in the window;
    - at 1.8 Hz, count V3 from seq 4 on.

    Repeat an invalid window once (design.md, "Invalid windows"): a clock step, a run with no
    readable log record, at 1.8 Hz a latency spread above the trigger period, or a periodic job
    that ran during it. An idle-CPU window or a soak that such a job overlapped is repeated once
    too. An invalid window does not count; a second invalid one stops the session with a report to
    the owner.
  - **The 1.8 Hz rule** (design.md). Run another 1.8 Hz window, at least 10 s after the last one,
    when the last one had exactly one dispatcher drop or missed V3. Run at most three 1.8 Hz windows
    after the deployment.
  - **Check (steps 6–7)**:
    - idle CPU on the new build, under the background rule: at least 120 s after the last run of
      the windows above;
    - the soak, started clear of every scheduled job: 10 minutes at 1.8 Hz, with V1 over its runs,
      followed by 20 minutes idle;
    - then re-check the free space, the backend container's id and its `RestartCount`;
    - read V10 from `docker logs --since <deployment time>` of the container recorded at the
      deployment. If that log (at most 3 × 50 MB) no longer reaches back to the deployment, read the
      missing span from the backend's `application.log`.
  - **Acceptance criteria** (design.md, V1–V10 and V4x):
    - V1: 0 failed runs, in every window and in the soak;
    - V2, at 1.8 Hz: at most one dispatcher drop over all the 1.8 Hz windows, and none in the last
      one;
    - V3, at 1.8 Hz and counted from seq 4: ≤ 3 % dropped and ≥ 1.75 completed runs/s, in at least
      one window;
    - V4, at 5 Hz: p50 of idle_k − e_k ≤ 0.05 s;
    - V4x, only when the separation rule calls for it: ≤ 10 % of the f_x triggers dropped;
    - V5, at 5 Hz: ≥ 2.3 completed runs/s while c_k p95 ≤ 0.38 s;
    - V6: run p50 ≤ the same rate's step-1 p50 + 40 ms;
    - V7: first_{k+1} ≥ last_k for every pair whose run k has c_k ≤ 0.5 s;
    - V8: idle CPU at most the pre-deploy value plus 10 % of it or plus 0.5 % of one core,
      whichever is larger. If it misses, repeat the after window once, starting at least 120 s
      after the first one ended; V8 passes when the repeat meets it;
    - V9: no restart, with the container id and `RestartCount` from after the deployment unchanged;
    - V10: zero wait WARNINGs in the log of the backend container recorded at the deployment, from
      the deployment to the end of the soak;
    - V11: the manual run after the deployment completes;
    - V12: a continuous workflow, where one runs, resumes with its counters advancing within 60 s
      after each pause and after the deployment.

    Record every number next to today's in `verification-notes.md`. For each 1.8 Hz drop, record
    j, k, a_j − last_k, c_k and run k's excess latency. In these cases, record c_k and the windows
    and report them to the owner instead of failing the task:
    - V5's condition does not hold;
    - the separation rule finds that the `drop` windows cannot show the fix on this device;
    - V2 passes but no 1.8 Hz window meets V3.
  - **After the session (step 8):**
    - stop every trigger publisher;
    - record the session's run count and artifact volume (design.md, step 8). Delete nothing; the
      clean-up is the owner's;
    - after a pass, the new LocalServer stays deployed. If V3 is the only gate not met, it also stays
      deployed until the owner decides;
    - after any other failure, redeploy the previous version with the owner's go-ahead, following
      the same deployment safety steps;
    - leave the demo workflow, the clipper and the live view as they were found;
    - restart a demo trigger loop that step 1 stopped, with the options recorded there, once a
      LocalServer is deployed and healthy (the new one kept, or the previous one restored). After a
      stop-and-report it stays stopped, and the report says so.
  - _Requirements: 2.1, 2.2, 2.4, 2.5, 3.1, 3.5, 3.7, 3.9_

- [ ] 7. OWNER-GATED: JP6 and x86 builds and non-regression
  - **Builds** (design.md, "Build and Rollout"), with the pre-build checks of task 5 on each build host
    and in each checkout, from the snapshot commit of task 5 (the same commit as the JP5 build):
    - JP6 on the aarch64 build host, after the JP5 build there has finished:
      `./portal-build.sh aarch64 6 2>&1 | tee .gdk_build_jp6.log`;
    - x86 on an x86_64 build host, because `build-custom.sh` selects its compose profile from the
      build host's `uname -m`. Carry the snapshot over with `git bundle create` and `git fetch` from
      the bundle, and check that `git rev-parse HEAD` there prints the snapshot id. First record
      which component the x86 test device runs, then build that variant: `./portal-build.sh x86_64`
      for `aws.edgeml.dda.LocalServer.amd64` (CPU-only), or `./portal-build.sh x86_64_nvidia` for
      `aws.edgeml.dda.LocalServer.amd64Nvidia`, with the output in `.gdk_build_amd64.log`;
    - **x86 image base.** The x86 image takes the build host's Ubuntu release (build-custom.sh:61).
      Record the release of the x86 test device's current image (`/etc/os-release` inside its
      backend container) and build on an x86_64 host with the same release; if none is available,
      report to the owner before building. While the branch lacks the 22.04 JPEG decode fix, the
      minimal x86 test workflow uses no JPEG Folder source;
    - one build at a time per build host. Whether the x86 build may overlap an arm64 build on the
      other host is the owner's call;
    - after each build, `git checkout -- gdk-config.json recipe.yaml` in that checkout.
  - **Background activity:** the rule of task 6 applies on each device, with that device's own
    periodic jobs and continuous workflows.
  - **On each device, before deploying:** record the LocalServer component, version and build commit,
    apply task 5's Build base rule in both directions, and use an MQTT-triggered `drop` workflow
    (deploy a minimal one, with the owner's approval, if none is registered). Check the free space
    as in task 6, and record the device's periodic jobs. Measure idle CPU over 300 s, under the
    background rule, and the 5 Hz window on the current build. The 5 Hz window decides the
    separation rule on this device; measure the f_x window too if the rule calls for it.
  - **Deploy** with the deployment safety steps wherever Triton models are deployed, and record the
    backend container's id and `RestartCount` after the deployment on every device. Run the in-image
    check of task 6 before any window; a mismatch stops that device's session with a report to the
    owner. Then check:
    - the 5 Hz window (and f_x if required): V1, V4 (or V4x) and V7, and completed runs/s at least
      95 % of the current build's;
    - V11: one manual run through `POST /workflows/registrations/{id}/trigger` completes;
    - V12: a continuous workflow, if present, keeps running with its counters advancing, and resumes
      within 60 s after each pause and after the deployment;
    - V8: the idle-CPU window on the new build, under the background rule, with V8's one repeat;
    - a soak of 10 minutes at 5 Hz, started clear of every scheduled job, with V1 over its runs,
      followed by 30 minutes idle, with V9 over both;
    - V10 over the deployment to the end of the soak, read as in task 6.
  - **After the session:** as task 6, including the record of the session's run count and artifact
    volume.
  - JP7 is not in this spec's plan. Its next LocalServer build carries the change and should repeat
    these checks.
  - _Requirements: 2.1, 2.5, 3.4, 3.5, 3.6, 3.7, 3.10_

- [ ] 8. Write verification-notes.md and commit, with the owner's go-ahead
  - Write `.kiro/specs/trigger-dispatch-completion-wakeup/verification-notes.md`. Use neutral device
    wording (the MIC-730, a JP6 device, an x86 device). It holds:
    - the host and container suite counts, with the container's `$PY --version`;
    - the exploration counterexamples;
    - the build snapshot's id next to each published LocalServer version, and each device's
      in-image check (the two sha256 values and the result);
    - each device's Build base record: its build commit and the two `git log` lists, with the
      owner's decision where a list was non-empty;
    - the before-and-after tables for the MIC-730, JP6 and x86, including each device's separation
      rule outcome;
    - the MIC-730's 1.8 Hz drop attribution, and every repeated or invalid window with its reason,
      including any V8 repeat;
    - each device's background: the periodic jobs and their runs during the session, the live-view
      clients, and any continuous workflow paused for the idle-CPU windows;
    - each device session's run count and artifact volume, left for the owner's clean-up.
  - Commit on `fix/trigger-dispatch-completion-wakeup`, staging files by name. Before committing,
    check that the tree's `src/` equals the snapshot's (`git diff --quiet <snapshot> HEAD -- src/`
    after the commit, or the equivalent against the index before it). State what was verified on
    which device, and the LocalServer versions. The `wip/` snapshot branch stays local and is
    never pushed. No push of the fix branch either: the repo is public and the push is owner-gated.
  - Before committing, run the public-repo hygiene checks over the added lines and the message.

## Notes

- Tasks 1 and 2 run against the unfixed code. Task 1 must fail for the right reason and task 2 must
  pass, both before any line of task 3 is written.
- Fallback intervals in the timing tests:
  - E1 and E4 inject 3 s, so F's late return is unmistakable. P2 injects 0.2 s.
  - E2 and E3 keep the production 0.5 s, with bounds that separate F from F' by at least 2× (E3:
    1.0 s against about 2.0 s on F).
  - P1, P6 and P7 keep it too. They pass on F and on F', with bounds at least 4× the value expected
    on F' (P7: 3.5 s against about 0.8 s; about 1.0 s on F).
  - P8 injects 3 s, so that no fallback re-read falls inside the run. Its assertion is a lower
    bound (no return before `t_tail`), and it takes about 3 s on F.
  - Property 5 has no real time at all.
- Hypothesis: the workflow_engine conftest loads `engine-fast` (25 examples, the default 200 ms
  deadline). E1, E2, P1 and Property 4 run in real time and set
  `@settings(max_examples=10, deadline=None)` themselves.
- E4 checks that the completion signal reaches the starter through the real `WorkflowExecutor`.
  D3's ordering follows from Property 4 and from the tail running inside `execute`
  (`pipeline_executor.py:2383–2393`). P8 pins it on the starter: no return inside the run's tail.
- The 3.5–4 runs/s ceiling is an estimate and not an acceptance gate. The optional 20 Hz window
  measures it.
- Out of scope, as listed in bugfix.md:
  - run-artifact retention;
  - the demo's trigger rate and policy;
  - an execution row left active after its executor thread has ended;
  - a database read error during the wait;
  - cross-workflow run-log capture.
