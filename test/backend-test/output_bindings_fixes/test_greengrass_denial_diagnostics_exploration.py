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
"""Bug-condition exploration test — case 2: bare denial diagnostics
(workflow-output-bindings-fixes, Defect A, ``isBugCondition_A``).

Property 1: Bug Condition — a denied Greengrass IPC publish is diagnosable.

**These tests assert the FIXED (post-fix) engine behavior, so they are
EXPECTED TO FAIL on the UNFIXED tree.** The failure is the counterexample
confirming the diagnostics half of Defect A: when the Greengrass nucleus
denies ``PublishToIoTCore`` (as on live run 85bf7a61 — no accessControl
policy covered the workflow topic), ``_default_greengrass_publisher`` lets
the bare ``awsiot.greengrasscoreipc.model.UnauthorizedError`` propagate
verbatim. Nothing names the denied topic or the LocalServer component's
``aws.greengrass.ipc.mqttproxy`` accessControl configuration as the cause,
so the run error gives the user no remediation path.

Expected counterexample on the UNFIXED tree:
    _default_greengrass_publisher raises UnauthorizedError (bare, message
    'UnauthorizedError'); through OutputBindingProcessor.process the run
    error is "Output binding(s) failed: node mqtt_publish_1:
    UnauthorizedError" — no topic, no accessControl cause.

The SAME tests are re-run in task 3.4 against the fixed publisher (which
re-raises a RuntimeError naming the denied topic and the
aws.greengrass.ipc.mqttproxy accessControl configuration), where they must
PASS.

The awsiot Greengrass IPC boundary is faked in ``sys.modules`` (the real
publisher imports it lazily), with the operation's ``get_response().result``
raising the model-shaped ``UnauthorizedError`` — no Greengrass nucleus, no
network.

Validates: Requirements 1.3 (expected behavior 2.2)
"""
import sys
import types
from unittest.mock import patch

import pytest

from workflow_engine import output_bindings
from workflow_engine.output_bindings import (
    OutputBindingError,
    OutputBindingProcessor,
)

#: The catalog example workflow topic (free-string node contract).
TOPIC = "factory/line1/inspection"


def _fake_awsiot(raise_error):
    """Fake ``awsiot`` / ``awsiot.greengrasscoreipc`` /
    ``awsiot.greengrasscoreipc.model`` modules whose IPC operation result
    raises ``raise_error`` — the shape ``_default_greengrass_publisher``
    imports lazily."""
    model = types.ModuleType("awsiot.greengrasscoreipc.model")

    class QOS:
        AT_MOST_ONCE = 0
        AT_LEAST_ONCE = 1

    class PublishToIoTCoreRequest:
        def __init__(self):
            self.topic_name = None
            self.payload = None
            self.qos = None

    class UnauthorizedError(Exception):
        """awsiot.greengrasscoreipc.model.UnauthorizedError shape: the
        live denial surfaced as this bare exception."""

    model.QOS = QOS
    model.PublishToIoTCoreRequest = PublishToIoTCoreRequest
    model.UnauthorizedError = UnauthorizedError

    class _Future:
        def result(self, timeout=None):
            raise raise_error(UnauthorizedError)

    class _Operation:
        def __init__(self):
            self.request = None

        def activate(self, request):
            self.request = request

        def get_response(self):
            return _Future()

    class _IpcClient:
        def new_publish_to_iot_core(self):
            return _Operation()

    greengrasscoreipc = types.ModuleType("awsiot.greengrasscoreipc")
    greengrasscoreipc.model = model
    greengrasscoreipc.connect = lambda: _IpcClient()

    awsiot = types.ModuleType("awsiot")
    awsiot.greengrasscoreipc = greengrasscoreipc

    return {
        "awsiot": awsiot,
        "awsiot.greengrasscoreipc": greengrasscoreipc,
        "awsiot.greengrasscoreipc.model": model,
    }


def _unauthorized(cls):
    """The live-device denial: the nucleus rejects the publish and the IPC
    future resolves to a bare UnauthorizedError."""
    return cls("UnauthorizedError")


class TestDefaultGreengrassPublisherDiagnostics:
    def test_denied_publish_error_names_topic_and_access_control(self):
        """A denied IPC publish must surface an error naming the denied
        topic and the LocalServer ``aws.greengrass.ipc.mqttproxy``
        accessControl configuration as the cause.

        EXPECTED FAILURE on the unfixed tree: the bare UnauthorizedError
        propagates verbatim from ``operation.get_response().result()`` with
        no topic and no accessControl/remediation hint.

        Validates: Requirements 1.3 (expected behavior 2.2)
        """
        modules = _fake_awsiot(_unauthorized)
        with patch.dict(sys.modules, modules):
            with pytest.raises(Exception) as exc_info:
                output_bindings._default_greengrass_publisher(
                    TOPIC, '{"is_anomalous": true}', 1)

        message = str(exc_info.value)
        assert TOPIC in message, (
            "COUNTEREXAMPLE (Defect A): the denial error {0!r} does not "
            "name the denied topic {1!r} — the bare UnauthorizedError "
            "propagates verbatim".format(message, TOPIC))
        assert "aws.greengrass.ipc.mqttproxy" in message, (
            "COUNTEREXAMPLE (Defect A): the denial error {0!r} does not "
            "name the LocalServer aws.greengrass.ipc.mqttproxy "
            "accessControl configuration as the cause".format(message))
        assert "accessControl" in message, (
            "COUNTEREXAMPLE (Defect A): the denial error {0!r} carries no "
            "accessControl remediation hint".format(message))


class TestRunMqttPublishDenialSurfacing:
    def test_binding_error_carries_topic_and_access_control_cause(self):
        """Driven through ``OutputBindingProcessor._run_mqtt_publish`` with
        ``greengrass=true`` and the DEFAULT publisher over the faked IPC
        boundary, the collected ``OutputBindingError`` must carry the denied
        topic and the accessControl cause into the run error.

        EXPECTED FAILURE on the unfixed tree: the run error is
        "Output binding(s) failed: node mqtt_publish_1: UnauthorizedError"
        — exactly the live run's bare, non-actionable text.

        Validates: Requirements 1.3 (expected behavior 2.2)
        """
        document = {
            "executorBindings": [
                {"nodeId": "mqtt_publish_1", "binding": "mqtt_publish",
                 "parameters": {"topic": TOPIC, "greengrass": True,
                                "qos": 1}},
            ],
        }
        processor = OutputBindingProcessor()  # default greengrass publisher

        modules = _fake_awsiot(_unauthorized)
        with patch.dict(sys.modules, modules):
            with pytest.raises(OutputBindingError) as exc_info:
                processor.process(None, document, {"is_anomalous": True})

        assert exc_info.value.node_ids == ["mqtt_publish_1"]
        message = str(exc_info.value)
        assert TOPIC in message and "aws.greengrass.ipc.mqttproxy" in message, (
            "COUNTEREXAMPLE (Defect A): the surfaced run error {0!r} names "
            "neither the denied topic {1!r} nor the "
            "aws.greengrass.ipc.mqttproxy accessControl cause — the user "
            "sees only the bare UnauthorizedError".format(message, TOPIC))
