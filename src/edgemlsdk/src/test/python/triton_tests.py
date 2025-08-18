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

from panorama import buffer
from panorama import mlops

import json
import test_utils
import threading
import time
import os
import numpy as np

class TritonTests:
    def __init__(self):
        self.model_directory = f'{os.environ["BUILD_DIRECTORY"]}/bin/model_repo'
        self.triton_install = os.environ["TRITON_INSTALL_DIRECTORY"]
        self.server = mlops.create_triton_inference_server(self.model_directory, self.triton_install)
        self.expected_model_count = 6

    def __del__(self):
        mlops.release_triton_inference_servers()

    def test_interface(self):
        test_utils.Expect_Equal("70CE687C-477B-4760-82A5-7EDDD701E756", mlops.TritonInferenceServer.uuid())
        test_utils.Expect_Equal("A07DB75B-A4BA-445F-AC90-DE7219D969A1", mlops.TritonInferenceRequest.uuid())

        test_utils.Expect_Equal(None, self.server.query_interface(mlops.TritonInferenceRequest))
        test_utils.Expect_Equal(mlops.TritonInferenceServer, type(self.server.query_interface(mlops.TritonInferenceServer)))

    def test_inferencing(self):
        # Check meta data negative and postive cases.
        meta_data = self.server.model_metadata("doesn't_exist")
        test_utils.Expect_Equal(meta_data, None)
        self.server.load_model("test_model")
        test_utils.Expect_Equal(self.server.get_model_status("test_model"), "LOADING")
        # wait for mdoel to load.
        time.sleep(2)
        # More meta data and list model tests
        meta_data = self.server.model_metadata("test_model")
        test_utils.Expect_NotEqual(meta_data, None)
        list_models = self.server.list_models()
        test_utils.Expect_NotEqual(list_models, None)
        meta_json = json.loads(meta_data)
        list_json = json.loads(list_models)
        test_utils.Expect_True("name" in meta_json)
        test_utils.Expect_True("state" in meta_json)
        test_utils.Expect_True("platform" in meta_json)
        test_utils.Expect_True("inputs" in meta_json)
        test_utils.Expect_True("outputs" in meta_json)
        test_utils.Expect_Equal(meta_json["name"], "test_model")
        test_utils.Expect_Equal(meta_json["state"], "READY")
        test_utils.Expect_Equal(self.expected_model_count, len(list_json.keys())) # Number of models in the repo
        test_utils.Expect_Equal(list_json["test_model"]["name"], "test_model")
        test_utils.Expect_Equal(list_json["test_model"]["state"], "READY")

        request = mlops.create_triton_request(self.server, "test_model")

        # Get the input tensor
        idx = request.get_input_tensor_index("input_0")
        test_utils.Expect_Equal(idx, 0)
        tensor = request.get_input(idx)

        # Validate tensor metadata
        data = tensor.array()
        test_utils.Expect_Equal(data.shape[0], 1)
        test_utils.Expect_Equal(data.shape[1], 1024)
        test_utils.Expect_Equal(tensor.abstract(), False)
        test_utils.Expect_Equal(tensor.name(), "input_0")
        test_utils.Expect_Equal(tensor.abstract(), False)
        test_utils.Expect_Equal(tensor.data_type(), np.float32)

        for idx in range(data.shape[1]):
            data[0, idx] = (float)(idx)

        self.server.process_request(request)

        idx = request.get_output_tensor_index("output_0")
        test_utils.Expect_Equal(idx, 0)
        out_tensor = request.get_output(idx)
        out_arr = out_tensor.array()

        for idx in range(out_arr.shape[1]):
            test_utils.Expect_Equal(out_arr[0, idx], (float)(idx) / 2)
        # Get metrics from triton
        metrics_data = self.server.get_metrics()
        test_utils.Expect_NotEqual(metrics_data, None)
        self.server.unload_model("test_model")
        time.sleep(10)

        # After unload meta data and list model tests
        meta_data = self.server.model_metadata("test_model")
        test_utils.Expect_NotEqual(meta_data, None)
        list_models = self.server.list_models()
        test_utils.Expect_NotEqual(list_models, None)
        meta_json = json.loads(meta_data)
        list_json = json.loads(list_models)
        test_utils.Expect_True("name" in meta_json)
        test_utils.Expect_True("state" in meta_json)
        test_utils.Expect_True("platform" not in meta_json)
        test_utils.Expect_True("inputs" not in meta_json)
        test_utils.Expect_True("outputs" not in meta_json)
        test_utils.Expect_Equal(meta_json["name"], "test_model")
        test_utils.Expect_Equal(meta_json["state"], "UNLOADING")
        test_utils.Expect_Equal(self.expected_model_count, len(list_json.keys())) # Number of models in the repo
        test_utils.Expect_Equal(list_json["test_model"]["name"], "test_model")
        test_utils.Expect_Equal(list_json["test_model"]["state"], "UNLOADING")

    def test_dynamic_tensor(self):
        self.server.load_model("dynamic_model")
        # Wait for model to load.
        time.sleep(2)
        request = mlops.create_triton_request(self.server, "dynamic_model")

        # Get the input tensor
        idx = request.get_input_tensor_index("input_0")
        test_utils.Expect_Equal(idx, 0)
        tensor = request.get_input(idx)

        # Validate tensor metadata
        data = tensor.array()
        test_utils.Expect_Equal(data, None)
        test_utils.Expect_Equal(tensor.name(), "input_0")
        test_utils.Expect_Equal(tensor.abstract(), True)
        test_utils.Expect_Equal(tensor.data_type(), np.uint8)
        # Create an input tensor
        input_data = buffer.create_from_string("hello world")
        tensor = mlops.create_tensor("input_0", [input_data.size()], np.uint8, input_data)

        # Set the input as the newly created tensor
        request.set_input(idx, tensor)

        # Validate tensor metadata
        tensor = request.get_input(idx)
        data = tensor.array()
        test_utils.Expect_Equal(data.shape[0], 12)
        test_utils.Expect_Equal(tensor.name(), "input_0")
        test_utils.Expect_Equal(tensor.abstract(), False)
        test_utils.Expect_Equal(tensor.data_type(), np.uint8)

        self.server.process_request(request)

        # Validate output tensor metadata
        idx = request.get_output_tensor_index("output_0")
        test_utils.Expect_Equal(idx, 0)
        out_tensor = request.get_output(idx)

        test_utils.Expect_Equal(data.shape[0], 12)
        test_utils.Expect_Equal(out_tensor.name(), "output_0")
        test_utils.Expect_Equal(out_tensor.abstract(), False)
        test_utils.Expect_Equal(out_tensor.data_type(), np.uint8)
        out_arr = out_tensor.array()
        # check tensor.array() returned type.
        test_utils.Expect_Equal(out_arr.dtype, np.uint8)

        test_utils.Expect_Equal("hello world\0", out_arr.tobytes().decode('utf-8'))

        out_tensor1 = request.get_output(idx+1)
        test_utils.Expect_Equal("gdkkn vnqkc\0", out_tensor1.array().tobytes().decode('utf-8'))

        # Add a larger input
        input_data = buffer.create_from_string("a larger string than the first")
        tensor = mlops.create_tensor("input_0", [input_data.size()], np.uint8, input_data)
        request.set_input(idx, tensor)

         # Validate tensor metadata
        tensor = request.get_input(idx)
        data = tensor.array()
        test_utils.Expect_Equal(data.shape[0], 31)
        test_utils.Expect_Equal(tensor.name(), "input_0")
        test_utils.Expect_Equal(tensor.abstract(), False)
        test_utils.Expect_Equal(tensor.data_type(), np.uint8)

        self.server.process_request(request)

        out_tensor = request.get_output(idx)

        test_utils.Expect_Equal(data.shape[0], 31)
        test_utils.Expect_Equal(out_tensor.name(), "output_0")
        test_utils.Expect_Equal(out_tensor.abstract(), False)
        test_utils.Expect_Equal(out_tensor.data_type(), np.uint8)
        out_arr = out_tensor.array()
        test_utils.Expect_Equal("a larger string than the first\0", out_arr.tobytes().decode('utf-8'))
        test_utils.Expect_Equal("` k`qfdq rsqhmf sg`m sgd ehqrs\0", request.get_output(idx+1).array().tobytes().decode('utf-8'))
        # checking arbitary buffer input check
        input_buf = buffer.create(16)
        tensor = mlops.create_tensor("input_0", [16], np.uint8, input_buf)
        input_data = tensor.array()
        input_data[:] = np.ones([16], dtype=np.uint8)*70
        request.set_input(idx, tensor)
        tensor = request.get_input(idx)
        data = tensor.array()
        test_utils.Expect_Equal(data.shape[0], 16)
        test_utils.Expect_Equal(tensor.name(), "input_0")
        test_utils.Expect_Equal(tensor.abstract(), False)
        test_utils.Expect_Equal(tensor.data_type(), np.uint8)
        self.server.process_request(request)

        self.server.unload_model("dynamic_model")

        self.server.load_model("dynamic_model_2")
        time.sleep(2)
        request = mlops.create_triton_request(self.server, "dynamic_model_2")
        # Get the input tensor
        idx = request.get_input_tensor_index("input_0")
        test_utils.Expect_Equal(idx, 0)
        tensor = request.get_input(idx)

        # Validate tensor metadata
        data = tensor.array()
        test_utils.Expect_Equal(data, None)
        test_utils.Expect_Equal(tensor.name(), "input_0")
        test_utils.Expect_Equal(tensor.abstract(), True)
        test_utils.Expect_Equal(tensor.data_type(), np.uint8)
        dyn_shape = [512,512,3]
        input_buf = buffer.create(int(np.prod(dyn_shape)))
        tensor = mlops.create_tensor("input_0", dyn_shape, np.uint8, input_buf)
        input_data = tensor.array()
        input_data[:] = np.ones(dyn_shape, dtype=np.uint8)*70
        request.set_input(idx, tensor)
        tensor = request.get_input(idx)
        data = tensor.array()
        test_utils.Expect_Equal(data.shape[0], dyn_shape[0])
        test_utils.Expect_Equal(data.shape[1], dyn_shape[1])
        test_utils.Expect_Equal(data.shape[2], dyn_shape[2])
        test_utils.Expect_Equal(tensor.name(), "input_0")
        test_utils.Expect_Equal(tensor.abstract(), False)
        test_utils.Expect_Equal(tensor.data_type(), np.uint8)
        self.server.process_request(request)
        out_tensor = request.get_output(idx)
        test_utils.Expect_Equal(out_tensor.name(), "output_0")
        test_utils.Expect_Equal(out_tensor.abstract(), False)
        test_utils.Expect_Equal(out_tensor.data_type(), np.uint8)
        out_arr = out_tensor.array()
        test_utils.Expect_True(np.array_equal(out_arr,np.ones(dyn_shape, dtype=np.uint8)*70))
        self.server.unload_model("dynamic_model_2")

    def test_model_ensemble(self):
        self.server.load_model("ensemble_model")
        time.sleep(2)
        # check if all the models in ensemble are loaded
        listed_models = self.server.list_models()
        listed_models = json.loads(listed_models)
        test_utils.Expect_True("ensemble_model" in listed_models.keys())
        test_utils.Expect_True("test_model" in listed_models.keys())
        test_utils.Expect_True("test_model_dup" in listed_models.keys())
        test_model_meta = self.server.model_metadata("test_model")
        test_model_meta = json.loads(test_model_meta)
        test_model_dup_meta = self.server.model_metadata("test_model_dup")
        test_model_dup_meta = json.loads(test_model_dup_meta)
        # check if meta is loaded for nested models
        test_utils.Expect_Equal(test_model_meta["name"], "test_model")
        test_utils.Expect_Equal(test_model_dup_meta["name"], "test_model_dup")
        test_utils.Expect_True("inputs" in test_model_meta)
        test_utils.Expect_True("inputs" in test_model_dup_meta)
        test_utils.Expect_True("outputs" in test_model_meta)
        test_utils.Expect_Equal(test_model_meta["state"], "READY")
        test_utils.Expect_Equal(test_model_dup_meta["state"], "READY")
        request = mlops.create_triton_request(self.server, "ensemble_model")
        # Get input shape
        ensemble_model_meta = self.server.model_metadata("ensemble_model")
        ensemble_model_meta = json.loads(ensemble_model_meta)
        input_shape = ensemble_model_meta["inputs"][0]["shape"]
        output_shape = ensemble_model_meta["outputs"][0]["shape"]
        random_input = np.random.randn(*input_shape).astype(np.uint8)
        # Get the input tensor
        idx = request.get_input_tensor_index("input")
        test_utils.Expect_Equal(idx, 0)
        arr = request.get_input(idx).array()
        arr[:] = random_input
        # Check request inputs and outputs count
        assert request.get_number_of_input_tensors() == 1
        assert request.get_number_of_output_tensors() == 1

        self.server.process_request(request)
        output_idx = request.get_output_tensor_index("output")
        test_utils.Expect_Equal(output_idx, 0)
        output_arr = request.get_output(output_idx).array()
        test_utils.Expect_Equal(output_arr.shape, tuple(output_shape))

        self.server.unload_model("ensemble_model")
        # once unloaded only state should be available for sub models, no data on tensors.
        test_model_meta = self.server.model_metadata("test_model")
        test_model_meta = json.loads(test_model_meta)
        test_model_dup_meta = self.server.model_metadata("test_model_dup")
        test_model_dup_meta = json.loads(test_model_dup_meta)
        test_utils.Expect_NotEqual(test_model_meta["state"], "READY")
        test_utils.Expect_NotEqual(test_model_dup_meta["state"], "READY") # can be unavailable or unloading.
        test_utils.Expect_True("inputs" not in test_model_meta)
        test_utils.Expect_True("inputs" not in test_model_dup_meta)
        test_utils.Expect_True("outputs" not in test_model_meta)

def run_tests():
    tests = TritonTests()
    tests.test_interface()
    tests.test_inferencing()
    tests.test_dynamic_tensor()
    tests.test_model_ensemble()

run_tests()
