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
from typing import Dict, List

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
