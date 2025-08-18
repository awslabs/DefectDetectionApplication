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

import threading

from panorama import messagebroker
from panorama import application

import test_utils
import threading

class S3ProtocolClientTests:

    def _SendAndCheckProtocolClientMessage(self, s3_protocol_client: messagebroker.S3ProtocolClient, boto3_s3_client, is_async : bool, message_content : str, expected_content : str, set_explicit_overwrite_flag : bool, overwrite_flag : bool = True):
        async_wait_time = 3

        message_published = threading.Event()
        def OnMessagePublished(protocol : str, message : messagebroker.ProtocolMessage, successful : bool):
            test_utils.Expect_Equal(protocol, "s3")
            test_utils.Expect_Equal(successful, True)
            test_utils.Expect_Equal(message.payload().serialize_as_string(), message_content)
            message_published.set()

        key = ""
        if is_async:
            key = "test/s3client_publishasync_python"
        else:
            key = "test/s3client_publish_python"

        payload = messagebroker.create_payload_from_string(message_content)
        if set_explicit_overwrite_flag:
            if is_async:
                s3_protocol_client.publish_async("panorama-sdk-v2-artifacts", key, payload, OnMessagePublished, overwrite_flag)
                test_utils.Expect_True(message_published.wait(async_wait_time))
            else:
                s3_protocol_client.publish("panorama-sdk-v2-artifacts", key, payload, overwrite_flag)
        else:
            if is_async:
                s3_protocol_client.publish_async("panorama-sdk-v2-artifacts", key, payload, OnMessagePublished)
                test_utils.Expect_True(message_published.wait(async_wait_time))
            else:
                s3_protocol_client.publish("panorama-sdk-v2-artifacts", key, payload)

        # verify contents of s3 bucket
        obj = boto3_s3_client.get_object(Bucket="panorama-sdk-v2-artifacts", Key=key)
        # printing out string with ascii shows '\x00' NUL char, which is not removed by plain strip
        bucket_content = str(obj['Body'].read().decode('utf-8')).strip('\x00')
        test_utils.Expect_Equal(bucket_content, expected_content)

    def Publish(self):
        app = application.create()
        session = app.create_boto3_session()
        s3_protocol_client = messagebroker.S3ProtocolClient("us-west-2", app)
        boto3_s3_client = session.client('s3')

        # Test default behavior allows overwrite
        self._SendAndCheckProtocolClientMessage(s3_protocol_client=s3_protocol_client, boto3_s3_client=boto3_s3_client, is_async=False, message_content="content1", expected_content="content1", set_explicit_overwrite_flag=False)
        self._SendAndCheckProtocolClientMessage(s3_protocol_client=s3_protocol_client, boto3_s3_client=boto3_s3_client, is_async=False, message_content="content2", expected_content="content2", set_explicit_overwrite_flag=False)

        # Test overwrites explicitly disallowed
        self._SendAndCheckProtocolClientMessage(s3_protocol_client=s3_protocol_client, boto3_s3_client=boto3_s3_client, is_async=False, message_content="content3", expected_content="content2", set_explicit_overwrite_flag=True, overwrite_flag=False)
        
        # Test overwrites explicitly allowed
        self._SendAndCheckProtocolClientMessage(s3_protocol_client=s3_protocol_client, boto3_s3_client=boto3_s3_client, is_async=False, message_content="content4", expected_content="content4", set_explicit_overwrite_flag=True, overwrite_flag=True)

    def PublishAsync(self):
        app = application.create()
        session = app.create_boto3_session()
        s3_protocol_client = messagebroker.S3ProtocolClient("us-west-2", app)
        boto3_s3_client = session.client('s3')

        # Test default behavior allows overwrite
        self._SendAndCheckProtocolClientMessage(s3_protocol_client=s3_protocol_client, boto3_s3_client=boto3_s3_client, is_async=True, message_content="content1", expected_content="content1", set_explicit_overwrite_flag=False)
        self._SendAndCheckProtocolClientMessage(s3_protocol_client=s3_protocol_client, boto3_s3_client=boto3_s3_client, is_async=True, message_content="content2", expected_content="content2", set_explicit_overwrite_flag=False)

        # Test overwrites explicitly disallowed
        self._SendAndCheckProtocolClientMessage(s3_protocol_client=s3_protocol_client, boto3_s3_client=boto3_s3_client, is_async=True, message_content="content3", expected_content="content2", set_explicit_overwrite_flag=True, overwrite_flag=False)

        # Test overwrites explicitly allowed
        self._SendAndCheckProtocolClientMessage(s3_protocol_client=s3_protocol_client, boto3_s3_client=boto3_s3_client, is_async=True, message_content="content4", expected_content="content4", set_explicit_overwrite_flag=True, overwrite_flag=True)

def run_tests():
    tests = S3ProtocolClientTests()
    tests.Publish()
    tests.PublishAsync()

run_tests()
