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
import numpy as np
from typing import Dict, List
import cv2

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.utils import convert_mask_to_int32
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.score_calibrator import SKLearnScoreCalibration


class ImagestatsPostProcessor(InferencePostProcessor):

    def __init__(self, config: Dict):
        super().__init__(config)
        self._score_calibrator = SKLearnScoreCalibration(self.config['max_val_score'])
        self._score_calibrator.set_params([self.config['calibrator_scaling'], self.config['calibrator_shift']])

    def __call__(self, model_output: List[np.ndarray], *args, output_features=False, **kwargs):
        """
        Post-process model output
        :param model_output: output of inference model
        :return: inference result
        """
        masks, scores, image_features = model_output
        normalized_scores = self._score_calibrator.get_calibrated_score(scores)
        if 'pixel_level_classes' in self.config and len(self.config['pixel_level_classes']['normal_ids']) > 0:
            # We already thresholded using 3 sigma rule and applied a sigmoid in imagesats.
            # So a threshold of 0.5 should be applied to binarize the mask
            if len(masks.shape) == 4:
                masks = masks[0][0]
            elif len(masks.shape) == 3:
                masks = masks[0]
            if "raw_image_shape" in self.config:
                masks = cv2.resize(masks, tuple(self.config["raw_image_shape"][::-1]), interpolation=cv2.INTER_AREA)
            masks = convert_mask_to_int32(masks, threshold=0.5)
        else:
            masks = None

        if output_features:
            return normalized_scores, image_features, masks
        # index 0 since predict method is fixed to batch size of 1
        # mask is single-channel float32 with values between 0.0 and 1.0
        return AnomalyResult(score=normalized_scores[0], mask=masks)
