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
"""Preservation property tests — executor MQTT publish-call arguments.

Bugfix spec: workflow-manager-integration-bugfixes (Bug 2, task 2.2).

**Property 6: Preservation** — behavior unchanged outside every bug
condition. Bug 2 adds an additive, off-by-default Greengrass publishing
branch to ``OutputBindingProcessor._run_mqtt_publish``. Every publish
whose config does NOT request Greengrass (``greengrass`` off/absent) must
dispatch to the MQTT publisher with EXACTLY the same arguments as today:
the plain-broker path and the AWS IoT Core (``aws_iot``) path.

This module follows the observation-first methodology: the publish-call
argument tuples below were observed on the UNFIXED
``_run_mqtt_publish`` and are asserted here as the baseline the fix MUST
preserve. They MUST PASS on the current (unfixed) code and are re-run
after the fix (task 4.6) to prove no regression.

Baselines captured (observed on the UNFIXED executor, via an injected
publisher that never touches a real broker):

1. Plain broker (``aws_iot`` off): the publisher is called once with the
   5-tuple ``(host, port, topic, payload, qos)``:
     - ``host`` == ``str(broker_host)``,
     - ``port`` == ``int(broker_port)`` or the plain-MQTT default 1883,
     - ``topic`` == ``str(topic)``,
     - ``payload`` == the rendered ``payload_template`` (default
       ``{inference_json}``),
     - ``qos`` == ``int(qos)`` or the default 0 (never clamped).

2. AWS IoT Core (``aws_iot`` on): the publisher is called once with the
   7-tuple ``(host, port, topic, payload, qos, client_id, tls)``:
     - ``client_id`` == the ``iot_thing_name``,
     - ``tls`` == ``{ca_certs, certfile, keyfile}`` from the device-local
       certificate paths,
     - ``port`` == 8883 when ``broker_port`` was left at the 1883 default,
       else the explicit port,
     - ``qos`` == ``min(qos, 1)`` (AWS IoT Core does not support QoS 2).

**Validates: Requirements 3.2**
"""
import json

from hypothesis import given
from hypothesis import strategies as st

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.output_bindings import (
    AWS_IOT_TLS_PORT,
    DEFAULT_MQTT_PORT,
    OutputBindingProcessor,
    render_template,
)

METADATA = {"is_anomalous": True, "confidence": 0.9}


class Recorder:
    """Injectable MQTT publisher recording every call; never raising."""

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


def _publish(parameters):
    """Run the processor with an injected publisher and return its single
    recorded call tuple."""
    recorder = Recorder()
    OutputBindingProcessor(mqtt_publisher=recorder)(
        None, _document(parameters), dict(METADATA)
    )
    assert len(recorder.calls) == 1, (
        "expected exactly one publish call, got {0}".format(recorder.calls)
    )
    return recorder.calls[0]


def _expected_payload(parameters):
    payload = render_template(
        str(parameters.get("payload_template") or "{inference_json}"), METADATA
    )
    return payload if isinstance(payload, str) else json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Config generators (greengrass OFF — plain broker and aws_iot only)
# ---------------------------------------------------------------------------

_NONEMPTY_TEXT = st.text(min_size=1, max_size=40).filter(lambda s: s.strip() != "")
_TOPIC = st.one_of(
    st.sampled_from(["dda/results", "factory/line1/inspection"]), _NONEMPTY_TEXT
)
_BROKER_HOST = st.one_of(st.sampled_from(["broker.local", "10.0.0.12"]), _NONEMPTY_TEXT)
_OPTIONAL_PORT = st.one_of(st.none(), st.integers(min_value=1, max_value=65535))
_OPTIONAL_QOS = st.one_of(st.none(), st.sampled_from([0, 1, 2]))
_OPTIONAL_PAYLOAD = st.one_of(
    st.none(),
    st.sampled_from([
        "{inference_json}",
        "{is_anomalous}",
        "anomaly={is_anomalous} confidence={confidence}",
    ]),
)
_CERT_PATH = st.one_of(
    st.sampled_from([
        "/greengrass/v2/AmazonRootCA1.pem",
        "/greengrass/v2/thingCert.crt",
        "/greengrass/v2/privKey.key",
    ]),
    _NONEMPTY_TEXT,
)


def _with_optionals(params, broker_port, qos, payload_template):
    if broker_port is not None:
        params["broker_port"] = broker_port
    if qos is not None:
        params["qos"] = qos
    if payload_template is not None:
        params["payload_template"] = payload_template
    return params


@st.composite
def plain_broker_config(draw):
    params = {"broker_host": draw(_BROKER_HOST), "topic": draw(_TOPIC)}
    if draw(st.booleans()):
        params["aws_iot"] = False
    return _with_optionals(
        params, draw(_OPTIONAL_PORT), draw(_OPTIONAL_QOS), draw(_OPTIONAL_PAYLOAD)
    )


@st.composite
def aws_iot_config(draw):
    params = {
        "broker_host": draw(_BROKER_HOST),
        "topic": draw(_TOPIC),
        "aws_iot": True,
        "iot_thing_name": draw(_NONEMPTY_TEXT),
        "iot_ca_cert_path": draw(_CERT_PATH),
        "iot_client_cert_path": draw(_CERT_PATH),
        "iot_private_key_path": draw(_CERT_PATH),
    }
    return _with_optionals(
        params, draw(_OPTIONAL_PORT), draw(_OPTIONAL_QOS), draw(_OPTIONAL_PAYLOAD)
    )


# ---------------------------------------------------------------------------
# 1. Plain broker publish-call arguments preserved
# ---------------------------------------------------------------------------

class TestPlainBrokerPublishArgsPreserved:
    """The plain-broker path dispatches the 5-tuple (host, port, topic,
    payload, qos) exactly as today (greengrass off)."""

    @given(config=plain_broker_config())
    def test_publish_call_arguments(self, config):
        call = _publish(config)
        assert len(call) == 5, (
            "plain-broker publish must be a 5-tuple, got {0}".format(call)
        )
        host, port, topic, payload, qos = call
        assert host == str(config["broker_host"])
        assert port == int(config.get("broker_port", DEFAULT_MQTT_PORT))
        assert topic == str(config["topic"])
        assert payload == _expected_payload(config)
        # Plain broker never clamps qos (default 0).
        assert qos == int(config.get("qos", 0))


# ---------------------------------------------------------------------------
# 2. AWS IoT Core publish-call arguments preserved
# ---------------------------------------------------------------------------

class TestAwsIotPublishArgsPreserved:
    """The aws_iot path dispatches the 7-tuple with the thing-name client
    id, the tls cert-path dict, the mutual-TLS port default, and qos
    clamped to 1 — exactly as today."""

    @given(config=aws_iot_config())
    def test_publish_call_arguments(self, config):
        call = _publish(config)
        assert len(call) == 7, (
            "aws_iot publish must be a 7-tuple, got {0}".format(call)
        )
        host, port, topic, payload, qos, client_id, tls = call
        assert host == str(config["broker_host"])
        assert topic == str(config["topic"])
        assert payload == _expected_payload(config)
        assert client_id == str(config["iot_thing_name"])
        assert tls == {
            "ca_certs": str(config["iot_ca_cert_path"]),
            "certfile": str(config["iot_client_cert_path"]),
            "keyfile": str(config["iot_private_key_path"]),
        }
        # A broker_port left at the plain default switches to 8883; an
        # explicit port is respected unchanged.
        configured_port = config.get("broker_port", DEFAULT_MQTT_PORT)
        if configured_port == DEFAULT_MQTT_PORT:
            assert port == AWS_IOT_TLS_PORT
        else:
            assert port == int(configured_port)
        # AWS IoT Core does not support QoS 2 — the executor clamps to 1.
        assert qos == min(int(config.get("qos", 0)), 1)
