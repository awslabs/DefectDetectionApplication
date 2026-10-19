# Bugfix Requirements Document

## Introduction

`triton-model-cleanup-wedge` (F2) backported python_backend r25.08's bounded
stub start onto the Triton 2.45.0 builds (JP5, JP6, x86), so that a python
model whose stub exits before its first message fails within 30 s as
`UNAVAILABLE` (that spec's criterion 2.4). On the MIC-730 (JP5, LocalServer
arm64JP5 1.0.56, Triton 2.45.0 with python_backend r24.04 plus the patch), the
stub start is bounded, but the load does not fail: Triton launches the stub
again about every 10 s inside the same load call, the model stays `LOADING`,
and the single model-load thread stays busy, so other models cannot load.
That spec records 2.4 as partially met; this spec carries the rest.

Today's mitigations: the load watchdog reports such a load as `stalled` after
15 min and self-heal (on by default) restarts the backend; restoring the
missing file lets the same load reach `READY`.

## Evidence (MIC-730, JP5, 2026-10-16/17)

Reproduction (R3 of `triton-model-cleanup-wedge`): copy a working model's
three repository entries (`base_`, `marshal_`, ensemble) under a scratch name,
rename the model names inside each `config.pbtxt`, remove
`base_<scratch>/<v>/model.py`, then request `/start` for the ensemble.

- The image carries the patched `libtriton_python.so`: both sentinel strings
  (`Waiting for the stub health monitoring thread to start`,
  `Waiting for the stub process initialization response`) are present in
  `/opt/tritonserver/backends/python/libtriton_python.so`.
- Run 1: `/start` at 23:49:20Z. The backend log has 43 lines like the one
  below in 7.5 min, about 10 s apart, each from a new stub process id:

  ```
  I1016 23:49:32.871032 1178 pb_stub.cc:2081]  Failed to preinitialize Python stub: Python model file not found in '/aws_dda/dda_triton/triton_model_repo/base_model-r3-probe/8/model.py'
  I1016 23:49:42.908856 1216 pb_stub.cc:2081]  Failed to preinitialize Python stub: Python model file not found in '/aws_dda/dda_triton/triton_model_repo/base_model-r3-probe/8/model.py'
  I1016 23:49:52.946813 1254 pb_stub.cc:2081]  Failed to preinitialize Python stub: Python model file not found in '/aws_dda/dda_triton/triton_model_repo/base_model-r3-probe/8/model.py'
  ```

  The Triton index reports the base, marshal and ensemble entries as
  `LOADING` throughout. No Triton error line ("failed to load", "Stub process
  ... is not healthy") appears between relaunches. The watchdog's activity
  sample reads 0 stub processes and 0.0 CPU-s per 5 min. After `model.py` was
  restored (7.5 min in), the same load reached `READY` 32 s later.
- Run 2: `/start` for the scratch model, then 15 s later `/start` for
  another deployed model (`cookies-binary`). The second model stayed `LOADING`
  for more than 5 min, while the scratch load kept relaunching its stub. After
  `model.py` was restored, both reached `READY` within 79 s.
- Capture (2026-10-17, 80 s loop, `gdb -batch -p <backend pid> -ex "thread apply all bt"` twice, 10 s apart; the
  backend was not restarted):
  - The edgemlsdk load thread waits in one call:
    `TritonServer::_LoadModel` → `TRITONSERVER_ServerLoadModel` →
    `InferenceServer::LoadModel` → `ModelRepositoryManager::LoadUnloadModel` →
    `LoadModelByDependency` (a futex wait), with the same LWP in both
    captures.
  - A Triton thread-pool thread runs `ModelLifeCycle::AsyncLoad` →
    `ModelLifeCycle::CreateModel` → `TritonModel::Create` →
    `TritonModel::PrepareInstances`, joining a `std::async` result, with the
    same LWP in both captures.
  - The `std::async` thread is in
    `PrepareInstances` → `TritonModelInstance::CreateInstance` →
    `ConstructAndInitializeInstance` → `TRITONBACKEND_ModelInstanceInitialize`
    → `ModelInstanceState::LaunchStubProcess` → `StubLauncher::Launch` →
    `StubLauncher::ReceiveMessageFromStub(long&, unsigned long)` (the patched,
    bounded wait). Its LWP **differs** between the two captures (a relaunch
    happened between them), so each relaunch runs on a new `std::async` thread
    started under the same `PrepareInstances` call.
  - `docker top` sampled every 2 s shows the stub (`bash -c exec
    .../triton_python_backend_stub .../base_model-r3-probe/8/model.py ...`)
    only at the first sample: each stub exits within about 0.2 s of its start,
    and the parent then waits out the 10 s bound before the next attempt.

### Hypothesis

The relaunch loop is in the Triton 2.45.0 core (r24.04) instance-creation
path, not in edgemlsdk or python_backend: edgemlsdk calls
`TRITONSERVER_ServerLoadModel` once and has no retry (`triton_server.cpp`
`_LoadModel`), and python_backend's `TRITONBACKEND_ModelInstanceInitialize`
returns the launch error (`RETURN_IF_ERROR(err)` after the bounded wait in the
patched `StubLauncher::Launch`). The captures place every new attempt on a new
`std::async` thread under one `TritonModel::PrepareInstances` call, so that
function (or `TritonModelInstance::CreateInstance` below it) creates the
instance again after an initialization error, without a bound. A first check
for the design: read `TritonModel::PrepareInstances` and
`TritonModelInstance::CreateInstance` in triton-inference-server/core r24.04,
and run the reproduction with Triton verbose logging (`--log-verbose=1`) to see
the core's per-attempt messages.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a python-backend model's stub exits before its first message on a
Triton 2.45.0 image (for example because `model.py` is missing) THEN Triton
starts the stub again about every 10 s inside the same load call, without
limit.

1.2 WHEN that happens THEN the model stays `LOADING` and never reports
`UNAVAILABLE` or a failure reason, so `/feature-configurations`, the
`dda-model-status` shadow and the run gate show a load in progress.

1.3 WHEN that happens THEN other models' loads requested meanwhile stay
`LOADING` until the loop ends, because edgemlsdk's single model-load thread is
held by the looping call.

1.4 WHEN self-heal is off and the missing file is never restored THEN the loop
lasts until the backend restarts.

### Expected Behavior (Correct)

2.1 WHEN a python-backend stub exits or hangs before its first message on a
Triton 2.45.0 image THEN the load SHALL fail within 30 s and the model SHALL
report `UNAVAILABLE` with Triton's reason (`triton-model-cleanup-wedge` 2.4).

2.2 WHEN such a load fails THEN other models' loads SHALL proceed.

2.3 WHEN the missing file is restored and `/start` is requested again THEN the
model SHALL reach `READY`.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a python-backend model loads normally THEN it SHALL reach `READY` in
the same time as today (about 16 s alone on the MIC-730).

3.2 WHEN a stub start is slow but succeeds (for example a TensorRT engine
build in `initialize()`) THEN the load SHALL NOT fail because of this fix.

3.3 The bounded stub start of `triton-model-cleanup-wedge` (10 s per start)
SHALL stay, and the JP7 image (Triton 2.63.0) SHALL be unaffected.

3.4 The load watchdog's stall detection and self-heal SHALL keep working as
they do today.

## NITs carried from `triton-model-cleanup-wedge`

- **Log the disabler change once.** The load watchdog logs the "Triton model
  load wedge detected (...); self-heal is off on this device (...)" WARNING
  once per wedge episode and kind. When the reason changes during the same
  episode (for example from `ModelLoadSelfHealEnabled=false` to the kill file
  `/aws_dda/dda_triton/model_status/self-heal.disabled`), no new WARNING names
  the new reason; only `/feature-configurations` and the shadow show it
  (`disabledBy: "file"`, and the recovery action names the file). Log the
  WARNING once more whenever the disabler changes within an episode.

## Out of Scope

- Upgrading Triton on the JP5, JP6 or x86 images.
- The cleanup, publish, Startup-record and self-heal behavior of
  `triton-model-cleanup-wedge`.
