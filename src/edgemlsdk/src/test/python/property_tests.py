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

from panorama import properties
import test_utils
import threading
import os
import json

class StringTests:
    def __init__(self):
        self.value_changed = threading.Event()

    def property_changed(self, property):
        test_utils.Expect_Equal(property.get_value(), "another value")
        self.value_changed.set()

    def run(self):
        # string property
        str_prop = properties.create("str_id", "some value")
        str_prop.on_property_changed(self.property_changed) 
        test_utils.Expect_Equal(str_prop.get_id(), "str_id")
        test_utils.Expect_Equal(str_prop.get_type(), properties.PropertyType.STRING)
        test_utils.Expect_Equal(str_prop.get_value(), "some value")
        str_prop.set_value("another value")
        test_utils.Expect_True(self.value_changed.wait(0))
        test_utils.Expect_Equal(str_prop.get_value(), "another value")
        test_utils.Expect_Exception(lambda: str_prop.set_value(1))

class IntegerTests:
    def __init__(self):
        self.value_changed = threading.Event()

    def property_changed(self, property):
        test_utils.Expect_Equal(property.get_value(), 6)
        self.value_changed.set()

    def run(self):
        # integer property
        int_prop = properties.create("int_id", 5)
        int_prop.on_property_changed(self.property_changed)
        test_utils.Expect_Equal(int_prop.get_id(), "int_id")
        test_utils.Expect_Equal(int_prop.get_type(), properties.PropertyType.INT32)
        test_utils.Expect_Equal(int_prop.get_value(), 5)
        int_prop.set_value(6)
        test_utils.Expect_Equal(int_prop.get_value(), 6)
        test_utils.Expect_Exception(lambda: int_prop.set_value(6.58))

class FloatTests:
    def __init__(self):
        self.value_changed = threading.Event()

    def property_changed(self, property):
        test_utils.Expect_True(abs(property.get_value() - 6.2) < 0.001)
        self.value_changed.set()

    def run(self):
        # float property
        float_prop = properties.create("float_id", 5.2)
        float_prop.on_property_changed(self.property_changed)
        test_utils.Expect_Equal(float_prop.get_id(), "float_id")
        test_utils.Expect_Equal(float_prop.get_type(), properties.PropertyType.FLOAT)
        test_utils.Expect_True(abs(float_prop.get_value() - 5.2) < 0.001)
        float_prop.set_value(6.2)
        test_utils.Expect_True(abs(float_prop.get_value() - 6.2) < 0.001)
        test_utils.Expect_Exception(lambda: float_prop.set_value(False))
    
class BooleanTests:
    def __init__(self):
        self.value_changed = threading.Event()

    def property_changed(self, property):
        test_utils.Expect_Equal(property.get_value(), False)
        self.value_changed.set()

    def run(self):
        # boolean property
        bool_prop = properties.create("bool_id", True)
        bool_prop.on_property_changed(self.property_changed)
        test_utils.Expect_Equal(bool_prop.get_id(), "bool_id")
        test_utils.Expect_Equal(bool_prop.get_type(), properties.PropertyType.BOOL)
        test_utils.Expect_Equal(bool_prop.get_value(), True)
        bool_prop.set_value(False)
        test_utils.Expect_Equal(bool_prop.get_value(), False)
        test_utils.Expect_Exception(lambda: bool_prop.set_value(6.58))

class FilePropertyDelegateTests:
    def __init__(self):
        pass

    def run(self):
        file_path = f"{os.environ['BUILD_DIRECTORY']}/bin/properties.txt"
        with open(file_path, "w") as file:
            dict = {
                "prop1": 1,
                "prop2": "hello world"
            }

            file.write(json.dumps(dict))

        delegate = properties.create_file_property_delegate(file_path)
        prop1 = delegate.get_property("prop1")
        prop2 = delegate.get_property("prop2")

        test_utils.Expect_Exception(lambda: delegate.get_property("doesn'tExist"))
        test_utils.Expect_Equal(1, prop1.get_value())
        test_utils.Expect_Equal(properties.PropertyType.INT32, prop1.get_type())
        test_utils.Expect_Equal("hello world", prop2.get_value())
        test_utils.Expect_Equal(properties.PropertyType.STRING, prop2.get_type())

def run_tests():
    StringTests().run()
    IntegerTests().run()
    FloatTests().run()
    BooleanTests().run()
    FilePropertyDelegateTests().run()

run_tests()