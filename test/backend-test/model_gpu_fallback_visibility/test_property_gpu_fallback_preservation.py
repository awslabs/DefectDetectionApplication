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
"""Preservation property tests (Task 2) for model-gpu-fallback-visibility.

Property 2: Preservation — Everything Outside the Visibility Surface Is
Unchanged. Observation-first: the UNFIXED behavior is captured here as
reference implementations (extracted verbatim from the unfixed source at
task-2 time, 2026-08-16) and encoded as Hypothesis properties that PASS on
the unfixed tree and must KEEP passing after the fix.

- Provider chain identity (3.2, 3.3, 3.7): for any ``device`` value and any
  available-provider set, ``OnnxRunner.__select_providers`` returns exactly
  the chain the pinned reference returns — including the TensorRT
  ``(name, options)`` tuple shape and the trt_cache-dir leg.
- Status payload identity without records (3.4, 3.5): for any generated fake
  Triton model list (base_/marshal_ filtering, vLLM entries included),
  ``get_features_triton`` output with NO sidecar records deep-equals the
  pinned unfixed construction, and entries never carry
  ``executionProviderInfo`` when no record exists.
- Additive-only with records (3.4, 3.5): with records present, removing the
  ``executionProviderInfo`` key restores deep-equality. Skip-as-absent on
  the unfixed tree (``dda_triton.provider_visibility`` does not exist yet);
  BINDS at task 3.9.

Honesty guard: GPU-free, host-runnable; the fake ort simulates provider
availability; no real ORT/Triton/IPC.

# Validates: Requirements 3.2, 3.3, 3.4, 3.5, 3.7
"""
import copy
import os
import tempfile
import types

import pytest
from hypothesis import given, settings, strategies as st
from unittest.mock import MagicMock, patch

from model_gpu_fallback_visibility.fakes import (
    CPU_EP,
    CUDA_EP,
    TRT_EP,
    build_model_tree,
    import_with_awsiot_stubs,
    make_record,
    seed_active_provider_record,
)

import dda_triton.resources_for_copy.inference_runtimes as runners

# Imported ONCE at module scope (the importer pops sys.modules entries so
# nothing leaks; the module object keeps its own bound stubs).
feature_utils = import_with_awsiot_stubs("utils.feature_configs_utils")

from data_models.common import ListFeatureConfigurationAPIModel  # noqa: E402


# ---------------------------------------------------------------------------
# Reference implementation of the UNFIXED OnnxRunner.__select_providers,
# extracted VERBATIM from the unfixed source at task-2 time (2026-08-16).
# The source segment itself is additionally hash-pinned in
# goldens/inference_runtimes_pins.json (see the surface suite). Design
# "Explicitly NOT changed": the fix must leave __select_providers
# byte-identical, so fixed behavior must equal this reference forever.
# ---------------------------------------------------------------------------

def reference_select_providers(available_providers, device, model_dir):
    """Pinned unfixed ``__select_providers`` behavior.

    The trt_cache leg is handled exactly as the unfixed code does: when
    TensorRT is available, ``{model_dir}/trt_cache`` is created with
    ``exist_ok=True`` (deterministic whether the real implementation or this
    reference runs first against the same ``model_dir``); on ``OSError`` the
    TRT entry degrades to the plain provider-name string with no options.
    """
    available = set(available_providers)
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


def select_providers_under_test(available_providers, device, model_dir):
    """Invoke the real (name-mangled) static method against a minimal fake
    ort namespace — the only ort surface the method touches is
    ``get_available_providers()``."""
    fake_ort = types.SimpleNamespace(
        get_available_providers=lambda: list(available_providers))
    return runners.OnnxRunner._OnnxRunner__select_providers(
        fake_ort, device, model_dir)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_KNOWN_DEVICES = ["cpu", "gpu", "cuda", "tensorrt", "trt"]


def _case_mangled(word):
    """Every per-character upper/lower casing of a known device word."""
    return st.lists(
        st.booleans(), min_size=len(word), max_size=len(word)
    ).map(lambda flags: "".join(
        c.upper() if f else c for c, f in zip(word, flags)))


device_st = st.one_of(
    st.none(),
    st.sampled_from(_KNOWN_DEVICES).flatmap(_case_mangled),
    st.text(max_size=12),  # arbitrary junk values fall to the default chain
)

available_st = st.sets(st.sampled_from([
    CUDA_EP, TRT_EP, CPU_EP,
    "OpenVINOExecutionProvider", "AzureExecutionProvider",
])).map(sorted)


# ---------------------------------------------------------------------------
# Provider chain identity (Preservation test plan 1)
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(device=device_st, available=available_st)
def test_property_select_providers_identity(device, available):
    """For any ``device`` value × any available-provider set, the (future
    fixed) ``__select_providers`` returns EXACTLY the chain the pinned
    unfixed reference returns — same entries, same order, same TensorRT
    ``(name, options)`` tuple shape, same trt_cache path handling.

    # Validates: Requirements 3.2, 3.3, 3.7
    """
    with tempfile.TemporaryDirectory() as model_dir:
        actual = select_providers_under_test(available, device, model_dir)
        expected = reference_select_providers(available, device, model_dir)
        assert actual == expected, (
            f"__select_providers diverged from the pinned unfixed behavior "
            f"for device={device!r}, available={available!r}: "
            f"actual={actual!r}, expected={expected!r}")

        # Shape invariants of the unfixed chain, kept explicit: the chain
        # always terminates in the plain CPU provider string, and the ONLY
        # non-string entry ever produced is the TRT (name, options) tuple.
        assert actual[-1] == CPU_EP
        for entry in actual:
            if not isinstance(entry, str):
                name, options = entry
                assert name == TRT_EP
                assert options == {
                    "trt_engine_cache_enable": True,
                    "trt_engine_cache_path": os.path.join(
                        model_dir, "trt_cache"),
                    "trt_timing_cache_enable": True,
                }


# ---------------------------------------------------------------------------
# Status payload identity without records (Preservation test plan 4)
# ---------------------------------------------------------------------------

_model_name_st = st.from_regex(r"[a-z][a-z0-9_-]{0,10}", fullmatch=True)

_triton_entry_st = st.builds(
    lambda prefix, name, status: {"model_component": prefix + name,
                                  "status": status},
    st.sampled_from(["", "", "base_", "marshal_"]),
    _model_name_st,
    st.sampled_from(["READY", "UNAVAILABLE", "LOADING", "UNKNOWN"]),
)

_triton_models_st = st.lists(_triton_entry_st, max_size=6)

_vllm_status_st = st.builds(
    lambda state, reason: types.SimpleNamespace(state=state, reason=reason),
    st.sampled_from(["STAGED", "LOADING", "READY", "FAILED", "WEIRD"]),
    st.one_of(st.none(), st.text(min_size=1, max_size=20)),
)

_vllm_models_st = st.dictionaries(_model_name_st, _vllm_status_st, max_size=3)


def _default_configs(model_id):
    """Deterministic stand-in for the IPC-backed get_default_configs_lfv
    (its no-Greengrass-component defaults shape)."""
    return {
        "modelAlias": model_id,
        "modelMetaData": {},
        "modelVersion": "1.0.0",
        "modelConfidenceThresholds": {},
    }


# Pinned from the unfixed feature_configs_utils (2026-08-16): the vLLM
# status map and entry construction mirrored verbatim.
_VLLM_STATUS_MAP = {
    "STAGED": "LOADING",
    "LOADING": "LOADING",
    "READY": "READY",
    "FAILED": "FAILED",
}


def _reference_vllm_entries(statuses):
    results = []
    for model_name in sorted(statuses):
        status = statuses[model_name]
        state = getattr(status, "state", status)
        state_name = str(getattr(state, "value", state)).upper()
        mapped = _VLLM_STATUS_MAP.get(state_name, state_name)
        reason = getattr(status, "reason", None)
        default_configuration = {"modelAlias": model_name}
        if mapped == "FAILED" and reason:
            default_configuration["failureReason"] = reason
        results.append(ListFeatureConfigurationAPIModel(
            type="VllmModel",
            modelName=model_name,
            status=mapped,
            defaultConfiguration=default_configuration,
        ))
    return results


def reference_get_features_triton(triton_models, vllm_statuses):
    """Pinned unfixed ``get_features_triton`` construction: base_/marshal_
    prefixes filtered, one TritonModel entry per remaining model with the
    default-config dict, then the vLLM entries appended in sorted order."""
    results = []
    for model in triton_models:
        model_id = model.get("model_component")
        if model_id.startswith("base_") or model_id.startswith("marshal_"):
            continue
        results.append(ListFeatureConfigurationAPIModel(
            type="TritonModel",
            modelName=model_id,
            status=model.get("status"),
            defaultConfiguration=_default_configs(model_id),
        ))
    results.extend(_reference_vllm_entries(vllm_statuses))
    return results


def _run_get_features_triton(triton_models, vllm_statuses):
    fake_triton_server = MagicMock()
    fake_triton_server.list_triton_models.return_value = list(triton_models)
    fake_manager = types.SimpleNamespace(
        list_models=lambda: dict(vllm_statuses))
    feature_utils.set_vllm_manager(fake_manager if vllm_statuses else None)
    try:
        with patch.object(feature_utils, "get_default_configs_lfv",
                          new=_default_configs):
            return feature_utils.get_features_triton(fake_triton_server)
    finally:
        feature_utils.set_vllm_manager(None)


@settings(deadline=None)
@given(triton_models=_triton_models_st, vllm_statuses=_vllm_models_st)
def test_property_status_payload_identity_without_records(
        triton_models, vllm_statuses):
    """For any generated fake Triton model list (base_/marshal_ filtering,
    mixed statuses) plus any vLLM manager state, ``get_features_triton``
    output with NO sidecar records present deep-equals the pinned unfixed
    construction — and no entry ever carries ``executionProviderInfo``
    without a record (the additive-only clause's absence half; the fixed
    tree must keep this exactly, per Decision 6: no record → no field).

    # Validates: Requirements 3.4, 3.5
    """
    actual = _run_get_features_triton(triton_models, vllm_statuses)
    expected = reference_get_features_triton(triton_models, vllm_statuses)

    assert [r.model_dump() for r in actual] == \
        [r.model_dump() for r in expected], (
        "get_features_triton output diverged from the pinned unfixed "
        f"construction for triton_models={triton_models!r}, "
        f"vllm_statuses={vllm_statuses!r}")

    for entry in actual:
        assert "executionProviderInfo" not in (
            entry.defaultConfiguration or {}), (
            f"entry {entry.model_dump()!r} carries executionProviderInfo "
            "with NO Active_Provider_Record present — absence must mean "
            "no field (Decision 6)")


# ---------------------------------------------------------------------------
# Additive-only with records present (Preservation test plan 4, second half)
# — SKIP-AS-ABSENT on the unfixed tree; BINDS at task 3.9.
# ---------------------------------------------------------------------------

def _provider_visibility_or_skip():
    try:
        import dda_triton.provider_visibility as pv
        return pv
    except ImportError:
        pytest.skip(
            "dda_triton.provider_visibility absent (unfixed tree) — the "
            "records-present additive-only leg binds at task 3.9")


@settings(deadline=None)
@given(models=st.dictionaries(_model_name_st, st.booleans(),
                              min_size=1, max_size=4))
def test_property_records_present_is_additive_only(models):
    """With Active_Provider_Records present, the ONLY difference in the
    ``get_features_triton`` output is the added ``executionProviderInfo``
    key: removing it from every entry restores deep-equality with the
    no-records output. ``models`` maps model name → gpu_active.

    Skip-as-absent: requires the fixed reader module; binds at task 3.9.

    # Validates: Requirements 3.4, 3.5
    """
    pv = _provider_visibility_or_skip()
    if not hasattr(pv, "TRITON_MODEL_DIR"):
        pytest.skip("provider_visibility has no patchable TRITON_MODEL_DIR")

    triton_models = [{"model_component": name, "status": "READY"}
                     for name in sorted(models)]

    with tempfile.TemporaryDirectory() as repo, \
            tempfile.TemporaryDirectory() as empty_repo:
        for name, gpu_active in models.items():
            tree = build_model_tree(repo, name)
            seed_active_provider_record(
                tree["version_dir"],
                make_record(f"base_{name}", gpu_requested=True,
                            gpu_active=gpu_active))

        with patch.object(pv, "TRITON_MODEL_DIR", repo):
            with_records = _run_get_features_triton(triton_models, {})
        with patch.object(pv, "TRITON_MODEL_DIR", empty_repo):
            without_records = _run_get_features_triton(triton_models, {})

    stripped = []
    for entry in with_records:
        dumped = copy.deepcopy(entry.model_dump())
        (dumped.get("defaultConfiguration") or {}).pop(
            "executionProviderInfo", None)
        stripped.append(dumped)

    assert stripped == [r.model_dump() for r in without_records], (
        "records must be ADDITIVE ONLY: stripping executionProviderInfo "
        "must restore deep-equality with the no-records output; "
        f"models={models!r}")
