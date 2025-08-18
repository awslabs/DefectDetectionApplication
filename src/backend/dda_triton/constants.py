#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
### Globals ,constant variables.
GREEN_CHECK_MARK = "".join(["[", "\u2705", "]"])
RED_CROSS_MARK = "".join(["[", "\u274c", "]"])
MANIFEST_FILENAME = "manifest.json"
MODEL_GRAPH_MANIFEST_KEY = "model_graph"
DATASET_MANIFEST_KEY = "dataset"
DATASET_IMAGE_WIDTH_MANIFEST_KEY = "image_width"
DATASET_IMAGE_HEIGHT_MANIFEST_KEY = "image_height"
PIXEL_LEVEL_CLASSES = "pixel_level_classes"
DDA_ROOT_FOLDER="/aws_dda"
TRITON_MODEL_DIR = DDA_ROOT_FOLDER+ "/dda_triton/triton_model_repo"
TRITON_INSTALLATION_DIR = "/opt/tritonserver"
DDA_TRITON_FOLDER="/aws_dda/dda_triton/"