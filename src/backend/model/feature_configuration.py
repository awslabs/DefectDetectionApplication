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

valid_types = ["LFVModel"]


class FeatureConfiguration:
    def __init__(self, type, modelName):
        self.type = type
        self.modelName = modelName

    def get(self, attr_name, default=None):
        return getattr(self, attr_name, default)


class FeatureConfigurationSchema(Schema):
    type = fields.Str(validate=validate.OneOf(valid_types), required=True)
    modelName = fields.Str()

    @post_load
    def make_source(self, data, **kwargs):
        return FeatureConfiguration(**data)


class Old_FeatureConfiguration(FeatureConfiguration):
    def __init__(self, type, modelName, defaultConfiguration={}):
        super().__init__(type, modelName)
        self.defaultConfiguration = defaultConfiguration
