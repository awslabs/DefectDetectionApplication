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
import copy
import logging
import constants
import uuid
from local_server_base_test_case import LocalServerBaseTestCase
from fastapi import HTTPException
from constants import FAKE_TIME_STAMP
from sqlalchemy.orm import Session
from utils import utils
import pytest
from unittest.mock import patch, Mock
from model.image_source import ImageSourceType

class TestImageSourceAccessor(LocalServerBaseTestCase):

    def setUp(self):
        super().setUp()

        from resources.accessors.image_source_accessor import ImageSourceAccessor
        from dao.sqlite_db import image_source_dao as dao
        self.session = Session(self.engine)
        self.accessor = ImageSourceAccessor()

        self.img_src_cfg_data = {
            "processingPipeline": "", "exposure": 1, "gain": 1, "creationTime": FAKE_TIME_STAMP,
            "imageSourceConfigId": "fake_cfg_id"
        }
        self.default_config = {
            "gain": 1,
            "exposure": 500,
            "processingPipeline": "capsfilter caps=video/x-bayer,format=bggr ! bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert"
        }
        self.accessor._ImageSourceAccessor__get_default_image_source_configuration = Mock(return_value=self.default_config)
        self.create_test_image_source()

    def tearDown(self):
        super().tearDown()

    def create_test_image_source(self):
        from dao.sqlite_db.models import ImageSource
        from dao.sqlite_db.models import ImageSourceConfiguration
        self.session.add(ImageSourceConfiguration(**self.img_src_cfg_data))
        self.session.commit()
        self.fake_image_src_id = str(uuid.uuid4())[:8]
        self.test_image_source_data0 = {
            "description": "camera test initial",
            "type": "Camera",
            "cameraId": "fake_camera_id",
            "lastUpdateTime": constants.FAKE_TIME_STAMP,
            "location": "/tmp/ddatests",
            "imageCapturePath": "/aws_dda/image-capture/fake_imgsrc_uuid",
            "creationTime": constants.FAKE_TIME_STAMP,
            "name": "fake_camera0",
            "imageSourceId": self.fake_image_src_id,
            "imageSourceConfigId": "fake_cfg_id"
         }
        self.session.add(ImageSource(**self.test_image_source_data0))
        self.session.commit()

    def test_create_image_source_folder_happy_path(self):
        from dao.sqlite_db.models import ImageSource
        test_image_source_data = {
            "description": "folder test",
            "location": "/tmp/ddatests",
            "name": "fake_folder",
            "type": "Folder"
        }
        with patch.object(self.accessor, '_ImageSourceAccessor__create_folder', return_value='/tmp/ddatests') as method:
            p_key = self.accessor.create_image_source(test_image_source_data, self.session)
            result_data = self.session.get(ImageSource, p_key)
            self.assertEqual(result_data.location, "/tmp/ddatests")
            self.assertEqual(result_data.description, "folder test")
            self.assertEqual(result_data.type.value, "Folder")

    def test_create_image_source_camera_happy_path(self):
        from dao.sqlite_db.models import ImageSource
        test_image_source_data = {
            "description": "camera test",
            "location": "/tmp/ddatests",
            "name": "fake_camera",
            "type": "Camera",
            "cameraId": "fake_camera_id"
        }
        with patch.object(self.accessor, '_ImageSourceAccessor__create_folder', return_value='/tmp/ddatests') as method:
            p_key = self.accessor.create_image_source(test_image_source_data, self.session)
            result_data = self.session.get(ImageSource, p_key)
            self.assertEqual(result_data.location, "/tmp/ddatests")
            self.assertEqual(result_data.description, "camera test")
            self.assertEqual(result_data.type.value, "Camera")
            self.assertEqual(result_data.cameraId, "fake_camera_id")

    def test_create_image_source_missing_attr(self):
        test_image_source_data_missing_attr = {
            "description": "folder test",
            "name": "fake_folder1"
        }
        with pytest.raises(HTTPException) as err:
            err = self.accessor.create_image_source(test_image_source_data_missing_attr, self.session)
            self.assertIn("Invalid data provided: ", err.description)

    def test_create_image_source_invalid_data_folder_missing_location(self):
        test_image_source_data_missing_attr = {
            "description": "folder test",
            "name": "fake_folder",
            "type": "Folder"
        }
        with pytest.raises(HTTPException) as err:
            with patch.object(self.accessor, '_ImageSourceAccessor__create_folder', return_value='/tmp/ddatests') as method:
                err = self.accessor.create_image_source(test_image_source_data_missing_attr, self.session)
                err_msg = "Invalid data provided: {'_schema': ['location is required when image source type is Folder']}"
                self.assertEqual(err_msg, err.description)


    def test_create_image_source_invalid_data_camera_missing_params(self):
        test_image_source_data_missing_attr = {
            "description": "folder test",
            "name": "fake_folder",
            "type": "Camera",
            "cameraId": ""
        }
        with pytest.raises(HTTPException) as err:
            with patch.object(self.accessor, '_ImageSourceAccessor__create_folder', return_value='/tmp/ddatests') as method:
                err = self.accessor.create_image_source(test_image_source_data_missing_attr, self.session)
                err_msg = "Invalid data provided: {'_schema': [\"['cameraId'] required when image source type is Camera\"]}"
                self.assertEqual(err_msg, err.description)

    def test_list_image_source_camera_happy_path(self):
        list_result = self.accessor.list_image_sources('Camera', self.session)
        self.assertEqual(len(list_result), 1)
        self.assertEqual(list_result[0].imageSourceId, self.fake_image_src_id)

    def test_list_image_source_camera_happy_path_without_type(self):
        list_result = self.accessor.list_image_sources(None, self.session)
        self.assertEqual(len(list_result), 1)
        self.assertEqual(list_result[0].imageSourceId, self.fake_image_src_id)

    def test_get_image_source_by_id(self):
        from dao.sqlite_db.models import ImageSource
        fake_id = "fake_img_src-" + str(uuid.uuid4())[:8]
        self.img_src_data = {
            "description": "test image source config", "location": "/tmp/ddatests",
            "name": "fake_camera", "type": "Camera", "cameraId": "fake_camera_id",
            "lastUpdateTime": constants.FAKE_TIME_STAMP, "imageCapturePath": "/aws_dda/image-capture/fake_imgsrc_uuid",
            "creationTime": constants.FAKE_TIME_STAMP, "imageSourceConfigId": "fake_cfg_id",
            "imageSourceId": fake_id
        }
        img_src_cfg_data = {
            "processingPipeline": "", "exposure": 1, "gain": 1, "creationTime": FAKE_TIME_STAMP,
            "imageSourceConfigId": "fake_cfg_id",
            "imageCrop": None,
            "device": None,
            "deviceName": None,
        }
        self.session.add(ImageSource(**self.img_src_data))
        self.session.commit()
        get_result = self.accessor.get_image_source(self.img_src_data["imageSourceId"], self.session)
        self.assertEqual(get_result.description, "test image source config")
        self.assertEqual(utils.convert_sqlalchemy_object_to_dict(get_result.imageSourceConfiguration), img_src_cfg_data)

    def test_get_image_source_by_id_non_exist(self):
        with pytest.raises(HTTPException) as err:
            response = self.accessor.get_image_source("nonexist_id", self.session)
            assert response.type == HTTPException

    def test_update_image_source_happy_path(self):
        from dao.sqlite_db.models import ImageSource
        test_update = {"name": "update_folder", "description": "folder update"}
        p_key = self.accessor.update_image_source(self.test_image_source_data0["imageSourceId"], test_update, self.session)
        result_data = self.session.get(ImageSource, p_key)
        self.assertEqual(result_data.description, "folder update")
        self.assertEqual(result_data.name, "update_folder")

    def test_update_image_source_with_cfg(self):
        from dao.sqlite_db.models import ImageSource
        test_update = {
            "name": "update_with_cfg", "description": "update with config",
            "imageSourceConfiguration": copy.deepcopy(self.img_src_cfg_data)
        }
        p_key = self.accessor.update_image_source(self.test_image_source_data0["imageSourceId"], test_update, self.session)
        result_data = self.session.get(ImageSource, p_key)
        self.assertEqual(result_data.description, "update with config")
        self.assertIsNotNone(result_data.imageSourceConfigId)

    def test_update_image_source_with_location(self):
        from dao.sqlite_db.models import ImageSource
        test_update = {
            "name": "update_with_location", "description": "update with location",
            "location": "/tmp/ddatests_updated"
        }
        with patch.object(self.accessor, '_ImageSourceAccessor__create_folder', return_value='/tmp/ddatests_updated') as method:
            p_key = self.accessor.update_image_source(self.test_image_source_data0["imageSourceId"], test_update, self.session)
            result_data = self.session.get(ImageSource, p_key)
            self.assertEqual(result_data.location, "/tmp/ddatests_updated")

    def test_update_image_source_with_relative_path_location(self):
        test_update = {
            "name": "update_with_location", "description": "update with location",
            "location": "tmp/ddatests_updated"
        }
        with pytest.raises(HTTPException) as err:
            response = self.accessor.update_image_source(self.test_image_source_data0["imageSourceId"], test_update, self.session)
            err = "Invalid data provided: ['Folder path is required and should be absolute path: tmp/ddatests_updated']"
            assert response.description == err
            assert response.type == HTTPException

    def test_update_image_source_non_exist(self):
        with pytest.raises(HTTPException) as err:
            test_update = {"name": "update_folder", "description": "camera update with invalid schema"}
            response = self.accessor.update_image_source("nonexist_id", test_update, self.session)
            err = "Item with key value pair {'imageSourceId': 'nonexist_id'} does not exist."
            assert response.description.args[0] == err
            assert response.type == HTTPException

    def test_update_image_source_invalid_data(self):
        with pytest.raises(HTTPException) as err:
            test_update = {"unknow_field": ""}
            response = self.accessor.update_image_source(self.test_image_source_data0["imageSourceId"], test_update, self.session)
            err = "Invalid data provided"
            assert response.description == err
            assert response.type == HTTPException

    def test_delete_image_source(self):
        from dao.sqlite_db.models import ImageSource
        data_init = self.session.get(ImageSource, self.test_image_source_data0["imageSourceId"])
        self.assertEqual(data_init.imageSourceId, self.test_image_source_data0["imageSourceId"])
        self.accessor.delete_image_source(self.test_image_source_data0["imageSourceId"], self.session)
        data_del = self.session.get(ImageSource, self.test_image_source_data0["imageSourceId"])
        self.assertIsNone(data_del)

    def test_update_all_image_sources_with_camera_status(self):
        from dao.sqlite_db.models import ImageSource
        test_image_source_data = {
            "description": "camera test",
            "location": "/tmp/ddatests",
            "name": "fake_camera",
            "type": "Folder",
            "cameraId": "fake_camera_id"
        }
        with patch.object(self.accessor, '_ImageSourceAccessor__create_folder', return_value='/tmp/ddatests') as method:
            p_key = self.accessor.create_image_source(test_image_source_data, self.session)
            result_data = self.session.get(ImageSource, p_key)
            data = self.accessor.update_all_image_sources_with_camera_status([result_data])
            self.assertEqual(data[0]["cameraStatus"], None)

    def test_update_image_source_with_camera_status(self):
        from utils.common import CameraStatusEnum

        image_source_dict = {"type": "Camera"}
        data = self.accessor.update_image_source_with_camera_status(image_source_dict)
        self.assertEqual(data["cameraStatus"].status, CameraStatusEnum.DISCONNECTED)
