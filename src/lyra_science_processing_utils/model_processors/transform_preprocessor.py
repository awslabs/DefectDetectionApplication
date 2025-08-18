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
from typing import Optional
import cv2
import numpy as np

from lyra_science_processing_utils.inference_preprocessor import InferencePreProcessor
from lyra_science_processing_utils.utils.polar_transform import banded_polar_transform, find_circular_obj_consensus
from .basic_preprocessor import MEAN, STD

def get_transform_preprocessor(model_config):
    if "transform_preprocessing" not in model_config:
        raise ValueError('key transform_preprocessing in the model config')
    transform_type = model_config["transform_preprocessing"]
    if transform_type=="polar_transform":
        return PolarTransformPreProcessor(model_config)
    else:
        raise ValueError(f'the transform {transform_type} is not supported')

def _is_partial_circle(center, radius, image, tolerance=2):
    """
    Test if the circle is fully contained in the image
    """
    cx, cy = int(center[0]), int(center[1])
    hgt, wid = image.shape[0], image.shape[1]
    if cx - radius < -tolerance or cx + radius >= wid + tolerance  \
        or cy - radius < -tolerance or cy + radius >= hgt + tolerance:
            return True
    return False

class PolarTransformPreProcessor(InferencePreProcessor):

    def __call__(self, model_input: np.ndarray, resize_to_height: Optional[int]=None, *args, **kwargs) -> np.ndarray:
        """
        Pre-process image to be used by model
        :param model_input: an image as numpy array with type uint8 and shape
                           (height, width, channels) or (height, width)
        :return: an RGB image as numpy array with type float32 and shape (1, 3, height, width)
        """
        if len(model_input.shape) == 3 and model_input.shape[2] not in [1, 3, 4] or len(model_input.shape) not in [2,
                                                                                                                   3]:
            raise ValueError(f'Expected an image with shape (H,W,3) or (H,W), instead got {model_input.shape}')

        image = model_input
        # convert input to RGB
        if len(image.shape) == 2 or image.shape[2] == 1:
            # upconvert to RGB
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        if image.shape[2] == 4:
            # remove alpha channel
            image = cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)

        # apply polar transform
        obj_presence = "None"  # object presence status
        try:
            cx, cy, radius = find_circular_obj_consensus(image)
            obj_presence = "Partial" if _is_partial_circle((cx, cy), radius, image) else "Full"
        except:  # no circular object can be found, we will still do polar but it will be tagged with None
            cx, cy = image.shape[1] // 2, image.shape[0] // 2
        image, metad = banded_polar_transform(image, (cx, cy))
        metad["obj_presence"] = obj_presence

        # reformat image to correct axis order (1, C, H, W) and type (float32)
        image = np.expand_dims(image, axis=0).transpose((0, 3, 1, 2)).astype(np.float32)
        # scale image to range 0.0 to 1.0 if appropriate
        if self.config['image_range_scale']:
            image /= 255.0
        # normalize image if appropriate
        if self.config['normalize']:
            image = (image - MEAN) / STD

        return (image, metad)