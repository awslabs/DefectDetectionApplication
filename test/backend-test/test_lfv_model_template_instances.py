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
"""rfdetr-seg-support task 10.9 (Requirements 10.1, 10.3, 10.4, 17.1): the
base model template with instance-segmentation results.

``TritonPythonModel.__build_detection_tensors`` (``lfv_model_template.py``)
is exercised directly with ``pb_utils`` and the sklearn-bound graph factory
stubbed, as ``test_lfv_detection_tensor_set.py`` does. Both Output_Modes are
covered: the instance mode's ``InstanceSegmentationResult`` entries ride the
detection contract (``class_index`` / ``mask_rle`` / ``mask_area`` beside the
detection keys, the Composite_Mask in ``mask``), and the semantic mode's
``AnomalyResult`` goes through ``__build_anomaly_tensors`` as an anomaly
result does (never the instance branches). Zero instances yield the sentinel,
an empty mask and ``output = 0``; the ``DETECTION_LIST_MAX_BYTES`` ceiling
sheds ``mask_rle`` lowest confidence first and marks the entry; a YOLO
payload is byte-identical to the pre-Seg formula.

Run ONE suite per process: the template inserts ``DDA_APP_ROOT`` at
``sys.path[0]`` when loaded, so the first-party modules are imported here
before it (the worktree copies win).
"""
import importlib.util
import json
import os
import sys
import types

import numpy as np
import pytest

from lyra_anomalies_mask_utils import DEFAULT_ANOMALY_MASK_PALETTE
from lyra_science_processing_utils.utils import mask_rle
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.object_detection_result import (
    InstanceSegmentationResult,
    ObjectDetectionResult,
)

_TEMPLATE_PATH = os.path.join(
    os.getcwd(), "src", "backend", "dda_triton", "resources_for_copy", "lfv_model_template.py"
)

EXPECTED_TENSOR_NAMES = ["output", "output_confidence", "output_score", "mask", "anomalies"]
SENTINEL = [{"bounding_box": [], "class": "", "class_label": "", "confidence": 0.0, "no_objects": True}]


class _FakeTensor:
    def __init__(self, name, arr):
        self._name = name
        self._arr = arr

    def name(self):
        return self._name

    def as_numpy(self):
        return self._arr


def _load_template():
    pb_utils_stub = types.ModuleType("triton_python_backend_utils")
    pb_utils_stub.Tensor = _FakeTensor
    pb_utils_stub.triton_string_to_numpy = lambda s: np.float32
    sys.modules["triton_python_backend_utils"] = pb_utils_stub
    graph_factory_stub = types.ModuleType("lyra_science_processing_utils.model_graph_factory")

    class _ModelGraphFactory:  # pragma: no cover - stub
        pass

    graph_factory_stub.ModelGraphFactory = _ModelGraphFactory
    sys.modules["lyra_science_processing_utils.model_graph_factory"] = graph_factory_stub
    spec = importlib.util.spec_from_file_location("lfv_model_template", _TEMPLATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load_template()


class _FakeObject:
    def __init__(self, object_detection=None, anomaly=None):
        self.object_detection = object_detection
        self.anomaly = anomaly


class _FakeOutput:
    def __init__(self, objects):
        self.objects = objects


class _FakeGraphConfig:
    def __init__(self, pixel_level_classes):
        self._classes = pixel_level_classes

    def get_pixel_level_classes(self):
        return self._classes


def _instance(localization=True, class_names=None, pixel_level_classes=("background", "scratch", "x", "dent")):
    instance = MODULE.TritonPythonModel.__new__(MODULE.TritonPythonModel)
    instance.output_dtype = np.uint8
    instance.confidence_dtype = np.float32
    instance.score_dtype = np.float32
    instance.mask_dtype = np.uint8
    instance.anomalies_dtype = np.uint8
    instance._TritonPythonModel__class_names = class_names
    instance._TritonPythonModel__model_supports_anomaly_localization = localization
    instance._TritonPythonModel__model_graph_config = _FakeGraphConfig(list(pixel_level_classes))
    return instance


def _build(instance, output, frame):
    tensors = getattr(instance, "_TritonPythonModel__build_detection_tensors")(output, frame)
    assert [t.name() for t in tensors] == EXPECTED_TENSOR_NAMES
    return {t.name(): t.as_numpy() for t in tensors}


def _build_anomaly(instance, output, frame):
    tensors = getattr(instance, "_TritonPythonModel__build_anomaly_tensors")(output, frame)
    assert [t.name() for t in tensors] == EXPECTED_TENSOR_NAMES
    return {t.name(): t.as_numpy() for t in tensors}


def _payload(tensors):
    return json.loads(bytes(tensors["anomalies"]).decode("utf-8"))


def _seg(box, class_index, confidence, mask, label):
    return InstanceSegmentationResult(
        box, label, confidence, 0.5, class_index, np.packbits(mask.ravel()), mask.shape, int(mask.sum())
    )


H, W = 12, 16
FRAME = np.zeros((H, W, 3), dtype=np.uint8)
MASK_1 = np.zeros((H, W), dtype=bool)
MASK_1[2:6, 3:9] = True
MASK_2 = np.zeros((H, W), dtype=bool)
MASK_2[5:10, 8:14] = True
I1 = _seg([3, 2, 8, 5], 0, 0.9, MASK_1, "scratch")
I2 = _seg([8, 5, 13, 9], 2, 0.6, MASK_2, "dent")


class TestInstanceMode:
    def test_entries_carry_the_instance_keys_and_round_trip(self):
        tensors = _build(_instance(), _FakeOutput([_FakeObject(I1), _FakeObject(I2)]), FRAME)
        payload = _payload(tensors)
        assert len(payload) == 2
        for entry in payload:
            assert set(entry) == {
                "bounding_box", "class", "confidence", "confidence_threshold",
                "class_index", "mask_rle", "mask_area", "class_label",
            }
        assert payload[0]["class_index"] == 0 and payload[0]["class_label"] == "scratch"
        assert payload[1]["class_index"] == 2 and payload[1]["class_label"] == "dent"
        assert payload[0]["mask_area"] == int(MASK_1.sum())
        assert np.array_equal(mask_rle.decode(payload[0]["mask_rle"], W, H), MASK_1)
        assert np.array_equal(mask_rle.decode(payload[1]["mask_rle"], W, H), MASK_2)
        assert int(tensors["output"][0]) == 1
        assert abs(float(tensors["output_confidence"][0]) - 0.9) < 1e-6
        assert abs(float(tensors["output_score"][0]) - 0.9) < 1e-6

    def test_composite_mask_is_painted_from_the_instances(self):
        tensors = _build(_instance(), _FakeOutput([_FakeObject(I1), _FakeObject(I2)]), FRAME)
        mask = tensors["mask"]
        assert mask.shape == (H, W, 3) and mask.dtype == np.uint8
        # class 0 -> palette row 1, class 2 -> palette row 3, background row 0
        assert np.array_equal(mask[3, 4], DEFAULT_ANOMALY_MASK_PALETTE[1])
        assert np.array_equal(mask[7, 10], DEFAULT_ANOMALY_MASK_PALETTE[3])
        assert np.array_equal(mask[0, 0], DEFAULT_ANOMALY_MASK_PALETTE[0])
        # the overlap at (5, 8) is won by the more confident instance (I1, class 0)
        assert MASK_1[5, 8] and MASK_2[5, 8]
        assert np.array_equal(mask[5, 8], DEFAULT_ANOMALY_MASK_PALETTE[1])

    def test_no_pixel_level_classes_means_the_empty_mask(self):
        tensors = _build(_instance(localization=False), _FakeOutput([_FakeObject(I1)]), FRAME)
        assert tensors["mask"].shape == FRAME.shape
        assert not np.any(tensors["mask"])
        # the entries still carry their masks
        assert "mask_rle" in _payload(tensors)[0]

    def test_zero_instances_yield_the_sentinel_empty_mask_and_output_0(self):
        tensors = _build(_instance(), _FakeOutput([]), FRAME)
        assert _payload(tensors) == SENTINEL
        assert not np.any(tensors["mask"]) and tensors["mask"].shape == FRAME.shape
        assert int(tensors["output"][0]) == 0
        assert float(tensors["output_confidence"][0]) == 0.0


class TestCeiling:
    def test_lowest_confidence_entry_loses_its_mask_first_and_is_marked(self, monkeypatch):
        full = len(bytes(_build(_instance(), _FakeOutput([_FakeObject(I1), _FakeObject(I2)]), FRAME)["anomalies"]))
        monkeypatch.setattr(MODULE, "DETECTION_LIST_MAX_BYTES", full - 1)
        tensors = _build(_instance(), _FakeOutput([_FakeObject(I1), _FakeObject(I2)]), FRAME)
        payload = _payload(tensors)
        assert len(payload) == 2
        shed = [e for e in payload if e.get("mask_truncated")]
        kept = [e for e in payload if "mask_rle" in e]
        assert len(shed) == 1 and len(kept) == 1
        assert shed[0]["confidence"] == 0.6 and "mask_rle" not in shed[0] and shed[0]["mask_area"] == int(MASK_2.sum())
        assert kept[0]["confidence"] == 0.9 and "mask_truncated" not in kept[0]
        assert len(bytes(tensors["anomalies"])) <= full - 1
        # the ceiling never touches the Composite_Mask
        assert np.any(tensors["mask"])

    def test_ceiling_below_any_fit_sheds_every_mask_and_keeps_every_box(self, monkeypatch):
        monkeypatch.setattr(MODULE, "DETECTION_LIST_MAX_BYTES", 10)
        payload = _payload(_build(_instance(), _FakeOutput([_FakeObject(I1), _FakeObject(I2)]), FRAME))
        assert len(payload) == 2
        assert all("mask_rle" not in e and e["mask_truncated"] is True and len(e["bounding_box"]) == 4 for e in payload)

    def test_ceiling_order_is_lowest_confidence_first(self, monkeypatch):
        third = _seg([0, 0, 2, 2], 1, 0.75, MASK_1, "mid")
        objects = [_FakeObject(I1), _FakeObject(I2), _FakeObject(third)]
        full = len(bytes(_build(_instance(), _FakeOutput(objects), FRAME)["anomalies"]))
        # one byte short of the full list: exactly the 0.6 entry is shed
        monkeypatch.setattr(MODULE, "DETECTION_LIST_MAX_BYTES", full - 1)
        payload = _payload(_build(_instance(), _FakeOutput(objects), FRAME))
        assert sorted(e["confidence"] for e in payload if e.get("mask_truncated")) == [0.6]
        # one byte short of that: 0.6 then 0.75; the 0.9 entry keeps its mask
        after_one = len(json.dumps(payload))
        monkeypatch.setattr(MODULE, "DETECTION_LIST_MAX_BYTES", after_one - 1)
        payload = _payload(_build(_instance(), _FakeOutput(objects), FRAME))
        assert sorted(e["confidence"] for e in payload if e.get("mask_truncated")) == [0.6, 0.75]
        assert [e for e in payload if e["confidence"] == 0.9][0].get("mask_rle")


class TestPreservation:
    def test_yolo_payload_is_byte_identical_to_the_pre_seg_formula(self):
        yolo = ObjectDetectionResult([1, 2, 3, 4], "7", 0.8, 0.5)
        tensors = _build(_instance(), _FakeOutput([_FakeObject(yolo)]), FRAME)
        expected = json.dumps([dict(yolo.serialize(), class_label=MODULE.resolve_class_label("7", None))])
        assert bytes(tensors["anomalies"]).decode("utf-8") == expected
        assert not np.any(tensors["mask"]) and tensors["mask"].shape == FRAME.shape
        assert int(tensors["output"][0]) == 1

    def test_mixed_list_keeps_plain_entries_untouched(self):
        yolo = ObjectDetectionResult([1, 2, 3, 4], "7", 0.8, 0.5)
        payload = _payload(_build(_instance(), _FakeOutput([_FakeObject(yolo), _FakeObject(I1)]), FRAME))
        assert set(payload[0]) == {"bounding_box", "class", "confidence", "confidence_threshold", "class_label"}
        assert "mask_rle" in payload[1]

    def test_ceiling_constant_is_four_mebibytes(self):
        assert MODULE.DETECTION_LIST_MAX_BYTES == 4 * 1024 * 1024


class TestSemanticMode:
    """The semantic Output_Mode's AnomalyResult takes the anomaly branch and
    never the instance branches (Requirement 10.7)."""

    def test_semantic_result_goes_through_the_anomaly_contract(self):
        index_mask = np.zeros((H, W), dtype=np.int64)
        index_mask[2:6, 3:9] = 1
        anomaly = AnomalyResult(score=0.7, mask=index_mask, confidence=0.7, label="Anomaly")
        instance = _instance(pixel_level_classes=["background", "scratch"])
        tensors = _build_anomaly(instance, _FakeOutput([_FakeObject(anomaly=anomaly)]), FRAME)
        assert int(tensors["output"][0]) == 1
        labels = _payload(tensors)
        assert isinstance(labels, list) and labels and set(labels[0]) == {"name", "total_percentage_area", "hex_color"}
        assert all("mask_rle" not in label and "bounding_box" not in label for label in labels)
        assert tensors["mask"].shape == (H, W, 3)
        assert np.array_equal(tensors["mask"][3, 4], DEFAULT_ANOMALY_MASK_PALETTE[1])

    def test_semantic_result_without_a_mask_emits_the_empty_contract(self):
        anomaly = AnomalyResult(score=0.2, mask=None, confidence=0.8, label="Normal")
        tensors = _build_anomaly(_instance(), _FakeOutput([_FakeObject(anomaly=anomaly)]), FRAME)
        assert int(tensors["output"][0]) == 0
        assert _payload(tensors) == []
        assert not np.any(tensors["mask"])


@pytest.mark.parametrize("names", [None, {"0": "scratch", "2": "dent"}, {0: "other"}])
def test_class_label_is_the_label_the_decoder_embedded(names):
    """``resolve_class_label(d.obj_class, class_names)``: the decoder already
    resolved the label into ``obj_class`` (Requirement 9.5), and a non-numeric
    label falls back to itself whatever the manifest map says."""
    payload = _payload(_build(_instance(class_names=names), _FakeOutput([_FakeObject(I1), _FakeObject(I2)]), FRAME))
    assert [e["class_label"] for e in payload] == ["scratch", "dent"]
    assert [e["class"] for e in payload] == ["scratch", "dent"]
    assert [e["class_index"] for e in payload] == [0, 2]
