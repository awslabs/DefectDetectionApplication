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

- Defect A's tests need Python 3.10 or 3.11. Run them in the flask-app image (JP7 3.11, JP6 3.10), mounted at `/w/dda` rather than `/repo` (the repo-root `__init__.py` makes pytest put `/` first on `sys.path`, where the image's own backend copy shadows the tree), or in `python:3.11-slim` with `--noconftest`. The host venvs run Python 3.14, where the bug does not reproduce.
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

## Resume Here

- **State (2026-10-02).** Tasks 1–8 are done in the worktree `~/github/dda-vllm-lifecycle` (branch `spec/vllm-jp7-engine-lifecycle`), uncommitted. The same files are snapshotted on `wip/vllm-jp7-engine-lifecycle-verify` (`f69d9f5`, pushed) for the build system.
- **Owner decisions (task 8):** Decision 3 is option (a). The bound: "ok but I would like faster if possible". So the watchdog also fails a construction that makes no progress for 120 s, the hard bound is 600 s, and the grace is 30 s (design, change 6).
- **Builds:** submitted with the temp Cognito user `kiro-rtsp-build-temp` (owner-approved). Delete it when every build is done.
  - JP7 `1.0.53`: job `61237b71-2135-462f-9e02-4a53152708ac`, from `f69d9f5`. Every in-image gate passed. It waited from 18:09Z until 19:21Z, because the build servers were offline (next bullet).
  - JP6: job `4b8acea8-bab0-45c8-8c05-eabef686d1c6`, from `f69d9f5`, submitted at 20:53Z.
- **Build servers (2026-10-01/02).** Both arm64 build servers lost SSM when an org StackSet added SSM and EC2 interface endpoints without subnets, but with private DNS, to BuildVpc. A reboot does not help. Each server's user data now carries a boot-time DNS workaround, applied with the owner's OK on 2026-10-02 at 19:17Z. The `build-vpc-dns-blackhole` memory has the details. The proper fix, a subnet on the endpoints, needs the owner's decision.
- **Verification (task 10):** done on thor1 for the hot-patch and for the real `1.0.53`, except the real build's soak (10.5), which runs from 21:04Z to 21:34Z.
- **Next:** finish 10.5, then JP6 on the Orin, then the JP5 and amd64 builds (task 11), then the commit (task 12).

## Tasks

- [x] 1. Write the bug condition exploration tests (they must FAIL on the unfixed code)
  - [x] 1.1 A-1: `test/backend-test/vllm_jp7_engine_lifecycle/test_vjel_exploration_fork_wakeup.py`, over `wakeup_harness.py`.
    - OUTCOME: on the unfixed tree under Python 3.11 (`python:3.11-slim`), A-1, the five Property 1 cases and the `app.py` wiring check FAIL: "SIGTERM sent only to a forked child ran the parent's asyncio SIGTERM handler (hook installed: False)". On the host's Python 3.14 they skip: the unhooked control does not reproduce there.
    - _Requirements: 1.1, 1.2, 2.1, 2.2_
  - [x] 1.2 B-1: `test/backend-test/vllm_jp7_engine_lifecycle/test_vjel_exploration_construction_hang.py`.
    - OUTCOME: on the unfixed tree all three cases FAIL at the 5 s budget: "the construction never returned; the model stayed LOADING".
    - _Requirements: 1.3, 2.3_

- [x] 2. Write the preservation tests before implementing the fix
  - [x] 2.1 Property 2: `test_vjel_preservation_signals.py` (SIGTERM and SIGINT to the process itself still run its handler). The edge-deploy-reliability suite (`deploy_reliability/`, 72 tests) stays green in the flask-app image.
    - _Requirements: 3.1_
  - [x] 2.2 Property 4 (hypothesis): `test_vjel_property_watchdog.py::test_property4_fast_constructions_behave_identically`.
    - _Requirements: 3.2, 3.3_
  - [x] 2.3 Child signal handling: A-1 asserts the child's own handler ran; `test_sig_dfl_child_still_dies_from_sigterm`.
    - _Requirements: 3.4_
  - [x] 2.4 Baseline counts on the unfixed tree (host, Python 3.14): the `vllm_*` suites (`vllm_runtime`, `vllm_runtime_tests`, `vllm_model_reload`, `vllm_jp7_engine_cuda_init`, `jp6_vllm_kv_cache_oom`, `vllm_latency`, `vllm_hf_cache`, with `edge-cv-portal/backend` on the path) 329 passed, 3 skipped, 5 failed. The five are pre-existing: two stale `vllm_model_prep.py` hash pins, two multi-process stage-lock tests, and `test_workflow_llm_binding_poll_loop_rides_through_reload_window`. `workflow_engine` + `deploy_reliability`: 1955 passed, 9 skipped.
    - _Requirements: 3.2, 3.5_

- [x] 3. Fix A: forked children do not share the backend's signal wakeup fd
  - `src/backend/utils/fork_signal_hygiene.py`; `app.py`'s `__main__` block starts with `from utils import fork_signal_hygiene` and `fork_signal_hygiene.install()`.
  - `app.py`'s hash is pinned by `security/baselines/iam_out_of_scope_baseline.json`: rebaselined to `fc30876c…`.
  - _Requirements: 2.1, 2.2, 3.1, 3.4, 3.6_

- [x] 4. Fix B: bound every engine construction
  - [x] 4.1 `src/backend/vllm_runtime/construction_watchdog.py` (design, change 2): the stall and bound triggers, `/proc`-based (no psutil), engine-core selection by cmdline or comm and boot-tick start time, the diagnostics file (faulthandler, `py-spy dump --nonblocking`, `/proc`), keep 5, SIGKILL of the selected trees, the reason with `ENGINE_CONSTRUCTION_TIMEOUT_MARKER`.
    - _Requirements: 2.3_
  - [x] 4.2 `VllmRuntimeManager._construct_engine` (design, change 3): `ConstructionTimeout`, the marker in `FAILURE_CATEGORY_TOKENS` and `_NO_OFFLINE_RETRY_TOKENS`, the unblock grace, the late return, the constants and their environment overrides.
    - _Requirements: 2.3, 2.4, 3.2, 3.7_
  - [x] 4.3 Decision 3, option (a) (owner, 2026-10-02): the Hang_Marker, disk-derived FAILED with one WARNING per backend life, cleared by an explicit load; SIGTERM to the backend's own pid with a forced exit after 90 s. The reconciler is NOT changed: its source hash is pinned by jp6-vllm-kv-cache-oom-regression's preservation suite, and it already re-drives STAGED models only.
    - _Requirements: 2.4_

- [x] 5. Keep vLLM's log on persistent storage (design, change 5)
  - `src/backend/vllm_runtime/engine_log.py`, attached by `_default_engine_factory` after `import vllm`. Changed from the first design (`VLLM_LOGGING_CONFIG_PATH`): see change 5 for why.
  - _Requirements: 1.5, 2.5_

- [x] 6. Fix checking
  - Host: the new suite passes (55 passed, 6 skipped: Defect A on Python 3.14). flask-app image (Python 3.11.9, mounted at `/w/dda`): 61 passed. `python:3.10-slim` and `python:3.11-slim` (`--noconftest`): the 15 signal cases pass.
  - Mutation checks: all eight mutations listed in the design fail a test.
  - _Requirements: 2.1–2.5_

- [x] 7. Gates
  - The `vllm_*` suites: 329 passed, 3 skipped, the same 5 pre-existing failures. `workflow_engine` + `deploy_reliability`: 1955 passed, 9 skipped. Both identical to the baseline.
  - The security preservation suite: 146 passed, 7 skipped (host); 138 passed, 8 skipped (flask-app). The guard pair: 4 passed, 3 skipped. The six audits: 0 disallowed hits each. The exploration suites: as on the unfixed tree (two host-only `aws s3` CLI cases fail on both).
  - Compiles under Python 3.10.21 and 3.11.16. No Dockerfile, `requirements.txt`, compose file or recipe changed.
  - _Requirements: 3.1–3.8_

- [x] 8. Checkpoint: owner decisions before the builds
  - 2026-10-02: Decision 3 (a); the bound accepted, faster if possible (see Resume Here).

- [ ] 9. USER ACTION: builds, one at a time, with the builds.md pre-build checks
  - [x] 9.1 JP7 from `wip/vllm-jp7-engine-lifecycle-verify`: job `61237b71-2135-462f-9e02-4a53152708ac`.
    - OUTCOME: `aws.edgeml.dda.LocalServer.arm64JP7` `1.0.53`, from `f69d9f5`. Every in-image gate passed: the backend unit tests, the stream camera components gate, and the security gates with the rebaselined `app.py` hash. The build took about 22 minutes, because the onnxruntime and vLLM layers came from the server's cache.
  - [ ] 9.2 JP6: job `4b8acea8-bab0-45c8-8c05-eabef686d1c6`, started before 10.5 finished. The hot-patch checks had already passed, and a cached rebuild is cheap if 10.5 forces a change.
  - [ ] 9.3 JP5 and amd64 (amd64 over SSM on the x86 build host; see the `amd64-build-host` memory).
  - Before each build: no build running (local `pgrep` and the jobs table), the guard pair, `cdk.out` aside, no Portal deploy in progress.

- [ ] 10. USER ACTION: JP7 verification on jetson-thor1 (design, Integration Tests)
  - **Hot-patch first (2026-10-02, 18:23–19:35Z).** The six changed backend files were copied into the running `1.0.52` container, then restarted. The originals are in `/tmp/vjel-orig` on thor1.
    - A-2 on the unfixed `1.0.52`: the unload restarted the backend (RestartCount 1 → 2, "Local server shutdown complete" 0.1 s later). This reproduces Defect A on the device.
    - A-2 with the fix: no restart, `/health` 200.
    - B-2: FAILED 135 s after the load started, with the stall reason. The diagnostics hold 34 backend thread stacks and the engine core's py-spy stack, which was in weight loading.
    - Cold compile cache (`/root/.cache/vllm`, the Triton and inductor caches removed): READY in 129 s, no trigger.
    - The deployment sequence, emulated: a restart, then an unload queued behind the reconciler's load. The unload waited 77 s, ran after READY, and nothing restarted.
    - 53 minutes (18:41–19:35Z) with three unload and load cycles: 28,202 continuous runs, none failed, no restart. Backend RSS 1,371 → 1,374 MB.
  - **Real `1.0.53` (2026-10-02, 20:54Z on).** Deployment `7e20521e-9146-403e-8e99-115cd7186ba0` revised `cecbee46` with only the LocalServer version changed. The previous revision is saved as `~/rtsp-verify/results/deployments/jetson-thor1-rev113-cecbee46-01f1-4c3f-99c7-2261d7076ef0.json`.
  - [x] 10.1 A-2: unload the vLLM model through port 8901. RestartCount and StartedAt must be unchanged, with no "Local server shutdown" line, and the continuous workflows uninterrupted.
    - OUTCOME (`1.0.53`): the deployment's queued unload at 20:57:10Z and B-2's explicit unload at 21:00Z left the backend running (StartedAt 20:54:41Z, RestartCount 0). The soak (10.5) repeats it three times.
  - [x] 10.2 Redeploy LocalServer with the staged vLLM model. There must be exactly one backend restart, the deployment's own.
    - OUTCOME: exactly one, the deployment's own. `docker events`: kill, stop, die (exit 0) and destroy at 20:54:37–39Z, create and start at 20:54:41–42Z, and nothing after that.
      - The reconciler's load started at 20:54:46Z and was READY at 20:57:09Z.
      - The model component's Shutdown unload ran at 20:57:10Z, with no restart. Its Startup load was READY at 20:58:18Z.
      - The deployment COMPLETED at 20:58:35Z. `1.0.52` had restarted the backend a second time at this point (incident record A).
  - [x] 10.3 B-2 with the production settings:
    - SIGSTOP the `VLLM::EngineCore` during a load;
    - the load is FAILED about 120 s later (the stall trigger);
    - the diagnostics file holds both stacks;
    - the EngineCore is gone;
    - the component's next load reaches READY.
    - OUTCOME (`1.0.53`): SIGSTOP 15 s into the load. FAILED 136 s after the load started, with "made no progress for 120 s … stopped 1 engine core process(es)". The diagnostics (`vllm-construction-timeout-qwen3-vl-8b-instruct-20261002T210249.816958Z.txt`) hold 34 backend thread stacks and the engine core's py-spy stack. No engine core was left and the backend did not restart. The next load reached READY.
  - [x] 10.4 A cold-compile-cache construction (the first load in a fresh container) does not trip the stall check; record its duration against the 600 s bound, and check `vllm-engine.log` holds the backend's and the EngineCore's records.
    - OUTCOME (`1.0.53`): the reconciler's load in the deployment's fresh container (a cold compile cache) took 143 s, with no trigger and no diagnostics file. That is 4.2× below the 600 s bound and its CPU never stalled. `vllm-engine.log` holds records from the backend (pid 1) and from each engine core (pids 1532, 2533, 2774, 2978), and survived the container recreation.
  - [ ] 10.5 A 30-minute soak with the continuous workflows and a few load and unload cycles: no restart and no failed run.
  - [ ] 10.6 If a natural hang (Defect B) recurs, keep its diagnostics file and `vllm-engine.log`, and record which hypothesis (H1, H2 or H3) they support.

- [ ] 11. USER ACTION: JP6, JP5 and amd64 smoke
  - JP6 (Orin): a vLLM load (V0, in-process) reaches READY under the watchdog without tripping it.
  - JP6, JP5 and amd64: the backend starts, `docker stop` is graceful, and a continuous workflow runs. The hook is inert on these images, and the watchdog never runs on JP5 or amd64.

- [ ] 12. USER ACTION: commit and integrate
  - Commit on `spec/vllm-jp7-engine-lifecycle`, naming the verified devices.
  - Fast-forward `integration/all-specs`, then delete the wip branch.
  - Mark findings 19 and 20 in the rtsp-rtmp-stream-cameras tasks.md as fixed by this spec, or record what remains open.

## Notes

- **Reproductions** (on the build host, `~/rtsp-verify/`):
  - `wakeup_fd_fork_repro.py`, and `--fix` for the validated hook. Run it with `docker run -i --rm --entrypoint python3 flask-app:latest - < wakeup_fd_fork_repro.py` on thor1.
  - `capture_backend_logs.sh` keeps the next backend container's stdout on the device until task 5 ships.
- **Device access, builds and deploys:** the `lab-test-devices`, `component-build-deploy` and `jp7-vllm-unload-restart` memories.
