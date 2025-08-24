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
from typing import Dict, List

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.score_calibrator import SKLearnScoreCalibration


class DistanceBasedClassifierPostProcessor(InferencePostProcessor):

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
        features, train_feature_gallery = model_output
        # compute NNs
        neighbor_indices, distances = self.get_nearest_neighbors(self.config['num_neighbors'], features,
                                                                 train_feature_gallery)
        # compute scores
        scores = self.compute_scores(self.config['T_0'], neighbor_indices, distances)
        normalized_scores = self._score_calibrator.get_calibrated_score(scores)
        if output_features:
            return normalized_scores, features, None
        # index 0 since predict method is fixed to batch size of 1
        return AnomalyResult(score=normalized_scores[0])

    @staticmethod
    def get_nearest_neighbors(num_neighbors, test_features, train_feature_gallery):
        """
        Given a batch of test features, computes the nearest neighbors from the reference images
            using their image-level features and Euclidean (L2) distance metric.
        Args:
            test_features: (B, D) or (D) tensor of test features
        Returns:
            neighbor_indices: (B, k) indices of nearest neighbors from the training set
            sorted_dists: (B, k) list of distances to nearest neighbors
        """
        if len(test_features.shape) == 1:
            test_features = test_features[None, :]

        """
        Dimensions:
        # image_feature_gallery - (# training samples, d)
        # test_features - (b, d)
        # distance - (b, # training samples)
        # sorted_indices - (b, # training samples)
        """

        # compute + sort by distances to train features
        distance = np.sum((train_feature_gallery - test_features[:, None, :]) ** 2, axis=-1)
        if len(distance.shape) == 1:
            distance = distance[None, :]
        sorted_indices = np.argsort(distance)

        # return nearest neighbors
        neighbor_indices = sorted_indices[:, :num_neighbors]
        sorted_dists = np.take_along_axis(distance, neighbor_indices, axis=-1)

        return neighbor_indices.tolist(), sorted_dists.tolist()

    @staticmethod
    def compute_scores(T_0, neighbor_indices, distances):
        """
        Converts each test example's distances to its k-nearest neighbors into a soft score between [0, 1].
        Args:
            neighbor_indices: (B, k) list of each test example's nearest neighbors (indices) in the training set
            distances: (B, k) list of distances from test example i to each of its k-nearest neighbors
        Returns:
            scores: (B) list of soft scores for each test example i
            confidences: (B) list of confidences for each test example i
        """
        scores = []
        for indices, dists in zip(neighbor_indices, distances):
            score = 1 - math.exp(-T_0 * dists[0] * dists[0])
            scores.append(score)
        return scores
