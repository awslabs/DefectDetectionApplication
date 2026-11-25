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
"""Import shim (rfdetr-seg-support DD2). The RF-DETR semantic-segmentation
decoder is ``RfDetrSegPostProcessor`` in ``rf_detr_seg_postprocessor.py``: one
class for both Seg stage types, decoding in semantic mode for every stage type
other than ``rf_detr_instance_segmentation`` (the legacy
``rf_detr_semantic_segmentation`` manifest and a config without a ``type``
included). This module keeps the old class name and the three ``DEFAULT_*``
constants importable so existing imports resolve unchanged.
"""
from lyra_science_processing_utils.model_processors.rf_detr_seg_postprocessor import (
    DEFAULT_MASK_THRESHOLD,
    DEFAULT_NETWORK_INPUT,
    DEFAULT_SCORE_THRESHOLD,
    RfDetrSegPostProcessor,
)

RfDetrSemanticSegmentationPostProcessor = RfDetrSegPostProcessor

__all__ = [
    "DEFAULT_MASK_THRESHOLD",
    "DEFAULT_NETWORK_INPUT",
    "DEFAULT_SCORE_THRESHOLD",
    "RfDetrSemanticSegmentationPostProcessor",
]
