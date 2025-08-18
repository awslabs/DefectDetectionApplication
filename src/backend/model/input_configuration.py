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
from utils.constants import DIGITAL_IO_SIGNAL_TYPES

class InputConfiguration:
    def __init__(self, inputConfigurationId: str, creationTime: int, pin: str, triggerState: str, debounceTime: int):
        self.inputConfigurationId = inputConfigurationId
        self.creationTime = creationTime
        self.pin = pin
        self.triggerState = triggerState
        self.debounceTime = debounceTime

    def get(self, attr_name, default=None):
        return getattr(self, attr_name, default)

class InputConfigurationSchema(Schema):
    inputConfigurationId = fields.Str(required=True)
    creationTime = fields.Number()
    pin = fields.Str(required=True)
    triggerState = fields.Str(validate=validate.OneOf(DIGITAL_IO_SIGNAL_TYPES), required=True)
    debounceTime = fields.Int(required=True)

    @post_load
    def make_source(self, data, **kwargs):
        return InputConfiguration(**data)
