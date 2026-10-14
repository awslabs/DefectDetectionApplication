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
import logging
import numpy as np
from scipy import linalg
from typing import Dict, List

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.score_calibrator import SKLearnScoreCalibration

LOG = logging.getLogger(__name__)


class HypercolumnFIDPostProcessor(InferencePostProcessor):

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
        features, logits, data = model_output
        train_mu_sigma = [tuple(data[i]) for i in range(data.shape[0]) if
                          data[i][0] != -1.0 and data[i][1] != -1.0]
        distances = self.get_nearest_neighbors(self.config['num_neighbors'], features, train_mu_sigma)
        scores = self.compute_scores(distances)
        normalized_scores = self._score_calibrator.get_calibrated_score(scores)
        if output_features:
            return normalized_scores, logits, None
        # index 0 since predict method is fixed to batch size of 1
        return AnomalyResult(score=normalized_scores[0])

    @staticmethod
    def get_nearest_neighbors(num_neighbors, test_features, train_mu_sigma):
        """
        Given a batch of test features, computes the nearest neighbors from the reference images
            using their image-level features and Euclidean (L2) distance metric.
        Args:
            test_features: (B, D) or (D) tensor of test features
        Returns:
            sorted_dists: (B, k) list of distances to nearest neighbors
        """
        if len(test_features.shape) == 1:
            test_features = test_features.unsqueeze(0)

        distances = HypercolumnFIDPostProcessor.get_distances(test_features, train_mu_sigma, sort=True)
        sorted_dists = distances[:, :num_neighbors]

        return sorted_dists.tolist()

    @staticmethod
    def get_distances(test_features, train_mu_sigma, sort=False):
        if len(test_features.shape) == 1:
            test_features = test_features.unsqueeze(0)

        mu_sigma_test = list()
        distances = np.zeros((test_features.shape[0], len(train_mu_sigma)), dtype='float')
        for i in range(test_features.shape[0]):
            mu = np.mean(test_features[i].flatten(), axis=0)
            sigma = np.cov(test_features[i].flatten(), rowvar=False)
            mu_sigma_test.append((mu, sigma))

        for x in range(len(mu_sigma_test)):
            for y in range(len(train_mu_sigma)):
                mu1, sigma1 = mu_sigma_test[x]
                mu2, sigma2 = train_mu_sigma[y]
                distances[x, y] = HypercolumnFIDPostProcessor._calculate_frechet_distance(mu1, sigma1, mu2, sigma2)
            if sort:
                distances[x] = np.sort(distances[x])

        return distances

    @staticmethod
    def compute_scores(distances):
        """
        Converts each test example's distances to its k-nearest neighbors into a soft score between [0, 1].
        Args:
            distances: (B, k) list of distances from test example i to each of its k-nearest neighbors
        Returns:
            scores: (B) list of soft scores for each test example i
        """
        scores = []
        for dists in distances:
            score = np.mean(np.array(dists))
            scores.append(score)
        return scores

    @staticmethod
    def _calculate_frechet_distance(mu1, sigma1, mu2, sigma2, eps=1e-6):
        """Numpy implementation of the Frechet Distance.
        The Frechet distance between two multivariate Gaussians X_1 ~ N(mu_1, C_1)
        and X_2 ~ N(mu_2, C_2) is
                d^2 = ||mu_1 - mu_2||^2 + Tr(C_1 + C_2 - 2*sqrt(C_1*C_2)).

        Stable version by Dougal J. Sutherland.

        Params:
        -- mu1   : Numpy array containing the activations of a layer of the
                inception net (like returned by the function 'get_predictions')
                for generated samples.
        -- mu2   : The sample mean over activations, precalculated on an
                representative data set.
        -- sigma1: The covariance matrix over activations for generated samples.
        -- sigma2: The covariance matrix over activations, precalculated on an
                representative data set.

        Returns:
        --   : The Frechet Distance.
        """
        for x in [mu1, mu2, sigma1, sigma2]:
            if np.any(np.isnan(x)) or np.any(np.isinf(x)):
                return np.array(np.nan)

        mu1 = np.atleast_1d(mu1)
        mu2 = np.atleast_1d(mu2)
        if mu1.shape != mu2.shape:
            raise ValueError('Training and test mean vectors have different lengths')

        sigma1 = np.atleast_2d(sigma1)
        sigma2 = np.atleast_2d(sigma2)
        if sigma1.shape != sigma2.shape:
            raise ValueError('Training and test covariances have different dimensions')

        diff = mu1 - mu2

        # Product might be almost singular
        covmean, _ = linalg.sqrtm(sigma1.dot(sigma2), disp=False)
        if not np.isfinite(covmean).all():
            LOG.warning(f'fid calculation produces singular product; adding {eps} to diagonal of cov estimates')
            offset = np.eye(sigma1.shape[0]) * eps
            covmean = linalg.sqrtm((sigma1 + offset).dot(sigma2 + offset))

        # Numerical error might give slight imaginary component
        if np.iscomplexobj(covmean):
            if not np.allclose(np.diagonal(covmean).imag, 0, atol=1e-3):
                m = np.max(np.abs(covmean.imag))
                raise ValueError('Imaginary component {}'.format(m))
            covmean = covmean.real

        tr_covmean = np.trace(covmean)

        return diff.dot(diff) + np.trace(sigma1) + np.trace(sigma2) - 2 * tr_covmean
