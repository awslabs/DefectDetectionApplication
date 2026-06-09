#  Copyright  Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Unit tests for the YOLO detection post-processor (the ONNX object-detection
output-contract adapter). Pure numpy, no triton/torch needed."""
import numpy as np
import pytest

from lyra_science_processing_utils.model_processors.yolo_detection_postprocessor import (
    YoloDetectionPostProcessor,
    _xywh_to_xyxy,
    _nms,
)
from lyra_science_processing_utils.utils.object_detection_result import ObjectDetectionResult


def _make_yolo_output(boxes_xywh, class_scores, num_classes, num_anchors=8400):
    """Build a YOLOv8-style [1, 4+num_classes, num_anchors] tensor with the given
    detections placed in the first anchors and the rest left as background."""
    c = 4 + num_classes
    arr = np.zeros((1, c, num_anchors), dtype=np.float32)
    for i, (box, scores) in enumerate(zip(boxes_xywh, class_scores)):
        arr[0, 0:4, i] = box
        arr[0, 4:, i] = scores
    return arr


def test_xywh_to_xyxy():
    boxes = np.array([[100.0, 100.0, 40.0, 20.0]])
    xyxy = _xywh_to_xyxy(boxes)
    np.testing.assert_allclose(xyxy[0], [80.0, 90.0, 120.0, 110.0])


def test_nms_suppresses_overlapping():
    boxes = np.array([
        [0, 0, 10, 10],
        [1, 1, 11, 11],   # heavily overlaps box 0
        [100, 100, 110, 110],  # disjoint
    ], dtype=np.float32)
    scores = np.array([0.9, 0.8, 0.7], dtype=np.float32)
    keep = _nms(boxes, scores, iou_threshold=0.45)
    assert 0 in keep          # highest score kept
    assert 1 not in keep      # suppressed by overlap with 0
    assert 2 in keep          # disjoint survives


def test_single_detection_decoded():
    num_classes = 80
    # one strong detection of class 3 at center (320,320) size 64x64
    out = _make_yolo_output(
        boxes_xywh=[[320.0, 320.0, 64.0, 64.0]],
        class_scores=[[0.0] * 3 + [0.9] + [0.0] * (num_classes - 4)],
        num_classes=num_classes,
    )
    pp = YoloDetectionPostProcessor(
        {"detection": {"num_classes": num_classes, "score_threshold": 0.25, "network_input": 640}}
    )
    results = pp([out])
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, ObjectDetectionResult)
    assert r.obj_class == "3"
    assert r.confidence == pytest.approx(0.9, abs=1e-5)
    # no src scaling -> network space coords; 320±32
    np.testing.assert_allclose(r.bounding_box, [288.0, 288.0, 352.0, 352.0], atol=1e-3)


def test_below_threshold_filtered():
    num_classes = 80
    out = _make_yolo_output(
        boxes_xywh=[[100.0, 100.0, 20.0, 20.0]],
        class_scores=[[0.1] + [0.0] * (num_classes - 1)],  # below default 0.25
        num_classes=num_classes,
    )
    pp = YoloDetectionPostProcessor({"detection": {"num_classes": num_classes}})
    assert pp([out]) == []


def test_class_names_label():
    num_classes = 3
    out = _make_yolo_output(
        boxes_xywh=[[50.0, 50.0, 10.0, 10.0]],
        class_scores=[[0.0, 0.8, 0.0]],
        num_classes=num_classes,
        num_anchors=10,
    )
    pp = YoloDetectionPostProcessor(
        {"detection": {"num_classes": num_classes, "class_names": ["cat", "dog", "bird"]}}
    )
    results = pp([out])
    assert len(results) == 1
    assert results[0].obj_class == "dog"


def test_src_image_scaling():
    num_classes = 5
    out = _make_yolo_output(
        boxes_xywh=[[320.0, 320.0, 64.0, 64.0]],
        class_scores=[[0.0, 0.9, 0.0, 0.0, 0.0]],
        num_classes=num_classes,
        num_anchors=10,
    )
    pp = YoloDetectionPostProcessor(
        {"detection": {"num_classes": num_classes, "network_input": 640}}
    )
    # source image is 1280x640 -> x scaled 2x, y scaled 1x
    results = pp([out], src_img_size=(1280, 640))
    assert len(results) == 1
    np.testing.assert_allclose(
        results[0].bounding_box, [576.0, 288.0, 704.0, 352.0], atol=1e-3
    )


def test_transposed_layout_accepted():
    """Tolerate the already-per-anchor [1, num_anchors, 4+num_classes] layout."""
    num_classes = 80
    native = _make_yolo_output(
        boxes_xywh=[[320.0, 320.0, 64.0, 64.0]],
        class_scores=[[0.0] * 5 + [0.9] + [0.0] * (num_classes - 6)],
        num_classes=num_classes,
    )
    transposed = np.transpose(native, (0, 2, 1))  # [1, anchors, 84]
    pp = YoloDetectionPostProcessor({"detection": {"num_classes": num_classes}})
    results = pp([transposed])
    assert len(results) == 1
    assert results[0].obj_class == "5"


def test_empty_output():
    pp = YoloDetectionPostProcessor({"detection": {}})
    assert pp([]) == []
    assert pp([np.zeros((1, 84, 8400), dtype=np.float32)]) == []


def test_detection_emit_json_roundtrip():
    """The execute() detection path serializes ObjectDetectionResults to a JSON
    byte tensor (via the existing 'anomalies' channel). Validate that contract:
    serialize -> uint8 bytes -> decode round-trips losslessly."""
    import json

    num_classes = 80
    out = _make_yolo_output(
        boxes_xywh=[[320.0, 320.0, 64.0, 64.0]],
        class_scores=[[0.0] * 5 + [0.88] + [0.0] * (num_classes - 6)],
        num_classes=num_classes,
    )
    pp = YoloDetectionPostProcessor(
        {"detection": {"num_classes": num_classes, "network_input": 640}}
    )
    results = pp([out])
    serialized = [d.serialize() for d in results]

    det_bytes = np.frombuffer(
        bytes(json.dumps(serialized), encoding="utf-8"), dtype=np.uint8
    )
    roundtrip = json.loads(det_bytes.tobytes().decode("utf-8"))
    assert roundtrip[0]["class"] == "5"
    assert roundtrip[0]["bounding_box"] == [288.0, 288.0, 352.0, 352.0]
    assert roundtrip[0]["confidence"] == pytest.approx(0.88, abs=1e-5)

    # top confidence drives output_score/output_confidence in execute()
    top_conf = max((float(d.confidence) for d in results), default=0.0)
    assert top_conf == pytest.approx(0.88, abs=1e-5)
