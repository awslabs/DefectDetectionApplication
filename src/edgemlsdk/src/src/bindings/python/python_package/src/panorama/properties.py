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

from abc import abstractmethod
from enum import Enum

from panorama import panorama_projections
from panorama import trace
from panorama import unknown
from panorama import apidefs

class PropertyType(Enum):
    INT32 = 0
    BOOL = 1
    FLOAT = 2
    STRING = 3
    UNKNOWN = 4

class PropertyEventHandler(unknown.UnknownImpl, panorama_projections.IPropertyEventHandler):
    def __init__(self, callback):
        panorama_projections.IPropertyEventHandler.__init__(self)
        unknown.UnknownImpl.__init__(self)
        self.uuid = "8E641CCC-678D-4CB6-B648-3841B945B12B"

        self.callback = callback
        trace.debug(f"Creating property event handler [{hex(id(self))}]")

    def __del__(self):
        trace.debug(f"Deleting property event handler [{hex(id(self))}]")

    def OnPropertyChanged(self, property):
        if self.callback is not None:
            self.callback(apidefs.assign(property, lambda x: Property(x)))

class Property(apidefs.BaseProjection):
    """
    Object that contains the value of an input argument provided to your application
    """
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def get_id(self):
        """
        Gets the ID of the property that is used to identify the property in the application

        :return: The id of the property
        :rtype: string
        """
        return self.native_pointer().ID()
    
    def get_type(self):
        """
        :return: The type of of the property
        """
        return PropertyType(self.native_pointer().Type())

    def get_value(self):
        """
        Gets the value of the property.  Depending on the underlying type
        the return type will be different.

        :return: The value of the property
        :rtype: (special): Type specific.  Possible values are string, int, float, and boolean.
        """
        propType = self.native_pointer().Type()

        # Given the I____Property all derive from IProperty and IProperty
        # doesn't actually have the Get() method like it's children
        # cannot simply call Get here as it will cause issues if you call
        # Get() on a IProperty*
        res = self.native_pointer().Buffer()
        apidefs.CHECKHR(res[0])

        # See include/Panorama/properties.h
        ret = None
        if propType == 0:
            ret = res[1].AsInt()
        elif propType == 1:
            ret = res[1].AsBoolean()
        elif propType == 2:
            ret = res[1].AsFloat()
        elif propType == 3:
            ret = res[1].AsString()
        else:
            res[1].Release()
            raise Exception("Unknown data type")

        res[1].Release()
        return ret

    def set_value(self, value):
        """
        Sets the value of the property.  This will raise the Property Changed event

        Args:
            value (special).  The value to assign the property.  The value type must mach the underlying value of the property.  Supported types are string, int, float, and boolean.
        """
        queried = None
        if PropertyType(self.native_pointer().Type()) == PropertyType.INT32:
            apidefs.check_type(value, int)
            queried = panorama_projections.PythonQueryInterfaceIntegerProperty(self.native_pointer())
        elif PropertyType(self.native_pointer().Type()) == PropertyType.FLOAT:
            apidefs.check_type(value, float)
            queried = panorama_projections.PythonQueryInterfaceFloatProperty(self.native_pointer())
        elif PropertyType(self.native_pointer().Type()) == PropertyType.STRING:
            apidefs.check_type(value, str)
            queried = panorama_projections.PythonQueryInterfaceStringProperty(self.native_pointer())
        elif PropertyType(self.native_pointer().Type()) == PropertyType.BOOL:
            apidefs.check_type(value, bool)
            queried = panorama_projections.PythonQueryInterfaceBooleanProperty(self.native_pointer())
        else:
            raise Exception("Not Implemented: Setting of unknown property type")

        apidefs.CHECKHR(queried[0])
        res = queried[1].Set(value)
        apidefs.CHECKHR(res)

    def on_property_changed(self, cb):
        """
        Registers a callback to invoked when the property has changed value
        
        Args:
            callback: Method (lambda or def) to be invoked when the property has changed.  Must be in the form void(Property).
                      If not in the correct format (i.e. incorrect number of arguments) or there is an error in your callback you will likely see the following error:
                      `SWIG director method error. Error detected when calling 'IPropertyEventHandler.Release'`
        """
        handler = PropertyEventHandler(cb)
        panorama_projections.PyObjectAddRef(handler)
        self.native_pointer().AddPropertyEventHandler(handler)

    def uuid():
        # Must equal IProperty UUID
        return "F7012DF9-DED0-4DC4-AFF2-004A0CCBBD08"

class PropertyCollection(apidefs.BaseProjection):
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def count(self):
        return self.native_pointer().Count()

    def at(self, idx: int):
        res = self.native_pointer().At(idx)
        apidefs.CHECKHR(res[0])

        return apidefs.attach(res[1], lambda x: Property(x))

    def contains(self, property: Property):
        return self.native_pointer().Contains(property.native_pointer())

    def contains_key(self, key: str):
        return self.native_pointer().ContainsKey(key)

    def uuid():
        # Must equal IPropertyCollection UUID
        return "050A201F-63A0-4A91-AB55-4F1BA7952359"
    
class PropertyDelegate(apidefs.BaseProjection):
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def get_property(self, id):
        """
        Gets a property from the device application

        Args:
            id: Name of the property
        """
        res = self.native_pointer().GetProperty(id)
        apidefs.CHECKHR(res[0], f"Could not get property '{id}'")
        return apidefs.attach(res[1], lambda x: Property(x))

    def synchronize(self):
        res = self.native_pointer().Synchronize()
        apidefs.CHECKHR(res[0])
        return apidefs.attach(res[1], lambda x: PropertyCollection(x))

    def uuid():
        # Must equal IPropertyDelegate UUID
        return "9103B88F-8478-4AFC-9D3C-577D31A1D019"

def create(id, value):
    res = None
    apidefs.check_type(id, str)
    if type(value) == str:
        res = panorama_projections.CreateStringProperty(id, value)
    elif type(value) == int:
        res = panorama_projections.CreateIntegerProperty(id, value)
    elif type(value) == float:
        res = panorama_projections.CreateFloatProperty(id, value)
    elif type(value) == bool:
        res = panorama_projections.CreateBooleanProperty(id, value)
    else:
        raise Exception(f'Cannot create a property of type {type(value)}.  Valid types are str, int, float, or bool')
    
    if apidefs.SUCCEEDED(res[0]):
        return apidefs.attach(res[1], lambda x: Property(x))
    
def create_file_property_delegate(path: str):
    res = panorama_projections.CreateFilePropertyDelegate(path)
    apidefs.CHECKHR(res[0])
    return PropertyDelegate(res[1])