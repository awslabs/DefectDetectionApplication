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
from marshmallow import Schema, fields, post_load, validate
from utils.constants import PREDICTION, DB_TEXT_NOTE_MAX_LENGTH, CAPTURE_TYPE

class InferenceResult:
    def __init__(self, captureId, captureType, workflowId, inferenceCreationTime, prediction, confidence, anomalyScore,
                 anomalyThreshod, inputImageFilePath, modelId, anomalyLabels=None, maskImage="",
                 maskBackground=None, outputImageFilePath="", modelName="", flagForReview=False,
                 downloaded=False, humanClassification=None, textNote=None, humanReviewRequired=False,
                 modelConfidenceThresholds={}):
        self.captureId = captureId
        self.captureType = captureType
        self.workflowId = workflowId
        self.inferenceCreationTime = inferenceCreationTime
        self.prediction = prediction
        self.confidence = confidence
        self.anomalyScore = anomalyScore
        self.anomalyThreshod = anomalyThreshod
        self.inputImageFilePath = inputImageFilePath
        self.modelId = modelId
        self.anomalyLabels = anomalyLabels
        self.maskImage = maskImage
        self.maskBackground = maskBackground
        self.outputImageFilePath = outputImageFilePath
        self.modelName = modelName
        self.flagForReview = flagForReview
        self.downloaded = downloaded
        self.humanClassification = humanClassification
        self.textNote = textNote
        self.humanReviewRequired = humanReviewRequired
        self.modelConfidenceThresholds = modelConfidenceThresholds

    def get(self, attr_name, default=None):
        return getattr(self, attr_name, default)

    def __repr__(self):
        return "<InferenceResult(captureId={self.captureId!r})>".format(self=self)


class AnomalyLabelSchema(Schema):
    className = fields.Str(required=False)
    hexColor = fields.Str(required=False)
    totalPercentageArea = fields.Str(required=False)


class MaskBackgroundSchema(Schema):
    className = fields.Str(required=False)
    hexColor = fields.List(fields.Int(validate=validate.Length(equal=3), required=True), required=False)
    totalPercentageArea = fields.Str(required=False)


class InferenceResultSchema(Schema):
    captureId = fields.Str(required=True)
    captureType = fields.Str(validate=validate.OneOf(CAPTURE_TYPE), required=True)
    workflowId = fields.Str(required=True)
    inferenceCreationTime = fields.Int(required=True)
    prediction = fields.Str(validate=validate.OneOf(PREDICTION), required=True)
    confidence = fields.Float(required=True)
    anomalyLabels = fields.List(fields.Dict(required=False), required=False)
    anomalyScore = fields.Float(required=True)
    anomalyThreshod = fields.Float(required=True)
    maskImage = fields.Str(required=False)
    maskBackground = fields.Dict(required=False)
    inputImageFilePath = fields.Str(required=True)
    outputImageFilePath = fields.Str(required=False)
    modelId = fields.Str(required=True)
    modelName = fields.Str(required=False)
    flagForReview = fields.Bool(required=False)
    downloaded = fields.Bool(required=False)
    humanClassification = fields.Str(validate=validate.OneOf(PREDICTION), required=False)
    textNote = fields.Str(validate=validate.Length(max=DB_TEXT_NOTE_MAX_LENGTH), required=False)
    humanReviewRequired = fields.Bool(required=False)
    modelConfidenceThresholds = fields.Dict(required=False)


    @post_load
    def make_source(self, data, **kwargs):
        return InferenceResult(**data)


class CapturedData:
    def __init__(self, captureId, captureType, workflowId, inferenceCreationTime, inputImageFilePath,
                 flagForReview=False, downloaded=False, humanClassification=None, textNote=None):
        self.captureId = captureId
        self.captureType = captureType
        self.workflowId = workflowId
        self.inferenceCreationTime = inferenceCreationTime
        self.inputImageFilePath = inputImageFilePath
        self.flagForReview = flagForReview
        self.downloaded = downloaded
        self.humanClassification = humanClassification
        self.textNote = textNote

    def get(self, attr_name, default=None):
        return getattr(self, attr_name, default)

    def __repr__(self):
        return "<CapturedData(captureId={self.captureId!r})>".format(self=self)


class CapturedDataSchema(Schema):
    captureId = fields.Str(required=True)
    captureType = fields.Str(validate=validate.OneOf(CAPTURE_TYPE), required=True)
    workflowId = fields.Str(required=True)
    inferenceCreationTime = fields.Int(required=True)
    inputImageFilePath = fields.Str(required=True)
    flagForReview = fields.Bool(required=False)
    downloaded = fields.Bool(required=False)
    humanClassification = fields.Str(validate=validate.OneOf(PREDICTION), required=False)
    textNote = fields.Str(validate=validate.Length(max=DB_TEXT_NOTE_MAX_LENGTH), required=False)

    @post_load
    def make_source(self, data, **kwargs):
        return CapturedData(**data)