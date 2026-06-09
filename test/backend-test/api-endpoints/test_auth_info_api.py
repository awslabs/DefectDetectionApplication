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
import json

from local_server_base_test_case import LocalServerBaseTestCase
from unittest.mock import patch

# Full auth settings as stored on the station. Critically includes a secret
# that MUST NOT be exposed by the /authorization-configurations response.
FULL_AUTH_SETTINGS = {
    "clientId": "test-client-id",
    "clientSecret": "super-secret-value",
    "authorizationEndpoint": "https://idp.example.com/oauth2/authorize",
    "logoutEndpoint": "https://idp.example.com/oauth2/logout",
    "introspectEndpoint": "https://idp.example.com/oauth2/introspect",
}


def _serialize(model):
    """Serialize a pydantic model to a JSON string across pydantic v1/v2."""
    if hasattr(model, "model_dump_json"):
        return model.model_dump_json()
    return model.json()


class TestAuthInfoApi(LocalServerBaseTestCase):
    """Tests for the /authorization-configurations handler.

    These call the endpoint function directly rather than through TestClient:
    the route uses AccessLogRoute, whose request logging dereferences
    request.client.host, which TestClient leaves unset under the fastapi/
    starlette versions in the runtime image. Calling the handler keeps the test
    focused on the response contract (and the no-secret-leak guarantee) and
    independent of the HTTP test-client plumbing.
    """

    @patch("endpoints.auth_info.utils.is_authorization_enabled_on_station", return_value=False)
    def test_auth_disabled_returns_enabled_false_and_no_settings(self, mock_enabled):
        from endpoints import auth_info

        result = auth_info.getAuthSettings()
        self.assertFalse(result.auth_enabled)
        self.assertIsNone(result.auth_settings)

    @patch("endpoints.auth_info.utils.get_authorization_settings_from_file", return_value=dict(FULL_AUTH_SETTINGS))
    @patch("endpoints.auth_info.utils.is_authorization_enabled_on_station", return_value=True)
    def test_auth_enabled_returns_public_settings(self, mock_enabled, mock_settings):
        from endpoints import auth_info

        result = auth_info.getAuthSettings()
        self.assertTrue(result.auth_enabled)
        self.assertEqual(result.auth_settings.clientId, FULL_AUTH_SETTINGS["clientId"])
        self.assertEqual(result.auth_settings.authorizationEndpoint, FULL_AUTH_SETTINGS["authorizationEndpoint"])
        self.assertEqual(result.auth_settings.logoutEndpoint, FULL_AUTH_SETTINGS["logoutEndpoint"])

    @patch("endpoints.auth_info.utils.get_authorization_settings_from_file", return_value=dict(FULL_AUTH_SETTINGS))
    @patch("endpoints.auth_info.utils.is_authorization_enabled_on_station", return_value=True)
    def test_secret_is_never_exposed(self, mock_enabled, mock_settings):
        from endpoints import auth_info

        result = auth_info.getAuthSettings()
        raw = _serialize(result)

        # Neither the secret value nor the secret/introspect field names should
        # appear anywhere in the serialized response.
        self.assertNotIn(FULL_AUTH_SETTINGS["clientSecret"], raw)
        self.assertNotIn("clientSecret", raw)
        self.assertNotIn(FULL_AUTH_SETTINGS["introspectEndpoint"], raw)
        self.assertNotIn("introspectEndpoint", raw)

        # Also confirm the response model has no attribute carrying them.
        self.assertFalse(hasattr(result.auth_settings, "clientSecret"))
        self.assertFalse(hasattr(result.auth_settings, "introspectEndpoint"))

    @patch("endpoints.auth_info.utils.get_authorization_settings_from_file",
           return_value={
               "clientId": "test-client-id",
               "clientSecret": "super-secret-value",
               "authorizationEndpoint": "https://idp.example.com/oauth2/authorize",
               "introspectEndpoint": "https://idp.example.com/oauth2/introspect",
           })
    @patch("endpoints.auth_info.utils.is_authorization_enabled_on_station", return_value=True)
    def test_optional_logout_endpoint_defaults_to_null(self, mock_enabled, mock_settings):
        from endpoints import auth_info

        result = auth_info.getAuthSettings()
        self.assertIsNone(result.auth_settings.logoutEndpoint)
