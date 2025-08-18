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

# System Modules
import base64
import mimetypes
import os
import json
import time
import string
import secrets
import math
import yaml
import uuid
import platform
import subprocess
from pathlib import Path
from fastapi import HTTPException
import logging
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR


# Custom Modules
from utils import constants
from model.authorization_configuration import AuthorizationSettings, AuthorizationSettingsSchema

logger = logging.getLogger(__name__)
def get_opcua_config(outputs):
    for output in outputs:
        if output.get("type") == "opcua":
            configs_path = os.path.join(os.environ["KERNEL_ROOT_PATH"], 'config', 'effectiveConfig.yaml')
            with open(configs_path, 'r') as f:
                configs = yaml.safe_load(f)
            client_key_path = configs['system']['privateKeyPath']
            server_cert_path = configs['system']['certificateFilePath']
            return outputs.index(output), [client_key_path, server_cert_path]
    return -1, []

def create_em_agent_config(workflow_config):
    output_path = ""
    stream_config_emagent_config_location = get_em_agent_config_path_for_stream(workflow_config.get("workflowId"))
    output_path = workflow_config.get("workflowOutputPath")

    with open(os.path.join(os.environ['KERNEL_ROOT_PATH'], constants.EM_AGENT_CONFIG_PATH), "r") as jsonFile:
        data = json.load(jsonFile)

    data["sagemaker_edge_core_capture_data_disk_path"] = output_path
    # Next inference app release will ingore this folder name, when using capture-folder property
    data["sagemaker_edge_core_folder_prefix"] = ""
    data["sagemaker_edge_local_data_root_path"] = os.path.join(constants.DDA_GREENGRASS_ROOT_FOLDER, "em_agent/local_data")
    if platform.uname()[4] == "aarch64":
        inference_lib_path = os.path.join(os.environ['INFERENCE_COMPONENT_DECOMPRESED_PATH'],
                                          "aarch64/libprovider_aws.so")
        certificate_dir = os.path.join(os.environ['INFERENCE_COMPONENT_DECOMPRESED_PATH'],
                                       "certificates")
    else:
        inference_lib_path = os.path.join(os.environ['INFERENCE_COMPONENT_DECOMPRESED_PATH'],
                                          "amd64/ibprovider_aws.so")
        certificate_dir = os.path.join(os.environ['INFERENCE_COMPONENT_DECOMPRESED_PATH'],
                                       "certificates")
    data["sagemaker_edge_provider_provider_path"] = inference_lib_path
    data["sagemaker_edge_core_root_certs_path"] = certificate_dir
    data["sagemaker_edge_core_capture_data_batch_size"] = 1
    # Increase buffer size to 64 to support concurrent workflow, need to evaluate latency change
    data["sagemaker_edge_core_capture_data_buffer_size"] = 64

    data.pop("sagemaker_edge_core_device_name", None)
    data.pop("sagemaker_edge_provider_provider", None)
    data.pop("sagemaker_edge_provider_provider_path", None)
    data.pop("sagemaker_edge_provider_s3_bucket_name", None)

    with open(stream_config_emagent_config_location, "w") as jsonFile:
        json.dump(data, jsonFile)

    return stream_config_emagent_config_location


def get_em_agent_config_path_for_stream(stream_id):
    return os.path.join(os.environ['COMPONENT_WORK_PATH'],
                        "em-agent-{}.json".format(stream_id))

def get_gst_plugins_path():
    path = ""
    # Add inference component plugins
    if platform.uname()[4] == "aarch64":
        path += os.path.join(os.environ['INFERENCE_COMPONENT_DECOMPRESED_PATH'],
                            "aarch64")
    else:
        path += os.path.join(os.environ['INFERENCE_COMPONENT_DECOMPRESED_PATH'],
                            "amd64")
    
    # Add EdgeML SDK plugins
    path += ":/usr/lib/panoramagst"

    return path

def generate_capture_id(streamId):
    from asgi_correlation_id import correlation_id
    #TODO: change to fastapi request id
    request_id = correlation_id.get()
    if request_id:
        return "{}-{}".format(streamId, request_id)
    return "{}-{}".format(streamId, uuid.uuid4().hex)

def get_dio_script_path():
    return os.path.join(os.environ['LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH'],
                        "script/dio.py")

def remove_prefix(text, prefix):
    if text.startswith(prefix):
        return text[len(prefix):]
    return text


def gen_uuid():
    alphabet = string.ascii_lowercase + string.digits
    uuid = ''.join(secrets.choice(alphabet) for _ in range(8))
    return uuid


def run_command(command):
    output = subprocess.run(command, capture_output=True)
    if output.returncode == 0:
        return True, output.stdout
    return False, output.stderr


def convert_disk_size(size_bytes):
    if size_bytes == 0:
        return "0 bytes"
    size_name = ("bytes", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    logarithm = math.floor(math.log(size_bytes, 1024))
    power = math.pow(1024, logarithm)
    size_value = round(size_bytes / power)
    return "%s %s" % (size_value, size_name[logarithm])

def split_file_name_and_path(full_path):
    if not full_path or "/" not in full_path:
        return "", full_path

    file_name = full_path.split("/")[-1]
    folder_path = "/".join(full_path.split("/")[:-1])
    return folder_path, file_name


# When convert db row object to dictionary, contains `_sa_instance_state`
# which will fail schema validation, only use this util function in accessors
# Do not use in dao base operation
# (_sa_instance_state is a non-database-persisted value used by SQLAlchemy internally)
def convert_sqlalchemy_object_to_dict(row_object):
    dict_obj = dict(row_object.__dict__)
    dict_obj.pop('_sa_instance_state', None)
    return dict_obj

def is_authorization_enabled_on_station():
    return Path(constants.AUTHORIZATION_SETTINGS_FILE).is_file()

def get_authorization_settings_from_file():
    if is_authorization_enabled_on_station():
        try:
            with open(constants.AUTHORIZATION_SETTINGS_FILE) as file:
                auth_settings = json.load(file)
                result = AuthorizationSettingsSchema().load(auth_settings)
                return AuthorizationSettings(**auth_settings)
        except Exception as err:
            raise HTTPException(
                    status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Unable to load authorization settings file. Error: '{err}'. Check the error message and try again"
                )
    else:
        return None

def get_image_bytes_from_file(filename : str) -> str:
    with open(filename, "rb") as image2string:
        return base64.b64encode(image2string.read()).decode()

def get_station_logo() -> str:
    if os.path.exists(constants.DDA_LOGO_FOLDER) and len(os.listdir(constants.DDA_LOGO_FOLDER)) > 0 :
        file_name = os.listdir(constants.DDA_LOGO_FOLDER)[0]        
        file_type = mimetypes.guess_type(file_name)[0].split('/')[-1]
        path = os.path.join(constants.DDA_LOGO_FOLDER, file_name)
        encoded_string = get_image_bytes_from_file(path)
        return f"data:image/{file_type};base64,{encoded_string}"   
    return None