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
from dda_triton import provider_visibility
from dda_triton.triton_edge_client import TritonEdgeClient
from utils import model_status_shadow
from utils.constants import TRUE_VALUES
from utils.get_is_triton import get_is_triton
import logging

logger = logging.getLogger(__name__)
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
    triton_entries = None
    if get_is_triton():
        # Stand up the Triton server only when models are actually deployed:
        # creating it against an empty model repository blocks indefinitely
        # (longstanding hang on this endpoint when no models are deployed —
        # the native server never reaches readiness with nothing to load).
        # With no Triton models, skip server creation and still surface any
        # vLLM models the runtime manager tracks.
        if feature_configs_utils.triton_repo_has_models():
            triton_entries = feature_configs_utils.get_features_triton(
                __get_triton_instance()
            )
            feature_configs.extend(triton_entries)
        else:
            feature_configs.extend(feature_configs_utils.get_features_vllm())
    else:
        feature_configs.extend(feature_configs_utils.get_features_lfv(lfv_edge_agent))
    # GPU-fallback visibility handoff (spec: model-gpu-fallback-visibility,
    # design Decision 5): AFTER the response data is computed, hand the
    # device-GPU-status snapshot — built from the SAME Triton listing the
    # response used — to the debounced shadow reporter. Failure-isolated:
    # a snapshot/reporter problem never affects this response.
    if triton_entries is not None:
        try:
            statuses = {
                entry.modelName: entry.status
                for entry in triton_entries
                if entry.type == "TritonModel"
            }
            model_status_shadow.report(__gpu_status_snapshot(statuses))
        except Exception as e:
            logger.warning(f"model-status shadow handoff skipped: {e}")
    return feature_configs


@router.get("/feature-configurations/gpu-status")
def get_gpu_status() -> dict:
    """Device-level degraded-GPU signal (spec: model-gpu-fallback-visibility,
    requirement 2.4, design Decision 2). Guarded exactly like the list
    route: with no Triton or an EMPTY model repository, return the
    empty/non-degraded shape WITHOUT standing up Triton (the empty-repo
    hang guard above applies here too)."""
    if get_is_triton() and feature_configs_utils.triton_repo_has_models():
        triton_server = __get_triton_instance()
        statuses = {}
        for model in triton_server.list_triton_models():
            model_id = model.get("model_component")
            if model_id.startswith("base_") or model_id.startswith("marshal_"):
                continue
            statuses[model_id] = model.get("status")
        snapshot = __gpu_status_snapshot(statuses)
    else:
        # No Triton / empty repo: no models, never degraded (and the
        # aggregator keeps the transition-logging state consistent).
        snapshot = provider_visibility.device_gpu_status({}, {})
    try:
        model_status_shadow.report(snapshot)
    except Exception as e:
        logger.warning(f"model-status shadow handoff skipped: {e}")
    return snapshot


def __gpu_status_snapshot(statuses):
    """The ``device_gpu_status`` snapshot for ``statuses`` (model name ->
    Triton status): read each model's Active_Provider_Record (absence-
    tolerant, Decision 6) and aggregate."""
    records = {
        name: provider_visibility.read_active_provider_record(name)
        for name in statuses
    }
    return provider_visibility.device_gpu_status(records, statuses)


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