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
import cv2

def get_label(score: float, threshold: float) -> str:
    """
    Compares score to threshold and returns Normal if score is under threshold, Anomaly otherwise.
    Comparison is done up to the 4th significant digit.
    """
    return 'normal' if round(score, 4) < round(threshold, 4) else 'anomaly'

def get_confidence(score, label):
    score = max(min(score, 1.0), 0.0)  # clip to 0 to 1.0
    if label == 'normal':
        return 1 - score
    else:
        return score

def get_pmax_confidence(scores):
    """
    Calculates confidence based on anomaly scores
    """
    return [max(x, 1 - x) for x in scores]


def get_fake_image(height, width):
    """
    Returns a random image with shape (height, width)
    """
    return (np.random.rand(height, width) * 255.0).astype(np.uint8)


def convert_image_to_numpy(image):
    """
    Rearranges an image in tensor format (CxHxW) to numpy format (HxWxC)
    """

    # nothing to do, image is (HxW)
    if len(image.shape) == 2:
        return image

    # reformat image to numpy axis order
    return image.transpose((1, 2, 0))

def convert_mask_to_int32(mask, threshold=0.5):
    return np.int32(mask > threshold)

def load_image_from_file_as_numpy_uint8(filename: str) -> np.ndarray:
    """
    Load image as numpy array 
    Args:
        filename : full path of the image
    Returns:
        numpy array of the image
    """
    image = cv2.imread(filename, cv2.IMREAD_UNCHANGED)
    if len(image.shape) > 2 and image.shape[2] >= 3:
        code = cv2.COLOR_BGR2RGB if image.shape[2] == 3 else cv2.COLOR_BGRA2RGB
        image = cv2.cvtColor(image, code)
    return image