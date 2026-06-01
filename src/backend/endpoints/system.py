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
import psutil
import shutil
import subprocess
import re
import sys

from typing import Optional
import importlib.metadata

# Aws Modules
from awsiot.greengrasscoreipc.model import (
    GetComponentDetailsRequest,
    ListComponentsRequest,
    RestartComponentRequest,
)


# Fast api
from fastapi import APIRouter, status, HTTPException
from exceptions.api.triton_exceptions import InvalidParamterException
from pydantic import BaseModel, Field
from typing import Literal

# Custom Modules
from utils.constants import (
    DDA_GG_COMPONENT_NAME_PREFIX,
    DDA_LOCAL_SERVER_COMPONENT,
    DDA_ROOT_FOLDER,
    GET_DDA_COMPONENT_STATUS_HEALTHY,
    GET_DDA_COMPONENT_STATUS_UNHEALTHY,
    GG_IPC_FUTURE_TIMEOUT,
    LFV_AGENT_GG_COMPONENT_NAME,
    EDGE_AGENT_VENV_PATH,
    PYTHON38,
    SNAPSHOT_FILE_PATTERN
)
from snapshot import Snapshotter
from utils.server_setup import ipc_client
from defect_detection_config.defect_detection_config import DefectDetectionConfig
from utils.utils import convert_disk_size, get_station_logo
from endpoints.route.access_log_router import get_api_router
import logging
logger = logging.getLogger(__name__)

from utils.gg_utils import list_gg_components, restart_components
router = get_api_router()

EDGE_AGENT_VENV_SITE_PACKAGE_PATH = EDGE_AGENT_VENV_PATH + "/env/lib/" + PYTHON38 + "/site-packages"
# to help python find the package in the venv
sys.path.append(EDGE_AGENT_VENV_SITE_PACKAGE_PATH)

class GetSystemHealthResponse(BaseModel):
    cpuUsagePercent: float = Field(ge=0, le=100)
    memoryUsagePercent: float = Field(ge=0, le=100)
    diskTotalSize: str
    diskUsedSize: str
    diskUsagePercent: float = Field(ge=0, le=100)
    cudaVersion: str
    tensorRTVersion: str
    opencvVersion: str
    localServerVersion: str


# Function to get CUDA version
def get_cuda_version():
    cuda_version = os.getenv("JETSON_CUDA")
    return cuda_version if cuda_version else "NOT_INSTALLED"


# Function to get TensorRT version
def get_tensorrt_version():
    tensorrt_version = os.getenv("JETSON_TENSORRT")
    return tensorrt_version if tensorrt_version else "NOT_INSTALLED"

def get_opencv_version_from_lfv():
    version = "NOT_INSTALLED"
    try:
        version = importlib.metadata.version('opencv_python_headless')
    except importlib.metadata.PackageNotFoundError:
        logger.warn("opencv_python_headless module is not installed for edge agent")
    return version

def get_local_server_component_version():
    """Get the version of the LocalServer component from Greengrass.

    Multiple LocalServer variants (.arm64, .arm64JP5, .amd64) can be present
    on a single core device, and they all share the "aws.edgeml.dda.LocalServer"
    prefix. Matching by prefix and taking the first hit can therefore report a
    different (e.g. leftover) variant's version than the one actually running
    here. Resolve THIS component's exact name from the decompressed-path env
    var the recipe injects (e.g. ".../aws.edgeml.dda.LocalServer.arm64JP5-aarch64")
    and match it exactly; fall back to the prefix match only if that name
    cannot be determined.
    """
    version = "NOT_FOUND"

    # The recipe sets LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH to
    # "{artifacts:decompressedPath}/<component-name>-<arch>". The last path
    # segment is "<component-name>-<arch>"; component names are dot-separated
    # and contain no '-', and the arch suffix (aarch64 / x86_64) contains none
    # either, so rsplit on the final '-' yields the exact component name.
    own_component_name = None
    decompressed_path = os.getenv("LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH")
    if decompressed_path:
        last_segment = os.path.basename(decompressed_path.rstrip("/"))
        candidate = last_segment.rsplit("-", 1)[0]
        if candidate.startswith(DDA_LOCAL_SERVER_COMPONENT):
            own_component_name = candidate

    try:
        list_components_request = ListComponentsRequest()
        list_components_operation = ipc_client.new_list_components()
        list_components_operation.activate(list_components_request)
        list_components_future = list_components_operation.get_response()
        list_components_response = list_components_future.result(GG_IPC_FUTURE_TIMEOUT)

        prefix_match_version = None
        for component in list_components_response.components:
            if own_component_name is not None:
                if component.component_name == own_component_name:
                    version = component.version
                    logger.info(f"Found LocalServer component: {component.component_name} version {version}")
                    break
            elif component.component_name.startswith(DDA_LOCAL_SERVER_COMPONENT):
                # No exact name available; remember the first prefix match as a fallback.
                if prefix_match_version is None:
                    prefix_match_version = component.version
                    logger.info(f"Found LocalServer component (prefix match): {component.component_name} version {component.version}")

        if version == "NOT_FOUND" and prefix_match_version is not None:
            version = prefix_match_version
    except Exception as e:
        logger.error(f"Failed to get LocalServer component version: {e}")

    return version

@router.get("/system-health")
def get_system_health() -> GetSystemHealthResponse:
    cpu_usage_percent = psutil.cpu_percent(interval=None)
    memory_usage_percent = psutil.virtual_memory().percent
    disk_usage = shutil.disk_usage(DDA_ROOT_FOLDER)
    disk_used_size = convert_disk_size(disk_usage.used)
    disk_total_size = convert_disk_size(disk_usage.total)
    disk_usage_percent = round(disk_usage.used / disk_usage.total * 100, 2)
    cuda_version = get_cuda_version()
    tensorrt_version = get_tensorrt_version()
    opencv_version = get_opencv_version_from_lfv()
    local_server_version = get_local_server_component_version()
    system_health = {
        "cpuUsagePercent": cpu_usage_percent,
        "memoryUsagePercent": memory_usage_percent,
        "diskTotalSize": disk_total_size,
        "diskUsedSize": disk_used_size,
        "diskUsagePercent": disk_usage_percent,
        "cudaVersion": cuda_version,
        "tensorRTVersion": tensorrt_version,
        "opencvVersion": opencv_version,
        "localServerVersion": local_server_version
    }
    return system_health


@router.post("/restart-dda")
def restart_dda():
    logger.info("Received request to restart DDA application")
    components_to_restart = list_gg_components(return_dda_components=True)
    restart_components(components_to_restart)

class GetDdaComponentHealthStatusResponse(BaseModel):
    status: Literal[GET_DDA_COMPONENT_STATUS_HEALTHY, GET_DDA_COMPONENT_STATUS_UNHEALTHY]


@router.get("/dda-component-status")
def get_dda_component_status() -> GetDdaComponentHealthStatusResponse:
    logger.info("Received request to check DDA component status")
    components = list_gg_components(return_dda_components=True)
    for component in components:
        get_component_details_request = GetComponentDetailsRequest()
        get_component_details_request.component_name = component
        get_component_details_operation = ipc_client.new_get_component_details()
        get_component_details_operation.activate(get_component_details_request)
        get_component_details_future = get_component_details_operation.get_response()
        get_component_details_response = get_component_details_future.result(GG_IPC_FUTURE_TIMEOUT)
        logger.info(
            f"Component {component} has state {get_component_details_response.component_details.state}"
        )
        if get_component_details_response.component_details.state not in [
            "RUNNING",
            "FINISHED",
        ]:
            return {"status": GET_DDA_COMPONENT_STATUS_UNHEALTHY}
    return {"status": GET_DDA_COMPONENT_STATUS_HEALTHY}


class SnapshotPathResponse(BaseModel):
    archivePath: str = Field(pattern=SNAPSHOT_FILE_PATTERN)


@router.get("/snapshot")
def get_snapshot() -> SnapshotPathResponse:
    stationName = get_station().get("name")
    return {"archivePath": Snapshotter.take_snapshot(stationName)}



defect_detection_config = DefectDetectionConfig(ipc_client)

class GetStationResponse(BaseModel):
    name: Optional[str] = "Station"
    version: Optional[str] = None
    webuxUrl: Optional[str] = None
    tenantId: Optional[str] = None
    deviceId: Optional[str] = None
    logoImage: Optional[str] = None

@router.get("/system/station")
def get_station() -> GetStationResponse:
    stationResponse = defect_detection_config.get_station()
    stationResponse['logoImage'] = get_station_logo()
    return stationResponse

# Deprecation message is part of api doc
@router.get("/station",
            status_code=status.HTTP_301_MOVED_PERMANENTLY,
            deprecated=True,
            response_description="The /station operation is deprecated. To get station name, use /system/station")
def get_station_old() -> GetStationResponse:
    return defect_detection_config.get_station_old()