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
from typing import Dict, Tuple, Type

from lyra_science_processing_utils.inference_postprocessor import InferencePostProcessor
from lyra_science_processing_utils.model_processors.contextual_spade_postprocessor import ContextualSpadePostProcessor
from lyra_science_processing_utils.model_processors.distance_based_classifier_postprocessor import \
    DistanceBasedClassifierPostProcessor
from lyra_science_processing_utils.model_processors.hypercolumn_fid_postprocessor import HypercolumnFIDPostProcessor
from lyra_science_processing_utils.model_processors.imagestats_postprocessor import ImagestatsPostProcessor
from lyra_science_processing_utils.model_processors.knn_feedback_postprocessor import KnnFeedbackPostProcessor
from lyra_science_processing_utils.model_processors.pretrained_feature_gaussian_postprocessor import \
    PretrainedFeatureGaussianPostProcessor
from lyra_science_processing_utils.model_processors.supervised_multihead_postprocessor import \
    SupervisedMultiHeadPostProcessor
from lyra_science_processing_utils.model_processors.supervised_bbox_multihead_postprocessor import \
    SupervisedBBoxMultiHeadPostProcessor
from lyra_science_processing_utils.model_processors.svdd_postprocessor import SVDDPostProcessor


MODEL_NAME_MAP: Dict[str, Tuple[str, Type[InferencePostProcessor]]] = {
    "mooncake": ("unsupervised_contextual_spade", ContextualSpadePostProcessor),
    "cheesecake": ("distance_based_classifier", DistanceBasedClassifierPostProcessor),
    "tiramisu": ("hypercolumn_fid_classifier", HypercolumnFIDPostProcessor),
    "chocolatemousse": ("imagestats_model", ImagestatsPostProcessor),
    "jalebi": ("unsupervised_pretrained_feature_gaussian", PretrainedFeatureGaussianPostProcessor),
    "baklava": ("unsupervised_svdd", SVDDPostProcessor),
    "torrone": ("knn_feedback_model", KnnFeedbackPostProcessor),
    "mochi": ("supervised_multi_head_model", SupervisedMultiHeadPostProcessor),
    "dango": ("supervised_multi_head_model_bbox", SupervisedBBoxMultiHeadPostProcessor)
}


def get_model_nickname(model_name: str) -> str:
    for k, v in MODEL_NAME_MAP.items():
        if model_name == v[0]:
            return k
    raise ValueError(f'Model name {model_name} is not defined in MODEL_NAME_MAP')


def get_processor(model_name: str) -> Type[InferencePostProcessor]:
    if model_name in MODEL_NAME_MAP:
        return MODEL_NAME_MAP[model_name][1]
    return MODEL_NAME_MAP[get_model_nickname(model_name)][1]
