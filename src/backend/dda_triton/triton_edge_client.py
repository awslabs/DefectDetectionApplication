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

import logging

logger = logging.getLogger(__name__)
from panorama import mlops
import json
from dda_triton.constants import TRITON_MODEL_DIR, TRITON_INSTALLATION_DIR
import traceback


class TritonEdgeClient:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(TritonEdgeClient, cls).__new__(cls)
        return cls._instance

    @classmethod
    def get_instance(cls, model_dir=None, installation_dir=None):
        if cls._instance is None:
            cls._instance = TritonEdgeClient(model_dir, installation_dir)
        return cls._instance

    def __init__(self, model_dir=None, installation_dir=None):
        if hasattr(self, "triton_instance"):
            return
        self.triton_instance = None
        self.create_server(
            model_dir or TRITON_MODEL_DIR, installation_dir or TRITON_INSTALLATION_DIR
        )

    def create_server(self, model_dir, installation_dir):
        try:
            self.triton_instance = mlops.create_triton_inference_server(model_dir, installation_dir)
            logger.info("Triton server created")
        except Exception as e:
            logger.error("Traceback:")
            traceback.print_exc()
            logger.error(f"Failed to create triton_instance , error: {e}")
        return self.triton_instance

    def list_triton_models(self):
        models = []
        try:
            models_list_response = json.loads(self.triton_instance.list_models())
            logger.info(f"Triton models(including base and marshal): {models_list_response}")
            for model, state in models_list_response.items():
                __model_dict = {"model_component": model, "status": state.get("state", "UNKNOWN")}
                models.append(__model_dict)
            return models
        except Exception as e:
            logger.error("Failed to list Triton models")
            raise e

    def get_model_description(self, model_id):
        try:
            describe_reponse = json.loads(self.triton_instance.model_metadata(model_id))
            model_component = describe_reponse.get("name", " ")
            status = describe_reponse.get("state", " ")
            return {
                "model_component": model_component,
                "model_lfv_arn": "None",
                "status": status,
                "status_message": status,
            }
        except Exception as e:
            logger.error("Failed to describe model")
            raise e

    def start_triton_model(self, model_id):
        try:
            self.triton_instance.load_model(model_id)
            return self.get_model_status(model_id)
        except Exception as e:
            logger.error("Failed to start model")
            raise e

    def get_model_status(self, model_id: str) -> str:
        return self.triton_instance.get_model_status(model_id)

    def stop_triton_model(self, model_id):
        try:
            self.triton_instance.unload_model(model_id)
            return self.get_model_description(model_id)
        except Exception as e:
            logger.error(f"Failed to stop model due to exception:  {e}")
            raise e
