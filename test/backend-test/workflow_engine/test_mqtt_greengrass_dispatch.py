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
"""Bug 2 fix-checking — executor dispatches to the Greengrass publisher.

Bugfix spec: workflow-manager-integration-bugfixes (Bug 2, task 4.5).

**Property 2: Expected Behavior** — MQTT publish through Greengrass with
only a topic. The catalog/validator fix (task 4.1/4.2) lets a topic-only
Greengrass ``mqtt_publish`` config validate and package; this module
covers the runtime side: when a packaged ``mqtt_publish`` binding has
``greengrass`` set, ``OutputBindingProcessor._run_mqtt_publish`` MUST
dispatch through the injectable ``greengrass_publisher`` with ONLY the
``(topic, payload, qos)`` arguments — no broker host/port and no
certificate paths — and MUST NOT touch the plain/AWS-IoT MQTT publisher.

The processor's ``greengrass_publisher`` is injected with a recorder so
the assertions run without a Greengrass IPC runtime (the same boundary
the paho publisher is injected at in the preservation tests).

Validates: Requirements 2.2
"""
import json

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.output_bindings import (
    OutputBindingProcessor,
    render_template,
)

METADATA = {"is_anomalous": True, "confidence": 0.9}


class Recorder:
    """Injectable publisher recording every positional-arg tuple; never
    touches a real broker or Greengrass IPC runtime."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)


def _document(parameters):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": "aarch64-jp5",
        "segments": [],
        "executorBindings": [
            {"nodeId": "m1", "binding": "mqtt_publish", "parameters": parameters}
        ],
        "pluginDependencies": [],
    }


def _run(parameters):
    """Run the processor over a single greengrass mqtt_publish binding
    with both publishers injected; return (greengrass_calls, mqtt_calls)."""
    greengrass = Recorder()
    mqtt = Recorder()
    processor = OutputBindingProcessor(
        mqtt_publisher=mqtt, greengrass_publisher=greengrass
    )
    processor(None, _document(parameters), dict(METADATA))
    return greengrass.calls, mqtt.calls


def _expected_payload(parameters):
    payload = render_template(
        str(parameters.get("payload_template") or "{inference_json}"), METADATA
    )
    return payload if isinstance(payload, str) else json.dumps(payload, default=str)


class TestGreengrassDispatch:
    """Property 2: Expected Behavior — a ``greengrass`` binding publishes
    through the Greengrass publisher with only topic/payload/qos."""

    def test_topic_only_greengrass_dispatches_to_greengrass_publisher(self):
        # Only the topic + greengrass flag — no broker host/port, no certs.
        params = {"topic": "factory/line1/inspection", "greengrass": True}
        greengrass_calls, mqtt_calls = _run(params)

        assert len(greengrass_calls) == 1, (
            "expected exactly one Greengrass publish, got "
            "{0}".format(greengrass_calls)
        )
        # Exactly (topic, payload, qos) — 3 positional args, nothing else.
        assert greengrass_calls[0] == (
            "factory/line1/inspection",
            _expected_payload(params),
            0,
        )
        # The plain / AWS-IoT MQTT publisher is never called.
        assert mqtt_calls == [], (
            "Greengrass publish must not touch the broker publisher; got "
            "{0}".format(mqtt_calls)
        )

    def test_greengrass_publish_uses_only_topic_payload_qos(self):
        # Even when broker/cert fields are absent, publishing succeeds and
        # forwards only the three Greengrass arguments.
        params = {
            "topic": "dda/results",
            "greengrass": True,
            "qos": 1,
            "payload_template": "anomaly={is_anomalous} conf={confidence}",
        }
        greengrass_calls, mqtt_calls = _run(params)

        assert len(greengrass_calls) == 1
        topic, payload, qos = greengrass_calls[0]
        assert topic == "dda/results"
        assert payload == "anomaly=True conf=0.9"
        assert qos == 1
        assert mqtt_calls == []

    def test_greengrass_takes_precedence_over_broker_fields(self):
        # If a stray broker_host lingers, greengrass still wins and the
        # broker publisher is not used (the greengrass branch is first).
        params = {
            "topic": "dda/results",
            "greengrass": True,
            "broker_host": "10.0.0.12",
            "qos": 1,
        }
        greengrass_calls, mqtt_calls = _run(params)

        assert len(greengrass_calls) == 1
        topic, payload, qos = greengrass_calls[0]
        assert topic == "dda/results"
        assert payload == _expected_payload(params)
        # The configured qos is forwarded to the injectable publisher as-is
        # (the default Greengrass publisher maps it to the IPC QOS enum).
        assert qos == 1
        assert mqtt_calls == []
