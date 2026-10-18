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
"""The per-agent ``IpcPublisher`` (workflow-telemetry-dashboards design §5.2
``publisher.py``; workflow-debug-mode tasks.md 1.2).

The thread hands ``bytes`` to ``output_bindings.publish_via_ipc`` with the
default ``retain=False``; ``publish_message`` encodes ``str`` at QoS 0; the
queue drops oldest and counts it; two instances are independent; the thread
survives every exception; the denial shape is logged with the fixed line
once per minute per topic; the spool-full shape is counted and never
re-queued.

Validates: dashboards R5.3, R5.4; debug R7.5, R11.4.
"""
import logging
import threading
import time

import pytest

import awsiot_host_stubs  # noqa: F401 - stands in for the SDK on the host
from awsiot_host_stubs import ServiceError, UnauthorizedError

from workflow_engine import output_bindings
from workflow_engine.telemetry import publisher as publisher_module
from workflow_engine.telemetry.publisher import IpcPublisher, classify_failure


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RecordingIpc:
    """Stands in for ``output_bindings.publish_via_ipc``."""

    def __init__(self, raise_with=None, block=None):
        self.calls = []
        self.raise_with = raise_with
        self.block = block
        self.entered = threading.Event()
        self.lock = threading.Lock()

    def __call__(self, topic, payload, qos, *, retain=False):
        self.entered.set()
        if self.block is not None:
            self.block.wait(5.0)
        with self.lock:
            self.calls.append((topic, payload, qos, retain))
        if self.raise_with is not None:
            error = self.raise_with() if callable(self.raise_with) else self.raise_with
            if error is not None:
                raise error


@pytest.fixture
def ipc(monkeypatch):
    fake = RecordingIpc()
    monkeypatch.setattr(output_bindings, "publish_via_ipc", fake)
    return fake


def _wait(publisher, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _denial():
    """The shape ``publish_via_ipc`` re-raises for a nucleus denial."""
    try:
        raise UnauthorizedError("UnauthorizedError")
    except UnauthorizedError as cause:
        error = RuntimeError(
            "Greengrass IPC denied PublishToIoTCore for topic 'dda/debug/u/w/record': "
            "the LocalServer component's aws.greengrass.ipc.mqttproxy accessControl "
            "configuration does not authorize publishing to this topic."
        )
        error.__cause__ = cause
        return error


class TestHandOver:
    def test_publish_hands_bytes_with_retain_false(self, ipc):
        pub = IpcPublisher("telemetry")
        try:
            pub.publish("dda/telemetry/u/w/summary", b'{"a":1}', 1)
            assert pub._wait_idle()
        finally:
            pub.close()
        assert ipc.calls == [("dda/telemetry/u/w/summary", b'{"a":1}', 1, False)]
        assert isinstance(ipc.calls[0][1], bytes)
        assert pub.publish_successes == 1
        assert pub.publish_failures == 0

    def test_publish_message_encodes_str_at_qos_0(self, ipc):
        pub = IpcPublisher("telemetry")
        try:
            pub.publish_message("$aws/things/t/shadow/name/s/get", "")
            pub.publish_message("$aws/things/t/shadow/name/s/update", '{"state":{}}')
            assert pub._wait_idle()
        finally:
            pub.close()
        assert ipc.calls == [
            ("$aws/things/t/shadow/name/s/get", b"", 0, False),
            ("$aws/things/t/shadow/name/s/update", b'{"state":{}}', 0, False),
        ]

    def test_thread_is_named_after_the_instance_and_is_a_daemon(self, ipc):
        pub = IpcPublisher("workflow-debug")
        try:
            assert pub._thread.name == "workflow-debug-publisher"
            assert pub._thread.daemon
            assert pub._thread.is_alive()
        finally:
            pub.close()


class TestQueue:
    def test_drop_oldest_is_counted(self, monkeypatch):
        gate = threading.Event()
        fake = RecordingIpc(block=gate)
        monkeypatch.setattr(output_bindings, "publish_via_ipc", fake)
        pub = IpcPublisher("telemetry", maxlen=3)
        try:
            # The first message blocks the thread; the next four fill a
            # queue of three, evicting the oldest queued one.
            pub.publish("t", b"0", 0)
            assert fake.entered.wait(5.0)
            for i in range(1, 5):
                pub.publish("t", str(i).encode(), 0)
            assert pub.pending() == 4  # 3 queued + 1 in flight
            assert pub.queue_drops == 1
            gate.set()
            assert pub._wait_idle()
        finally:
            pub.close()
        delivered = [call[1] for call in fake.calls]
        assert delivered[0] == b"0"
        assert delivered[1:] == [b"2", b"3", b"4"]  # b"1" was evicted
        assert pub.status()["queueDrops"] == 1

    def test_two_instances_have_independent_queues_and_threads(self, ipc):
        a = IpcPublisher("telemetry", maxlen=2)
        b = IpcPublisher("workflow-debug", maxlen=2)
        try:
            assert a._thread is not b._thread
            assert a._queue is not b._queue
            a.publish("ta", b"a", 0)
            b.publish("tb", b"b", 1)
            assert a._wait_idle() and b._wait_idle()
        finally:
            a.close()
            b.close()
        assert sorted(ipc.calls) == [("ta", b"a", 0, False), ("tb", b"b", 1, False)]
        assert a.queue_drops == 0 and b.queue_drops == 0

    def test_publish_after_close_is_ignored(self, ipc):
        pub = IpcPublisher("telemetry")
        pub.close()
        pub.publish("t", b"x", 0)
        time.sleep(0.05)
        assert ipc.calls == []


class TestFailures:
    def test_thread_survives_exceptions_and_counts_other(self, monkeypatch, caplog):
        errors = iter([ValueError("first"), None, TimeoutError("second"), None])
        fake = RecordingIpc(raise_with=lambda: next(errors))
        monkeypatch.setattr(output_bindings, "publish_via_ipc", fake)
        pub = IpcPublisher("telemetry", clock=FakeClock())
        try:
            with caplog.at_level(logging.WARNING, logger="workflow_engine.telemetry.publisher"):
                for i in range(4):
                    pub.publish("t", b"%d" % i, 0)
                assert pub._wait_idle()
            assert pub._thread.is_alive()
        finally:
            pub.close()
        assert len(fake.calls) == 4
        assert pub.publish_failures == 2
        assert pub.publish_successes == 2
        assert pub.failures_by_reason == {"denied": 0, "spool_full": 0, "other": 2}

    def test_denial_line_once_per_minute_per_topic(self, monkeypatch, caplog):
        fake = RecordingIpc(raise_with=_denial)
        monkeypatch.setattr(output_bindings, "publish_via_ipc", fake)
        clock = FakeClock()
        pub = IpcPublisher("workflow-debug", clock=clock)
        topic = "dda/debug/u/w/record"
        try:
            with caplog.at_level(logging.ERROR, logger="workflow_engine.telemetry.publisher"):
                for _ in range(3):
                    pub.publish(topic, b"{}", 1)
                assert pub._wait_idle()
                errors = [r for r in caplog.records if r.levelno == logging.ERROR]
                assert len(errors) == 1
                line = errors[0].getMessage()
                assert line.startswith(
                    "publish to dda/debug/u/w/record denied by Greengrass IPC: this "
                    "LocalServer variant's recipe lacks mqttproxy:2 "
                    "(aws.greengrass#PublishToIoTCore on *); known for "
                    "recipe-amd64-nvidia.yaml (A10/Q8). Original diagnosis: "
                )
                assert "aws.greengrass.ipc.mqttproxy accessControl" in line
                assert pub.publish_failures == 3
                assert pub.failures_by_reason["denied"] == 3

                clock.advance(59.0)
                pub.publish(topic, b"{}", 1)
                assert pub._wait_idle()
                assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1

                clock.advance(1.5)
                pub.publish(topic, b"{}", 1)
                assert pub._wait_idle()
                assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 2

                # Another topic has its own cadence.
                pub.publish("dda/debug/u/other/record", b"{}", 1)
                assert pub._wait_idle()
                assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 3
        finally:
            pub.close()
        assert pub.publish_failures == 6

    def test_spool_full_is_counted_not_requeued_and_warned_once(self, monkeypatch, caplog):
        fake = RecordingIpc(raise_with=lambda: ServiceError("spooler full"))
        monkeypatch.setattr(output_bindings, "publish_via_ipc", fake)
        pub = IpcPublisher("workflow-debug", clock=FakeClock())
        try:
            with caplog.at_level(logging.WARNING, logger="workflow_engine.telemetry.publisher"):
                for i in range(3):
                    pub.publish("dda/debug/u/w/record", b"%d" % i, 1)
                assert pub._wait_idle()
            assert pub._thread.is_alive()
        finally:
            pub.close()
        assert len(fake.calls) == 3  # one attempt each: nothing re-queued
        assert pub.pending() == 0
        assert pub.failures_by_reason["spool_full"] == 3
        assert pub.publish_failures == 3
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "spooler" in warnings[0].getMessage()
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 0

    def test_classify_failure_shapes(self):
        assert classify_failure(_denial()) == "denied"
        assert classify_failure(ServiceError("full")) == "spool_full"
        assert classify_failure(RuntimeError("plain")) == "other"
        assert classify_failure(UnauthorizedError("bare")) == "other"
        wrapped = RuntimeError("wrapped")
        wrapped.__cause__ = ServiceError("full")
        assert classify_failure(wrapped) == "spool_full"

    def test_status_reports_counters(self, ipc):
        pub = IpcPublisher("telemetry")
        try:
            pub.publish("t", b"x", 0)
            assert pub._wait_idle()
            status = pub.status()
        finally:
            pub.close()
        assert status["name"] == "telemetry"
        assert status["publishSuccesses"] == 1
        assert status["failuresByReason"] == {"denied": 0, "spool_full": 0, "other": 0}
        assert status["threadAlive"] is True


def test_module_surface():
    assert publisher_module.__all__ == ["IpcPublisher"]
