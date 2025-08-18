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

from model.input_configuration import InputConfigurationSchema
from model.output_configuration import OutputConfigurationSchema
from model.feature_configuration import Old_FeatureConfiguration
from model.workflow import Workflow, WorkflowSchema


valid_types = ["LFVModel"]


class Old_FeatureConfigurationSchema(Schema):
    type = fields.Str(validate=validate.OneOf(valid_types), required=True)
    modelName = fields.Str()
    defaultConfiguration = fields.Dict(required=False)

    @post_load
    def make_source(self, data, **kwargs):
        return Old_FeatureConfiguration(**data)


class Old_WorkflowSchema(WorkflowSchema):

    featureConfigurations = fields.List(fields.Nested(Old_FeatureConfigurationSchema(), required=True), validate=validate.Length(equal=1), required=True)

