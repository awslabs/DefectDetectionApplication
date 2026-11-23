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
"""Unit tests for the RF-DETR detection post-processor (DETR-family ONNX
output-contract adapter). Pure numpy, no triton/torch needed."""
import numpy as np
import pytest

import logging

from lyra_science_processing_utils.model_processors.rf_detr_detection_postprocessor import (
    ONNX_BOXES_NAME,
    ONNX_LOGITS_NAME,
    RfDetrDetectionPostProcessor,
    _sigmoid,
    _softmax,
    _cxcywh_to_xyxy,
    split_same_shape_pair,
)
from lyra_science_processing_utils.utils.object_detection_result import ObjectDetectionResult

LOGGER = "lyra_science_processing_utils.model_processors.rf_detr_detection_postprocessor"


def _logit_for_prob(p):
    # inverse sigmoid
    return float(np.log(p / (1.0 - p)))


def _make_rf_detr_output(dets, num_queries, num_classes, transpose=False):
    """Build (boxes[1,Q,4] normalized cxcywh, logits[1,Q,C]) with the given
    detections placed on the first queries; other queries score ~0."""
    boxes = np.zeros((1, num_queries, 4), dtype=np.float32)
    logits = np.full((1, num_queries, num_classes), -10.0, dtype=np.float32)  # sigmoid(-10)~0
    for i, (box, cls, prob) in enumerate(dets):
        boxes[0, i, :] = box
        logits[0, i, cls] = _logit_for_prob(prob)
    if transpose:  # return logits first to test order-independence
        return [logits, boxes]
    return [boxes, logits]


def test_helpers():
    np.testing.assert_allclose(_sigmoid(np.array([0.0])), [0.5], atol=1e-6)
    s = _softmax(np.array([[1.0, 1.0, 1.0]]))
    np.testing.assert_allclose(s, [[1 / 3, 1 / 3, 1 / 3]], atol=1e-6)
    xyxy = _cxcywh_to_xyxy(np.array([[0.5, 0.5, 0.4, 0.2]]))
    np.testing.assert_allclose(xyxy[0], [0.3, 0.4, 0.7, 0.6], atol=1e-6)


def test_single_detection_scaled_to_source():
    out = _make_rf_detr_output(
        dets=[((0.5, 0.5, 0.2, 0.4), 3, 0.9)],
        num_queries=300, num_classes=80,
    )
    pp = RfDetrDetectionPostProcessor(
        {"detection": {"num_classes": 80, "score_threshold": 0.5}}
    )
    results = pp([b for b in out], src_img_size=(640, 480))
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, ObjectDetectionResult)
    assert r.obj_class == "3"
    assert r.confidence == pytest.approx(0.9, abs=1e-4)
    # normalized cxcywh (0.5,0.5,0.2,0.4) -> xyxy (0.4,0.3,0.6,0.7) * (640,480)
    np.testing.assert_allclose(r.bounding_box, [256.0, 144.0, 384.0, 336.0], atol=1e-2)


def test_threshold_filters():
    out = _make_rf_detr_output(
        dets=[((0.5, 0.5, 0.1, 0.1), 1, 0.2)],  # below default 0.5
        num_queries=100, num_classes=80,
    )
    pp = RfDetrDetectionPostProcessor({"detection": {"num_classes": 80}})
    assert pp(out) == []


def test_no_nms_keeps_overlapping():
    # Two heavily-overlapping high-score boxes of the SAME class: DETR is
    # NMS-free, so BOTH must survive (unlike YOLO which would suppress one).
    out = _make_rf_detr_output(
        dets=[
            ((0.5, 0.5, 0.4, 0.4), 2, 0.95),
            ((0.51, 0.5, 0.4, 0.4), 2, 0.90),
        ],
        num_queries=100, num_classes=80,
    )
    pp = RfDetrDetectionPostProcessor(
        {"detection": {"num_classes": 80, "score_threshold": 0.5}}
    )
    results = pp(out, src_img_size=(100, 100))
    assert len(results) == 2  # no suppression


def test_output_order_independent():
    out = _make_rf_detr_output(
        dets=[((0.25, 0.25, 0.2, 0.2), 7, 0.8)],
        num_queries=50, num_classes=80, transpose=True,  # logits first
    )
    pp = RfDetrDetectionPostProcessor({"detection": {"num_classes": 80}})
    results = pp(out, src_img_size=(200, 200))
    assert len(results) == 1
    assert results[0].obj_class == "7"


def test_class_names_and_topk():
    out = _make_rf_detr_output(
        dets=[
            ((0.3, 0.3, 0.1, 0.1), 0, 0.9),
            ((0.6, 0.6, 0.1, 0.1), 1, 0.8),
            ((0.7, 0.2, 0.1, 0.1), 2, 0.7),
        ],
        num_queries=50, num_classes=3,
    )
    pp = RfDetrDetectionPostProcessor(
        {"detection": {"num_classes": 3, "score_threshold": 0.5,
                       "top_k": 2, "class_names": ["cat", "dog", "bird"]}}
    )
    results = pp(out, src_img_size=(100, 100))
    # top_k=2 keeps only the two highest-scoring
    assert len(results) == 2
    labels = sorted(r.obj_class for r in results)
    assert labels == ["cat", "dog"]


def test_softmax_drops_background_class():
    # Classic DETR: softmax over classes incl. a trailing no-object class.
    num_classes = 4  # classes 0,1,2 + background=3
    logits = np.full((1, 10, num_classes), -10.0, dtype=np.float32)
    boxes = np.zeros((1, 10, 4), dtype=np.float32)
    boxes[0, 0] = (0.5, 0.5, 0.2, 0.2)
    # query 0: strongly class 1
    logits[0, 0] = [0.0, 8.0, 0.0, 0.0]
    # query 1: strongly background -> should be dropped
    boxes[0, 1] = (0.1, 0.1, 0.1, 0.1)
    logits[0, 1] = [0.0, 0.0, 0.0, 8.0]
    pp = RfDetrDetectionPostProcessor(
        {"detection": {"num_classes": num_classes, "score_threshold": 0.5,
                       "use_softmax": True, "background_class": 3}}
    )
    results = pp([boxes, logits], src_img_size=(100, 100))
    assert len(results) == 1
    assert results[0].obj_class == "1"


def test_bad_output_returns_empty():
    pp = RfDetrDetectionPostProcessor({"detection": {}})
    assert pp([]) == []
    assert pp([np.zeros((1, 300, 4), dtype=np.float32)]) == []  # only one tensor


# ---------------------------------------------------------------------------
# Three-class model: num_classes + 1 == 4, so logits are [Q, 4] like the boxes
# (rfdetr-seg-support task 9.4; DD18; Requirements 9.2, 17.2)
# ---------------------------------------------------------------------------
def _three_class_output():
    """boxes [1,Q,4] and logits [1,Q,4] (3 classes + the background slot):
    query 0 is class 1 at 0.9 with box cxcywh (0.5,0.5,0.2,0.4)."""
    boxes = np.zeros((1, 20, 4), dtype=np.float32)
    logits = np.full((1, 20, 4), -10.0, dtype=np.float32)
    boxes[0, 0] = (0.5, 0.5, 0.2, 0.4)
    logits[0, 0, 1] = _logit_for_prob(0.9)
    return boxes, logits


def _warnings(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


def test_three_class_model_resolves_boxes_and_logits_by_output_names(caplog):
    caplog.set_level(logging.DEBUG)
    boxes, logits = _three_class_output()
    # The runner returns graph order; detection.output_names is that order.
    pp = RfDetrDetectionPostProcessor(
        {"detection": {"num_classes": 3, "output_names": ["labels", "dets"]}}
    )
    results = pp([logits, boxes], src_img_size=(640, 480))
    assert len(results) == 1
    assert results[0].obj_class == "1"
    assert results[0].confidence == pytest.approx(0.9, abs=1e-4)
    np.testing.assert_allclose(results[0].bounding_box, [256.0, 144.0, 384.0, 336.0], atol=1e-2)
    assert _warnings(caplog) == []
    pp2 = RfDetrDetectionPostProcessor(
        {"detection": {"num_classes": 3, "output_names": ["dets", "labels"]}}
    )
    results2 = pp2([boxes, logits], src_img_size=(640, 480))
    assert [r.obj_class for r in results2] == ["1"]
    np.testing.assert_allclose(results2[0].bounding_box, [256.0, 144.0, 384.0, 336.0], atol=1e-2)
    assert _warnings(caplog) == []


def test_three_class_model_without_output_names_takes_list_order_with_one_warning(caplog):
    caplog.set_level(logging.DEBUG)
    boxes, logits = _three_class_output()
    pp = RfDetrDetectionPostProcessor({"detection": {"num_classes": 3}})
    for _ in range(2):
        results = pp([boxes, logits], src_img_size=(640, 480))
        assert [r.obj_class for r in results] == ["1"]
        np.testing.assert_allclose(results[0].bounding_box, [256.0, 144.0, 384.0, 336.0], atol=1e-2)
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "two [Q, 4] tensors" in warnings[0].getMessage()
    assert "output_names" in warnings[0].getMessage()


def test_output_names_do_not_change_a_shape_distinguishable_decode(caplog):
    """Outside the three-class case shapes decide, as before: names in any
    order (even wrong) leave the decode and the silence unchanged (Req 17.1)."""
    caplog.set_level(logging.DEBUG)
    out = _make_rf_detr_output(
        dets=[((0.5, 0.5, 0.2, 0.4), 3, 0.9)], num_queries=300, num_classes=80,
    )
    for names in (None, ["dets", "labels"], ["labels", "dets"]):
        detection = {"num_classes": 80, "score_threshold": 0.5}
        if names:
            detection["output_names"] = names
        results = RfDetrDetectionPostProcessor({"detection": detection})(out, src_img_size=(640, 480))
        assert len(results) == 1 and results[0].obj_class == "3"
        np.testing.assert_allclose(results[0].bounding_box, [256.0, 144.0, 384.0, 336.0], atol=1e-2)
    assert _warnings(caplog) == []


def test_split_same_shape_pair_resolution_rules():
    assert (ONNX_BOXES_NAME, ONNX_LOGITS_NAME) == ("dets", "labels")
    a = np.zeros((5, 4), dtype=np.float32)
    b = np.ones((5, 4), dtype=np.float32)
    # Both names: positions decide, whichever order the tensors arrive in.
    boxes, logits, by_name = split_same_shape_pair([a, b], [0, 1], ["labels", "dets", "masks"])
    assert boxes is b and logits is a and by_name
    boxes, logits, by_name = split_same_shape_pair([a, b], [0, 1], ["dets", "labels"])
    assert boxes is a and logits is b and by_name
    # One name is enough; the other [Q, 4] tensor is the remaining one.
    boxes, logits, by_name = split_same_shape_pair([a, b], [0, 1], ["labels", "x"])
    assert boxes is b and logits is a and by_name
    boxes, logits, by_name = split_same_shape_pair([a, b], [0, 1], ["x", "dets"])
    assert boxes is b and logits is a and by_name
    # Positions are the runner-list positions, not the rank-2 indices.
    boxes, logits, by_name = split_same_shape_pair([a, b], [1, 2], ["masks", "labels", "dets"])
    assert boxes is b and logits is a and by_name
    # No names, or names that name neither: list order [boxes, logits], flagged.
    for names in (None, [], ["boxes", "scores"]):
        boxes, logits, by_name = split_same_shape_pair([a, b], [0, 1], names)
        assert boxes is a and logits is b and not by_name
