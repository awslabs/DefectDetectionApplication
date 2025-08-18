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
from sklearn.base import BaseEstimator
from sklearn.calibration import CalibratedClassifierCV


class IdentityBaseEstimator(BaseEstimator):
    def __init__(self):
        self.classes_ = [0, 1]
        pass

    def fit(self, X, y=None):  # noqa: N803 , required by scikit-learn
        pass

    @staticmethod
    def predict_proba(X):  # noqa: N803 , required by scikit-learn
        a = np.zeros((X.shape[0], 2))
        a[:, 0] = 1 - X[:, 0]
        a[:, 1] = X[:, 0]
        return a


class SKLearnScoreCalibration:

    def __init__(self, max_val_score):
        self.identity_estimator = IdentityBaseEstimator()
        self._calibrator = CalibratedClassifierCV(self.identity_estimator, method='sigmoid', cv='prefit')
        self._max_val_score = max_val_score

    def set_calibration_params(self, scores, targets):
        normalized_scores = scores / self._max_val_score if self._max_val_score > 1 else scores
        pred_scores_arr = np.asarray(normalized_scores).reshape(-1, 1)
        targets = np.asarray(targets)
        ntotal = pred_scores_arr.shape[0]
        zerowt = 1 - float(np.sum(targets == 0)) / ntotal
        onewt = 1 - zerowt
        sample_weight = [onewt if x == 1 else zerowt for x in targets]
        self._calibrator.fit(pred_scores_arr, targets, sample_weight)

    def get_calibrated_score(self, scores):
        scores = scores / self._max_val_score if self._max_val_score > 1 else scores
        pred_scores_arr = np.asarray(scores).reshape(-1, 1)
        out = self._calibrator.predict_proba(pred_scores_arr)
        return out[:, 1]

    def get_params(self):
        a = self._calibrator.calibrated_classifiers_[0].calibrators_[0].a_
        b = self._calibrator.calibrated_classifiers_[0].calibrators_[0].b_
        return [a, b]

    def set_params(self, params):
        self.identity_estimator = IdentityBaseEstimator()
        self._calibrator = CalibratedClassifierCV(self.identity_estimator, method='sigmoid', cv='prefit')
        # ToDo check if I can skip fit here
        a = np.zeros((2, 1))
        a[0] = 0.5
        a[1] = 0.25
        b = [0, 1]
        self._calibrator.fit(a, b)
        self._calibrator.calibrated_classifiers_[0].calibrators_[0].a_ = params[0]
        self._calibrator.calibrated_classifiers_[0].calibrators_[0].b_ = params[1]
