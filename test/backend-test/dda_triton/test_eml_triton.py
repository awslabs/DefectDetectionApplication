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
import sys
import zipfile
import platform
import dda_triton.model_convertor as model_convertor
import dda_triton.model_config_pb2 as tmc
import shutil
from google.protobuf import text_format
from panorama import mlops
from panorama import buffer
import numpy as np
import json
import time


def test_model_convertor():
    machine = platform.machine()
    model_name = "test_model"
    working_dir = os.path.dirname(os.path.realpath(__file__))
    model_repository = os.path.abspath(os.path.join(working_dir, "test_model_repository"))
    extract_location = os.path.abspath(os.path.join(working_dir, "test_model_extracted_location"))
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
    assert os.path.exists(os.path.join(model_repository, f"base_{model_name}"))
    pbtxt_path = os.path.join(model_repository, f"base_{model_name}", "config.pbtxt")
    assert os.path.exists(pbtxt_path)
    txt = None
    with open(pbtxt_path) as f:
        txt = f.read()
    model_config = text_format.Parse(txt, tmc.ModelConfig())
    assert model_config.name == f"base_{model_name}"
    assert model_config.backend == "python"
    assert len(model_config.output) == 5
    assert len(model_config.input) == 1
    # check marshal pbtxt
    pbtxt_path = os.path.join(model_repository, f"marshal_{model_name}", "config.pbtxt")
    assert os.path.exists(pbtxt_path)
    txt = None
    with open(pbtxt_path) as f:
        txt = f.read()
    model_config = text_format.Parse(txt, tmc.ModelConfig())
    assert model_config.name == f"marshal_{model_name}"
    assert model_config.backend == "python"
    assert len(model_config.output) == 5
    assert len(model_config.input) == 7
    # check ensemble pbtxt
    pbtxt_path = os.path.join(model_repository, f"{model_name}", "config.pbtxt")
    assert os.path.exists(pbtxt_path)
    txt = None
    with open(pbtxt_path) as f:
        txt = f.read()
    model_config = text_format.Parse(txt, tmc.ModelConfig())
    assert model_config.name == f"{model_name}"
    assert model_config.platform == "ensemble"
    assert len(model_config.output) == 5
    assert len(model_config.input) == 2
    # sub_folder checks
    sub_folder = os.path.join(model_repository, f"base_{model_name}", "1")
    assert os.path.exists(sub_folder)
    sub_files = os.listdir(sub_folder)
    assert "manifest.json" in sub_files
    assert "model.py" in sub_files
    for f in sub_files:
        # Model.py is an actual file, rest are symlinks
        if f == "model.py":
            assert not os.path.islink(os.path.join(sub_folder, f))
        else:
            assert os.path.islink(os.path.join(sub_folder, f))
    # sub_folder for marshal
    sub_folder = os.path.join(model_repository, f"marshal_{model_name}", "1")
    assert os.path.exists(sub_folder)
    sub_files = os.listdir(sub_folder)
    assert "manifest.json" not in sub_files
    assert "model.py" in sub_files
    # sub folder for ensemble
    sub_folder = os.path.join(model_repository, f"{model_name}", "1")
    assert os.path.exists(sub_folder)
    sub_files = os.listdir(sub_folder)
    assert "manifest.json" not in sub_files
    assert "model.py" not in sub_files
    assert "ensemble_model" in sub_files
    shutil.rmtree(model_repository)
    shutil.rmtree(extract_location)
    # Some negative tests.
    bad_model_repository = os.path.abspath(
        os.path.join(working_dir, "test_model_repository_no_exist")
    )
    ret = model_convertor.convert_to_triton_structure(
        bad_model_repository, extract_location, model_name
    )
    assert not ret
    bad_extract_location = os.path.abspath(
        os.path.join(working_dir, "test_model_extracted_location_no_exist")
    )
    ret = model_convertor.convert_to_triton_structure(
        model_repository, bad_extract_location, model_name
    )
    assert not ret
    ret = model_convertor.convert_to_triton_structure(model_repository, extract_location, "")
    assert not ret


def test_converted_model_with_triton():
    machine = platform.machine()
    model_name = "test_model"
    working_dir = os.path.dirname(os.path.realpath(__file__))
    model_repository = os.path.abspath(os.path.join(working_dir, "test_model_repository"))
    extract_location = os.path.abspath(os.path.join(working_dir, "test_model_extracted_location"))
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
    server = mlops.create_triton_inference_server(
        model_repository, os.environ["TRITON_INSTALL_DIR"]
    )
    assert server is not None

    sub_folder = os.path.join(model_repository, f"base_{model_name}", "1")
    shutil.copy(
        os.path.join(working_dir, "test_mock_model", "mock_model_template.py"),
        os.path.join(sub_folder, "model.py"),
    )
    assert server.list_models()
    models_list = json.loads(server.list_models())
    assert len(models_list.keys()) == 3
    assert f"base_{model_name}" in models_list
    assert "state" in models_list[f"base_{model_name}"]
    assert models_list[f"base_{model_name}"]["state"] == "UNKNOWN"
    # Check loading model, loading the model runs 3 predictions as warm-up so should check that internally as well
    server.load_model(f"base_{model_name}")
    # Wait for model to be ready
    while server.get_model_status(f"base_{model_name}") != "READY":
        assert server.get_model_status(f"base_{model_name}") == "LOADING"
        time.sleep(0.2)
    # get metadata of loaded model
    metadata_string = server.model_metadata(f"base_{model_name}")
    assert metadata_string
    metadata = json.loads(metadata_string)
    assert metadata["name"] == f"base_{model_name}"
    assert "inputs" in metadata
    assert len(metadata["inputs"]) == 1
    assert "outputs" in metadata
    assert len(metadata["outputs"]) == 5
    assert metadata["platform"] == "python"

    assert metadata["state"] == "READY"
    # Create the request
    request = mlops.create_triton_request(server, f"base_{model_name}")
    input_idx = request.get_input_tensor_index("input")
    input = request.get_input(input_idx)
    assert input.abstract()
    input_shape = [768,576,3]

    # Giving a random input in expected shape
    sample = np.random.rand(*input_shape).astype(np.uint8)
    assert request.get_input(input_idx).data_type() == np.uint8
    input_buffer = buffer.create(int(np.prod(input_shape)))
    assert input_buffer
    inp_tensor = mlops.create_tensor("input", input_shape, np.uint8, input_buffer)
    arr = inp_tensor.array()
    arr[:] = sample
    request.set_input(input_idx, inp_tensor)
    # Process the request
    server.process_request(request)
    # Check outputs
    output_idx = request.get_output_tensor_index("mask")
    mask = request.get_output(output_idx).array()
    assert mask.dtype == np.uint8
    output_idx = request.get_output_tensor_index("anomalies")
    anomalies = request.get_output(output_idx).array()
    assert anomalies.dtype == np.uint8
    anomalies = anomalies.view(f"S{anomalies.shape[0]}").astype("U")
    # should be empty json. since test model has no anomalies
    assert not json.loads(anomalies[0])
    output_idx = request.get_output_tensor_index("output")
    output = request.get_output(output_idx).array()
    assert output.dtype == np.uint8
    output_idx = request.get_output_tensor_index("output_confidence")
    confidence = request.get_output(output_idx).array()
    assert confidence.dtype == np.float32
    output_idx = request.get_output_tensor_index("output_score")
    score = request.get_output(output_idx).array()
    assert score.dtype == np.float32
    server.load_model(f"marshal_{model_name}")
    # Wait for model to be ready
    while server.get_model_status(f"marshal_{model_name}") != "READY":
        assert server.get_model_status(f"marshal_{model_name}") == "LOADING"
        time.sleep(0.2)
    # get metadata of loaded model
    metadata_string = server.model_metadata(f"marshal_{model_name}")
    assert metadata_string
    metadata = json.loads(metadata_string)
    assert metadata["name"] == f"marshal_{model_name}"
    assert "inputs" in metadata
    assert len(metadata["inputs"]) == 7
    assert "outputs" in metadata
    assert len(metadata["outputs"]) == 5
    assert metadata["platform"] == "python"
    assert metadata["state"] == "READY"
    # request for marshal model
    request_marshal = mlops.create_triton_request(server, f"marshal_{model_name}")
    input_idx = request_marshal.get_input_tensor_index("input")
    input_buffer = buffer.create(int(np.prod(input_shape)))
    inp_tensor = mlops.create_tensor("input", input_shape, np.uint8, input_buffer)
    request_marshal.set_input(input_idx, inp_tensor)
    input_idx = request_marshal.get_input_tensor_index("inference_mask")
    mask_buffer = buffer.create(int(np.prod(input_shape)))
    mask_tensor = mlops.create_tensor("inference_mask", input_shape, np.uint8, mask_buffer)
    arr = mask_tensor.array()
    arr[:] = mask
    request_marshal.set_input(input_idx, mask_tensor)
    input_idx = request_marshal.get_input_tensor_index("inference_confidence")
    input = request_marshal.get_input(input_idx).array()
    input[:] = confidence
    input_idx = request_marshal.get_input_tensor_index("inference_anomalies")
    anomalies_str = anomalies[0]
    an_data = buffer.create_from_string(anomalies_str)
    an_tensor = mlops.create_tensor("inference_anomalies", [an_data.size()], np.uint8, an_data)
    request_marshal.set_input(input_idx, an_tensor)
    input_idx = request_marshal.get_input_tensor_index("inference_output")
    input = request_marshal.get_input(input_idx).array()
    input[:] = output
    input_idx = request_marshal.get_input_tensor_index("inference_score")
    input = request_marshal.get_input(input_idx).array()
    input[:] = score
    meta_data = {
        "sagemaker_edge_core_capture_data_disk_path": working_dir,
        "capture_id": "test_capture",
        "model_name": model_name,
        "model_version": "1",
        "event_id": 0,
        "threshold": 0.60,
        "sagemaker_edge_core_device_fleet_name": "device_fleet",
    }
    meta_data = json.dumps(meta_data)
    meta_data = buffer.create_from_string(meta_data)
    input_idx = request_marshal.get_input_tensor_index("metadata")
    meta_tensor = mlops.create_tensor("metadata", [meta_data.size()], np.uint8, meta_data)
    request_marshal.set_input(input_idx, meta_tensor)
    # Process request
    server.process_request(request_marshal)
    # Unload model
    server.unload_model(f"base_{model_name}")
    # check model output from marshal
    output_idx = request_marshal.get_output_tensor_index("output")
    output = request_marshal.get_output(output_idx).array()
    assert output.dtype == np.uint8
    output = output.view(f"S{output.shape[0]}").astype("U")
    assert json.loads(output[0])
    server.unload_model(f"marshal_{model_name}")
    shutil.rmtree(model_repository)
    shutil.rmtree(extract_location)


def test_ensemble_model():
    machine = platform.machine()
    model_name = "test_model"
    working_dir = os.path.dirname(os.path.realpath(__file__))
    model_repository = os.path.abspath(os.path.join(working_dir, "test_model_repository"))
    extract_location = os.path.abspath(os.path.join(working_dir, "test_model_extracted_location"))
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
    server = mlops.create_triton_inference_server(
        model_repository, os.environ["TRITON_INSTALL_DIR"]
    )
    assert server is not None

    server.load_model(model_name)
    # Wait for model to be ready
    while server.get_model_status(model_name) != "READY":
        assert server.get_model_status(model_name) == "LOADING"
        time.sleep(0.2)
    # Base model and marshal should be loaded and ready to go after ensemble
    metadata_string = server.model_metadata(f"base_{model_name}")
    assert metadata_string
    metadata = json.loads(metadata_string)
    assert metadata["name"] == f"base_{model_name}"
    assert metadata["state"] == "READY"
    metadata_string = server.model_metadata(f"marshal_{model_name}")
    assert metadata_string
    metadata = json.loads(metadata_string)
    assert metadata["name"] == f"marshal_{model_name}"
    assert metadata["state"] == "READY"
    server.load_model(f"base_{model_name}")
    # Wait for model to be ready
    while server.get_model_status(f"base_{model_name}") != "READY":
        assert server.get_model_status(f"base_{model_name}") == "LOADING"
        time.sleep(0.2)
    metadata_string = server.model_metadata(f"base_{model_name}")
    assert metadata_string
    metadata = json.loads(metadata_string)
    # Create the request
    request = mlops.create_triton_request(server, model_name)
    input_idx = request.get_input_tensor_index("input")
    input = request.get_input(input_idx)
    assert input.abstract()
    input_shape = [768,576,3]
    # Giving a random input in expected shape
    sample = np.random.randn(*input_shape).astype(request.get_input(input_idx).data_type())
    input_buffer = buffer.create(int(np.prod(input_shape)))
    assert input_buffer
    inp_tensor = mlops.create_tensor("input", input_shape, np.uint8, input_buffer)
    arr = inp_tensor.array()
    arr[:] = sample
    request.set_input(input_idx, inp_tensor)
    # some variable repeated, to maintain compatability with eminfer.
    meta_data = {
        "sagemaker_edge_core_capture_data_disk_path": working_dir,
        "capture_id": "test_capture",
        "model_name": model_name,
        "model_version": "1",
        "event_id": 0,
        "threshold": 0.60,
        "sagemaker_edge_core_device_fleet_name": "device_fleet",
    }
    meta_data = json.dumps(meta_data)
    meta_data = buffer.create_from_string(meta_data)
    input_idx = request.get_input_tensor_index("METADATA")
    meta_tensor = mlops.create_tensor("METADATA", [meta_data.size()], np.uint8, meta_data)
    request.set_input(input_idx, meta_tensor)
    # Process the request
    server.process_request(request)
    # Validate output values.
    output_idx = request.get_output_tensor_index("output_mask")
    # empty buffer,overlay.
    output = request.get_output(output_idx).array()
    assert output is None
    output_idx = request.get_output_tensor_index("output_overlay")
    output = request.get_output(output_idx).array()
    assert output is None

    output_idx = request.get_output_tensor_index("output_capture")
    assert output_idx == 0
    output = request.get_output(output_idx).array()
    assert output.dtype == np.uint8
    output = output.view(f"S{output.shape[0]}").astype("U")
    assert json.loads(output[0])
    dat = json.loads(output[0])
    assert "deviceGroundTruthData" in dat
    output_idx = request.get_output_tensor_index("output_anomalous")
    output = request.get_output(output_idx).array()
    assert output.dtype == np.uint8
    assert output.shape[0] == 1
    output_idx = request.get_output_tensor_index("output_confidence")
    output = request.get_output(output_idx).array()
    assert output.dtype == np.float32
    assert output.shape[0] == 1
    server.unload_model(model_name)
    shutil.rmtree(model_repository)
    shutil.rmtree(extract_location)
