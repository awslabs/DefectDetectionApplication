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
# that MUST NOT be exposed by the public /authorization-configurations route.
FULL_AUTH_SETTINGS = {
    "clientId": "test-client-id",
    "clientSecret": "super-secret-value",
    "authorizationEndpoint": "https://idp.example.com/oauth2/authorize",
    "logoutEndpoint": "https://idp.example.com/oauth2/logout",
    "introspectEndpoint": "https://idp.example.com/oauth2/introspect",
}

URL = "/authorization-configurations"


class TestAuthInfoApi(LocalServerBaseTestCase):

    @patch("endpoints.auth_info.utils.is_authorization_enabled_on_station", return_value=False)
    def test_auth_disabled_returns_enabled_false_and_no_settings(self, mock_enabled):
        response = self.client.get(URL)
        assert response.status_code == 200, f"status_code: {response.status_code}"

        data = response.json()
        self.assertFalse(data["auth_enabled"])
        self.assertIsNone(data["auth_settings"])

    @patch("endpoints.auth_info.utils.get_authorization_settings_from_file", return_value=dict(FULL_AUTH_SETTINGS))
    @patch("endpoints.auth_info.utils.is_authorization_enabled_on_station", return_value=True)
    def test_auth_enabled_returns_public_settings(self, mock_enabled, mock_settings):
        response = self.client.get(URL)
        assert response.status_code == 200, f"status_code: {response.status_code}"

        data = response.json()
        self.assertTrue(data["auth_enabled"])
        settings = data["auth_settings"]
        self.assertEqual(settings["clientId"], FULL_AUTH_SETTINGS["clientId"])
        self.assertEqual(settings["authorizationEndpoint"], FULL_AUTH_SETTINGS["authorizationEndpoint"])
        self.assertEqual(settings["logoutEndpoint"], FULL_AUTH_SETTINGS["logoutEndpoint"])

    @patch("endpoints.auth_info.utils.get_authorization_settings_from_file", return_value=dict(FULL_AUTH_SETTINGS))
    @patch("endpoints.auth_info.utils.is_authorization_enabled_on_station", return_value=True)
    def test_secret_is_never_exposed(self, mock_enabled, mock_settings):
        response = self.client.get(URL)
        assert response.status_code == 200, f"status_code: {response.status_code}"

        # The clientSecret and introspectEndpoint must not appear anywhere in
        # the serialized response — neither as a field nor leaked into a value.
        raw_body = response.text
        self.assertNotIn(FULL_AUTH_SETTINGS["clientSecret"], raw_body)
        self.assertNotIn("clientSecret", raw_body)
        self.assertNotIn(FULL_AUTH_SETTINGS["introspectEndpoint"], raw_body)

        settings = response.json()["auth_settings"]
        self.assertNotIn("clientSecret", settings)
        self.assertNotIn("introspectEndpoint", settings)

    @patch("endpoints.auth_info.utils.get_authorization_settings_from_file",
           return_value={
               "clientId": "test-client-id",
               "clientSecret": "super-secret-value",
               "authorizationEndpoint": "https://idp.example.com/oauth2/authorize",
               "introspectEndpoint": "https://idp.example.com/oauth2/introspect",
           })
    @patch("endpoints.auth_info.utils.is_authorization_enabled_on_station", return_value=True)
    def test_optional_logout_endpoint_defaults_to_null(self, mock_enabled, mock_settings):
        response = self.client.get(URL)
        assert response.status_code == 200, f"status_code: {response.status_code}"

        settings = response.json()["auth_settings"]
        self.assertIsNone(settings["logoutEndpoint"])
