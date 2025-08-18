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

import os
import zipfile
import platform
import dda_triton.model_convertor as model_convertor
import shutil
from dda_triton.triton_edge_client import TritonEdgeClient
import logging
logger = logging.getLogger(__name__)
import unittest
import time

def test_converted_model_with_triton_dda():
    testcase = unittest.TestCase()
    machine = platform.machine()
    model_name = "test_model"
    working_dir = os.path.dirname(os.path.realpath(__file__))
    model_repository = os.path.abspath(os.path.join(working_dir, "test_model_repository_2"))
    extract_location = os.path.abspath(os.path.join(working_dir, "test_model_extracted_location_2"))
    #cleanup before start
    if os.path.exists(model_repository) and os.listdir(model_repository):
        shutil.rmtree(model_repository)
    if os.path.exists(extract_location) and os.listdir(extract_location):
        # cleanup before start
        shutil.rmtree(extract_location)
    
    try:
        os.makedirs(model_repository, exist_ok=True)
        os.makedirs(extract_location, exist_ok=True)
        with zipfile.ZipFile(
            os.path.join(working_dir, "test-artifacts", "models", f"{machine}_cpu_model.zip")
        ) as z:
            z.extractall(extract_location)
    except OSError as e:
        print(f"Error: {e}")
    ret = model_convertor.convert_to_triton_structure(
        model_repository, extract_location, model_name
    )
    assert ret
    assert os.environ["TRITON_INSTALL_DIR"]
    server = None
    server = TritonEdgeClient(
        model_repository, os.environ["TRITON_INSTALL_DIR"]
    )
    assert server is not None
   
    time.sleep(10)
    #1. List Triton models
    assert server.list_triton_models()
    models_list_response = server.list_triton_models()
    logger.info(f"models_list: {models_list_response}")
    expected_list_models = [{'model_component': 'base_test_model', 'status': 'UNKNOWN'}, {'model_component': 'marshal_test_model', 'status': 'UNKNOWN'}, {'model_component': 'test_model', 'status': 'UNKNOWN'}]
    testcase.assertEqual(models_list_response, expected_list_models)
    
    #2. Describe model
    assert server.get_model_description(f"base_{model_name}")
    model_describe_response = server.get_model_description(f"base_{model_name}")
    logger.info(f"model_describe_from_trtion_test: {model_describe_response}")
    expected_model_describe_response={'model_component': 'base_test_model', 'model_lfv_arn': 'None', 'status': 'UNKNOWN', 'status_message': 'UNKNOWN'}
    testcase.assertEqual(model_describe_response, expected_model_describe_response)
    # # Check loading model, loading the model runs 3 predictions as warm-up so should check that internally as well
    model_describe_response = server.get_model_description(f"base_{model_name}")
    logger.info(f"model_describe: {model_describe_response}")
    
    #3. Start/Load model
    assert server.start_triton_model(f"base_{model_name}")
    response_start = server.start_triton_model(f"base_{model_name}")
    logger.info(f"response_start: {response_start}")
    # Wait for model to be ready
    while server.get_model_status(f"base_{model_name}") != "READY":
        assert server.get_model_status(f"base_{model_name}") == "LOADING"
        time.sleep(0.2)

    #4. Stop/Unload model
    assert server.stop_triton_model(f"base_{model_name}")
    stop_model_repsonse=server.stop_triton_model(f"base_{model_name}")
    expected_stop_model_response = {'model_component': 'base_test_model', 'model_lfv_arn': 'None', 'status': 'UNLOADING', 'status_message': 'UNLOADING'}
    testcase.assertEqual(stop_model_repsonse, expected_stop_model_response)

    shutil.rmtree(model_repository)
    shutil.rmtree(extract_location)