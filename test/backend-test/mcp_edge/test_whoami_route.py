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
"""``GET /local-auth/whoami`` on the authenticated router.

Feature: mcp-servers (design §6 ``endpoints/local_auth.py`` row, Data Models
"``AuthDecision`` and ``GET /local-auth/whoami``", F8; tasks.md 10.4).
Requirements 9.1, 9.3, 10.4, 12.7.

Each row of the decision matrix through the REAL ``authorize_request``
dependency (``get_api_router()``), ``expiresAt`` from the validated token,
the Existing_Token_Auth username from a faked introspection, and both
runtime shapes of the credential cache (mapping and ``Account`` objects).
"""
import contextlib
import json
import time
import types
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from endpoints import local_auth as local_auth_endpoints
from local_auth import credential_cache, session_tokens

SECRET = b"\x07" * 32
OTHER_SECRET = b"\x08" * 32
AUTH_SETTINGS = {
    "clientId": "test-client-id",
    "clientSecret": "test-client-secret",
    "introspectEndpoint": "https://idp.example.com/oauth2/introspect",
}
BEARER = "remote-opaque-bearer-token"


def mapping_cache(role="Operator", enabled=True, username="operator1"):
    return {"accounts": {username: {"email": f"{username}@example.com",
                                    "role": role, "enabled": enabled}}}


class FakeIntrospection:
    def __init__(self, active=(), payload=None):
        self.active = set(active)
        self.payload = payload or {}
        self.calls = []

    def __call__(self, token, clientId, clientSecret, introspectEndpoint):
        self.calls.append(token)
        body = dict(self.payload, active=token in self.active)
        return types.SimpleNamespace(status_code=200, json=lambda body=body: body)


@contextlib.contextmanager
def configured(local_login_enabled, existing_auth, cache=None, introspection=None):
    introspection = introspection or FakeIntrospection()
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
        return_value=cache if cache is not None else mapping_cache(),
    ), mock.patch("utils.auth.validate_remotely", introspection):
        yield introspection


class _WithClientAddress:
    """Sets scope["client"]: AccessLogRoute reads request.client.host."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope.get("client"):
            scope["client"] = ("testclient", 50000)
        await self.app(scope, receive, send)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(local_auth_endpoints.router)
    app.include_router(local_auth_endpoints.authenticated_router)
    return TestClient(_WithClientAddress(app))


def token(username="operator1", role="Operator", secret=SECRET, expired=False):
    issued_at = int(time.time())
    if expired:
        issued_at -= session_tokens.TOKEN_LIFETIME_SECONDS + 60
    value = session_tokens.issue_token(secret, username, role, issued_at)
    return value, issued_at + session_tokens.TOKEN_LIFETIME_SECONDS


def bearer(value):
    return {"Authorization": f"Bearer {value}"}


def test_routers_are_separate():
    unauthenticated = {route.path for route in local_auth_endpoints.router.routes}
    authenticated = {route.path for route in
                     local_auth_endpoints.authenticated_router.routes}
    assert unauthenticated == {"/local-auth/status", "/local-auth/login"}
    assert authenticated == {"/local-auth/whoami"}
    assert local_auth_endpoints.router.dependencies == []
    assert len(local_auth_endpoints.authenticated_router.dependencies) == 1


# --------------------------------------------------- open-access row

def test_open_access_is_anonymous_without_expiry(client):
    with configured(False, False):
        response = client.get("/local-auth/whoami")
    assert response.status_code == 200
    assert response.json() == {"username": "anonymous", "role": None,
                               "source": "anonymous"}


def test_open_access_ignores_a_stray_credential(client):
    value, _ = token()
    with configured(False, False):
        response = client.get("/local-auth/whoami", headers=bearer(value))
    assert response.json()["source"] == "anonymous"


# --------------------------------------------------- local login only

def test_local_session_token(client):
    value, exp = token(role="Viewer")
    # The role is the synced account's, not the token's claim.
    with configured(True, False, cache=mapping_cache(role="Operator")) as remote:
        response = client.get("/local-auth/whoami", headers=bearer(value))
    assert response.status_code == 200
    assert response.json() == {"username": "operator1", "role": "Operator",
                               "source": "local_session", "expiresAt": exp}
    assert remote.calls == []


def test_local_session_token_with_account_objects(client, tmp_path):
    # The on-device cache returns credential_cache.Account objects.
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(mapping_cache(role="UseCaseAdmin",
                                             username="admin.two")))
    cache = credential_cache.CredentialCache(str(path))
    assert isinstance(cache.get_account("admin.two"), credential_cache.Account)
    value, exp = token(username="admin.two", role="Viewer")
    with configured(True, False, cache=cache):
        response = client.get("/local-auth/whoami", headers=bearer(value))
    assert response.json() == {"username": "admin.two", "role": "UseCaseAdmin",
                               "source": "local_session", "expiresAt": exp}


@pytest.mark.parametrize("credential", [
    lambda: None,
    lambda: token(expired=True)[0],
    lambda: token(secret=OTHER_SECRET)[0],
    lambda: BEARER,
])
def test_local_only_rejections(client, credential):
    value = credential()
    headers = bearer(value) if value else {}
    with configured(True, False):
        response = client.get("/local-auth/whoami", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_disabled_account_is_rejected(client):
    value, _ = token()
    with configured(True, False, cache=mapping_cache(enabled=False)):
        response = client.get("/local-auth/whoami", headers=bearer(value))
    assert response.status_code == 401


# --------------------------------------------- Existing_Token_Auth only

@pytest.mark.parametrize("payload,username,expires", [
    ({"username": "remote-user", "sub": "remote-sub", "exp": 1760043200},
     "remote-user", 1760043200),
    ({"sub": "remote-sub"}, "remote-sub", None),
    ({}, "unknown", None),
])
def test_existing_token_auth(client, payload, username, expires):
    introspection = FakeIntrospection(active=(BEARER,), payload=payload)
    with configured(False, True, introspection=introspection):
        response = client.get("/local-auth/whoami", headers=bearer(BEARER))
    assert response.status_code == 200
    assert response.json() == {"username": username, "role": None,
                               "source": "existing_token_auth",
                               "expiresAt": expires}
    assert introspection.calls == [BEARER]


def test_existing_token_auth_rejects_inactive_bearer(client):
    with configured(False, True, introspection=FakeIntrospection()):
        response = client.get("/local-auth/whoami", headers=bearer(BEARER))
    assert response.status_code == 401


def test_query_token_is_not_read(client):
    introspection = FakeIntrospection(active=(BEARER,), payload={"sub": "s"})
    with configured(False, True, introspection=introspection):
        response = client.get("/local-auth/whoami", params={"token": BEARER})
    assert response.status_code == 401
    assert introspection.calls == []


# ------------------------------------------------- both mechanisms

def test_both_prefers_the_local_token(client):
    value, exp = token()
    introspection = FakeIntrospection(active=(value,), payload={"sub": "remote"})
    with configured(True, True, introspection=introspection):
        response = client.get("/local-auth/whoami", headers=bearer(value))
    assert response.json() == {"username": "operator1", "role": "Operator",
                               "source": "local_session", "expiresAt": exp}
    assert introspection.calls == []


def test_both_accepts_the_bearer(client):
    introspection = FakeIntrospection(active=(BEARER,),
                                      payload={"username": "remote-user"})
    with configured(True, True, introspection=introspection):
        response = client.get("/local-auth/whoami", headers=bearer(BEARER))
    assert response.json()["source"] == "existing_token_auth"
    assert response.json()["username"] == "remote-user"


def test_both_rejects_neither(client):
    with configured(True, True):
        response = client.get("/local-auth/whoami", headers=bearer("nope"))
    assert response.status_code == 401


# ------------------------------------------ unauthenticated routes unchanged

def test_status_stays_unauthenticated(client):
    with configured(True, True):
        response = client.get("/local-auth/status")
    assert response.status_code == 200
    assert response.json() == {"localLoginEnabled": True}
