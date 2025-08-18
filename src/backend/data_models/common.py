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

from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from utils.common import CameraStatusEnum
from utils.constants import DB_TEXT_NOTE_MAX_LENGTH

class CameraModel(BaseModel):
    id: str
    model: str
    address: str
    physical_id: str
    protocol: str
    serial: str
    vendor: str

class CameraStatusModel(BaseModel):
    status: CameraStatusEnum
    lastUpdatedTime: float
    error: Optional[str] = None


class ImageSourceConfigurationsOutputModel(BaseModel):
    creationTime: int
    gain: int
    exposure: int
    processingPipeline: str
    device: Optional[str] = None
    deviceName: Optional[str] = None
    imageSourceConfigId: str


class ImageSourceCropModel(BaseModel):
    top: int
    bottom: int
    left: int
    right: int


class ImageSourceConfigurationsInputModel(BaseModel):
    gain: Optional[int] = None
    exposure: Optional[int] = None
    processingPipeline: Optional[str] = None
    imageCrop: Optional[ImageSourceCropModel] = None
    device: Optional[str] = None
    deviceName: Optional[str] = None


class InputConfigurationsModel(BaseModel):
    inputConfigurationId: str
    creationTime: int
    pin: str
    triggerState: str
    debounceTime: int


class OutputConfigurationsModel(BaseModel):
    outputConfigurationId: str
    pin: str
    signalType: str
    pulseWidth: int
    creationTime: int
    rule: str

class BaseFeatureConfigurationsModel(BaseModel):
    type: str
    modelName: str

class FeatureConfigurationsModel(BaseFeatureConfigurationsModel):
    defaultConfiguration: dict = None

class FeatureConfigurationAPIModel(BaseFeatureConfigurationsModel):
    status: str

class ListFeatureConfigurationAPIModel(FeatureConfigurationAPIModel):
    defaultConfiguration: dict = None


class ImageSourceConfigIdModel(BaseModel):
    imageSourceConfigId: str


class ImageSourceIdModel(BaseModel):
    imageSourceId: str


class ImageSourceModel(BaseModel):
    imageSourceId: str
    name: str
    type: Literal["Camera", "Folder"]
    location: Optional[str] = None
    cameraId: Optional[str] = None
    description: Optional[str] = None
    creationTime: int
    lastUpdateTime: int
    imageCapturePath: str = None
    imageSourceConfigId: Optional[str] = None
    imageSourceConfiguration: Optional[dict] = {}


# TODO: Validate anomalies structure
class InferenceResultModel(BaseModel):
    anomalies: Optional[dict] = None
    confidence: float
    anomaly_score: Optional[float] = None
    anomaly_threshold: Optional[float] = None
    inference_result: str
    mask_background: Optional[dict] = None
    mask_image: Optional[str] = None

class InferenceResultHistoryModel(BaseModel):
    anomalyScore: Optional[float] = None
    anomalyThreshod: Optional[float] = None
    maskBackground: Optional[dict] = None
    maskImage: Optional[str] = None
    inputImageFilePath: str
    outputImageFilePath: Optional[str] = None
    modelId: Optional[str] = None
    modelName: Optional[str] = None
    captureId: str
    captureType: str
    workflowId: str
    inferenceCreationTime: Optional[int] = None
    confidence: Optional[float] = None
    prediction: Optional[str] = None
    anomalyLabels: Optional[List[dict]] = None
    flagForReview: bool
    downloaded: bool
    humanClassification: Optional[str] = None
    textNote: Optional[str] = Field(None, max_length = DB_TEXT_NOTE_MAX_LENGTH)
    humanReviewRequired: Optional[bool] = False

    class Config:
        from_attributes = True


class CapturedImageModel(BaseModel):
    path: str
    image: str


class WorkflowModel(BaseModel):
    workflowId: str
    name: str
    description: Optional[str] = None
    creationTime: int
    lastUpdatedTime: int
    workflowOutputPath: str
    imageSourceId: Optional[str] = None
    imageSources: Optional[List[ImageSourceModel]] = None
    featureConfigurations: Optional[List[FeatureConfigurationsModel]] = None
    inputConfigurations: Optional[List[InputConfigurationsModel]] = None
    outputConfigurations: Optional[List[OutputConfigurationsModel]] = None


class RunWorkflowModel(BaseModel):
    creationTime: Optional[str] = None
    imageDataFilePath: Optional[str] = None
    inferenceResult: InferenceResultModel
    inferenceFilePath: Optional[str] = None
    image: Optional[str] = None
    captureId: Optional[str] = None
    inputImageFilePath: Optional[str] = None
    humanReviewRequired: bool = False

class LatencyTimeModel(BaseModel):
    inferenceCaptureId: str
    latencyType: str
    timestamp: float

class WorkflowMetadataModel(BaseModel):
    workflowId: str
    summaryStartTime: int