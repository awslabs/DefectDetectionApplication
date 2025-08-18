#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
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
import edge_agent_pb2 as pb2
from edge_agent_pb2_grpc import EdgeAgentStub
from exceptions.api.grpc_exceptions import GrpcException

LFV_EDGE_AGENT_SOCKET = "unix:///tmp/aws.iot.lookoutvision.EdgeAgent.sock"

import logging
logger = logging.getLogger(__name__)

class LFVEdgeAgent:

    def __init__(self):
        self.grpc_client = self.get_lfv_grpc_client()


    def get_lfv_grpc_client(self):
        try:
            channel = grpc.insecure_channel(LFV_EDGE_AGENT_SOCKET)
            stub = EdgeAgentStub(channel)
            return stub
        except Exception as e:
            logger.error("Failed to create LFV edge agent stub..")
            raise e


    def list_models(self):
        models = []
        try:
            models_list_response = self.grpc_client.ListModels(pb2.ListModelsRequest())
            for model in models_list_response.models:
                __model_dict = {
                    "model_component" : model.model_component,
                    "status" : pb2.ModelStatus.Name(model.status)
                }
                models.append(__model_dict)
        except grpc.RpcError as ge:
            logger.error(f"Error code: {ge.code()}, Status: {ge.details()}")
            raise GrpcException(message=ge.details(), status_code=ge.code())
        except Exception as e:
            logger.error("Failed to list models")
            raise e
        return models


    def get_model_description(self, model_id):
        try:
            __request = pb2.DescribeModelRequest()
            __request.model_component = model_id
            response = self.grpc_client.DescribeModel(__request).model_description
            return {
                "model_component" : response.model_component,
                "model_lfv_arn" : response.lookout_vision_model_arn,
                "status" : pb2.ModelStatus.Name(response.status),
                "status_message" : response.status_message
            }
        except grpc.RpcError as ge:
            logger.error(f"Error code: {ge.code()}, Status: {ge.details()}")
            raise GrpcException(message=ge.details(), status_code=ge.code())
        except Exception as e:
            logger.error("Failed to describe model")
            raise e


    def start_model(self, model_id):
        try:
            __request = pb2.StartModelRequest()
            __request.model_component = model_id
            response = self.grpc_client.StartModel(__request)
            return pb2.ModelStatus.Name(response.status)
        except grpc.RpcError as ge:
            logger.error(f"Error code: {ge.code()}, Status: {ge.details()}")
            raise GrpcException(message=ge.details(), status_code=ge.code())
        except Exception as e:
            logger.error("Failed to stop model")
            raise e


    def stop_model(self, model_id):
        try:
            __request = pb2.StopModelRequest()
            __request.model_component = model_id
            response = self.grpc_client.StopModel(__request)
            return pb2.ModelStatus.Name(response.status)
        except grpc.RpcError as ge:
            logger.error(f"Error code: {ge.code()}, Status: {ge.details()}")
            raise GrpcException(message=ge.details(), status_code=ge.code())
        except Exception as e:
            logger.error("Failed to stop model")
            raise e
