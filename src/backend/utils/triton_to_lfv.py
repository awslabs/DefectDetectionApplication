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
from dda_triton.constants import TRITON_MODEL_DIR
import logging
from dda_triton.model_convertor import clean_directory
from utils.gg_utils import restart_components, save_is_triton_value_to_file, list_gg_components
logger = logging.getLogger(__name__)
from exceptions.api.triton_exceptions import TritonInternalServerException

def switch_to_lfv():
    """
    This function lists the LFV models, restart them all , sets the triton flag to True and finally restarts the LFV agent GG component.
    """
    try:

        logger.info("-"*70)
        logger.info("Starting the switch to LFV")
        # Set the is_triton environment variable to False
        logger.info("1. Setting the flag to LFV")
        os.environ["is_triton"] = "False"
        log_is_triton = os.environ["is_triton"]
        logger.info(f"Triton flag value is set to {log_is_triton}")

        # Call the function to save the is_triton value to a file
        save_is_triton_value_to_file(is_triton="False")
        logger.info("Triton flag value false saved to file successfully")
        logger.info("2. Cleaning the triton_models repository")
        clean_triton_model_repo()
        logger.info("Triton models directory cleaned successfully")
        
        #get a list of model components which are stopped and re-start them
        logger.info("3. Getting the list of LFV related components which are STOPPED")
        stopped_model_components = list_gg_components(return_stopped_components=True)
        if len(stopped_model_components):
            restart_components(stopped_model_components)
            logger.info("3. Models/Edge Agent restarted, moving ahead")
        else:
            logger.info("3. No stopped model to re-start, moving ahead")        
        logger.info("-"*70)
        return True
    except Exception as e:
        raise TritonInternalServerException(
                status_code=500,
                detail=f"Error occured while switching to LFV : {e}"
            )

def clean_triton_model_repo(model_dir=TRITON_MODEL_DIR):
    """
    This function cleans the Triton model repository
    """
    try:
        logger.info("Cleaning up the Triton directory")
        if os.path.isdir(model_dir):
            for model in os.listdir(model_dir):
                model_path = os.path.join(model_dir, model)
                if clean_directory(model_path):
                    logger.info("Triton Directory cleaned ")
                else:
                    raise TritonInternalServerException(
                            status_code=500,
                            detail=f"Cleanup for Triton model repository failed."
                        )
        else:
            logger.info(f"Triton model_directory : {model_dir} does not exist or is empty, moving forward")
        return True
    except TritonInternalServerException as internal_server_exception:
        raise internal_server_exception
    except Exception as e:
        raise TritonInternalServerException(
                status_code=500,
                detail=f"Error occured while cleaning up the triton model repository : {e}"
            )
