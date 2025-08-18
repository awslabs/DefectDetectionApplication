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

import json
import numpy as np
import triton_python_backend_utils as pb_utils

class TritonPythonModel:
    """Your Python model must use the same class name. Every Python model
    that is created must have "TritonPythonModel" as the class name.
    """

    def initialize(self, args):
        # You must parse model_config. JSON string is not parsed here
        self.model_config = model_config = json.loads(args["model_config"])

        # Get the output configurations
        self.output_data_types = []
        for i in range(len(self.model_config["output"])):
            layer = f'output_{i}'
            config = pb_utils.get_output_config_by_name(self.model_config, layer)
            self.output_data_types.append(
                pb_utils.triton_string_to_numpy(config["data_type"]))

    def execute(self, requests):
        responses = []

        for request in requests:
            input_tensor = pb_utils.get_input_tensor_by_name(request, "input_0")
            input = input_tensor.as_numpy()
            # Pass through this is a dimensions test model.
            output_tensor0 = pb_utils.Tensor("output_0", input.astype(self.output_data_types[0]))
            inference_response = pb_utils.InferenceResponse(
                output_tensors=[output_tensor0]
            )

            responses.append(inference_response)

        return responses

    def finalize(self):
        pass