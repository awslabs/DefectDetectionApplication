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
import os
import time
import pytest
import logging
import shutil
from local_server_base_test_case import LocalServerBaseTestCase
from exceptions.api.captured_images_exception import CapturedImageException, ImageNotFoundException

@pytest.mark.usefixtures("caplog")
class TestCapturedImagesUtils(LocalServerBaseTestCase):
    def setUp(self):
        super().setUp()
        self.path = "test/backend-test/captured_images_for_test/"
        self.max_images = 12
        if "test-2.jpg" not in os.listdir(self.path):
            shutil.copy(self.path + 'test-1.jpg', self.path + 'test-2.jpg')
        
        self.infer_path = self.path + 'folder_img_src/'
        if os.path.exists(self.infer_path):
            shutil.rmtree(self.infer_path)
        os.mkdir(self.infer_path)
        for file in ["non-image.log", "non-jpg.png", "empty.jpg", "test-1.jpg"]:
            shutil.copy(self.path + file, self.infer_path + file)
            time.sleep(0.1)
        
        self.logger = logging.getLogger(__name__)

    def tearDown(self):
        super().tearDown()
        copy_image = "test/backend-test/utils/tmp/A_img0_03-14T16:11:30_1-1.jpg"
        shutil.copy(copy_image, self.path + 'test-1.jpg')
        shutil.copy(copy_image, self.path + 'test-2.jpg')

        shutil.rmtree(self.infer_path)

    def test_get_images(self):
        from utils.captured_images_utils import get_images
        res = get_images(self.path, self.max_images)
        assert set(res[0]) == {"path", "image"}
        assert len(res) == 5
        assert {res[0]["path"], res[1]["path"]} == {self.path + 'test-1.jpg', self.path + 'test-2.jpg'}

    def test_get_images_empty_path(self):
        from utils.captured_images_utils import get_images
        empty_path = self.path + '/subfolder/'
        temp_path = self.path + '/'
        for file in os.listdir(empty_path):
            shutil.move(empty_path + file, temp_path + file)
        res = get_images(empty_path, self.max_images)
        assert res == []
        shutil.move(temp_path + file, empty_path + file)

    def test_delete_image(self):
        from utils.captured_images_utils import delete_image
        res = delete_image(self.path + 'test-2.jpg')
        assert "test-2.jpg" not in os.listdir(self.path)
        assert res == "test-2.jpg"

    def test_get_oldest_image_file_path_corrupted(self):
        from utils.captured_images_utils import get_oldest_image_file_path
        os.remove(self.infer_path + 'test-1.jpg')
        try:
            jpg_filepath = get_oldest_image_file_path(self.infer_path)
            assert False
        except CapturedImageException as e:
            assert "corrupted" in str(e)

    def test_get_oldest_image_file_path_success(self):
        from utils.captured_images_utils import get_oldest_image_file_path
        os.remove(self.infer_path + 'empty.jpg')
        jpg_filepath = get_oldest_image_file_path(self.infer_path)
        assert jpg_filepath == self.infer_path + "test-1.jpg"

    def test_get_oldest_image_file_path_no_jpg(self):
        from utils.captured_images_utils import get_oldest_image_file_path
        os.remove(self.infer_path + 'empty.jpg')
        os.remove(self.infer_path + 'test-1.jpg')
        try:
            jpg_filepath = get_oldest_image_file_path(self.infer_path)
            assert False, jpg_filepath
        except ImageNotFoundException as e:
            assert "No JPG/JPEG image files found" in str(e)

    def test_convert_captured_data_to_save_in_db(self):
        from utils.captured_images_utils import convert_captured_data_to_db
        workflow_id = "fake-workflow-id"
        capture_task_id = "fake-id-1"
        captured_data = {
            "captureTaskId": capture_task_id,
            "capturedLocation": "/aws/fake/fake-path-1234567890.jpg",
        }
        expected = {
            "captureId": capture_task_id + "-1234567890",
            "inputImageFilePath": "/aws/fake/fake-path-1234567890.jpg",
            "workflowId": workflow_id,
            "inferenceCreationTime": "1234567890",
            "captureType": "Capture",
            "downloaded": False,
            "flagForReview": False
        }
        result_data = convert_captured_data_to_db(captured_data, workflow_id)
        assert result_data == expected

