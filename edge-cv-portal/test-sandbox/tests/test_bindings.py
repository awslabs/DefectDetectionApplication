"""Recording-stub execution of simulation executor bindings
(Requirement 12.6): would-be actuations are recorded with parameters
and triggering metadata; nothing contacts an endpoint."""

import pytest

from harness.bindings import (
    evaluate_condition,
    execute_bindings,
    is_recording_binding,
)
from harness.results import ResultsStore


class TestConditionEvaluation:
    @pytest.mark.parametrize("condition,metadata,expected", [
        ("is_anomalous == true", {"is_anomalous": True}, True),
        ("is_anomalous == true", {"is_anomalous": False}, False),
        # Tag values arrive as strings from the GStreamer tag list.
        ("is_anomalous == true", {"is_anomalous": "true"}, True),
        ("confidence >= 0.8", {"confidence": 0.9}, True),
        ("confidence >= 0.8", {"confidence": "0.75"}, False),
        ("is_anomalous == true && confidence >= 0.8",
         {"is_anomalous": True, "confidence": 0.85}, True),
        ("is_anomalous == true && confidence >= 0.8",
         {"is_anomalous": True, "confidence": 0.5}, False),
        ("is_anomalous == true || confidence >= 0.8",
         {"is_anomalous": False, "confidence": 0.9}, True),
        ("(is_anomalous == false) || (confidence > 1)",
         {"is_anomalous": False, "confidence": 0}, True),
        ("is_anomalous != false", {"is_anomalous": True}, True),
        ("is_anomalous", {"is_anomalous": True}, True),
        ("label == 'defect'", {"label": "defect"}, True),
        # Unary negation (the compiler composes the conditional node's
        # "false"-path gate as "!(condition)").
        ("!(is_anomalous == true)", {"is_anomalous": True}, False),
        ("!(is_anomalous == true)", {"is_anomalous": False}, True),
        ("!is_anomalous", {"is_anomalous": False}, True),
        ("!!is_anomalous", {"is_anomalous": True}, True),
        ("!(is_anomalous == true && confidence >= 0.8)",
         {"is_anomalous": True, "confidence": 0.5}, True),
        ("!(is_anomalous) || confidence >= 0.8",
         {"is_anomalous": True, "confidence": 0.9}, True),
    ])
    def test_evaluation(self, condition, metadata, expected):
        assert evaluate_condition(condition, metadata) is expected

    @pytest.mark.parametrize("condition", [
        "", "&&", "confidence >=", "confidence >= 0.8 extra",
        "unknown_field == 1",  # field absent from metadata
        "confidence == @bad@",
        "!", "! &&", "confidence == !",
    ])
    def test_malformed_or_unknown_raises(self, condition):
        with pytest.raises(ValueError):
            evaluate_condition(condition, {"confidence": 1})


class TestIsRecordingBinding:
    def test_recording_prefix_detected(self):
        assert is_recording_binding({"binding": "recording_mqtt_publish"})
        assert not is_recording_binding({"binding": "inference_filter"})
        assert not is_recording_binding({"binding": "mqtt_publish"})


def _store(node_ids):
    snapshots = []
    store = ResultsStore(node_ids, snapshots.append)
    return store, snapshots


METADATA = {"is_anomalous": True, "confidence": 0.9}


class TestExecuteBindings:
    def test_recording_stub_records_parameters_and_metadata(self):
        store, snapshots = _store(["mq"])
        bindings = [{
            "nodeId": "mq",
            "binding": "recording_mqtt_publish",
            "parameters": {"broker_host": "10.0.0.5", "topic": "dda/results"},
            "upstreamNodeIds": ["inf"],
            "downstreamNodeIds": [],
        }]
        execute_bindings(bindings, METADATA, store)

        record = store.record("mq")
        assert record["status"] == "completed"
        activity = record["stubActivity"][0]
        assert activity["type"] == "recorded_actuation"
        assert activity["binding"] == "recording_mqtt_publish"
        assert activity["parameters"]["topic"] == "dda/results"
        assert activity["triggered"] is True
        assert activity["triggeringMetadata"] == METADATA
        assert snapshots  # flushed incrementally

    def test_filter_gates_downstream_recorder(self):
        store, _ = _store(["filt", "dout"])
        bindings = [
            {"nodeId": "filt", "binding": "inference_filter",
             "parameters": {"condition": "confidence >= 0.99"},
             "upstreamNodeIds": ["inf"], "downstreamNodeIds": ["dout"]},
            {"nodeId": "dout", "binding": "recording_digital_output",
             "parameters": {"pin": 4, "signal_type": "pulse",
                            "condition": "is_anomalous == true"},
             "upstreamNodeIds": ["filt"], "downstreamNodeIds": []},
        ]
        execute_bindings(bindings, METADATA, store)

        filt = store.record("filt")
        assert filt["status"] == "completed"
        assert filt["outputs"][0]["result"] is False  # 0.9 < 0.99

        activity = store.record("dout")["stubActivity"][0]
        assert activity["triggered"] is False  # gated out by the filter

    def test_own_condition_gates_actuation(self):
        store, _ = _store(["dout"])
        bindings = [{
            "nodeId": "dout", "binding": "recording_digital_output",
            "parameters": {"pin": 4, "condition": "is_anomalous == false"},
            "upstreamNodeIds": [], "downstreamNodeIds": [],
        }]
        execute_bindings(bindings, METADATA, store)
        activity = store.record("dout")["stubActivity"][0]
        assert activity["triggered"] is False
        assert store.record("dout")["status"] == "completed"

    def test_malformed_filter_condition_fails_node_and_gates(self):
        store, _ = _store(["filt", "mq"])
        bindings = [
            {"nodeId": "filt", "binding": "inference_filter",
             "parameters": {"condition": "not_a_field == 1"},
             "upstreamNodeIds": [], "downstreamNodeIds": ["mq"]},
            {"nodeId": "mq", "binding": "recording_mqtt_publish",
             "parameters": {"topic": "t"},
             "upstreamNodeIds": ["filt"], "downstreamNodeIds": []},
        ]
        execute_bindings(bindings, METADATA, store)
        assert store.record("filt")["status"] == "failed"
        assert store.record("filt")["error"]["code"] == "FILTER_CONDITION_ERROR"
        assert store.record("mq")["stubActivity"][0]["triggered"] is False
        assert store.has_failure()

    def _conditional_bindings(self, condition="is_anomalous == true"):
        """conditional 'br' routing to 'mq' (true path) and 'dout' (false
        path), the shape the compiler emits for the conditional node."""
        return [
            {"nodeId": "br", "binding": "conditional",
             "parameters": {"condition": condition},
             "upstreamNodeIds": ["inf"], "downstreamNodeIds": ["mq", "dout"],
             "downstreamNodeIdsByPort": {"true": ["mq"], "false": ["dout"]},
             "portConditions": {"true": condition,
                                "false": "!({0})".format(condition)}},
            {"nodeId": "mq", "binding": "recording_mqtt_publish",
             "parameters": {"topic": "t"},
             "upstreamNodeIds": ["br"], "downstreamNodeIds": []},
            {"nodeId": "dout", "binding": "recording_digital_output",
             "parameters": {"pin": 4, "signal_type": "pulse"},
             "upstreamNodeIds": ["br"], "downstreamNodeIds": []},
        ]

    def test_conditional_true_path_fires_and_false_path_is_gated(self):
        # METADATA is anomalous: the true-path recorder triggers, the
        # false-path recorder records untriggered.
        store, _ = _store(["br", "mq", "dout"])
        execute_bindings(self._conditional_bindings(), METADATA, store)

        conditional = store.record("br")
        assert conditional["status"] == "completed"
        assert conditional["outputs"][0]["type"] == "conditional_evaluation"
        assert conditional["outputs"][0]["results"] == {"true": True, "false": False}

        assert store.record("mq")["stubActivity"][0]["triggered"] is True
        assert store.record("dout")["stubActivity"][0]["triggered"] is False

    def test_conditional_false_path_fires_when_condition_does_not_hold(self):
        store, _ = _store(["br", "mq", "dout"])
        execute_bindings(self._conditional_bindings(), {"is_anomalous": False}, store)

        conditional = store.record("br")
        assert conditional["outputs"][0]["results"] == {"true": False, "false": True}
        assert store.record("mq")["stubActivity"][0]["triggered"] is False
        assert store.record("dout")["stubActivity"][0]["triggered"] is True

    def test_malformed_conditional_condition_fails_node_and_gates_both_paths(self):
        store, _ = _store(["br", "mq", "dout"])
        execute_bindings(self._conditional_bindings("not_a_field == 1"),
                         METADATA, store)

        assert store.record("br")["status"] == "failed"
        assert store.record("br")["error"]["code"] == "CONDITIONAL_CONDITION_ERROR"
        # Never actuate on an unevaluable rule: both sides gate.
        assert store.record("mq")["stubActivity"][0]["triggered"] is False
        assert store.record("dout")["stubActivity"][0]["triggered"] is False
        assert store.has_failure()

    # Feature: modbus-tcp-output — recording_modbus_write resolves through
    # the same prefix-generic recorder path as the existing hardware
    # outputs (Requirements 7.1, 7.2); zero harness changes.
    MODBUS_PARAMETERS = {
        "host": "192.168.1.30", "port": 502, "unit_id": 1,
        "register_type": "coil", "address": 12,
        "value_template": "{is_anomalous}", "pulse_ms": 0,
    }

    def _modbus_conditional_bindings(self, condition="is_anomalous == true"):
        """conditional 'br' routing to 'mb_true' (true path) and 'mb_false'
        (false path), each a recording_modbus_write stub."""
        return [
            {"nodeId": "br", "binding": "conditional",
             "parameters": {"condition": condition},
             "upstreamNodeIds": ["inf"],
             "downstreamNodeIds": ["mb_true", "mb_false"],
             "downstreamNodeIdsByPort": {"true": ["mb_true"],
                                         "false": ["mb_false"]},
             "portConditions": {"true": condition,
                                "false": "!({0})".format(condition)}},
            {"nodeId": "mb_true", "binding": "recording_modbus_write",
             "parameters": dict(self.MODBUS_PARAMETERS),
             "upstreamNodeIds": ["br"], "downstreamNodeIds": []},
            {"nodeId": "mb_false", "binding": "recording_modbus_write",
             "parameters": dict(self.MODBUS_PARAMETERS,
                                register_type="holding_register",
                                address=40001),
             "upstreamNodeIds": ["br"], "downstreamNodeIds": []},
        ]

    def test_modbus_recording_records_parameters_and_metadata_when_gate_passes(self):
        # Requirement 7.1: a recording_modbus_write whose gates passed is
        # recorded with its parameters and the triggering metadata.
        store, snapshots = _store(["br", "mb_true", "mb_false"])
        execute_bindings(self._modbus_conditional_bindings(), METADATA, store)

        record = store.record("mb_true")
        assert record["status"] == "completed"
        activity = record["stubActivity"][0]
        assert activity["type"] == "recorded_actuation"
        assert activity["binding"] == "recording_modbus_write"
        assert activity["parameters"] == self.MODBUS_PARAMETERS
        assert activity["triggered"] is True
        assert activity["triggeringMetadata"] == METADATA
        assert snapshots  # flushed incrementally

    def test_modbus_recording_not_triggered_behind_false_conditional(self):
        # Requirement 7.2: a recording_modbus_write gated out by the
        # conditional's non-passing port records as not triggered.
        store, _ = _store(["br", "mb_true", "mb_false"])
        execute_bindings(self._modbus_conditional_bindings(), METADATA, store)

        activity = store.record("mb_false")["stubActivity"][0]
        assert activity["triggered"] is False
        assert activity["binding"] == "recording_modbus_write"
        assert store.record("mb_false")["status"] == "completed"

    def test_non_recording_non_filter_bindings_untouched(self):
        # A non-simulation binding (device digital_input) is not executed
        # by the recorder path; nothing is recorded against it here.
        store, _ = _store(["din"])
        execute_bindings([{"nodeId": "din", "binding": "digital_input",
                           "parameters": {}, "upstreamNodeIds": [],
                           "downstreamNodeIds": []}], METADATA, store)
        assert store.record("din")["status"] == "pending"
        assert store.record("din")["stubActivity"] == []
