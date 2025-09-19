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

import venv
import os
import sys
import logging
import subprocess
from exceptions.api.triton_exceptions import TritonSetupException
from dda_triton.constants import DDA_ROOT_FOLDER, DDA_TRITON_FOLDER

logger = logging.getLogger(__name__)

# 082925 - ryanv@ disable venv because install_greengrass.sh installs base OS
# TODO move all of this init code inside the backend container to avoid dep issues
def create_virtual_env(
    #env_name="gg_venv",
    #venv_dir="/aws_dda/greengrass/v2/work/aws.edgeml.dda.LocalServer/",
    python_path = "/usr/local/bin/python3",
    requirements_file="/dda_triton/model_conversion_requirements.txt",
):
    try:
        #env_path = os.path.join(venv_dir, env_name)
        #if os.path.exists(env_path):
        #    logger.info(f"Virtual environment '{env_name}' already exists at {env_path}")
        #else:
        #    venv.create(env_path, with_pip=True)
        #    logger.info(f"Virtual environment '{env_name}' created at {env_path}")

        if os.path.exists(requirements_file):
            #[python_path, "-m", "pip", "install", "-r", requirements_file]
            installcommand = python_path + " -m pip install -r " + requirements_file
            print("install command="+str(installcommand),file=sys.stderr)
            subprocess.check_call(installcommand, shell=True)
            logger.info(f"Dependencies from '{requirements_file}' installed successfully.")
        else:
            logger.error(
                f"No model_conversion_requirements.txt file found at {requirements_file}. Skipping dependency installation."
            )
    except Exception as e:
        logger.error(f"Exception caught while setting up model phython requirements: {e}")


def cp_model_conversion_files():
    try:
        import shutil

        destination_folder_dda_triton = DDA_TRITON_FOLDER
        destination_folder_aws_dda = DDA_ROOT_FOLDER
        source_folder = "/dda_triton/"
        files_to_copy_to_dda_triton = [
            "constants.py",
            "model_config_pb2.py",
            "model_autostart_utils.py",
        ]
        files_to_copy_to_aws_dda = ["model_convertor.py", "convert_model_cleanup.py","model_conversion_requirements.txt",]
        if not os.path.exists(destination_folder_dda_triton):
            os.makedirs(destination_folder_dda_triton)
            logger.info(f"Folder {destination_folder_dda_triton} created successfully.")
        for file in files_to_copy_to_dda_triton:
            shutil.copy2(source_folder + file, destination_folder_dda_triton)
            logger.info(f"File {file} copied successfully to {destination_folder_dda_triton}")
        for file in files_to_copy_to_aws_dda:
            shutil.copy2(source_folder + file, destination_folder_aws_dda)
            logger.info(f"File {file} copied successfully to {destination_folder_aws_dda}")
        if os.path.exists("/aws_dda/resources_for_copy/"):
            shutil.copytree(source_folder + "resources_for_copy/", "/aws_dda/resources_for_copy")
            logger.info("Resources copied successfully.")
    except Exception as e:
        logger.error(f"Exception caught: {e}")
