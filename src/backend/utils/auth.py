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
import json
import re
import time
from typing import NamedTuple, Optional

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
import logging

from metrics.collector import Timer
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_500_INTERNAL_SERVER_ERROR

from local_auth import config as local_login_config
from local_auth import credential_cache, session_tokens
from utils import utils

logger = logging.getLogger(__name__)

# Define the auth scheme and access token URL
oauth2_scheme = OAuth2PasswordBearer(tokenUrl='token')

# Non-erroring bearer extraction for authorize_request: the decision matrix
# has an open-access row, so "no credential presented" must reach the
# dependency instead of being auto-rejected by the scheme.
optional_oauth2_scheme = OAuth2PasswordBearer(tokenUrl='token', auto_error=False)

# Paths reachable in every configuration (portal-user-manager, Requirements
# 8.1/9.5): the local login endpoint and its status probe must work before
# any credential exists, and static SPA assets must load so the login screen
# itself can render.
AUTHORIZATION_EXEMPT_PATHS = frozenset({"/local-auth/login", "/local-auth/status"})
AUTHORIZATION_EXEMPT_PATH_PREFIXES = ("/static/",)

# Local_Session_Token shape (design D5): exactly two non-empty base64url
# segments. Anything else is never treated as a local token.
_LOCAL_SESSION_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


class AuthDecision(NamedTuple):
    """Who a request was authorized as (mcp-servers design §6, M3).

    ``source`` is ``'local_session'``, ``'existing_token_auth'`` or
    ``'anonymous'`` (open access and exempt paths). ``role`` is the synced
    role of a Local_Session_Token's account, else None. ``expires_at`` is the
    credential's ``exp`` when known. Surfacing it changes no decision: the
    matrix and every raise of :func:`authorize_credential` are unchanged.
    """

    source: str
    username: str
    role: Optional[str]
    expires_at: Optional[int]


ANONYMOUS_DECISION = AuthDecision("anonymous", "anonymous", None, None)


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
    _introspect_token(token)


def _introspect_token(token) -> dict:
    """The body of :func:`validate_token`, returning the introspection JSON
    of an accepted bearer. Same settings read, remote call, timer and 401
    behaviour; only data already in hand is returned."""
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
    return response.json()


def _raise_unauthorized():
    raise HTTPException(
        status_code=HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def is_local_session_token_shaped(credential: str) -> bool:
    """True when ``credential`` has the Local_Session_Token wire shape:
    two non-empty base64url segments separated by a dot (design D5)."""
    return bool(_LOCAL_SESSION_TOKEN_PATTERN.match(credential))


def _validate_local_session_token(credential: str):
    """Validate a Local_Session_Token entirely locally (Requirement 8.5).

    Uses the shared signing secret and the live Local_Credential_Cache so a
    disabled or removed account instantly invalidates its outstanding tokens
    (Requirement 8.6, design D6). Never touches the network.

    Returns the token's account (as ``session_tokens.validate_token``
    returns it) when the token is valid, else None.
    """
    try:
        secret = session_tokens.get_or_create_secret()
    except OSError as error:
        logger.warning(
            "Local session token rejected: signing secret unavailable: %s", error
        )
        return None
    result = session_tokens.validate_token(
        secret, credential, int(time.time()), credential_cache.get_default_cache()
    )
    if isinstance(result, session_tokens.AuthError):
        # Reason is logged only; the HTTP response stays uniform (8.7).
        logger.info("Local session token rejected: %s", result.reason)
        return None
    return result


def _session_token_payload(credential: str) -> dict:
    """The decoded payload segment of an already validated
    Local_Session_Token (the signature was checked by
    :func:`_validate_local_session_token`); ``{}`` when it does not decode."""
    try:
        payload_b64 = credential.split(".", 1)[0]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, TypeError, AttributeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _session_token_expiry(credential: str) -> Optional[int]:
    """The ``exp`` of an already validated Local_Session_Token, else None.
    Pure."""
    exp = _session_token_payload(credential).get("exp")
    if isinstance(exp, int) and not isinstance(exp, bool):
        return exp
    return None


def _local_session_decision(account, credential: str) -> AuthDecision:
    """The decision for a valid Local_Session_Token. The credential cache
    may hold accounts as objects or as mappings keyed by username; the
    token's ``sub`` is the username the account was looked up by."""
    if isinstance(account, dict):
        username, role = account.get("username"), account.get("role")
    else:
        username = getattr(account, "username", None)
        role = getattr(account, "role", None)
    if not username:
        username = _session_token_payload(credential).get("sub")
    return AuthDecision(
        "local_session", username, role, _session_token_expiry(credential)
    )


def authorize_credential(credential: Optional[str]) -> Optional[AuthDecision]:
    """Per-request authorization decision matrix (portal-user-manager).

    | Local login | Existing_Token_Auth | Decision                            |
    |-------------|---------------------|-------------------------------------|
    | disabled    | not configured      | Allow — open access (9.1, 9.3)      |
    | disabled    | configured          | Valid bearer via ``validate_token`` |
    |             |                     | required; else 401 (9.4, 10.4)      |
    | enabled     | not configured      | Valid Local_Session_Token required; |
    |             |                     | else 401 (8.5, 8.9)                 |
    | enabled     | configured          | Either valid bearer OR valid        |
    |             |                     | Local_Session_Token (10.1/10.2/10.5)|

    Existing_Token_Auth state comes solely from the settings-file presence
    check — this function never reads or writes the file's contents beyond
    that existing check (10.3). Two-segment base64url credentials are tried
    locally first, falling back to the existing remote ``validate_token``
    when Existing_Token_Auth is configured. Raises HTTP 401 with
    ``WWW-Authenticate: Bearer`` when no acceptance rule matches.

    Returns None in the open-access row, else the :class:`AuthDecision`
    naming the accepted credential (callers that ignore the value are
    unaffected).
    """
    local_login_enabled = local_login_config.is_local_login_enabled()
    existing_token_auth = utils.is_authorization_enabled_on_station()

    # Row (disabled, not configured): open access — today's behavior, pinned.
    if not local_login_enabled and not existing_token_auth:
        return None

    if not credential:
        _raise_unauthorized()

    # A Local_Session_Token authorizes only while local login is enabled
    # (8.5, 10.2); validated locally first (never remotely).
    if local_login_enabled and is_local_session_token_shaped(credential):
        account = _validate_local_session_token(credential)
        if account is not None:
            return _local_session_decision(account, credential)

    # Fall back to the existing remote bearer validation when configured
    # (10.1, 10.4); _introspect_token raises its own 401 on rejection.
    if existing_token_auth:
        introspection = _introspect_token(credential)
        if not isinstance(introspection, dict):
            introspection = {}
        return AuthDecision(
            "existing_token_auth",
            introspection.get("username") or introspection.get("sub") or "unknown",
            None,
            introspection.get("exp"),
        )

    _raise_unauthorized()


def authorize_request(
    request: Request, token: Optional[str] = Depends(optional_oauth2_scheme)
) -> None:
    """FastAPI dependency applying the decision matrix per request.

    Evaluated on every request (not frozen at import time), so runtime
    Local_Login_Configuration changes take effect on subsequent requests
    without a process restart (Requirement 11.2). The login/status endpoints
    and static SPA assets are exempt in every configuration.

    Stores the decision as ``request.state.auth_decision`` (anonymous for
    open access and exempt paths) for ``GET /local-auth/whoami`` and the
    ``/mcp`` route.
    """
    path = request.url.path
    if path in AUTHORIZATION_EXEMPT_PATHS or path.startswith(
        AUTHORIZATION_EXEMPT_PATH_PREFIXES
    ):
        request.state.auth_decision = ANONYMOUS_DECISION
        return
    decision = authorize_credential(token)
    request.state.auth_decision = decision or ANONYMOUS_DECISION
