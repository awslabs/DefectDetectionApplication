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
import re
import logging
import subprocess
import importlib.metadata
import importlib.util
from exceptions.api.triton_exceptions import TritonSetupException
from dda_triton.constants import DDA_ROOT_FOLDER, DDA_TRITON_FOLDER

logger = logging.getLogger(__name__)


def _parse_requirement_names(requirements_file):
    """Parse the distribution names from a requirements file.

    Strips version specifiers (e.g. ``grpcio==1.56.2`` -> ``grpcio``), extras,
    environment markers, inline comments and blank lines, returning the bare
    distribution names (e.g. ``setuptools``, ``scikit-learn``, ``opencv-python``).
    """
    names = []
    with open(requirements_file) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Drop any inline comment.
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            # Split off the first version specifier / extras / marker character.
            name = re.split(r"[<>=!~;\[\s]", line, 1)[0].strip()
            if name:
                names.append(name)
    return names


def _is_distribution_present(name):
    """Return True if the given distribution is already installed/locatable.

    Uses ``importlib.metadata.version`` (distribution-name lookup) first, falling
    back to ``importlib.util.find_spec`` (module lookup). No network access and no
    installation is performed.
    """
    try:
        importlib.metadata.version(name)
        return True
    except importlib.metadata.PackageNotFoundError:
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False
    except Exception:
        return False


# 082925 - ryanv@ disable venv because install_greengrass.sh installs base OS
# TODO move all of this init code inside the backend container to avoid dep issues
def create_virtual_env(
    #env_name="gg_venv",
    #venv_dir="/aws_dda/greengrass/v2/work/aws.edgeml.dda.LocalServer/",
    python_path = "/usr/local/bin/python3",
    requirements_file="/dda_triton/model_conversion_requirements.txt",
):
    # The model conversion dependencies are baked into the backend image at build
    # time, so they are already present when the container starts. This step
    # therefore VERIFIES they are importable rather than installing them: no
    # `pip install` is issued and no network access is required at runtime. A
    # missing package here indicates a build-time regression, not a runtime
    # network problem.
    try:
        if os.path.exists(requirements_file):
            package_names = _parse_requirement_names(requirements_file)
            missing = [name for name in package_names if not _is_distribution_present(name)]
            if missing:
                logger.error(
                    "Model conversion dependencies are missing from the image: "
                    f"{', '.join(missing)}. These dependencies are expected to be baked "
                    f"into the image at build time from '{requirements_file}'. A missing "
                    "package indicates a build-time regression (the build-time install "
                    "step did not run or failed), not a runtime network problem. Rebuild "
                    "the backend image so the dependencies are installed at build time."
                )
            else:
                logger.info(
                    f"All model conversion dependencies from '{requirements_file}' are "
                    "already present and importable. Skipping installation (verify-only, "
                    "no network access required)."
                )
        else:
            logger.error(
                f"No model_conversion_requirements.txt file found at {requirements_file}. Skipping dependency installation."
            )
    except Exception as e:
        logger.error(f"Exception caught while verifying model python requirements: {e}")


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

        files_to_copy_resources = [
           "ensemble_model",
           "lfv_model_template.py",
           "marshal_for_capture_template.py",
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
        if not os.path.exists("/aws_dda/resources_for_copy/"):
            shutil.copytree(source_folder + "resources_for_copy/", "/aws_dda/resources_for_copy")
            logger.info("Resources copied successfully.")
        else:
            logger.info("/aws_dda/resources_for_copy does exist, just copy files")
            for file in files_to_copy_resources:
                shutil.copy2(source_folder + "resources_for_copy/"+file, "/aws_dda/resources_for_copy")
                logger.info("copied file "+str(file))
            logger.info("Resources copied successfully.")

    except Exception as e:
        logger.error(f"Exception caught: {e}")
