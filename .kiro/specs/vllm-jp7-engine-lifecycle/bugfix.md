# Bugfix Requirements Document

## Introduction

Two defects in the JP7 LocalServer's vLLM runtime. Both were found during the rtsp-rtmp-stream-cameras hardware verification, where they are recorded as findings 19 and 20 (that spec's tasks.md, 25.3 and 28.5). Neither is caused by that spec, and both predate it.

- **Defect A (finding 20, cause confirmed and reproduced).** Stopping a vLLM engine restarts the whole LocalServer backend.
- **Defect B (finding 19, cause unknown).** A vLLM engine construction can hang without a bound. The vLLM runtime then answers nothing, the model component goes BROKEN after three startup timeouts, and Greengrass rolls back the entire deployment.

They meet in one place: every JP7 deployment that includes a staged vLLM model. At the new backend's start the reconciler re-drives the staged model's load, and the model component's Shutdown unload queues behind it. Defect B can hang that load. When it completes, Defect A restarts the backend a second time as soon as the queued unload runs.

### Incident record A (jetson-thor1, 2026-10-02, LocalServer arm64JP7 `1.0.52`, deployment `cecbee46`)

- 01:55:18Z: the vLLM reconciler re-drove the load of the staged `qwen3-vl-8b-instruct` at backend start. At 01:55:20Z the model component's Shutdown script sent an unload. The unload waited, because the engine construction runs synchronously on the vLLM runtime server's event loop.
- 01:57:41.98Z: the model was READY, and the queued unload ran. vLLM's engine shutdown sent SIGTERM to its forked EngineCore process.
- 01:57:42.12Z: the backend's main server began a graceful shutdown ("Cleaning up digital input workflows"). At 01:57:43Z it logged "Local server shutdown complete; exiting." and exited 0.
- `docker events` show `die exitCode=0` with no `kill` event. Docker's restart policy restarted the container (RestartCount 1). The workflows waited for their models and ran again two minutes later.
- Reproduced in the JP7 backend image (Python 3.11.16), without vLLM. A SIGTERM sent only to a `multiprocessing` fork child runs the parent's asyncio SIGTERM handler: `~/rtsp-verify/wakeup_fd_fork_repro.py` prints `PARENT-GOT-SIGTERM`. On Python 3.14 it does not.
- The mechanism: `loop.add_signal_handler` sets the process's signal wakeup fd to the event loop's self-pipe. A forked child inherits that fd. When a signal trips a Python-level handler in the child (vLLM's `run_engine_core` installs one for SIGTERM and SIGINT), CPython writes the signal number to the wakeup fd. The parent's loop reads it and runs its own SIGTERM handler. uvicorn 0.23.2, the backend's main server in `app.py`, registers its `handle_exit` exactly that way.
- With `os.register_at_fork(after_in_child=lambda: signal.set_wakeup_fd(-1))` installed in the parent, the same reproduction no longer reaches the parent, and the child still exits cleanly on SIGTERM (`wakeup_fd_fork_repro.py --fix`).

### Incident record B (jetson-thor1, 2026-10-01, deployment `e8c4694a`, arm64JP7 `1.0.51` → `1.0.52`)

- 11:46:04Z: the reconciler re-drove the staged model's load at backend start. "Loading vLLM model 'qwen3-vl-8b-instruct'" was logged, and `AsyncLLMEngine.from_engine_args` never returned.
- 11:46:07Z: the component Shutdown's unload queued behind it and timed out after 300 s. Every later load request timed out after 1500 s.
- After three 30-minute Startup timeouts the component went BROKEN at 13:31Z, and the deployment rolled back (`FAILED_ROLLBACK_COMPLETE`).
- The engine core writes to the container's stdout. The rollback recreated the container, so that output is gone, and `application.log` stops at "Loading vLLM model".
- The same build deployed cleanly on 2026-10-02 (`cecbee46`), with READY in 2 min 24 s and the same concurrency: Triton loaded 8 ONNX models while the engine core forked.
- vLLM 0.11's `wait_for_engine_startup` polls every 10 s and fails only when an engine process has exited. Nothing bounds the wait.

## Bug Analysis

### Bug Condition

- **C_A(x):** the backend process runs Python 3.11 or older, has a main-thread asyncio loop with a handler registered through `add_signal_handler`, and a fork-without-exec child receives a signal for which the child installed a Python-level handler. On JP7 this holds for every vLLM engine shutdown: a component Shutdown unload, a model stop, a failed load's cleanup, and the reconciler's KV-cache recovery unload.
- **C_B(x):** a vLLM engine construction that does not complete. The engine core process may be alive but stuck before its handshake, or the backend may be stuck inside the construction before or after the fork. Which one is still unknown (design, Hypothesized Root Cause).

### Current Behavior (Defect)

1.1 WHEN a vLLM model on a JP7 device is unloaded, by any path in C_A, THEN the system shuts the whole LocalServer backend down gracefully (exit 0), and docker restarts it. Every workflow is interrupted, and every Triton model is unloaded.

1.2 WHEN a JP7 LocalServer deployment includes a staged vLLM model THEN the backend restarts a second time, when the reconciler's startup load completes and the queued unload runs.

1.3 WHEN a vLLM engine construction does not complete THEN the system waits forever. The model stays LOADING, and the vLLM runtime server answers no request (load, unload, index or generate), because the construction blocks its event loop.

1.4 WHEN the model component's Startup load request times out three times THEN the component goes BROKEN, and Greengrass rolls back the whole deployment, LocalServer included.

1.5 WHEN the backend container is recreated (a rollback or a redeploy) THEN the engine core's log output is lost, so a hung construction cannot be diagnosed afterwards.

### Expected Behavior (Correct)

2.1 WHEN a vLLM engine shuts down, by any path in 1.1, THEN the system SHALL stop only the engine's own processes. The backend process SHALL keep running, with no exit and no container restart, and other models and workflows SHALL be unaffected.

2.2 WHEN a forked child of the backend receives any signal THEN the system SHALL NOT run the backend's own signal handlers for it.

2.3 WHEN a vLLM engine construction has made no progress over the Stall_Window, or has not completed within the Construction_Bound, THEN the system SHALL capture diagnostics (the Python stacks of every backend thread, and of the engine core process when one exists), stop the stuck construction's engine processes, and mark the load FAILED with a reason that names the trigger. A later load request SHALL be able to try again.

2.4 WHEN a construction is stopped under 2.3 THEN the vLLM runtime server SHALL answer requests again (unload, index, generate and a new load) without a backend restart. IF the construction cannot be unblocked (there is no engine process to stop, or the construction still has not returned within the Unblock_Grace) THEN the system SHALL report the model FAILED, log the condition as CRITICAL with the diagnostics, and recover the runtime as the design decides (design Decision 3).

2.5 WHEN the vLLM runtime constructs, serves or shuts down an engine THEN the system SHALL keep the vLLM log output of the backend and of the engine core in size-bounded files on the device's persistent storage, where it survives a container recreation.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the backend itself receives SIGTERM or SIGINT (docker stop, compose down, a deployment) THEN the system SHALL CONTINUE TO shut down gracefully within the compose stop grace period, with its bounded cleanup and exit code 0.

3.2 WHEN a vLLM load completes within the Construction_Bound THEN the system SHALL CONTINUE TO produce the current READY or FAILED result, logs, Starvation_Latch, tombstone and reconciler behavior.

3.3 WHEN a vLLM model is unloaded THEN the system SHALL CONTINUE TO free its engine (the engine core exits and its GPU memory returns), write the Unload_Tombstone where it does today, and return the same HTTP response.

3.4 WHEN forked children handle their own signals (the vLLM engine core's SIGTERM and SIGINT handlers, the digital-input processes, multiprocessing workers) THEN they SHALL CONTINUE TO receive and handle them as today.

3.5 WHEN Triton vision models, the stream workers and the continuous runners operate THEN they SHALL CONTINUE TO behave identically.

3.6 WHEN LocalServer runs on an image without vLLM (JP5, amd64) or with JP6's in-process V0 engine (vLLM 0.9.3, `VLLM_USE_V1=0`) THEN the system SHALL CONTINUE TO behave identically, apart from the inert fork hook.

3.7 WHEN a vLLM model's construction legitimately takes long (a first load with a cold compile cache, a large model) THEN the system SHALL CONTINUE TO let it finish. The Construction_Bound SHALL exceed the slowest legitimate construction measured on the device, and a construction that keeps making progress SHALL never trip the stall check.

3.8 WHEN the model component's lifecycle scripts run (`vllm_model_prep.py` load and `--cleanup`) THEN they SHALL CONTINUE TO use their current requests, retries and timeouts, and the recipe's Startup (1800 s) and Shutdown (900 s) timeouts SHALL be unchanged.
