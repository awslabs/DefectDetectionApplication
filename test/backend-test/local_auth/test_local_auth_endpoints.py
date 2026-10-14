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
"""Example-based unit tests for endpoints/local_auth.py (task 9.1).

Feature: portal-user-manager (Requirements 8.2, 8.3, 8.10, 9.5, 11.3).

Exercised through FastAPI's TestClient against a minimal app carrying only
the local-auth router. Real credential-cache files (with real PBKDF2
verifiers at a reduced iteration count), the real lockout tracker, and the
real token issuance/validation code run underneath — only the file paths
and the Local_Login_Configuration state are injected.
"""
import base64
import hashlib
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from local_auth import config as local_login_config
from local_auth import credential_cache, lockout, session_tokens
from local_auth.credential_cache import CredentialCache
from local_auth.lockout import LockoutTracker

import endpoints.local_auth as local_auth_endpoints

# Reduced PBKDF2 cost so each login verification is fast; the parser only
# requires a positive iteration count.
TEST_ITERATIONS = 1000
GOOD_PASSWORD = "correct horse battery staple"
NOW = 1_700_000_000


def make_verifier(password, iterations=TEST_ITERATIONS):
    salt = b"0123456789abcdef"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {
        "algorithm": "pbkdf2-sha256",
        "iterations": iterations,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
    }


def write_cache(path, accounts):
    path.write_text(
        json.dumps({"version": 1, "syncId": "test-sync", "accounts": accounts})
    )


class _ClientAddressInjector:
    """ASGI wrapper setting scope['client'] when the test client leaves it
    unset — AccessLogRoute's request logging dereferences request.client,
    which this starlette version's TestClient does not populate."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope.get("client"):
            scope["client"] = ("testclient", 50000)
        await self.app(scope, receive, send)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient over a minimal app with the local-auth router, with the
    cache, lockout tracker, secret path, and config state all injected."""
    cache_path = tmp_path / "local_credential_cache.json"
    write_cache(
        cache_path,
        {
            "operator1": {
                "email": "op1@example.com",
                "role": "Operator",
                "enabled": True,
                "verifier": make_verifier(GOOD_PASSWORD),
            },
            "disabled_user": {
                "email": "d@example.com",
                "role": "Viewer",
                "enabled": False,
                "verifier": make_verifier(GOOD_PASSWORD),
            },
        },
    )
    monkeypatch.setattr(
        credential_cache, "_default_cache", CredentialCache(str(cache_path))
    )
    monkeypatch.setattr(lockout, "_default_tracker", LockoutTracker())

    secret_path = tmp_path / "local_session_secret"
    real_get_or_create_secret = session_tokens.get_or_create_secret
    monkeypatch.setattr(
        session_tokens,
        "get_or_create_secret",
        lambda path=str(secret_path): real_get_or_create_secret(path),
    )

    # Local login enabled by default; tests flip it via the same attribute.
    monkeypatch.setattr(local_login_config, "is_local_login_enabled", lambda: True)

    app = FastAPI()
    app.include_router(local_auth_endpoints.router)
    return TestClient(_ClientAddressInjector(app))


def set_local_login_enabled(monkeypatch, enabled):
    monkeypatch.setattr(
        local_login_config, "is_local_login_enabled", lambda: enabled
    )


def login(client, username, password):
    return client.post(
        "/local-auth/login", json={"username": username, "password": password}
    )


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def log_records():
    """Capture WARNING+ records from the root logger. The repo conftest
    overrides pytest's caplog fixture, so we attach our own handler."""
    handler = _ListHandler()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    yield handler.records
    root.removeHandler(handler)
    root.setLevel(old_level)


def messages(records):
    return "\n".join(record.getMessage() for record in records)


class TestStatusEndpoint:
    def test_status_reflects_enabled_config(self, client):
        response = client.get("/local-auth/status")
        assert response.status_code == 200
        assert response.json() == {"localLoginEnabled": True}

    def test_status_reflects_disabled_config(self, client, monkeypatch):
        set_local_login_enabled(monkeypatch, False)
        response = client.get("/local-auth/status")
        assert response.status_code == 200
        assert response.json() == {"localLoginEnabled": False}


class TestLoginDisabled:
    def test_disabled_login_returns_403_and_no_token(self, client, monkeypatch):
        """Requirement 9.5: disabled ⇒ error naming local login disabled,
        never a token — even for otherwise valid credentials."""
        set_local_login_enabled(monkeypatch, False)
        response = login(client, "operator1", GOOD_PASSWORD)
        assert response.status_code == 403
        body = response.json()
        assert body["detail"] == "local login is disabled"
        assert "token" not in response.text


class TestLoginSuccess:
    def test_good_credentials_return_token_payload(self, client):
        """Requirement 8.2: success issues a 12-hour Local_Session_Token."""
        response = login(client, "operator1", GOOD_PASSWORD)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"token", "expiresAt", "role", "username"}
        assert body["username"] == "operator1"
        assert body["role"] == "Operator"

        # The token is a real, valid Local_Session_Token whose exp matches
        # the advertised expiresAt (iat + 12 h).
        secret = session_tokens.get_or_create_secret()
        payload_b64 = body["token"].split(".")[0]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        assert payload["sub"] == "operator1"
        assert payload["exp"] == body["expiresAt"]
        assert payload["exp"] - payload["iat"] == 12 * 60 * 60

        account = session_tokens.validate_token(
            secret,
            body["token"],
            payload["iat"] + 1,
            credential_cache.get_default_cache(),
        )
        assert not isinstance(account, session_tokens.AuthError)
        assert account.username == "operator1"


class TestUniform401:
    def test_wrong_password_and_unknown_username_are_identical(self, client):
        """Requirement 8.3: the 401 is identical in status and body whether
        or not the submitted username exists in the cache."""
        wrong_password = login(client, "operator1", "wrong-password")
        unknown_user = login(client, "no_such_user", "anything")
        assert wrong_password.status_code == 401
        assert unknown_user.status_code == 401
        assert wrong_password.json() == unknown_user.json()

    def test_disabled_account_gets_the_same_401(self, client):
        """Requirement 8.6/8.3: disabled account + correct password is
        indistinguishable from any other failure."""
        disabled = login(client, "disabled_user", GOOD_PASSWORD)
        unknown = login(client, "no_such_user", "anything")
        assert disabled.status_code == 401
        assert disabled.json() == unknown.json()


class TestLockout:
    def test_lockout_after_five_failures_rejects_correct_credentials(self, client):
        """Requirement 8.10: 5 consecutive failures lock the account; the
        next attempt is rejected with the same uniform 401 even with the
        correct password."""
        for _ in range(5):
            assert login(client, "operator1", "wrong-password").status_code == 401

        locked = login(client, "operator1", GOOD_PASSWORD)
        unknown = login(client, "no_such_user", "anything")
        assert locked.status_code == 401
        assert locked.json() == unknown.json()

    def test_other_accounts_unaffected_by_lockout(self, client, tmp_path):
        for _ in range(5):
            login(client, "no_such_user", "wrong-password")
        # operator1 has no failures recorded; login still succeeds.
        assert login(client, "operator1", GOOD_PASSWORD).status_code == 200


class TestLogging:
    def test_failed_login_logs_username_never_password(self, client, log_records):
        """Failed logins are logged with the username; the submitted
        password never appears in any log record."""
        submitted_password = "s3cret-password-value"
        login(client, "operator1", submitted_password)
        combined = messages(log_records)
        assert "operator1" in combined
        assert submitted_password not in combined

    def test_lockout_rejection_logs_username_never_password(self, client, log_records):
        submitted_password = "another-s3cret-value"
        for _ in range(6):
            login(client, "operator1", submitted_password)
        combined = messages(log_records)
        assert "locked" in combined
        assert "operator1" in combined
        assert submitted_password not in combined

    def test_empty_cache_logs_no_synchronized_accounts_diagnostic(
        self, client, tmp_path, monkeypatch, log_records
    ):
        """Requirement 11.3: empty cache ⇒ uniform 401 plus the
        'no synchronized accounts available' diagnostic."""
        empty_cache = tmp_path / "empty_cache.json"
        write_cache(empty_cache, {})
        monkeypatch.setattr(
            credential_cache, "_default_cache", CredentialCache(str(empty_cache))
        )
        response = login(client, "anyone", "anything")
        assert response.status_code == 401
        assert "no synchronized accounts available" in messages(log_records)
