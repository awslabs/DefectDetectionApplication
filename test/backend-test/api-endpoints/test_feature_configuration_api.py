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
from unittest.mock import patch, MagicMock
import os
import logging
from dda_triton.triton_edge_client import TritonEdgeClient
import unittest
logger = logging.getLogger(__name__)

class TestFeatureConfigurations(LocalServerBaseTestCase):

    @patch("utils.feature_configs_utils.start_model_lfv", return_value={"type": "LFVModel","modelName": "model-1", "status": "STARTING"})
    @patch("endpoints.feature_config.__get_triton_instance", return_value=None)
    @patch.dict(os.environ, {'is_triton':'False'})
    def test_happy_path_start_feature_config_lfv(self, mock_start_model_lfv, mock_get_triton_instance):
        response = self.client.get("/feature-configurations/models/model-1/start")
        assert response.status_code == 200, f"status_code: {response.status_code}"
        mock_start_model_lfv.assert_called_once()

    @patch("utils.feature_configs_utils.start_model_triton", return_value={"type": "TritonModel","modelName": "model-1", "status": "UNKNOWN"})
    @patch.dict(os.environ, {'is_triton':'True'})
    @patch("utils.get_is_triton.get_is_triton")
    @patch.object(TritonEdgeClient, 'get_instance')
    def test_happy_path_start_feature_config_triton(self, mock_start_model_triton , mock_get_is_triton , mock_get_instance):
        mock_get_instance = MagicMock()
        mock_get_is_triton.return_value = mock_get_instance
        response = self.client.get("/feature-configurations/models/model-1/start")
        assert response.status_code == 200, f"status_code: {response.status_code}"
        mock_start_model_triton.assert_called_once()

    #update the response strings
    @patch("utils.feature_configs_utils.stop_model_lfv", return_value={"type": "LFVModel","modelName": "model-1", "status": "STOPPED"})
    @patch("endpoints.feature_config.__get_triton_instance", return_value=None)
    def test_happy_path_stop_feature_config_lfv(self, mock_stop_model_lfv, mock_get_triton_instance):
        response = self.client.get("/feature-configurations/models/model-1/stop")
        assert response.status_code == 200, f"status_code: {response.status_code}"
        mock_stop_model_lfv.assert_called_once()

    @patch("utils.feature_configs_utils.stop_model_triton", return_value={"type": "TritonModel","modelName": "model-1", "status": "UNAVAILABLE"})
    @patch.dict(os.environ, {'is_triton':'True'})
    @patch("utils.get_is_triton.get_is_triton")
    @patch.object(TritonEdgeClient, 'get_instance')
    def test_happy_path_stop_feature_config_triton(self, mock_stop_model_triton, mock_get_is_triton, mock_get_instance):
        mock_get_instance = MagicMock()
        mock_get_is_triton.return_value = mock_get_instance
        response = self.client.get("/feature-configurations/models/model-1/stop")
        assert response.status_code == 200, f"status_code: {response.status_code}"
        mock_stop_model_triton.assert_called_once()

    @patch("utils.feature_configs_utils.start_model_lfv", return_value={"type": "LFVModel","modelName": "model-1", "status": "STARTING"})
    @patch("endpoints.feature_config.__get_triton_instance", return_value=None)
    def test_invalid_model_name_start_feature_config_lfv(self, mock_start_model_lfv, mock_get_triton_instance):
        response = self.client.get("/feature-configurations/models/wrong-model-name/start")
        assert response.status_code == 400, f"status_code: {response.status_code}"
    
    @patch("utils.feature_configs_utils.start_model_triton", return_value={"type": "TritonModel","modelName": "model-1", "status": "READY"})
    @patch("utils.get_is_triton.get_is_triton")
    @patch.dict(os.environ, {'is_triton':'True'})
    @patch.object(TritonEdgeClient, 'get_instance')
    def test_invalid_model_name_start_feature_config_triton(self, mock_start_model_triton, mock_get_is_triton, mock_get_instance):
        mock_get_instance = MagicMock()
        mock_get_is_triton.return_value = mock_get_instance
        response = self.client.get("/feature-configurations/models/wrong-model-name/start")
        assert response.status_code == 400, f"status_code: {response.status_code}"
    
    @patch("utils.feature_configs_utils.stop_model_lfv", return_value={"type": "LFVModel","modelName": "model-1", "status": "STOPPED"})
    @patch("endpoints.feature_config.__get_triton_instance", return_value=None)
    def test_invalid_model_name_stop_feature_config_lfv(self, mock_stop_model_lfv, mock_get_triton_instance):
        response = self.client.get("/feature-configurations/models/wrong-model-name/stop")
        assert response.status_code == 400, f"status_code: {response.status_code}"

    @patch("utils.feature_configs_utils.stop_model_triton", return_value={"type": "TritonModel","modelName": "model-1", "status": "UNAVAILABLE"})
    @patch("utils.get_is_triton.get_is_triton")
    @patch.dict(os.environ, {'is_triton':'True'})
    @patch.object(TritonEdgeClient, 'get_instance')
    def test_invalid_model_name_stop_feature_config_triton(self, mock_stop_model_triton, mock_get_is_triton, mock_get_instance):
        mock_get_instance = MagicMock()
        mock_get_is_triton.return_value = mock_get_instance
        response = self.client.get("/feature-configurations/models/wrong-model-name/stop")
        assert response.status_code == 400, f"status_code: {response.status_code}"