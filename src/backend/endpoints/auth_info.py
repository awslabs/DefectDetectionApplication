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
# Fastapi modules
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import logging

from endpoints.route.access_log_router import AccessLogRoute
from utils import utils

router = APIRouter(route_class=AccessLogRoute)
logger = logging.getLogger(__name__)


class AuthSettingsModel(BaseModel):
    clientId: str
    authorizationEndpoint: str
    logoutEndpoint: Optional[str] = None

class AuthSettingsResponse(BaseModel):
    auth_enabled: bool
    auth_settings: Optional[AuthSettingsModel] = None


@router.get("/authorization-configurations")
def getAuthSettings() -> AuthSettingsResponse:
    if utils.is_authorization_enabled_on_station():
        auth_settings = utils.get_authorization_settings_from_file()
        return AuthSettingsResponse(
            auth_enabled=True,
            auth_settings=AuthSettingsModel(
                clientId=auth_settings.get("clientId"),
                authorizationEndpoint=auth_settings.get("authorizationEndpoint"),
                logoutEndpoint=auth_settings.get("logoutEndpoint"),
            )
        )
    return AuthSettingsResponse(auth_enabled=False)