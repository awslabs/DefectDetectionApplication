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
"""Post-processor that decodes a raw RF-DETR **instance-segmentation** ONNX
output into a single semantic **index mask**, so it renders through the existing
anomaly-localization path (colored mask + toggleable overlay + per-class color
map) with no changes to the Triton base/marshal contract.

RF-DETR seg emits three tensors (order/name unreliable — the reference export
even mislabels them, so we identify strictly by shape):

  * boxes:  ``[1, Q, 4]``           — normalized cxcywh (unused for the mask)
  * logits: ``[1, Q, num_classes]`` — per-query class logits (sigmoid scoring)
  * masks:  ``[1, Q, Hm, Wm]``      — per-query low-res mask logits

Decode:
  1. ``scores = sigmoid(logits)``; per query take the best class + its score.
  2. keep queries with ``score >= score_threshold``.
  3. composite the kept instance masks into one HxW **index mask** (0 =
     background). Each kept mask is ``sigmoid(mask) > mask_threshold``, resized
     (nearest) to the source image size; instances are painted in ascending
     score order so higher-confidence instances win on overlap. The painted
     value is ``class_id + 1`` (index 0 reserved for background), matching the
     palette/`pixel_level_classes` convention (names[0] = background).

Returns an ``AnomalyResult(score=top_score, mask=index_mask)``. The model graph
wraps it and the base model's ``__build_anomaly_tensors`` renders it exactly
like a semantic-segmentation anomaly model. Uses numpy + cv2 (already a science
-lib dependency); no torch on the hot path.
"""
import logging
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult

LOG = logging.getLogger(__name__)

DEFAULT_SCORE_THRESHOLD = 0.5
DEFAULT_MASK_THRESHOLD = 0.5
DEFAULT_NETWORK_INPUT = 312  # RF-DETR-seg-nano default square input


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return np.where(x >= 0, 1.0 / (1.0 + np.exp(-x)), np.exp(x) / (1.0 + np.exp(x)))


class RfDetrSemanticSegmentationPostProcessor(InferencePostProcessor):
    """Decode a raw RF-DETR instance-seg output into a semantic index mask
    wrapped in an AnomalyResult."""

    def __init__(self, config: Dict, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        detection = config.get("detection", {}) if isinstance(config, dict) else {}
        self.score_threshold = float(detection.get("score_threshold", DEFAULT_SCORE_THRESHOLD))
        self.mask_threshold = float(detection.get("mask_threshold", DEFAULT_MASK_THRESHOLD))
        self.num_classes = detection.get("num_classes")
        self.network_input = int(
            detection.get("network_input")
            or config.get("image_width")
            or DEFAULT_NETWORK_INPUT
        )

    def __call__(self, model_output: List[np.ndarray], *args, **kwargs) -> AnomalyResult:
        src_img_size = kwargs.get("src_img_size")  # (width, height) of source image
        if src_img_size:
            out_w, out_h = int(src_img_size[0]), int(src_img_size[1])
        else:
            out_w = out_h = self.network_input

        logits, masks = self._split_outputs(model_output)
        if logits is None or masks is None:
            # Nothing to decode -> all-background mask.
            return AnomalyResult(score=0.0, mask=np.zeros((out_h, out_w), dtype=np.uint8))

        scores = _sigmoid(logits)                 # (Q, C)
        best_cls = np.argmax(scores, axis=1)      # (Q,)
        best_score = np.max(scores, axis=1)       # (Q,)
        keep = np.where(best_score >= self.score_threshold)[0]

        index_mask = np.zeros((out_h, out_w), dtype=np.uint8)
        if keep.size == 0:
            return AnomalyResult(score=0.0, mask=index_mask)

        # Paint ascending by score so higher-confidence instances win overlaps.
        keep = keep[np.argsort(best_score[keep])]
        # Cap the class id so the painted index (class_id + 1) never exceeds the
        # configured class count — keeps it within pixel_level_classes.names and
        # the palette (index 0 = background is reserved).
        max_cls = (int(self.num_classes) - 1) if self.num_classes else None
        for q in keep:
            m = masks[q]
            prob = _sigmoid(m)
            binary = (prob > self.mask_threshold).astype(np.uint8)
            if binary.shape != (out_h, out_w):
                binary = cv2.resize(binary, (out_w, out_h), interpolation=cv2.INTER_NEAREST)
            cls_id = int(best_cls[q])
            if max_cls is not None:
                cls_id = min(cls_id, max_cls)
            # class_id + 1 (reserve 0 for background).
            index_mask[binary.astype(bool)] = np.uint8(cls_id + 1)

        top_score = float(np.max(best_score[keep]))
        return AnomalyResult(score=top_score, mask=index_mask)

    # ── helpers ────────────────────────────────────────────────────────────
    def _split_outputs(
        self, model_output: List[np.ndarray]
    ) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """Identify (logits[Q,C], masks[Q,Hm,Wm]) from the runner output list by
        shape (names are unreliable). The mask tensor is the 4-D (batch,Q,Hm,Wm)
        / 3-D (Q,Hm,Wm) one; logits is the 2-D (Q,C) tensor whose last dim != 4;
        boxes ([Q,4]) is ignored. Batch dim (leading 1) is squeezed."""
        if not isinstance(model_output, (list, tuple)) or len(model_output) < 3:
            return None, None
        two_d, mask_arr = [], None
        for o in model_output:
            a = np.asarray(o)
            # Squeeze a single leading batch dim so boxes->(Q,4), logits->(Q,C),
            # masks->(Q,Hm,Wm).
            if a.ndim >= 3 and a.shape[0] == 1:
                a = a[0]
            if a.ndim == 3:
                mask_arr = a  # (Q, Hm, Wm)
            elif a.ndim == 2 and a.shape[1] == 4:
                continue  # boxes — ignored for segmentation
            elif a.ndim == 2:
                two_d.append(a)  # logits candidate (Q, C)
        logits = None
        if two_d:
            if self.num_classes:
                for a in two_d:
                    if a.shape[1] == int(self.num_classes):
                        logits = a
                        break
            logits = logits if logits is not None else two_d[0]
        if logits is None or mask_arr is None:
            return None, None
        if mask_arr.shape[0] != logits.shape[0]:
            n = min(mask_arr.shape[0], logits.shape[0])
            mask_arr, logits = mask_arr[:n], logits[:n]
        return logits, mask_arr
