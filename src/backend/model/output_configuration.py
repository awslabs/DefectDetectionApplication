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
from utils.constants import DIGITAL_IO_SIGNAL_TYPES, OUTPUT_RULE


class OutputConfiguration:
    def __init__(self, outputConfigurationId: str, pin: str, signalType: str, pulseWidth: int, creationTime: int, rule: str):
        self.outputConfigurationId = outputConfigurationId
        self.pin = pin
        self.signalType = signalType
        self.pulseWidth = pulseWidth
        self.creationTime = creationTime
        self.rule = rule

    def get(self, attr_name, default=None):
        return getattr(self, attr_name, default)

    def __repr__(self):
        return "<OutputConfiguration(outputConfigurationId={self.outputConfigurationId!r})>".format(self=self)


class OutputConfigurationSchema(Schema):
    outputConfigurationId = fields.Str(required=True)
    pin = fields.Str(required=True)
    signalType = fields.Str(validate=validate.OneOf(DIGITAL_IO_SIGNAL_TYPES), required=True)
    pulseWidth = fields.Int(required=True)
    creationTime = fields.Number()
    rule = fields.Str(validate=validate.OneOf(OUTPUT_RULE), required=True)

    @post_load
    def make_source(self, data, **kwargs):
        return OutputConfiguration(**data)
