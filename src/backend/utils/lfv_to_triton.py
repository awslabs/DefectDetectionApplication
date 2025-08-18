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
import os
from utils.constants import LFV_MODEL_DIR_PATH,LFV_AGENT_GG_COMPONENT_NAME, UNARCHIVED_COMPONENTS_PATH, MODEL_PREFIX
from dda_triton.constants import TRITON_MODEL_DIR
import logging
from dda_triton.model_convertor import convert_to_triton_structure
logger = logging.getLogger(__name__)
from exceptions.api.triton_exceptions import GreengrassOperationException, TritonInternalServerException, TritonSetupException
from utils.gg_utils import restart_components, save_is_triton_value_to_file, list_gg_components, stop_running_component, un_archive_lfv_models
from dda_triton.model_convertor import clean_directory

def switch_to_triton():
    """
    This function lists the LFV models, shuts them all down, shuts down the LFV agent GG component, and finally sets the triton flag to True.
    """
    try:
        #get a list of model components which are running and stop them
        logger.info("-"*70)
        logger.info("Starting the Triton Switch")
        logger.info("1. Getting the list of LFV related components which are Running")
        running_model_component = list_gg_components(return_stopped_components=False)
        components_stopped = []
        if len(running_model_component):
            for model_component in running_model_component:
                if model_component==LFV_AGENT_GG_COMPONENT_NAME:
                    logger.info("Stopping LFV edge agent ")
                    if stop_running_component(str(model_component)):
                        logger.info("LFV edge agent stopped.")
                    else:
                        raise GreengrassOperationException(
                            status_code=400,
                            message=f"Error while stopping LFV edge agent"
                        )
                else:
                    logger.info(f"Stopping model :{model_component}")
                    response = stop_running_component(str(model_component))
                    if response is not True:
                        logger.info(f"Error while stopping GG component :{model_component}, restarting stopped components")
                        components_stopped.append(LFV_AGENT_GG_COMPONENT_NAME)
                        restart_components(components_stopped)
                        raise GreengrassOperationException(
                            status_code=400,
                            message=f"Error while stopping GG component :{model_component}, restarted stopped components"
                        )
                    logger.info(f"Model stopped: {model_component}")
                    components_stopped.append(model_component)
    
            logger.info("2. LFV related models and agent stopped, moving ahead")
        else:
            logger.info("2. No Running model to stop, moving ahead")
        logger.info("checking if the triton model repository exists")
        if not create_triton_model_repo():
            logger.info("Triton model repository exists")
        logger.info("Cleaning the unarchive directory")
        clean_model_unarchive_directory()
        logger.info("Cleaned the model unarchive directory")
        # Set the is_triton environment variable to True
        logger.info("3. Setting the flag to Triton")
        os.environ["is_triton"] = "True"
        log_is_triton = os.environ["is_triton"]
        logger.info(f"Triton flag value is set to {log_is_triton}")

        # Call the function to save the is_triton value to a file
        save_is_triton_value_to_file(is_triton="True")
        logger.info("Triton flag value saved to file successfully")
        logger.info("4. Unarchiving the models which were stopped")
        un_archive_lfv_models()
        logger.info("5. Converting the models into Triton format")
        convert_models()
        logger.info("Models converted successfully")
        logger.info("Switched to Triton")
        logger.info("-"*70)
        logger.info("Restarting the container")
        restart_components([LFV_AGENT_GG_COMPONENT_NAME])
        return True
    except GreengrassOperationException as gg_error:
        raise gg_error
    except Exception as e:
        raise TritonInternalServerException(
                status_code=500,
                detail=f"Error occured while setting up the triton model repository : {e}"
            )


def convert_models():
    """
    This function converts the models to the Triton format
    """
    try:
        suffix='model_component'
        no_lfv_model_found=True
        models_to_convert={}
        for entry in os.listdir(LFV_MODEL_DIR_PATH):
            # Construct the full path of the entry
            model_dir = os.path.join(LFV_MODEL_DIR_PATH, entry)
            # Check if the entry is a directory and has the specified prefix
            if os.path.isdir(model_dir) and entry.startswith(MODEL_PREFIX):
                no_lfv_model_found=False
                lfv_model_dir=""
                model_version="1"
                logger.info(f"model_dir is : {model_dir}")
                model_version_found=False
                for root, dirs, files in os.walk(model_dir):
                    if model_version_found:
                        break
                    for dir_name in dirs:
                        if dir_name.endswith(suffix):
                            logger.info(f"root_dir is {root}")
                            logger.info(f"dir is {dir_name}")
                            lfv_model_dir=os.path.join(root, dir_name)
                            parts = root.split('/')
                            model_version = parts[-1]
                            model_version = str(model_version.split('.')[0])
                            models_to_convert[entry] = root
                            logger.info(f"models being converted is: {models_to_convert}")
                            model_version_found=True
                            break
                logger.info(f"LFV model dir is:{lfv_model_dir}")
                if convert_to_triton_structure(model_repo_dir=TRITON_MODEL_DIR, deployed_model_path=lfv_model_dir,model_name=entry, model_version=model_version):
                    logger.info(f"Model conversion for model {entry} to Triton format completed successfully")
                else:
                    raise TritonInternalServerException(
                        status_code=500,
                        detail=f"Model conversion for model {entry} to Triton format failed"
                    )
        if no_lfv_model_found:
            logger.info(f"No model found for conversion in directory {LFV_MODEL_DIR_PATH}")
        return True
    except TritonInternalServerException as triton_server_exception:
        raise triton_server_exception
    except Exception as e:
        raise TritonInternalServerException(
                status_code=500,
                detail=f"An error occurred while converting models to Triton format: {e}"
            )

def create_triton_model_repo():
    """
    Creates the triton model repository if it doesn't exist.
    """
    if not os.path.exists(TRITON_MODEL_DIR):
        try:
            os.makedirs(TRITON_MODEL_DIR)
            return True
        except OSError as e:
            raise TritonSetupException(
                status_code=500,
                detail=f"Error creating directory: {e}"
            )
    return False

def clean_model_unarchive_directory():
    """
        Cleans the lfv unarchived model repository before converting 
        to ensure new artifacts are unarchived.
    """
    try:
        for root, dirs, files in os.walk(UNARCHIVED_COMPONENTS_PATH):
            for dir in dirs:
                if dir.startswith(MODEL_PREFIX):
                    model_unarchived_path = os.path.join(root, dir)
                    if clean_directory(model_unarchived_path):
                        logger.info(f"Cleaned the unarchived model directory {model_unarchived_path}")
                    else:
                        raise  TritonSetupException(
                        status_code=500,
                        detail=f"Error while cleaning unarchive directory {model_unarchived_path}"
                    )
    except Exception as e:
        raise TritonSetupException(
                status_code=500, 
                detail="Error while cleaning unarchive directory {e}"
                )