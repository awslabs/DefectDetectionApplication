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
"""Post-processor that decodes a raw RF-DETR-Seg ONNX output into per-instance
results (**instance** mode) or one semantic index mask (**semantic** mode).
One class serves both Seg stage types; the mode is the stage type
(rfdetr-seg-support DD2, DD3, DD18; Requirements 9.1-9.6).

RF-DETR-Seg emits three tensors (order/name unreliable, so they are identified
strictly by shape after squeezing a leading batch dim of 1):

  * boxes:  ``[1, Q, 4]``       -- normalized ``cxcywh`` in [0, 1]
  * logits: ``[1, Q, S]``       -- per-query class logits; ``S = num_classes + 1``
                                   (rfdetr's trailing background slot, never a
                                   class) or exactly ``num_classes`` for a
                                   foreign export declared as such
  * masks:  ``[1, Q, Hm, Wm]``  -- per-query low-resolution mask logits

When ``S == 4`` (a three-class model) boxes and logits share a shape; the
manifest's graph-order ``detection.output_names`` (``dets`` / ``labels``)
settle it, else the runner's order is taken as ``[boxes, logits]`` with a
one-time warning (DD18) -- the same rule as the detection decoder.

Decode (O3 b -- candidates as the detection decoder selects them, masks as
rfdetr evaluates them):
  1. ``scores = sigmoid(logits[:, :C])`` with ``C = min(num_classes, S)``
     (``num_classes > S`` logs an error once and uses ``S``; no ``num_classes``
     scores every slot).
  2. flattened top-k over query x class pairs with ``k = min(top_k, Q * C)``,
     then ``score >= score_threshold`` (``rf_detr_detection_postprocessor``);
     one query may yield two instances of different classes.
  3. per kept pair, score descending: ``sigmoid(masks[q])`` **bilinearly**
     upsampled to the source frame, **then** ``> mask_threshold``; a mask with
     no foreground pixel drops the candidate (a box without a mask). The mask
     is bit-packed (``numpy.packbits``, DD3) before the next pair, the box is
     denormalised with the detection decoder's squash geometry.

Instance mode returns ``list[InstanceSegmentationResult]`` (confidence
descending, at most ``top_k``); the model graph wraps it as detections and the
Triton base model paints the Composite_Mask with ``paint_composite``. Semantic
mode returns ``AnomalyResult(score=top confidence, mask=paint_composite(...))``
over the same instances (index ``class + 1``, 0 = background, higher confidence
wins overlaps), so both modes produce the identical composite. The manifest is
the only source of the mode (O15 a). Uses numpy + cv2; no torch on the hot path.
"""
import logging
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.model_processors.rf_detr_detection_postprocessor import (
    _cxcywh_to_xyxy,
    _sigmoid,
    split_same_shape_pair,
)
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.object_detection_result import InstanceSegmentationResult

LOG = logging.getLogger(__name__)

MODE_INSTANCE, MODE_SEMANTIC = "instance", "semantic"
STAGE_TYPE_INSTANCE, STAGE_TYPE_SEMANTIC = "rf_detr_instance_segmentation", "rf_detr_semantic_segmentation"
DEFAULT_SCORE_THRESHOLD = DEFAULT_MASK_THRESHOLD = 0.5
# Queries per model size, keyed by the square network input
# (nano 312, small 384, medium 432, large 504, xlarge 624, 2xlarge 768).
DEFAULT_TOP_K_FOR_INPUT = {312: 100, 384: 100, 432: 200, 504: 200, 624: 300, 768: 300}
DEFAULT_TOP_K = 300
DEFAULT_NETWORK_INPUT = 312  # RF-DETR-Seg-Nano default square input
# Last row of the anomaly-mask palette (104 rows): the Composite_Mask index is
# clamped to it so a graph with more than 103 classes never indexes past the
# palette. Pinned by a test to len(DEFAULT_ANOMALY_MASK_PALETTE) - 1; this
# module imports nothing from lyra_anomalies_mask_utils on purpose.
PALETTE_MAX_INDEX = 103


def paint_composite(instances: Sequence[InstanceSegmentationResult], out_h: int, out_w: int) -> np.ndarray:
    """Paint the semantic Composite_Mask (``uint8[out_h, out_w]``, 0 =
    background) from instance results, ascending by confidence so the
    higher-confidence instance wins every overlap; the painted value is
    ``min(class_index + 1, PALETTE_MAX_INDEX)``. Reads only ``.confidence``,
    ``.class_index`` and ``.mask()`` -- the semantic branch and the Triton
    base model call this one function with the same argument type."""
    index_mask = np.zeros((int(out_h), int(out_w)), dtype=np.uint8)
    for inst in sorted(instances, key=lambda r: float(r.confidence)):
        value = min(int(inst.class_index) + 1, PALETTE_MAX_INDEX)
        index_mask[inst.mask()] = np.uint8(value)
    return index_mask


class RfDetrSegPostProcessor(InferencePostProcessor):
    """Decode a raw RF-DETR-Seg output into InstanceSegmentationResult list
    (instance mode) or a semantic index mask wrapped in an AnomalyResult
    (semantic mode), per the stage type."""

    def __init__(self, config: Dict, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        config = config if isinstance(config, dict) else {}
        # The stage type is the mode: rf_detr_instance_segmentation -> instance,
        # anything else (rf_detr_semantic_segmentation, the legacy manifest, a
        # config without a type) -> semantic.
        stage_type = config.get("type")
        self.mode = MODE_INSTANCE if stage_type == STAGE_TYPE_INSTANCE else MODE_SEMANTIC
        detection = config.get("detection") or {}
        declared = detection.get("output_mode")
        if declared is not None and declared != self.mode:
            LOG.error(
                "RF-DETR-Seg stage type %r decodes in %s mode but detection.output_mode is %r; "
                "the stage type wins", stage_type, self.mode, declared,
            )
        self.score_threshold = float(detection.get("score_threshold", DEFAULT_SCORE_THRESHOLD))
        self.mask_threshold = float(detection.get("mask_threshold", DEFAULT_MASK_THRESHOLD))
        num_classes = detection.get("num_classes")
        self.num_classes = int(num_classes) if num_classes is not None else None
        self.network_input = int(
            detection.get("network_input")
            or config.get("image_width")
            or DEFAULT_NETWORK_INPUT
        )
        self.top_k = int(detection.get("top_k") or DEFAULT_TOP_K_FOR_INPUT.get(self.network_input, DEFAULT_TOP_K))
        self.output_names = detection.get("output_names") or None  # graph order (DD18)
        self.class_names = detection.get("class_names") or None
        if self.num_classes is not None and self.num_classes > PALETTE_MAX_INDEX:
            LOG.warning(
                "%d classes exceed the %d-colour palette; classes >= %d share the last colour in the "
                "Composite_Mask", self.num_classes, PALETTE_MAX_INDEX, PALETTE_MAX_INDEX,
            )
        self._warned_split = False
        self._warned_order = False
        self._warned_slots = False

    def __call__(self, model_output: List[np.ndarray], *args, **kwargs):
        src_img_size = kwargs.get("src_img_size")  # (width, height) of source image
        if src_img_size:
            out_w, out_h = int(src_img_size[0]), int(src_img_size[1])
        else:
            out_w = out_h = self.network_input

        boxes, logits, masks = self._split_outputs(model_output)
        instances = [] if boxes is None else self._select(boxes, logits, masks, out_w, out_h)
        if self.mode == MODE_INSTANCE:
            # The graph wraps the list as detections; the Triton base model
            # paints the composite from the packed masks (Requirement 10.1).
            return instances
        top_score = float(instances[0].confidence) if instances else 0.0
        return AnomalyResult(score=top_score, mask=paint_composite(instances, out_h, out_w))

    # ── helpers ────────────────────────────────────────────────────────────
    def _split_outputs(
        self, model_output: List[np.ndarray]
    ) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], Optional[np.ndarray]]:
        """Identify (boxes[Q,4], logits[Q,S], masks[Q,Hm,Wm]) from the runner
        output list by shape, in any order, after squeezing a leading batch dim
        of 1. Fewer than three usable tensors or a query-count mismatch gives
        (None, None, None) with one warning per processor instance -- never a
        raise. Two [Q, 4] tensors (S == 4) are told apart by the manifest's
        graph-order output_names, else by list order with a one-time warning."""
        if not isinstance(model_output, (list, tuple)) or len(model_output) < 3:
            return self._split_failed("expected three output tensors, got %s" % (
                len(model_output) if isinstance(model_output, (list, tuple)) else type(model_output).__name__))
        rank2, positions, rank3 = [], [], []
        for pos, o in enumerate(model_output):
            a = np.asarray(o)
            # Squeeze a single leading batch dim so boxes->(Q,4), logits->(Q,S),
            # masks->(Q,Hm,Wm).
            if a.ndim >= 3 and a.shape[0] == 1:
                a = a[0]
            if a.ndim == 2:
                rank2.append(a)
                positions.append(pos)
            elif a.ndim == 3:
                rank3.append(a)
        if len(rank2) < 2 or not rank3:
            return self._split_failed("need two rank-2 tensors and one rank-3 tensor, got shapes %s" % (
                [tuple(np.asarray(o).shape) for o in model_output],))
        masks = rank3[0]
        four = [a for a in rank2 if a.shape[1] == 4]
        other = [a for a in rank2 if a.shape[1] != 4]
        if len(four) >= 2:
            boxes, logits, by_name = split_same_shape_pair(rank2, positions, self.output_names)
            if not by_name and not self._warned_order:
                LOG.warning(
                    "RF-DETR-Seg outputs hold two [Q, 4] tensors (a three-class model) and the manifest has no "
                    "detection.output_names naming dets/labels; taking the runner's order as [boxes, logits]"
                )
                self._warned_order = True
        elif len(four) == 1 and other:
            boxes, logits = four[0], other[0]
        else:
            return self._split_failed("no [Q, 4] boxes tensor among shapes %s" % ([a.shape for a in rank2],))
        if not (boxes.shape[0] == logits.shape[0] == masks.shape[0]):
            return self._split_failed("query count mismatch: boxes %s, logits %s, masks %s" % (
                boxes.shape, logits.shape, masks.shape))
        return boxes, logits, masks

    def _split_failed(self, reason: str) -> Tuple[None, None, None]:
        if not self._warned_split:
            LOG.warning("RF-DETR-Seg output cannot be decoded (%s); emitting an empty result", reason)
            self._warned_split = True
        return None, None, None

    def _select(self, boxes: np.ndarray, logits: np.ndarray, masks: np.ndarray,
                out_w: int, out_h: int) -> List[InstanceSegmentationResult]:
        """Flattened top-k selection as the detection decoder, then one mask per
        kept pair: sigmoid -> bilinear resize to the source frame -> threshold
        -> packbits. Returns the instances by confidence descending (<= top_k)."""
        num_queries, num_slots = logits.shape
        n_cls = num_slots
        if self.num_classes is not None:
            if self.num_classes > num_slots:
                if not self._warned_slots:
                    LOG.error(
                        "RF-DETR-Seg manifest declares num_classes=%d but the logits tensor has %d slots; "
                        "scoring %d slots", self.num_classes, num_slots, num_slots,
                    )
                    self._warned_slots = True
            else:
                n_cls = self.num_classes  # the trailing slot is the background slot
        probs = _sigmoid(logits[:, :n_cls])
        flat = probs.reshape(-1)  # (num_queries * n_cls,)
        if flat.size == 0:
            return []

        # Top-k over all query-class pairs (DETR set selection), then threshold.
        k = min(self.top_k, flat.size)
        top_idx = np.argpartition(flat, -k)[-k:]
        top_idx = top_idx[np.argsort(flat[top_idx])[::-1]]
        top_scores = flat[top_idx]

        keep = top_scores >= self.score_threshold
        top_idx = top_idx[keep]
        top_scores = top_scores[keep]
        if top_idx.size == 0:
            return []

        query_idx = top_idx // n_cls
        class_col = top_idx % n_cls
        xyxy = _cxcywh_to_xyxy(boxes[query_idx])  # normalized cxcywh -> xyxy

        results: List[InstanceSegmentationResult] = []
        for i in range(top_idx.size):
            packed, area = self._pack_mask(masks[int(query_idx[i])], out_w, out_h)
            if area == 0:
                continue  # a box without a mask
            results.append(self._make_result(
                xyxy[i], int(class_col[i]), float(top_scores[i]), packed, area, out_w, out_h))
        return results

    def _pack_mask(self, mask_logits: np.ndarray, out_w: int, out_h: int) -> Tuple[np.ndarray, int]:
        """sigmoid -> bilinear upsample to (out_h, out_w) -> > mask_threshold ->
        (packbits of the row-major mask, foreground pixel count). The dense
        arrays are locals and die here, before the next pair (DD3)."""
        prob = _sigmoid(np.asarray(mask_logits, dtype=np.float32))
        resized = cv2.resize(prob, (int(out_w), int(out_h)), interpolation=cv2.INTER_LINEAR)
        binary = resized > self.mask_threshold
        area = int(np.count_nonzero(binary))
        return np.packbits(binary.ravel()), area

    def _make_result(self, box_xyxy, cls_id, score, packed, area, out_w, out_h) -> InstanceSegmentationResult:
        x_min, y_min, x_max, y_max = (float(v) for v in box_xyxy)
        # RF-DETR boxes are normalized to [0, 1]: scale to the source frame
        # (fallback: the square network input), as the detection decoder does.
        sw, sh = float(out_w), float(out_h)
        label = (
            self.class_names[cls_id]
            if self.class_names and 0 <= cls_id < len(self.class_names)
            else str(cls_id)
        )
        return InstanceSegmentationResult(
            [x_min * sw, y_min * sh, x_max * sw, y_max * sh], label, score, self.score_threshold,
            cls_id, packed, (int(out_h), int(out_w)), area,
        )
