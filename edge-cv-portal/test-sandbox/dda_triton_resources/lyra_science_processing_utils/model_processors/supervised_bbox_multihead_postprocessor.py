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
from typing import List

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.object_detection_result import ObjectDetectionResult
from lyra_science_processing_utils.utils.bbox_processing import bbox_post_processing

BBOX_SCORE_COLUMN_INX = 4

class SupervisedBBoxMultiHeadPostProcessor(InferencePostProcessor):
    def __call__(self, model_output: List[np.ndarray], *args, **kwargs) -> List[ObjectDetectionResult]:
        """
        Post-process model output
        Args: 
            model_output : output of inference model
        Return: 
            inference result
        """
        # post-processing parameters
        pp_type =  self.config['pp_type']
        score_thr = self.config['pp_score_thr']
        iou_thr = self.config['pp_iou_thr']
        # scaling parameters
        src_img_size = self.config['src_img_size']
        network_input_size = (self.config['input_shape'][3], self.config['input_shape'][2])
        down_ratio = self.config['down_ratio']

        if self.config['classification_head_enabled'] and self.config['bbox_head_enabled']:
            bbox_pred, classification_pred = model_output
            score = float(classification_pred[0][1])
            bboxes = self._get_final_anomaly_pred_bbox(bbox_pred, pp_type, score_thr, 
                                                    iou_thr, src_img_size, network_input_size, down_ratio)
            return AnomalyResult(score=float(score), mask=None, bboxes=bboxes, confidence=score)
        elif self.config['classification_head_enabled']:
            score = float(model_output[0][1])
            return AnomalyResult(score=float(score), mask=None, confidence=score)
        elif self.config['bbox_head_enabled']:
            return self._get_final_anomaly_pred_bbox(model_output, pp_type, score_thr, 
                                                    iou_thr, src_img_size, network_input_size, down_ratio)
        else:
            raise ValueError('No heads were enabled for supervised multi-head model')

    @staticmethod
    def _get_final_anomaly_pred_bbox(bboxes_tensors, pp_type, score_thr, iou_thr, 
                                                src_img_size, network_input_size, down_ratio):
        """
        Add bbox results to ObjectDetectionResult
        :param:
            bboxes : tensors corresponding to predicted bbox results 
            pp_type, score_thr, iou_thr : post-processing parameters
            src_img_size, down_ratio : source image size, down ratio used in CenterNet, for scaling back
        :return: list of ObjectDetectionResult
        """
        def _scale_back_to_origional(bboxes, src_img_size, network_input_size, down_ratio):
            x_scale = src_img_size[0] / network_input_size[0] * down_ratio
            y_scale = src_img_size[1] / network_input_size[1] * down_ratio
            if bboxes is not None and bboxes.shape[0] > 0:
                bboxes[:, 0] *= x_scale 
                bboxes[:, 2] *= x_scale 
                bboxes[:, 1] *= y_scale 
                bboxes[:, 3] *= y_scale 
            return bboxes

        bbox_cls = None 
        if len(bboxes_tensors)==3:
            bboxes, scores, clses = bboxes_tensors
        else: 
            bboxes, scores, clses, bbox_cls = bboxes_tensors

        bboxes_out, scores_out, clses_out = bbox_post_processing(bboxes, scores, clses, bbox_cls, 
                                                                pp_type, score_thr, iou_thr)

        bboxes_out = _scale_back_to_origional(bboxes_out, src_img_size, network_input_size, down_ratio)
        res = []
        for i in range(bboxes_out.shape[0]):
            res.append(ObjectDetectionResult(bboxes_out[i,:].tolist(), str(int(clses_out[i])), float(scores_out[i]), 0.5))
        return res 

    @staticmethod
    def _get_confidence_score_bbox(bboxes):
        """
        Gets image level confidence score from anomaly bbox

        :param bboxes : object denoting predicted anomaly bboxes
        
        :return: object denoting confidence score
        """
        return bboxes[:, BBOX_SCORE_COLUMN_INX].max()