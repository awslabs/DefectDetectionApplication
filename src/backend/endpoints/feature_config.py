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

from fastapi import Depends
from sqlalchemy.orm import Session
from dao.sqlite_db.sqlite_db_operations import SessionLocal

# Fast api
from fastapi import APIRouter, HTTPException, Depends
from utils import feature_configs_utils
from utils.server_setup import (
    input_cfg_accessor,
    output_cfg_accessor,
    lfv_edge_agent
)
import os
from typing import List
from pydantic import RootModel
from data_models.common import (
    InputConfigurationsModel,
    OutputConfigurationsModel,
    FeatureConfigurationAPIModel,
    ListFeatureConfigurationAPIModel
)
from endpoints.route.access_log_router import get_api_router
from dda_triton.triton_edge_client import TritonEdgeClient
from utils.constants import TRUE_VALUES
from utils.get_is_triton import get_is_triton
router = get_api_router()
# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class ListInputConfigsResponse(RootModel):
    root: List[InputConfigurationsModel]


@router.get("/input-configurations")
def list_input_configs(db: Session = Depends(get_db)) -> ListInputConfigsResponse:
    return input_cfg_accessor.list_input_configurations(db)


class ListOuputConfigsResponse(RootModel):
    root: List[OutputConfigurationsModel]


@router.get("/output-configurations")
def list_output_configs(db: Session = Depends(get_db)) -> ListOuputConfigsResponse:
    return output_cfg_accessor.list_output_configurations(db)


class ListFeatureConfigsResponse(RootModel):
    root: List[ListFeatureConfigurationAPIModel]

@router.get("/feature-configurations")
def list_feature_configs() -> ListFeatureConfigsResponse:
    feature_configs = []
    triton_server = __get_triton_instance()
    if triton_server:
        feature_configs.extend(feature_configs_utils.get_features_triton(triton_server))
    else:
        feature_configs.extend(feature_configs_utils.get_features_lfv(lfv_edge_agent))
    return feature_configs


@router.get("/feature-configurations/models/{modelName}/start")
def start_feature_config(modelName : str) -> FeatureConfigurationAPIModel:
    __validate_model_name(modelName)
    triton_server = __get_triton_instance()
    if triton_server:
        return feature_configs_utils.start_model_triton(triton_server, modelName)
    else:
        return feature_configs_utils.start_model_lfv(lfv_edge_agent, modelName)


@router.get("/feature-configurations/models/{modelName}/stop")
def stop_feature_configs(modelName : str) -> FeatureConfigurationAPIModel:
    __validate_model_name(modelName)
    triton_server = __get_triton_instance()
    if triton_server:
        return feature_configs_utils.stop_model_triton(triton_server, modelName)
    else:    
        return feature_configs_utils.stop_model_lfv(lfv_edge_agent, modelName)


def __validate_model_name(modelName : str):
    if not modelName.startswith("model-"):
        raise HTTPException(
            status_code=400,
            detail=f"The server can't process this request. Error: Invalid model name '{modelName}'. Check the model name and try again.",
        )

def __get_triton_instance():
    return TritonEdgeClient.get_instance() if get_is_triton() else None