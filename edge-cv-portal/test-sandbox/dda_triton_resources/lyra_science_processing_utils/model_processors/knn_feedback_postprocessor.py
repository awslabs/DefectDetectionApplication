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
import numpy as np
from typing import Dict, List, Optional

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.score_calibrator import SKLearnScoreCalibration


class KnnFeedbackPostProcessor(InferencePostProcessor):

    def __init__(self, config: Dict):
        super().__init__(config)
        self._score_calibrator = SKLearnScoreCalibration(max_val_score=1)
        self._score_calibrator.set_params([self.config['calibrator_scaling'], self.config['calibrator_shift']])

    def __call__(self, model_output: List[np.ndarray], *args, scores: Optional[List[float]] = None,
                 **kwargs) -> AnomalyResult:
        """
        Post-process model output
        :param model_output: output of inference model
        :return: inference result
        """
        neighbor_indices, distances, train_labels_gallery = model_output
        # scores has to have as many items as neighbor_indices
        if scores is None or len(scores) != len(neighbor_indices):
            raise ValueError(f'Need at least {len(neighbor_indices)} reference scores')
        # compute scores
        scores = self.compute_scores(self.config['T'], neighbor_indices, distances, scores, train_labels_gallery)
        normalized_scores = self._score_calibrator.get_calibrated_score(scores)
        # index 0 since predict method is fixed to batch size of 1
        return AnomalyResult(score=normalized_scores[0])

    @staticmethod
    def compute_scores(T, neighbor_indices, distances, ref_scores, train_labels_gallery):
        """
        Converts each test example's average distance to its k-nearest neighbors
            into a soft score between [0, 1].
        Args:
            distances: (B) list of average distances from test example i to its k-nearest neighbors
        Returns:
            scores: (B) list of soft scores for each test example i
        """
        scores = []

        # iterate through neighbor indices and distances
        for i, (indices, dists) in enumerate(zip(neighbor_indices, distances)):
            votes = [train_labels_gallery[idx] for idx in indices]
            ref_score = ref_scores[i]
            term = math.exp(-T * dists[0] * dists[0])
            if votes[0] == 0:
                scores.append(ref_score * (1 - term))
            else:
                scores.append(ref_score * (1 - term) + term)
        return scores
