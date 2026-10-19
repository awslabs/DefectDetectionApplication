# Triton Model Cleanup Wedge — Bugfix Design

## Overview

The wedge needs three things to line up:

- A model component's Shutdown deletes the model's Triton repository directories
  while the model is loading.
- On the Triton 2.45.0 images (JP5, JP6, x86), the python stub that then cannot
  find `model.py` is waited on forever.
- edgemlsdk's single model-load thread makes every later load queue behind that
  stuck one.

Nothing reports or recovers the wedge, and the deployment reports COMPLETED. The
trigger is structural. LocalServer depends HARD on the Greengrass CLI, which
restarts on nearly every deployment. That restarts LocalServer, and every model
component restarts through its own HARD dependency on LocalServer.

The fix closes the deletion window from both sides and adds a safety net:

- **A cross-process per-model lock.** It is an `flock` on a file under
  `/aws_dda`, which both the host-side model scripts and the backend container
  see. Every load the backend requests holds the lock shared until the load
  ends. Removing the model's entries, or replacing them with changed content,
  takes it exclusive. The Shutdown cleanup waits (bounded), unloads a READY
  model, and renames the entries to trash names under the lock. A republish of
  unchanged content leaves the entries alone, and a changed republish swaps all
  of a model's directories under one hold (only after a 300 s wait for the
  lock times out does it swap without it, D9). The per-run gate requests a
  load only when the entries are complete and stable.
- **A bounded stub start.** python_backend r25.08's bounded wait for a stub's
  first message is backported onto the 2.45.0 builds. A stub that dies before
  its first message then fails the load in about 10 s instead of wedging
  Triton.
- **An honest Startup.** A Startup that cannot confirm READY records the outcome
  instead of hiding it.
- **A watchdog.** A backend watchdog detects a wedge from positive evidence only
  and reports it on every device. It then restarts the backend (self-heal),
  which is on by default with a per-device opt-out (Owner Decisions). It acts
  only without load activity and after a bounded quiesce, during which
  continuous workflows pause and every workflow run counts as in flight from
  the moment it starts (a digital-input trigger from the trigger, before its
  frame grab). No run is ever held, and the quiesce ends on every path that
  does not restart, by itself after 120 s at the latest. It re-checks every
  safeguard immediately before its point of no return, marks the runs it
  interrupts as failed, and stays under a persisted rate limit.
- **A SOFT CLI dependency.** LocalServer's dependency on the CLI becomes SOFT, so
  a configuration merge no longer restarts LocalServer and the model components.
- **Portal visibility.** The Portal's view of the latest deployment lists the
  models that devices currently report as not ready.

| ID | Change | Files | Requirements |
|---|---|---|---|
| F1 | Per-model load lock and cleanup guard | new `src/backend/dda_triton/model_locks.py`, new `src/backend/dda_triton/load_tracker.py`; `dda_triton/triton_edge_client.py`, `utils/feature_configs_utils.py`, `dda_triton/convert_model_cleanup.py`, `dda_triton/triton_setup.py` | 2.1, 2.2 |
| F2 | Backport of the bounded first-message wait | new `src/edgemlsdk/patches/edgeml-triton-python-backend.diff`; `src/edgemlsdk/Dockerfile.jp5`, `Dockerfile.jp6`, `Dockerfile` | 2.4 |
| Gate | Per-run gate requests a load only for stable entries; stale hidden directories are swept | new `src/backend/dda_triton/repo_entries.py`; `dda_triton/model_readiness.py`, `workflow_engine/model_gate.py` | 2.3 |
| F3 | Startup outcome record | new `src/backend/dda_triton/model_status_records.py`; `dda_triton/model_convertor.py` | 2.7 |
| F4 | Wedge watchdog: detection, self-heal (on by default, per-device opt-out), run quiesce | new `src/backend/dda_triton/load_watchdog.py`, new `src/backend/utils/run_activity.py`; `app.py`, `gstreamer/gst_pipeline.py`, `workflow_engine/pipeline_executor.py`, `workflow_engine/continuous_runner.py`, `endpoints/workflow.py`, `utils/capture_task_manager.py`, `utils/digital_input_thread_manager.py`, `utils/feature_configs_utils.py`, `utils/model_status_shadow.py`, `endpoints/feature_config.py`, `dda_triton/model_readiness.py` | 2.5, 2.6 |
| F5b | Content-aware publish, one lock hold per conversion | `dda_triton/model_convertor.py` | 2.8 |
| F5a | SOFT CLI dependency | `recipe-arm64-jp5.yaml`, `recipe-arm64-jp6.yaml`, `recipe-arm64-jp7.yaml`, `recipe-amd64.yaml`, `recipe-amd64-nvidia.yaml`, `recipe-arm64.yaml`, `recipe.yaml`; the ECR rewriters in `gdk-component-build-and-publish.sh`, `portal-build.sh`, `publish-ecr-only.sh` | 2.9 |
| F6 | Portal deployment readiness warning | `edge-cv-portal/backend/functions/deployments.py`, `edge-cv-portal/backend/layers/shared/python/shared_utils.py` (an optional client `config`), `edge-cv-portal/frontend/src/pages/DeploymentDetail.tsx` | 2.10 |
| Docs | Follow-up note and cross-reference | `.kiro/specs/triton-model-load-race/bugfix.md`, `.kiro/specs/cold-model-first-run-failure/bugfix.md` | — |

All device-side changes ship in the LocalServer component. The backend re-seeds
the host copies of the model scripts and their helpers on every start
(`triton_setup.cp_model_conversion_files`), so model components need no
republish.

## Technology Stack (locked)

- **Device backend.** The existing FastAPI backend: CPython 3.11 on JP5 and
  x86, 3.10 on JP6, and the JP7 image's interpreter. It uses only the standard
  library (`threading`, `fcntl`, `os`, `json`, `re`, `signal`) and adds no
  dependency. New code must keep passing `test/backend-test/test_py310_compat.py`.
- **Host scripts.** The model component lifecycle runs on the host's system
  `python3`: 3.8 on JP5 and arm64 CPU hosts, 3.10 on JP6, 3.12 on JP7
  (`station_install/setup_station.sh`, around line 660). Host-run modules use
  the standard library, `requests` and protobuf only, and must run under
  Python 3.8; their tests also run under a real 3.8 interpreter (Testing
  Strategy).
- **Cross-process coordination.** `fcntl.flock` on files under
  `/aws_dda/dda_triton/locks/`, the `locks` sibling of the model repository.
  The backend container bind-mounts `/aws_dda` (`src/docker-compose.yaml`,
  `/aws_dda:/aws_dda`), so host and container see the same inode.
- **Native.** Triton Inference Server v2.45.0 with python_backend r24.04, built
  by `build.py` inside the edgemlsdk Dockerfiles. The backport is a `git apply`
  patch. JP7 stays on v2.63.0, untouched.
- **Recipes.** Greengrass recipe YAML. The publish rewriters are the embedded
  Python (PyYAML) in the three ECR publish scripts.
- **Portal.** A Python Lambda (`deployments.py`) and the React + Cloudscape
  frontend (`DeploymentDetail.tsx`), tested with vitest.
- **Tests.** pytest + hypothesis host-side under `test/backend-test/`, the
  in-image security gate, and vitest.

## Owner Decisions

The owner made these decisions on 2026-10-15. Where an earlier iteration of
this design or of bugfix.md said otherwise, they win.

| # | Decision | Where this design implements it |
|---|---|---|
| 1 | Backport python_backend's bounded wait for a stub's first message onto Triton 2.45.0. Triton is not upgraded. | D3, for JP5, JP6 and x86. JP7 keeps Triton 2.63.0. |
| 2 | Make LocalServer's dependency on `aws.greengrass.Cli` SOFT, and write a follow-up spec for the configuration that LocalServer reads only at startup. | D10; bugfix.md Operator notes; the follow-up spec `localserver-config-live-update`. |
| 3a | Allow an automatic backend restart on a detected wedge, provided that it clearly logs that it acted and why. | D6-D8: the ERROR line, the persistent record and its surfaces. |
| 3b | Self-heal is **on by default on every device**, with a documented per-device opt-out. This was decided later the same day. | D7's switch: the recipes ship `ModelLoadSelfHealEnabled: "true"`, a missing key means on, and a configuration merge of `"false"` or the device file opts a device out. |
| 4 | On-device verification may change the state of the JP5 test device (MIC-730), including deliberate wedge reproductions on unfixed builds. | On-Device Verification. |

Decision 3b supersedes the opt-in default that iteration 2 of this design had
adopted. Because self-heal runs everywhere by default, D6 and D7 keep every
safeguard:

- it acts only on positive wedge evidence, never on `LOADING` time alone;
- it never acts during load activity: stub CPU vetoes the stall verdict, and
  GPU load, where readable, defers the restart, so a TensorRT engine build is
  never cut off;
- it never interrupts a run that the wedge does not block, and it marks the
  blocked runs that it interrupts as failed;
- it restarts at most once per 30 min and three times per 24 h, then reports
  "self-heal exhausted";
- it triggers only on Triton wedges, and its record names any vLLM models
  that the restart dropped;
- every action writes one ERROR line and a persistent record.

## Glossary

- **Entry**: a model's three repository directories `base_<m>`, `marshal_<m>`
  and `<m>` under `/aws_dda/dda_triton/triton_model_repo` (`TRITON_MODEL_DIR`),
  where `<m>` is the component name (`model-…`).
- **Lock key**: the component name with a leading `base_` or `marshal_`
  removed. One lock per component covers all three directories.
- **Tracked load**: a load requested through `TritonEdgeClient.start_triton_model`,
  for which the backend holds the shared lock until the load reaches a
  confirmed terminal state, or an unconfirmed state has lasted 120 s (D1).
- **Confirmed terminal state**: `READY`, or `UNAVAILABLE` with a `reason`,
  which only edgemlsdk's `_markModelLoadFailed` writes. Every other state that
  is not `LOADING` (`UNAVAILABLE` without a reason, `UNKNOWN`, `UNLOADING`) is
  **unconfirmed**: it can be Triton's state from before a load that is still
  queued in edgemlsdk (D1).
- **Release**: the cleanup's removal of an entry. It unloads a READY model,
  renames the directories to trash names under the exclusive lock, and deletes
  them outside it.
- **Boot stamp**: `<boot8>-<boottime>`, the first 8 characters of the
  kernel's boot id (`/proc/sys/kernel/random/boot_id`) and the whole seconds of
  `CLOCK_BOOTTIME` when a name was made. Host and container share the kernel,
  so both see the same values; both survive a backend restart, and neither
  moves with the wall clock (D4).
- **Hidden directories**: `.staging-<directory>-<boot stamp>-<random>` (a
  publish in progress, from `tempfile.mkdtemp`) and
  `.trash-<directory>-<boot stamp>-<pid>` (a removal in progress) inside the
  repository directory, on the same filesystem as the entries. A hidden
  directory is **stale** when it comes from another boot or is more than
  600 s old by `CLOCK_BOOTTIME` (D4).
- **Load progress**: any change in Triton's model index other than a model
  becoming `LOADING`.
- **Load activity**: CPU used by the python stub processes
  (`triton_python_backend_stub`) of the models that are `LOADING`, which vetoes
  a stall verdict; and, for self-heal only, Jetson GPU load, which defers a
  restart (D6, D7).
- **Stalled**: the watchdog's verdict for a `LOADING` model with positive wedge
  evidence and no load activity (D6). It is distinct from the continuous
  ModelGate's time-only `ModelWait.stalled` flag, which stays unchanged.
- **Self-heal**: the watchdog's automatic restart of the backend process (D7).
  It is on by default; a device opts out.
- **Run ticket**: `run_activity`'s registration of one run, from the moment
  the run starts (an engine execution, a classic run request, a capture task,
  a digital-input trigger, or any other `run_pipeline` call) until it ends, so
  its capture and preparation count as in flight. A ticket is **in its gate**
  while its readiness gate waits on a model that is not READY (D7).
- **Quiesce**: the bounded phase before a self-heal restart in which
  continuous ticks are skipped, new workflow runs are accepted at once and
  count as in flight, and runs in flight drain (`run_activity` state
  QUIESCING, which expires by itself after `QUIESCE_MAX_S` = 120 s;
  RESTARTING follows the commit, D7). No run is ever held.
- **Interrupted run**: a triggered or manual engine execution that the wedge
  blocked (in its readiness gate or in `emltriton`) and that a self-heal
  restart cuts off. It is marked failed before the restart (D7).
- **Load outcome record**: F3's per-model JSON, written by a Startup that could
  not confirm READY.
- **Self-heal record**: `/aws_dda/dda_triton/model_status/self_heal.json`, the
  persisted attempt history.
- **F / F'**: the unfixed and the fixed code.

## Bug Details

### Bug Condition

`isWedgeCondition`, `isSilentSuccess` and `isCascade` in bugfix.md. In one
line: a removal or replacement of a model's entries lands during an in-flight
load, before the base stub `stat()`s `model.py`, on python_backend older than
r25.08.

### Mechanism, with the code that implements each link

All line numbers were re-checked against this worktree.

| Link | Code |
|---|---|
| The Shutdown deletes unconditionally | `src/backend/dda_triton/convert_model_cleanup.py:47-59` (`stop_model` only logs a non-200), `:62-79` (`shutil.rmtree` at `:72`). The HTTP request has no timeout. |
| The Shutdown lifecycle | `edge-cv-portal/backend/functions/greengrass_publish.py:340-346`: `python3 /aws_dda/convert_model_cleanup.py --model_name <component>`, `Timeout: 900`, root |
| The publish deletes before it renames, one directory at a time | `src/backend/dda_triton/model_convertor.py:262-269` (`clean_directory(published_path)` then `os.replace`), called once per directory at `:604`, `:643`, `:682` |
| The Startup hides the outcome | `model_convertor.py:769-795`; `start_model(model_name)` at `:788`, whose result is ignored; `start_model` `:110-169` never raises |
| Start and stop refusals | `src/backend/utils/feature_configs_utils.py:279-299` (start only from UNKNOWN/UNAVAILABLE), `:314-334` (stop only from READY) |
| The run gate requests a load immediately | `src/backend/dda_triton/model_readiness.py:219-240` |
| The continuous gate checks files first | `src/backend/workflow_engine/model_gate.py:285-335` (`_file_state`), `QUIET_PERIOD_S` `:77` |
| The only Python load path | `src/backend/dda_triton/triton_edge_client.py:147-153` (`start_triton_model`), called from `feature_configs_utils.py:288`, `model_readiness.py:230`, `model_gate.py:397` |
| One load thread; Triton core logging off | `src/edgemlsdk/src/src/mlops/triton/triton_server.cpp:459`, `:461-464` |
| A failed load becomes UNAVAILABLE | `triton_server.cpp:216-276` (`_LoadModel`), `:204-214` (`_markModelLoadFailed`) |
| The unbounded stub wait | python_backend r24.04 `src/stub_launcher.cc`, Linux `StubLauncher::Launch()`, `parent_message_queue_->Pop()` (line 461). The stub exits at `src/pb_stub.cc` line 2081 after the model-file check at line 1963. |
| 2.45.0 is built here | `src/edgemlsdk/Dockerfile.jp5:212-244`, `Dockerfile.jp6:199-231`, `Dockerfile:318-370` |
| HARD CLI dependency | `recipe-arm64-jp5.yaml:12-13`, the same in the other six LocalServer recipes |
| Model components depend HARD on LocalServer | `greengrass_publish.py:316-320` |
| Startup-only configuration | `src/backend/workflow_engine/runtime.py:279-300` (`workflowTuning`) |
| The shadow is refreshed only by polling | `src/backend/endpoints/feature_config.py:89-151`; `utils/model_status_shadow.py` |

### Examples

- **2026-10-14, MIC-730, 1.0.54 (the bug).**
  1. A deployment adds a PPE model component.
  2. The CLI restarts, LocalServer stops, and the new component's Startup
     starts.
  3. 1.9 s later the component's dependency-restart Shutdown starts.
  4. The new backend comes up. The Startup's load is enqueued at 14:52:17.12.
  5. The Shutdown gets 403 "LOADING" and deletes the three directories at
     14:52:18.78.
  6. The base stub fails "Python model file not found" at 14:52:21.20.
  7. Triton logs nothing more for 67 min, and three other models queue
     `LOADING` behind it.
  8. The Startups give up and exit 0, and the deployment reports COMPLETED.
  9. A backend restart recovers it: the same files load in 16.4 s.
- **2026-07-31, MIC-730, 1.0.32 (the bug).** The cookies Shutdown deleted
  `base_model-cookies…` 154 ms before the stub's read. The models were wedged
  for 13+ min.
- **2026-09-30, MIC-730, 1.0.50 (the bug).** A continuous workflow's load was
  in flight when the component's cleanup deleted its directory. The device was
  wedged for 19 min.
- **Not the bug: a LocalServer-only deployment with idle models.** The cleanup
  sees UNKNOWN, nothing is loading, and the Startups re-convert. It is harmless
  today, and it must stay working.
- **Not the bug: JP7 with Triton 2.63.0.** The same deletion makes the stub
  fail. The bounded wait turns that into UNAVAILABLE, a retry follows, and all
  models are READY within about 40 s.

## Expected Behavior and Preservation

The expected behavior is bugfix.md 2.1-2.11 and the preserved behavior is
3.1-3.14. These preservation items carry the most weight in this design:

- **3.2 and 3.3.** The publish layout and the Startup's success path are
  byte-for-byte as today.
- **3.4.** The start and stop contracts are unchanged, except for one new 409
  during a removal.
- **3.5.** A READY model pays nothing. No run is ever held; continuous ticks
  are skipped only during a self-heal's quiesce, and runs are refused only
  while it restarts the backend.
- **3.6.** The vLLM path is untouched.
- **3.9.** The continuous ModelGate's results are identical.
- **3.10.** The empty-repository guard comes first in every new Triton reader.
- **3.11.** A healthy device stays silent.
- **3.14.** The host scripts still run under Python 3.8.

## Design Decisions

### D1 — One cross-process lock per model (F1)

The cleanup runs on the host. The load requests run inside the backend
container. In the cascade, the cleanup often starts while the backend is down
or not yet reachable: on 2026-10-14 the Shutdown spent 21 s on "connection
refused". Three designs were considered:

- **A backend `/release` endpoint behind an in-process lock (rejected).** An
  in-process lock cannot coordinate with a host process. Every case where the
  script cannot reach the backend would fall back to today's unguarded delete:
  the cascade, a hung API, and stations with authorization enabled, which serve
  only TLS.
- **HTTP-only polling in the script (rejected).** It keeps the check-then-delete
  gap: a load can start between the state read and the `rmtree`. It also gives
  no protection when the backend cannot be reached.
- **Chosen: `fcntl.flock` on `/aws_dda/dda_triton/locks/<key>.lock`.**
  - Every tracked load holds the lock **shared** from its request until the
    model reaches a terminal state.
  - A removal (D2), and a publish of changed content (D9), takes it
    **exclusive**.
  - The kernel drops a process's locks when the process dies. A backend restart,
    including self-heal, can never leave a stale lock.
  - Whether a load is in flight is then known without HTTP.

**The lock contract** (`dda_triton/model_locks.py`, a host-shared module):

- **Key.** `lock_key(name)` strips one leading `base_` or `marshal_` and
  validates the result (see "Input Validation"). It raises `ValueError` on an
  invalid name.
- **Location.** `lock_dir_for(repo_dir)` is
  `os.path.join(os.path.dirname(os.path.abspath(repo_dir)), "locks")`. For the
  production repository `TRITON_MODEL_DIR` (`/aws_dda/dda_triton/triton_model_repo`,
  `dda_triton/constants.py:44`) that is `LOCK_DIR = /aws_dda/dda_triton/locks`;
  host and container compute it from the same constants file. Every API takes
  `lock_dir=None`, which means `LOCK_DIR`. The cleanup and the convertor pass
  `lock_dir_for(<the repository they operate on>)`, so a test on a temporary
  repository gets a temporary lock directory without patching.
  `TritonEdgeClient` has no attribute for the repository it serves today
  (`triton_edge_client.py:62-68` passes `model_dir or TRITON_MODEL_DIR`
  straight to `create_server`). `__init__` adds
  `self.model_dir = model_dir or TRITON_MODEL_DIR`, after its singleton
  early return and before `create_server(self.model_dir, …)`, and
  `start_triton_model` passes `lock_dir=lock_dir_for(self.model_dir)` to
  `request_load`, so the backend and the scripts lock the same directory for
  any repository, including the test repositories that
  `test_triton_edge_client.py` builds. The lock file is
  `os.path.join(lock_dir, key + ".lock")`.
  - The directory is created with mode 0755 if it is missing; files with mode
    0644. Both host and backend run as root.
  - Lock files are never unlinked. An unlink while another process holds the
    lock would let a new opener lock a different inode.
- **API.** `acquire(name, exclusive, timeout_s, poll_s=1.0, on_wait=None, lock_dir=None)`
  - It opens the file with `O_RDWR | O_CREAT | O_CLOEXEC`.
  - It polls `flock(fd, (LOCK_EX or LOCK_SH) | LOCK_NB)` until it succeeds or
    `timeout_s` passes. `timeout_s=0` means one non-blocking try.
  - `on_wait(elapsed_s)` is called before each sleep.
  - It returns a `LockHandle`. It raises `LockTimeout` when the lock stays held,
    and `LockUnavailable` when the file cannot be opened or locked for another
    reason (any `OSError`).
  - `clock` and `sleep` are injectable for tests.
- **Release.** `LockHandle.release()` calls `flock(fd, LOCK_UN)` before
  `os.close(fd)` and is idempotent. The explicit `LOCK_UN` matters because a
  process forked without `exec` (the vLLM engine core) shares the open file
  description. `O_CLOEXEC` keeps the descriptor out of the python stubs, which
  python_backend starts through `fork` + `execvp`.

**The backend side** (`dda_triton/load_tracker.py`, backend-only):
`request_load(model_id, native_load, read_states, lock_dir=None)` is called
from `TritonEdgeClient.start_triton_model`. That is the single choke point: its
three callers are `start_model_triton`, `ensure_model_ready` and
`ModelGate._load`, and after a restart they can all request the same model at
once (FastAPI runs `/start` on its thread pool). One module lock makes the
check, the lock acquisition, the native enqueue and the registration a single
critical section, and the tracker releases a handle only under the same lock.

`read_states` is the new `TritonEdgeClient.index_states()`: one native
`ListModels` call through `_list_models_raw(quiet=True)` (so it shares the
client's `_list_models_lock` and its empty-answer retries), parsed into
`{name: (state, reason)}`. `_list_models_raw` gains `quiet: bool = False`;
with `quiet=True` its "returned an empty response (attempt n/3)" line
(`triton_edge_client.py:91-95`) is logged at DEBUG instead of WARNING, and the
existing callers keep the default. `index_states()` raises on failure and
logs nothing itself, so neither a failing nor an empty read logs anything
above DEBUG every 2 s (the tracker) or every 30 s (the watchdog, D6). It does
not repeat `list_triton_models`' ERROR line either.

```python
TRACK_POLL_S = 2.0
TRACK_UNCONFIRMED_S = 120.0

_lock = threading.Lock()
_tracked = {}        # model_id -> _Track(handle, generation, unconfirmed_since)
_requested = {}      # model_id -> (wall, mono) of the latest accepted request; kept after release
_read_states = None  # the index reader of the latest request (one client per process)

def request_load(model_id, native_load, read_states, lock_dir=None):
    global _read_states
    with _lock:
        _read_states = read_states
        track = _tracked.get(model_id)
        if track is not None:                       # 1: already tracked
            native_load(model_id)                   #    edgemlsdk dedupes when its cache says LOADING
            track.generation += 1
            track.unconfirmed_since = None
            _requested[model_id] = (time.time(), time.monotonic())
            return
        try:                                        # 2: non-blocking shared lock
            handle = model_locks.acquire(model_id, exclusive=False, timeout_s=0,
                                         lock_dir=lock_dir)
        except model_locks.LockTimeout:
            raise ModelReleaseInProgress(model_id)  #    INFO line; nothing reaches the native load
        except model_locks.LockUnavailable:
            handle = None                           #    WARNING once per key; fail open as today
        try:
            native_load(model_id)                   # 3: LoadModel only enqueues
        except Exception:
            if handle is not None:
                handle.release()
            raise                                   #    today's error path
        _tracked[model_id] = _Track(handle, generation=0, unconfirmed_since=None)  # 4: register
        _requested[model_id] = (time.time(), time.monotonic())
        _ensure_tracker_thread()

def _classify(state, reason):
    state = str(state or "").strip().upper()
    if state in ("", "LOADING"):
        return "in_flight"                          # an empty read counts as a failed read
    if state == "READY" or (state == "UNAVAILABLE" and reason):
        return "terminal"                           # confirmed: a load requested after the
                                                    # registration finished or failed
    return "unconfirmed"                            # UNAVAILABLE without a reason, UNKNOWN,
                                                    # UNLOADING, or absent from the index

def _tracker_pass(now_mono):                        # thread "triton-load-tracker", every TRACK_POLL_S
    with _lock:
        pending = [(m, t.generation) for m, t in _tracked.items()]
        reader = _read_states
    if not pending:
        return
    try:
        states = reader()                           # one index read per pass, outside _lock
    except Exception:
        return                                      # DEBUG; every handle kept, retried next pass
    for m, generation in pending:
        verdict = _classify(*states.get(m, ("UNKNOWN", None)))
        with _lock:
            t = _tracked.get(m)
            if t is None or t.generation != generation:
                continue                            # a newer request owns the handle now
            if verdict == "in_flight":
                t.unconfirmed_since = None          # no time bound: a wedged load keeps its lock
                continue
            if verdict == "unconfirmed":
                if t.unconfirmed_since is None:
                    t.unconfirmed_since = now_mono
                if now_mono - t.unconfirmed_since < TRACK_UNCONFIRMED_S:
                    continue
                # INFO "released ... without confirmation" (log catalogue)
            del _tracked[m]
            if t.handle is not None:
                t.handle.release()
```

- **Why "terminal" needs confirmation.** A state other than `LOADING` does not
  prove that the tracked load ended:
  - edgemlsdk's `LoadModel` writes `LOADING` into its cache and only enqueues
    the job (`triton_server.cpp:187-190`). Triton marks the model `LOADING` only
    once a job-queue worker calls `TRITONSERVER_ServerLoadModel` (`:231`).
  - Until then, every index refresh copies Triton's previous state into the
    cache: `ModelMetadata` (`:327`) and `ListModels` (`:356`). For a model that
    Triton loaded and unloaded, or that failed before, that state is
    `UNAVAILABLE`. These refreshes run constantly: the Startup and the cleanup
    poll `/feature-configurations` every 3 s, ModelGate every 3 s, the watchdog
    every 30 s, and `/stop` reads `ModelMetadata`.
  - The job can wait in the queue. `MultiThreadedJobQueue` has 4 workers and
    frees only its oldest (`scheduling.h:14`, `:203-212`), so a fifth job waits
    behind a slow or wedged load.
  - Only `_markModelLoadFailed` (`:204-214`) writes a `reason` (`:210`). The
    index refreshes copy only `state`, and `LoadModel` replaced the whole cache
    entry when it enqueued (`:188-190`). So a `reason` read after the request
    means that a load enqueued after it failed, and `READY` means that one
    finished.
  - Every other state is unconfirmed. It keeps the lock until it has lasted
    `TRACK_UNCONFIRMED_S`. That bounds the cases that never confirm, such as a
    load that finished and was unloaded between two passes, without releasing
    during a queued reload's normal wait.
- **The residual.** A job still queued in edgemlsdk after 120 s of an
  unconfirmed state is invisible to Triton's index, so neither the tracker nor
  the cleanup's own re-check under the lock (D2 step 4a) can see it. If its
  entries are removed meanwhile, Triton fails it with NOT_FOUND when it runs;
  if the removal lands between its configuration read and its stub start, F2
  bounds the stub wait to about 10 s. The same holds for a second job that
  edgemlsdk enqueues for one model after an index refresh turned the cached
  `LOADING` into `UNAVAILABLE` (its dedupe at `:174-184` reads the cache): the
  tracker releases after the first job's terminal state. Either way the
  outcome is a fast `UNAVAILABLE`, not a wedge.
- **Why holding `_lock` across `native_load` is safe.** edgemlsdk's
  `LoadModel` only checks the cache and enqueues under `_metadata_mutex`
  (`triton_server.cpp:136-193`); no thread holds that mutex across a Triton
  load (`_LoadModel` calls `TRITONSERVER_ServerLoadModel` without it), so the
  call returns promptly even during a wedge.
- **Why the index read happens outside `_lock`.** It takes the client's
  `_list_models_lock` and edgemlsdk's `_metadata_mutex`. Reading under `_lock`
  would let a slow read block every `request_load`; reading outside it and
  re-checking the generation keeps them independent. It never takes
  `TRITON_NATIVE_LOCK`, which pipeline state changes hold
  (`gst_pipeline.py:250`).
- **Why the generation check.** Between a read and the release, a new request
  for a model whose last load failed can enqueue a new load (step 1). The
  generation bump makes the tracker skip that release; the next pass reads
  `LOADING` and keeps the handle.
- **The cache side effect is not new.** Each pass refreshes edgemlsdk's cache
  from Triton's index, as every other index reader already does. The tracker
  reads only while it tracks a load, and once per pass for all tracked models.
- **The tracker thread** is a daemon started on first use that runs for the
  life of the process. Its loop calls `_tracker_pass` inside
  `try/except Exception`: the first failing pass of a streak logs one ERROR
  with the traceback (log catalogue), later ones log at DEBUG, the next
  successful pass ends the streak, and every handle is kept meanwhile, so a
  failing pass can only delay a release, never make one early.
  `ensure_tracker_thread()` starts a new thread when the current one is not
  alive (for example after a `BaseException`). `request_load` calls it, and so
  does every watchdog tick while anything is tracked (D6 step 1), so a dead
  tracker is replaced within 30 s even when no new load is requested. Until
  then its shared locks stay held, which can only make a cleanup skip its
  delete (2.1), never delete under a load.
- `requested_at(model_id)` returns `(wall, mono)` of the latest accepted
  request, kept after release. The watchdog uses it only when it is newer than
  its own latest sighting of the model outside `LOADING` (D6, step 5).

`start_model_triton` maps `ModelReleaseInProgress` to HTTP 409 with the
detail text in the log catalogue. The exclusive lock is held by a cleanup's
removal (D2) or a changed publish's replacement (D9), so the text names both.
The two gates must not treat the refusal as "a load is under way", because
their bookkeeping would then never ask again:

- `ensure_model_ready` sets `kicked = True` before its request
  (`model_readiness.py:224`). On `ModelReleaseInProgress` it sets `kicked`
  back to False, so the next poll (3 s later) inspects the entry again (D4)
  and requests again. An entry that the removal deleted is then absent, so the
  request fails fast as `UNAVAILABLE` (NOT_FOUND) and so does the run; a
  present, stable entry (the publish replaced it) is loaded. Without this a
  run on a removed model would wait the full 600 s and end with the misleading
  "a first load can take minutes".
- `ModelGate._maybe_load_unknown` adds the model to `_load_requested` before
  `_load` (`model_gate.py:383-384`). `_load` now returns False on
  `ModelReleaseInProgress` (True otherwise), and `_maybe_load_unknown` then
  discards the model from `_load_requested`, so the next check re-runs the
  file checks first: a removed entry reports NOT_DEPLOYED, a replaced one
  waits for its quiet period and is loaded. `_maybe_reload_unavailable` keeps
  its backoff.
- Both log the refusal as one INFO line without a traceback (log catalogue):
  the per-run gate once per call, ModelGate once per model per streak of
  refusals. That replaces today's WARNING with `exc_info`
  (`model_readiness.py:235-238`, `model_gate.py:400-401`) for this case only;
  other exceptions keep it.

**Residual: untracked loads.** `emltriton` itself enqueues a native load when a
pipeline starts against a model that is not READY. That happens whenever a
pipeline starts against a model that is not READY without passing a readiness
gate: a digital-input run, which has no gate
(`DigitalInputThread.execute_workflow` →
`GstPipelineExecutor.execute_workflow_pipeline` → `run_pipeline`), or a run
whose gate failed open. So after any backend restart, including every
self-heal restart, the first digital-input trigger on each `UNKNOWN` model
enqueues such a load. Such a load is untracked, so it holds no lock. The
cleanup's HTTP re-check under the exclusive lock (D2 step 4c) still sees it as
`LOADING` and waits. D9's publish relies on the lock alone, so a changed publish
can swap under it (D9 step 5). F2 makes an untracked load that loses its files
fail fast instead of wedging.

### D2 — The cleanup waits, unloads, renames, then deletes (F1)

`convert_model_cleanup.py` keeps its CLI (`--model_name <component>`), its log
format (`%(levelname)s:%(message)s`), the `Cleaned directory: <path>` and
`Directory cleanup finished` lines, and `exit(1)` for a missing or invalid
argument. A new `release_model(model_name, repo=TRITON_MODEL_DIR, ...)` holds
the logic; its HTTP session, clock and sleep are injectable. Every HTTP call
gets an explicit 10 s timeout.

**Exit codes.** Today the script exits 1 on a missing argument and on any
exception, including an `rmtree` failure (`convert_model_cleanup.py:75-79`).
After the change only argument errors exit 1. `__main__` validates the
argument first, then calls `release_model` inside `try/except Exception`: an
unexpected exception logs the ERROR line with its traceback (log catalogue),
leaves the entries as they are, and exits 0. A Shutdown that fails during a
deployment can leave the component errored, while entries left in place are
harmless: the next cleanup of the model, or F5c, removes them, and anything
already renamed to a trash name is removed by the next sweep (D4).
`release_model` releases the exclusive lock in a `finally`, and the kernel
releases it anyway when the process exits. `SystemExit` and
`KeyboardInterrupt` are not caught.

```
RELEASE_WAIT_S = 300; UNLOAD_WAIT_S = 60; HTTP_POLL_S = 3; LOCK_POLL_S = 1
PROGRESS_LOG_EVERY_S = 30

validate model_name                       # invalid -> ERROR, exit 1
reachable = wait_for_server(...)          # existing ladder, 45-95 s worst case
deadline  = now + RELEASE_WAIT_S
loop:
  state = read_state(m) if reachable else None     # GET /feature-configurations
  if state == LOADING (or UNLOADING): log "a load is in flight, waiting" (once)
  try EX = acquire(m, exclusive, timeout = deadline - now, on_wait = progress log,
                   lock_dir = lock_dir_for(repo))
  except LockTimeout:   WARNING "skipped deleting ... still in flight"; exit 0
  except LockUnavailable: -> fallback (below)
  under EX:
    4a  state = read_state(m): always one HTTP read (10 s timeout), whatever
        the ladder returned, because the backend may have come up meanwhile;
        None when it fails (treated as unknown)
    4b  READY -> GET /stop; poll until state not in {READY, UNLOADING}
        (<= UNLOAD_WAIT_S; on timeout WARNING and continue: no load is in flight)
    4c  LOADING/UNLOADING (an untracked load) -> release EX, sleep HTTP_POLL_S,
        continue loop while now < deadline, else WARNING skip; exit 0
    4d  rename each existing base_<m>, marshal_<m>, <m> to
        _trash_name(<dir>): repo_entries.trash_name(<dir>) =
        .trash-<dir>-<boot stamp>-<pid> (D4), or, when repo_entries could not
        be imported, the unstamped .trash-<dir>-<pid>-<time_ns> (below)
        (rename failure other than ENOENT -> rmtree that directory in place
         while EX is still held, WARNING)
  release EX
  rmtree each trash directory; log "Cleaned directory: <original path>"
        (ENOENT: a concurrent sweep already removed it; treated as done)
  repo_entries.sweep_stale_hidden_dirs(repo)        # stale .trash-* and .staging-*, D4
  clear m's load outcome record; log "Directory cleanup finished"; exit 0

fallback (lock file unusable, or model_locks not importable):
  not reachable -> WARNING "cannot prove no load is in flight"; skip; exit 0
  reachable     -> poll HTTP state until it is terminal, that is neither
                   LOADING nor UNLOADING (<= deadline), READY ->
                   stop + unload wait, then rename and delete without the lock
                   (WARNING names the missing lock); still LOADING or
                   UNLOADING at the deadline -> skip
```

**Imports.** The script imports `dda_triton.model_locks`,
`dda_triton.repo_entries` and `dda_triton.model_status_records` inside
`try: … except Exception:`. The backend re-seeds those files with
`shutil.copy2` on every start, which is not atomic, so a half-written module
can raise `SyntaxError` as well as `ImportError`. A failed `model_locks` import
takes the fallback path; a failed `repo_entries` import skips the sweep and uses
the unstamped trash name; a failed `model_status_records` import skips the
record clear. Each logs one WARNING naming the module and the error, and none
fails the Shutdown. Step 4d names its trash directories through a local helper,
so a missing `repo_entries` cannot break the removal:

```python
def _trash_name(directory):
    if repo_entries is not None:
        return repo_entries.trash_name(directory)
    # Unstamped: the sweep ages it by mtime (D4).
    return ".trash-{0}-{1}-{2}".format(directory, os.getpid(), time.time_ns())
```

**Bound.** The ladder (≤ 95 s), the lock wait and any re-check loops (≤ 300 s
together), the fresh read and the `/stop` request under the lock (≤ 10 s
each), the unload wait (≤ 60 s) and the deletes add up to about 480 s, well
inside the Shutdown's `Timeout: 900` (`greengrass_publish.py:342`).

**Outcomes in practice.**

- **The 2026-10-14 ordering.**
  1. The Shutdown reaches the backend at the +21 s probe and reads `LOADING`.
  2. It waits on the exclusive lock while the tracked load (about 16 s) runs.
  3. The tracker sees READY and releases the shared lock.
  4. The Shutdown takes the lock, stops the model, waits for the unload,
     renames, and deletes.
  5. The next Startup republishes and loads.
- **A wedged load.** The shared lock is never released, the wait expires, and
  the cleanup skips with a WARNING. Detection (D6) reports the wedge, and
  self-heal (D7) clears it unless the device opted out.
- **An undeploy.** The entries are removed after at most the bounded wait (3.1).
  A skipped delete leaves stale entries. A later cleanup of the same model
  removes them, and F5c removes them in general.
- **Stale hidden directories.** The sweep removes leftovers from a crash
  between the rename and the delete, and staging directories of an interrupted
  publish (D4).

**Hidden directories elsewhere.** `triton_repo_has_models` ignores names that
start with `.`, so hidden leftovers can never make an empty repository look
populated (3.10). `get_features_triton` and the watchdog skip such names in
Triton's index.

### D3 — Backport python_backend's bounded first-message wait (F2)

**What upstream changed.**

- Upstream commit `c94f397` ("Improve error handling for Python backend model
  initialization failures", released in r25.08) changes `StubLauncher::Launch`
  in both branches:
  - The unbounded `parent_message_queue_->Pop()` becomes
    `ReceiveMessageFromStub(message, 10000)`. On error the stub is killed with
    `KillStubProcess()`, and the error is thrown (auto-complete stub) or
    returned (model instance stub).
  - `ReceiveMessageFromStub` gains a `timeout_miliseconds` parameter
    (default 1000).
  - `ModelInstanceStubProcess` waits on the initialize response in 5 s
    iterations.
  - The scope guard pushes its dummy message only if the queue exists.
- Upstream commit `9d2c513` ("Add error handling in case of AutocompleteStub
  Failure", in r24.06 and later, so not in r24.04) guards
  `WaitForStubProcess` with `if (stub_pid_ != 0)`. **The backport needs it.**
  - `KillStubProcess()` reaps the stub and zeroes `stub_pid_`.
  - `Launch`'s scope guard then calls `WaitForStubProcess()` again.
  - Without the guard that becomes `waitpid(0, …)`, which waits for any child of
    the backend, such as a sibling model's stub. That would recreate the hang.

**The patch.** Add `src/edgemlsdk/patches/edgeml-triton-python-backend.diff`,
next to `edgeml-triton-server.diff` and `edgeml-triton-core.diff`. It is a pure
`git diff` relative to the python_backend repository root, covering
`src/stub_launcher.cc` and `src/stub_launcher.h`. Regenerate it from a **full**
clone: a `--single-branch -b r24.04` clone contains neither commit
(`9d2c513` first ships in r24.06, and `c94f397` exists only on the `r25.08`
branch).

```
git clone https://github.com/triton-inference-server/python_backend.git
cd python_backend
git checkout -b edgeml-backport c848884                        # origin/r24.04 head
git cherry-pick -n 9d2c513d41368d4932ea3e6207cbb248d5d8c9ee    # WaitForStubProcess guard (r24.06)
git cherry-pick -n c94f39760a5454738cdcf36f5caff3292a87a606    # bounded first-message wait (r25.08)
git checkout HEAD -- src/pb_stub.cc
git diff --cached HEAD -- src/stub_launcher.cc src/stub_launcher.h \
  > edgeml-triton-python-backend.diff
```

This was run during design: both cherry-picks apply cleanly on `c848884`, the
result is 2 files and 10 hunks (+53/−13), and
`git -C <shallow r24.04 clone> apply --check <absolute patch path>` passes. The
patched `stub_launcher.cc` contains `initialization_timeout_ms = 10000`, the
`if (stub_pid_ != 0)` guard and the string "Waiting for the stub health
monitoring thread to start", none of which r24.04 has. c94f397's `pb_stub.cc`
hunk (stub-side destructor cleanup on a preinitialize failure) is deliberately
left out. It is not needed for the bounded wait, and the stub already exits on
that path today.

**Behavior after the patch.** The parent waits for the stub's
health-thread-started message in 10 s health-check iterations. The stub starts
that thread right after `Instantiate()` and the interpreter start
(`pb_stub.cc` main), so 10 s is generous.

- **A stub that died, or never reports.** It is killed and reaped, `Launch`
  returns the error, and the model instance initialization fails. edgemlsdk's
  `_LoadModel` then records `UNAVAILABLE` with Triton's message (for example
  "…Stub process '<instance>' is not healthy.") in about 10-11 s, and at most
  about 21 s if the stub died holding the health mutex. Either way it is inside
  2.4's 30 s.
- **A stub that is alive but blocks in `initialize()`.** It still holds the load
  thread, because upstream bounds only the first message. D6/D7 handle that.

**Dockerfile mechanics.**

- **Why a pre-clone is needed.** `build.py` v2.45.0 clones every backend into
  `<build-dir>/<backend>`. Unless `--no-force-clone` is given, it deletes that
  directory first (`gitclone()`: `if not FLAGS.no_force_clone:
  self.rmdir(clone_dir)`, then `if [[ ! -e python ]]; then git clone …`). So
  the patched source must be in place before `build.py` runs, and `build.py`
  must be told not to re-clone.
- **Nothing else is affected.** In these Dockerfiles nothing else goes through
  `gitclone` (no `--repoagent`, no `--cache`). The core is built from the
  checked-out server tree, and the helper repositories come through CMake
  `FetchContent`.
- **The insertion.** Only new lines are inserted; no existing line changes,
  including its trailing whitespace, so the masked-bytes baselines change only
  by these additions. Directly after the existing
  `git apply edgeml-triton-server.diff && \` line of each Triton `RUN`
  (jp5 `:223`, jp6 `:210`, x86 `:351`), insert:

```
    git clone --single-branch --depth=1 -b r24.04 https://github.com/triton-inference-server/python_backend.git build/python && \
    git -C build/python apply <absolute patch path> && \
    grep -q "initialization_timeout_ms = 10000" build/python/src/stub_launcher.cc && \
```

  Then insert one new argument line, `--no-force-clone \`, directly after the
  `python3.11 build.py … --no-container-build \` line.
- **The patch path is always absolute.** `git -C build/python` changes
  directory before it parses its arguments, so a relative patch path resolves
  inside `build/python` and fails with "can't open patch" (reproduced during
  design). The `apply` argument is exactly the `COPY` destination below.
- **The binary sentinel.** It checks that the patched code is in the built
  library:

```
grep -a -q "Waiting for the stub health monitoring thread to start" <install dir>/backends/python/libtriton_python.so
```

  - The string is new in c94f397 and absent from r24.04.
  - Both artifacts come from that install tree:
    - the `triton-python-backend` deb, from `create_aws_triton_deps_pkg.sh:49`,
      via `build/python/build/install_manifest.txt`;
    - the tarball, via build.py's copy into `build/opt/tritonserver`.
- **The paths in each Dockerfile:**
  - `Dockerfile.jp5`, `Dockerfile.jp6`:
    - Add `COPY patches/edgeml-triton-python-backend.diff /tmp/edgeml-triton-python-backend.diff`
      after the two existing `COPY` lines (jp5 `:213-214`, jp6 `:200-201`).
    - The `apply` argument is `/tmp/edgeml-triton-python-backend.diff`.
    - Insert the sentinel as
      `    grep -a -q "…" build/python/install/backends/python/libtriton_python.so && \`
      directly after the `--backend python --build-dir=$(pwd)/build && \` line
      (jp5 `:238`, jp6 `:225`), before the existing `mv`.
  - `Dockerfile` (x86, x86 NVIDIA and arm64 CPU):
    - Add `COPY patches/edgeml-triton-python-backend.diff /dependencies/server/edgeml-triton-python-backend.diff`
      after `:323-324`.
    - The clone, apply and source-grep lines go after `:351`, inside the `RUN`
      that starts at `:349` (its working directory is `/dependencies/server`).
      The `apply` line is
      `    git -C build/python apply /dependencies/server/edgeml-triton-python-backend.diff && \`.
    - That `RUN` ends at `:364` without a continuation, so the binary sentinel
      is a new
      `RUN grep -a -q "…" /dependencies/server/build/python/install/backends/python/libtriton_python.so`
      between `:364` and `:365`.
- **Failure behavior.** `git apply` is all-or-nothing and both `grep`s fail the
  `RUN`, so a patch that no longer applies or did not take effect fails the
  build loudly.
- **Other effects.**
  - The clone tracks the `r24.04` branch head, exactly as `build.py` does today.
  - Changing the Triton layer rebuilds every later layer. That is part of the
    usual 1-2 h build.
  - `src/edgemlsdk/Dockerfile.jp7` is not changed (3.7).

**Why not upgrade Triton.** It was the owner's decision. An upgrade would also
move CUDA and TensorRT compatibility on JP5/JP6 and the python_backend ABI that
edgemlsdk links against. The backport changes one file of one backend.

### D4 — The per-run gate waits for stable entries; stale hidden directories are swept (2.3)

`ensure_model_ready` (`dda_triton/model_readiness.py`) changes in its
`UNKNOWN` branch and its `LOADING` branch, and holds a gate wait while it
polls ("Run activity", below); its signature does not change.

**The `UNKNOWN` branch, before the first kick.** It now inspects the entry with
`repo_entries.inspect_entry(TRITON_MODEL_DIR, target, wall=time.time,
quiet_period_s=ENTRY_QUIET_PERIOD_S)`, where `TRITON_MODEL_DIR` is
`dda_triton.constants.TRITON_MODEL_DIR` (`constants.py:44`, the repository
`triton_repo_has_models` checks) and `ENTRY_QUIET_PERIOD_S = 10.0`, the same
value as `model_gate.QUIET_PERIOD_S`.

- **Absent entry** (`inspection.present` is false): request the load as today.
  Triton fails it fast with NOT_FOUND and the gate fails fast on `UNAVAILABLE`.
  No stub runs, so nothing can wedge.
- **Present, but incomplete or changing** (`inspection.blocker` is set): do not
  request a load yet. Log one INFO line per call (see the catalogue) and keep
  polling every 3 s. The first poll that finds the entry stable requests the
  load exactly once.
- **Present and stable**: request the load as today.
- **A request refused with `ModelReleaseInProgress`** (a removal or a
  replacement holds the exclusive lock, D1): `kicked` goes back to False and
  one INFO line is logged per call, so the next poll inspects the entry again
  and requests again. Today's WARNING with a traceback stays for every other
  exception.

If the 600 s budget runs out while the entry never stabilized, the failure
message names the blocker and says that no load was requested (see the
catalogue).

**A user-visible change for permanently incomplete entries.** Today a run
whose `<m>` exists but whose `config.pbtxt`, a step's `config.pbtxt` or a
step's `model.py` is missing or unreadable requests the load, and Triton fails
it at once with its reason. Such an entry can be left by an interrupted
removal or a broken component. After this change the gate requests no load
for it, and the run fails only when its 600 s budget runs out. The message
names the blocker and, when the blocker is a missing or unreadable path, adds
the remedy: restart or redeploy the model component, so that its Startup
republishes the entry. The continuous ModelGate already behaves this way
(`INCOMPLETE`, no request). The gate does not request a load just to fail
such a run sooner: 2.3 allows a request only for a complete, stable entry,
and a load of an entry that is still changing is exactly what the wedge
needs. bugfix.md 2.3 states the change.

The inspector is injectable (`entry_inspector=None` means the real one). An
inspection that raises counts as "absent", so the gate fails open, in line with
its rule that it must never be the only reason a working run stops.

**The `LOADING` branch.** It asks the watchdog (`load_watchdog.stall_for(target)`,
injectable as `stall_lookup`). For a stalled model it returns at once with the
stall message: the evidence and the recovery action. It no longer waits out the
600 s.

**Run activity.** From its first read that is not `READY` until it returns,
`ensure_model_ready` holds `run_activity.gate_wait(target)` (D7). Both run
paths register their run ticket before they reach the gate (D7, "A run is
registered for its whole life"), and the gate wait marks that ticket as in
its gate. A self-heal decision therefore counts the run as blocked by the
wedge while it waits here, and as unblocked before and after. A restart that
cuts the run off reaches the ticket's `on_interrupt`, which the engine
registered with the ticket. If the gate wait is refused, because self-heal is
restarting the backend, it returns not-ready at once with the refusal text as
its message.

The gate does not adopt ModelGate's `UNKNOWN_LOAD_GRACE_S` (120 s after engine
start). That grace keeps a load away from a republish in progress. Here a
changed publish swaps under the exclusive lock (D9), and the 10 s quiet period
covers a fresh entry. An entry that a restart's cleanup removed is absent, so a
run fails fast with `UNAVAILABLE` (NOT_FOUND), as today, instead of waiting up
to 2 min. With the SOFT CLI dependency (D10), that window opens only when a
deployment restarts LocalServer itself.

**`repo_entries`** is host-shared (stdlib only, Python 3.8): the cleanup and
the convertor use its sweep, and it is seeded to `/aws_dda/dda_triton/` with
the other helpers (D9's seeding note).

`inspect_entry(repo, target, wall, quiet_period_s) -> EntryInspection`:

```
present: bool                  # os.path.isdir(os.path.join(repo, target)): ModelGate's
                               # NOT_DEPLOYED test (model_gate.py:227)
blocker: Optional[str]         # exactly ModelGate._file_state's result
missing_path: Optional[str]    # the path named by the first "is missing" check, in the
                               # order _file_state checks; None when nothing is missing
paths: Tuple[str, ...]         # the paths whose mtimes were compared; complete only when
                               # nothing is missing or unreadable
newest_path: Optional[str]
newest_mtime: Optional[float]
```

`blocker` is computed by moving `ModelGate._file_state`
(`model_gate.py:285-335`) here verbatim: the same path set, the same staging
check (`:318-321`), the same reason strings. `ModelGate._file_state` becomes
`return repo_entries.inspect_entry(self._repo, triton_model, wall=self._wall,
quiet_period_s=QUIET_PERIOD_S).blocker`, reading its module-level
`QUIET_PERIOD_S` at call time, so tests that patch it still work (3.9). The
watchdog calls it with `quiet_period_s=0` to get `missing_path`, `paths` and
`newest_mtime` for E1 (D6).

**Names of hidden directories.** They carry a boot stamp, so their age never
depends on the wall clock. An NTP step of more than 10 min is typical at boot
on Jetsons without a working RTC, which is also when Startups publish, so a
wall-clock age could make a live directory look stale.

- `repo_entries.boot_stamp() -> Optional[str]` returns `<boot8>-<boottime>`:
  the first 8 characters of `/proc/sys/kernel/random/boot_id` (lower-case hex)
  and `int(time.clock_gettime(time.CLOCK_BOOTTIME))`. It returns `None` when
  the boot id cannot be read.
- `repo_entries.staging_prefix(name)` is `.staging-<name>-<stamp>-`. A publish
  passes it to `tempfile.mkdtemp`, which appends 8 random characters, so the
  ModelGate glob `.staging-<name>-*` still matches.
- `repo_entries.trash_name(directory)` is `.trash-<directory>-<stamp>-<pid>`.
- Without a stamp, the names are today's `.staging-<name>-` prefix and
  `.trash-<directory>-<pid>-<time.time_ns()>`.

**The sweep.** `sweep_stale_hidden_dirs(repo, names=None,
stale_after_s=HIDDEN_DIR_STALE_S, stamp_reader=boot_stamp, wall=time.time)
-> List[str]`, with `HIDDEN_DIR_STALE_S = 600`:

- It parses each `.staging-*` and `.trash-*` name with
  `^\.staging-(?P<name>.+)-(?P<boot>[0-9a-f]{8})-(?P<bt>[0-9]+)-[a-z0-9_]{8}$`
  and `^\.trash-(?P<dir>.+)-(?P<boot>[0-9a-f]{8})-(?P<bt>[0-9]+)-(?P<pid>[0-9]+)$`.
- A stamped name from **another boot** is stale: no process survives a
  reboot.
- A stamped name from **this boot** is stale once
  `CLOCK_BOOTTIME - bt > 600`. Neither an NTP step nor a backend restart moves
  that clock.
- An **unstamped** name falls back to the directory's mtime against the wall
  clock, which is today's only signal. Such names come from the pre-fix
  convertor, or from a host where the boot id cannot be read.
- With `names` set, it considers only the staging directories of those names
  (the parsed `name`, or the `.staging-<name>-` prefix of an unstamped name)
  and no trash.
- It uses `shutil.rmtree`, ignores a directory that disappeared meanwhile,
  never raises (other errors are DEBUG and the directory stays for the next
  sweep), and returns the removed names so the caller logs one INFO line each.

It runs at the start of every publish (D9, for the model's three names), at the
end of every cleanup (D2) and on every watchdog tick (D6).

- **Why it is needed.** `_atomic_publish_model_dir` creates its staging
  directory with `mkdtemp` (`model_convertor.py:248-249`) and removes it only on
  its own exit paths. A Startup that is terminated mid-publish leaves it
  behind, and `_file_state` reports "is being published" for as long as it
  exists (`model_gate.py:318-321`). After this change that would hold back
  every triggered run of the model, not only continuous ones. The SIGTERM
  handling in D9 removes the common case. The sweep removes the rest: after a
  SIGKILL within about 10.5 min, and after a power loss at the first sweep of
  the next boot.
- **Why 600 s never touches live work.** A publish builds its staging
  directories in well under a second (two small copies, symlinks and
  `config.pbtxt`, written last), then waits at most 300 s for the exclusive
  lock before it swaps (D9). A removal renames and deletes within seconds.
  Ages come from the boot stamp, measured on `CLOCK_BOOTTIME`, so a live
  stamped directory is always younger than 600 s, whatever the wall clock
  does, and the ModelGate's results while a publish is live stay identical
  (3.9). Only unstamped names depend on the wall clock, and the fixed code
  creates them only when the boot id cannot be read.

### D5 — The Startup records an outcome it cannot confirm (F3)

**Where records live.** `dda_triton/model_status_records.py` is host-shared and
stdlib-only.

- `MODEL_STATUS_DIR = /aws_dda/dda_triton/model_status`, created with mode
  0755. Every function takes `records_dir=None`, which means that constant.
- A load outcome is `<component>.json`.
- The self-heal record is `self_heal.json`, and the self-heal kill switch is
  `self-heal.disabled`.

**Writes are atomic.** A write goes through `tempfile.mkstemp(dir=…,
prefix=".tmp-")`, `fsync`, `os.replace` and `chmod 0644`, and never raises:
`write_json_atomic` returns False and the caller logs an ERROR.

**Reads are tolerant.** `read_load_outcomes()` returns `{component: record}`
for files matching `^model-[A-Za-z0-9._-]+\.json$` that are ≤ 16 KiB and parse
as schema 1. It skips anything else (DEBUG).

**The load outcome record:**

```json
{
  "schema": 1,
  "model": "model-yolo-test-jetson-xavier-jp5",
  "componentVersion": "8.0.0",
  "outcome": "not_ready",
  "state": "LOADING",
  "reason": "still LOADING after 3 start attempts over 382 s; the load continues in the backend",
  "failureReason": null,
  "backendReachable": true,
  "attempts": 3,
  "startupStartedAt": "2026-10-14T14:51:55Z",
  "recordedAt": "2026-10-14T14:58:45Z",
  "pbStubMissingFile": null,
  "source": "model_component_startup"
}
```

**How `model_convertor.py` produces it.**

- **A new entry point.** `start_model_detailed(model_name, …) -> StartOutcome`
  returns `ready`, `state`, `reason`, `failure_reason`, `attempts`,
  `elapsed_s` and `backend_reachable`, and keeps the retry ladder and its
  constants unchanged.
- **The old API stays.** `start_model(...)` stays as
  `return start_model_detailed(...).ready`, so `test_start_model_retry.py`
  keeps working.
- **Observing the last state.** `_wait_for_model_ready` gains an optional
  `observer` callback that receives each observed state, so the last state is
  known without changing its return type.
- **`failureReason`.** It comes from one final read of `/feature-configurations`:
  `defaultConfiguration.failureReason` for an UNAVAILABLE model.
- **Caps on write.** `write_load_outcome` cuts `reason` and `failureReason` to
  2,000 characters each (`LOAD_OUTCOME_TEXT_MAX = 2000`), so a long Python
  `initialize()` traceback in Triton's error cannot push the record over the
  reader's 16 KiB limit and hide the outcome F3 exists to surface. Every other
  field is fixed-size or bounded by the model-name pattern, so the largest
  record the writer can produce stays well under 16 KiB.
- **Reason texts and log levels.** A load still in progress is not a failure
  (cold-model 2.6), so the level follows the reason:

  | Reason class | Reason text | Level |
  |---|---|---|
  | last state `LOADING` | "still LOADING after N start attempts over S s; the load continues in the backend" | WARNING |
  | `UNAVAILABLE` | "load failed (UNAVAILABLE): <failureReason>" | ERROR |
  | backend not reachable | "LocalServer backend was not reachable; no load was requested" | ERROR |
  | anything else | "the load request was not accepted (last state <state>, last start answer HTTP <code>)" | ERROR |

- **`__main__`:**
  - installs the SIGTERM handler of D9 first;
  - validates `--model_name` (Input Validation) before any filesystem write;
  - when conversion succeeded and `outcome.ready` is true, calls
    `clear_load_outcome(model)`;
  - otherwise captures the pb_stub line, writes the record, and logs the
    Startup line at the level above;
  - then exits 0 as today (bugfix "Owner decisions": a non-zero exit would retry
    and then roll back the deployment). A conversion failure still raises and
    exits 1, as today.
- **Imports.** `model_status_records`, `model_locks` and `repo_entries` are
  imported inside `try: … except Exception:` for the reason given in D2. A
  failed `model_status_records` import is handled like a failed record write
  (ERROR, exit 0); a failed `model_locks` import like an unusable lock (D9);
  a failed `repo_entries` import skips the sweep. None fails a lifecycle step.

**pb_stub capture (best effort, host-side only).** Triton core logging is off
(`triton_server.cpp:461-464`). The stub prints its preinitialize failure to the
stdout it inherited, which is the backend container's log. The backend cannot
read its own container log, so the host-side Startup, which runs as root next
to Docker, captures it:

```python
ids_cmd = ["docker", "ps", "-q", "--filter", "ancestor=flask-app"]
ids = subprocess.run(ids_cmd, capture_output=True, text=True, timeout=10)   # no shell

since = int(startup_started_epoch)
logs_cmd = ["docker", "logs", "--timestamps", "--since", str(since), container_id]
proc = subprocess.Popen(logs_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)   # no shell
```

- There is no `--tail`: on a busy backend the INFO index dumps and continuous
  runs written after the stub's line would push it out of a line-count window.
  There is no `--until` and no `--follow` either. Without `--follow`,
  `docker logs` returns what the container logged up to now and exits, so the
  read covers everything from the Startup's start (the convertor's own start,
  after the seed wait) to the end of its start budget, however long the
  Startup took (D9, "Startup bound"). A fixed `--until` would cut that window
  short on a slow Startup.
- **Several containers.** The `docker ps` query can return more than one id,
  for example a stopping container next to its replacement. The capture
  scans the ids in the order `docker ps` returns them (newest first), at most
  `PB_STUB_MAX_CONTAINERS = 3`, and stops at the first match.
- Each container's output (stdout and stderr merged) is read line by line,
  stopping at the first match, after `PB_STUB_SCAN_BYTES = 20 MiB`, or when a
  `threading.Timer(10, proc.kill)` fires; the process is then killed and
  reaped. The whole capture, including `docker ps`, stops after
  `PB_STUB_TOTAL_S = 20` s.
- The argument lists are built on their own lines, so the call lines carry no
  interpolation (`repo_audit.py`'s subprocess rule).
- The first line that contains `Python model file not found in '` and
  `/base_<m>/` or `/marshal_<m>/` is stored as
  `{"line": <≤ 300 chars, without the docker timestamp>, "at": <docker timestamp, ISO-8601 UTC>, "capturedAt": <now>}`.
- Any error, timeout, cap or missing match means `null` (DEBUG log).

**Record lifecycle.**

- The Startup writes it on not-ready and clears it on READY.
- The cleanup clears it after a completed removal (D2).
- The watchdog deletes it once it sees the model READY (INFO log). A record
  therefore always means "not READY since `recordedAt`".

### D6 — Detecting a wedge from positive evidence (F4, ships regardless of self-heal)

`dda_triton/load_watchdog.py` runs one daemon thread, `triton-load-watchdog`.

**Startup and containment.**

- `app.py` starts it after `health.set_vllm_server(vllm_server)`
  (`app.py:445`), inside `start_model_load_watchdog()`.
- It starts only when `get_is_triton()` is true.
- It is contained: any exception during construction is logged and LocalServer
  starts as before.
- At start it closes out the self-heal record (D7, "After the restart"), looks
  for the Jetson GPU load node once (step 6), and reads the self-heal switch
  once, for its start line.
- **Every tick is contained too.** The thread's loop calls the tick inside
  `try/except Exception`. The first failing tick of a streak logs one ERROR
  with the traceback (log catalogue), later ones log at DEBUG, and the loop
  sleeps to the next interval and goes on. A tick that returns, including one
  that stops early at step 2 or skips the rest after a failed index read at
  step 3, sets `last_completed_tick_mono` and ends the streak. An exception
  inside the self-heal action is handled by the action itself, never by this
  wrapper: steps 3-5 and steps 6-8 each run in a guarded block that ends the
  quiesce or aborts the restart (D7, "The action"). Should the thread die or
  hang mid-attempt anyway, `run_activity`'s QUIESCING state expires by itself
  after `QUIESCE_MAX_S` (D7).

**One published view (thread safety).** Only the watchdog thread changes its
working state: the sightings, the activity deques, the progress signature,
the switch values and the in-process hold-offs. Gate threads (`stall_for`),
FastAPI's thread pool (`model_annotations`) and the endpoints
(`build_model_status_snapshot`) never read that state. At the end of every
tick, and at every decision change inside an action (D7), the watchdog builds
one immutable `WatchdogView`, a frozen dataclass with read-only mappings:

- the `LoadHealth` of every `LOADING` model and the suspected culprit;
- the self-heal decision, its `deferredBy`, the effective switch and its
  `disabledBy`, the attempts in the last 24 h, `rearmsAt` and the latest
  attempt;
- the GPU node and `last_completed_tick_mono`.

It publishes the view with one reference assignment (`self._view = view`).
Every reader reads that reference once per call and uses only that view, so
readers take no lock and never see a half-updated state.

**A stale view counts as no watchdog.** When the published view's
`last_completed_tick_mono` is older than `WATCHDOG_STALE_S = 300`, or no view
was published yet, every reader treats the watchdog as not running:

- `stall_for` returns `None`, so the run gate keeps waiting as it does today;
- `loadHealth` is left out of `/feature-configurations`;
- the snapshot reports `selfHeal.enabled: false` with
  `disabledBy: "watchdog_not_running"`, and the attempts from the record.

An action takes at most about 120 s (a fresh switch read, a quiesce of at
most about 85 s and the 10 s interrupt budget), so a live watchdog never
looks stale.

**Clocks.** Every duration it measures (how long a model has been `LOADING`,
the time without load progress, the activity window, report heartbeats) uses
`time.monotonic()`. Wall-clock time is used only to compare with file mtimes
(E1), for displayed timestamps, and for the persisted self-heal history (D7).
A wall-clock step at NTP sync (Jetsons without a working RTC) can therefore
never make a model look stalled.

**Each tick (`WATCHDOG_INTERVAL_S = 30`):**

1. **Hygiene.** `repo_entries.sweep_stale_hidden_dirs(TRITON_MODEL_DIR)`, one
   INFO line per removal (D4). While `load_tracker.inflight()` is not empty,
   `load_tracker.ensure_tracker_thread()` (D1).
2. If `triton_repo_has_models()` is false, clear the tracking state (so the
   progress window restarts when models reappear), report the empty snapshot
   `build_model_status_snapshot({})` if its signature changed, after step 9's
   size-rejection check (D8), and stop there. This guard runs before any
   `TritonEdgeClient.get_instance()` (3.10).
3. Read `client.index_states()` (D1), the same native `ListModels` call as
   `list_triton_models`, which also refreshes edgemlsdk's cached states. It
   logs nothing itself, so on error the watchdog skips the tick with one
   WARNING per error streak, and `list_triton_models`' ERROR line is not
   repeated every 30 s.
4. **Progress signature.** Take the set of `(name, status, reason)` for every
   index entry whose status is not `LOADING`, skipping names that start with
   `.`. Load progress means this tick's set holds a member the previous tick's
   set did not: a model reached or changed a non-`LOADING` state, or a new
   entry appeared. That sets `last_progress_mono = monotonic()`.
   - An entry that leaves the set by becoming `LOADING` is not progress. A
     ModelGate retry that turns an `UNAVAILABLE` model `LOADING` behind a wedge
     therefore does not reset the window.
   - `last_progress_mono` starts at the watchdog's start, so a fresh boot never
     counts as "15 min without progress".
5. **Loading set.** Collect the component models (names `model-…`, without the
   `base_`/`marshal_` steps) whose status is `LOADING`. Per component model the
   watchdog keeps two sightings, each as `(mono, wall)`:
   `last_outside_loading(m)`, the latest tick that saw the model in any other
   state or absent from the index, and `first_loading(m)`, the first tick of
   its current `LOADING` episode. For each `LOADING` model:
   - `since(m)` is `load_tracker.requested_at(m)` when that request is newer,
     by the monotonic clock, than `last_outside_loading(m)`, or when the model
     was never seen outside `LOADING`. Otherwise it is `first_loading(m)`. The
     tracker keeps a request after its release, so this rule stops a request
     from an earlier episode from dating a new episode back.
   - `loading_since_mono`, the displayed `loadingSince` and E1's `ref_wall(m)`
     all come from `since(m)`.
   - forget models that left `LOADING`, logging an INFO recovery line if one was
     stalled.
6. **Activity sample**, taken only while some model has been `LOADING` for
   ≥ 60 s:
   - Scan `/proc/[0-9]*/cmdline` for processes whose `argv[0]` basename is
     `triton_python_backend_stub` and whose `argv[1]` (the model path) lies
     under `TRITON_MODEL_DIR/<step>/`.
   - The step maps to its component by stripping `base_`/`marshal_`.
   - Per pid, read `utime + stime` from `/proc/<pid>/stat` (fields 14 and 15,
     parsed after the last `)`) and divide by `os.sysconf("SC_CLK_TCK")`.
   - Keep a deque of `(mono, {pid: cpu_s})` samples covering
     `ACTIVITY_WINDOW_S`.
   - **Window CPU** is the sum, over consecutive sample pairs, of
     `max(0, b[pid] - a.get(pid, 0))` for each pid in `b`, across the stubs of
     every `LOADING` component.
   - The window counts as covered only once the oldest retained sample is at
     least `ACTIVITY_WINDOW_S` old by the monotonic clock. Before that,
     activity is "unknown".
   - **GPU load (Jetson; used by self-heal only, D7 rule 7).** At start the
     watchdog looks once for the integrated GPU's devfreq node: the first
     entry of `/sys/class/devfreq/`, in sorted order, whose
     `device/of_node/name` is `gv11b` (Xavier), `ga10b` (Orin), `gb10b` (Thor),
     `gp10b` or `gpu`, and whose `os.path.realpath(<entry>/device)/load` is
     readable. The backend containers are privileged, so sysfs is visible. On
     each sampled tick the watchdog reads that file `GPU_SAMPLE_READS = 5`
     times, `GPU_SAMPLE_GAP_S = 0.1` s apart (the value is the GPU's busy share
     in per mille over a short recent window), and keeps the tick's maximum in
     a deque that covers `ACTIVITY_WINDOW_S`. A failed read is skipped. With no
     node, or no successful read in the window, the GPU signal is
     **unavailable**. x86 hosts have no such node, so there the stub-CPU veto
     applies alone.
7. **F3 resolution.** Delete the load outcome record of every model that is
   `READY`.
8. **Verdict.** Run the pure `evaluate_wedge(…)` (below), hand it to the
   self-heal policy (D7), then publish the tick's view (above), which the
   surfaces read (D8).
9. **Report.** If the reporter flagged a size rejection since the last tick
   (`model_status_shadow.take_size_rejection()`), read the reported document
   once and record the retirements first (D8, "The rest of the document, and
   size rejections"). Then report the shadow when its signature changed or the
   heartbeat is due (D8, "Reporting cadence").

**`evaluate_wedge(now_mono, now_wall, loading, last_progress_mono,
window_cpu_s, window_covered, entries, pb_stub_lines) -> WedgeVerdict`**, pure
and unit-testable:

- **Candidate.** `m` is a candidate when
  `now_mono - loading_since_mono(m) >= STALL_AFTER_S`. `LOADING` time is
  necessary but never sufficient.
- **Activity veto (device-wide).** If `window_covered` is false, or
  `window_cpu_s >= ACTIVITY_CPU_THRESHOLD_S`, nothing is stalled. Triton loads
  one model at a time, so if any load is active the queue is moving. A
  TensorRT engine build keeps a builder thread busy launching and timing
  kernels, which shows up as stub CPU.
- **E1 (repository).** `inspect_entry(…, quiet_period_s=0)` reports a
  `missing_path`, or `newest_mtime > ref_wall(m) + 1`, where `ref_wall(m)` is
  the wall time of `since(m)` (step 5).
  - Evidence text: `[repository] <relative path> was missing at <ts>, after the
    load request at <ts>` or `[repository] <relative path> changed at <ts>,
    after the load request at <ts>`.
  - E1 uses the same path set as ModelGate: the directories, `config.pbtxt`,
    and the steps' `model.py`. Files the stub writes at load time
    (`__pycache__`, the provider sidecar) therefore never count.
- **E2 (stall).** `now_mono - last_progress_mono >= STALL_AFTER_S`.
  - Evidence text: `[stall] the Triton model index has not changed for <dur>
    and the loading models' stub processes used <x.x> CPU-s in the last 5m (<n>
    stub process(es) alive)`.
- **pb_stub.** When an F3 record for `m` carries `pbStubMissingFile` with
  `at >= ref_wall(m) - 60 s`, append
  `[pb_stub] "<line>" at <at> (captured by the <m> Startup from the backend container log)`.
  Only the host-side Startup can read the container log (D5), so this
  evidence exists only when a Startup of that model captured the line. A wedge
  that the run gate, ModelGate or `/start` started carries `[repository]` and
  `[stall]` evidence only. With F2, the missing-file case fails fast anyway.
- **Stalled** means candidate ∧ ¬veto ∧ (E1 ∨ E2).
- **Suspected culprit.** Among the stalled models: a model with E1 evidence;
  else one with a live stub; else the earliest `loading_since`. It is reported
  as "(suspected culprit)".

**Unknowns resolve conservatively.** A `/proc` read error, a parse failure or
fewer samples than the window all leave activity "unknown", and nothing is
stalled.

**Why the verdict uses stub CPU, and self-heal also GPU load.** The safeguard
is never to act during a legitimate long load, such as a TensorRT engine
build, which shows CPU or GPU activity.

- **Stub CPU vetoes the verdict.** It can be attributed to the models that are
  loading. A TensorRT build keeps a builder thread busy launching and timing
  kernels, which shows as stub CPU.
- **GPU load defers the restart, not the verdict.** Jetson GPU load is
  device-wide: inference by READY models, vLLM serving and a display all add
  to it. As a stall veto it would hide real wedges whenever anything else used
  the GPU, so detection does not use it. Self-heal does (D7 rule 7): any GPU
  load in the window defers the restart. That can only delay an action that
  already requires a stall, which is the conservative side.
- **Where the GPU cannot be read** (x86, or a missing node), the stub-CPU veto
  and the two 15-minute windows apply alone.

**Thresholds:**

| Constant | Value | Why |
|---|---|---|
| `WATCHDOG_INTERVAL_S` | 30 | Cheap: one index read and one directory listing; `/proc` only while a model is loading |
| `WATCHDOG_STALE_S` | 300 | Ten missed ticks; longer than the longest action (about 120 s), so only a watchdog that stopped completing ticks looks stale |
| `LOAD_HEALTH_AFTER_S` | 120 | `loadHealth` appears only for loads longer than 2 min |
| `STALL_AFTER_S` | 900 | On the MIC-730, single loads take 16 s to 4 min, and 3-4 serialized loads take 4.5-6 min |
| `ACTIVITY_WINDOW_S` | 300 | Five minutes of stub CPU |
| `ACTIVITY_CPU_THRESHOLD_S` | 5.0 | About 1.7 % of one core. A stuck stub's 300 ms health loop uses far less; any real load uses far more |
| `GPU_SAMPLE_READS`, `GPU_SAMPLE_GAP_S` | 5, 0.1 s | A burst per sampled tick, because the node reports only a short recent window |
| `GPU_ACTIVITY_PERMILLE` | 1 | Any non-zero GPU load in the window defers self-heal. An idle Jetson reads 0; the MIC-730's GPU was idle (GR3D 0 %) during the 2026-10-14 wedge |

**Detection output.**

- **Per model.** For every `LOADING` model:
  - `LoadHealth(state = "loading" | "stalled", loading_since, no_progress_s,
    stub_count, window_cpu_s, gpu_max_permille, evidence, recovery_action,
    self_heal_state)`, where `gpu_max_permille` is `None` while the GPU signal
    is unavailable;
  - available from the published view through `stall_for(name)`,
    `load_health(name)` and the surfaces (D8).
- **Logging, once per model per episode:**
  - the WARNING "is stalled" line when the model becomes stalled;
  - the INFO "no longer stalled" line when it leaves `LOADING`.

### D7 — Self-heal: on by default, conservative, rate-limited, never looping (F4)

**The switch.** Self-heal is on by default on every device, and each device
can opt out (Owner Decisions 3b; bugfix.md Operator notes).

- **LocalServer configuration `ModelLoadSelfHealEnabled`.**
  - The seven LocalServer recipes ship `ModelLoadSelfHealEnabled: "true"` in
    `DefaultConfiguration` (D10), so the default and the key's name are
    visible in the Greengrass console. A missing key also means on.
  - A device opts out with a configuration merge of `"false"`, and back in
    with `"true"` or a reset of the key. With the SOFT CLI dependency (D10)
    that merge does not restart LocalServer, and none is needed: the key is
    read live.
  - **The reader.** One module-level reader, created on first use and reused,
    as `local_auth/config.py:104-111` does:
    `_SWITCH_READER = DefectDetectionConfig(get_ipc_client())`, then
    `_SWITCH_READER.get_component_config(_SWITCH_READER.get_local_server_component_name())`.
    `get_component_config` is uncached, and the reader caches the component
    name after its first successful discovery
    (`defect_detection_config/defect_detection_config.py:93-111`), so only the
    first read lists the components. A successful value is refreshed every
    `SWITCH_REFRESH_S = 300` for rule 2 and the surfaces. After a failed read
    the periodic refresh waits `SWITCH_RETRY_AFTER_S = 60` before it tries
    again, because each failed read blocks the watchdog thread for up to the
    IPC timeout and makes `get_component_config` log its own ERROR line
    (`defect_detection_config.py:77-88`). An IPC outage therefore costs at
    most one such read per minute. The value is also read fresh at the start
    of every attempt (the action, step 1) and once more immediately before its
    commit (step 5), whatever the retry delay. Each read is bounded by the IPC
    client's 10 s timeout (`TIME_OUT`, `defect_detection_config.py:40`, used at
    `:76`), and by 20 s on a read that must first discover the component name
    (`:119`).
  - **A read counts as successful only if it returns a `dict` that contains
    `StreamShadowName`.** All seven LocalServer recipes carry that key in
    `DefaultConfiguration`, so every effective configuration does
    (`ComponentName` and `StationName` are missing from the amd64 recipes).
    `get_component_config` (`defect_detection_config.py:61-91`) logs and
    returns `{}` on `UnauthorizedError`, `RuntimeError` and any other error from
    the response, and the IPC calls before its `try` can raise. So an
    exception, a non-dict, an empty dict, or a dict without
    `StreamShadowName` is a **failed read**.
  - **Parsing a successful read** (`str(v).strip().lower()`, booleans
    included): a missing key, `true`, `1`, `yes`, `on` and `enabled` mean on;
    `false`, `0`, `no`, `off` and `disabled` mean off. Any other value means
    off, with one WARNING per distinct value: an explicit but unparseable
    setting counts as an opt-out, never as consent to an automatic restart.
  - **Before the first successful read** in this process, self-heal is off
    with `disabledBy: "config_unreadable"`, so a read failure can never override
    a device's opt-out. **After a failed read** the last successful value
    applies. One WARNING is logged per streak of failed reads.
- **The kill switch `/aws_dda/dda_triton/model_status/self-heal.disabled`.** If
  it exists, self-heal is off whatever the configuration says. It is checked
  every tick, at the start of every attempt and once more immediately before
  its commit, and it needs no IPC, so it works when the configuration cannot
  be read or changed. It stops any attempt that has not reached its commit,
  including one that is quiescing; from the commit on, the restart is
  unconditional (the action, step 5).

The surfaces report `enabled` (the effective switch) and
`disabledBy: "config" | "file" | "config_unreadable" | "watchdog_not_running" | null`.
`"config"` covers `"false"` and an unparseable value; the WARNING and the start
line say which. `"watchdog_not_running"` comes only from the snapshot builder,
when no watchdog runs or its published view is stale (D6, D8). On a device
with the default configuration the surfaces report `enabled: true` and
`disabledBy: null`.

**The policy.** `decide_self_heal(verdict, switch, history, activity, gpu,
vllm, local, now_wall, now_mono)` is a pure function that returns one
decision. `gpu` is the GPU window of D6 step 6, `vllm` the mapped vLLM states
(below), and `local` this process's hold-offs: the time and the `deferredBy`
of its latest deferral after quiescing (at the drain, at the re-check or at
the commit, the action, steps 4-5) and the time of its latest aborted attempt
(an exception in steps 3-5 or in steps 6-8, or a `begin_quiesce()` that
refused). It applies the first rule that matches:

| Order | Condition | Decision | Log |
|---|---|---|---|
| 1 | no stalled model | `idle` | — |
| 2 | switched off | `disabled` | WARNING once per episode |
| 3 | the history record is unreadable or invalid (the action's write probe, step 2, reports the same decision when the record cannot be written) | `blocked_record` (no action) | ERROR once per episode |
| 4 | at least `SELF_HEAL_MAX_PER_24H` (3) attempts whose age (below) is under 24 h | `exhausted`, re-arms when the oldest of them is 24 h old | ERROR "SELF-HEAL EXHAUSTED" once per episode, then WARNING every 60 min |
| 5 | the newest attempt's age is under `SELF_HEAL_MIN_INTERVAL_S` (1800 s), or this process aborted an attempt less than 1800 s ago by the monotonic clock | `cooldown` until 1800 s after the later of the two | WARNING once per episode |
| 6 | a vLLM model reports `LOADING` through the feature-config mapping, or the vLLM manager cannot list its models | `deferred`, `deferredBy: "vllm"` | WARNING at most every 10 min |
| 7 | the GPU signal is available and its maximum in the activity window is at least `GPU_ACTIVITY_PERMILLE` | `deferred`, `deferredBy: "gpu"` | WARNING at most every 10 min |
| 8 | an unblocked run is in flight, a continuous workflow is running (below), or this process deferred after quiescing less than `QUIESCE_RETRY_AFTER_S` (300 s) ago | `deferred`, `deferredBy: "runs"`; during the hold-off, the `deferredBy` that the deferral recorded (`runs`, `vllm`, `gpu` or `recheck`) | WARNING at most every 10 min, naming the workflows |
| 9 | otherwise | `act`, attempt number = attempts in the 24 h window + 1 | the action's lines |

**Rule 6 in detail.** `feature_configs_utils.get_vllm_manager()` returns
`None` on images without the vLLM runtime, which means no vLLM models.
Otherwise each status from `list_models()` is mapped with
`feature_configs_utils._vllm_feature_status(status)[0]`, the mapping that
`/feature-configurations` reports (`_VLLM_STATUS_MAP`,
`feature_configs_utils.py:144-150`). A `STAGED` model reports `LOADING` there,
so it defers too. A `list_models()` that raises defers as well, with a
WARNING, so a vLLM-side error can never lead to a restart. Rule 6 is
evaluated at every decision and again immediately before the commit (the
action, step 5), so a vLLM load that starts while the attempt quiesces also
defers it. `vllmModelsDropped` lists every vLLM model that the restart drops,
with the manager's own state: `READY`, `LOADING` or `STAGED` (mapped `READY`
or `LOADING`). `FAILED` and `UNLOADED` models hold nothing that a restart
could drop.

**An attempt's age, and wall-clock steps.** The history must survive the
restarts it causes, so a monotonic clock cannot measure it. Jetsons without a
working RTC can also step their wall clock at NTP sync. Each attempt therefore
records three references: `requestedAt` (wall clock, ISO-8601 UTC), `bootId`
(`/proc/sys/kernel/random/boot_id`, read once at watchdog start) and
`bootSeconds` (`time.clock_gettime(time.CLOCK_BOOTTIME)`; the container shares
the host kernel, so both survive a backend restart).

- **Same boot** (`bootId` matches): age = now's `CLOCK_BOOTTIME` −
  `bootSeconds`. A wall-clock step cannot change it.
- **Another boot, or the fields missing or unreadable**: age =
  `now_wall − requestedAt`.
- **A negative age counts as 0**, so an attempt dated in the future is inside
  both windows. A backward step can only delay self-heal.

Within one boot, then, a wall-clock step never shortens either window. Across a
reboot the wall clock is the only shared reference.

**Runs in flight and continuous workflows** come from `utils/run_activity.py`.
It is in-process and thread-safe, and it never raises into its callers: an
internal error is logged and the run proceeds as today.

- **A run is registered for its whole life.** Every workflow run path
  registers its ticket when the run starts, before it captures a frame,
  prepares a feed or runs user code, and finishes the ticket when the run
  ends. The registration points are listed below, from "Engine executions" to
  "Digital-input triggers". A run that is still capturing or preparing (for an
  engine execution, everything `execute` does before its readiness gate,
  `pipeline_executor.py:1563-1824`; for a classic run, its gate and its image
  capture; for a digital-input trigger, its frame grab) is therefore visible
  to rule 8, the drain and the commit, exactly like a run in its pipeline.
  Only operator image-source previews register later ("Other `run_pipeline`
  callers").
- **No run is ever held.** `start_run` returns at once in every state, and
  no call that a run makes waits for a state change. A held run would capture
  its frame late: a triggered run would inspect what is in front of the
  camera when the hold ends, often another part, and record a pass or fail
  for the wrong one. A run that starts during a quiesce is therefore accepted
  and counts as in flight; the drain waits for it, and the commit defers
  while it is unblocked (the conservative side of "never act while a run is
  in flight").
- **States.** One `threading.Condition` guards all state, including every
  registration. Only the watchdog's action calls the functions that change it,
  apart from the expiry below.

  ```
  IDLE --begin_quiesce()--> QUIESCING --commit_restart() == "committed"--> RESTARTING
  QUIESCING --end_quiesce(), or QUIESCE_MAX_S after begin_quiesce()--> IDLE
  QUIESCING or RESTARTING --abort_restart()--> IDLE
  ```

  | Call | IDLE | QUIESCING | RESTARTING |
  |---|---|---|---|
  | `start_run` | accepted | accepted at once; the run counts as in flight | refused |
  | `gate_wait` entry | accepted | accepted (the ticket is in its gate, so blocked) | refused |
  | `after_gate` | `None` | `None` | the refusal text |
  | `quiescing()` | False | True | True |
  | `commit_restart` | `"not_quiescing"` | `"runs"` while an unblocked ticket is registered, else `"committed"` | `"not_quiescing"` |

  - Calls that do not match a transition change nothing: `begin_quiesce()`
    outside IDLE returns False, `commit_restart()` outside QUIESCING returns
    `"not_quiescing"`, and `end_quiesce()` outside QUIESCING and
    `abort_restart()` in IDLE are no-ops.
  - **The quiesce expires by itself.** `begin_quiesce()` records the
    monotonic time. Every call first applies the expiry: a QUIESCING state
    older than `QUIESCE_MAX_S = 120` s becomes IDLE, with one WARNING (log
    catalogue), logged after the condition's lock is released, as every line
    of this module is. From then on `quiescing()` is False, continuous runners tick
    again, and `commit_restart()` returns `"not_quiescing"`, even when the
    watchdog thread died or hung in the middle of an attempt. A regular
    quiesce ends well before: the drain takes at most 60 s and the re-check at
    most about 25 s (the action, step 5). `state()` returns the state name
    after applying the expiry.
  - Only `abort_restart()` leaves RESTARTING, and RESTARTING does not expire.
    Every failure between the commit and step 9 calls `abort_restart()` (the
    action, "If steps 6-8 fail"), so a failure can never strand a live backend
    that refuses every run; from step 9 on, the backstop, or the immediate
    `os._exit(86)` when the backstop cannot start, ends the process within
    60 s.
  - `quiescing()` stays True in RESTARTING, so continuous runners keep
    skipping their ticks during the shutdown.
- **The current ticket.** A run's ticket is kept in a
  `contextvars.ContextVar`. A context variable is per thread for the
  executor's daemon threads (`executor.py:57-82`), the continuous runners, the
  DIO threads and their pool workers, and per task for coroutines: the
  classic handler is `async def`, and every capture task is its own asyncio
  task (`capture_task_manager.py:99`). One variable therefore serves both,
  where a `threading.local` would let concurrent requests on the event-loop
  thread see each other's tickets. `start_run` sets it (with the default
  `bind=True`), and `finish_run` resets it with the token that `start_run`
  kept. A `start_run` in a context that already holds a live ticket returns a
  no-op ticket, which is never refused or counted, so a nested registration
  (the `run_pipeline` call inside an engine execution, a classic run, a
  capture task or a digital-input run) counts once.
- **A run that changes threads.** A digital-input trigger registers on the DIO
  thread and runs its work on a pool thread. A context variable set on one
  thread is not visible on another, and `ContextVar.reset(token)` raises
  `ValueError` in a different `Context`. So that caller registers with
  `bind=False`, which counts the ticket without touching the variable, and
  the pool thread enters `bound(ticket)`, which sets the variable for the
  work and resets it on exit, on the same thread. Pool threads are reused, and
  this reset keeps a finished run's ticket out of the next task.
- **API.**
  - `start_run(kind, run_id, models=(), workflow=None, continuous=False, on_interrupt=None, bind=True) -> RunTicket`.
    `kind` is `"execution"`, `"pipeline"` or `"capture"`. `ticket.refused` is
    `None`, or the refusal text in RESTARTING. The call never waits. With
    `bind=True` the ticket becomes the context's current ticket; with
    `bind=False` it is only registered (the nested check applies either way).
    A ticket with `continuous=True` also records, per `workflow`, the
    monotonic time of that workflow's latest execution start.
  - `bound(ticket)`: a context manager that makes a ticket registered with
    `bind=False` the current ticket of the calling context for its duration,
    so the run's nested registrations are no-ops, and resets the variable on
    exit. It registers and finishes nothing, and does nothing for `None`, a
    no-op ticket or a refused ticket.
  - `set_details(ticket, models=None, workflow_label=None)` fills in what a
    run learns after it registered: its models after name resolution, and the
    display label `<workflow_id> v<version>` after the registration lookup.
    The snapshot, the deferral lines and the record show the label once it is
    set, else the `workflow` given at registration (the engine passes the
    registration id). It is a no-op for `None` and for no-op tickets.
  - `finish_run(ticket)` accepts `None` and no-op tickets, and is idempotent.
    It unregisters the ticket; for a ticket registered with `bind=True` it
    also resets the context variable with the token that `start_run` kept. A
    `bind=False` ticket can be finished from any thread.
  - **Refused tickets.** A refused ticket is neither registered nor bound.
    `start_run` sets no context variable for it, and `finish_run`, `bound` and
    `set_details` treat it as a no-op. The callers' `finally: finish_run(ticket)`
    after a refusal in RESTARTING is therefore safe and leaves the context
    empty.
  - `gate_wait(model)`: the context manager that `ensure_model_ready` holds
    while it polls a model that is not READY (D4). It marks the context's
    ticket as in its gate for its duration (a counter, so repeated gate waits
    in one run are safe) and records when the ticket leaves its gate. Without
    a ticket in the context it registers a gate-only ticket for its duration.
    It yields a `GateTicket` whose `refused` is `None`, or the refusal text
    when the gate is entered in RESTARTING.
  - `after_gate(ticket) -> Optional[str]`: the refusal text in RESTARTING,
    else `None`. Both run paths call it right after their readiness gate
    passes, so a run that leaves its gate after the commit stops before its
    pipeline instead of starting work that the restart would cut off.
  - `on_interrupt(error_text)`, given to `start_run`, is an optional callback.
    A self-heal restart calls it for every registered ticket that it cuts off
    (the action, step 10).
  - `begin_quiesce() -> bool`, `end_quiesce()`, `abort_restart()`,
    `quiescing()`, `state()`,
    `commit_restart(is_blocked, refusal) -> str` (`"committed"`, `"runs"` or
    `"not_quiescing"`, per the state table),
    `interrupt_all(error_text, budget_s) -> InterruptReport` and `snapshot()`.
    `snapshot()` returns every live ticket (kind, id, models, workflow label,
    start time, whether it is in its gate, since when it has been outside its
    gate, whether it has a callback) and the latest execution start of each
    continuous workflow; `decide_self_heal` receives it as `activity`.
- **Engine executions.** In `PipelineExecutor.execute`:
  - `ticket = None` is set before the existing `try`
    (`pipeline_executor.py:1539`), and `run_activity.finish_run(ticket)` is the
    last statement of its existing `finally` (`:2383`).
  - Directly after the pending check (`:1547-1553`) and the trigger-context
    read (`:1559-1561`), before the registration lookup and every capture and
    preparation step:

    ```python
    # is_continuous_context: run_retention.py:107, imported at module level
    # (run_retention imports only the standard library and stream_feed there)
    continuous = is_continuous_context(trigger_context)
    on_interrupt = None if continuous else functools.partial(
        self._mark_failed_best_effort, execution_id)
    ticket = run_activity.start_run(
        "execution", execution_id, workflow=execution.registration_id,
        continuous=continuous, on_interrupt=on_interrupt)
    if ticket.refused is not None:
        self._finish_failed(session, execution, error=ticket.refused)
        return
    ```

    In QUIESCING the call returns at once, so the execution captures its
    frame without delay and counts as in flight. Continuous executions
    register no callback: the startup reconciliation finishes and counts an
    interrupted continuous run ("After the restart").
  - After the registration lookup (`:1563-1565`), when it found the row:
    `run_activity.set_details(ticket, workflow_label="<workflow_id> v<version>")`.
  - After `self._resolve_model_names(document)` (`:1824`):
    `run_activity.set_details(ticket, models=…)` with the document's resolved
    `emltriton` models. The loop of `_await_model_readiness` (`:1483-1495`)
    moves into a static helper that both use, and `_await_model_readiness`
    keeps its signature.
  - After the readiness gate passes (`:1843-1847`):
    `refused = run_activity.after_gate(ticket)`. A refusal ends the execution
    through `self._finish_failed(session, execution, error=refused)`.
  - Continuous executions take this path too (`continuous_runner.py:427-450`).
- **Classic runs.** `endpoints/workflow.run_inference_for_stream` registers
  right after `validate_workflow_requirements(workflow)` (`:244`), before its
  readiness gate and its image capture:
  `ticket = run_activity.start_run("pipeline", workflow_id, models=<the configured modelName, if any>, workflow=workflow_id)`.
  - The rest of the handler body runs inside `try:` …
    `finally: run_activity.finish_run(ticket)`.
  - A refused ticket raises `HTTPException(status_code=503, detail=ticket.refused)`
    before `validate_model_readiness(workflow)` (`:254`). After the gate, a
    refusal from `after_gate(ticket)` raises the same 503 before
    `configure_image_source_and_run_pipeline` (`:259`).
  - The handler is `async def` and runs synchronously on the event loop; the
    registration never waits, so it never stalls other requests.
  - A classic run keeps no row, so it registers no `on_interrupt`; its HTTP
    caller sees the connection close.
- **Other `run_pipeline` callers.** `gstreamer/gst_pipeline.PipelineManager.run_pipeline`
  calls `start_run("pipeline", …)` just before its `try:` (`gst_pipeline.py:217`)
  and `finish_run` in its existing `finally:` (`:305`). The models are parsed
  from the launch string with `emltriton\b[^!]*?\bmodel="?([^\s"!]+)`. A refused
  ticket raises `PipelineExecutionException(ticket.refused, status_code=503)`
  (`exceptions/api/gst_pipeline_exception.py:28`) before the `try`. Inside an
  engine execution, a classic run, a capture task or a digital-input run the
  call is nested and changes nothing.
  - On its own it covers only the operator's image-source previews, captures
    and connection tests in `endpoints/image_source.py` (sync endpoints on the
    thread pool, through `gst_pipeline_executor.execute_image_source_pipeline`):
    no model, no run row and no inspection result. They grab their frame
    before `run_pipeline`, so one whose grab overlaps the commit is invisible
    to it: the restart cuts it off, or `run_pipeline` refuses it with the 503,
    and the operator repeats it.
  - No workflow run reaches `run_pipeline` without a ticket of its own: engine
    executions, classic runs, capture tasks and digital-input triggers all
    register earlier.
- **Capture tasks.** `utils/capture_task_manager.CaptureTaskManager.capture_image_in_time_interval`
  (`:70`) calls `start_run("capture", <capture task id>, workflow=<workflow id>)`
  at its start and `finish_run` in a `finally`. A capture task therefore counts
  as in flight for its whole duration, including the sleeps between its images
  (at most 100 images, at most 120 s apart, `utils/constants.py:85-87`). A
  refused ticket logs the INFO refusal line and ends the task before its first
  image. The coroutine runs on the event loop; the registration never waits.
- **Digital-input triggers.** In Triton mode a digital-input trigger runs
  `DigitalInputThread.trigger_workflow_and_debounce`
  (`utils/digital_input_thread_manager.py:247-273`) on the workflow's DIO
  thread (in-process in Triton mode, `app.py:363-366`). It reads the image
  source (`:257`), grabs the camera frame in that thread (`get_camera_frame`,
  `:260`) and submits `execute_workflow` to the thread's two-worker pool
  (`:99`, `:263`). Inside the pool the first registration would be
  `run_pipeline`'s, after the frame grab, so the trigger registers first. At
  the top of the branch that runs the workflow
  (`if self.future_in_progress is None or …`, `:255`), before
  `_get_image_source`:

  ```python
  ticket = run_activity.start_run(
      "pipeline", self.workflow_id, models=<the configured modelName, if any>,
      workflow=self.workflow_id, bind=False)
  if ticket.refused is not None:
      logger.info(...)   # the catalogue's refusal line; no frame grab, no submit
  else:
      submitted = False
      try:
          ...            # the existing image-source read, frame grab and submit,
                         # unchanged, passing the ticket to execute_workflow
          submitted = True
      finally:
          if not submitted:
              run_activity.finish_run(ticket)   # the read, the grab or the submit raised
  ```

  - `execute_workflow(self, latency_metrics, frame=None, ticket=None)` (`:158`)
    runs its existing body, including its `try/except` and the health
    updates, inside `with run_activity.bound(ticket):`, and calls
    `run_activity.finish_run(ticket)` in a new outer `finally`, on the pool
    thread. The nested `run_pipeline` registration is then a no-op.
  - The "Pipeline is full" branch (`:265-266`) registers nothing, and the
    debounce after both branches is unchanged.
  - A refused trigger is treated like a trigger during the restart itself: the
    INFO line records it, nothing is captured, and the DIO health is not
    changed.
  - `models` is the workflow's configured `modelName`, as for a classic run.
    DIO runs have no readiness gate, so the 900 s rule below is what counts
    one as blocked while its `emltriton` waits on a stalled model.
- **Continuous runners.** `ContinuousRunner.step()` returns
  `QUIESCE_POLL_S = 1.0` while `run_activity.quiescing()` is true, without
  taking a frame, inserting a run row or starting a run. The check sits directly
  after the model-gate check (`continuous_runner.py:346-351`) and clears the
  schedule the same way, so the first tick after a quiesce that ends is
  immediate. No counter changes.

**Classifying activity at a decision.**

- **Blocked tickets.** A ticket is blocked by the wedge while it is in its
  gate. It is also blocked when it has been outside its gate for at least
  `RUN_BLOCKED_AFTER_S = 900` and one of its models is stalled: a run whose
  gate failed open, or a digital-input run, which has no gate, whose
  `emltriton` waits on the stuck load. A ticket that never entered a gate has
  been outside one since its registration. Gate-only tickets are always
  blocked.
- **Unblocked tickets.** Every other ticket: a run that is capturing,
  preparing, between two models' gates or running its pipeline on READY
  models, and every capture task. Any of them defers self-heal (rule 8), keeps
  the drain waiting, and makes `commit_restart` return `"runs"`.
- **Running continuous workflows.** A continuous workflow is running when it
  started an execution in the last `CONTINUOUS_ACTIVE_S = 60` s. It executes
  several times a second on READY models, and a backend restart would stop it
  for minutes, because its models come back `UNKNOWN`. So a running continuous
  workflow always defers self-heal (rule 8). The state is `deferred` with
  `deferredBy: "runs"`, and `recoveryAction` names the workflows and says that
  pausing them, or restarting the backend by hand, clears the wedge. A
  continuous workflow that waits at its ModelGate on the wedged models, is
  paused, or has no stream starts no execution and does not defer.

**The action**, in this order:

1. Read the switch fresh: the configuration key and the kill file. If it now
   says off, stop with the decision `disabled`.
2. **Probe the record.** Read it again (rule 3) and write it back unchanged,
   atomically; with no record yet, write `{"schema": 1, "attempts": []}`. If
   the write fails, stop with `blocked_record` (ERROR "SELF-HEAL off: the
   record … cannot be written", once per episode). The probe runs before
   every quiesce, so an unwritable record never pauses a continuous workflow.
3. **Quiesce.** Call `run_activity.begin_quiesce()`.
   - If it returns False, the state is not IDLE, which only an internal fault
     can cause. Log the WARNING "SELF-HEAL: run activity is <state>, not
     IDLE; not quiescing", record the abort time for rule 5, so this process
     does not try again for 30 min, publish the decision `cooldown` and stop.
     No quiesce began, so nothing needs ending, and a stale QUIESCING expires
     by itself (`QUIESCE_MAX_S`).
   - Otherwise the quiesce has begun: from now on continuous runners skip
     their ticks. Every `start_run` is still accepted at once, and its run
     counts as in flight.
   - **The rest of the attempt up to the commit runs in one guarded block**
     (steps 3-5 below), so that every way out of it that is not a commit ends
     the quiesce. Its first statement logs the INFO "quiescing" line:

     ```python
     outcome, step = None, "the quiesce"
     try:
         log.info("SELF-HEAL quiescing: ...")         # step 3's line
         step = "the drain"
         if not self._drain():                        # step 4
             return self._defer_after_quiesce()       # deferred, runs
         step = "the re-check"
         decision = self._recheck()                   # step 5; read failures give
         if decision.kind != "act":                   #   deferred, recheck (below)
             return self._defer_at_commit(decision)
         step = "the commit"
         outcome = run_activity.commit_restart(self._is_blocked, refusal)
         if outcome != "committed":                   # "runs" or "not_quiescing"
             return self._defer_at_commit(outcome)
     except Exception as exc:
         # ERROR "SELF-HEAL not performed: <step> failed before the commit ..."
         self._local.aborted_at = self._clock()       # rule 5: no new attempt for 30 min
         return self._publish_decision("cooldown")
     finally:
         if outcome != "committed":
             run_activity.end_quiesce()               # a no-op outside QUIESCING
     # RESTARTING from here. The try of steps 6-8 begins with the next statement.
     ```

     - The `finally` runs on every return and every exception, including one
       raised while the handler logs or publishes. So no exception in the
       drain, the re-check, the commit or the deferral bookkeeping can leave
       continuous workflows paused.
     - An exception logs the ERROR "SELF-HEAL not performed: <the quiesce|the
       drain|the re-check|the commit> failed before the commit" with its
       traceback. It records the abort time, so rule 5 holds this process off
       for 30 min (`cooldown`), as after a failure in steps 6-8.
     - After a commit that returned `"committed"` nothing else runs in the
       block, and nothing runs between the block and the `try` of steps 6-8,
       so a failure after the commit is always handled by `abort_restart()`
       ("If steps 6-8 fail").
4. **Drain.** Poll every second, for at most `QUIESCE_DRAIN_S = 60`, until no
   unblocked ticket is registered. If one is still registered after 60 s, the
   attempt ends here as a deferral after quiescing, exactly as when the
   commit finds one (step 5): `deferred` with `deferredBy: "runs"`, the
   WARNING "deferred after quiescing" and the 300 s hold-off, and the
   block's `finally` ends the quiesce. Step 5 is never reached without its
   re-check.
5. **Re-check, then commit.**
   - **Re-check on fresh inputs.** Immediately before the commit the watchdog
     gathers the inputs of its decision again:
     - the kill file, and the configuration switch with one IPC read (a failed
       read keeps the last successful value);
     - the vLLM models (`list_models()`, mapped as in rule 6);
     - one GPU burst, added to the GPU window (rule 7);
     - one `client.index_states()` read and one `/proc` sample, from which it
       re-runs `evaluate_wedge` as a tick would.

     It then calls `decide_self_heal` again on these inputs, with run activity
     treated as idle, because the tickets are `commit_restart`'s job. The
     reads take at most about 25 s: the IPC read is bounded by its 10 s
     timeout (20 s when it must first discover the component name), the GPU
     burst by 0.5 s, and the index read and the `/proc` sample take well
     under a second each.
   - **A read that fails cannot confirm the stall.** A tick skips itself when
     its index read fails (D6 step 3); the re-check cannot, so it decides:
     - when `client.index_states()` raises (for example the
       `RuntimeError` after three empty answers, `triton_edge_client.py:98`),
       or the `/proc` sample cannot list `/proc` (a single pid that vanishes
       or cannot be parsed is only skipped, as in a tick), the decision is
       `deferred` with `deferredBy: "recheck"`;
     - the WARNING "SELF-HEAL deferred at commit: <the Triton model index|the
       stub processes under /proc> could not be read (<error>)" names the
       read;
     - the 300 s hold-off of rule 8 follows, so the next attempt comes no
       sooner than 5 min later and re-reads everything.

     `deferred` rather than `idle`: the published `loadHealth` still shows
     the model stalled, and `selfHeal.state` must not claim that nothing is
     wedged. A failed IPC read keeps the last successful value (above), a
     failed GPU read skips that read, a `list_models()` that raises defers as
     `vllm` (rule 6), and an unreadable record gives `blocked_record` (rule
     3), so every other read failure already has its decision.
   - **Anything but `act` ends the attempt before the commit.** That covers:
     - the kill file or an opt-out that appeared (`disabled`);
     - a vLLM model that is now `LOADING` or `STAGED`, or a vLLM manager that
       cannot list its models (`deferred`, `vllm`);
     - GPU load (`deferred`, `gpu`);
     - a failed index read or `/proc` listing (`deferred`, `recheck`);
     - a verdict without a stalled model: a stalled model left `LOADING`, or
       load progress or stub CPU now rules the stall out (`idle`);
     - a record that changed meanwhile, for example one an operator seeded
       (`blocked_record`, `cooldown` or `exhausted`).

     The watchdog logs the WARNING "SELF-HEAL deferred at commit" with the
     reason and publishes the new decision; the block's `finally` ends the
     quiesce, so continuous workflows resume. After a `vllm`, `gpu` or
     `recheck` deferral it records the 300 s hold-off of rule 8 with that
     `deferredBy` and its recovery text (D8), which the surfaces show until
     the hold-off ends; `disabled` and `idle` need none. No attempt is
     recorded, because none was made.
   - **Commit.** `commit_restart(is_blocked, refusal)` applies the expiry,
     then re-checks the tickets under the condition's lock.
     - If an unblocked ticket is registered, it returns `"runs"`. The decision
       becomes `deferred` with `deferredBy: "runs"` (WARNING "deferred after
       quiescing"), and the block's `finally` ends the quiesce. The watchdog
       does not quiesce again for `QUIESCE_RETRY_AFTER_S = 300` (rule 8), so a
       busy device is not quiesced every 30 s.
     - If the quiesce has expired (more than `QUIESCE_MAX_S` since
       `begin_quiesce()`), it returns `"not_quiescing"`: something in the
       attempt was far slower than its bounds, so its inputs are stale. The
       decision is `deferred` with `deferredBy: "recheck"`, the WARNING
       "SELF-HEAL deferred at commit: the quiesce expired after 120s before
       the commit", and the 300 s hold-off.
     - Otherwise it switches to RESTARTING and returns `"committed"`. From
       then on every `start_run`, every new gate wait and every `after_gate`
       is refused with "The LocalServer backend is restarting to clear a
       wedged Triton model load (self-heal attempt <k>); run the workflow
       again in a few minutes."
     - The check and the switch form one critical section with every
       registration, so no unblocked ticket is registered from here on, and
       every ticket still registered is one that the wedge blocks. A run that
       is refused at its registration or after its gate has not started its
       pipeline. The engine marks its row failed with the refusal text; a
       classic run gets the 503; a capture task and a digital-input trigger
       log the INFO refusal.
   - **The boundary.** The kill file, the configuration switch, rule 6 and
     rule 7 stop an attempt up to this re-check, which comes at most about
     85 s after the "SELF-HEAL quiescing" line (the 60 s drain plus the
     re-check), and never later than 120 s after it, because an older quiesce
     has expired and the commit then returns `"not_quiescing"`. From the
     commit on, the restart is unconditional; the ERROR line of step 8
     follows within a second. bugfix.md's Operator notes state the same
     boundary.
6. **Build the attempt.** Steps 6-8 run inside one `try:` (see "If steps 6-8
   fail" below).

   ```json
   {"attempt": 1, "requestedAt": "…Z", "bootId": "…", "bootSeconds": 86512.4,
    "restartedAt": null,
    "models": [{"name": "model-a", "loadingForSeconds": 4032, "suspectedCulprit": true}],
    "evidence": ["[repository] …", "[stall] …", "[pb_stub] …"],
    "action": "backend_restart",
    "runsBlocked": 2,
    "runsInterrupted": [{"kind": "execution", "id": "…", "workflow": "wf-1 v2"}],
    "vllmModelsDropped": [{"name": "…", "state": "READY"}],
    "budget": {"maxPer24h": 3, "minIntervalSeconds": 1800}}
   ```

   - `runsBlocked` counts every registered ticket (all of them blocked, step
     5); the restart interrupts all of them. `runsInterrupted` lists, at most
     10, those with an `on_interrupt` callback, which step 10 marks failed.
   - `vllmModelsDropped` is built here, from a fresh `list_models()`: every
     vLLM model that the manager reports as `READY`, `LOADING` or `STAGED`,
     with that state (rule 6). The re-check deferred on `LOADING` and
     `STAGED`, so those appear only if a load started in the moment since;
     the restart drops them all the same, and the record names them. Step 11
     refreshes the list once more just before the signal. A `list_models()`
     that raises here gives `null` (unknown) and a WARNING; it does not abort
     the attempt. After the restart the vLLM reconciler
     (`vllm_runtime/reconciler.py`, from
     `vllm-model-reload-after-backend-restart`) re-drives the staged models.
   - Strings are cut to 300 characters.
   - **Caps on write.** `models` keeps at most 10 entries
     (`SELF_HEAL_ATTEMPT_MODELS_MAX = 10`; the suspected culprit first, then
     the longest `LOADING`), and `evidence` at most 5 items
     (`SELF_HEAL_ATTEMPT_EVIDENCE_MAX = 5`), so a device with many models
     cannot grow an attempt without bound.
7. **Persist it.** Append the attempt to the record, keeping the last
   `SELF_HEAL_HISTORY_MAX = 20`. Then, while the serialized record is larger
   than 48 KiB (`SELF_HEAL_RECORD_WRITE_MAX_BYTES = 49152`), drop the oldest
   attempt that is older than 24 h; the rate limit needs only the last 24 h.
   Write it atomically. Rule 4 admits at most 3 attempts under 24 h, and with
   the caps of step 6 each of them stays under about 13 KiB plus its
   `vllmModelsDropped` list, which names one entry per vLLM model on the
   device and is not cut, so that the record keeps naming everything the
   restart drops. A record at the caps, with 20 vLLM models in each attempt,
   therefore stays under the reader's 64 KiB limit (Input Validation), and the
   system does not write a record that its own reader reports as
   `blocked_record`.
8. **Log it.** Log the ERROR line (see the catalogue) and flush the logging
   handlers.

   **If steps 6-8 fail.** On any exception in them, including a failed write:
   - call `abort_restart()`, so every `start_run`, gate wait and `after_gate`
     is accepted again at once;
   - log the ERROR "SELF-HEAL not performed";
   - if step 7 had written the attempt, rewrite it with
     `"action": "not_performed"` and a `notPerformedReason` (300 characters at
     most);
   - record the abort time for rule 5, so this process does not try again for
     30 min.

   A not-performed attempt still counts toward the rate limit, which is the
   conservative side, and the close-out after a restart skips it. Nothing
   after step 8 can abort.
9. **Arm the backstop.** A daemon thread calls `os._exit(86)`
   (`SELF_HEAL_BACKSTOP_EXIT_CODE`) after `SELF_HEAL_BACKSTOP_S = 60`. From
   here the restart is unconditional, and the backstop bounds step 10 too. If
   the thread cannot be started, step 11 calls `os._exit(86)` instead of
   sending SIGTERM, because a graceful shutdown without the backstop could
   wait on an open request forever.
10. **Fail the interrupted runs.** `run_activity.interrupt_all(error_text,
    budget_s=INTERRUPT_BUDGET_S)`, with `INTERRUPT_BUDGET_S = 10`:
    - It collects every registered ticket under the lock, then calls each
      `on_interrupt` outside it with "Interrupted: self-heal attempt <k>
      restarted the LocalServer backend to clear a wedged Triton model load;
      run the workflow again."
    - For an engine execution the callback is `_mark_failed_best_effort`
      (`pipeline_executor.py:3859-3876`). It uses a fresh session and changes
      only a row that is still `pending` or `running`.
    - Each call is contained: a failure logs a WARNING that names the run, and
      the action continues. Calls still left when the budget runs out are
      skipped, with one WARNING that gives their count. The whole step is
      inside its own `try`, so even an unexpected error in `run_activity`
      only logs a WARNING before step 11.
    - It returns `InterruptReport(marked: List[str], failed: List[str],
      skipped: int)`, a frozen dataclass: `marked` holds the run ids whose
      callback returned, `failed` the run ids whose callback raised, and
      `skipped` the number of calls the budget left out. The ids are those of
      the attempt's `runsInterrupted` entries, which step 6 built from the
      same registered tickets before they were written in step 7.
    - One INFO line lists the runs that were marked failed (`marked`), and
      the budget WARNING gives `skipped`; both read the report.
11. **Signal.**
    - **Refresh `vllmModelsDropped`.** List the vLLM models once more. If the
      list names a model that the attempt does not, rewrite the attempt with
      the new list (one atomic write). A failed write or a raising
      `list_models()` keeps the record as written, with one WARNING; it never
      delays the signal beyond the backstop.
    - Send `os.kill(os.getpid(), signal.SIGTERM)`; if it raises, call
      `os._exit(86)` at once.
    - The graceful path is uvicorn's shutdown: the bounded `shutdown_event`,
      then `app.py`'s `os._exit(0)`. Runs cannot start during it: `start_run`,
      new gate waits and `after_gate` are refused, and continuous runners skip
      their ticks.
    - The backstop covers a shutdown that stalls on an open request, since
      uvicorn waits for open requests with no bound.
    - `src/docker-compose.yaml` restarts the container on any exit (`restart:
      always` for the tegra and generic services, `unless-stopped` for
      generic_nvidia).
    - `killer`, `exiter`, the GPU and `/proc` roots and the `run_activity`
      instance are injectable for tests.

**After the restart.**

- **Greengrass is not involved.** This is a Docker-level process restart. The
  LocalServer component stays RUNNING, and the model components are not
  restarted, so no cleanup runs.
- **The record is closed out.** The new watchdog reads the record at start.
  Every attempt with `"action": "backend_restart"` and `restartedAt == null`
  gets `restartedAt` = now, and the INFO "backend restarted" line is logged. A
  new process proves that a restart happened after the attempt was persisted,
  whatever the wall clock says. Attempts that were not performed are left as
  they are.
- **Models come back on demand.** Models are `UNKNOWN` until something requests
  them. The per-run gate does that when a run needs the model, the continuous
  ModelGate after its grace, and operators through `/start`. A boot-time reload
  is F5c, out of scope.
- **Digital-input workflows.** Digital-input workflows have no readiness gate:
  a trigger that arrives before its model is READY fails and starts the load,
  and triggers after the load succeed. To avoid failed triggers, request
  `/start` for those models after a self-heal restart, or restart their model
  components.
- **Interrupted runs.**
  - Triggered and manual engine executions that the restart cut off were
    marked failed in step 10, with the self-heal error, before the signal.
    Nothing else would finish them: the existing startup reconciliation
    (`run_retention.reconcile_interrupted`, `run_retention.py:466-517`, called
    from `workflow_engine/runtime.py:159`) selects only continuous rows and
    never touches triggered or manual runs.
  - Continuous executions are left to that reconciliation. At the next start
    it fails their `pending` or `running` rows with its own error and counts
    them in their workflow's counters, which a direct update would skip.
  - Classic pipelines keep no row, so there is nothing to finish.
  - **Every workflow run that started before the commit is accounted for.**
    It registered at its start (a digital-input run at its trigger, before
    its frame grab), so at the commit it was either blocked, and was
    interrupted and marked failed in step 10, or unblocked, which made the
    commit defer. A run whose execution starts after the commit, or that had
    not yet reached its registration at the commit, is refused at
    `start_run`; one that leaves its gate after the commit is refused at
    `after_gate`. The execution's own thread then marks its row failed with
    the refusal text (for a new run, within milliseconds of the row's
    creation), while steps 6-11 and uvicorn's shutdown are still running. A
    refused digital-input trigger grabs no frame and logs the INFO refusal.
    Operator image-source previews are the one exception: they register at
    `run_pipeline`, after their frame grab ("Other `run_pipeline` callers").
  - **What remains.** A row that a trigger creates during the shutdown itself
    stays `pending` only if the process exit overtakes its thread in those
    milliseconds, as after any backend exit today. Such a run never started
    its work; the startup reconciliation keeps its rule of never touching
    triggered or manual rows (`run_retention.py:477-479`, its Requirement
    12.8).

**Reading the record.** The record is re-read from disk at every decision, since
it is small. An operator's reset, or a record seeded for a test, therefore takes
effect without a restart.

**Why it never loops.**

- It acts only on a stalled model, and only while the switch is on: an
  opt-out, the kill file, or a process that has not yet read its
  configuration turns it off.
- Each attempt is persisted before the signal.
- A fault inside the action cannot disrupt runs every tick. An aborted
  attempt, before the commit (step 3) or after it (steps 6-8), holds this
  process off for 30 min (rule 5); an unwritable record stops every action at
  its write probe, before any quiesce begins (step 2); a deferral after
  quiescing holds off further quiescing for 5 min (rule 8); and a quiesce
  never outlives `QUIESCE_MAX_S`, even if the watchdog thread dies.
- The budget is evaluated from that persisted history, which survives restarts,
  crashes and redeployments, because `/aws_dda` is a host directory.
- A new process starts its no-progress window at its own start, so a stall needs
  another 15 min to be detected again.
- Within any 24 h there are at most 3 restarts, at least 30 min apart; within
  one boot the ages come from `CLOCK_BOOTTIME`, so a wall-clock step cannot
  shorten either window.
- An unreadable record or a failed write means no action.
- Deleting the record is the operator's explicit reset.

### D8 — Surfaces: `/feature-configurations`, the shadow, the run-gate error

**`/feature-configurations`.** `get_features_triton` merges
`load_watchdog.model_annotations(model_id, status)` into a copy of each Triton
entry's `defaultConfiguration`, following the existing `failureReason` and
`executionProviderInfo` pattern. The merge is wrapped in `try/except`, so a
failure means no field, never a 500. The three fields are additive and each
appears only when it applies:

```json
"loadOutcome": {"outcome": "not_ready", "state": "LOADING", "reason": "…",
                "failureReason": null, "recordedAt": "…Z", "componentVersion": "8.0.0",
                "pbStubMissingFile": null},
"loadHealth":  {"state": "stalled", "loadingSince": "…Z", "loadingForSeconds": 4032,
                "noLoadProgressForSeconds": 3990,
                "activity": {"stubProcesses": 0, "cpuSecondsLastWindow": 0.0,
                             "gpuMaxPercentLastWindow": 0.0, "windowSeconds": 300},
                "evidence": ["…"], "recoveryAction": "…",
                "selfHeal": {"state": "cooldown", "until": "…Z"}},
"lastSelfHeal": {"attempt": 1, "requestedAt": "…Z", "restartedAt": "…Z",
                 "loadingForSeconds": 4032, "evidence": ["…"], "action": "backend_restart"}
```

| Field | Present when |
|---|---|
| `loadOutcome` | the status is not `READY` and a load outcome record exists |
| `loadHealth` | the model has been `LOADING` for at least `LOAD_HEALTH_AFTER_S`. `evidence`, `recoveryAction` and `selfHeal` appear only once it is stalled. |
| `lastSelfHeal` | a self-heal attempt in the last 24 h named this model (`LAST_SELF_HEAL_VISIBLE_S = 86400`) |

`activity.gpuMaxPercentLastWindow` is `gpu_max_permille / 10` (D6), rounded
to one decimal, and `null` while the GPU signal is unavailable.

Records come from disk and `loadHealth` from the watchdog's published view
(D6). Without a watchdog (it failed to start, its view is stale, or in host
tests) only the record-backed fields appear. `status` keeps Triton's raw
token, so every existing consumer, including
`model_convertor._get_triton_model_state`, is unaffected (3.4).

**Models that edgemlsdk does not list.** Triton lists a model that was
published after the backend started, and never loaded since, by name only;
edgemlsdk's `ListModels` copies only entries that carry a state
(`triton_server.cpp:353-357`), and only server creation seeds every name as
`UNKNOWN` (`:507-520`). So `list_triton_models` omits such a model, and when
its first load request was refused (409) or never sent, its load outcome
record would have no entry to appear on. `get_features_triton` therefore
appends, after the listed models and before the vLLM entries, one entry per
such record:

- the record's model is not among the listed names, and its `<m>` directory
  exists in the client's repository (`triton_server.model_dir`, D1);
- the entry is `TritonModel`, `modelName` the record's model,
  `status: "UNKNOWN"` (not loaded, the vocabulary of 2.8), and a copy of
  `get_default_configs_lfv(<m>)` plus `loadOutcome` as its
  `defaultConfiguration`;
- a record whose directory is gone adds nothing; the cleanup clears it after
  a removal (D2).

The helper that finds them,
`model_status_records.unlisted_recorded_models(listed, repo_dir, records_dir=None)`,
is shared with the snapshot builder, which lists the same models as
`UNKNOWN` in `modelReadiness.models` and with their record in `notReady`.
The step has its own `try/except`: a failure logs one WARNING and adds
nothing.

**Recovery action texts** (`recoveryAction`):

- **act or restarting:** "Self-heal is restarting the LocalServer backend
  (attempt k of 3 in 24 h)."
- **deferred, runs:** "Self-heal will restart the LocalServer backend once no
  workflow run or capture task is in flight (n in flight: <workflow_id vN, …>)
  and no continuous workflow is running (running: <workflow_id vN, …>). Pause
  those workflows, or restart the LocalServer backend manually, to clear the
  wedge."
- **deferred, vllm:** "Self-heal will restart the LocalServer backend once the
  vLLM model <name> finishes loading." When the vLLM manager cannot list its
  models: "Self-heal will restart the LocalServer backend once the vLLM
  runtime can list its models again."
- **deferred, gpu:** "Self-heal will restart the LocalServer backend once the
  GPU shows no load for 5 min (it reached <x>% in the last 5 min, which can be
  a long model load such as a TensorRT engine build). Restart the LocalServer
  backend manually to clear the wedge now."
- **deferred, recheck:** "Self-heal will check the wedge again after <ts>:
  immediately before restarting it could not confirm the stall (<the Triton
  model index could not be read|the stub processes could not be read|the
  check took too long>). Restart the LocalServer backend manually to clear the
  wedge now."
- **cooldown:** "Self-heal will restart the LocalServer backend after <ts> (at
  most one restart per 30 min)."
- **exhausted:** "Self-heal is exhausted: it restarted the backend n times in
  the last 24 h. Restart the LocalServer backend manually and investigate
  <culprit>; self-heal re-arms at <ts>."
- **disabled and blocked_record:** "Self-heal is off on this device (<reason>).
  Restart the LocalServer backend: LocalServer UI, Application health overview,
  Application logs and restart; or on the device `sudo docker restart <backend
  container>`." The reason names the opt-out (`ModelLoadSelfHealEnabled=<v>`,
  or the kill file), a configuration not read yet, or the record that is
  unreadable or cannot be written.

**The `dda-model-status` shadow.** The snapshot that both endpoints build
(`endpoints/feature_config.__gpu_status_snapshot`, `:154-161`) moves into
`load_watchdog.build_model_status_snapshot(statuses)`, which the endpoints and
the watchdog share. It never creates Triton: `statuses` is passed in, and
everything else comes from the running watchdog (`load_watchdog.current()`) and
from the records on disk. It returns the unchanged `device_gpu_status(...)`
document plus two additive keys, which the gpu-status endpoint's response also
gains:

```json
"modelReadiness": {"updatedAt": "…Z", "readyModels": 3, "coldModels": 0,
  "models": [{"model": "model-a", "state": "LOADING", "stalled": true},
             {"model": "model-b", "state": "READY", "stalled": false}],
  "notReady": [{"model": "model-a", "status": "LOADING", "stalled": true, "since": "…Z",
                "reason": "…",
                "loadOutcome": {"state": "LOADING", "recordedAt": "…Z", "reason": "…"}}],
  "truncated": false},
"selfHeal": {"enabled": true, "disabledBy": null, "state": "idle", "deferredBy": null,
  "attemptsIn24h": 0, "maxPer24h": 3, "minIntervalSeconds": 1800, "rearmsAt": null,
  "last": {"attempt": 1, "requestedAt": "…Z", "restartedAt": "…Z",
           "models": [{"name": "model-a", "loadingForSeconds": 4032}],
           "evidence": ["…"], "vllmModelsDropped": []}}
```

**Shadow update semantics decide the shape.** `model_status_shadow.report()`
sends `{"state": {"reported": snapshot}}` as an `UpdateThingShadow` through
`IoTShadowAccessor.update_thing_shadow_state_request`. A shadow update merges
objects key by key, replaces an array whole, and deletes a key whose value is
`null`; a key that a report leaves out survives. The repo records this as a
hardware finding (`camera_sync/agent.py:335-337` and `:549-552`). So:

- **Every per-model collection is an array** sorted by model name: `models`,
  `notReady`, `selfHeal.last.models`, `selfHeal.last.evidence` and
  `selfHeal.last.vllmModelsDropped`. A report replaces each array whole, so a
  model that recovered or was removed is gone from it after the next report.
- **The two objects have a fixed key set,** written in full on every report. A
  value that does not apply is written as `null`, which removes the previous
  value from the shadow; readers treat a missing key and `null` alike. Inside
  array elements, keys that do not apply are left out instead: an element is
  replaced whole, so nothing in it can go stale.
- **Both keys are in every report.** `/feature-configurations/gpu-status`'s
  branch for an empty repository or a non-Triton device (`feature_config.py:143-146`)
  reports `build_model_status_snapshot({})` instead of
  `device_gpu_status({}, {})`, so its arrays are empty rather than absent.
  `/feature-configurations` reports only when it listed Triton models, as
  today, and the watchdog reports the empty snapshot when the repository becomes
  empty (D6 step 2).

The GPU-fallback document's existing `models` object keeps its merge behavior,
except after a size rejection ("Size", below); F6 does not read it (bugfix.md,
Out of Scope).

**Contents.**

- `models`: every Triton model component in `statuses` (names `model-…`;
  `base_`/`marshal_` steps and `.`-names skipped), plus the record-backed
  models that `statuses` does not list ("Models that edgemlsdk does not
  list", as `UNKNOWN`), as `{"model", "state", "stalled"}`. `state` is
  Triton's token: `UNKNOWN` (not loaded), `LOADING`, `READY`, `UNAVAILABLE`
  (failed) or `UNLOADING`. This partially satisfies cold-model 2.8 on the
  device side: not loaded, loading, READY and failed are distinguished, but a
  conversion in progress reports as `UNKNOWN` (not loaded) until its load is
  requested (D12).
- `notReady`: the models whose status is `LOADING`, `UNAVAILABLE` or
  `UNLOADING`, plus those whose status is not `READY` and that have a load
  outcome record (the `loadOutcome` presence rule, Property 16), as
  `{"model", "status", "stalled", "since"?, "reason"?, "loadOutcome"?}`. A
  `READY` model is never listed, even while its leftover record waits for the
  watchdog to delete it (D6 step 7).
  - An `UNKNOWN` model without a record (for example after a backend restart)
    is counted in `coldModels` but not listed here, so a normal restart raises no
    deployment warning.
  - `since` is a stable wall-clock timestamp: the load request, or the first
    sighting of the state. Durations are left out, so content changes only on
    transitions.
  - `reason` is the stall's first evidence item, else Triton's failure reason,
    else the record's reason.
- `readyModels` and `coldModels`: counts over all models, exact even when the
  arrays are cut.
- `selfHeal`: `enabled`, `disabledBy`, `state`, `deferredBy`, `attemptsIn24h`,
  `maxPer24h`, `minIntervalSeconds`, `rearmsAt`, and `last` (the latest attempt
  in the last 24 h, else `null`). Without a running watchdog (a non-Triton
  device, a watchdog that failed to start, or a published view older than
  `WATCHDOG_STALE_S`, D6) `enabled` is false, `disabledBy` is
  `"watchdog_not_running"`, and the attempts come from the record.
  - It is built by `load_watchdog.self_heal_document(view, records_dir=None)
    -> dict`, which `build_model_status_snapshot` calls. It returns exactly
    this fixed key set: the switch, the decision and the deferral from the
    published `WatchdogView` (`None` when the watchdog is not running or the
    view is stale), and the counts, `rearmsAt` and `last` from `self_heal.json`
    in `records_dir` (`None` means `MODEL_STATUS_DIR`, D5). It never
    raises: an unreadable record gives `attemptsIn24h: 0` and `last: null`
    (a running watchdog reports such a record as `blocked_record`, D7).
- **The `selfHeal.state` values** map one-to-one from D7's decisions:
  - `idle`;
  - `deferred`, with `deferredBy: "runs" | "vllm" | "gpu" | "recheck"`
    (`recheck`: the re-check before the commit could not confirm the stall,
    D7 step 5);
  - `cooldown`;
  - `restarting` (decision `act`);
  - `exhausted`;
  - `disabled`, only while a model is stalled and self-heal is off;
  - `blocked_record`.

**Size.** At most `SHADOW_MODELS_MAX = 20` `models` entries (not-READY models
first), `SHADOW_NOT_READY_MAX = 10` `notReady` entries (stalled models first),
5 `selfHeal.last.models` entries and 3 evidence strings; every string at most
200 characters. The two keys together must fit
`READINESS_SHADOW_BUDGET_BYTES = 4096` bytes of canonical JSON. When they do
not, the builder applies these steps in order until they fit, and sets
`truncated: true`:

1. `selfHeal.last.evidence` keeps one item;
2. every `reason` in `notReady` (including `loadOutcome.reason`) is cut to 80
   characters;
3. `notReady` keeps 5 entries;
4. `models` keeps 10 entries;
5. the `reason` fields and `selfHeal.last.evidence` are dropped;
6. entries are dropped from the end of `models`, then of `notReady`, until it
   fits.

The last step always terminates.

**The rest of the document, and size rejections.** The budget bounds only the
two new keys. The GPU-fallback `models` object keeps an entry of about 130
bytes for every ONNX model with a provider record that the device ever
reported, because a shadow update never removes a key that a report leaves
out. On a long-lived device the merged document can therefore grow past
ShadowManager's size limit (8 KB by default), and from then on every write is
rejected, including the existing GPU-fallback content. So that the new keys
can never leave a device stuck there, a size rejection retires stale entries:

- **Detecting it.** When `model_status_shadow._write_shadow` catches an
  exception that is a size rejection, it sets a module flag. The test is the
  one `camera_sync/agent.py::_is_size_rejection` (`:238-254`, with
  `_SIZE_REJECTION_CODE` at `:259`) applies: the error code 413 as a
  standalone number, or "too large", "exceeds the maximum" or "size limit" in
  the error's type name, text or `message`. It is copied into the module as a
  private helper rather than imported: `camera_sync/agent.py` imports
  FastAPI, marshmallow and the camera-discovery modules at module level, and
  `model_status_shadow` imports only the standard library today.
  `take_size_rejection()` returns the flag and clears it.
- **Finding the stale entries.** On its next tick after a rejection, the
  watchdog reads the reported document once:
  `IoTShadowAccessor.get_thing_shadow_state_request(thing, "dda-model-status")`.
  Every LocalServer recipe already authorizes `aws.greengrass#GetThingShadow`
  on `$aws/things/*/shadow/name/*`. It collects the keys of
  `reported.models` whose model is not in the current Triton index, keeps
  them, at most 100, as retirements for the life of the process, and logs one
  WARNING with their count and names. It then clears its last-reported
  signature, so it reports on this tick even on a device where nothing polls
  the endpoints (a report inside the debounce window is retried on the next
  tick, as in "Reporting cadence").
- **Deleting them.** `build_model_status_snapshot` writes each retirement as
  `null` in `models`, an explicit delete that follows the camera-sync
  precedent (`deleted_source_retirements`, `camera_sync/agent.py:327-345`). A
  model that is present again keeps its live entry. The nulls are stable, so
  the change gate is unaffected after the first write.
- **When nothing helps.** If the read fails, or finds nothing to retire, the
  WARNING says so, and the next rejection retries. Entries of models that are
  still deployed are never deleted.
- **What it cannot see.** ShadowManager synchronizes the local document to the
  cloud. If only the cloud side rejects a document, the local write succeeds
  and nothing is retired. That limit predates this change; the 4096-byte
  budget keeps the new keys' share of it bounded.

**Reporting cadence.**

- `model_status_shadow.report()` now returns `"scheduled"` (a write was
  started), `"unchanged"` (identical to the last written snapshot) or
  `"dropped"` (a write in flight, inside the debounce window, no thing name, or
  an error). Nothing else in it changes, and the endpoints keep ignoring the
  result.
- The endpoints keep reporting as today.
- The watchdog's signature is the canonical JSON of `modelReadiness` (without
  `updatedAt`) plus `selfHeal`. It calls `report()` when the signature differs
  from the last one it reported, and advances its last-reported signature only
  on `"scheduled"` or `"unchanged"`. On `"dropped"` it retries on the next tick,
  so a transition that lands inside the reporter's 30 s debounce is written
  about 30 s later. A write that fails asynchronously is retried at the next
  change or heartbeat; the reporter already un-pins a failed write.
- While a model is not ready the watchdog also reports at least every
  `REPORT_HEARTBEAT_S = 900`.
- A healthy, unchanging device gets no watchdog writes (3.11).
- **Why a background reporter now.** This revisits
  `model-gpu-fallback-visibility` Decision 5 ("no dedicated poller") for this
  content.
  - In the incident nothing polled the endpoint after the Startups gave up, so
    a polling-only reporter would never have published the degraded state.
  - The reason that decision rejected a poller, standing up Triton from a
    background thread against an empty repository, is met the same way the
    continuous ModelGate meets it: `triton_repo_has_models()` runs first.
- **Portal reach.** The Portal's `GET /devices/{id}` already passes the
  reported document through (`devices.py:488-515`), so the new keys reach the
  device API without a Portal change. The Portal's ShadowManager synchronize
  list already includes `dda-model-status` (`deployments.py:588-590`).

**The run-gate error.** When D4's stall branch fires, `ensure_model_ready`
returns "Model '<model>' (Triton model '<target>') is not ready for inference:
it is stalled, LOADING for <dur> with no load progress for <dur> (<first
evidence item>). <recoveryAction>". The classic path returns it as HTTP 503;
the engine records it as the execution error.

### D9 — Leave unchanged entries alone; replace changed ones together, between loads (F5b)

A conversion publishes three directories: `base_<m>`, `marshal_<m>` and the
ensemble `<m>`. Today `convert_to_triton_structure` publishes them one after
another, each through `_atomic_publish_model_dir` (`model_convertor.py:604`,
`:643`, `:682`). The fix stages all three first, then replaces only the changed
ones, under one exclusive hold of the model's lock (step 6 states the one
exception, after a 300 s wait for it).

**The helpers.**

- The three `_build` closures move unchanged into `_base_model_builder(...)`,
  `_marshal_model_builder(...)` and `_ensemble_model_builder(...)`, which
  return them.
- `_atomic_publish_model_dir` (`:217-274`) splits into:
  - `_stage_model_dir(model_repo_dir, published_name, build_fn) -> Optional[str]`:
    today's `mkdtemp` and `build_fn` call, with the stamped prefix
    `repo_entries.staging_prefix(published_name)` (D4) instead of
    `".staging-<name>-"`, or today's prefix when `repo_entries` cannot be
    imported; it returns the staging path, or `None` after removing the staging
    directory and logging today's error line;
  - `_swap_model_dir(model_repo_dir, published_name, staging_path) -> bool`:
    today's swap, which removes the published directory and then calls
    `os.replace`.
- `_publish_model_dirs(model_repo_dir, model_name, specs) -> bool`, where
  `specs` is a list of `(published_name, build_fn, label)`, does the work below.
- `_atomic_publish_model_dir(model_repo_dir, published_name, build_fn)` stays as
  `_publish_model_dirs(model_repo_dir, lock_key(published_name),
  [(published_name, build_fn, None)])`. The three `_create_*_model_structure`
  functions keep their signatures and use it, because tests call
  `_create_base_model_structure` directly
  (`test_triton_inference_runtimes_fix.py:396`,
  `test_triton_inference_runtimes_preservation.py:352`).

**`convert_to_triton_structure`** validates its inputs as today (`:685-751`),
then calls `_publish_model_dirs(model_repo_dir, model_name,
[(f"base_{m}", base builder, "base"), (f"marshal_{m}", marshal builder,
"marshal"), (m, ensemble builder, "ensemble")])`, which:

1. Sweeps stale staging directories of these three names
   (`repo_entries.sweep_stale_hidden_dirs(model_repo_dir, names=…)`, D4),
   logging `INFO:Removed stale staging directory <name>` for each.
2. Stages all three, in that order. If one fails, it logs today's line
   ("Unable to create <label> model structure"), removes every staging
   directory, and returns False. Nothing published changes.
3. Compares each staging tree with its published tree:
   `_tree_manifest(staging) == _tree_manifest(published, ignore_runtime=True)`.
4. Removes the staging directory of every unchanged directory and logs
   "unchanged; leaving the published repository entry untouched". The published
   inode and mtime are untouched. If nothing changed, it returns True.
5. Takes `model_locks.acquire(model_name, exclusive=True,
   timeout_s=PUBLISH_LOCK_WAIT_S, lock_dir=lock_dir_for(model_repo_dir),
   on_wait=progress log)` **once**, with `PUBLISH_LOCK_WAIT_S = 300`, so the
   whole conversion waits at most 300 s.
   - On `LockTimeout`: log the WARNING and swap anyway. New artifacts must land.
     Replacing the directories is the lesser risk. A tracked load still in
     flight after 300 s is in one of three places:
     - already wedged (D6/D7);
     - past its stub's preinitialize: `model.py` is imported, and its
       artifacts are reached through symlinks into the Greengrass artifact
       directory, which the swap does not touch;
     - or still queued, in edgemlsdk's 4-worker queue or behind slower loads
       on Triton's single load thread (3-4 serialized loads take 4.5-6 min on
       the MIC-730), so it has not read its configuration yet and loads the new
       entries when it starts.

     A swap that races such a load's start, between its configuration read and
     its stub start, is bounded by F2 on the 2.45.0 images and by Triton
     2.63.0's own wait on JP7: the load fails fast as `UNAVAILABLE` and the
     Startup's retry loads the new entries.

     An untracked load (D1, "Residual: untracked loads") holds no lock, so a
     changed publish can swap under it. F2 on the 2.45.0 images, and Triton
     2.63.0's bounded wait on JP7, turn a resulting stub failure into a fast
     `UNAVAILABLE`, and the Startup's retry loads the new entries.
   - On `LockUnavailable`, a `ValueError` from `lock_key`, or when
     `model_locks` cannot be imported: WARNING, then swap as today.
6. Swaps the changed directories in the order base, marshal, ensemble, all
   under that one hold, except after the 300 s timeout of step 5, when they
   are swapped without the lock. Under the hold no tracked load can start
   between the swaps, so no load can see a new `base_` with an old ensemble.
   After the timeout, a load that starts between them can see mixed entries;
   F2 on the 2.45.0 images, and Triton 2.63.0's own bounded wait on JP7, turn
   that into a fast `UNAVAILABLE`, and the Startup's retry loads the new
   entries. A failed swap logs today's error, removes the remaining staging
   directories and returns False; directories already swapped stay swapped,
   as today.
7. Releases the lock in a `finally`, before `start_model` runs.
8. Removes, in an outer `finally`, any staging directory it still owns, also on
   `SystemExit`.

**SIGTERM.** Greengrass ends a racing Startup with SIGTERM (exit 143 at
14:52:19-20 in the incident). Python's default action ends the process without
running `finally` blocks, which is what leaves a staging directory behind.
`model_convertor.__main__` installs `signal.signal(signal.SIGTERM,
_sigterm_exit)` first; the handler only raises `SystemExit(143)`. The
`finally` blocks then remove the staging directories and release the lock, and
the exit code stays 143. `SystemExit` is not an `Exception`, so the existing
`except Exception` blocks (`:793-795` and those inside `start_model`) do not
swallow it. A SIGKILL or a power loss can still leave a staging directory; the
sweep (D4) removes it once it is 600 s old.

**Startup bound.** The new publish wait lengthens the Startup, which must
still finish inside the recipe's `Timeout: 1800` (`greengrass_publish.py:334`).
Worst case, in order:

| Phase | Bound | Source |
|---|---|---|
| Seed wait (shell, before the convertor starts) | 600 s | `seed_timeout=600`, `greengrass_publish.py:285` |
| Conversion: sweep, staging, manifests | seconds | D9 steps 1-4 |
| Publish lock wait | 300 s | `PUBLISH_LOCK_WAIT_S`, step 5 |
| `wait_for_server` ladder | about 95 s | 45 s of sleeps plus 5 connects of up to 10 s (`model_autostart_utils.py:52-75`) |
| Start budget, 3 attempts | about 520 s | per attempt a state read (10 s), `/start` (30 s) and the ready wait (120 s, plus up to 10 s for its last read), and 5 s backoff after the first two (`model_convertor.py:61-64`, `:67`, `:141`) |
| Final `failureReason` read and the record | about 10 s | D5 |
| pb_stub capture | 20 s | `PB_STUB_TOTAL_S`, D5 |

That is about 1,550 s plus the conversion's seconds, under 1,800 s even when
every phase runs to its bound. The Shutdown's bound is in D2.

**The manifest.** It is a dict from relative path to `("dir",)`,
`("file", sha256)` or `("link", os.readlink target)`. It walks with
`os.walk(followlinks=False)` and treats directory symlinks as links.

- **Runtime-written files.** `ignore_runtime=True` skips what the stub writes
  into the published tree while it loads: any `__pycache__` directory,
  `dda_active_providers.json` and `.dda_active_providers.*.tmp`. The TensorRT
  cache lives in the stage directory behind a symlink, so it is never walked.
- **Manifest errors.** If computing a manifest raises, that directory counts as
  changed: the publish runs as today (WARNING).

**Other callers.** `utils/lfv_to_triton.convert_models` (`lfv_to_triton.py:145`)
publishes through `convert_to_triton_structure` inside the backend during the
LFV→Triton switch. No Triton load is tracked then, so the lock is free and it
behaves like a Startup. The switch's other writer is out of scope (bugfix.md,
Out of Scope).

**Seeding.** `model_locks.py`, `model_status_records.py` and `repo_entries.py`
are added to `files_to_copy_to_dda_triton` (`triton_setup.py:134-138`).
`cp_model_conversion_files` copies the `/aws_dda/dda_triton/` files before the
`/aws_dda` scripts (`:158-163`, inside one `try`), so the recipe's seed-wait,
which waits for the scripts, also covers the new modules. The backend image
already has them: `COPY dda_triton ./dda_triton` puts the whole package at
`/dda_triton/`, which is the copy source.

The final layout and `config.pbtxt` content are byte-identical to today's
whenever a publish happens (3.2).

### D10 — LocalServer's CLI dependency becomes SOFT (F5a)

**The recipe edit.** In all seven LocalServer recipe files (`recipe-arm64-jp5.yaml`,
`recipe-arm64-jp6.yaml`, `recipe-arm64-jp7.yaml`, `recipe-amd64.yaml`,
`recipe-amd64-nvidia.yaml`, `recipe-arm64.yaml`, `recipe.yaml`), make
`ComponentDependencies.aws.greengrass.Cli`:

```yaml
  aws.greengrass.Cli:
    VersionRequirement: ">=2.6.0"
    # SOFT (triton-model-cleanup-wedge): the CLI restarts whenever a
    # deployment merges configuration. A HARD dependency made each restart
    # restart LocalServer and, through their HARD dependency on it, every
    # model component. LocalServer needs only the CLI's IPC operations
    # (restart, list, component details) at call time.
    DependencyType: SOFT
```

Also add `ModelLoadSelfHealEnabled: "true"` to `DefaultConfiguration`, with a
one-line comment: self-heal is on by default; merge "false" to opt a device
out (triton-model-cleanup-wedge). The value is the string `"true"`, like the
recipes' `LocalLoginEnabled: "false"`, and the parser also accepts a YAML
boolean (D7).

`recipe.yaml` stays byte-identical to `recipe-arm64-jp6.yaml`
(`test_f2325_recipes_declare_tes.py`). `greengrass-build/recipes/*.yaml` are
build outputs (`build-custom.sh:109` copies `recipe.yaml`; the ECR path writes
`recipe-ecr.yaml`), so they pick the change up when they are rebuilt.

**The publish path.** All three ECR rewriters copy `ComponentDependencies`
through `yaml.safe_load`/`yaml.dump`, each in an embedded heredoc:

- `gdk-component-build-and-publish.sh:448-493`;
- `portal-build.sh:455-500`;
- `publish-ecr-only.sh:114-168`.

Each gets one explicit normalization line after
`deps = recipe.setdefault('ComponentDependencies', {})`
(`gdk-component-build-and-publish.sh:458`, `portal-build.sh:465`,
`publish-ecr-only.sh:135`):

```python
deps.setdefault('aws.greengrass.Cli', {'VersionRequirement': '>=2.6.0'})['DependencyType'] = 'SOFT'
```

That way a stale `recipe.yaml` can never republish the HARD dependency. The gdk
(non-ECR) publish path copies `recipe.yaml` verbatim, so it inherits the edit.
`publish-ecr-only.sh` is sha256-pinned:
`test/backend-test/build_save_pkgs/baselines/publish-ecr-only.sh.sha256.txt`
must be rebaselined.

**The goldens.** Five golden families pin the recipes and must change only by
the two intended keys. In each commit message they are named as intended
recipe changes of this spec:

- `test/backend-test/csi_nvargus_optional/goldens/recipe_sha256.json`: update
  all seven hashes.
- `test/backend-test/output_bindings_fixes/goldens/recipe_{arm64_jp6,arm64_jp5,arm64,amd64}_full.golden.json`.
- `test/backend-test/deploy_reliability/goldens/recipe_{amd64,arm64,arm64_jp5,arm64_jp6}_structure.golden.json`.
- `test/backend-test/deploy_reliability/goldens/recipe_{amd64,arm64,arm64_jp5,arm64_jp6}_defect_e_baseline.golden.json`.
- `test/backend-test/vllm_hf_cache/goldens/code_baseline.sha256.golden.json`:
  the hashes of `recipe-amd64-nvidia.yaml`, `recipe-amd64.yaml`,
  `recipe-arm64-jp5.yaml`, `recipe-arm64-jp6.yaml` and `recipe-arm64.yaml`,
  pinned by `test_hf_cache_preservation.py::test_no_code_drift_from_unfixed_baseline`
  (`:394`).

The JSON structure goldens are edited surgically: add the `DependencyType` key
and the `DefaultConfiguration` key. The `--regenerate` options are not used,
because the Defect E baselines must be captured on a tree without the Defect E
fix. The `git diff` of the goldens must show only those two keys, or only the
recipe hashes.

**What changes for the backend.** With SOFT, a CLI restart no longer restarts
the backend. A CLI IPC call made during the restart, which takes about 1 s,
fails instead, and the callers already handle that:

- `gg_utils.restart_components` raises and the endpoint returns an error;
- `list_gg_components` raises to its endpoint;
- `DefectDetectionConfig.find_local_server_component_name` falls back to the
  prefix and retries on the next call (it caches only a real discovery).

The start ordering on a fresh device is unaffected: the dependency is still
declared, only restarts stop propagating. The interim behavior for
startup-only configuration, and how to force a restart, are in bugfix.md's
Operator notes. The follow-up spec `localserver-config-live-update` removes the
need.

### D11 — Portal deployment readiness warning (F6)

**In `edge-cv-portal/backend/functions/deployments.py`.**

- **The reader.** `_read_model_status_shadow(iot_data, thing_name)` mirrors
  `devices.get_model_status`. It returns the reported document, or None on
  `ResourceNotFoundException` or any other error, logging a warning on anything
  other than "not found".
- **The summary.** `_model_readiness_summary(usecase, region, session_name,
  component_names, effective_deployments)`:
  - Runs only when the deployment's `is_latest_for_target` is true (`:1036`).
    The shadow holds the device's current state, so for a superseded deployment
    it would describe what came after; such a deployment gets no field.
  - Returns None unless the deployment includes components named `model-*`.
  - Checks at most `MODEL_READINESS_MAX_DEVICES = 20` effective deployments
    whose `deployment_status` is `SUCCEEDED` or `COMPLETED`.
  - **Bounded reads.** It creates one `iot-data` client per summary, so a
    cross-account use case assumes its role once:
    `get_usecase_client('iot-data', usecase, session_name=…, region=region, config=SHADOW_READ_CONFIG)`,
    with
    `SHADOW_READ_CONFIG = botocore.config.Config(connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 1})`.
    - `total_max_attempts` counts the first request, so 1 means no retry;
      `max_attempts: 1` would still allow one retry (botocore's `Config`
      documentation). One shadow read therefore takes at most about 4 s.
    - A total budget, `MODEL_READINESS_BUDGET_S = 5`, is measured on
      `time.monotonic()` from the start of the summary and checked before
      each device's read. When it is used up, the summary stops there and
      sets `truncated: true`, as the 20-device cap does.
    - The summary therefore adds at most about 9 s, plus the one client
      creation, to `get_deployment`. That call runs behind API Gateway's
      integration timeout (29 s by default); the handler's own timeout is
      60 s (`compute-stack.ts`, `DeploymentsHandler`).
    - A read that times out is one more error for the reader (None, a
      warning), and the device is skipped.
  - For each device, keeps:
    - the `modelReadiness.notReady` list items whose `model` is one of the
      deployment's components;
    - `selfHeal` when its `state` is not `idle`, or its `last` is not null.
- **The payload.** It is added to the response as `deployment['model_readiness']`
  only when non-empty:

  ```json
  {"warning": "Current device state: 2 model(s) of this deployment are not READY on 1 device(s)",
   "devices": [{"core_device": "…", "shadow_updated_at": "…Z",
                "not_ready": [{"model": "model-a", "status": "LOADING", "since": "…Z",
                               "stalled": true, "reason": "…"}],
                "self_heal": {"state": "exhausted", "enabled": true, "attempts_in_24h": 3,
                              "last_requested_at": "…Z", "rearms_at": "…Z"}}],
   "checked_devices": 1, "truncated": false}
  ```

  - `shadow_updated_at` is `modelReadiness.updatedAt`.
  - Strings are cut to 200 characters and types are checked: `notReady` must
    be a list of objects with a string `model`, and `selfHeal` an object;
    anything malformed is skipped.
  - The whole summary is wrapped in `try/except`, so an error means no field
    (3.8).
- **The client factory.** `get_usecase_client` and `create_boto3_client`
  (`edge-cv-portal/backend/layers/shared/python/shared_utils.py:1530`,
  `:1633`) cannot pass a client configuration today. Both gain an optional
  keyword argument, `config=None`. When it is set, every `boto3.client(...)`
  call they make passes `config=config`; when it is `None`, the calls are
  exactly today's, with no `config` argument. Every existing caller is
  therefore unchanged. The alternative, building the client inside
  `deployments.py`, would duplicate the cross-account credential handling.
- **Permissions.** No IAM change is needed. The use-case role already grants
  `iot:GetThingShadow` (`usecase-account-stack.ts`, "IoTShadows"), and the
  deployments handler has it for the single-account path
  (`compute-stack.ts`, `deploymentsHandler`).

**In `edge-cv-portal/frontend/src/pages/DeploymentDetail.tsx`.** Extend the
`DeploymentDetail` interface with an optional `model_readiness`. Beside the
existing "Deployment Failed" alert (around line 364), render a Cloudscape
`<Alert type="warning" header="Models not ready on the device (current state)">`
with the warning text, the shadow's update time per device ("Current device
state, shadow updated <ts>"), one line per device and model ("model-a: LOADING
since <ts>, stalled"), and one self-heal line per device. Cloudscape's `Alert`
carries the alert semantics, and the text needs no color to be understood.

The Portal deploy is owner-gated: implementation stops before deploying and
asks.

### D12 — Documentation notes

Append to `.kiro/specs/triton-model-load-race/bugfix.md`:

```markdown
## Follow-up (2026-10-15)

Root cause #1 above was incomplete. The model component's Shutdown script,
`src/backend/dda_triton/convert_model_cleanup.py`, deletes the model's Triton
repository directories even when StopModel answers 403 "LOADING", and the
original incident's component log shows it did: the cookies component's
Shutdown logged the 403 "Model current state is LOADING" at 04:36:31.991 and
`Cleaned directory: …/base_model-cookies…` at 04:36:31.993, 154 ms before the
stub's failed read recorded above. The atomic publish in this spec is real but
secondary. The deletion under an in-flight load, Triton 2.45.0's unbounded wait
for the stub and the silent Startup exit are fixed in
[`triton-model-cleanup-wedge`](../triton-model-cleanup-wedge/bugfix.md).
```

Append this bullet to the "Revision history" list of
`.kiro/specs/cold-model-first-run-failure/bugfix.md`, in its existing
`- **date** — text` format:

```markdown
- **2026-10-15** — `.kiro/specs/triton-model-cleanup-wedge/` satisfies
  requirement 2.5 and, on the device side, 2.6: the Startup outcome record and
  the stall detection. It partially satisfies 2.8 on the device side: the
  `dda-model-status` shadow's per-model readiness list
  (`modelReadiness.models`, with the not-ready details in
  `modelReadiness.notReady`) distinguishes not loaded, loading, READY and
  failed, but a conversion in progress reports as not loaded until its load
  is requested, so the converting-in-progress state of 2.8 remains open
  here. 2.9 is extended there with the stall reason. 2.7 stays deferred to
  the Triton boot-time reconciler follow-up.
```

## Error Handling

| Operation | Failure | Recoverable? | Caller receives | Log |
|---|---|---|---|---|
| Cleanup: wait for the backend | ladder exhausted | yes | lock-only path (D2) | INFO, existing line |
| Cleanup: state read | timeout, non-200, bad JSON | yes | `None` (treated as unknown) | DEBUG |
| Cleanup: exclusive lock | still held at the bound | yes (skip) | exit 0, entries left | WARNING "skipped deleting" |
| Cleanup: exclusive lock | lock file unusable, or `model_locks` not importable | yes | HTTP-only fallback, or skip if unreachable | WARNING |
| Cleanup: `/stop` | non-200 | yes | rename anyway: the lock proves no tracked load | WARNING with status and body (≤ 200 chars) |
| Cleanup: unload wait | still READY/UNLOADING after 60 s | yes | rename anyway | WARNING |
| Cleanup: rename | `ENOENT` | yes | skip that directory | — |
| Cleanup: rename | other `OSError` | yes | `rmtree` in place while still holding the lock | WARNING |
| Cleanup: trash delete | `ENOENT` (a concurrent sweep removed it) | yes | treated as deleted; `Cleaned directory` logged | — |
| Cleanup: trash delete | other `OSError` | yes | trash left for the next sweep, exit 0 | ERROR |
| Cleanup: optional import | `repo_entries` or `model_status_records` raises on import | yes | a failed `repo_entries` import skips the sweep and uses the unstamped trash name (`.trash-<dir>-<pid>-<time_ns>`, D2); a failed `model_status_records` import skips the record clear; exit 0 | WARNING naming the module |
| Cleanup: arguments | missing or invalid `--model_name` | fatal for the run | exit 1, as today | ERROR |
| Cleanup: unexpected exception after argument validation | any `Exception` from `release_model` | yes (the Shutdown completes) | the entries are left as they are (trash names are swept later); the lock is released; exit 0 | ERROR with the traceback (log catalogue) |
| Hidden-directory sweep | `OSError` on a directory | yes | that directory stays for the next sweep | DEBUG |
| `request_load` | exclusive lock held (a removal or a replacement) | yes | `ModelReleaseInProgress`; HTTP 409 from `/start`; the gates re-inspect the entry and request again (next two rows) | INFO |
| Per-run gate: load request | `ModelReleaseInProgress` | yes | `kicked` back to False; the next poll re-inspects the entry and requests again | INFO once per call, no traceback |
| ModelGate: load request | `ModelReleaseInProgress` | yes | `_load` returns False and the model leaves `_load_requested`; the next check re-runs the file checks | INFO once per model per streak, no traceback |
| `request_load` | lock file unusable | yes | load proceeds without a lock | WARNING once per key |
| `request_load` | native `load_model` raises | as today | the exception, after the lock is released | ERROR, existing "Failed to start model" |
| Tracker | index read raises | yes | every lock kept; retried in 2 s | DEBUG |
| Tracker | unconfirmed state (no reason) for 120 s | yes | lock released; the residual of D1 applies | INFO |
| Tracker | a new request races a terminal read | yes | the generation check keeps the lock | — |
| Tracker pass | unexpected exception | yes | every handle kept; the next pass runs 2 s later | ERROR once per streak, with the traceback; DEBUG after that |
| Tracker thread | not alive | yes | `ensure_tracker_thread()` starts a new one at the next `request_load` or watchdog tick (≤ 30 s while anything is tracked) | — |
| Per-run gate | entry inspection raises | yes | treated as absent: load requested (fail open) | DEBUG |
| Per-run gate | entry never stable within 600 s | yes | not-ready outcome, no load requested | the outcome's message |
| Publish: staging | `mkdtemp` or `build_fn` fails | yes | `convert_to_triton_structure` returns False; every staging directory removed; nothing published changes | ERROR, today's lines |
| Publish: manifest | any | yes | that directory counts as changed; publish as today | WARNING |
| Publish: exclusive lock | held after 300 s | yes | publish anyway | WARNING |
| Publish: exclusive lock | lock file unusable, or `model_locks` not importable | yes | publish as today | WARNING |
| Publish: swap | `OSError` | as today | returns False; remaining staging removed | ERROR, today's line |
| Startup: SIGTERM | Greengrass stops a running Startup | — | `SystemExit(143)`: staging removed, lock released, exit 143 as today | — |
| Startup: record write | `OSError`, or `model_status_records` not importable | yes | Startup still exits 0 | ERROR |
| Startup: pb_stub capture | docker missing, timeout, byte cap, no match | yes | `pbStubMissingFile: null` | DEBUG |
| Watchdog: construction | any | yes | LocalServer starts without it | exception logged |
| Watchdog: tick | unexpected exception | yes | the tick is skipped and the next runs 30 s later; after `WATCHDOG_STALE_S` (300 s) without a completed tick, readers treat the watchdog as not running (`stall_for` gives `None`, `disabledBy: "watchdog_not_running"`) | ERROR once per streak, with the traceback; DEBUG after that |
| Watchdog: index read | any | yes | tick skipped | WARNING once per streak |
| Watchdog: `/proc` read or parse | any | yes | activity unknown, so not stalled | DEBUG |
| Watchdog: GPU node | none found at start | yes | GPU signal unavailable; the stub-CPU veto applies alone | — (the start line names it) |
| Watchdog: GPU read | `OSError` or a value that is not an integer 0-1000 | yes | that read skipped; no read in the window means unavailable | DEBUG |
| Watchdog: switch read | exception, non-dict, empty dict, or no `StreamShadowName` | yes | last successful value; before any success, off (`config_unreadable`); the periodic refresh retries no sooner than 60 s later (the reads at an attempt's start and before its commit are always made) | WARNING once per streak; `get_component_config` also logs its own ERROR line for each failed read (at most one per minute from the refresh) |
| Watchdog: switch value | unrecognized | yes | off (`config`), as an opt-out | WARNING once per value |
| Watchdog: record read | unreadable or invalid | yes (no self-heal) | decision `blocked_record`; file preserved | ERROR once per episode |
| Watchdog: vLLM list | `list_models()` raises | yes | decision `deferred` (`vllm`) | WARNING at most every 10 min |
| Self-heal: record write probe (step 2) | `OSError` | yes (no action) | decision `blocked_record`; no quiesce began | ERROR "SELF-HEAL off: … cannot be written" |
| Self-heal: `begin_quiesce()` (step 3) | returns False: the state is not IDLE (an internal fault) | yes (no action) | no quiesce began; decision `cooldown`: this process does not try again for 30 min (rule 5); a stale QUIESCING expires by itself after 120 s | WARNING "SELF-HEAL: run activity is <state>, not IDLE; not quiescing" |
| Self-heal: steps 3-5 before the commit | any unexpected exception in the drain, the re-check or the commit call | yes (no action) | the guarded block's `finally` calls `end_quiesce()`, so continuous workflows resume at once; decision `cooldown` for 30 min (rule 5); no attempt recorded | ERROR "SELF-HEAL not performed: <step> failed before the commit", with the traceback |
| Self-heal: drain | an unblocked ticket still registered after 60 s | yes | `end_quiesce()`; decision `deferred` (`runs`); no new quiesce for 300 s | WARNING "deferred after quiescing" |
| Self-heal: re-check reads (step 5) | `client.index_states()` raises, or `/proc` cannot be listed | yes (no action) | the stall cannot be confirmed: `end_quiesce()`; decision `deferred` (`recheck`); no new quiesce for 300 s; no attempt recorded | WARNING "SELF-HEAL deferred at commit: … could not be read" |
| Self-heal: commit (step 5) | the quiesce expired (more than 120 s since `begin_quiesce()`) | yes (no action) | `commit_restart` returns `"not_quiescing"`; decision `deferred` (`recheck`); no new quiesce for 300 s; no attempt recorded | WARNING "SELF-HEAL deferred at commit: the quiesce expired …" |
| Self-heal: re-check before the commit (step 5) | the kill file or an opt-out appeared, a vLLM model is `LOADING`/`STAGED` or cannot be listed, GPU load, the stall no longer holds, or the record changed | yes (no action) | `end_quiesce()`; the re-decided outcome (`disabled`, `deferred`, `idle`, …); a `vllm`, `gpu` or `recheck` deferral holds off a new quiesce for 300 s; no attempt recorded | WARNING "SELF-HEAL deferred at commit" |
| Self-heal: vLLM list at step 6 | `list_models()` raises | yes | `vllmModelsDropped: null`; the attempt continues | WARNING |
| Self-heal: vLLM refresh before the signal (step 11) | `list_models()` raises, or the rewrite fails | yes | the record stays as written; the signal is sent | WARNING |
| Self-heal: steps 6-8 after the commit | any exception, including a failed attempt write | yes (no action) | `abort_restart()`, so runs are accepted again at once; a written attempt is rewritten as `not_performed`; this process holds off for 30 min (rule 5) | ERROR "SELF-HEAL not performed" |
| Self-heal: rewrite as `not_performed` | `OSError` | yes | the attempt stays as written: it counts toward the limit, and the next process start may stamp it `restartedAt` although no self-heal restart followed | WARNING |
| Self-heal: interrupt callback (step 10) | raises | yes | that run is not marked; the restart proceeds | WARNING naming the run |
| Self-heal: interrupt budget (step 10) | 10 s used up | yes | the remaining calls are skipped; the restart proceeds | WARNING with the count |
| Self-heal: backstop (step 9) | the thread cannot be started | — | step 11 calls `os._exit(86)` instead of sending SIGTERM | ERROR |
| Self-heal: SIGTERM | `os.kill` raises | — | `os._exit(86)` immediately | ERROR |
| Self-heal: graceful shutdown stalls | uvicorn waits on an open request | — | backstop `os._exit(86)` after 60 s | — |
| Run start during the restart | `start_run`, a new gate wait or `after_gate` refused | yes | engine: execution failed with the refusal text; classic run: HTTP 503; other `run_pipeline` callers: `PipelineExecutionException` (503); capture task: ends before its first image; digital-input trigger: no frame grab and no submit, DIO health unchanged | INFO |
| DIO trigger: image-source read, frame grab or submit | raises after the ticket was registered | as today (the exception propagates to the DIO thread's handler) | the ticket is finished in the trigger's `finally`, so it never counts as in flight afterwards | as today |
| `run_activity`: quiesce expiry | QUIESCING for more than 120 s without a commit (a slow or dead watchdog) | yes | the state reverts to IDLE at the next call: `quiescing()` False, continuous runners tick again, `commit_restart` gives `"not_quiescing"` | WARNING once |
| `run_activity` | internal error | yes | the run proceeds as today | exception logged |
| Shadow report | write in flight, debounce, no thing name, error | yes | `"dropped"`; the watchdog retries next tick | — (existing WARNING on errors) |
| Shadow report: async write | accessor raises | yes | swallowed by the reporter, as today; retried at the next change or heartbeat | WARNING (existing) |
| Shadow report: async write | a size rejection | yes | flag set; the watchdog's next tick retires stale `models` entries (D8) | WARNING (existing), then the retirement WARNING |
| Shadow read for retirements | accessor fails, or `reported.models` is not an object | yes | nothing retired; the next rejection retries | WARNING |
| `/feature-configurations` merge | any | yes | entry without the new fields | WARNING |
| `/feature-configurations` record-backed entries (D8) | any | yes | no entries added | WARNING |
| Portal summary | any | yes | response without `model_readiness` | warning |
| Portal summary: one shadow read | connect or read timeout (2 s each, no retry), or any other error | yes | that device is skipped | warning (the reader's) |
| Portal summary: time budget | 5 s used up before every device was read | yes | the devices read so far, `truncated: true` | — |

## Input Validation

| Input | Required | Type and limits | On failure |
|---|---|---|---|
| `--model_name` (cleanup) | yes | `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`, not `.` or `..` | ERROR, exit 1. No path is built from an invalid name. |
| `--model_name` (convertor) | yes | the same pattern | ERROR, exit 1, before any filesystem write |
| Lock key | yes (derived) | the same pattern after stripping one `base_`/`marshal_` prefix | `ValueError`. `start_model_triton` maps it to HTTP 400 with the existing "Invalid model name" text; the gates log it and continue. |
| LocalServer configuration read | — | a `dict` that contains `StreamShadowName` | failed read: the last successful value, else off (`config_unreadable`) |
| `ModelLoadSelfHealEnabled` | no (missing = on, the default) | string or bool; the accepted values are in D7 | unrecognized: self-heal off, as an opt-out, WARNING once per value |
| `self-heal.disabled` | no | presence only; content ignored | — |
| `self_heal.json` | no | JSON object, `schema == 1`, ≤ 64 KiB, `attempts` a list of ≤ 20 objects whose `requestedAt` parses as ISO-8601 UTC; `bootId` (string ≤ 64), `bootSeconds` (number ≥ 0), `action` (`"backend_restart"` or `"not_performed"`; missing means `"backend_restart"`) and `runsInterrupted` (a list of ≤ 10 objects) optional. The writer caps each attempt's `models` at 10 and `evidence` at 5, and after appending drops attempts older than 24 h until the record is ≤ 48 KiB (D7 step 7) | treated as `blocked_record`: no action, ERROR, file left untouched for diagnosis. A missing or malformed `bootId`/`bootSeconds` only makes that attempt's age wall-clock based |
| Load outcome record | no | filename `^model-[A-Za-z0-9._-]+\.json$`, ≤ 16 KiB, `schema == 1`, `outcome == "not_ready"`; the writer cuts `reason` and `failureReason` to 2,000 characters (D5) | ignored (absent), DEBUG |
| Hidden directory names | — | stamped: the two patterns of D4; unstamped: `.staging-<name>-*` or `.trash-*` | an unstamped name is aged by its mtime and the wall clock |
| Boot id | — | `/proc/sys/kernel/random/boot_id`; its first 8 characters must be lower-case hex | unreadable or malformed: no stamp, so unstamped names (D4) and wall-clock attempt ages (D7) |
| GPU load node | no | the stripped content is an integer 0-1000 (per mille) | that read skipped (DEBUG); no successful read in the window: GPU signal unavailable |
| `/proc/<pid>/cmdline`, `/proc/<pid>/stat` | — | NUL-separated argv; `stat` parsed after the last `)` | that pid ignored |
| Reported `dda-model-status` document (retirements) | — | `reported.models` an object; its keys strings ≤ 255 characters | not an object: nothing retired, WARNING; at most 100 retirements kept |
| Shadow document (Portal F6) | no | `modelReadiness.notReady` a list of objects with a string `model`; `selfHeal` an object; strings ≤ 200 characters | malformed entries skipped |

## Invariants and Who Enforces Them

| Invariant | Owner | Why that layer |
|---|---|---|
| No removal or replacement of a model's entries while a tracked load of it is in flight | the flock: `model_locks` plus `load_tracker`; the cleanup and the publish take it exclusive | Only a kernel lock spans host and container, works without HTTP, and disappears with a dead process |
| A model holds at most one shared lock, released only after a confirmed terminal state (`READY`, or `UNAVAILABLE` with a reason) or 120 s of an unconfirmed one, with no request after that read | `load_tracker`'s one critical section, its classification and its generation check | Its callers run on several threads at once, and only edgemlsdk's `reason` distinguishes a finished load from Triton's pre-load state |
| Every Python-initiated load is tracked | `TritonEdgeClient.start_triton_model` (the single choke point) | All three of its callers go through it |
| A changed conversion swaps all its directories under one exclusive hold, after at most one 300 s wait; only when that wait times out does it swap them without the lock (D9 step 6) | `_publish_model_dirs` | It is the one place that sees all three directories |
| Stamped hidden directories from another boot, or older than 600 s by the boot clock, are removed, and younger ones are never touched, whatever the wall clock does; unstamped leftovers are aged by their mtime | `repo_entries.sweep_stale_hidden_dirs`, with the boot stamp that `staging_prefix` and `trash_name` put into the names | One helper names and ages them, used by the publish, the cleanup and the watchdog |
| A Startup whose conversion succeeded exits 0 | `model_convertor.__main__` | A non-zero exit rolls back the deployment |
| Self-heal never acts on a device that opted out, or in a process that has not yet read its configuration | `decide_self_heal` rule 2 plus the fresh read before acting | The switch is parsed in one place, and a failed read can never override an opt-out |
| At most 3 self-heal restarts per 24 h, at least 30 min apart, across restarts | the persisted record plus `decide_self_heal`; persisted before the signal | Only state on the host volume survives the restart it causes |
| Self-heal never acts on `LOADING` time alone, or during load activity | `evaluate_wedge` (evidence and the stub-CPU veto) and `decide_self_heal` rule 7 (GPU load), both pure functions | Testable exhaustively; there is no other way to reach `act` |
| No unblocked workflow run, including one that is still capturing or preparing, is registered when the self-heal signal is sent | `run_activity.commit_restart`, one critical section with every registration, plus a registration at the start of every workflow run path, a digital-input trigger's before its frame grab (D7) | Registration and the final check share one lock, and a run is visible from its first step |
| No workflow run is ever held, so a triggered run always captures its frame when it is triggered | `run_activity.start_run`, which never waits | A held run would inspect a later scene, often another part, and record a wrong result |
| Self-heal never commits when, immediately before the commit, the kill file or an opt-out exists, a vLLM model is loading or cannot be listed, the GPU shows load, no model is stalled, the stall cannot be confirmed because a read failed, or the record forbids it | the action's re-check (D7 step 5), which re-runs `evaluate_wedge` and `decide_self_heal` on fresh inputs | The quiesce takes up to about 85 s (never more than 120 s), during which each of these can change |
| Readers never see a half-updated watchdog state | the watchdog's single published `WatchdogView` (D6) | Gate threads, the thread pool and the endpoints read while the watchdog thread works |
| A failure before the point of no return leaves the backend accepting runs and continuous workflows running | before the commit, the guarded block of steps 3-5, whose `finally` calls `end_quiesce()` unless the commit succeeded, plus `run_activity`'s expiry of QUIESCING after 120 s; after it, the `try` around steps 6-8, which always calls `abort_restart()`, the only way out of RESTARTING | Otherwise a live backend could pause continuous workflows, or refuse every run, until something restarted it; the expiry also covers a watchdog thread that dies or hangs mid-attempt |
| Every triggered or manual run that a self-heal restart cuts off is marked failed before the signal, within the 10 s budget of step 10; a run that the budget skips is counted in a WARNING | `run_activity.interrupt_all` with the callbacks the executor registers; continuous runs by the startup reconciliation | Only the process that holds the registrations knows which runs it interrupts, and nothing reconciles triggered or manual rows at start |
| No new code creates Triton against an empty repository | the watchdog tick, the snapshot builder and the gates call `triton_repo_has_models()` first or take statuses from a caller that did | Creating the server against an empty repository has a documented hang |
| A published layout is unchanged whenever a publish happens | `_publish_model_dirs` | It is the only writer of entries besides the cleanup's removal; the LFV↔Triton switch is out of scope |
| Every published LocalServer recipe declares the CLI dependency SOFT | the recipe files plus the ECR rewriters' normalization line | Both publish paths derive from these |
| Host-run modules run under Python 3.8 and import only the stdlib, `requests` and protobuf | the Python 3.8 test run plus the import check | The host's system `python3` runs them |
| The shadow's per-model collections are arrays, its objects have fixed keys, the two keys stay ≤ 4096 bytes, and a size rejection retires stale GPU-fallback entries | `build_model_status_snapshot`, plus the reporter's rejection flag and the watchdog's retirement read | Shadow updates merge objects, and ShadowManager rejects oversized documents |

## Log Line Catalogue

`<…>` marks a value. Durations are written `1h07m12s`, `15m02s` or `42s`;
timestamps as ISO-8601 UTC (`2026-10-14T14:52:17Z`); paths relative to the
repository are shortened to `base_<m>/2/model.py`.

**Cleanup (host, `LEVEL:message`)**

```
INFO:Release <m>: backend reachable, state <state>
INFO:Release <m>: backend not reachable; relying on the model lock
INFO:Release <m>: a load is in flight (state <state>); waiting up to 300s for it to finish before removing its repository entries
INFO:Release <m>: still waiting for the in-flight load (<elapsed>s of 300s)
INFO:Release <m>: load finished (READY); unloading before removing its repository entries
INFO:Model stopped successfully!
INFO:Release <m>: unloaded (state <state>)
WARNING:Release <m>: the unload did not finish within 60s (state <state>); removing the entries anyway, no load is in flight
INFO:Cleaned directory: <absolute path>
INFO:Release <m>: removed stale trash <name>
INFO:Removed stale staging directory <name>
WARNING:Release <m>: skipped deleting the repository entries: a load of the model is still in flight after 300s (state <state>). The entries are left in place.
WARNING:Release <m>: the model lock is unavailable (<error>); falling back to the backend's reported state
WARNING:Release <m>: skipped deleting the repository entries: the model lock is unavailable and the backend is not reachable, so no in-flight load can be ruled out
WARNING:Could not import dda_triton.<module> (<error>); <falling back to the backend's reported state|skipping the hidden-directory sweep|not clearing the load outcome record>
ERROR:Release <m>: unexpected error (<error>); the repository entries are left as they are and the Shutdown exits 0 (followed by the traceback)
INFO:Directory cleanup finished
```

**Backend**

```
# load_tracker
INFO  Refusing a load of <m>: its repository entries are being removed or replaced (component shutdown or update in progress)
WARNING Model lock for <key> is unavailable (<error>); loading without it
INFO  Released the model lock of <m>: its load state stayed <state> without a recorded reason for 120s, so the load is treated as ended
ERROR Triton model-load tracker pass failed (<error>); every model lock is kept and the next pass runs in 2s (followed by the traceback; once per streak)

# start_model_triton (HTTP 409 detail)
Error while attempting to start model <m>. Its repository entries are being removed or replaced (component shutdown or update in progress). Start it again once its component is running.

# model_readiness
INFO  Triton model <target> is UNKNOWN but its repository entry is not ready (<blocker>); waiting before requesting a load (workflow model <model>)
INFO  Load request for Triton model <target> was refused: its repository entries are being removed or replaced; checking the entry again before the next request (workflow model <model>)

# model_gate (continuous workflows; replaces today's WARNING for this case only)
INFO  The load request for Triton model <triton_model> was refused: its repository entries are being removed or replaced; re-checking its files before requesting again
(returned) Model '<model>' (Triton model '<target>') is not ready for inference: state UNKNOWN; its repository entry is not ready (<blocker>). Waited <s>s; no load was requested.< If the entry stays incomplete, restart or redeploy the model component <target> so that its Startup republishes it.>
(the last sentence only when the blocker names a missing or unreadable path)
(returned) Model '<model>' (Triton model '<target>') is not ready for inference: it is stalled, LOADING for <dur> with no load progress for <dur> (<first evidence item>). <recoveryAction>

# load_watchdog
INFO  Triton model-load watchdog started (self-heal <on (default)|on (ModelLoadSelfHealEnabled=<v>)|off (ModelLoadSelfHealEnabled=<v>)|off (file /aws_dda/dda_triton/model_status/self-heal.disabled)|off until the LocalServer configuration is read>; GPU load signal <path|unavailable>; stall after 15m, at most 3 restarts per 24h and 30m apart)
ERROR Triton model-load watchdog tick failed (<error>); detection and self-heal resume at the next tick in 30s (followed by the traceback; once per streak)
INFO  Removed stale staging directory <name> (<<age> old|from an earlier boot>; left by an interrupted publish)
INFO  Removed stale trash directory <name> (<<age> old|from an earlier boot>)
WARNING Self-heal configuration could not be read (<error>); <using the last value read (<v>)|self-heal stays off until a read succeeds>
WARNING ModelLoadSelfHealEnabled has an unrecognized value '<v>'; self-heal is off (treated as an opt-out)
WARNING Triton model <m> is stalled: LOADING for <dur> with no load progress for <dur>; evidence: <items joined by "; ">. Recovery: <recoveryAction>
INFO  Triton model <m> is no longer stalled (state <state>)
INFO  Load outcome for <m> resolved: the model is READY; removed <path>
INFO  SELF-HEAL quiescing: the Triton model load is wedged (models LOADING: <…>); pausing continuous workflows and waiting up to 60s for <n> run(s) not blocked by the wedge to finish; new workflow runs count as in flight
WARNING SELF-HEAL: run activity is <state>, not IDLE; not quiescing, and self-heal waits 30m before it acts again
ERROR SELF-HEAL not performed: <the quiesce|the drain|the re-check|the commit> failed before the commit (<error>); continuous workflows resume, and self-heal waits 30m before it acts again. Restart the LocalServer backend manually to clear the wedge. (followed by the traceback)
ERROR SELF-HEAL: restarting the LocalServer backend to clear a wedged Triton model load. Models LOADING: <m> for <dur> (suspected culprit), <m2> for <dur>. Evidence: <ev1>; <ev2>; <ev3>. Action: backend process restart (attempt <k> of at most 3 per 24h, at least 30m apart; <b> run(s) blocked by the wedge are interrupted, <f> of them to be marked failed; vLLM models dropped: <name (state), …|none|unknown>). Record: /aws_dda/dda_triton/model_status/self_heal.json
INFO  SELF-HEAL marked <f> interrupted workflow run(s) failed: <execution id> (<workflow_id vN>), …
WARNING SELF-HEAL could not mark workflow run <execution id> failed (<error>); it may show as pending or running after the restart
WARNING SELF-HEAL skipped marking <n> interrupted workflow run(s): the 10s budget ran out
ERROR SELF-HEAL EXHAUSTED: the Triton model load is wedged (models LOADING: <m> for <dur>) but self-heal already restarted the backend <n> times in the last 24h; not restarting. Evidence: <ev>. Restart the LocalServer backend manually and investigate <culprit>; self-heal re-arms at <ts>. Record: <path>
WARNING SELF-HEAL waiting: the Triton model load is wedged (models LOADING: <…>); the last self-heal restart was at <ts> and self-heal restarts at most once per 30m; next possible restart at <ts>
WARNING SELF-HEAL deferred: the Triton model load is wedged (models LOADING: <…>); waiting because <n> workflow run(s) or capture task(s) not blocked by the wedge are in flight (<workflow_id vN, …|none>) and continuous workflow(s) <workflow_id vN, …|none> are running on READY models. Pause those workflows, or restart the LocalServer backend manually, to clear the wedge. Re-checking every 30s
WARNING SELF-HEAL deferred after quiescing: <n> run(s) or capture task(s) not blocked by the wedge were <still in flight after 60s|in flight at the commit> (<workflow_id vN, …>); the quiesce ends and continuous workflows resume. Not quiescing again for 5m
WARNING SELF-HEAL deferred at commit: <the kill file /aws_dda/dda_triton/model_status/self-heal.disabled exists|ModelLoadSelfHealEnabled=<v>|the vLLM model <name> is <LOADING|STAGED>|the vLLM runtime could not list its models (<error>)|the GPU was busy (up to <x.x>%)|no model is stalled any more (<m> is <state>|load progress|stub CPU <x.x> CPU-s)|the self-heal record changed (<decision>)|the Triton model index could not be read (<error>)|the stub processes under /proc could not be read (<error>)|the quiesce expired after 120s before the commit>; the quiesce ends and continuous workflows resume<. Not quiescing again for 5m (vllm, gpu and the last three reasons only)>
WARNING SELF-HEAL could not list the vLLM models (<error>); the attempt records vllmModelsDropped as unknown
WARNING SELF-HEAL could not refresh vllmModelsDropped before the restart (<error>); the record keeps the list taken at the commit
WARNING SELF-HEAL deferred: the Triton model load is wedged (models LOADING: <…>); waiting because the vLLM model <name> is <LOADING|STAGED>
WARNING SELF-HEAL deferred: the Triton model load is wedged (models LOADING: <…>); waiting because the vLLM runtime could not list its models (<error>)
WARNING SELF-HEAL deferred: the Triton model load is wedged (models LOADING: <…>); waiting because the GPU was busy in the last 5m (up to <x.x>%), which can be a long model load such as a TensorRT engine build. Restart the LocalServer backend manually to clear the wedge now. Re-checking every 30s
WARNING Triton model load wedge detected (models LOADING: <…>; evidence: <…>); self-heal is off on this device (<ModelLoadSelfHealEnabled=<v>|file /aws_dda/dda_triton/model_status/self-heal.disabled|LocalServer configuration not read yet>). Restart the LocalServer backend manually.
ERROR SELF-HEAL not performed: <building|recording|logging> the attempt failed (<error>); new workflow runs are accepted again, the attempt <is recorded as not performed|could not be recorded>, and self-heal waits 30m before it acts again. Restart the LocalServer backend manually to clear the wedge.
ERROR SELF-HEAL off: the record <path> is unreadable (<error>); delete or repair it to re-enable self-heal
ERROR SELF-HEAL off: the record <path> cannot be written (<error>); fix the directory or delete the file to re-enable self-heal
WARNING dda-model-status: ShadowManager rejected a report for its size; retiring <n> GPU-fallback entr(y|ies) of models that are no longer in the Triton repository: <names>
WARNING dda-model-status: ShadowManager rejected a report for its size, but no stale GPU-fallback entries could be retired (<reason>); the next rejection retries
INFO  SELF-HEAL: the backend restarted after self-heal attempt <k> (requested <ts>); record: <path>

# run_activity (refusal while restarting, at start_run, a new gate wait or after_gate; also the execution error or HTTP 503 detail)
INFO  Refusing <workflow run|capture task> <run id>: The LocalServer backend is restarting to clear a wedged Triton model load (self-heal attempt <k>); run the workflow again in a few minutes.
(a refused digital-input trigger logs this line with its workflow id as the run id)

# run_activity (the quiesce expired, once per expiry)
WARNING The self-heal quiesce expired after 120s without a commit; continuous workflows resume and workflow runs proceed normally
```

**Startup (host)**

```
WARNING:StartModel: <m> is not READY at the end of its Startup (state LOADING, <n> attempts over <s>s): <reason>. Recorded the outcome in /aws_dda/dda_triton/model_status/<m>.json; the Startup still exits 0 so the deployment is not rolled back.
ERROR:StartModel: <m> is not READY at the end of its Startup (state <state>, <n> attempts over <s>s): <reason>. Recorded the outcome in /aws_dda/dda_triton/model_status/<m>.json; the Startup still exits 0 so the deployment is not rolled back.
INFO:Removed stale staging directory <name>
INFO:Model <published> unchanged; leaving the published repository entry untouched
INFO:Model <published> changed; publishing the new entry
INFO:Publishing <m>: waiting for an in-flight load of the model to finish (<elapsed>s of 300s)
WARNING:Publishing <m>: a load of the model is still in flight after 300s; replacing the changed entries anyway so the new artifacts land
```

The WARNING Startup line is used when the last state is `LOADING`; the ERROR
line when the load is `UNAVAILABLE`, the backend was unreachable, or the
request was not accepted (D5).

## Correctness Properties

**Property 1: no removal under a tracked load (F1).** For any interleaving of
tracked load requests and cleanup runs of model M, the cleanup never renames or
deletes M's entries while M holds a tracked load. If the load is still in
flight at the bound, the cleanup exits 0 with the entries in place.
**Validates: 2.1, 2.2, 3.1**

**Property 2: refused loads during a removal or a replacement (F1).** While a
cleanup or a changed publish holds M's exclusive lock, every `request_load(M)`
raises `ModelReleaseInProgress` without calling the native load, and `/start`
answers 409. Neither gate stops asking because of it: after a refusal the
per-run gate's next poll inspects the entry again and requests again when the
entry is absent or stable, and ModelGate's next check re-runs its file checks
before it requests again. **Validates: 2.2, 2.3, 3.4**

**Property 3: one shared lock per tracked model, released only on a confirmed
end (F1).** For any set of concurrent `request_load` calls and any sequence of
index reads, each model holds at most one shared lock handle. It releases the
handle only after a pass that read a confirmed terminal state (`READY`, or
`UNAVAILABLE` with a reason), or after an unconfirmed state has lasted 120 s,
and in both cases only with no request after that read. It never releases on
`LOADING`, on an empty or failed read, or on an unconfirmed state younger than
120 s. **Validates: 2.2**

**Property 4: the per-run gate requests a load only for stable entries.** For
any repository state, `ensure_model_ready` requests a load of an `UNKNOWN`
model only when its entry is absent, or present, complete and unchanged for
10 s. It requests at most once per call, apart from a request refused with
`ModelReleaseInProgress`, after which it inspects the entry again before it
requests again, and it never requests while a live `.staging-` sibling
exists. A stale one (from another boot, or older than 600 s by the
boot clock) is removed by the next publish, cleanup or watchdog tick, after
which the load is requested. No wall-clock value makes a stamped directory
stale. **Validates: 2.3, 3.5**

**Property 5: an honest Startup outcome (F3).** When the conversion succeeds:
`start_model_detailed` not ready ⇒ a schema-1 record exists and the exit code
is 0, and the Startup line is WARNING exactly when the last state is
`LOADING`; ready ⇒ there is no record and the exit code is 0.
**Validates: 2.7, 3.3**

**Property 6: stalled needs evidence and no activity (F4).** For any input,
`evaluate_wedge` reports M stalled only if M has been `LOADING` for ≥ 900 s by
the monotonic clock, measured from `since(M)` (D6 step 5), the activity window
is covered with < 5 CPU-s, and E1(M) or E2 holds. Any covered window with
≥ 5 CPU-s yields no stalled model. No wall-clock value changes the result
except through E1's mtime comparison, and no GPU value changes it at all. A
tracked request older than the model's latest sighting outside `LOADING`
never makes a new episode a candidate. **Validates: 2.5**

**Property 7: self-heal is on by default, bounded and conservative (F4).** For
any sequence of verdicts, run activity, vLLM states, GPU samples, switch reads
(values, a missing key, failed reads, the kill file), process restarts (the
record re-read from disk), aborted attempts and clock advances or steps:

- the decision is `act` only while the switch is on: the last successful read
  in this process gave a missing key or an accepted "on" value, at least one
  read has succeeded, and there is no kill file;
- it is `act` at most 3 times within any 24 h of attempt age, at least 1800 s
  apart, counting not-performed attempts; an attempt dated in the future
  counts against both limits, and within one boot a wall-clock step changes no
  age (D7);
- it is not `act` within 1800 s of an aborted attempt in the same process, or
  within 300 s of a deferral after quiescing;
- it is never `act` while an unblocked run is in flight, a continuous workflow
  started an execution in the last 60 s, a vLLM model maps to `LOADING`
  (including `STAGED`), the vLLM manager cannot list its models, or the GPU
  signal is available with a non-zero maximum in the window;
- a kill file, an opt-out, a vLLM model mapped to `LOADING`, a vLLM manager
  that cannot list, a GPU sample of at least 1 per mille, a change that leaves
  no model stalled, or a record that forbids acting, any of which appears
  between `begin_quiesce` and `commit_restart`, and an index read or `/proc`
  listing that fails at the re-check, each mean that `commit_restart` is not
  called, `end_quiesce()` is, and no attempt is recorded;
- an exception anywhere between `begin_quiesce` and the commit means that no
  attempt is recorded, `end_quiesce()` is called, and the decision is not
  `act` for the next 1800 s in this process;
- every restart signal is preceded by a successful write probe and a
  successful attempt write, and the attempt's `vllmModelsDropped` names every
  vLLM model that the manager listed as `READY`, `LOADING` or `STAGED` at
  step 6 or at step 11.

**Validates: 2.6**

**Property 8: the quiesce never cuts off or holds an unblocked run, and every
way out of it restores service (F4).** For any interleaving of `start_run`
(bound or not), `bound`, `set_details`, `finish_run`, `gate_wait` entries and
exits and `after_gate` calls on several threads and tasks, with
`begin_quiesce`, `end_quiesce`, `commit_restart`, `abort_restart`,
exceptions injected anywhere in the action before the commit, and monotonic
clock advances:

- when `commit_restart` returns `"committed"`, every registered ticket is in
  its gate, gate-only, or outside its gate for at least 900 s with a stalled
  model; no ticket that is capturing, preparing or running on READY models is
  registered;
- in RESTARTING every `start_run`, every new gate wait and every `after_gate`
  is refused, so a ticket that leaves its gate after the commit never reaches
  its pipeline;
- no `start_run` is ever held: it returns at once in every state, and in
  QUIESCING it is accepted and counted;
- after `abort_restart()`, after `end_quiesce()` from QUIESCING, after an
  exception anywhere between `begin_quiesce` and `commit_restart`, and
  `QUIESCE_MAX_S` after `begin_quiesce` without a commit, `quiescing()` is
  False and every `start_run` is accepted; after the expiry `commit_restart`
  returns `"not_quiescing"`;
- a nested `start_run` in a context that holds a ticket is never refused or
  counted, and a run registered with `bind=False` and run inside
  `bound(ticket)` on another thread counts once; after `bound` exits, that
  thread's context holds no ticket;
- `quiescing()` is True exactly in QUIESCING (until it expires) and
  RESTARTING.

**Validates: 2.6, 3.5**

**Property 9: content-aware publish (F5b).** Converting the same inputs twice
leaves the published directories' inodes and mtimes unchanged the second time.
Changed inputs produce exactly today's layout and `config.pbtxt` bytes, and all
changed directories are swapped under a single exclusive hold after at most one
300 s wait, except when that wait times out, when they are swapped without
the lock (D9 step 6). Runtime-written files never count as a change. A failed staging
step leaves every published directory unchanged and no staging directory
behind. **Validates: 2.8, 3.2**

**Property 10: the CLI dependency is SOFT (F5a).** Every LocalServer recipe
variant, and the output of each ECR rewriter run on it, declares
`aws.greengrass.Cli` with `DependencyType: SOFT`. Nothing else in the parsed
recipe differs from the golden except `ModelLoadSelfHealEnabled: "true"`.
**Validates: 2.9, 3.12**

**Property 11: the backport is present only where it applies (F2).**

- The JP5, JP6 and x86 edgemlsdk Dockerfiles copy and apply the patch, pass
  `--no-force-clone`, and check both sentinels.
- Every `git -C build/python apply` argument is an absolute path equal to that
  Dockerfile's `COPY` destination for the patch.
- The patch touches only `src/stub_launcher.cc` and `src/stub_launcher.h`, and
  contains the bounded call and the `stub_pid_ != 0` guard.
- `Dockerfile.jp7` does not reference it.

**Validates: 2.4, 3.7** (host-checkable part; the behavior is checked on the
device by R3)

**Property 12: the shadow converges under merge semantics (F3, F4).** For any
sequence of snapshots applied to a document with shadow update semantics
(objects merge key by key, arrays are replaced, `null` deletes a key), the
merged `modelReadiness` and `selfHeal` equal the last snapshot's with its
`null` values removed. In particular, a model that recovered or was removed is
absent from `notReady` and `models`, and the two keys fit 4096 bytes. After a
size rejection, the next report removes every legacy `models` entry whose
model is not in the Triton index and keeps every other entry.
**Validates: 2.5, 2.6, 2.7**

**Property 13: preservation.** For inputs outside the bug conditions, F and F'
behave the same:

- READY run paths, never held, with no skipped tick outside a quiesce;
- the start and stop contracts, apart from the 409;
- ModelGate waits and reasons;
- `/feature-configurations` entries without the new fields;
- the shadow's existing keys;
- vLLM behavior;
- recipe structure outside the two keys.

**Validates: 3.1-3.14**

**Property 14: the Portal readiness field is additive (F6).**
`get_deployment`'s response keeps every existing field.

- `model_readiness` appears only for the latest deployment of its target, and
  only when a checked device reports a not-READY deployed model or self-heal
  activity.
- It is absent on any read error, missing shadow or malformed document.

**Validates: 2.10, 3.8**

**Property 15: every interrupted or refused triggered or manual run is
finished (F4).** For any set of tickets registered when `commit_restart`
returns `"committed"`, after step 10 each one with an `on_interrupt` callback has been
called once with the self-heal error, unless the 10 s budget ran out (one
WARNING gives the count). A triggered or manual engine execution whose row
was `pending` or `running` is then `failed` with that error. An engine
execution refused at `start_run` or at `after_gate` ends `failed` with the
refusal text, whatever phase it was in. Step 10 changes no row of a
continuous execution. **Validates: 2.6**

**Property 16: a load outcome record is always surfaced (F3).** For any set
of load outcome records and any Triton listing, every record whose model is
not `READY` and whose `<m>` directory exists appears in
`/feature-configurations` (on the listed entry, or on an appended `UNKNOWN`
entry when edgemlsdk does not list the model) and in the snapshot's
`notReady`; a record whose directory is gone appears in neither, and the
listed entries are unchanged apart from the additive fields.
**Validates: 2.7**

## Fix Implementation — Changes Required

**New, host-shared** (copied to `/aws_dda/dda_triton/`; Python 3.8; stdlib
only):

1. `src/backend/dda_triton/model_locks.py`: D1. Exports `LOCK_DIR`,
   `lock_dir_for`, `lock_key`, `lock_path`, `acquire`, `LockHandle`,
   `LockTimeout` and `LockUnavailable`.
2. `src/backend/dda_triton/model_status_records.py`: D5. Exports
   `MODEL_STATUS_DIR`, `SELF_HEAL_RECORD`, `SELF_HEAL_DISABLED_FILE`,
   `write_json_atomic`, `read_json`, `write_load_outcome`, `read_load_outcomes`,
   `clear_load_outcome`, `unlisted_recorded_models` (D8) and `utc_iso`.
3. `src/backend/dda_triton/repo_entries.py`: D4. Exports `inspect_entry`,
   `EntryInspection`, `boot_stamp`, `staging_prefix`, `trash_name`,
   `sweep_stale_hidden_dirs` and `HIDDEN_DIR_STALE_S`; it holds the code moved
   from `ModelGate._file_state`.

**New, backend-only:**

4. `src/backend/dda_triton/load_tracker.py`: D1. Exports `request_load`,
   `requested_at`, `inflight`, `ensure_tracker_thread`,
   `ModelReleaseInProgress`, `TRACK_POLL_S` and `TRACK_UNCONFIRMED_S`; the
   tracker's clock is injectable, and each pass is contained. Imports neither
   `panorama` nor `triton_edge_client`.
5. `src/backend/dda_triton/load_watchdog.py`: D6-D8.
   - The constants of D6/D7/D8.
   - The pure functions `evaluate_wedge`, `decide_self_heal`,
     `parse_self_heal_switch` and the snapshot size ladder.
   - The GPU sampler (`find_gpu_load_node(sys_root)`, `read_gpu_burst(path)`)
     and the shadow retirement read (D8).
   - `LoadWatchdog(client_provider, repo_has_models, clock, wall, proc_root,
     sys_root, config_reader, vllm_states, run_activity, shadow_reader,
     killer, exiter, records_dir)`, with its contained tick loop, the action,
     the guarded block of steps 3-5 with its re-check before the commit and
     its read-failure decisions (D7 steps 3-5), and the switch reader's 60 s
     retry delay (D7, "The reader").
   - The immutable `WatchdogView`, published once per tick, and
     `WATCHDOG_STALE_S` (D6).
   - `start_model_load_watchdog()`, `current()`, `stall_for`, `load_health`,
     `model_annotations`, `build_model_status_snapshot` and
     `self_heal_document(view, records_dir=None) -> dict` (the `selfHeal`
     object of D8), all reading the published view.
   - Module import has no side effects and imports no `panorama`.
6. `src/backend/utils/run_activity.py`: D7. Exports `start_run` (with
   `bind`, and no hold of any kind), `bound`, `set_details`, `finish_run`,
   `RunTicket`, `gate_wait`, `GateTicket`, `after_gate`, `begin_quiesce`,
   `end_quiesce`, `abort_restart`, `quiescing`, `state`, `commit_restart`
   (returning `"committed"`, `"runs"` or `"not_quiescing"`), `interrupt_all`
   with its `InterruptReport(marked, failed, skipped)` (D7 step 10),
   `snapshot`, the state names and the constants of D7, including
   `QUIESCE_MAX_S = 120`. The current ticket is a `contextvars.ContextVar`;
   the module state is one instance, with an injectable monotonic clock, that
   tests can replace.

**Modified backend:**

7. `src/backend/dda_triton/triton_edge_client.py`:
   - `__init__` adds `self.model_dir = model_dir or TRITON_MODEL_DIR` (the
     attribute does not exist today) and passes it to `create_server`;
   - `_list_models_raw` gains `quiet: bool = False`, which logs its
     empty-answer retry line at DEBUG;
   - the new `index_states()` returns `{name: (state, reason)}` from
     `_list_models_raw(quiet=True)` and raises on failure without logging (D1);
   - `start_triton_model` calls
     `load_tracker.request_load(model_id, self.triton_instance.load_model,
     self.index_states, lock_dir=model_locks.lock_dir_for(self.model_dir))`
     and then returns `self.get_model_status(model_id)`.
     `ModelReleaseInProgress` propagates without the "Failed to start model"
     ERROR line.
8. `src/backend/utils/feature_configs_utils.py`:
   - `triton_repo_has_models` ignores entries that start with `.`;
   - `start_model_triton` maps `ModelReleaseInProgress` to 409 and the lock
     key's `ValueError` to 400;
   - `get_features_triton` skips `.`-names, merges `model_annotations`, and
     appends the `UNKNOWN` entries of record-backed models that edgemlsdk
     does not list (D8).
9. `src/backend/endpoints/feature_config.py`: `__gpu_status_snapshot`
   delegates to `load_watchdog.build_model_status_snapshot`, and the
   gpu-status route's other branch reports `build_model_status_snapshot({})`.
   The routes are otherwise unchanged.
10. `src/backend/utils/model_status_shadow.py`: `report()` returns
    `"scheduled"`, `"unchanged"` or `"dropped"`, and `_write_shadow` sets the
    size-rejection flag that `take_size_rejection()` returns and clears (D8);
    nothing else changes.
11. `src/backend/dda_triton/model_readiness.py`: D4's `UNKNOWN` and `LOADING`
    branches on `TRITON_MODEL_DIR`, the injectable `entry_inspector` and
    `stall_lookup`, the re-request after `ModelReleaseInProgress` (D1), and
    `run_activity.gate_wait` with its refusal. The signature is otherwise
    unchanged.
12. `src/backend/workflow_engine/model_gate.py`: `_file_state` delegates to
    `repo_entries` (imported inside the method, as the module already does for
    `dda_triton`); `_load` returns False on `ModelReleaseInProgress`, which
    `_maybe_load_unknown` answers by discarding the model from
    `_load_requested` (D1).
13. `src/backend/dda_triton/convert_model_cleanup.py`: D2. It is rewritten
    around `release_model`; its CLI and existing log lines are kept, and only
    argument errors exit 1.
14. `src/backend/dda_triton/model_convertor.py`: `start_model_detailed`, the
    `observer` on `_wait_for_model_ready`, the record, pb_stub capture and log
    levels in `__main__` (D5), the builders, `_stage_model_dir`,
    `_swap_model_dir`, `_publish_model_dirs` and the SIGTERM handler (D9), and
    `--model_name` validation.
15. `src/backend/dda_triton/triton_setup.py`: add `model_locks.py`,
    `model_status_records.py` and `repo_entries.py` to
    `files_to_copy_to_dda_triton` (D9, "Seeding").
16. `src/backend/gstreamer/gst_pipeline.py`: `run_activity.start_run` before
    `run_pipeline`'s `try` and `finish_run` in its `finally` (D7); nested
    inside a registered run it changes nothing.
17. `src/backend/workflow_engine/pipeline_executor.py`: in `execute`,
    `start_run` directly after the pending check with the `on_interrupt`
    callback for triggered and manual executions and the refusal path,
    `set_details` after the registration lookup and after name resolution,
    `after_gate` after the readiness gate, and `finish_run` in the `finally`;
    the shared model-list helper (D7). `_await_model_readiness` keeps its
    signature.
18. `src/backend/workflow_engine/continuous_runner.py`: the quiesce check in
    `step()` (D7).
19. `src/backend/app.py`: `start_model_load_watchdog()` after
    `health.set_vllm_server(vllm_server)` (`:445`), contained.
20. `src/backend/endpoints/workflow.py`: in `run_inference_for_stream`,
    `start_run(…)` after `validate_workflow_requirements`, the rest of the
    body inside `try`/`finally: finish_run`, and the 503 refusals at
    registration and after the gate (D7, "Classic runs").
    `validate_model_readiness` is unchanged.
21. `src/backend/utils/capture_task_manager.py`:
    `capture_image_in_time_interval` registers a `"capture"` ticket for the
    task's whole duration (D7, "Capture tasks").
22. `src/backend/utils/digital_input_thread_manager.py`:
    `trigger_workflow_and_debounce` registers a `"pipeline"` ticket with
    `bind=False` before `_get_image_source`, logs the refusal and returns
    before the frame grab when it is refused, and finishes the ticket when the
    read, the grab or the submit raises; `execute_workflow` gains the
    `ticket=None` parameter, runs its body inside `run_activity.bound(ticket)`
    and finishes the ticket in an outer `finally` (D7, "Digital-input
    triggers"). The process-based `digital_input_process_manager.py`, used
    only outside Triton mode, is not changed.

**Native and recipes:**

23. `src/edgemlsdk/patches/edgeml-triton-python-backend.diff` (new), plus
    `src/edgemlsdk/Dockerfile.jp5`, `Dockerfile.jp6` and `Dockerfile`: D3.
24. Seven LocalServer recipes, plus the three ECR rewriters: D10.

**Portal:**

25. `edge-cv-portal/backend/functions/deployments.py`,
    `edge-cv-portal/backend/layers/shared/python/shared_utils.py` (the
    optional `config` keyword of `get_usecase_client` and
    `create_boto3_client`) and
    `edge-cv-portal/frontend/src/pages/DeploymentDetail.tsx`: D11.

**Documentation:**

26. `.kiro/specs/triton-model-load-race/bugfix.md` and
    `.kiro/specs/cold-model-first-run-failure/bugfix.md`: D12.

**Not changed:**

- `src/edgemlsdk/Dockerfile.jp7`, `triton_server.cpp`, `emltriton`.
- `vllm_model_prep.py` and the vLLM runtime.
- The model component recipe generator: model components stay HARD on
  LocalServer.
- `src/docker-compose.yaml`.
- The Startup budget constants.
- `continuous_runner.py`'s stall messages and counters.
- `utils/triton_to_lfv.py` and `utils/lfv_to_triton.py` (out of scope).

## Build, Baselines and Rollout

**Tests and baselines to update, in the same commit as the change that needs
them.** Each commit message names its rebaselines as intended changes of this
spec. Every pin below was matched against the current files with `sha256sum`.

- **With the edgemlsdk Dockerfiles and the patch (D3):**
  - Security preservation (`test/backend-test/security/baselines/`):
    - `docker_baseline_edgemlsdk_Dockerfile.jp5_masked.txt` and
      `docker_baseline_edgemlsdk_Dockerfile.jp6_masked.txt`: delete each and
      re-run `test_preservation_docker_masked_bytes.py` once to capture it. The
      capture-when-absent rule is in
      `_docker_preservation_support.capture_or_assert_text`. Their diff must
      show only the D3 lines.
    - `docker_baseline_out_of_scope.json`: the new `sha256sum` of
      `src/edgemlsdk/Dockerfile` (today `021a7f60…`).
    - `docker_baseline_multistage.json` and `docker_baseline_default_refs.json`
      are unaffected (no `FROM` or stage changes). Confirm by running the
      suite.
  - `test/backend-test/edgemlsdk_pythondev/baselines/`:
    - `edgemlsdk_Dockerfile.jp5.sha256.txt` (today `dd46208c…`, pinned by
      `test_preservation_baseline.py:103-108`);
    - `edgemlsdk_Dockerfile.jp6.sha256.txt` (today `72f34b75…`, `:110-114`);
    - `edgemlsdk_Dockerfile_pythondev_masked.txt`, the x86 file with only its
      python-dev line masked (`:91-101`).
    - `edgemlsdk_Dockerfile.jp7.sha256.txt` stays: JP7 is not edited.
  - `test/backend-test/edgemlsdk_cmake/baselines/`:
    - `edgemlsdk_Dockerfile.jp6.sha256.txt` (today `72f34b75…`, pinned by
      `test_preservation_baseline.py:87-91`);
    - `edgemlsdk_Dockerfile.jp5_cmake_masked.txt` (`:101-111`);
    - `edgemlsdk_Dockerfile_cmake_masked.txt`, the x86 file (`:93-99`).
    - `machine_setup.sh_install_cmake_masked.txt` stays.
  - **How to rebaseline these six.** Delete them, run
    `edgemlsdk_pythondev/test_preservation_baseline.py` and
    `edgemlsdk_cmake/test_preservation_baseline.py` once to capture them (both
    use `capture_or_assert_text`, which captures a baseline that is absent),
    then run both again to assert. The `git diff` of each masked view must
    show only the D3 lines. Both JP6 hashes must equal
    `sha256sum src/edgemlsdk/Dockerfile.jp6`, and the JP5 hash
    `sha256sum src/edgemlsdk/Dockerfile.jp5`.
- **With `app.py` (D6):** `test/backend-test/security/baselines/iam_out_of_scope_baseline.json`
  pins `src/backend/app.py` (today `fc30876c…`) for
  `security/preservation/test_preservation_iam_out_of_scope_guard.py::test_sibling_spec_files_unchanged`.
  Replace the hash, and append to the file's `note` field: "Rebaselined
  src/backend/app.py for triton-model-cleanup-wedge: only the contained
  start_model_load_watchdog() call after health.set_vllm_server was added; no
  IAM change. Hash fc30876c -> <new>."
- **With `pipeline_executor.py` (D7):**
  `test/backend-test/csi_nvargus_optional/goldens/backend_csi_consumers_sha256.json`
  pins `src/backend/workflow_engine/pipeline_executor.py` (today `7b80c297…`)
  for `test_preservation_csi_surface.py::test_backend_csi_consumer_file_untouched`.
  Replace that one hash; the other three files in it stay. The commit message
  states that the CSI consumer contract is unchanged: the edit adds only the
  run-activity registration at the start of `execute`, the `set_details` and
  `after_gate` calls, and the interrupt callback.
- **With `endpoints/workflow.py`, `utils/capture_task_manager.py` and
  `utils/digital_input_thread_manager.py` (D7):** no sha256 pin names any of
  them (checked with the pre-commit greps below). The existing in-image
  `test/backend-test/utils/test_digital_input_thread.py` patches
  `trigger_workflow_and_debounce` or the GPIO calls and still holds.
- **With `shared_utils.py` (D11):** no pin holds its hash or names it. The
  Portal tests that replace `get_usecase_client` do so for other handlers,
  and no test asserts its signature, so the additive keyword changes none of
  them.
  `test_cold_model_classic_path.py` asserts the handler's source statically;
  its four tests still hold after the change, because the gate call stays
  before the pipeline call and `validate_model_readiness` is unchanged.
- **With the recipes and the ECR rewriters (D10):**
  - `test/backend-test/build_save_pkgs/baselines/publish-ecr-only.sh.sha256.txt`
    (today `c0275cf1…`): rebaseline for the D10 line.
  - The five recipe golden families in D10, including the vLLM HF-cache
    golden's five recipe hashes.
- **`test/backend-test/dda_triton/test_triton_setup_preservation.py`**: its
  expected `/aws_dda/dda_triton/` copy set (`EXPECTED_DDA_TRITON_COPIES`,
  `:51-55`) gains the three host-shared modules; the exact-equality assertion
  (around `:208`) depends on it.
- **`test/backend-test/dda_triton/test_triton_inference_runtimes_{bug,fix,preservation}.py`**:
  add `model_locks.py`, `model_status_records.py` and `repo_entries.py` to
  `SRC_AUX_DDA_TRITON` (`:85`, `:76`, `:79`). These harnesses build a fake
  `/dda_triton/` source tree from that list; without the names,
  `shutil.copy2` raises inside `cp_model_conversion_files`, which then skips the
  `/aws_dda` scripts and `resources_for_copy`, and the tree assertions fail.
- **`test/backend-test/dda_triton/test_model_readiness.py`**: its `UNKNOWN`
  cases that assert a load request use the injectable `entry_inspector`
  (present and stable), or a temporary repository holding a complete, stable
  entry. Every other expectation stays.
- **The in-image native tests that publish into a repository inside the test
  tree** (`test/backend-test/dda_triton/test_eml_triton.py` and
  `test_triton_edge_client.py` call `convert_to_triton_structure` on
  `test_model_repository*` there): a changed publish now takes the model lock
  in `lock_dir_for(<repository>)`, which is
  `test/backend-test/dda_triton/locks/`. Each such test's existing cleanup
  also removes that directory, so no lock directory is left in the tree. The
  implementation checks the other `dda_triton` tests for the same pattern.

**Before every commit**, find every pin that the staged change can break, and
review each hit:

```
# pins that name a staged file
git diff --cached --name-only | xargs -n1 basename | sort -u \
  | xargs -I{} git grep -l -F {} -- test/ edge-cv-portal/backend/tests/

# pins that hold the pre-change sha256 of a staged file, however they name it
git diff --cached --name-only --diff-filter=M | while read -r f; do
  git show "HEAD:$f" | sha256sum | cut -c1-64 \
    | xargs -I{} git grep -l {} -- test/ edge-cv-portal/
done
```

Every hit is either updated in the same commit, as listed above, or shown to
be unaffected. Then run the per-commit public-repo hygiene checks of the
repository's process.

**Before every build** (`.kiro/steering/builds.md`):

1. Confirm no build is running (`pgrep -af "gdk component build"`,
   `pgrep -af "build-custom.sh"`).
2. Rebaseline as above.
3. Move `edge-cv-portal/infrastructure/cdk.out` aside.
4. Run the guard suite until it is green, and the full preservation suite in the
   flask-app image.
5. Build one target at a time with `gdk component build`, JP5
   (`aws.edgeml.dda.LocalServer.arm64JP5`) first, with each build's output in its
   own log.
6. Afterwards restore `gdk-config.json` and `recipe.yaml`
   (`git checkout -- gdk-config.json recipe.yaml`). The build scripts copy the
   target's recipe over the tracked `recipe.yaml`
   (`gdk-component-build-and-publish.sh:189`, `portal-build.sh:229`), and
   `test_f2325_recipes_declare_tes.py` and the CSI golden pin it to the JP6
   recipe.

JP6, JP7 and x86 builds follow, one at a time, after JP5 is verified on the
device. JP7 needs a build for the backend and recipe changes; its edgemlsdk
Dockerfile is unchanged.

**Rollout caveat: the first deployment of the fixed LocalServer.** During that
deployment's own cascade, the model components' Shutdowns run the previous
cleanup script: the host copies are re-seeded only when the new backend starts.

- Deploy the fixed LocalServer in a revision that adds or updates no model
  component, with workflows and triggers idle. That shape has been safe
  historically.
- On JP5, JP6 and x86 the new image's bounded stub wait (F2) also turns any
  deletion during that window into a fast `UNAVAILABLE` and a retry.

From the next deployment on, the guarded scripts run.

**Rollout caveat: self-heal is armed from the first start.** The fixed
LocalServer arms self-heal on every device that has not opted out. A device
that must never restart its backend automatically gets the opt-out in the same
deployment that brings the fixed LocalServer (a configuration merge of
`{"ModelLoadSelfHealEnabled": "false"}` for the LocalServer component), or the
kill file before that deployment
(`sudo mkdir -p /aws_dda/dda_triton/model_status && sudo touch /aws_dda/dda_triton/model_status/self-heal.disabled`;
the directory may not exist yet).

## Testing Strategy

### Exploration tests: must FAIL on unfixed code

They live under `test/backend-test/triton_cleanup_wedge/` (with a local
`fakes.py`; `test/backend-test/readiness_fakes.py` is not modified).

| File | Asserts the fixed behavior; on F it fails because… |
|---|---|
| `test_exploration_cleanup_under_load.py` | It runs the cleanup's main with a fake backend reporting `LOADING` and a temporary repository and lock directory, and asserts the directories survive. On F, `rmtree` runs (Property 1). |
| `test_exploration_run_gate_kick.py` | It runs `ensure_model_ready` on `UNKNOWN` with an entry whose `model.py` was written 2 s ago, and asserts no load is requested. On F, it requests at once (Property 4). |
| `test_exploration_republish_unchanged.py` | It converts the same fixture twice and asserts the published inodes are unchanged. On F, they are replaced (Property 9). |
| `test_exploration_silent_startup.py` | It runs the convertor's main with `start_model` not ready and asserts a record exists and the exit code is 0. On F, there is no record (Property 5). |
| `test_exploration_cli_dependency.py` | It parses the seven recipes and asserts SOFT. On F, the type is missing (HARD) (Property 10). |
| `test_exploration_python_backend_patch.py` | It checks the patch file and the Dockerfile lines, and that every `git -C build/python apply` argument in the three Dockerfiles is an absolute path equal to that Dockerfile's `COPY` destination for the patch. On F, they are absent (Property 11). |
| `test_exploration_wedge_report.py` | It feeds a stalled scenario to `/feature-configurations` and the shadow builder and asserts `loadHealth.state == "stalled"`. On F, the field is absent (Property 6). |

### Fix checking

The exploration tests pass on F' unchanged.

### Preservation checking

The baseline pass and skip counts are recorded before the change. Pre-existing
failures, such as the two in `test_lfv_to_triton.py` noted by
`triton-model-load-race`, are compared against those counts. The suites:

- `test/backend-test/dda_triton/`, in the flask-app image for the native tests,
  including `test_model_convertor_atomicity_{bug,fix}.py`, which must pass
  unchanged against the staged publish;
- `test/backend-test/workflow_engine/`, including
  `test_workflow_continuous_model_gate.py` and the cold-model suites;
- `test/backend-test/utils/test_feature_configs_utils.py` and
  `test_feature_configs_vllm_merge.py`;
- `test/backend-test/api-endpoints/test_feature_configuration_api.py` and
  `test_workflows_api.py` (the classic run handler and the capture task
  manager, in the flask-app image), plus
  `test/backend-test/workflow_engine/test_cold_model_classic_path.py`;
- `test/backend-test/model_gpu_fallback_visibility/`, including the reporter
  and gpu-status tests;
- `test/backend-test/vllm_model_reload/` and `test/backend-test/vllm_hf_cache/`;
- `test/backend-test/deploy_reliability/`, `output_bindings_fixes/`,
  `csi_nvargus_optional/` and `build_save_pkgs/`, after the intended golden
  edits;
- `test/backend-test/edgemlsdk_pythondev/` and `test/backend-test/edgemlsdk_cmake/`,
  after the D3 rebaselines;
- `test/backend-test/gstreamer/` and `test/backend-test/static_image_camera/`,
  which exercise `gst_pipeline.py` and the executor;
- `test/backend-test/test_py310_compat.py`;
- `test/backend-test/security/preservation`, the guard suite and the audits run
  by the in-image gate (`build-custom.sh`);
- the Portal backend tests for deployments, and the frontend vitest suite.

### Unit and property tests

All are host-side under `test/backend-test/triton_cleanup_wedge/` unless noted.

- **`test_model_locks.py`.**
  - Real `flock` between processes (a helper started with `subprocess`): shared
    excludes exclusive, two shared locks coexist, and a held exclusive lock
    times out.
  - A lock dies with its process.
  - `LockUnavailable` on an unwritable directory; key validation; `O_CLOEXEC`
    set; `LOCK_UN` before close; `lock_dir_for` on a temporary repository.
- **`test_load_tracker.py`.**
  - The shared lock is held from the request until a fake status turns
    terminal, and released after a native exception.
  - A held exclusive lock raises `ModelReleaseInProgress` without calling the
    native load. `start_model_triton` returns 409. An unusable lock still loads.
  - 16 threads call `request_load("model-a")` at once: exactly one lock handle
    is open, and none after the fake index turns READY (Property 3).
  - A terminal read followed by a new request before the release keeps the
    handle (the generation check).
  - Confirmation, with a fake index reader and a fake clock: after the request,
    `UNAVAILABLE` without a reason keeps the handle; `UNAVAILABLE` with a reason
    releases it; `UNKNOWN`, `UNLOADING` and an absent entry keep it until they
    have lasted 120 s, then release it with the INFO line; a `LOADING` read in
    between restarts the 120 s; a raising reader keeps every handle.
  - The tracker reads the index once per pass, however many models it tracks.
  - With a temporary repository, the client's `request_load` call locks
    `lock_dir_for(<that repository>)`, the same directory the cleanup and the
    convertor use.
  - A pass that raises (a fake classifier that throws) logs one ERROR for the
    streak, keeps every handle, and the next pass releases normally; a
    tracker thread stopped by a `BaseException` is replaced by
    `ensure_tracker_thread()`.
  - `index_states()` on a fake native client that answers empty logs nothing
    above DEBUG and raises; `list_triton_models` keeps today's WARNING.
- **`test_cleanup_release.py`.** The D2 decision table:
  - `LOADING` → `READY`: stop, unload, rename, delete;
  - `LOADING` at the bound: skip;
  - `UNKNOWN`: delete;
  - unreachable with a free lock: delete;
  - unreachable with an unusable lock: skip;
  - untracked `LOADING` under the exclusive lock: loop, then skip;
  - a `model_locks` import that raises `SyntaxError`: the fallback path;
  - a `repo_entries` import that raises `SyntaxError`: the entries are still
    renamed to the unstamped trash name `.trash-<dir>-<pid>-<time_ns>` and
    deleted, the sweep is skipped, and one WARNING names the module;
  - an unexpected exception inside `release_model`: the ERROR line with its
    traceback, the entries left as they are, no lock held, exit 0; a missing
    argument still exits 1;
  - the fresh read under the lock is made even when the ladder found the
    backend unreachable;
  - plus trash naming, the stale trash and staging sweep, and the exact log
    lines.
- **`test_repo_entries.py`.**
  - A hypothesis property over generated repository layouts:
    `inspect_entry(...).blocker` equals the pre-change `ModelGate._file_state`
    for every layout. The old body is kept as a test oracle.
  - `present` and `missing_path` as defined in D4.
  - The sweep, with a fake boot stamp and fake clocks: a same-boot staging
    directory 20 min old by `CLOCK_BOOTTIME` is removed and one 5 min old is
    kept; a wall clock that jumps forward by hours never removes a same-boot
    directory; a stamped directory from another boot is removed at once; an
    unstamped one is aged by its mtime; `names` restricts it to the parsed
    staging name (so `model-yolo` never matches `model-yolo-test`); a stamped
    trash directory is aged by its stamp.
  - `staging_prefix` and `trash_name` round-trip through the sweep's
    patterns, and `mkdtemp(prefix=staging_prefix(n))` still matches the
    ModelGate glob `.staging-<n>-*`.
  - The cleanup treats `ENOENT` on its own trash delete as done.
- **`test_run_gate_stable_entries.py`.** Absent, present-incomplete,
  present-changing, staging and stable entries; the stalled fail-fast message;
  the timeout message when the entry never settled, which adds the
  restart-or-redeploy sentence for a missing `base_<m>/<v>/model.py` and an
  unreadable `config.pbtxt` but not for "changed N s ago" or "is being
  published", and no load request for the permanently incomplete entry
  during the whole budget; a 20-min-old staging
  directory is removed by the next sweep, after which the gate requests the
  load.
  - A request refused with `ModelReleaseInProgress`, then the entry removed:
    the next poll requests again, the fake index turns `UNAVAILABLE`, and the
    gate fails fast, not after 600 s; one INFO line and no traceback. A
    refusal followed by a replaced, stable entry requests again and reaches
    READY. A generic exception keeps today's single kick (the existing
    `test_a_refused_load_request_keeps_waiting`).
  - ModelGate: a `_load` refused with `ModelReleaseInProgress` returns False
    and leaves `_load_requested` without the model; with the entry gone the
    next `check()` reports NOT_DEPLOYED, and with a replaced entry it requests
    once the quiet period has passed.
- **`test_startup_outcome_record.py`.**
  - Record contents for each reason class, atomic replace, the clear on READY,
    exit 0.
  - Write caps: a 50,000-character `failureReason` and `reason` are stored cut
    to 2,000 characters each, and the largest record the writer can produce
    (both texts at the cap, a model name of 255 characters) is returned by
    `read_load_outcomes()`.
  - The Startup line is WARNING for `LOADING` and ERROR for the other classes.
  - pb_stub capture with a fake `subprocess.Popen`: the command carries
    `--since` and none of `--until`, `--follow` and `--tail`; a match, no
    match, the 20 MiB cap, the timeout, docker missing; two container ids,
    where only the second (older) one has the line, are scanned newest first
    and the match is found.
- **`test_load_watchdog_detection.py`.** Unit cases for E1, E2, the activity
  veto, a window that is not yet covered, and culprit selection, plus a
  hypothesis test of Property 6. A wall clock that jumps forward by hours never
  makes a model stalled. The `/proc` sampler is tested against a fake
  `proc_root` tree.
  - `since(m)`: a request 2 h ago, READY seen since, and `LOADING` first seen
    now gives no candidate for 900 s, and E1 compares against the new
    sighting; a request newer than the last sighting outside `LOADING` dates
    the episode from the request.
  - The GPU sampler against a fake `sys_root`: a devfreq entry named `gv11b`,
    `ga10b` or `gb10b` is found; none means unavailable; unreadable or
    malformed reads are skipped; the window keeps the per-tick maximum. GPU
    values never change `evaluate_wedge`'s result.
  - Containment and the view: a tick that raises logs one ERROR for the
    streak and the next tick runs; with a fake clock 300 s past the last
    completed tick, `stall_for` returns `None` and the snapshot reports
    `enabled: false` with `disabledBy: "watchdog_not_running"`. Reader threads
    that call `stall_for` and `build_model_status_snapshot` in a loop while
    ticks run only ever see a complete view (each read view's fields belong
    to one tick).
- **`test_self_heal_policy.py`.**
  - The `decide_self_heal` table and switch parsing: the shipped `"true"` and a
    missing key mean on; `"false"` opts out; an unrecognized value is off, with
    `disabledBy: "config"`.
  - Failed reads: a reader returning `{}` after a `"true"` or missing-key read
    keeps it on; a reader returning `{}` after a `"false"` read keeps it off;
    `{}` with no earlier read means off with `config_unreadable`; a dict
    without `StreamShadowName` and a raising reader are failed reads.
  - The kill file wins over `"true"` and over a missing key.
  - The module-level reader is created once: two refreshes call
    `ListComponents` once (a fake IPC client counts the calls).
  - Clock steps: an attempt dated in the future counts toward exhaustion and
    cooldown; with the same `bootId`, a forward wall-clock step of hours does
    not end a cooldown; with another `bootId`, the wall clock decides.
  - vLLM, rule 6: a `None` manager means no vLLM models, for rule 6 and
    `vllmModelsDropped`; a `STAGED` model defers; a `list_models()` that raises
    defers with `deferredBy: "vllm"`; `vllmModelsDropped` lists the models
    mapped to `READY`.
  - GPU, rule 7: an available signal with a maximum of 1 per mille defers
    with `deferredBy: "gpu"`; 0 does not; an unavailable signal never defers.
  - A continuous workflow with an execution started 10 s ago defers, and the
    recovery action names it; one whose last execution was 90 s ago does not.
  - A corrupted or oversized record gives `blocked_record`; so does a failing
    write probe, before any quiesce begins.
  - Write caps: an attempt built from 30 stalled models and 12 evidence items
    keeps 10 models (the culprit first) and 5 evidence items; after appending
    to a record of 20 attempts older than 24 h, the oldest are dropped until
    the serialized record is ≤ 48 KiB. The largest record the writer can
    produce (3 attempts under 24 h at every cap, with 300-character strings
    and 20 vLLM models each, plus older attempts up to the history limit)
    passes the reader's validation and does not give `blocked_record`.
  - Aborts: an exception in steps 6, 7 or 8 calls `abort_restart()`; a written
    attempt is rewritten as `not_performed` and still counts toward the limit;
    the next decisions in the same process give `cooldown` for 30 min; the
    close-out at the next start skips the attempt.
  - A deferral after quiescing holds off a new quiesce for 300 s.
  - Every way out before the commit ends the quiesce, each with the real
    `run_activity` and a real `ContinuousRunner` on fakes:
    - a drain that raises: the ERROR "SELF-HEAL not performed: the drain
      failed before the commit" with its traceback, `run_activity.state()` is
      IDLE, `ContinuousRunner.step()` inserts a row again, and the decisions
      give `cooldown` for 1800 s;
    - a re-check whose `index_states()` raises, and one whose `/proc` listing
      raises: the WARNING "SELF-HEAL deferred at commit: <the Triton model
      index|the stub processes under /proc> could not be read", IDLE,
      `step()` inserts a row again, `deferred` with `deferredBy: "recheck"`,
      no new quiesce for 300 s, no attempt written;
    - a `decide_self_heal` that raises inside the re-check, and a
      `commit_restart` that raises: the ERROR names the step, IDLE, `step()`
      inserts a row again, `cooldown`;
    - a fake `run_activity` whose `begin_quiesce()` returns False: the WARNING
      "not IDLE; not quiescing", no `end_quiesce()` call, `cooldown`;
    - a re-check that a fake vLLM lister stretches past 120 s on the fake
      monotonic clock: `step()` inserts a row again once 120 s have passed,
      `commit_restart` returns `"not_quiescing"`, the WARNING "the quiesce
      expired", `deferred` (`recheck`) and the 300 s hold-off.
  - The switch reader after a failure: a reader that fails is not called
    again by the periodic refresh for 60 s (a fake IPC client counts the
    calls), while the reads at an attempt's start and before its commit are
    made whatever the delay.
  - The re-check before the commit, each with a fake drain that changes one
    input while it runs: the kill file is created; the configuration reader
    starts answering `"false"`; a `STAGED` vLLM model appears; the vLLM
    manager starts raising; a GPU sample of 1 per mille arrives; the stalled
    model turns READY; a seeded record puts the device in cooldown. In every
    case `commit_restart` is not called, `end_quiesce()` is, no attempt is
    written, the WARNING "SELF-HEAL deferred at commit" names the reason, and
    the `vllm` and `gpu` cases hold off the next quiesce for 300 s.
  - `vllmModelsDropped`: a manager listing `READY`, `STAGED`, `FAILED` and
    `UNLOADED` models records the first two with their states; a model that
    appears between step 6 and the signal is added by the refresh; a raising
    `list_models()` at step 6 records `null` and the attempt goes on.
  - With a fake killer: the record on disk already holds the attempt when the
    signal is sent, the backstop is armed before the interrupt callbacks run,
    and an interrupt callback that raises or hangs past the budget does not
    stop the signal.
  - Hypothesis, Property 7: random timelines of verdicts, switch reads and
    restarts, with a new `LoadWatchdog` per simulated restart reading the same
    records directory.
- **`test_run_activity.py`.**
  - Tickets, nested no-op tickets, model parsing from launch strings,
    blocked-ticket classification, `set_details`, and that instrumentation
    never raises.
  - The context variable: two threads and two asyncio tasks each hold their
    own ticket at the same time; a nested `start_run` in each is a no-op;
    after `finish_run` the context holds no ticket.
  - A run that changes threads: a ticket registered with `bind=False` on one
    thread leaves that thread's context empty and is counted once; inside
    `bound(ticket)` on a pool thread a nested `start_run` returns a no-op
    ticket; after `bound` exits, the pool thread's context holds no ticket and
    the next task on the same worker registers normally; `finish_run` of the
    unbound ticket from the pool thread unregisters it without raising, and a
    second `finish_run` is a no-op.
  - The gate: a registered ticket that is not in its gate makes
    `commit_restart` return `"runs"`; inside `gate_wait` it counts as blocked
    and the commit returns `"committed"`; leaving the gate after the commit
    makes `after_gate` return the refusal text; a gate wait without a ticket
    registers a gate-only ticket.
  - No hold: a `start_run` in QUIESCING returns at once (each call runs on a
    thread joined with a 1 s timeout), is accepted and counted, and makes
    `commit_restart` return `"runs"` until it finishes; once `commit_restart`
    returned `"committed"`, a `start_run` is refused.
  - The expiry, with a fake monotonic clock: `quiescing()` is True 119 s after
    `begin_quiesce()` and False at 121 s, with one WARNING; `commit_restart`
    then returns `"not_quiescing"`; `end_quiesce()` afterwards is a no-op; a
    new `begin_quiesce()` is accepted; RESTARTING never expires.
  - The state table of D7, cell by cell, including the no-op calls
    (`begin_quiesce()` outside IDLE, `commit_restart()` outside QUIESCING,
    `end_quiesce()` in RESTARTING), `commit_restart`'s three results, and
    `quiescing()` and `state()` in each state.
  - A fake run stream that starts a run every 100 ms on several threads, with a
    quiesce and commit in the middle: when `commit_restart` returns
    `"committed"` no unblocked run is registered, and no run is registered
    after it (Property 8).
  - Commit, then a record write that fails, then `abort_restart()`: afterwards
    `start_run` is accepted, a gate wait is accepted, and
    `ContinuousRunner.step()` inserts a row again.
  - `ContinuousRunner.step()` inserts no row while quiescing or restarting, and
    runs again at once afterwards.
  - In RESTARTING a new gate wait is refused, and `ensure_model_ready` returns
    not-ready at once with the refusal text.
  - `interrupt_all` calls each registered callback once with the error text,
    outside the lock, skips runs without a callback, contains a callback that
    raises, and stops at the budget with the count (Property 15); its
    `InterruptReport` lists the marked and the failed run ids and the skipped
    count.
  - Refused tickets: in RESTARTING, `start_run()` then `finish_run()` raises
    nothing and leaves the context empty, and `bound` and `set_details` on the
    refused ticket are no-ops; after `abort_restart()`, a new `start_run()`
    registers normally.
- **`test_interrupted_runs.py`.** With an in-memory database and a fake
  `run_activity`:
  - a manual execution blocked in the readiness gate (`pending`) and a
    triggered one blocked after it (`running`) are both `failed` with the
    self-heal error after `interrupt_all`;
  - a continuous execution registers no callback, and its row stays for the
    startup reconciliation;
  - a row already `succeeded` is not changed;
  - a triggered execution blocked inside `_prepare_stream_frame_feed` (a fake
    that waits on an event) at decision time makes rule 8 defer, and makes
    `commit_restart` return `"runs"` when it is still there at the commit;
  - an execution that enters `execute()` during a quiesce reaches its frame
    feed without waiting (the fake feed records the time it was called);
  - an execution that enters `execute()` after the commit ends `failed` with
    the refusal text before any capture or preparation step runs;
  - an execution whose readiness gate returns after the commit ends `failed`
    at `after_gate`, and its row never becomes `running`;
  - `_await_model_readiness(document)` keeps its signature and behaves as
    today outside a quiesce.
- **`test_run_registration_points.py`.** `endpoints/workflow.py` cannot be
  imported on the host (`test_cold_model_classic_path.py` explains why), so
  the classic handler is checked statically, in that suite's style: in
  `run_inference_for_stream`, `run_activity.start_run(` comes after
  `validate_workflow_requirements(` and before `validate_model_readiness(`,
  `after_gate(` comes between the gate and
  `configure_image_source_and_run_pipeline(`, and `finish_run(` sits in a
  `finally`. The capture task is checked by behavior: with
  `utils.captured_images_utils` and `dao.sqlite_db.sqlite_db_operations`
  replaced in `sys.modules` by stubs, `capture_image_in_time_interval` holds
  one `"capture"` ticket across its sleeps, its per-image pipeline call is
  nested, and the ticket is finished when the task ends or raises. A static
  check also asserts that no `start_run(` call in `src/backend` passes a
  `hold` argument.
- **`test_dio_run_registration.py`** (in the flask-app image, because
  `utils/digital_input_thread_manager.py` imports GStreamer, as in
  `test/backend-test/utils/test_digital_input_thread.py`). With
  `get_camera_frame`, `_get_image_source` and the executor's
  `execute_workflow_pipeline` patched, and a fresh `run_activity` instance:
  - a trigger registers one `"pipeline"` ticket, with the workflow's
    `modelName`, before `get_camera_frame` is called (the fakes record the
    order);
  - the ticket stays registered until `execute_workflow` returns on the pool
    thread, a `start_run` inside the fake `execute_workflow_pipeline` is a
    no-op, and afterwards the worker thread's context holds no ticket;
  - in RESTARTING a trigger is refused: `get_camera_frame` is not called,
    nothing is submitted, the INFO refusal line is logged once and the DIO
    health is unchanged;
  - a `get_camera_frame` that raises, and a `submit` that raises, leave no
    ticket registered;
  - the "Pipeline is full" branch registers nothing;
  - during a quiesce the trigger grabs its frame without waiting and its
    ticket makes `commit_restart` return `"runs"`.
- **`test_self_heal_surfaces.py`.**
  - The three `defaultConfiguration` fields and their presence rules, and the
    `lastSelfHeal` 24 h window.
  - The shadow keys, the fixed key sets, the size ladder and truncation.
  - A fake accessor that applies shadow semantics (recursive object merge,
    arrays replaced, `null` deletes): after LOADING → READY, the model is gone
    from `notReady` and `models` shows READY; after a model's removal it is
    gone from both; `selfHeal.last` replaced by a newer attempt keeps no stale
    key (Property 12).
  - The gpu-status branch for an empty repository reports both keys with empty
    arrays.
  - The report cadence: no write while unchanged; a write on a transition; a
    transition inside the debounce window is written on the next tick
    (`"dropped"` then `"scheduled"`); the heartbeat.
  - A `READY` model with a stale record shows no `loadOutcome` and the record is
    deleted.
  - Property 16: a record for a model that the fake `list_triton_models`
    omits, with its directory present, adds one `UNKNOWN` `TritonModel` entry
    with `loadOutcome` before the vLLM entries, and the snapshot lists the
    model in `models` and `notReady`; without the directory nothing is added.
  - `gpuMaxPercentLastWindow` is the per-mille maximum divided by 10, and
    `null` while the GPU signal is unavailable.
  - Size rejection: a fake accessor that applies shadow semantics and rejects
    a merged document over 8192 bytes, holding 60 legacy `models` entries of
    which 50 belong to models no longer in the index, rejects the first
    report with a 413-style error; the next tick reads the document, retires
    the 50 with `null`, keeps the 10 live ones and logs the WARNING once; the
    following report is accepted. A failing read retires nothing and logs the
    other WARNING.
  - The default switch shows `enabled: true`, `disabledBy: null`.
- **`test_content_aware_publish.py`.**
  - Property 9, including runtime extras.
  - With a held shared lock and a fake clock, a changed conversion waits about
    300 s in total (not 900 s), logs the timeout WARNING once, and performs all
    swaps under one exclusive hold; an unusable lock publishes as today.
  - A conversion run in a subprocess whose build blocks, then SIGTERMed, exits
    143 and leaves no `.staging-*` directory and no lock held.
- **`test_recipes_soft_cli.py`.**
  - Property 10 across the seven recipes, plus `recipe.yaml == recipe-arm64-jp6.yaml`.
  - Each ECR rewriter's embedded heredoc Python, extracted from the script text
    between `<<'PYEOF'` and `PYEOF`, run on a HARD-dependency fixture recipe,
    outputs SOFT.
- **`test_host_script_imports.py`.** An AST scan of `model_convertor.py`,
  `convert_model_cleanup.py`, `model_autostart_utils.py`, `constants.py`,
  `model_locks.py`, `model_status_records.py` and `repo_entries.py`: imports
  only from the stdlib, `requests`, `google.protobuf` and `dda_triton.*`, and
  the optional imports are wrapped in `except Exception`.
- **Python 3.8 run.** `test_model_locks.py`, `test_cleanup_release.py`,
  `test_startup_outcome_record.py`, `test_content_aware_publish.py` and
  `test_repo_entries.py` also run under a real Python 3.8, which catches 3.9+
  runtime APIs (`str.removeprefix`, `dict | dict`, PEP 585 generics) that a
  parse check misses. The interpreter is the build host's `/usr/bin/python3.8`
  (3.8.10, the JP5 host's version; `python:3.8-slim` where no host 3.8
  exists), in a throwaway virtual environment outside the repository with
  pytest 8.3.5, hypothesis 6.113.0, requests 2.32.4 and protobuf 5.29.6. If a
  pin no longer resolves, pip's resolver under 3.8 picks the version and
  `verification-notes.md` records it. These test files stay 3.8-compatible.
- **Portal.**
  - `edge-cv-portal/backend/tests/test_deployment_model_readiness.py`: shadow
    present, absent, error, malformed (a map instead of a list), truncated at
    20 devices; non-model deployments; a deployment that is not the latest for
    its target gets no field; existing fields unchanged. The time budget: the
    `iot-data` client is requested once per summary, with a `config` whose
    connect and read timeouts are 2 s and whose `total_max_attempts` is 1;
    with a fake monotonic clock that advances 2 s per shadow read, the
    summary stops after the 5 s budget with `truncated: true` and the devices
    read so far; a read that raises a timeout skips only that device.
  - `edge-cv-portal/backend/tests/test_shared_utils_client_config.py` (new):
    `get_usecase_client(..., config=cfg)` passes `config=cfg` to
    `boto3.client` in the single-account branch, the cross-account branch and
    the default-credentials case of `create_boto3_client`; without `config`
    the `boto3.client` calls carry no `config` argument, as today.
  - `edge-cv-portal/frontend/src/pages/DeploymentDetail.modelReadiness.test.tsx`:
    the warning is rendered with its content and the "current state" wording,
    and absent without the field.

### What host tests cannot prove

They cannot prove:

- Triton's real behavior with the patch;
- `flock` between the host and the container through the bind mount (the
  semantics are the kernel's, and the test uses two host processes);
- stub CPU during a real TensorRT build, and the Jetson GPU load node's
  readings on an idle and a busy device;
- Greengrass's restart semantics for SOFT dependencies, and whether an
  opt-out merged by a local deployment survives later cloud deployments that
  do not reset the key (the Operator notes recommend the device's own
  deployment for that reason);
- ShadowManager's merge behavior on the device (the fake accessor encodes the
  documented semantics);
- the restart policy.

On-device verification covers each of these, or records it as unverified.

## On-Device Verification (MIC-730, JP5)

Device changes need the owner's approval; it is given for the MIC-730 and is
sequenced in tasks.md. The MIC-730 also hosts the owner's PPE demo, so the
steps start with coordination. Device identifiers stay out of committed files,
including `verification-notes.md`.

**Before the first change.**

1. Read-only inspection is allowed at any time. Before the first change to the
   device's state (any deployment, restart, reproduction, or model start or
   stop), pause with a warning and wait for the owner's go.
2. Record the pre-state: the deployment revision, the component versions, the
   model states (`/feature-configurations`), pinned media, deployed workflows,
   and the demo processes in the backend container (the PPE clipper and live
   view).
3. Stop the demo's host trigger loop if one is running.
4. The fixed LocalServer arms self-heal by default. If the owner wants
   no automatic backend restart while other work uses the device, create the
   kill file once the fixed LocalServer runs, and delete it only at self-heal
   step 0.

**R1: cleanup during a load.**

- Restart the backend and wait for it to become healthy.
- Request `/start` for the yolo-test model, then run its cleanup about 1 s
  later.
- **Pass:**
  - the cleanup logs the "a load is in flight" line, then "load finished
    (READY); unloading", "unloaded", and three `Cleaned directory` lines;
  - no `pb_stub` missing-file line appears in the backend log;
  - every other model reaches READY normally (about 16 s alone; 3-4 models
    serialized in 4-6 min).
- Recovery is a component restart, which re-converts.

**R2: today's deployment shape.**

- Deploy a revision that adds an unused model component together with a trivial
  workflow.
- **Pass:**
  - the device's Greengrass log has no `Restarting service because dependency
    aws.greengrass.Cli was in a bad state` line for LocalServer;
  - no deletion happens under `LOADING`;
  - the deployment completes with all models READY, and the first workflow run
    succeeds.
- Roll back afterwards.

**R3: native probe.**

- Publish scratch entries with `base_*/<v>/model.py` removed, then request
  `/start`.
- **Pass:**
  - `UNAVAILABLE` with a `failureReason` within 30 s (typically 10-11 s);
  - sibling loads are unaffected;
  - restoring the file and requesting `/start` gives READY.
- Remove the scratch entries afterwards.

**Self-heal.** With F2 in place a missing-file stub no longer wedges, so the
stuck load is a scratch python-backend probe model (`model-selfheal-probe`)
whose `initialize()` blocks without using CPU (`threading.Event().wait()`, or a
`time.sleep(5)` loop). Its stub then stays far below `ACTIVITY_CPU_THRESHOLD_S`;
a probe that spins would trip the activity veto and never be reported as
stalled.

Self-heal is on by default on the fixed build, so it is armed throughout R1-R3
as well. They are expected to leave nothing wedged; if one does, its self-heal
lines and record are captured as part of that step's result.

**Every seeding of `self_heal.json`, and every step that re-wedges the probe
while self-heal could act, is done while the kill file
`/aws_dda/dda_triton/model_status/self-heal.disabled` exists.** That keeps the
device to the one planned restart in step 1.

0. **Preconditions.** The device has no `ModelLoadSelfHealEnabled` set (the
   recipe's `"true"` applies), no kill file and no `self_heal.json`. The demo
   is stopped (coordination above), and no capture task or other workflow run
   is in progress, since any of them defers self-heal (D7). For one minute, read the GPU load node
   that the watchdog's start line names; it must read 0, or rule 7 defers the
   restart. If it does not, find what uses the GPU before going on.
1. **The shipped default acts.** Publish the probe and request `/start` for
   it. If the owner approves a scratch workflow on the probe, start one
   manual run of it 6-14 min after the `/start`, so that its 600 s readiness
   wait spans the moment the probe becomes stalled. **Pass:**
   - after at least 15 min the "is stalled" WARNING appears, with the
     `[stall]` evidence;
   - at the next decision, one INFO "quiescing" line, then one `SELF-HEAL:`
     ERROR line naming the probe, how long it was LOADING, the evidence and
     attempt 1 of at most 3;
   - the container restarts (`docker inspect` start time and restart count),
     and the INFO "backend restarted after self-heal attempt 1" line appears;
   - `lastSelfHeal` appears on the probe in `/feature-configurations`, and
     `selfHeal.last` in the shadow, which the Portal's device API also
     returns;
   - the manual run, if one was started, ends `failed`: with the stall
     message if its gate saw the stall first (D4), or with "Interrupted:
     self-heal attempt 1 restarted the LocalServer backend …" if the restart
     cut it off. It is never left `pending` or `running` after the restart,
     and no unblocked execution failed at the restart. Which of the two
     messages appears depends on timing; the unit tests cover both paths.
2. **The per-device opt-out.** Merge `{"ModelLoadSelfHealEnabled": "false"}`
   with the `greengrass-cli` command from bugfix.md's Operator notes, and wait
   until the shadow reports `enabled == false` with `disabledBy == "config"`
   (at most 5 min). If step 5 will run, first request `/start` for the model
   that its continuous workflow uses and wait until it is READY: once the
   probe holds the single load thread, no other model can load. Then re-wedge
   the probe (request `/start`). **Pass:** after at
   least 15 min the "is stalled" WARNING, then the "self-heal is off on this
   device (ModelLoadSelfHealEnabled=false)" WARNING; `loadHealth.state` is
   `"stalled"` with the manual recovery action; `selfHeal.state` is
   `"disabled"`; no restart happens. The probe stays wedged for the steps
   below.
3. **The kill switch.** Create the kill file first, then reset the key with
   the `RESET` form from the Operator notes. **Pass:** `disabledBy` is
   `"file"`, the "self-heal is off" WARNING names the file, and no restart
   happens, although the configuration now says on.
4. **Rate limit and exhaustion** (the kill file exists; the probe is still
   wedged). Waiting out the real windows is not required.
   1. Seed `self_heal.json` with one attempt 10 min old: the current `bootId`
      and a `bootSeconds` 600 s in the past (D7).
   2. Delete the kill file. **Pass:** `selfHeal.state == "cooldown"`, the
      "SELF-HEAL waiting" line, and no restart. The seeded attempt leaves the
      cooldown 20 min later, so sub-step 3 must follow within that time.
   3. Recreate the kill file, seed three attempts within 24 h, and delete the
      kill file again. **Pass:** the "SELF-HEAL EXHAUSTED" line,
      `selfHeal.state == "exhausted"`, and no restart.
   4. Recreate the kill file.
5. **Deferral by a continuous workflow** (optional, and only with the
   owner's go, because it runs a workflow on the device during the
   test). It needs a continuous workflow whose model was made READY in step 2:
   the PPE demo workflow if it is deployed as a continuous workflow, or a
   scratch continuous workflow on a READY model. With the kill file present,
   delete `self_heal.json` and start the workflow. Then delete the kill file.
   **Pass:** the "SELF-HEAL deferred" WARNING names the workflow, and no
   restart happens. Recreate the kill file before pausing the workflow. If the
   workflow stops executing during the step, self-heal acts; that is attempt
   2, inside the budget, and is recorded as such. Without the go, or without
   such a workflow, the unit tests carry the rule and `verification-notes.md`
   says so.
6. **Clean up and restore the default.** With the kill file still present,
   restart the backend by hand to clear the probe's wedge, remove the probe
   and any scratch workflow, and delete `self_heal.json`. Then delete the kill
   file. **Pass:** the shadow reports `enabled == true` with
   `disabledBy == null`, nothing is `LOADING`, and no restart follows.

**F3 on the device.** While the probe holds the load thread and the kill file
exists (between self-heal steps 3 and 4, or before step 6), run the
convertor's Startup command by hand for a scratch component. **Pass:** after
about 6.3 min the WARNING `StartModel: … is not READY … (state LOADING …)` line
appears, the record appears in `/feature-configurations` and the shadow, and
the exit code is 0.

**Activity veto on real hardware.** If a non-demo model whose first load builds
a TensorRT engine is deployed, cold-load it with its engine cache moved aside
and record the watchdog's `activity.cpuSecondsLastWindow` during the build; it
must reach at least 5 CPU-s per 5 min, and the model must never be reported as
stalled. Record `activity.gpuMaxPercentLastWindow` as well: a build that drives
the GPU must read above 0, which would also defer self-heal. Restore the cache
afterwards. If no such model is available, record both thresholds as verified
by host tests only.

**After the tests.** Self-heal step 6 has removed the probe, the scratch
components and any seeded `self_heal.json`, and restored the default switch.
Confirm that nothing is wedged. Restart the demo's clipper and live view with the demo's
`demo.sh start`, and confirm the PPE demo workflow produces detections on the
fixed LocalServer.

**Sustained health.** After each deployment, the backend stays healthy for at
least 15 min, with no crash loop and no unexpected container restart.

**Other architectures.** JP6 (R1, R3), x86 (R1, R3) and JP7 (R1, as a
non-regression check) run only with the owner's go for each device. They are
recorded as pending in `verification-notes.md` until then.

## Out of Scope

These are the items in bugfix.md's "Out of Scope":

- F5c, the repository garbage collection and the Triton boot-time reconciler;
- the H hygiene items;
- the `localserver-config-live-update` spec;
- a Portal control for the self-heal switch;
- a GPU activity signal on x86;
- Startup budget sizing;
- reloading changed content that is already loaded;
- stations with authorization enabled;
- the LFV↔Triton switch's repository writers;
- the existing `models` object of the `dda-model-status` shadow, apart from
  the retirement after a size rejection (D8);
- Portal device-page rendering.

In addition:

- native stack capture of a live wedge;
- GPU load as a veto on the stall verdict (D6 explains why it defers only the
  restart).

## Review responses: design review, iteration 1 (2026-10-15)

| Finding | Response |
|---|---|
| 1 (HIGH): the self-heal default | Addressed at the time with option (A): self-heal was made opt-in, with the kill switch kept. **Superseded in iteration 3:** the owner decided that self-heal is on by default on every device, with a per-device opt-out (Owner Decisions 3b, and the iteration 2 responses below). The kill switch and the switch parsing carry over. |
| 2 (HIGH): the x86 patch path | Addressed. D3 makes every `apply` argument absolute; the x86 line is `git -C build/python apply /dependencies/server/edgeml-triton-python-backend.diff`. Reproduced during design: the relative form fails with "can't open patch", the absolute form passes. `test_exploration_python_backend_patch.py` and Property 11 assert absolute paths equal to each Dockerfile's `COPY` destination. |
| 3 (HIGH): stale `notReady` entries under merge semantics | Addressed. D8 makes every per-model collection an array sorted by model name, gives both objects a fixed key set with `null` for values that do not apply, and routes the gpu-status branch for an empty repository through `build_model_status_snapshot`, so both keys are in every report; the watchdog reports the empty snapshot too. D11's reader and the Input Validation row take a list of objects. `test_self_heal_surfaces.py` adds the merge-semantics fake accessor, and Property 12 states the convergence. The existing GPU-fallback `models` object keeps its behavior and is recorded in Out of Scope. |
| 4 (MEDIUM): the regeneration recipe | Addressed with the full clone and full SHAs. Re-run during design: 2 files, 10 hunks, +53/−13, and `apply --check` passes on a shallow r24.04 clone with an absolute patch path. |
| 5 (MEDIUM): missing goldens and harnesses | Addressed. D10 lists the vLLM HF-cache golden as a fifth family; Build, Baselines and Rollout adds `SRC_AUX_DDA_TRITON` in the three runtime harnesses (now three module names, since `repo_entries.py` is also seeded) and the pre-commit grep step. |
| 6 (MEDIUM): `load_tracker` critical sections | Addressed. D1 specifies one module lock across the tracked check, the non-blocking shared acquire, the native enqueue and the registration, and a tracker that reads status outside the lock and releases under it with a generation check. It deviates from the review's sketch in one respect: the status read is outside the lock. `test_load_tracker.py` adds the 16-thread test and the generation race, and Property 3 states the result. **Superseded in part by iteration 2, response 1:** this row's original reason, that the status read takes `TRITON_NATIVE_LOCK`, no longer holds. The tracker now reads `index_states()` through `_list_models_lock` and never takes `TRITON_NATIVE_LOCK`; the read stays outside the tracker's lock so that a slow read cannot block `request_load` (D1). |
| 7 (MEDIUM): a failed configuration read looked like a missing key | Addressed. D7 counts only a dict containing `StreamShadowName` as a successful read; an exception, a non-dict, `{}` or a dict without that key is a failed read. After a failure the last successful value applies; before any success, self-heal is off (`config_unreadable`) with one WARNING per streak. With the default now on (iteration 3), this rule is what keeps a read failure from overriding a device's opt-out. Tests are in `test_self_heal_policy.py`. |
| 8 (MEDIUM): continuous workflows and the restart window | Addressed with both parts. (a) A continuous workflow that started an execution in the last 60 s defers self-heal (`deferred`, `deferredBy: "runs"`), and the recovery action names it and suggests pausing it. (b) D7 adds the quiesce: continuous ticks skip without a row, `start_run` holds new runs for at most 30 s, runs drain for at most 60 s, and `commit_restart` re-checks and switches to refusing new runs in one critical section with registration, so no unblocked run is in flight at the signal. `test_run_activity.py` adds the 100 ms run-stream test, and Property 8 states it. **Superseded in part in iteration 4 (response 2):** `start_run` no longer holds any run; a run that starts during a quiesce is accepted and counts as in flight. |
| 9 (MEDIUM): the 2.8 reconciliation | Addressed with option (A): `modelReadiness.models` lists every Triton model component with its state and `stalled` (cap 20, inside the size ladder). bugfix.md's 2.8 row and the D12 cross-reference say "satisfied on the device side", noting that a conversion in progress shows as not loaded. **Superseded in iteration 4 (response 5):** both now say "partially satisfied on the device side". |
| 10 (MEDIUM): per-directory publish locking | Addressed. D9 stages all three directories, compares their manifests, takes the exclusive lock once (one 300 s deadline), swaps the changed directories under that hold and releases it before `start_model`. `_atomic_publish_model_dir` is split into stage and swap helpers and kept as a single-directory wrapper for the existing direct callers. 2.8, Property 1 and Property 9 follow. |
| 11 (MEDIUM): dropped shadow reports | Addressed. `report()` returns `"scheduled"`, `"unchanged"` or `"dropped"`; the watchdog advances its signature only on the first two and retries on the next tick. Tested in `test_self_heal_surfaces.py`. |
| 12 (MEDIUM): stale `.staging-*` directories | Addressed. `repo_entries.sweep_stale_hidden_dirs` removes staging and trash directories older than 600 s at the start of each publish, at the end of each cleanup and on every watchdog tick, with an INFO line per removal. D9 also adds a SIGTERM handler to the convertor, so a terminated Startup removes its own staging directories. Tested in `test_repo_entries.py`, `test_run_gate_stable_entries.py` and `test_content_aware_publish.py`; Property 4 and 2.3 follow. |
| 13 (NIT): `present` undefined | Addressed in D4, with `missing_path`. |
| 14 (NIT): Property 5 and R3 | Addressed: bugfix.md's Property 5 is limited to the CLI cause, with a note on the remaining HARD dependencies, and R3 passes at 30 s (typically 10-11 s). |
| 15 (NIT): Operator notes and cross-reference | Addressed: the `/restart-dda` sentence, the switch table's timing, and the `- **date** — text` format in D12 and in bugfix.md's own revision history. |
| 16 (NIT): host compatibility checked only by parsing | Addressed: the host-module tests also run under a real Python 3.8, optional imports catch `Exception`, and 3.14 says "standard library, `requests` and protobuf". The parse-only test is replaced by an import scan. |
| 17 (NIT): the pb_stub capture | Addressed: no `--tail`; `--since`/`--until` bound the read, and it is scanned line by line with a 20 MiB cap and a 10 s timer. **Superseded in part in iteration 3 (response 4):** `--until` is dropped, because `docker logs` without `--follow` stops at the present and a fixed `--until` could cut a slow Startup's window short. |
| 18 (NIT): clock steps and a missing vLLM manager | Addressed: all in-process durations are monotonic, and attempts dated in the future count toward both windows, as asked. D7 goes one step further: each attempt also records `bootId` and `CLOCK_BOOTTIME`, so within one boot a forward step cannot shorten a window either. A `None` vLLM manager means no vLLM models. |
| 19 (NIT): the probe must not use CPU | Addressed in On-Device Verification. |
| 20 (NIT): F3 log level for a load in progress | Addressed: WARNING for `LOADING`, ERROR otherwise (D5, the catalogue, 2.7, Property 5). |
| 21 (NIT): LFV↔Triton writers | Addressed with option (A): both are out of scope, with the reason, in bugfix.md and D9. |
| 22 (NIT): missing procedures | Addressed: `recipe.yaml` is restored with `gdk-config.json` after each build, and On-Device Verification adds the coordination pause, the pre-state capture and the demo restart. |
| 23 (NIT): superseded deployments | Addressed with both options: the warning is computed only when `is_latest_for_target` is true, and it is worded as the current device state with the shadow's update time. |

## Review responses: design review, iteration 2 (2026-10-15)

| Finding | Response |
|---|---|
| — (not a finding): the self-heal default | **Changed to on by default**, following the owner's decision of 2026-10-15 (Owner Decisions 3b), which supersedes the opt-in default of iteration 2. The recipes ship `ModelLoadSelfHealEnabled: "true"`; a missing key means on; a configuration merge of `"false"`, an unrecognized value or the kill file opts a device out; before its first successful configuration read a process does not act, so a read failure can never override an opt-out. Every iteration-2 safeguard stays, and findings 3, 4 and 11 add three more. Changed in bugfix.md (Owner decisions, 2.6, 2.11, 3.5, 3.12, Property 4, Operator notes, Out of Scope) and here (Overview, Owner Decisions, Glossary, D2, D7, D8, D10, the catalogue, Input Validation, Invariants, Properties 7 and 10, the tests and On-Device Verification). |
| 1 (MEDIUM): the tracker released on a cache read | Addressed as suggested. The tracker reads `TritonEdgeClient.index_states()` once per pass, outside its lock; it treats only `READY` and `UNAVAILABLE` with a reason as terminal, and releases any other state that is not `LOADING` only after 120 s (`TRACK_UNCONFIRMED_S`), with an INFO line. D1 replaces the false claim with the mechanism and the residual: a job still queued after 120 s, and a second job that edgemlsdk enqueues after an index refresh, both end in a fast `UNAVAILABLE`. The Glossary, Property 3, the Invariants row and `test_load_tracker.py` follow. Line references were re-checked: `LoadModel` 136-193 (enqueue 187, entry 188-190), dedupe 174-184, `_markModelLoadFailed` 204-214 (`reason` 210), `TRITONSERVER_ServerLoadModel` 231, `ModelMetadata` 327, `ListModels` 356, `scheduling.h` 14 and 203-212. |
| 2 (MEDIUM): eight unlisted pins | Addressed. "Tests and baselines to update" now lists every pin by the change that needs it, with today's hash, including the six edgemlsdk baselines (with the delete-and-capture procedure), the `app.py` pin and its `note`, and the executor pin. Both suites are in Preservation checking. The fixed pre-commit grep is replaced by a basename grep and a pre-change-hash grep over the staged files. A sweep of the sha256 of every file this design edits found no other test pin; the only other hit is a historical record in another spec's tasks.md. While re-checking, the in-image native tests that publish inside the test tree were found to need a `locks` cleanup; that is listed too. |
| 3 (MEDIUM): interrupted triggered and manual runs stayed open | Option (A), with callbacks instead of execution ids. The executor registers `on_interrupt = partial(_mark_failed_best_effort, execution_id)` for triggered and manual executions, through `_await_model_readiness` → `ensure_model_ready` → `gate_wait`, and through `start_run`. Step 10 of the action calls them after the point of no return, with the backstop armed and a 10 s budget. Continuous runs stay with `reconcile_interrupted`, which also counts them; classic runs keep no row. The residual (an execution created in the last moments before the exit) is stated. bugfix.md 2.6 and Operator notes, D4, D7, the catalogue, Error Handling, Invariants, Property 15, `test_interrupted_runs.py` and on-device step 1 follow. **Superseded in part in iteration 3 (response 1):** runs now register at their start and carry `on_interrupt` on their ticket, which `gate_wait` marks as in its gate; `_await_model_readiness` and `ensure_model_ready` keep their signatures, and the residual is narrowed to a row created in the last milliseconds before the exit. |
| 4 (MEDIUM): run activity after the commit was undefined | Addressed as suggested: the IDLE / QUIESCING / RESTARTING table with its no-op rules, `abort_restart()` as the only way out of RESTARTING, `quiescing()` True in both, steps 6-8 inside one `try` that aborts and rewrites a written attempt as `not_performed`, and Property 8 rewritten, with the commit, failed write and abort test. Two additions close loops the review did not name. A write probe before quiescing means an unwritable record never holds runs. In-process hold-offs (30 min after an abort, 5 min after a deferral after quiescing) mean neither a persistent fault nor a busy device makes the watchdog quiesce every 30 s. After step 8 the restart is unconditional: the backstop is armed first (step 9), step 10 is contained, and a backstop that cannot start makes step 11 exit at once, so no exception can strand the backend in RESTARTING. |
| 5 (NIT): a stale request time | Addressed as suggested: `last_outside_loading(m)`, `first_loading(m)` and `since(m)` (D6 step 5), used for the duration, the display and E1. Property 6 and the unit case follow. |
| 6 (NIT): the vLLM state vocabulary | Addressed as suggested (D7 "Rule 6 in detail"): the feature-config mapping, so `STAGED` defers; a raising `list_models()` defers with a WARNING; `vllmModelsDropped` uses the same mapping. |
| 7 (NIT): the switch's timing and vocabulary | Addressed, reworded for the new default: opting out takes effect at the next action at the latest, opting back in and the reported state within 5 min, the file within one tick. `watchdog_not_running` is in D7's list, and one module-level reader on the shared IPC client replaces the per-read reader. |
| 8 (NIT): unplanned restarts in the on-device steps | Addressed, and adapted to the new default: every seeding, and every re-wedge that self-heal could act on, happens while the kill file exists; the kill file is created before the key is reset; the 20-minute deadline is stated; and step 6 clears the wedge before the kill file is deleted. Step 1 holds the only planned restart. |
| 9 (NIT): two inconsistent constants | Addressed as suggested: D4 uses `dda_triton.constants.TRITON_MODEL_DIR`, and `TritonEdgeClient` keeps `model_dir` and passes `lock_dir_for(self.model_dir)`. A test asserts the shared directory. |
| 10 (NIT): wall-clock aging of hidden directories | Addressed as suggested: names carry the boot stamp (`staging_prefix`, `trash_name`), the sweep ages them by `CLOCK_BOOTTIME`, treats another boot as stale, and uses mtime only for unstamped names, and the cleanup treats `ENOENT` on its own trash as done. As a side effect, the sweep's `names` filter compares the parsed name, so `model-yolo` no longer matches `model-yolo-test`; ModelGate's glob is unchanged (3.9). |
| 11 (NIT): the GPU part of the activity safeguard | Addressed with the first option, applied to the restart: D6 samples the Jetson devfreq GPU load node, D7 rule 7 defers on any non-zero load, and an unreadable node means no veto. D6 explains why GPU load does not veto the stall verdict. With self-heal on by default, this extra safeguard is worth its small cost. |
| 12 (NIT): the pb_stub capture | Addressed as suggested: up to 3 containers, newest first, 20 s in total; D6 states when `[pb_stub]` evidence exists. |
| 13 (NIT): the 8 KB claim | Addressed with the bound, made reactive. A size rejection sets a flag in the reporter; the watchdog then reads the reported document once, writes `null` for the legacy entries of models no longer in the index, and reports at once. The claim is replaced with an accurate statement, and bugfix.md's Out of Scope records this one exception to the legacy object's merge behavior. It uses the existing `GetThingShadow` authorization, so no recipe change is needed. |

## Review responses: design review, iteration 3 (2026-10-15)

Every line reference added in this revision was re-checked against the
worktree.

| Finding | Response |
|---|---|
| 1 (MEDIUM): runs in their pre-gate phase were invisible to `run_activity` | Addressed with the recommended fix, registration at entry; bugfix 2.6 is not narrowed. `PipelineExecutor.execute` registers directly after the pending check and the trigger-context read (`pipeline_executor.py:1547-1561`), before the registration lookup and every capture and preparation step; `set_details` adds the label and the models later. The classic handler registers right after `validate_workflow_requirements` (`endpoints/workflow.py:244`), before its gate and its image capture. Capture tasks, which the review did not name, register for their whole duration (`capture_task_manager.py:70`), because a restart would cut them off too. `gate_wait` marks the context's ticket as in its gate, and a gate-only ticket remains only for a caller without one. Every other ticket is unblocked and defers rule 8, the drain and the commit; the 900 s rule now counts time outside the gate. The second `start_run` after the gate is replaced by `after_gate()`, which refuses in RESTARTING, so a run that leaves its gate after the commit stops before its pipeline and never sets its row `running`, which would overwrite step 10's `failed`. Two choices the review left open: the ticket lives in a `contextvars.ContextVar` (per thread for the executor, per task for the `async def` handler and the asyncio capture tasks), and event-loop callers register with `hold=False`, because a hold there would stall every request for up to 30 s (**superseded in iteration 4, response 2:** the `hold` parameter is gone and no caller is ever held). `on_interrupt` moves onto the ticket, so `_await_model_readiness` and `ensure_model_ready` keep their signatures. The residual paragraph is replaced by an accurate account (D7, "After the restart"): every run that started before the commit was either blocked, then interrupted and marked failed, or made the commit defer; later runs are refused and fail their own row; only a row that a trigger creates in the last milliseconds before the exit can stay `pending`. Updated: bugfix.md 2.6 and Property 4; the Glossary, D4, D7, Error Handling, Invariants, the catalogue, Properties 8 and 15, Changes items 17, 20 and 21; `test_run_activity.py`, `test_interrupted_runs.py` and the new `test_run_registration_points.py`. |
| 2 (MEDIUM): the switch, the kill file and the vLLM rule were not re-checked at the point of no return | Addressed as suggested, and widened to the whole decision. Step 5 re-reads the kill file and the switch (one IPC read, bounded by 10 s), lists the vLLM models, takes one GPU burst, reads the index and one `/proc` sample, and re-runs `evaluate_wedge` and `decide_self_heal` with run activity left to `commit_restart`. Anything but `act` ends the attempt before the commit: `end_quiesce()`, the WARNING "SELF-HEAL deferred at commit", a 300 s hold-off after a `vllm` or `gpu` deferral, and no attempt recorded. `vllmModelsDropped` is built at step 6 from every model that the manager reports as `READY`, `LOADING` or `STAGED`, and refreshed once more just before the signal, so the record names everything the restart drops. The boundary is documented in D7 and in bugfix.md's Operator notes: the kill file stops an attempt up to its commit, at most about 75 s after the "quiescing" line. Property 7 and `test_self_heal_policy.py` gain the drain cases: the kill file, an opt-out, a `STAGED` vLLM model, a raising manager, GPU load, a model that recovered and a seeded record. |
| 3 (NIT): D9's swap rationale | Addressed with the suggested wording, plus the queued load's outcome: it reads the new entries when it starts, and a swap that races its start is bounded by F2 or by Triton 2.63.0. |
| 4 (NIT): the Startup bound and `--until` | Addressed. D9 gains a "Startup bound" table: seed wait 600 s, publish wait 300 s, ladder about 95 s, start budget about 520 s (counting each attempt's 10 s state read and the overrun of the ready wait's last read), record about 10 s and capture 20 s, about 1,550 s in total against `Timeout: 1800`. `--until` is dropped; D5 explains that `docker logs` without `--follow` already stops at the present. The test asserts no `--until`, `--follow` or `--tail`. |
| 5 (NIT): `ensure_model_ready` never retried after a 409 | Addressed as suggested: on `ModelReleaseInProgress`, `kicked` goes back to False and one INFO line is logged without a traceback. `ModelGate` had the same problem through `_load_requested` (`model_gate.py:383-384`): `_load` now returns False on that refusal, and `_maybe_load_unknown` discards the model, so the next check re-runs the file checks. The D1 sentence is corrected; D4, Error Handling, the catalogue, Properties 2 and 4 and `test_run_gate_stable_entries.py` follow. |
| 6 (NIT): exceptions inside a watchdog tick or a tracker pass | Addressed as suggested. Both loops are contained, with one ERROR line per streak. `ensure_tracker_thread()` also runs on every watchdog tick while anything is tracked. `last_completed_tick_mono` drives `WATCHDOG_STALE_S = 300`, after which readers report `disabledBy: "watchdog_not_running"`. `_list_models_raw(quiet=True)` keeps `index_states()` silent. The watchdog now reads `index_states()` as well, so `list_triton_models`' ERROR line is not repeated every 30 s during an index outage. Error Handling rows and tests are added. |
| 7 (NIT): `/feature-configurations` could not show a record for a model that edgemlsdk does not list | Addressed with the first option. `get_features_triton` appends one `UNKNOWN` `TritonModel` entry with `loadOutcome` for each record whose model edgemlsdk does not list and whose directory exists; the snapshot builder uses the same helper. The cited lines were re-checked (`triton_server.cpp:353-357`, `:507-520`). Property 16 and a surfaces test are added, and 2.7 needs no narrowing. |
| 8 (NIT): the 409 texts named only removal | Addressed: the tracker's INFO line, the 409 detail, the new gate lines, Property 2 and bugfix 2.2 and 3.4 all say "removed or replaced (component shutdown or update in progress)". |
| 9 (NIT): bugfix 3.4 listed only the 409 | Addressed with the suggested wording. |
| 10 (NIT): wording and consistency | All five addressed. D1 and Changes item 7 say that `__init__` adds `self.model_dir`. D2's step 4a always makes one HTTP read. D8 defines `gpuMaxPercentLastWindow`. The ERROR line says "to be marked failed". Iteration-1 row 6 is marked as superseded in part. |
| 11 (NIT): bugfix 2.11 expected timing-dependent evidence | Addressed with the suggested wording. |
| 12 (NIT): thread safety of the watchdog's shared state | Addressed as suggested. One immutable `WatchdogView` is published per tick, and per decision change inside an action, by one reference assignment, and every reader uses only that view. An Invariants row and a threaded reader test are added. |
| 13 (NIT): the cleanup exited 1 on any unexpected exception | Addressed as suggested. After argument validation, `__main__` catches any `Exception` from `release_model`, logs the ERROR line with the traceback, leaves the entries in place and exits 0. Argument errors still exit 1. |

## Review responses: design review, iteration 4 (2026-10-16)

Every line reference added in this revision was re-checked against the
worktree with `grep -n`. Self-heal stays on by default (Owner Decisions 3b).

| Finding | Response |
|---|---|
| 1 (MEDIUM): an exception or a failed read between `begin_quiesce()` and the commit left `run_activity` in QUIESCING | Addressed as suggested, with one deviation. (a) The quiescing line, the drain, the re-check and the commit run in one guarded block (D7 step 3, with its code). Its `finally` calls `end_quiesce()` unless the commit returned `"committed"`. An exception logs the ERROR "SELF-HEAL not performed: <step> failed before the commit" and holds this process off for 30 min (rule 5). A `begin_quiesce()` that returns False logs the WARNING, counts as an aborted attempt and stops. (b) The deviation: the re-check's read failures (`index_states()` raising, `/proc` not listable) decide `deferred` with a new `deferredBy: "recheck"` and the 300 s hold-off, not the suggested `idle`. The published `loadHealth` still shows the model stalled, so `selfHeal.state` must not say `idle`. D8 adds the value and its recovery text. (c) QUIESCING expires after `QUIESCE_MAX_S = 120` s on the monotonic clock; every `run_activity` call applies the expiry, with one WARNING. So that an expired quiesce is reported precisely, `commit_restart` returns `"committed"`, `"runs"` or `"not_quiescing"` instead of a bool, and `"not_quiescing"` defers as `recheck`. While bounding the block, the re-check's duration was corrected from about 12 s to about 25 s, because the IPC read can include a component-name discovery. The point of no return therefore comes at most about 85 s after the quiescing line, never more than 120 s; bugfix.md's Operator notes follow. Also updated: Error Handling (rows for `begin_quiesce()`, steps 3-5, the re-check reads, the expired commit and the expiry), the Invariants row, D6's containment note and action duration, Properties 7 and 8, bugfix.md 2.6 and Property 4, and the tests in `test_self_heal_policy.py` and `test_run_activity.py` (injected failures, expiry). |
| 2 (MEDIUM): the quiesce hold delayed an engine execution's frame capture | Addressed with the recommended fix: holds are removed. `start_run` has no `hold` parameter and never waits, and `QUIESCE_HOLD_S` is gone. A run that starts during a quiesce is accepted and counts as in flight, so the drain waits for it and the commit defers while it is unblocked. No owner decision is needed, because nothing holds a run any more. Updated: the Overview, the Glossary, the 3.5 bullet of "Expected Behavior and Preservation", D7 (a "No run is ever held" bullet with the reason, the state table, the API, every registration point, steps 2-5), the catalogue's quiescing and deferral lines ("the quiesce ends and continuous workflows resume" replaces "held runs resume"), a new Invariants row, Properties 8 and 13, Changes items 6, 20 and 21, `test_run_activity.py` (a no-hold case replaces the hold cases, plus a static check that no caller passes `hold`), `test_interrupted_runs.py` (an execution reaches its frame feed without waiting), and bugfix.md 2.6, 3.5 and Property 4. |
| 3 (MEDIUM): DIO-triggered runs registered only after their frame grab | Addressed with the recommended fix. The trigger registers before `_get_image_source` with the new `bind=False`. In RESTARTING it logs the refusal and returns before the frame grab, and it finishes the ticket if the read, the grab or the submit raises. `execute_workflow` runs inside `run_activity.bound(ticket)` and finishes the ticket in an outer `finally`, so the nested `run_pipeline` registration is a no-op. The API gains `bind` and `bound`; `finish_run` of an unbound ticket only unregisters it. Two details the review left open: the registration sits inside the branch that runs the workflow (`:255`), so the "Pipeline is full" branch needs no ticket; and the ticket carries the configured `modelName`, as a classic run's does, so that the 900 s rule can count a DIO run, which has no gate, as blocked. Checking the other `run_pipeline` callers found that the operator's image-source previews, captures and connection tests (`endpoints/image_source.py`) also register after their own frame grab. They keep no row and produce no inspection result, so D7 states them as the one residual instead of moving their registration. Added: the D7 "Digital-input triggers" bullet, Changes item 22 (later items renumbered), two Error Handling rows, the Glossary entry, D7 "After the restart", the Invariants row, Property 8, `test_dio_run_registration.py` (in the flask-app image), and bugfix.md 2.6 and Property 4. |
| 4 (NIT): the swaps after the 300 s timeout run without the lock | Addressed with the suggested wording in D9 step 6, Property 9, the Invariants row, the Overview and bugfix.md 2.8. |
| 5 (NIT): cold-model 2.8 claimed as satisfied | Option (a): bugfix.md's row, D8 and D12's cross-reference say "partially satisfied on the device side" and name the converting-in-progress state as open. A `PUBLISHING` state was not added: it would add a repository inspection per model to every snapshot, and it would flicker on every unchanged republish, which stages and discards in under a second. |
| 6 (NIT): `notReady` could list a READY model | Addressed as suggested (D8): "plus those whose status is not `READY` and that have a load outcome record". |
| 7 (NIT): bugfix.md 2.3 promised wall-clock-proof ages | Addressed as suggested in bugfix.md 2.3 and the Invariants row. |
| 8 (NIT): a failing switch read was retried every tick | Addressed. The periodic refresh retries no sooner than `SWITCH_RETRY_AFTER_S = 60` after a failure; the reads at an attempt's start and before its commit stay. Error Handling states that `get_component_config` logs its own ERROR line per failed read (`defect_detection_config.py:77-88`). A test counts the IPC calls. |
| 9 (NIT): the per-run gate waits 600 s on a permanently incomplete entry | The change is stated in D4 and bugfix.md 2.3. When the blocker is a missing or unreadable path, the timeout message adds the remedy: restart or redeploy the model component. The optional fast-fail load request is not adopted. Requirement 2.3 allows the per-run gate to request a load only for a complete, stable entry, and the continuous ModelGate behaves the same way. Telling a permanently incomplete entry from one being replaced or republished at that moment would also need a staging check, a persistence window and an age rule, for a case that requires an interrupted removal or a broken component. |
| 10 (NIT): the F6 summary had no time budget | Addressed, with one correction. Each summary creates one `iot-data` client with `connect_timeout=2`, `read_timeout=2` and `retries={"total_max_attempts": 1}`. The suggested `max_attempts: 1` would still allow one retry (botocore's `Config` documentation), so `total_max_attempts` is used. A 5 s total budget sets `truncated: true`. `get_usecase_client` cannot pass a client configuration today, so it and `create_boto3_client` gain an optional `config=None` keyword (`shared_utils.py:1530`, `:1633`), which leaves every existing call unchanged. Changes item 25, two Error Handling rows and two Portal tests follow. |
| 11 (NIT): line references off by one or two | Three of the five corrected: `triton_edge_client.py:62-68`, `model_convertor.py:61-64`, and the heredocs at `gdk-component-build-and-publish.sh:448-493`, `portal-build.sh:455-500` and `publish-ecr-only.sh:114-168`. The other two were already right and stay: `TIME_OUT = 10` is at `defect_detection_config.py:40`, and `TRITON_MODEL_DIR` at `dda_triton/constants.py:44`, both re-checked with `grep -n` and `awk`. |

## Review responses: design review, iteration 5 (2026-10-16)

The review approved the design with 0 HIGH, 0 MEDIUM and 9 NIT findings.
Self-heal stays on by default (Owner Decisions 3b). All nine are addressed in
this revision; none changes the behavior the owner approved.

| Finding | Response |
|---|---|
| 1 (NIT): the cleanup had no trash name when `repo_entries` cannot be imported | Addressed as suggested. D2 adds the local `_trash_name` helper, which falls back to the unstamped `.trash-<dir>-<pid>-<time_ns>` that the sweep ages by mtime; step 4d uses it. The Imports sentence and the Error Handling row say that a failed `repo_entries` import skips the sweep and uses the unstamped trash name. The lock-unusable fallback defines terminal as neither `LOADING` nor `UNLOADING`. `test_cleanup_release.py` adds the `SyntaxError` import case. |
| 2 (NIT): D1 understated where untracked loads come from | Addressed as suggested. D1 names digital-input runs, which pass no readiness gate, next to a gate that failed open, and says that the first digital-input trigger on each `UNKNOWN` model after any backend restart enqueues one. D9 step 5 adds the sentence on a changed publish swapping under an untracked load. No code change. |
| 3 (NIT): refused tickets were not covered by the `run_activity` API rules | Addressed as suggested. D7 "API" states that a refused ticket is neither registered nor bound, that `start_run` sets no context variable for it, and that `finish_run`, `bound` and `set_details` treat it as a no-op. `test_run_activity.py` adds the RESTARTING case and the registration after `abort_restart()`. |
| 4 (NIT): bugfix.md promised that every cut-off run is marked failed | Addressed as suggested. bugfix.md 2.6 and Property 4 add the 10 s budget and the WARNING for a skipped run, which can stay `pending` or `running`; the Operator notes and this design's Invariants row say the same. |
| 5 (NIT): digital-input workflows after a self-heal restart | Addressed with the suggested sentence, in D7 "After the restart" and in bugfix.md's Operator notes (self-heal section). |
| 6 (NIT): D4's reason for not adopting `UNKNOWN_LOAD_GRACE_S` | Addressed: the paragraph is replaced with the suggested text. The decision stands. |
| 7 (NIT): two exported names were not defined | Addressed. D8 defines `self_heal_document(view, records_dir=None) -> dict` as the builder of the `selfHeal` object, which `build_model_status_snapshot` calls; Changes item 5 names its signature. D7 step 10 defines `InterruptReport(marked: List[str], failed: List[str], skipped: int)`; its INFO and WARNING lines read it. One deviation: the attempt's `runsInterrupted` is written in step 7, before step 10 runs, so it is built from the same registered tickets and shares their run ids with the report instead of being read from it. Rewriting the record after the callbacks would add a write after the point of no return for no operator benefit. |
| 8 (NIT): the design cited documents outside the repository | Addressed. Iteration-4 response 9 says "Requirement 2.3 allows ...", and On-Device Verification says "the owner" and "the owner's go" in all four places. |
| 9 (NIT): record size limits were enforced only on read | Addressed as suggested. D7 steps 6 and 7 cap an attempt's `models` at 10 and `evidence` at 5, and after appending drop attempts older than 24 h until the record is at most 48 KiB; D5 cuts a load outcome's `reason` and `failureReason` to 2,000 characters. Input Validation states the write caps. `test_self_heal_policy.py` and `test_startup_outcome_record.py` each add a test that the largest record the writer can produce passes the reader's validation. `vllmModelsDropped` is not cut, so that the record keeps naming every vLLM model the restart drops; the test bounds it at 20 models per attempt, and rule 4 bounds the attempts under 24 h at 3. |
