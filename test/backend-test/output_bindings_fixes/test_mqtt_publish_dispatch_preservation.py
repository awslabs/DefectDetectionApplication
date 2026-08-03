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
"""Preservation property tests (Task 2) for workflow-output-bindings-fixes.

Property 4: Preservation — MQTT publish paths unchanged: for random
parameter dicts across the greengrass / plain-broker / aws_iot paths,
``_run_mqtt_publish`` invokes the injected publishers with identical
arguments.

**Validates: Requirements 3.1, 3.2**

Extends ``test_mqtt_publish_call_preservation.py`` (which pins the
plain-broker and aws_iot argument tuples with ``greengrass`` off/absent) with
the coverage this spec's fixes must not disturb:

* **Greengrass path call identity** — a ``greengrass``-enabled config
  publishes EXACTLY ONCE through the injected greengrass publisher with the
  3 positional args ``(topic, payload_text, qos)`` and never touches the
  paho publisher, regardless of any broker/aws_iot parameters also present
  (the greengrass branch dispatches before the broker read, so
  ``broker_host`` is not even required);
* **Cross-path dispatch identity** — for any generated config in the three
  families, exactly one publisher receives exactly one call and the other
  receives none.

Observation-first: both patterns were OBSERVED on the current (unfixed)
tree by driving ``_run_mqtt_publish`` with recording publishers. These
tests MUST PASS today and keep passing after the fix (the Defect A engine
change only wraps the DEFAULT greengrass publisher's UnauthorizedError —
the dispatch to an injected publisher is untouched).

Runs with the hypothesis profiles registered in ``test/backend-test/
conftest.py`` (``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci``
= 100).
"""
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.output_bindings import (
    OutputBindingProcessor,
    render_template,
)


class _RecordingPublisher:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _expected_payload_text(parameters, metadata):
    """Replicate the payload rendering in ``_run_mqtt_publish`` (observed):
    render the template, JSON-encode any non-string result."""
    payload = render_template(
        str(parameters.get("payload_template") or "{inference_json}"),
        metadata,
    )
    return (
        payload if isinstance(payload, str)
        else json.dumps(payload, default=str)
    )


def _run(parameters, metadata):
    greengrass = _RecordingPublisher()
    paho = _RecordingPublisher()
    processor = OutputBindingProcessor(
        mqtt_publisher=paho, greengrass_publisher=greengrass
    )
    processor._run_mqtt_publish(dict(parameters), metadata)
    return greengrass.calls, paho.calls


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_NONEMPTY_TEXT = st.text(min_size=1, max_size=40).filter(
    lambda s: s.strip() != "")
_TOPIC = st.one_of(
    st.sampled_from(["factory/line1/inspection", "dda/results", "a"]),
    _NONEMPTY_TEXT,
)
_OPTIONAL_QOS = st.one_of(st.none(), st.sampled_from([0, 1, 2]))
_OPTIONAL_PAYLOAD = st.one_of(
    st.none(),
    st.sampled_from([
        "{inference_json}",
        "{is_anomalous}",
        "anomaly={is_anomalous} confidence={confidence}",
    ]),
)
_METADATA = st.fixed_dictionaries({
    "is_anomalous": st.booleans(),
    "confidence": st.floats(min_value=0.0, max_value=1.0),
})


@st.composite
def greengrass_config(draw):
    """A ``greengrass``-enabled config. Broker/aws_iot parameters may also
    be present — the greengrass branch wins today and must keep winning."""
    params = {"topic": draw(_TOPIC), "greengrass": True}
    qos = draw(_OPTIONAL_QOS)
    if qos is not None:
        params["qos"] = qos
    payload_template = draw(_OPTIONAL_PAYLOAD)
    if payload_template is not None:
        params["payload_template"] = payload_template
    # Stray parameters from other paths must not change the dispatch.
    if draw(st.booleans()):
        params["broker_host"] = draw(_NONEMPTY_TEXT)
        if draw(st.booleans()):
            params["broker_port"] = draw(
                st.integers(min_value=1, max_value=65535))
    if draw(st.booleans()):
        params["aws_iot"] = draw(st.booleans())
    return params


@st.composite
def plain_broker_config(draw):
    params = {"broker_host": draw(_NONEMPTY_TEXT), "topic": draw(_TOPIC)}
    if draw(st.booleans()):
        params["greengrass"] = False
    if draw(st.booleans()):
        params["aws_iot"] = False
    return params


@st.composite
def aws_iot_config(draw):
    params = {
        "broker_host": draw(_NONEMPTY_TEXT),
        "topic": draw(_TOPIC),
        "aws_iot": True,
        "iot_thing_name": draw(_NONEMPTY_TEXT),
        "iot_ca_cert_path": "/greengrass/v2/rootCA.pem",
        "iot_client_cert_path": "/greengrass/v2/thingCert.crt",
        "iot_private_key_path": "/greengrass/v2/privKey.key",
    }
    if draw(st.booleans()):
        params["greengrass"] = False
    return params


# ---------------------------------------------------------------------------
# 1. Greengrass publish call identity
# ---------------------------------------------------------------------------

class TestGreengrassPublishCallPreserved:
    """Property 4: a greengrass-enabled config publishes once through the
    injected greengrass publisher with (topic, payload_text, qos); the paho
    publisher is never called.

    **Validates: Requirements 3.2**
    """

    @given(config=greengrass_config(), metadata=_METADATA)
    @settings(deadline=None)
    def test_greengrass_publish_args(self, config, metadata):
        greengrass_calls, paho_calls = _run(config, metadata)
        assert paho_calls == [], (
            "greengrass config leaked into the paho publisher: {!r}".format(
                paho_calls))
        assert len(greengrass_calls) == 1, (
            "expected exactly one greengrass publish call, got {!r}".format(
                greengrass_calls))
        args, kwargs = greengrass_calls[0]
        assert kwargs == {}
        expected = (
            str(config["topic"]),
            _expected_payload_text(config, metadata),
            int(config.get("qos", 0)),
        )
        assert args == expected, (
            "greengrass publish args changed: {!r} != {!r}".format(
                args, expected))

    def test_observed_example(self):
        """The exact observed baseline call (documentation anchor)."""
        greengrass_calls, paho_calls = _run(
            {"topic": "factory/line1/inspection", "greengrass": True,
             "qos": 1},
            {"is_anomalous": True, "confidence": 0.9},
        )
        assert paho_calls == []
        assert greengrass_calls == [(
            ("factory/line1/inspection",
             '{"confidence": 0.9, "is_anomalous": true}', 1),
            {},
        )]


# ---------------------------------------------------------------------------
# 2. Cross-path dispatch identity
# ---------------------------------------------------------------------------

class TestDispatchRoutingPreserved:
    """Property 4: each config family dispatches to exactly its publisher —
    greengrass configs never reach paho, broker/aws_iot configs never reach
    the greengrass publisher.

    **Validates: Requirements 3.1, 3.2**
    """

    @given(
        config=st.one_of(
            greengrass_config(), plain_broker_config(), aws_iot_config()),
        metadata=_METADATA,
    )
    @settings(deadline=None)
    def test_exactly_one_publisher_receives_exactly_one_call(
            self, config, metadata):
        greengrass_calls, paho_calls = _run(config, metadata)
        if config.get("greengrass"):
            assert len(greengrass_calls) == 1 and paho_calls == []
        else:
            assert len(paho_calls) == 1 and greengrass_calls == []
