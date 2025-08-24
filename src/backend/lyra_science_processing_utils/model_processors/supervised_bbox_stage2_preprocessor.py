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
import cv2
import numpy as np
from typing import Dict, List 

from lyra_science_processing_utils.inference_preprocessor import InferencePreProcessor
from lyra_science_processing_utils.model_processors.basic_preprocessor import BasicPreProcessor

class SupervisedBBoxStage2PreProcessor(InferencePreProcessor):
    def __init__(self, config2: Dict):
        super().__init__(config2)
        self.pre_processor = BasicPreProcessor(config2)

    def __call__(self, input_image: np.ndarray, feature_tensors: List[np.ndarray], *args, **kwargs) -> np.ndarray:
        """
        Pre-process image to be used by stage2 model
        :param input_image: an image as numpy array with type uint8 and shape
                           (height, width, channels) or (height, width)
        :return: 
        """

        image_feat_tensor, ref_image_feat_tensor = feature_tensors

        # compute diff image 
        diff_map = np.abs(image_feat_tensor - ref_image_feat_tensor)
        diff_map = np.mean(diff_map, 0)
        diff_map = np.clip(diff_map, 0, 1.0)  # clamp to [0, 1]
        diff_map *= 255
        diff_map = cv2.cvtColor(np.uint8(diff_map), cv2.COLOR_GRAY2BGR)

        h, w, c = input_image.shape 
        diff_map = cv2.resize(diff_map, (w, h))

        # concat
        input_image = self.pre_processor(input_image)
        ref_image = self.pre_processor(diff_map)
        return np.concatenate([input_image, ref_image], axis=1)
