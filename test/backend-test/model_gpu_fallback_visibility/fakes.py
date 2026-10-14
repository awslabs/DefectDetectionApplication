# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Suite-shared fakes for the model-gpu-fallback-visibility spec suites.

Honesty guard (design "Honesty Guard"): no host test executes real ORT-CUDA,
a GPU, Triton, or Greengrass IPC. CPU fallback is SIMULATED through the fake
``ort`` module here — a controllable ``get_available_providers()`` plus an
``InferenceSession`` fake whose ``get_providers()`` returns a configured
active list and records its call args. The fake must be installed in
``sys.modules`` as ``onnxruntime`` BEFORE ``OnnxRunner`` is constructed
(the runner does ``import onnxruntime as ort`` INSIDE ``__init__``).

Also provided: a fake shadow accessor (for the reporter suites), the awsiot
IPC-module stubbing helper (the established sys.modules-stubbing pattern from
``test_feature_configs_vllm_merge.py``), a temp ``base_model/{version}/{stage}/``
tree builder matching the ``model_convertor._create_base_model_structure``
layout (version dir is a real directory holding model.py +
inference_runtimes.py + config.pbtxt at the base level; the stage dir is a
SYMLINK into a deployed-artifact dir, per ``create_sym_links``), and an
Active_Provider_Record factory matching design Decision 1.
"""
import contextlib
import json
import os
import sys
import types
from unittest.mock import MagicMock

#: Sidecar filename per design Decision 1 (keep in sync with the fix).
ACTIVE_PROVIDER_RECORD = "dda_active_providers.json"

CUDA_EP = "CUDAExecutionProvider"
TRT_EP = "TensorrtExecutionProvider"
CPU_EP = "CPUExecutionProvider"


# ---------------------------------------------------------------------------
# Fake onnxruntime module
# ---------------------------------------------------------------------------

def make_fake_ort(available_providers, active_providers):
    """Build a fake ``onnxruntime`` module.

    ``get_available_providers()`` returns ``available_providers`` (the
    COMPILED-IN set — CUDA stays "available" even when the driver is dead,
    exactly the incident mechanism). ``InferenceSession`` records its call
    args on construction and appends itself to ``module.sessions``; its
    ``get_providers()`` returns ``active_providers`` (the ACTIVE set of the
    created session — CPU-only simulates the silent fallback) and counts its
    calls in ``session.get_providers_calls``.
    """
    ort = types.ModuleType("onnxruntime")
    ort.sessions = []

    class SessionOptions:
        def __init__(self):
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0

    class _FakeInput:
        name = "input"
        type = "tensor(float)"

    class InferenceSession:
        def __init__(self, model_path, sess_options=None, providers=None,
                     **kwargs):
            self.model_path = model_path
            self.sess_options = sess_options
            self.providers = list(providers) if providers is not None else None
            self.kwargs = kwargs
            self.get_providers_calls = 0
            ort.sessions.append(self)

        def get_providers(self):
            self.get_providers_calls += 1
            return list(active_providers)

        def get_inputs(self):
            return [_FakeInput()]

        def run(self, output_names, feed):
            # DLR runner contract: list of raw output tensors.
            return [next(iter(feed.values()))]

    ort.SessionOptions = SessionOptions
    ort.InferenceSession = InferenceSession
    ort.get_available_providers = lambda: list(available_providers)
    return ort


@contextlib.contextmanager
def installed_fake_ort(available_providers, active_providers):
    """Install the fake ort in ``sys.modules['onnxruntime']`` for the block.

    Must wrap ``OnnxRunner(...)`` construction: the runner imports
    onnxruntime lazily inside ``__init__``.
    """
    fake = make_fake_ort(available_providers, active_providers)
    previous = sys.modules.get("onnxruntime")
    sys.modules["onnxruntime"] = fake
    try:
        yield fake
    finally:
        if previous is None:
            sys.modules.pop("onnxruntime", None)
        else:
            sys.modules["onnxruntime"] = previous


# ---------------------------------------------------------------------------
# Fake shadow accessor (IoTShadowAccessor stand-in; real IPC is device-only)
# ---------------------------------------------------------------------------

class FakeShadowAccessor:
    """Records ``update_thing_shadow_state_request`` calls; optionally raises.

    Matches the REAL ``IoTShadowAccessor`` surface (verified at task 3.5):
    the accessor method is ``update_thing_shadow_state_request(thing_name,
    shadow_name, payload)`` and the accessor itself wraps the payload in
    ``{"state": ...}`` — callers pass ``{"reported": document}`` (the
    camera-sync convention). There is no ``update_thing_shadow`` method on
    the real accessor.
    """

    def __init__(self, raise_exc=None):
        self.calls = []
        self.raise_exc = raise_exc

    def update_thing_shadow_state_request(self, thing_name, shadow_name,
                                          payload):
        self.calls.append((thing_name, shadow_name, payload))
        if self.raise_exc is not None:
            raise self.raise_exc


# ---------------------------------------------------------------------------
# Temp Triton model-repo tree builder (model_convertor layout)
# ---------------------------------------------------------------------------

def build_model_tree(repo_dir, model_name, version="1", stage="stage_model",
                     artifact="model.onnx"):
    """Create ``{repo_dir}/base_{model_name}/{version}/{stage}/`` matching
    ``model_convertor._create_base_model_structure``:

    - ``base_{model}/config.pbtxt`` (written last on device; content stub)
    - ``base_{model}/{version}/model.py`` + ``inference_runtimes.py``
    - ``base_{model}/{version}/{stage}`` is a SYMLINK to a real
      deployed-artifact dir (``create_sym_links``) holding the ONNX artifact

    Returns a dict of paths: repo, base, version_dir, stage_dir (the runner's
    ``model_dir``), deployed_dir, artifact.
    """
    base_dir = os.path.join(repo_dir, f"base_{model_name}")
    version_dir = os.path.join(base_dir, str(version))
    os.makedirs(version_dir)
    with open(os.path.join(version_dir, "model.py"), "w") as fh:
        fh.write("# staged lfv_model_template.py stand-in\n")
    with open(os.path.join(version_dir, "inference_runtimes.py"), "w") as fh:
        fh.write("# staged per-model runner copy stand-in\n")
    with open(os.path.join(base_dir, "config.pbtxt"), "w") as fh:
        fh.write('name: "base_%s"\n' % model_name)

    # Deployed Greengrass model artifact dir, symlinked in as the stage dir.
    deployed_dir = os.path.join(repo_dir, "deployed_artifacts", model_name,
                                stage)
    os.makedirs(deployed_dir)
    artifact_path = os.path.join(deployed_dir, artifact)
    with open(artifact_path, "wb") as fh:
        fh.write(b"\x08\x01")  # placeholder bytes; never parsed by the fake
    stage_dir = os.path.join(version_dir, stage)
    os.symlink(deployed_dir, stage_dir)

    return {
        "repo": repo_dir,
        "base": base_dir,
        "version_dir": version_dir,
        "stage_dir": stage_dir,
        "deployed_dir": deployed_dir,
        "artifact": artifact_path,
    }


# ---------------------------------------------------------------------------
# Active_Provider_Record factory (design Decision 1 shape)
# ---------------------------------------------------------------------------

def make_record(model_id, gpu_requested=True, gpu_active=False,
                requested=None, active=None, stage="stage_model",
                updated_at="2026-08-15T20:23:13Z"):
    if requested is None:
        requested = [CUDA_EP, CPU_EP] if gpu_requested else [CPU_EP]
    if active is None:
        active = ([CUDA_EP] if gpu_active else [CPU_EP])
    return {
        "modelId": model_id,
        "runtime": "onnx",
        "stages": {
            stage: {
                "requestedProviders": list(requested),
                "activeProviders": list(active),
                "gpuRequested": gpu_requested,
                "gpuActive": gpu_active,
            }
        },
        "gpuRequested": gpu_requested,
        "gpuActive": gpu_active,
        "updatedAt": updated_at,
    }


def seed_active_provider_record(version_dir, record):
    """Write an Active_Provider_Record sidecar into a version dir."""
    path = os.path.join(version_dir, ACTIVE_PROVIDER_RECORD)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    return path


# ---------------------------------------------------------------------------
# awsiot stubbing importer (test_feature_configs_vllm_merge.py pattern)
# ---------------------------------------------------------------------------

def import_with_awsiot_stubs(module_name):
    """Import a backend module with the runtime-image-only ``awsiot`` modules
    stubbed, then drop the stubs AND the imported module from ``sys.modules``
    so nothing leaks into other test modules. The returned module object
    keeps its own references to the stubs.
    """
    installed = []

    def _register(name, module):
        if name in sys.modules:
            return
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module
            installed.append(name)

    awsiot = types.ModuleType("awsiot")
    ggipc = types.ModuleType("awsiot.greengrasscoreipc")
    ggipc.connect = MagicMock()
    ggipc_model = types.ModuleType("awsiot.greengrasscoreipc.model")
    ggipc_model.ResourceNotFoundError = type(
        "ResourceNotFoundError", (Exception,), {})
    ggipc_model.UnauthorizedError = type("UnauthorizedError", (Exception,), {})
    ggipc_model.GetConfigurationRequest = MagicMock()
    awsiot.greengrasscoreipc = ggipc
    ggipc.model = ggipc_model
    _register("awsiot", awsiot)
    _register("awsiot.greengrasscoreipc", ggipc)
    _register("awsiot.greengrasscoreipc.model", ggipc_model)

    try:
        module = __import__(module_name, fromlist=["_"])
    finally:
        for name in installed:
            sys.modules.pop(name, None)
        if installed:
            sys.modules.pop(module_name, None)
    return module
