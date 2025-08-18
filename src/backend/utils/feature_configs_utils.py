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
import logging
import awsiot.greengrasscoreipc
import awsiot.greengrasscoreipc.model as model
from fastapi import HTTPException
from data_models.common import (
    FeatureConfigurationAPIModel,
    ListFeatureConfigurationAPIModel
)
from functools import lru_cache

TIME_OUT=10
import logging
logger = logging.getLogger(__name__)

def __get_model_component_config(ipc_client, model_compoent_id=None):
    configRequest = ipc_client.new_get_configuration()  
    request = model.GetConfigurationRequest(component_name=model_compoent_id)
    configRequest.activate(request)
    full_response = configRequest.get_response()
    return full_response.result(TIME_OUT)

#TODO: once we completely switch to triton, update this to get details from Triton
#SIM : https://issues.amazon.com/issues/DD-19533
@lru_cache
def get_default_configs_lfv(model_id):
    try:
        # TODO: Use ipc client from server_setup after making it multiprocess safe
        # from utils.server_setup import ipc_client
        ipc_client = awsiot.greengrasscoreipc.connect()

        default_configs = __get_model_component_config(ipc_client, model_id).value
        default_configs_dict = {
            "modelAlias": default_configs.get("ModelName"),
            "modelMetaData": default_configs.get("ModelMetaData"),
            "modelVersion": default_configs.get("ModelVersion"),
            "modelConfidenceThresholds": default_configs.get("ModelConfidenceThresholds")
        }
        return default_configs_dict
    except model.UnauthorizedError as ue:
        logger.error('Unauthorized error while get config for component topic: ' + model_id)
        raise ue
    except Exception as e:
        logger.error('Exception occurred: '+ str(e))
        raise e

def get_features_lfv(lfv_edge_agent):
    results = []
    for lfv_model in lfv_edge_agent.list_models():
        model_id=lfv_model.get("model_component")
        default_configs_dict = get_default_configs_lfv(model_id)
        results.append(
            ListFeatureConfigurationAPIModel(
                type="LFVModel", 
                modelName=model_id,
                status=lfv_model.get("status"),
                defaultConfiguration=default_configs_dict
            )
        )
    return results

def get_features_triton(triton_server=None):
    results = []
    if triton_server is not None:
        logger.info("Using Triton")
        triton_models = triton_server.list_triton_models()
        for model in triton_models:
            model_id = model.get("model_component")
            if model_id.startswith("base_") or model_id.startswith("marshal_"):
                continue
            default_configs_dict = get_default_configs_lfv(model_id)
            results.append(
                ListFeatureConfigurationAPIModel(
                    type="TritonModel",
                    modelName=model_id,
                    status=model.get("status"),
                    defaultConfiguration=default_configs_dict
                )
            )
    else:
        logger.info("Triton server instance is not provided")
        raise HTTPException(
            status_code=403,
            detail=f"Triton server instance is not provided",
        )
    return results

def start_model_lfv(lfv_edge_agent, model_name):
    __model_desc = lfv_edge_agent.get_model_description(model_name)
    if __model_desc.get("status") not in ["STOPPED", "FAILED"]:
        raise HTTPException(
            status_code=403,
            detail=f"Error while attempting to start model {model_name}. Model current state is {__model_desc.get('status')}. Can only attempt to start STOPPED or FAILED models.",
        )
    return FeatureConfigurationAPIModel(
        type="LFVModel",
        modelName= __model_desc.get("model_component"),
        status=lfv_edge_agent.start_model(model_name)
    )

def start_model_triton(triton_server=None, model_name=None):
    if triton_server is not None and model_name is not None:
        __status = triton_server.get_model_status(model_name)
        logger.info(f"Model, {model_name} , status is :  {__status}")
        if __status not in ["UNKNOWN", "UNAVAILABLE"]:
            raise HTTPException(
            status_code=403,
            detail=f"Error while attempting to start model {model_name}. Model current state is {__status}. Can only attempt to start UNKNOWN or UNAVAILABLE models.",
            )
        response = triton_server.start_triton_model(model_name)
        return FeatureConfigurationAPIModel(
            type="TritonModel",
            modelName=model_name,
            status=response
        )
    else:
        logger.info(f"Triton server instance is not provided for starting the model : {model_name}")
        raise HTTPException(
            status_code=403,
            detail=f"Triton server instance is not provided",
        )

def stop_model_lfv(lfv_edge_agent, model_name):
    __model_desc = lfv_edge_agent.get_model_description(model_name)
    if __model_desc.get("status") not in ["RUNNING"]:
        raise HTTPException(
            status_code=403,
            detail=f"Error while attempting to stop model {model_name}. Model current state is {__model_desc.get('status')}. Can only attempt to stop RUNNING models.",
        )
    return FeatureConfigurationAPIModel(
        type="LFVModel",
        modelName= __model_desc.get("model_component"),
        status=lfv_edge_agent.stop_model(model_name)
    )

def stop_model_triton(triton_server=None, model_name=None):
    if triton_server is not None and model_name is not None:
        __model_desc = triton_server.get_model_description(model_name)
        if __model_desc.get("status") not in ["READY"]:
            raise HTTPException(
            status_code=403,
            detail=f"Error while attempting to stop model {model_name}. Model current state is {__model_desc.get('status')}. Can only attempt to stop READY models.",
        )
        logger.info("Using Triton to stop model")
        response = triton_server.stop_triton_model(model_name)
        return FeatureConfigurationAPIModel(
            type="TritonModel",
            modelName=model_name,
            status=response.get("status")
        )
    else:
        logger.info("Triton server instance or model name is not provided to stop the model")
        raise HTTPException(
            status_code=403,
            detail=f"Triton server instance is not provided",
        )

