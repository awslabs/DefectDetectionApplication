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

class MqttProtocolClientTests:
    def __init__(self):
        self.message_received = threading.Event()
        self.message_published = threading.Event()

    def OnMessageReceived(self, payload):
        test_utils.Expect_Equal(payload.serialize_as_string(), "hello world")
        self.message_received.set()

    def OnMessagePublished(self, protocol : str, message : messagebroker.ProtocolMessage, successful : bool):
        test_utils.Expect_Equal(protocol, "mqtt")
        test_utils.Expect_Equal(successful, True)
        self.message_published.set()

    def Publish(self):
        app = application.create()
        mqtt = messagebroker.MqttProtocolClient("a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com", "us-west-2", app)
        cancellation_token = mqtt.subscribe("my_test_topic_python", self.OnMessageReceived)

        # Subscription isn't immediately hooked up on server side, give it time to complete that
        time.sleep(1)

        payload = messagebroker.create_payload_from_string("hello world")
        mqtt.publish("my_test_topic_python", payload)

        test_utils.Expect_True(self.message_received.wait(3))
        self.message_received.clear()

        mqtt.unsubscribe(cancellation_token)
        mqtt.publish("my_test_topic_python", payload)
        test_utils.Expect_False(self.message_received.wait(3))

    def PublishAsync(self):
        app = application.create()
        mqtt = messagebroker.MqttProtocolClient("a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com", "us-west-2", app)
        mqtt.subscribe("my_test_topic_python", self.OnMessageReceived)

        # Subscription isn't immediately hooked up on server side, give it time to complete that
        time.sleep(1)

        payload = messagebroker.create_payload_from_string("hello world")
        mqtt.publish_async("my_test_topic_python", payload, self.OnMessagePublished)
        test_utils.Expect_True(self.message_published.wait(3))
        test_utils.Expect_True(self.message_received.wait(3))
        self.message_received.clear()

def run_tests():
    tests = MqttProtocolClientTests()
    tests.Publish()
    tests.PublishAsync()

run_tests()