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
"""Unit tests for metadata attachment in ``OutputBindingProcessor.process``
and ``_run_mqtt_publish`` (workflow-manager-gaps Requirements 7.1, 7.7,
7.8, task 4.3).

Injected stub publishers capture the emitted payloads; no broker, GPIO,
or OPC UA server. Property coverage lives in
test/backend-test/output_bindings_metadata/ (tasks 4.4, 4.5).
"""
import json

import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.output_bindings import (
    BINDING_METADATA,
    OutputBindingProcessor,
)

TAG_VALUES = {"is_anomalous": True, "confidence": 0.9}


def binding(node_id, kind, parameters=None, upstream=(), downstream=()):
    return {
        "nodeId": node_id,
        "binding": kind,
        "parameters": dict(parameters or {}),
        "upstreamNodeIds": list(upstream),
        "downstreamNodeIds": list(downstream),
    }


def metadata_binding(node_id="meta-1", mappings=(), static=None, attach_to=()):
    entry = binding(node_id, BINDING_METADATA)
    entry["metadataMappings"] = [
        {"fieldPath": path, "key": key} for path, key in mappings
    ]
    entry["staticJson"] = dict(static or {})
    entry["attachTo"] = list(attach_to)
    return entry


def mqtt_binding(node_id="mqtt-1", **parameters):
    parameters.setdefault("broker_host", "localhost")
    parameters.setdefault("topic", "swagfactory/quality")
    return binding(node_id, "mqtt_publish", parameters)


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


def tag_values_with_trigger(payload_json, **extra):
    values = dict(TAG_VALUES)
    values.update(extra)
    values["trigger"] = {
        "topic": "swagfactory/invoke",
        "payload": "<raw>",
        "payload_json": payload_json,
        "qos": 1,
        "timestamp": "2025-01-01T00:00:00Z",
    }
    return values


class Recorder:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error

    @property
    def payloads(self):
        # _default_mqtt_publisher signature: (host, port, topic, payload, qos, ...)
        return [call[3] for call in self.calls]


def run(doc, tag_values, mqtt=None, opcua=None, dio=None):
    processor = OutputBindingProcessor(
        dio_actuator=dio or Recorder(),
        mqtt_publisher=mqtt or Recorder(),
        opcua_writer=opcua or Recorder(),
    )
    processor.process(None, doc, tag_values)


class TestMetadataBindingInLoop:
    def test_metadata_binding_is_a_no_op_in_the_loop(self):
        mqtt = Recorder()
        doc = document(metadata_binding(attach_to=[]))
        run(doc, dict(TAG_VALUES), mqtt=mqtt)
        assert mqtt.calls == []


class TestMqttAttachment:
    def test_json_object_payload_merges_attached_entries_top_level(self):
        mqtt = Recorder()
        doc = document(
            metadata_binding(
                mappings=[("job_id", "job_id")],
                static={"station": "line-1"},
                attach_to=["mqtt-1"],
            ),
            mqtt_binding(),
        )
        run(doc, tag_values_with_trigger({"job_id": "J-1042"}), mqtt=mqtt)
        assert len(mqtt.calls) == 1
        payload = json.loads(mqtt.payloads[0])
        assert payload["job_id"] == "J-1042"
        assert payload["station"] == "line-1"
        # Workflow result values carried alongside, unaltered.
        assert payload["is_anomalous"] is True
        assert payload["confidence"] == 0.9

    def test_workflow_result_keys_win_collisions(self, caplog):
        mqtt = Recorder()
        doc = document(
            metadata_binding(
                static={"result": "attached"}, attach_to=["mqtt-1"]),
            mqtt_binding(
                payload_template='{{"result": "workflow"}}'),
        )
        with caplog.at_level("INFO"):
            run(doc, tag_values_with_trigger({}), mqtt=mqtt)
        payload = json.loads(mqtt.payloads[0])
        assert payload["result"] == "workflow"
        assert any(
            "workflow result value wins" in r.message
            for r in caplog.records
        )

    def test_non_object_payload_is_wrapped(self):
        mqtt = Recorder()
        doc = document(
            metadata_binding(
                mappings=[("job_id", "job_id")], attach_to=["mqtt-1"]),
            mqtt_binding(payload_template="anomaly={is_anomalous}"),
        )
        run(doc, tag_values_with_trigger({"job_id": "J-7"}), mqtt=mqtt)
        payload = json.loads(mqtt.payloads[0])
        assert payload == {
            "payload": "anomaly=True",
            "metadata": {"job_id": "J-7"},
        }

    def test_non_attach_to_output_payload_is_byte_identical(self):
        # The same mqtt binding, with and without an unrelated metadata
        # binding in the document, publishes byte-identical payloads
        # (Requirement 7.8).
        tag_values = tag_values_with_trigger({"job_id": "J-1"})

        with_meta = Recorder()
        run(
            document(
                metadata_binding(
                    mappings=[("job_id", "job_id")], attach_to=["other-out"]),
                mqtt_binding(),
            ),
            dict(tag_values), mqtt=with_meta,
        )
        without_meta = Recorder()
        run(document(mqtt_binding()), dict(tag_values), mqtt=without_meta)

        assert with_meta.payloads == without_meta.payloads

    def test_empty_attached_map_still_marks_output_downstream(self):
        # A Metadata_Node that resolves nothing still wraps a non-object
        # payload with empty metadata (Requirement 7.9: empty metadata).
        mqtt = Recorder()
        doc = document(
            metadata_binding(attach_to=["mqtt-1"]),
            mqtt_binding(payload_template="plain-text"),
        )
        run(doc, dict(TAG_VALUES), mqtt=mqtt)
        assert json.loads(mqtt.payloads[0]) == {
            "payload": "plain-text", "metadata": {},
        }


class TestEffectiveMetadata:
    def test_payload_template_sees_attached_keys_and_metadata_json(self):
        mqtt = Recorder()
        doc = document(
            metadata_binding(
                mappings=[("job_id", "job_id")], attach_to=["mqtt-1"]),
            mqtt_binding(
                payload_template="job={job_id} meta={metadata_json}"),
        )
        run(doc, tag_values_with_trigger({"job_id": "J-9"}), mqtt=mqtt)
        payload = json.loads(mqtt.payloads[0])
        assert payload["payload"] == 'job=J-9 meta={"job_id": "J-9"}'

    def test_condition_can_reference_attached_keys(self):
        mqtt = Recorder()
        doc = document(
            metadata_binding(
                mappings=[("priority", "priority")], attach_to=["mqtt-1"]),
            mqtt_binding(condition="priority >= 5"),
        )
        run(doc, tag_values_with_trigger({"priority": 3}), mqtt=mqtt)
        assert mqtt.calls == []
        run(doc, tag_values_with_trigger({"priority": 7}), mqtt=mqtt)
        assert len(mqtt.calls) == 1

    def test_existing_tag_keys_win_over_attached_entries(self, caplog):
        mqtt = Recorder()
        doc = document(
            metadata_binding(
                static={"confidence": "attached"}, attach_to=["mqtt-1"]),
            mqtt_binding(payload_template="confidence={confidence}"),
        )
        with caplog.at_level("INFO"):
            run(doc, tag_values_with_trigger({}), mqtt=mqtt)
        payload = json.loads(mqtt.payloads[0])
        assert payload["payload"] == "confidence=0.9"
        assert any(
            "existing tag value wins" in r.message for r in caplog.records)


class TestScalarOutputsNoEmbedding:
    def test_opcua_write_gains_no_automatic_embedding(self):
        opcua = Recorder()
        doc = document(
            metadata_binding(
                mappings=[("job_id", "job_id")], attach_to=["opcua-1"]),
            binding(
                "opcua-1", "opcua_write",
                {"endpoint": "opc.tcp://s:4840", "node_id": "ns=2;i=3",
                 "value_template": "{job_id}"},
            ),
        )
        run(doc, tag_values_with_trigger({"job_id": "J-3"}), opcua=opcua)
        # The value template resolves the attached key through the
        # effective metadata dict, but nothing is auto-embedded.
        assert opcua.calls == [("opc.tcp://s:4840", "ns=2;i=3", "J-3")]

    def test_digital_output_gains_no_automatic_embedding(self):
        dio = Recorder()
        doc = document(
            metadata_binding(static={"k": 1}, attach_to=["dio-1"]),
            binding(
                "dio-1", "digital_output",
                {"pin": 5, "signal_type": "high"},
            ),
        )
        run(doc, tag_values_with_trigger({}), dio=dio)
        assert dio.calls == [(5, "high", 100)]
