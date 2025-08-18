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
from utils.gg_utils import save_is_triton_value_to_file
import json
from exceptions.api.triton_exceptions import FileSaveException

class TestSaveTritonValue(unittest.TestCase):
    def test_save_is_triton_true(self):
        with patch('builtins.open', mock_open()) as mock_file:
            # Call the save_is_triton_value_to_file function
            result = save_is_triton_value_to_file(is_triton="True")

            self.assertTrue(result)
            test_dict= {"is_triton": "True"}
            # Assert that the file was opened with the correct mode and content
            mock_file.assert_called_once_with('/aws_dda/dda_triton/is_triton.txt', 'w')
            mock_file().write.assert_called_once_with(json.dumps(test_dict))

    def test_save_is_triton_false(self):
        # Call the save_is_triton_value_to_file function
        with patch('builtins.open', mock_open()) as mock_file:
            result = save_is_triton_value_to_file(is_triton="False")
            self.assertTrue(result)
            test_dict= {"is_triton": "False"}
            # Assert that the file was opened with the correct mode and content
            mock_file.assert_called_once_with('/aws_dda/dda_triton/is_triton.txt', 'w')
            mock_file().write.assert_called_once_with(json.dumps(test_dict))


    @patch('builtins.open', side_effect=IOError)
    def test_save_is_triton_value_error(self, mock_open):
        with self.assertRaises(FileSaveException) as exc:
        #Assert there was an error opening the file
            save_is_triton_value_to_file(is_triton="True")
        
