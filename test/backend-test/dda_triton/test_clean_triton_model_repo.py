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
import pytest
from unittest.mock import patch, call
from utils.triton_to_lfv import clean_triton_model_repo
from exceptions.api.triton_exceptions import TritonInternalServerException
from tempfile import TemporaryDirectory
import os

@patch('dda_triton.model_convertor.clean_directory', return_value= False)
@patch('os.listdir')
@patch('os.path.isdir', return_value=True)
def test_clean_triton_model_repo_fails(mock_idsir, mock_listdir, mock_rmtree):
    with TemporaryDirectory() as temp_dir:
        with pytest.raises(TritonInternalServerException, match="Cleanup for Triton model repository failed."):
            mock_listdir.return_value = ['subdir1']
            clean_triton_model_repo(model_dir=temp_dir)

def test_clean_triton_model_repo_no_directory():
    with patch('os.path.isdir', return_value=False):
        assert clean_triton_model_repo() is True

def test_clean_triton_model_repo_exception():
    with patch('os.path.isdir', side_effect=TritonInternalServerException(detail="Test exception")):
        with pytest.raises(TritonInternalServerException, match="500: Test exception"):
            clean_triton_model_repo()

@patch('os.listdir')
@patch('shutil.rmtree')
def test_clean_triton_model_repo_success(mock_rmtree, mock_listdir):
        with TemporaryDirectory() as temp_dir:
            # Setup mock return values
            mock_listdir.return_value = ['subdir1', 'subdir2', 'README.md']

            # Call the function to test
            clean_triton_model_repo(model_dir = temp_dir)
            
            # Check that os.listdir was called with the temp directory
            mock_listdir.assert_called_once_with(temp_dir)
            
            # Check that shutil.rmtree was called for each subdirectory
            expected_rmtree_calls = [call(os.path.join(temp_dir, 'subdir1')), 
                                     call(os.path.join(temp_dir, 'subdir2'))]
            mock_rmtree.assert_has_calls(expected_rmtree_calls, any_order=True)
