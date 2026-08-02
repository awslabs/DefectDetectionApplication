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
"""Verification for the vLLM model-state merge into the feature-config
status (vllm-triton-inference task 12.1; Requirements 4.6, 4.7, 4.10).

Runs without the device container: only the runtime-image-only ``awsiot``
modules are stubbed while ``utils.feature_configs_utils`` is imported
(the established sys.modules-stubbing pattern in this test tree),
everything else is imported for real. The stubs are removed from
``sys.modules`` immediately after the import so they cannot leak into
other test modules collected in the same session (the imported module
keeps its own references).
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


_installed_stub_names = []


def _register_stub(name, module):
    if name in sys.modules:
        return
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = module
        _installed_stub_names.append(name)


_awsiot = types.ModuleType("awsiot")
_awsiot_ggipc = types.ModuleType("awsiot.greengrasscoreipc")
_awsiot_ggipc.connect = MagicMock()
_awsiot_ggipc_model = types.ModuleType("awsiot.greengrasscoreipc.model")
_awsiot_ggipc_model.ResourceNotFoundError = type(
    "ResourceNotFoundError", (Exception,), {}
)
_awsiot_ggipc_model.UnauthorizedError = type("UnauthorizedError", (Exception,), {})
_awsiot_ggipc_model.GetConfigurationRequest = MagicMock()
_awsiot.greengrasscoreipc = _awsiot_ggipc
_awsiot_ggipc.model = _awsiot_ggipc_model
_register_stub("awsiot", _awsiot)
_register_stub("awsiot.greengrasscoreipc", _awsiot_ggipc)
_register_stub("awsiot.greengrasscoreipc.model", _awsiot_ggipc_model)

try:
    import utils.feature_configs_utils as feature_utils  # noqa: E402
    from vllm_runtime import ModelState, ModelStatus  # noqa: E402
finally:
    # Drop only the stubs this module installed so later test modules see
    # the host's real import behavior (clean ModuleNotFoundError, or the
    # real package). feature_utils retains its references to the stubs.
    for _name in _installed_stub_names:
        sys.modules.pop(_name, None)
    # Also drop the module imported under the stubs from the cache: later
    # importers must re-evaluate it against the real environment instead
    # of receiving a copy silently wired to the stubs. This module keeps
    # working through its direct ``feature_utils`` reference.
    if _installed_stub_names:
        sys.modules.pop("utils.feature_configs_utils", None)

_DEFAULT_CONFIGS = {
    "modelAlias": "model1",
    "modelMetaData": {},
    "modelVersion": "1.0.0",
    "modelConfidenceThresholds": {},
}

_TRITON_MODELS = [
    {"model_component": "model1", "status": "READY"},
    {"model_component": "model2", "status": "UNAVAILABLE"},
]


@pytest.fixture(autouse=True)
def _no_manager_leak():
    yield
    feature_utils.set_vllm_manager(None)


def _mock_triton_server():
    server = MagicMock()
    server.list_triton_models.return_value = list(_TRITON_MODELS)
    return server


@patch.object(feature_utils, "get_default_configs_lfv", return_value=_DEFAULT_CONFIGS)
def test_no_manager_installed_output_unchanged(_mock_defaults):
    """vLLM-free images: no manager installed, get_features_triton output is
    identical to pre-feature."""
    result = feature_utils.get_features_triton(_mock_triton_server())
    assert [(r.type, r.modelName, r.status) for r in result] == [
        ("TritonModel", "model1", "READY"),
        ("TritonModel", "model2", "UNAVAILABLE"),
    ]


@patch.object(feature_utils, "get_default_configs_lfv", return_value=_DEFAULT_CONFIGS)
def test_manager_states_merge_beside_triton_entries(_mock_defaults):
    """Manager states map LOADING→LOADING, READY→READY, FAILED→FAILED (reason
    retained) as VllmModel entries beside unaltered Triton vision entries."""
    manager = MagicMock()
    manager.list_models.return_value = {
        "llm-ready": ModelStatus(ModelState.READY),
        "llm-loading": ModelStatus(ModelState.LOADING),
        "llm-failed": ModelStatus(ModelState.FAILED, reason="CUDA out of memory"),
        "llm-staged": ModelStatus(ModelState.STAGED),
    }
    feature_utils.set_vllm_manager(manager)

    result = feature_utils.get_features_triton(_mock_triton_server())

    # Vision entries first, unaltered.
    assert [(r.type, r.modelName, r.status) for r in result[:2]] == [
        ("TritonModel", "model1", "READY"),
        ("TritonModel", "model2", "UNAVAILABLE"),
    ]
    vllm = {r.modelName: r for r in result[2:]}
    assert all(r.type == "VllmModel" for r in vllm.values())
    assert vllm["llm-ready"].status == "READY"
    assert vllm["llm-loading"].status == "LOADING"
    assert vllm["llm-staged"].status == "LOADING"
    assert vllm["llm-failed"].status == "FAILED"
    # The backend failure reason is retained (4.6).
    assert vllm["llm-failed"].defaultConfiguration["failureReason"] == (
        "CUDA out of memory"
    )


def test_get_features_vllm_empty_without_manager():
    assert feature_utils.get_features_vllm() == []
