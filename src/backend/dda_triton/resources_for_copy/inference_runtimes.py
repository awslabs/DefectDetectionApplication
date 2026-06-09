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
"""Pluggable inference runtimes for the DDA Triton Python model.

See docs/multi-runtime-inference.md. The runner contract every engine must
honor (matching the historical DLR runner) is:

    runner(input_np: np.ndarray) -> list[np.ndarray]

The engine is selected per-model by the manifest ``runtime`` field
(``dlr`` default | ``onnx`` | ``pytorch``). Each engine is imported lazily
*inside* its runner so a DLR-only device never imports onnxruntime/torch and a
missing optional dependency only fails models that actually request it.
"""
import os
import ctypes
import logging
import typing
from abc import ABC, abstractmethod

import numpy as np

log = logging.getLogger(__name__)

DLR_DEVICE_TYPE_MAP = {
    1: "cpu",
    2: "gpu",
    4: "opencl",
}

# Recognized manifest runtime identifiers.
RUNTIME_DLR = "dlr"
RUNTIME_ONNX = "onnx"
RUNTIME_PYTORCH = "pytorch"


def load_lib(lib_path):
    """Load a shared library, temporarily adding its directory to PATH so that
    non-system-available dependencies resolve. (Moved verbatim from the DLR
    path in lfv_model_template.py.)
    """
    try:
        path_backup = os.environ["PATH"].split(os.pathsep)
    except KeyError:
        path_backup = []

    try:
        os.environ["PATH"] = os.pathsep.join(path_backup + [os.path.dirname(lib_path)])
        lib = ctypes.cdll.LoadLibrary(lib_path)
    except Exception as e:
        libname = os.path.basename(lib_path)
        raise OSError(
            f"Library ({format(libname)}) could not be loaded. Error message(s): {format(e)}"
        )
    finally:
        os.environ["PATH"] = os.pathsep.join(path_backup)

    return lib


def dlr_device_type(model_path):
    """Determine the device type baked into the model-bundled libdlr.so."""
    # 3rd party import `pip install dlr` from https://pypi.org/project/dlr/
    from dlr.libpath import find_lib_path

    dlr_lib = load_lib(
        find_lib_path(
            model_path,
            use_default_dlr=False,
            logger=log,
        )
    )
    dlr_lib.DLRGetLastError.restype = ctypes.c_char_p
    device_type_id = dlr_lib.GetDLRDeviceType(
        ctypes.c_char_p(model_path.encode()),
    )
    if device_type_id == -1:
        raise RuntimeError(f"Cannot get DLR Device type from the dlr model at: {model_path}.")
    if device_type_id not in DLR_DEVICE_TYPE_MAP:
        raise RuntimeError(
            f"Device type Id: {device_type_id}, got from dlr model, is not supported."
        )
    return DLR_DEVICE_TYPE_MAP[device_type_id]


class BaseInferenceRunner(ABC):
    """Common interface for all inference engines.

    Subclasses load the engine in ``__init__`` and run it in ``__call__``,
    returning a ``list[np.ndarray]`` of raw output tensors (the DLR contract).
    """

    def __init__(self, model_id: str, model_dir: str, device_id: int = 0):
        self.model_id = model_id
        self.model_dir = model_dir
        self.device_id = device_id

    @abstractmethod
    def __call__(self, inference_input: np.ndarray) -> typing.List[np.ndarray]:
        raise NotImplementedError


class DlrRunner(BaseInferenceRunner):
    """SageMaker Neo / DLR engine. Behavior is identical to the historical
    ``_InferenceRunner`` in lfv_model_template.py (logic moved verbatim).
    """

    def __init__(self, model_id: str, model_dir: str, device_id: int = 0):
        super().__init__(model_id, model_dir, device_id)
        self.__model = self.__load_model(model_id, model_dir, device_id)

    def __call__(self, inference_input: np.ndarray) -> typing.List[np.ndarray]:
        return self.__model.run(inference_input)

    @staticmethod
    def __load_model(model_id: str, model_path: str, device_id: int = 0):
        import dlr
        from dlr.counter.phone_home import PhoneHome

        try:
            PhoneHome.disable_feature()
        except OSError as e:
            log.warning("Failed to disable DLR phone home: %s", e.strerror)

        device_type = dlr_device_type(model_path)
        log.info(
            f"{model_path}: Starting loading: dev_type: {device_type}, "
            f"dev_id: {device_id}, model: {model_id}"
        )
        model = dlr.DLRModel(model_path, device_type, device_id)
        log.info(f"{model_path}: Initialization complete for model {model_id}")
        return model


class OnnxRunner(BaseInferenceRunner):
    """ONNX Runtime engine.

    Loads ``model.onnx`` (or ``runtime_artifact``) from the stage dir and runs
    it via onnxruntime. Provider order falls back gracefully:
    TensorRT -> CUDA -> CPU. The single positional input the DLR contract used
    is mapped onto the graph's first declared input name.

    No libdlr.so is loaded, so this engine sidesteps the libjpeg/cudart-version
    collision class of bugs entirely.
    """

    DEFAULT_ARTIFACT = "model.onnx"

    def __init__(
        self,
        model_id: str,
        model_dir: str,
        device_id: int = 0,
        artifact: typing.Optional[str] = None,
        device: typing.Optional[str] = None,
    ):
        super().__init__(model_id, model_dir, device_id)
        import onnxruntime as ort

        model_path = os.path.join(model_dir, artifact or self.DEFAULT_ARTIFACT)
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"ONNX artifact not found: {model_path}")

        providers = self.__select_providers(ort, device)
        log.info(f"{model_path}: loading ONNX model {model_id} with providers {providers}")
        self.__session = ort.InferenceSession(model_path, providers=providers)
        self.__input_name = self.__session.get_inputs()[0].name
        self.__input_dtype = self.__numpy_dtype(self.__session.get_inputs()[0].type)
        log.info(
            f"{model_path}: ONNX init complete for {model_id}; "
            f"input '{self.__input_name}' dtype {self.__input_dtype}"
        )

    @staticmethod
    def __select_providers(ort, device: typing.Optional[str]):
        available = set(ort.get_available_providers())
        if device == "cpu":
            return ["CPUExecutionProvider"]
        preferred = [
            "TensorrtExecutionProvider",
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ]
        chosen = [p for p in preferred if p in available]
        return chosen or ["CPUExecutionProvider"]

    @staticmethod
    def __numpy_dtype(onnx_type: str):
        # e.g. "tensor(float)" -> np.float32
        mapping = {
            "tensor(float)": np.float32,
            "tensor(double)": np.float64,
            "tensor(float16)": np.float16,
            "tensor(uint8)": np.uint8,
            "tensor(int8)": np.int8,
            "tensor(int32)": np.int32,
            "tensor(int64)": np.int64,
        }
        return mapping.get(onnx_type, np.float32)

    def __call__(self, inference_input: np.ndarray) -> typing.List[np.ndarray]:
        feed = {self.__input_name: inference_input.astype(self.__input_dtype, copy=False)}
        outputs = self.__session.run(None, feed)
        return list(outputs)


class TorchRunner(BaseInferenceRunner):
    """Native PyTorch engine (TorchScript preferred).

    Loads ``model.pt`` (or ``runtime_artifact``) via torch.jit.load, falling
    back to torch.load for a pickled nn.Module. Runs under ``no_grad`` on CUDA
    when available.
    """

    DEFAULT_ARTIFACT = "model.pt"

    def __init__(
        self,
        model_id: str,
        model_dir: str,
        device_id: int = 0,
        artifact: typing.Optional[str] = None,
        device: typing.Optional[str] = None,
    ):
        super().__init__(model_id, model_dir, device_id)
        import torch

        self.__torch = torch
        model_path = os.path.join(model_dir, artifact or self.DEFAULT_ARTIFACT)
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"PyTorch artifact not found: {model_path}")

        if device == "cpu" or not torch.cuda.is_available():
            self.__device = torch.device("cpu")
        else:
            self.__device = torch.device(f"cuda:{device_id}")

        try:
            model = torch.jit.load(model_path, map_location=self.__device)
        except Exception:
            model = torch.load(model_path, map_location=self.__device)
        model.eval()
        self.__model = model
        log.info(f"{model_path}: PyTorch init complete for {model_id} on {self.__device}")

    def __call__(self, inference_input: np.ndarray) -> typing.List[np.ndarray]:
        torch = self.__torch
        with torch.no_grad():
            tensor = torch.from_numpy(np.ascontiguousarray(inference_input)).to(self.__device)
            out = self.__model(tensor)
        if isinstance(out, (list, tuple)):
            return [o.detach().cpu().numpy() for o in out]
        return [out.detach().cpu().numpy()]


def make_runner(
    runtime: str,
    model_id: str,
    model_dir: str,
    device_id: int = 0,
    artifact: typing.Optional[str] = None,
    device: typing.Optional[str] = None,
) -> BaseInferenceRunner:
    """Factory: build the inference runner for the requested runtime.

    :param runtime: ``dlr`` (default) | ``onnx`` | ``pytorch``.
    :raises ValueError: for an unknown runtime identifier.
    """
    runtime = (runtime or RUNTIME_DLR).lower()
    if runtime == RUNTIME_DLR:
        return DlrRunner(model_id, model_dir, device_id)
    if runtime == RUNTIME_ONNX:
        return OnnxRunner(model_id, model_dir, device_id, artifact=artifact, device=device)
    if runtime == RUNTIME_PYTORCH:
        return TorchRunner(model_id, model_dir, device_id, artifact=artifact, device=device)
    raise ValueError(
        f"Unknown runtime '{runtime}'. Supported: "
        f"{RUNTIME_DLR}, {RUNTIME_ONNX}, {RUNTIME_PYTORCH}."
    )
