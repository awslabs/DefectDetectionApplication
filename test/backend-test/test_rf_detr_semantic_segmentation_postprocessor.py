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
"""Unit tests for the RF-DETR semantic-segmentation post-processor.

Synthetic three-tensor outputs (boxes [Q,4], logits [Q,C], masks [Q,Hm,Wm])
exercise: shape-based tensor identification with mislabeled/reordered outputs,
score thresholding, mask compositing with higher-score-wins overlap, the
background=0 / class_id+1 index convention, resize-to-source, and the
empty/degenerate cases.
"""
import numpy as np
import pytest

from lyra_science_processing_utils.model_processors.rf_detr_semantic_segmentation_postprocessor import (
    RfDetrSemanticSegmentationPostProcessor,
)
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult


def _logit(p):
    # Inverse sigmoid so a target class probability p is produced.
    p = min(max(p, 1e-6), 1 - 1e-6)
    return float(np.log(p / (1 - p)))


def _make(num_queries=3, num_classes=5, hm=8, wm=8):
    boxes = np.zeros((1, num_queries, 4), np.float32)
    logits = np.full((1, num_queries, num_classes), _logit(0.01), np.float32)
    masks = np.full((1, num_queries, hm, wm), -10.0, np.float32)  # sigmoid ~0
    return boxes, logits, masks


def _pp(num_classes=5, score_threshold=0.5, mask_threshold=0.5):
    return RfDetrSemanticSegmentationPostProcessor(
        {"image_width": 16, "detection": {
            "score_threshold": score_threshold,
            "mask_threshold": mask_threshold,
            "num_classes": num_classes,
        }}
    )


def test_returns_anomaly_result_with_index_mask():
    boxes, logits, masks = _make()
    # Query 0: class 2 confident over its whole (8x8) mask.
    logits[0, 0, 2] = _logit(0.9)
    masks[0, 0, :, :] = 10.0
    res = _pp()([boxes, logits, masks], src_img_size=(16, 16))
    assert isinstance(res, AnomalyResult)
    assert res.mask.shape == (16, 16)          # resized to source (H, W)
    assert res.mask.dtype == np.uint8
    # class_id 2 -> index 3 (background reserved as 0).
    assert set(np.unique(res.mask).tolist()) == {3}
    assert res.score == pytest.approx(0.9, abs=1e-3)


def test_shape_based_id_is_output_order_independent():
    boxes, logits, masks = _make()
    logits[0, 0, 1] = _logit(0.8)
    masks[0, 0, :, :] = 10.0
    # Feed in a shuffled order (masks, boxes, logits) — names/order unreliable.
    res = _pp()([masks, boxes, logits], src_img_size=(16, 16))
    assert set(np.unique(res.mask).tolist()) == {2}  # class 1 -> index 2


def test_threshold_filters_low_confidence():
    boxes, logits, masks = _make()
    logits[0, 0, 3] = _logit(0.3)  # below 0.5 threshold
    masks[0, 0, :, :] = 10.0
    res = _pp()([boxes, logits, masks], src_img_size=(16, 16))
    assert np.count_nonzero(res.mask) == 0     # nothing kept -> all background
    assert res.score == 0.0


def test_higher_score_wins_on_overlap():
    boxes, logits, masks = _make(num_queries=2)
    # Both masks cover the full frame; query 1 has the higher score and must win.
    logits[0, 0, 1] = _logit(0.6); masks[0, 0, :, :] = 10.0
    logits[0, 1, 2] = _logit(0.95); masks[0, 1, :, :] = 10.0
    res = _pp()([boxes, logits, masks], src_img_size=(16, 16))
    # class 2 -> index 3 should dominate the overlap.
    assert set(np.unique(res.mask).tolist()) == {3}


def test_mask_threshold_limits_region():
    boxes, logits, masks = _make(num_queries=1, hm=8, wm=8)
    logits[0, 0, 1] = _logit(0.9)
    # Only the top half of the mask is active.
    masks[0, 0, :4, :] = 10.0
    res = _pp()([boxes, logits, masks], src_img_size=(8, 8))
    fg = np.count_nonzero(res.mask)
    assert 0 < fg < res.mask.size            # partial coverage
    assert set(np.unique(res.mask).tolist()) == {0, 2}


def test_no_source_size_falls_back_to_network_input():
    boxes, logits, masks = _make(num_queries=1)
    logits[0, 0, 0] = _logit(0.9); masks[0, 0, :, :] = 10.0
    pp = RfDetrSemanticSegmentationPostProcessor(
        {"detection": {"num_classes": 5, "network_input": 24}}
    )
    res = pp([boxes, logits, masks])          # no src_img_size
    assert res.mask.shape == (24, 24)
    assert set(np.unique(res.mask).tolist()) == {1}  # class 0 -> index 1


def test_too_few_outputs_returns_background():
    boxes, logits, _ = _make()
    res = _pp()([boxes, logits], src_img_size=(16, 16))  # missing masks
    assert np.count_nonzero(res.mask) == 0
    assert res.score == 0.0


def test_empty_when_all_below_threshold_multiclass():
    boxes, logits, masks = _make(num_queries=3)
    for q in range(3):
        masks[0, q, :, :] = 10.0            # masks active but scores stay ~0.01
    res = _pp()([boxes, logits, masks], src_img_size=(10, 10))
    assert np.count_nonzero(res.mask) == 0
