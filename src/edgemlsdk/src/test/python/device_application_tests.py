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

from panorama import application
from panorama import trace
from panorama import panorama_projections
from panorama import properties

import test_utils
import threading

class DeviceApplicationTests:
    def __init__(self):
        self.value_changed_evt = threading.Event()
        self.properties_changed_evt = threading.Event()

    def properties_changed(self, collection):
        test_utils.Expect_Equal(len(collection), 1)
        test_utils.Expect_Equal(collection[0].get_value(), "another value")
        self.properties_changed_evt.set()

    def property_changed(self, property):
        test_utils.Expect_Equal(property.get_value(), "another value")
        self.value_changed_evt.set()

    def Properties(self):
        trace.info("======== Properties ===========")
        test_utils.Expect_Exception(lambda: application.create([b"invalid command line"]))
        app = application.create([b"--MyProperty", b"my property"])
        prop = app.get_property("MyProperty")
        test_utils.Expect_Equal(prop.get_value(), "my property")

        # Validate property event handling works from this object
        prop.on_property_changed(self.property_changed)
        prop.set_value("another value")
        test_utils.Expect_True(self.value_changed_evt.wait(0))

    def Credentials(self):
        trace.info("======== CREDENTIALS ===========")
        app = application.create()
        creds = app.get_credentials()
        test_utils.Expect_Equal(creds['access_key'], os.environ.get('AWS_ACCESS_KEY_ID'))
        test_utils.Expect_Equal(creds['secret_key'], os.environ.get('AWS_SECRET_ACCESS_KEY'))
        test_utils.Expect_Equal(creds['token'], os.environ.get('AWS_SESSION_TOKEN'))

    def Synchronize(self):
        trace.info("======== SYNCHRONIZE ===========")

        file1 = "python_synchronize_test_file1.json"
        file2 = "python_synchronize_test_file2.json"

        contents1 = "{\"var1\":\"hello\", \"var2\":\"world\"}"
        contents2 = "{\"var3\":\"foo\", \"var4\":\"bar\"}"

        with open(file1, 'w') as file:
            file.write(contents1)

        with open(file2, 'w') as file:
            file.write(contents2)

        file_prop_delegate1 = properties.create_file_property_delegate(file1)
        file_prop_delegate2 = properties.create_file_property_delegate(file2)

        app = application.create()
        app.add_property_delegate(file_prop_delegate1)
        app.add_property_delegate(file_prop_delegate2)

        contents1 = "{\"var1\":\"hello2\", \"var2\":\"world\"}"
        contents2 = "{\"var3\":\"foo\", \"var4\":\"bar2\"}"

        with open(file1, 'w') as file:
            file.write(contents1)

        with open(file2, 'w') as file:
            file.write(contents2)

        changed_properties = app.synchronize()
        test_utils.Expect_Equal(changed_properties.count(), 2)
        test_utils.Expect_True(changed_properties.contains_key("var1"))
        test_utils.Expect_True(changed_properties.contains_key("var4"))

        prop1 = changed_properties.at(0)
        test_utils.Expect_Equal(prop1.get_value(), "hello2")

        prop2 = changed_properties.at(1)
        test_utils.Expect_Equal(prop2.get_value(), "bar2")

    def Boto3(self):
        trace.info("======== Boto3 ===========")
        # Expects that a SigV4 tokens exist in the environment variables
        # That have permissions to list s3 buckets
        app = application.create()
        session = app.create_boto3_session()
        s3client = session.client('s3')
        test_utils.Expect_True(len(s3client.list_buckets()) > 0)


def run_tests():
    tests = DeviceApplicationTests()
    tests.Properties()
    tests.Credentials()
    tests.Synchronize()
    tests.Boto3()

run_tests()
