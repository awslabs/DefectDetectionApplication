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


class TestLatencyTimeAccessor(LocalServerBaseTestCase):

    def setUp(self):
        super().setUp()

        from resources.accessors.latency_time_accessor import LatencyTimeAccessor
        from dao.sqlite_db.models import LatencyTime
        logger = logging.getLogger(__name__)
        self.session = Session(self.metadata_engine)
        self.test_latency_time_data0 = {
            "inferenceCaptureId": "fake-capture-id1", 
            "latencyType": "FRAME_CAPTURE",
            "timestamp": 100.0,
        }
        self.session.add(LatencyTime(**self.test_latency_time_data0))
        self.session.commit()
        self.accessor = LatencyTimeAccessor()

    def tearDown(self):
        super().tearDown()

    def test_store_latency_time_happy_path(self):
        from dao.sqlite_db.models import LatencyTime
        test_latency_time_data = {
            "inferenceCaptureId": "12345", 
            "latencyType": "FRAME_CAPTURE",
            "timestamp": 123.0,
        }
        p_key = self.accessor.store_latency_time(self.session, test_latency_time_data)
        print(p_key)
        result_data = self.session.get(LatencyTime, (p_key, "FRAME_CAPTURE"))
        self.assertEqual(result_data.inferenceCaptureId, "12345")
        self.assertEqual(result_data.latencyType, "FRAME_CAPTURE")
        self.assertEqual(result_data.timestamp, 123.0)

    def test_store_latency_times_happy_path(self):
        from dao.sqlite_db.models import LatencyTime
        test_latency_time_data_entries = [
            {
                "inferenceCaptureId": "123456", 
                "latencyType": "FRAME_CAPTURE",
                "timestamp": 123.0,
            },
            {
                "inferenceCaptureId": "123456", 
                "latencyType": "INFERENCE_RECIEVED",
                "timestamp": 1234.0,
            }
        ]
        self.accessor.store_latency_times(self.session, test_latency_time_data_entries)
        result_data0 = self.session.get(LatencyTime, ("123456", "FRAME_CAPTURE"))
        self.assertEqual(result_data0.inferenceCaptureId, "123456")
        self.assertEqual(result_data0.latencyType, "FRAME_CAPTURE")
        self.assertEqual(result_data0.timestamp, 123.0)

        result_data1 = self.session.get(LatencyTime, ("123456", "INFERENCE_RECIEVED"))
        self.assertEqual(result_data1.inferenceCaptureId, "123456")
        self.assertEqual(result_data1.latencyType, "INFERENCE_RECIEVED")
        self.assertEqual(result_data1.timestamp, 1234.0)

    def test_store_latency_time_missing_attr(self):
        from dao.sqlite_db.models import LatencyTime
        test_latency_time_data = {
            "inferenceCaptureId": "1234567", 
        }
        with pytest.raises(HTTPException) as err:
            response = self.accessor.store_latency_time(self.session, test_latency_time_data)
            self.assertIn("Invalid data provided: ", response.description)

    def test_store_latency_time_invalid_param(self):
        test_latency_time_data = {
            "inferenceCaptureId": "23456", 
            "latencyType": 123,
            "timestamp": "WRONG"
        }
        with pytest.raises(HTTPException) as err:
            response = self.accessor.store_latency_time(self.session, test_latency_time_data)
            self.assertIn("Invalid data provided: ", response.description)

    def test_get_latency_time(self):
        timestamp = self.accessor.get_latency_time(self.session, "fake-capture-id1", "FRAME_CAPTURE")
        self.assertEqual(timestamp, 100.0)
