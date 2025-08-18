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
from dda_triton.model_autostart_utils import wait_for_server, is_port_open
import socket


class TestWaitForServer(unittest.TestCase):
    @patch("dda_triton.model_autostart_utils.is_port_open")
    def test_server_reachable_immediately(self, mock_is_port_open):
        # Simulate server is reachable on first attempt
        mock_is_port_open.return_value = True

        # Call the function
        result = wait_for_server("localhost", 5000, "TestModel", retries=3, delay=1)
        self.assertTrue(result)
        mock_is_port_open.assert_called_once()

    @patch("dda_triton.model_autostart_utils.is_port_open")
    def test_server_reachable_after_retries(self, mock_is_port_open):
        # Simulate server unreachable for the first two attempts, then reachable
        mock_is_port_open.side_effect = [False, False, True]
        result = wait_for_server("localhost", 5000, "TestModel", retries=3, delay=1)
        self.assertTrue(result)
        self.assertEqual(mock_is_port_open.call_count, 3)

    @patch("dda_triton.model_autostart_utils.is_port_open")
    def test_server_unreachable(self, mock_is_port_open):
        # Simulate server unreachable for all attempts
        mock_is_port_open.return_value = False

        result = wait_for_server("localhost", 5000, "TestModel", retries=3, delay=1)
        self.assertFalse(result)
        self.assertEqual(mock_is_port_open.call_count, 3)


class TestIsPortOpen(unittest.TestCase):
    @patch("socket.create_connection")
    def test_port_is_open(self, mock_create_connection):
        # Simulate that the port is open (no exception raised)
        mock_create_connection.return_value = MagicMock()

        result = is_port_open("localhost", 5000, "TestModel")
        self.assertTrue(result)
        mock_create_connection.assert_called_with(("localhost", 5000), timeout=10)

    @patch("socket.create_connection")
    def test_port_is_closed(self, mock_create_connection):
        # Simulate a socket.error, meaning the port is closed
        mock_create_connection.side_effect = socket.error
        result = is_port_open("localhost", 5000, "TestModel")
        self.assertFalse(result)
        mock_create_connection.assert_called_with(("localhost", 5000), timeout=10)

    @patch("socket.create_connection")
    def test_port_timeout(self, mock_create_connection):
        # Simulate a socket.timeout, meaning the connection timed out
        mock_create_connection.side_effect = socket.timeout
        result = is_port_open("localhost", 5000, "TestModel")
        self.assertFalse(result)
        mock_create_connection.assert_called_with(("localhost", 5000), timeout=10)
