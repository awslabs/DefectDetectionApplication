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
"""Tests for output bindings' sent-message / skipped-outcome detail recording
(output-node-sent-message feature; Requirements 1.1-1.5, 2.1, 2.3, 3.1-3.4).

An injectable ``detail_sink`` on ``OutputBindingProcessor.process()`` receives
``(node_id, detail)`` summaries: what an mqtt/opcua/dio binding sent, or why a
binding did not fire. These tests inject fake clients + a recording sink so
composition, truncation, skipped outcomes, sink containment, failure
precedence, and detail_sink=None baseline equality are exercised without GPIO,
a broker, or an OPC UA server. Hypothesis covers the composition across payload
templates/metadata and the 512-char preview bound.
"""
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.node_status import (
    NodeStatusCollector,
    STATUS_FAILURE,
)
from workflow_engine.output_bindings import (
    DETAIL_PREVIEW_LIMIT,
    OutputBindingError,
    OutputBindingProcessor,
    _preview,
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


def conditional_binding(node_id, condition, true_downstream, false_downstream):
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


class RecordingSink:
    """A detail_sink recording (node_id, detail) pairs."""

    def __init__(self, error=None):
        self.details = []
        self.error = error

    def __call__(self, node_id, detail):
        self.details.append((node_id, detail))
        if self.error is not None:
            raise self.error

    def detail_for(self, node_id):
        for nid, detail in self.details:
            if nid == node_id:
                return detail
        return None


def make_processor(clients):
    return OutputBindingProcessor(
        dio_actuator=clients["dio"],
        mqtt_publisher=clients["mqtt"],
        opcua_writer=clients["opcua"],
        greengrass_publisher=clients["greengrass"],
    )


@pytest.fixture
def clients():
    return {
        "dio": Recorder(),
        "mqtt": Recorder(),
        "opcua": Recorder(),
        "greengrass": Recorder(),
    }


@pytest.fixture
def processor(clients):
    return make_processor(clients)


# --------------------------------------------------------------------------
# _preview truncation helper (Requirement 2.3)
# --------------------------------------------------------------------------

class TestPreview:
    def test_short_text_unchanged(self):
        assert _preview("hello") == "hello"

    def test_text_at_limit_unchanged(self):
        text = "x" * DETAIL_PREVIEW_LIMIT
        assert _preview(text) == text

    def test_long_text_truncated_with_ellipsis(self):
        text = "x" * (DETAIL_PREVIEW_LIMIT + 50)
        out = _preview(text)
        assert out == "x" * DETAIL_PREVIEW_LIMIT + "\u2026"
        assert len(out) == DETAIL_PREVIEW_LIMIT + 1

    def test_none_is_empty(self):
        assert _preview(None) == ""

    @given(
        text=st.text(min_size=0, max_size=2000),
        limit=st.integers(min_value=1, max_value=1024),
    )
    def test_preview_never_exceeds_bound(self, text, limit):
        out = _preview(text, limit)
        if len(text) <= limit:
            assert out == text
        else:
            # The ellipsis marker is exactly one extra char.
            assert len(out) == limit + 1
            assert out.endswith("\u2026")
            assert out[:limit] == text[:limit]


# --------------------------------------------------------------------------
# Property 1: successful sends record bounded sent-message details
# (Requirements 1.1, 1.2, 1.3, 2.1, 2.3)
# --------------------------------------------------------------------------

_metadata = st.fixed_dictionaries({
    "is_anomalous": st.booleans(),
    "confidence": st.floats(
        min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
})

_payload_templates = st.sampled_from([
    "{inference_json}",
    "{is_anomalous}",
    "anomaly={is_anomalous} conf={confidence}",
    "static message",
])


class TestMqttSentDetail:
    @settings(max_examples=60, deadline=None)
    @given(metadata=_metadata, template=_payload_templates, qos=st.integers(0, 2))
    def test_mqtt_success_records_bounded_detail(self, metadata, template, qos):
        clients = {
            "dio": Recorder(), "mqtt": Recorder(),
            "opcua": Recorder(), "greengrass": Recorder(),
        }
        sink = RecordingSink()
        processor = make_processor(clients)
        params = {"broker_host": "b", "topic": "factory/line1",
                  "topic_qos": qos, "qos": qos, "payload_template": template}
        processor.process(
            None, document(binding("m1", "mqtt_publish", params)),
            metadata, detail_sink=sink,
        )
        detail = sink.detail_for("m1")
        assert detail is not None
        # Identifying fields present (Requirement 1.1).
        assert "factory/line1" in detail
        assert "qos {0}".format(qos) in detail
        assert "plain" in detail
        # The rendered payload preview is present and bounded (2.3).
        payload = render_template(template, metadata)
        payload_text = (
            payload if isinstance(payload, str)
            else json.dumps(payload, default=str)
        )
        assert _preview(payload_text) in detail

    def test_mqtt_payload_truncated_to_bound(self, processor, clients):
        sink = RecordingSink()
        long_msg = "A" * (DETAIL_PREVIEW_LIMIT + 100)
        params = {"broker_host": "b", "topic": "t",
                  "payload_template": long_msg}
        processor.process(
            None, document(binding("m1", "mqtt_publish", params)),
            METADATA, detail_sink=sink,
        )
        detail = sink.detail_for("m1")
        assert "\u2026" in detail
        # The payload portion is the truncated preview.
        assert ("A" * DETAIL_PREVIEW_LIMIT + "\u2026") in detail

    def test_mqtt_aws_iot_path_named(self, processor, clients):
        sink = RecordingSink()
        params = {
            "broker_host": "b", "topic": "t", "qos": 1, "aws_iot": True,
            "iot_thing_name": "thing", "iot_ca_cert_path": "/ca",
            "iot_client_cert_path": "/cert", "iot_private_key_path": "/key",
        }
        processor.process(
            None, document(binding("m1", "mqtt_publish", params)),
            METADATA, detail_sink=sink,
        )
        assert "aws_iot" in sink.detail_for("m1")

    def test_mqtt_greengrass_path_named(self, processor, clients):
        sink = RecordingSink()
        params = {"topic": "t", "qos": 1, "greengrass": True}
        processor.process(
            None, document(binding("m1", "mqtt_publish", params)),
            METADATA, detail_sink=sink,
        )
        assert "greengrass" in sink.detail_for("m1")


class TestOpcuaSentDetail:
    @settings(max_examples=40, deadline=None)
    @given(metadata=_metadata)
    def test_opcua_success_records_detail(self, metadata):
        clients = {
            "dio": Recorder(), "mqtt": Recorder(),
            "opcua": Recorder(), "greengrass": Recorder(),
        }
        sink = RecordingSink()
        processor = make_processor(clients)
        params = {"endpoint": "opc.tcp://p:4840", "node_id": "ns=2;s=Tag1"}
        processor.process(
            None, document(binding("o1", "opcua_write", params)),
            metadata, detail_sink=sink,
        )
        detail = sink.detail_for("o1")
        assert detail is not None
        assert "ns=2;s=Tag1" in detail
        assert "opc.tcp://p:4840" in detail
        assert detail.startswith("wrote ")

    def test_opcua_value_preview_bounded(self, processor, clients):
        sink = RecordingSink()
        long_value = "V" * (DETAIL_PREVIEW_LIMIT + 100)
        params = {"endpoint": "opc.tcp://p:4840", "node_id": "n",
                  "value_template": long_value}
        processor.process(
            None, document(binding("o1", "opcua_write", params)),
            METADATA, detail_sink=sink,
        )
        assert "\u2026" in sink.detail_for("o1")


class TestDioSentDetail:
    def test_pulse_records_pulsed_detail(self, processor, clients):
        sink = RecordingSink()
        params = {"pin": 7, "signal_type": "pulse", "pulse_width_ms": 100}
        processor.process(
            None, document(binding("d1", "digital_output", params)),
            METADATA, detail_sink=sink,
        )
        assert sink.detail_for("d1") == "pulsed pin 7 (100ms)"

    def test_high_records_set_detail(self, processor, clients):
        sink = RecordingSink()
        params = {"pin": 3, "signal_type": "high"}
        processor.process(
            None, document(binding("d1", "digital_output", params)),
            METADATA, detail_sink=sink,
        )
        assert sink.detail_for("d1") == "set pin 3 high"


# --------------------------------------------------------------------------
# Skipped outcomes (Requirement 1.4)
# --------------------------------------------------------------------------

class TestSkippedOutcomes:
    def test_gated_out_by_filter_records_skip_detail(self, processor, clients):
        sink = RecordingSink()
        doc = document(
            binding("f1", "inference_filter",
                    {"condition": "confidence > 0.95"}, downstream=["m1"]),
            binding("m1", "mqtt_publish",
                    {"broker_host": "b", "topic": "t"}, upstream=["f1"]),
        )
        processor.process(None, doc, METADATA, detail_sink=sink)
        assert clients["mqtt"].calls == []
        detail = sink.detail_for("m1")
        assert detail == (
            "not sent: gated out by an upstream inference filter or conditional"
        )

    def test_gated_out_by_conditional_records_skip_detail(self, processor, clients):
        sink = RecordingSink()
        doc = document(
            conditional_binding("b1", "is_anomalous == true", ["m1"], ["d1"]),
            binding("m1", "mqtt_publish",
                    {"broker_host": "b", "topic": "t"}, upstream=["b1"]),
            binding("d1", "digital_output",
                    {"pin": 5, "signal_type": "high"}, upstream=["b1"]),
        )
        # is_anomalous True -> true path (m1) fires, false path (d1) gated.
        processor.process(None, doc, METADATA, detail_sink=sink)
        assert sink.detail_for("d1") == (
            "not sent: gated out by an upstream inference filter or conditional"
        )
        assert "sent to topic 't'" in sink.detail_for("m1")

    def test_condition_false_records_skip_detail(self, processor, clients):
        sink = RecordingSink()
        params = {"pin": 5, "signal_type": "high",
                  "condition": "confidence > 0.95"}
        processor.process(
            None, document(binding("d1", "digital_output", params)),
            METADATA, detail_sink=sink,
        )
        assert clients["dio"].calls == []
        assert sink.detail_for("d1") == (
            "not sent: condition 'confidence > 0.95' evaluated false"
        )

    def test_condition_unevaluable_records_skip_detail(self, processor, clients):
        sink = RecordingSink()
        params = {"pin": 5, "signal_type": "high", "condition": "confidence >= "}
        processor.process(
            None, document(binding("d1", "digital_output", params)),
            METADATA, detail_sink=sink,
        )
        assert clients["dio"].calls == []
        assert sink.detail_for("d1") == (
            "not sent: condition 'confidence >= ' could not be evaluated"
        )


# --------------------------------------------------------------------------
# Property 2: preservation - behavior identical apart from detail recording
# (Requirements 1.5, 3.1, 3.2, 3.3, 3.4)
# --------------------------------------------------------------------------

def _mixed_document():
    return document(
        binding("m1", "mqtt_publish",
                {"broker_host": "b", "topic": "t", "qos": 1}),
        binding("o1", "opcua_write",
                {"endpoint": "opc.tcp://p:4840", "node_id": "n"}),
        binding("d1", "digital_output",
                {"pin": 7, "signal_type": "pulse", "pulse_width_ms": 50}),
    )


class TestPreservation:
    def test_detail_sink_none_baseline_equality(self):
        # Same client calls with detail_sink=None vs a passive sink.
        c_none = {"dio": Recorder(), "mqtt": Recorder(),
                  "opcua": Recorder(), "greengrass": Recorder()}
        c_sink = {"dio": Recorder(), "mqtt": Recorder(),
                  "opcua": Recorder(), "greengrass": Recorder()}
        make_processor(c_none).process(None, _mixed_document(), METADATA)
        make_processor(c_sink).process(
            None, _mixed_document(), METADATA, detail_sink=RecordingSink())
        for key in ("dio", "mqtt", "opcua", "greengrass"):
            assert c_none[key].calls == c_sink[key].calls

    def test_error_aggregation_identical_with_and_without_sink(self):
        # A failing opcua writer -> OutputBindingError naming o1, in BOTH
        # runs; the other bindings still fire in both.
        def run(sink):
            clients = {
                "dio": Recorder(), "mqtt": Recorder(),
                "opcua": Recorder(error=RuntimeError("boom")),
                "greengrass": Recorder(),
            }
            with pytest.raises(OutputBindingError) as excinfo:
                make_processor(clients).process(
                    None, _mixed_document(), METADATA, detail_sink=sink)
            return clients, excinfo.value

        c_none, err_none = run(None)
        recording = RecordingSink()
        c_sink, err_sink = run(recording)
        assert err_none.node_ids == err_sink.node_ids == ["o1"]
        for key in ("dio", "mqtt", "greengrass"):
            assert c_none[key].calls == c_sink[key].calls
        # The failing node received NO sent detail (Requirement 1.5 / 3.3).
        assert recording.detail_for("o1") is None
        # The successful siblings still recorded their details.
        assert recording.detail_for("m1") is not None
        assert recording.detail_for("d1") is not None

    def test_failing_runner_records_no_sent_detail(self, clients):
        sink = RecordingSink()
        clients["mqtt"] = Recorder(error=RuntimeError("broker down"))
        processor = make_processor(clients)
        with pytest.raises(OutputBindingError):
            processor.process(
                None,
                document(binding("m1", "mqtt_publish",
                                 {"broker_host": "b", "topic": "t"})),
                METADATA, detail_sink=sink,
            )
        assert sink.detail_for("m1") is None

    def test_raising_sink_is_contained(self, processor, clients):
        # A sink that always raises must not affect binding execution nor
        # the error aggregation.
        raising = RecordingSink(error=RuntimeError("sink kaboom"))
        # No OutputBindingError expected: all bindings succeed, sink errors
        # are swallowed.
        processor.process(None, _mixed_document(), METADATA, detail_sink=raising)
        assert clients["mqtt"].calls  # publish still happened
        assert clients["opcua"].calls
        assert clients["dio"].calls


# --------------------------------------------------------------------------
# Integration with NodeStatusCollector (Requirement 2.1)
# --------------------------------------------------------------------------

class TestCollectorIntegration:
    def test_details_surface_through_collector_to_map(self, processor, clients):
        collector = NodeStatusCollector(extra_node_ids=["m1", "o1", "d1"])
        processor.process(
            None, _mixed_document(), METADATA,
            detail_sink=collector.set_detail,
        )
        m = collector.to_map()
        assert "sent to topic 't'" in m["m1"]["detail"]
        assert m["o1"]["detail"].startswith("wrote ")
        assert m["d1"]["detail"] == "pulsed pin 7 (50ms)"

    def test_failure_detail_wins_over_sent_detail(self, processor, clients):
        # A node already marked failure keeps its failure detail even if a
        # sent detail is recorded afterward (Requirement 3.3).
        collector = NodeStatusCollector(extra_node_ids=["m1"])
        collector.mark_failure("m1", "publish failed: broker unreachable")
        processor.process(
            None,
            document(binding("m1", "mqtt_publish",
                             {"broker_host": "b", "topic": "t"})),
            METADATA, detail_sink=collector.set_detail,
        )
        entry = collector.to_map()["m1"]
        assert entry["status"] == STATUS_FAILURE
        assert entry["detail"] == "publish failed: broker unreachable"
