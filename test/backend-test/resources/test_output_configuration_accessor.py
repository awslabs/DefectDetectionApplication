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


class TestOutputConfigurationAccessor(LocalServerBaseTestCase):

    def setUp(self):
        super().setUp()

        from resources.accessors.output_configuration_accessor import OutputConfigurationAccessor
        from dao.sqlite_db import output_configuration_dao as dao
        from dao.sqlite_db.models import OutputConfiguration
        self.session = Session(self.engine)
        self.test_output_cfg_data0 = {
            "outputConfigurationId": "fake_output_id", "creationTime": 0,
            "pin": "0", "signalType": GPIO_RISING, "pulseWidth": 0, "rule": "All"
        }
        self.session.add(OutputConfiguration(**self.test_output_cfg_data0))
        self.session.commit()
        self.accessor = OutputConfigurationAccessor()

    def tearDown(self):
        super().tearDown()

    def test_create_output_cfg_happy_path(self):
        from dao.sqlite_db.models import OutputConfiguration
        test_output_cfg_data = {
            "pin": "3",
            "signalType": GPIO_FALLING,
            "pulseWidth": 1,
            "creationTime": 1683745174,
            "rule": "Normal"
        }
        p_key = self.accessor.create_output_configuration(self.session, test_output_cfg_data)
        result_data = self.session.get(OutputConfiguration, p_key)
        self.assertEqual(result_data.pin, "3")
        self.assertEqual(result_data.signalType, GPIO_FALLING)
        self.assertEqual(result_data.pulseWidth, 1)
        self.assertEqual(result_data.rule, "Normal")
        self.assertIsNotNone(result_data.outputConfigurationId)
        self.assertIsNotNone(result_data.creationTime)

    def test_create_output_cfg_missing_attr(self):
        test_output_cfg_data = {
            "pin": "3",
            "pulseWidth": 100
        }
        with pytest.raises(HTTPException) as err:
            response = self.accessor.create_output_configuration(self.session, test_output_cfg_data)
            self.assertIn("Invalid data provided: ", response.description)

    def test_create_output_cfg_invalid_param(self):
        test_output_cfg_data = {
            "signalType": "Something else",
            "pin": "3",
            "pulseWidth": 100
        }
        with pytest.raises(HTTPException) as err:
            response = self.accessor.create_output_configuration(self.session, test_output_cfg_data)
            self.assertIn("Invalid data provided: ", response.description)

    def test_list_output_configurations(self):
        result_data = self.accessor.list_output_configurations(self.session)
        self.assertEqual(len(result_data), 1)
        self.assertEqual(result_data[0].outputConfigurationId, "fake_output_id")
