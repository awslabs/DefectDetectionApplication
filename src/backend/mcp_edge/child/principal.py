# Copyright 2025 Amazon Web Services, Inc.
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
"""Principal resolution of the child (design §5 ``child/principal.py``, §7
step 1; Requirements 9.1, 9.4, 9.5, 10.4).

``resolve_principal(authorization_header, request_id)`` asks the
Backend_API who the caller is: ``GET /local-auth/whoami`` with the caller's
own ``Authorization`` header and ``X-Request-ID``. The Backend_API's
``authorize_request`` makes the decision; the child never reads a token, so
the role comes only from the ``whoami`` body. A 401 (no, an expired or an
invalid credential; a disabled account) is ``not_authenticated``. The call
is made on every tool call, so a credential that stops being valid between
two calls is refused on the next one.
"""
import logging
from dataclasses import dataclass
from typing import Optional, Union

import httpx2

from mcp_edge.child import dispatch
from mcp_edge.child.dispatch import ToolFailure

logger = logging.getLogger(__name__)

WHOAMI_PATH = '/local-auth/whoami'
WHOAMI_TIMEOUT = httpx2.Timeout(connect=2.0, read=10.0, write=10.0, pool=2.0)
SOURCES = ('local_session', 'existing_token_auth', 'anonymous')


@dataclass(frozen=True)
class Principal:
    username: str
    role: Optional[str]
    source: str
    expires_at: Optional[int] = None


def resolve_principal(authorization_header: Optional[str],
                      request_id: str) -> Union[Principal, ToolFailure]:
    headers = {'X-Request-ID': request_id}
    if authorization_header:
        headers['Authorization'] = authorization_header
    try:
        response = dispatch.client().get(WHOAMI_PATH, headers=headers, timeout=WHOAMI_TIMEOUT)
    except httpx2.HTTPError as error:
        logger.warning('whoami call failed: %s', type(error).__name__)
        return ToolFailure('backend_failure', 'the Backend_API is unreachable')
    if response.status_code == 401:
        return ToolFailure('not_authenticated', 'a valid credential is required', status=401)
    if response.status_code != 200:
        return dispatch.failure_for(dispatch.BackendResponse(
            response.status_code, response.headers.get('content-type', ''), response.content))
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get('source') not in SOURCES \
            or not isinstance(body.get('username'), str):
        logger.warning('whoami returned an unexpected body')
        return ToolFailure('backend_failure', 'unexpected identity response')
    role = body.get('role')
    expires_at = body.get('expiresAt')
    return Principal(username=body['username'],
                     role=role if isinstance(role, str) else None,
                     source=body['source'],
                     expires_at=expires_at if isinstance(expires_at, int) else None)
