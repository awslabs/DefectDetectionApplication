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
from marshmallow import Schema, fields, post_load

class Station:
    def __init__(self, name: str, version="N/A", device_id="N/A", tenant_id="N/A", webux_url="N/A"):
        self.name = name
        self.version = version
        self.deviceId = device_id
        self.tenantId = tenant_id
        self.webuxUrl = webux_url

    def __repr__(self):
        return "<Station(name={self.name!r} \
                         version={self.version!r} \
                         deviceId={self.deviceId!r} \
                         tenantId={self.tenantId!r} \
                         webuxUrl={self.webuxUrl!r})>".format(self=self)


class StationSchema(Schema):
    name = fields.Str()
    version = fields.Str()
    deviceId = fields.Str()
    tenantId = fields.Str()
    webuxUrl = fields.Str()

    @post_load
    def make_source(self, data, **kwargs):
        return Station(**data)