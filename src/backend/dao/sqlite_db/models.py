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

from sqlalchemy import Column, ForeignKey, Integer, String, Enum, JSON, Float, Boolean
from sqlalchemy.orm import relationship, backref
from model.image_source import ImageSourceType
from utils.constants import ANOMALY, NORMAL, GPIO_FALLING, GPIO_RISING, CAPTURE, INFERENCE

from .sqlite_db_operations import Base, BaseMetadata


############################
## Configuration Database ##
############################
class ImageSourceConfiguration(Base):
    __tablename__ = "image_source_configuration"

    imageSourceConfigId = Column(String, primary_key=True, index=True)
    gain = Column(Integer)
    exposure = Column(Integer)
    processingPipeline = Column(String)
    creationTime = Column(Integer)
    imageCrop = Column(JSON)
    device = Column(String)
    deviceName = Column(String)


class InputConfiguration(Base):
    __tablename__ = "input_configuration"

    inputConfigurationId = Column(String, primary_key=True, index=True)
    creationTime = Column(Integer)
    pin = Column(String, nullable=False)
    triggerState = Column(Enum(GPIO_RISING, GPIO_FALLING, name="enum_digital_to_signal_type"), nullable=False)
    debounceTime = Column(Integer, nullable=False)


class OutputConfiguration(Base):
    __tablename__ = "output_configuration"

    outputConfigurationId = Column(String, primary_key=True, index=True)
    pin = Column(String, nullable=False)
    signalType = Column(Enum(GPIO_RISING, GPIO_FALLING, name="enum_digital_to_signal_type"), nullable=False)
    pulseWidth = Column(Integer, nullable=False)
    creationTime = Column(Integer)
    rule = Column(Enum("All", NORMAL, ANOMALY, name="output_rule"), nullable=False)


class ImageSource(Base):
    __tablename__ = "image_source"

    imageSourceId = Column(String, primary_key=True, index=True)
    name = Column(String)
    type = Column(Enum(ImageSourceType, values_callable=lambda x: [i.value for i in x]))
    location = Column(String)
    cameraId = Column(String)
    description = Column(String)
    creationTime = Column(Integer)
    lastUpdateTime = Column(Integer)
    imageCapturePath = Column(String)

    imageSourceConfigId = Column(String, ForeignKey('image_source_configuration.imageSourceConfigId'))
    imageSourceConfiguration = relationship("ImageSourceConfiguration", backref=backref("image_source", uselist=False))


class Workflow(Base):
    __tablename__ = "workflow"

    workflowId = Column(String, primary_key=True, index=True)
    name = Column(String)
    description = Column(String)
    creationTime = Column(Integer)
    lastUpdatedTime = Column(Integer)
    workflowOutputPath = Column(String)
    featureConfigurations = Column(JSON)
    inputConfigurations = Column(JSON)
    outputConfigurations = Column(JSON)

    imageSourceId = Column(String, ForeignKey('image_source.imageSourceId'))
    imageSources = relationship("ImageSource", backref=backref("workflow", uselist=True))

#######################
## Metadata Database ##
#######################
class InferenceResult(BaseMetadata):
    __tablename__ = "inference_result_metadata"

    captureId = Column(String, primary_key=True, index=True)
    captureType = Column(Enum(CAPTURE, INFERENCE, name="enum_capture_type"))
    workflowId = Column(String, index=True)
    inferenceCreationTime = Column(Integer)
    prediction = Column(Enum(ANOMALY, NORMAL, name="enum_prediction_type"))
    confidence = Column(Float)
    anomalyLabels = Column(JSON)
    anomalyScore = Column(Float)
    anomalyThreshod = Column(Float)
    maskImage = Column(String)
    maskBackground = Column(JSON)
    inputImageFilePath = Column(String)
    outputImageFilePath = Column(String)
    modelId = Column(String)
    modelName = Column(String)
    flagForReview = Column(Boolean)
    downloaded = Column(Boolean)
    humanClassification = Column(Enum(ANOMALY, NORMAL, name="enum_prediction_type"))
    textNote = Column(String)
    humanReviewRequired = Column(Boolean)
    modelConfidenceThresholds = Column(JSON)


class WorkflowMetadata(BaseMetadata):
    __tablename__ = "workflow_metadata"

    workflowId = Column(String, primary_key=True, index=True)
    summaryStartTime = Column(Integer, nullable=False)

class LatencyTime(BaseMetadata):
    __tablename__ = "latency_time"

    inferenceCaptureId = Column(String, primary_key=True, index=True)
    latencyType = Column(String, primary_key=True, index=True)
    timestamp = Column(Float, nullable=False)
