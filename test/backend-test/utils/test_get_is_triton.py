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
import unittest
from unittest.mock import patch
from utils.get_is_triton import get_is_triton

class TestGetIsTriton(unittest.TestCase):

    @patch.dict(os.environ, {"is_triton": "true"}, clear=True)
    def test_get_is_triton_returns_true(self):
        self.assertTrue(get_is_triton())

    @patch.dict(os.environ, {"is_triton": "false"}, clear=True)
    def test_get_is_triton_returns_false(self):
        self.assertFalse(get_is_triton())

    @patch.dict(os.environ, {}, clear=True)
    def test_get_is_triton_returns_false_when_not_set(self):
        self.assertFalse(get_is_triton())
