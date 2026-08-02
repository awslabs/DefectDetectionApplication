"""
Unit tests for the user_admin.py Lambda scaffold
(spec: portal-user-manager, task 1.2).

Covers the PortalAdmin gate on every routed handler, the router's
CORS-preflight and not-found paths, and the two pure credential
functions: generate_temp_password (policy conformance) and
make_verifier (PBKDF2-SHA256 verifier shape and correctness).

_Requirements: 1.5, 4.1, 7.3_
"""
import base64
import hashlib
import json
import string
import sys

import pytest


@pytest.fixture(scope="module")
def user_admin():
    """Import the real module (conftest puts functions/ + shared layer on
    sys.path). The scaffold's gate and pure functions make no AWS calls."""
    sys.modules.pop("user_admin", None)
    import user_admin as module
    return module


def make_event(method, path, role="PortalAdmin"):
    return {
        "httpMethod": method,
        "path": path,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": "caller-1",
                    "email": "caller@example.com",
                    "cognito:username": "caller-1",
                    "custom:role": role,
                }
            }
        },
    }


ROUTES = [
    ("GET", "/api/v1/admin/users"),
    ("POST", "/api/v1/admin/users/someuser/password"),
    ("POST", "/api/v1/admin/users/someuser/forgot-password"),
    ("PUT", "/api/v1/admin/users/someuser/role"),
    ("GET", "/api/v1/admin/edge-sync/devices"),
    ("POST", "/api/v1/admin/edge-sync/devices/device-1"),
]


class TestPortalAdminGate:
    """Req 1.5: every handler rejects non-PortalAdmin callers with 403."""

    @pytest.mark.parametrize("method,path", ROUTES)
    @pytest.mark.parametrize(
        "role", ["Viewer", "Operator", "DataScientist", "UseCaseAdmin"])
    def test_non_portal_admin_gets_403_on_every_route(
            self, user_admin, method, path, role):
        response = user_admin.handler(make_event(method, path, role), None)
        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"] == "Access denied"

    def test_missing_authorizer_claims_treated_as_viewer_403(self, user_admin):
        event = {"httpMethod": "PUT",
                 "path": "/api/v1/admin/users/x/role",
                 "requestContext": {}}
        response = user_admin.handler(event, None)
        assert response["statusCode"] == 403

    @pytest.mark.parametrize("method,path", [
        r for r in ROUTES if r != ("GET", "/api/v1/admin/users")])
    def test_portal_admin_passes_the_gate(self, user_admin, method, path):
        """PortalAdmin callers reach the handler bodies (501 placeholders
        for endpoints implemented by later tasks) rather than 403."""
        response = user_admin.handler(
            make_event(method, path, "PortalAdmin"), None)
        assert response["statusCode"] != 403


class TestRouter:
    def test_options_preflight_returns_200(self, user_admin):
        response = user_admin.handler(
            {"httpMethod": "OPTIONS", "path": "/api/v1/admin/users"}, None)
        assert response["statusCode"] == 200
        assert "Access-Control-Allow-Methods" in response["headers"]

    def test_unknown_route_returns_404(self, user_admin):
        response = user_admin.handler(
            make_event("GET", "/api/v1/admin/unknown"), None)
        assert response["statusCode"] == 404


class TestGenerateTempPassword:
    """Req 4.1: temporary passwords conform to the pool Password_Policy."""

    def test_default_length_is_16(self, user_admin):
        assert len(user_admin.generate_temp_password()) == 16

    @pytest.mark.parametrize("length", [12, 16, 24])
    def test_contains_all_required_character_classes(self, user_admin, length):
        password = user_admin.generate_temp_password(length)
        assert len(password) == length
        assert any(c in string.ascii_lowercase for c in password)
        assert any(c in string.ascii_uppercase for c in password)
        assert any(c in string.digits for c in password)
        assert any(c in user_admin.PASSWORD_SYMBOLS for c in password)

    def test_only_uses_allowed_alphabet(self, user_admin):
        alphabet = set(string.ascii_letters + string.digits
                       + user_admin.PASSWORD_SYMBOLS)
        password = user_admin.generate_temp_password()
        assert set(password) <= alphabet

    @pytest.mark.parametrize("length", [0, 8, 11])
    def test_rejects_lengths_below_policy_minimum(self, user_admin, length):
        with pytest.raises(ValueError):
            user_admin.generate_temp_password(length)

    def test_successive_calls_differ(self, user_admin):
        passwords = {user_admin.generate_temp_password() for _ in range(5)}
        assert len(passwords) == 5


class TestMakeVerifier:
    """Req 7.3: credential material is a salted one-way verifier only."""

    def test_verifier_shape_and_defaults(self, user_admin):
        verifier = user_admin.make_verifier("Sup3r-Secret-Pw!")
        assert set(verifier) == {"algorithm", "iterations", "salt", "hash"}
        assert verifier["algorithm"] == "pbkdf2-sha256"
        assert verifier["iterations"] == 210000
        assert len(base64.b64decode(verifier["salt"])) == 16
        assert len(base64.b64decode(verifier["hash"])) == 32

    def test_iteration_count_is_parameterizable(self, user_admin):
        verifier = user_admin.make_verifier("Sup3r-Secret-Pw!", iterations=10)
        assert verifier["iterations"] == 10

    def test_hash_matches_pbkdf2_recomputation(self, user_admin):
        password = "Sup3r-Secret-Pw!"
        verifier = user_admin.make_verifier(password, iterations=10)
        expected = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            base64.b64decode(verifier["salt"]),
            verifier["iterations"],
            dklen=32,
        )
        assert base64.b64decode(verifier["hash"]) == expected

    def test_same_password_gets_fresh_salt_and_hash(self, user_admin):
        first = user_admin.make_verifier("Sup3r-Secret-Pw!", iterations=10)
        second = user_admin.make_verifier("Sup3r-Secret-Pw!", iterations=10)
        assert first["salt"] != second["salt"]
        assert first["hash"] != second["hash"]

    def test_plaintext_absent_from_serialized_verifier(self, user_admin):
        password = "Sup3r-Secret-Pw!"
        verifier = user_admin.make_verifier(password, iterations=10)
        assert password not in json.dumps(verifier)
