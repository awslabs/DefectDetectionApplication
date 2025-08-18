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
from marshmallow import ValidationError, Schema, fields, post_load, post_dump, EXCLUDE, validates_schema, validate

VALID_SOURCE_TYPES = ["file", "camera"]


class SourceConfiguration:
    def __init__(self, type, path=None, cameraId=None):
        self.type = type
        self.path = path
        self.cameraId = cameraId


class OpcuaConfiguration:
    def __init__(self, scriptPath):
        self.scriptPath = scriptPath


class OutputConfiguration:
    def __init__(self, type, path=None, opcuaConfig=None):
        self.type = type
        self.path = path
        self.opcuaConfig = opcuaConfig


class StreamConfiguration:
    def __init__(self, streamId, name="", sources=[], modelName="", outputs=[], enabled=""):
        self.streamId = streamId
        self.name = name
        self.sources = sources
        self.modelName = modelName
        self.outputs = outputs
        self.enabled = enabled

    def __repr__(self):
        return "<StreamConfiguration(streamId={self.streamId!r})>".format(self=self)

    def get_source_by_type(self, srcType):
        for source in self.sources:
            if source.type == srcType:
                return source
        return None


class SourceConfigurationSchema(Schema):
    type = fields.Str()
    path = fields.Str(required=False, allow_none=True)
    cameraId = fields.Str(required=False)

    @post_load
    def make_source(self, data, **kwargs):
        return SourceConfiguration(**data)

    @validates_schema
    def validate_preview_vars(self, data, **kwargs):
        if data.get('type') not in VALID_SOURCE_TYPES:
            raise ValidationError('Unknown source type')
        if data.get('type') == "camera" and (data.get('cameraId') is None or data['cameraId']) == '':
            raise ValidationError('Camera id is missing')
        if data.get('type') == "file" and (data.get('path') is None or data['path']) == '':
            raise ValidationError('Path is missing')


class OpcuaConfigurationSchema(Schema):
    scriptPath = fields.Str()
    @post_load
    def make_source(self, data, **kwargs):
        return OpcuaConfiguration(**data)


class OutputConfigurationSchema(Schema):
    type = fields.Str()
    path = fields.Str()
    opcuaConfig = fields.Nested(OpcuaConfigurationSchema(), required = False)

    @post_load
    def make_source(self, data, **kwargs):
        return OutputConfiguration(**data)

    @post_dump
    def remove_skip_values(self, data, **kwargs):
        return {key: value for key, value in data.items() if value is not None}


class StreamConfigurationSchema(Schema):
    # Fix this to be workflowId, when change from cloud is made.
    streamId = fields.Str()
    name = fields.Str(required=False)
    sources = fields.List(fields.Nested(SourceConfigurationSchema()), required=False)
    modelName = fields.Str(required=False)
    outputs = fields.List(fields.Nested(OutputConfigurationSchema()), required=False)
    enabled = fields.Boolean(required=False)

    @post_load
    def make_stream(self, data, **kwargs):
        return StreamConfiguration(**data)

    @validates_schema
    def validate_preview_vars(self, data, **kwargs):
        if data.get('sources') is not None and len(data.get('sources')) > 1:
            raise ValidationError('Stream configuration currently supports 1 source')


class StreamConfigurationShadowStateConfiguration:
    def __init__(self, streams=None):
        self.streams = streams


class StreamConfigurationShadow:
    def __init__(self, desired=None, reported=None, delta=None):
        self.desired = desired
        self.reported = reported
        self.delta = delta


class StreamConfigurationShadowStateConfigurationSchema(Schema):
    streams = fields.List(fields.Nested(StreamConfigurationSchema()))

    @post_load
    def make_source(self, data, **kwargs):
        return StreamConfigurationShadowStateConfiguration(**data)


class StreamConfigurationShadowSchema(Schema):
    desired = fields.Nested(StreamConfigurationShadowStateConfigurationSchema(), required=False, unknown=EXCLUDE)
    reported = fields.Nested(StreamConfigurationShadowStateConfigurationSchema(), required=False, unknown=EXCLUDE)
    delta = fields.Nested(StreamConfigurationShadowStateConfigurationSchema(), required=False, unknown=EXCLUDE)

    @post_load
    def make_source(self, data, **kwargs):
        return StreamConfigurationShadow(**data)
