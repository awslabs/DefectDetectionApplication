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

import sys

if len(sys.argv[1:]) < 1:
    raise RuntimeError("Please pass in base directory")

base_dir = sys.argv[1]

# include directories of python source code
sys.path.insert(0, f'{base_dir}/lib/python_package/src')
sys.path.insert(0, f'{base_dir}/lib')

print("Begin executing tests")
import device_application_tests
import file_protocol_client_tests
import gst_tests
import message_broker_tests
import python_protocol_client_tests
import query_interface_tests
import trace_tests

# Tests not run in python_tests.cpp
# import sqlite_protocol_client_tests

print("Finished executing tests")
