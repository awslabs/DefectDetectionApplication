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

from html import unescape
from local_server_base_test_case import LocalServerBaseTestCase
from unittest.mock import patch, call
import logging
logging.basicConfig(level=logging.INFO)


class TestListImages(LocalServerBaseTestCase):

    def setUp(self):
        super().setUp()

        self.get_em_agent_config_patcher = patch("utils.utils.get_em_agent_config_path_for_stream")
        self.is_file_patcher = patch("app.os.path.isfile", return_value=True)
        self.get_inference_results_object_patcher = patch("utils.inference_results_utils.GetInferenceResults")
        
        self.mock_get_em_agent_config = self.get_em_agent_config_patcher.start()
        self.mock_get_em_agent_config.return_value = "test/backend-test/em-agent-fake-workflow-id.json"
        self.mock_is_file = self.is_file_patcher.start()
        self.mock_get_inference_results_object = self.get_inference_results_object_patcher.start()
        self.mock_get_inference_results_object.return_value.get_inference_results.return_value = {}

    def tearDown(self):
        super().tearDown()
        self.get_em_agent_config_patcher.stop()
        self.is_file_patcher.stop()
        self.get_inference_results_object_patcher.stop()

    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id", return_value={"workflowOutputPath": ""})
    @patch("utils.server_setup.workflow_accessor.check_workflow_health")
    @patch("utils.inference_results_utils.GetInferenceResults")
    @patch("utils.server_setup.latency_time_accessor.store_latency_time")
    def test_list_images_minimum_options_happy_path(self, store_latency_time_mock, mock_get_inference_results, mock_check_workflow_health, get_workflows_mock):
        response = self.client.get('/workflows/fake-workflow-id/images')
        mock_get_inference_results.assert_called_once()
        mock_get_inference_results.return_value.get_inference_results.return_value = {"images":[{"inferenceFilePath": "fake-path/12345678.jsonl"}]}
        get_workflows_mock.assert_called_once()
        object_mock_calls = [call('fake-workflow-id', 'desc', 0, 2), call().get_inference_results("")]
        assert mock_get_inference_results.mock_calls[0] == object_mock_calls[0]
        assert response.status_code == 200, f"status_code: {response.status_code}"

    @patch("app.os.path.isfile", return_value=False)
    def test_list_images_invalid_workflow_id(self, mock_is_file):
        response = self.client.get("/workflows/fake-workflow-id/images")
        logging.info(response)
        assert response.status_code == 404, f"status_code: {response.status_code}"
        assert "The server can't get the analysis results from the workflow fake-workflow-id. Error: 'Unable to find emagent config file: 'em-agent-fake-workflow-id.json''.  Check the error message and try again." \
               in response.json()['message']

    def test_list_images_invalid_sort(self):
        response = self.client.get('/workflows/fake-workflow-id/images?sort=abc')
        assert response.status_code == 400, f"status_code: {response.status_code}"
        assert "The server can't get the analysis results from the workflow. Error: 'Invalid sorting method provided: 'abc'. Valid sorting methods are 'desc' or 'asc''. Check the error message and try again" in response.json()['message']

    @patch("utils.server_setup.workflow_accessor.get_workflow_by_id", return_value={"workflowOutputPath": ""})
    def test_list_images_invalid_maxresults_startingpoint_letter(self, get_workflows_mock):
        response = self.client.get("/workflows/fake-workflow-id/images?maxResults=a&startingPoint=b")
        self.mock_get_inference_results_object.assert_not_called()
        get_workflows_mock.assert_not_called()
        
        assert response.status_code == 400, f"status_code: {response.status_code}"

    def test_list_images_invalid_maxresults_exceeded(self):
        response = self.client.get("/workflows/fake-workflow-id/images?maxResults=100")
        assert response.status_code == 400, f"status_code: {response.status_code}"
        assert f"The server can't get the analysis results from the workflow. Error: 'Invalid input for maxResults: '100', valid values are (0, 2]'. Check the error message and try again." in response.json()['message']

    def test_list_images_invalid_startingpoint_negative(self):
        logging.basicConfig(level=logging.INFO)
        response = self.client.get("/workflows/fake-workflow-id/images?startingPoint=-1")
        assert response.status_code == 400, f"status_code: {response.status_code}"
        assert "The server can't get the analysis results from the workflow. Error: 'Invalid starting point value provided: '-1'', Expected non-negative integer. Check the error message and try again." in response.json()['message']
