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
"""Targeted unit tests for the Bedrock detection-crop path
(detection-guided-bedrock-inspection task 7.2, Requirements 2.1-2.7).

A Bedrock_Binding carrying ``crop_detection_index`` = k slices the
captured 'in' frame to entry k of the run's merged Detection_List
(margin-expanded, frame-clamped), persists the crop as
``{output_dir}/{capture_id}.crop.{detection_id}.jpg``, sends the crop
bytes as the Converse "Input image" block, and records the entry's
Detection_ID under ``bedrock.{nodeId}.detection_id``. Every crop-path
failure is a RECORDED error outcome at ``bedrock.{nodeId}.error`` —
never a ``BedrockInferenceError`` raise — leaving the run and the
sibling bindings unaffected. An absent ``crop_detection_index`` keeps
today's whole-frame behavior byte-identical.

The comprehensive unit + property suite lands with task 7.5; these
tests pin the crop mechanics and the no-new-params preservation.
"""
import json
import os

import numpy as np
import cv2
import pytest

from workflow_engine.node_status import (
    STATUS_FAILURE,
    NodeStatusCollector,
)
from workflow_engine.output_bindings import (
    BedrockInferenceProcessor,
    RunContext,
    compute_crop_box,
    _capture_record_source_dimensions,
)

NODE_ID = "bedrock1"
DETECTION_ID = "abc12345"
FRAME_NAME = "bedrock_frame_cam.jpg"
CAPTURE_ID = "cap-1"


def make_binding(parameters=None, node_id=NODE_ID, capture_paths=None):
    return {
        "nodeId": node_id,
        "binding": "bedrock_inference",
        "parameters": dict(parameters or {}),
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
        "capturePaths": (
            {"in": "{work_dir}/" + FRAME_NAME, "reference": None}
            if capture_paths is None else capture_paths
        ),
    }


def make_document(bindings):
    """Minimal compiled-document shape the processor consumes — only
    ``executorBindings`` matters to BedrockInferenceProcessor."""
    return {"schemaVersion": 1, "executorBindings": list(bindings)}


def make_detection(
    detection_id=DETECTION_ID, x_min=10.0, y_min=20.0, x_max=50.0, y_max=60.0
):
    return {
        "id": detection_id,
        "label": "blue box",
        "confidence": 0.9,
        "x_min": x_min,
        "y_min": y_min,
        "x_max": x_max,
        "y_max": y_max,
    }


def write_frame(work_dir, width=100, height=80, name=FRAME_NAME):
    """A real JPEG frame (gradient, so crops are decodable and sized)."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    frame[:, :, 1] = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    path = os.path.join(work_dir, name)
    assert cv2.imwrite(path, frame)
    return path


def decode_jpeg(data):
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)


class RecordingInvoker:
    def __init__(self, answer='{"is_anomalous": true, "confidence": 0.9}'):
        self.answer = answer
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens):
        self.calls.append({"images": list(images)})
        return self.answer


def make_run_context(tmp_path, detections, node_status=None):
    return RunContext(
        tag_values={"detections": detections},
        output_dir=str(tmp_path),
        capture_id=CAPTURE_ID,
        graph_document=None,
        node_status=node_status,
    )


# ---------------------------------------------------------------------------
# compute_crop_box (crop math: margin expansion, clamping, scaling)
# ---------------------------------------------------------------------------

class TestComputeCropBox:
    def test_plain_box_no_margin(self):
        assert compute_crop_box((10, 20, 50, 60), 100, 80, 0) == \
            (10, 20, 50, 60)

    def test_margin_expands_per_side_by_box_dimension_percent(self):
        # 40x40 box, 25% margin -> 10 px per side on both axes (2.3).
        assert compute_crop_box((20, 20, 60, 60), 100, 100, 25) == \
            (10, 10, 70, 70)

    def test_expansion_clamps_to_frame_bounds(self):
        assert compute_crop_box((10, 20, 50, 60), 100, 80, 100) == \
            (0, 0, 90, 80)

    def test_source_dimension_mismatch_scales_defensively(self):
        # Source frame 200x100 vs captured 100x50 -> boxes halve.
        assert compute_crop_box(
            (20, 20, 40, 40), 100, 50, 0, source_dimensions=(200, 100)
        ) == (10, 10, 20, 20)

    def test_matching_source_dimensions_degenerate_to_clamp(self):
        assert compute_crop_box(
            (10, 20, 50, 60), 100, 80, 0, source_dimensions=(100, 80)
        ) == (10, 20, 50, 60)

    @pytest.mark.parametrize("box", [
        (50, 20, 10, 60),      # inverted x
        (10, 60, 50, 20),      # inverted y
        (200, 20, 240, 60),    # fully outside the frame
    ])
    def test_degenerate_boxes_yield_none(self, box):
        assert compute_crop_box(box, 100, 80, 0) is None

    def test_zero_area_box_yields_none(self):
        assert compute_crop_box((30, 30, 30, 30), 100, 80, 0) is None


# ---------------------------------------------------------------------------
# The crop path (Requirements 2.2, 2.3, 2.5, 2.7)
# ---------------------------------------------------------------------------

class TestDetectionCrop:
    def test_crop_replaces_input_image_and_records_detection_id(
        self, tmp_path
    ):
        write_frame(str(tmp_path))
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)
        binding = make_binding({"crop_detection_index": 0})
        run_context = make_run_context(tmp_path, [make_detection()])

        metadata = processor.process(
            make_document([binding]), {}, str(tmp_path),
            run_context=run_context)

        # The Converse "Input image" block carries the 40x40 crop, not
        # the 100x80 whole frame (Requirement 2.2).
        assert len(invoker.calls) == 1
        images = invoker.calls[0]["images"]
        assert [label for label, _ in images] == ["Input image"]
        crop = decode_jpeg(images[0][1])
        assert crop.shape[:2] == (40, 40)
        # The verdict merged and the Detection_ID recorded (2.7).
        assert metadata["is_anomalous"] is True
        assert metadata["bedrock"][NODE_ID]["detection_id"] == DETECTION_ID
        assert "error" not in metadata["bedrock"][NODE_ID]

    def test_crop_artifact_persisted_with_detection_id_name(self, tmp_path):
        write_frame(str(tmp_path))
        processor = BedrockInferenceProcessor(invoker=RecordingInvoker())
        binding = make_binding({"crop_detection_index": 0})

        processor.process(
            make_document([binding]), {}, str(tmp_path),
            run_context=make_run_context(tmp_path, [make_detection()]))

        artifact = tmp_path / "{0}.crop.{1}.jpg".format(
            CAPTURE_ID, DETECTION_ID)
        assert artifact.exists()
        persisted = decode_jpeg(artifact.read_bytes())
        assert persisted.shape[:2] == (40, 40)

    def test_margin_expansion_applies_to_sent_crop(self, tmp_path):
        write_frame(str(tmp_path))
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)
        binding = make_binding(
            {"crop_detection_index": 0, "crop_margin_percent": 25})

        processor.process(
            make_document([binding]), {}, str(tmp_path),
            run_context=make_run_context(tmp_path, [make_detection()]))

        # 40x40 box + 10 px per side, clamped: x 0..60, y 10..70 -> 60x60.
        crop = decode_jpeg(invoker.calls[0]["images"][0][1])
        assert crop.shape[:2] == (60, 60)


# ---------------------------------------------------------------------------
# Recorded error outcomes (Requirement 2.4): never a raise, siblings
# unaffected, node status failed
# ---------------------------------------------------------------------------

class TestCropRecordedErrors:
    def test_out_of_range_index_records_error_naming_index_and_count(
        self, tmp_path
    ):
        write_frame(str(tmp_path))
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)
        binding = make_binding({"crop_detection_index": 2})

        metadata = processor.process(
            make_document([binding]), {}, str(tmp_path),
            run_context=make_run_context(tmp_path, [make_detection()]))

        error = metadata["bedrock"][NODE_ID]["error"]
        assert NODE_ID in error
        assert "2" in error and "1 detection(s)" in error
        assert invoker.calls == []  # never invoked
        assert "is_anomalous" not in metadata  # no flat verdict keys

    def test_absent_detection_list_records_error(self, tmp_path):
        write_frame(str(tmp_path))
        processor = BedrockInferenceProcessor(invoker=RecordingInvoker())
        binding = make_binding({"crop_detection_index": 0})
        run_context = RunContext(
            tag_values={}, output_dir=str(tmp_path), capture_id=CAPTURE_ID)

        metadata = processor.process(
            make_document([binding]), {}, str(tmp_path),
            run_context=run_context)

        error = metadata["bedrock"][NODE_ID]["error"]
        assert "no Detection_List" in error and "0 detections" in error

    def test_degenerate_box_records_error_never_a_zero_size_crop(
        self, tmp_path
    ):
        write_frame(str(tmp_path))
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)
        binding = make_binding({"crop_detection_index": 0})
        degenerate = make_detection(x_min=50.0, x_max=10.0)

        metadata = processor.process(
            make_document([binding]), {}, str(tmp_path),
            run_context=make_run_context(tmp_path, [degenerate]))

        assert "empty crop" in metadata["bedrock"][NODE_ID]["error"]
        assert invoker.calls == []

    def test_missing_captured_frame_is_a_recorded_error_not_a_raise(
        self, tmp_path
    ):
        # Without a capture node no captured .jpg lands on disk: the crop
        # path records the error instead of crashing the run.
        processor = BedrockInferenceProcessor(invoker=RecordingInvoker())
        binding = make_binding(
            {"crop_detection_index": 0},
            capture_paths={"in": None, "reference": None})

        metadata = processor.process(
            make_document([binding]), {}, str(tmp_path),
            run_context=make_run_context(tmp_path, [make_detection()]))

        error = metadata["bedrock"][NODE_ID]["error"]
        assert "no captured frame" in error

    def test_recorded_error_marks_node_status_failure(self, tmp_path):
        write_frame(str(tmp_path))
        processor = BedrockInferenceProcessor(invoker=RecordingInvoker())
        binding = make_binding({"crop_detection_index": 5})
        collector = NodeStatusCollector(extra_node_ids=[NODE_ID])
        run_context = make_run_context(
            tmp_path, [make_detection()], node_status=collector)

        processor.process(
            make_document([binding]), {}, str(tmp_path),
            run_context=run_context)

        assert collector.status_of(NODE_ID) == STATUS_FAILURE

    def test_sibling_bindings_and_run_proceed_after_recorded_error(
        self, tmp_path
    ):
        write_frame(str(tmp_path))
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)
        errored = make_binding({"crop_detection_index": 9}, node_id="b_err")
        sibling = make_binding({}, node_id="b_ok")

        metadata = processor.process(
            make_document([errored, sibling]), {"kept": 1}, str(tmp_path),
            run_context=make_run_context(tmp_path, [make_detection()]))

        # The sibling's whole-frame inference ran and merged its verdict.
        assert len(invoker.calls) == 1
        assert metadata["is_anomalous"] is True
        assert metadata["kept"] == 1
        assert "error" in metadata["bedrock"]["b_err"]
        assert metadata["bedrock"]["b_ok"]["text"] == invoker.answer


# ---------------------------------------------------------------------------
# No-new-params preservation (Requirements 2.1, 7.1)
# ---------------------------------------------------------------------------

class TestWholeFramePreservation:
    def test_absent_crop_index_sends_whole_frame_bytes_unchanged(
        self, tmp_path
    ):
        frame_path = write_frame(str(tmp_path))
        invoker = RecordingInvoker()
        processor = BedrockInferenceProcessor(invoker=invoker)
        binding = make_binding({})  # no new parameters

        metadata = processor.process(
            make_document([binding]), {}, str(tmp_path),
            run_context=make_run_context(tmp_path, [make_detection()]))

        with open(frame_path, "rb") as f:
            whole_frame = f.read()
        images = invoker.calls[0]["images"]
        assert images[0] == ("Input image", whole_frame)
        # No detection attribution on the whole-frame path.
        assert "detection_id" not in metadata["bedrock"][NODE_ID]
        assert "error" not in metadata["bedrock"][NODE_ID]


# ---------------------------------------------------------------------------
# Source-dimension reader (defensive captured/source scaling input)
# ---------------------------------------------------------------------------

class TestCaptureRecordSourceDimensions:
    def test_reads_input_image_dimensions_from_the_record(self, tmp_path):
        input_path = write_frame(str(tmp_path), width=64, height=48,
                                 name="input.jpg")
        record = {
            "deviceFleetAuxiliaryInputs": [
                {"data-ref": "file://{0}".format(input_path),
                 "encoding": "NONE", "observedContentType": "jpg"},
            ],
            "deviceFleetAuxiliaryOutputs": [],
        }
        (tmp_path / (CAPTURE_ID + ".jsonl")).write_text(json.dumps(record))

        assert _capture_record_source_dimensions(
            str(tmp_path), CAPTURE_ID) == (64, 48)

    def test_absent_record_yields_none(self, tmp_path):
        assert _capture_record_source_dimensions(
            str(tmp_path), CAPTURE_ID) is None
