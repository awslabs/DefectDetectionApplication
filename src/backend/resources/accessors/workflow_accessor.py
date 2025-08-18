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
from marshmallow import ValidationError
from fastapi import HTTPException
from sqlalchemy.orm import Session

import os
import time
import awsiot.greengrasscoreipc.model as model

from dao.sqlite_db import workflow_dao, image_source_dao, input_configuration_dao, output_configuration_dao
from model.image_source import ImageSourceType
from .image_source_accessor import ImageSourceAccessor
from .input_configuration_accessor import InputConfigurationAccessor
from .output_configuration_accessor import OutputConfigurationAccessor
from model.workflow import WorkflowSchema, Workflow
import utils.digital_input_process_manager as digital_input_mgr
import utils.digital_input_thread_manager as digital_input_thread_mgr
from utils.common import DIOProcessHealthStatusEnum
import utils.utils as utils
from utils import constants, dda_user_management_utils
from utils.camera_manager import disconnect_camera
from utils.feature_configs_utils import get_default_configs_lfv
from utils.get_is_triton import get_is_triton

import logging
logger = logging.getLogger(__name__)

class WorkflowAccessor:
    def __init__(self, iot_shadow_accessor):
        self.schema = WorkflowSchema()
        self.primary_key = 'workflowId'

        self.iot_shadow_accessor = iot_shadow_accessor
        self.input_configuration_accessor = InputConfigurationAccessor()
        self.output_configuration_accessor = OutputConfigurationAccessor()
        self.image_source_accessor = ImageSourceAccessor()

    def create_workflow(self, data, db: Session):
        # Workflow is created on SaaS app. We create an empty workflow with only the workflow ID and no configs
        # so we have no schema validation here. Necessary configurations are added through update_workflow.

        try:
            if self.primary_key not in data:
                raise HTTPException(
                    status_code=500,
                    detail=f"Server cannot create workflow. Error: 'No primary key provided.'",
                )

            workflow_id = data[self.primary_key]

            if workflow_dao.get_workflow(db, workflow_id):
                raise HTTPException(
                    status_code=500,
                    detail=f"Server cannot create workflow. Error 'Workflow {workflow_id} already exists.'",
                )

            data["name"] = f"workflow_{workflow_id}"

            current_timestamp = int(time.time() * 1000)
            data["creationTime"] = current_timestamp
            data["lastUpdatedTime"] = current_timestamp
            data["workflowOutputPath"] = self.__create_folder(data[self.primary_key])

            workflow_dao.create_workflow(db, data)
            logger.info(f"Stored workflow with id: {workflow_id}")

            return data[self.primary_key]

        except Exception as e:
            raise e

    def list_workflows_with_image_sources(self, db: Session):
        workflows = workflow_dao.list_workflows(db)
        workflow_detail_list = []
        for workflow in workflows:
            workflow_dict = self.get_workflow_with_default_config(workflow.workflowId, db)
            workflow_detail_list.append(workflow_dict)
        return workflow_detail_list
    
    def list_workflows_by_camera(self, camera_id, db:Session):
        image_souce_ids = self.image_source_accessor.list_image_source_ids_by_camera(camera_id, db)
        workflow_ids = workflow_dao.list_workflows_ids_by_image_sources(image_souce_ids, db)
        
        workflows = []
        for workflow_id in workflow_ids:
            workflow_dict = self.get_workflow_with_default_config(workflow_id, db)
            workflows.append(workflow_dict)
        return workflows



    def list_workflows(self, db: Session):
        return workflow_dao.list_workflows(db)

    def update_workflow(self, data, db: Session):
        try:
            if self.primary_key not in data:
                raise HTTPException(
                    status_code=400,
                    detail=f"The server can't find the workflow. Error: 'The workflow {self.primary_key} doesn't exist'. Check the workflow ID and try again.",
                )

            workflow_id = data[self.primary_key]
            original_workflow = workflow_dao.get_workflow(db, workflow_id)
            if not original_workflow:
                raise HTTPException(
                    status_code=404,
                    detail=f"The server can't find the workflow. Error: 'The workflow {workflow_id} doesn't exist'. Check the workflow ID and try again.",
                )

            data["lastUpdatedTime"] = int(time.time() * 1000)
            data["creationTime"] = original_workflow.creationTime

            # Supress any changes to workflow output path or provide generated output path
            data["workflowOutputPath"] = original_workflow.workflowOutputPath

            if "imageSources" not in data:
                raise ValidationError("Image sources not provided")
            data["imageSources"] = self.__get_image_sources(data["imageSources"], db)
            data["imageSourceId"] = data["imageSources"][0]["imageSourceId"]
            del data["imageSources"]

            # DIO only with inputConfiguration
            if original_workflow.inputConfigurations:
                self.__clear_digital_input_process(self.get_workflow_by_id(workflow_id, db))

            if "inputConfigurations" in data:
                data["inputConfigurations"] = self.__create_input_configurations(
                    data["inputConfigurations"], db
                )

            if "outputConfigurations" in data:
                data["outputConfigurations"] = self.__create_output_configurations(
                    data["outputConfigurations"], db
                )

            result = self.schema.load(data)
            workflow_dao.update_workflow(
                db, self.schema.dump(result), data[self.primary_key]
            )
            logger.info(f"Stored workflow with id: {workflow_id}")

            # Pass workflow object to digital input process
            if result.get("inputConfigurations"):
                workflow = self.get_workflow_by_id(workflow_id, db)
                if not get_is_triton():
                    digital_input_mgr.create_digital_input_process(workflow)
                else:
                    digital_input_thread_mgr.create_digital_input_thread(workflow)
            # Copy EM Agent config created during bootstrap, update the output path and save it for this stream id.
            em_agent_config_file_path = utils.create_em_agent_config(result)

            return getattr(result, self.primary_key)

        except ValidationError as err:
            logger.error(err.messages)
            raise HTTPException(
                status_code=400,
                detail=f"The server can't update the workflow. Error: '{err.messages}'. Check the error message and try again.",
            )

    def delete_workflow(self, workflow_id, db: Session):
        workflow = self.get_workflow_by_id(workflow_id, db)

        # Clear out any existing input processes.
        if workflow.get("inputConfigurations"):
            camera_id = self.get_camera_id(workflow, db)
            disconnect_camera(camera_id)
            
            self.__clear_digital_input_process(workflow)

        workflow_dao.delete_workflow(db, workflow_id)

    def get_workflow_by_id(self, workflow_id, db: Session):
        original_workflow = workflow_dao.get_workflow(db, workflow_id)

        if not original_workflow:
            raise HTTPException(
                status_code=404,
                detail=f"The server can't find the workflow. Error: 'The workflow {workflow_id} doesn't exist'. Check the workflow ID and try again.",
            )
        else:
            workflow_dict = utils.convert_sqlalchemy_object_to_dict(original_workflow)
            if workflow_dict["imageSources"]:
                workflow_dict["imageSources"] = [workflow_dict["imageSources"]]
            return workflow_dict
        
    def get_workflow_with_default_config(self, workflow_id, db: Session):
        workflow_dict = self.get_workflow_by_id(workflow_id, db)
        if workflow_dict.get("featureConfigurations"):
            model_id = workflow_dict["featureConfigurations"][0].get("modelName")
            if model_id:
                try:
                    workflow_dict["featureConfigurations"][0]["defaultConfiguration"] = get_default_configs_lfv(model_id)
                except model.ResourceNotFoundError as err:
                    logger.info(f"Model {model_id} was removed, model component not found, ignore featching model metadata")
                    workflow_dict["featureConfigurations"][0]["defaultConfiguration"] = {}
                    pass
        return workflow_dict


    def retry_dio_workflow(self, workflow_id, db: Session):
        try:
            current_workflow_dict = self.get_workflow_by_id(workflow_id, db)
            if not current_workflow_dict.get("inputConfigurations"):
                raise HTTPException(
                    status_code=400,
                    detail=f"The server can't restart the workflow. Error: 'The workflow {workflow_id} doesn't have any input configurations'.",
                ) 
            self.__clear_digital_input_process(current_workflow_dict)    

            # TODO add new property reset time to reflect restart
            if not get_is_triton():
                digital_input_mgr.create_digital_input_process(current_workflow_dict)
            else:
                digital_input_thread_mgr.create_digital_input_thread(current_workflow_dict)
            logger.info(f"Restart workflow {workflow_id} successfully.")
            
            return workflow_id
        except Exception as e:
            logger.error(f"Failed to restart workflow: {workflow_id} with error {e}")
            raise HTTPException(
                status_code=500,
                detail=f"The server can't restart the workflow {workflow_id}. Error: '{e}'. Check the error message and try again.",
            )


    def check_workflow_health(self, workflow: Workflow) -> DIOProcessHealthStatusEnum:
        workflow_id = workflow.get("workflowId")
        # check if process is running
        if not get_is_triton():
            if not digital_input_mgr.is_process_running(workflow_id):
                raise HTTPException(
                    status_code=500,
                    detail=f"The server cannot get the latest workflow health status for {workflow_id}. \
                    The digital input process is not running. \
                    Please check the logs for more information."
                )
        else:
            if not digital_input_thread_mgr.is_thread_running(workflow_id):
                raise HTTPException(
                    status_code=500,
                    detail=f"The server cannot get the latest workflow health status for {workflow_id}. \
                    The digital input thread is not running. \
                    Please check the logs for more information."
                )

        # get health status
        report = None
        if not get_is_triton():
            report = digital_input_mgr.get_dio_process_health_report(workflow_id)
        else:
            report = digital_input_thread_mgr.get_dio_thread_health_report(workflow_id)
        if not report:
            raise HTTPException(
                status_code=500,
                detail=f"The server cannot get the workflow health status for {workflow_id}. \
                    Error: 'Unable to find get health report for workflow.",
        )

        if report.get("status") == DIOProcessHealthStatusEnum.ERROR:
            ## TODO: Handle different exception types and provide meaningful error messages
            error = report.get('error_type')
            raise HTTPException(
                status_code=500,
                detail=f"Error occured in the digital input monitor process for {workflow_id}. Error: {str(error)}.",
            )
        return report.get("status")


    def __create_folder(self, workflow_id):
        folder_path = constants.INFERENCE_RESULTS_DIR + "/" + workflow_id
        return dda_user_management_utils.create_dda_user_directory(folder_path)

    def __get_image_sources(self, image_sources_id_only, db: Session):
        final_image_sources = []

        for image_src in image_sources_id_only:
            retrieved_image_source = image_source_dao.get_image_source(
                db, image_src["imageSourceId"]
            )

            if not retrieved_image_source:
                raise ValidationError(f"Image source {image_src['imageSourceId']} does not exist")

            image_source = utils.convert_sqlalchemy_object_to_dict(retrieved_image_source)

            # If image source was created with no config we will load an empty config {} that
            # will fail schema validation so it needs to be deleted.
            if not image_source["imageSourceConfiguration"]:
                del image_source["imageSourceConfiguration"]

            final_image_sources.append(image_source)

        return final_image_sources

    def __create_input_configurations(self, input_configurations, db: Session):
        ret_input_configurations = []

        for input_configuration_data in input_configurations:
            input_config_id = self.input_configuration_accessor.create_input_configuration(
                db, input_configuration_data
            )
            input_config = input_configuration_dao.get_input_cfg(db, input_config_id)
            input_config_dict = utils.convert_sqlalchemy_object_to_dict(input_config)
            ret_input_configurations.append(input_config_dict)

        return ret_input_configurations

    def __clear_digital_input_process(self, workflow):
        if not get_is_triton():
            digital_input_mgr.terminate_digital_input_task(workflow)
        else:
            digital_input_thread_mgr.terminate_digital_input_task_thread(workflow)

    def __create_output_configurations(self, output_configurations, db: Session):
        ret_output_configurations = []

        for output_configuration_data in output_configurations:
            output_config_id = self.output_configuration_accessor.create_output_configuration(
                db, output_configuration_data
            )
            output_config = output_configuration_dao.get_output_cfg(db, output_config_id)
            output_config_dict = utils.convert_sqlalchemy_object_to_dict(output_config)
            ret_output_configurations.append(output_config_dict)

        return ret_output_configurations



    # TODO: The deletion to be handled for a camera source when we scale to api and preview so this is to be removed
    def get_camera_id(self, workflow:Workflow,  db: Session):
        image_source_id = workflow.get('imageSourceId')
        image_source_db = self.image_source_accessor.get_image_source(image_source_id, db)
        image_source = utils.convert_sqlalchemy_object_to_dict(image_source_db)
        camera_id = image_source.get('cameraId')

        return camera_id