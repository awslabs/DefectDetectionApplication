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
import awsiot.greengrasscoreipc

from gstreamer.gst_pipeline_executor import GstPipelineExecutor
from dao.iotshadow.IoTShadowAccessor import IoTShadowAccessor
from mqtt.PublishHandler import PublishHandler
from edgeagent.lfv_edge_agent import LFVEdgeAgent
from resources.accessors.image_source_configuration_accessor import ImageSourceConfigurationAccessor
from resources.accessors.input_configuration_accessor import InputConfigurationAccessor
from resources.accessors.output_configuration_accessor import OutputConfigurationAccessor
from resources.accessors.workflow_accessor import WorkflowAccessor
from resources.accessors.workflow_metadata_accessor import WorkflowMetadataAccessor
from resources.accessors.latency_time_accessor import LatencyTimeAccessor
from defect_detection_config.defect_detection_config import DefectDetectionConfig
from resources.accessors.image_source_accessor import ImageSourceAccessor
from resources.accessors.inference_result_accessor import InferenceResultAccessor
from utils.capture_task_manager import CaptureTaskManager
# set up IPC client to connect to the IPC server
ipc_client = awsiot.greengrasscoreipc.connect()
iot_shadow_accessor = IoTShadowAccessor(ipc_client)
image_source_accessor = ImageSourceAccessor()
image_src_cfg_accessor = ImageSourceConfigurationAccessor()
input_cfg_accessor = InputConfigurationAccessor()
output_cfg_accessor = OutputConfigurationAccessor()
latency_time_accessor = LatencyTimeAccessor()
inference_result_accessor = InferenceResultAccessor()
workflow_accessor = WorkflowAccessor(iot_shadow_accessor)
workflow_metadata_accessor = WorkflowMetadataAccessor()
publish_handler = PublishHandler(ipc_client)
gst_pipeline_executor = GstPipelineExecutor()
capture_task_manager = CaptureTaskManager(inference_result_accessor, gst_pipeline_executor)

# set up LFV edge agent gRPC client
lfv_edge_agent = LFVEdgeAgent()

# set up local server config
defect_detection_config = DefectDetectionConfig(ipc_client)