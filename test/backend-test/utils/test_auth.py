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
import base64

import httpx
from fastapi import HTTPException
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_500_INTERNAL_SERVER_ERROR

from local_server_base_test_case import LocalServerBaseTestCase
from unittest.mock import patch, MagicMock


AUTH_SETTINGS = {
    "clientId": "test-client-id",
    "clientSecret": "test-client-secret",
    "introspectEndpoint": "https://idp.example.com/oauth2/introspect",
}


def _introspection_response(status_code=200, active=True):
    """Build a mock httpx response for the token introspection endpoint."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"active": active}
    return resp


class TestValidateToken(LocalServerBaseTestCase):
    """Tests for auth.validate_token — the bearer-token gate for the API."""

    def test_no_token_with_auth_enabled_raises_401(self):
        from utils import auth

        with patch("utils.utils.is_authorization_enabled_on_station", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                auth.validate_token(token="")  # nosec B106 — test-only fixture, not a real credential.

        self.assertEqual(ctx.exception.status_code, HTTP_401_UNAUTHORIZED)
        self.assertEqual(ctx.exception.detail, "Not authenticated")
        self.assertEqual(ctx.exception.headers.get("WWW-Authenticate"), "Bearer")

    def test_valid_active_token_passes(self):
        from utils import auth

        with patch("utils.utils.is_authorization_enabled_on_station", return_value=True), \
                patch("utils.utils.get_authorization_settings_from_file", return_value=dict(AUTH_SETTINGS)), \
                patch("utils.auth.validate_remotely", return_value=_introspection_response(200, True)):
            # Should not raise for a valid, active token.
            result = auth.validate_token(token="good-token")  # nosec B106 — test-only fixture, not a real credential.

        self.assertIsNone(result)

    def test_inactive_token_raises_401_access_denied(self):
        from utils import auth

        with patch("utils.utils.is_authorization_enabled_on_station", return_value=True), \
                patch("utils.utils.get_authorization_settings_from_file", return_value=dict(AUTH_SETTINGS)), \
                patch("utils.auth.validate_remotely", return_value=_introspection_response(200, False)):
            with self.assertRaises(HTTPException) as ctx:
                auth.validate_token(token="inactive-token")  # nosec B106 — test-only fixture, not a real credential.

        self.assertEqual(ctx.exception.status_code, HTTP_401_UNAUTHORIZED)
        self.assertEqual(ctx.exception.detail, "Access Denied")

    def test_non_200_introspection_raises_401_access_denied(self):
        from utils import auth

        with patch("utils.utils.is_authorization_enabled_on_station", return_value=True), \
                patch("utils.utils.get_authorization_settings_from_file", return_value=dict(AUTH_SETTINGS)), \
                patch("utils.auth.validate_remotely", return_value=_introspection_response(403, True)):
            with self.assertRaises(HTTPException) as ctx:
                auth.validate_token(token="some-token")  # nosec B106 — test-only fixture, not a real credential.

        self.assertEqual(ctx.exception.status_code, HTTP_401_UNAUTHORIZED)
        self.assertEqual(ctx.exception.detail, "Access Denied")

    def test_empty_introspection_response_raises_401(self):
        from utils import auth

        with patch("utils.utils.is_authorization_enabled_on_station", return_value=True), \
                patch("utils.utils.get_authorization_settings_from_file", return_value=dict(AUTH_SETTINGS)), \
                patch("utils.auth.validate_remotely", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                auth.validate_token(token="some-token")  # nosec B106 — test-only fixture, not a real credential.

        self.assertEqual(ctx.exception.status_code, HTTP_401_UNAUTHORIZED)
        self.assertEqual(ctx.exception.detail, "Access Denied")

    def test_validate_token_forwards_settings_to_validate_remotely(self):
        from utils import auth

        with patch("utils.utils.is_authorization_enabled_on_station", return_value=True), \
                patch("utils.utils.get_authorization_settings_from_file", return_value=dict(AUTH_SETTINGS)), \
                patch("utils.auth.validate_remotely", return_value=_introspection_response(200, True)) as mock_remote:
            auth.validate_token(token="good-token")  # nosec B106 — test-only fixture, not a real credential.

        mock_remote.assert_called_once_with(
            "good-token",
            AUTH_SETTINGS["clientId"],
            AUTH_SETTINGS["clientSecret"],
            AUTH_SETTINGS["introspectEndpoint"],
        )


class TestValidateRemotely(LocalServerBaseTestCase):
    """Tests for auth.validate_remotely — the IdP introspection call."""

    def test_returns_response_on_success(self):
        from utils import auth

        expected = _introspection_response(200, True)
        with patch("utils.auth.httpx.post", return_value=expected) as mock_post:
            result = auth.validate_remotely(
                "tok", AUTH_SETTINGS["clientId"], AUTH_SETTINGS["clientSecret"],
                AUTH_SETTINGS["introspectEndpoint"],
            )

        self.assertIs(result, expected)
        mock_post.assert_called_once()

    def test_builds_basic_auth_header_and_posts_to_endpoint(self):
        from utils import auth

        with patch("utils.auth.httpx.post", return_value=_introspection_response()) as mock_post:
            auth.validate_remotely(
                "tok", AUTH_SETTINGS["clientId"], AUTH_SETTINGS["clientSecret"],
                AUTH_SETTINGS["introspectEndpoint"],
            )

        args, kwargs = mock_post.call_args
        # Endpoint is the first positional arg.
        self.assertEqual(args[0], AUTH_SETTINGS["introspectEndpoint"])

        expected_basic = base64.b64encode(
            f"{AUTH_SETTINGS['clientId']}:{AUTH_SETTINGS['clientSecret']}".encode()
        ).decode()
        self.assertEqual(kwargs["headers"]["Authorization"], f"Basic {expected_basic}")
        self.assertEqual(kwargs["data"]["token"], "tok")
        self.assertEqual(kwargs["data"]["client_id"], AUTH_SETTINGS["clientId"])

    def test_raises_500_when_post_fails(self):
        from utils import auth

        with patch("utils.auth.httpx.post", side_effect=httpx.ConnectError("boom")):
            with self.assertRaises(HTTPException) as ctx:
                auth.validate_remotely(
                    "tok", AUTH_SETTINGS["clientId"], AUTH_SETTINGS["clientSecret"],
                    AUTH_SETTINGS["introspectEndpoint"],
                )

        self.assertEqual(ctx.exception.status_code, HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(ctx.exception.headers.get("WWW-Authenticate"), "Bearer")
