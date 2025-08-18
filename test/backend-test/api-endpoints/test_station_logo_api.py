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
from local_server_base_test_case import LocalServerBaseTestCase
from unittest.mock import patch

class TestFeatureConfigurations(LocalServerBaseTestCase):
    @patch("endpoints.system.get_station_logo", return_value="logoString")
    def test_get_station_logo(self, mock_get_station_logo):
        response = self.client.get("/system/station")
        assert response.status_code == 200, f"status_code: {response.status_code}"
        
        assert response.json()["logoImage"] == "logoString"
        mock_get_station_logo.assert_called_once()

    @patch("endpoints.system.get_station_logo", return_value=None)
    def test_get_station_logo_no_logo(self, mock_get_station_logo):
        response = self.client.get("/system/station")
        assert response.status_code == 200, f"status_code: {response.status_code}"
        
        assert response.json()["logoImage"] == None
        mock_get_station_logo.assert_called_once()