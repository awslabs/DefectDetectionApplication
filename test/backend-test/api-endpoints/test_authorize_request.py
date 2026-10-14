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
"""Example-based unit tests for the ``authorize_request`` dependency and the
router rewiring (portal-user-manager, task 9.4).

Drives the dependency through FastAPI (router built by the REAL
``get_api_router()``) with the remote introspection stubbed, covering the
full per-request decision matrix:

| Local login | Existing_Token_Auth | Decision                                |
|-------------|---------------------|-----------------------------------------|
| disabled    | not configured      | Allow — open access (9.1, 9.3)          |
| disabled    | configured          | Valid bearer required; else 401 (9.4,   |
|             |                     | 10.4)                                   |
| enabled     | not configured      | Valid Local_Session_Token; else 401     |
|             |                     | (8.5, 8.9)                              |
| enabled     | configured          | Either valid bearer OR valid local      |
|             |                     | token (10.1, 10.2, 10.5)                |

plus the path exemptions (/local-auth/login, /local-auth/status, static SPA
assets) and the download-file query-parameter token path.

Validates: Requirements 8.1, 8.5, 8.9, 9.1, 9.3, 9.4, 10.1, 10.2, 10.3,
10.4, 10.5, 11.2.
"""
import contextlib
import os
import sys
import time
import types
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Stub the runtime-image-only dependencies BEFORE importing the modules under
# test (the established sys.modules-stubbing pattern in this test tree). Only
# modules genuinely missing from the unit-test venv are stubbed; utils.auth
# and endpoints.route.access_log_router are imported for real.
# ---------------------------------------------------------------------------


def _register_stub(name, module):
    if name in sys.modules:
        return
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = module


_correlation_id = types.SimpleNamespace(get=lambda: "test-request-id")

_asgi_correlation_id = types.ModuleType("asgi_correlation_id")
_asgi_correlation_id.correlation_id = _correlation_id
_asgi_context = types.ModuleType("asgi_correlation_id.context")
_asgi_context.correlation_id = _correlation_id
_register_stub("asgi_correlation_id", _asgi_correlation_id)
_register_stub("asgi_correlation_id.context", _asgi_context)

_structlog = types.ModuleType("structlog")
_structlog.stdlib = types.SimpleNamespace(get_logger=lambda *a, **k: mock.Mock())
_structlog.contextvars = types.SimpleNamespace(
    clear_contextvars=lambda: None,
    bind_contextvars=lambda **kwargs: None,
)
_register_stub("structlog", _structlog)

_uvicorn = types.ModuleType("uvicorn")
_uvicorn_protocols = types.ModuleType("uvicorn.protocols")
_uvicorn_utils = types.ModuleType("uvicorn.protocols.utils")
_uvicorn_utils.get_path_with_query_string = lambda scope: scope.get("path", "/")
_register_stub("uvicorn", _uvicorn)
_register_stub("uvicorn.protocols", _uvicorn_protocols)
_register_stub("uvicorn.protocols.utils", _uvicorn_utils)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from endpoints.route.access_log_router import get_api_router  # noqa: E402
from local_auth import session_tokens  # noqa: E402
from utils import auth as auth_module  # noqa: E402
from utils.auth import authorize_credential, authorize_request  # noqa: E402

SECRET = b"\x07" * 32
OTHER_SECRET = b"\x08" * 32
VALID_BEARER = "remote-opaque-bearer-token"
AUTH_SETTINGS = {
    "clientId": "test-client-id",
    "clientSecret": "test-client-secret",
    "introspectEndpoint": "https://idp.example.com/oauth2/introspect",
}


def make_cache(enabled=True, username="operator1"):
    return {
        "accounts": {
            username: {"email": "op1@example.com", "role": "Operator", "enabled": enabled}
        }
    }


def make_local_token(secret=SECRET, expired=False, username="operator1"):
    issued_at = int(time.time())
    if expired:
        issued_at -= session_tokens.TOKEN_LIFETIME_SECONDS + 60
    return session_tokens.issue_token(secret, username, "Operator", issued_at)


class FakeIntrospection:
    """Stub for the remote introspection call, recording every invocation."""

    def __init__(self, active_tokens=()):
        self.active_tokens = set(active_tokens)
        self.calls = []

    def __call__(self, token, clientId, clientSecret, introspectEndpoint):
        self.calls.append(token)
        active = token in self.active_tokens
        return types.SimpleNamespace(
            status_code=200, json=lambda active=active: {"active": active}
        )


@contextlib.contextmanager
def configured(local_login_enabled, existing_auth, cache=None, active_bearers=()):
    """Pin one row of the decision matrix and stub the remote introspection."""
    introspection = FakeIntrospection(active_bearers)
    with mock.patch(
        "local_auth.config.is_local_login_enabled", return_value=local_login_enabled
    ), mock.patch(
        "utils.utils.is_authorization_enabled_on_station", return_value=existing_auth
    ), mock.patch(
        "utils.utils.get_authorization_settings_from_file",
        return_value=dict(AUTH_SETTINGS),
    ), mock.patch(
        "local_auth.session_tokens.get_or_create_secret", return_value=SECRET
    ), mock.patch(
        "local_auth.credential_cache.get_default_cache",
        return_value=cache if cache is not None else make_cache(),
    ), mock.patch(
        "utils.auth.validate_remotely", introspection
    ):
        yield introspection


class _WithClientAddress:
    """ASGI shim setting scope["client"]: the AccessLogRoute access logging
    dereferences request.client.host, which this starlette version's
    TestClient leaves unset."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope.get("client"):
            scope["client"] = ("testclient", 50000)
        await self.app(scope, receive, send)


def build_client():
    """A FastAPI app whose API router comes from the REAL get_api_router()."""
    app = FastAPI()
    router = get_api_router()

    @router.get("/protected")
    def protected():
        return {"ok": True}

    # Stand-ins for the exempt paths, deliberately mounted on the SAME
    # guarded router to prove the dependency's own path exemptions (in
    # production these live on an unauthenticated router as well).
    @router.get("/local-auth/status")
    def status_stub():
        return {"localLoginEnabled": True}

    @router.post("/local-auth/login")
    def login_stub():
        return {"login": "stub"}

    @router.get("/static/js/app.js")
    def static_asset_stub():
        return {"asset": True}

    app.include_router(router)
    return TestClient(_WithClientAddress(app))


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class TestRowOpenAccess:
    """Row (disabled, not configured): open access — today's behavior.

    Validates: Requirements 9.1, 9.3.
    """

    def test_request_without_credentials_is_allowed(self):
        with configured(local_login_enabled=False, existing_auth=False) as introspection:
            response = build_client().get("/protected")
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert introspection.calls == []

    def test_request_with_stray_credential_is_still_allowed(self):
        # Open access must not start rejecting clients that happen to send
        # an Authorization header.
        with configured(local_login_enabled=False, existing_auth=False):
            response = build_client().get("/protected", headers=bearer("anything"))
        assert response.status_code == 200


class TestRowExistingTokenAuthOnly:
    """Row (disabled, configured): existing bearer required.

    Validates: Requirements 9.4, 10.4.
    """

    def test_missing_credential_is_rejected_with_401_bearer_challenge(self):
        with configured(local_login_enabled=False, existing_auth=True) as introspection:
            response = build_client().get("/protected")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert introspection.calls == []

    def test_valid_bearer_is_authorized_via_remote_validation(self):
        with configured(
            local_login_enabled=False, existing_auth=True, active_bearers=[VALID_BEARER]
        ) as introspection:
            response = build_client().get("/protected", headers=bearer(VALID_BEARER))
        assert response.status_code == 200
        assert introspection.calls == [VALID_BEARER]

    def test_invalid_bearer_is_rejected_with_401(self):
        with configured(local_login_enabled=False, existing_auth=True):
            response = build_client().get("/protected", headers=bearer("bogus"))
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_local_session_token_is_not_accepted_while_local_login_disabled(self):
        # A perfectly valid Local_Session_Token must not authorize when the
        # Local_Login_Configuration is disabled; it falls through to the
        # remote validation, which does not know it.
        token = make_local_token()
        with configured(local_login_enabled=False, existing_auth=True) as introspection:
            response = build_client().get("/protected", headers=bearer(token))
        assert response.status_code == 401
        assert introspection.calls == [token]


class TestRowLocalLoginOnly:
    """Row (enabled, not configured): Local_Session_Token required.

    Validates: Requirements 8.5, 8.9.
    """

    def test_missing_credential_is_rejected_with_401_bearer_challenge(self):
        with configured(local_login_enabled=True, existing_auth=False):
            response = build_client().get("/protected")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_valid_local_session_token_is_authorized_without_any_remote_call(self):
        with configured(local_login_enabled=True, existing_auth=False) as introspection:
            response = build_client().get(
                "/protected", headers=bearer(make_local_token())
            )
        assert response.status_code == 200
        assert introspection.calls == []

    def test_expired_local_session_token_is_rejected(self):
        with configured(local_login_enabled=True, existing_auth=False):
            response = build_client().get(
                "/protected", headers=bearer(make_local_token(expired=True))
            )
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_foreign_secret_local_session_token_is_rejected(self):
        with configured(local_login_enabled=True, existing_auth=False):
            response = build_client().get(
                "/protected", headers=bearer(make_local_token(secret=OTHER_SECRET))
            )
        assert response.status_code == 401

    def test_token_of_disabled_account_is_rejected(self):
        with configured(
            local_login_enabled=True, existing_auth=False, cache=make_cache(enabled=False)
        ):
            response = build_client().get(
                "/protected", headers=bearer(make_local_token())
            )
        assert response.status_code == 401

    def test_opaque_bearer_is_rejected_and_never_validated_remotely(self):
        with configured(local_login_enabled=True, existing_auth=False) as introspection:
            response = build_client().get("/protected", headers=bearer(VALID_BEARER))
        assert response.status_code == 401
        assert introspection.calls == []


class TestRowBothMechanisms:
    """Row (enabled, configured): either credential kind authorizes.

    Validates: Requirements 10.1, 10.2, 10.5.
    """

    def test_valid_local_session_token_is_authorized_locally_first(self):
        with configured(
            local_login_enabled=True, existing_auth=True, active_bearers=[VALID_BEARER]
        ) as introspection:
            response = build_client().get(
                "/protected", headers=bearer(make_local_token())
            )
        assert response.status_code == 200
        # Validated locally: the remote introspection is never consulted.
        assert introspection.calls == []

    def test_valid_existing_bearer_is_authorized(self):
        with configured(
            local_login_enabled=True, existing_auth=True, active_bearers=[VALID_BEARER]
        ) as introspection:
            response = build_client().get("/protected", headers=bearer(VALID_BEARER))
        assert response.status_code == 200
        assert introspection.calls == [VALID_BEARER]

    def test_locally_invalid_two_segment_credential_falls_back_to_remote(self):
        # A credential that merely looks like a Local_Session_Token but fails
        # local validation must still be given to the existing remote
        # validation (it could be a remote token of coincidental shape).
        foreign = make_local_token(secret=OTHER_SECRET)
        with configured(
            local_login_enabled=True, existing_auth=True, active_bearers=[foreign]
        ) as introspection:
            response = build_client().get("/protected", headers=bearer(foreign))
        assert response.status_code == 200
        assert introspection.calls == [foreign]

    def test_credential_valid_under_neither_mechanism_is_rejected(self):
        with configured(local_login_enabled=True, existing_auth=True):
            response = build_client().get("/protected", headers=bearer("bogus"))
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_missing_credential_is_rejected(self):
        with configured(local_login_enabled=True, existing_auth=True):
            response = build_client().get("/protected")
        assert response.status_code == 401


class TestExemptPaths:
    """Login, status, and static SPA assets stay reachable in every
    configuration, even in the most locked-down rows.

    Validates: Requirements 8.1 (login screen/endpoint exemption), 9.5
    exemption plumbing.
    """

    @pytest.mark.parametrize(
        "local_login_enabled,existing_auth",
        [(False, False), (False, True), (True, False), (True, True)],
    )
    def test_exempt_paths_reachable_without_credentials(
        self, local_login_enabled, existing_auth
    ):
        with configured(local_login_enabled, existing_auth):
            client = build_client()
            assert client.get("/local-auth/status").status_code == 200
            assert client.post("/local-auth/login").status_code == 200
            assert client.get("/static/js/app.js").status_code == 200

    def test_non_exempt_path_still_guarded_in_same_configuration(self):
        with configured(local_login_enabled=True, existing_auth=False):
            client = build_client()
            assert client.get("/protected").status_code == 401
            assert client.get("/local-auth/status").status_code == 200


class TestRouterWiring:
    """get_api_router attaches authorize_request unconditionally: the
    decision is made per request, not frozen at import/startup time.

    Validates: Requirement 11.2.
    """

    @pytest.mark.parametrize("settings_file_present", [True, False])
    def test_dependency_attached_regardless_of_settings_file(
        self, settings_file_present
    ):
        with mock.patch(
            "utils.utils.is_authorization_enabled_on_station",
            return_value=settings_file_present,
        ):
            router = get_api_router()
        dependency_calls = [dep.dependency for dep in router.dependencies]
        assert dependency_calls == [authorize_request]


class TestDownloadFileQueryParamTokenPath:
    """The download-file query-parameter token path applies the same
    either/or acceptance through authorize_credential.

    Validates: Requirements 9.1, 10.1, 10.2, 10.4.
    """

    def test_open_access_row_allows_missing_query_token(self):
        with configured(local_login_enabled=False, existing_auth=False):
            assert authorize_credential(None) is None

    def test_valid_local_session_token_in_query_param_is_accepted(self):
        with configured(local_login_enabled=True, existing_auth=True):
            assert authorize_credential(make_local_token()) is None

    def test_valid_existing_bearer_in_query_param_is_accepted(self):
        with configured(
            local_login_enabled=True, existing_auth=True, active_bearers=[VALID_BEARER]
        ):
            assert authorize_credential(VALID_BEARER) is None

    def test_invalid_query_token_is_rejected_with_401_bearer_challenge(self):
        from fastapi import HTTPException

        with configured(local_login_enabled=True, existing_auth=True):
            with pytest.raises(HTTPException) as exc_info:
                authorize_credential("bogus")
        assert exc_info.value.status_code == 401
        assert exc_info.value.headers["WWW-Authenticate"] == "Bearer"

    def test_missing_query_token_is_rejected_when_local_login_enabled(self):
        from fastapi import HTTPException

        with configured(local_login_enabled=True, existing_auth=False):
            with pytest.raises(HTTPException) as exc_info:
                authorize_credential(None)
        assert exc_info.value.status_code == 401

    def test_download_file_endpoint_delegates_to_authorize_credential(self):
        # endpoints/download_file.py drags in heavy runtime-only deps
        # (sqlalchemy, server accessors), so the delegation is verified at
        # source level: validate_token_in_query_param must call
        # authorize_credential and must no longer gate on the settings-file
        # presence check itself.
        source_path = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "..", "src", "backend", "endpoints", "download_file.py",
        )
        with open(source_path, "r", encoding="utf-8") as source_file:
            source = source_file.read()
        assert "from utils.auth import authorize_credential" in source
        body = source.split("def validate_token_in_query_param(token: str):")[1]
        body = body.split("###")[0]
        assert "authorize_credential(token)" in body
        assert "is_authorization_enabled_on_station" not in body
