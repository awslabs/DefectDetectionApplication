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
import unittest
from unittest.mock import patch, MagicMock
import os
from utils.lfv_to_triton import convert_models


import zipfile
import platform
import logging
logger = logging.getLogger(__name__)
import shutil
from exceptions.api.triton_exceptions import TritonInternalServerException
import pytest

model_name = "test_model"
working_dir = os.path.dirname(os.path.realpath(__file__))
model_repository = os.path.abspath(os.path.join(working_dir, "test_model_repository"))
extract_location = os.path.abspath(os.path.join(working_dir, "test_model_extracted_location"))

class TestConvertModels(unittest.TestCase):

    @patch('os.path.exists', return_value=True)
    @patch('os.listdir', return_value=['model-1', 'model-2'])
    @patch('os.path.isdir', return_value=True)
    @patch('utils.lfv_to_triton.TRITON_MODEL_DIR', extract_location)
    @patch('utils.lfv_to_triton.LFV_MODEL_DIR_PATH', model_repository)
    @patch("utils.lfv_to_triton.convert_to_triton_structure", return_value=True)
    def test_convert_models_success(self, mock_isdir, mock_listdir, mock_exists, mock_model_convertor):

        # Execute the function
        result = convert_models()
        
        # Assertions
        self.assertTrue(result)

    @patch('os.path.exists', return_value=False)
    @patch('utils.lfv_to_triton.TRITON_MODEL_DIR', extract_location)
    @patch('utils.lfv_to_triton.LFV_MODEL_DIR_PATH', model_repository)
    def test_convert_models_triton_dir_not_exist(self, mock_exists):
        try:
            machine = platform.machine()
            os.makedirs(model_repository, exist_ok=True)
            os.makedirs(extract_location, exist_ok=True)
            with zipfile.ZipFile(
            os.path.join(working_dir, "test-artifacts", "models", f"{machine}_cpu_model.zip")
        ) as z:
                z.extractall(extract_location)
        except OSError as e:
            logger.info(f"Error: {e}")
        
        # Execute the function
        result = convert_models()
        
        # Assertions
        self.assertTrue(result)
        shutil.rmtree(model_repository)
        shutil.rmtree(extract_location)

    @patch('os.path.exists', return_value=True)
    @patch('os.listdir', return_value=['invalid-model'])
    @patch('os.path.isdir', return_value=True)
    @patch('utils.lfv_to_triton.TRITON_MODEL_DIR', extract_location)
    @patch('utils.lfv_to_triton.LFV_MODEL_DIR_PATH', model_repository)
    def test_convert_models_no_valid_models(self, mock_isdir, mock_listdir, mock_exists):
        
        # Execute the function
        result = convert_models()
        
        # Assertions
        self.assertTrue(result)

    @patch('os.path.exists', return_value=True)
    @patch('os.listdir', side_effect=OSError('Mock OS error'))
    @patch('utils.lfv_to_triton.TRITON_MODEL_DIR', extract_location)
    @patch('utils.lfv_to_triton.LFV_MODEL_DIR_PATH', model_repository)
    def test_convert_models_os_error(self, mock_listdir, mock_exists):
        
        # Execute the function
        with pytest.raises(TritonInternalServerException) as err:
            convert_models()

    @patch('os.path.exists', return_value=True)
    @patch('os.listdir', return_value=['model-1', 'model-2'])
    @patch('os.path.isdir', return_value=True)
    @patch('utils.lfv_to_triton.TRITON_MODEL_DIR', extract_location)
    @patch('utils.lfv_to_triton.LFV_MODEL_DIR_PATH', model_repository)
    @patch("utils.lfv_to_triton.convert_to_triton_structure", return_value=False)
    def test_convert_models_failure(self, mock_isdir, mock_listdir, mock_exists, mock_model_convertor):

        # Execute the function
        with pytest.raises(TritonInternalServerException) as err:
            convert_models()