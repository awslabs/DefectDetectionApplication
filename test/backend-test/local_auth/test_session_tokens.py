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
"""Unit tests for local_auth.session_tokens.

Feature: portal-user-manager (Requirements 8.2, 8.5, 8.6, 8.7).
"""
import base64
import json
import os
import stat

from local_auth.session_tokens import (
    SECRET_NUM_BYTES,
    TOKEN_LIFETIME_SECONDS,
    AuthError,
    get_or_create_secret,
    issue_token,
    validate_token,
)

SECRET = b"\x01" * 32
OTHER_SECRET = b"\x02" * 32
NOW = 1_700_000_000


def make_cache(enabled=True, username="operator1"):
    return {
        "accounts": {
            username: {"email": "op1@example.com", "role": "Operator", "enabled": enabled}
        }
    }


def decode_payload(token):
    payload_b64 = token.split(".")[0]
    padding = "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64 + padding))


class TestIssueToken:
    def test_payload_fields_and_12_hour_expiry(self):
        token = issue_token(SECRET, "operator1", "Operator", NOW)
        payload = decode_payload(token)
        assert payload["sub"] == "operator1"
        assert payload["role"] == "Operator"
        assert payload["iat"] == NOW
        assert payload["exp"] == NOW + 43_200
        assert TOKEN_LIFETIME_SECONDS == 43_200
        assert payload["jti"]

    def test_token_structure_is_two_base64url_segments(self):
        token = issue_token(SECRET, "operator1", "Operator", NOW)
        segments = token.split(".")
        assert len(segments) == 2
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert all(set(segment) <= allowed for segment in segments)

    def test_jti_unique_per_token(self):
        first = issue_token(SECRET, "operator1", "Operator", NOW)
        second = issue_token(SECRET, "operator1", "Operator", NOW)
        assert decode_payload(first)["jti"] != decode_payload(second)["jti"]


class TestValidateToken:
    def test_round_trip_returns_account(self):
        cache = make_cache()
        token = issue_token(SECRET, "operator1", "Operator", NOW)
        account = validate_token(SECRET, token, NOW + 100, cache)
        assert account == cache["accounts"]["operator1"]

    def test_expired_at_exact_boundary_rejected(self):
        token = issue_token(SECRET, "operator1", "Operator", NOW)
        result = validate_token(SECRET, token, NOW + 43_200, make_cache())
        assert isinstance(result, AuthError)

    def test_valid_one_second_before_expiry(self):
        token = issue_token(SECRET, "operator1", "Operator", NOW)
        result = validate_token(SECRET, token, NOW + 43_199, make_cache())
        assert not isinstance(result, AuthError)

    def test_wrong_secret_rejected(self):
        token = issue_token(OTHER_SECRET, "operator1", "Operator", NOW)
        result = validate_token(SECRET, token, NOW, make_cache())
        assert isinstance(result, AuthError)

    def test_tampered_payload_rejected(self):
        token = issue_token(SECRET, "operator1", "Operator", NOW)
        payload_b64, signature_b64 = token.split(".")
        forged_payload = dict(decode_payload(token), role="PortalAdmin")
        forged_b64 = (
            base64.urlsafe_b64encode(
                json.dumps(forged_payload, separators=(",", ":"), sort_keys=True).encode()
            )
            .rstrip(b"=")
            .decode()
        )
        result = validate_token(SECRET, forged_b64 + "." + signature_b64, NOW, make_cache())
        assert isinstance(result, AuthError)

    def test_malformed_structures_rejected(self):
        cache = make_cache()
        for bad in ["", "onesegment", "a.b.c", ".", "x.", ".y", "not base64!.sig"]:
            assert isinstance(validate_token(SECRET, bad, NOW, cache), AuthError)

    def test_account_absent_from_cache_rejected(self):
        token = issue_token(SECRET, "ghost", "Operator", NOW)
        result = validate_token(SECRET, token, NOW, make_cache())
        assert isinstance(result, AuthError)

    def test_disabled_account_rejected_revocation_on_disable(self):
        # D6: a token issued while enabled is rejected once the account is
        # marked disabled in the credential cache (Requirement 8.6).
        token = issue_token(SECRET, "operator1", "Operator", NOW)
        assert not isinstance(validate_token(SECRET, token, NOW, make_cache(enabled=True)), AuthError)
        assert isinstance(validate_token(SECRET, token, NOW, make_cache(enabled=False)), AuthError)

    def test_empty_cache_rejected(self):
        token = issue_token(SECRET, "operator1", "Operator", NOW)
        assert isinstance(validate_token(SECRET, token, NOW, {}), AuthError)

    def test_get_account_style_cache_supported(self):
        class Store:
            def __init__(self, accounts):
                self._accounts = accounts

            def get_account(self, username):
                return self._accounts.get(username)

        token = issue_token(SECRET, "operator1", "Operator", NOW)
        store = Store({"operator1": {"role": "Operator", "enabled": True}})
        assert not isinstance(validate_token(SECRET, token, NOW, store), AuthError)
        assert isinstance(validate_token(SECRET, token, NOW, Store({})), AuthError)


class TestGetOrCreateSecret:
    def test_creates_32_bytes_with_0600_mode(self, tmp_path):
        path = str(tmp_path / "local_session_secret")
        secret = get_or_create_secret(path)
        assert len(secret) == SECRET_NUM_BYTES
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_returns_same_secret_on_subsequent_calls(self, tmp_path):
        path = str(tmp_path / "local_session_secret")
        assert get_or_create_secret(path) == get_or_create_secret(path)

    def test_regenerates_wrong_size_file(self, tmp_path):
        path = str(tmp_path / "local_session_secret")
        with open(path, "wb") as handle:
            handle.write(b"short")
        secret = get_or_create_secret(path)
        assert len(secret) == SECRET_NUM_BYTES
        with open(path, "rb") as handle:
            assert handle.read() == secret
