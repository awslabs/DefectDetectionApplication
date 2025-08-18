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

import ctypes
import numpy as np

from panorama import panorama_projections
from panorama import apidefs

def GetShape(shape, size, bytesPerElem):
    if shape is None:
        return [int(size / bytesPerElem)]
    else:
        if np.prod(shape) * bytesPerElem != size:
            raise Exception("Provided shape is not consistent with buffer size")
        return shape

class Buffer(apidefs.BaseProjection):
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def as_string(self):
        return self.native_pointer().AsString()
    
    def size(self):
        return self.native_pointer().Size()

    def array(self, elem_type, shape=None):
        if elem_type == np.char or elem_type == np.uint8:
            data_pointer = ctypes.cast(self.native_pointer().Data(), ctypes.POINTER(ctypes.c_ubyte))
            shape = GetShape(shape, self.size(), 1)
        elif elem_type == np.int32:
            data_pointer = ctypes.cast(self.native_pointer().Data(), ctypes.POINTER(ctypes.c_int))
            shape = GetShape(shape, self.size(), 4)
        elif elem_type == np.int64:
            data_pointer = ctypes.cast(self.native_pointer().Data(), ctypes.POINTER(ctypes.c_longlong))
            shape = GetShape(shape, self.size(), 8)
        elif elem_type == np.float32:
            data_pointer = ctypes.cast(self.native_pointer().Data(), ctypes.POINTER(ctypes.c_float))
            shape = GetShape(shape, self.size(), 4)
        else:
            raise Exception("Unsupported element type")

        nd_array = np.ctypeslib.as_array(data_pointer,shape=shape)
        return nd_array

    def uuid():
        # Must equal IBuffer UUID
        return "8F904259-6CDE-4C75-8B55-E2828E55345F"

def create_from_string(payload):
    apidefs.check_type(payload, str)
    res = panorama_projections.CreateBufferFromString(payload)
    apidefs.CHECKHR(res[0])
    return apidefs.attach(res[1], lambda x: Buffer(x))

def create(size):
    apidefs.check_type(size, int)
    res = panorama_projections.CreateBuffer(size)
    apidefs.CHECKHR(res[0])
    return apidefs.attach(res[1], lambda x: Buffer(x))

def create_from_bytearray(payload):
    pass