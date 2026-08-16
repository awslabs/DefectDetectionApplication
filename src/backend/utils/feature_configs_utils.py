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
import os
import awsiot.greengrasscoreipc
import awsiot.greengrasscoreipc.model as model
from fastapi import HTTPException
from utils.ipc_client import get_ipc_client
from data_models.common import (
    FeatureConfigurationAPIModel,
    ListFeatureConfigurationAPIModel
)
from dda_triton.constants import TRITON_MODEL_DIR
from dda_triton.provider_visibility import (
    execution_provider_info,
    read_active_provider_record,
)
from functools import lru_cache

TIME_OUT=10
import logging
logger = logging.getLogger(__name__)


def triton_repo_has_models(model_repo_dir: str = TRITON_MODEL_DIR) -> bool:
    """True when the Triton model repository holds at least one model directory.

    Standing up the Triton inference server against an EMPTY model repository
    blocks indefinitely (a longstanding hang observed on the
    ``/feature-configurations`` endpoint when no models are deployed): the
    native ``mlops.create_triton_inference_server`` call never returns because
    the server has nothing to load and never reaches readiness. Callers use
    this cheap filesystem check to skip server creation entirely when no
    models are deployed and return an empty (or vLLM-only) feature list
    instead. A missing/unreadable repo directory means no models are deployed.
    """
    try:
        return any(
            os.path.isdir(os.path.join(model_repo_dir, entry))
            for entry in os.listdir(model_repo_dir)
        )
    except OSError:
        return False

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
        # Reuse the process-wide shared IPC client (DD-19576). Connecting a
        # fresh client here and never closing it left one connection per
        # model_id to Python GC finalization, which aborted the process with
        # the aws-c-event-stream "Continuation ref count has gone negative"
        # fatal assert. /feature-configurations is polled continuously, so
        # this was the hottest offender.
        ipc_client = get_ipc_client()

        default_configs = __get_model_component_config(ipc_client, model_id).value
        default_configs_dict = {
            "modelAlias": default_configs.get("ModelName"),
            "modelMetaData": default_configs.get("ModelMetaData"),
            "modelVersion": default_configs.get("ModelVersion"),
            "modelConfidenceThresholds": default_configs.get("ModelConfidenceThresholds")
        }
        return default_configs_dict
    except model.ResourceNotFoundError:
        logger.info(f'No Greengrass component found for model {model_id}, using defaults')
        return {
            "modelAlias": model_id,
            "modelMetaData": {},
            "modelVersion": "1.0.0",
            "modelConfidenceThresholds": {}
        }
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

# --- vLLM runtime status merge (Requirements 4.6, 4.7, 4.10) ---------------

#: Feature-config entry type for vLLM models (design section 9).
VLLM_FEATURE_TYPE = "VllmModel"

#: Manager model state -> the status reported through the existing device
#: model-status mechanisms (feature-config API, shadow sync): LOADING→LOADING,
#: READY→READY, FAILED→FAILED. STAGED models have their load request on the
#: way (vllm_model_prep.py stages then immediately requests the load), so they
#: report as LOADING (Requirement 4.7).
_VLLM_STATUS_MAP = {
    "STAGED": "LOADING",
    "LOADING": "LOADING",
    "READY": "READY",
    "FAILED": "FAILED",
}

_vllm_manager = None


def set_vllm_manager(manager):
    """Install the started ``VllmRuntimeManager`` whose model list is merged
    into the feature-config status (app.py calls this on vLLM-capable images,
    mirroring ``text_generation.set_runtime``; ``None`` uninstalls it). With
    no manager installed — vLLM-free images — feature-config behavior is
    identical to pre-feature."""
    global _vllm_manager
    _vllm_manager = manager


def get_vllm_manager():
    """The installed vLLM runtime manager, or ``None`` on images without the
    vLLM runtime."""
    return _vllm_manager


def _vllm_feature_status(status):
    """Map a manager ``ModelStatus`` (tolerant of fakes passing plain state
    strings) to the ``(status, failure_reason)`` the feature-config
    mechanisms report."""
    state = getattr(status, "state", status)
    state_name = str(getattr(state, "value", state)).upper()
    return _VLLM_STATUS_MAP.get(state_name, state_name), getattr(status, "reason", None)


def get_features_vllm():
    """Feature-config entries for every vLLM model the runtime manager
    tracks: one ``VllmModel`` entry per model with the mapped status, the
    backend failure reason retained for FAILED models (Requirements 4.6,
    4.7, 4.10). Empty when no manager is installed. The manager pushes state
    transitions synchronously, so every feature-config read observes the
    current state — READY propagates well within the 30-second bound (4.10).
    """
    manager = get_vllm_manager()
    if manager is None:
        return []
    results = []
    try:
        statuses = manager.list_models()
    except Exception as e:
        # A vLLM-side failure never takes down the vision model status feed
        # (Requirement 4.6: failures are isolated).
        logger.error(f"Failed to list vLLM models: {e}")
        return results
    for model_name in sorted(statuses):
        mapped_status, reason = _vllm_feature_status(statuses[model_name])
        default_configuration = {"modelAlias": model_name}
        if mapped_status == "FAILED" and reason:
            default_configuration["failureReason"] = reason
        results.append(
            ListFeatureConfigurationAPIModel(
                type=VLLM_FEATURE_TYPE,
                modelName=model_name,
                status=mapped_status,
                defaultConfiguration=default_configuration,
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
            # Additive GPU-fallback visibility merge (requirement 2.2, design
            # File 3; the vLLM failureReason precedent): expose the stub's
            # Active_Provider_Record as executionProviderInfo. No record →
            # no field (Decision 6), and a reader bug degrades to "no field",
            # never a 500. Copy before adding: get_default_configs_lfv is
            # lru_cached and its dict must never carry the merged field.
            try:
                record = read_active_provider_record(model_id)
                if record:
                    default_configs_dict = dict(default_configs_dict)
                    default_configs_dict["executionProviderInfo"] = (
                        execution_provider_info(record)
                    )
            except Exception as e:
                logger.warning(
                    f"executionProviderInfo merge skipped for {model_id}: {e}"
                )
            results.append(
                ListFeatureConfigurationAPIModel(
                    type="TritonModel",
                    modelName=model_id,
                    status=model.get("status"),
                    defaultConfiguration=default_configs_dict
                )
            )
        results.extend(get_features_vllm())
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

