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
import os
import shutil
import json
import dda_triton.model_config_pb2 as triton_model_config
from dda_triton.constants import *
from google.protobuf import text_format
import argparse
import sys
import requests
from dda_triton.model_autostart_utils import wait_for_server

parser = argparse.ArgumentParser(description=" Script converts model to Triton format")
parser.add_argument("--unarchived_model_path", help="Path where the model is unarchived")
parser.add_argument("--model_version", help="Model version")
parser.add_argument("--model_name", help="Model name")

logging.basicConfig(
    format="%(levelname)s:%(message)s",
    level=logging.DEBUG,
    handlers=[logging.StreamHandler(sys.stdout)],
)


def start_model(model_name):
    if wait_for_server("localhost", 5000, "StartModel"):
        logging.info("localserver:5000 is reachable, sending start model request")
        url = f"http://localhost:5000/feature-configurations/models/{model_name}/start"
        headers = {"accept": "application/json"}
        response = requests.get(url, headers=headers)

        if response.status_code == 200:
            logging.info("Model started successfully!")
        else:
            logging.error(f"StartModel: Request failed with status code: {response.status_code}")
            logging.error(response.text)
    else:
        logging.info("StartModel: localserver:5000 is not reachable")


def clean_directory(path: str) -> bool:
    """
    Clean the directory by removing all files and subdirectories.

    Args:
        path (str): The path to the directory to be cleaned.

    Returns:
        bool: True if the directory is cleaned successfully, False otherwise.
    """
    try:
        shutil.rmtree(path)
        return True
    except OSError as e:
        logging.error(f"Unable to clean directory '{path}' error {str(e)}")
        return False


def create_sym_links(src_dir: str, dst_dir: str) -> bool:
    """
    Create symbolic links for all files in the source directory to the destination directory.

    Args:
        src_dir (str): The path to the source directory.
        dst_dir (str): The path to the destination directory.

    Returns:
        bool: True if the symbolic links are created successfully, False otherwise.
    """
    try:
        for file in os.listdir(src_dir):
            src_file_path = os.path.join(src_dir, file)
            dst_file_path = os.path.join(dst_dir, file)
            if os.path.isfile(src_file_path):
                os.symlink(src_file_path, dst_file_path)
            elif os.path.isdir(src_file_path):
                os.symlink(src_file_path, dst_file_path, target_is_directory=True)
        return True
    except OSError as e:
        logging.error(
            f"Unable to create symbolic links from '{src_dir}' to '{dst_dir}' error {str(e)}"
        )
        return False


def _create_base_model_config_pbtxt(model_name: str, input_shape: list) -> str:
    global triton_model_config
    base_model_input = triton_model_config.ModelInput()
    base_model_input.name = "input"
    base_model_input.data_type = triton_model_config.DataType.TYPE_UINT8
    base_model_input.dims.extend(input_shape)
    # Output information based on lfv model template backend file.
    base_model_output_anomaly = triton_model_config.ModelOutput()
    base_model_output_anomaly.name = "output"
    base_model_output_anomaly.data_type = triton_model_config.DataType.TYPE_UINT8
    base_model_output_anomaly.dims.extend([1])
    base_model_output_confidence = triton_model_config.ModelOutput()
    base_model_output_confidence.name = "output_confidence"
    base_model_output_confidence.data_type = triton_model_config.DataType.TYPE_FP32
    base_model_output_confidence.dims.extend([1])
    base_model_output_score = triton_model_config.ModelOutput()
    base_model_output_score.name = "output_score"
    base_model_output_score.data_type = triton_model_config.DataType.TYPE_FP32
    base_model_output_score.dims.extend([1])
    base_model_output_mask = triton_model_config.ModelOutput()
    base_model_output_mask.name = "mask"
    base_model_output_mask.data_type = triton_model_config.DataType.TYPE_UINT8
    # Mask shape is same as input shape.
    base_model_output_mask.dims.extend(input_shape)
    base_model_anomalies = triton_model_config.ModelOutput()
    base_model_anomalies.name = "anomalies"
    base_model_anomalies.data_type = triton_model_config.DataType.TYPE_UINT8
    base_model_anomalies.dims.extend([-1])
    # Create model config file to generate config.pbtxt
    base_model_config = triton_model_config.ModelConfig()
    base_model_config.name = f"base_{model_name}"
    base_model_config.backend = "python"
    base_model_config.max_batch_size = 0
    base_model_config.input.extend([base_model_input])
    base_model_config.output.extend(
        [
            base_model_output_anomaly,
            base_model_output_confidence,
            base_model_output_score,
            base_model_output_mask,
            base_model_anomalies,
        ]
    )
    return text_format.MessageToString(
        base_model_config, use_short_repeated_primitives=True, use_index_order=True
    )


# Used to see if model supports anomaly localization.
def _has_pixel_level_classes(manifest: dict) -> bool:
    graph_manifest = manifest.get(MODEL_GRAPH_MANIFEST_KEY, {})
    if not graph_manifest:
        return False
    pixel_level_classes = graph_manifest.get(PIXEL_LEVEL_CLASSES, {})
    if not pixel_level_classes:
        return False
    names = pixel_level_classes.get("names", [])
    return bool(len(names))


def _create_marshal_model_config_pbtxt(model_name: str, input_shape: list) -> str:
    global triton_model_config
    marshal_model_input = triton_model_config.ModelInput()
    marshal_model_input.name = "input"
    marshal_model_input.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_input.dims.extend(input_shape)
    marshal_model_inference_output = triton_model_config.ModelInput()
    marshal_model_inference_output.name = "inference_output"
    marshal_model_inference_output.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_inference_output.dims.extend([1])
    marshal_model_inference_mask = triton_model_config.ModelInput()
    marshal_model_inference_mask.name = "inference_mask"
    marshal_model_inference_mask.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_inference_mask.dims.extend(input_shape)
    marshal_model_inference_score = triton_model_config.ModelInput()
    marshal_model_inference_score.name = "inference_score"
    marshal_model_inference_score.data_type = triton_model_config.DataType.TYPE_FP32
    marshal_model_inference_score.dims.extend([1])
    marshal_model_inference_confidence = triton_model_config.ModelInput()
    marshal_model_inference_confidence.name = "inference_confidence"
    marshal_model_inference_confidence.data_type = triton_model_config.DataType.TYPE_FP32
    marshal_model_inference_confidence.dims.extend([1])
    marshal_model_inference_anomalies = triton_model_config.ModelInput()
    marshal_model_inference_anomalies.name = "inference_anomalies"
    marshal_model_inference_anomalies.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_inference_anomalies.dims.extend([-1])
    marshal_model_metadata = triton_model_config.ModelInput()
    marshal_model_metadata.name = "metadata"
    marshal_model_metadata.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_metadata.dims.extend([-1])
    # outputs
    marshal_model_output = triton_model_config.ModelOutput()
    marshal_model_output.name = "output"
    marshal_model_output.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_output.dims.extend([-1])
    marshal_model_output_mask = triton_model_config.ModelOutput()
    marshal_model_output_mask.name = "mask"
    marshal_model_output_mask.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_output_mask.dims.extend([-1])
    marshal_model_output_overlay = triton_model_config.ModelOutput()
    marshal_model_output_overlay.name = "overlay"
    marshal_model_output_overlay.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_output_overlay.dims.extend([-1])
    marshal_model_output_anomalous = triton_model_config.ModelOutput()
    marshal_model_output_anomalous.name = "output_anomalous"
    marshal_model_output_anomalous.data_type = triton_model_config.DataType.TYPE_UINT8
    marshal_model_output_anomalous.dims.extend([1])
    marshal_model_output_confidence = triton_model_config.ModelOutput()
    marshal_model_output_confidence.name = "output_confidence"
    marshal_model_output_confidence.data_type = triton_model_config.DataType.TYPE_FP32
    marshal_model_output_confidence.dims.extend([1])
    marshal_model_config = triton_model_config.ModelConfig()
    marshal_model_config.name = f"marshal_{model_name}"
    marshal_model_config.backend = "python"
    marshal_model_config.max_batch_size = 0
    marshal_model_config.input.extend(
        [
            marshal_model_input,
            marshal_model_inference_output,
            marshal_model_inference_mask,
            marshal_model_inference_confidence,
            marshal_model_inference_score,
            marshal_model_inference_anomalies,
            marshal_model_metadata,
        ]
    )
    marshal_model_config.output.extend(
        [
            marshal_model_output,
            marshal_model_output_mask,
            marshal_model_output_overlay,
            marshal_model_output_anomalous,
            marshal_model_output_confidence,
        ]
    )
    return text_format.MessageToString(
        marshal_model_config, use_short_repeated_primitives=True, use_index_order=True
    )


def _create_ensemble_model_config_pbtxt(model_name: str, input_shape: list) -> str:
    global triton_model_config
    ensemble_model_input = triton_model_config.ModelInput()
    ensemble_model_input.name = "input"
    ensemble_model_input.data_type = triton_model_config.DataType.TYPE_UINT8
    ensemble_model_input.dims.extend(input_shape)
    ensemble_model_input_metadata = triton_model_config.ModelInput()
    # used in emltriton.
    ensemble_model_input_metadata.name = "METADATA"
    ensemble_model_input_metadata.data_type = triton_model_config.DataType.TYPE_UINT8
    ensemble_model_input_metadata.dims.extend([-1])

    ensemble_model_output = triton_model_config.ModelOutput()
    ensemble_model_output.name = "output_overlay"
    ensemble_model_output.data_type = triton_model_config.DataType.TYPE_UINT8
    ensemble_model_output.dims.extend([-1])
    ensemble_model_output_mask = triton_model_config.ModelOutput()
    ensemble_model_output_mask.name = "output_mask"
    ensemble_model_output_mask.data_type = triton_model_config.DataType.TYPE_UINT8
    ensemble_model_output_mask.dims.extend([-1])
    ensemble_model_output_capture = triton_model_config.ModelOutput()
    ensemble_model_output_capture.name = "output_capture"
    ensemble_model_output_capture.data_type = triton_model_config.DataType.TYPE_UINT8
    ensemble_model_output_capture.dims.extend([-1])
    ensemble_model_output_anomalous = triton_model_config.ModelOutput()
    ensemble_model_output_anomalous.name = "output_anomalous"
    ensemble_model_output_anomalous.data_type = triton_model_config.DataType.TYPE_UINT8
    ensemble_model_output_anomalous.dims.extend([1])
    ensemble_model_output_confidence = triton_model_config.ModelOutput()
    ensemble_model_output_confidence.name = "output_confidence"
    ensemble_model_output_confidence.data_type = triton_model_config.DataType.TYPE_FP32
    ensemble_model_output_confidence.dims.extend([1])
    ensemble_model_config = triton_model_config.ModelConfig()
    ensemble_model_config.name = model_name
    ensemble_model_config.platform = "ensemble"
    ensemble_model_config.max_batch_size = 0
    ensemble_model_config.input.extend([ensemble_model_input, ensemble_model_input_metadata])
    ensemble_model_config.output.extend(
        [
            ensemble_model_output_capture,
            ensemble_model_output_mask,
            ensemble_model_output,
            ensemble_model_output_anomalous,
            ensemble_model_output_confidence,
        ]
    )
    # steps
    ensemble_model_step_1 = triton_model_config.ModelEnsembling.Step()
    ensemble_model_step_1.model_name = f"base_{model_name}"
    # -1 indicates latest
    ensemble_model_step_1.model_version = -1
    ensemble_model_step_1.input_map["input"] = "input"
    ensemble_model_step_1.output_map["output"] = "inference_output"

    ensemble_model_step_1.output_map["mask"] = "inference_mask"

    ensemble_model_step_1.output_map["anomalies"] = "inference_anomalies"
    ensemble_model_step_1.output_map["output_score"] = "inference_score"
    ensemble_model_step_1.output_map["output_confidence"] = "inference_confidence"

    ensemble_model_step_2 = triton_model_config.ModelEnsembling.Step()
    ensemble_model_step_2.model_name = f"marshal_{model_name}"
    # -1 indicates latest
    ensemble_model_step_2.model_version = -1
    ensemble_model_step_2.input_map["input"] = "input"
    ensemble_model_step_2.input_map["inference_output"] = "inference_output"
    ensemble_model_step_2.input_map["inference_mask"] = "inference_mask"
    ensemble_model_step_2.input_map["inference_anomalies"] = "inference_anomalies"
    ensemble_model_step_2.input_map["inference_confidence"] = "inference_confidence"
    ensemble_model_step_2.input_map["inference_score"] = "inference_score"

    ensemble_model_step_2.input_map["metadata"] = "METADATA"
    ensemble_model_step_2.output_map["output"] = "output_capture"
    ensemble_model_step_2.output_map["mask"] = "output_mask"
    ensemble_model_step_2.output_map["overlay"] = "output_overlay"
    ensemble_model_step_2.output_map["output_anomalous"] = "output_anomalous"
    ensemble_model_step_2.output_map["output_confidence"] = "output_confidence"
    ensemble_model_ensemble = triton_model_config.ModelEnsembling()
    ensemble_model_ensemble.step.extend([ensemble_model_step_1, ensemble_model_step_2])
    ensemble_model_config.ensemble_scheduling.CopyFrom(ensemble_model_ensemble)
    return text_format.MessageToString(
        ensemble_model_config, use_short_repeated_primitives=True, use_index_order=True
    )


def _create_base_model_structure(
    model_repo_dir: str,
    deployed_model_path: str,
    model_name: str,
    model_version: str,
    manifest: dict,
) -> bool:
    # Base lfv model create config.pbtxt.
    # Use dataset information.
    model_internal = manifest["dataset"]
    # Dynamic input if model does not support anomaly localization.
    input_shape = [-1, -1, -1]
    if _has_pixel_level_classes(manifest):
        input_shape = [model_internal["image_height"], model_internal["image_width"], 3]
    base_model_config_pbtxt = _create_base_model_config_pbtxt(model_name, input_shape)
    # create directories
    base_model_path = os.path.join(model_repo_dir, f"base_{model_name}")
    # clear existing path
    if os.path.exists(base_model_path) and os.path.isdir(base_model_path):
        ret = clean_directory(base_model_path)
        if not ret:
            logging.error(f"Unable to clean directory '{base_model_path}'")
            return False
    try:
        os.makedirs(base_model_path)
        os.makedirs(os.path.join(base_model_path, model_version))
    except OSError as e:
        logging.error(f"Unable to create directory '{base_model_path}' error {str(e)}")
        return False
    # Write config.pbtxt file first.
    try:
        with open(os.path.join(base_model_path, "config.pbtxt"), "w", encoding="utf-8") as f:
            f.write(base_model_config_pbtxt)
    except OSError as e:
        logging.error(f"Unable to write config.pbtxt file error {str(e)}")
        return False
    # Get working directory of this code to get the model.py file
    working_dir = os.path.dirname(os.path.realpath(__file__))
    model_py_path = os.path.abspath(
        os.path.join(working_dir, "resources_for_copy", "lfv_model_template.py")
    )
    if not os.path.exists(model_py_path):
        logging.error(f"Unable to find lfv_model_template.py file at '{model_py_path}'")
        return False
    try:
        shutil.copy(model_py_path, os.path.join(base_model_path, model_version, "model.py"))
    except OSError as e:
        logging.error(f"Unable to copy model.py file error {str(e)}")
        return False
    # Create symlinks for model folders
    ret = create_sym_links(deployed_model_path, os.path.join(base_model_path, model_version))
    if not ret:
        logging.error(f"Unable to create symbolic links for model folders")
        return False
    return True


def _create_marshal_model_structure(
    model_repo_dir: str, model_name: str, model_version: str, manifest: dict
) -> bool:
    model_internal = manifest["dataset"]
    input_shape = [-1, -1, -1]
    if _has_pixel_level_classes(manifest):
        input_shape = [model_internal["image_height"], model_internal["image_width"], 3]
    marshal_model_config_pbtxt = _create_marshal_model_config_pbtxt(model_name, input_shape)
    # create directories
    marshal_model_path = os.path.join(model_repo_dir, f"marshal_{model_name}")
    # clear existing path
    if os.path.exists(marshal_model_path) and os.path.isdir(marshal_model_path):
        ret = clean_directory(marshal_model_path)
        if not ret:
            logging.error(f"Unable to clean directory '{marshal_model_path}'")
            return False
    try:
        os.makedirs(marshal_model_path)
        os.makedirs(os.path.join(marshal_model_path, model_version))
    except OSError as e:
        logging.error(f"Unable to create directory '{marshal_model_path}' error {str(e)}")
        return False
    # Write config.pbtxt file first.
    try:
        with open(os.path.join(marshal_model_path, "config.pbtxt"), "w", encoding="utf-8") as f:
            f.write(marshal_model_config_pbtxt)
    except OSError as e:
        logging.error(f"Unable to write config.pbtxt file error {str(e)}")
        return False
    # Get working directory of this code to get the model.py file
    working_dir = os.path.dirname(os.path.realpath(__file__))
    model_py_path = os.path.abspath(
        os.path.join(working_dir, "resources_for_copy", "marshal_for_capture_template.py")
    )
    if not os.path.exists(model_py_path):
        logging.error(f"Unable to find marshal_for_capture_template.py file at '{model_py_path}'")
        return False
    try:
        shutil.copy(model_py_path, os.path.join(marshal_model_path, model_version, "model.py"))
    except OSError as e:
        logging.error(f"Unable to copy model.py file error {str(e)}")
        return False
    return True


def _create_ensemble_model_structure(
    model_repo_dir: str, model_name: str, model_version: str, manifest: dict
) -> bool:
    model_internal = manifest["dataset"]
    input_shape = [-1, -1, -1]
    if _has_pixel_level_classes(manifest):
        input_shape = [model_internal["image_height"], model_internal["image_width"], 3]
    ensemble_model_config_pbtxt = _create_ensemble_model_config_pbtxt(model_name, input_shape)
    # create directories
    ensemble_model_path = os.path.join(model_repo_dir, model_name)
    # clear existing path
    if os.path.exists(ensemble_model_path) and os.path.isdir(ensemble_model_path):
        ret = clean_directory(ensemble_model_path)
        if not ret:
            logging.error(f"Unable to clean directory '{ensemble_model_path}'")
            return False
    try:
        os.makedirs(ensemble_model_path)
        os.makedirs(os.path.join(ensemble_model_path, model_version))
    except OSError as e:
        logging.error(f"Unable to create directory '{ensemble_model_path}' error {str(e)}")
        return False
    # Write config.pbtxt file first.
    try:
        with open(os.path.join(ensemble_model_path, "config.pbtxt"), "w", encoding="utf-8") as f:
            f.write(ensemble_model_config_pbtxt)
    except OSError as e:
        logging.error(f"Unable to write config.pbtxt file error {str(e)}")
        return False
    # Get working directory of this code to get the model.py file
    working_dir = os.path.dirname(os.path.realpath(__file__))
    ensemble_file_path = os.path.abspath(
        os.path.join(working_dir, "resources_for_copy", "ensemble_model")
    )
    if not os.path.exists(ensemble_file_path):
        logging.error(f"Unable to find ensemble_model file at '{ensemble_file_path}'")
        return False
    try:
        shutil.copy(
            ensemble_file_path, os.path.join(ensemble_model_path, model_version, "ensemble_model")
        )
    except OSError as e:
        logging.error(f"Unable to copy ensemble_model file error {str(e)}")
        return False
    return True


def convert_to_triton_structure(
    model_repo_dir: str, deployed_model_path: str, model_name: str, model_version: str = "1"
) -> bool:
    """
    Convert the model to Triton's structure and save it to the specified path.

    Args:
        model_repo_dir (str): The path to the model repository directory.
        deployed_model_path (str): The original model path of lfv deployed model.
        model_name (str): The name of the model.
        model_version (str): Model version passed from lfv, default is 1


    Returns:
        bool: True if the conversion is successful, False otherwise.
    """
    # Check if model_repo_dir exists
    if not os.path.exists(model_repo_dir):
        os.makedirs(model_repo_dir)
        logging.info(f"model_repo_dir '{model_repo_dir}' created")

    # Check if model_repo_dir is an absolute path
    if not os.path.isabs(model_repo_dir) or not os.path.isdir(model_repo_dir):
        logging.error(
            f"model_repo_dir must be an absolute path and a directory, got '{model_repo_dir}'"
        )
        return False

    # Check if deployed_model_path exists
    if not os.path.exists(deployed_model_path):
        logging.error(f"deployed_model_path '{deployed_model_path}' does not exist")
        return False

    # Check if deployed_model_path is an absolute path
    if not os.path.isabs(deployed_model_path) or not os.path.isdir(deployed_model_path):
        logging.error(
            f"deployed_model_path must be an absolute path and a directory,  got '{deployed_model_path}'"
        )
        return False

    # Check if model_name is empty.
    if not model_name:
        logging.error(f"model_name cannot be empty got '{model_name}'")
        return False
    try:
        model_version_int = int(model_version)
        if model_version_int <= 0:
            raise Exception("model version should be > 0")
    except Exception as e:
        logging.error(
            f"Error converting model_version to int , it should be > 0 got {model_version}, exception : {e}"
        )
        return False
    manifest_json_path = os.path.join(deployed_model_path, "manifest.json")
    if not os.path.exists(manifest_json_path):
        logging.error(f"unable to find manifest.json in model_repo_dir '{model_repo_dir}'")
        return False
    manifest = None
    try:
        with open(manifest_json_path, encoding="utf-8") as f:
            manifest = json.load(f)
        if manifest is None:
            logging.error("Error reading lfv manifest")
            return False
    except Exception as e:
        logging.error(f"Error reading lfv manifest: {str(e)}")
        return False
    ret = _create_base_model_structure(
        model_repo_dir, deployed_model_path, model_name, model_version, manifest
    )
    if not ret:
        logging.error(f"Unable to create base model structure")
        return False
    ret = _create_marshal_model_structure(model_repo_dir, model_name, model_version, manifest)
    if not ret:
        logging.error(f"Unable to create marshal model structure")
        return False
    ret = _create_ensemble_model_structure(model_repo_dir, model_name, model_version, manifest)
    if not ret:
        logging.error(f"Unable to create ensemble model structure")
        return False
    return ret


if __name__ == "__main__":
    try:
        args = parser.parse_args()
        if args.unarchived_model_path and args.model_name and args.model_version is not None:
            input_model_path = args.unarchived_model_path
            model_name = args.model_name
            model_version = args.model_version
            model_version_spiltted = model_version.split(".")[0]
            logging.info(
                f"model unzipped path and major version provided is {input_model_path} : {model_version.split('.')[0]}"
            )
            model_converted = convert_to_triton_structure(
                model_repo_dir=TRITON_MODEL_DIR,
                deployed_model_path=input_model_path,
                model_name=model_name,
                model_version=model_version_spiltted,
            )
            if model_converted:
                logging.info("Model converted successfully")
                start_model(model_name)
            else:
                raise Exception("Model conversion failed")
        else:
            logging.error("Args not provided to do the conversion")
    except Exception as e:
        logging.error(f"Exception occured while doing model conversion : {str(e)}")
        exit(1)
