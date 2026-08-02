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

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.score_calibrator import SKLearnScoreCalibration


class SVDDPostProcessor(InferencePostProcessor):

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
        scores, image_features = model_output
        normalized_scores = self._score_calibrator.get_calibrated_score(scores)
        if output_features:
            return normalized_scores, image_features, None
        # index 0 since predict method is fixed to batch size of 1
        return AnomalyResult(score=normalized_scores[0])
