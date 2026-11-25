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
from typing import Dict, List, Optional, Tuple

import numpy as np

from lyra_science_processing_utils.utils import mask_rle
from lyra_science_processing_utils.utils.serializable import Serializable


class ObjectDetectionResult(Serializable):
    """
    This class represents result of object detection for one object.
    """

    def __init__(self, bounding_box: List, obj_class: str = 'default', confidence: float = 1.0, threshold: float = 0.5):
        """
        :param bounding_box: A 4-element list consisting of bbox attributes [x_min, y_min, x_max, y_max]
        :param obj_class: Label of the class this object belongs to.
        :param confidence: Detection confidence score, between 0.0 and 1.0
        :param confidence_threshold: Confidence threshold, between 0.0 and 1.0,
                                     above which to materialize bbox predictions
        """
        self.bounding_box = bounding_box
        self.obj_class = obj_class
        self.confidence = confidence
        self.confidence_threshold = threshold

    def serialize(self) -> Dict:
        """
        Returns a version of this object that is JSON serializable
        """
        return {"bounding_box": self.bounding_box, "class": self.obj_class, "confidence": self.confidence,
                "confidence_threshold": self.confidence_threshold}

    @classmethod
    def deserialize(cls, data: Dict):
        """
        Returns an instance of this object from the data given
        :param data: the data to populate the class
        """
        return cls(data['bounding_box'], data['class'], data['confidence'], data['confidence_threshold'])


class InstanceSegmentationResult(ObjectDetectionResult):
    """
    This class represents one instance of an instance-segmentation result: the
    detection fields plus the integer class index and the binary mask at source
    resolution (rfdetr-seg-support DD3, DD5, DD11; Requirements 9.5, 10.1).

    The mask is held bit-packed (``numpy.packbits`` of the row-major boolean
    mask, 1 bit per pixel) so the dense array never outlives the decode loop;
    ``mask()`` unpacks it on demand. ``serialize()`` adds ``class_index``, the
    Canonical_RLE ``mask_rle`` and ``mask_area`` to the detection dict.
    ``obj_class`` keeps the detection decoder's meaning (the class label when
    ``class_names`` is configured, else the index string).
    """

    def __init__(self, bounding_box: List, obj_class: str, confidence: float, threshold: float,
                 class_index: int, mask_packed: np.ndarray, mask_shape: Tuple[int, int], mask_area: int):
        """
        :param bounding_box: A 4-element list consisting of bbox attributes [x_min, y_min, x_max, y_max]
        :param obj_class: Label of the class this object belongs to.
        :param confidence: Detection confidence score, between 0.0 and 1.0
        :param threshold: Confidence threshold, between 0.0 and 1.0
        :param class_index: Integer class index (the Composite_Mask paints palette row class_index + 1)
        :param mask_packed: ``numpy.packbits`` of the row-major boolean mask
        :param mask_shape: (height, width) of the mask, in source-frame pixels
        :param mask_area: Number of foreground pixels in the mask
        """
        super().__init__(bounding_box, obj_class, confidence, threshold)
        self.class_index = int(class_index)
        self.mask_packed = mask_packed
        self.mask_shape = (int(mask_shape[0]), int(mask_shape[1]))
        self.mask_area = int(mask_area)

    def mask(self) -> np.ndarray:
        """
        Returns the dense boolean mask of shape ``mask_shape``, unpacked on demand
        """
        height, width = self.mask_shape
        return np.unpackbits(self.mask_packed, count=height * width).reshape(height, width).astype(bool)

    def serialize(self) -> Dict:
        """
        Returns a version of this object that is JSON serializable: the detection
        dict plus ``class_index``, ``mask_rle`` (Canonical_RLE) and ``mask_area``
        """
        data = super().serialize()
        data["class_index"] = self.class_index
        data["mask_rle"] = mask_rle.encode(self.mask())
        data["mask_area"] = self.mask_area
        return data

    @classmethod
    def deserialize(cls, data: Dict, mask_shape: Optional[Tuple[int, int]] = None):
        """
        Returns an instance of this object from the data given
        :param data: the data to populate the class (``mask_rle`` as Canonical_RLE)
        :param mask_shape: (height, width) the mask was encoded at; a serialized
                           entry does not carry it, so the caller supplies it
        """
        if mask_shape is None:
            raise ValueError("mask_shape (height, width) is required to decode mask_rle")
        height, width = int(mask_shape[0]), int(mask_shape[1])
        mask = mask_rle.decode(data['mask_rle'], width, height)
        return cls(data['bounding_box'], data['class'], data['confidence'], data['confidence_threshold'],
                   data['class_index'], np.packbits(mask.ravel()), (height, width), int(mask.sum()))
