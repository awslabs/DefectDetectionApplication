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
import base64

import httpx
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
import logging

from metrics.collector import Timer
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_500_INTERNAL_SERVER_ERROR

from utils import utils

logger = logging.getLogger(__name__)

# Define the auth scheme and access token URL
oauth2_scheme = OAuth2PasswordBearer(tokenUrl='token')


def validate_remotely(token, clientId, clientSecret, introspectEndpoint):
    dda_client_credentials = f'{clientId}:{clientSecret}'
    basic_auth_header = base64.b64encode(dda_client_credentials.encode()).decode()
    headers = {
        'accept': 'application/json',
        'cache-control': 'no-cache',
        'content-type': 'application/x-www-form-urlencoded',
        'Authorization': f'Basic {basic_auth_header}'
    }
    data = {
        'client_id': clientId,
        'client_secret': clientSecret,
        'token': token,
    }
    try:
        response = httpx.post(
            introspectEndpoint,
            headers=headers,
            data=data
        )
        return response
    except Exception as e:
        logger.error(f"Error occured while trying to validate authorization token. Error: {e}")
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal Server Error. Please check logs for more information.",
            headers={"WWW-Authenticate": "Bearer"},
        )

def validate_token(token: str = Depends(oauth2_scheme)):
    # Raise 401 if bearer token not provided and auth enabled
    if (not token) and utils.is_authorization_enabled_on_station():
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Fetch the authorization details from file.
    auth_settings = utils.get_authorization_settings_from_file()

    with Timer(metric_name="AuthTotalTime") as t:
        response = validate_remotely(
            token,
            auth_settings.get("clientId"),
            auth_settings.get("clientSecret"),
            auth_settings.get("introspectEndpoint")
        )
        if (not response) \
            or (response.status_code != httpx.codes.OK) \
            or (not response.json()['active']):
            raise HTTPException(
                status_code=HTTP_401_UNAUTHORIZED,
                detail=f"Access Denied",
                headers={"WWW-Authenticate": "Bearer"},
            )
    logger.info(f"Auth Total Time taken: {t.elapsed_time}")
