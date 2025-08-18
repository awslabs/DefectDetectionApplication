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
import os
import yaml
from string import Template

from marshmallow import Schema, fields, post_load

from utils import constants, utils

PLUGIN_ARG_PATTERN = "{}={}"


class PluginArg:
    def __init__(self, arg_name, arg_value):
        self.arg_name = arg_name
        self.arg_value = arg_value

    def __str__(self):
        return PLUGIN_ARG_PATTERN.format(self.arg_name, self.arg_value)


class PluginDefinition:
    def __init__(self, plugin_name, plugin_args=[]):
        self.plugin_name = plugin_name
        self.plugin_args = plugin_args

    def __str__(self):
        plugin_definition = [self.plugin_name]
        if len(self.plugin_args) > 0:
            plugin_definition.extend(self.plugin_args)
        return " ".join(map(str, plugin_definition))


class PipelineConfiguration:

    def __init__(self):
        self.plugins = []

    def add_plugin(self, plugin_definition):
        self.plugins.append(plugin_definition)

    def build_pipeline_string(self):
        return " ! ".join(map(str, self.plugins))


class PipelineShadowObject:
    def __init__(self, id, definition):
        self.id = id
        self.definition = definition


class PipelineShadowStateConfiguration:
    def __init__(self, pipelines):
        self.pipelines = pipelines

    def upsert(self, pipeline_shadow_object=None):
        for idx, pipeline in enumerate(self.pipelines):
            if pipeline.id == pipeline_shadow_object.id:
                self.pipelines[idx] = pipeline_shadow_object
                break
        else:
            self.pipelines.append(pipeline_shadow_object)

    def delete(self, pipeline_id=None):
        for idx, pipeline in enumerate(self.pipelines):
            if pipeline.id == pipeline_id:
                del self.pipelines[idx]
                break


class PipelineShadow:
    def __init__(self, desired=None, reported=None, delta=None):
        self.desired = desired
        self.reported = reported
        self.delta = delta


class PipelineShadowObjectSchema(Schema):
    id = fields.Str()
    definition = fields.Str()

    @post_load
    def make_source(self, data, **kwargs):
        return PipelineShadowObject(**data)


class PipelineShadowStateConfigurationSchema(Schema):
    pipelines = fields.List(fields.Nested(PipelineShadowObjectSchema()))

    @post_load
    def make_source(self, data, **kwargs):
        return PipelineShadowStateConfiguration(**data)


class PipelineShadowSchema(Schema):
    desired = fields.Nested(PipelineShadowStateConfigurationSchema(), required=False)
    reported = fields.Nested(PipelineShadowStateConfigurationSchema(), required=False)
    delta = fields.Nested(PipelineShadowStateConfigurationSchema(), required=False)

    @post_load
    def make_source(self, data, **kwargs):
        return PipelineShadow(**data)
