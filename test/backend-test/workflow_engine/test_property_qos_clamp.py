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
"""Property test for the Greengrass subscribe QoS clamp (Task 7.4).

# Feature: trigger-activation-runtime, Property 14: Greengrass subscribe QoS clamp

*For any configured `qos` value, the Greengrass IPC subscribe request
carries `min(qos, 1)`, mirroring the publish path's clamp.*

The ``awsiot`` Greengrass IPC boundary is faked in ``sys.modules`` (the
established stubbing pattern in this test tree — the worker imports the
SDK lazily inside ``_subscribe``), and the worker's injectable
``ipc_connect`` seam hands back a recording IPC client, so the exact
``SubscribeToIoTCoreRequest`` the worker activates is observable with no
SDK and no nucleus socket.

**Validates: Requirements 6.3**
"""
import sys
import types
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.trigger_runtime import (
    GREENGRASS_MAX_QOS,
    GreengrassIpcSubscriber,
    TriggerHealth,
)


# ---------------------------------------------------------------------------
# Fake awsiot SDK (sys.modules stubs) and a recording IPC client
# ---------------------------------------------------------------------------


def _fake_awsiot_modules():
    """Fake ``awsiot`` / ``awsiot.greengrasscoreipc`` / ``.client`` /
    ``.model`` modules shaped like what ``_subscribe`` imports lazily:
    a QOS enum-like, a recording ``SubscribeToIoTCoreRequest``, the
    ``UnauthorizedError`` type, and a real ``SubscribeToIoTCoreStreamHandler``
    base class (the worker subclasses it)."""
    model = types.ModuleType("awsiot.greengrasscoreipc.model")

    class QOS:  # enum-like, mirroring the SDK's model.QOS values
        AT_MOST_ONCE = "0"
        AT_LEAST_ONCE = "1"

    class SubscribeToIoTCoreRequest:
        def __init__(self):
            self.topic_name = None
            self.qos = None

    class UnauthorizedError(Exception):
        pass

    model.QOS = QOS
    model.SubscribeToIoTCoreRequest = SubscribeToIoTCoreRequest
    model.UnauthorizedError = UnauthorizedError

    client = types.ModuleType("awsiot.greengrasscoreipc.client")

    class SubscribeToIoTCoreStreamHandler:
        pass

    client.SubscribeToIoTCoreStreamHandler = SubscribeToIoTCoreStreamHandler

    greengrasscoreipc = types.ModuleType("awsiot.greengrasscoreipc")
    greengrasscoreipc.model = model
    greengrasscoreipc.client = client
    # Never called in these tests: the worker's ipc_connect seam is injected.
    greengrasscoreipc.connect = None

    awsiot = types.ModuleType("awsiot")
    awsiot.greengrasscoreipc = greengrasscoreipc

    modules = {
        "awsiot": awsiot,
        "awsiot.greengrasscoreipc": greengrasscoreipc,
        "awsiot.greengrasscoreipc.client": client,
        "awsiot.greengrasscoreipc.model": model,
    }
    return modules, model


class _FakeOperation:
    """Records the request the worker activates; the response future
    resolves immediately (successful subscribe)."""

    def __init__(self):
        self.activated_request = None

    def activate(self, request):
        self.activated_request = request

    def get_response(self):
        class _Response:
            @staticmethod
            def result(timeout=None):
                return None

        return _Response()

    def close(self):
        pass


class _FakeIpcClient:
    def __init__(self):
        self.operation = _FakeOperation()
        self.handler = None

    def new_subscribe_to_iot_core(self, handler):
        self.handler = handler
        return self.operation

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Property 14
# ---------------------------------------------------------------------------

# Catalog qos values (0/1/2) generated often, plus arbitrary ints for
# robustness against out-of-catalog configurations.
_QOS_VALUES = st.one_of(
    st.sampled_from([0, 1, 2]),
    st.integers(min_value=-1000, max_value=1000),
)

_TOPIC = "factory/line-1/#"


# Feature: trigger-activation-runtime, Property 14: Greengrass subscribe QoS clamp
@settings(max_examples=100)
@given(qos=_QOS_VALUES)
def test_greengrass_subscribe_request_carries_clamped_qos(qos):
    """For any configured qos, the activated SubscribeToIoTCoreRequest
    carries AT_LEAST_ONCE iff qos >= 1 (else AT_MOST_ONCE) — i.e. the
    request qos is min(qos, 1), exactly the publish path's clamp — and
    `effective_qos` (mirrored into the delivered Trigger_Context's qos)
    equals min(qos, 1).

    **Validates: Requirements 6.3**
    """
    modules, model = _fake_awsiot_modules()
    ipc = _FakeIpcClient()
    deliveries = []
    health = TriggerHealth("trig-mqtt-1", "mqtt_subscribe")
    subscriber = GreengrassIpcSubscriber(
        {"topic": _TOPIC, "qos": qos},
        deliveries.append,
        lambda error: None,
        health,
        ipc_connect=lambda: ipc,
    )

    clamped = min(qos, GREENGRASS_MAX_QOS)
    with patch.dict(sys.modules, modules):
        subscriber.start()

        request = ipc.operation.activated_request
        assert isinstance(request, model.SubscribeToIoTCoreRequest)
        assert request.topic_name == _TOPIC
        expected_qos = (
            model.QOS.AT_LEAST_ONCE if qos >= 1 else model.QOS.AT_MOST_ONCE
        )
        assert request.qos == expected_qos
        assert subscriber.effective_qos == clamped
        assert clamped == min(qos, 1)

        # The delivered Trigger_Context's qos mirrors the clamp too.
        event = types.SimpleNamespace(
            message=types.SimpleNamespace(
                topic_name="factory/line-1/station", payload=b"{}"
            )
        )
        ipc.handler.on_stream_event(event)

    subscriber.stop()
    assert len(deliveries) == 1
    assert deliveries[0]["qos"] == clamped
