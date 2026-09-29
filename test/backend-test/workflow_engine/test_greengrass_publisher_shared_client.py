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

These tests drive the real ``utils.ipc_client`` cache and the real awsiot
request model, with only ``connect`` and the IPC client faked:

- any number of publishes open one connection;
- a publish that fails for a broken connection reconnects once and retries;
- a denial neither reconnects nor retries, and keeps its diagnosis.
"""
import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

awsiot_model = pytest.importorskip("awsiot.greengrasscoreipc.model")

from unittest.mock import patch  # noqa: E402

import utils.ipc_client as shared_ipc  # noqa: E402
from workflow_engine.output_bindings import _default_greengrass_publisher  # noqa: E402

TOPIC = "factory/line1/events"


class FakeIpc:
    """``awsiot.greengrasscoreipc.connect`` and the clients it returns.
    ``failures`` lists, per publish attempt in order, the exception its
    result raises (None: it succeeds)."""

    def __init__(self, failures=()):
        self.connects = 0
        self.published = []  # (client number, topic, payload)
        self.failures = list(failures)

    def connect(self):
        self.connects += 1
        return FakeClient(self, self.connects)


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
    with patch.object(shared_ipc, "_client", None), \
            patch.object(shared_ipc.awsiot.greengrasscoreipc, "connect", fake.connect):
        yield fake


def test_many_publishes_open_one_connection(ipc):
    for index in range(50):
        _default_greengrass_publisher(TOPIC, f"message {index}", 1)
    assert ipc.connects == 1
    assert [entry[0] for entry in ipc.published] == [1] * 50
    assert ipc.published[-1] == (1, TOPIC, b"message 49")


def test_the_publisher_shares_the_process_wide_client(ipc):
    """Other IPC callers and the publisher use one connection."""
    first = shared_ipc.get_ipc_client()
    _default_greengrass_publisher(TOPIC, "payload", 0)
    assert ipc.connects == 1 and shared_ipc.get_ipc_client() is first


def test_a_broken_connection_reconnects_once_and_retries(ipc):
    _default_greengrass_publisher(TOPIC, "before", 1)
    ipc.failures = [ConnectionError("event stream connection closed")]
    _default_greengrass_publisher(TOPIC, "after", 1)
    assert ipc.connects == 2
    assert ipc.published == [(1, TOPIC, b"before"), (2, TOPIC, b"after")]
    _default_greengrass_publisher(TOPIC, "next", 1)
    assert ipc.connects == 2 and ipc.published[-1] == (2, TOPIC, b"next")


def test_a_second_failure_propagates(ipc):
    ipc.failures = [ConnectionError("closed"), ConnectionError("still closed")]
    with pytest.raises(ConnectionError, match="still closed"):
        _default_greengrass_publisher(TOPIC, "payload", 1)
    assert ipc.connects == 2 and ipc.published == []


def test_a_denial_neither_reconnects_nor_retries(ipc):
    ipc.failures = [awsiot_model.UnauthorizedError(message="denied")]
    with pytest.raises(RuntimeError) as denied:
        _default_greengrass_publisher(TOPIC, "payload", 1)
    assert TOPIC in str(denied.value) and "aws.greengrass.ipc.mqttproxy" in str(denied.value)
    assert ipc.connects == 1 and ipc.published == []
    _default_greengrass_publisher(TOPIC, "payload", 1)
    assert ipc.connects == 1, "a denial must not drop the shared connection"
