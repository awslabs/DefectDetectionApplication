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
from typing import Dict

BLANK_CLASSES: Dict = {
    'names': [],
    'normal_ids': []
}
LOG = logging.getLogger(__name__)


class ModelConfig:
    """
    Encapsulates the configuration of a model graph
    """

    def __init__(self, config: Dict):
        self._validate_config(config)
        self._model_graph_type = config['model_graph_type']
        self._stages = config['stages']
        self._image_level_classes = config['image_level_classes'] if 'image_level_classes' in config else BLANK_CLASSES
        self._pixel_level_classes = config['pixel_level_classes'] if 'pixel_level_classes' in config else BLANK_CLASSES
        self._bbox_level_classes = config['bbox_level_classes'] if 'bbox_level_classes' in config else BLANK_CLASSES
        classes_info = {
            'image_level_classes': self._image_level_classes,
            'pixel_level_classes': self._pixel_level_classes,
            'bbox_level_classes': self._bbox_level_classes,
        }
        for stage in self._stages:
            stage.update(classes_info)

    @staticmethod
    def _validate_config(config: Dict):
        """
        Ensures that the given config dictionary conforms to the structure defined here:
        https://code.amazon.com/packages/LyraScienceProcessingUtils/blobs/mainline/--/README.md
        """
        keys = config.keys()
        if len(keys) < 2:
            raise ValueError(f'Expected at least two top-level keys in config, got {len(keys)}: {keys}')
        if len(keys) > 4:
            LOG.warning(f'Expected at most four top-level keys in config, got {len(keys)}: {keys}')
        if 'model_graph_type' not in keys or 'stages' not in keys:
            raise ValueError(f'Expected keys "model_graph_type" and "stages", got {keys}')
        for i, stage in enumerate(config['stages']):
            if 'type' not in stage:
                raise ValueError(f'Expected key "type" in stage {i + 1}, not found')

    def num_stages(self):
        return len(self._stages)

    def get_stage(self, index):
        return self._stages[index]

    def get_stage_type(self, index):
        return self._stages[index]['type']

    def get_model_graph_type(self):
        return self._model_graph_type

    def get_image_level_classes(self):
        return self._image_level_classes['names']

    def get_image_level_normal_ids(self):
        return self._image_level_classes['normal_ids']

    def get_pixel_level_classes(self):
        return self._pixel_level_classes['names']

    def get_pixel_level_normal_ids(self):
        return self._pixel_level_classes['normal_ids']

    def get_bbox_level_classes(self):
        return self._bbox_level_classes['names']

    def get_bbox_level_normal_ids(self):
        return self._bbox_level_classes['normal_ids']

    def get_threshold(self):
        return self._stages[0]['threshold']