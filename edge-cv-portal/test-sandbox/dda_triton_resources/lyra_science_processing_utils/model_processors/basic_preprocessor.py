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


#: Metadata key carrying the letterbox transform to the post-processor. Namespaced
#: so it cannot collide with the transform pre-processor's polar-transform metadata,
#: which travels through the same `preprocess_metad` channel.
LETTERBOX_METADATA_KEY = 'letterbox'


def letterbox_transform(src_width: int, src_height: int,
                        dst_width: int, dst_height: int) -> dict:
    """The uniform-scale-and-pad mapping from a source image into a
    ``dst_width`` x ``dst_height`` canvas.

    Returns ``{'ratio', 'pad_x', 'pad_y', 'resized_width', 'resized_height'}``.
    A point ``(sx, sy)`` in the source lands at
    ``(sx * ratio + pad_x, sy * ratio + pad_y)``, so a detector's box in network
    space inverts as ``(nx - pad_x) / ratio`` — see
    ``YoloDetectionPostProcessor``. Pure arithmetic, so both directions are
    testable without images.
    """
    ratio = min(dst_width / float(src_width), dst_height / float(src_height))
    resized_w = int(round(src_width * ratio))
    resized_h = int(round(src_height * ratio))
    return {
        'ratio': ratio,
        'pad_x': (dst_width - resized_w) // 2,
        'pad_y': (dst_height - resized_h) // 2,
        'resized_width': resized_w,
        'resized_height': resized_h,
    }


class BasicPreProcessor(InferencePreProcessor):

    def __call__(self, model_input: np.ndarray, resize_to_height: Optional[int]=None, *args, **kwargs) -> np.ndarray:
        """
        Pre-process image to be used by model
        :param model_input: an image as numpy array with type uint8 and shape
                           (height, width, channels) or (height, width)
        :return: an RGB image as numpy array with type float32 and shape (1, 3, height, width).
                 When the model opts into ``preserve_aspect``, returns
                 ``(image, {'letterbox': ...})`` instead, so the post-processor can
                 invert the padding when it maps boxes back to the source image.

        Sizing has two modes. By default the image is resized straight to
        ``image_width`` x ``image_height``, which does NOT preserve aspect ratio: a
        wide frame is squashed horizontally, distorting every object by the frame's
        aspect ratio. That is fine for models trained the same way, and it is the
        historical behaviour, so it stays the default.

        A model whose manifest sets ``preserve_aspect`` is instead LETTERBOXED —
        scaled by a single ratio and centre-padded — so object proportions survive.
        Measured on a DLAP-701 with the yolo-world-blue-plate detector, squashing a
        4608x3288 frame into the 1280x1280 input cost ~5x of confidence (0.044 vs
        0.134 on a 2560x1376 crop; 5.02x on the full frame) and was the difference
        between zero detections and detections. The flag is per-model precisely
        because switching every model would change results for any that were
        trained against the squash.
        """
        if len(model_input.shape) == 3 and model_input.shape[2] not in [1, 3, 4] or len(model_input.shape) not in [2,
                                                                                                                   3]:
            raise ValueError(f'Expected an image with shape (H,W,3) or (H,W), instead got {model_input.shape}')

        interpolation = self.config['interpolation'] if 'interpolation' in self.config else cv2.INTER_AREA
        letterbox_metad = None
        # scale image to proper input size
        if resize_to_height:
            h, w, _ = model_input.shape
            resize_h = resize_to_height
            resize_w = (int(w * resize_h / h) // 8) * 8
            image = cv2.resize(model_input, (resize_w, resize_h), interpolation=interpolation)  # resize to 224x()
        elif self._preserve_aspect():
            image, letterbox_metad = self._letterbox(model_input, interpolation)
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

        # Only letterboxed models return the tuple form. Every other caller --
        # including supervised_bbox_stage1/stage2, which build their own
        # BasicPreProcessor -- keeps receiving a bare array exactly as before.
        if letterbox_metad is not None:
            return image, {LETTERBOX_METADATA_KEY: letterbox_metad}
        return image

    # ── letterboxing (opt-in) ──────────────────────────────────────────────
    def _preserve_aspect(self) -> bool:
        """True when this model opts into aspect-preserving resize.

        Accepted in the stage config or nested under ``preprocessing`` /
        ``detection``, because the manifest's stage dict and the assembled model
        config do not always carry the same nesting.
        """
        config = self.config if isinstance(self.config, dict) else {}
        if config.get('preserve_aspect'):
            return True
        for section in ('preprocessing', 'detection'):
            nested = config.get(section)
            if isinstance(nested, dict) and nested.get('preserve_aspect'):
                return True
        return False

    def _letterbox(self, model_input: np.ndarray, interpolation):
        """Uniform-scale into the model input and centre-pad the remainder.

        Padded with 114 (the value YOLO training pipelines conventionally use),
        so the filler matches what such a model saw for padding during training
        rather than introducing pure black borders it never encountered.
        """
        dst_w = int(self.config['image_width'])
        dst_h = int(self.config['image_height'])
        src_h, src_w = model_input.shape[0], model_input.shape[1]
        metad = letterbox_transform(src_w, src_h, dst_w, dst_h)

        resized = cv2.resize(
            model_input, (metad['resized_width'], metad['resized_height']),
            interpolation=interpolation)
        if resized.ndim == 2:
            canvas = np.full((dst_h, dst_w), 114, dtype=model_input.dtype)
        else:
            canvas = np.full((dst_h, dst_w, resized.shape[2]), 114,
                             dtype=model_input.dtype)
        top, left = metad['pad_y'], metad['pad_x']
        canvas[top:top + metad['resized_height'],
               left:left + metad['resized_width']] = resized
        return canvas, metad