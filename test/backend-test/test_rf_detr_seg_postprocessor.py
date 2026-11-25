#  #
#   Copyright  Amazon Web Services, Inc.
#  #
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#  #
#        http://www.apache.org/licenses/LICENSE-2.0
#  #
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#  #
"""Unit tests for the RF-DETR-Seg decoder ``RfDetrSegPostProcessor``, its
result type ``InstanceSegmentationResult`` and the shared composite painter
(rfdetr-seg-support task 9.5; design section 9; DD2, DD3, DD5, DD11, DD18;
Requirements 9.1-9.7, 10.3, 17.2).

Synthetic three-tensor outputs (boxes [1,Q,4], logits [1,Q,S], masks
[1,Q,Hm,Wm]) exercise: instance-mode count / order / box geometry / mask_rle
round trip / mask_area / mask threshold (bilinear upsample THEN threshold,
O3 b); flattened top-k with one query yielding two classes; slot handling
(C + 1 background slot, C, over-declared num_classes); shuffled and nameless
tensors; the three-class (S == 4) tie-break via output_names and the
list-order fallback warning; too-few outputs; the top_k cap; the
zero-foreground drop; the legacy semantic manifest (D5); the output_mode
contradiction log; paint_composite overlap precedence and palette
saturation; the registry and the import shim. The eight pre-existing cases
of test_rf_detr_semantic_segmentation_postprocessor.py are unmodified and
run through the shim. The S6 timing fixture (``@pytest.mark.timing``) is
skipped unless DDA_SEG_TIMING=1. Pure numpy + cv2, no triton/torch needed.
"""
import json
import logging
import os
import platform
import re
import time

import cv2
import numpy as np
import pytest

from lyra_anomalies_mask_utils import DEFAULT_ANOMALY_MASK_PALETTE, convert_index_mask_to_color_mask
from lyra_science_processing_utils.model_processors import MODEL_NAME_MAP, get_processor
from lyra_science_processing_utils.model_processors import rf_detr_seg_postprocessor as seg_module
from lyra_science_processing_utils.model_processors import rf_detr_semantic_segmentation_postprocessor as shim
from lyra_science_processing_utils.model_processors.rf_detr_seg_postprocessor import (
    DEFAULT_MASK_THRESHOLD,
    DEFAULT_NETWORK_INPUT,
    DEFAULT_SCORE_THRESHOLD,
    DEFAULT_TOP_K_FOR_INPUT,
    MODE_INSTANCE,
    MODE_SEMANTIC,
    PALETTE_MAX_INDEX,
    STAGE_TYPE_INSTANCE,
    STAGE_TYPE_SEMANTIC,
    RfDetrSegPostProcessor,
    paint_composite,
)
from lyra_science_processing_utils.utils import mask_rle
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.object_detection_result import (
    InstanceSegmentationResult,
    ObjectDetectionResult,
)

LOGGER = "lyra_science_processing_utils.model_processors.rf_detr_seg_postprocessor"


def _logit(p):
    # Inverse sigmoid so a target probability p is produced.
    p = min(max(p, 1e-6), 1 - 1e-6)
    return float(np.log(p / (1 - p)))


def _make(num_queries=4, num_slots=6, hm=8, wm=8):
    """Boxes [1,Q,4], logits [1,Q,S] at ~0.01, masks [1,Q,Hm,Wm] at sigmoid ~0."""
    boxes = np.zeros((1, num_queries, 4), np.float32)
    logits = np.full((1, num_queries, num_slots), _logit(0.01), np.float32)
    masks = np.full((1, num_queries, hm, wm), -10.0, np.float32)
    return boxes, logits, masks


def _pp(stage_type=STAGE_TYPE_INSTANCE, num_classes=5, **detection):
    detection.setdefault("num_classes", num_classes)
    return RfDetrSegPostProcessor({"type": stage_type, "image_width": 16, "detection": detection})


def _records(caplog, level, needle=""):
    return [r for r in caplog.records
            if r.name == LOGGER and r.levelno == level and needle in r.getMessage()]


# ---------------------------------------------------------------------------
# Instance mode (Req 9.4, 9.5)
# ---------------------------------------------------------------------------
def test_instance_mode_count_order_geometry_and_result_type():
    boxes, logits, masks = _make(num_queries=4)
    # Query 0: class 2 at 0.9, full mask. Query 1: class 0 at 0.7, full mask.
    boxes[0, 0] = (0.5, 0.5, 0.2, 0.4); logits[0, 0, 2] = _logit(0.9); masks[0, 0] = 10.0
    boxes[0, 1] = (0.25, 0.25, 0.1, 0.1); logits[0, 1, 0] = _logit(0.7); masks[0, 1] = 10.0
    results = _pp(class_names=["a", "b", "c", "d", "e"])([boxes, logits, masks], src_img_size=(640, 480))
    assert isinstance(results, list) and len(results) == 2
    # Confidence descending.
    assert [r.class_index for r in results] == [2, 0]
    assert [round(r.confidence, 3) for r in results] == [0.9, 0.7]
    r = results[0]
    assert isinstance(r, InstanceSegmentationResult) and isinstance(r, ObjectDetectionResult)
    # class keeps the detection meaning (label when class_names is configured).
    assert r.obj_class == "c" and r.class_index == 2
    assert r.confidence_threshold == DEFAULT_SCORE_THRESHOLD
    # cxcywh (0.5,0.5,0.2,0.4) -> xyxy (0.4,0.3,0.6,0.7) * (640,480): the detection squash geometry.
    np.testing.assert_allclose(r.bounding_box, [256.0, 144.0, 384.0, 336.0], atol=1e-2)
    # Mask at source resolution (H, W), area = pixel count.
    assert r.mask_shape == (480, 640)
    mask = r.mask()
    assert mask.dtype == np.bool_ and mask.shape == (480, 640)
    assert mask.all() and r.mask_area == 480 * 640


def test_instance_mode_without_class_names_uses_the_index_string():
    boxes, logits, masks = _make(num_queries=1)
    logits[0, 0, 3] = _logit(0.8); masks[0, 0] = 10.0
    results = _pp()([boxes, logits, masks], src_img_size=(8, 8))
    assert len(results) == 1
    assert results[0].obj_class == "3" and results[0].class_index == 3


def test_instance_mode_falls_back_to_network_input_without_source_size():
    boxes, logits, masks = _make(num_queries=1)
    boxes[0, 0] = (0.5, 0.5, 1.0, 1.0); logits[0, 0, 0] = _logit(0.9); masks[0, 0] = 10.0
    results = _pp(network_input=24)([boxes, logits, masks])
    assert results[0].mask_shape == (24, 24)
    np.testing.assert_allclose(results[0].bounding_box, [0.0, 0.0, 24.0, 24.0], atol=1e-4)


def test_serialize_adds_class_index_mask_rle_and_mask_area_and_round_trips():
    boxes, logits, masks = _make(num_queries=1, hm=4, wm=4)
    logits[0, 0, 1] = _logit(0.9)
    masks[0, 0, :2, :] = 10.0  # top half
    r = _pp()([boxes, logits, masks], src_img_size=(4, 4))[0]
    entry = r.serialize()
    assert list(entry.keys()) == [
        "bounding_box", "class", "confidence", "confidence_threshold", "class_index", "mask_rle", "mask_area",
    ]
    assert entry["class"] == "1" and entry["class_index"] == 1
    assert entry["mask_area"] == 8 == int(r.mask().sum())
    # Canonical_RLE round trip through the device codec (DD5).
    decoded = mask_rle.decode(entry["mask_rle"], 4, 4)
    assert np.array_equal(decoded, r.mask())
    assert sum(int(c) for c in entry["mask_rle"].split()) == 16
    # The JSON form is plain (ints, floats, str, list).
    json.dumps(entry)
    # deserialize rebuilds the packed mask from the RLE (the shape is supplied).
    back = InstanceSegmentationResult.deserialize(entry, mask_shape=(4, 4))
    assert np.array_equal(back.mask(), r.mask())
    assert (back.class_index, back.mask_area, back.obj_class, back.bounding_box, back.confidence) == (
        r.class_index, r.mask_area, r.obj_class, r.bounding_box, r.confidence)
    assert back.serialize() == entry
    with pytest.raises(ValueError, match="mask_shape"):
        InstanceSegmentationResult.deserialize(entry)


def test_mask_is_bit_packed_and_unpacks_on_demand():
    mask = np.zeros((6, 10), dtype=bool)
    mask[1:4, 2:7] = True
    r = InstanceSegmentationResult([0, 0, 1, 1], "0", 0.9, 0.5, 0, np.packbits(mask.ravel()), (6, 10), 15)
    assert r.mask_packed.dtype == np.uint8 and r.mask_packed.size == (60 + 7) // 8  # 1 bit per pixel
    assert np.array_equal(r.mask(), mask)
    assert r.serialize()["mask_rle"] == mask_rle.encode(mask)


@pytest.mark.parametrize("mask_threshold, expected_columns", [(0.2, 3), (0.5, 2), (0.8, 1)])
def test_mask_is_bilinearly_upsampled_then_thresholded(mask_threshold, expected_columns):
    """O3 b (rfdetr's reference order): sigmoid -> bilinear resize to the
    source frame -> threshold. A 2x2 grid whose left column is foreground
    upsampled to 4x4 gives column probabilities ~1, 0.75, 0.25, ~0, so the
    threshold moves the edge (nearest-then-threshold would always give two
    columns)."""
    boxes, logits, masks = _make(num_queries=1, hm=2, wm=2)
    logits[0, 0, 0] = _logit(0.9)
    masks[0, 0, :, 0] = 10.0
    r = _pp(mask_threshold=mask_threshold)([boxes, logits, masks], src_img_size=(4, 4))[0]
    mask = r.mask()
    assert mask.shape == (4, 4)
    assert mask[:, :expected_columns].all() and not mask[:, expected_columns:].any()
    assert r.mask_area == 4 * expected_columns


def test_flattened_top_k_lets_one_query_yield_two_classes():
    boxes, logits, masks = _make(num_queries=2)
    boxes[0, 0] = (0.5, 0.5, 0.5, 0.5)
    logits[0, 0, 1] = _logit(0.9); logits[0, 0, 3] = _logit(0.8)
    masks[0, 0] = 10.0
    results = _pp()([boxes, logits, masks], src_img_size=(8, 8))
    assert [(r.class_index, round(r.confidence, 3)) for r in results] == [(1, 0.9), (3, 0.8)]
    # Same query: same box, same mask, two instances (as the detection decoder yields two boxes).
    assert results[0].bounding_box == results[1].bounding_box
    assert np.array_equal(results[0].mask(), results[1].mask())


def test_top_k_caps_the_instance_list_to_the_highest_scores():
    boxes, logits, masks = _make(num_queries=10)
    for q in range(10):
        logits[0, q, 0] = _logit(0.55 + 0.04 * q)
        masks[0, q] = 10.0
    results = _pp(top_k=3)([boxes, logits, masks], src_img_size=(8, 8))
    assert len(results) == 3
    assert [round(r.confidence, 2) for r in results] == [0.91, 0.87, 0.83]


def test_top_k_default_comes_from_the_size_table_and_is_capped_by_q_times_c():
    assert DEFAULT_TOP_K_FOR_INPUT == {312: 100, 384: 100, 432: 200, 504: 200, 624: 300, 768: 300}
    assert _pp(network_input=312).top_k == 100
    assert _pp(network_input=504).top_k == 200
    assert _pp(network_input=768).top_k == 300
    assert _pp(network_input=640).top_k == 300  # off-table: the detection default
    assert _pp(network_input=312, top_k=7).top_k == 7
    # k = min(top_k, Q x C): 2 queries x 5 classes all confident -> 10 instances with top_k 100.
    boxes, logits, masks = _make(num_queries=2)
    logits[:] = _logit(0.9); masks[:] = 10.0
    assert len(_pp(top_k=100)([boxes, logits, masks], src_img_size=(8, 8))) == 10


def test_zero_foreground_candidate_is_dropped():
    boxes, logits, masks = _make(num_queries=2)
    logits[0, 0, 0] = _logit(0.95)                       # confident, but mask stays ~0 everywhere
    logits[0, 1, 1] = _logit(0.6); masks[0, 1] = 10.0    # kept
    results = _pp()([boxes, logits, masks], src_img_size=(8, 8))
    assert [(r.class_index, round(r.confidence, 2)) for r in results] == [(1, 0.6)]


def test_score_threshold_filters_candidates():
    boxes, logits, masks = _make(num_queries=1)
    logits[0, 0, 0] = _logit(0.3); masks[0, 0] = 10.0
    assert _pp()([boxes, logits, masks], src_img_size=(8, 8)) == []
    assert len(_pp(score_threshold=0.25)([boxes, logits, masks], src_img_size=(8, 8))) == 1


# ---------------------------------------------------------------------------
# Slot handling (Req 9.3, O6 a)
# ---------------------------------------------------------------------------
def test_c_plus_one_slots_never_score_the_trailing_background_slot(caplog):
    boxes, logits, masks = _make(num_queries=2, num_slots=6)   # num_classes 5 + background
    logits[0, 0, 5] = _logit(0.99); masks[0, 0] = 10.0          # background slot confident
    logits[0, 1, 4] = _logit(0.8); masks[0, 1] = 10.0           # last real class
    results = _pp(num_classes=5)([boxes, logits, masks], src_img_size=(8, 8))
    assert [(r.class_index, round(r.confidence, 2)) for r in results] == [(4, 0.8)]
    assert not _records(caplog, logging.ERROR)


def test_exactly_c_slots_scores_every_slot():
    boxes, logits, masks = _make(num_queries=1, num_slots=5)   # foreign export, no background slot
    logits[0, 0, 4] = _logit(0.8); masks[0, 0] = 10.0
    results = _pp(num_classes=5)([boxes, logits, masks], src_img_size=(8, 8))
    assert [(r.class_index, round(r.confidence, 2)) for r in results] == [(4, 0.8)]


def test_over_declared_num_classes_logs_an_error_once_and_uses_the_slot_count(caplog):
    caplog.set_level(logging.DEBUG)
    boxes, logits, masks = _make(num_queries=1, num_slots=5)
    logits[0, 0, 4] = _logit(0.8); masks[0, 0] = 10.0
    pp = _pp(num_classes=7)
    for _ in range(2):
        results = pp([boxes, logits, masks], src_img_size=(8, 8))
        assert [(r.class_index, round(r.confidence, 2)) for r in results] == [(4, 0.8)]
    errors = _records(caplog, logging.ERROR, "num_classes=7")
    assert len(errors) == 1 and "5 slots" in errors[0].getMessage()


def test_no_num_classes_scores_every_slot():
    boxes, logits, masks = _make(num_queries=1, num_slots=6)
    logits[0, 0, 5] = _logit(0.8); masks[0, 0] = 10.0
    pp = RfDetrSegPostProcessor({"type": STAGE_TYPE_INSTANCE, "detection": {}})
    assert pp.num_classes is None
    results = pp([boxes, logits, masks], src_img_size=(8, 8))
    assert [(r.class_index, round(r.confidence, 2)) for r in results] == [(5, 0.8)]


# ---------------------------------------------------------------------------
# Tensor identification (Req 9.2, DD18)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 2, 0), (2, 0, 1)])
def test_shape_based_identification_is_order_independent_and_nameless(order, caplog):
    boxes, logits, masks = _make(num_queries=2)
    boxes[0, 0] = (0.5, 0.5, 0.5, 0.5); logits[0, 0, 2] = _logit(0.9); masks[0, 0, :4, :] = 10.0
    tensors = [boxes, logits, masks]
    results = _pp()([tensors[i] for i in order], src_img_size=(8, 8))
    assert [(r.class_index, round(r.confidence, 2), r.mask_area) for r in results] == [(2, 0.9, 32)]
    assert results[0].bounding_box == [2.0, 2.0, 6.0, 6.0]
    assert not _records(caplog, logging.WARNING)


def test_batch_dim_already_squeezed_is_accepted():
    boxes, logits, masks = _make(num_queries=2)
    logits[0, 0, 1] = _logit(0.9); masks[0, 0] = 10.0
    results = _pp()([boxes[0], logits[0], masks[0]], src_img_size=(8, 8))
    assert [r.class_index for r in results] == [1]


def _three_class_outputs():
    """A three-class model: logits [Q, 4] (3 classes + background) like the boxes."""
    boxes, logits, masks = _make(num_queries=3, num_slots=4)
    boxes[0, 0] = (0.5, 0.5, 0.5, 0.5); logits[0, 0, 1] = _logit(0.8); masks[0, 0] = 10.0
    return boxes, logits, masks


def test_three_class_tie_break_uses_output_names_in_graph_order(caplog):
    caplog.set_level(logging.DEBUG)
    boxes, logits, masks = _three_class_outputs()
    # The runner returns graph order; the manifest's output_names are that order.
    pp = _pp(num_classes=3, output_names=["labels", "dets", "masks"])
    results = pp([logits, boxes, masks], src_img_size=(8, 8))
    assert [(r.class_index, round(r.confidence, 2)) for r in results] == [(1, 0.8)]
    assert results[0].bounding_box == [2.0, 2.0, 6.0, 6.0]
    assert not _records(caplog, logging.WARNING)
    # Names in the other order, outputs in that order: same decode.
    pp2 = _pp(num_classes=3, output_names=["dets", "labels", "masks"])
    results2 = pp2([boxes, logits, masks], src_img_size=(8, 8))
    assert [(r.class_index, r.bounding_box) for r in results2] == [(1, [2.0, 2.0, 6.0, 6.0])]


def test_three_class_tie_break_without_names_takes_list_order_with_one_warning(caplog):
    caplog.set_level(logging.DEBUG)
    boxes, logits, masks = _three_class_outputs()
    pp = _pp(num_classes=3)
    for _ in range(2):
        results = pp([boxes, logits, masks], src_img_size=(8, 8))
        assert [(r.class_index, r.bounding_box) for r in results] == [(1, [2.0, 2.0, 6.0, 6.0])]
    warnings = _records(caplog, logging.WARNING, "two [Q, 4] tensors")
    assert len(warnings) == 1
    assert "output_names" in warnings[0].getMessage()


def test_three_class_tie_break_with_names_not_naming_dets_or_labels_falls_back(caplog):
    caplog.set_level(logging.DEBUG)
    boxes, logits, masks = _three_class_outputs()
    pp = _pp(num_classes=3, output_names=["boxes", "scores", "masks"])
    results = pp([boxes, logits, masks], src_img_size=(8, 8))
    assert [(r.class_index, r.bounding_box) for r in results] == [(1, [2.0, 2.0, 6.0, 6.0])]
    assert len(_records(caplog, logging.WARNING, "two [Q, 4] tensors")) == 1


@pytest.mark.parametrize("bad", [
    [],
    [np.zeros((1, 4, 4), np.float32)],
    [np.zeros((1, 4, 4), np.float32), np.zeros((1, 4, 6), np.float32)],                      # no masks
    [np.zeros((1, 4, 4), np.float32), np.zeros((1, 4, 8, 8), np.float32), np.zeros((1, 4, 8, 8), np.float32)],  # one rank-2
    [np.zeros((1, 4, 6), np.float32), np.zeros((1, 4, 7), np.float32), np.zeros((1, 4, 8, 8), np.float32)],  # no [Q, 4]
    [np.zeros((1, 4, 4), np.float32), np.zeros((1, 5, 6), np.float32), np.zeros((1, 4, 8, 8), np.float32)],  # Q mismatch
    "not a list",
])
def test_too_few_or_inconsistent_outputs_give_empty_results_and_warn_once(bad, caplog):
    caplog.set_level(logging.DEBUG)
    inst = _pp()
    for _ in range(2):
        assert inst(bad, src_img_size=(8, 8)) == []
    assert len(_records(caplog, logging.WARNING, "cannot be decoded")) == 1
    sem = _pp(stage_type=STAGE_TYPE_SEMANTIC)
    res = sem(bad, src_img_size=(8, 8))
    assert isinstance(res, AnomalyResult)
    assert res.score == 0.0 and res.mask.shape == (8, 8) and not res.mask.any()
    assert len(_records(caplog, logging.WARNING, "cannot be decoded")) == 2  # one per processor instance


# ---------------------------------------------------------------------------
# Mode (Req 9.1, 9.6, D5)
# ---------------------------------------------------------------------------
def test_mode_is_the_stage_type():
    assert _pp(stage_type=STAGE_TYPE_INSTANCE).mode == MODE_INSTANCE == "instance"
    assert _pp(stage_type=STAGE_TYPE_SEMANTIC).mode == MODE_SEMANTIC == "semantic"
    assert STAGE_TYPE_INSTANCE == "rf_detr_instance_segmentation"
    assert STAGE_TYPE_SEMANTIC == "rf_detr_semantic_segmentation"
    # No type at all (the eight existing tests) -> semantic.
    assert RfDetrSegPostProcessor({"detection": {"num_classes": 5}}).mode == MODE_SEMANTIC


def test_legacy_semantic_manifest_decodes_in_semantic_mode_with_top_k_100():
    """The D5 component: stage type rf_detr_semantic_segmentation, a stage-level
    detection block without output_mode / top_k / output_names."""
    pp = RfDetrSegPostProcessor({
        "type": "rf_detr_semantic_segmentation",
        "input_shape": [1, 3, 312, 312],
        "image_width": 312, "image_height": 312,
        "image_range_scale": True, "normalize": True, "threshold": 0.5,
        "num_classes": 91,
        "detection": {"layout": "rf_detr", "num_classes": 91, "score_threshold": 0.5,
                      "mask_threshold": 0.5, "network_input": 312},
    })
    assert pp.mode == MODE_SEMANTIC
    assert pp.top_k == 100 == DEFAULT_TOP_K_FOR_INPUT[312]
    assert pp.output_names is None and pp.class_names is None
    assert (pp.score_threshold, pp.mask_threshold, pp.num_classes, pp.network_input) == (0.5, 0.5, 91, 312)
    boxes, logits, masks = _make(num_queries=3, num_slots=91, hm=78, wm=78)
    logits[0, 0, 7] = _logit(0.9); masks[0, 0] = 10.0
    res = pp([boxes, logits, masks], src_img_size=(32, 24))
    assert isinstance(res, AnomalyResult)
    assert res.mask.shape == (24, 32) and res.mask.dtype == np.uint8
    assert set(np.unique(res.mask).tolist()) == {8}          # class 7 -> index 8
    assert res.score == pytest.approx(0.9, abs=1e-3)


def test_defaults_when_the_detection_block_is_absent():
    pp = RfDetrSegPostProcessor({"type": STAGE_TYPE_INSTANCE})
    assert (pp.score_threshold, pp.mask_threshold) == (DEFAULT_SCORE_THRESHOLD, DEFAULT_MASK_THRESHOLD) == (0.5, 0.5)
    assert pp.network_input == DEFAULT_NETWORK_INPUT == 312
    assert pp.top_k == 100
    assert pp.num_classes is None and pp.output_names is None and pp.class_names is None


def test_output_mode_contradiction_logs_an_error_and_the_stage_type_wins(caplog):
    caplog.set_level(logging.DEBUG)
    boxes, logits, masks = _make(num_queries=1)
    logits[0, 0, 0] = _logit(0.9); masks[0, 0] = 10.0
    pp = _pp(stage_type=STAGE_TYPE_INSTANCE, output_mode="semantic")
    assert pp.mode == MODE_INSTANCE
    assert isinstance(pp([boxes, logits, masks], src_img_size=(8, 8)), list)
    errors = _records(caplog, logging.ERROR, "output_mode")
    assert len(errors) == 1 and "'semantic'" in errors[0].getMessage()
    pp2 = _pp(stage_type=STAGE_TYPE_SEMANTIC, output_mode="instance")
    assert pp2.mode == MODE_SEMANTIC
    assert isinstance(pp2([boxes, logits, masks], src_img_size=(8, 8)), AnomalyResult)
    assert len(_records(caplog, logging.ERROR, "output_mode")) == 2


def test_consistent_output_mode_logs_nothing(caplog):
    caplog.set_level(logging.DEBUG)
    _pp(stage_type=STAGE_TYPE_INSTANCE, output_mode="instance")
    _pp(stage_type=STAGE_TYPE_SEMANTIC, output_mode="semantic")
    assert not _records(caplog, logging.ERROR)


def test_semantic_mode_paints_the_same_instances_as_instance_mode():
    boxes, logits, masks = _make(num_queries=2)
    logits[0, 0, 1] = _logit(0.6); masks[0, 0] = 10.0            # class 1 everywhere
    logits[0, 1, 2] = _logit(0.95); masks[0, 1, :4, :] = 10.0    # class 2, top half, higher score
    instance_pp = _pp(stage_type=STAGE_TYPE_INSTANCE)
    semantic_pp = _pp(stage_type=STAGE_TYPE_SEMANTIC)
    instances = instance_pp([boxes, logits, masks], src_img_size=(8, 8))
    res = semantic_pp([boxes, logits, masks], src_img_size=(8, 8))
    assert isinstance(res, AnomalyResult)
    assert res.score == pytest.approx(0.95, abs=1e-3)
    assert np.array_equal(res.mask, paint_composite(instances, 8, 8))
    assert (res.mask[:4] == 3).all() and (res.mask[4:] == 2).all()


# ---------------------------------------------------------------------------
# paint_composite (Req 9.5, 10.1)
# ---------------------------------------------------------------------------
def _instance(class_index, confidence, mask):
    mask = np.asarray(mask, dtype=bool)
    return InstanceSegmentationResult([0, 0, 1, 1], str(class_index), confidence, 0.5, class_index,
                                      np.packbits(mask.ravel()), mask.shape, int(mask.sum()))


def test_paint_composite_higher_confidence_wins_overlap_regardless_of_input_order():
    full = np.ones((4, 4), dtype=bool)
    left = np.zeros((4, 4), dtype=bool); left[:, :2] = True
    high = _instance(0, 0.9, left)   # class 0 -> index 1
    low = _instance(4, 0.6, full)    # class 4 -> index 5
    expected = np.full((4, 4), 5, dtype=np.uint8); expected[:, :2] = 1
    for order in ([high, low], [low, high]):
        composite = paint_composite(order, 4, 4)
        assert composite.dtype == np.uint8 and composite.shape == (4, 4)
        assert np.array_equal(composite, expected)
    assert not paint_composite([], 3, 5).any() and paint_composite([], 3, 5).shape == (3, 5)


def test_paint_composite_saturates_at_the_palette_and_the_colour_mask_renders():
    assert PALETTE_MAX_INDEX == len(DEFAULT_ANOMALY_MASK_PALETTE) - 1 == 103
    full = np.ones((2, 3), dtype=bool)
    composite = paint_composite([_instance(150, 0.9, full), _instance(102, 0.8, full)], 2, 3)
    assert composite.max() == PALETTE_MAX_INDEX
    assert convert_index_mask_to_color_mask(composite).shape == (2, 3, 3)
    assert paint_composite([_instance(102, 0.9, full)], 2, 3).max() == 103  # class 102 -> row 103 unclamped


def test_110_class_manifest_warns_once_and_never_indexes_past_the_palette(caplog):
    caplog.set_level(logging.DEBUG)
    boxes, logits, masks = _make(num_queries=2, num_slots=111)
    logits[0, 0, 109] = _logit(0.9); masks[0, 0] = 10.0           # class 109 -> would be index 110
    logits[0, 1, 50] = _logit(0.7); masks[0, 1, :4, :] = 10.0     # class 50 -> index 51
    sem = _pp(stage_type=STAGE_TYPE_SEMANTIC, num_classes=110)
    inst = _pp(stage_type=STAGE_TYPE_INSTANCE, num_classes=110)
    warnings = _records(caplog, logging.WARNING, "palette")
    assert len(warnings) == 2 and "110 classes" in warnings[0].getMessage()  # one per processor
    res = sem([boxes, logits, masks], src_img_size=(8, 8))
    assert res.mask.max() == PALETTE_MAX_INDEX == 103
    assert set(np.unique(res.mask).tolist()) == {103}       # the 0.9 instance paints last everywhere
    convert_index_mask_to_color_mask(res.mask)              # does not raise
    instances = inst([boxes, logits, masks], src_img_size=(8, 8))
    assert [r.class_index for r in instances] == [109, 50]  # the Instance_Results stay exact
    assert len(_records(caplog, logging.WARNING, "palette")) == 2


def test_decoder_module_imports_nothing_from_the_mask_utils_package():
    with open(seg_module.__file__) as fh:
        source = fh.read()
    assert re.search(r"^\s*(from|import)\s+lyra_anomalies_mask_utils", source, re.M) is None


# ---------------------------------------------------------------------------
# Registry and shim (Req 9.1, DD2)
# ---------------------------------------------------------------------------
def test_registry_maps_both_seg_stage_types_to_the_one_decoder():
    assert MODEL_NAME_MAP["financier"] == ("rf_detr_semantic_segmentation", RfDetrSegPostProcessor)
    assert MODEL_NAME_MAP["macaron"] == ("rf_detr_instance_segmentation", RfDetrSegPostProcessor)
    assert get_processor("rf_detr_instance_segmentation") is RfDetrSegPostProcessor
    assert get_processor("rf_detr_semantic_segmentation") is RfDetrSegPostProcessor
    assert get_processor("macaron") is RfDetrSegPostProcessor


def test_semantic_module_is_an_import_shim():
    assert shim.RfDetrSemanticSegmentationPostProcessor is RfDetrSegPostProcessor
    assert shim.DEFAULT_SCORE_THRESHOLD == DEFAULT_SCORE_THRESHOLD == 0.5
    assert shim.DEFAULT_MASK_THRESHOLD == DEFAULT_MASK_THRESHOLD == 0.5
    assert shim.DEFAULT_NETWORK_INPUT == DEFAULT_NETWORK_INPUT == 312


# ---------------------------------------------------------------------------
# S6 timing fixture (DD3 / DD5 costs; design spike S6, Req 10.3)
# ---------------------------------------------------------------------------
@pytest.mark.timing
@pytest.mark.skipif(not os.environ.get("DDA_SEG_TIMING"),
                    reason="S6 timing fixture: set DDA_SEG_TIMING=1 to record the decode costs on the device under test")
@pytest.mark.parametrize("n_instances", [10, 100])
def test_timing_s6_decode_costs_at_1080p(n_instances):
    """Records, for 10 and 100 kept instances at 1920x1080 from the Seg-Nano
    78x78 mask grid: the decode (per-instance sigmoid + bilinear resize +
    threshold + packbits), the composite paint (unpackbits + paint), the
    serialisation (unpackbits + Canonical_RLE encode) and a bare unpack.
    Prints one ``S6_TIMING`` JSON line; appends it to $DDA_SEG_TIMING_OUT
    when set."""
    out_w, out_h, grid = 1920, 1080, 78
    rng = np.random.default_rng(0)
    boxes, logits, masks = _make(num_queries=n_instances, num_slots=6, hm=grid, wm=grid)
    boxes[0] = rng.uniform(0.2, 0.8, size=(n_instances, 4)).astype(np.float32)
    logits[0, :, 0] = _logit(0.9)
    for q in range(n_instances):
        r0, c0 = rng.integers(0, grid // 2, size=2)
        masks[0, q, r0:r0 + grid // 3, c0:c0 + grid // 3] = 10.0
    pp = _pp(num_classes=5, top_k=300)
    t0 = time.perf_counter()
    instances = pp([boxes, logits, masks], src_img_size=(out_w, out_h))
    t_decode = time.perf_counter() - t0
    assert len(instances) == n_instances
    t0 = time.perf_counter()
    composite = paint_composite(instances, out_h, out_w)
    t_paint = time.perf_counter() - t0
    assert composite.shape == (out_h, out_w)
    t0 = time.perf_counter()
    entries = [inst.serialize() for inst in instances]
    t_serialize = time.perf_counter() - t0
    t0 = time.perf_counter()
    for inst in instances:
        inst.mask()
    t_unpack = time.perf_counter() - t0
    record = {
        "instances": n_instances, "frame": [out_w, out_h], "mask_grid": grid,
        "decode_ms": round(t_decode * 1000, 1), "decode_ms_per_instance": round(t_decode * 1000 / n_instances, 2),
        "paint_ms": round(t_paint * 1000, 1), "serialize_ms": round(t_serialize * 1000, 1),
        "unpack_ms": round(t_unpack * 1000, 1),
        "packed_bytes_per_instance": int(instances[0].mask_packed.nbytes),
        "rle_chars_total": sum(len(e["mask_rle"]) for e in entries),
        "numpy": np.__version__, "cv2": cv2.__version__, "machine": platform.machine(),
    }
    print("S6_TIMING " + json.dumps(record))
    out = os.environ.get("DDA_SEG_TIMING_OUT")
    if out:
        with open(out, "a") as fh:
            fh.write(json.dumps(record) + "\n")
