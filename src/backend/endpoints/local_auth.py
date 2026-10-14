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
"""Local_Login endpoints (portal-user-manager, task 9.1).

Two unauthenticated routes on the LocalServer FastAPI backend:

- ``GET /local-auth/status`` → ``{localLoginEnabled}`` — drives the web UI
  login gate (design D8). Always available, never requires credentials.
- ``POST /local-auth/login`` ``{username, password}``:

  - Local_Login_Configuration disabled → 403 ``local login is disabled``;
    a Local_Session_Token is never issued (Requirement 9.5).
  - Enabled → lockout check (Requirement 8.10) →
    ``verify_credentials`` against the Local_Credential_Cache →
    on success ``record_success`` + ``issue_token`` →
    ``{token, expiresAt, role, username}`` (Requirement 8.2);
    on failure ``record_failure`` + a **uniform** 401 whose status and body
    are identical whether the username exists, the password is wrong, or
    the account is locked out (Requirement 8.3).

Failed logins and lockout rejections are logged with the username only —
the submitted password never reaches a log record. The "no synchronized
accounts available" diagnostic for an empty cache (Requirement 11.3) is
logged inside ``credential_cache.verify_credentials``.

Both routes must be exempt from the ``authorize_request`` authorization
dependency; that exemption is wired where the routers are assembled
(task 9.4). This router intentionally carries no auth dependency itself.
"""
import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

from endpoints.route.access_log_router import AccessLogRoute
from local_auth import config as local_login_config
from local_auth import credential_cache, lockout, session_tokens

logger = logging.getLogger(__name__)

router = APIRouter(route_class=AccessLogRoute)

#: The single 401 detail used for every login failure — wrong password,
#: unknown username, disabled account, and lockout all produce a response
#: identical in status and body (Requirement 8.3).
UNIFORM_LOGIN_FAILURE_DETAIL = "invalid username or password"

#: The 403 detail returned while the Local_Login_Configuration is disabled
#: (Requirement 9.5).
LOCAL_LOGIN_DISABLED_DETAIL = "local login is disabled"


class LocalAuthStatusResponse(BaseModel):
    localLoginEnabled: bool


class LocalLoginRequest(BaseModel):
    username: str
    password: str


class LocalLoginResponse(BaseModel):
    token: str
    expiresAt: int
    role: str
    username: str


def _uniform_login_failure() -> HTTPException:
    """The one and only login-failure response (Requirement 8.3)."""
    return HTTPException(
        status_code=HTTP_401_UNAUTHORIZED, detail=UNIFORM_LOGIN_FAILURE_DETAIL
    )


@router.get("/local-auth/status")
def get_local_auth_status() -> LocalAuthStatusResponse:
    """Whether Local_Login is currently enabled on this device (D8)."""
    return LocalAuthStatusResponse(
        localLoginEnabled=local_login_config.is_local_login_enabled()
    )


@router.post("/local-auth/login")
def local_login(request: LocalLoginRequest) -> LocalLoginResponse:
    """Authenticate against the Local_Credential_Cache and issue a
    Local_Session_Token (Requirements 8.2, 8.3, 8.10, 9.5, 11.3)."""
    if not local_login_config.is_local_login_enabled():
        # Never issue a token while disabled (9.5).
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, detail=LOCAL_LOGIN_DISABLED_DETAIL
        )

    now = int(time.time())
    tracker = lockout.get_default_tracker()

    # Lockout check happens before any credential verification (8.10);
    # locked accounts get the same uniform 401 as any other failure.
    if tracker.is_locked(request.username, now=now):
        logger.warning(
            "Local login rejected for account %s: account is locked out",
            request.username,
        )
        raise _uniform_login_failure()

    # Constant-shape verification; logs the "no synchronized accounts
    # available" diagnostic itself when the cache is empty (11.3).
    account = credential_cache.verify_credentials(request.username, request.password)
    if account is None:
        tracker.record_failure(request.username, now=now)
        logger.warning(
            "Local login failed for account %s: credentials did not match "
            "an enabled synchronized account",
            request.username,
        )
        raise _uniform_login_failure()

    tracker.record_success(request.username, now=now)
    secret = session_tokens.get_or_create_secret()
    token = session_tokens.issue_token(secret, account.username, account.role, now)
    return LocalLoginResponse(
        token=token,
        expiresAt=now + session_tokens.TOKEN_LIFETIME_SECONDS,
        role=account.role,
        username=account.username,
    )
