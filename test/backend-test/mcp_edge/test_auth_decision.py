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
"""Property test: the decision matrix is unchanged by ``AuthDecision``.

**Feature: mcp-servers, Property 14: Unchanged matrix after `AuthDecision`**

Spec: .kiro/specs/mcp-servers/ (design.md §6 ``utils/auth.py`` row, Data
Models "``AuthDecision`` and ``GET /local-auth/whoami``", Correctness
Property 14; tasks.md 10.3). Validates: Requirements 9.1, 12.7.

For any combination of the matrix inputs (local login on/off,
Existing_Token_Auth configured or not, the presented credential, the
credential cache's account shape and state, the introspection answer),
``authorize_credential`` raises HTTP 401 exactly when the documented matrix
rejects, returns None exactly in the open-access row, and otherwise returns
an ``AuthDecision`` whose ``source`` names the admitting rule, whose
``username``/``role`` are the validated account's for a Local_Session_Token
and whose ``expires_at`` is the token payload's ``exp``. The matrix is
driven with the ``configured(...)`` pattern of
``api-endpoints/test_authorize_request.py``.
"""
import contextlib
import json
import os
import tempfile
import time
import types
from unittest import mock

import pytest
from fastapi import HTTPException
from hypothesis import given, settings
from hypothesis import strategies as st

from local_auth import credential_cache, session_tokens
from utils import auth as auth_module
from utils.auth import ANONYMOUS_DECISION, AuthDecision, authorize_credential

SECRET = b"\x07" * 32
OTHER_SECRET = b"\x08" * 32
AUTH_SETTINGS = {
    "clientId": "test-client-id",
    "clientSecret": "test-client-secret",
    "introspectEndpoint": "https://idp.example.com/oauth2/introspect",
}
ROLES = ("PortalAdmin", "UseCaseAdmin", "DataScientist", "Operator",
         "Viewer", "DataLabeler")
USERNAMES = ("operator1", "admin.two", "viewer-3")
BEARERS = ("remote-opaque-bearer", "remote.bearer")   # the second is token-shaped
_CACHE_DIR = tempfile.mkdtemp(prefix="mcp-auth-decision-")


class FakeIntrospection:
    """The remote introspection call: active for `active` tokens, with the
    given payload."""

    def __init__(self, active=(), payload=None):
        self.active = set(active)
        self.payload = payload or {}
        self.calls = []

    def __call__(self, token, clientId, clientSecret, introspectEndpoint):
        self.calls.append(token)
        body = dict(self.payload, active=token in self.active)
        return types.SimpleNamespace(status_code=200, json=lambda body=body: body)


@contextlib.contextmanager
def configured(local_login_enabled, existing_auth, cache, introspection):
    """Pin one row of the decision matrix and stub the remote introspection."""
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
        "local_auth.credential_cache.get_default_cache", return_value=cache
    ), mock.patch("utils.auth.validate_remotely", introspection):
        yield introspection


def cache_of(shape, username, role, enabled):
    """The credential cache in one of its two runtime shapes: a mapping (the
    cache-file shape, records keyed by username) or the on-device
    ``CredentialCache`` returning ``Account`` objects."""
    record = {"email": f"{username}@example.com", "role": role, "enabled": enabled}
    accounts = {username: record}
    if shape == "mapping":
        return {"accounts": accounts}
    path = os.path.join(_CACHE_DIR, f"{shape}-{username}-{role}-{enabled}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"accounts": accounts}, handle)
    return credential_cache.CredentialCache(path)


# ------------------------------------------------------------- strategies

credentials = st.one_of(
    st.just(("none", None)),
    st.just(("empty", "")),
    st.tuples(st.just("local"), st.sampled_from(("valid", "expired", "foreign",
                                                 "unknown_account"))),
    st.tuples(st.just("bearer"), st.sampled_from(BEARERS)),
    st.just(("garbage", "not-a-token at all")),
)
introspection_payloads = st.fixed_dictionaries({}, optional={
    "username": st.sampled_from(("remote-user", "")),
    "sub": st.sampled_from(("remote-sub",)),
    "exp": st.integers(min_value=1, max_value=2**31),
})


@st.composite
def matrix_inputs(draw):
    return {
        "local_login_enabled": draw(st.booleans()),
        "existing_auth": draw(st.booleans()),
        "credential": draw(credentials),
        "shape": draw(st.sampled_from(("mapping", "account"))),
        "username": draw(st.sampled_from(USERNAMES)),
        "token_role": draw(st.sampled_from(ROLES)),
        "cache_role": draw(st.sampled_from(ROLES)),
        "enabled": draw(st.booleans()),
        "bearer_active": draw(st.booleans()),
        "payload": draw(introspection_payloads),
    }


def make_credential(kind, detail, username, token_role):
    """(credential string or None, token exp or None)."""
    if kind in ("none", "empty", "garbage"):
        return detail, None
    if kind == "bearer":
        return detail, None
    issued_at = int(time.time())
    secret = SECRET
    subject = username
    if detail == "expired":
        issued_at -= session_tokens.TOKEN_LIFETIME_SECONDS + 60
    elif detail == "foreign":
        secret = OTHER_SECRET
    elif detail == "unknown_account":
        subject = "nobody-" + username
    token = session_tokens.issue_token(secret, subject, token_role, issued_at)
    return token, issued_at + session_tokens.TOKEN_LIFETIME_SECONDS


# ------------------------------------------------------------- the oracle

def expected_outcome(inputs, credential, token_exp):
    """The documented matrix (``authorize_credential`` docstring), with the
    decision each admitting rule now names. Returns 'raise', None or an
    AuthDecision."""
    local, existing = inputs["local_login_enabled"], inputs["existing_auth"]
    kind, detail = inputs["credential"]
    if not local and not existing:
        return None                                    # open access
    if not credential:
        return "raise"
    local_valid = (kind == "local" and detail == "valid" and inputs["enabled"])
    if local and local_valid:
        return AuthDecision("local_session", inputs["username"],
                            inputs["cache_role"], token_exp)
    if existing:
        if kind == "bearer" and inputs["bearer_active"]:
            payload = inputs["payload"]
            return AuthDecision(
                "existing_token_auth",
                payload.get("username") or payload.get("sub") or "unknown",
                None, payload.get("exp"))
        return "raise"
    return "raise"


@settings(max_examples=100, deadline=None)
@given(inputs=matrix_inputs())
def test_matrix_unchanged_and_decision_names_the_rule(inputs):
    kind, detail = inputs["credential"]
    credential, token_exp = make_credential(kind, detail, inputs["username"],
                                            inputs["token_role"])
    cache = cache_of(inputs["shape"], inputs["username"], inputs["cache_role"],
                     inputs["enabled"])
    active = (credential,) if (kind == "bearer" and inputs["bearer_active"]) else ()
    introspection = FakeIntrospection(active, inputs["payload"])
    expected = expected_outcome(inputs, credential, token_exp)

    with configured(inputs["local_login_enabled"], inputs["existing_auth"],
                    cache, introspection):
        if expected == "raise":
            with pytest.raises(HTTPException) as raised:
                authorize_credential(credential)
            assert raised.value.status_code == 401
            assert raised.value.headers == {"WWW-Authenticate": "Bearer"}
            return
        result = authorize_credential(credential)

    if expected is None:
        assert result is None
        assert introspection.calls == []
        return
    assert isinstance(result, AuthDecision)
    assert result == expected
    if result.source == "local_session":
        # Validated locally: never sent to the remote introspection.
        assert introspection.calls == []


@settings(max_examples=100, deadline=None)
@given(inputs=matrix_inputs())
def test_authorize_request_stores_the_decision(inputs):
    kind, detail = inputs["credential"]
    credential, token_exp = make_credential(kind, detail, inputs["username"],
                                            inputs["token_role"])
    cache = cache_of(inputs["shape"], inputs["username"], inputs["cache_role"],
                     inputs["enabled"])
    active = (credential,) if (kind == "bearer" and inputs["bearer_active"]) else ()
    expected = expected_outcome(inputs, credential, token_exp)
    request = types.SimpleNamespace(
        url=types.SimpleNamespace(path="/protected"),
        state=types.SimpleNamespace())

    with configured(inputs["local_login_enabled"], inputs["existing_auth"],
                    cache, FakeIntrospection(active, inputs["payload"])):
        if expected == "raise":
            with pytest.raises(HTTPException):
                auth_module.authorize_request(request, credential)
            assert not hasattr(request.state, "auth_decision")
            return
        auth_module.authorize_request(request, credential)
    assert request.state.auth_decision == (expected or ANONYMOUS_DECISION)


@pytest.mark.parametrize("path", sorted(auth_module.AUTHORIZATION_EXEMPT_PATHS)
                         + ["/static/js/app.js"])
def test_exempt_paths_store_the_anonymous_decision(path):
    request = types.SimpleNamespace(url=types.SimpleNamespace(path=path),
                                    state=types.SimpleNamespace())
    with configured(True, True, {"accounts": {}}, FakeIntrospection()):
        auth_module.authorize_request(request, None)
    assert request.state.auth_decision == ANONYMOUS_DECISION
