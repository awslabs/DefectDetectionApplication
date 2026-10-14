# Copyright 2026 Amazon Web Services, Inc.
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
"""Example tests for the Bedrock nested per-node verdict keys
(detection-guided-bedrock-inspection task 7.5, Requirements 4.1-4.4).

The nested merge shape: an anomaly-mode Bedrock_Binding's verdict lands
under ``bedrock.{nodeId}.{is_anomalous, confidence, text[,
detection_id]}`` beside the unchanged flat keys; a recorded error lands
at ``bedrock.{nodeId}.error``; parallel nodes never overwrite each
other's nested entries. The dotted per-node keys are addressable from
``mqtt_publish`` payload templates (``render_template``) and from
output-binding / conditional / inference_filter conditions
(``evaluate_condition``), end-to-end through the output processor with
injected fake clients — no network.
"""
import os

import numpy as np
import cv2
import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.output_bindings import (
    BedrockInferenceProcessor,
    OutputBindingProcessor,
    RunContext,
    evaluate_condition,
    render_template,
)

NODE_ID = "bedrock_1"
DETECTION_ID = "3f9a2c1e"
FRAME_NAME = "bedrock_frame_cam.jpg"
CAPTURE_ID = "cap-1"
ANSWER = '{"is_anomalous": false, "confidence": 0.93}'

#: A representative post-run Run_Metadata slice: two parallel Bedrock
#: branches — one verdict (with a Detection_Crop attribution), one
#: recorded error (design Data Models example).
NESTED_METADATA = {
    "is_anomalous": False,
    "confidence": 0.93,
    "bedrock": {
        "bedrock_1": {
            "is_anomalous": False, "confidence": 0.93,
            "text": "looks fine", "detection_id": DETECTION_ID,
        },
        "bedrock_2": {
            "error": "crop_detection_index 1 requested but only 1 "
                     "detection(s) available",
        },
    },
}


class RecordingInvoker:
    def __init__(self, answer=ANSWER):
        self.answer = answer
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens):
        self.calls.append({"images": list(images)})
        return self.answer


def make_binding(node_id, parameters=None):
    return {
        "nodeId": node_id,
        "binding": "bedrock_inference",
        "parameters": dict(parameters or {}),
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
        "capturePaths": {"in": "{work_dir}/" + FRAME_NAME,
                         "reference": None},
    }


def make_document(bindings):
    return {"schemaVersion": 1, "executorBindings": list(bindings)}


def write_frame(work_dir, width=100, height=80, name=FRAME_NAME):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    path = os.path.join(work_dir, name)
    assert cv2.imwrite(path, frame)
    return path


def make_detection():
    return {
        "id": DETECTION_ID, "label": "blue box", "confidence": 0.9,
        "x_min": 10.0, "y_min": 20.0, "x_max": 50.0, "y_max": 60.0,
    }


# ---------------------------------------------------------------------------
# The nested merge shape (Requirements 4.1, 4.2, 2.7)
# ---------------------------------------------------------------------------

class TestNestedMergeShape:
    def test_verdict_and_error_shape_across_parallel_nodes(self, tmp_path):
        """One run, two anomaly-mode nodes: the crop node's verdict lands
        under ``bedrock.{nodeId}.{is_anomalous, confidence, text,
        detection_id}``, the out-of-range node's outcome lands at
        ``bedrock.{nodeId}.error`` — the exact design Data Models shape,
        with the flat keys carrying the last successful verdict."""
        write_frame(str(tmp_path))
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)
        crop_node = make_binding("bedrock_1", {"crop_detection_index": 0})
        errored_node = make_binding("bedrock_2", {"crop_detection_index": 1})
        run_context = RunContext(
            tag_values={"detections": [make_detection()]},
            output_dir=str(tmp_path), capture_id=CAPTURE_ID)

        metadata = processor.process(
            make_document([crop_node, errored_node]), {}, str(tmp_path),
            run_context=run_context)

        assert metadata["bedrock"] == {
            "bedrock_1": {
                "text": ANSWER,
                "detection_id": DETECTION_ID,
                "is_anomalous": False,
                "confidence": 0.93,
            },
            "bedrock_2": {
                "error": "Bedrock inference node 'bedrock_2' requested "
                         "crop_detection_index 1 but only 1 detection(s) "
                         "are available",
            },
        }
        # Flat keys unchanged beside the nested namespace (4.1).
        assert metadata["is_anomalous"] is False
        assert metadata["confidence"] == 0.93
        assert metadata["bedrock_text"] == ANSWER

    def test_whole_frame_verdict_carries_no_detection_id(self, tmp_path):
        """The legacy whole-frame path's nested entry has exactly
        ``{text, is_anomalous, confidence}`` — ``detection_id`` appears
        only when a Detection_Crop was inspected (2.7, 7.1)."""
        write_frame(str(tmp_path))
        processor = BedrockInferenceProcessor(invoker=RecordingInvoker())

        metadata = processor.process(
            make_document([make_binding(NODE_ID)]), {}, str(tmp_path))

        assert metadata["bedrock"][NODE_ID] == {
            "text": ANSWER, "is_anomalous": False, "confidence": 0.93,
        }

    def test_parallel_verdicts_never_overwrite_each_other(self, tmp_path):
        """Three anomaly-mode nodes in one run keep three distinct
        nested entries while the flat keys stay last-writer-wins."""
        write_frame(str(tmp_path))
        processor = BedrockInferenceProcessor(invoker=RecordingInvoker())
        bindings = [make_binding("bedrock_{0}".format(i)) for i in (1, 2, 3)]

        metadata = processor.process(
            make_document(bindings), {}, str(tmp_path))

        assert sorted(metadata["bedrock"]) == \
            ["bedrock_1", "bedrock_2", "bedrock_3"]
        for node_id in ("bedrock_1", "bedrock_2", "bedrock_3"):
            assert metadata["bedrock"][node_id]["is_anomalous"] is False
            assert metadata["bedrock"][node_id]["confidence"] == 0.93


# ---------------------------------------------------------------------------
# Dotted-key rendering in payload templates (Requirement 4.3)
# ---------------------------------------------------------------------------

class TestDottedTemplateRendering:
    def test_single_dotted_placeholder_keeps_native_type(self):
        assert render_template(
            "{bedrock.bedrock_1.is_anomalous}", NESTED_METADATA) is False
        assert render_template(
            "{bedrock.bedrock_1.confidence}", NESTED_METADATA) == 0.93

    def test_mixed_template_substitutes_dotted_values_as_strings(self):
        rendered = render_template(
            "part={bedrock.bedrock_1.detection_id} "
            "anomalous={bedrock.bedrock_1.is_anomalous}",
            NESTED_METADATA)
        assert rendered == "part=3f9a2c1e anomalous=False"

    def test_dotted_error_key_renders(self):
        rendered = render_template(
            "failed: {bedrock.bedrock_2.error}", NESTED_METADATA)
        assert rendered == (
            "failed: crop_detection_index 1 requested but only 1 "
            "detection(s) available")

    def test_unresolved_dotted_placeholder_degrades_to_a_literal(self):
        # Same lenient contract as unknown flat placeholders: a template
        # typo degrades to a literal rather than a failure.
        assert render_template(
            "{bedrock.no_such_node.is_anomalous}", NESTED_METADATA) == \
            "{bedrock.no_such_node.is_anomalous}"

    def test_flat_placeholders_render_unchanged_beside_dotted_ones(self):
        rendered = render_template(
            "flat={is_anomalous} nested={bedrock.bedrock_1.is_anomalous}",
            NESTED_METADATA)
        assert rendered == "flat=False nested=False"

    def test_mqtt_payload_template_resolves_dotted_keys_end_to_end(self):
        """An ``mqtt_publish`` payload template referencing the per-node
        keys renders each branch's own verdict (4.3)."""
        published = []

        def mqtt(host, port, topic, payload, qos, *args):
            published.append((topic, payload))

        processor = OutputBindingProcessor(mqtt_publisher=mqtt)
        document = {
            "schemaVersion": 1,
            "executorBindings": [{
                "nodeId": "mqtt1",
                "binding": "mqtt_publish",
                "parameters": {
                    "broker_host": "broker.local",
                    "topic": "dda/results/part1",
                    "payload_template":
                        '{{"detection": "{bedrock.bedrock_1.detection_id}",'
                        ' "anomalous": {bedrock.bedrock_1.is_anomalous},'
                        ' "confidence": {bedrock.bedrock_1.confidence}}}',
                },
                "upstreamNodeIds": [],
                "downstreamNodeIds": [],
            }],
        }

        processor(None, document, dict(NESTED_METADATA))

        assert published == [(
            "dda/results/part1",
            '{"detection": "3f9a2c1e", "anomalous": False,'
            ' "confidence": 0.93}',
        )]


# ---------------------------------------------------------------------------
# Dotted-key condition evaluation (Requirement 4.4)
# ---------------------------------------------------------------------------

class TestDottedConditionEvaluation:
    def test_dotted_comparison_and_conjunction(self):
        assert evaluate_condition(
            "bedrock.bedrock_1.is_anomalous == false", NESTED_METADATA
        ) is True
        assert evaluate_condition(
            "bedrock.bedrock_1.confidence >= 0.9 && is_anomalous == false",
            NESTED_METADATA) is True
        assert evaluate_condition(
            "bedrock.bedrock_1.confidence > 0.95", NESTED_METADATA) is False

    def test_bare_dotted_field_is_truthy_operand(self):
        assert evaluate_condition(
            "!bedrock.bedrock_1.is_anomalous", NESTED_METADATA) is True

    def test_unresolvable_dotted_field_raises_like_unknown_flat(self):
        with pytest.raises(ValueError):
            evaluate_condition(
                "bedrock.no_such_node.is_anomalous == true", NESTED_METADATA)

    def test_flat_conditions_evaluate_unchanged(self):
        assert evaluate_condition(
            "is_anomalous == false && confidence >= 0.9", NESTED_METADATA
        ) is True

    def test_conditional_binding_routes_on_dotted_key_end_to_end(self):
        """A ``conditional`` binding whose expression references a
        per-node verdict key routes the matching port (4.4)."""
        published = []

        def mqtt(host, port, topic, payload, qos, *args):
            published.append(topic)

        processor = OutputBindingProcessor(mqtt_publisher=mqtt)
        condition = "bedrock.bedrock_1.is_anomalous == false"
        document = {
            "schemaVersion": 1,
            "executorBindings": [
                {
                    "nodeId": "cond1",
                    "binding": "conditional",
                    "parameters": {"condition": condition},
                    "upstreamNodeIds": [],
                    "downstreamNodeIds": ["ok_out", "bad_out"],
                    "downstreamNodeIdsByPort": {
                        "true": ["ok_out"], "false": ["bad_out"],
                    },
                    "portConditions": {
                        "true": condition,
                        "false": "!({0})".format(condition),
                    },
                },
                {
                    "nodeId": "ok_out",
                    "binding": "mqtt_publish",
                    "parameters": {"broker_host": "b", "topic": "pass"},
                    "upstreamNodeIds": ["cond1"],
                    "downstreamNodeIds": [],
                },
                {
                    "nodeId": "bad_out",
                    "binding": "mqtt_publish",
                    "parameters": {"broker_host": "b", "topic": "fail"},
                    "upstreamNodeIds": ["cond1"],
                    "downstreamNodeIds": [],
                },
            ],
        }

        processor(None, document, dict(NESTED_METADATA))

        assert published == ["pass"]

    def test_inference_filter_gates_on_dotted_error_presence(self):
        """An ``inference_filter`` over an errored branch's verdict key
        cannot evaluate (the key resolves to nothing comparable) and
        gates its downstream output — the failed-condition gating
        Requirement 2.4 prescribes."""
        published = []

        def mqtt(host, port, topic, payload, qos, *args):
            published.append(topic)

        processor = OutputBindingProcessor(mqtt_publisher=mqtt)
        document = {
            "schemaVersion": 1,
            "executorBindings": [
                {
                    "nodeId": "filter1",
                    "binding": "inference_filter",
                    "parameters": {
                        "condition":
                            "bedrock.bedrock_2.is_anomalous == true",
                    },
                    "upstreamNodeIds": [],
                    "downstreamNodeIds": ["out1"],
                },
                {
                    "nodeId": "out1",
                    "binding": "mqtt_publish",
                    "parameters": {"broker_host": "b", "topic": "gated"},
                    "upstreamNodeIds": ["filter1"],
                    "downstreamNodeIds": [],
                },
            ],
        }

        processor(None, document, dict(NESTED_METADATA))

        assert published == []
