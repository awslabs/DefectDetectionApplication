# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The Continuous_Runner's Triton model gate (finding 16).

Feature: rtsp-rtmp-stream-cameras (Requirements 11.1, 11.11, 11.12, 16.2;
design component 14, "Model gate"). Everything here runs off-device: the
Triton model repository is a temporary directory, Triton itself is a fake
recording every ``list_triton_models`` / ``start_triton_model`` call, and
the runner is driven through ``step()`` with a fake clock, so the MIC-730
incident is reproduced exactly:

- the model components rewrite the repository ~26 s after the backend
  starts, so a resumed continuous workflow's first runs asked Triton to
  load a half-written model and its state then stayed ``LOADING`` for 19
  minutes. The gate must start no run, insert no row, and request no load
  while the files are incomplete or still changing;
- once the files are complete and stable it requests exactly ONE load, and
  resumes at the first tick after the model reports ``READY``.

Also covered: an unresolved model name, the 120 s grace for a repository
older than the engine's start, the UNAVAILABLE backoff, the stall report,
fail-open, a document without ``emltriton`` (no gate and no Triton call at
all), a runner restart when a model name changes, and the state precedence
of the Continuous status.
"""
import logging
import os

import pytest

from test_workflow_continuous_runner import (
    Clock,
    FakeExecutor,
    FakeStream,
    MemoryStore,
    make_feed,
)
from workflow_engine_test_utils import make_session_factory

from workflow_engine import model_gate as model_gate_module
from workflow_engine.continuous_runner import (
    MODEL_POLL_S,
    STATE_PAUSED,
    STATE_RUNNING,
    STATE_WAITING,
    STATE_WAITING_MODEL,
    ContinuousRunner,
    ContinuousRunnerManager,
    document_model_names,
    runner_fingerprint,
)
from workflow_engine.model_gate import (
    QUIET_PERIOD_S,
    STALL_AFTER_S,
    STATE_INCOMPLETE,
    STATE_LOADING,
    STATE_NOT_DEPLOYED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    ModelGate,
    ModelWait,
)

#: A fixed wall clock, so file ages are exact.
WALL = 1_790_000_000.0
#: The deployed name of the model the MIC-730 workflows use; the compiled
#: document names it ``yolo-test``, which resolution maps onto this.
DEPLOYED = "model-yolo-test-jetson-xavier-jp5"

_ENSEMBLE_CONFIG = """name: "@NAME@"
platform: "ensemble"
max_batch_size: 0
input [ { name: "input", data_type: TYPE_UINT8, dims: [ -1, -1, 3 ] } ]
input [ { name: "METADATA", data_type: TYPE_UINT8, dims: [ -1 ] } ]
output [ { name: "output_overlay", data_type: TYPE_UINT8, dims: [ -1 ] } ]
ensemble_scheduling {
  step [
    {
      model_name: "base_@NAME@"
      model_version: -1
    },
    {
      model_name: "marshal_@NAME@"
      model_version: -1
    }
  ]
}
"""

_PYTHON_CONFIG = """name: "@NAME@"
backend: "python"
max_batch_size: 0
"""


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def write_repo(repo, name=DEPLOYED, *, base_model_py=True, marshal_model_py=True,
               staging=False):
    """A Triton model repository holding one DDA ensemble.

    ``base_model_py=False`` is the MIC-730 mid-rewrite state: the base
    model's ``config.pbtxt`` is published but its python-backend
    ``1/model.py`` is not there yet.
    """
    repo = str(repo)
    _write(os.path.join(repo, name, "config.pbtxt"), _ENSEMBLE_CONFIG.replace("@NAME@", name))
    _write(os.path.join(repo, name, "1", "ensemble_model"), "")
    for step, has_model_py in (("base_" + name, base_model_py),
                               ("marshal_" + name, marshal_model_py)):
        _write(os.path.join(repo, step, "config.pbtxt"), _PYTHON_CONFIG.replace("@NAME@", step))
        os.makedirs(os.path.join(repo, step, "1"), exist_ok=True)
        if has_model_py:
            _write(os.path.join(repo, step, "1", "model.py"), "class TritonPythonModel: pass\n")
    if staging:
        os.makedirs(os.path.join(repo, ".staging-{0}-abc123".format(name)), exist_ok=True)
    return repo


def touch_all(repo, at):
    """Age every path in the repository to ``at`` (directories last, so
    writing a file cannot re-stamp its parent)."""
    for root, dirs, files in os.walk(str(repo), topdown=False):
        for name in files:
            os.utime(os.path.join(root, name), (at, at))
        for name in dirs:
            os.utime(os.path.join(root, name), (at, at))
        os.utime(root, (at, at))


class FakeTriton:
    """The two calls the gate makes, recorded.

    ``load_state`` is the state a requested load moves a model to; None
    leaves the state alone (an UNAVAILABLE model that keeps failing).
    """

    def __init__(self, states=None, reasons=None, load_state=STATE_LOADING, raises=None):
        self.states = dict(states or {})
        self.reasons = dict(reasons or {})
        self.load_state = load_state
        self.raises = raises
        self.loads = []
        self.lists = 0
        self.quiet = []

    def list_triton_models(self, quiet=False):
        self.lists += 1
        self.quiet.append(quiet)
        if self.raises is not None:
            raise self.raises
        records = []
        for name, state in self.states.items():
            record = {"model_component": name, "status": state}
            if name in self.reasons:
                record["reason"] = self.reasons[name]
            records.append(record)
        return records

    def start_triton_model(self, model_id):
        self.loads.append(model_id)
        if self.load_state is not None:
            self.states[model_id] = self.load_state
        return self.load_state


class WallClock:
    """A wall clock the tests move by hand, in step with the monotonic one."""

    def __init__(self, clock, start=WALL):
        self._clock = clock
        self._base = start - clock.now

    def __call__(self):
        return self._base + self._clock.now


def make_gate(repo, client, clock=None, wall=None, models=("yolo-test",),
              engine_started_at=WALL - 3600.0, repo_has_models=True):
    clock = clock or Clock()
    wall = wall or WallClock(clock)
    gate = ModelGate(models, repo=str(repo), client_provider=lambda: client,
                     repo_has_models=lambda _repo: repo_has_models,
                     clock=clock, wall=wall, engine_started_at=engine_started_at)
    return gate, clock, wall


def make_runner(gate=None, clock=None, wall=None, stream=None, store=None, executor=None,
                models=(), **kwargs):
    clock = clock or Clock()
    wall = wall or WallClock(clock)
    store = store or MemoryStore()
    stream = stream or FakeStream()
    runner = ContinuousRunner("wf-1:3", make_feed(1.0), execute=executor or FakeExecutor(clock),
                              stream_manager=stream, store=store, clock=clock, wall=wall,
                              models=models, gate=gate, **kwargs)
    return runner, clock, store, stream


def warnings_of(caplog, logger_name):
    return [record for record in caplog.records
            if record.name == logger_name and record.levelno == logging.WARNING]


def infos_of(caplog, logger_name):
    return [record for record in caplog.records
            if record.name == logger_name and record.levelno == logging.INFO]


# --- the MIC-730 sequence ---------------------------------------------------------


class TestTheMic730Sequence:
    """11.11, 11.12: what actually happened on the MIC-730, step by step."""

    def test_a_mid_rewrite_repository_starts_no_run_and_requests_no_load(self, tmp_path, caplog):
        caplog.set_level(logging.DEBUG)
        repo = write_repo(tmp_path / "repo", base_model_py=False)
        client = FakeTriton()
        clock = Clock()
        wall = WallClock(clock)
        touch_all(repo, wall() - 30.0)
        gate, _clock, _wall = make_gate(repo, client, clock=clock, wall=wall)
        runner, _clock, store, _stream = make_runner(gate=gate, clock=clock, wall=wall,
                                                     models=("yolo-test",))

        delay = runner.step()

        assert delay == MODEL_POLL_S
        assert store.count == 0 and runner.counters()["started"] == 0
        assert client.loads == []
        status = runner.status()
        assert status["state"] == STATE_WAITING_MODEL
        readiness = status["modelReadiness"]
        assert readiness["model"] == "yolo-test" and readiness["tritonModel"] == DEPLOYED
        assert readiness["state"] == STATE_INCOMPLETE
        assert readiness["reason"].endswith(os.path.join("base_" + DEPLOYED, "1", "model.py")
                                           + " is missing")
        assert readiness["stalled"] is False
        assert status["counters"]["modelUnavailable"] == 1
        # 12.6: the polled listing is quiet.
        assert client.quiet in ([], [True])

    def test_files_that_are_complete_but_still_changing_request_no_load(self, tmp_path):
        """11.12: the 10 s quiet period. The model components publish base,
        marshal and ensemble as three separate renames, so a complete-looking
        repository can still be mid-rewrite."""
        repo = write_repo(tmp_path / "repo")
        client = FakeTriton()
        gate, clock, wall = make_gate(repo, client)
        touch_all(repo, wall() - (QUIET_PERIOD_S / 2.0))

        wait = gate.check()

        assert wait is not None and wait.state == STATE_INCOMPLETE
        assert "changed" in wait.reason
        assert client.loads == [] and client.lists == 0

    def test_once_the_files_are_stable_exactly_one_load_is_requested(self, tmp_path):
        """11.12: UNKNOWN is the state a restarted backend sees, and a pure
        wait never converges — so one load is kicked, and only one."""
        repo = write_repo(tmp_path / "repo")
        client = FakeTriton(load_state=None)
        gate, clock, wall = make_gate(repo, client)
        touch_all(repo, wall() - 30.0)

        for _ in range(5):
            wait = gate.check()
            clock.advance(MODEL_POLL_S)
            assert wait is not None and wait.state == STATE_UNKNOWN

        assert client.loads == [DEPLOYED]

    def test_the_runner_waits_while_loading_and_resumes_at_ready(self, tmp_path, caplog):
        """11.1, 11.11: no run, no row and one event while the model loads;
        the first tick after READY runs, with one WARNING and one INFO."""
        caplog.set_level(logging.DEBUG)
        repo = write_repo(tmp_path / "repo", base_model_py=False)
        client = FakeTriton()
        clock = Clock()
        wall = WallClock(clock)
        touch_all(repo, wall() - 30.0)
        gate, _clock, _wall = make_gate(repo, client, clock=clock, wall=wall)
        runner, _clock, store, _stream = make_runner(gate=gate, clock=clock, wall=wall,
                                                     models=("yolo-test",))

        # 1. Mid-rewrite: nothing runs.
        runner.step()
        assert store.count == 0

        # 2. The rewrite finishes; the fresh files are not yet stable.
        write_repo(repo)
        touch_all(repo, wall())
        clock.advance(MODEL_POLL_S)
        runner.step()
        assert client.loads == [] and store.count == 0
        assert runner.status()["modelReadiness"]["state"] == STATE_INCOMPLETE

        # 3. Stable: exactly one load, and the runner waits while LOADING.
        clock.advance(QUIET_PERIOD_S + 1.0)
        runner.step()
        assert client.loads == [DEPLOYED]
        # The wait reports the state the load was requested from; the next
        # poll sees Triton's answer to it.
        assert runner.status()["modelReadiness"]["state"] == STATE_UNKNOWN
        clock.advance(MODEL_POLL_S)
        runner.step()
        assert runner.status()["modelReadiness"]["state"] == STATE_LOADING
        for _ in range(3):
            clock.advance(MODEL_POLL_S)
            assert runner.step() == MODEL_POLL_S
        assert store.count == 0 and client.loads == [DEPLOYED]

        # 4. READY: runs resume at the first tick.
        client.states[DEPLOYED] = "READY"
        clock.advance(MODEL_POLL_S)
        runner.step()

        assert store.count == 1
        status = runner.status()
        assert status["state"] == STATE_RUNNING and status["modelReadiness"] is None
        assert status["counters"]["modelUnavailable"] == 1
        runner_log = "workflow_engine.continuous_runner"
        assert len(warnings_of(caplog, runner_log)) == 1
        resumed = [record for record in infos_of(caplog, runner_log)
                   if "READY; resuming" in record.getMessage()]
        assert len(resumed) == 1


# --- the gate on its own ----------------------------------------------------------


class TestModelGate:
    def test_an_unresolved_name_is_not_deployed_and_no_load_is_requested(self, tmp_path):
        repo = write_repo(tmp_path / "repo")
        client = FakeTriton()
        gate, _clock, wall = make_gate(repo, client, models=("cookies-binary",))
        touch_all(repo, wall() - 30.0)

        wait = gate.check()

        assert wait is not None
        assert (wait.model, wait.triton_model, wait.state) == (
            "cookies-binary", "cookies-binary", STATE_NOT_DEPLOYED)
        assert client.loads == [] and client.lists == 0

    def test_an_empty_repository_is_not_deployed_and_creates_no_client(self, tmp_path):
        """11.12: creating the Triton client against an empty repository
        hangs, so it must not be created at all."""
        repo = write_repo(tmp_path / "repo")

        def explode():
            raise AssertionError("the Triton client must not be created")

        gate = ModelGate(("yolo-test",), repo=str(repo), client_provider=explode,
                         repo_has_models=lambda _repo: False, clock=Clock(),
                         wall=lambda: WALL, engine_started_at=WALL - 3600.0)

        wait = gate.check()

        assert wait is not None and wait.state == STATE_NOT_DEPLOYED

    def test_a_staging_sibling_is_incomplete(self, tmp_path):
        repo = write_repo(tmp_path / "repo", staging=True)
        client = FakeTriton()
        gate, _clock, wall = make_gate(repo, client)
        touch_all(repo, wall() - 30.0)

        wait = gate.check()

        assert wait is not None and wait.state == STATE_INCOMPLETE
        assert ".staging-" in wait.reason and "being published" in wait.reason
        assert client.lists == 0

    def test_files_older_than_the_engine_start_wait_120_s_for_their_load(self, tmp_path):
        """11.12: after a LocalServer deployment the model components rewrite
        the repository 26-31 s in, so a load requested against the
        about-to-be-replaced directory is the load that wedges."""
        repo = write_repo(tmp_path / "repo")
        client = FakeTriton(load_state=None)
        clock = Clock()
        wall = WallClock(clock)
        engine_started_at = wall() - 5.0
        touch_all(repo, wall() - 30.0)  # older than the engine's start
        gate, _clock, _wall = make_gate(repo, client, clock=clock, wall=wall,
                                        engine_started_at=engine_started_at)

        wait = gate.check()
        assert wait is not None and wait.state == STATE_UNKNOWN
        assert client.loads == []

        clock.advance(model_gate_module.UNKNOWN_LOAD_GRACE_S)

        assert gate.check().state == STATE_UNKNOWN
        assert client.loads == [DEPLOYED]

    def test_a_repository_newer_than_the_engine_start_loads_at_once(self, tmp_path):
        repo = write_repo(tmp_path / "repo")
        client = FakeTriton(load_state=None)
        clock = Clock()
        wall = WallClock(clock)
        touch_all(repo, wall() - 30.0)
        gate, _clock, _wall = make_gate(repo, client, clock=clock, wall=wall,
                                        engine_started_at=wall() - 60.0)

        assert gate.check().state == STATE_UNKNOWN
        assert client.loads == [DEPLOYED]

    def test_an_unavailable_model_is_retried_with_a_backoff_carrying_the_reason(self, tmp_path):
        """11.12: 15, 30, 60, 120 s, then every 300 s."""
        repo = write_repo(tmp_path / "repo")
        client = FakeTriton(states={DEPLOYED: STATE_UNAVAILABLE},
                            reasons={DEPLOYED: "failed to load model"}, load_state=None)
        gate, clock, wall = make_gate(repo, client)
        touch_all(repo, wall() - 30.0)

        wait = gate.check()
        assert wait.state == STATE_UNAVAILABLE and wait.reason == "failed to load model"
        assert client.loads == []

        seen = []
        for elapsed in (14.0, 15.0, 29.0, 30.0, 60.0, 120.0, 419.0, 420.0, 720.0):
            clock.now = 100.0 + elapsed
            gate.check()
            seen.append((elapsed, len(client.loads)))

        assert seen == [(14.0, 0), (15.0, 1), (29.0, 1), (30.0, 2), (60.0, 3),
                        (120.0, 4), (419.0, 4), (420.0, 5), (720.0, 6)]

    def test_a_wait_is_stalled_after_the_stall_budget(self, tmp_path):
        repo = write_repo(tmp_path / "repo")
        client = FakeTriton(states={DEPLOYED: STATE_LOADING})
        gate, clock, wall = make_gate(repo, client)
        touch_all(repo, wall() - 30.0)

        assert gate.check().stalled is False
        since_ms = gate.check().since_ms
        clock.advance(STALL_AFTER_S)
        assert gate.check().stalled is False
        clock.advance(1.0)

        wait = gate.check()
        assert wait.stalled is True
        # The wait's start never moves while it lasts.
        assert wait.since_ms == since_ms

    def test_a_ready_model_ends_the_wait(self, tmp_path):
        repo = write_repo(tmp_path / "repo")
        client = FakeTriton(states={DEPLOYED: STATE_LOADING})
        gate, clock, wall = make_gate(repo, client)
        touch_all(repo, wall() - 30.0)
        assert gate.check() is not None

        client.states[DEPLOYED] = "READY"

        assert gate.check() is None

    def test_state_is_read_fresh_once_per_check_for_every_model(self, tmp_path):
        """11.12: ListModels refreshes edgemlsdk's cache (get_model_status
        returns only the cache, which stayed LOADING for 19 minutes), and one
        listing serves every model of the document."""
        repo = write_repo(tmp_path / "repo")
        write_repo(repo, "model-second-jetson-xavier-jp5")
        client = FakeTriton(states={DEPLOYED: "READY",
                                    "model-second-jetson-xavier-jp5": STATE_LOADING})
        gate, _clock, wall = make_gate(repo, client, models=("yolo-test", "second"))
        touch_all(repo, wall() - 30.0)

        wait = gate.check()

        assert wait is not None and wait.model == "second"
        assert client.lists == 1
        assert client.quiet == [True]

    def test_an_unreadable_triton_fails_open_with_one_warning(self, tmp_path, caplog):
        """11.12: the gate must never be the sole reason a working workflow
        stops running; the executor's per-run gate still applies."""
        caplog.set_level(logging.DEBUG)
        repo = write_repo(tmp_path / "repo")
        client = FakeTriton(raises=RuntimeError("triton is not answering"))
        gate, _clock, wall = make_gate(repo, client)
        touch_all(repo, wall() - 30.0)

        assert gate.check() is None
        assert gate.check() is None

        assert len(warnings_of(caplog, "workflow_engine.model_gate")) == 1

    def test_no_model_means_no_call_at_all(self, tmp_path):
        def explode():
            raise AssertionError("Triton must not be consulted")

        gate = ModelGate((), repo=str(tmp_path), client_provider=explode,
                         repo_has_models=explode)

        assert gate.check() is None
        assert gate.models == ()


# --- the runner and the manager ---------------------------------------------------


class StubGate:
    def __init__(self, wait=None):
        self.wait = wait
        self.checks = 0

    def check(self):
        self.checks += 1
        return self.wait


def a_wait(state=STATE_LOADING, stalled=False):
    return ModelWait(model="yolo-test", triton_model=DEPLOYED, state=state,
                     reason="loading the engine", since_ms=int(WALL * 1000), stalled=stalled)


class TestRunnerGating:
    def test_a_document_without_emltriton_gets_no_gate_and_makes_no_triton_call(
            self, tmp_path, monkeypatch):
        def explode():
            raise AssertionError("Triton must not be consulted")

        monkeypatch.setattr(model_gate_module, "_default_client", explode)
        runner, clock, store, _stream = make_runner()

        assert runner._gate is None
        for _ in range(3):
            clock.advance(runner.step() or 0.0)

        assert store.count >= 2
        assert runner.status()["modelReadiness"] is None
        assert runner.counters()["modelUnavailable"] == 0

    def test_the_gate_is_polled_at_most_every_3_s_while_waiting(self):
        gate = StubGate(a_wait())
        runner, clock, store, _stream = make_runner(gate=gate, models=("yolo-test",))

        runner.step()
        clock.advance(1.0)
        runner.step()
        clock.advance(MODEL_POLL_S)
        runner.step()

        assert gate.checks == 2
        assert store.count == 0

    def test_a_ready_gate_is_not_polled_again_until_a_run_does_not_complete(self):
        gate = StubGate(None)
        store = MemoryStore(status="completed")
        runner, clock, _store, _stream = make_runner(gate=gate, models=("yolo-test",),
                                                     store=store)

        for _ in range(4):
            clock.advance(runner.step() or 0.0)
        assert gate.checks == 1

        store.default_status = "failed"
        clock.advance(runner.step() or 0.0)
        clock.advance(MODEL_POLL_S)
        runner.step()

        assert gate.checks == 2

    def test_a_stalled_wait_is_warned_about_every_300_s_and_never_per_poll(self, caplog):
        caplog.set_level(logging.DEBUG)
        gate = StubGate(a_wait())
        runner, clock, store, _stream = make_runner(gate=gate, models=("yolo-test",))
        log = "workflow_engine.continuous_runner"

        for _ in range(5):
            runner.step()
            clock.advance(MODEL_POLL_S)
        assert len(warnings_of(caplog, log)) == 1

        gate.wait = a_wait(stalled=True)
        runner.step()
        assert len(warnings_of(caplog, log)) == 2
        clock.advance(100.0)
        runner.step()
        assert len(warnings_of(caplog, log)) == 2
        clock.advance(STALL_AFTER_S)
        runner.step()
        assert len(warnings_of(caplog, log)) == 3
        assert store.count == 0

    def test_a_raising_gate_fails_open(self, caplog):
        caplog.set_level(logging.DEBUG)

        class Boom:
            def check(self):
                raise RuntimeError("gate exploded")

        runner, clock, store, _stream = make_runner(gate=Boom(), models=("yolo-test",))

        clock.advance(runner.step() or 0.0)

        assert store.count == 1
        assert runner.status()["state"] == STATE_RUNNING
        assert len(warnings_of(caplog, "workflow_engine.continuous_runner")) == 1

    def test_the_state_precedence_is_paused_stream_model_running(self):
        """16.2: paused > waiting_for_stream > waiting_for_model > running."""
        gate = StubGate(a_wait())
        stream = FakeStream()
        runner, clock, _store, _stream = make_runner(gate=gate, models=("yolo-test",),
                                                     stream=stream)

        runner.step()
        status = runner.status()
        assert status["state"] == STATE_WAITING_MODEL
        assert status["modelReadiness"] == {
            "model": "yolo-test", "tritonModel": DEPLOYED, "state": STATE_LOADING,
            "reason": "loading the engine", "sinceMs": int(WALL * 1000), "stalled": False}

        stream.state = "reconnecting"
        assert runner.status()["state"] == STATE_WAITING
        runner.pause()
        assert runner.status()["state"] == STATE_PAUSED

        runner.resume()
        stream.state = "streaming"
        gate.wait = None
        clock.advance(MODEL_POLL_S)
        runner.step()
        status = runner.status()
        assert status["state"] == STATE_RUNNING and status["modelReadiness"] is None

    def test_stored_counters_without_the_new_key_are_accepted(self):
        runner, _clock, _store, _stream = make_runner(
            counters={"started": 4, "completed": 4})

        counters = runner.counters()

        assert counters["started"] == 4 and counters["modelUnavailable"] == 0


class TestModelNames:
    def test_the_model_names_are_the_distinct_emltriton_models(self):
        document = {"segments": [
            {"elements": [
                {"nodeId": "a", "factory": "appsrc", "args": {}},
                {"nodeId": "b", "factory": "emltriton", "args": {"model": "yolo-test"}},
                {"nodeId": "c", "factory": "emltriton", "args": {"model": "yolo-test"}},
            ]},
            {"elements": [{"nodeId": "d", "factory": "emltriton", "args": {"model": "second"}}]},
        ]}

        assert document_model_names(document) == ("yolo-test", "second")
        assert document_model_names({"segments": [{"elements": []}]}) == ()
        assert document_model_names(None) == ()

    def test_the_fingerprint_carries_the_models_and_still_takes_two_arguments(self):
        feed = make_feed(1.0)

        assert runner_fingerprint(feed, ("out1",)) == runner_fingerprint(feed, ("out1",), ())
        assert runner_fingerprint(feed, ("out1",), ("a",)) != runner_fingerprint(
            feed, ("out1",), ("b",))

    def test_a_changed_model_name_restarts_the_runner(self):
        session_factory = make_session_factory()
        clock = Clock()
        manager = ContinuousRunnerManager(
            session_factory=session_factory, execute_provider=lambda: FakeExecutor(clock),
            manager_provider=lambda: FakeStream(), stream_camera_resolver=lambda session: {},
            start_threads=False, clock=clock, wall=WallClock(clock))
        feed = make_feed(1.0)
        models = ["yolo-test"]
        manager._desired = lambda: ({"wf-1:3": (feed, ("out1",), tuple(models))}, [])

        manager.on_registrations_changed()
        first = manager.runner("wf-1:3")
        assert first is not None and first.models == ("yolo-test",)

        manager.on_registrations_changed()
        assert manager.runner("wf-1:3") is first

        models[0] = "cookies-binary"
        manager.on_registrations_changed()
        restarted = manager.runner("wf-1:3")

        assert restarted is not first
        assert restarted.models == ("cookies-binary",)
        assert first.stopped is True

    def test_the_manager_passes_its_construction_wall_time_as_the_engine_start(self):
        session_factory = make_session_factory()
        clock = Clock()
        wall = WallClock(clock)
        manager = ContinuousRunnerManager(
            session_factory=session_factory, execute_provider=lambda: FakeExecutor(clock),
            manager_provider=lambda: FakeStream(), stream_camera_resolver=lambda session: {},
            start_threads=False, clock=clock, wall=wall)
        engine_started_at = wall()
        clock.advance(30.0)
        manager._desired = lambda: ({"wf-1:3": (make_feed(1.0), (), ("yolo-test",))}, [])

        manager.on_registrations_changed()
        gate = manager.runner("wf-1:3")._gate

        assert isinstance(gate, ModelGate)
        assert gate._engine_started_at == pytest.approx(engine_started_at)
