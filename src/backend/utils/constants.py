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

DDA_ROOT_FOLDER = "/aws_dda"
DDA_GREENGRASS_ROOT_FOLDER = DDA_ROOT_FOLDER + "/greengrass/v2"
DDA_LOGO_FOLDER = DDA_ROOT_FOLDER + "/assets/logo"
DDA_SYSTEM_FOLDER = DDA_ROOT_FOLDER + "/system"
IMAGE_CAPTURE_DIR = DDA_ROOT_FOLDER + "/image-capture"
INFERENCE_RESULTS_DIR = DDA_ROOT_FOLDER + "/inference-results"
AUTHORIZATION_SETTINGS_FILE = DDA_ROOT_FOLDER + "/authorization_settings.json"

DEFAULT_IMAGE_OUTPUT_PREFIX = "default_file_prefix"
DEFAULT_IMAGE_SAVE_DIR_PATH = IMAGE_CAPTURE_DIR + "/preview"

DEFAULT_CAMERA_CONFIG_FILE_PATH = "./utils/config/default_camera_configurations.json"

EM_AGENT_CONFIG_PATH = "em_agent/config/edge_manager_agent_config.json"

GPIO_RISING = 'GPIO.RISING'
GPIO_FALLING = 'GPIO.FALLING'

DIGITAL_IO_SIGNAL_TYPES = [GPIO_RISING, GPIO_FALLING]
ANOMALY = 'Anomaly'
NORMAL = 'Normal'
CAPTURE = 'Capture'
INFERENCE = 'Inference'
OUTPUT_RULE = ['All', NORMAL, ANOMALY]
PREDICTION = [NORMAL, ANOMALY]
CAPTURE_TYPE = [CAPTURE, INFERENCE]
DB_TEXT_NOTE_MAX_LENGTH = 50

FRAME_CAPTURE_TIMESTAMP = 'FRAME_CAPTURE'
INFERENCE_RECEIVED_TIMESTAMP = "INFERENCE_RECIEVED" # TODO: Typo in literal, but fixing this requires migrating the db entries as well. Do with next db migration.
TRIGGER_TIMESTAMP = "TRIGGER_TIMESTAMP"

LATENCY_TYPES = [FRAME_CAPTURE_TIMESTAMP, INFERENCE_RECEIVED_TIMESTAMP, TRIGGER_TIMESTAMP]

DDA_GG_COMPONENT_NAME_PREFIX = "aws.edgeml.dda."
LFV_AGENT_GG_COMPONENT_NAME = "aws.iot.lookoutvision.EdgeAgent"
DDA_LOCAL_SERVER_COMPONENT = "aws.edgeml.dda.LocalServer"

DDA_LOCAL_SERVER_SSL_CERT = DDA_GREENGRASS_ROOT_FOLDER + "/device.pem.crt"
DDA_LOCAL_SERVER_SSL_KEY = DDA_GREENGRASS_ROOT_FOLDER + "/private.pem.key"

GG_IPC_FUTURE_TIMEOUT = 10
INFERENCE_RESULT_MAX_DOWNLOAD = 1000

# Data Capture
MIN_IMAGE_CAPTURE_COUNT = 1
MAX_IMAGE_CAPTURE_COUNT = 100
MIN_IMAGE_CAPTURE_TIME_INTERVAL = 1
MAX_IMAGE_CAPTURE_TIME_INTERVAL = 120

GET_DDA_COMPONENT_STATUS_HEALTHY = "HEALTHY"
GET_DDA_COMPONENT_STATUS_UNHEALTHY = "UNHEALTHY"

DDA_SYSTEM_GROUP = "dda_system_group"
DDA_SYSTEM_USER = "dda_system_user"
DDA_ADMIN_GROUP = "dda_admin_group"
DDA_ADMIN_USER = "dda_admin_user"

GGC_USER = "ggc_user"
GGC_GROUP = "ggc_group"
VIDEO_GROUP = "video"

INFERENCE_INPUT_IMAGE_CONTENT_TYPE = "jpg"
INFERENCE_OUTPUT_RES_CONTENT_TYPE = "json"
INFERENCE_OUTPUT_RES_LABEL_CONTENT_TYPE = "json_with_base64_encoding"
INFERENCE_OUTPUT_IMAGE_CONTENT_TYPE = "out.jpg" # will be deprecated since inference app 1.20230911.528802a8
INFERENCE_OUTPUT_MASK_CONTENT_TYPE_PREFIX = "mask" # available since inference app 1.20230911.528802a8
INFERENCE_OUTPUT_OVERLAY_CONTENT_TYPE = "overlay.jpg" # available since inference app 1.20230911.528802a8

EDGE_AGENT_VENV_PATH = DDA_GREENGRASS_ROOT_FOLDER + "/work/" + LFV_AGENT_GG_COMPONENT_NAME
CAPTURED_IDS_PATH_PATTERN = r"\/aws_dda\/system\/capture-id-path-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}"
SNAPSHOT_FILE_PATTERN = r"(snapshotfile/)?snapshot-.*-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.tar\.gz"
CAPTURED_IMAGE_FOLDER_PATTHERN = r"\/aws_dda\/image-capture\/[a-zA-Z0-9]+"
CAPTURED_IMAGE_FILE_PATH_PATTHERN = r"\/aws_dda\/image-capture\/[a-zA-Z0-9_/s-]+"
PYTHON38 = "python3.11"

MODEL_CONFIDENCE_THRESHOLDS = "modelConfidenceThresholds"
MODEL_CONFIDENCE_THRESHOLD_NORMAL = "NormalThreshold"
MODEL_CONFIDENCE_THRESHOLD_ANOMALY = "AnomalyThreshold"

ALEMBIC_CONFIG_PATH= "alembic.ini"
ALEMBIC_CP_DATABASE_INIT_SECTION = "database_configuration"
ALEMBIC_METADATA_DATABASE_INIT_SECTION = "database_metadata"

IS_TRITON_FILE_PATH = DDA_ROOT_FOLDER + "/dda_triton/is_triton.txt"
LFV_MODEL_DIR_PATH = DDA_ROOT_FOLDER + "/greengrass/v2/packages/artifacts-unarchived/"
ARCHIVED_COMPONENTS_PATH = DDA_ROOT_FOLDER + "/greengrass/v2/packages/artifacts/"
UNARCHIVED_COMPONENTS_PATH = DDA_ROOT_FOLDER + "/greengrass/v2/packages/artifacts-unarchived/"
MODEL_PREFIX = "model-"
TRUE_VALUES = ('true', 'True')