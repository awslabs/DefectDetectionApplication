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

from unittest.mock import patch
from utils.common import CameraStatusEnum
from local_server_base_test_case import LocalServerBaseTestCase
from exceptions.api.aravis_camera_exception import AravisCameraException
from data_models.common import CameraStatusModel

class TestCameraManager(LocalServerBaseTestCase):
    def test_connect_camera(self):
        from utils.camera_manager import connect_camera
        result = connect_camera("Fake_1")
        self.assertEqual(result, True)

    @patch("utils.camera_manager.get_camera_status", return_value = CameraStatusModel(status=CameraStatusEnum.DISCONNECTED, lastUpdatedTime=0.0))
    def test_connect_camera_failed(self, mock_get_camera_status):
        from utils.camera_manager import connect_camera
        try:
            connect_camera("Fake_1")
        except AravisCameraException:
            assert "Camera connection failed" 

    def test_disconnect_camera(self):
        from utils.camera_manager import disconnect_camera, get_all_camera_statuses, connect_camera
        connect_camera("Fake_1")
        result = get_all_camera_statuses()
        self.assertEqual(len(result), 1)

        disconnect_camera("Fake_1")
        result = get_all_camera_statuses()
        self.assertEqual(len(result), 0)

    def test_disconnect_nonexistent_camera(self):
        from utils.camera_manager import disconnect_camera, get_all_camera_statuses
        result = get_all_camera_statuses()
        self.assertEqual(len(result), 0)

        disconnect_camera("Fake_1")
        result = get_all_camera_statuses()
        self.assertEqual(len(result), 0) 

    def test_get_all_camera_statuses(self):
        from utils.camera_manager import get_all_camera_statuses, connect_camera
        connect_camera("Fake_1")
        connect_camera("Fake_2")
        result = get_all_camera_statuses()
        self.assertEqual(len(result), 2)

    def test_get_camera_status(self):
        from utils.camera_manager import get_camera_status, connect_camera
        connect_camera("Fake_1")
        result = get_camera_status("Fake_1")
        self.assertEqual(result.status, CameraStatusEnum.CONNECTED)

    def test_get_nonexistent_camera_status(self):
        from utils.camera_manager import get_camera_status 
        result = get_camera_status("fake")
        self.assertEqual(result.status, CameraStatusEnum.DISCONNECTED)