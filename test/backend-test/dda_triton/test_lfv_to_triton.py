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
from utils.lfv_to_triton import switch_to_triton
from fastapi import HTTPException
from exceptions.api.triton_exceptions import TritonInternalServerException, GreengrassOperationException
class TestLFVToTriton(unittest.TestCase):
    @patch('utils.lfv_to_triton.list_gg_components', return_value=['model-1', 'model-2', 'model-3'])
    @patch('utils.lfv_to_triton.stop_running_component', return_value=True)
    @patch('utils.lfv_to_triton.open', new_callable=mock_open)
    @patch('utils.lfv_to_triton.save_is_triton_value_to_file', return_value=True)
    @patch('utils.lfv_to_triton.convert_models', return_value= True)
    @patch('utils.lfv_to_triton.create_triton_model_repo', return_value=True)
    @patch('utils.lfv_to_triton.restart_components', return_value = None)
    def test_lfv_to_triton_success(self, mock_restart_components,mock_create_triton_model_repo, mock_convert_models, mock_save_value, mock_file, mock_stop_component, mock_list_components):
        # Test case where all functions succeed
        result = switch_to_triton()
        self.assertTrue(result)
    
    @patch('utils.lfv_to_triton.list_gg_components', return_value=[])
    @patch('utils.lfv_to_triton.stop_running_component', return_value=True)
    @patch('utils.lfv_to_triton.open', new_callable=mock_open)
    @patch('utils.lfv_to_triton.save_is_triton_value_to_file', return_value=True)
    @patch('utils.lfv_to_triton.convert_models', return_value= True)
    @patch('utils.lfv_to_triton.create_triton_model_repo', return_value=True)
    @patch('utils.lfv_to_triton.restart_components', return_value = None)
    def test_lfv_to_triton_list_empty(self, mock_restart_components,mock_create_triton_model_repo, mock_convert_models, mock_save_value, mock_file, mock_stop_component, mock_list_components):
        # Test case where list_gg_components returns an empty list
        result = switch_to_triton()
        self.assertTrue(result)
    
    @patch('utils.lfv_to_triton.list_gg_components', return_value=['model-1', 'model-2', 'model-3'])
    @patch('utils.lfv_to_triton.stop_running_component', return_value=True)
    @patch('utils.lfv_to_triton.save_is_triton_value_to_file', return_value=True)
    @patch('utils.lfv_to_triton.convert_models', return_value= True)
    @patch('utils.lfv_to_triton.restart_components', return_value= None)
    def test_lfv_to_triton_stop_failure(self, mock_restart_components, mock_convert_models, mock_save_value, mock_stop_component, mock_list_components):
        # Test case where stop_running_component fails for one component
        with self.assertRaises(HTTPException) as exc:
            switch_to_triton()
    
    @patch('utils.lfv_to_triton.list_gg_components', return_value=['model-1', 'model-2', 'model-3'])
    @patch('utils.lfv_to_triton.stop_running_component', return_value=True)
    @patch('utils.lfv_to_triton.open', new_callable=mock_open)
    @patch('utils.lfv_to_triton.save_is_triton_value_to_file')
    @patch('utils.lfv_to_triton.convert_models', return_value= True)
    @patch('utils.lfv_to_triton.restart_components', return_value= None)
    def test_lfv_to_triton_save_value_failure(self, mock_restart_components, mock_convert_models, mock_save_value, mock_file, mock_stop_component, mock_list_components):
        # Test case where save_is_triton_value_to_file fails
        mock_save_value.side_effect = Exception("An error occurred while saving is_triton value to file")
        with self.assertRaises(HTTPException) as exc:
            switch_to_triton()
    
    @patch('utils.lfv_to_triton.list_gg_components', return_value=['model-1', 'model-2', 'model-3'])
    @patch('utils.lfv_to_triton.stop_running_component', return_value=True)
    @patch('utils.lfv_to_triton.open', new_callable=mock_open)
    @patch('utils.lfv_to_triton.save_is_triton_value_to_file', return_value=True)
    @patch('utils.lfv_to_triton.un_archive_lfv_models', return_value=None)
    @patch('utils.lfv_to_triton.convert_models', return_value= False)
    @patch('utils.lfv_to_triton.restart_components', return_value= None)
    def test_lfv_to_triton_model_conversion_failure(self, mock_restart_components, mock_convert_models,mock_unarchive_models, mock_save_value, mock_file, mock_stop_component, mock_list_components):
         with self.assertRaises(TritonInternalServerException) as exc:
            # Test case where model_conversion fails
            switch_to_triton()
