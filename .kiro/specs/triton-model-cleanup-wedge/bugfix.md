# Bugfix Requirements Document

## Introduction

After a Greengrass deployment restarts LocalServer, every Triton model on a JP5,
JP6 or x86 device can stay `LOADING` until the backend container is restarted,
while the deployment reports `COMPLETED`. Seen on the MIC-730 (JP5) on
2026-07-31 (LocalServer 1.0.32, 13+ min), 2026-09-30 (1.0.50, 19 min) and
2026-10-14 (1.0.54, 67 min). A JP6 Jetson AGX Orin (Triton 2.45.0) shows a
consistent sighting on 2026-08-18. A JP7 device (Triton 2.63.0) recovered from
the same trigger by itself within about 40 s.

The `triton-model-load-race` fix (atomic publish plus a bounded `start_model`
retry) shipped in 1.0.54 and the device ran it: the host copies of
`model_convertor.py` and `convert_model_cleanup.py` that the backend seeds into
`/aws_dda` were byte-identical to the fixed source. It closed a window that was
not the one that broke.

**Root cause (verified in code and in the device logs):**

1. **The cleanup deletes under an in-flight load.** A model component's
   Shutdown runs `src/backend/dda_triton/convert_model_cleanup.py`. Its
   `stop_model()` (lines 47-59) only logs a non-200 answer, and the main block
   (lines 62-79, `shutil.rmtree` at line 72) then deletes `base_<m>`,
   `marshal_<m>` and `<m>` from the Triton repository unconditionally, also
   after StopModel answered 403 "Model current state is LOADING". The file is
   unchanged since its first check-in. The atomic publish protects building a
   directory, not deleting one.
2. **Triton 2.45.0 waits for the stub forever.** The JP5, JP6 and x86 images
   build Triton v2.45.0, whose python_backend (r24.04) waits for a stub's first
   message with an unbounded `parent_message_queue_->Pop()` in
   `StubLauncher::Launch`. A stub whose `model.py` disappeared exits at
   preinitialize ("Python model file not found"), so the load neither completes
   nor fails. Upstream bounded that wait (10 s, then kill the stub) in r25.08;
   JP7 already runs Triton 2.63.0 with it.
3. **One stuck load blocks every load.** edgemlsdk creates Triton with a single
   model-load thread
   (`src/edgemlsdk/src/src/mlops/triton/triton_server.cpp:459`), so every later
   load queues behind the stuck one.
4. **Nothing recovers it.** `start_model_triton` refuses unless the model is
   `UNKNOWN`/`UNAVAILABLE` and `stop_model_triton` refuses unless it is `READY`
   (`src/backend/utils/feature_configs_utils.py:279-334`). Only a backend
   restart clears the wedge.
5. **The deployment reports success.** `model_convertor.py` ignores
   `start_model()` returning `False` (lines 786-788) and exits 0, so the
   component reports RUNNING and the deployment COMPLETED.
6. **The trigger is structural.** Every LocalServer recipe declares
   `aws.greengrass.Cli` without a `DependencyType`, which defaults to HARD (for
   example `recipe-arm64-jp5.yaml:12-13`). The CLI restarts whenever a
   deployment merges configuration, so LocalServer restarts, and through their
   HARD dependency on LocalServer every model component restarts and runs the
   destructive Shutdown. 6 of the 7 deployments in the MIC-730's retained logs
   show this cascade.

The original 2026-07-31 incident had the same cause. The cookies component's
Shutdown logged the 403 "LOADING" at 04:36:31.991 and
`Cleaned directory: …/base_model-cookies…` at 04:36:31.993, 154 ms before the
stub's failed read that `triton-model-load-race/bugfix.md` recorded.

On 2026-10-14 the in-flight load came from the new PPE model component's own
first Startup. Greengrass started it while LocalServer was stopping, then ran
its dependency-restart Shutdown 1.9 s later while the Startup was still alive.
Both scripts wait for the backend with `model_autostart_utils.wait_for_server`
(probes at +0, +3, +9, +21 s), so both reached the new backend, the Startup
about 1.7 s first: the load was enqueued at 14:52:17.12, the directories were
deleted at 14:52:18.78, and the base stub's `stat(model.py)` failed at
14:52:21.20.

**Owner decisions (2026-10-15).**

- **F2, backport.** Backport python_backend r25.08's bounded wait for the
  stub's first message onto the Triton 2.45.0 / python_backend r24.04 builds
  (JP5, JP6, x86) as a build patch. Triton is not upgraded.
- **F5a, SOFT.** LocalServer's dependency on `aws.greengrass.Cli` becomes SOFT
  in every recipe variant and in the publish path. Configuration that
  LocalServer reads only at startup gets a follow-up spec
  (`localserver-config-live-update`); this bugfix documents the interim
  behavior (Operator notes below).
- **F4, self-heal: approved, on by default.** The owner approved an automatic
  backend restart on a detected wedge, on the condition that it clearly logs
  that it acted and why. Later the same day the owner decided that self-heal
  is **on by default on every device**, with a documented per-device opt-out.
  Every LocalServer recipe therefore carries `ModelLoadSelfHealEnabled:
  "true"`, a missing key also means on, a device opts out with a configuration
  merge of `"false"`, and a device-local file turns it off whatever the
  configuration says (Operator notes). Detection (`stalled`, with the evidence
  and the recovery action) ships on every device either way. Because it runs
  everywhere by default, self-heal is conservative: it acts only on positive
  wedge evidence, never on `LOADING` time alone, never during a load that
  shows stub CPU or GPU activity (such as a TensorRT engine build), never
  while an unblocked workflow run is in flight or a continuous workflow runs
  on READY models, never while a vLLM model loads, and at most once per
  30 min and three times per 24 h, after which it reports "self-heal
  exhausted". It checks every safeguard again immediately before its point of
  no return.
- **On-device verification.** The owner approved changing the MIC-730's
  state for verification, including deliberate wedge reproductions on
  unfixed builds.

**Exit code (scope of this fix).** Startup keeps exiting 0; a non-zero exit
would retry the Startup and then roll back the whole deployment.

## Bug Analysis

### Current Behavior (Defect)

**Cleanup and publish under an in-flight load**

1.1 WHEN a model component's Shutdown runs and StopModel answers 403 because
the model is `LOADING` THEN the system deletes the model's `base_`, `marshal_`
and ensemble directories anyway.

1.2 WHEN a deployment adds or updates a model component and restarts
LocalServer THEN the system runs the new component's Startup and its
dependency-restart Shutdown concurrently, and the Startup's load request can
reach the new backend before the Shutdown deletes the directories.

1.3 WHEN a workflow run finds a model `UNKNOWN` THEN the per-run gate
(`dda_triton/model_readiness.ensure_model_ready`, lines 219-240) requests a
load without checking that the model's repository entries are complete and
stable.

1.4 WHEN a model component's Startup re-runs with unchanged artifacts THEN
`_atomic_publish_model_dir` (`model_convertor.py:262-269`) deletes each
published directory and renames a fresh copy into place, one directory at a
time, which is a second deletion under any load of that model that is in
flight.

**Unrecoverable load wedge**

1.5 WHEN a model's directories are deleted between the start of its Triton load
and its python stub's start THEN the stub exits "Python model file not found"
and the load never completes or fails (python_backend r24.04 waits for the
stub's first message without a timeout).

1.6 WHEN one model load hangs THEN every later load on the device stays
`LOADING` behind it, until the backend process restarts.

1.7 WHEN a model is stuck `LOADING` THEN the system refuses both start and
stop, `start_model` re-issues the refused start, the run gate waits 600 s and
reports that "a first load can take minutes", nothing reports the wedge, and
no API, UI or component recovers it short of a backend restart.

**Restart cascade**

1.8 WHEN any deployment merges configuration THEN the Greengrass CLI restarts,
LocalServer restarts through its HARD dependency on `aws.greengrass.Cli`, and
every model component restarts and runs the destructive Shutdown, even when no
model or LocalServer artifact changed.

**Silent success**

1.9 WHEN `start_model` gives up THEN `model_convertor.py` exits 0, the
component reports RUNNING, the deployment reports COMPLETED, and neither the
Portal deployment view nor the `dda-model-status` shadow shows that the models
are not ready.

### Expected Behavior (Correct)

2.1 WHEN a model component's Shutdown runs THEN the system SHALL NOT delete or
replace the model's repository entries while a load of that model is in
flight: it SHALL wait, bounded (300 s), for the load to reach a terminal state,
unload a `READY` model before deleting, and skip the deletion with a WARNING
(still exiting 0) if the load is still in flight at the bound.

2.2 WHEN a removal and a load request for the same model overlap THEN the
system SHALL serialize them through a per-model lock that the cleanup and every
backend load request share (`start_model_triton`, the per-run gate and the
continuous ModelGate). A load request during a removal, or during a changed
publish's replacement of the entries (2.8), SHALL be refused (HTTP 409 on the
start API), and neither gate SHALL stop asking because of the refusal: each
SHALL check the entries again and request the load again once they allow it.
The removal SHALL rename the directories to trash names while it holds the
lock and delete them after releasing it.

2.3 WHEN the per-run gate finds a model `UNKNOWN` and the model's repository
entry exists THEN it SHALL request a load only once the entry is complete and
has been unchanged for 10 s, as the continuous ModelGate does. A model whose
entry is absent keeps today's behavior: the load is requested and fails fast as
`UNAVAILABLE`. This changes one user-visible behavior: a run on an entry that
exists but stays incomplete (a missing or unreadable `config.pbtxt` or
`model.py`, left by an interrupted removal or a broken component), which today
fails at once with Triton's reason, SHALL instead fail when the gate's 600 s
budget runs out, with an error that names the missing or unreadable path and
says how to republish the entry; no load is requested for it. A staging or
trash directory left behind by an interrupted publish or removal SHALL be
removed once it is 10 min old, or at once when it comes from an earlier boot,
so it cannot hold either gate back indefinitely. For the names the fixed code
creates, which carry a boot stamp, the age SHALL be measured on a clock that a
wall-clock step does not move, so a live directory is never removed;
unstamped leftovers (from the pre-fix code during the first rollout
deployment, or from a host whose boot id cannot be read) are aged by their
modification time against the wall clock.

2.4 WHEN a python-backend stub on a Triton 2.45.0 image (JP5, JP6, x86) exits
or hangs before sending its first message THEN the load SHALL fail within
30 s, the model SHALL report `UNAVAILABLE` with Triton's reason, and other
models' loads SHALL proceed. This SHALL be delivered by backporting
python_backend r25.08's bounded wait as a patch in the edgemlsdk build, without
upgrading Triton.

> **Deviation (device verification, 2026-10-16).** Criterion 2.4 is
> **partially met** on JP5 (the MIC-730, LocalServer arm64JP5 1.0.56), agreed
> with the owner.
>
> - Verified: each stub start is bounded at 10 s, and the patched
>   `libtriton_python.so` is in the image (both sentinel strings present).
> - Not met: when `base_<model>/<v>/model.py` is missing, the load relaunches
>   the stub about every 10 s inside one load call (43 relaunches in 7.5 min).
>   The model stays `LOADING` instead of going `UNAVAILABLE`, and other
>   models' loads requested meanwhile stay blocked.
> - Mitigations observed: self-heal, on by default, restarts the backend after
>   15 min of such a loop (2.5, 2.6); restoring `model.py` makes the same load
>   reach `READY` in 32-79 s.
>
> The relaunch loop is carried by the `triton-python-stub-relaunch-loop` spec
> (`.kiro/specs/triton-python-stub-relaunch-loop/bugfix.md`).

2.5 WHEN a model has been `LOADING` for at least 15 min, there is positive wedge
evidence (its repository entries are missing or changed after its load
request, or Triton's model index has not changed for 15 min), and no load shows
activity THEN the system SHALL report the model as `stalled`, with the evidence
and the recovery action, through `/feature-configurations`, the
`dda-model-status` shadow and the run-gate error. `LOADING` time alone SHALL
never make a model stalled, and a load whose stub processes use CPU (for
example a TensorRT engine build) SHALL never be reported as stalled. This
detection SHALL work whether or not self-heal is enabled.

2.6 WHEN a model is reported stalled THEN the system SHALL restart the backend
process automatically (self-heal), unless the device has opted out. Self-heal
SHALL be on by default on every device: every LocalServer recipe ships
`ModelLoadSelfHealEnabled: "true"`, and a missing key means on. A device SHALL
opt out with a configuration merge of `"false"`, or with the file
`/aws_dda/dda_triton/model_status/self-heal.disabled`, which wins over the
configuration. An unrecognized value SHALL count as an opt-out, and self-heal
SHALL NOT act in a backend process that has not yet read its configuration
successfully, so a read failure can never override an opt-out. Because it runs
everywhere by default, self-heal SHALL be conservative:

- it SHALL act only on a model reported stalled (2.5: positive wedge evidence
  and no stub CPU activity, never `LOADING` time alone), and it SHALL defer
  while the GPU shows any load where the device exposes it (Jetson), so a
  legitimate long load such as a TensorRT engine build is never interrupted;
- never while a workflow run or a capture task is in flight that the wedge
  has not blocked, and never while a continuous workflow is running on READY
  models; such a deferral SHALL be reported, naming the workflows. A run SHALL
  count as in flight from the moment it starts, including its frame capture,
  feed preparation and readiness wait; a digital-input run counts from its
  trigger, before its frame grab. The wedge blocks it only while it waits in
  its readiness gate, or once it has been stuck on a stalled model for
  15 min;
- before restarting, continuous workflows SHALL pause, new runs SHALL be
  accepted at once and count as in flight, and runs in flight SHALL be
  drained (at most 60 s). No run SHALL ever be held, so a triggered run always
  captures its frame when it is triggered. If a run that the wedge has not
  blocked is still in flight, self-heal SHALL defer instead. This quiesce
  SHALL end on every path that does not restart, and by itself at the latest
  120 s after it began, even if the watchdog stops working. No run that
  started before the restart SHALL be cut off unless the wedge blocked it.
  Every triggered or manual workflow run that the restart cuts off SHALL be
  marked failed before the restart, within a 10 s budget, with an error that
  names the self-heal attempt; a run that the budget skips is counted in a
  WARNING and can stay `pending` or `running` after the restart. A run that starts, or leaves its readiness gate, after the point
  of no return SHALL be refused before its pipeline starts (a digital-input
  trigger before its frame grab), and a triggered or manual one SHALL be
  marked failed with the refusal while the backend process still runs (a row
  that a trigger creates in the last milliseconds before the process exits
  can stay `pending`, as after any backend exit). Continuous runs are finished
  by the existing startup reconciliation;
- immediately before the point of no return it SHALL check the opt-outs, the
  vLLM rule, the GPU rule and the stall again, and it SHALL NOT restart if
  any of them now forbids it, or if the stall cannot be confirmed because a
  read failed;
- if persisting or logging the attempt fails, or any step before it, the
  backend SHALL NOT restart, new runs SHALL be accepted and continuous
  workflows SHALL resume at once, and an attempt already persisted SHALL be
  recorded as not performed;
- never while a vLLM model is loading or staged;
- at most once per 30 min and three times per rolling 24 h, counted across
  restarts and including attempts that were not performed; within one boot, a
  wall-clock step SHALL never shorten either window. After that it SHALL stop
  acting and report "self-heal exhausted"; it SHALL never loop.

Before acting it SHALL persist the attempt, then log one ERROR line that names
the models and how long they were `LOADING`, the wedge evidence (including
pb_stub's missing-file line when a Startup captured it), the action, the
attempt count, the runs it interrupts and any vLLM models the restart drops.
The record SHALL stay visible after the restart through
`/feature-configurations`, the `dda-model-status` shadow and the Portal
deployment view. Self-heal SHALL trigger only on Triton wedges. On a device
that has opted out, the system SHALL report the stall with a manual recovery
action and SHALL NOT restart anything.

2.7 WHEN a model component's Startup cannot confirm `READY` THEN the system
SHALL record the outcome (state, reason, time) in a device-side record that
`/feature-configurations`, the `dda-model-status` shadow and the Portal
deployment view surface, and SHALL still exit 0 so the deployment is not rolled
back. A load still in progress SHALL be recorded and logged as in progress (at
WARNING), not as a failure; a failed load, an unreachable backend or a refused
load request SHALL be logged at ERROR. The record SHALL be cleared once the
model is `READY`.

2.8 WHEN a restarted model component's artifacts are unchanged THEN its Startup
SHALL leave the published repository entries untouched and only request the
load. WHEN any of them changed THEN it SHALL replace the changed directories
together, only after any in-flight load of the model ends, waiting at most
300 s in total and then replacing them with a WARNING. Except after that 300 s
bound, no load SHALL be able to start between those replacements. After it, a
load that starts between them can see mixed entries; if that breaks the load,
it SHALL fail fast as `UNAVAILABLE` instead of wedging (2.4 on the Triton
2.45.0 images, Triton 2.63.0's own bounded wait on JP7), and the Startup's
retry SHALL load the new entries.

2.9 WHEN a deployment does not change LocalServer THEN LocalServer SHALL NOT
restart because the Greengrass CLI restarted: every LocalServer recipe variant
and every published LocalServer recipe SHALL declare the `aws.greengrass.Cli`
dependency `SOFT`. The interim behavior change SHALL be documented: until
`localserver-config-live-update` lands, configuration that LocalServer reads
only at startup takes effect at the next LocalServer restart, which the
operator can force (Operator notes).

2.10 WHEN the Portal returns the latest deployment for its target and the
target devices completed it THEN it SHALL add, without changing any existing
field, a warning labeled as the devices' current state that lists the
deployment's model components that a device reports as not `READY` (state,
since, stalled), together with that device's self-heal state.

2.11 WHEN the fix is implemented THEN it SHALL be verified on the MIC-730 (JP5)
with reproductions R1 (a cleanup about 1 s after a load request), R2 (a
deployment that adds a model component and a workflow) and R3 (a stub whose
`model.py` is missing), and with the self-heal scenario: the shipped default
(self-heal on: the detection, the restart, its ERROR line and its persistent
record; a run blocked by the wedge ends `failed`, with the stall message or
the interruption message, and none is left `pending` or `running`), a
per-device opt-out by configuration merge (the stall reported, no restart),
the kill-switch file, the rate limit and exhaustion. The backend SHALL stay
healthy, with no crash loop and no unexpected container restart, for at least
15 min after each deployment. JP6,
JP7 and x86 device verification follows only with the owner's go for each
device, and is recorded as pending in `verification-notes.md` until then.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a model component is removed by a deployment THEN the system SHALL
CONTINUE TO unload the model and remove its repository entries, after at most a
bounded delay; the only exception is a load still in flight at the bound
(2.1).

3.2 WHEN a model component starts on a fresh device or with new artifacts THEN
the system SHALL CONTINUE TO publish atomically and request the load; the final
on-disk layout and `config.pbtxt` content stay byte-identical.

3.3 WHEN a model reaches `READY` within the Startup budget THEN the Startup
SHALL CONTINUE TO exit 0 and the deployment SHALL CONTINUE TO complete as
today; the budget (3 attempts of 120 s) is unchanged.

3.4 WHEN `/start` is requested for an `UNKNOWN` or `UNAVAILABLE` model, or
`/stop` for a `READY` model, THEN the API SHALL CONTINUE TO respond as today,
except that `/start` answers 409 while the same model's entries are being
removed or replaced, and 400 for a model name outside
`^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`.

3.5 WHEN a workflow run or continuous runner uses a `READY` model THEN the
system SHALL CONTINUE TO run it with no added latency and no new log lines.
No run is ever held. Continuous ticks are skipped only while self-heal is
quiescing or restarting the backend, and runs are refused only while it
restarts the backend (2.6).

3.6 WHEN vLLM model components are staged, loaded or cleaned up
(`vllm_model_prep.py`, the vLLM runtime and its reconciler) THEN the system
SHALL CONTINUE TO behave as today; vLLM state never triggers self-heal.

3.7 WHEN JP7 (Triton 2.63.0) loads models THEN the native side SHALL CONTINUE
TO behave as today; `src/edgemlsdk/Dockerfile.jp7` is not changed.

3.8 WHEN the Portal returns a deployment or a device THEN every existing field
SHALL CONTINUE TO be present and unchanged; the readiness information is
additive and absent when the device reports none.

3.9 WHEN a continuous workflow waits for its models THEN the ModelGate SHALL
CONTINUE TO return the same waits and reasons; its file checks move into a
shared helper with identical results.

3.10 WHEN the Triton model repository is empty THEN no new code SHALL create
the Triton server (the `triton_repo_has_models` guard runs first).

3.11 WHEN every model on a device is `READY` and nothing changes THEN the new
background checks SHALL log nothing and write no shadow update.

3.12 WHEN the LocalServer recipes are compared with today's THEN they SHALL
differ only by the `SOFT` dependency type and the
`ModelLoadSelfHealEnabled: "true"` default; the model component recipes (HARD
dependency on LocalServer) are unchanged.

3.13 WHEN LocalServer configuration that is read live (`LocalLoginEnabled`) or
the CLI's IPC operations (restart, list and component details) are used THEN
they SHALL CONTINUE TO work as today.

3.14 WHEN the model component scripts run on the host THEN they SHALL CONTINUE
TO run under the host's system `python3` (3.8 on JP5) with only the standard
library, `requests` and protobuf.

### Bug Condition

```pascal
FUNCTION isWedgeCondition(X)
  INPUT: X = (backend B, model M, Triton load L of M on B,
              removal or replacement R of M's repository entries)
  OUTPUT: boolean

  RETURN inFlight(L, B)
     AND R happens during L, before M's base stub stat()s model.py
     AND pythonBackendRelease(B) < r25.08          // JP5, JP6, x86 images
END FUNCTION
// Unfixed: L never completes or fails, and every later load on B blocks.

FUNCTION isSilentSuccess(S)            // S: one model component Startup
  RETURN startModel(S) = False AND exitCode(S) = 0 AND NOT recorded(S)
END FUNCTION

FUNCTION isCascade(D)                  // D: one Greengrass deployment
  RETURN mergesConfiguration(D) AND NOT changesLocalServer(D)
     AND restarts(LocalServer) AND restarts(every model component)
END FUNCTION
```

```pascal
// Property 1 - no removal or replacement under a tracked load (2.1, 2.2, 2.8)
FOR ALL interleavings X of backend load requests and removals/publishes of M DO
  ASSERT NOT (entriesRemovedOrReplaced(M) DURING trackedLoad(M))
     OR (publish(M) AND waited >= 300 s once for the whole conversion
         AND WARNING logged)
END FOR

// Property 2 - bounded stub start on 2.45.0 images (2.4)
FOR ALL loads L WHERE M's base stub exits before its first message DO
  ASSERT state(M) = UNAVAILABLE WITH reason WITHIN 30 s
     AND later loads proceed
END FOR

// Property 3 - honest Startup outcome (2.7)
FOR ALL Startups S WHERE startModel(S) = False DO
  ASSERT recorded(S) AND exitCode(S) = 0
END FOR

// Property 4 - conservative, bounded self-heal, on by default (2.5, 2.6)
FOR ALL histories H of detections, runs, switch values and restarts DO
  ASSERT stalled(M) IMPLIES (evidence(M) AND NOT stubCpuActivity)
  ASSERT restart(H) IMPLIES switchOn(device) AND NOT killFile(device)
         // switchOn: the last successful read gave an "on" value or no key
  ASSERT restart(H) IMPLIES NOT gpuLoad(window)      // where the GPU is readable
  ASSERT restarts(H, any 24 h window) <= 3 AND spacing(H) >= 30 min
  ASSERT every restart was persisted before it was signalled
  ASSERT restart(H) IMPLIES the opt-outs, the vLLM rule, the GPU rule and the
         stall were checked again after the drain and still allowed it
  ASSERT at every signal, no run is in flight that the wedge has not blocked
         // in flight: from the run's start, including capture and preparation
         // (a digital-input run from its trigger)
  ASSERT no run is ever held
  ASSERT every quiesce that does not lead to a restart ends, at the latest
         120 s after it began
  ASSERT every triggered or manual run a restart cuts off is marked failed
         before the restart, within a 10 s budget; a run that the budget
         skips is counted in a WARNING and can stay pending or running after
         the restart
END FOR

// Property 5 - no cascade from a CLI bounce (2.9)
FOR ALL deployments D DO
  ASSERT NOT (restarts(LocalServer) AND cause(D) = dependency aws.greengrass.Cli)
END FOR

// Preservation
FOR ALL X WHERE NOT (isWedgeCondition(X) OR isSilentSuccess(X) OR isCascade(X)) DO
  ASSERT F(X) = F'(X)        // 3.1-3.14
END FOR
```

Property 5 covers only the CLI cause. LocalServer still depends HARD on
Nucleus, ShadowManager and TokenExchangeService (and, on the ECR recipes,
DockerApplicationManager), so it can still restart through those.

## Relationship to `cold-model-first-run-failure` Requirements 2.5-2.9

That spec's tasks cover 2.1-2.4 and 2.10-2.15 only. This spec takes the
following positions on 2.5-2.9:

| Requirement | Position in this spec |
|---|---|
| 2.5 surface the Startup's unconfirmed outcome | **Satisfied** by 2.7: an explicit device-side record, surfaced through `/feature-configurations`, the `dda-model-status` shadow and the Portal deployment view; the Startup still exits 0. |
| 2.6 don't misclassify a minutes-long first load | **Satisfied on the device side.** An unconfirmed load that is still in progress is recorded as `LOADING` and logged at WARNING, never as a failure, and the record clears automatically at `READY`; the stall detection never classifies a CPU-active load as stalled. The Startup budget is unchanged. Portal rendering of the lifecycle belongs to 2.8. |
| 2.7 reload at boot, or an explicit non-`READY` state | **Not addressed.** The Triton boot-time reconciler stays deferred (F5c follow-up). The cold-model run gate's handling of `UNKNOWN` is unchanged apart from 2.3. |
| 2.8 a lifecycle state the Portal can render | **Partially satisfied on the device side.** The shadow's `modelReadiness.models` lists every Triton model component with its state (`UNKNOWN` = not loaded, `LOADING`, `READY`, `UNAVAILABLE` = failed, `UNLOADING`) and a `stalled` flag, beside `/feature-configurations`' per-model `status`. Not loaded, loading, READY and failed are distinguished. A conversion in progress is not: it reports as not loaded (`UNKNOWN`) until its load is requested, so the "converting-in-progress" part of 2.8 stays open for `cold-model-first-run-failure`. The Portal renders the not-ready models only in the deployment warning (2.10); the device page passes the section through unrendered. |
| 2.9 the run error names the model's state | **Already met** by the cold-model gate. This spec adds the stall reason and the recovery action to that error (2.5). |

## Operator notes

### Interim behavior change from the SOFT CLI dependency (2.9)

Before this fix, nearly every deployment restarted LocalServer as a side
effect of the CLI restart, so configuration that LocalServer reads only at
startup was applied on every deployment by accident. After this fix:

- a deployment that changes LocalServer's version still restarts it, and
  applies everything;
- a deployment that only merges LocalServer configuration no longer restarts
  it. Keys that LocalServer reads only at startup take effect at the next
  LocalServer restart.

Startup-only keys known today:

- `workflowTuning`, read once by
  `workflow_engine/runtime.py::_configure_sample_export`;
- the keys served from the per-process cache in
  `DefectDetectionConfig.get_local_server_config()`, for example
  `StationName`, `SoftwareVersion` and `CameraDiscoveryIntervalSeconds`.

`localserver-config-live-update` inventories the rest. `LocalLoginEnabled`,
the ShadowManager document size limit, the `accessControl` policies and
`ModelLoadSelfHealEnabled` are read live and are not affected.

To apply a startup-only key now, restart LocalServer in one of these ways:

1. In the LocalServer UI, open **Application health overview**, then
   **Application logs and restart**, and restart the application
   (`POST /restart-dda`). This restarts, through Greengrass, every
   `aws.edgeml.dda.*` component and every `model*` component that is RUNNING
   or STARTING (and the LFV edge agent where it is installed).
2. On the device, run
   `sudo /aws_dda/greengrass/v2/bin/greengrass-cli component restart --names <LocalServer component name>`.
   The model components then restart through their HARD dependency on
   LocalServer.
3. To restart the backend only, without restarting the model components, run
   `sudo docker restart <backend container>`. The backend re-reads its
   configuration over IPC when it starts.

### Self-heal switch (2.6)

Self-heal is **on by default** on every device. Detection reports `stalled`
with the evidence on every device; where self-heal is off, the recovery action
says how to restart the backend by hand.

| Switch | How | Takes effect |
|---|---|---|
| LocalServer configuration `ModelLoadSelfHealEnabled` | The recipes ship `"true"`, and a missing key also means on. To opt one device out, merge `{"ModelLoadSelfHealEnabled": "false"}` into that device's LocalServer configuration through the device's cloud deployment: revise the deployment that targets the device (or its thing group) with a configuration update of the LocalServer component, `{"merge": "{\"ModelLoadSelfHealEnabled\":\"false\"}"}`, keeping every other component and configuration update. This is the supported opt-out for a LocalServer deployed from a thing or thing group, and it persists with the deployment. A local `sudo /aws_dda/greengrass/v2/bin/greengrass-cli deployment create --update-config '{"<LocalServer component name>": {"MERGE": {"ModelLoadSelfHealEnabled": "false"}}}'` does **not** opt such a device out: the local deployment reports SUCCEEDED, but the cloud deployment owns the component's configuration, and the effective value stays unchanged (verified on the MIC-730, 2026-10-16). The local form only works for a LocalServer that was deployed locally. To opt back in, merge `"true"`, or reset the key in the same cloud deployment with a configuration update `{"reset": ["/ModelLoadSelfHealEnabled"]}`. Accepted values: `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off`, `enabled`/`disabled`. Any other value means off and logs a WARNING. If the configuration cannot be read, the last value read successfully applies; until a read succeeds after a backend start, self-heal stays off. | Opting out: at the next action at the latest, because the key is read again when an attempt starts and once more immediately before its point of no return. Opting back in: within 5 min. Reported state: within 5 min. No restart needed. |
| Device file `/aws_dda/dda_triton/model_status/self-heal.disabled` | `sudo touch` it to turn self-heal off whatever the configuration says (it needs no Greengrass access); delete it to return to the configuration | At the next decision (within 30 s). It also stops an attempt that is already quiescing, up to the attempt's point of no return, which comes at most about 85 s after the `SELF-HEAL quiescing` line and never more than 120 s after it. From that point the restart is unconditional, and the `SELF-HEAL:` ERROR line follows within a second. |

The attempt history lives in
`/aws_dda/dda_triton/model_status/self_heal.json`. Deleting it resets the rate
limit; if it is unreadable or cannot be written, self-heal stays off until it
is repaired or deleted.

A self-heal restart marks the triggered and manual workflow runs it
interrupts as failed, with an error that names the attempt; run them again.
A run that starts while the backend is restarting is refused with an error
that says so (HTTP 503 for a classic run). Continuous workflows resume on
their own once their models reload. Marking runs failed has a 10 s budget; a
run that the budget skips is counted in a WARNING and can stay `pending` or
`running` after the restart.

Digital-input workflows have no readiness gate: a trigger that arrives before
its model is READY fails and starts the load, and triggers after the load
succeed. To avoid failed triggers, request `/start` for those models after a
self-heal restart, or restart their model components.

Self-heal never restarts the backend while a workflow run or capture task
that the wedge does not block is in progress, while a continuous workflow
runs on READY models, or while the GPU shows load. A capture task can defer
it for as long as the task runs (at most 100 images, 120 s apart). It reports
the deferral and names the workflows; pausing them, or restarting the backend
by hand, clears the wedge. While it prepares a restart, continuous workflows
pause for at most about 85 s (never more than 120 s); triggered runs are never
delayed.

## Out of Scope (follow-ups)

- **F5c.** Backend-owned repository garbage collection plus the Triton
  boot-time reconciler (cold-model 2.7). After it, model components could take
  a SOFT dependency on LocalServer.
- **Hygiene (H).**
  - Make the model-load thread count configurable (`triton_server.cpp:459`).
  - Free any finished worker in `MultiThreadedJobQueue`, not only the oldest.
  - Fix the stale "LocalServer uses a `Run` lifecycle" comment in
    `edge-cv-portal/backend/functions/greengrass_publish.py:264-276`.
- **`localserver-config-live-update`** (`.kiro/specs/localserver-config-live-update/`).
  A deployment that changes startup-only keys takes effect without relying on
  the CLI restart. Spec documents only; written separately.
- **A Portal control for the self-heal switch.** Devices opt out through
  their deployment's LocalServer configuration or the device file.
- **A GPU activity signal on x86.** The self-heal GPU veto reads the Jetson
  GPU load node; x86 devices rely on the stub-CPU veto alone.
- **Startup budget sizing** for minutes-long first loads (cold-model 1.9).
- **Reloading changed content.** When a load of a model's old content finishes
  before a changed publish replaces its entries, the old content stays loaded
  until the model is next reloaded.
- **Stations with authorization enabled.** The backend serves only its TLS
  listener there, so the model scripts' HTTP calls cannot reach it. This
  predates this fix; the per-model lock still protects removals there.
- **The LFV↔Triton switch's repository writers**
  (`utils/triton_to_lfv.clean_triton_model_repo`, `utils/lfv_to_triton.convert_models`).
  The switch to LFV leaves Triton mode, so a Triton load it interrupts no
  longer matters there, and F2 makes such a load fail fast on the 2.45.0
  images. The switch to Triton runs while no Triton load is tracked;
  `convert_models` publishes through `convert_to_triton_structure` and so gets
  the content-aware publish and the lock like a Startup.
- **The `dda-model-status` shadow's existing `models` object** (GPU-fallback
  visibility) keeps its merge behavior: the entry of a removed model persists
  in the shadow. The one exception: when ShadowManager rejects a report for
  its size, the entries of models that are no longer in the Triton repository
  are deleted, so the document fits again. The new readiness keys use arrays
  and are not affected.
- Rendering `modelReadiness`/`selfHeal` on the Portal device page.

## Revision history

- **2026-10-15** — requirements written after the 2026-10-14 incident, with
  the owner's decisions.
- **2026-10-15** — revised after design review iteration 1: self-heal is
  opt-in per device (off by default), matching the owner's answer recorded at
  the time (superseded by the next entry); runs are quiesced before a self-heal restart and continuous workflows on
  READY models defer it; a changed publish replaces its directories under one
  lock hold; stale staging and trash directories are swept; the shadow's
  readiness section gains a per-model list (cold-model 2.8); the Portal warning
  is shown on the latest deployment only, as current device state.
- **2026-10-15** — revised after design review iteration 2 and the owner's
  decision that self-heal is on by default on every device, with a per-device
  opt-out (Owner decisions; the recipes ship `"true"`). Self-heal also defers
  while the GPU shows load, marks the triggered and manual runs it interrupts
  as failed, and accepts runs again at once if it aborts before the restart;
  attempts that were not performed count toward the rate limit; a size
  rejection of the `dda-model-status` shadow retires stale GPU-fallback
  entries.
- **2026-10-15** — revised after design review iteration 3: a run counts as
  in flight from its start, including its capture and preparation, and
  capture tasks count as runs (2.6); runs that start or leave their readiness
  gate after the point of no return are refused and marked failed (2.6);
  every safeguard is checked again immediately before the point of no return
  (2.6, Property 4, Operator notes); a load request refused during a removal
  or a replacement is asked again by both gates (2.2); 2.11 accepts either
  failure message for a blocked run; 3.4 lists the new 400.
- **2026-10-16** — revised after design review iteration 4: no run is ever
  held, so a triggered run always captures its frame at its trigger (2.6,
  3.5); a digital-input run counts as in flight from its trigger (2.6); the
  quiesce ends on every path that does not restart, and by itself after
  120 s, and a failed read immediately before the point of no return means no
  restart (2.6, Property 4, Operator notes); 2.3 states the user-visible
  change for a permanently incomplete entry and which hidden-directory names
  are aged by the wall clock; 2.8 states the exception after the 300 s
  publish wait; cold-model 2.8 is partially satisfied.
- **2026-10-16** — revised after design review iteration 5 (no HIGH or MEDIUM
  findings): 2.6 and Property 4 state that interrupted runs are marked failed
  within a 10 s budget, and that a run the budget skips is counted in a
  WARNING and can stay `pending` or `running`; the Operator notes say that
  digital-input workflows fail their first trigger after a self-heal restart
  until their model is READY, and how to avoid it.
- **2026-10-16** — after the JP5 device verification: 2.4 carries a deviation
  record (partially met; the stub relaunch loop moves to the
  `triton-python-stub-relaunch-loop` spec); the Operator notes name the cloud
  configuration merge as the supported opt-out, because a local
  `greengrass-cli` merge does not change a cloud-deployed LocalServer's
  configuration.
