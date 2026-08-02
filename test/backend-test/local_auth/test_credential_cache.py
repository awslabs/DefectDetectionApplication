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
"""Unit tests for ``local_auth.credential_cache`` (task 8.1).

Feature: portal-user-manager (Requirements 8.3, 8.4, 8.6, 11.3).
"""
import base64
import hashlib
import json

import pytest

from local_auth.credential_cache import Account, CredentialCache

# Low iteration count keeps the unit tests fast; the count is read from the
# stored verifier, so verification honors whatever the record carries.
TEST_ITERATIONS = 1000


def make_verifier(password: str, salt: bytes = b"0123456789abcdef",
                  iterations: int = TEST_ITERATIONS) -> dict:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {
        "algorithm": "pbkdf2-sha256",
        "iterations": iterations,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
    }


def write_cache(tmp_path, accounts) -> CredentialCache:
    path = tmp_path / "local_credential_cache.json"
    path.write_text(json.dumps({
        "version": 1,
        "syncId": "test-sync",
        "appliedAt": 1700000000000,
        "accounts": accounts,
    }))
    return CredentialCache(path=str(path))


@pytest.fixture
def cache(tmp_path):
    return write_cache(tmp_path, {
        "operator1": {
            "email": "op1@example.com",
            "role": "Operator",
            "enabled": True,
            "verifier": make_verifier("correct horse"),
        },
        "disabled1": {
            "email": "d1@example.com",
            "role": "Viewer",
            "enabled": False,
            "verifier": make_verifier("disabled pass"),
        },
        "noverifier": {
            "email": "nv@example.com",
            "role": "Viewer",
            "enabled": True,
        },
    })


class TestVerifyCredentials:
    def test_correct_credentials_return_enabled_account(self, cache):
        account = cache.verify_credentials("operator1", "correct horse")
        assert account == Account(
            username="operator1", email="op1@example.com",
            role="Operator", enabled=True,
        )

    def test_wrong_password_returns_none(self, cache):
        assert cache.verify_credentials("operator1", "wrong password") is None

    def test_unknown_username_returns_none(self, cache):
        assert cache.verify_credentials("nobody", "correct horse") is None

    def test_unknown_username_indistinguishable_from_wrong_password(self, cache):
        # Requirement 8.3: identical outcome for both failure kinds.
        known_wrong = cache.verify_credentials("operator1", "wrong password")
        unknown = cache.verify_credentials("nobody", "wrong password")
        assert known_wrong is None and unknown is None

    def test_disabled_account_rejected_even_with_correct_password(self, cache):
        # Requirement 8.6.
        assert cache.verify_credentials("disabled1", "disabled pass") is None

    def test_account_without_verifier_rejected(self, cache):
        assert cache.verify_credentials("noverifier", "anything") is None

    def test_uppercase_submission_matches_normalized_key(self, cache):
        account = cache.verify_credentials("Operator1", "correct horse")
        assert account is not None and account.username == "operator1"


class TestEmptyAndCorruptCache:
    # The shared test/backend-test/conftest.py overrides the caplog fixture
    # to stash the real one on request.cls, so class-based tests reach it
    # through self.caplog (the injected argument arrives as None).
    def test_missing_file_fails_and_logs_diagnostic(self, tmp_path, caplog):
        cache = CredentialCache(path=str(tmp_path / "does_not_exist.json"))
        with self.caplog.at_level("WARNING"):
            assert cache.verify_credentials("operator1", "correct horse") is None
        assert "no synchronized accounts available" in self.caplog.text

    def test_corrupt_file_treated_as_empty(self, tmp_path, caplog):
        path = tmp_path / "local_credential_cache.json"
        path.write_text("{not json!!")
        cache = CredentialCache(path=str(path))
        with self.caplog.at_level("WARNING"):
            assert cache.verify_credentials("operator1", "anything") is None
        assert "no synchronized accounts available" in self.caplog.text

    def test_wrong_shape_treated_as_empty(self, tmp_path):
        path = tmp_path / "local_credential_cache.json"
        path.write_text(json.dumps({"accounts": ["not", "a", "mapping"]}))
        cache = CredentialCache(path=str(path))
        assert cache.load_accounts() == {}

    def test_empty_accounts_fail_uniformly(self, tmp_path):
        cache = write_cache(tmp_path, {})
        assert cache.verify_credentials("anyone", "anything") is None


class TestGetAccount:
    def test_returns_enabled_flag_as_stored(self, cache):
        account = cache.get_account("disabled1")
        assert account is not None and account.enabled is False

    def test_absent_account_returns_none(self, cache):
        assert cache.get_account("nobody") is None

    def test_reflects_cache_replacement(self, tmp_path):
        # The file is re-read per call, so an applied sync takes effect
        # immediately (design: atomic full-set replacement by the agent).
        cache = write_cache(tmp_path, {
            "operator1": {"email": "e", "role": "Operator", "enabled": True},
        })
        assert cache.get_account("operator1").enabled is True
        write_cache(tmp_path, {
            "operator1": {"email": "e", "role": "Operator", "enabled": False},
        })
        assert cache.get_account("operator1").enabled is False


class TestMalformedVerifiers:
    @pytest.mark.parametrize("verifier", [
        {"algorithm": "bcrypt", "iterations": 10, "salt": "AAAA", "hash": "AAAA"},
        {"algorithm": "pbkdf2-sha256", "iterations": 0, "salt": "AAAA", "hash": "AAAA"},
        {"algorithm": "pbkdf2-sha256", "iterations": 1000, "salt": "%%%", "hash": "AAAA"},
        {"algorithm": "pbkdf2-sha256", "iterations": 1000, "salt": "", "hash": ""},
        "not a mapping",
        None,
    ])
    def test_malformed_verifier_fails_like_wrong_password(self, tmp_path, verifier):
        cache = write_cache(tmp_path, {
            "user1": {"email": "e", "role": "Viewer", "enabled": True,
                      "verifier": verifier},
        })
        assert cache.verify_credentials("user1", "anything") is None
