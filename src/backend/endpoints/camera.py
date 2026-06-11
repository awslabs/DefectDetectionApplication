
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
# Fastapi modules
from fastapi import APIRouter, Depends
from typing import List, Optional
from pydantic import RootModel, BaseModel

# Custom Modules
from edge_ml1_p_camera_management import aravis_functions
from endpoints.route.access_log_router import get_api_router
from utils.camera_manager import (
    connect_camera,
    disconnect_camera,
    get_camera_feature_bounds,
    apply_camera_features,
)

# Schema and Validation models
from model.Camera import CameraSchema
from data_models.common import CameraModel
from pydantic import RootModel

class GetCamerasResponse(RootModel):
    root: List[CameraModel]


router = get_api_router()

# returns a list of cameras that the IPC can see
@router.get("/cameras")
def getcameras() -> GetCamerasResponse:
    cameras = aravis_functions.getCameras()
    schema = CameraSchema(many=True)
    result = schema.dump(cameras)
    return result

# Forces a fresh bus enumeration and returns the cameras.
# Use this to pick up a camera that was connected after the server started,
# without restarting the container (the container does not receive host udev
# hotplug events, so a plain GET /cameras may not see it).
@router.post("/cameras/rescan")
def rescan_cameras_endpoint() -> GetCamerasResponse:
    cameras = aravis_functions.rescan_cameras()
    schema = CameraSchema(many=True)
    result = schema.dump(cameras)
    return result

@router.get("/cameras/{cameraId}/connect")
def connect_camera_endpoint(cameraId: str):
    # Check if the camera is online, 
    #   throws exception if camera not connected
    aravis_functions.getCamera(cameraId)
    return connect_camera(cameraId)

@router.get("/cameras/{cameraId}/disconnect")
def disconnect_camera_endpoint(cameraId: str):
    return disconnect_camera(cameraId)

# Returns the adjustable feature ranges (gain, exposure, ...) for a camera,
# read from the device's GenICam feature map, so the UI can present limits
# that match the actual hardware instead of hard-coded defaults.
@router.get("/cameras/{cameraId}/feature-bounds")
def get_camera_feature_bounds_endpoint(cameraId: str):
    return get_camera_feature_bounds(cameraId)


class ApplyCameraFeature(BaseModel):
    feature: str
    type: str
    value: object


class ApplyCameraFeaturesRequest(BaseModel):
    features: List[ApplyCameraFeature] = []


# Applies advanced GenICam feature values (white balance, flip, pixel format,
# ROI, ...) to the live camera and returns the device-accepted values.
@router.post("/cameras/{cameraId}/features")
def apply_camera_features_endpoint(cameraId: str, request: ApplyCameraFeaturesRequest):
    features = [f.dict() for f in request.features]
    return apply_camera_features(cameraId, features)
