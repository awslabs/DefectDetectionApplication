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
import collections
import json

from html import unescape
from local_server_base_test_case import LocalServerBaseTestCase
from unittest import mock
from unittest.mock import patch
import logging
mock._magics.add("__round__")

class TestSystemHealth(LocalServerBaseTestCase):

    url = ["/system-health", "https://testserver/system-health"]

    @patch("psutil.cpu_percent")
    @patch("psutil.virtual_memory")
    @patch("shutil.disk_usage")
    def test_system_health(self, disk_usage_mock, psutil_memory_mock, psutil_cpu_mock):
        psutil_cpu_mock.return_value = 5.5
        psutil_memory_mock.return_value.percent = 34.5
        disk_usage = collections.namedtuple('usage', 'total used free')
        disk_usage_mock.return_value = disk_usage(total=57039806464, used=23638577152, free=33384452096)

        for url in self.url:
            with self.subTest(url=url):
                api_response = self.client.get(url)
                assert api_response.status_code == 200, f"status_code: {api_response.status_code}"

                response_data = api_response.json()
                assert response_data["cpuUsagePercent"] == 5.5
                assert response_data["memoryUsagePercent"] == 34.5
                assert response_data["diskTotalSize"] == "53 GB"
                assert response_data["diskUsedSize"] == "22 GB"
                assert response_data["diskUsagePercent"] == 41.44
    
