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
import unittest
from unittest.mock import patch, MagicMock
from utils.gg_utils import  stop_running_component


class TestStopFunction(unittest.TestCase):

    @patch('awsiot.greengrasscoreipc.connect')
    def test_stop_component_success(self, mock_connect):
        # Mock IPC client and operations
        mock_ipc_client = MagicMock()
        mock_connect.return_value = mock_ipc_client
        
        mock_stop_component_operation = MagicMock()
        mock_ipc_client.new_stop_component.return_value = mock_stop_component_operation
        
        # Mock response from IPC
        mock_stop_component_future = MagicMock()
        mock_stop_component_response = MagicMock()
        mock_stop_component_response.stop_status = "SUCCEEDED"
        mock_stop_component_future.result.return_value = mock_stop_component_response
        mock_stop_component_operation.get_response.return_value = mock_stop_component_future
        
        # Test stop_function
        component_name = "test_component"
        result = stop_running_component(component_name)
        
        # Assertions
        self.assertTrue(result)
        mock_ipc_client.new_stop_component.assert_called_once()

    @patch('awsiot.greengrasscoreipc.connect')
    def test_stop_component_failure(self, mock_connect):
        # Mock IPC client and operations
        mock_ipc_client = MagicMock()
        mock_connect.return_value = mock_ipc_client
        
        mock_stop_component_operation = MagicMock()
        mock_ipc_client.new_stop_component.return_value = mock_stop_component_operation
        
        # Mock response from IPC
        mock_stop_component_future = MagicMock()
        mock_stop_component_response = MagicMock()
        mock_stop_component_response.stop_status = "FAILED"
        mock_stop_component_future.result.return_value = mock_stop_component_response
        mock_stop_component_operation.get_response.return_value = mock_stop_component_future
        
        # Test stop_function
        component_name = "test_component"
        result = stop_running_component(component_name)
        
        # Assertions
        self.assertFalse(result)
        mock_ipc_client.new_stop_component.assert_called_once()
