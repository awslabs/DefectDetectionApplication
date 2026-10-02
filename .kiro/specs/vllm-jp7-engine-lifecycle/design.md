# vLLM JP7 Engine Lifecycle Bugfix Design

## Overview

Two fixes in the LocalServer backend, both on the path where the vLLM runtime starts and stops an engine (bugfix.md):

- **Defect A, an engine shutdown restarts the backend.** Forked children stop sharing the backend's signal wakeup fd. One `os.register_at_fork` hook, installed first thing in `app.py`, resets the fd in every forked child. The cause is confirmed, and the fix is validated against the reproduction in the JP7 image.
- **Defect B, an engine construction can hang forever.** A Construction_Watchdog bounds every construction. It fires when the construction makes no progress over the Stall_Window (120 s), or at the hard Construction_Bound (600 s), whichever comes first. When it fires it records diagnostics, stops the stuck engine core, and turns the load into an ordinary FAILED load that the model component retries. vLLM's log output is also copied to a persistent, size-bounded file, so the next hang can be diagnosed even after a rollback. The cause of the hang is still unknown. The watchdog's diagnostics are how it gets found, and the triggers make the outcome safe whichever hypothesis holds.

Neither fix changes vLLM, uvicorn, the recipes, `vllm_model_prep.py`, the reconciler, a Dockerfile or `requirements.txt`. `app.py` changes by two lines, so its pinned hash in `test/backend-test/security/baselines/iam_out_of_scope_baseline.json` is rebaselined.

**Owner decisions (2026-10-02, task 8).** Decision 3: option (a). The 900 s bound first proposed was accepted, with a request for something faster if possible. That led to the stall check, a 600 s hard bound and a 30 s grace (change 6).

## Glossary

- **Engine_Construction**: `VllmRuntimeManager._construct_engine`, which runs `AsyncLLMEngine.from_engine_args` (vLLM 0.11, V1 `AsyncLLM`), from "Loading vLLM model" to READY or FAILED. It runs on the Runtime_Server's event loop and blocks that loop.
- **EngineCore**: the engine-core process vLLM forks during construction (multiprocessing `fork` context; process title `VLLM::EngineCore`). It is a direct child of the backend process.
- **Runtime_Server**: the loopback uvicorn server on `127.0.0.1:8901` (`vllm_runtime/server.py`), on the daemon thread `vllm-runtime-http`.
- **Main_Server**: `app.py`'s uvicorn 0.23.2 server on port 5000 (or 5443), in the main thread. It owns the process's SIGTERM and SIGINT handlers, which it installs with `loop.add_signal_handler`.
- **Wakeup_Fd**: the fd that `signal.set_wakeup_fd` points at. asyncio sets it to the loop's self-pipe when a handler is added with `add_signal_handler`, and CPython writes each tripped signal's number to it.
- **Construction_Bound**: the longest an Engine_Construction may run: `VLLM_ENGINE_CONSTRUCTION_TIMEOUT_S`, default 600 s (0 disables it).
- **Construction_Progress**: the CPU time and I/O (`rchar + wchar`) of the constructing thread plus the construction's EngineCore process trees, read from `/proc`.
- **Stall_Window**: `VLLM_ENGINE_STALL_WINDOW_S`, default 120 s (0 disables it). A construction whose Construction_Progress over the window stays under BOTH 1 CPU-second and 1 MiB of I/O is stalled.
- **Unblock_Grace**: how long the construction may take to return after the watchdog fired and stopped its EngineCore: `VLLM_ENGINE_UNBLOCK_GRACE_S`, default 30 s. vLLM notices a dead engine process at once: `wait_for_engine_startup` polls the process sentinels together with the handshake socket.
- **Construction_Watchdog**: the thread that enforces the Stall_Window and the Construction_Bound for one construction.
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

vLLM's `wait_for_engine_startup` (`v1/engine/utils.py`) polls the handshake socket and the engine processes' sentinels every `STARTUP_POLL_PERIOD_MS` (10 s). It raises as soon as an engine process exits, and never otherwise: nothing bounds the wait. So:

- **H1, a live but stuck EngineCore.** The backend runs about 150 threads (GStreamer, Triton, both uvicorn servers, awscrt, the stream supervisors), and vLLM forks it without exec. A lock that another thread held at the instant of the fork is never released in the child: glibc's dynamic-loader lock, a CUDA driver lock, or a Python `BufferedWriter` lock (CPython reinitializes `logging`'s locks after fork, but not these). The parent then polls forever.
  - Discriminator: the child's stack is blocked in a lock or futex, and the parent's is in `wait_for_engine_startup`.
- **H2, the backend stuck before the fork.** Model-config resolution, the transformers processor load in HF offline mode, or a file lock.
  - Discriminator: there is no EngineCore child when the watchdog fires, and the parent's stack is outside `wait_for_engine_startup`.
- **H3, a lost handshake.** The child is ready, but the parent never sees the message.
  - Discriminator: the child is in its busy loop, and the parent is in `wait_for_engine_startup`.

All three sit still: no CPU time and no I/O in the constructing thread or the EngineCore. A legitimate construction never does, because weight loading, torch.compile, FlashInfer's JIT, CUDA graph capture and profiling all keep burning CPU (in the EngineCore or its compiler subprocesses). That is what the stall check measures.

Stopping the EngineCore resolves H1 and H3: vLLM sees the dead process at once, and the load fails normally. H2 needs Decision 3. The diagnostics written when the watchdog fires say which one it was.

Spawn (`VLLM_WORKER_MULTIPROC_METHOD=spawn`) would remove H1's inherited state. It is not used: vllm-jp7-engine-cuda-init found the spawned engine dying in its profile run on this stack, and spawn re-imports `app.py` as the child's main module.

## Correctness Properties

Property 1 (Fix, A): For every signal s in {SIGTERM, SIGINT, SIGHUP, SIGUSR1, SIGCHLD} delivered to a fork child that installed a Python handler for s, the parent's asyncio handler for s does not run, and the child's own handler does.

Property 2 (Preservation, A): A signal delivered to the backend process itself still runs the Main_Server's handler, and the backend exits through its graceful path.

Property 3 (Fix, B): For a construction that never completes, `load` returns FAILED within the earlier of (the Stall_Window after progress stopped) and the Construction_Bound, plus one 5 s sample and a few seconds for the diagnostics, with a reason that starts with `ENGINE_CONSTRUCTION_TIMEOUT_MARKER` and names the trigger. The manager's state is FAILED, not LOADING, and the Runtime_Server serves the next unload, index and load requests.

Property 4 (Preservation, B): For constructions that finish before the watchdog fires, `load`'s result, state transitions, retained reason, log lines and side effects are identical with and without the watchdog, and no process is signalled.

Property 5 (Isolation, B): The watchdog only ever signals EngineCore processes that are children of the backend and were created after the timed-out construction started, plus their descendants. Triton stubs, stream workers, digital-input processes and other models' engine cores are never signalled.

Property 6 (Persistence): Every vLLM log record emitted by the backend or an EngineCore after the first `import vllm` lands in the persistent vLLM log, and the log's file set stays within its size cap.

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
   - In `app.py`, `from utils import fork_signal_hygiene` and `fork_signal_hygiene.install()` are the first two statements under `if __name__ == "__main__":`, before `TritonEdgeClient.get_instance()` and before the backend forks anything.
   - `DigitalInputProcess.run` already resets the wakeup fd itself. The hook makes that the rule for every fork child.

2. **`src/backend/vllm_runtime/construction_watchdog.py` (new): `ConstructionWatchdog(model_name, bound_s, stall_window_s, grace_s, on_unblock_failed, diagnostics_dir, ...)`.**
   - Everything that touches the system is injectable: the clock, the `/proc` process table, the uptime, the I/O and thread-progress readers, the killer, the stack dumpers. It uses only the standard library and reads `/proc` directly (psutil is not in every image, nor in the host test venv).
   - `start()` runs on the constructing thread. It records the monotonic start, the start in clock ticks since boot (`/proc/uptime`), and the thread's native id, then starts a daemon thread. The first sample comes one 5 s period later, so a fast construction never scans `/proc`. `cancel()` returns `True` when the watchdog had not fired, and `False` when it had (the construction is then a timeout whatever it returned). `fired`, `trigger`, `reason`, `diagnostics_path` and `killed_pids` are read-only.
   - Each sample adds the positive deltas of Construction_Progress. It fires on the first of:
     - **stall**: over the last Stall_Window, under 1 CPU-second and under 1 MiB of I/O;
     - **bound**: the Construction_Bound elapsed.
   - When it fires it:
     1. Selects this construction's EngineCores (Property 5): children of the backend's pid whose cmdline contains `EngineCore` or whose comm contains `EngineCor` (the 15-character truncation of `VLLM::EngineCore`), started at or after the construction start minus 1 s. Start times compare in clock ticks since boot, so a wall-clock step cannot change the selection.
     2. Writes one diagnostics file, `vllm-construction-timeout-<model>-<UTC>.txt`, in `$COMPONENT_WORK_PATH/logs`. It holds the trigger, the settings, the recent progress samples, `faulthandler.dump_traceback(all_threads=True)` for the backend, the constructing thread's state and wait channel, and for each selected EngineCore `py-spy dump --nonblocking --pid <pid>` (py-spy is in the JP7 image; `--nonblocking` cannot hang on a stopped process) plus its `/proc` thread states, wait channels and kernel stacks. At most 5 such files are kept, oldest deleted first. A failure here never stops the recovery.
     3. Sets `reason`, which starts with `ENGINE_CONSTRUCTION_TIMEOUT_MARKER` (`engine-construction-timeout:`), names the model and the trigger, the number of engine cores stopped and the diagnostics file, and names the environment variable that tunes the trigger. It contains none of the KV-cache markers, so neither the component's nor the reconciler's KV-OOM recovery fires on it.
     4. SIGKILLs each selected EngineCore's descendants, then the EngineCore. SIGKILL ends a stopped or deadlocked process, where SIGTERM might never be handled.
     5. Waits the Unblock_Grace for the construction to return (`cancel()`), and otherwise calls `on_unblock_failed(reason)`.

3. **`src/backend/vllm_runtime/manager.py`.**
   - `_construct_engine(engine_args, model_name)` arms a watchdog around the factory call. When the factory raises after the watchdog fired, it raises `ConstructionTimeout(watchdog.reason)` instead, so the retained reason names the trigger and not vLLM's generic "Engine core initialization failed". A late successful return after the watchdog fired shuts the new engine down and raises the same.
   - `ENGINE_CONSTRUCTION_TIMEOUT_MARKER` joins `FAILURE_CATEGORY_TOKENS` (the classifier keeps it as the reason's one token) and `_NO_OFFLINE_RETRY_TOKENS` (the offline-mode cache-miss fallback never repeats a timed-out construction).
   - **Unblock grace** (`_on_construction_unblock_failed`, on the watchdog thread): the entry is set FAILED under the manager lock, with the reason plus a sentence on the restart. The device model-status feeds read the manager directly, so they report FAILED while the Runtime_Server's loop is still blocked. It logs CRITICAL, writes the Hang_Marker and restarts the backend (Decision 3). Nothing here touches CUDA: the constructing thread may be stuck inside it.
   - The manager's constructor takes `construction_bound_s`, `stall_window_s`, `unblock_grace_s`, `diagnostics_dir`, `watchdog_options` and `self_restart`, all defaulting to the production values.

4. **Decision 3: recovery when a construction cannot be unblocked. The owner chose (a) on 2026-10-02.**
   - Write a Hang_Marker (`.dda_construction_hang`, JSON with the UTC time and the reason) into the staged repository, then send SIGTERM to the backend's own pid. In the container the backend is PID 1, which receives SIGTERM because its handler is installed. The Main_Server shuts down gracefully (about 35 s: the 20 s cleanup budget plus the Runtime_Server's 10 s join, which times out because its loop is the blocked one), and docker's restart policy brings the backend back with a free Runtime_Server. If the process has not exited 90 s after the SIGTERM, the watchdog thread calls `os._exit(1)`, so docker still restarts it.
   - A staged repository carrying the Hang_Marker reports FAILED with the recorded reason (disk-derived, like UNLOADED for the Unload_Tombstone; the tombstone takes precedence). The reconciler re-drives STAGED models only, so it skips the model, and a recurring H2 hang cannot become a restart loop. The manager logs one WARNING per backend life for it. The reconciler itself is unchanged (its source is pinned by jp6-vllm-kv-cache-oom-regression's preservation suite).
   - An explicit load (the component Startup or an operator) clears the marker first, like the Unload_Tombstone, and the component's atomic re-stage removes it with the old directory. The component's retry is then bounded by the same watchdog.
   - Option (b), stay blocked with the model FAILED until someone restarts the backend, was not chosen.

5. **The persistent vLLM log (`src/backend/vllm_runtime/engine_log.py`, new).**
   - `attach_persistent_vllm_log()` adds one `RotatingFileHandler` on `$COMPONENT_WORK_PATH/logs/vllm-engine.log` (10 MB × 3 backups, INFO, UTC timestamps, the writer's pid on every line) to the `vllm` logger. That is the persistent directory `application.log` uses, which a container recreation keeps.
   - `_default_engine_factory` calls it right after its `import vllm` and before the engine is built. It is idempotent. vLLM's import-time `dictConfig` removes any handler attached to its logger earlier, so the handler cannot be attached sooner, and records logged while `vllm` itself is imported stay stdout-only.
   - vLLM's own logging configuration and stdout handler are untouched, so `docker logs` and the container log caps from the rtsp spec's finding 18 are unchanged.
   - This replaces the first design (`VLLM_LOGGING_CONFIG_PATH` with a generated dictConfig file): a config file that failed to load would make `import vllm` itself fail, and every load with it. It also keeps `app.py` out of it.
   - The forked EngineCore inherits the handler, so its records land in the same file (Property 6). After a rotation by one process the other keeps appending to the renamed file until its own next rollover. That is acceptable for diagnostics, and the file set stays bounded.

6. **Constants** (`vllm_runtime/constants.py`), each overridable through its environment variable, read when the manager is built:
   - `DEFAULT_ENGINE_STALL_WINDOW_S = 120`, with the 1 CPU-second and 1 MiB thresholds and a 5 s sample period;
   - `DEFAULT_ENGINE_CONSTRUCTION_TIMEOUT_S = 600`;
   - `DEFAULT_ENGINE_UNBLOCK_GRACE_S = 30`.

   Measured legitimate constructions (2026-10-01 and 02): `qwen3-vl-8b-instruct` on jetson-thor1 (JP7, V1) 144 s with a cold compile cache and 72 s warm. The compile cache lives in the container layer (`/root/.cache/vllm`), so every deployment's first load is cold. `qwen2.5-vl-7b-instruct-awq` on the Orin (JP6, V0) 159 s and 164 s. The 600 s bound is about 3.7× the slowest of these. Task 10.4 re-checks the cold construction against both triggers. A hang then costs about 2 min 10 s instead of 1 h 45 min, and bound plus grace stays well below the component's 1500 s load request timeout and 1800 s Startup timeout, so one bounded failure plus the component's retry fit in one deployment.

### Out of Scope (future hardening)

- **The double load at deployment.** The reconciler's startup load is followed by the component's Shutdown unload and Startup load. Once Defect A is fixed it costs one extra load of about 2.5 min, not a restart.
- **Moving the construction off the Runtime_Server's loop**, so the runtime stays responsive during a normal 2.5 min load. That conflicts with vllm-model-reload-after-backend-restart's Decision 1: the engine binds to the loop it is built on.
- **Upgrading uvicorn.** From 0.29 it installs handlers with `signal.signal`. That is a dependency change across every image, and the fork hook fixes the class of bug whatever the server does.
- JP6's V0 in-process engine (no fork) and JP5 and amd64 (no vLLM), apart from the inertness smoke.

## Testing Strategy

### Validation Approach

First surface the counterexamples on unfixed code, then check the fix and preservation. The suite is `test/backend-test/vllm_jp7_engine_lifecycle/` (file names carry a `test_vjel_` prefix, because pytest imports these directories by basename). Defect A's cases run a subprocess harness and first run an unhooked control: where the control does not reproduce the bug (the build host's Python 3.14) they skip, saying so. Run them under Python 3.10 or 3.11: in the flask-app image, or in `python:3.11-slim` / `python:3.10-slim` with only pytest (`--noconftest`; `harness_support.py` is standard-library only).

### Exploratory Bug Condition Checking

- **A-1 (`test_vjel_exploration_fork_wakeup.py`).** `wakeup_harness.py`: the parent runs an asyncio loop with `add_signal_handler`, and a non-main thread starts a `multiprocessing` fork child (as the Runtime_Server's thread does) that installs a Python handler and reports through a pipe. The harness signals only the child and records whether the parent's handler ran. The `app` hook installs what `app.py` installs. FAILS on the unfixed tree under 3.11 (the module does not exist there), with the counterexample "SIGTERM sent only to a forked child ran the parent's asyncio SIGTERM handler".
- **A-2 (device, JP7).** Unload the vLLM model through port 8901 and assert that the backend's RestartCount and StartedAt are unchanged and no "Local server shutdown" line appears. Expected to FAIL today.
- **B-1 (`test_vjel_exploration_construction_hang.py`).** `EngineCoreLikeFactory` mimics vLLM's wait: it starts a child named like an engine core (`/proc/self/comm`) that sleeps forever (or spins), polls its liveness and raises only when it died. With a 1 s stall window inside a 2 s bound, `load` must return FAILED within the bound + 1 s. Unfixed, it blocks; the test drives the load on a worker thread and FAILS at the budget with "the construction never returned; the model stayed LOADING".
- **B-2 (device, JP7).** During a load, SIGSTOP the `VLLM::EngineCore`: a live but stuck engine core, H1's shape. Unfixed, the load never completes. Stop the experiment after a few minutes and SIGKILL the stopped process to recover.

### Fix Checking

- A-1 and A-2 pass. Property 1 is checked over the signal set, in the flask-app image (3.11) and `python:3.10-slim`.
- B-1 passes: the stall trigger for the idle child, the bound for the spinning one, the reason, the FAILED state, the served follow-up load and unload, and a diagnostics file naming the engine core (Property 3).
- **The H2 shape** (`test_vjel_manager_units.py`). `BlockingFactory` blocks on an Event without starting a process: the watchdog fires, has nothing to stop, and after the grace the manager reports FAILED while the construction is still blocked, logs CRITICAL, writes the Hang_Marker and calls the self-restart double. The late return then keeps FAILED and shuts the engine down.
- B-2 on the device with the production settings: FAILED at about the 120 s stall window, the diagnostics hold both stacks, the EngineCore is gone, and the component's next load reaches READY.
- Mutation checks: each of these fails a test: dropping the hook registration, selecting all children, dropping the create-time filter, leaving the timeout marker out of `_NO_OFFLINE_RETRY_TOKENS`, not setting FAILED after the grace, not killing, no stall check, and accepting a late return.

### Preservation Checking

- **Property 2** (`test_vjel_preservation_signals.py`): SIGTERM and SIGINT sent to the harness's parent itself still run its handler, and a `SIG_DFL` child still dies from SIGTERM. The edge-deploy-reliability shutdown suites stay green in the flask-app image.
- **Property 4** (`test_vjel_property_watchdog.py`, hypothesis): a construction that succeeds or fails before the watchdog fires gives the same status, reason, manager log lines and files with the watchdog enabled as with it disabled, and nothing is signalled.
- **Property 5** (same file, hypothesis): over generated process tables (names, comms, parent pids, start times), the selection is exactly the new engine-core children, and the killed set is exactly their trees.
- **Existing suites, unchanged:** the `vllm_*`, reconciler, `workflow_engine` and `deploy_reliability` suites at their baseline counts (the same five pre-existing failures on the host as on the unfixed tree), the security preservation suite and its guards, and the six security audits.

### Unit Tests

- `fork_signal_hygiene`: an idempotent install with one registration, a no-op without `os.register_at_fork`, and the child-side reset tolerating `ValueError` and `OSError`.
- The watchdog (`test_vjel_watchdog_units.py`, through its seams): cancel before firing does nothing; the stall and bound triggers; CPU, I/O and engine-core progress are not a stall, while progress outside the construction does not count; only the construction's engine-core trees are killed, descendants first; the grace with and without a return; the diagnostics contents, the py-spy fallback, the retention of 5, and a failing dumper not stopping the recovery; the `/proc` parsers.
- `manager` (`test_vjel_manager_units.py`): the token, the H2 shape, the late return, the Hang_Marker round trip (FAILED once with one WARNING, the reconciler skip, the corrupt-marker case, the explicit-load clear, the tombstone precedence), no offline retry after a timeout while an ordinary failure still retries, and the settings from the environment.
- The persistent log (`test_vjel_engine_log.py`): an idempotent attach, no directory and an unwritable one, a forked child's records landing in the same file, and the default factory attaching it after `import vllm` and before the construction.

### Property-Based Tests

Properties 4, 5 and 6 use hypothesis at the project profile. Property 1 runs over the signal set in the subprocess harness.

### Integration Tests (on device)

1. JP7: A-2 passes. An unload leaves the backend running and the continuous workflows uninterrupted.
2. JP7: a LocalServer redeploy with a staged vLLM model gives exactly one backend restart, the deployment's own.
3. JP7: B-2 with the production settings, as above.
4. JP7: a construction with a cold compile cache (a fresh container) never trips the stall check, and its duration is recorded against the 600 s bound. `vllm-engine.log` holds the backend's and the EngineCore's records.
5. JP7: a 30-minute soak with the continuous workflows and a few load and unload cycles. No restart, no failed run.
6. JP6: a load reaches READY under the watchdog (V0, in-process) without tripping it. JP6, JP5 and amd64: the backend starts, a docker stop is graceful, and a continuous workflow runs. The hook is inert there.

## Rollout

Builds run one at a time, with the builds.md pre-build checks. JP7 comes first, because only there are both fixes active. JP6, JP5 and amd64 follow, because `app.py` changes on every architecture. On-device verification comes before the commit, for every architecture the change touches.
