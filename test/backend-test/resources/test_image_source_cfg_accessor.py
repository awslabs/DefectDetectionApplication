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
import logging
from unittest.mock import Mock
from fastapi import HTTPException
from local_server_base_test_case import LocalServerBaseTestCase
from constants import FAKE_TIME_STAMP
from sqlalchemy.orm import Session
import pytest
import uuid


class TestImageSourceConfigurationAccessor(LocalServerBaseTestCase):
    accessor = None

    def setUp(self):
        super().setUp()

        # These need to be imported inline, because they use an environment variable
        from resources.accessors.image_source_configuration_accessor import ImageSourceConfigurationAccessor
        from dao.sqlite_db import image_source_configuration_dao as dao
        from dao.sqlite_db.models import ImageSourceConfiguration

        self.logger = logging.getLogger(__name__)
        self.session = Session(self.engine)
        self.test_img_src_cfg_data0 = {
            "processingPipeline": "video/x-bayer,format=bggr ! bayer2rgb ! video/x-raw,format=RGBA ! videoconvert",
            "exposure": 4000,
            "imageSourceConfigId": str(uuid.uuid4())[:8],
            "gain": 10,
            "creationTime": FAKE_TIME_STAMP
            }
        self.session.add(ImageSourceConfiguration(**self.test_img_src_cfg_data0))
        self.session.commit()
        self.test_img_src_cfg_data1 = {
            "processingPipeline": "video/x-bayer,format=bggr ! bayer2rgb ! video/x-raw,format=RGBA ! videoconvert",
            "exposure": 4000,
            "imageSourceConfigId": "fake_img_src_cfg_uuid",
            "gain": 10,
            "creationTime": FAKE_TIME_STAMP,
            "imageCrop": {"top" : 100, "bottom" : 100, "left" : 100, "right" : 100}
            }
        self.session.add(ImageSourceConfiguration(**self.test_img_src_cfg_data1))
        self.session.commit()
        self.accessor = ImageSourceConfigurationAccessor()

    def tearDown(self):
        super().tearDown()

    def test_create_valid_image_source_configuration(self):
        from dao.sqlite_db.models import ImageSourceConfiguration
        image_src_cfg_id = self.accessor.create_image_source_configuration(
            self.session,
            {
                "gain": 11,
                "exposure": 4001,
                "processingPipeline": "test",
                "imageSourceConfigId": "fake_uuid"
            })
        result_data = self.session.get(ImageSourceConfiguration, image_src_cfg_id)
        self.assertEqual(result_data.gain, 11)

    def test_create_valid_image_source_configuration_with_crop(self):
        from dao.sqlite_db.models import ImageSourceConfiguration
        image_src_cfg_id = self.accessor.create_image_source_configuration(
            self.session,
            {
                "gain": 11,
                "exposure": 4001,
                "processingPipeline": "test",
                "imageSourceConfigId": "fake_uuid",
                "imageCrop": {"top" : 100, "bottom" : 101, "left" : 102, "right" : 103}
            })
        result_data = self.session.get(ImageSourceConfiguration, image_src_cfg_id)
        self.assertEqual(result_data.gain, 11)
        self.assertEqual(result_data.imageCrop, {"top" : 100, "bottom" : 101, "left" : 102, "right" : 103})

    def test_create_invalid_image_source_configuration(self):
        with pytest.raises(HTTPException) as err:
            response = self.accessor.create_image_source_configuration(
                self.session,
                {
                    "gain": 10,
                    "exposure": 10
                })
            assert response.type == HTTPException


    def test_create_invalid_image_source_configuration_with_crop(self):
        # missing imageCrop fields
        with pytest.raises(HTTPException) as err:
            response = self.accessor.create_image_source_configuration(
                self.session,
                {
                    "gain": 10,
                    "exposure": 10,
                    "processingPipeline": "test",
                    "imageSourceConfigId": "fake_uuid",
                    "imageCrop": {"top" : 100}
                })
            assert response.type == HTTPException

        # incorrect imageCrop fields
            response = self.accessor.create_image_source_configuration(
                self.session,
                {
                    "gain": 10,
                    "exposure": 10,
                    "processingPipeline": "test",
                    "imageSourceConfigId": "fake_uuid",
                    "imageCrop": {"top" : 100, "bot" : 101, "left" : 102, "right" : 103}
                })
            assert response.type == HTTPException

        # incorrect imageCrop data
            response = self.accessor.create_image_source_configuration(
                self.session,
                {
                    "gain": 10,
                    "exposure": 10,
                    "processingPipeline": "test",
                    "imageSourceConfigId": "fake_uuid",
                    "imageCrop": {"top" : "wrong", "bot" : 101, "left" : 102, "right" : 103}
                })
            assert response.type == HTTPException


    def test_list_image_source_configurations(self):
        response = self.accessor.list_image_source_configurations(self.session)
        self.assertEqual(len(response), 2)
        self.assertEqual(response[0].gain, 10)
        self.assertEqual(response[1].imageCrop, {"top" : 100, "bottom" : 100, "left" : 100, "right" : 100})

