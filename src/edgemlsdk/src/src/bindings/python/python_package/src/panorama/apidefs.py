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

import inspect
from panorama import panorama_projections
from abc import abstractmethod

S_OK=0
S_FALSE=1
E_NOINTERFACE=-2147467262
E_POINTER=-2147467261
E_OUTOFMEMORY=-2147024882
E_HANDLE=-2147024890
E_NOTIMPL=-2147467263
E_INVALIDARG=-2147024809
E_FAIL=-2147467259
E_TIMEOUT=-2147417825
E_OUTOFRANGE=-2147479543
E_INVALID_STATE=-2144796416
E_NOT_FOUND=-2144796415

def ErrorCodeToString(error_code):
    if error_code == E_NOINTERFACE:
        return "No Interface"
    elif error_code == E_POINTER:
        return "Invalid out parameter pointer"
    elif error_code == E_OUTOFMEMORY:
        return "Out of memory"
    elif error_code == E_HANDLE:
        return "Handle invalid"
    elif error_code == E_NOTIMPL:
        return "Not implemented"
    elif error_code == E_INVALIDARG:
        return "Invalid argument"
    elif error_code == E_FAIL:
        return "Generic failure"
    elif error_code == E_TIMEOUT:
        return "Timeout"
    elif error_code == E_NOT_FOUND:
        return "Not Found"
    elif error_code == E_OUTOFRANGE:
        return "Out of range"
    else:
        return "Unknown error"

def SUCCEEDED(hr):
    """
    Checks if a call has succeeded.

    Args:
        hr (int32): The result from a call to panoramasdkv2 C method

    :return: True if hr <= 0.  False otherwise
    :rtype: boolean
    """
    if hr >= 0:
        return True
    else:
        return False

def FAILED(hr):
    """
    Checks if a call has failed.

    Args:
        hr (int32): The result from a call to panoramasdkv2 C method

    :return: True if hr > 0.  False otherwise
    :rtype: boolean
    """
    if hr < 0:
        return True
    else:
        return False

def CHECKHR(hr, message: str = None):
    """
    Raises an exception if :meth:`panoramasdkv2.flowcontrol.SUCCEEDED` returns false

    Args:
        hr (int32): The result from a call to panoramasdkv2 C method
    """
    if FAILED(hr):
        raise Exception(f"Call failed with error `{ErrorCodeToString(hr)}`.  {message}")

class BaseProjection:
    def __init__(self, native):
        self._native = native

        # Not necessary, just will cause a runtime error if uuid has not been implemented
        # So lack of implementation of uuid would be caught in unit test
        # Python doesn't enforce implementation since it's an STATIC method, so an instantiation would not be considered abstract
        type(self).uuid()

    def __del__(self):
        if self._native is not None:
            self._native.Release()
            self._native = None

    def native_pointer(self):
        return self._native
    
    def query_interface(self, target):
        # Default case, native pointer will be of the original type
        ret = panorama_projections.PythonQueryInterface(self._native, target.uuid())
        if FAILED(ret):
            return None
        else:
            return assign(self._native, lambda x: target(x))

    @staticmethod
    @abstractmethod
    def uuid():
        raise NotImplementedError("uuid has not been specified")

def check_type(val, expected_type):
    if isinstance(val, expected_type) == False:
        raise Exception(f"Expected type {expected_type} but got {type(val)} instead")
    
def attach(native_pointer, ctor):
    """
    Attaches a native pointer to object without incremement reference
    """
    return ctor(native_pointer)

def assign(native_pointer, ctor):
    """
    Attaches a native pointer to object and increments the reference count
    """
    obj = attach(native_pointer, ctor)
    obj._native.AddRef()
    return obj
