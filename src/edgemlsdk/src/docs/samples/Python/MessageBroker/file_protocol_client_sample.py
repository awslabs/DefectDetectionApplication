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

from panorama import messagebroker
from panorama import credentials
from panorama import trace

import threading

message_published_event = threading.Event()

def OnMessagePublished(protocol, protocol_message, successful):
    if successful:
        trace.info(f"Succesfully published to {protocol}")
    else:
        trace.error(f"Succesfully published to {protocol}")

    message_published_event.set()

def main():
    trace.add_console_trace_listener()

    # Get the credential provider for AWS credentials set in your environment variables
    creds = credentials.create_default_aws_credential_provider()

    # Create the File Protocol Client
    client = messagebroker.FileProtocolClient()

    # Create the payload
    payload = messagebroker.create_payload_from_string("hello world")
    payload.set_correlation_id("my-correlation-id")
    
    # Publish the payload to the client
    dir = "./"
    filename = "${timestamp}-file_name-${c_id}-${id}.txt"

    client.publish_async(payload, dir, filename, OnMessagePublished)
    message_published_event.wait()

if __name__ == "__main__":
    main()