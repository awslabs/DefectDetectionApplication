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
from html import unescape
from fastapi import HTTPException
from fastapi.testclient import TestClient
from model.image_source import ImageSourceType, ImageSource
from typing import Union

from exceptions.api.captured_images_exception import CapturedImageException

test_image_source = {
    "description": "camera test initial",
    "type": ImageSourceType.CAMERA,
    "lastUpdateTime":12345,
    "imageCapturePath": "/aws_dda/image-capture/fake_imgsrc_uuid",
    "creationTime": 12345,
    "name": "fake_camera0",
    "imageSourceId": "fake_image_src_id",
}


class TestImageSource(LocalServerBaseTestCase):
    def setUp(self):
        super().setUp()

        from app import app
        from endpoints.image_source import get_db
        # raise_server_exceptions=False uses our exception handlers rather than raising exception during testing
        self.client = TestClient(app, raise_server_exceptions = False)
        app.dependency_overrides[get_db] = self.override_dep

    def tearDown(self):
        super().tearDown()
    
        from app import app
        app.dependency_overrides = {}
    
    def override_dep(q: Union[str, None] = None):
        return "fake-db-session"

    @patch("endpoints.image_source.get_frame", return_value=None)
    @patch("utils.server_setup.image_source_accessor.get_image_source")
    @patch("utils.utils.convert_sqlalchemy_object_to_dict", return_value=test_image_source)
    @patch("utils.server_setup.gst_pipeline_executor.execute_image_source_pipeline", return_value={"image": "asdfg"})
    def test_preview_image_without_data(self, exec_image_source_pipeline_mock, convert_dict_mock, get_image_source_mock, get_camera_frame_mock):
        response = self.client.post("/image-sources/fake_image_src_id/preview", json={})
        exec_image_source_pipeline_mock.assert_called_once()
        assert response.status_code == 200, f"status_code: {response.status_code}"   
    
    @patch("endpoints.image_source.get_frame", return_value=None)
    @patch("utils.server_setup.image_source_accessor.get_image_source")
    @patch("utils.utils.convert_sqlalchemy_object_to_dict", return_value=test_image_source)
    @patch("utils.server_setup.gst_pipeline_executor.execute_image_source_pipeline", return_value={"image": "asdfg"})
    def test_capture(self, exec_image_source_pipeline_mock, convert_dict_mock, get_image_source_mock, get_camera_frame_mock):
        response = self.client.post("/image-sources/fake_image_src_id/capture", json={})
        exec_image_source_pipeline_mock.assert_called_once()
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("endpoints.image_source.get_frame", return_value=None)
    @patch("utils.server_setup.image_source_accessor.get_image_source")
    @patch("utils.utils.convert_sqlalchemy_object_to_dict", return_value={"type": ImageSourceType.FOLDER})
    def test_capture_folder(self, convert_dict_mock, get_image_source_mock, get_camera_frame_mock):
        response = self.client.post("/image-sources/fake_image_src_id/capture", json={})
        assert response.status_code == 400, f"status_code: {response.status_code}"

    @patch("endpoints.image_source.get_frame", return_value=None)
    @patch("utils.server_setup.image_source_accessor.get_image_source")
    @patch("utils.utils.convert_sqlalchemy_object_to_dict", return_value={"type": "notatype"})
    def test_capture_invalid_type(self, convert_dict_mock, get_image_source_mock, get_camera_frame_mock):
        response = self.client.post("/image-sources/fake_image_src_id/capture", json={})
        assert response.status_code == 500, f"status_code: {response.status_code}"

    @patch("endpoints.image_source.get_frame", return_value=None)
    @patch("utils.server_setup.image_source_accessor.get_image_source")
    @patch("utils.utils.convert_sqlalchemy_object_to_dict", return_value=test_image_source)
    @patch("utils.server_setup.gst_pipeline_executor.execute_image_source_pipeline", side_effect=CapturedImageException("Captured image was corrupted and has been deleted.", status_code=500))
    def test_capture_image_failed(self, exec_image_source_pipeline_mock, convert_dict_mock, get_image_source_mock, get_camera_frame_mock):
        response = self.client.post("/image-sources/fake_image_src_id/capture", json={})
        exec_image_source_pipeline_mock.assert_called_once()
        assert response.status_code == 500, f"status_code: {response.status_code}"

    @patch("utils.server_setup.image_source_accessor.list_image_sources", return_value=[])
    def test_list_image_source(self, list_image_source_mock):
        response = self.client.get("/image-sources")
        list_image_source_mock.assert_called_once()
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("utils.utils.convert_sqlalchemy_object_to_dict", return_value = {})
    @patch("utils.camera_manager.get_camera_status")
    @patch("utils.server_setup.image_source_accessor.get_image_source", return_value = {})
    def test_get_image_source(self, get_image_source_mock, get_camera_status_mock, get_convert_sqlalchemy_object_to_dict_mock):
        response = self.client.get("/image-sources/fake_image_src_id")
        get_image_source_mock.assert_called_once()
        assert get_image_source_mock.mock_calls[0] == call("fake_image_src_id", 'fake-db-session')
        assert response.status_code == 200, f"status_code: {response.status_code}"    

    @patch("utils.server_setup.image_source_accessor.create_image_source", return_value={"root": {"imageSourceId": "fake-id"}})
    def test_add_image_source(self, add_image_source_mock):
        response = self.client.post("/image-sources", json={})
        add_image_source_mock.assert_called_once()
        assert add_image_source_mock.mock_calls == [call({}, 'fake-db-session')]
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("utils.server_setup.image_source_accessor.update_image_source", return_value={"root": {"imageSourceId": "fake-id"}})
    def test_update_image_source(self, update_image_source_mock):
        response = self.client.patch("/image-sources/fake-img-src-id", json={"imageSourceConfiguration":{"gain":10,"exposure":4000,"processingPipeline":"capsfilter caps=video/x-raw,format=RGB ! videocrop top=700 bottom=0 left=500 right=100 ! videoconvert"}})
        update_image_source_mock.assert_called_once()
        assert update_image_source_mock.mock_calls == [call('fake-img-src-id', {"imageSourceConfiguration":{"gain":10,"exposure":4000,"processingPipeline":"capsfilter caps=video/x-raw,format=RGB ! videocrop top=700 bottom=0 left=500 right=100 ! videoconvert"}}, 'fake-db-session')]
        assert response.status_code == 200, f"status_code: {response.status_code}"  

    @patch("utils.server_setup.image_source_accessor.update_image_source", return_value={"root": {"imageSourceId": "fake-id"}})
    def test_update_image_source_with_crop(self, update_image_source_mock):
        response = self.client.patch("/image-sources/fake-img-src-id", json={"imageSourceConfiguration":{"gain":10,"exposure":4000,"processingPipeline":"capsfilter caps=video/x-raw,format=RGB ! videoconvert", "imageCrop": {"top" : 100, "bottom" : 100, "left" : 100, "right" : 100}}})
        update_image_source_mock.assert_called_once()
        assert update_image_source_mock.mock_calls == [call('fake-img-src-id', {"imageSourceConfiguration":{"gain":10,"exposure":4000,"processingPipeline":"capsfilter caps=video/x-raw,format=RGB ! videoconvert","imageCrop": {"top" : 100, "bottom" : 100, "left" : 100, "right" : 100}}}, 'fake-db-session')]
        assert response.status_code == 200, f"status_code: {response.status_code}"  

    @patch("utils.server_setup.image_source_accessor.delete_image_source", return_value=[])
    def test_delete_image_source(self, delete_image_source_mock):
        response = self.client.delete("/image-sources/fake-img-src-id")
        delete_image_source_mock.assert_called_once()
        assert delete_image_source_mock.mock_calls == [call('fake-img-src-id', 'fake-db-session')]
        assert response.status_code == 200, f"status_code: {response.status_code}" 
