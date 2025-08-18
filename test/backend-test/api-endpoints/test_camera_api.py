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
from unittest.mock import patch, call, Mock
from fastapi import HTTPException
from fastapi.testclient import TestClient

from typing import Union

test_camera_id = "Fake_1"

class TestCamera(LocalServerBaseTestCase):
    @patch("endpoints.camera.connect_camera")
    def test_connect_camera_endpoint(self, mock_connect_camera):
        response = self.client.get(f"/cameras/{test_camera_id}/connect")
        assert response.status_code == 200, f"status_code: {response.status_code}"
        mock_connect_camera.assert_called_once()

    @patch("endpoints.camera.disconnect_camera")
    def test_disconnect_camera_endpoint(self, mock_disconnect_camera):
        response = self.client.get(f"/cameras/{test_camera_id}/disconnect")
        assert response.status_code == 200, f"status_code: {response.status_code}"
        mock_disconnect_camera.assert_called_once()