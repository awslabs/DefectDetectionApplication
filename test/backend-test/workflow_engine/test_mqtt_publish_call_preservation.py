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
"""Preservation property tests — ``_run_mqtt_publish`` publish-call args.

Bugfix spec: workflow-manager-integration-bugfixes (Bug 2, task 2.2).

**Property 6: Preservation** — behavior unchanged outside every bug
condition. Bug 2 adds an additive, off-by-default Greengrass publishing
option whose fix inserts a ``greengrass`` branch into
``OutputBindingProcessor._run_mqtt_publish`` *before* the existing broker
read. Every ``mqtt_publish`` configuration that does NOT request
Greengrass publishing (``greengrass`` off/absent) must therefore still
dispatch to the injected MQTT publisher with EXACTLY the same call
arguments it does today:

* **Plain-broker path** (``aws_iot`` off/absent) — a single publish call
  with the 5 positional args ``(host, port, topic, payload_text, qos)``
  and no ``client_id``/``tls``.
* **AWS IoT Core path** (``aws_iot`` on) — a single publish call with the
  7 positional args ``(host, port, topic, payload_text, qos, thing_name,
  tls)`` where the port defaults 1883 -> 8883 (mutual-TLS), the qos is
  clamped to at most 1, and ``tls`` is the ``ca_certs``/``certfile``/
  ``keyfile`` device-path dict.

This module follows the observation-first methodology: every argument
tuple below was OBSERVED by driving ``_run_mqtt_publish`` on the UNFIXED
tree with an injected recording publisher, and is asserted here as the
baseline the fix MUST preserve. These tests MUST PASS on the current
(unfixed) code and are re-run after the fix (task 4.6) to prove the
``greengrass``-off publish call is byte-for-byte unchanged.

The tests inject the MQTT publisher at the ``OutputBindingProcessor``
boundary so they run without paho-mqtt or a broker.

**Validates: Requirements 3.2**
"""
import json

from hypothesis import given
from hypothesis import strategies as st

# workflow_engine resolves from src/backend (PYTHONPATH set by the runner).
from workflow_engine.output_bindings import (
    AWS_IOT_MAX_QOS,
    AWS_IOT_TLS_PORT,
    DEFAULT_MQTT_PORT,
    OutputBindingProcessor,
    render_template,
)


# ---------------------------------------------------------------------------
# Recording publisher + expected-argument derivation (observed on UNFIXED)
# ---------------------------------------------------------------------------

class _RecordingPublisher:
    """Injected at the MQTT client boundary; records every positional-arg
    tuple so tests assert the exact publish call without a broker."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _expected_payload_text(parameters, metadata):
    """Replicate the two-line payload rendering in ``_run_mqtt_publish``:
    render the template, then JSON-encode any non-string result."""
    payload = render_template(
        str(parameters.get("payload_template") or "{inference_json}"),
        metadata,
    )
    return (
        payload if isinstance(payload, str)
        else json.dumps(payload, default=str)
    )


def _run(parameters, metadata):
    publisher = _RecordingPublisher()
    processor = OutputBindingProcessor(mqtt_publisher=publisher)
    processor._run_mqtt_publish(parameters, metadata)
    return publisher.calls


# ---------------------------------------------------------------------------
# Generators (greengrass OFF — plain broker and aws_iot only)
# ---------------------------------------------------------------------------

_NONEMPTY_TEXT = st.text(min_size=1, max_size=40).filter(lambda s: s.strip() != "")
_TOPIC = st.one_of(
    st.sampled_from(["factory/line1/inspection", "dda/results", "a"]),
    _NONEMPTY_TEXT,
)
_BROKER_HOST = st.one_of(
    st.sampled_from(["10.0.0.12", "broker.local"]),
    _NONEMPTY_TEXT,
)
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
        "/greengrass/v2/rootCA.pem",
        "/greengrass/v2/thingCert.crt",
        "/greengrass/v2/privKey.key",
    ]),
    _NONEMPTY_TEXT,
)
_METADATA = st.fixed_dictionaries({
    "is_anomalous": st.booleans(),
    "confidence": st.floats(min_value=0.0, max_value=1.0),
})


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
    """A plain-MQTT-broker config: broker_host + topic (+ optionals),
    ``aws_iot`` off, ``greengrass`` absent."""
    params = {"broker_host": draw(_BROKER_HOST), "topic": draw(_TOPIC)}
    if draw(st.booleans()):
        params["aws_iot"] = False
    return _with_optionals(
        params, draw(_OPTIONAL_PORT), draw(_OPTIONAL_QOS), draw(_OPTIONAL_PAYLOAD)
    )


@st.composite
def aws_iot_config(draw):
    """An AWS IoT Core config: aws_iot on with thing name + the three
    device-local certificate paths (+ optionals), ``greengrass`` absent."""
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
# 1. Plain-broker publish call preserved
# ---------------------------------------------------------------------------

class TestPlainBrokerPublishCallPreserved:
    """A broker-targeting config (greengrass off) publishes once with the
    5 positional args (host, port, topic, payload_text, qos)."""

    @given(config=plain_broker_config(), metadata=_METADATA)
    def test_plain_broker_publish_args(self, config, metadata):
        calls = _run(dict(config), metadata)
        assert len(calls) == 1, "expected exactly one publish call"
        args, kwargs = calls[0]
        assert kwargs == {}
        expected = (
            str(config["broker_host"]),
            int(config.get("broker_port", DEFAULT_MQTT_PORT)),
            str(config["topic"]),
            _expected_payload_text(config, metadata),
            int(config.get("qos", 0)),
        )
        assert args == expected, (
            "plain-broker publish args changed: {0!r} != {1!r}".format(
                args, expected)
        )


# ---------------------------------------------------------------------------
# 2. AWS IoT Core publish call preserved
# ---------------------------------------------------------------------------

class TestAwsIotPublishCallPreserved:
    """An aws_iot config (greengrass off) publishes once with the 7
    positional args, the 1883->8883 default-port switch, qos clamped to 1,
    and the ca/cert/key tls dict."""

    @given(config=aws_iot_config(), metadata=_METADATA)
    def test_aws_iot_publish_args(self, config, metadata):
        calls = _run(dict(config), metadata)
        assert len(calls) == 1, "expected exactly one publish call"
        args, kwargs = calls[0]
        assert kwargs == {}

        configured_port = int(config.get("broker_port", DEFAULT_MQTT_PORT))
        expected_port = (
            AWS_IOT_TLS_PORT if configured_port == DEFAULT_MQTT_PORT
            else configured_port
        )
        expected_qos = min(int(config.get("qos", 0)), AWS_IOT_MAX_QOS)
        expected_tls = {
            "ca_certs": str(config["iot_ca_cert_path"]),
            "certfile": str(config["iot_client_cert_path"]),
            "keyfile": str(config["iot_private_key_path"]),
        }
        expected = (
            str(config["broker_host"]),
            expected_port,
            str(config["topic"]),
            _expected_payload_text(config, metadata),
            expected_qos,
            str(config["iot_thing_name"]),
            expected_tls,
        )
        assert args == expected, (
            "aws_iot publish args changed: {0!r} != {1!r}".format(args, expected)
        )


# ---------------------------------------------------------------------------
# 3. Concrete observed examples (documentation of the exact baseline)
# ---------------------------------------------------------------------------

class TestObservedExamples:
    """The exact tuples observed on the UNFIXED executor, pinned as
    regression anchors alongside the property tests."""

    def test_plain_broker_default_port_and_qos(self):
        calls = _run(
            {"broker_host": "10.0.0.12", "topic": "factory/line1"},
            {"is_anomalous": True, "confidence": 0.9},
        )
        assert calls == [(
            ("10.0.0.12", 1883, "factory/line1",
             '{"confidence": 0.9, "is_anomalous": true}', 0),
            {},
        )]

    def test_plain_broker_explicit_port_qos_payload(self):
        calls = _run(
            {"broker_host": "h", "broker_port": 1884, "topic": "t", "qos": 2,
             "payload_template": "anomaly={is_anomalous} conf={confidence}"},
            {"is_anomalous": True, "confidence": 0.5},
        )
        assert calls == [(("h", 1884, "t", "anomaly=True conf=0.5", 2), {})]

    def test_aws_iot_default_port_switches_to_tls_and_qos_clamped(self):
        calls = _run(
            {"broker_host": "h", "topic": "t", "aws_iot": True, "qos": 2,
             "iot_thing_name": "dev01", "iot_ca_cert_path": "/ca",
             "iot_client_cert_path": "/cert", "iot_private_key_path": "/key"},
            {"is_anomalous": False, "confidence": 0.1},
        )
        assert calls == [(
            ("h", 8883, "t", '{"confidence": 0.1, "is_anomalous": false}', 1,
             "dev01", {"ca_certs": "/ca", "certfile": "/cert", "keyfile": "/key"}),
            {},
        )]

    def test_aws_iot_explicit_port_preserved(self):
        calls = _run(
            {"broker_host": "h", "broker_port": 9999, "topic": "t",
             "aws_iot": True, "qos": 0, "iot_thing_name": "dev01",
             "iot_ca_cert_path": "/ca", "iot_client_cert_path": "/cert",
             "iot_private_key_path": "/key"},
            {"is_anomalous": False, "confidence": 0.1},
        )
        assert calls == [(
            ("h", 9999, "t", '{"confidence": 0.1, "is_anomalous": false}', 0,
             "dev01", {"ca_certs": "/ca", "certfile": "/cert", "keyfile": "/key"}),
            {},
        )]
