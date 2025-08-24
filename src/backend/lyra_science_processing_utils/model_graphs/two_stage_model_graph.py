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
from typing import Callable, Dict, List

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.inference_preprocessor import InferencePreProcessor
from lyra_science_processing_utils.model_graph import ModelGraph
from lyra_science_processing_utils.utils import get_label, get_fake_image, get_confidence
from lyra_science_processing_utils.utils.inference_data import InferenceData, SingleObjectInferenceData

LOG = logging.getLogger(__name__)


class TwoStageModelGraph(ModelGraph):

    def __init__(self, stage1_config: Dict, stage2_config: Dict,
                 stage1_model: Callable[[np.ndarray], List[np.ndarray]],
                 stage2_model: Callable[[np.ndarray], List[np.ndarray]],
                 stage1_pre_processor: InferencePreProcessor,
                 stage1_post_processor: InferencePostProcessor,
                 stage2_post_processor: InferencePostProcessor):
        """
        Creates a two-stage model graph

        The stage1_model and stage2_model parameters are Callables that takes care of calling the actual models
        inference methods for us.
        """
        self.threshold = stage1_config['threshold']
        self.stage1_model = stage1_model
        self.stage2_model = stage2_model
        self.stage1_pre_processor = stage1_pre_processor
        self.stage1_post_processor = stage1_post_processor
        self.stage2_post_processor = stage2_post_processor

        # warm-up models
        LOG.debug('Warming up models')
        for i in range(3):
            self.predict(get_fake_image(stage1_config['image_height'], stage1_config['image_width']))
        LOG.debug('Warm-up done')

    def predict(self, image: np.ndarray) -> InferenceData:
        # preprocess
        stage1_preprocess_output = self.stage1_pre_processor(image)

        # run stage1 model
        stage1_output = self.stage1_model(stage1_preprocess_output)

        # post-process stage1
        scores, features, mask = self.stage1_post_processor(stage1_output, output_features=True)

        # run stage2 model
        stage2_output = self.stage2_model(features)

        # post-process stage2
        result = self.stage2_post_processor(stage2_output, scores=scores)

        # add mask
        if mask is not None:
            result.mask = mask

        # add label
        if result.score is not None:
            result.label = get_label(result.score, self.threshold)
            result.confidence = get_confidence(result.score, result.label)

        return InferenceData(None, [SingleObjectInferenceData(anomaly_result=result)])
