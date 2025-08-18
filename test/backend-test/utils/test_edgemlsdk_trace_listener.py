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
from panorama import trace
from utils.edgemlsdk_trace_listener import EdgeMLSdkLoggingTraceListener
import unittest
from unittest.mock import patch

class TestEdgeTraceListener(unittest.TestCase):
    def setUp(self):
        self.listener = EdgeMLSdkLoggingTraceListener()
        self.timestamp = 12345678
        self.file = 'example.py'
        self.line = 42
        self.error_level = trace.TraceLevel.Error.value
        self.warning_level = trace.TraceLevel.Warning.value
        self.info_level = trace.TraceLevel.Info.value
        self.debug_level = trace.TraceLevel.Verbose.value

    @patch('utils.edgemlsdk_trace_listener.logger.error')
    def test_WriteMessage_error(self, mock_error):
        self.listener.WriteMessage(self.error_level,self.timestamp, self.line, self.file, 'This is an error message.')
        mock_error.assert_called_with(f"[{self.file}:{self.line}] This is an error message.")

    @patch('utils.edgemlsdk_trace_listener.logger.warning')
    def test_WriteMessage_warning(self, mock_warning):
        self.listener.WriteMessage(self.warning_level,self.timestamp, self.line, self.file, 'This is an warning message.')
        mock_warning.assert_called_with(f"[{self.file}:{self.line}] This is an warning message.")
    
    @patch('utils.edgemlsdk_trace_listener.logger.info')
    def test_WriteMessage_info(self, mock_info):
        self.listener.WriteMessage(self.info_level,self.timestamp, self.line, self.file, 'This is an info message.')
        mock_info.assert_called_with(f"[{self.file}:{self.line}] This is an info message.")
    
    @patch('utils.edgemlsdk_trace_listener.logger.debug')
    def test_WriteMessage_debug(self, mock_debug):
        self.listener.WriteMessage(self.debug_level,self.timestamp, self.line, self.file, 'This is an debug message.')
        mock_debug.assert_called_with(f"[{self.file}:{self.line}] This is an debug message.")