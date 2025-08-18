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
from typing import Callable, List

from lyra_science_processing_utils.model_config import ModelConfig
from lyra_science_processing_utils.model_graphs.single_stage_model_graph import SingleStageModelGraph
from lyra_science_processing_utils.model_graphs.two_stage_model_graph import TwoStageModelGraph
from lyra_science_processing_utils.model_graphs.two_stage_model_graph_bbox import TwoStageModelGraphBBox
from lyra_science_processing_utils.model_processors import get_processor
from lyra_science_processing_utils.model_processors.basic_preprocessor import BasicPreProcessor
from lyra_science_processing_utils.model_processors.transform_preprocessor import get_transform_preprocessor
from lyra_science_processing_utils.model_processors.supervised_bbox_multihead_postprocessor import \
    SupervisedBBoxMultiHeadPostProcessor
from lyra_science_processing_utils.model_processors.supervised_bbox_stage2_preprocessor import \
    SupervisedBBoxStage2PreProcessor
from lyra_science_processing_utils.model_processors.supervised_bbox_stage1_postprocessor import \
    SupervisedBBoxStage1PostProcessor

LOG = logging.getLogger(__name__)

class ModelGraphFactory:

    @staticmethod
    def get_model_graph(config: ModelConfig, models: List[Callable[[np.ndarray], List[np.ndarray]]]):
        """
        Initializes and returns a model graph as defined in config.
        :param config: dictionary with configuration for model graph and model(s)
        :param models: list of references to models needed for model graph
        """

        # Single-stage model graph
        if config.get_model_graph_type() == 'single_stage_model_graph':
            ModelGraphFactory._validate_model_graph(1, config, len(models))
            model_config = config.get_stage(0)
            if "transform_preprocessing" not in model_config:
                pre_processor = BasicPreProcessor(model_config)
            else:
                pre_processor = get_transform_preprocessor(model_config)
                LOG.info(f"Transform preprocessing ({model_config['transform_preprocessing']}) is enabled")
            post_processor = get_processor(config.get_stage_type(0))(model_config)
            return SingleStageModelGraph(model_config, models[0], pre_processor, post_processor)

        # Two-stage model graph
        if config.get_model_graph_type() == 'two_stage_model_graph':
            ModelGraphFactory._validate_model_graph(2, config, len(models))
            model1_config = config.get_stage(0)
            model2_config = config.get_stage(1)
            stage1_pre_processor = BasicPreProcessor(model1_config)
            stage1_post_processor = get_processor(config.get_stage_type(0))(model1_config)
            stage2_post_processor = get_processor(config.get_stage_type(1))(model2_config)
            return TwoStageModelGraph(model1_config, model2_config, models[0], models[1], stage1_pre_processor,
                                      stage1_post_processor, stage2_post_processor)

        # two-stage model graph for bbox
        if config.get_model_graph_type() == 'two_stage_model_graph_bbox':
            ModelGraphFactory._validate_model_graph(2, config, len(models))
            model1_config = config.get_stage(0)
            model2_config = config.get_stage(1)
            stage1_pre_processor = BasicPreProcessor(model1_config)
            stage1_post_processor = SupervisedBBoxStage1PostProcessor(model1_config, model2_config)
            stage2_post_processor = SupervisedBBoxMultiHeadPostProcessor(model2_config)
            stage2_pre_processor = SupervisedBBoxStage2PreProcessor(model2_config)
            return TwoStageModelGraphBBox(model1_config, model2_config, models[0], models[1], stage1_pre_processor,
                                      stage1_post_processor, stage2_pre_processor, stage2_post_processor)

        raise ValueError(f'Unsupported model graph type: {config.get_model_graph_type()}')

    @staticmethod
    def _validate_model_graph(expected_stages, config, num_models):
        if config.num_stages() != expected_stages:
            raise ValueError(f'Config for {config.get_model_graph_type()} should have {expected_stages} stage(s), got {config.num_stages()}')  # noqa: E501
        if num_models != expected_stages:
            raise ValueError(f'{expected_stages} model(s) needed for {config.get_model_graph_type()}, got {num_models}')
