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
        import sys
        print("WARMUP START: Beginning model warmup", file=sys.stderr)
        LOG.debug('Warming up model')
        try:
            for i in range(3):
                print(f"WARMUP ITER {i+1}: Starting iteration {i+1}/3", file=sys.stderr)
                LOG.debug(f'Warmup iteration {i+1}/3 starting')
                if 'raw_image_shape' in config:
                    print(f"WARMUP ITER {i+1}: Using raw_image_shape {config['raw_image_shape']}", file=sys.stderr)
                    fake_image = get_fake_image(config['raw_image_shape'][0], config['raw_image_shape'][1])
                    print(f"WARMUP ITER {i+1}: Created fake image with raw_image_shape: {fake_image.shape}", file=sys.stderr)
                    LOG.debug(f'Created fake image with raw_image_shape: {fake_image.shape}')
                else:
                    print(f"WARMUP ITER {i+1}: Using config dimensions {config['image_height']}x{config['image_width']}", file=sys.stderr)
                    fake_image = get_fake_image(config['image_height'], config['image_width'])
                    print(f"WARMUP ITER {i+1}: Created fake image with config dimensions: {fake_image.shape}", file=sys.stderr)
                    LOG.debug(f'Created fake image with config dimensions: {fake_image.shape}')
                print(f"WARMUP ITER {i+1}: About to call predict()", file=sys.stderr)
                LOG.debug(f'Starting predict() call for iteration {i+1}')
                self.predict(fake_image)
                print(f"WARMUP ITER {i+1}: predict() completed successfully", file=sys.stderr)
                LOG.debug(f'Completed predict() call for iteration {i+1}')
        except Exception as e:
            print(f"WARMUP ERROR: Warmup failed with exception: {e}", file=sys.stderr)
            import traceback
            print(f"WARMUP TRACEBACK: {traceback.format_exc()}", file=sys.stderr)
            LOG.warning(f'Warmup failed: {e}. Continuing without warmup.')
        print("WARMUP END: Warmup process completed", file=sys.stderr)
        LOG.debug('Warm-up done')

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
        import sys
        print(f"PREDICT START: Input image shape: {image.shape}", file=sys.stderr)
        LOG.debug(f'Starting predict with image shape: {image.shape}')
        
        # preprocess
        print("PREDICT: Starting preprocessing", file=sys.stderr)
        preprocess_output = self.pre_processor(image)
        print(f"PREDICT: Preprocessing completed, output type: {type(preprocess_output)}", file=sys.stderr)
        LOG.debug(f'Preprocessing completed, output type: {type(preprocess_output)}')

        # run stage1 model with or without transform preprocessing params
        print("PREDICT: Starting model inference", file=sys.stderr)
        model_input = preprocess_output[0] if isinstance(preprocess_output, tuple) else preprocess_output
        print(f"PREDICT: Model input shape: {model_input.shape if hasattr(model_input, 'shape') else type(model_input)}", file=sys.stderr)
        model_output = self.model(model_input)
        print(f"PREDICT: Model inference completed, output type: {type(model_output)}", file=sys.stderr)
        LOG.debug(f'Model inference completed, output type: {type(model_output)}')

        # post-process stage1 with or without transform preprocessing params
        print("PREDICT: Starting postprocessing", file=sys.stderr)
        result = self.post_processor(model_output, preprocess_metad = preprocess_output[1]) \
            if isinstance(preprocess_output, tuple) else self.post_processor(model_output)
        print(f"PREDICT: Postprocessing completed, result type: {type(result)}", file=sys.stderr)
        LOG.debug(f'Postprocessing completed, result type: {type(result)}')

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
            print(f"PREDICT END: Returning InferenceData with anomaly result - label: {result.label}, confidence: {result.confidence}", file=sys.stderr)
            return InferenceData(None, [SingleObjectInferenceData(anomaly_result=result)])

        raise NotImplementedError
