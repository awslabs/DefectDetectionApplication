#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import os
import time
import threading

from panorama import trace
from panorama import messagebroker
from panorama import credentials
from panorama import buffer
from panorama import application

import test_utils
import threading

class FileProtocolClientTests:
    def __init__(self):
        self.message_published = threading.Event()

    def OnMessagePublished(self, protocol : str, message : messagebroker.ProtocolMessage, successful : bool):
        test_utils.Expect_Equal(protocol, "file")
        test_utils.Expect_Equal(successful, True)
        self.message_published.set()

    def Publish(self):
        client = messagebroker.FileProtocolClient()
        payload = messagebroker.create_payload_from_string("hello world")
        payload.set_correlation_id("my-correlation-id")
        client.publish(payload, "./", "py_filename_${c_id}.txt")

        file_path = "./py_filename_my-correlation-id.txt"
        with open(file_path, 'r') as file:
            test_utils.Expect_Equal(file.read().rstrip("\x00"), "hello world")


    def PublishAsync(self):
        client = messagebroker.FileProtocolClient()
        payload = messagebroker.create_payload_from_string("hello world")
        payload.set_correlation_id("my-correlation-id")
        client.publish_async(payload, "./", "${c_id}_py_filename", self.OnMessagePublished)
        test_utils.Expect_True(self.message_published.wait(3))

        file_path = "./my-correlation-id_py_filename"
        with open(file_path, 'r') as file:
            test_utils.Expect_Equal(file.read().rstrip("\x00"), "hello world")

def run_tests():
    tests = FileProtocolClientTests()
    tests.Publish()
    tests.PublishAsync()

run_tests()