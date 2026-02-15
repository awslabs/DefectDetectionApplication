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
import json
import time

from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR

from exceptions.api.unexpected_type_exception import UnexpectedTypeException
from model.PipelineConfiguration import PluginDefinition, PipelineConfiguration, PluginArg
from model.image_source import ImageSourceType
from model.workflow import Workflow
from model.output_configuration import OutputConfigurationSchema
from utils import utils, captured_images_utils, constants
from utils.get_is_triton import get_is_triton
from dda_triton.constants import TRITON_INSTALLATION_DIR, TRITON_MODEL_DIR
import logging
logger = logging.getLogger(__name__)

class GstPipelineBuilder:
    def __init__(self):
        self.workflow_config = None
        self.image_source = None
        self.pipeline_config = PipelineConfiguration()

    def _add_camera_image_source(self, image_source, override_processing_pipeline: str = None):
        self.pipeline_config.add_plugin(PluginDefinition("appsrc", [PluginArg("name", "appsrc")]))
        self.pipeline_config.add_plugin(override_processing_pipeline or image_source.get("processingPipeline"))

        crop_config = image_source.get("imageCrop")
        if crop_config:
            self.pipeline_config.add_plugin(PluginDefinition("videocrop", [
                PluginArg("top", crop_config.get("top")),
                PluginArg("bottom", crop_config.get("bottom")),
                PluginArg("left", crop_config.get("left")),
                PluginArg("right", crop_config.get("right"))
            ]))

    ## DD-18130: Add support for smart cameras
    def _add_icam_image_source(self, image_source_config, override_processing_pipeline: str = None):
        logger.debug("setup pipeline for icam image_source="+str(image_source_config))
        # default for icam, NEON uses video0
        device = image_source_config.get("device", "/dev/video0")
        deviceName = image_source_config.get("deviceName", "v4l2src")

        self.pipeline_config.add_plugin(PluginDefinition("v4l2src", [
            PluginArg("name", deviceName),
            PluginArg("device", device),
            PluginArg("num-buffers", 1)])) # may need to hard-code these?
        logger.debug("in _add_icam_image_source override_processing_pipeline="+str(override_processing_pipeline))
        logger.debug("in _add_icam_image_source image src processingPipeline="+str(image_source_config.get("processingPipeline")))
        if override_processing_pipeline or image_source_config.get("processingPipeline"):
           self.pipeline_config.add_plugin(override_processing_pipeline or image_source_config.get("processingPipeline"))
        else:
           self.pipeline_config.add_plugin(PluginDefinition("videoconvert", []))
        crop_config = image_source_config.get("imageCrop")
        if crop_config:
            self.pipeline_config.add_plugin(PluginDefinition("videocrop", [
                PluginArg("top", crop_config.get("top")),
                PluginArg("bottom", crop_config.get("bottom")),
                PluginArg("left", crop_config.get("left")),
                PluginArg("right", crop_config.get("right"))
            ]))
        logger.debug("building pipeline for icam")

    def _add_nvidia_csi_image_source(self, image_source_config, override_processing_pipeline: str = None):
        logger.debug("setup pipeline for nvidia csi image_source="+str(image_source_config))
        # default sensor_id for nvidia CSI camera
        sensor_id = image_source_config.get("device", "0")
        deviceName = image_source_config.get("deviceName", "nvarguscamerasrc")

        self.pipeline_config.add_plugin(PluginDefinition("nvarguscamerasrc", [
            PluginArg("sensor_id", sensor_id),
            PluginArg("num-buffers", 1)]))
        logger.debug("in _add_nvidia_csi_image_source override_processing_pipeline="+str(override_processing_pipeline))
        logger.debug("in _add_nvidia_csi_image_source image src processingPipeline="+str(image_source_config.get("processingPipeline")))
        if override_processing_pipeline or image_source_config.get("processingPipeline"):
           self.pipeline_config.add_plugin(override_processing_pipeline or image_source_config.get("processingPipeline"))
        else:
           # nvvidconv requires explicit output format specification
           self.pipeline_config.add_plugin(PluginDefinition("nvvidconv", []))
           self.pipeline_config.add_plugin(PluginDefinition("capsfilter caps=video/x-raw(memory:NVMM),format=I420"))
           self.pipeline_config.add_plugin(PluginDefinition("nvvidconv", []))
           self.pipeline_config.add_plugin(PluginDefinition("videoconvert", []))
        crop_config = image_source_config.get("imageCrop")
        if crop_config:
            self.pipeline_config.add_plugin(PluginDefinition("videocrop", [
                PluginArg("top", crop_config.get("top")),
                PluginArg("bottom", crop_config.get("bottom")),
                PluginArg("left", crop_config.get("left")),
                PluginArg("right", crop_config.get("right"))
            ]))
        logger.debug("building pipeline for nvidia csi")

    def _add_file_image_source(self, file_path):
        self.pipeline_config.add_plugin(PluginDefinition("filesrc",
                                                            [PluginArg("blocksize", -1),
                                                            PluginArg("location", f'"{file_path}"')
                                                            ]))
        self.pipeline_config.add_plugin(PluginDefinition("emexifextract"))
        self.pipeline_config.add_plugin(PluginDefinition("jpegdec",
                                                            [PluginArg("idct-method", 2)
                                                            ]))
        self.pipeline_config.add_plugin(PluginDefinition("videoconvert"))
        self.pipeline_config.add_plugin(PluginDefinition("videoflip",
                                                            [PluginArg("method", "automatic")
                                                              ]))     
        
    def _add_pre_processing_plugins(self):
        self.pipeline_config.add_plugin(PluginDefinition("capsfilter caps=video/x-raw,format=RGB"))

    def _add_inference_plugins(self):
        if get_is_triton():
            config_json = None
            # Read config_file_path and convert into string
            with open(self.em_agent_config_path, "r") as f:
                config = f.read()
                config_json = json.loads(config)
            if config_json:
                config_json["capture_id"] = self.capture_id
                dump = json.dumps(config_json).replace('"', '\\"')
                meta = f"\"{dump}\""
                self.pipeline_config.add_plugin(
                    PluginDefinition(
                        "emltriton",
                        [
                            PluginArg("model-repo", TRITON_MODEL_DIR),
                            PluginArg("server-path", TRITON_INSTALLATION_DIR),
                            PluginArg(
                                "model",
                                self.workflow_config.get("featureConfigurations")[0].get(
                                    "modelName"
                                ),
                            ),
                            PluginArg("metadata", meta),
                            PluginArg("correlation-id", self.capture_id),
                        ],
                    )
                )
        else:
            self.pipeline_config.add_plugin(
                PluginDefinition(
                    "eminfer",
                    [
                        PluginArg("name", "eminferX"),
                        PluginArg("mode", "2"),
                        PluginArg("tensor-source", "1"),
                        PluginArg("config", self.em_agent_config_path),
                        PluginArg(
                            "model-component",
                            self.workflow_config.get("featureConfigurations")[0].get("modelName"),
                        ),
                        PluginArg("confidence-watermark", "1"),
                    ],
                )
            )
    
    def _add_output_plugins(self, output_configurations):
        if output_configurations and not get_is_triton():
            self.pipeline_config.add_plugin(PluginDefinition("tee", [PluginArg("name", "t t.")]))
            self.pipeline_config.add_plugin(PluginDefinition("queue"))

            output_config_schema = OutputConfigurationSchema(many=True,
                                                            only=("pin", "pulseWidth", "signalType", "rule"))
            output_config_json = json.dumps(output_config_schema.dumps(output_configurations), separators=(',', ':'))
            self.pipeline_config.add_plugin(PluginDefinition("emoutputevent", [
                PluginArg("script-path", utils.get_dio_script_path()),
                PluginArg("config", output_config_json)
            ]))

            self.pipeline_config.add_plugin(PluginDefinition("fakesink t."))
            self.pipeline_config.add_plugin(PluginDefinition("queue"))
    
    def _add_post_processing_plugins(self):
        self._add_output_plugins(self.workflow_config.get("outputConfigurations", []))
        self.pipeline_config.add_plugin(PluginDefinition("jpegenc", [
            PluginArg("idct-method", 2),
            PluginArg("quality", 100)
        ]))

        if get_is_triton():
            w_path = self.workflow_config.get("workflowOutputPath")
            emlcapture_plugin_args = [
                PluginArg("buffer-message-id", f"file-target_{w_path}-jpg"),
                PluginArg("interval", 0),
                PluginArg(
                    "meta",
                    f"triton_inference_output_overlay:file-target_{w_path}-overlay.jpg,triton_inference_output_mask:file-target_{w_path}-mask.png,triton_inference_output_capture:file-target_{w_path}-jsonl,triton_inference_output_anomalous:{w_path}_is-anomalous,triton_inference_output_confidence:{w_path}_confidence",
                ),
            ]
            output_configurations = self.workflow_config.get("outputConfigurations", [])
            if output_configurations:
                output_config_schema = OutputConfigurationSchema(many=True,
                                                            only=("pin", "pulseWidth", "signalType", "rule"))
                configs = json.loads(output_config_schema.dumps(output_configurations))
                rules = []
                st = []
                pins = []
                pwms = []
                for oc in configs:
                    rules.append(oc["rule"])
                    st.append(oc["signalType"])
                    pins.append(str(oc["pin"]))
                    pwms.append(str(oc["pulseWidth"]))
                rules = ';'.join(rules)
                st = ';'.join(st)
                pins = ';'.join(pins)
                pwms = ';'.join(pwms)
                configs_s = '_'.join([rules,st,pins,pwms])
                emlcapture_plugin_args = [
                    PluginArg("buffer-message-id", f"file-target_{w_path}-jpg"),
                    PluginArg("interval", 0),
                    PluginArg(
                    "meta",
                    f"triton_inference_output_overlay:file-target_{w_path}-overlay.jpg,triton_inference_output_mask:file-target_{w_path}-mask.png,triton_inference_output_capture:file-target_{w_path}-jsonl,triton_inference_output_anomalous:gpio-target_{configs_s}",
                    ),
                ]
            self.pipeline_config.add_plugin(PluginDefinition("emlcapture", emlcapture_plugin_args))

        else:
            emdatacapture_plugin_args = [
                PluginArg("config", self.em_agent_config_path),
                PluginArg("aws-cred-source", "0"),
                PluginArg("target", "eminferX"),
                PluginArg("file-extension", "jpg"),
                PluginArg("capture-folder", self.workflow_config.get("workflowOutputPath")),
            ]
            if self.capture_id:
                emdatacapture_plugin_args.append(PluginArg("capture-id", self.capture_id))

            self.pipeline_config.add_plugin(
                PluginDefinition("emdatacapture", emdatacapture_plugin_args)
            )

        self.pipeline_config.add_plugin(PluginDefinition("fakesink"))
    
    def add_image_source(self, image_source, override_processing_pipeline: str = None, override_folder_source_file : str = None):
        self.image_source = image_source
        source_type = self.image_source.get("type")

        if source_type == ImageSourceType.CAMERA:
            image_source_config = self.image_source.get("imageSourceConfiguration", None)
            if not isinstance(image_source_config, dict):
                image_source_config = utils.convert_sqlalchemy_object_to_dict(image_source_config)
            self._add_camera_image_source(image_source_config, override_processing_pipeline)

        ## DD-18130: Add support for smart cameras
        elif source_type == ImageSourceType.ICAM:
            image_source_config = self.image_source.get("imageSourceConfiguration", None)
            logger.debug("icam image_source_config="+str(image_source_config))
            if not isinstance(image_source_config, dict):
                image_source_config = utils.convert_sqlalchemy_object_to_dict(image_source_config)
            self._add_icam_image_source(image_source_config, override_processing_pipeline)

        elif source_type == ImageSourceType.NVIDIA_CSI:
            image_source_config = self.image_source.get("imageSourceConfiguration", None)
            logger.debug("nvidia csi image_source_config="+str(image_source_config))
            if not isinstance(image_source_config, dict):
                image_source_config = utils.convert_sqlalchemy_object_to_dict(image_source_config)
            self._add_nvidia_csi_image_source(image_source_config, override_processing_pipeline)

        elif source_type == ImageSourceType.FOLDER:
            if override_folder_source_file:
                self._add_file_image_source(override_folder_source_file)
            else:
                file_path = captured_images_utils.get_oldest_image_file_path(self.image_source.get('location'))
                self._add_file_image_source(file_path)

        else:
            raise UnexpectedTypeException(f"Unexpected type: {source_type}", status_code=HTTP_500_INTERNAL_SERVER_ERROR)

        return self

    def add_inference(self, workflow_config: Workflow, capture_id: str = None):
        self.workflow_config = workflow_config
        self.capture_id = capture_id
        self.em_agent_config_path = utils.get_em_agent_config_path_for_stream(self.workflow_config.get("workflowId"))
 
        self._add_pre_processing_plugins()
        self._add_inference_plugins()
        self._add_post_processing_plugins()

        return self

    def build(self, is_preview=False, file_prefix: str = None, override_output_location: str = None):
        location = ''
        if not self.image_source and not self.workflow_config:
            return None
        if not self.workflow_config:
            if is_preview:
                filename = "{}-{}.jpg".format(constants.DEFAULT_IMAGE_OUTPUT_PREFIX, self.image_source.get("imageSourceId"))
                location = "{}/{}".format(constants.DEFAULT_IMAGE_SAVE_DIR_PATH, filename)
            else:
                unix_timestamp_ms = int(time.time() * 1000)
                prefix = f"{file_prefix}-" if file_prefix else ""
                filename = "{}{}.jpg".format(prefix, unix_timestamp_ms)
                if override_output_location is not None:
                    location = "{}/{}".format(override_output_location, filename)
                else:
                    location = "{}/{}".format(self.image_source.get("imageCapturePath"), filename)
            self.pipeline_config.add_plugin(PluginDefinition("jpegenc", [
                PluginArg("idct-method", 2),
                PluginArg("quality", 100)
            ]))
            self.pipeline_config.add_plugin(PluginDefinition("filesink", [PluginArg("location", location)]))
        return self.pipeline_config.build_pipeline_string(), location
