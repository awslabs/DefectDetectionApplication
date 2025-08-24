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
import math
import os 
import numpy as np
import cv2
from typing import Dict, List
import dill

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.utils import get_pmax_confidence
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.score_calibrator import SKLearnScoreCalibration
from lyra_science_processing_utils.model_processors.basic_preprocessor import BasicPreProcessor
from lyra_science_processing_utils.utils import load_image_from_file_as_numpy_uint8
from lyra_science_processing_utils.model_processors.distance_based_classifier_postprocessor import DistanceBasedClassifierPostProcessor
from lyra_science_processing_utils.utils.image_alignment import get_affine_aligned_image

class SupervisedBBoxStage1PostProcessor(InferencePostProcessor):
    # adapted from distance_based_classifier_postprocessor.py

    def __init__(self, config1: Dict, config2: Dict):
        super().__init__(config1)
        self.reference_image_dir = config1['reference_image_dir']
        reference_image_map_file = config1['reference_image_map_file']
        self.affine_align = config1['affine_align']

        with open(reference_image_map_file, 'rb') as handle:
            data = dill.load(handle)
        image_index = data['image_index']
        train_feature_gallery = []
        self.reference_image_paths = []
        for path, feature in image_index.items():
            train_feature_gallery.append(feature)
            self.reference_image_paths.append(path)
        self.train_feature_gallery = np.vstack(train_feature_gallery)
        self.pre_processor = BasicPreProcessor(config2)
        
    def __call__(self, input_image: np.ndarray, model_output: List[np.ndarray], *args, **kwargs):
        """
        Post-process model output
        :param model_output: output of inference model
        :return: inference result
        """
        pixel_features = None
        if len(model_output) > 1:
            image_features, pixel_features = model_output
        else: 
            image_features = model_output[0]

        # compute NNs
        neighbor_indices, distances = DistanceBasedClassifierPostProcessor.get_nearest_neighbors(1, image_features, self.train_feature_gallery)
        ref_image_path = self.reference_image_paths[neighbor_indices[0][0]]
        image_name = os.path.basename(ref_image_path)
        ref_image_path = os.path.join(self.reference_image_dir, image_name)
        ref_image = load_image_from_file_as_numpy_uint8(ref_image_path)
        if len(ref_image.shape)<3:
            ref_image = cv2.cvtColor(ref_image, cv2.COLOR_GRAY2BGR)

        if not self.affine_align:
            input_image = self.pre_processor(input_image)
            ref_image = self.pre_processor(ref_image)
            diff_image = input_image - ref_image 

            return np.concatenate([input_image, diff_image], axis=1)

        h, w, c = ref_image.shape
        resized_input_image = cv2.resize(input_image, (w, h))
        aligned_ref_image = get_affine_aligned_image(ref_image, resized_input_image)
        return pixel_features, resized_input_image, aligned_ref_image 
