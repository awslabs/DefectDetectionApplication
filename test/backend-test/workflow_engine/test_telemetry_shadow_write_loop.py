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
"""The ``dda-workflow-telemetry`` reported writes stay bounded
(workflow-telemetry-dashboards design §5.4).

The real ``TelemetryAgent``, ``ShadowControlAgent`` and
``TelemetryDesiredState`` run against a fake shadow service that keeps a
version counter: every ``.../update`` publish bumps the version and delivers
the ``update/documents`` echo (``desired`` unchanged, the new version) back
through ``handle_message``, as the shadow service does for the agent's own
``reported`` writes. Applying such an echo must not cause another write;
otherwise every write produces the next one.

Bounded means one write for the initial apply, one per genuine desired
change and at most one per ``REPORT_INTERVAL_S`` period. A genuine change
(``summaryIntervalS`` 10 -> 20) is still written at the next report check,
and a live lease renewed every 20 s produces at most one write per renewal.
"""
import json
import logging

import pytest

import awsiot_host_stubs  # noqa: F401 - parity with the other agent tests; the fakes need no SDK
from workflow_engine.telemetry import hooks
from workflow_engine.telemetry.agent import REPORT_COUNTER_KEYS, SHADOW_NAME, TICK_S, TelemetryAgent
from workflow_engine.telemetry.control_shadow import REPORT_INTERVAL_S, ShadowControlAgent
from workflow_engine.telemetry.desired_state import TelemetryDesiredState
from workflow_engine.telemetry.registry_view import RegistrationInfo

THING = "thing-1"
UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF_A = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
REG_A = "reg-a:3"
#: Aligned to the 10 s and 60 s grids.
BASE = 1_792_075_200.0
BASE_MS = int(BASE * 1000)
PREFIX = "dda-telemetry/live/%s/%s/%s/" % (UC, THING, WF_A)
TOPIC_PREFIX = "$aws/things/%s/shadow/name/%s/" % (THING, SHADOW_NAME)
#: 11 minutes: crosses two REPORT_INTERVAL_S periods.
RUN_S = 660.0


class FakeClock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = BASE

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds


class FakeShadowService:
    """The named shadow and the agent's publisher in one: ``publish`` takes the
    Summaries, ``publish_message`` the shadow ``get`` and ``update`` requests,
    answered at once through the control agent's ``handle_message``."""

    def __init__(self, desired):
        self.version = 1
        self.desired = dict(desired)
        self.reported = {}
        self.writes = []  # (wall seconds, version created, reported document)
        self.published = []
        self.queue_drops = 0
        self.publish_failures = 0
        self.control = None

    # the agent's IpcPublisher surface
    def publish(self, topic, payload, qos):
        self.published.append((topic, json.loads(payload), qos))

    def publish_message(self, topic, payload):
        assert topic.startswith(TOPIC_PREFIX)
        subtopic = topic[len(TOPIC_PREFIX):]
        if subtopic == "get":
            self._deliver("get/accepted", {"state": {"desired": self.desired, "reported": self.reported},
                                           "version": self.version})
        elif subtopic == "update":
            document = json.loads(payload)
            reported = document["state"]["reported"]
            self.reported = reported
            self._bump()
            self.writes.append((self.clock.wall, self.version, reported))

    # the Portal side
    def write_desired(self, desired):
        self.desired = dict(desired)
        self._bump()

    def _bump(self):
        previous = self.version
        self.version += 1
        state = {"desired": self.desired, "reported": self.reported}
        self._deliver("update/documents", {"previous": {"state": state, "version": previous},
                                           "current": {"state": state, "version": self.version}})

    def _deliver(self, subtopic, document):
        self.control.handle_message(TOPIC_PREFIX + subtopic, json.dumps(document).encode("utf-8"))


class FakeSubscription:
    def __init__(self, topic_prefix, handler, publisher, on_active=None):
        self.on_active = on_active

    def subscribe(self):
        return None

    def close(self):
        return None


class FakeView:
    def __init__(self):
        self.rows = {REG_A: RegistrationInfo(WF_A, "3", (), False)}

    def snapshot(self):
        return dict(self.rows)

    def add_listener(self, callback):
        pass

    def remove_listener(self, callback):
        pass


def document(**overrides):
    doc = {"v": 1, "usecaseId": UC, "enabled": True, "summaryIntervalS": 10, "idleHeartbeatS": 60,
           "personClasses": ["person", "human"], "live": {}, "updatedAt": BASE_MS}
    doc.update(overrides)
    return doc


def lease_entry(expires_at):
    return {"expiresAt": expires_at, "maxFps": 1, "maxWidth": 1280, "maxHeight": 720, "maxBytes": 307200,
            "bucket": "results-bucket", "region": "eu-west-1", "prefix": PREFIX}


@pytest.fixture
def world():
    hooks._clear_observers()
    clock = FakeClock()
    service = FakeShadowService(document())
    service.clock = clock

    def shadow_factory(*args, **kwargs):
        control = ShadowControlAgent(*args, subscription_factory=FakeSubscription, clock=lambda: clock.mono,
                                     **kwargs)
        service.control = control
        return control

    agent = TelemetryAgent(
        thing_name=THING, publisher=service, shadow_factory=shadow_factory, registry_view=FakeView(),
        client_factory=lambda region: None, status_provider=lambda rid: None, secrets=lambda: [],
        clock=lambda: clock.mono, wall=lambda: clock.wall, run_thread=False,
    )
    agent.start()
    agent.shadow.on_active()  # the subscription's catch-up get
    yield agent, service, clock
    agent.stop()
    hooks._clear_observers()


def run_for(agent, clock, seconds, every=None):
    """Tick every ``TICK_S`` for ``seconds``; ``every`` is ``(period_s, callback)``."""
    steps = int(round(seconds / TICK_S))
    for step in range(steps):
        clock.advance(TICK_S)
        if every is not None and step and (step * TICK_S) % every[0] == 0:
            every[1]()
        agent.tick()


def writes_between(service, start, end):
    return [w for w in service.writes if start <= w[0] < end]


def periodic_bound(seconds):
    return int(seconds // REPORT_INTERVAL_S) + 1


class TestReportedWritesStayBounded:
    def test_echoes_of_the_agents_own_writes_cause_no_further_writes(self, world, caplog):
        agent, service, clock = world
        caplog.set_level(logging.INFO, logger="workflow_engine.telemetry.desired_state")
        start = clock.wall
        run_for(agent, clock, RUN_S)
        writes = len(service.writes)
        # one write for the initial apply plus at most one per REPORT_INTERVAL_S period
        assert writes <= 1 + periodic_bound(RUN_S), (
            "%d reported writes in %.0f s (shadow version %d)" % (writes, RUN_S, service.version))
        assert writes >= 1 + int(RUN_S // REPORT_INTERVAL_S), "the periodic counter writes still happen"
        first = service.writes[0][2]
        assert first["enabled"] is True and first["usecaseId"] == UC and first["appliedVersion"] == 1
        assert first["summaryIntervalS"] == 10 and first["idleHeartbeatS"] == 60
        # the periodic writes carry fresh counters
        assert service.writes[-1][2]["counters"]["published"] > first["counters"]["published"]
        assert service.writes[-1][0] - start >= REPORT_INTERVAL_S
        applied = [r for r in caplog.records if "applied" in r.getMessage() and r.levelno >= logging.INFO]
        assert len(applied) == 1, [r.getMessage() for r in applied]
        # the echoes were seen and recorded, not re-applied
        assert agent.shadow.last_version == service.version
        status = agent.state.status()
        assert status["appliedDocuments"] == 1 and status["appliedVersion"] == 1
        assert status["lastVersion"] == service.version

    def test_echoes_do_not_reconfigure_the_summarizer_or_the_sender(self, world, monkeypatch):
        agent, service, clock = world
        run_for(agent, clock, 1.0)
        calls = []
        monkeypatch.setattr(agent.summarizer, "configure", lambda settings: calls.append(("configure", settings)))
        monkeypatch.setattr(agent.sender, "set_leases", lambda leases: calls.append(("set_leases", leases)))
        run_for(agent, clock, 2 * REPORT_INTERVAL_S + 5)
        assert len(service.writes) >= 2  # at least one periodic write and its echo
        assert calls == []

    def test_a_genuine_desired_change_is_written_promptly(self, world):
        agent, service, clock = world
        run_for(agent, clock, 30.0)
        before = len(service.writes)
        service.write_desired(document(summaryIntervalS=20, updatedAt=BASE_MS + 30_000))
        portal_version = service.version
        changed_at = clock.wall
        run_for(agent, clock, 2.0)
        after = writes_between(service, changed_at, clock.wall)
        assert len(after) == 1, after
        reported = after[0][2]
        assert reported["summaryIntervalS"] == 20 and reported["appliedVersion"] == portal_version
        assert agent.summarizer.settings.summary_interval_s == 20
        run_for(agent, clock, RUN_S)
        # one for the change, then at most the periodic writes
        assert len(service.writes) - before <= 1 + periodic_bound(RUN_S + 2.0)

    def test_live_lease_renewals_give_at_most_one_write_per_renewal(self, world):
        agent, service, clock = world
        run_for(agent, clock, 5.0)
        before = len(service.writes)
        renewals = []

        def renew():
            expires = int(clock.wall * 1000) + 60_000
            service.write_desired(document(live={WF_A: lease_entry(expires)}))
            renewals.append(expires)

        renew()
        run_for(agent, clock, 180.0, every=(20.0, renew))
        writes = service.writes[before:]
        assert len(renewals) == 9
        assert len(writes) <= len(renewals) + periodic_bound(180.0), (len(writes), len(renewals))
        # every renewal is reported with its expiry, each by exactly one write
        reported_expiries = [w[2]["live"][WF_A]["activeUntil"] for w in writes if WF_A in w[2]["live"]]
        assert sorted(set(reported_expiries)) == renewals
        assert len(reported_expiries) == len(set(reported_expiries))
        # the echo of the last renewal's write was applied without another write
        assert agent.shadow.last_version == service.version and agent.state.status()["lastVersion"] == service.version

    def test_an_unchanged_document_is_applied_again_once_a_lease_lapsed(self):
        wall = {"now": BASE}
        changes = []
        state = TelemetryDesiredState(THING, wall=lambda: wall["now"], on_change=changes.append)
        doc = document(live={WF_A: lease_entry(BASE_MS + 60_000)})
        assert state.apply(doc, 1) is True and len(changes) == 1 and list(state.leases) == [WF_A]
        assert state.apply(dict(doc), 2) is True and len(changes) == 1  # the echo: recorded only
        assert state.applied_version == 1 and state.last_version == 2
        wall["now"] += 61
        state.apply(dict(doc), 3)  # the lease lapsed: a full apply drops it
        assert len(changes) == 2 and state.leases == {} and state.applied_version == 3
        state.apply(dict(doc), 4)
        assert len(changes) == 2 and state.last_version == 4

    def test_the_counter_keys_are_stripped_at_the_top_level_and_nested(self):
        sent = []

        class Publisher:
            def publish_message(self, topic, payload):
                sent.append(payload)

        clock = {"now": 1000.0}
        control = ShadowControlAgent(THING, SHADOW_NAME, lambda d, v: None, Publisher(),
                                     subscription_factory=FakeSubscription, clock=lambda: clock["now"],
                                     counter_keys=REPORT_COUNTER_KEYS)

        def state(applied_version, last_seq, applied_documents=1):
            return {"v": 1, "enabled": True, "appliedVersion": applied_version, "appliedDocuments": applied_documents,
                    "ignoredDocuments": 0, "counters": {"published": applied_version},
                    "live": {WF_A: {"activeUntil": BASE_MS, "lastSeq": last_seq, "framesTooLarge": 0}}}

        assert control.report(state(1, None)) is True
        clock["now"] += 1
        assert control.report(state(7, 3, applied_documents=4)) is False  # only counter keys moved
        clock["now"] += 1
        changed = state(8, 4)
        changed["live"][WF_A]["activeUntil"] = BASE_MS + 20_000
        assert control.report(changed) is True  # a lease expiry is a real change
        assert len(sent) == 2

    def test_the_fake_service_reproduces_the_loop_without_the_counter_keys(self, monkeypatch):
        """The harness itself: with ``appliedVersion`` counted as a change
        (the counter keys of 1.0.57) and the echo re-applied, every write
        produces the next one. Guards the harness against silently not
        delivering echoes."""
        hooks._clear_observers()
        clock = FakeClock()
        service = FakeShadowService(document())
        service.clock = clock

        def shadow_factory(*args, **kwargs):
            kwargs["counter_keys"] = ("counters", "lastPublishAt", "lastSeq", "framesTooLarge")
            control = ShadowControlAgent(*args, subscription_factory=FakeSubscription, clock=lambda: clock.mono,
                                         **kwargs)
            service.control = control
            return control

        agent = TelemetryAgent(
            thing_name=THING, publisher=service, shadow_factory=shadow_factory, registry_view=FakeView(),
            client_factory=lambda region: None, status_provider=lambda rid: None, secrets=lambda: [],
            clock=lambda: clock.mono, wall=lambda: clock.wall, run_thread=False,
        )
        agent.start()
        # re-apply every echo as a change, as 1.0.57 did
        original = agent.state.apply

        def reapply(desired, version):
            agent.state._last_document = None
            return original(desired, version)

        monkeypatch.setattr(agent.state, "apply", reapply)
        try:
            agent.shadow.on_active()
            run_for(agent, clock, 60.0)
            assert len(service.writes) > 100
        finally:
            agent.stop()
            hooks._clear_observers()
