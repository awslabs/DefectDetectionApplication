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
from typing import Dict, List, Optional

from lyra_science_processing_utils.utils.alignment_result import AlignmentResult
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.object_detection_result import ObjectDetectionResult
from lyra_science_processing_utils.utils.serializable import Serializable


class SingleObjectInferenceData(Serializable):
    """
    This class represents the data of an individual object in the sample image.
    It encapsulates an ObjectDetectionResult and optionally also an AlignmentResult
    and an AnomalyResult.
    """

    def __init__(self, object_detection_result: ObjectDetectionResult = None, alignment_result: AlignmentResult = None,
                 anomaly_result: AnomalyResult = None):
        """
        :param object_detection_result:
        :param alignment_result:
        :param anomaly_result:
        """
        self.object_detection: Optional[ObjectDetectionResult] = object_detection_result
        self.alignment: Optional[AlignmentResult] = alignment_result
        self.anomaly: Optional[AnomalyResult] = anomaly_result

    def serialize(self) -> Dict:
        """
        Returns a version of this object that is JSON serializable
        """
        object_detection = self.object_detection.serialize() if self.object_detection is not None else None
        alignment = self.alignment.serialize() if self.alignment is not None else None
        anomaly = self.anomaly.serialize() if self.anomaly is not None else None
        return {"object_detection": object_detection, "alignment": alignment, "anomaly": anomaly}

    @classmethod
    def deserialize(cls, data: Dict):
        """
        Returns an instance of this object from the data given
        :param data: the data to populate the class
        """
        object_detection = None if data['object_detection'] is None else ObjectDetectionResult.deserialize(
            data['object_detection'])
        alignment = None if data['alignment'] is None else AlignmentResult.deserialize(data['alignment'])
        anomaly = None if data['anomaly'] is None else AnomalyResult.deserialize(data['anomaly'])
        return cls(object_detection, alignment, anomaly)


class InferenceData(Serializable):
    """
    This class defines the data used as input/ouput between Lyra models during inference.
    It contains the image to be inferred on, optionally a global AlignmentResult, and a list
    of ObjectInference.
    """

    def __init__(self, image_path: Optional[str] = None, objects: List[SingleObjectInferenceData] = []):
        """
        :param image_path: original path to the Image that is been inferenced
        :param objects: list of per object inference data
        """
        self.image_path = image_path
        self.objects = objects

    def serialize(self) -> Dict:
        """
        Returns a version of this object that is JSON serializable
        """
        return {"image_path": self.image_path, "objects": [x.serialize() for x in self.objects]}

    @classmethod
    def deserialize(cls, data: Dict):
        """
        Returns an instance of this object from the data given
        :param data: the data to populate the class
        """
        return cls(data['image_path'], [SingleObjectInferenceData.deserialize(x) for x in data['objects']])
