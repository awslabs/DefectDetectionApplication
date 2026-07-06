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
"""Post-processor that decodes a raw YOLO (v8-style) detection tensor into a
list of ``ObjectDetectionResult``.

This is the concrete §7 "output-contract adapter" for bring-your-own ONNX
object-detection models (see docs/multi-runtime-inference.md, "Object-detection
task type"). A YOLOv8 ONNX export emits a single raw tensor of shape
``[1, 4 + num_classes, num_anchors]`` (e.g. ``[1, 84, 8400]`` for 80 COCO
classes) — 4 box coordinates (cx, cy, w, h, in network-input pixels) plus one
score per class for every anchor. There is no baked-in NMS or thresholding, so
this decoder performs:

  1. transpose to per-anchor rows (also tolerates the already-transposed
     ``[1, num_anchors, 4 + num_classes]`` layout),
  2. per-anchor class score -> (best class, confidence),
  3. confidence thresholding,
  4. xywh(center) -> xyxy,
  5. class-wise Non-Max Suppression,
  6. scale from the network input size back to the source image size.

Pure numpy: no torch on the inference hot path. Returns
``list[ObjectDetectionResult]`` which ``SingleStageModelGraph`` already wraps
into ``InferenceData``.
"""
import logging
from typing import Dict, List

import numpy as np

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.utils.object_detection_result import ObjectDetectionResult

LOG = logging.getLogger(__name__)

DEFAULT_SCORE_THRESHOLD = 0.25
DEFAULT_IOU_THRESHOLD = 0.45
DEFAULT_NETWORK_INPUT = 640


def _xywh_to_xyxy(boxes: np.ndarray) -> np.ndarray:
    """Convert [cx, cy, w, h] rows to [x_min, y_min, x_max, y_max]."""
    xyxy = np.empty_like(boxes)
    half_w = boxes[:, 2] / 2.0
    half_h = boxes[:, 3] / 2.0
    xyxy[:, 0] = boxes[:, 0] - half_w
    xyxy[:, 1] = boxes[:, 1] - half_h
    xyxy[:, 2] = boxes[:, 0] + half_w
    xyxy[:, 3] = boxes[:, 1] + half_h
    return xyxy


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> List[int]:
    """Greedy Non-Max Suppression on xyxy boxes. Returns kept indices.

    Pure-numpy implementation (no torchvision/cv2 dependency on the hot path).
    """
    if boxes.shape[0] == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]

    keep: List[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[i] + areas[rest] - inter
        iou = np.where(union > 0, inter / union, 0.0)
        order = rest[iou <= iou_threshold]
    return keep


class YoloDetectionPostProcessor(InferencePostProcessor):
    """Decode a raw YOLO detection tensor into ObjectDetectionResult list."""

    def __init__(self, config: Dict, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        detection = config.get("detection", {}) if isinstance(config, dict) else {}
        self.score_threshold = float(
            detection.get("score_threshold", DEFAULT_SCORE_THRESHOLD)
        )
        self.iou_threshold = float(
            detection.get("iou_threshold", DEFAULT_IOU_THRESHOLD)
        )
        self.class_names = detection.get("class_names") or None
        # Network input size (square) the model was exported with. Used to scale
        # detections back to the source image. Falls back to image_width/height
        # from the stage config, then the YOLO default of 640.
        self.network_input = int(
            detection.get("network_input")
            or config.get("image_width")
            or DEFAULT_NETWORK_INPUT
        )

    def __call__(self, model_output: List[np.ndarray], *args, **kwargs) -> List[ObjectDetectionResult]:
        src_img_size = kwargs.get("src_img_size")  # (width, height) of source image
        raw = self._select_output(model_output)
        if raw is None:
            return []

        preds = self._to_anchor_rows(raw)  # shape (num_anchors, 4 + num_classes)
        if preds.size == 0 or preds.shape[1] < 5:
            LOG.warning("YOLO decode: unusable output shape %s", getattr(preds, "shape", None))
            return []

        boxes_xywh = preds[:, :4]
        class_scores = preds[:, 4:]
        class_ids = class_scores.argmax(axis=1)
        confidences = class_scores[np.arange(class_scores.shape[0]), class_ids]

        # Diagnostic: raw output shape + score distribution. An all-low max
        # confidence here (with a valid COCO image) points at the execution
        # provider mis-running the graph (e.g. TensorRT on YOLOv8 decode ops)
        # rather than the decode logic, which is exercised by unit tests.
        LOG.info(
            "YOLO decode: raw=%s rows=%s classes=%s max_conf=%.4f thr=%.2f kept=%d",
            getattr(np.asarray(raw), "shape", None),
            preds.shape[0],
            class_scores.shape[1],
            float(confidences.max()) if confidences.size else -1.0,
            self.score_threshold,
            int(np.count_nonzero(confidences >= self.score_threshold)),
        )

        # Threshold.
        keep_mask = confidences >= self.score_threshold
        if not np.any(keep_mask):
            return []
        boxes_xywh = boxes_xywh[keep_mask]
        confidences = confidences[keep_mask]
        class_ids = class_ids[keep_mask]

        boxes_xyxy = _xywh_to_xyxy(boxes_xywh)

        # Class-wise NMS.
        results: List[ObjectDetectionResult] = []
        for cls in np.unique(class_ids):
            cls_mask = class_ids == cls
            cls_boxes = boxes_xyxy[cls_mask]
            cls_scores = confidences[cls_mask]
            kept = _nms(cls_boxes, cls_scores, self.iou_threshold)
            for k in kept:
                box = cls_boxes[k]
                results.append(
                    self._make_result(box, int(cls), float(cls_scores[k]), src_img_size)
                )
        return results

    # ── helpers ────────────────────────────────────────────────────────────
    @staticmethod
    def _select_output(model_output: List[np.ndarray]) -> np.ndarray:
        """Pick the raw detection tensor from the runner's output list."""
        if model_output is None:
            return None
        if isinstance(model_output, np.ndarray):
            return model_output
        if isinstance(model_output, (list, tuple)) and len(model_output) > 0:
            return model_output[0]
        return None

    @staticmethod
    def _to_anchor_rows(raw: np.ndarray) -> np.ndarray:
        """Normalize a YOLO output to shape (num_anchors, 4 + num_classes).

        Handles:
          - [1, C, N] (YOLOv8 native, C = 4 + num_classes) -> transpose to (N, C)
          - [C, N]                                          -> transpose to (N, C)
          - [1, N, C] / [N, C] (already per-anchor)         -> as-is
        Heuristic: the channel axis (4 + num_classes) is the smaller of the two
        non-batch dims for realistic models (classes << anchors).
        """
        arr = np.asarray(raw)
        if arr.ndim == 3:
            # drop the batch dim
            arr = arr[0]
        if arr.ndim != 2:
            return np.empty((0, 0))
        rows, cols = arr.shape
        # If rows looks like the channel axis (small) and cols like anchors
        # (large), transpose so anchors are the row axis.
        if rows < cols:
            arr = arr.T
        return arr

    def _make_result(self, box_xyxy, cls_id, score, src_img_size) -> ObjectDetectionResult:
        x_min, y_min, x_max, y_max = (float(v) for v in box_xyxy)
        # Scale from network input space back to the source image, if known.
        if src_img_size and self.network_input:
            sw, sh = float(src_img_size[0]), float(src_img_size[1])
            sx = sw / float(self.network_input)
            sy = sh / float(self.network_input)
            x_min *= sx
            x_max *= sx
            y_min *= sy
            y_max *= sy
        label = (
            self.class_names[cls_id]
            if self.class_names and 0 <= cls_id < len(self.class_names)
            else str(cls_id)
        )
        return ObjectDetectionResult(
            [x_min, y_min, x_max, y_max], label, score, self.score_threshold
        )
