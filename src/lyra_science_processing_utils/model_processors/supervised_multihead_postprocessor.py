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

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.score_calibrator import SKLearnScoreCalibration
from lyra_science_processing_utils.utils.polar_transform import inverse_banded_polar_transform

class SupervisedMultiHeadPostProcessor(InferencePostProcessor):

    def __init__(self, config: Dict):
        super().__init__(config)
        self.calibrated = self.config['calibrated'] if 'calibrated' in self.config else False

        if self.calibrated:
            self._score_calibrator = SKLearnScoreCalibration(self.config['max_val_score'])
            self._score_calibrator.set_params([self.config['calibrator_scaling'], self.config['calibrator_shift']])
        self.seg_recall_bias = config.get('seg_recall_bias', None)
        self.ood_by_transform_preprocessor = config.get('OOD_by_transform_preprocessor', False)

    def __call__(self, model_output: List[np.ndarray], *args, **kwargs) -> AnomalyResult:
        """
        Post-process model output
        :param model_output: output of inference model
        :return: inference result
        """
        score = None
        mask = None
        confidence = None
        if self.config['classification_head_enabled'] and self.config['segmentation_head_enabled']:
            mask, score = model_output
            score = self._get_final_anomaly_pred(score, self.config['class_normal_ids'])
            if self.calibrated:
                score = self._score_calibrator.get_calibrated_score([score])[0]
        elif self.config['classification_head_enabled']:
            score = model_output[0]
            score = self._get_final_anomaly_pred(score, self.config['class_normal_ids'])
            if self.calibrated:
                score = self._score_calibrator.get_calibrated_score([score])[0]
        elif self.config['segmentation_head_enabled']:
            mask = model_output[0]
        else:
            raise ValueError('No heads were enabled for supervised multi-head model')
        if mask is not None:  # seg head is active
            if score is None:  # but not cls head
                score = np.mean(mask)

            if self.seg_recall_bias is not None:  # increase defect segmentation recall by higher recall bias
                for i in range(mask[0].shape[0]):
                    if i not in self.config['seg_normal_ids']:
                        mask[0][i][:,:] += self.seg_recall_bias
            mask = np.argmax(mask.squeeze(0), axis=0)
            if "preprocess_metad" in kwargs:
                metad = kwargs["preprocess_metad"]
                tgt_img_size = metad["transformed_img_dim"]
                mask = cv2.resize(mask, tgt_img_size, interpolation=cv2.INTER_NEAREST)
                transform_type = metad["transform_type"]
                if transform_type != "polar_transform":
                    raise ValueError(f'currently only support polar transform for transform preprocessing')
                mask = inverse_banded_polar_transform(mask, metad, self.config['seg_normal_ids'][0])
                if self.ood_by_transform_preprocessor and "obj_presence" in metad \
                    and (metad["obj_presence"]=="Partial" or metad["obj_presence"]=="None"):
                        score = 2.0  # if there's no object or partial object, set it to anomaly with score=2.0
            else:
                mask = cv2.resize(mask, tuple(self.config["raw_image_shape"][::-1]), interpolation=cv2.INTER_NEAREST)
        score = score if score is None else float(score)

        # mask is single-channel int32 with values representing the label (0, 1, 2, ...)
        return AnomalyResult(score=score, mask=mask)

    @staticmethod
    def _get_final_anomaly_pred(pred, ignore_channels):
        """
        Convert multi-channel anomaly mask/score to single channel
        :param pred: predicted mask of shape 1 x C x H x W or predicted score of shape 1 x C
        :param ignore_channels: list of indices denoting normal channels
        :return: object of shape H x W or 1
        """
        n_channels = pred.shape[1]
        anomaly_ids = [channel_id for channel_id in range(n_channels) if channel_id not in ignore_channels]
        pred = np.take(pred, anomaly_ids, axis=1)
        pred = np.sum(pred, axis=1)[0]
        return pred

    @staticmethod
    def _get_confidence_score(model_output: np.ndarray) -> float:
        """
        calculate confidence score from segmentation mask prediction
        :param model_output: seg mask of shape 1 x C x H x W
        :return: float, confidence score for the corresponding model output
        """
        return float(np.mean(np.max(model_output, axis=1)))
