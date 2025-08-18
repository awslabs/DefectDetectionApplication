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

from model.feature_configuration import FeatureConfigurationSchema
from model.input_configuration import InputConfigurationSchema
from model.image_source import ImageSourceSchema
from model.output_configuration import OutputConfigurationSchema


class Workflow:

    def __init__(self, workflowId, name, workflowOutputPath, creationTime, lastUpdatedTime, featureConfigurations=[],
                 description="", imageSourceId=None, imageSources=[], inputConfigurations=[], outputConfigurations=[]):

        self.workflowId = workflowId
        self.name = name
        self.description = description
        self.creationTime = creationTime
        self.lastUpdatedTime = lastUpdatedTime
        self.workflowOutputPath = workflowOutputPath
        self.imageSources = imageSources
        self.imageSourceId = imageSourceId
        self.featureConfigurations = featureConfigurations
        self.inputConfigurations = inputConfigurations
        self.outputConfigurations = outputConfigurations

    def get(self, attr_name, default=None):
        return getattr(self, attr_name, default)

    def __repr__(self):
        return "<Workflow(workflowId={self.workflowId!r})>".format(self=self)


class WorkflowSchema(Schema):

    workflowId = fields.Str(required=True)
    name = fields.Str(required=True)
    description = fields.Str(required=False)
    creationTime = fields.Int(required=True)
    lastUpdatedTime = fields.Int(required=True)
    workflowOutputPath = fields.Str(required=True)
    imageSourceId = fields.Str(required=False)
    featureConfigurations = fields.List(fields.Nested(FeatureConfigurationSchema(), required=False), required=False)
    inputConfigurations = fields.List(fields.Nested(InputConfigurationSchema(), required=False), required=False)
    outputConfigurations = fields.List(fields.Nested(OutputConfigurationSchema(), required=False), required=False)

    @post_load
    def make_workflow(self, data, **kwargs):
        return Workflow(**data)
