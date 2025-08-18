
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
from awsiot.greengrasscoreipc.model import StopComponentRequest, ListComponentsRequest, RestartComponentRequest
from utils.constants import GG_IPC_FUTURE_TIMEOUT,LFV_AGENT_GG_COMPONENT_NAME, DDA_GG_COMPONENT_NAME_PREFIX
import logging
logger = logging.getLogger(__name__)
from exceptions.api.triton_exceptions import GreengrassOperationException, FileSaveException, UnarchiveFailureException
from fastapi import HTTPException
import json
import zipfile
from dda_triton.model_convertor import clean_directory
import os
import utils.constants as constants
#TODO create a common ipc_clinet for gg related functions: https://issues.amazon.com/issues/DD-19576
def restart_components(components_to_restart):
    ipc_client = awsiot.greengrasscoreipc.connect()
    for component in components_to_restart:
        logger.info(f"Re-starting component :{component}")
        restart_component_request = RestartComponentRequest()
        restart_component_request.component_name = component
        restart_component_operation = ipc_client.new_restart_component()
        restart_component_operation.activate(restart_component_request)
        restart_component_future = restart_component_operation.get_response()
        restart_component_response = restart_component_future.result(GG_IPC_FUTURE_TIMEOUT)
        if restart_component_response.restart_status == "SUCCEEDED":
            logger.info(f"Component {component} restarted successfully")
        else:
            raise GreengrassOperationException(
                            status_code=400,
                            message=f"Error while re-starting GG component :  {component}, due to : {restart_component_response.message}"
                        )
    ipc_client.close()

#TODO investigate usage of boolean in file directly: https://issues.amazon.com/issues/DD-19577
def save_is_triton_value_to_file(is_triton="False"):
    """
    This function saves the value of the is_triton environment variable to a file.
    """
    from utils.constants import IS_TRITON_FILE_PATH

    try:
        logger.info("Saving the flag value in file")
        is_triton_value = {"is_triton": is_triton}
        json_triton = json.dumps(is_triton_value)
        with open(IS_TRITON_FILE_PATH, "w") as f:
            f.write(json_triton)
        return True
    except OSError as e:
        raise FileSaveException(
                            status_code=400,
                            detail=f"An error occurred while saving is_triton value to file: {e}"
                        )
    except Exception as e:
        raise HTTPException(
                            status_code=400,
                            detail=f"An error occurred while saving is_triton value to file: {e}"
                        )


def list_gg_components(return_stopped_components=False, return_dda_components=False):
    """
    This function lists the LFV models and LFV edge agent GG components inluding their state
    """
    component_list=[]
    # Create an IPC client
    ipc_client = awsiot.greengrasscoreipc.connect()
    logger.info("Created the ipc client ")
    list_components_request = ListComponentsRequest()
    list_components_operation = ipc_client.new_list_components()
    list_components_operation.activate(list_components_request)
    list_components_future = list_components_operation.get_response()
    list_components_response = list_components_future.result(GG_IPC_FUTURE_TIMEOUT)
    return_edge_agent = False
    for component in list_components_response.components:
        #models
        if component.component_name.startswith("model"):
            if return_stopped_components:
                #return STOPPED components
                if component.state in ["STOPPED", "FINISHED"]:
                    logger.info(f"Component Name: {component.component_name}")
                    logger.info(f"Component Version: {component.version}")
                    logger.info(f"Component State: {component.state}")
                    component_list.append(component.component_name)
                    logger.info(f"{component.component_name} is in STOPPED state")
                else:
                    logger.info(f"{component.component_name} is in {component.state} state")
                
            #return STARTING and RUNNING components
            else:
                if component.state in ["RUNNING", "STARTING"]:
                    logger.info(f"Component Name: {component.component_name}")
                    logger.info(f"Component Version: {component.version}")
                    logger.info(f"Component State: {component.state}")
                    component_list.append(component.component_name)
                    logger.info(f"{component.component_name} is in Running state")
                else:
                    logger.info(f"{component.component_name} is in {component.state} state")
        #dda-components
        if return_dda_components:
            if component.component_name.startswith(DDA_GG_COMPONENT_NAME_PREFIX):
                logger.info(f"Component Name: {component.component_name}")
                logger.info(f"Component Version: {component.version}")
                logger.info(f"Component State: {component.state}")
                component_list.append(component.component_name)
        #LFV edge agent
        if component.component_name == LFV_AGENT_GG_COMPONENT_NAME:
            return_edge_agent = True
            
    #Append LFV edge agent at the end
    if return_edge_agent:
        component_list.append(LFV_AGENT_GG_COMPONENT_NAME)
    ipc_client.close()
    return component_list

def stop_running_component(component_name):
    """
    This function stops the GG component provided to the function as the argument
    """
    # Create an IPC client
    ipc_client = awsiot.greengrasscoreipc.connect()

    stop_component_request = StopComponentRequest()
    stop_component_request.component_name = component_name
    stop_component_operation = ipc_client.new_stop_component()
    stop_component_operation.activate(stop_component_request)
    stop_component_future = stop_component_operation.get_response()
    stop_component_response = stop_component_future.result(GG_IPC_FUTURE_TIMEOUT)
    ipc_client.close()
    if stop_component_response.stop_status == "SUCCEEDED":
        logger.info(f"Component {component_name} stopped successfully")
        return True
    else:
        logger.info(f"Component {component_name} stop failed: {stop_component_response.message}")
        return False


def un_archive_lfv_models(archived_dir=constants.ARCHIVED_COMPONENTS_PATH):
    suffix = "greengrass_model_component.zip"
    try:
        for root, dirs, files in os.walk(archived_dir):
            for file in files:
                if file.endswith(suffix):
                    logger.info(f"model root_dir is {root}")
                    logger.info(f"zipfile  is {file}")
                    lfv_model_dir=os.path.join(root, file)
                    logger.info(f"lfv_model_dir is {lfv_model_dir}")
                    destination_folder=root.replace("artifacts", "artifacts-unarchived")
                    clean_directory(destination_folder)
                    zip_name = os.path.splitext(file)[0]
                    destination_folder = os.path.join(destination_folder, zip_name)
        
                    # Create the destination folder if it doesn't exist
                    os.makedirs(destination_folder, exist_ok=True)
                    logger.info(f"destination_folder is {destination_folder}")
                    
                    with zipfile.ZipFile(lfv_model_dir, 'r') as zip_ref:
                        zip_ref.extractall(destination_folder)
                    logger.info(f"Extraction finished for model_path : {root}")
    except Exception as e:
        logger.info(f"Failed to unarchieve the model due to {e}")
        raise UnarchiveFailureException(status_code=500, detail=f"{e}")
          