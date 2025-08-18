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

from enum import Enum
import numpy as np

from panorama import panorama_projections
from panorama import apidefs
from panorama.buffer import Buffer
from panorama.vector import Int64Vector

class TensorDataType(Enum):
    BOOL = 0
    UINT8 = 1
    INT8 = 2
    BYTES = 3
    UINT16 = 4
    INT16 = 5
    FP16 = 6
    BF16 = 7
    UINT32 = 8
    INT32 = 9
    FP32 = 10
    UINT64 = 11
    INT64 = 12
    FP64 = 13
    END = 14

def TensorDataType_To_NpType(dt : TensorDataType):
    if dt == TensorDataType.UINT8:
        return np.uint8
    elif dt == TensorDataType.INT8:
        return np.int8
    elif dt == TensorDataType.BYTES:
        return np.char
    elif dt == TensorDataType.UINT16:
        return np.uint16
    elif dt == TensorDataType.INT16:
        return np.int16
    elif dt == TensorDataType.FP16:
        return np.float16
    elif dt == TensorDataType.UINT32:
        return np.uint32
    elif dt == TensorDataType.INT32:
        return np.int32
    elif dt == TensorDataType.FP32:
        return np.float32
    elif dt == TensorDataType.UINT64:
        return np.uint64
    elif dt == TensorDataType.INT64:
        return np.int64
    elif dt == TensorDataType.FP64:
        return np.float64
    else:
        raise Exception("Data type is not supported")
            
def NpType_To_TensorDataType(dt):
    if dt == np.uint8:
        return TensorDataType.UINT8
    elif dt == np.int8:
        return TensorDataType.INT8
    elif dt == np.char:
        return TensorDataType.BYTES
    elif dt == np.uint16:
        return TensorDataType.UINT16
    elif dt == np.int16:
        return TensorDataType.INT16
    elif dt == np.float16:
        return TensorDataType.FP16
    elif dt == np.uint32:
        return TensorDataType.UINT32
    elif dt == np.int32:
        return TensorDataType.INT32
    elif dt == np.float32:
        return TensorDataType.FP32
    elif dt == np.uint64:
        return TensorDataType.UINT64
    elif dt == np.int64:
        return TensorDataType.INT64
    elif dt == np.float64:
        return TensorDataType.FP64
    else:
        raise Exception("Data type is not supported")

class Tensor(apidefs.BaseProjection):
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def uuid():
        # Must equal ITensor
        return "6E7C211A-80F1-47DE-9462-FCCE0A0D1E95"
    
    def name(self) -> str:
        return self.native_pointer().Name()

    def data_type(self):
        return TensorDataType_To_NpType((TensorDataType)(self.native_pointer().DataType()))

    def abstract(self) -> bool:
        return self.native_pointer().Abstract()

    def array(self):
        res = self.native_pointer().Shape()
        apidefs.CHECKHR(res[0])
        vector = apidefs.attach(res[1], lambda x: Int64Vector(x))

        res = self.native_pointer().Buffer()
        apidefs.CHECKHR(res[0])
        buffer = apidefs.attach(res[1], lambda x: Buffer(x))

        if buffer.native_pointer() is None:
            return None

        return buffer.array(self.data_type(), vector.array())

class InferenceRequest(apidefs.BaseProjection):
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def uuid():
        # Must equal IInferenceRequest
        return "EE92B592-AFB6-4EF0-BCB1-7CBCF1985C34"
    
    def wait_for_request_to_complete(self, timeout : int = -1):
        apidefs.CHECKHR(self.native_pointer().WaitForRequestToComplete(timeout))

    def get_input_tensor_index(self, name : str) -> int:
        return self.native_pointer().GetInputTensorIndex(name)

    def get_number_of_input_tensors(self) -> int:
        return self.native_pointer().GetNumOfInputTensors()
    
    def get_input(self, index : int) -> Tensor:
        res = self.native_pointer().Input(index)
        apidefs.CHECKHR(res[0])
        return apidefs.attach(res[1], lambda x: Tensor(x))
    
    def set_input(self, index: int, tensor : Tensor) -> None:
        res = self.native_pointer().SetInput(tensor.native_pointer(), index)
        apidefs.CHECKHR(res)

    def get_output_tensor_index(self, name : str) -> int:
        return self.native_pointer().GetOutputTensorIndex(name)

    def get_number_of_output_tensors(self) -> int:
        return self.native_pointer().GetNumOfOutputTensors()

    def get_output(self, idx: int) -> Tensor:
        res = self.native_pointer().Output(idx)
        apidefs.CHECKHR(res[0])
        return apidefs.attach(res[1], lambda x: Tensor(x))
    
    def move_output(self, idx : int) -> Tensor:
        res = self.native_pointer().MoveOutput(idx)
        apidefs.CHECKHR(res[0])
        return apidefs.attach(res[1], lambda x: Tensor(x))

class InferenceServer(apidefs.BaseProjection):
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def uuid():
        # Must equal IInferenceServer
        return "FF9ADDA7-B127-48A1-9289-03CFF616DE15"

    def load_model(self, model_name : str):
        apidefs.CHECKHR(self.native_pointer().LoadModel(model_name))

    def model_metadata(self, model_name : str) -> str:
        return self.native_pointer().ModelMetadata(model_name)

    def get_model_status(self, model_name: str) ->str:
        return self.native_pointer().GetModelStatus(model_name)

    def list_models(self) -> str:
        return self.native_pointer().ListModels()

    def get_metrics(self) -> str:
        return self.native_pointer().GetMetrics()

    def get_status(self) -> Buffer:
        res = self.native_pointer().GetStatus()
        apidefs.CHECKHR(res[0])
        return apidefs.attach(res[1], lambda x: Buffer(x))

    def process_request(self, request : InferenceRequest):
        apidefs.CHECKHR(self.native_pointer().ProcessRequest(request.native_pointer()))

    def unload_model(self, model_name: str):
        apidefs.CHECKHR(self.native_pointer().UnloadModel(model_name))

class TritonInferenceRequest(InferenceRequest):
    def __init__(self, native):
        InferenceRequest.__init__(self, native)

    def uuid():
        # Must equal ITritonRequest
        return "A07DB75B-A4BA-445F-AC90-DE7219D969A1"
    
class TritonInferenceServer(InferenceServer):
    def __init__(self, native):
        InferenceServer.__init__(self, native)

    def uuid():
        # Must equal ITritonInferenceServer
        return "70CE687C-477B-4760-82A5-7EDDD701E756"

def create_triton_inference_server(model_repo_path : str, triton_server_path : str, unique : bool = False) -> TritonInferenceServer:
    res = panorama_projections.CreateTritonInferenceServer(model_repo_path, triton_server_path, unique)
    apidefs.CHECKHR(res[0])
    return apidefs.attach(res[1], lambda x: TritonInferenceServer(x))

def release_triton_inference_servers():
    panorama_projections.ReleaseTritonInferenceServers()

def create_triton_request(server : TritonInferenceServer, model_name : str) -> TritonInferenceRequest:
    res = panorama_projections.CreateTritonRequest(server.native_pointer(), model_name)
    apidefs.CHECKHR(res[0])
    return apidefs.attach(res[1], lambda x: TritonInferenceRequest(x))


def create_tensor(name : str, shape : list, dataType, data : Buffer) -> Tensor:
    res = panorama_projections.CreateInt64Vector(len(shape))
    apidefs.CHECKHR(res[0])
    vector = apidefs.attach(res[1], lambda x: Int64Vector(x))

    for idx in range(len(shape)):
        vector.native_pointer().Set(shape[idx], idx)

    res = panorama_projections.CreateTensor(name, vector.native_pointer(), int(NpType_To_TensorDataType(dataType).value), data.native_pointer())
    apidefs.CHECKHR(res[0])
    return apidefs.attach(res[1], lambda x: Tensor(x))