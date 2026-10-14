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
#  #
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  #
#      http://www.apache.org/licenses/LICENSE-2.0
#  #
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Shared Class_Label_Map utilities for object-detection results.

This module provides the canonical COCO 80-class index -> name mapping used to
turn numeric detection class indices into human-readable labels, along with a
defensive resolver that never raises. It is imported by both the Base_Model
(``lfv_model_template.py``) and the Marshal (``marshal_for_capture_template.py``)
so that class-label resolution has a single source of truth.
"""
from typing import Dict, Optional, Union

# Canonical COCO 80-class detection labels. Indices are 0-based and contiguous
# (0..79), matching the class indices produced by YOLOv8 object-detection models.
COCO_CLASS_LABELS: Dict[int, str] = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    4: "airplane",
    5: "bus",
    6: "train",
    7: "truck",
    8: "boat",
    9: "traffic light",
    10: "fire hydrant",
    11: "stop sign",
    12: "parking meter",
    13: "bench",
    14: "bird",
    15: "cat",
    16: "dog",
    17: "horse",
    18: "sheep",
    19: "cow",
    20: "elephant",
    21: "bear",
    22: "zebra",
    23: "giraffe",
    24: "backpack",
    25: "umbrella",
    26: "handbag",
    27: "tie",
    28: "suitcase",
    29: "frisbee",
    30: "skis",
    31: "snowboard",
    32: "sports ball",
    33: "kite",
    34: "baseball bat",
    35: "baseball glove",
    36: "skateboard",
    37: "surfboard",
    38: "tennis racket",
    39: "bottle",
    40: "wine glass",
    41: "cup",
    42: "fork",
    43: "knife",
    44: "spoon",
    45: "bowl",
    46: "banana",
    47: "apple",
    48: "sandwich",
    49: "orange",
    50: "broccoli",
    51: "carrot",
    52: "hot dog",
    53: "pizza",
    54: "donut",
    55: "cake",
    56: "chair",
    57: "couch",
    58: "potted plant",
    59: "bed",
    60: "dining table",
    61: "toilet",
    62: "tv",
    63: "laptop",
    64: "mouse",
    65: "remote",
    66: "keyboard",
    67: "cell phone",
    68: "microwave",
    69: "oven",
    70: "toaster",
    71: "sink",
    72: "refrigerator",
    73: "book",
    74: "clock",
    75: "vase",
    76: "scissors",
    77: "teddy bear",
    78: "hair drier",
    79: "toothbrush",
}


def resolve_class_label(
    class_index: Union[int, str],
    class_map: Optional[Dict] = None,
) -> str:
    """Resolve a detection class index to a human-readable label.

    Returns the mapped label when the resolved map (the provided ``class_map``
    if given, otherwise the default :data:`COCO_CLASS_LABELS`) contains an entry
    for ``class_index``. Otherwise, falls back to the string form of the input.

    Both ``int`` and numeric-string indices are accepted: numeric strings are
    coerced to ``int`` for lookup so that, e.g., ``"17"`` resolves the same as
    ``17``. Provided maps may key on either ``int`` or ``str`` indices; both are
    checked. This function never raises: a non-numeric or missing index simply
    falls back to ``str(class_index)``.

    :param class_index: The numeric class identifier (``int`` or numeric-string).
    :param class_map: Optional index -> label mapping; defaults to COCO labels.
    :return: The human-readable label, or the class index rendered as a string.
    """
    resolved_map = class_map if class_map is not None else COCO_CLASS_LABELS

    # Attempt a direct lookup on the raw index first (supports maps keyed by the
    # original type, including non-numeric string keys).
    try:
        if class_index in resolved_map:
            return str(resolved_map[class_index])
    except TypeError:
        # Unhashable index type; fall through to the string fallback below.
        return str(class_index)

    # Coerce numeric-string indices to int and retry both int and str keys.
    int_index: Optional[int] = None
    if isinstance(class_index, int):
        int_index = class_index
    elif isinstance(class_index, str):
        try:
            int_index = int(class_index.strip())
        except (ValueError, TypeError):
            int_index = None

    if int_index is not None:
        if int_index in resolved_map:
            return str(resolved_map[int_index])
        str_index = str(int_index)
        if str_index in resolved_map:
            return str(resolved_map[str_index])

    return str(class_index)
