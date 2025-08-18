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
    system_health = {
        "cpuUsagePercent": cpu_usage_percent,
        "memoryUsagePercent": memory_usage_percent,
        "diskTotalSize": disk_total_size,
        "diskUsedSize": disk_used_size,
        "diskUsagePercent": disk_usage_percent,
        "cudaVersion": cuda_version,
        "tensorRTVersion": tensorrt_version,
        "opencvVersion": opencv_version
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