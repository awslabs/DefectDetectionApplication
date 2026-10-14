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
"""Post-processor that decodes a raw RF-DETR (DETR-family) detection output into
a list of ``ObjectDetectionResult``.

RF-DETR (Roboflow's real-time Detection Transformer, LW-DETR / Deformable-DETR
lineage) is **NMS-free** and emits a fixed set of query predictions as **two**
tensors (order-independent here — identified by shape):

  * boxes:  ``[1, num_queries, 4]`` — normalized ``cxcywh`` in [0, 1]
  * logits: ``[1, num_queries, num_classes]`` — per-query class logits

Decode (matches the Deformable-DETR / RF-DETR convention):
  1. ``scores = sigmoid(logits)`` (RF-DETR uses focal/sigmoid scoring, not a
     softmax over a background class). Optional ``use_softmax`` supports classic
     DETR, dropping an optional trailing no-object class.
  2. flatten (query x class), take the top-k highest scores (DETR selects a set
     rather than thresholding per anchor), then keep those above
     ``score_threshold``. box index = k // num_classes, label = k % num_classes.
     (A single query may thus yield more than one class — the standard behavior.)
  3. normalized ``cxcywh`` -> ``xyxy`` scaled to the source image.

No NMS (DETR is set-based). Pure numpy — no torch on the hot path. Returns
``list[ObjectDetectionResult]``, the same contract as the YOLO decoder, so the
model graph, GStreamer pipeline, and workflow layers are architecture-agnostic.
"""
import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.utils.object_detection_result import ObjectDetectionResult

LOG = logging.getLogger(__name__)

DEFAULT_SCORE_THRESHOLD = 0.5
DEFAULT_TOP_K = 300
DEFAULT_NETWORK_INPUT = 560  # RF-DETR default square input (nano/small/medium)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    # Numerically stable logistic sigmoid.
    return np.where(x >= 0, 1.0 / (1.0 + np.exp(-x)), np.exp(x) / (1.0 + np.exp(x)))


def _softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    z = x - np.max(x, axis=axis, keepdims=True)
    e = np.exp(z)
    return e / np.sum(e, axis=axis, keepdims=True)


def _cxcywh_to_xyxy(boxes: np.ndarray) -> np.ndarray:
    """Convert [cx, cy, w, h] rows to [x_min, y_min, x_max, y_max]."""
    xyxy = np.empty_like(boxes)
    half_w = boxes[:, 2] / 2.0
    half_h = boxes[:, 3] / 2.0
    xyxy[:, 0] = boxes[:, 0] - half_w
    xyxy[:, 1] = boxes[:, 1] - half_h
    xyxy[:, 2] = boxes[:, 0] + half_w
    xyxy[:, 3] = boxes[:, 1] + half_h
    return xyxy


class RfDetrDetectionPostProcessor(InferencePostProcessor):
    """Decode a raw RF-DETR (DETR-family) detection output into
    ObjectDetectionResult list."""

    def __init__(self, config: Dict, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        detection = config.get("detection", {}) if isinstance(config, dict) else {}
        self.score_threshold = float(
            detection.get("score_threshold", DEFAULT_SCORE_THRESHOLD)
        )
        self.top_k = int(detection.get("top_k", DEFAULT_TOP_K))
        self.class_names = detection.get("class_names") or None
        self.num_classes = detection.get("num_classes")
        # RF-DETR/Deformable-DETR use sigmoid scoring. Classic DETR uses softmax
        # with a trailing no-object class — enable via use_softmax.
        self.use_softmax = bool(detection.get("use_softmax", False))
        # Optional index of a background/no-object class to drop (softmax DETR).
        self.background_class = detection.get("background_class")
        self.network_input = int(
            detection.get("network_input")
            or config.get("image_width")
            or DEFAULT_NETWORK_INPUT
        )

    def __call__(self, model_output: List[np.ndarray], *args, **kwargs) -> List[ObjectDetectionResult]:
        src_img_size = kwargs.get("src_img_size")  # (width, height) of source image
        boxes, logits = self._split_outputs(model_output)
        if boxes is None or logits is None:
            return []

        num_queries, num_classes = logits.shape
        if self.use_softmax:
            probs = _softmax(logits, axis=-1)
            # Drop the no-object column if configured (classic DETR: last class).
            if self.background_class is not None:
                bg = int(self.background_class)
                keep_cols = [c for c in range(num_classes) if c != bg]
                probs = probs[:, keep_cols]
                col_to_label = keep_cols
            else:
                col_to_label = list(range(num_classes))
        else:
            probs = _sigmoid(logits)
            col_to_label = list(range(num_classes))

        n_cls = probs.shape[1]
        flat = probs.reshape(-1)  # (num_queries * n_cls,)
        if flat.size == 0:
            return []

        # Top-k over all query-class pairs (DETR set selection), then threshold.
        k = min(self.top_k, flat.size)
        # argpartition for the k largest, then sort those descending.
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

        sel_boxes = boxes[query_idx]  # normalized cxcywh
        xyxy = _cxcywh_to_xyxy(sel_boxes)

        results: List[ObjectDetectionResult] = []
        for i in range(xyxy.shape[0]):
            cls_id = int(col_to_label[int(class_col[i])])
            results.append(
                self._make_result(xyxy[i], cls_id, float(top_scores[i]), src_img_size)
            )
        return results

    # ── helpers ────────────────────────────────────────────────────────────
    def _split_outputs(
        self, model_output: List[np.ndarray]
    ) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """Identify the (boxes, logits) tensors from the runner output list by
        shape: the boxes tensor's last dim is 4; the other is logits. Batch dim
        (leading 1) is squeezed. Returns (boxes[Q,4], logits[Q,C]) or (None,None).
        """
        if not isinstance(model_output, (list, tuple)) or len(model_output) < 2:
            return None, None
        arrs = []
        for o in model_output:
            a = np.asarray(o)
            if a.ndim == 3 and a.shape[0] == 1:
                a = a[0]
            if a.ndim == 2:
                arrs.append(a)
        if len(arrs) < 2:
            return None, None

        boxes = logits = None
        for a in arrs:
            if a.shape[1] == 4 and boxes is None:
                boxes = a
            else:
                logits = a
        # Disambiguate the pathological case where num_classes == 4 using the
        # configured num_classes if available.
        if (boxes is None or logits is None) and self.num_classes:
            for a in arrs:
                if a.shape[1] == self.num_classes:
                    logits = a
                elif a.shape[1] == 4:
                    boxes = a
        if boxes is None or logits is None:
            return None, None
        if boxes.shape[0] != logits.shape[0]:
            LOG.warning(
                "RF-DETR boxes/logits query count mismatch: %s vs %s",
                boxes.shape, logits.shape,
            )
            n = min(boxes.shape[0], logits.shape[0])
            boxes, logits = boxes[:n], logits[:n]
        return boxes, logits

    def _make_result(self, box_xyxy, cls_id, score, src_img_size) -> ObjectDetectionResult:
        x_min, y_min, x_max, y_max = (float(v) for v in box_xyxy)
        # RF-DETR boxes are normalized to [0, 1]. Scale to the source image when
        # its size is known; otherwise fall back to the (square) network input.
        if src_img_size:
            sw, sh = float(src_img_size[0]), float(src_img_size[1])
        else:
            sw = sh = float(self.network_input)
        x_min *= sw
        x_max *= sw
        y_min *= sh
        y_max *= sh
        label = (
            self.class_names[cls_id]
            if self.class_names and 0 <= cls_id < len(self.class_names)
            else str(cls_id)
        )
        return ObjectDetectionResult(
            [x_min, y_min, x_max, y_max], label, score, self.score_threshold
        )
