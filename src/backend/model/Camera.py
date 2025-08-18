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


class Camera:
    def __init__(self, id, model, address, physical_id, protocol, serial, vendor):
        self.id = id
        self.model = model
        self.address = address
        self.physical_id = physical_id
        self.protocol = protocol
        self.serial = serial
        self.vendor = vendor

    def __repr__(self):
        return "<Camera(id={self.id!r})>".format(self=self)


class CameraSchema(Schema):
    id = fields.Str()
    model = fields.Str()
    address = fields.Str()
    physical_id = fields.Str()
    protocol = fields.Str()
    serial = fields.Str()
    vendor = fields.Str()

    @post_load
    def make_source(self, data, **kwargs):
        return Camera(**data)
