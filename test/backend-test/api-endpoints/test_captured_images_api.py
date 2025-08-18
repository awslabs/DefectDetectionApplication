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

from html import unescape
from local_server_base_test_case import LocalServerBaseTestCase
from unittest.mock import patch, call
from fastapi.testclient import TestClient
import json


import logging

logger = logging.getLogger(__name__)

class TestCapturedImages(LocalServerBaseTestCase):
    def setUp(self):
        super().setUp()

        from app import app
        from endpoints.image_source import Query
        self.client = TestClient(app)
        app.dependency_overrides[Query] = self.override_query_parameter_validation

    def tearDown(self):
        super().tearDown()

    # Override query parameter for unit test purpose,
    # because test files all store in "test/backend-test/..."
    def override_query_parameter_validation(q: str):
        return q

    @patch("os.path.exists", return_value=True)
    @patch("utils.captured_images_utils.get_images", return_value={"root": []})
    def test_list_captured_images(self, get_images_mock, mock_path_exists):
        response = self.client.get("/captured-images?path=/aws_dda/image-capture/fake")
        get_images_mock.assert_called_once()
        assert get_images_mock.mock_calls == [call('/aws_dda/image-capture/fake', 12)]
        assert response.status_code == 200, f"status_code: {response.status_code}"

    def test_list_captured_images_invalid_path(self):
        response = self.client.get("/captured-images")
        assert response.status_code == 400, f"status_code: {response.status_code}"
        assert "{'type': 'missing', 'loc': ('query', 'path'), 'msg': 'Field required', 'input': None, 'url': 'https://errors.pydantic.dev/2.6/v/missing'}" in  response.json()['message']

    def test_list_captured_images_non_exist_path(self):
        response = self.client.get("/captured-images?path=/aws_dda/image-capture/nonexist")
        assert response.status_code == 404, f"status_code: {response.status_code}"
        assert "The server can't get captured Images. Error: 'No images were found in /aws_dda/image-capture/nonexist'." in response.json()['message']

    @patch("os.path.isfile", return_value=True)
    @patch("utils.captured_images_utils.delete_image", return_value="")
    def test_delete_captured_images(self, get_images_mock, mock_file_exists):
        response = self.client.delete("/captured-images?filePath=/aws_dda/image-capture/test-fake.jpg")
        get_images_mock.assert_called_once()
        assert call('/aws_dda/image-capture/test-fake.jpg') in get_images_mock.mock_calls
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("os.path.isfile", return_value=False)
    @patch("utils.captured_images_utils.delete_image", return_value="")
    def test_delete_captured_images_non_exists_file(self, get_images_mock, mock_file_nonexists):
        response = self.client.delete("/captured-images?filePath=/aws_dda/image-capture/test-fake-non.jpg")
        assert response.status_code == 404, f"status_code: {response.status_code}"
        assert "The server can't delete the captured images. Error: 'No images were found in /aws_dda/image-capture/test-fake-non.jpg'." in response.json()['message']

    def test_delete_captured_images_invalid_path(self):
        response = self.client.delete("/captured-images")
        logger.info(response.json())
        assert response.status_code == 400, f"status_code: {response.status_code}"
        assert "{'type': 'missing', 'loc': ('query', 'filePath'), 'msg': 'Field required', 'input': None, 'url': 'https://errors.pydantic.dev/2.6/v/missing'}" in  response.json()['message']
