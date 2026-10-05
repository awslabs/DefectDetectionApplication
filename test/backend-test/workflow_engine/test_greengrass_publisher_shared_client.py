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
"""The Greengrass MQTT publisher reuses the shared IPC client
(rtsp-rtmp-stream-cameras hardware finding, task 25.3).

``_default_greengrass_publisher`` used to open a new Greengrass IPC
connection for every message and never close it. Each connection kept its
``AwsEventLoop`` thread and buffers for the life of the process: on a JP5
device, a continuous workflow whose event gate published every few seconds
left 2,700 such threads, and about 175 KB of resident memory per message,
in 12 hours.

These tests drive the real ``utils.ipc_client`` and the real awsiot request
model, with only ``connect``, the reconnect factory ``_open_connection`` and
the IPC client faked (finding 24):

- any number of publishes open one connection;
- a publish whose connection closed (``ConnectionClosedError``) is retried
  once, on the shared reconnect;
- a denial neither reconnects nor retries, and keeps its diagnosis;
- a timeout neither reconnects nor retries.
"""
import concurrent.futures
import threading
from concurrent.futures import Future

import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

awsiot_model = pytest.importorskip("awsiot.greengrasscoreipc.model")

from unittest.mock import patch  # noqa: E402

from awsiot.eventstreamrpc import ConnectionClosedError  # noqa: E402

import utils.ipc_client as shared_ipc  # noqa: E402
from workflow_engine.output_bindings import _default_greengrass_publisher  # noqa: E402

TOPIC = "factory/line1/events"
#: The reconnect timing, in milliseconds.
FAST_TIMINGS = {"CONNECT_TIMEOUT_S": 0.5, "WATCHDOG_INTERVAL_S": 0.02,
                "RECONNECT_BACKOFF_S": (0.01, 0.02, 0.04, 0.08, 0.1), "RETRY_WAIT_S": 2.0}


class FakeIpc:
    """``awsiot.greengrasscoreipc.connect``, the reconnect factory and the
    clients they return, numbered by connect. ``failures`` lists, per publish
    attempt in order, the exception its result raises (None: it succeeds)."""

    def __init__(self, failures=()):
        self.connects = 0
        self.published = []  # (client number, topic, payload)
        self.failures = list(failures)
        self._lock = threading.Lock()

    def _new_client(self):
        with self._lock:
            self.connects += 1
            return FakeClient(self, self.connects)

    def connect(self, **kwargs):
        """The first connect (``lifecycle_handler`` and ``timeout``)."""
        return self._new_client()

    def open_connection(self, lifecycle):
        """A reconnect attempt, acknowledged at once."""
        connected = Future()
        connected.set_result(None)
        return self._new_client(), connected


class FakeClient:
    def __init__(self, ipc, number):
        self.ipc, self.number = ipc, number

    def new_publish_to_iot_core(self):
        return FakeOperation(self)


class FakeOperation:
    def __init__(self, client):
        self.client = client
        self.request = None

    def activate(self, request):
        self.request = request

    def get_response(self):
        return self

    def result(self, timeout=None):
        ipc = self.client.ipc
        failure = ipc.failures.pop(0) if ipc.failures else None
        if failure is not None:
            raise failure
        ipc.published.append((self.client.number, self.request.topic_name, self.request.payload))


@pytest.fixture
def ipc():
    fake = FakeIpc()
    with patch.multiple(shared_ipc, **FAST_TIMINGS), \
            patch.object(shared_ipc.awsiot.greengrasscoreipc, "connect", fake.connect), \
            patch.object(shared_ipc, "_open_connection", fake.open_connection):
        shared_ipc.reset_ipc_client()
        try:
            yield fake
        finally:
            shared_ipc.reset_ipc_client()


def test_many_publishes_open_one_connection(ipc):
    for index in range(50):
        _default_greengrass_publisher(TOPIC, f"message {index}", 1)
    assert ipc.connects == 1
    assert [entry[0] for entry in ipc.published] == [1] * 50
    assert ipc.published[-1] == (1, TOPIC, b"message 49")


def test_the_publisher_shares_the_process_wide_client(ipc):
    """Other IPC callers and the publisher use one connection."""
    first = shared_ipc.get_ipc_client()
    client = shared_ipc.current_client()
    _default_greengrass_publisher(TOPIC, "payload", 0)
    assert ipc.connects == 1 and shared_ipc.get_ipc_client() is first
    assert shared_ipc.current_client() is client and ipc.published == [(client.number, TOPIC, b"payload")]


def test_a_broken_connection_reconnects_once_and_retries(ipc):
    _default_greengrass_publisher(TOPIC, "before", 1)
    ipc.failures = [ConnectionClosedError()]
    _default_greengrass_publisher(TOPIC, "after", 1)
    assert ipc.connects == 2
    assert ipc.published == [(1, TOPIC, b"before"), (2, TOPIC, b"after")]
    _default_greengrass_publisher(TOPIC, "next", 1)
    assert ipc.connects == 2 and ipc.published[-1] == (2, TOPIC, b"next")


def test_a_second_failure_propagates(ipc):
    shared_ipc.get_ipc_client()
    g0 = shared_ipc.generation()
    ipc.failures = [ConnectionClosedError("closed"), ConnectionClosedError("still closed")]
    with pytest.raises(ConnectionClosedError, match="still closed"):
        _default_greengrass_publisher(TOPIC, "payload", 1)
    # The retry's ConnectionClosedError is reported too: a second reconnect.
    assert shared_ipc.wait_for_new_connection(g0 + 1, 1.0)
    assert shared_ipc.generation() == g0 + 2
    assert ipc.connects == 3 and ipc.published == []


def test_a_denial_neither_reconnects_nor_retries(ipc):
    ipc.failures = [awsiot_model.UnauthorizedError(message="denied")]
    with pytest.raises(RuntimeError) as denied:
        _default_greengrass_publisher(TOPIC, "payload", 1)
    assert TOPIC in str(denied.value) and "aws.greengrass.ipc.mqttproxy" in str(denied.value)
    assert ipc.connects == 1 and ipc.published == []
    _default_greengrass_publisher(TOPIC, "payload", 1)
    assert ipc.connects == 1, "a denial must not drop the shared connection"


def test_a_timeout_neither_reconnects_nor_retries(ipc):
    """A timeout says nothing about the connection (R5): it propagates after
    one attempt, and the next publish uses the same connection."""
    ipc.failures = [concurrent.futures.TimeoutError()]
    with pytest.raises(concurrent.futures.TimeoutError):
        _default_greengrass_publisher(TOPIC, "payload", 1)
    assert ipc.connects == 1 and ipc.published == []
    assert shared_ipc.connection_usable()
    _default_greengrass_publisher(TOPIC, "next", 1)
    assert ipc.connects == 1 and ipc.published == [(1, TOPIC, b"next")]
