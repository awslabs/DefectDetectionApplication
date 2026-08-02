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
import base64
import cv2
import numpy as np
from typing import Dict, List, Optional

from lyra_science_processing_utils.utils.serializable import Serializable
from lyra_science_processing_utils.utils.object_detection_result import ObjectDetectionResult


class AnomalyResult(Serializable):
    """
    This class represents result of anomaly detection for one object.
    """

    # Which image encoding to use when serializing, this should be one of ['.exr', '.png', None].
    # Setting it to None will cause the mask to be excluded from serialization.
    mask_encoding: Optional[str] = '.exr'

    @property
    def mask(self):
        if self._mask is not None:
            return self._mask
        if self._mask_path is not None:
            return cv2.imread(self._mask_path, cv2.IMREAD_UNCHANGED)
        return None

    @mask.setter
    def mask(self, new_mask):
        self._mask = new_mask
        self._mask_path = None

    @property
    def mask_path(self):
        return self._mask_path

    def save_mask(self, mask_path: str):
        if AnomalyResult.mask_encoding is None or self.mask is None:
            return
        mask_bytes = self._encode_mask(self.mask)
        with open(mask_path, 'wb') as f:
            f.write(mask_bytes)
        self._mask_path = mask_path
        self._mask = None

    def __init__(self, score: float = None, mask: np.ndarray = None, confidence: float = None, label: str = None, bboxes: List = None):
        """
        :param score: The anomaly score, in range 0.0 to 1.0.
        :param mask: An optional mask, as np.ndarray, indicating anomalous areas in the image.
        :param confidence: confidence to make image-level decision
        :param label: label of the image
        :param bboxes: bounding boxes (for bbox detection model)
        """

        if score is None and mask is None:
            raise ValueError("At least one of score or mask should be defined")
        self.score = score
        self._mask = mask
        self._mask_path = None
        self.bboxes = bboxes
        self.confidence = confidence
        self.label = label

    def serialize(self) -> Dict:
        """
        Returns a version of this object that is JSON serializable
        """

        # Here we encode the mask as an image (to reduce space)
        # and encode it in base64 so it can be embedded in JSON
        if AnomalyResult.mask_encoding is None or self.mask is None:
            encoded_mask = None
        else:
            encoded_mask = base64.b64encode(self._encode_mask(self.mask)).decode('UTF-8')
        Serialized_bboxes = None if self.bboxes is None else [box.serialize() for box in self.bboxes]
        return {"score": self.score, "mask": encoded_mask, "bboxes": Serialized_bboxes, "confidence": self.confidence, "label": self.label}

    @classmethod
    def deserialize(cls, data: Dict):
        """
        Returns an instance of this object from the data given
        :param data: the data to populate the class
        """
        mask = None
        if data['mask'] is not None:
            mask = cv2.imdecode(np.asarray(bytearray(base64.b64decode(data['mask'])),
                                           dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        bboxes = None if 'bboxes' not in data or data['bboxes'] is None else [ObjectDetectionResult.deserialize(box) for box in data['bboxes']]
        confidence = data['confidence'] if 'confidence' in data else None
        label = data['label'] if 'label' in data else None
        return cls(data['score'], mask, confidence, label, bboxes)

    @staticmethod
    def _encode_mask(mask: np.ndarray) -> bytes:
        mask_to_encode = mask
        if AnomalyResult.mask_encoding != '.exr':
            mask_to_encode = mask.astype(np.uint8)
        _, encoded_mask_bytes = cv2.imencode(AnomalyResult.mask_encoding, mask_to_encode)
        return encoded_mask_bytes