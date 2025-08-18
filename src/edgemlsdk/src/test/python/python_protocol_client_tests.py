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

import typing
import test_utils
import threading
import time

from panorama import messagebroker
from panorama import buffer
from panorama import apidefs
from panorama import credentials
from panorama import unknown

published_message_option = ""
published_payload = None

# ====== Implementations ======
class ProtocolMessageImpl(messagebroker.PyProtocolMessage):
    def __init__(self, payload, message_option):
        messagebroker.PyProtocolMessage.__init__(self)
        self._payload = payload
        self._message_option = message_option

    def payload(self):
        return self._payload

    def message_option(self):
        return self._message_option
    
class ProtocolSubscriptionImpl(messagebroker.PyProtocolSubscription):
    def __init__(self, sub_id):
        messagebroker.PyProtocolSubscription.__init__(self)
        self._sub_id = sub_id

    def sub_id(self):
        return self._sub_id


class ProtocolClientImpl(messagebroker.PyProtocolClient):
    def __init__(self):
        messagebroker.PyProtocolClient.__init__(self)

    def subscribe(self, subscription):
        # this implementation doesn't need to do anything specific for subscribe
        # as is handled by base implementation
        pass
    
    def unsubscribe(self, cancellation_token):
        # this implementation doesn't need to do anything specific for unsubscribe
        # as is handled by base implementation
        pass

    def publish(self, message):
        # Invoke the callback that was subscribed with the same message_option as this message.message_option
        global published_message_option
        global published_payload

        published_message_option = message.message_option()
        published_payload = message.payload()

        self.invoke_message_received(message.payload(), lambda x: x.sub_id() == message.message_option())

    def publish_async(self, message, eventHandler):
        self.publish(message)
        eventHandler.OnMessagePublished(self.friendly_name(), message, True)

    def friendly_name(self):
        return "test_protocol_client"
    
class ProtocolFactoryImpl(messagebroker.PyProtocolFactory):
    def __init__(self):
        messagebroker.PyProtocolFactory.__init__(self)

    def create_protocol(self, creation_options, credential_provider):
        if "some_option" in creation_options:
            return ProtocolClientImpl()

        raise Exception("Invalid creation options")
    
    def validate_message_options(self, message_options):
        return "message_option" in message_options
    
    def create_message(self, payload, message_options):
        return ProtocolMessageImpl(payload, message_options["message_option"])

    def protocol_name(self):
        return "test_protocol_client"

# ======= Pythonified Wrapper (not actually necessary, but easier to use if not going through message broker) ==========
class TestProtocolClient:
    def __init__(self):
        impl = ProtocolClientImpl()
        self.client = messagebroker.ProtocolClient(impl)

    def subscribe(self, subscription_option, cb : typing.Callable[[messagebroker.Payload], None]):
        impl = ProtocolSubscriptionImpl(subscription_option)
        subscription = messagebroker.ProtocolSubscription(impl)
        return self.client.subscribe(subscription, cb)
    
    def unsubscribe(self, cancellation_token):
        self.client.unsubscribe(cancellation_token)

    def publish(self, message_id, payload):
        impl = ProtocolMessageImpl(payload, message_id)
        self.client.publish(messagebroker.ProtocolMessage(impl))

    def publish_async(self, message_id, payload, cb : typing.Callable[[str, messagebroker.ProtocolMessage, bool], None]):
        impl = ProtocolMessageImpl(payload, message_id)
        self.client.publish_async(messagebroker.ProtocolMessage(impl), cb)

    def friendly_name(self):
        return self.client.friendly_name()
    

# ===== Tests =======

class PythonProtocolTests():
    def __init__(self):
        self.message_received = threading.Event()
        self.message_published = threading.Event()

    def OnMessageReceived(self, payload):
        test_utils.Expect_Equal("hello world", payload.serialize_as_string())
        test_utils.Expect_Equal("my-correlation-id", payload.correlation_id())
        self.message_received.set()

    def PublishComplete(self, protocol, message, successful):
        test_utils.Expect_Equal("test_protocol_client", protocol)
        test_utils.Expect_True(successful)
        test_utils.Expect_Equal("hello world", message.payload().serialize_as_string())
        self.message_published.set()

    def Publish(self):
        # Create test payload
        payload = messagebroker.create_payload_from_string("hello world")
        payload.set_correlation_id("my-correlation-id")

        # Create the protocol client
        client = TestProtocolClient()
        test_utils.Expect_Equal(client.friendly_name(), "test_protocol_client")

        # Subscribe to 'id' and publish to 'id'
        token = client.subscribe("id", self.OnMessageReceived)
        client.publish("id", payload)
        test_utils.Expect_True(self.message_received.wait(0))

        # async message published
        self.message_received.clear()
        client.publish_async("id", payload, self.PublishComplete)
        test_utils.Expect_True(self.message_received.wait(0))
        test_utils.Expect_True(self.message_published.wait(0))

        # unsubscribe
        self.message_received.clear()
        client.unsubscribe(token)
        client.publish("id", payload)
        test_utils.Expect_False(self.message_received.wait(0))

    def MessageBrokerIntegration(self):
        config = """{
                        "targets": [
                            {
                                "protocol": "test_protocol_client",
                                "name": "test",
                                "test_protocol_client_options": {
                                    "some_option": 5
                                }
                            }
                        ],
                        "pipes": [
                            {
                                "message_id": "some_id",
                                "destinations": [
                                    {
                                        "target_name": "test",
                                        "test_protocol_client_message_options": {
                                            "message_option": "some_val"
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                    """

        # create the message broker
        creds = credentials.create_default_aws_credential_provider()
        messagebroker.set_default_config(config)
        broker = messagebroker.create(creds)

        # add the custom factory to the message broker
        factory = ProtocolFactoryImpl()
        broker.add_protocol_factory(factory)

        # Initialize the broker after the factory has been added
        broker.initialize()

        # Publish message to "some_id"
        payload = messagebroker.create_payload_from_string("hello world")
        payload.set_correlation_id("some_correlation_id")
        broker.publish("some_id", payload)

        test_utils.Expect_Equal("some_val", published_message_option)
        test_utils.Expect_Equal("hello world", published_payload.SerializeAsString())
        test_utils.Expect_Equal("some_correlation_id", published_payload.CorrelationId())

def run_tests():
    tests = PythonProtocolTests()

    tests.Publish()
    tests.MessageBrokerIntegration()

    global published_payload
    del published_payload

run_tests()