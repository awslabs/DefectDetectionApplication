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
import datetime
import json
import os
import ctypes
import logging
import tempfile
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

# --- GPU-fallback visibility (spec: model-gpu-fallback-visibility) ----------
# Keep in sync with dda_triton/provider_visibility.py (the backend-side
# reader) — this per-model runner copy runs inside the Triton python-backend
# stub process and cannot import backend modules.
GPU_PROVIDERS = {"CUDAExecutionProvider", "TensorrtExecutionProvider"}

#: Active_Provider_Record sidecar written into the model VERSION directory.
ACTIVE_PROVIDER_RECORD = "dda_active_providers.json"


def _provider_names(providers):
    """Normalize an ORT provider chain to plain provider-name strings.

    TensorRT rides in the chain as a ``(name, options)`` tuple (see
    ``OnnxRunner.__select_providers``); every other entry is already a plain
    provider-name string.
    """
    return [p[0] if isinstance(p, (tuple, list)) else p
            for p in (providers or [])]


def _write_active_provider_record(model_id, model_dir, stage_record):
    """Atomically merge one stage's provider state into the model's
    Active_Provider_Record sidecar (design Decision 1).

    The runner's ``model_dir`` is the stage subdirectory (a symlink into the
    deployed artifact dir), so the record lands in its parent — the real
    model VERSION directory. Stages initialize sequentially inside one stub
    process, so a plain read-merge-write per stage key is race-free; the
    temp-file + ``os.replace`` write keeps readers from ever seeing a torn
    file.
    """
    version_dir = os.path.dirname(model_dir)
    stage = os.path.basename(model_dir)
    record_path = os.path.join(version_dir, ACTIVE_PROVIDER_RECORD)

    stages = {}
    if os.path.exists(record_path):
        try:
            with open(record_path, encoding="utf-8") as fh:
                existing = json.load(fh)
            if isinstance(existing, dict) and isinstance(
                    existing.get("stages"), dict):
                stages = existing["stages"]
        except (OSError, ValueError):
            # Corrupt/unreadable prior record: start fresh rather than fail
            # the load (the whole visibility block is failure-isolated).
            stages = {}
    stages[stage] = stage_record

    # Model-level aggregate: gpuRequested if ANY stage requested a GPU
    # provider; gpuActive only if EVERY GPU-requesting stage obtained one
    # (a single fallen-back stage degrades the model).
    gpu_stages = [s for s in stages.values() if s.get("gpuRequested")]
    record = {
        "modelId": model_id,
        "runtime": RUNTIME_ONNX,
        "stages": stages,
        "gpuRequested": bool(gpu_stages),
        "gpuActive": bool(gpu_stages) and all(
            s.get("gpuActive") for s in gpu_stages),
        "updatedAt": datetime.datetime.now(datetime.timezone.utc)
                             .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    fd, tmp_path = tempfile.mkstemp(
        dir=version_dir, prefix=".dda_active_providers.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        os.replace(tmp_path, record_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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

        # Set the thread counts explicitly. In a restricted-cpuset container
        # (e.g. Greengrass on Jetson) ORT's default global thread pool tries to
        # pin threads to specific cores and logs
        #   "pthread_setaffinity_np failed ... error code: 22 (Invalid argument)".
        # ORT's own guidance is to specify the thread count so affinity is not
        # set. Non-fatal, but this keeps the logs clean and avoids the pinning.
        sess_options = ort.SessionOptions()
        cpu_count = os.cpu_count() or 1
        sess_options.intra_op_num_threads = cpu_count
        sess_options.inter_op_num_threads = 1

        providers = self.__select_providers(ort, device, model_dir)
        log.info(f"{model_path}: loading ONNX model {model_id} with providers {providers}")
        self.__session = ort.InferenceSession(
            model_path, sess_options=sess_options, providers=providers
        )
        # GPU-fallback visibility (spec: model-gpu-fallback-visibility).
        # Introspection + logging + Active_Provider_Record sidecar, fully
        # failure-isolated: a visibility problem must never fail the load
        # (ORT's CUDA->CPU fallback is a feature; this only makes it VISIBLE).
        try:
            active = list(self.__session.get_providers())
            requested_names = _provider_names(providers)
            gpu_requested = bool(GPU_PROVIDERS & set(requested_names))
            gpu_active = bool(GPU_PROVIDERS & set(active))
            log.info(
                f"{model_path}: ONNX session for {model_id} active "
                f"providers {active}"
            )
            if gpu_requested and not gpu_active:
                log.warning(
                    f"{model_path}: GPU FALLBACK for {model_id} — requested "
                    f"{requested_names} but session is running on {active}; "
                    f"inference will run DEGRADED on CPU until the model is "
                    f"reloaded with a working GPU "
                    f"(spec: model-gpu-fallback-visibility)"
                )
            _write_active_provider_record(
                model_id,
                model_dir,
                {
                    "requestedProviders": requested_names,
                    "activeProviders": active,
                    "gpuRequested": gpu_requested,
                    "gpuActive": gpu_active,
                },
            )
        except Exception as e:
            log.warning(
                f"{model_path}: provider-visibility bookkeeping failed for "
                f"{model_id} (load unaffected): {e}"
            )
        self.__input_name = self.__session.get_inputs()[0].name
        self.__input_dtype = self.__numpy_dtype(self.__session.get_inputs()[0].type)
        log.info(
            f"{model_path}: ONNX init complete for {model_id}; "
            f"input '{self.__input_name}' dtype {self.__input_dtype}"
        )

    @staticmethod
    def __select_providers(ort, device: typing.Optional[str], model_dir: str):
        """Build the ORT execution-provider list for the requested device.

        Default (device unset / "gpu" / "cuda"): CUDA -> CPU.

        TensorRT is deliberately NOT in the default order. It can mis-execute
        complex bring-your-own decode graphs — e.g. YOLOv8's in-graph DFL /
        anchor-grid ops with INT64 weights that TensorRT clamps to INT32 — and
        silently produce wrong or empty results (all class scores below
        threshold => no detections), and it adds a multi-minute engine build.
        The CUDA EP is numerically faithful to the ONNX / CPU reference, so it
        is the safe default for arbitrary user models. TensorRT remains
        available opt-in via manifest ``device: "tensorrt"`` for models the user
        has validated on it.

        Returns a list where a TensorRT entry is a (name, options) tuple and the
        rest are plain provider-name strings.
        """
        available = set(ort.get_available_providers())
        dev = (device or "").lower()

        if dev == "cpu":
            return ["CPUExecutionProvider"]

        def _cuda():
            return ["CUDAExecutionProvider"] if "CUDAExecutionProvider" in available else []

        def _trt():
            if "TensorrtExecutionProvider" not in available:
                return []
            cache_dir = os.path.join(model_dir, "trt_cache")
            try:
                os.makedirs(cache_dir, exist_ok=True)
            except OSError:
                cache_dir = ""
            if cache_dir:
                # Persist the built engine + timing cache so subsequent loads
                # skip the multi-minute TensorRT engine build. (fp16 left at
                # ORT's default to avoid changing inference numerics.)
                return [(
                    "TensorrtExecutionProvider",
                    {
                        "trt_engine_cache_enable": True,
                        "trt_engine_cache_path": cache_dir,
                        "trt_timing_cache_enable": True,
                    },
                )]
            return ["TensorrtExecutionProvider"]

        if dev in ("tensorrt", "trt"):
            chosen = _trt() + _cuda() + ["CPUExecutionProvider"]
        else:  # default, "cuda", "gpu"
            chosen = _cuda() + ["CPUExecutionProvider"]
        return chosen

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
