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
from utils.constants import LATENCY_TYPES

class LatencyTime:
    def __init__(self, inferenceCaptureId, latencyType, timestamp):
        self.inferenceCaptureId = inferenceCaptureId
        self.latencyType = latencyType
        self.timestamp = timestamp

    def get(self, attr_name, default = None):
        return getattr(self, attr_name, default)
    
class LatencyTimeSchema(Schema):
    inferenceCaptureId = fields.Str(required=True)
    latencyType = fields.Str(validate=validate.OneOf(LATENCY_TYPES), required=True)
    timestamp = fields.Float(required=True)

    @post_load
    def make_source(self, data, **kwargs):
        return LatencyTime(**data)
        
    