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

import base64
import os
import threading
import glob
import json
from sqlalchemy.orm import Session
from starlette import status

import numpy as np

from edge_ml1_p_camera_management.aravis_functions import get_input_file_from_pipeline
from exceptions.api.captured_images_exception import CapturedImageException
from gstreamer.gst_pipeline import GstPipelineManager
from gstreamer.pipeline_builder import GstPipelineBuilder
from model.image_source import ImageSource, ImageSourceType
from dda_triton.message_broker_client import MessageBrokerClient

from metrics.collector import Timer
from model.workflow import Workflow, WorkflowSchema
from resources.accessors.image_source_accessor import ImageSourceAccessor
from utils import constants, utils, inference_results_utils, captured_images_utils, dda_user_management_utils, dio_utils
from utils.get_is_triton import get_is_triton

import logging
logger = logging.getLogger(__name__)


class GstPipelineExecutor:
    def __init__(self):
        self.workflow_schema = WorkflowSchema()
        self.pipeline_manager = GstPipelineManager()
        self.image_source_accessor = ImageSourceAccessor()

        # Message Broker per thread of pipeline executor
        self.message_broker = MessageBrokerClient()

    def execute_image_source_pipeline(self, image_source: ImageSource, image_source_config_override: dict = {},
                                      is_preview: bool = False, file_prefix: str = None, frame_data = None,
                                      workflow_output_path: str = None):
        # TODO: improve expand imageSourceConfiguration in image source

        ## DD-18130: Add support for smart cameras
        image_source.set("imageSourceConfiguration", utils.convert_sqlalchemy_object_to_dict(image_source.get('imageSourceConfiguration')))

        if image_source_config_override:
            for param in ['gain', 'exposure', 'processingPipeline', 'imageCrop']:
                if image_source_config_override.get(param):
                    image_source.get('imageSourceConfiguration')[param] = image_source_config_override.get(param)

        image_source_dict = utils.convert_sqlalchemy_object_to_dict(image_source)
        pipeline_definition, capture_location = self._create_image_source_pipeline(image_source_dict, is_preview, file_prefix, workflow_output_path)
        logger.info(f"Created image processing pipeline {pipeline_definition}")
        img = ""
        with Timer(metric_name="ImageSourceExecutionTime"):
            self._run_pipeline(pipeline_definition, frame_data)

        if os.path.getsize(capture_location) > 0:
            img = utils.get_image_bytes_from_file(capture_location)
            # [DDS-141] Change ownership of output file to dda_admin_user dda_admin_group
            dda_user_management_utils.update_dda_user_file_permissions(capture_location)
        else:
            captured_images_utils.delete_image(capture_location)
            # Not enough data to conclude whether or not captured 0-byte images are because of our internal gstreamer pipeline or external camera configurations.
            # Returning error code 500 for now, can reconsider 422 if we get more data points.
            raise CapturedImageException("Captured image was corrupted and has been deleted.", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return {"image": img, "captureLocation": capture_location}

    def _create_image_source_pipeline(self, image_source, is_preview: bool = False, file_prefix: str = None, workflow_output_path: str = None):
        with Timer(metric_name="GstInitTime"):
            pipeline_builder = GstPipelineBuilder()

        with Timer(metric_name="ImageSourceStartupTime"):
            pipeline_definition, capture_location = pipeline_builder \
                .add_image_source(image_source) \
                .build(is_preview, file_prefix, workflow_output_path)

        logger.info("Created pipeline {}".format(pipeline_definition))
        
        return pipeline_definition, capture_location

    def _create_workflow_pipeline(self, workflow: Workflow, image_source, inference_capture_id, override_folder_source_file : str = None):
        pipeline_builder = GstPipelineBuilder()
        
        with Timer(metric_name="WorkflowStartupTime"):
            pipeline_definition, _ = pipeline_builder \
                .add_image_source(image_source, override_folder_source_file = override_folder_source_file) \
                .add_inference(workflow, inference_capture_id) \
                .build()

        logger.info("Created combined pipeline {}".format(pipeline_definition))
        
        return pipeline_definition
        
    # [DDS-141] Change ownership of output file to dda_admin_user dda_admin_group
    # find all generated files and change file permissions
    def _update_file_permissions(self, workflow, inference_capture_id):
        captured_files_prefix = workflow.get('workflowOutputPath') + "/" + inference_capture_id + "*"
        for _file in glob.glob(captured_files_prefix):
            dda_user_management_utils.update_dda_user_file_permissions(_file)

    def _run_pipeline(self, pipeline_definition, frame_data = None, latency_metrics = None):
        with Timer(metric_name="WorkflowExecutionTime"):
            return self.pipeline_manager.run_pipeline(pipeline_definition, frame_data, latency_metrics)
    
    def _cleanup_file_after_processing(self, pipeline_definition):
        input_file_name = get_input_file_from_pipeline(pipeline_definition)
        logger.info("Clean up processed image file: {}".format(input_file_name))
        captured_images_utils.delete_image(input_file_name)


    def _reset_digital_output(self, workflow_config: Workflow):
        output_configurations = workflow_config.get("outputConfigurations", [])
        if output_configurations:
            logger.info(f"Resetting digital outputs for workflow ID: {workflow_config.get('workflowId')}")
            for config in output_configurations:
                dio_utils.reset_output_pin(
                    int(config.get("pin")),
                    str(config.get("signalType"))
                )

    def _move_bad_folder_image_source(self, workflow_id : str, bad_image_filepath : str) -> str:
        bad_files_dir = os.path.join(constants.INFERENCE_RESULTS_DIR, workflow_id, "failed")
        if not os.path.exists(bad_files_dir):
            dda_user_management_utils.create_dda_user_directory(bad_files_dir)
        relocated_bad_jpg_image = os.path.join(bad_files_dir, os.path.basename(bad_image_filepath))
        os.rename(bad_image_filepath, relocated_bad_jpg_image)
        return relocated_bad_jpg_image

    def execute_workflow_pipeline(self, workflow: Workflow, db: Session, frame_data = None, latency_metrics = None):
        inference_capture_id = utils.generate_capture_id(workflow.get('workflowId'))
        image_source_id = workflow.get('imageSourceId')
        image_source_db = self.image_source_accessor.get_image_source(image_source_id, db)
        image_source_dict = utils.convert_sqlalchemy_object_to_dict(image_source_db)

        # Identify the oldest file if folder source
        oldest_jpg_image = None
        if image_source_dict.get("type") == ImageSourceType.FOLDER:
            oldest_jpg_image = captured_images_utils.get_oldest_image_file_path(image_source_dict.get('location'), False)
            if os.path.getsize(oldest_jpg_image) == 0:
                relocated_bad_jpg_image = self._move_bad_folder_image_source(workflow.get('workflowId'), oldest_jpg_image)
                raise CapturedImageException(f"Workflow execution failed due to source image file corruption. Source image file has been moved to {relocated_bad_jpg_image}", status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)

        try:
            w_path = workflow.get("workflowOutputPath")
            w_output_configs = workflow.get("outputConfigurations", [])

            # If folder based workflow, we'll override the file to use with the one we just selected. This prevents searching again and potentially using a different file than what we are prepared to clean up.
            # If non-folder based workflow, the override will be None and will be ignored.
            pipeline_definition = self._create_workflow_pipeline(workflow, image_source_dict, inference_capture_id, override_folder_source_file=oldest_jpg_image)

            ## DDS-267: Digital output reset at workflow start if latching mode is enabled (pulseWidth <= 0)
            ## This implementation has a limitation when two or more threads are concurrently executing workflow,
            ## which might impact the order of digital output.
            ## We will need to address this in the future with logical workflow relationships.
            self._reset_digital_output(workflow)

            ## Execute pipeline
            parsed_tags_dict = self._run_pipeline(pipeline_definition, frame_data, latency_metrics)
        except Exception as e:
            # If the pipeline fails for whatever reason, if it was a folder-based image source we want to move the source image file to make way for the next execution.
            if image_source_dict.get("type") == ImageSourceType.FOLDER:
                relocated_failed_image = self._move_bad_folder_image_source(workflow.get('workflowId'), oldest_jpg_image)
                # Append source image relocation message to the exception without changing its type or other parameters: https://stackoverflow.com/a/6062677
                e.args = (e.args[0] + f" : Source image file has been moved to {relocated_failed_image}",) + e.args[1:]
            raise e
        else:
            # On successful inference pipeline run, remove the source image file for folder-based image sources to make way for the next execution.
            if image_source_dict.get("type") == ImageSourceType.FOLDER:
                self._cleanup_file_after_processing(pipeline_definition)
                self._update_file_permissions(workflow, inference_capture_id)

            return inference_capture_id, parsed_tags_dict
