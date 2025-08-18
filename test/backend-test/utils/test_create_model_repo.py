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
from utils.lfv_to_triton import create_triton_model_repo
from unittest.mock import patch, Mock
import unittest
import pytest
from exceptions.api.triton_exceptions import TritonSetupException
class test_create_triton_model_repo(unittest.TestCase):
    @patch('utils.lfv_to_triton.os.path.exists')
    @patch('utils.lfv_to_triton.os.makedirs')
    def test_create_triton_model_repo_when_dir_does_not_exist(self, mock_mkdir, mock_dir_exists):
        mock_dir_exists.return_value = False
        result = create_triton_model_repo()
        mock_dir_exists.assert_called_once()
        mock_mkdir.assert_called_once()
        self.assertTrue(result)

    @patch('utils.lfv_to_triton.os.path.exists')
    def test_create_triton_model_repo_when_dir_does_exist(self, mock_dir_exists):
        mock_dir_exists.return_value = True
        result = create_triton_model_repo()
        mock_dir_exists.assert_called_once()
        self.assertFalse(result)
    
    @patch('utils.lfv_to_triton.os.path.exists')
    @patch('utils.lfv_to_triton.os.makedirs')
    def test_create_triton_model_repo_failure(self, mock_makedirs, mock_dir_exists):
        with pytest.raises(TritonSetupException):
            mock_makedirs.side_effect = OSError("Permission denied")
            mock_dir_exists.return_value = False
            create_triton_model_repo()
            mock_dir_exists.assert_called_once()
    