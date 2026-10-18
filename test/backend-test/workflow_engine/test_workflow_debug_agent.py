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
"""The Debug_Agent end to end with fakes (workflow-debug-mode design §5.1
``agent.py``, §5.3, §4.2; tasks.md 2.8).

A complete shadow document activates a session; a fixture run yields one
``run`` record on the agent's publisher with the expected topic and QoS 1;
``drop`` records are coalesced per reason per second; the 30 s ``session``
heartbeat; expiry and stop produce the final record; the restart path through
a second agent instance (new ``instance``, ``seq`` 1, ``reason: resumed``); an
observer that raises never propagates; ``status()``.

Validates: debug R2.7, R4.3, R4.4, R5.6, R10.3, R13.1.
"""
import json
import logging
from pathlib import Path

import pytest

import awsiot_host_stubs  # noqa: F401 - the real IpcPublisher imports nothing of awsiot, kept for parity
from workflow_engine.telemetry import hooks
from workflow_engine.telemetry.debug import agent as agent_module
from workflow_engine.telemetry.debug.agent import AGENT_VERSION, DebugAgent, debug_status_for
from workflow_engine.telemetry.registry_view import RegistrationInfo

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "workflow_debug"
THING = "thing-1"
UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF_A = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
SESSION_A = "11111111-1111-4111-8111-111111111111"
REG_A = "reg-a:3"
NOW_MS = 1_792_075_062_000
TOPIC = "dda/debug/%s/%s/record" % (UC, WF_A)
LOG_WE = logging.getLogger("workflow_engine.agent_test")


class FakeClock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = NOW_MS / 1000.0

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds


class FakePublisher:
    def __init__(self):
        self.published = []
        self.messages_sent = []
        self.failures_by_reason = {"denied": 0, "spool_full": 0, "other": 0}
        self.publish_failures = 0
        self.publish_successes = 0

    def publish(self, topic, payload, qos):
        self.published.append((topic, payload, qos))
        self.publish_successes += 1

    def publish_message(self, topic, payload):
        self.messages_sent.append((topic, payload))

    def status(self):
        return {"name": "fake"}

    def records(self, kind=None):
        out = []
        for topic, payload, qos in self.published:
            for record in json.loads(payload)["records"]:
                if kind is None or record["type"] == kind:
                    out.append(record)
        return out

    def envelopes(self):
        return [json.loads(p[1]) for p in self.published]


class FakeShadow:
    instances = []

    def __init__(self, thing_name, shadow_name, on_desired, publisher):
        self.thing_name = thing_name
        self.shadow_name = shadow_name
        self.on_desired = on_desired
        self.publisher = publisher
        self.reports = []
        self.started = False
        self.closed = False
        FakeShadow.instances.append(self)

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def report(self, state):
        self.reports.append(json.loads(json.dumps(state)))
        return True


class FakeView:
    def __init__(self):
        self.rows = {REG_A: RegistrationInfo(WF_A, "3", (), False)}
        self.listeners = []

    def snapshot(self):
        return dict(self.rows)

    def add_listener(self, callback):
        self.listeners.append(callback)

    def remove_listener(self, callback):
        if callback in self.listeners:
            self.listeners.remove(callback)


class FakeUploader:
    def __init__(self):
        self.calls = []

    def maybe_upload(self, session, run_record):
        self.calls.append((session.session_id, run_record.execution_id))
        return None


def entry(session_id=SESSION_A, enabled_at=NOW_MS - 60_000, expires_at=NOW_MS + 3_600_000, level="debug"):
    return {
        "sessionId": session_id, "usecaseId": UC, "enabledAt": enabled_at, "expiresAt": expires_at,
        "level": level, "images": False, "imageMaxKb": 150, "bucket": "results-bucket", "region": "us-east-1",
        "prefix": "dda-telemetry/debug/%s/%s/%s/%s/" % (UC, THING, WF_A, session_id),
        "maxMessagesPerSecond": 10, "maxBytesPerSecond": 262144,
    }


def run_record(execution_id="exec-1", **overrides):
    values = dict(
        registration_id=REG_A, workflow_id=WF_A, workflow_version="3", execution_id=execution_id,
        status="completed", error=None, failing_node_id=None, traceback=None,
        capture_id="wf-1-exec-1", output_dir=str(FIXTURES / "completed"),
        log_path=str(FIXTURES / "completed" / "run.log"),
        enqueued_at_s=NOW_MS // 1000, accepted_ms=NOW_MS + 100, run_started_ms=NOW_MS + 400,
        finished_ms=NOW_MS + 1100, queued_ms=100, setup_ms=300, readiness_wait_ms=0,
        trigger_context={"topic": "ppe/line1", "payload": "{}", "qos": 1, "timestamp": NOW_MS / 1000.0},
        node_status={"n1": {"status": "success", "durationMs": 12}}, tag_values=None,
        detections=None, detection_count=0, continuous=False,
    )
    values.update(overrides)
    return hooks.RunRecord(**values)


@pytest.fixture
def world():
    hooks._clear_observers()
    FakeShadow.instances.clear()
    clock = FakeClock()
    publisher = FakePublisher()
    view = FakeView()
    uploader = FakeUploader()
    saved_level = logging.getLogger("workflow_engine").level
    agent = DebugAgent(thing_name=THING, publisher=publisher, shadow_factory=FakeShadow, registry_view=view,
                       uploader=uploader, secrets=lambda: [], clock=lambda: clock.mono,
                       wall=lambda: clock.wall, run_thread=False)
    agent.start()
    yield agent, publisher, view, clock, uploader
    agent.stop()
    hooks._clear_observers()
    logging.getLogger("workflow_engine").setLevel(saved_level)


def activate(agent, clock, **kwargs):
    agent._on_desired({"v": 1, "sessions": {WF_A: entry(**kwargs)}}, 1)
    agent.tick()
    return agent.sessions.session_for_workflow(WF_A)


def settle(agent, clock, ticks=4, step=0.2):
    """Run ticks 200 ms apart: enough for the two-tick assembly delay and
    the batcher's 0.5 s base drain interval."""
    for _ in range(ticks):
        clock.advance(step)
        agent.tick()


class TestStartAndActivation:
    def test_start_wires_the_collaborators_and_the_observers(self, world):
        agent, publisher, view, clock, uploader = world
        shadow = FakeShadow.instances[-1]
        assert shadow.thing_name == THING and shadow.shadow_name == "dda-workflow-debug"
        assert shadow.publisher is publisher and shadow.started
        assert shadow.on_desired == agent._on_desired
        assert agent._on_registrations in view.listeners
        assert agent._on_run_finished in hooks._run_observers
        assert agent._on_trigger_dropped in hooks._drop_observers
        assert agent._on_continuous_tick in hooks._continuous_observers
        assert agent._on_model_event in hooks._model_observers
        assert agent.status()["started"] is True
        assert agent.status()["tapAttached"] is False

    def test_a_complete_shadow_document_activates_a_session_and_emits_started(self, world):
        agent, publisher, view, clock, uploader = world
        session = activate(agent, clock, enabled_at=NOW_MS + 500, expires_at=NOW_MS + 3_600_000)
        assert session.state == "active" and session.batcher is not None
        assert agent.tap.attached and agent.guard.active
        assert logging.getLogger("workflow_engine").level == logging.DEBUG
        settle(agent, clock)
        records = publisher.records("session")
        assert len(records) == 1
        started = records[0]
        assert started["state"] == "active" and started["reason"] == "started"
        assert started["instance"] == agent.instance and started["agentVersion"] == AGENT_VERSION
        assert started["level"] == "debug" and started["images"] is False
        assert set(started["counters"]) >= {"records", "bytes", "queueDrops", "tapDrops", "truncations",
                                            "rateDeferred", "bytesDeferred", "spoolFull", "imagesSent",
                                            "imagesCapped", "imagesTooLarge", "publishFailures",
                                            "unattributedLines", "otherPairLines"}
        topic, payload, qos = publisher.published[0]
        assert topic == TOPIC and qos == 1
        envelope = json.loads(payload)
        assert envelope["schema"] == "dda.debug.v1" and envelope["seq"] == 1
        assert envelope["deviceId"] == THING and envelope["workflowId"] == WF_A
        assert envelope["workflowVersion"] == "3" and envelope["sessionId"] == SESSION_A
        # reported through the shadow
        reported = FakeShadow.instances[-1].reports[-1]
        assert reported["sessions"][WF_A]["state"] == "active"
        assert reported["instance"] == agent.instance and reported["v"] == 1

    def test_info_level_session_does_not_lower_the_loggers(self, world):
        agent, publisher, view, clock, uploader = world
        before = logging.getLogger("workflow_engine").level
        session = activate(agent, clock, level="info")
        assert session.state == "active"
        assert agent.tap.attached and not agent.guard.active
        assert logging.getLogger("workflow_engine").level == before

    def test_status_and_debug_status_for(self, world):
        agent, publisher, view, clock, uploader = world
        assert debug_status_for(WF_A) is None  # no agent registered: the API omits the field
        agent_module.set_agent(agent)
        try:
            assert debug_status_for(WF_A) == {"active": False, "expiresAt": None}
            activate(agent, clock)
            assert debug_status_for(WF_A) == {"active": True, "expiresAt": NOW_MS + 3_600_000}
            assert debug_status_for("other") == {"active": False, "expiresAt": None}
            status = agent.status()
            assert status["sessions"][WF_A]["active"] is True
            assert status["publisher"] == {"name": "fake"}
            assert status["levelGuardActive"] is True
        finally:
            agent_module.set_agent(None)
        assert debug_status_for(WF_A) is None


class TestRecords:
    def test_a_fixture_run_yields_one_run_record_on_the_publisher(self, world):
        agent, publisher, view, clock, uploader = world
        activate(agent, clock)
        hooks.notify_model_readiness(REG_A, "exec-1", "plates", "READY", 1200, True)
        hooks.notify_model_wait(REG_A, {"model": "plates", "state": "READY"})
        token = hooks.CURRENT_EXECUTION.set((REG_A, WF_A, "exec-1"))
        try:
            LOG_WE.debug("a DEBUG line of the run")
        finally:
            hooks.CURRENT_EXECUTION.reset(token)
        hooks.notify_run_finished(run_record())
        settle(agent, clock, ticks=4)
        runs = publisher.records("run")
        assert len(runs) == 1
        run = runs[0]
        assert run["executionId"] == "exec-1" and run["status"] == "completed"
        assert run["trigger"]["type"] == "mqtt"
        assert run["model"] == {"readiness": [{"model": "plates", "state": "READY", "waitedMs": 1200,
                                               "ready": True}],
                                "wait": {"model": "plates", "state": "READY"}}
        assert run["outputs"]["detections"][0]["label"] == "person"
        assert [l["message"] for l in run["logs"] if l["src"] == "tap"] == ["a DEBUG line of the run"]
        assert any(l["src"] == "runlog" for l in run["logs"])
        assert run["image"] is None
        assert uploader.calls == [(SESSION_A, "exec-1")]
        assert all(p[0] == TOPIC and p[2] == 1 for p in publisher.published)
        assert agent.tap.buffer_for("exec-1") is None  # the buffer was taken
        assert "exec-1" not in agent._readiness

    def test_a_run_of_another_workflow_is_ignored(self, world):
        agent, publisher, view, clock, uploader = world
        activate(agent, clock)
        hooks.notify_run_finished(run_record(registration_id="reg-b:1", workflow_id="wf-b"))
        settle(agent, clock, ticks=4)
        assert publisher.records("run") == []

    def test_drop_records_are_coalesced_per_reason_per_second(self, world):
        agent, publisher, view, clock, uploader = world
        activate(agent, clock)
        for _ in range(5):
            hooks.notify_trigger_dropped(REG_A, "trig-1", "queue_full")
        hooks.notify_trigger_dropped(REG_A, "trig-1", "drop_policy")
        hooks.notify_continuous_tick(REG_A, "skippedBusy", 3)
        hooks.notify_continuous_tick(REG_A, "skippedNoNewFrame", 1)
        hooks.notify_continuous_tick(REG_A, "outputsSent", 1)  # not a drop
        hooks.notify_trigger_dropped("reg-other:1", "t", "queue_full")  # not a session
        agent.tick()
        assert publisher.records("drop") == []  # the second is still open
        clock.advance(1.2)
        agent.tick()
        settle(agent, clock, ticks=2)
        drops = {(d["nodeId"], d["reason"]): d["count"] for d in publisher.records("drop")}
        assert drops == {("trig-1", "queue_full"): 5, ("trig-1", "drop_policy"): 1,
                         (None, "skipped_busy"): 3, (None, "skipped_no_new_frame"): 1}
        assert all(isinstance(d["ts"], int) for d in publisher.records("drop"))

    def test_between_runs_log_record(self, world):
        agent, publisher, view, clock, uploader = world
        activate(agent, clock)
        LOG_WE.warning("a device-wide warning between runs")
        settle(agent, clock, ticks=3)
        logs = publisher.records("log")
        assert len(logs) == 1
        assert logs[0]["attribution"] == "device" and logs[0]["level"] == "WARNING"

    def test_heartbeat_every_30_s(self, world):
        agent, publisher, view, clock, uploader = world
        activate(agent, clock)
        settle(agent, clock, ticks=2)
        assert [r["reason"] for r in publisher.records("session")] == ["resumed"]
        for _ in range(29):
            clock.advance(1.0)
            agent.tick()
        assert [r["reason"] for r in publisher.records("session")] == ["resumed"]
        clock.advance(1.5)
        agent.tick()
        settle(agent, clock, ticks=2)
        assert [r["reason"] for r in publisher.records("session")] == ["resumed", "heartbeat"]
        for _ in range(31):
            clock.advance(1.0)
            agent.tick()
        settle(agent, clock, ticks=2)
        assert [r["reason"] for r in publisher.records("session")] == ["resumed", "heartbeat", "heartbeat"]

    def test_expiry_produces_the_final_record_and_detaches(self, world):
        agent, publisher, view, clock, uploader = world
        activate(agent, clock, expires_at=NOW_MS + 5_000)
        settle(agent, clock, ticks=2)
        clock.advance(5.0)
        agent.tick()
        settle(agent, clock, ticks=3)
        reasons = [(r["state"], r["reason"]) for r in publisher.records("session")]
        assert reasons == [("active", "resumed"), ("expired", "expired")]
        assert not agent.tap.attached and not agent.guard.active
        assert logging.getLogger("workflow_engine").level != logging.DEBUG
        assert FakeShadow.instances[-1].reports[-1]["sessions"][WF_A]["state"] == "expired"
        assert agent.sessions.session_for_workflow(WF_A).batcher is None

    def test_stop_through_an_absent_key_produces_the_final_record(self, world):
        agent, publisher, view, clock, uploader = world
        activate(agent, clock)
        settle(agent, clock, ticks=2)
        agent._on_desired({"v": 1, "sessions": {}}, 2)
        agent.tick()
        settle(agent, clock, ticks=3)
        assert [(r["state"], r["reason"]) for r in publisher.records("session")] == [
            ("active", "resumed"), ("stopped", "stopped")]
        assert not agent.tap.attached

    def test_registration_notification_wakes_the_evaluation(self, world):
        agent, publisher, view, clock, uploader = world
        view.rows.clear()
        session = activate(agent, clock)
        assert session.state == "pending"
        view.rows[REG_A] = RegistrationInfo(WF_A, "3", (), False)
        for callback in view.listeners:
            callback(frozenset({REG_A}), frozenset())
        agent.tick()
        assert session.state == "active"


class TestRestartPath:
    def test_a_second_agent_instance_resumes_with_seq_1(self, world):
        agent, publisher, view, clock, uploader = world
        activate(agent, clock)
        hooks.notify_run_finished(run_record("exec-1"))
        settle(agent, clock, ticks=4)
        first_envelopes = publisher.envelopes()
        assert [e["seq"] for e in first_envelopes] == [1, 2]
        agent.stop()

        clock.advance(60)
        second_publisher = FakePublisher()
        restarted = DebugAgent(thing_name=THING, publisher=second_publisher, shadow_factory=FakeShadow,
                               registry_view=view, uploader=uploader, secrets=lambda: [],
                               clock=lambda: clock.mono, wall=lambda: clock.wall, run_thread=False)
        restarted.start()
        try:
            assert restarted.instance != agent.instance
            restarted._on_desired({"v": 1, "sessions": {WF_A: entry()}}, 1)  # the shadow re-read
            restarted.tick()
            settle(restarted, clock, ticks=2)
            envelopes = second_publisher.envelopes()
            assert [e["seq"] for e in envelopes] == [1]
            assert envelopes[0]["instance"] == restarted.instance
            session_records = second_publisher.records("session")
            assert session_records[0]["reason"] == "resumed"
            assert session_records[0]["instance"] == restarted.instance
        finally:
            restarted.stop()


class TestContainment:
    def test_a_raising_observer_never_propagates(self, world):
        agent, publisher, view, clock, uploader = world
        activate(agent, clock)

        def boom(*args):
            raise RuntimeError("observer boom")

        hooks.register_run_observer(boom)
        hooks.register_drop_observer(boom)
        hooks.notify_run_finished(run_record())
        hooks.notify_trigger_dropped(REG_A, "t", "queue_full")
        settle(agent, clock, ticks=4)
        assert len(publisher.records("run")) == 1

    def test_garbage_handed_to_the_agents_observers_never_raises(self, world):
        agent, publisher, view, clock, uploader = world
        activate(agent, clock)
        hooks.notify_run_finished("not a record")
        hooks.notify_model_readiness(None, None, None, None, None, None)
        hooks.notify_model_wait(None, "not a dict")
        agent._on_desired("not a dict", 0)  # an older version: ignored
        settle(agent, clock, ticks=4)  # no exception escapes tick()
        assert agent.sessions.session_for_workflow(WF_A).state == "active"
        # A newer document that is not an object means "no desired state": the
        # session ends cleanly, as with an empty sessions map.
        agent._on_desired("not a dict", 5)
        settle(agent, clock, ticks=2)
        assert agent.sessions.session_for_workflow(WF_A).state == "stopped"

    def test_start_without_a_thing_name_raises_for_the_isolation_guard(self):
        hooks._clear_observers()
        with pytest.raises(RuntimeError):
            DebugAgent(thing_name="", publisher=FakePublisher(), shadow_factory=FakeShadow,
                       registry_view=FakeView(), run_thread=False).start()
        hooks._clear_observers()

    def test_the_real_thread_runs_ticks_and_stops(self):
        hooks._clear_observers()
        import time

        agent = DebugAgent(thing_name=THING, publisher=FakePublisher(), shadow_factory=FakeShadow,
                           registry_view=FakeView(), secrets=lambda: [])
        agent.start()
        try:
            assert agent._thread.name == "workflow-debug-agent" and agent._thread.daemon
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and agent.ticks_run < 2:
                time.sleep(0.02)
            assert agent.ticks_run >= 2
        finally:
            agent.stop()
            agent._thread.join(5.0)
            hooks._clear_observers()
        assert not agent._thread.is_alive()
