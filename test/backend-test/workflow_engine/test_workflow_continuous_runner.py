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
"""Continuous_Runner, its manager, and the continuous API.

Feature: rtsp-rtmp-stream-cameras (Requirements 11.1-11.10, 12.5, 12.8,
18.4). The manager runs against the real WorkflowWatcher and sqlite
(the ``workflow_continuous_state`` table included); the runners are
driven through ``step()`` with a fake clock, a fake stream manager and a
fake executor, except where a real thread is the point:

- a pause persists across a restart (a new manager) until resumed;
- superseding a registration stops its runner after the in-flight run and
  deletes its state row;
- a manual trigger is accepted only while the workflow is paused;
- the run history of non-continuous workflows is untouched;
- the trigger runtime and manual trigger behave exactly as before for
  workflows without a continuous stream node.
"""
import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_workflow_binding_store import get_row
from test_workflow_stream_executor import make_stream_document
from workflow_engine_test_utils import (
    VALID_MANIFEST,
    make_session_factory,
    make_watcher,
    write_artifact_set,
)

from dda_logging.run_context import CONTINUOUS_RUN
from workflow_engine import api as workflow_engine_api
from workflow_engine import executor as executor_module
from workflow_engine import gst_plugins, runtime
from workflow_engine.continuous_runner import (
    STATE_PAUSED,
    STATE_RUNNING,
    STATE_WAITING,
    ContinuousRunner,
    ContinuousRunnerManager,
    ExecutionStore,
)
from workflow_engine.models import WorkflowContinuousState, WorkflowExecution
from workflow_engine.run_retention import RunOutcome, RunRetention
from workflow_engine.stream_feed import FrameHandoff, StreamFeed


class Clock:
    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Frame:
    def __init__(self, seq, acquired_at_ms=1_790_000_000_000):
        self.seq = seq
        self.acquired_at_ms = acquired_at_ms
        self.data = b"\x00" * 24
        self.width, self.height = 4, 2


class FakeStream:
    """Streaming with a new frame on every read unless told otherwise."""

    def __init__(self, state="streaming"):
        self.state = state
        self.seq = 0
        self.new_frames = True
        self.reads = []

    def health(self, camera_key):
        return {"cameraKey": camera_key, "state": self.state}

    def latest_frame(self, camera_key, after_seq=0, max_age_ms=None, wait_ms=0):
        self.reads.append(after_seq)
        if self.new_frames:
            self.seq += 1
        if self.seq <= after_seq:
            return None
        return Frame(self.seq)


class FakeExecutor:
    def __init__(self, clock=None, duration=0.0, handoff=None, status_of=None, gate=None):
        self.clock = clock
        self.duration = duration
        self.handoff = handoff
        self.gate = gate
        self.runs = []
        self.handed = []
        self.contexts = []

    def __call__(self, execution_id):
        self.runs.append(execution_id)
        self.contexts.append(CONTINUOUS_RUN.get())
        if self.handoff is not None:
            self.handed.append(self.handoff.take(execution_id))
        if self.gate is not None:
            self.gate.wait(10)
        if self.clock is not None:
            self.clock.advance(self.duration)


class MemoryStore:
    def __init__(self, status="completed"):
        self.rows = {}
        self.statuses = {}
        self.default_status = status
        self.count = 0

    def insert(self, registration_id, context):
        self.count += 1
        execution_id = "exec-{0}".format(self.count)
        self.rows[execution_id] = (registration_id, dict(context))
        return execution_id

    def status(self, execution_id):
        return self.statuses.get(execution_id, self.default_status)


def make_feed(fps=1.0, **fields):
    values = dict(node_id="cam", protocol="rtsp", camera_key="cfg-7", url="rtsp://10.0.0.7/main",
                  processing_mode="continuous", frames_per_second=fps, max_frame_age_ms=2000,
                  keep_recent_runs=20, keep_notable_runs=200, camera_source_id="cfg-7")
    values.update(fields)
    return StreamFeed(**values)


def make_runner(fps=1.0, clock=None, stream=None, executor=None, store=None, **kwargs):
    clock = clock or Clock()
    handoff = kwargs.pop("handoff", FrameHandoff())
    return ContinuousRunner("wf-1:3", make_feed(fps), execute=executor or FakeExecutor(clock),
                            stream_manager=stream or FakeStream(), store=store or MemoryStore(),
                            handoff=handoff, clock=clock, wall=lambda: 1_790_000_000.0 + clock.now,
                            **kwargs), clock


def drive(runner, clock, seconds):
    end = clock.now + seconds
    while clock.now < end:
        delay = runner.step()
        if delay is None:
            return
        clock.advance(delay)


# --- the runner -----------------------------------------------------------------


class TestRunner:
    def test_runs_start_at_the_sampling_rate_with_the_continuous_context(self):
        """11.1, 11.9: a run per tick, whose trigger context records the
        continuous source, the frame and the tick."""
        store = MemoryStore()
        runner, clock = make_runner(fps=2.0, store=store)

        drive(runner, clock, 5.0)

        assert runner.counters()["started"] == 10
        registration_id, context = store.rows["exec-1"]
        assert registration_id == "wf-1:3"
        assert context == {"source": "continuous", "frameSeq": 1, "frameAcquiredAtMs": 1_790_000_000_000,
                           "tickAtMs": int((1_790_000_000.0 + 100.0) * 1000)}
        ticks = [context["tickAtMs"] for _registration, context in store.rows.values()]
        assert [later - earlier for earlier, later in zip(ticks, ticks[1:])] == [500] * 9

    def test_the_tick_frame_is_handed_to_its_run_and_the_run_is_quiet(self):
        handoff = FrameHandoff()
        runner, clock = make_runner(handoff=handoff)
        executor = FakeExecutor(clock, handoff=handoff)
        runner._execute = executor

        drive(runner, clock, 3.0)

        assert [frame.seq for frame in executor.handed] == [1, 2, 3]
        assert len(handoff) == 0
        # 12.6: every run executes inside the continuous_run context.
        assert executor.contexts == ["wf-1:3"] * 3
        assert CONTINUOUS_RUN.get() is None

    def test_a_tick_without_a_new_frame_is_skipped(self):
        """11.2: each frame is processed at most once."""
        stream = FakeStream()
        runner, clock = make_runner(stream=stream)
        drive(runner, clock, 2.0)
        stream.new_frames = False

        drive(runner, clock, 3.0)

        counters = runner.counters()
        assert counters["started"] == 2
        assert counters["skippedNoNewFrame"] == 3
        assert stream.reads[-1] == 2

    def test_ticks_during_a_run_are_skipped_never_queued(self):
        """11.3: a 2.5 s run at 1 run/s skips the two ticks it spans."""
        runner, clock = make_runner()
        runner._execute = FakeExecutor(clock, duration=2.5)

        drive(runner, clock, 6.0)

        counters = runner.counters()
        assert counters["started"] == 2
        assert counters["skippedBusy"] == 4

    def test_a_late_loop_skips_the_ticks_it_missed(self):
        runner, clock = make_runner()
        runner.step()
        clock.advance(3.5)

        runner.step()

        assert runner.counters()["skippedBusy"] == 2
        assert runner.counters()["started"] == 2

    def test_an_outage_records_one_event_and_starts_no_run(self):
        """11.5: nothing runs while not streaming, one event per outage,
        and the next run follows at once when streaming resumes."""
        stream = FakeStream(state="reconnecting")
        store = MemoryStore()
        runner, clock = make_runner(stream=stream, store=store)

        drive(runner, clock, 5.0)
        assert runner.counters()["started"] == 0
        assert runner.counters()["streamUnavailable"] == 1
        assert runner.status()["state"] == STATE_WAITING

        stream.state = "streaming"
        resumed_at = clock.now
        drive(runner, clock, 0.6)
        assert runner.counters()["started"] == 1
        assert runner.status()["state"] == STATE_RUNNING
        started_at = store.rows["exec-1"][1]["tickAtMs"] / 1000.0 - 1_790_000_000.0
        assert started_at - resumed_at <= 0.5

        stream.state = "failed"
        drive(runner, clock, 2.0)
        assert runner.counters()["streamUnavailable"] == 2

    def test_a_failed_run_is_counted_and_the_next_tick_runs(self):
        """11.10."""
        store = MemoryStore(status="failed")
        runner, clock = make_runner(store=store)

        drive(runner, clock, 3.0)

        assert runner.counters()["failed"] == 3
        assert runner.counters()["completed"] == 0
        assert runner.counters()["started"] == 3

    def test_a_raising_executor_does_not_stop_the_runner(self):
        store = MemoryStore(status="failed")
        runner, clock = make_runner(store=store)

        def boom(execution_id):
            raise RuntimeError("pipeline exploded")

        runner._execute = boom
        drive(runner, clock, 2.0)

        assert runner.counters()["started"] == 2
        assert runner.counters()["failed"] == 2

    def test_retention_outcomes_feed_the_counters(self):
        class Retention:
            def __init__(self):
                self.calls = []

            def on_run_complete(self, execution_id, keep_recent, keep_notable, output_ids=(),
                                stream_node_id=None):
                self.calls.append((execution_id, keep_recent, keep_notable, output_ids, stream_node_id))
                return RunOutcome("completed", notable=True, outputs_sent=2, processed_seq=5)

        retention = Retention()
        stream = FakeStream()
        runner, clock = make_runner(stream=stream, retention=retention, output_ids=("out1",))

        drive(runner, clock, 1.0)

        assert retention.calls == [("exec-1", 20, 200, ("out1",), "cam")]
        counters = runner.counters()
        assert (counters["completed"], counters["notable"], counters["outputsSent"]) == (1, 1, 2)
        # A newer frame the run analyzed is never run again.
        runner.step()
        assert stream.reads[-1] == 5

    def test_pause_and_resume(self):
        runner, clock = make_runner()
        drive(runner, clock, 2.0)

        runner.pause()
        drive(runner, clock, 5.0)
        assert runner.counters()["started"] == 2
        status = runner.status()
        assert status["state"] == STATE_PAUSED
        assert status["pausedAtMs"] == int((1_790_000_000.0 + 102.0) * 1000)

        runner.resume()
        drive(runner, clock, 0.1)
        assert runner.counters()["started"] == 3
        assert runner.status()["pausedAtMs"] is None

    def test_status_reports_rates_and_counters(self):
        runner, clock = make_runner(fps=2.0)
        drive(runner, clock, 30.0)

        status = runner.status()
        assert status["registrationId"] == "wf-1:3"
        assert status["configuredFps"] == 2.0
        assert status["effectiveFps"] == 2.0
        assert status["streamHealth"]["state"] == "streaming"
        assert status["cameraSourceId"] == "cfg-7"
        assert set(status["counters"]) == {"started", "completed", "failed", "skippedBusy",
                                           "skippedNoNewFrame", "notable", "outputsSent",
                                           "streamUnavailable", "modelUnavailable"}
        # 11.11: no model gate for a document without an ``emltriton``
        # element, so nothing is ever waited for.
        assert status["modelReadiness"] is None

    def test_one_summary_line_per_minute(self, caplog):
        runner, clock = make_runner(fps=4.0)
        with caplog.at_level("INFO", logger="workflow_engine.continuous_runner"):
            drive(runner, clock, 185.0)
        summaries = [record for record in caplog.records if "last minute" in record.getMessage()]
        assert len(summaries) == 3
        assert "240 runs" in summaries[0].getMessage()

    def test_stop_ends_the_loop(self):
        runner, clock = make_runner()
        runner.stop()
        assert runner.step() is None


# --- the manager, against the watcher and sqlite ----------------------------------------


@pytest.fixture(autouse=True)
def no_registry_scan():
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


@pytest.fixture
def session_factory():
    return make_session_factory()


def manifest(version):
    return dict(VALID_MANIFEST, componentVersion="{0}.0.0".format(version), workflowVersion=int(version))


def write_continuous(root, version="3", **parameters):
    parameters.setdefault("processing_mode", "continuous")
    document = make_stream_document(**parameters)
    document["workflowVersion"] = version
    return write_artifact_set(root, "wf-1", version, manifest=manifest(version), compiled=document)


def make_manager(session_factory, watcher, stream=None, execute=None, clock=None, start_threads=False,
                 retention=None):
    clock = clock or Clock()
    manager = ContinuousRunnerManager(
        session_factory=session_factory, resolution_provider=watcher.binding_resolution,
        execute_provider=lambda: execute or FakeExecutor(clock),
        manager_provider=lambda: stream or FakeStream(), stream_camera_resolver=lambda session: {},
        retention=retention, start_threads=start_threads, clock=clock,
        wall=lambda: 1_790_000_000.0 + clock.now)
    watcher.registrations_listeners.append(manager.on_registrations_changed)
    return manager, clock


def state_row(session_factory, registration_id):
    session = session_factory()
    try:
        row = session.get(WorkflowContinuousState, registration_id)
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


class TestManager:
    def test_only_continuous_registrations_get_a_runner(self, tmp_path, session_factory):
        write_continuous(tmp_path)
        write_artifact_set(tmp_path, "wf-2", "1", manifest=dict(VALID_MANIFEST, workflowId="wf-2"),
                           compiled=make_stream_document(processing_mode="on_trigger"))
        write_artifact_set(tmp_path, "wf-9", "1", manifest=dict(VALID_MANIFEST, workflowId="wf-9"))
        watcher = make_watcher(tmp_path, session_factory)
        manager, _clock = make_manager(session_factory, watcher)

        watcher.sync_once()

        assert manager.runner("wf-1:3") is not None
        assert manager.runner("wf-2:1") is None and manager.status("wf-2:1") is None
        assert manager.runner("wf-9:1") is None

    def test_a_pause_persists_across_a_restart(self, tmp_path, session_factory):
        """11.6: the pause survives a new manager (a backend restart) until
        the operator resumes."""
        write_continuous(tmp_path)
        watcher = make_watcher(tmp_path, session_factory)
        manager, clock = make_manager(session_factory, watcher)
        watcher.sync_once()
        runner = manager.runner("wf-1:3")
        drive(runner, clock, 2.0)

        paused = manager.pause("wf-1:3")
        assert paused["state"] == STATE_PAUSED
        row = state_row(session_factory, "wf-1:3")
        assert row.paused and row.paused_at == paused["pausedAtMs"]
        assert json.loads(row.counters_json)["started"] == 2

        # A restart: a new watcher and manager over the same database.
        restarted_watcher = make_watcher(tmp_path, session_factory)
        restarted, clock2 = make_manager(session_factory, restarted_watcher)
        restarted_watcher.sync_once()
        runner = restarted.runner("wf-1:3")
        assert runner.paused
        assert restarted.status("wf-1:3")["pausedAtMs"] == paused["pausedAtMs"]
        assert restarted.status("wf-1:3")["counters"]["started"] == 2
        drive(runner, clock2, 5.0)
        assert runner.counters()["started"] == 2

        resumed = restarted.resume("wf-1:3")
        assert resumed["state"] == STATE_RUNNING
        assert not state_row(session_factory, "wf-1:3").paused
        drive(runner, clock2, 1.0)
        assert runner.counters()["started"] == 3

    def test_counter_snapshots_are_saved(self, tmp_path, session_factory):
        write_continuous(tmp_path)
        watcher = make_watcher(tmp_path, session_factory)
        manager, clock = make_manager(session_factory, watcher)
        watcher.sync_once()
        drive(manager.runner("wf-1:3"), clock, 3.0)

        manager.persist_counters()

        assert json.loads(state_row(session_factory, "wf-1:3").counters_json)["started"] == 3

    def test_superseding_stops_the_runner_after_the_in_flight_run(self, tmp_path, session_factory):
        """11.8: no run starts after supersession, the in-flight one
        finishes, and the superseded registration's state row goes."""
        write_continuous(tmp_path, "3")
        watcher = make_watcher(tmp_path, session_factory)
        gate = threading.Event()
        executor = FakeExecutor(gate=gate)
        manager, _clock = make_manager(session_factory, watcher, execute=executor, clock=Clock(),
                                       start_threads=True)
        # A real clock for the thread.
        manager._clock = time.monotonic
        watcher.sync_once()
        runner = manager.runner("wf-1:3")
        manager.pause("wf-1:3")
        manager.resume("wf-1:3")
        deadline = time.monotonic() + 5
        while not executor.runs and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(executor.runs) == 1
        assert state_row(session_factory, "wf-1:3") is not None

        write_continuous(tmp_path, "4")
        watcher.sync_once()

        assert get_row(session_factory, "wf-1:3").status == "superseded"
        assert runner.stopped
        assert manager.runner("wf-1:3") is None
        assert manager.runner("wf-1:4") is not None
        assert state_row(session_factory, "wf-1:3") is None
        # The in-flight run is still running; releasing it ends the thread
        # without another run of the superseded registration.
        successor = manager.runner("wf-1:4")
        gate.set()
        runner.join(5)
        assert not runner._thread.is_alive()
        manager.stop_all()
        successor.join(5)
        session = session_factory()
        superseded_runs = session.query(WorkflowExecution).filter(
            WorkflowExecution.registration_id == "wf-1:3").count()
        session.close()
        assert superseded_runs == 1
        # The exit hook never re-creates the superseded registration's row.
        assert state_row(session_factory, "wf-1:3") is None

    def test_removal_stops_the_runner_and_keeps_the_pause(self, tmp_path, session_factory):
        import shutil

        path = write_continuous(tmp_path)
        watcher = make_watcher(tmp_path, session_factory)
        manager, _clock = make_manager(session_factory, watcher)
        watcher.sync_once()
        manager.pause("wf-1:3")

        shutil.rmtree(path)
        watcher.sync_once()

        assert manager.runner("wf-1:3") is None
        assert state_row(session_factory, "wf-1:3").paused

    def test_a_changed_rate_restarts_the_runner_with_its_counters(self, tmp_path, session_factory):
        write_continuous(tmp_path, frames_per_second=1.0)
        watcher = make_watcher(tmp_path, session_factory)
        manager, clock = make_manager(session_factory, watcher)
        watcher.sync_once()
        first = manager.runner("wf-1:3")
        drive(first, clock, 2.0)

        write_continuous(tmp_path, frames_per_second=4.0)
        watcher.sync_once()

        second = manager.runner("wf-1:3")
        assert second is not first and first.stopped
        assert second.feed.frames_per_second == 4.0
        assert second.counters()["started"] == 2

    def test_no_executor_means_no_runner(self, tmp_path, session_factory):
        write_continuous(tmp_path)
        watcher = make_watcher(tmp_path, session_factory)
        manager = ContinuousRunnerManager(session_factory=session_factory, execute_provider=lambda: None,
                                          manager_provider=FakeStream, stream_camera_resolver=lambda s: {},
                                          start_threads=False)
        watcher.registrations_listeners.append(manager.on_registrations_changed)

        watcher.sync_once()

        assert manager.runner("wf-1:3") is None


class TestExecutionStore:
    def test_insert_writes_a_pending_run_like_the_trigger_runtime(self, tmp_path, session_factory):
        write_continuous(tmp_path)
        make_watcher(tmp_path, session_factory).sync_once()
        store = ExecutionStore(session_factory)

        execution_id = store.insert("wf-1:3", {"source": "continuous", "frameSeq": 3})

        session = session_factory()
        row = session.get(WorkflowExecution, execution_id)
        assert row.status == "pending"
        assert json.loads(row.trigger_context_json) == {"source": "continuous", "frameSeq": 3}
        session.close()
        assert store.status(execution_id) == "pending"
        assert store.status("missing") is None


# --- the API ---------------------------------------------------------------------------


@pytest.fixture
def client(session_factory):
    app = FastAPI()
    app.include_router(workflow_engine_api.router)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[workflow_engine_api.get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides = {}


def executions(session_factory):
    session = session_factory()
    try:
        return session.query(WorkflowExecution).count()
    finally:
        session.close()


class TestContinuousApi:
    @pytest.fixture
    def wired(self, tmp_path, session_factory):
        write_continuous(tmp_path)
        write_artifact_set(tmp_path, "wf-9", "1", manifest=dict(VALID_MANIFEST, workflowId="wf-9"))
        watcher = make_watcher(tmp_path, session_factory)
        manager, _clock = make_manager(session_factory, watcher)
        watcher.sync_once()
        with patch.object(runtime, "_watcher", watcher), \
                patch.object(runtime, "_continuous_manager", manager), \
                patch.object(executor_module, "_executor", None):
            yield manager

    def test_status_pause_and_resume_routes(self, wired, client):
        status = client.get("/workflows/registrations/wf-1:3/continuous")
        assert status.status_code == 200
        assert status.json()["state"] == STATE_RUNNING
        assert status.json()["configuredFps"] == 1.0

        paused = client.post("/workflows/registrations/wf-1:3/continuous/pause")
        assert paused.status_code == 200 and paused.json()["state"] == STATE_PAUSED
        assert isinstance(paused.json()["pausedAtMs"], int)

        resumed = client.post("/workflows/registrations/wf-1:3/continuous/resume")
        assert resumed.status_code == 200 and resumed.json()["state"] == STATE_RUNNING

    @pytest.mark.parametrize("route", ["continuous", "continuous/pause", "continuous/resume"])
    def test_a_non_continuous_or_unknown_registration_is_404(self, wired, client, route):
        method = client.get if route == "continuous" else client.post
        assert method("/workflows/registrations/wf-9:1/{0}".format(route)).status_code == 404
        assert method("/workflows/registrations/nope:1/{0}".format(route)).status_code == 404

    def test_a_manual_trigger_is_accepted_only_while_paused(self, wired, client, session_factory):
        """11.7, with the real manager behind the guard."""
        refused = client.post("/workflows/registrations/wf-1:3/trigger")
        assert refused.status_code == 409
        assert refused.json()["detail"].startswith("CONTINUOUS_WORKFLOW_RUNNING: ")
        assert executions(session_factory) == 0

        client.post("/workflows/registrations/wf-1:3/continuous/pause")
        accepted = client.post("/workflows/registrations/wf-1:3/trigger")
        assert accepted.status_code == 200
        assert executions(session_factory) == 1

    def test_workflows_without_a_continuous_node_trigger_as_before(self, wired, client, session_factory):
        """18.4: no continuous status, no guard."""
        assert client.post("/workflows/registrations/wf-9:1/trigger").status_code == 200
        assert executions(session_factory) == 1

    def test_the_notable_filter_lists_only_notable_runs(self, wired, client, session_factory):
        """16.2: ``notable=true`` keeps the retained Notable_Runs, newest
        first; without it the list is unchanged."""
        session = session_factory()
        for index, execution_id in enumerate(["exec-a", "exec-b", "exec-c"]):
            session.add(WorkflowExecution(id=execution_id, registration_id="wf-1:3", started_at=100 + index,
                                          status="failed" if execution_id != "exec-b" else "completed"))
        session.commit()
        session.close()
        wired.notable_execution_ids = lambda registration_id: ["exec-c", "exec-a"] \
            if registration_id == "wf-1:3" else []

        everything = client.get("/workflows/registrations/wf-1:3/executions")
        notable = client.get("/workflows/registrations/wf-1:3/executions?notable=true")
        other = client.get("/workflows/registrations/wf-9:1/executions?notable=true")

        assert [run["executionId"] for run in everything.json()] == ["exec-c", "exec-b", "exec-a"]
        assert [run["executionId"] for run in notable.json()] == ["exec-c", "exec-a"]
        assert other.json() == []

    def test_the_real_manager_reports_the_retentions_notable_runs(self, tmp_path, session_factory):
        class Retention:
            def notable_ids(self, registration_id):
                return ["exec-9"]

        watcher = make_watcher(tmp_path, session_factory)
        manager, _clock = make_manager(session_factory, watcher, retention=Retention())
        assert manager.notable_execution_ids("wf-1:3") == ["exec-9"]
        without, _clock = make_manager(session_factory, watcher)
        assert without.notable_execution_ids("wf-1:3") is None
        with patch.object(runtime, "_continuous_manager", without):
            assert runtime.notable_execution_ids("wf-1:3") == []


# --- the history of other workflows ------------------------------------------------------------


class TestHistoryOfOtherWorkflows:
    def test_caps_never_delete_a_run_that_is_not_continuous(self, tmp_path, session_factory):
        """12.8: even with every cap at zero, triggered and manual runs stay."""
        write_continuous(tmp_path)
        make_watcher(tmp_path, session_factory).sync_once()
        captures = tmp_path / "captures"
        session = session_factory()
        for index, context in enumerate([None, json.dumps({"source": "mqtt"}),
                                         json.dumps({"source": "opcua", "continuous": True})]):
            run_dir = captures / "wf-1" / "exec-{0}".format(index)
            os.makedirs(run_dir)
            (run_dir / "frame.jpg").write_bytes(b"\xff" * 4096)
            session.add(WorkflowExecution(id="exec-{0}".format(index), registration_id="wf-1:3",
                                          started_at=1, status="completed", output_dir=str(run_dir),
                                          capture_id="c", trigger_context_json=context))
        session.commit()
        session.close()
        retention = RunRetention(session_factory=session_factory, persistent_root=str(captures),
                                 staging_candidate=str(tmp_path / "shm"), debug_log_path=None,
                                 limits=lambda: SimpleNamespace(retention_bytes=0, staging_bytes=0),
                                 min_staging_free_bytes=0)

        retention.enforce_caps()
        for index in range(3):
            retention.on_run_complete("exec-{0}".format(index), 0, 0)

        assert executions(session_factory) == 3
        assert all((captures / "wf-1" / "exec-{0}".format(index)).is_dir() for index in range(3))
