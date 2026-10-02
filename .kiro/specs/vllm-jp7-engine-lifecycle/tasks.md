# Implementation Plan

## Overview

The plan fixes Defect A (an engine shutdown restarts the backend) and Defect B (an unbounded engine construction), in bugfix order:

1. exploration tests that fail on the unfixed code
2. preservation tests
3. the fixes
4. fix checking
5. the gates
6. builds and hardware verification
7. the commit

bugfix.md and design.md hold the incidents and the decisions. They are findings 20 and 19 in the rtsp-rtmp-stream-cameras spec.

Conventions on the build host (the `build-host-test-tooling` memory):

- Defect A's tests need Python 3.10 or 3.11. Run them in the flask-app image (JP7 3.11, JP6 3.10) or in `python:3.11-slim`. The host venvs run Python 3.14, where the bug does not reproduce.
- Use `~/.venvs/dda-edge-tests/bin/python` for the vllm_runtime host suites, with `PYTHONPATH=src/backend:test/backend-test`.
- After editing backend files, compile them under Python 3.10 and 3.11 in the slim images.
- These are on-device changes. Per builds.md they are committed only after the hardware verification (tasks 10–12).

## Task Dependency Graph

```
1 (exploration) ─┐
2 (preservation) ┴─> 3 (fix A) ──┐
                     4 (fix B) ──┼─> 6 (fix checking) -> 7 (gates) -> 8 (checkpoint)
                     5 (log) ────┘                                     │
                                                       9 (builds) <────┘
                                                       10 (JP7 device) -> 11 (JP6, JP5, amd64) -> 12 (commit)
```

## Tasks

- [ ] 1. Write the bug condition exploration tests (they must FAIL on the unfixed code)
  - [ ] 1.1 A-1: `test/backend-test/vllm_runtime/test_fork_signal_wakeup_bug.py`
    - A subprocess harness shaped like `~/rtsp-verify/wakeup_fd_fork_repro.py`:
      - the parent runs an asyncio loop with `add_signal_handler(SIGTERM)`;
      - a non-main thread starts a `multiprocessing` fork child that installs a Python SIGTERM handler;
      - the harness signals only the child.
    - Assert that the parent's handler does not run.
    - It skips on Python 3.12 and later, with a message saying the bug does not reproduce there.
    - Run it in the flask-app image and record that it FAILS.
    - _Requirements: 1.1, 1.2, 2.1, 2.2_
  - [ ] 1.2 B-1: `test/backend-test/vllm_runtime/test_engine_construction_hang_bug.py`
    - Use a fake engine factory that mimics vLLM's `wait_for_engine_startup`: it forks a child that sleeps forever, polls the child's liveness every 0.1 s, and raises only when the child has died.
    - With a 2 s test bound, assert that `load` returns FAILED within the bound + 1 s.
    - Drive the load on a worker thread, so the unfixed hang fails the test at bound + margin instead of hanging the suite. Record that it FAILS.
    - _Requirements: 1.3, 2.3_

- [ ] 2. Write the preservation tests before implementing the fix
  - [ ] 2.1 Property 2: SIGTERM to the backend process itself still exits through the graceful path inside the stop grace.
    - Extend or reuse the edge-deploy-reliability shutdown tests, in the flask-app image.
    - _Requirements: 3.1_
  - [ ] 2.2 Property 4 (hypothesis): constructions shorter than the bound give the same results, states, retained reasons, Starvation_Latch effects and log lines with and without the watchdog, over the existing fake-engine fixtures.
    - _Requirements: 3.2, 3.3_
  - [ ] 2.3 Child signal handling: a fork child's own SIGTERM and SIGINT handlers still run, and a child left at `SIG_DFL` still dies from the signal.
    - _Requirements: 3.4_
  - [ ] 2.4 Run the existing `vllm_*` and reconciler suites and record their baseline counts.
    - _Requirements: 3.2, 3.5_

- [ ] 3. Fix A: forked children do not share the backend's signal wakeup fd
  - Add `src/backend/utils/fork_signal_hygiene.py`, with `install()` and the `after_in_child` reset (design, change 1).
  - Call `fork_signal_hygiene.install()` as the first statement of `app.py`'s `__main__` block, before `TritonEdgeClient.get_instance()` and before any thread starts.
  - Unit tests: the install is idempotent, and the child-side reset tolerates `ValueError`.
  - _Requirements: 2.1, 2.2, 3.1, 3.4, 3.6_

- [ ] 4. Fix B: bound every engine construction
  - [ ] 4.1 Add `src/backend/vllm_runtime/construction_watchdog.py` (design, change 2).
    - The timer thread, with injectable clock, killer and process lister.
    - EngineCore selection by process title or cmdline and create time (Property 5).
    - The diagnostics file: faulthandler for every backend thread, plus py-spy, or the `/proc` fallback, for each selected EngineCore. Keep at most 5 files.
    - A SIGKILL of each selected process tree.
    - The timeout reason, carrying `ENGINE_CONSTRUCTION_TIMEOUT_MARKER`.
    - _Requirements: 2.3_
  - [ ] 4.2 Wire the watchdog into `VllmRuntimeManager._construct_engine` (design, change 3).
    - Raise `ConstructionTimeout` once the watchdog has fired.
    - Add `ENGINE_CONSTRUCTION_TIMEOUT_MARKER` to `_NO_OFFLINE_RETRY_TOKENS`.
    - Unblock grace: after the kill, or when no EngineCore existed, set the entry FAILED under the lock, and log CRITICAL.
    - A late return shuts the new engine down and keeps FAILED.
    - Add the constants to `vllm_runtime/constants.py`, with their environment overrides (design, change 6).
    - _Requirements: 2.3, 2.4, 3.2, 3.7_
  - [ ] 4.3 Decision 3: implement the option the owner confirmed at task 8. Until then, keep it behind a constant.
    - Option (a): the Hang_Marker plus SIGTERM to the backend's own pid; the reconciler skips a marked model (with one WARNING); an explicit load clears the marker, like the Unload_Tombstone.
    - Option (b): stay blocked with the model FAILED and a CRITICAL log.
    - _Requirements: 2.4_

- [ ] 5. Keep vLLM's log on persistent storage (design, change 5)
  - When `VLLM_AVAILABLE`, `app.py` writes `$COMPONENT_WORK_PATH/vllm_logging.json` and sets `VLLM_LOGGING_CONFIG_PATH` and `VLLM_CONFIGURE_LOGGING=1`, before anything imports `vllm`.
  - The config keeps vLLM's stdout handler and adds a `RotatingFileHandler` on `$COMPONENT_WORK_PATH/logs/vllm-engine.log`, 10 MB × 3.
  - Tests:
    - the config file's content;
    - the environment is set before the first `vllm` import;
    - a record from a forked child lands in the file;
    - the size cap holds (Property 6).
  - _Requirements: 1.5, 2.5_

- [ ] 6. Fix checking
  - A-1 passes in the flask-app image, under both Python 3.10 and 3.11. Property 1 is checked over SIGTERM, SIGINT, SIGHUP, SIGUSR1 and SIGCHLD.
  - B-1 passes. Property 3: the reason, the FAILED state, the next unload, index and load requests are served, and a diagnostics file exists.
  - The H2 shape passes: a factory that blocks without forking leads to FAILED after the grace, then Decision 3, with test doubles for the SIGTERM and the marker.
  - Property 5 (hypothesis) over generated process tables.
  - Mutation-check the new code. Each of these must fail a test:
    - dropping the hook registration;
    - selecting all children instead of EngineCore ones;
    - dropping the create-time filter;
    - leaving the timeout marker out of `_NO_OFFLINE_RETRY_TOKENS`;
    - not setting FAILED after the grace.
  - _Requirements: 2.1–2.5_

- [ ] 7. Gates
  - Every `vllm_*`, reconciler and `workflow_engine` host suite at its baseline counts plus the new tests.
  - The security preservation suite and its two guards.
  - Compile under Python 3.10 and 3.11 in the slim images.
  - The `app.py`-importing tests in the flask-app image.
  - No preservation-tracked file changes: no Dockerfile, `requirements.txt`, compose file or recipe. If one does, rebaseline it per builds.md.
  - _Requirements: 3.1–3.8_

- [ ] 8. Checkpoint: owner decisions before the builds
  - Decision 3: option (a), recommended, or (b).
  - The 900 s Construction_Bound default. It is confirmed again by task 10.4's measurement.

- [ ] 9. USER ACTION: builds, one at a time, with the builds.md pre-build checks
  - Snapshot the tree onto a `wip/vllm-jp7-engine-lifecycle-verify` branch (the build system builds only pushed refs). The temp Cognito user needs the owner's OK.
  - Build JP7 first, then JP6, then JP5 and amd64. `app.py` changes on every architecture.
  - Before each build:
    - check that no build is running;
    - run the guard pair;
    - move `cdk.out` aside;
    - make sure no Portal deploy is in progress.

- [ ] 10. USER ACTION: JP7 verification on jetson-thor1 (design, Integration Tests)
  - [ ] 10.1 A-2: unload the vLLM model through port 8901. RestartCount and StartedAt must be unchanged, with no "Local server shutdown" line, and the continuous workflows uninterrupted.
  - [ ] 10.2 Redeploy LocalServer with the staged vLLM model. There must be exactly one backend restart, the deployment's own.
  - [ ] 10.3 B-2, with `VLLM_ENGINE_CONSTRUCTION_TIMEOUT_S=120`:
    - SIGSTOP the `VLLM::EngineCore` during a load;
    - the load is FAILED at about 120 s;
    - the diagnostics file holds both stacks;
    - the EngineCore is gone;
    - the component's next load reaches READY.
  - [ ] 10.4 Measure a cold-compile-cache construction (a fresh container), and record its margin against the 900 s default.
  - [ ] 10.5 A 30-minute soak with the continuous workflows and a few load and unload cycles: no restart and no failed run.
  - [ ] 10.6 If a natural hang (Defect B) recurs, keep its diagnostics file and `vllm-engine.log`, and record which hypothesis (H1, H2 or H3) they support.

- [ ] 11. USER ACTION: JP6, JP5 and amd64 smoke
  - The backend starts, `docker stop` is graceful, and a continuous workflow runs. The hook is inert on these images, and the watchdog never runs on JP5 or amd64.

- [ ] 12. USER ACTION: commit and integrate
  - Commit on `spec/vllm-jp7-engine-lifecycle`, naming the verified devices.
  - Fast-forward `integration/all-specs`, then delete the wip branch.
  - Mark findings 19 and 20 in the rtsp-rtmp-stream-cameras tasks.md as fixed by this spec, or record what remains open.

## Notes

- **Reproductions** (on the build host, `~/rtsp-verify/`):
  - `wakeup_fd_fork_repro.py`, and `--fix` for the validated hook. Run it with `docker run -i --rm --entrypoint python3 flask-app:latest - < wakeup_fd_fork_repro.py` on thor1.
  - `capture_backend_logs.sh` keeps the next backend container's stdout on the device until task 5 ships.
- **Device access, builds and deploys:** the `lab-test-devices`, `component-build-deploy` and `jp7-vllm-unload-restart` memories.
