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
from utils.lfv_to_triton import clean_model_unarchive_directory
import pytest
from exceptions.api.triton_exceptions import TritonSetupException


class TestCleanModelUnarchiveDirectory(unittest.TestCase):
    
    @patch('os.walk')
    @patch('utils.lfv_to_triton.clean_directory', return_value=True)
    def test_clean_model_unarchive_directory(self, mock_clean, mock_walk):
      
        mock_walk.return_value = [
            ('/path/to/your/base/directory', ['model-dir1', 'model-dir2', 'dir3'], []),
            ('/path/to/your/base/directory/model-dir1', [], []),
            ('/path/to/your/base/directory/model-dir2', [], []),
            ('/path/to/your/base/directory/dir3', [], [])
        ]
        clean_model_unarchive_directory()
        mock_clean.assert_any_call('/path/to/your/base/directory/model-dir1')
        mock_clean.assert_any_call('/path/to/your/base/directory/model-dir2')
        
    @patch('os.walk')
    @patch('utils.lfv_to_triton.clean_directory', return_value=True)
    def test_clean_model_unarchive_directory_no_match(self, mock_clean, mock_walk):
        mock_walk.return_value = [
            ('/path/to/your/base/directory', ['dir1', 'dir2'], []),
            ('/path/to/your/base/directory/dir1', [], []),
            ('/path/to/your/base/directory/dir2', [], [])
        ]
        clean_model_unarchive_directory()
        mock_clean.assert_not_called()
        
    @patch('os.walk')
    @patch('utils.lfv_to_triton.clean_directory', return_value=False)
    def test_clean_model_unarchive_directory_failure(self, mock_clean, mock_walk):
        with pytest.raises(TritonSetupException):
            mock_walk.return_value = [
                ('/path/to/your/base/directory', ['model-dir1', 'dir3'], []),
                ('/path/to/your/base/directory/model-dir1', [], []),
                ('/path/to/your/base/directory/dir2', [], [])
            ]
            clean_model_unarchive_directory()
            mock_clean.assert_any_call('/path/to/your/base/directory/model-dir1')
    
    @patch('os.walk', side_effect=OSError)
    @patch('utils.lfv_to_triton.clean_directory', return_value=True)
    def test_clean_model_unarchive_directory_OS_failure(self, mock_clean, mock_walk):
        with pytest.raises(TritonSetupException):
            mock_walk.return_value = [
                ('/path/to/your/base/directory', ['model-dir1', 'dir3'], []),
                ('/path/to/your/base/directory/model-dir1', [], []),
                ('/path/to/your/base/directory/dir2', [], [])
            ]
            clean_model_unarchive_directory()
            mock_clean.assert_any_call('/path/to/your/base/directory/model-dir1')

if __name__ == '__main__':
    unittest.main()
