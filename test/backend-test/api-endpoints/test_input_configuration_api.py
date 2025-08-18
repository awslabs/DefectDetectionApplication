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
from unittest.mock import patch, call


class TestInputConfigurations(LocalServerBaseTestCase):

    @patch("utils.server_setup.input_cfg_accessor.list_input_configurations", return_value={"root": []})
    def test_list_input_configs(self, list_input_config_mock):
        response = self.client.get("/input-configurations")
        list_input_config_mock.assert_called_once()
        assert response.status_code == 200, f"status_code: {response.status_code}"

