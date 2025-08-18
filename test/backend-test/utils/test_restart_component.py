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
from unittest.mock import MagicMock, patch
from utils.gg_utils import restart_components  
import pytest 
from exceptions.api.triton_exceptions import GreengrassOperationException
class RestartComponentRequest:
    def __init__(self):
        self.component_name = None

# Mock response class
class MockResponse:
    def __init__(self, status="SUCCEEDED", message=""):
        self.restart_status = status
        self.message = message

# Test class for restart_components function
class TestRestartComponent(unittest.TestCase):
    @patch('utils.gg_utils.awsiot.greengrasscoreipc.connect')
    def test_restart_component_success(self, mock_connect):
        # Mock the IPC client and its methods
        mock_ipc_client = MagicMock()
        mock_connect.return_value = mock_ipc_client
        mock_restart_operation = MagicMock()
        mock_ipc_client.new_restart_component.return_value = mock_restart_operation
        mock_restart_future = MagicMock()
        mock_restart_operation.get_response.return_value = mock_restart_future
        mock_restart_future.result.return_value = MockResponse(status="SUCCEEDED")

        # Define the components to restart
        components_to_restart = ["model-1", "model-2", "aws.iot.lookoutvision.EdgeAgent"]

        # Call the function
        restart_components(components_to_restart)

        # Assertions to ensure the methods were called with the correct arguments
        self.assertEqual(mock_ipc_client.new_restart_component.call_count, 3)
        self.assertEqual(mock_restart_operation.activate.call_count, 3)
        self.assertEqual(mock_restart_operation.get_response.call_count, 3)
        self.assertEqual(mock_restart_future.result.call_count, 3)

    @patch('utils.gg_utils.awsiot.greengrasscoreipc.connect')
    def test_restart_component_failure(self, mock_connect):
        with pytest.raises(GreengrassOperationException) as err:
            # Mock the IPC client and its methods
            mock_ipc_client = MagicMock()
            mock_connect.return_value = mock_ipc_client
            mock_restart_operation = MagicMock()
            mock_ipc_client.new_restart_component.return_value = mock_restart_operation
            mock_restart_future = MagicMock()
            mock_restart_operation.get_response.return_value = mock_restart_future
            mock_restart_future.result.return_value = MockResponse(status="FAILED", message="Error")

            # Define the components to restart
            components_to_restart = ["model-1", "model-2", "aws.iot.lookoutvision.EdgeAgent"]

            # Call the function
            restart_components(components_to_restart)

            # Assertions to ensure the methods were called with the correct arguments
            self.assertEqual(mock_ipc_client.new_restart_component.call_count, 3)
            self.assertEqual(mock_restart_operation.activate.call_count, 3)
            self.assertEqual(mock_restart_operation.get_response.call_count, 3)
            self.assertEqual(mock_restart_future.result.call_count, 3)

