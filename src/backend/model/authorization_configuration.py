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

class AuthorizationSettings:
    def __init__(self, clientId, clientSecret, tokenEndpoint, authorizationEndpoint, introspectEndpoint, logoutEndpoint):
        self.clientId = clientId
        self.clientSecret = clientSecret
        self.tokenEndpoint = tokenEndpoint
        self.authorizationEndpoint = authorizationEndpoint
        self.introspectEndpoint = introspectEndpoint
        self.logoutEndpoint = logoutEndpoint

    def get(self, attr_name, default=None):
        return getattr(self, attr_name, default)
    
class AuthorizationSettingsSchema(Schema):
    clientId = fields.Str(required=True)
    clientSecret = fields.Str(required=True)
    tokenEndpoint = fields.Str(required=False)
    authorizationEndpoint = fields.Str(required=True)
    introspectEndpoint = fields.Str(required=False)
    logoutEndpoint = fields.Str(required=False)

    @post_load
    def make_source(self, data, **kwargs):
        return AuthorizationSettings(**data)
