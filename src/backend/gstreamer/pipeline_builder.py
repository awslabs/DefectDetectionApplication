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
        logger.warning(f"NVIDIA CSI SOURCE CONFIG DEBUG: Using file-based capture from host service")

        # Write gain, exposure, and crop settings to the host service config
        # file. The write is factored into workflow_engine.csi_capture so the
        # deployed-workflow executor writes the identical config
        # (csi-icam-input-nodes Requirement 7.1).
        from workflow_engine import csi_capture

        csi_capture.write_csi_config(
            gain=image_source_config.get("gain", csi_capture.DEFAULT_GAIN),
            exposure=image_source_config.get(
                "exposure", csi_capture.DEFAULT_EXPOSURE),
            crop=image_source_config.get("imageCrop"),
        )

        # Nvidia CSI uses file-based capture from host service
        # The host service continuously captures to /aws_dda/nvidia-csi-capture/latest.jpg
        self._add_file_image_source(csi_capture.CSI_LATEST_JPG)
        logger.debug("building pipeline for nvidia csi using file source")

    def _add_file_image_source(self, file_path):
        if self._is_jp6():
            # JetPack 6 ONLY. The Neo model's bundled libdlr.so (loaded in-process
            # via emltriton) brings its own libjpeg that interposes the system
            # libjpeg GStreamer's jpegdec uses, so jpegdec dies with "Improper
            # call to JPEG library in state 205" once a model is loaded. The
            # hardware nvv4l2decoder avoids libjpeg but mis-decodes these iPhone
            # JPEGs (stride/MPF issues -> visibly distorted output). The robust
            # path is to decode the JPEG in Python with Pillow (its own
            # libjpeg-turbo, immune to the collision; verified correct in-process
            # with DLR loaded), bake in EXIF orientation, and stage a PNG that
            # the pipeline reads via pngdec (libpng, not libjpeg). This drops
            # emexifextract/jpegparse/videoflip since orientation is already
            # applied. Gated to JP6; JP4/JP5/x86 keep the jpegdec path.
            png_path = self._stage_decoded_png(file_path)
            self.pipeline_config.add_plugin(PluginDefinition("filesrc",
                                                                [PluginArg("blocksize", -1),
                                                                PluginArg("location", f'"{png_path}"')
                                                                ]))
            self.pipeline_config.add_plugin(PluginDefinition("pngdec"))
            self.pipeline_config.add_plugin(PluginDefinition("videoconvert"))
            return

        self.pipeline_config.add_plugin(PluginDefinition("filesrc",
                                                            [PluginArg("blocksize", -1),
                                                            PluginArg("location", f'"{file_path}"')
                                                            ]))
        self.pipeline_config.add_plugin(PluginDefinition("emexifextract"))
        # jpegparse delimits each JPEG into a clean SOI->EOI frame before jpegdec
        # (required on GStreamer 1.20 to avoid intermittent "no valid frames").
        self.pipeline_config.add_plugin(PluginDefinition("jpegparse"))
        self.pipeline_config.add_plugin(PluginDefinition("jpegdec",
                                                            [PluginArg("idct-method", 2)
                                                            ]))
        self.pipeline_config.add_plugin(PluginDefinition("videoconvert"))
        self.pipeline_config.add_plugin(PluginDefinition("videoflip",
                                                            [PluginArg("method", "automatic")
                                                              ]))     

    @staticmethod
    def _is_jp6() -> bool:
        """True when running on the JetPack 6 LocalServer variant. Detected via
        the LocalServer component path env (contains 'JP6'), the same signal
        endpoints/system.py uses to report the LocalServer version."""
        import os
        path = os.environ.get("LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH", "")
        return "JP6" in path

    @staticmethod
    def _stage_decoded_png(file_path: str) -> str:
        """Decode a JPEG to a temp PNG using Pillow, applying EXIF orientation.

        Used by the JP6 file source to avoid GStreamer's libjpeg-based jpegdec
        (which collides with the model's libdlr.so libjpeg) and the hardware
        nvv4l2decoder (which mis-decodes these JPEGs). Pillow uses its own
        libjpeg-turbo, which decodes correctly even with DLR loaded in-process.
        Returns the PNG path; falls back to the original file on any failure.
        """
        import os
        try:
            from PIL import Image, ImageOps
            png_path = f"{file_path}.dda_decoded.png"
            with Image.open(file_path) as im:
                im = ImageOps.exif_transpose(im.convert("RGB"))
                im.save(png_path)
            return png_path
        except Exception as e:
            logger.error(f"JP6 PNG staging failed for '{file_path}', falling back to original: {e}")
            return file_path
        
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
            logger.warning(f"BUILD DEBUG: is_preview={is_preview}, imageCapturePath={self.image_source.get('imageCapturePath')}, imageSourceId={self.image_source.get('imageSourceId')}")
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
            logger.warning(f"BUILD DEBUG: Final location={location}")
            self.pipeline_config.add_plugin(PluginDefinition("jpegenc", [
                PluginArg("idct-method", 2),
                PluginArg("quality", 100)
            ]))
            self.pipeline_config.add_plugin(PluginDefinition("filesink", [PluginArg("location", location)]))
        return self.pipeline_config.build_pipeline_string(), location
