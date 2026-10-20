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
"""The named-shadow control reader (workflow-telemetry-dashboards design §5.2
``control_shadow.py``; workflow-debug-mode tasks.md 1.3).

Only complete documents are applied (``get/accepted`` and
``update/documents``), in version order; a delta triggers one coalesced
``get``; the agent's own echoes and the accepted/rejected answers change
nothing; ``report`` writes at once on a non-counter change and otherwise at
most once per 5 minutes.

Validates: dashboards R5.6; debug R5.2, R5.4, R12.3.
"""
import json

import pytest

from workflow_engine.telemetry import control_shadow
from workflow_engine.telemetry.control_shadow import ShadowControlAgent

THING = "thing-1"
SHADOW = "dda-workflow-debug"
PREFIX = "$aws/things/thing-1/shadow/name/dda-workflow-debug/"


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakePublisher:
    def __init__(self):
        self.messages = []

    def publish_message(self, topic, payload):
        self.messages.append((topic, payload))

    def gets(self):
        return [m for m in self.messages if m[0] == PREFIX + "get"]

    def updates(self):
        return [json.loads(m[1]) for m in self.messages if m[0] == PREFIX + "update"]


class FakeSubscription:
    """Stands in for ``mqtt.SubscriptionHandler``: records its wiring and
    runs the catch-up when ``subscribe()`` is called."""

    instances = []

    def __init__(self, topic_prefix, handler, publish_handler, *, on_active=None):
        self.topic_prefix = topic_prefix
        self.handler = handler
        self.publish_handler = publish_handler
        self.on_active = on_active
        self.closed = False
        FakeSubscription.instances.append(self)

    def subscribe(self):
        if self.on_active is not None:
            self.on_active()

    def close(self):
        self.closed = True


class FakeEvent:
    def __init__(self, topic, payload):
        class _Message:
            pass

        self.message = _Message()
        self.message.topic_name = topic
        self.message.payload = payload


@pytest.fixture
def agent():
    calls = []
    publisher = FakePublisher()
    clock = FakeClock()
    instance = ShadowControlAgent(
        THING, SHADOW, lambda desired, version: calls.append((dict(desired), version)),
        publisher, subscription_factory=FakeSubscription, clock=clock,
    )
    instance.test_calls = calls
    instance.test_publisher = publisher
    instance.test_clock = clock
    return instance


def _get_accepted(desired, version):
    return PREFIX + "get/accepted", json.dumps(
        {"state": {"desired": desired, "reported": {}}, "version": version}
    ).encode("utf-8")


def _documents(desired, version):
    return PREFIX + "update/documents", json.dumps({
        "previous": {"state": {}, "version": version - 1},
        "current": {"state": {"desired": desired}, "version": version},
    }).encode("utf-8")


def _delta(desired, version):
    return PREFIX + "update/delta", json.dumps(
        {"state": desired, "version": version}
    ).encode("utf-8")


A = {"sessionId": "a", "level": "info"}
B = {"sessionId": "b", "level": "debug"}


class TestCompleteDocuments:
    def test_topic_prefix(self, agent):
        assert agent.topic_prefix == PREFIX

    def test_get_accepted_then_documents_without_a_removes_a(self, agent):
        agent.handle_message(*_get_accepted({"sessions": {"A": A, "B": B}}, 7))
        agent.handle_message(*_documents({"sessions": {"B": B}}, 8))
        assert agent.test_calls == [
            ({"sessions": {"A": A, "B": B}}, 7),
            ({"sessions": {"B": B}}, 8),
        ]
        assert agent.applied_documents == 2

    def test_delta_with_only_b_removes_nothing_and_requests_exactly_one_get(self, agent):
        agent.handle_message(*_get_accepted({"sessions": {"A": A, "B": B}}, 7))
        agent.handle_message(*_delta({"sessions": {"B": B}}, 8))
        agent.handle_message(*_delta({"sessions": {"B": B}}, 9))
        assert agent.test_calls == [({"sessions": {"A": A, "B": B}}, 7)]
        assert len(agent.test_publisher.gets()) == 1
        assert agent.test_publisher.gets()[0] == (PREFIX + "get", "")

    def test_a_delta_after_the_get_answer_requests_again(self, agent):
        agent.handle_message(*_delta({"x": 1}, 3))
        assert len(agent.test_publisher.gets()) == 1
        agent.handle_message(*_get_accepted({"x": 1}, 3))
        agent.handle_message(*_delta({"x": 2}, 4))
        assert len(agent.test_publisher.gets()) == 2

    def test_a_lost_get_answer_lets_a_later_delta_request_again(self, agent):
        agent.handle_message(*_delta({"x": 1}, 3))
        agent.handle_message(*_delta({"x": 1}, 4))
        assert len(agent.test_publisher.gets()) == 1
        agent.test_clock.advance(control_shadow.GET_IN_FLIGHT_S + 1)
        agent.handle_message(*_delta({"x": 1}, 5))
        assert len(agent.test_publisher.gets()) == 2

    def test_lower_or_equal_version_is_ignored(self, agent):
        agent.handle_message(*_get_accepted({"v": "new"}, 10))
        agent.handle_message(*_documents({"v": "old"}, 9))
        agent.handle_message(*_documents({"v": "same"}, 10))
        agent.handle_message(*_get_accepted({"v": "older"}, 2))
        assert agent.test_calls == [({"v": "new"}, 10)]
        assert agent.last_version == 10

    def test_a_document_without_desired_applies_an_empty_map(self, agent):
        topic = PREFIX + "get/accepted"
        payload = json.dumps({"state": {"reported": {"x": 1}}, "version": 4}).encode()
        agent.handle_message(topic, payload)
        assert agent.test_calls == [({}, 4)]

    def test_echoes_and_answers_change_nothing(self, agent):
        agent.handle_message(*_get_accepted({"a": 1}, 1))
        for subtopic, payload in (
            ("get", b""),
            ("update", json.dumps({"state": {"reported": {"a": 2}}}).encode()),
            ("update/accepted", json.dumps({"state": {"reported": {}}, "version": 2}).encode()),
            ("update/rejected", json.dumps({"code": 400, "message": "bad"}).encode()),
            ("get/rejected", json.dumps({"code": 404, "message": "none"}).encode()),
        ):
            agent.handle_message(PREFIX + subtopic, payload)
        assert agent.test_calls == [({"a": 1}, 1)]
        assert agent.test_publisher.gets() == []

    def test_malformed_payloads_and_foreign_topics_are_ignored(self, agent):
        agent.handle_message(PREFIX + "get/accepted", b"not json")
        agent.handle_message(PREFIX + "update/documents", b"[1, 2]")
        agent.handle_message(PREFIX + "get/accepted",
                             json.dumps({"state": {"desired": {}}, "version": "7"}).encode())
        agent.handle_message("$aws/things/other/shadow/name/x/get/accepted", b"{}")
        agent.handle_message(None, b"{}")
        assert agent.test_calls == []

    def test_a_raising_on_desired_is_contained(self):
        def boom(desired, version):
            raise RuntimeError("boom")

        agent = ShadowControlAgent(THING, SHADOW, boom, FakePublisher(),
                                   subscription_factory=FakeSubscription)
        agent.handle_message(*_get_accepted({"a": 1}, 1))  # must not raise
        assert agent.last_version == 1

    def test_stream_handler_forwards_events(self, agent):
        handler = agent._stream_handler()
        handler.on_stream_event(FakeEvent(*_get_accepted({"a": 1}, 1)))
        assert agent.test_calls == [({"a": 1}, 1)]
        assert handler.on_stream_error(RuntimeError("x")) is True
        handler.on_stream_closed()


class TestLifecycle:
    def test_start_wires_the_subscription_and_the_catch_up_requests_a_get(self, agent):
        FakeSubscription.instances.clear()
        agent.start()
        agent._thread.join(5.0)
        subscription = FakeSubscription.instances[-1]
        assert subscription.topic_prefix == PREFIX
        assert subscription.publish_handler is agent.test_publisher
        assert subscription.on_active == agent.on_active
        assert agent.test_publisher.gets() == [(PREFIX + "get", "")]
        agent.close()
        assert subscription.closed

    def test_on_active_returns_done(self, agent):
        assert agent.on_active() is True


class TestReport:
    def test_first_report_writes_and_counter_only_changes_wait_five_minutes(self, agent):
        state = {"v": 1, "sessions": {"wf": {"state": "active", "counters": {"records": 1}}}}
        assert agent.report(state) is True
        state["sessions"]["wf"]["counters"]["records"] = 2
        assert agent.report(state) is False
        agent.test_clock.advance(299.0)
        state["sessions"]["wf"]["counters"]["records"] = 3
        assert agent.report(state) is False
        agent.test_clock.advance(1.0)
        assert agent.report(state) is True
        updates = agent.test_publisher.updates()
        assert len(updates) == 2
        assert updates[0] == {"state": {"reported": {
            "v": 1, "sessions": {"wf": {"state": "active", "counters": {"records": 1}}}}}}
        assert updates[1]["state"]["reported"]["sessions"]["wf"]["counters"] == {"records": 3}
        assert agent.report_writes == 2

    def test_non_counter_change_writes_at_once(self, agent):
        state = {"sessions": {"wf": {"state": "pending", "counters": {"records": 0}}}}
        assert agent.report(state) is True
        state["sessions"]["wf"]["state"] = "active"
        assert agent.report(state) is True
        state["sessions"]["wf"]["counters"]["records"] = 5
        assert agent.report(state) is False
        state["lastPublishAt"] = 123
        assert agent.report(state) is False
        state["sessions"]["other"] = {"state": "pending"}
        assert agent.report(state) is True
        assert len(agent.test_publisher.updates()) == 3

    def test_identical_state_is_not_rewritten_before_the_interval(self, agent):
        state = {"a": 1}
        assert agent.report(state) is True
        assert agent.report(state) is False
        assert agent.report(dict(state)) is False

    def test_oversized_report_is_not_written(self, agent):
        state = {"blob": "x" * 5000}
        assert agent.report(state) is False
        assert agent.test_publisher.updates() == []
        assert agent.report_skipped_too_large == 1

    def test_report_payload_shape(self, agent):
        agent.report({"v": 1, "instance": "i"})
        topic, payload = agent.test_publisher.messages[-1]
        assert topic == PREFIX + "update"
        assert json.loads(payload) == {"state": {"reported": {"v": 1, "instance": "i"}}}
        assert len(payload.encode("utf-8")) <= control_shadow.REPORT_MAX_BYTES

    def test_non_dict_state_is_refused(self, agent):
        assert agent.report("nope") is False  # type: ignore[arg-type]


def test_module_surface():
    assert control_shadow.__all__ == ["ShadowControlAgent"]
