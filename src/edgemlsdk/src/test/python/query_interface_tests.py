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
import test_utils

def query_interface():
    payload = messagebroker.create_payload_from_string("hello_world")
    queried_payload = payload.query_interface(messagebroker.Payload)
    test_utils.Expect_True(type(queried_payload) is messagebroker.Payload)
    
    no_interface = payload.query_interface(messagebroker.ProtocolMessage)
    test_utils.Expect_True(no_interface == None)

def run_tests():
    query_interface()

run_tests()
