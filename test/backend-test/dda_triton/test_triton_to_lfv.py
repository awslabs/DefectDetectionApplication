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
from unittest.mock import patch, mock_open
from utils.triton_to_lfv import switch_to_lfv
from exceptions.api.triton_exceptions import TritonInternalServerException, GreengrassOperationException
from fastapi import HTTPException

class TestTritonToLFV(unittest.TestCase):
    @patch('utils.triton_to_lfv.list_gg_components', return_value=['model-1', 'model-2', 'model-3'])
    @patch('utils.triton_to_lfv.restart_components', return_value=None)
    @patch('utils.triton_to_lfv.open', new_callable=mock_open)
    @patch('utils.triton_to_lfv.save_is_triton_value_to_file', return_value=True)
    @patch('utils.triton_to_lfv.clean_triton_model_repo', return_value= True)
    def test_triton_to_lfv_success(self, mock_clean_triton_repo, mock_save_value, mock_open, mock_restart_components, mock_list_components):
        # Test case where all functions succeed
        result = switch_to_lfv()
        self.assertTrue(result)
    
    @patch('utils.triton_to_lfv.list_gg_components', return_value=[])
    @patch('utils.triton_to_lfv.restart_components')
    @patch('utils.triton_to_lfv.open', new_callable=mock_open)
    @patch('utils.triton_to_lfv.save_is_triton_value_to_file', return_value=True)
    @patch('utils.triton_to_lfv.clean_triton_model_repo', return_value= True)
    def test_triton_to_lfv_list_empty(self ,mock_clean_triton_model_repo, mock_save_is_triton_value, mock_open, mock_restart_components, mock_list_components):
        # Test case where list_gg_components returns an empty list
        result = switch_to_lfv()
        self.assertTrue(result)
    
    @patch('utils.triton_to_lfv.list_gg_components', return_value=['model-1', 'model-2', 'model-3'])
    @patch('utils.triton_to_lfv.restart_components')
    @patch('utils.triton_to_lfv.save_is_triton_value_to_file', return_value=True)
    @patch('utils.triton_to_lfv.clean_triton_model_repo', return_value= True)
    def test_triton_to_lfv_restart_failure(self, mock_clean_triton_repo, mock_save_value, mock_restart_components, mock_list_components):
        mock_restart_components.side_effect = [GreengrassOperationException("Failed to restart component")]
        # Test case where restart_components fails for one component
        with self.assertRaises(TritonInternalServerException) as exc:
            switch_to_lfv()

    @patch('utils.triton_to_lfv.list_gg_components', return_value=['model-1', 'model-2', 'model-3'])
    @patch('utils.triton_to_lfv.restart_components', return_value=None)
    @patch('utils.triton_to_lfv.open', new_callable=mock_open)
    @patch('utils.triton_to_lfv.save_is_triton_value_to_file')
    @patch('utils.triton_to_lfv.clean_triton_model_repo', return_value= True)
    def test_triton_to_lfv_save_value_failure(self, mock_clean_triton_repo, mock_save_value, mock_open, mock_restart_components, mock_list_components):
        # Test case where save_is_triton_value_to_file fails
        mock_save_value.side_effect = Exception("An error occurred while saving is_triton value to file")
        with self.assertRaises(HTTPException) as exc:
            switch_to_lfv()
    
    @patch('utils.triton_to_lfv.list_gg_components', return_value=['model-1', 'model-2', 'model-3'])
    @patch('utils.triton_to_lfv.restart_components', return_value=None)
    @patch('utils.triton_to_lfv.open', new_callable=mock_open)
    @patch('utils.triton_to_lfv.save_is_triton_value_to_file', return_value=True)
    @patch('utils.triton_to_lfv.os.path.isdir', return_value=True)
    @patch('utils.triton_to_lfv.clean_directory', return_value= False)
    def test_triton_to_lfv_model_conversion_failure(self, mock_clean_dir, mock_isdir, mock_save_is_triton, mock_open, mock_restart_componentss, mock_list_components):
         with self.assertRaises(TritonInternalServerException) as exc:
        # Test case where model_conversion fails
            switch_to_lfv()

    