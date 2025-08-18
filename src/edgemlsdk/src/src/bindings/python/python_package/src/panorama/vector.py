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

class Int64Vector(apidefs.BaseProjection):
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def uuid():
        # Must equal IInt64Vector
        return "A60B58FB-C005-4E14-A53C-52FDF5455613"

    def array(self):
        data_pointer = ctypes.cast(self.native_pointer().Data(), ctypes.POINTER(ctypes.c_int64))
        nd_array = np.ctypeslib.as_array(data_pointer,shape=[self.native_pointer().Count()])
        return nd_array