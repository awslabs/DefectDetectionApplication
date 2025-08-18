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
import grpc
from fastapi import HTTPException
import utils.feature_configs_utils as feature_utils
from unittest.mock import patch, MagicMock
from mock_edge_agent_pb2_grpc import edge_agent_pb2_grpc_module
from local_server_base_test_case import LocalServerBaseTestCase
from exceptions.api.grpc_exceptions import GrpcException
import logging
logger = logging.getLogger(__name__)
class TestFeatureConfigsUtils(LocalServerBaseTestCase):

    @patch('edge_agent_pb2_grpc.EdgeAgentStub', create=True)
    def test_get_features_lfv(self, mock_edge_agent_stub):
        from mock_edge_agent_pb2_grpc import edge_agent_pb2_grpc_module
        lfv_edge_agent = mock_edge_agent_stub.return_value
        lfv_edge_agent.list_models.return_value = [ 
                { "model_component" : "model-1", "status" : "RUNNING"},
                { "model_component" : "model-2", "status" : "STOPPED"},
            ]
        res = feature_utils.get_features_lfv(lfv_edge_agent)
        assert len(res) == 2
        assert res[0].type == "LFVModel" and res[0].modelName == "model-1" and res[0].status == "RUNNING"
        assert res[1].type == "LFVModel" and res[1].modelName == "model-2" and res[1].status == "STOPPED"


    @patch('edge_agent_pb2_grpc.EdgeAgentStub', create=True)
    def test_happy_path_start_model_lfv(self, mock_edge_agent_stub):
        from mock_edge_agent_pb2_grpc import edge_agent_pb2_grpc_module
        lfv_edge_agent = mock_edge_agent_stub.return_value
        lfv_edge_agent.get_model_description.return_value = { "model_component" : "model-1", "status" : "STOPPED"}
        lfv_edge_agent.start_model.return_value = "STARTING"
        res = feature_utils.start_model_lfv(lfv_edge_agent, "model-1")
        assert res.model_dump() == { "type": "LFVModel", "modelName": "model-1", "status": "STARTING" }


    @patch('edge_agent_pb2_grpc.EdgeAgentStub', create=True)
    def test_happy_path_stop_model_lfv(self, mock_edge_agent_stub):
        from mock_edge_agent_pb2_grpc import edge_agent_pb2_grpc_module
        lfv_edge_agent = mock_edge_agent_stub.return_value
        lfv_edge_agent.get_model_description.return_value = { "model_component" : "model-1", "status" : "RUNNING"}
        lfv_edge_agent.stop_model.return_value = "STOPPED"
        res = feature_utils.stop_model_lfv(lfv_edge_agent, "model-1")
        assert res.model_dump() == { "type": "LFVModel", "modelName": "model-1", "status": "STOPPED" }


    @patch('edge_agent_pb2_grpc.EdgeAgentStub', create=True)
    def test_incorrect_start_model_usage_lfv(self, mock_edge_agent_stub):
        from mock_edge_agent_pb2_grpc import edge_agent_pb2_grpc_module
        lfv_edge_agent = mock_edge_agent_stub.return_value
        lfv_edge_agent.get_model_description.return_value = { "model_component" : "model-1", "status" : "RUNNING"}
        with self.assertRaises(HTTPException) as err:
            feature_utils.start_model_lfv(lfv_edge_agent, "model-1")


    @patch('edge_agent_pb2_grpc.EdgeAgentStub', create=True)
    def test_incorrect_stop_model_usage_lfv(self, mock_edge_agent_stub):
        from mock_edge_agent_pb2_grpc import edge_agent_pb2_grpc_module
        lfv_edge_agent = mock_edge_agent_stub.return_value
        lfv_edge_agent.get_model_description.return_value = { "model_component" : "model-1", "status" : "STOPPING"}
        with self.assertRaises(HTTPException) as err:
            feature_utils.stop_model_lfv(lfv_edge_agent, "model-1")


    @patch('edge_agent_pb2_grpc.EdgeAgentStub', create=True)
    def test_invalid_start_model_lfv(self, mock_edge_agent_stub):
        from mock_edge_agent_pb2_grpc import edge_agent_pb2_grpc_module
        lfv_edge_agent = mock_edge_agent_stub.return_value
        # grpc.StatusCode.NOT_FOUND
        lfv_edge_agent.get_model_description.side_effect = GrpcException(message="test", status_code=grpc.StatusCode.NOT_FOUND)
        with self.assertRaises(GrpcException) as err:
            feature_utils.start_model_lfv(lfv_edge_agent, "model-1")
        # grpc.StatusCode.INTERNAL
        lfv_edge_agent.get_model_description.side_effect = GrpcException(message="test", status_code=grpc.StatusCode.INTERNAL)
        with self.assertRaises(GrpcException) as err:
            feature_utils.start_model_lfv(lfv_edge_agent, "model-1")
        # grpc.StatusCode.UNKNOWN
        lfv_edge_agent.get_model_description.return_value = { "model_component" : "model-1", "status" : "STOPPED"}
        lfv_edge_agent.start_model.side_effect = GrpcException(message="test", status_code=grpc.StatusCode.UNKNOWN)
        with self.assertRaises(GrpcException) as err:
            feature_utils.start_model_lfv(lfv_edge_agent, "model-1")

    @patch('edge_agent_pb2_grpc.EdgeAgentStub', create=True)
    def test_invalid_stop_model_lfv(self, mock_edge_agent_stub):
        from mock_edge_agent_pb2_grpc import edge_agent_pb2_grpc_module
        lfv_edge_agent = mock_edge_agent_stub.return_value 
        # grpc.StatusCode.NOT_FOUND
        lfv_edge_agent.get_model_description.side_effect = GrpcException(message="test", status_code=grpc.StatusCode.NOT_FOUND)
        with self.assertRaises(GrpcException) as err:
            feature_utils.stop_model_lfv(lfv_edge_agent, "model-1")
        # grpc.StatusCode.INTERNAL
        lfv_edge_agent.get_model_description.side_effect = GrpcException(message="test", status_code=grpc.StatusCode.INTERNAL)
        with self.assertRaises(GrpcException) as err:
            feature_utils.stop_model_lfv(lfv_edge_agent, "model-1")
        # grpc.StatusCode.UNKNOWN
        lfv_edge_agent.get_model_description.return_value = { "model_component" : "model-1", "status" : "RUNNING"}
        lfv_edge_agent.stop_model.side_effect = GrpcException(message="test", status_code=grpc.StatusCode.UNKNOWN)
        with self.assertRaises(GrpcException) as err:
            feature_utils.stop_model_lfv(lfv_edge_agent, "model-1")

class TestTritonFeatureConfigsUtils(LocalServerBaseTestCase):
    def test_get_features_triton(self):
        mock_triton_server = MagicMock()
        mock_triton_server.list_triton_models.return_value = [
            {"model_component": "model1", "status": "READY"},
            {"model_component": "model2", "status": "UNAVAILABLE"},
        ]
        result = feature_utils.get_features_triton(mock_triton_server)
        assert len(result) == 2
        assert result[0].type == "TritonModel"
        assert result[0].modelName == "model1"
        assert result[0].status == "READY"
        assert result[1].type == "TritonModel"
        assert result[1].modelName == "model2"
        assert result[1].status == "UNAVAILABLE"

        logger.info(f"Result is {result}")

    def test_get_features_triton_no_server(self):
        with self.assertRaises(HTTPException) as e:
            feature_utils.get_features_triton(None)

    def test_start_model_triton(self):
        mock_triton_server = MagicMock()
        mock_triton_server.get_model_status.return_value = "UNKNOWN"
        mock_triton_server.start_triton_model.return_value = "LOADING"

        result = feature_utils.start_model_triton(mock_triton_server, "model1")
        assert result.type == "TritonModel"
        assert result.modelName == "model1"
        assert result.status == "LOADING"

    def test_start_model_triton_no_server(self):
        with self.assertRaises(HTTPException) as e:
            feature_utils.start_model_triton(None, "model1")

    def test_start_model_triton_invalid_status(self):
        mock_triton_server = MagicMock()
        mock_triton_server.get_model_status.return_value = "READY"
        with self.assertRaises(HTTPException) as exc:
            feature_utils.start_model_triton(mock_triton_server, "model1")
        self.assertEqual(exc.exception.status_code, 403)
        self.assertIn("Error while attempting to start model model1", str(exc.exception.detail))

    def test_stop_model_triton(self):
        mock_triton_server = MagicMock()
        mock_triton_server.get_model_description.return_value = {"status": "READY"}
        mock_triton_server.stop_triton_model.return_value = {"status": "STOPPED"}
        result = feature_utils.stop_model_triton(mock_triton_server, "model1")
        assert result.type == "TritonModel"
        assert result.modelName == "model1"
        assert result.status == "STOPPED"

    def test_stop_model_triton_no_server(self):
        with self.assertRaises(HTTPException) as e:
            feature_utils.stop_model_triton(None, "model1")