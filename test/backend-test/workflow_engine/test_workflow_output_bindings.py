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
"""Tests for the executor output bindings (Requirements 9.4, 9.5, 9.6, 13.7).

Uses injected fake clients so condition evaluation, inference_filter
gating, template rendering, and failure containment are exercised
without GPIO hardware, an MQTT broker, or an OPC UA server.
"""
import sys
import types
from unittest.mock import patch

import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.output_bindings import (
    OutputBindingProcessor,
    _default_mqtt_publisher,
    evaluate_condition,
    render_template,
)

METADATA = {"is_anomalous": True, "confidence": 0.9}


def binding(node_id, kind, parameters=None, upstream=(), downstream=()):
    return {
        "nodeId": node_id,
        "binding": kind,
        "parameters": dict(parameters or {}),
        "upstreamNodeIds": list(upstream),
        "downstreamNodeIds": list(downstream),
    }


def document(*bindings):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": "aarch64-jp5",
        "segments": [],
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


class Recorder:
    """Injectable client recording calls; optionally raising."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error


@pytest.fixture
def clients():
    return {
        "dio": Recorder(),
        "mqtt": Recorder(),
        "opcua": Recorder(),
    }


@pytest.fixture
def processor(clients):
    return OutputBindingProcessor(
        dio_actuator=clients["dio"],
        mqtt_publisher=clients["mqtt"],
        opcua_writer=clients["opcua"],
    )


# --------------------------------------------------------------------------
# Condition evaluation
# --------------------------------------------------------------------------

class TestEvaluateCondition:
    def test_conjunction_over_metadata(self):
        assert evaluate_condition(
            "is_anomalous == true && confidence >= 0.8", METADATA
        ) is True

    def test_comparison_false(self):
        assert evaluate_condition("confidence > 0.95", METADATA) is False

    def test_disjunction_and_parentheses(self):
        assert evaluate_condition(
            "(confidence > 0.95) || is_anomalous", METADATA
        ) is True

    def test_string_tag_values_are_coerced(self):
        metadata = {"is_anomalous": "true", "confidence": "0.9"}
        assert evaluate_condition(
            "is_anomalous == true && confidence >= 0.8", metadata
        ) is True

    def test_unknown_field_raises(self):
        with pytest.raises(ValueError):
            evaluate_condition("no_such_field == 1", METADATA)

    def test_malformed_condition_raises(self):
        with pytest.raises(ValueError):
            evaluate_condition("confidence >= ", METADATA)

    def test_empty_condition_raises(self):
        with pytest.raises(ValueError):
            evaluate_condition("", METADATA)

    def test_negated_parenthesized_expression(self):
        # The compiler composes the conditional node's "false"-path gate
        # as "!(condition)".
        assert evaluate_condition("!(is_anomalous == true)", METADATA) is False
        assert evaluate_condition(
            "!(is_anomalous == true && confidence >= 0.95)", METADATA
        ) is True

    def test_negated_bare_field(self):
        assert evaluate_condition(
            "!is_anomalous", {"is_anomalous": False}) is True
        assert evaluate_condition("!is_anomalous", METADATA) is False

    def test_catalog_condition_examples_evaluate(self):
        # The example expressions the catalog documents for every
        # condition parameter (inference_filter, conditional,
        # digital_output) must be working expressions in this evaluator:
        # the field-level help never advertises an unevaluable example.
        from workflow_engine.vendor.workflow_core.catalog import (
            CONDITION_EXAMPLES,
            get_node_type,
        )

        assert len(CONDITION_EXAMPLES) >= 3
        for type_id in ("inference_filter", "conditional", "digital_output"):
            condition = next(
                p for p in get_node_type(type_id).parameters
                if p.name == "condition")
            assert condition.examples, type_id
            for example in condition.examples:
                # Evaluates without error on the metadata fields the
                # executor provides (both polarities are legal outcomes).
                assert evaluate_condition(example, METADATA) in (True, False)
                # The compiler's "false"-path composition stays evaluable.
                assert evaluate_condition(
                    "!({0})".format(example), METADATA) in (True, False)

    def test_dangling_negation_raises(self):
        with pytest.raises(ValueError):
            evaluate_condition("!", METADATA)
        with pytest.raises(ValueError):
            evaluate_condition("confidence == !", METADATA)


# --------------------------------------------------------------------------
# Template rendering
# --------------------------------------------------------------------------

class TestRenderTemplate:
    def test_single_placeholder_keeps_native_type(self):
        assert render_template("{is_anomalous}", METADATA) is True

    def test_inference_json_placeholder(self):
        rendered = render_template("{inference_json}", METADATA)
        assert '"is_anomalous": true' in rendered
        assert '"confidence": 0.9' in rendered

    def test_mixed_template_formats_as_string(self):
        rendered = render_template(
            "anomalous={is_anomalous} score={confidence}", METADATA)
        assert rendered == "anomalous=True score=0.9"

    def test_unknown_placeholder_left_intact(self):
        assert render_template("{nope}!", METADATA) == "{nope}!"


# --------------------------------------------------------------------------
# Digital output (Requirement 9.4)
# --------------------------------------------------------------------------

class TestDigitalOutput:
    PARAMS = {
        "pin": 245,
        "signal_type": "pulse",
        "pulse_width_ms": 500,
        "condition": "is_anomalous == true && confidence >= 0.8",
    }

    def test_actuates_when_condition_true(self, processor, clients):
        processor(None, document(
            binding("d1", "digital_output", self.PARAMS)), METADATA)
        assert clients["dio"].calls == [(245, "pulse", 500)]

    def test_not_actuated_when_condition_false(self, processor, clients):
        params = dict(self.PARAMS, condition="confidence > 0.95")
        processor(None, document(
            binding("d1", "digital_output", params)), METADATA)
        assert clients["dio"].calls == []

    def test_not_actuated_when_condition_malformed(self, processor, clients):
        params = dict(self.PARAMS, condition="confidence >= ")
        processor(None, document(
            binding("d1", "digital_output", params)), METADATA)
        assert clients["dio"].calls == []


# --------------------------------------------------------------------------
# MQTT publish (Requirement 9.5)
# --------------------------------------------------------------------------

class TestMqttPublish:
    def test_publishes_rendered_payload_to_broker(self, processor, clients):
        processor(None, document(binding("m1", "mqtt_publish", {
            "broker_host": "broker.local",
            "broker_port": 8883,
            "topic": "dda/results",
            "payload_template": "anomalous={is_anomalous}",
            "qos": 1,
        })), METADATA)
        assert clients["mqtt"].calls == [
            ("broker.local", 8883, "dda/results", "anomalous=True", 1)
        ]

    def test_defaults_port_qos_and_payload_template(self, processor, clients):
        processor(None, document(binding("m1", "mqtt_publish", {
            "broker_host": "broker.local",
            "topic": "dda/results",
        })), METADATA)
        (host, port, topic, payload, qos), = clients["mqtt"].calls
        assert (host, port, topic, qos) == ("broker.local", 1883, "dda/results", 0)
        assert '"is_anomalous": true' in payload


# --------------------------------------------------------------------------
# MQTT publish to AWS IoT Core (aws_iot enabled)
# --------------------------------------------------------------------------

class TestMqttPublishAwsIot:
    PARAMS = {
        "broker_host": "abc123-ats.iot.us-east-1.amazonaws.com",
        "topic": "dda/results",
        "aws_iot": True,
        "iot_thing_name": "edge-device-01",
        "iot_ca_cert_path": "/greengrass/v2/AmazonRootCA1.pem",
        "iot_client_cert_path": "/greengrass/v2/thingCert.crt",
        "iot_private_key_path": "/greengrass/v2/privKey.key",
    }

    def test_publishes_with_thing_client_id_and_tls_paths(self, processor, clients):
        processor(None, document(binding(
            "m1", "mqtt_publish", dict(self.PARAMS, qos=1))), METADATA)
        (host, port, topic, payload, qos, client_id, tls), = clients["mqtt"].calls
        assert host == "abc123-ats.iot.us-east-1.amazonaws.com"
        assert topic == "dda/results"
        assert qos == 1
        assert client_id == "edge-device-01"
        assert tls == {
            "ca_certs": "/greengrass/v2/AmazonRootCA1.pem",
            "certfile": "/greengrass/v2/thingCert.crt",
            "keyfile": "/greengrass/v2/privKey.key",
        }

    def test_default_broker_port_switches_to_8883(self, processor, clients):
        # broker_port left at the catalog default (1883): mutual TLS uses
        # the standard 8883 instead.
        processor(None, document(binding(
            "m1", "mqtt_publish", self.PARAMS)), METADATA)
        (_, port, _, _, _, _, _), = clients["mqtt"].calls
        assert port == 8883

    def test_explicit_broker_port_is_respected(self, processor, clients):
        processor(None, document(binding(
            "m1", "mqtt_publish", dict(self.PARAMS, broker_port=443))), METADATA)
        (_, port, _, _, _, _, _), = clients["mqtt"].calls
        assert port == 443

    def test_qos_2_is_clamped_to_1(self, processor, clients):
        # AWS IoT Core does not support MQTT QoS 2.
        processor(None, document(binding(
            "m1", "mqtt_publish", dict(self.PARAMS, qos=2))), METADATA)
        (_, _, _, _, qos, _, _), = clients["mqtt"].calls
        assert qos == 1

    def test_qos_0_is_not_clamped(self, processor, clients):
        processor(None, document(binding(
            "m1", "mqtt_publish", dict(self.PARAMS, qos=0))), METADATA)
        (_, _, _, _, qos, _, _), = clients["mqtt"].calls
        assert qos == 0

    @pytest.mark.parametrize("missing", [
        "iot_thing_name",
        "iot_ca_cert_path",
        "iot_client_cert_path",
        "iot_private_key_path",
    ])
    def test_missing_iot_parameter_is_contained(self, processor, clients, missing):
        # Never publish insecurely: the binding fails (contained per
        # 13.7) instead of falling back to a plain connection, and other
        # bindings are unaffected.
        params = {key: value for key, value in self.PARAMS.items()
                  if key != missing}
        processor(None, document(
            binding("m1", "mqtt_publish", params),
            binding("m2", "mqtt_publish", {"broker_host": "b", "topic": "t2"}),
        ), METADATA)
        (_, _, topic, _, _), = clients["mqtt"].calls
        assert topic == "t2"

    def test_aws_iot_disabled_publishes_plain(self, processor, clients):
        processor(None, document(binding("m1", "mqtt_publish", dict(
            self.PARAMS, aws_iot=False))), METADATA)
        (host, port, topic, payload, qos), = clients["mqtt"].calls
        assert port == 1883

    def test_default_publisher_forwards_client_id_and_tls_to_paho(self):
        # The real _default_mqtt_publisher against a fake paho module
        # (paho is not installed on this host): publish.single receives
        # the thing-name client id and the tls_set argument dict.
        published = []

        def single(topic, payload=None, qos=0, hostname=None, port=1883,
                   client_id="", tls=None):
            published.append({
                "topic": topic, "payload": payload, "qos": qos,
                "hostname": hostname, "port": port,
                "client_id": client_id, "tls": tls,
            })

        publish_module = types.ModuleType("paho.mqtt.publish")
        publish_module.single = single
        mqtt_module = types.ModuleType("paho.mqtt")
        mqtt_module.publish = publish_module
        paho_module = types.ModuleType("paho")
        paho_module.mqtt = mqtt_module
        with patch.dict(sys.modules, {
            "paho": paho_module,
            "paho.mqtt": mqtt_module,
            "paho.mqtt.publish": publish_module,
        }):
            _default_mqtt_publisher(
                "endpoint.iot", 8883, "dda/results", "payload", 1,
                client_id="edge-device-01",
                tls={"ca_certs": "/ca.pem", "certfile": "/cert.crt",
                     "keyfile": "/key.key"},
            )
        assert published == [{
            "topic": "dda/results", "payload": "payload", "qos": 1,
            "hostname": "endpoint.iot", "port": 8883,
            "client_id": "edge-device-01",
            "tls": {"ca_certs": "/ca.pem", "certfile": "/cert.crt",
                    "keyfile": "/key.key"},
        }]


# --------------------------------------------------------------------------
# OPC UA write (Requirement 9.6)
# --------------------------------------------------------------------------

class TestOpcuaWrite:
    def test_writes_rendered_value_to_node(self, processor, clients):
        processor(None, document(binding("o1", "opcua_write", {
            "endpoint": "opc.tcp://plc.local:4840",
            "node_id": "ns=2;s=Defect",
            "value_template": "{is_anomalous}",
        })), METADATA)
        assert clients["opcua"].calls == [
            ("opc.tcp://plc.local:4840", "ns=2;s=Defect", True)
        ]

    def test_default_value_template(self, processor, clients):
        processor(None, document(binding("o1", "opcua_write", {
            "endpoint": "opc.tcp://plc.local:4840",
            "node_id": "ns=2;s=Defect",
        })), METADATA)
        assert clients["opcua"].calls == [
            ("opc.tcp://plc.local:4840", "ns=2;s=Defect", True)
        ]


# --------------------------------------------------------------------------
# inference_filter gating
# --------------------------------------------------------------------------

class TestInferenceFilterGating:
    def test_passing_filter_lets_downstream_run(self, processor, clients):
        processor(None, document(
            binding("f1", "inference_filter",
                    {"condition": "confidence >= 0.8"}, downstream=["m1"]),
            binding("m1", "mqtt_publish",
                    {"broker_host": "b", "topic": "t"}, upstream=["f1"]),
        ), METADATA)
        assert len(clients["mqtt"].calls) == 1

    def test_failing_filter_gates_downstream(self, processor, clients):
        processor(None, document(
            binding("f1", "inference_filter",
                    {"condition": "confidence > 0.95"}, downstream=["m1"]),
            binding("m1", "mqtt_publish",
                    {"broker_host": "b", "topic": "t"}, upstream=["f1"]),
        ), METADATA)
        assert clients["mqtt"].calls == []

    def test_unevaluable_filter_gates_downstream(self, processor, clients):
        processor(None, document(
            binding("f1", "inference_filter",
                    {"condition": "bogus_field == 1"}, downstream=["o1"]),
            binding("o1", "opcua_write",
                    {"endpoint": "opc.tcp://p:4840", "node_id": "n"},
                    upstream=["f1"]),
        ), METADATA)
        assert clients["opcua"].calls == []

    def test_gate_applies_only_to_downstream_of_filter(self, processor, clients):
        processor(None, document(
            binding("f1", "inference_filter",
                    {"condition": "confidence > 0.95"}, downstream=["m1"]),
            binding("m1", "mqtt_publish",
                    {"broker_host": "b", "topic": "t"}, upstream=["f1"]),
            binding("m2", "mqtt_publish",
                    {"broker_host": "b", "topic": "t2"}, upstream=["n0"]),
        ), METADATA)
        (_, _, topic, _, _), = clients["mqtt"].calls
        assert topic == "t2"


# --------------------------------------------------------------------------
# conditional gating (two-path routing)
# --------------------------------------------------------------------------

def conditional_binding(node_id, condition, true_downstream, false_downstream):
    """The conditional executor-binding entry as the compiler emits it: the
    "true" port gated by the condition, the "false" port by "!(condition)",
    with the downstream node ids partitioned per output port."""
    entry = binding(
        node_id, "conditional", {"condition": condition},
        downstream=list(true_downstream) + list(false_downstream),
    )
    entry["downstreamNodeIdsByPort"] = {
        "true": list(true_downstream),
        "false": list(false_downstream),
    }
    entry["portConditions"] = {
        "true": condition,
        "false": "!({0})".format(condition),
    }
    return entry


class TestConditionalGating:
    def _document(self, condition="is_anomalous == true"):
        """conditional b1: mqtt m1 on the true path, dio d1 on the false path
        (the andon-light story: red on anomaly, green on normal)."""
        return document(
            conditional_binding("b1", condition, ["m1"], ["d1"]),
            binding("m1", "mqtt_publish",
                    {"broker_host": "b", "topic": "anomaly"}, upstream=["b1"]),
            binding("d1", "digital_output",
                    {"pin": 5, "signal_type": "pulse", "pulse_width_ms": 10,
                     "condition": "true"},
                    upstream=["b1"]),
        )

    def test_true_path_fires_and_false_path_is_gated_on_anomaly(
            self, processor, clients):
        processor(None, self._document(), METADATA)  # is_anomalous: True
        assert len(clients["mqtt"].calls) == 1
        assert clients["dio"].calls == []

    def test_false_path_fires_and_true_path_is_gated_on_normal(
            self, processor, clients):
        processor(None, self._document(),
                  {"is_anomalous": False, "confidence": 0.9})
        assert clients["mqtt"].calls == []
        assert clients["dio"].calls == [(5, "pulse", 10)]

    def test_negated_composite_condition_routes_the_false_path(
            self, processor, clients):
        # 0.9 < 0.95: the composite condition fails, so the false path
        # (the negation) receives the metadata.
        processor(None, self._document(
            "is_anomalous == true && confidence >= 0.95"), METADATA)
        assert clients["mqtt"].calls == []
        assert len(clients["dio"].calls) == 1

    def test_unevaluable_conditional_condition_gates_both_paths(
            self, processor, clients):
        # Never actuate on an unevaluable rule: neither side fires.
        processor(None, self._document("bogus_field == 1"), METADATA)
        assert clients["mqtt"].calls == []
        assert clients["dio"].calls == []

    def test_gate_applies_only_to_downstream_of_conditional(
            self, processor, clients):
        doc = self._document()
        doc["executorBindings"].append(
            binding("m2", "mqtt_publish",
                    {"broker_host": "b", "topic": "unrelated"},
                    upstream=["n0"]))
        processor(None, doc, {"is_anomalous": False, "confidence": 0.9})
        # The true path is gated; the unrelated publish still runs.
        topics = [call[2] for call in clients["mqtt"].calls]
        assert topics == ["unrelated"]

    def test_conditional_binding_itself_performs_no_action(self, processor, clients):
        processor(None, document(
            conditional_binding("b1", "is_anomalous == true", [], [])), METADATA)
        assert clients["mqtt"].calls == []
        assert clients["dio"].calls == []
        assert clients["opcua"].calls == []


# --------------------------------------------------------------------------
# Containment and skipping (Requirement 13.7)
# --------------------------------------------------------------------------

class TestContainment:
    def test_failing_binding_does_not_affect_others(self, clients):
        processor = OutputBindingProcessor(
            dio_actuator=clients["dio"],
            mqtt_publisher=Recorder(error=RuntimeError("broker down")),
            opcua_writer=clients["opcua"],
        )
        # No exception propagates, and the later bindings still run.
        processor(None, document(
            binding("m1", "mqtt_publish", {"broker_host": "b", "topic": "t"}),
            binding("o1", "opcua_write",
                    {"endpoint": "opc.tcp://p:4840", "node_id": "n"}),
            binding("d1", "digital_output",
                    {"pin": 1, "signal_type": "high", "condition": "true"}),
        ), METADATA)
        assert len(clients["opcua"].calls) == 1
        assert len(clients["dio"].calls) == 1

    def test_missing_required_parameter_is_contained(self, processor, clients):
        processor(None, document(
            binding("m1", "mqtt_publish", {"topic": "t"}),  # no broker_host
            binding("m2", "mqtt_publish", {"broker_host": "b", "topic": "t2"}),
        ), METADATA)
        (_, _, topic, _, _), = clients["mqtt"].calls
        assert topic == "t2"

    def test_input_and_unknown_bindings_are_skipped(self, processor, clients):
        processor(None, document(
            binding("i1", "digital_input", {"pin": 7}),
            binding("r1", "recording_mqtt_publish", {"topic": "t"}),
            binding("x1", "something_else", {}),
        ), METADATA)
        assert clients["dio"].calls == []
        assert clients["mqtt"].calls == []
        assert clients["opcua"].calls == []

    def test_no_bindings_is_a_no_op(self, processor, clients):
        processor(None, document(), METADATA)
        assert clients["mqtt"].calls == []
