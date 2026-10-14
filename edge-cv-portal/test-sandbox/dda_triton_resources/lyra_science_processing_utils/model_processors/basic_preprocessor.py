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

MEAN = np.array([[[0.485]], [[0.456]], [[0.406]]], dtype=np.float32)
STD = np.array([[[0.229]], [[0.224]], [[0.225]]], dtype=np.float32)


class BasicPreProcessor(InferencePreProcessor):

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

        interpolation = self.config['interpolation'] if 'interpolation' in self.config else cv2.INTER_AREA
        # scale image to proper input size
        if resize_to_height:
            h, w, _ = model_input.shape
            resize_h = resize_to_height
            resize_w = (int(w * resize_h / h) // 8) * 8
            image = cv2.resize(model_input, (resize_w, resize_h), interpolation=interpolation)  # resize to 224x()
        else:
            image = cv2.resize(model_input, (self.config['image_width'], self.config['image_height']),
                           interpolation=interpolation)
        # convert input to RGB
        if len(image.shape) == 2 or image.shape[2] == 1:
            # upconvert to RGB
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        if image.shape[2] == 4:
            # remove alpha channel
            image = cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)

        # reformat image to correct axis order (1, C, H, W) and type (float32)
        image = np.expand_dims(image, axis=0).transpose((0, 3, 1, 2)).astype(np.float32)
        # scale image to range 0.0 to 1.0 if appropriate
        if self.config['image_range_scale']:
            image /= 255.0
        # normalize image if appropriate
        if self.config['normalize']:
            image = (image - MEAN) / STD

        return image