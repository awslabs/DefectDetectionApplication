#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""rfdetr-seg-support task 10.9 (Requirements 10.1, 10.3, 10.7, 17.1): the
marshal template with instance-segmentation captures.

``TritonPythonModel._generate_capture_meta_data`` and the pure helpers of
``marshal_for_capture_template.py`` are exercised directly with ``pb_utils``
stubbed (the ``test_marshal_detections_block.py`` pattern). Covered: the
instance capture writes the mask reference and its metadata with the
``internal-color-map`` (``name`` labels), the detections block copies
``mask_rle`` / ``mask_area`` / ``mask_truncated`` and prefers ``class_index``,
``detections_truncated`` appears only when an entry lost its mask,
``mask_color_map`` (``class-name`` labels) only when a mask was written, the
sidecar carries the same object, the local ``_decode_mask_rle`` reproduces the
shared Canonical_RLE vectors byte for byte, ``_fill_instance_masks`` blends
the palette colour under the mask only, the ``_MASK_UTILS`` fallback keeps
``mask.png`` and drops both maps with one warning, the zero-object sentinel
yields ``{"detections": {}}`` and no ``mask.png``, a YOLO entry is
byte-identical, and the semantic mode (an anomaly payload) never enters the
instance branches.

Run ONE suite per process: the template inserts ``DDA_APP_ROOT`` at
``sys.path[0]`` when loaded, so the first-party modules are imported before it.
"""
import base64
import importlib.util
import json
import logging
import os
import sys
import tempfile
import types

import numpy as np
import pytest

from lyra_anomalies_mask_utils import DEFAULT_ANOMALY_MASK_PALETTE, hex_color_string
from lyra_science_processing_utils.utils import mask_rle

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE_PATH = os.path.join(
    os.getcwd(), "src", "backend", "dda_triton", "resources_for_copy", "marshal_for_capture_template.py"
)
_VECTORS_PATH = os.path.join(_HERE, "..", "fixtures", "canonical_rle_vectors.json")


def _load_template():
    sys.modules["triton_python_backend_utils"] = types.ModuleType("triton_python_backend_utils")
    try:
        import cv2  # noqa: F401
    except ImportError:  # pragma: no cover - depends on runner
        sys.modules["cv2"] = types.ModuleType("cv2")
    spec = importlib.util.spec_from_file_location("marshal_for_capture_template", _TEMPLATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load_template()
Marshal = MODULE.TritonPythonModel


def _instance():
    instance = Marshal.__new__(Marshal)
    instance.model_name = "test_model"
    instance.model_version = "1"
    return instance


H, W = 12, 16
FRAME = np.zeros((H, W, 3), dtype=np.uint8)
MASK_1 = np.zeros((H, W), dtype=bool)
MASK_1[2:6, 3:9] = True
MASK_2 = np.zeros((H, W), dtype=bool)
MASK_2[5:10, 8:14] = True


def _entry(box, class_index, confidence, mask, label):
    return {
        "bounding_box": box,
        "class": label,
        "confidence": confidence,
        "confidence_threshold": 0.5,
        "class_index": class_index,
        "mask_rle": mask_rle.encode(mask),
        "mask_area": int(mask.sum()),
        "class_label": label,
    }


INSTANCE_PAYLOAD = [
    _entry([3, 2, 8, 5], 0, 0.9, MASK_1, "scratch"),
    _entry([8, 5, 13, 9], 2, 0.6, MASK_2, "dent"),
]
YOLO_PAYLOAD = [{
    "bounding_box": [1, 2, 3, 4], "class": "7", "confidence": 0.8,
    "confidence_threshold": 0.5, "class_label": "truck",
}]
SENTINEL_PAYLOAD = [{"bounding_box": [], "class": "", "class_label": "", "confidence": 0.0, "no_objects": True}]
ANOMALY_PAYLOAD = [{"name": "crack", "total_percentage_area": 0.2, "hex_color": "#ff0000"}]


def _composite():
    """The Composite_Mask the base model paints: palette row class_index + 1,
    higher confidence wins the overlap."""
    index = np.zeros((H, W), dtype=np.int64)
    index[MASK_2] = 3
    index[MASK_1] = 1
    return DEFAULT_ANOMALY_MASK_PALETTE[index].astype(np.uint8)


COMPOSITE = _composite()


def _generate(instance, anomalies, mask, folder, image=FRAME):
    return instance._generate_capture_meta_data(
        capture_meta_data={
            "capture_id": "cap", "capture_folder": folder, "workflow_id": "wf",
            "event_id": "cap", "device_fleet_name": "fleet",
        },
        inference_output=np.uint8(1),
        time_str="2025-01-01T00:00:00",
        inference_confidence=np.float32(0.9),
        inference_mask=mask,
        inference_anomalies=anomalies,
        inference_score=np.float32(0.9),
        input_image=image,
    )


def _label_block(record):
    for output in record["deviceFleetAuxiliaryOutputs"]:
        if output.get("observedContentType") == "json_with_base64_encoding":
            return json.loads(base64.b64decode(output["data"]))
    return None


def _aux_types(record):
    return [output.get("observedContentType") for output in record["deviceFleetAuxiliaryOutputs"]]


@pytest.fixture
def folder():
    with tempfile.TemporaryDirectory() as path:
        yield path


class TestDecoderVectors:
    def test_local_decoder_matches_every_shared_vector(self):
        with open(_VECTORS_PATH) as handle:
            vectors = json.load(handle)["vectors"]
        assert vectors
        for vector in vectors:
            expected = np.array([[c == "1" for c in row] for row in vector["mask_rows"]], dtype=bool)
            got = Marshal._decode_mask_rle(vector["rle"], vector["width"], vector["height"])
            assert got.shape == (vector["height"], vector["width"]), vector["name"]
            assert np.array_equal(got, expected), vector["name"]

    def test_local_decoder_agrees_with_the_shared_module(self):
        for mask in (MASK_1, MASK_2, np.zeros((H, W), dtype=bool), np.ones((H, W), dtype=bool)):
            rle = mask_rle.encode(mask)
            assert np.array_equal(Marshal._decode_mask_rle(rle, W, H), mask_rle.decode(rle, W, H))

    def test_local_decoder_rejects_a_wrong_size(self):
        with pytest.raises(ValueError):
            Marshal._decode_mask_rle(mask_rle.encode(MASK_1), W + 1, H)


class TestHelpers:
    def test_has_instance_masks(self):
        assert Marshal._has_instance_masks(INSTANCE_PAYLOAD) is True
        assert Marshal._has_instance_masks(YOLO_PAYLOAD) is False
        assert Marshal._has_instance_masks(SENTINEL_PAYLOAD) is False
        assert Marshal._has_instance_masks(ANOMALY_PAYLOAD) is False
        truncated = [dict(INSTANCE_PAYLOAD[0])]
        del truncated[0]["mask_rle"]
        truncated[0]["mask_truncated"] = True
        assert Marshal._has_instance_masks(truncated) is False

    def test_fill_blends_the_palette_colour_under_the_mask_only(self):
        filled = Marshal._fill_instance_masks(FRAME, INSTANCE_PAYLOAD)
        assert filled.shape == FRAME.shape and filled.dtype == np.uint8
        assert not np.any(FRAME), "the input frame is not modified"
        assert np.array_equal(filled[0, 0], [0, 0, 0])
        expected = np.rint(0.4 * DEFAULT_ANOMALY_MASK_PALETTE[1].astype(np.float32)).astype(np.uint8)
        assert np.array_equal(filled[3, 4], expected)
        expected_2 = np.rint(0.4 * DEFAULT_ANOMALY_MASK_PALETTE[3].astype(np.float32)).astype(np.uint8)
        assert np.array_equal(filled[8, 12], expected_2)

    def test_fill_skips_an_entry_whose_mask_does_not_decode(self):
        bad = [dict(INSTANCE_PAYLOAD[0], mask_rle="1 2 3")]
        assert np.array_equal(Marshal._fill_instance_masks(FRAME, bad), FRAME)

    def test_instance_color_map_shares_sum_to_one_and_use_palette_rows(self):
        cmap = Marshal._instance_color_map(COMPOSITE, INSTANCE_PAYLOAD, "class-name")
        assert list(cmap) == ["0", "1", "2"]
        assert cmap["0"]["class-name"] == "background"
        assert cmap["1"]["class-name"] == "scratch" and cmap["2"]["class-name"] == "dent"
        assert cmap["1"]["hex-color"] == hex_color_string(DEFAULT_ANOMALY_MASK_PALETTE[1].tolist()).lower()
        assert cmap["2"]["hex-color"] == hex_color_string(DEFAULT_ANOMALY_MASK_PALETTE[3].tolist()).lower()
        shares = [cmap[key]["total-percentage-area"] for key in cmap]
        assert abs(sum(shares) - 1.0) < 1e-9
        assert cmap["1"]["total-percentage-area"] == pytest.approx(
            float((COMPOSITE == DEFAULT_ANOMALY_MASK_PALETTE[1]).all(axis=2).sum()) / (H * W))
        named = Marshal._instance_color_map(COMPOSITE, INSTANCE_PAYLOAD, "name")
        assert named["1"]["name"] == "scratch" and "class-name" not in named["1"]


class TestInstanceCapture:
    def test_mask_reference_metadata_and_block(self, folder):
        record = _generate(_instance(), INSTANCE_PAYLOAD, COMPOSITE, folder)
        ground_truth = record["deviceGroundTruthData"][0]
        assert ground_truth["anomaly-mask-ref-detected"].endswith("cap.mask.png")
        metadata = ground_truth["anomaly-mask-ref-detected-metadata"]
        assert list(metadata) == ["internal-color-map", "creation-date", "human-annotated", "type", "job-name"]
        cmap = metadata["internal-color-map"]
        assert list(cmap) == ["0", "1", "2"] and cmap["0"]["name"] == "background"
        assert cmap["1"]["name"] == "scratch" and cmap["2"]["name"] == "dent"
        assert _aux_types(record) == ["mask.png", "overlay.jpg", "json", "json_with_base64_encoding"]
        block = _label_block(record)
        assert set(block) == {"detections", "mask_color_map"}
        assert "detections_truncated" not in block
        entry = block["detections"]["0"]
        assert set(entry) == {"class_index", "class_label", "bounding_box", "confidence", "mask_rle", "mask_area"}
        assert entry["class_index"] == "0" and entry["class_label"] == "scratch"
        assert entry["mask_rle"] == INSTANCE_PAYLOAD[0]["mask_rle"]
        assert entry["mask_area"] == int(MASK_1.sum())
        assert block["detections"]["1"]["class_index"] == "2"
        assert block["mask_color_map"]["1"]["class-name"] == "scratch"
        assert block["mask_color_map"]["0"]["class-name"] == "background"

    def test_class_index_wins_over_class(self, folder):
        payload = [dict(INSTANCE_PAYLOAD[0], **{"class": "scratch", "class_index": 5})]
        block = _label_block(_generate(_instance(), payload, COMPOSITE, folder))
        assert block["detections"]["0"]["class_index"] == "5"

    def test_sidecar_carries_the_same_object(self, folder):
        record = _generate(_instance(), INSTANCE_PAYLOAD, COMPOSITE, folder)
        with open(os.path.join(folder, "cap.detections.json")) as handle:
            sidecar = json.load(handle)
        assert sidecar == _label_block(record)
        assert "mask_rle" in sidecar["detections"]["0"]

    def test_truncated_entry_sets_the_block_marker(self, folder):
        payload = json.loads(json.dumps(INSTANCE_PAYLOAD))
        del payload[1]["mask_rle"]
        payload[1]["mask_truncated"] = True
        block = _label_block(_generate(_instance(), payload, COMPOSITE, folder))
        assert block["detections_truncated"] is True
        assert block["detections"]["1"]["mask_truncated"] is True
        assert "mask_rle" not in block["detections"]["1"]
        assert block["detections"]["1"]["mask_area"] == int(MASK_2.sum())
        assert "mask_rle" in block["detections"]["0"]

    def test_all_masks_shed_still_writes_the_composite_mask(self, folder):
        payload = json.loads(json.dumps(INSTANCE_PAYLOAD))
        for entry in payload:
            del entry["mask_rle"]
            entry["mask_truncated"] = True
        record = _generate(_instance(), payload, COMPOSITE, folder)
        block = _label_block(record)
        assert block["detections_truncated"] is True
        assert "mask_color_map" in block
        assert "anomaly-mask-ref-detected" in record["deviceGroundTruthData"][0]

    def test_zero_instances_sentinel_writes_no_mask(self, folder):
        record = _generate(_instance(), SENTINEL_PAYLOAD, np.zeros_like(FRAME), folder)
        assert _label_block(record) == {"detections": {}}
        assert "anomaly-mask-ref-detected" not in record["deviceGroundTruthData"][0]
        assert "mask.png" not in _aux_types(record)
        assert not os.path.exists(os.path.join(folder, "cap.mask.png"))


class TestPreservation:
    def test_yolo_block_is_unchanged(self, folder):
        record = _generate(_instance(), YOLO_PAYLOAD, np.zeros_like(FRAME), folder)
        block = _label_block(record)
        assert set(block) == {"detections"}
        assert set(block["detections"]["0"]) == {"class_index", "class_label", "bounding_box", "confidence"}
        assert block["detections"]["0"] == {
            "class_index": "7", "class_label": "truck", "bounding_box": [1, 2, 3, 4], "confidence": 0.8,
        }
        assert "anomaly-mask-ref-detected" not in record["deviceGroundTruthData"][0]
        assert "mask.png" not in _aux_types(record)

    def test_semantic_anomaly_payload_never_enters_the_instance_branches(self, folder):
        assert Marshal._is_detection_list(ANOMALY_PAYLOAD) is False
        assert Marshal._has_instance_masks(ANOMALY_PAYLOAD) is False
        record = _generate(_instance(), ANOMALY_PAYLOAD, COMPOSITE, folder)
        block = _label_block(record)
        assert "detections" not in block and "anomalies" in block
        assert "mask_color_map" not in block and "detections_truncated" not in block


class TestMaskUtilsFallback:
    def test_fallback_keeps_the_mask_png_and_drops_both_maps(self, folder, monkeypatch, caplog):
        monkeypatch.setattr(MODULE, "_MASK_UTILS", False)
        with caplog.at_level(logging.WARNING):
            record = _generate(_instance(), INSTANCE_PAYLOAD, COMPOSITE, folder)
        ground_truth = record["deviceGroundTruthData"][0]
        assert ground_truth["anomaly-mask-ref-detected"].endswith("cap.mask.png")
        assert "internal-color-map" not in ground_truth["anomaly-mask-ref-detected-metadata"]
        block = _label_block(record)
        assert set(block) == {"detections"}
        assert "mask_rle" in block["detections"]["0"]
        assert any("color" in message.lower() or "mask" in message.lower()
                   for message in caplog.messages), caplog.messages

    def test_fallback_skips_the_fills(self, monkeypatch):
        monkeypatch.setattr(MODULE, "_MASK_UTILS", False)
        assert np.array_equal(Marshal._fill_instance_masks(FRAME, INSTANCE_PAYLOAD), FRAME)

    def test_flag_is_true_with_the_shared_package_installed(self):
        assert MODULE._MASK_UTILS is True
