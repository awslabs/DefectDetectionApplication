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
from utils.gg_utils import list_gg_components

class TestListComponents(unittest.TestCase):
    @patch('awsiot.greengrasscoreipc.connect')
    def test_list_component_running_components(self, mock_connect):
        # Create a mock IPC client
        mock_ipc_client = MagicMock()
        mock_connect.return_value = mock_ipc_client

        # Mock the list_components_operation
        mock_list_components_operation = MagicMock()
        mock_ipc_client.new_list_components.return_value = mock_list_components_operation

        # Mock the response future
        mock_list_components_future = MagicMock()
        mock_list_components_operation.get_response.return_value = mock_list_components_future
        # Create a mock response
        mock_list_components_response = MagicMock()
        mock_list_components_response.components = [
            MagicMock(component_name="model-1", state="RUNNING"),
            MagicMock(component_name="aws.iot.lookoutvision.EdgeAgent", state="STARTING"),
            MagicMock(component_name="othername", state="STOPPED"),
            MagicMock(component_name="othername-2", state="RUNNING")
        ]
        mock_list_components_future.result.return_value = mock_list_components_response

        return_stopped_components=False
        # Run the function
        result = list_gg_components(return_stopped_components)

        # Check the expected result
        expected_result = ["model-1", "aws.iot.lookoutvision.EdgeAgent"]
        self.assertEqual(result, expected_result)

    @patch('awsiot.greengrasscoreipc.connect')
    def test_list_component_stopped_components(self, mock_connect):
        # Create a mock IPC client
        mock_ipc_client = MagicMock()
        mock_connect.return_value = mock_ipc_client

        # Mock the list_components_operation
        mock_list_components_operation = MagicMock()
        mock_ipc_client.new_list_components.return_value = mock_list_components_operation

        # Mock the response future
        mock_list_components_future = MagicMock()
        mock_list_components_operation.get_response.return_value = mock_list_components_future
        # Create a mock response
        mock_list_components_response = MagicMock()
        mock_list_components_response.components = [
            MagicMock(component_name="model-1", state="STOPPED"),
            MagicMock(component_name="model-2", state="STOPPED"),
            MagicMock(component_name="aws.iot.lookoutvision.EdgeAgent", state="STOPPED"),
            MagicMock(component_name="model-3", state="FINISHED"),
            MagicMock(component_name="othername", state="STARTING"),
            MagicMock(component_name="othername-2", state="RUNNING")
        ]
        mock_list_components_future.result.return_value = mock_list_components_response

        return_stopped_components=True
        # Run the function
        result = list_gg_components(return_stopped_components)

        # Check the expected result
        expected_result = ["model-1", "model-2", "model-3","aws.iot.lookoutvision.EdgeAgent"]
        self.assertEqual(result, expected_result)

    @patch('awsiot.greengrasscoreipc.connect')
    def test_list_component_dda_components(self, mock_connect):
        # Create a mock IPC client
        mock_ipc_client = MagicMock()
        mock_connect.return_value = mock_ipc_client

        # Mock the list_components_operation
        mock_list_components_operation = MagicMock()
        mock_ipc_client.new_list_components.return_value = mock_list_components_operation

        # Mock the response future
        mock_list_components_future = MagicMock()
        mock_list_components_operation.get_response.return_value = mock_list_components_future
        # Create a mock response
        mock_list_components_response = MagicMock()
        mock_list_components_response.components = [
            MagicMock(component_name="aws.edgeml.dda.LocalServer", state="STOPPED"),
            MagicMock(component_name="model-2", state="STOPPED"),
            MagicMock(component_name="aws.iot.lookoutvision.EdgeAgent", state="STOPPED"),
            MagicMock(component_name="model-3", state="FINISHED"),
            MagicMock(component_name="aws.edgeml.dda.SecondComponent", state="STARTING"),
            MagicMock(component_name="othername-2", state="RUNNING")
        ]
        mock_list_components_future.result.return_value = mock_list_components_response

        return_stopped_components=True
        return_dda_components=True
        # Run the function
        result = list_gg_components(return_stopped_components, return_dda_components)

        # Check the expected result
        expected_result = ["aws.edgeml.dda.LocalServer", "aws.edgeml.dda.SecondComponent", "aws.iot.lookoutvision.EdgeAgent","model-2", "model-3"]
        self.assertCountEqual(result, expected_result)

    @patch('awsiot.greengrasscoreipc.connect')
    def test_list_components_empty(self, mock_client):
        # Mocking an empty list_components_response
        #LFV edge agent is retrurned every time
        mock_list_components_response = ['aws.iot.lookoutvision.EdgeAgent']
        mock_client.list_components.return_value = mock_list_components_response

        return_stopped_components=False
        # Call the list_components function
        running_components = list_gg_components(return_stopped_components)

        # Assert the expected output
        self.assertCountEqual(running_components, [])
