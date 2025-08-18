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

from panorama import buffer
from panorama import messagebroker
from panorama import credentials

import json
import test_utils
import threading
import time

class MessageBrokerTests:

    def test_create_message_broker(self):
        creds = credentials.create_default_aws_credential_provider()
        test_utils.Expect_Exception(lambda: messagebroker.create(None))
        broker = messagebroker.create(creds)
        test_utils.Expect_Equal("7BF68FD8-8D46-4A7A-A1A4-F2922CEA74FE", type(broker).uuid())

    def test_create_payload(self):
        test_utils.Expect_Exception(lambda: messagebroker.create_payload_from_string(None))
        test_utils.Expect_Exception(lambda: messagebroker.create_payload_from_buffer(None))

        buf = buffer.create_from_string("hi")

        payload_from_string = messagebroker.create_payload_from_string("contents")
        payload_from_buffer = messagebroker.create_payload_from_buffer(buf)
        test_utils.Expect_Equal("A0BE4CF1-0241-4157-B7F1-4E5D35D92990", type(payload_from_string).uuid())
        test_utils.Expect_Equal("A0BE4CF1-0241-4157-B7F1-4E5D35D92990", type(payload_from_buffer).uuid())

    def test_create_batch_payload(self):
        batch = messagebroker.create_empty_batch_payload()
        test_utils.Expect_Equal("789123A1-40BC-4773-A246-F183D307E219", type(batch).uuid())

    def test_id(self):
        creds = credentials.create_default_aws_credential_provider()
        broker = messagebroker.create(creds)
        payload = messagebroker.create_payload_from_string("contents")
        batch_payload = messagebroker.create_empty_batch_payload()

        message_received = threading.Event()
        last_id = ""
        def check_id(payload):
            nonlocal last_id
            test_utils.Expect_True(payload.id() != None)
            test_utils.Expect_True(payload.id() != last_id)
            test_utils.Expect_Equal(len(payload.id()), 36)
            last_id = payload.id()
            message_received.set()
        broker.subscribe("test_id", check_id)

        broker.publish("test_id", payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

        broker.publish("test_id", batch_payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

    def test_correlation_id(self):
        creds = credentials.create_default_aws_credential_provider()
        broker = messagebroker.create(creds)

        payload = messagebroker.create_payload_from_string("hello world")
        batch_payload = messagebroker.create_empty_batch_payload()

        message_received = threading.Event()
        def default_cid(payload):
            test_utils.Expect_Equal("", payload.correlation_id())
            message_received.set()
        broker.subscribe("default_cid", default_cid)

        broker.publish("default_cid", payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

        broker.publish("default_cid", batch_payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

        def custom_cid(payload):
            test_utils.Expect_Equal("my-cid", payload.correlation_id())
            message_received.set()
        broker.subscribe("custom_cid", custom_cid)

        payload.set_correlation_id("my-cid")
        broker.publish("custom_cid", payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

        batch_payload.set_correlation_id("my-cid")
        broker.publish("custom_cid", batch_payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

    def test_timestamp(self):
        creds = credentials.create_default_aws_credential_provider()
        broker = messagebroker.create(creds)

        # default timestamp is in 10^-7 seconds since epoch
        before_ts = time.time_ns() / 100
        payload = messagebroker.create_payload_from_string("hello world")
        batch_payload = messagebroker.create_empty_batch_payload()
        after_ts = time.time_ns() / 100

        message_received = threading.Event()
        def default_ts(payload):
            test_utils.Expect_True(before_ts <= payload.timestamp())
            test_utils.Expect_True(payload.timestamp() <= after_ts)
            message_received.set()
        broker.subscribe("default_ts", default_ts)

        broker.publish("default_ts", payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

        broker.publish("default_ts", batch_payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

        def custom_ts(payload):
            test_utils.Expect_Equal(123, payload.timestamp())
            message_received.set()
        broker.subscribe("set_ts", custom_ts)

        payload.set_timestamp(123)
        broker.publish("set_ts", payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

        batch_payload.set_timestamp(123)
        broker.publish("set_ts", batch_payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

    def test_publish_subscribe(self):
        message_received = threading.Event()
        message_published = threading.Event()

        def OnMessageReceived(payload):
            test_utils.Expect_Equal(payload.serialize_as_string(), "hello world")
            message_received.set()

        def OnMessagePublished(protocol : str, message_id : str, payload : messagebroker.Payload, successful : bool):
            test_utils.Expect_Equal(protocol, "loopback")
            test_utils.Expect_Equal(message_id, "topic")
            test_utils.Expect_Equal(successful, True)
            message_published.set()

        creds = credentials.create_default_aws_credential_provider()
        broker = messagebroker.create(creds)

        # subscribe and create payload
        cancellation_token = broker.subscribe("topic", OnMessageReceived)
        payload = messagebroker.create_payload_from_string("hello world")
        
        # publish, validate message was received by subscriber
        broker.publish("topic", payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

        # publish async
        broker.publish_async("topic", payload, OnMessagePublished)
        test_utils.Expect_True(message_published.wait(3))
        test_utils.Expect_True(message_received.wait(3))
        message_received.clear()

        # unsubcribe, publish again, should not invoke callback
        broker.unsubscribe(cancellation_token)
        broker.publish("topic", payload)
        test_utils.Expect_False(message_received.wait(0))

    def test_buffer_payload(self):
        creds = credentials.create_default_aws_credential_provider()
        broker = messagebroker.create(creds)

        buf = buffer.create_from_string("contents")
        payload1 = messagebroker.create_payload_from_buffer(buf)
        payload2 = messagebroker.create_payload_from_string("contents")

        message_received = threading.Event()
        def check_buffer_payload(payload):
            nonlocal buf
            test_utils.Expect_Equal(payload.serialize_as_string(), "contents")
            test_utils.Expect_Equal(buf.as_string(), "contents")
            received_buf = payload.serialize()
            test_utils.Expect_Equal(buf.as_string(), received_buf.as_string())
            test_utils.Expect_Equal("8F904259-6CDE-4C75-8B55-E2828E55345F", type(received_buf).uuid())
            message_received.set()
        broker.subscribe("test_buffer_payload", check_buffer_payload)

        broker.publish("test_buffer_payload", payload1)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

        broker.publish("test_buffer_payload", payload2)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

    def test_batch_payload(self):
        creds = credentials.create_default_aws_credential_provider()
        broker = messagebroker.create(creds)
        batch_payload = messagebroker.create_empty_batch_payload()

        message_received = threading.Event()
        def check_empty_batch(payload):
            queried_batch = payload.query_interface(messagebroker.BatchPayload)
            test_utils.Expect_Exception(lambda: queried_batch.payload(0))
            test_utils.Expect_Exception(lambda: queried_batch.payload(-1))
            test_utils.Expect_Exception(lambda: queried_batch.payload(1))
            test_utils.Expect_Exception(lambda: queried_batch.payload(None))
            test_utils.Expect_Exception(lambda: queried_batch.payload("not_the_right_id"))
            test_utils.Expect_Equal(queried_batch.count(), 0)

            serialized_buffer = queried_batch.serialize()
            test_utils.Expect_True(isinstance(serialized_buffer, buffer.Buffer))
            test_utils.Expect_Equal(serialized_buffer.as_string(), queried_batch.serialize_as_string())

            j = json.loads(queried_batch.serialize_as_string())
            test_utils.Expect_True("timestamp" in j)
            test_utils.Expect_True("id" in j)
            test_utils.Expect_True("correlation_id" in j)
            test_utils.Expect_True("payload_count" in j)
            test_utils.Expect_True("payloads" in j)
            test_utils.Expect_Equal(j["payload_count"], 0)
            test_utils.Expect_Equal(j["payloads"], [])

            message_received.set()
        broker.subscribe("empty_batch_message", check_empty_batch)

        broker.publish("empty_batch_message", batch_payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

        payload1 = messagebroker.create_payload_from_string("contents1")
        payloadId1 = payload1.id()
        batch_payload.add_payload(payload1)
        test_utils.Expect_Exception(lambda: batch_payload.add_payload(batch_payload))

        def check_single_batch(payload):
            nonlocal payloadId1

            queried_batch = payload.query_interface(messagebroker.BatchPayload)
            test_utils.Expect_Equal(queried_batch.count(), 1)
            test_utils.Expect_Exception(lambda: queried_batch.payload(1))

            subpayload1 = queried_batch.payload(0)
            test_utils.Expect_Equal(subpayload1.serialize_as_string(), "contents1")

            samesubpayload1 = queried_batch.payload(payloadId1)
            test_utils.Expect_Equal(samesubpayload1.serialize_as_string(), "contents1")

            serialized_buffer = queried_batch.serialize()
            test_utils.Expect_True(isinstance(serialized_buffer, buffer.Buffer))
            test_utils.Expect_Equal(serialized_buffer.as_string(), queried_batch.serialize_as_string())

            j = json.loads(queried_batch.serialize_as_string())
            test_utils.Expect_True("timestamp" in j)
            test_utils.Expect_True("id" in j)
            test_utils.Expect_True("correlation_id" in j)
            test_utils.Expect_True("payload_count" in j)
            test_utils.Expect_True("payloads" in j)
            test_utils.Expect_Equal(j["payload_count"], 1)
            test_utils.Expect_True("timestamp" in j["payloads"][0])
            test_utils.Expect_Equal(j["payloads"][0]["id"], payloadId1)
            test_utils.Expect_True("correlation_id" in j["payloads"][0])

            message_received.set()
        broker.subscribe("single_batch_message", check_single_batch)

        broker.publish("single_batch_message", batch_payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

        payload2 = messagebroker.create_payload_from_string("contents2")
        payloadId2 = payload2.id()
        batch_payload.add_payload(payload2)

        def _batch_payload_helper(batch):
            nonlocal payloadId1
            nonlocal payloadId2

            test_utils.Expect_Equal(batch.count(), 2)
            test_utils.Expect_Exception(lambda: batch.payload(2))
            subpayload1 = batch.payload(0)
            test_utils.Expect_Equal(subpayload1.serialize_as_string(), "contents1")
            samesubpayload1 = batch.payload(payloadId1)
            test_utils.Expect_Equal(samesubpayload1.serialize_as_string(), "contents1")

            subpayload2 = batch.payload(1)
            test_utils.Expect_Equal(subpayload2.serialize_as_string(), "contents2")
            samesubpayload2 = batch.payload(payloadId2)
            test_utils.Expect_Equal(samesubpayload2.serialize_as_string(), "contents2")

            j = json.loads(batch.serialize_as_string())
            test_utils.Expect_True("timestamp" in j)
            test_utils.Expect_True("id" in j)
            test_utils.Expect_True("correlation_id" in j)
            test_utils.Expect_True("payload_count" in j)
            test_utils.Expect_True("payloads" in j)
            test_utils.Expect_Equal(j["payload_count"], 2)
            test_utils.Expect_True("timestamp" in j["payloads"][0])
            test_utils.Expect_Equal(j["payloads"][0]["id"], payloadId1)
            test_utils.Expect_True("correlation_id" in j["payloads"][0])
            test_utils.Expect_True("timestamp" in j["payloads"][1])
            test_utils.Expect_Equal(j["payloads"][1]["id"], payloadId2)
            test_utils.Expect_True("correlation_id" in j["payloads"][1])

        def check_multi_batch(payload):
            queried_batch = payload.query_interface(messagebroker.BatchPayload)
            _batch_payload_helper(queried_batch)
            message_received.set()
        broker.subscribe("multi_batch_message", check_multi_batch)

        broker.publish("multi_batch_message", batch_payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

        recursive_batch_payload = messagebroker.create_empty_batch_payload()
        recursive_batch_payload.add_payload(batch_payload)

        def check_recursive_batch(payload):
            queried_batch = payload.query_interface(messagebroker.BatchPayload)
            test_utils.Expect_Equal(queried_batch.count(), 1)
            batch_in_batch = queried_batch.payload(0).query_interface(messagebroker.BatchPayload)
            _batch_payload_helper(batch_in_batch)
            message_received.set()
        broker.subscribe("recursive_batch_message", check_recursive_batch)

        broker.publish("recursive_batch_message", recursive_batch_payload)
        test_utils.Expect_True(message_received.wait(0))
        message_received.clear()

    def add_custom_protocol(self):
        # TODO.  Need to project IProtocolFactory
        pass

def run_tests():
    tests = MessageBrokerTests()
    tests.test_create_message_broker()
    tests.test_create_payload()
    tests.test_create_batch_payload()
    tests.test_id()
    tests.test_correlation_id()
    tests.test_timestamp()
    tests.test_publish_subscribe()
    tests.test_buffer_payload()
    tests.test_batch_payload()

run_tests()
