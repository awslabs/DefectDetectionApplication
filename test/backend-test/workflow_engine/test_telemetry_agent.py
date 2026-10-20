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
"""The Telemetry_Agent end to end with fakes (workflow-telemetry-dashboards
design §5.2 ``agent.py``, §5.4, §5.5; tasks.md 1.5).

A complete shadow document with ``enabled: true`` and a ``usecaseId`` makes a
fixture run produce one Summary on the right topic at the next boundary with
QoS 0; nothing is published without ``usecaseId`` or while disabled; a
failing run produces a coalesced ``run_error`` Event with QoS 1; a lease
produces a live PUT then a ``dda.live.v1`` publish; an observer that raises
never propagates; ``status()``.

Validates: R4.1, R4.3, R5.3, R5.5, R5.7, R5.8.
"""
import json
import logging
import uuid

import pytest
from PIL import Image

import awsiot_host_stubs  # noqa: F401 - parity with the other agent tests; the fakes need no SDK
from workflow_engine.telemetry import hooks
from workflow_engine.telemetry import agent as agent_module
from workflow_engine.telemetry.agent import AGENT_VERSION, REPORT_COUNTER_KEYS, TelemetryAgent
from workflow_engine.telemetry.registry_view import RegistrationInfo

THING = "thing-1"
UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF_A = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
REG_A = "reg-a:3"
#: Aligned to the 10 s and 60 s grids.
BASE = 1_792_075_200.0
BASE_MS = int(BASE * 1000)
SUMMARY_TOPIC = "dda/telemetry/%s/%s/summary" % (UC, WF_A)
EVENT_TOPIC = "dda/telemetry/%s/%s/event" % (UC, WF_A)
LIVE_TOPIC = "dda/telemetry/%s/%s/live" % (UC, WF_A)
PREFIX = "dda-telemetry/live/%s/%s/%s/" % (UC, THING, WF_A)


class FakeClock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = BASE

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds

    def set_wall(self, seconds):
        self.advance(seconds - self.wall)


class FakePublisher:
    def __init__(self):
        self.published = []
        self.messages_sent = []
        self.queue_drops = 0
        self.publish_failures = 0
        self.publish_successes = 0

    def publish(self, topic, payload, qos):
        self.published.append((topic, json.loads(payload), qos))
        self.publish_successes += 1

    def publish_message(self, topic, payload):
        self.messages_sent.append((topic, payload))

    def status(self):
        return {"name": "fake"}

    def on(self, kind):
        return [(t, p, q) for t, p, q in self.published if t.endswith("/" + kind)]


class FakeShadow:
    instances = []

    def __init__(self, thing_name, shadow_name, on_desired, publisher, **kwargs):
        self.thing_name = thing_name
        self.shadow_name = shadow_name
        self.on_desired = on_desired
        self.publisher = publisher
        self.kwargs = kwargs
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


class FakeS3:
    def __init__(self):
        self.puts = []
        self.log = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        self.log.append(("put", kwargs["Key"]))


def document(**overrides):
    doc = {"v": 1, "usecaseId": UC, "enabled": True, "summaryIntervalS": 10, "idleHeartbeatS": 60,
           "personClasses": ["person", "human"], "live": {}, "updatedAt": BASE_MS}
    doc.update(overrides)
    return doc


def lease_entry(expires_at=BASE_MS + 600_000):
    return {"expiresAt": expires_at, "maxFps": 2, "maxWidth": 1280, "maxHeight": 720, "maxBytes": 307200,
            "bucket": "results-bucket", "region": "eu-west-1", "prefix": PREFIX}


def run_record(clock, execution_id="exec-1", status="completed", error=None, failing_node_id=None,
               output_dir=None, capture_id=None, detections=None, registration_id=REG_A):
    finished_ms = int(clock.wall * 1000)
    return hooks.RunRecord(
        registration_id=registration_id, workflow_id=WF_A, workflow_version="3", execution_id=execution_id,
        status=status, error=error, failing_node_id=failing_node_id, traceback=None, capture_id=capture_id,
        output_dir=output_dir, log_path=None, enqueued_at_s=finished_ms // 1000 - 1, accepted_ms=finished_ms - 400,
        run_started_ms=finished_ms - 300, finished_ms=finished_ms, queued_ms=0, setup_ms=100, readiness_wait_ms=0,
        trigger_context={}, node_status=None, tag_values=None, detections=detections,
        detection_count=len(detections) if detections else 0, continuous=False,
    )


def frame_dir(tmp_path, capture_id="wf-exec-1"):
    output_dir = tmp_path / "out"
    output_dir.mkdir(exist_ok=True)
    Image.new("RGB", (1920, 1080), (40, 90, 160)).save(str(output_dir / (capture_id + ".overlay.jpg")),
                                                        format="JPEG", quality=90)
    return str(output_dir)


@pytest.fixture
def world():
    hooks._clear_observers()
    FakeShadow.instances.clear()
    clock = FakeClock()
    publisher = FakePublisher()
    view = FakeView()
    s3 = FakeS3()
    statuses = {}
    agent = TelemetryAgent(
        thing_name=THING, publisher=publisher, shadow_factory=FakeShadow, registry_view=view,
        client_factory=lambda region: s3, status_provider=lambda rid: statuses.get(rid), secrets=lambda: ["s3cr3t"],
        clock=lambda: clock.mono, wall=lambda: clock.wall, run_thread=False,
    )
    agent.start()
    agent.test_s3 = s3
    agent.test_statuses = statuses
    yield agent, publisher, view, clock
    agent.stop()
    hooks._clear_observers()


def enable(agent, clock, **overrides):
    agent._on_desired(document(**overrides), 1)
    agent.tick()


def tick_at(agent, clock, wall_seconds):
    clock.set_wall(wall_seconds)
    agent.tick()


class TestStartAndWiring:
    def test_start_wires_the_collaborators_and_the_observers(self, world):
        agent, publisher, view, clock = world
        shadow = FakeShadow.instances[-1]
        assert shadow.thing_name == THING and shadow.shadow_name == "dda-workflow-telemetry"
        assert shadow.publisher is publisher and shadow.started
        assert shadow.on_desired == agent._on_desired
        assert shadow.kwargs == {"counter_keys": REPORT_COUNTER_KEYS}
        assert set(REPORT_COUNTER_KEYS) == {"counters", "lastPublishAt", "lastSeq", "framesTooLarge"}
        assert agent._on_registrations in view.listeners
        assert agent._on_run_finished in hooks._run_observers
        assert agent._on_trigger_dropped in hooks._drop_observers
        assert agent._on_continuous_tick in hooks._continuous_observers
        assert agent._on_model_event in hooks._model_observers
        uuid.UUID(agent.instance)
        assert agent.summarizer.instance == agent.instance and agent.sender.instance == agent.instance
        assert agent.summarizer.device_id == THING
        status = agent.status()
        assert status["started"] is True and status["enabled"] is False and status["active"] is False
        assert status["trackedRegistrations"] == [] and status["seq"] == 0

    def test_start_without_a_thing_name_raises_for_the_isolation_guard(self):
        hooks._clear_observers()
        with pytest.raises(RuntimeError):
            TelemetryAgent(thing_name="", publisher=FakePublisher(), shadow_factory=FakeShadow,
                           registry_view=FakeView(), run_thread=False).start()
        hooks._clear_observers()

    def test_stop_unregisters_the_observers_and_closes_the_shadow(self, world):
        agent, publisher, view, clock = world
        agent.stop()
        assert agent._on_run_finished not in hooks._run_observers
        assert agent._on_registrations not in view.listeners
        assert FakeShadow.instances[-1].closed


class TestSummaries:
    def test_a_complete_document_makes_a_fixture_run_produce_one_summary_at_the_next_boundary(self, world):
        agent, publisher, view, clock = world
        enable(agent, clock)
        assert agent.status()["active"] is True and agent.status()["trackedRegistrations"] == [REG_A]
        clock.advance(2)
        hooks.notify_run_finished(run_record(clock, detections=[{"label": "person"}]))
        hooks.notify_trigger_dropped(REG_A, "trigger-1", "queue_full")
        tick_at(agent, clock, BASE + 9.5)
        assert publisher.published == []  # before the boundary
        tick_at(agent, clock, BASE + 10.3)
        assert len(publisher.published) == 1
        topic, payload, qos = publisher.published[0]
        assert topic == SUMMARY_TOPIC and qos == 0
        assert payload["schema"] == "dda.summary.v1" and payload["idle"] is False
        assert payload["deviceId"] == THING and payload["workflowId"] == WF_A and payload["workflowVersion"] == "3"
        assert payload["producer"] == {"name": "engine", "version": AGENT_VERSION, "instance": agent.instance}
        assert payload["seq"] == 1 and payload["intervalS"] == 10
        assert payload["intervalStart"] == BASE_MS and payload["intervalEnd"] == BASE_MS + 10_000
        assert payload["runs"] == {"completed": 1, "failed": 0, "dropped": 1}
        assert payload["detections"]["perClass"] == {"person": {"total": 1, "max": 1}}
        assert payload["people"] == {"seen": 1}
        assert payload["telemetry"] == {"queueDrops": 0, "publishFailures": 0, "eventsDropped": 0}
        assert agent.published == 1 and agent.summaries_published == 1
        assert agent.last_publish_at == payload["emittedAt"]
        # reported through the shadow (the report cadence is one check per second)
        tick_at(agent, clock, BASE + 11.5)
        reported = FakeShadow.instances[-1].reports[-1]
        assert reported["enabled"] is True and reported["usecaseId"] == UC and reported["appliedVersion"] == 1
        assert reported["agentVersion"] == AGENT_VERSION and reported["counters"]["published"] == 1
        assert reported["lastPublishAt"] == payload["emittedAt"]

    def test_heartbeat_for_an_idle_registration(self, world):
        agent, publisher, view, clock = world
        enable(agent, clock)
        for boundary in (10, 20, 30, 40, 50):
            tick_at(agent, clock, BASE + boundary + 0.2)
        assert publisher.published == []
        tick_at(agent, clock, BASE + 60.2)
        assert len(publisher.published) == 1
        topic, payload, qos = publisher.published[0]
        assert topic == SUMMARY_TOPIC and qos == 0 and payload["idle"] is True and payload["intervalS"] == 60

    def test_nothing_is_published_without_a_usecase_id(self, world):
        agent, publisher, view, clock = world
        doc = document()
        del doc["usecaseId"]
        agent._on_desired(doc, 1)
        agent.tick()
        clock.advance(2)
        hooks.notify_run_finished(run_record(clock))
        hooks.notify_run_finished(run_record(clock, "exec-2", status="failed", error="boom"))
        for boundary in (10, 20, 60, 120):
            tick_at(agent, clock, BASE + boundary + 0.2)
        assert publisher.published == []
        assert agent.status()["active"] is False and agent.summarizer.discarded_observations >= 2

    def test_nothing_is_published_while_disabled_and_the_enable_resets(self, world):
        agent, publisher, view, clock = world
        enable(agent, clock, enabled=False)
        for i in range(3):
            clock.advance(1)
            hooks.notify_run_finished(run_record(clock, "disabled-%d" % i))
        tick_at(agent, clock, BASE + 10.2)
        assert publisher.published == []
        clock.set_wall(BASE + 12)
        agent._on_desired(document(enabled=True), 2)
        agent.tick()
        clock.advance(1)
        hooks.notify_run_finished(run_record(clock, "enabled"))
        tick_at(agent, clock, BASE + 20.2)
        assert len(publisher.published) == 1
        payload = publisher.published[0][1]
        assert payload["runs"]["completed"] == 1 and payload["intervalStart"] == BASE_MS + 12_000
        # disable: the window in progress is discarded, seq continues at the next enable
        clock.advance(1)
        hooks.notify_run_finished(run_record(clock, "lost"))
        agent._on_desired(document(enabled=False), 3)
        tick_at(agent, clock, BASE + 30.2)
        tick_at(agent, clock, BASE + 60.2)
        assert len(publisher.published) == 1
        agent._on_desired(document(enabled=True), 4)
        agent.tick()
        clock.advance(1)
        hooks.notify_run_finished(run_record(clock, "again"))
        tick_at(agent, clock, BASE + 70.2)
        assert [p["seq"] for _, p, _ in publisher.published] == [1, 2]

    def test_continuous_status_provider_feeds_the_continuous_block(self, world):
        agent, publisher, view, clock = world
        agent.test_statuses[REG_A] = {"paused": False, "effectiveFps": 4.0}
        enable(agent, clock)
        clock.advance(1)
        hooks.notify_run_finished(run_record(clock))
        hooks.notify_continuous_tick(REG_A, "skippedNoNewFrame", 2)
        hooks.notify_continuous_tick(REG_A, "skippedBusy", 1)
        hooks.notify_model_wait(REG_A, {"model": "plates", "state": "LOADING"})
        hooks.notify_model_wait(REG_A, {"model": "plates", "state": "LOADING"})
        hooks.notify_model_readiness(REG_A, "exec-1", "plates", "READY", 500, True)
        tick_at(agent, clock, BASE + 10.2)
        payload = publisher.published[0][1]
        assert payload["continuous"] == {"skippedNoNewFrame": 2, "paused": False, "effectiveFps": 4.0}
        assert payload["runs"]["dropped"] == 1
        assert payload["model"] == {"state": "LOADING", "models": {"plates": "READY"}, "waits": 2, "waitedMs": 500}

    def test_a_registration_change_wakes_the_summarizer(self, world):
        agent, publisher, view, clock = world
        enable(agent, clock)
        view.rows.clear()
        for callback in view.listeners:
            callback(frozenset(), frozenset({REG_A}))
        tick_at(agent, clock, BASE + 5)
        assert agent.status()["trackedRegistrations"] == []


class TestEvents:
    def test_a_failing_run_produces_a_coalesced_run_error_event_with_qos_1(self, world):
        agent, publisher, view, clock = world
        enable(agent, clock)
        clock.advance(1)
        hooks.notify_run_finished(run_record(clock, "e1", status="failed", failing_node_id="triton-1",
                                             error="Triton rtsp://admin:s3cr3t@cam.example.com failed"))
        agent.tick()
        events = publisher.on("event")
        assert len(events) == 1
        topic, event, qos = events[0]
        assert topic == EVENT_TOPIC and qos == 1
        assert event["schema"] == "dda.event.v1" and event["type"] == "run_error" and event["severity"] == "error"
        uuid.UUID(event["eventId"])
        assert event["deviceId"] == THING and event["workflowId"] == WF_A and event["workflowVersion"] == "3"
        assert event["producer"] == {"name": "engine", "version": AGENT_VERSION, "instance": agent.instance}
        assert event["executionId"] == "e1"
        assert event["data"]["failingNodeId"] == "triton-1" and event["data"]["count"] == 1
        assert "s3cr3t" not in event["data"]["error"] and "rtsp://***@cam.example.com" in event["data"]["error"]
        assert event["ts"] == int(clock.wall * 1000)
        # two more failures of the same key within the window: coalesced into one Event after 10 s
        for i in range(2):
            clock.advance(1)
            hooks.notify_run_finished(run_record(clock, "e%d" % (i + 2), status="failed", failing_node_id="triton-1",
                                                 error="Triton rtsp://admin:s3cr3t@cam.example.com failed"))
            agent.tick()
        assert len(publisher.on("event")) == 1
        clock.advance(8)
        agent.tick()
        events = publisher.on("event")
        assert len(events) == 2 and events[1][1]["data"]["count"] == 2
        assert events[1][1]["data"]["firstTs"] < events[1][1]["data"]["lastTs"]
        assert agent.events_published == 2
        # a Summary at the boundary counts the failures, and the events are not Summaries
        tick_at(agent, clock, BASE + 20.2)
        summaries = publisher.on("summary")
        assert [s[1]["runs"]["failed"] for s in summaries] == [3, 0]  # the [0, 10] and [10, 20] windows

    def test_events_beyond_60_per_minute_are_dropped_and_counted_in_the_next_summary(self, world):
        agent, publisher, view, clock = world
        enable(agent, clock)
        for i in range(70):
            clock.advance(0.05)
            hooks.notify_run_finished(run_record(clock, "e%d" % i, status="failed", failing_node_id="n%d" % i,
                                                 error="distinct error %d" % i))
        agent.tick()
        assert len(publisher.on("event")) == 60
        assert agent.rate_limiter.dropped == 10
        tick_at(agent, clock, BASE + 10.2)
        summary = publisher.on("summary")[-1][1]
        assert summary["telemetry"]["eventsDropped"] == 10 and summary["runs"]["failed"] == 70
        assert agent.status()["counters"]["eventsDropped"] == 10

    def test_failures_while_disabled_produce_no_event(self, world):
        agent, publisher, view, clock = world
        enable(agent, clock, enabled=False)
        clock.advance(1)
        hooks.notify_run_finished(run_record(clock, status="failed", error="boom"))
        agent.tick()
        clock.advance(11)
        agent.tick()
        assert publisher.on("event") == []


class TestLiveFrames:
    def test_a_lease_produces_a_put_then_a_live_publish(self, world, tmp_path):
        agent, publisher, view, clock = world
        enable(agent, clock, live={WF_A: lease_entry()})
        assert sorted(agent.sender.leases()) == [WF_A]
        clock.advance(1)
        hooks.notify_run_finished(run_record(clock, output_dir=frame_dir(tmp_path), capture_id="wf-exec-1",
                                             detections=[{"label": "person"}, {"label": "hat"}]))
        agent.tick()
        s3 = agent.test_s3
        assert len(s3.puts) == 1
        put = s3.puts[0]
        assert put["Bucket"] == "results-bucket" and put["Key"] == PREFIX + "0.jpg"
        assert put["ContentType"] == "image/jpeg" and put["Metadata"] == {"seq": "0"}
        live = publisher.on("live")
        assert len(live) == 1
        topic, payload, qos = live[0]
        assert topic == LIVE_TOPIC and qos == 0
        assert payload["schema"] == "dda.live.v1" and payload["instance"] == agent.instance and payload["seq"] == 0
        assert payload["frame"]["key"] == PREFIX + "0.jpg" and payload["frame"]["width"] == 1280
        assert payload["perClass"] == {"person": 1, "hat": 1} and payload["people"] == 1
        # the PUT happened before the publish (the publisher recorded after the S3 fake)
        assert s3.log == [("put", PREFIX + "0.jpg")] and agent.published == 1
        reported = FakeShadow.instances[-1].reports[-1]
        assert reported["live"][WF_A] == {"activeUntil": BASE_MS + 600_000, "lastSeq": 0, "framesTooLarge": 0}
        assert agent.status()["framesTooLarge"] == 0

    def test_a_removed_lease_stops_the_frames(self, world, tmp_path):
        agent, publisher, view, clock = world
        enable(agent, clock, live={WF_A: lease_entry()})
        agent._on_desired(document(live={}), 2)
        agent.tick()
        clock.advance(1)
        hooks.notify_run_finished(run_record(clock, output_dir=frame_dir(tmp_path), capture_id="wf-exec-1"))
        agent.tick()
        assert agent.test_s3.puts == [] and publisher.on("live") == []

    def test_leases_are_dropped_when_the_agent_is_disabled(self, world, tmp_path):
        agent, publisher, view, clock = world
        enable(agent, clock, live={WF_A: lease_entry()})
        agent._on_desired(document(enabled=False, live={WF_A: lease_entry()}), 2)
        agent.tick()
        assert agent.sender.leases() == {}
        clock.advance(1)
        hooks.notify_run_finished(run_record(clock, output_dir=frame_dir(tmp_path), capture_id="wf-exec-1"))
        agent.tick()
        assert agent.test_s3.puts == [] and publisher.published == []


class TestContainmentAndStatus:
    def test_a_raising_observer_never_propagates(self, world):
        agent, publisher, view, clock = world
        enable(agent, clock)

        def boom(*args):
            raise RuntimeError("observer boom")

        hooks.register_run_observer(boom)
        hooks.register_drop_observer(boom)
        hooks.register_continuous_observer(boom)
        hooks.register_model_observer(boom)
        clock.advance(1)
        hooks.notify_run_finished(run_record(clock))
        hooks.notify_trigger_dropped(REG_A, "t", "queue_full")
        hooks.notify_continuous_tick(REG_A, "skippedBusy", 1)
        hooks.notify_model_readiness(REG_A, "exec-1", "m", "READY", 0, True)
        tick_at(agent, clock, BASE + 10.2)
        summary = publisher.on("summary")[0][1]
        assert summary["runs"] == {"completed": 1, "failed": 0, "dropped": 2}

    def test_garbage_handed_to_the_observers_never_raises(self, world):
        agent, publisher, view, clock = world
        enable(agent, clock)
        hooks.notify_run_finished("not a record")
        hooks.notify_run_finished(None)
        hooks.notify_model_readiness(None, None, None, None, None, None)
        hooks.notify_model_wait(None, "not a dict")
        hooks.notify_continuous_tick(None, "skippedBusy", "x")
        agent._on_desired("not a dict", 2)
        agent._on_desired(None, 3)
        tick_at(agent, clock, BASE + 10.2)  # no exception escapes
        assert agent.ticks_run >= 2

    def test_status_shape(self, world):
        agent, publisher, view, clock = world
        enable(agent, clock, live={WF_A: lease_entry()})
        status = agent.status()
        assert status["instance"] == agent.instance and status["agentVersion"] == AGENT_VERSION
        assert status["started"] is True and status["threadAlive"] is False
        assert status["enabled"] is True and status["usecaseId"] == UC and status["active"] is True
        assert status["trackedRegistrations"] == [REG_A] and status["seq"] == 0
        assert set(status["counters"]) == {"published", "summariesPublished", "eventsPublished", "eventsSuppressed",
                                           "eventsDropped", "queueDrops", "publishFailures"}
        assert status["framesTooLarge"] == 0 and status["lastPublishAt"] is None
        assert status["desired"]["leases"] == [WF_A] and status["live"]["leases"] == [WF_A]
        assert status["summarizer"]["enabled"] is True
        assert status["publisher"] == {"name": "fake"} and status["ticks"] >= 1

    def test_module_level_agent_registry(self, world):
        agent, publisher, view, clock = world
        assert agent_module.get_agent() is None
        agent_module.set_agent(agent)
        try:
            assert agent_module.get_agent() is agent
        finally:
            agent_module.set_agent(None)
        assert agent_module.get_agent() is None

    def test_the_real_threads_run_and_stop(self):
        hooks._clear_observers()
        import time

        agent = TelemetryAgent(thing_name=THING, publisher=FakePublisher(), shadow_factory=FakeShadow,
                               registry_view=FakeView(), secrets=lambda: [])
        agent.start()
        try:
            assert agent._thread.name == "workflow-telemetry-agent" and agent._thread.daemon
            assert agent.summarizer._thread.name == "telemetry-summarizer"
            assert agent.sender._thread.name == "telemetry-live-frames"
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and agent.ticks_run < 2:
                time.sleep(0.02)
            assert agent.ticks_run >= 2
            assert agent.status()["threadAlive"] is True
        finally:
            agent.stop()
            agent._thread.join(5.0)
            agent.summarizer._thread.join(5.0)
            agent.sender._thread.join(5.0)
            hooks._clear_observers()
        assert not agent._thread.is_alive() and not agent.summarizer._thread.is_alive()
        assert not agent.sender._thread.is_alive()
