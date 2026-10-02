# vLLM JP7 Engine Lifecycle Bugfix Design

## Overview

Two fixes in the LocalServer backend, both on the path where the vLLM runtime starts and stops an engine (bugfix.md):

- **Defect A, an engine shutdown restarts the backend.** Forked children stop sharing the backend's signal wakeup fd. One `os.register_at_fork` hook, installed first thing in `app.py`, resets the fd in every forked child. The cause is confirmed, and the fix is validated against the reproduction in the JP7 image.
- **Defect B, an engine construction can hang forever.** A Construction_Watchdog bounds every construction. At the bound it records diagnostics, stops the stuck engine core, and turns the load into an ordinary FAILED load that the model component retries. vLLM's log output also moves to a persistent, size-bounded file, so the next hang can be diagnosed even after a rollback. The cause of the hang is still unknown. The watchdog's diagnostics are how it gets found, and the bound makes the outcome safe whichever hypothesis holds.

Neither fix changes vLLM, uvicorn, the recipes, `vllm_model_prep.py`, a Dockerfile or `requirements.txt`.

## Glossary

- **Engine_Construction**: `VllmRuntimeManager._construct_engine`, which runs `AsyncLLMEngine.from_engine_args` (vLLM 0.11, V1 `AsyncLLM`), from "Loading vLLM model" to READY or FAILED. It runs on the Runtime_Server's event loop and blocks that loop.
- **EngineCore**: the engine-core process vLLM forks during construction (multiprocessing `fork` context; process title `VLLM::EngineCore`). It is a direct child of the backend process.
- **Runtime_Server**: the loopback uvicorn server on `127.0.0.1:8901` (`vllm_runtime/server.py`), on the daemon thread `vllm-runtime-http`.
- **Main_Server**: `app.py`'s uvicorn 0.23.2 server on port 5000 (or 5443), in the main thread. It owns the process's SIGTERM and SIGINT handlers, which it installs with `loop.add_signal_handler`.
- **Wakeup_Fd**: the fd that `signal.set_wakeup_fd` points at. asyncio sets it to the loop's self-pipe when a handler is added with `add_signal_handler`, and CPython writes each tripped signal's number to it.
- **Construction_Bound**: the longest an Engine_Construction may run: `VLLM_ENGINE_CONSTRUCTION_TIMEOUT_S`, default 900 s.
- **Unblock_Grace**: how long the construction may take to return after its EngineCore was stopped: `VLLM_ENGINE_UNBLOCK_GRACE_S`, default 120 s. vLLM notices a dead engine process within one 10 s startup poll.
- **Construction_Watchdog**: the thread that enforces the Construction_Bound for one construction.
- **Hang_Marker**: a marker file written into a staged model's repository when a construction could not be unblocked (Decision 3). The reconciler skips that model at the next backend start.

## Bug Details

### Bug Condition

```
isBugCondition_A(event):
    event is a signal s delivered to a process P
    AND P was forked (without exec) from the backend
    AND P installed a Python-level handler for s
    AND the backend runs Python <= 3.11 with a Main_Server handler for s
        registered through loop.add_signal_handler
    -> the backend's handler for s runs (SIGTERM: graceful exit, container restart)

isBugCondition_B(construction):
    construction started (log "Loading vLLM model '<m>'")
    AND it has neither returned an engine nor raised after any finite time
    -> the model stays LOADING and the Runtime_Server serves nothing
```

On JP7 every engine shutdown is a C_A event: vLLM's `shutdown(procs)` calls `proc.terminate()` on the EngineCore, which handles SIGTERM in Python (`run_engine_core`).

### Examples

- `cecbee46` (2026-10-02): the queued Shutdown unload ran right after the reconciler's load reached READY. The EngineCore got SIGTERM, and 0.14 s later the backend began its shutdown and exited 0. Docker restarted it.
- `e8c4694a` (2026-10-01): a construction never returned. The Shutdown unload and four component load requests queued behind it for 1 h 45 min, the component went BROKEN, and the deployment rolled back.
- `wakeup_fd_fork_repro.py` in the JP7 image (Python 3.11.16): `PARENT-GOT-SIGTERM`. With `--fix`: "parent did not get SIGTERM", and the child still exits 0.

## Expected Behavior

### Preservation Requirements

- A SIGTERM or SIGINT sent to the backend process itself still shuts it down gracefully (edge-deploy-reliability: bounded `shutdown_event`, the vLLM runtime stop, `os._exit`) inside the 120 s stop grace period.
- Forked children keep handling their own signals. The EngineCore still exits on SIGTERM, and the digital-input processes keep `SIG_DFL`.
- A construction shorter than the bound behaves exactly as today: the same results, state transitions, Starvation_Latch, tombstones, reconciler passes and log lines. The watchdog signals nothing.
- The Triton vision path, the stream workers and the continuous runners are untouched.
- JP5 and amd64 (no vLLM) and JP6 (vLLM 0.9.3 V0, in-process engine) behave identically. On them the fork hook is inert, and the watchdog never runs on JP5 or amd64.

## Hypothesized Root Cause

### Defect A: confirmed

`loop.add_signal_handler` calls `signal.set_wakeup_fd(self._csock.fileno())`. A child forked without exec inherits the C-level wakeup fd, which points at the same socketpair as the parent's loop. CPython's `PyOS_AfterFork_Child` clears pending signals but keeps the wakeup fd. vLLM's EngineCore installs Python handlers for SIGTERM and SIGINT, so when the engine is stopped, CPython's C handler in the child writes `15` to the shared fd. The parent's loop reads it in `_process_self_data` and calls `_handle_signal(SIGTERM)`. That is uvicorn's `handle_exit`, so the main server shuts down.

The reproduction does not trigger on Python 3.14, so the host venvs cannot show it. The fix must be tested under Python 3.10 or 3.11.

### Defect B: unknown, three hypotheses

vLLM's `wait_for_engine_startup` (`v1/engine/utils.py`) polls the handshake sockets every `STARTUP_POLL_PERIOD_MS` (10 s) and raises only when `proc_manager.finished_procs()` reports an engine process that exited. So:

- **H1, a live but stuck EngineCore.** The backend runs about 150 threads (GStreamer, Triton, both uvicorn servers, awscrt, the stream supervisors), and vLLM forks it without exec. A lock that another thread held at the instant of the fork is never released in the child: glibc's dynamic-loader lock, a CUDA driver lock, or a Python `BufferedWriter` lock (CPython reinitializes `logging`'s locks after fork, but not these). The parent then polls forever.
  - Discriminator: the child's stack is blocked in a lock or futex, and the parent's is in `wait_for_engine_startup`.
- **H2, the backend stuck before the fork.** Model-config resolution, the transformers processor load in HF offline mode, or a file lock.
  - Discriminator: there is no EngineCore child at the bound, and the parent's stack is outside `wait_for_engine_startup`.
- **H3, a lost handshake.** The child is ready, but the parent never sees the message.
  - Discriminator: the child is in its busy loop, and the parent is in `wait_for_engine_startup`.

Stopping the EngineCore resolves H1 and H3: the next poll raises, and the load fails normally. H2 needs Decision 3. The diagnostics written at the bound say which one it was.

Spawn (`VLLM_WORKER_MULTIPROC_METHOD=spawn`) would remove H1's inherited state. It is not used: vllm-jp7-engine-cuda-init found the spawned engine dying in its profile run on this stack, and spawn re-imports `app.py` as the child's main module.

## Correctness Properties

Property 1 (Fix, A): For every signal s in {SIGTERM, SIGINT, SIGHUP, SIGUSR1, SIGCHLD} delivered to a fork child that installed a Python handler for s, the parent's asyncio handler for s does not run, and the child's own handler does.

Property 2 (Preservation, A): A signal delivered to the backend process itself still runs the Main_Server's handler, and the backend exits through its graceful path.

Property 3 (Fix, B): For a construction that never completes, `load` returns FAILED within Construction_Bound + 10 s (one vLLM poll) + 5 s, with a reason that contains `ENGINE_CONSTRUCTION_TIMEOUT_MARKER` and the bound. The manager's state is FAILED, not LOADING, and the Runtime_Server serves the next unload, index and load requests.

Property 4 (Preservation, B): For constructions that finish in d < Construction_Bound, `load`'s result, state transitions, retained reason, log lines and side effects are identical with and without the watchdog, and no process is signalled.

Property 5 (Isolation, B): The watchdog only ever signals EngineCore processes that are children of the backend and were created after the timed-out construction started. Triton stubs, stream workers, digital-input processes and other models' engine cores are never signalled.

Property 6 (Persistence): Every vLLM log record emitted by the backend or an EngineCore lands in the persistent vLLM log, and the log's file set stays within its size cap.

## Fix Implementation

### Changes Required

1. **`src/backend/utils/fork_signal_hygiene.py` (new), wired first in `app.py`.**
   ```python
   _installed = False

   def _detach_signal_wakeup_fd():
       try:
           signal.set_wakeup_fd(-1)
       except (ValueError, OSError):
           pass  # not the child's main thread; nothing inherited to detach

   def install():
       global _installed
       if _installed or not hasattr(os, "register_at_fork"):
           return
       os.register_at_fork(after_in_child=_detach_signal_wakeup_fd)
       _installed = True
   ```
   - The forking thread is the child's main thread, so `set_wakeup_fd` is allowed there (validated with `wakeup_fd_fork_repro.py --fix`).
   - Fork plus exec (`subprocess`, Triton's stubs) never runs Python at-fork hooks and needs none: exec resets everything.
   - In `app.py`, call `fork_signal_hygiene.install()` as the first statement under `if __name__ == "__main__":`, before `TritonEdgeClient.get_instance()` and before any thread starts.

2. **`src/backend/vllm_runtime/construction_watchdog.py` (new): `ConstructionWatchdog(model_name, bound_s, grace_s, diagnostics_dir, clock, killer, process_lister)`.**
   - `start()` records the start time and the backend's pid, then arms a daemon timer thread. `cancel()` disarms it. `fired` and `reason` are read-only.
   - At the bound it does three things:
     1. Writes one diagnostics file, `vllm-construction-timeout-<model>-<UTC>.txt`, in the diagnostics dir. It holds `faulthandler.dump_traceback(all_threads=True)` for the backend. For each selected EngineCore it adds `py-spy dump --pid <pid>` when py-spy is on PATH (it is in the JP7 image), and the `/proc/<pid>/{status,wchan}` and per-task `wchan` otherwise. At most 5 such files are kept, oldest deleted first.
     2. Selects the EngineCore processes and SIGKILLs each one with its descendants. The selection is the children of the backend's pid whose process title or cmdline contains `EngineCore` and whose create time is at or after the construction start minus 1 s (Property 5). SIGKILL works on a stopped or deadlocked process, where SIGTERM might never be handled.
     3. Sets `reason`: "vLLM engine construction for '<m>' exceeded the <N> s Construction_Bound; stopped <k> engine core process(es); diagnostics: <file>". The reason carries `ENGINE_CONSTRUCTION_TIMEOUT_MARKER`.
   - Process listing uses `psutil`, a vLLM dependency. It falls back to scanning `/proc` when psutil cannot be imported.

3. **`src/backend/vllm_runtime/manager.py`.**
   - `_construct_engine` arms a watchdog around the factory call, and cancels it in a `finally`. When the factory raises after the watchdog fired, it raises `ConstructionTimeout(watchdog.reason)` instead, so the retained reason names the bound and not vLLM's generic "Engine core initialization failed".
   - `ENGINE_CONSTRUCTION_TIMEOUT_MARKER` joins `_NO_OFFLINE_RETRY_TOKENS`, so the offline-mode cache-miss fallback never repeats a timed-out construction.
   - **Unblock grace.** When the construction has not returned within Unblock_Grace after the kill, or no EngineCore existed at the bound (H2), the watchdog thread sets the entry FAILED itself, under the manager lock, with the timeout reason. The device model-status feeds (feature configs and the shadow) read the manager directly, so they report FAILED even while the Runtime_Server's loop is still blocked. It then logs CRITICAL with the diagnostics path and applies Decision 3.
   - A construction that returns after the watchdog set FAILED (a late return) shuts the new engine down and keeps FAILED. That mirrors the existing "unloaded mid-flight" branch.

4. **Decision 3: recovery when a construction cannot be unblocked (owner to confirm; (a) recommended).**
   - (a) Write a Hang_Marker (`.dda_construction_hang`, JSON with the UTC time and the reason) into the staged repository, then send SIGTERM to the backend's own pid. The Main_Server shuts down gracefully, and docker's restart policy brings the backend back with a free Runtime_Server. At that start the reconciler skips a model carrying the Hang_Marker, with one WARNING, so a recurring H2 hang cannot become a restart loop. An explicit load (the component Startup or an operator) clears the marker, the way it clears the Unload_Tombstone. The component's own retry is then bounded by the same watchdog.
   - (b) Stay blocked, with the model reported FAILED and a CRITICAL log, until someone restarts the backend. A deployment then still ends BROKEN and rolled back, as today, but now diagnosable.

5. **The persistent vLLM log.**
   - When `VLLM_AVAILABLE`, `app.py` writes `$COMPONENT_WORK_PATH/vllm_logging.json` before the vLLM runtime starts, and before anything imports `vllm`. It sets `VLLM_LOGGING_CONFIG_PATH` to that file, with `VLLM_CONFIGURE_LOGGING=1`.
   - The dictConfig keeps vLLM's default: the `vllm` logger at INFO, to stdout, with vLLM's formatter. So `docker logs` and the container log caps from the rtsp spec's finding 18 are unchanged. It adds a `RotatingFileHandler` on `$COMPONENT_WORK_PATH/logs/vllm-engine.log` (10 MB × 3 backups), the persistent directory `application.log` uses, which a container recreation keeps.
   - The forked EngineCore inherits the configured handlers, so its records land in the same file (Property 6). After a rotation by one process the other keeps appending to the renamed file until its own next rollover. That is acceptable for diagnostics, and the total stays bounded per process.

6. **Constants** (`vllm_runtime/constants.py`): `ENGINE_CONSTRUCTION_TIMEOUT_S = 900` and `ENGINE_UNBLOCK_GRACE_S = 120`, both overridable through the environment variables above. The on-device check uses a short bound. The 900 s default is about 6× the measured JP7 construction for `qwen3-vl-8b-instruct` (2 min 24 s). It must be confirmed against a cold-cache construction on the device (3.7), and it stays well below the component's 1500 s load read timeout and its 1800 s Startup timeout, so one bounded failure plus the component's retry fit in one deployment.

### Out of Scope (future hardening)

- **The double load at deployment.** The reconciler's startup load is followed by the component's Shutdown unload and Startup load. Once Defect A is fixed it costs one extra load of about 2.5 min, not a restart.
- **Moving the construction off the Runtime_Server's loop**, so the runtime stays responsive during a normal 2.5 min load. That conflicts with vllm-model-reload-after-backend-restart's Decision 1: the engine binds to the loop it is built on.
- **Upgrading uvicorn.** From 0.29 it installs handlers with `signal.signal`. That is a dependency change across every image, and the fork hook fixes the class of bug whatever the server does.
- JP6's V0 in-process engine (no fork) and JP5 and amd64 (no vLLM), apart from the inertness smoke.

## Testing Strategy

### Validation Approach

First surface the counterexamples on unfixed code, then check the fix and preservation. Defect A's unit tests must run under Python 3.10 or 3.11: in the flask-app image (JP7 3.11, JP6 3.10) or in `python:3.11-slim`. On Python 3.12 and later they skip with a message saying so, because the bug does not reproduce there.

### Exploratory Bug Condition Checking

- **A-1 (unit, Python 3.11).** A subprocess harness shaped like the reproduction: the parent runs an asyncio loop with `add_signal_handler(SIGTERM)`, and a non-main thread starts a `multiprocessing` fork child (as the Runtime_Server's thread does) that installs a Python SIGTERM handler. The harness signals only the child and asserts that the parent's handler did not run. Expected to FAIL on unfixed code.
- **A-2 (device, JP7).** Unload the vLLM model through port 8901 and assert that the backend's RestartCount and StartedAt are unchanged and no "Local server shutdown" line appears. Expected to FAIL today.
- **B-1 (unit, host).** A fake engine factory that mimics vLLM's wait: it forks a child that sleeps forever, then polls the child's liveness every 0.1 s and raises only when the child has died. With a 2 s test bound, assert that `load` returns FAILED within the bound + 1 s. Unfixed, it blocks; the test drives the load on a worker thread and fails at bound + margin.
- **B-2 (device, JP7).** During a load, SIGSTOP the `VLLM::EngineCore`: a live but stuck engine core, H1's shape. Unfixed, the load never completes. Stop the experiment after a few minutes and SIGKILL the stopped process to recover.

### Fix Checking

- A-1 and A-2 pass. Property 1 is checked over the signal set.
- B-1 passes, with the reason, the FAILED state, the served follow-up requests and a diagnostics file (Property 3).
- **The H2 shape.** A fake factory that blocks on an Event without forking: the watchdog sets FAILED after the grace and applies Decision 3, with the SIGTERM-to-self and the marker injected as test doubles.
- B-2 on the device, with `VLLM_ENGINE_CONSTRUCTION_TIMEOUT_S=120`: FAILED at about 120 s, the diagnostics hold both stacks, the EngineCore is gone, and the component's next load reaches READY.

### Preservation Checking

- **Property 2:** SIGTERM to the backend process (in the flask-app image) still exits through the graceful path, within the grace.
- **Property 4 (hypothesis):** over construction durations below the bound and the existing fake-engine fixtures, identical results, states, reasons and logs with and without the watchdog.
- **Property 5 (hypothesis):** over generated process tables (names, parent pids, create times), only `EngineCore` children of the backend created after the start are selected.
- **Existing suites, unchanged and green:** the `vllm_*` and reconciler suites, the edge-deploy-reliability shutdown tests, the security preservation suite and its guards.

### Unit Tests

- `fork_signal_hygiene`: idempotent install, the hook registered once, the child-side reset tolerating `ValueError`.
- The watchdog: cancel before the bound means nothing fires; the diagnostics file contents and the retention of 5; py-spy absent falls back to `/proc`; SIGKILL lands on the selected pids only.
- `manager`: the timeout reason and marker, no offline retry, a late return keeps FAILED and shuts the engine down, and the Hang_Marker round trip with the reconciler skip and the explicit-load clear.
- The persistent log: the dictConfig file content, the env vars set before `vllm` is imported, and a record from a forked child landing in the file.

### Property-Based Tests

Properties 1 (where it fits in a subprocess harness), 4, 5 and 6, with hypothesis at the project defaults, in `test_property_*.py` files.

### Integration Tests (on device)

1. JP7: A-2 passes. An unload leaves the backend running and the continuous workflows uninterrupted.
2. JP7: a LocalServer redeploy with a staged vLLM model gives exactly one backend restart, the deployment's own.
3. JP7: B-2 with a 120 s bound, as above.
4. JP7: measure a construction with a cold compile cache (a fresh container) and record its margin against the 900 s default.
5. JP7: a 30-minute soak with the continuous workflows and a few load and unload cycles. No restart, no failed run.
6. JP6, JP5 and amd64: the backend starts, a docker stop is graceful, and a continuous workflow runs. The hook is inert there.

## Rollout

Builds run one at a time, with the builds.md pre-build checks. JP7 comes first, because only there are both fixes active. JP6, JP5 and amd64 follow, because `app.py` changes on every architecture. On-device verification comes before the commit, for every architecture the change touches.
