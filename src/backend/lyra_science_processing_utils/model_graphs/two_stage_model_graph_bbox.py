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
import cv2 
from typing import Callable, Dict, List

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.inference_preprocessor import InferencePreProcessor
from lyra_science_processing_utils.model_graph import ModelGraph
from lyra_science_processing_utils.utils import get_label, get_fake_image, get_confidence
from lyra_science_processing_utils.utils.inference_data import InferenceData, SingleObjectInferenceData
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from .two_stage_model_graph import TwoStageModelGraph
LOG = logging.getLogger(__name__)

class TwoStageModelGraphBBox(TwoStageModelGraph):
    def __init__(self, stage1_config: Dict, stage2_config: Dict,
                 stage1_model: Callable[[np.ndarray], List[np.ndarray]],
                 stage2_model: Callable[[np.ndarray], List[np.ndarray]],
                 stage1_pre_processor: InferencePreProcessor,
                 stage1_post_processor: InferencePostProcessor,
                 stage2_pre_processor: InferencePreProcessor,
                 stage2_post_processor: InferencePostProcessor):
        """
        Creates a two-stage model graph for bbox detection

        The stage1_model and stage2_model parameters are Callables that takes care of calling the actual models
        inference methods for us.
        """
        self.affine_align = stage1_config['affine_align']
        self.stage2_pre_processor = stage2_pre_processor

        super().__init__(stage1_config, stage2_config, stage1_model, stage2_model, stage1_pre_processor,
                                      stage1_post_processor, stage2_post_processor)

    def predict(self, image: np.ndarray) -> InferenceData:
        if len(image.shape) == 2: # for the fake image generated for warmup
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        # preprocess
        stage1_preprocess_output = self.stage1_pre_processor(image, (None if self.affine_align else 224))
        # run stage1 model
        stage1_output = self.stage1_model(stage1_preprocess_output)
        # post-process stage1
        stage2_input = self.stage1_post_processor(image, stage1_output)
        
        # if affine_align enabled, get the affine-alined image
        if self.affine_align:
            image_feat_tensor, image, aligned_ref_image = stage2_input
            aligned_ref_image = self.stage1_pre_processor(aligned_ref_image)
            _, ref_image_feat_tensor = self.stage1_model(aligned_ref_image)
            stage2_input = self.stage2_pre_processor(image, [image_feat_tensor, ref_image_feat_tensor])

        # run stage2 model
        stage2_output = self.stage2_model(stage2_input)
        # post-process stage2
        result = self.stage2_post_processor(stage2_output)

        if isinstance(result, AnomalyResult):
            # add label
            if result.score is not None:
                result.label = get_label(result.score, self.threshold)
                result.confidence = get_confidence(result.score, self.threshold)

            return InferenceData(None, [SingleObjectInferenceData(anomaly_result=result)])

        # multiple result output
        if isinstance(result, list):
            return InferenceData(None, [SingleObjectInferenceData(object_detection_result=x) for x in result])
        else:
            raise NotImplementedError
