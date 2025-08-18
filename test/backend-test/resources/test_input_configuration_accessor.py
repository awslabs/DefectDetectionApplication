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
from unittest.mock import patch

from fastapi import HTTPException
from marshmallow import ValidationError
from sqlalchemy.orm import Session
import pytest

from local_server_base_test_case import LocalServerBaseTestCase
from utils.constants import GPIO_RISING, GPIO_FALLING


class TestInputConfigurationAccessor(LocalServerBaseTestCase):

    def setUp(self):
        super().setUp()

        from resources.accessors.input_configuration_accessor import InputConfigurationAccessor
        from dao.sqlite_db import input_configuration_dao as dao
        from dao.sqlite_db.models import InputConfiguration
        self.session = Session(self.engine)
        self.test_input_cfg_data0 = {
            "inputConfigurationId": "fake_input_id", "creationTime": 0,
            "pin": "0", "triggerState": GPIO_RISING, "debounceTime": 0
        }
        self.session.add(InputConfiguration(**self.test_input_cfg_data0))
        self.session.commit()
        self.accessor = InputConfigurationAccessor()

    def tearDown(self):
        super().tearDown()

    def test_create_input_cfg_happy_path(self):
        from dao.sqlite_db.models import InputConfiguration
        test_input_cfg_data = {
            "triggerState": GPIO_FALLING,
            "pin": "3",
            "debounceTime": 100
        }
        p_key = self.accessor.create_input_configuration(self.session, test_input_cfg_data)
        result_data = self.session.get(InputConfiguration, p_key)
        self.assertEqual(result_data.triggerState, GPIO_FALLING)
        self.assertEqual(result_data.pin, "3")
        self.assertEqual(result_data.debounceTime, 100)
        self.assertIsNotNone(result_data.inputConfigurationId)
        self.assertIsNotNone(result_data.creationTime)

    def test_create_input_cfg_missing_attr(self):
        test_input_cfg_data = {
            "pin": "3",
            "debounceTime": 100
        }
        with pytest.raises(HTTPException) as err:
            response = self.accessor.create_input_configuration(self.session, test_input_cfg_data)
            self.assertIn("Invalid data provided: ", response.description)

    def test_create_input_cfg_invalid_param(self):
        test_input_cfg_data = {
            "triggerState": "Other",
            "pin": "3",
            "debounceTime": 100
        }
        with pytest.raises(HTTPException) as err:
            response = self.accessor.create_input_configuration(self.session, test_input_cfg_data)
            self.assertIn("Invalid data provided: ", response.description)

    def test_list_input_configurations(self):
        result_data = self.accessor.list_input_configurations(self.session)
        self.assertEqual(len(result_data), 1)
        self.assertEqual(result_data[0].inputConfigurationId, "fake_input_id")
