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
from lyra_science_processing_utils.utils.anomaly_result import AnomalyResult
from lyra_science_processing_utils.utils.object_detection_result import ObjectDetectionResult

LOG = logging.getLogger(__name__)


class SingleStageModelGraph(ModelGraph):

    def __init__(self, config: Dict,
                 model: Callable[[np.ndarray], List[np.ndarray]],
                 pre_processor: InferencePreProcessor,
                 post_processor: InferencePostProcessor):
        """
        Creates a single-stage model graph

        The model parameter is a Callable that takes care of calling the actual model inference method for us.
        """
        self.threshold = config['threshold']
        self.classification_logic = config["classification_logic"] if "classification_logic" in config else None
        self.seg_normal_ids = config["seg_normal_ids"] if "seg_normal_ids" in config else None
        self.model = model
        self.pre_processor = pre_processor
        self.post_processor = post_processor

        # warm-up models
        LOG.info('Starting model warmup')
        try:
            for i in range(3):
                if 'raw_image_shape' in config:
                    fake_image = get_fake_image(config['raw_image_shape'][0], config['raw_image_shape'][1])
                else:
                    fake_image = get_fake_image(config['image_height'], config['image_width'])
                self.predict(fake_image)
        except Exception as e:
            LOG.warning(f'Warmup failed: {e}. Continuing without warmup.')
        LOG.info('Model warmup completed')

    def _overide_class_label(self, result):
        """
        Override the class label with classification logic
        """
        if result.mask is None or self.classification_logic=="cls_head":
            return result
        labels = np.unique(result.mask)
        seg_label = 'normal' if set(labels) <= set(self.seg_normal_ids) else 'anomaly'
        if self.classification_logic=="seg_head":
            result.label = seg_label
        elif self.classification_logic=="cls_and_seg":
            if result.label != seg_label:
                result.label = 'normal'
        elif self.classification_logic=="cls_or_seg":
            if result.label != seg_label:
                result.label = 'anomaly'
        else:
            raise ValueError(f"Unrecongized classification logic {self.classification_logic}")
        return result

    def predict(self, image: np.ndarray) -> InferenceData:
        LOG.debug(f'Starting predict with image shape: {image.shape}')
        
        # preprocess
        preprocess_output = self.pre_processor(image)

        # run stage1 model with or without transform preprocessing params
        model_input = preprocess_output[0] if isinstance(preprocess_output, tuple) else preprocess_output
        model_output = self.model(model_input)

        # post-process stage1 with or without transform preprocessing params
        result = self.post_processor(model_output, preprocess_metad = preprocess_output[1]) \
            if isinstance(preprocess_output, tuple) else self.post_processor(model_output)

        if isinstance(result, list): # bbox detection results when only bbox head is enabled
            if result:
                if isinstance(result[0], ObjectDetectionResult):
                    return InferenceData(None, [SingleObjectInferenceData(object_detection_result=x) for x in result])
            else:
                return InferenceData() # if no bbox present, return empty InferenceData
        elif isinstance(result, AnomalyResult):  # classification result with segmentation (.mask) or bbox (.bboxes)
            if result.score is not None:
                result.label = get_label(result.score, self.threshold)
                if result.score<=1.0 and self.classification_logic is not None and self.seg_normal_ids is not None:
                    result = self._overide_class_label(result)
                result.confidence = get_confidence(result.score, result.label)
            return InferenceData(None, [SingleObjectInferenceData(anomaly_result=result)])

        raise NotImplementedError
