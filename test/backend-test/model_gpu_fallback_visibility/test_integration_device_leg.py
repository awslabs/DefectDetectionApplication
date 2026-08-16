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
"""Integration test (Task 4.5, design Integration Tests): the full device
leg through the real staging layout, without Triton.

One temp ``base_{model}/{version}/{stage}/`` tree per model, built the way
``model_convertor._create_base_model_structure`` does (fakes.build_model_tree:
real version dir, stage dir a symlink into a deployed-artifact dir). Then the
REAL chain end to end:

1. the FIXED ``OnnxRunner`` (fake ort) loads inside the tree — the atomic
   Active_Provider_Record lands in the model VERSION dir;
2. ``get_features_triton`` (real ``utils.feature_configs_utils``, awsiot-stub
   import pattern, fake triton_server, ``pv.TRITON_MODEL_DIR`` pointed at the
   temp repo) reads the SAME tree — the entry carries
   ``defaultConfiguration.executionProviderInfo``;
3. ``device_gpu_status`` aggregates the records read back through the real
   ``read_active_provider_record`` — the aggregation reflects the records;
4. the shadow reporter (injectable accessor + ``AWS_IOT_THING_NAME``) emits
   the documented ``dda-model-status`` shadow document (design Decision 4).

Both shapes are covered: all-fallback (degraded) and mixed (non-degraded).

Honesty guard (design "Honesty Guard" + the task NOTE): this test does NOT
claim device truth — no real ORT-CUDA, GPU, Triton, or Greengrass IPC is
exercised; CPU fallback is SIMULATED through the fake ort. The on-hardware
Session A (task 11) is the real integration tier.

Validates: Requirements 2.2, 2.3, 2.4
"""
import logging
import os

import pytest
from unittest.mock import MagicMock, patch

from model_gpu_fallback_visibility.fakes import (
    ACTIVE_PROVIDER_RECORD,
    CPU_EP,
    CUDA_EP,
    FakeShadowAccessor,
    build_model_tree,
    import_with_awsiot_stubs,
    installed_fake_ort,
)

import dda_triton.provider_visibility as pv
import dda_triton.resources_for_copy.inference_runtimes as runners
from utils import model_status_shadow as mss

feature_utils = import_with_awsiot_stubs("utils.feature_configs_utils")

THING_NAME = "jetson-thor1"

#: Documented shadow-document shape (design Decision 4).
DOCUMENT_KEYS = {
    "models", "gpuDegraded", "gpuChainModels", "gpuActiveModels", "updatedAt",
}
MODEL_ENTRY_KEYS = {"status", "runtime", "gpuRequested", "gpuActive"}


@pytest.fixture(autouse=True)
def _clean_state():
    """Neutralize the module-level transition/reporter state around every
    test (the task-4.2/4.3 convention)."""
    pv._last_gpu_degraded = None
    mss._reset_state()
    yield
    thread = mss._write_thread
    if thread is not None:
        thread.join(timeout=5)
    pv._last_gpu_degraded = None
    mss._reset_state()


@pytest.fixture()
def shadow_accessor(monkeypatch):
    """Wire the reporter to a recording fake accessor with a real thing
    name — the injectable-accessor seam, no IPC."""
    monkeypatch.setenv("AWS_IOT_THING_NAME", THING_NAME)
    accessor = FakeShadowAccessor()
    monkeypatch.setattr(mss, "_accessor_override", accessor)
    return accessor


def _load_model(repo, name, active_providers):
    """Build the model_convertor-layout tree for ``name`` and run the FIXED
    OnnxRunner inside it (fake ort; requested chain CUDA->CPU). Returns the
    tree paths dict."""
    tree = build_model_tree(repo, name)
    with installed_fake_ort(
        available_providers=[CUDA_EP, CPU_EP],
        active_providers=active_providers,
    ):
        runner = runners.OnnxRunner(
            model_id=name,
            model_dir=tree["stage_dir"],
            device=None,  # default chain: CUDA -> CPU
        )
    assert runner is not None  # fallback/healthy load always serves
    return tree


def _default_configs(model_id):
    return {
        "modelAlias": model_id,
        "modelMetaData": {},
        "modelVersion": "1.0.0",
        "modelConfidenceThresholds": {},
    }


def _feature_entries(repo, statuses):
    """Run the real ``get_features_triton`` over the temp repo (fake triton
    server listing ``statuses``) and return {modelName: entry}."""
    fake_server = MagicMock()
    fake_server.list_triton_models.return_value = [
        {"model_component": name, "status": status}
        for name, status in statuses.items()
    ]
    with patch.object(feature_utils, "get_default_configs_lfv",
                      side_effect=_default_configs), \
            patch.object(pv, "TRITON_MODEL_DIR", repo):
        results = feature_utils.get_features_triton(fake_server)
    return {entry.modelName: entry
            for entry in results if entry.type == "TritonModel"}


def _aggregate(repo, statuses):
    """Read the records back through the real reader over the SAME tree and
    aggregate — the gpu-status computation shape (design File 4)."""
    with patch.object(pv, "TRITON_MODEL_DIR", repo):
        records = {name: pv.read_active_provider_record(name)
                   for name in statuses}
    return records, pv.device_gpu_status(records, statuses)


def _assert_reported_document(accessor, snapshot):
    """The reporter emitted exactly the documented shadow document."""
    thread = mss._write_thread
    if thread is not None:
        thread.join(timeout=5)
    assert accessor.calls == [
        (THING_NAME, "dda-model-status", {"reported": snapshot})
    ]
    document = accessor.calls[0][2]["reported"]
    assert set(document.keys()) == DOCUMENT_KEYS
    for entry in document["models"].values():
        assert set(entry.keys()) == MODEL_ENTRY_KEYS


# ---------------------------------------------------------------------------
# All-fallback shape — the jetson-thor1 incident signature (degraded)
# ---------------------------------------------------------------------------

def test_all_fallback_full_device_leg_reports_degraded(
        tmp_path, shadow_accessor, caplog):
    """Two GPU-chain models both come up CPU-only (the incident shape):
    records land in the version dirs, both entries carry a gpuFallback
    executionProviderInfo, the aggregation is degraded (with the transition
    WARNING), and the reporter emits the documented degraded document.

    Validates: Requirements 2.2, 2.3, 2.4
    """
    repo = str(tmp_path)
    models = ("yolo_test", "cookies-segmentation")
    trees = {name: _load_model(repo, name, active_providers=[CPU_EP])
             for name in models}

    # 1. The record landed in each model VERSION dir (written by the runner).
    for name, tree in trees.items():
        record_path = os.path.join(tree["version_dir"],
                                   ACTIVE_PROVIDER_RECORD)
        assert os.path.exists(record_path), (
            f"no {ACTIVE_PROVIDER_RECORD} in {tree['version_dir']!r}")

    # 2. The /feature-configurations entries carry executionProviderInfo.
    statuses = {name: "READY" for name in models}
    entries = _feature_entries(repo, statuses)
    assert set(entries) == set(models)
    for name in models:
        info = entries[name].defaultConfiguration["executionProviderInfo"]
        assert info["gpuRequested"] is True
        assert info["gpuActive"] is False
        assert info["gpuFallback"] is True
        assert info["requestedProviders"] == [CUDA_EP, CPU_EP]
        assert info["activeProviders"] == [CPU_EP]

    # 3. The aggregation reflects the records: all-fallback -> degraded,
    #    with the device-level transition WARNING.
    caplog.set_level(logging.INFO, logger=pv.__name__)
    records, snapshot = _aggregate(repo, statuses)
    assert all(records[name] for name in models)  # every record read back
    assert snapshot["gpuDegraded"] is True
    assert snapshot["gpuChainModels"] == 2
    assert snapshot["gpuActiveModels"] == 0
    assert set(snapshot["models"]) == set(models)
    for name in models:
        assert snapshot["models"][name] == {
            "status": "READY",
            "runtime": "onnx",
            "gpuRequested": True,
            "gpuActive": False,
        }
    assert any(
        r.levelno == logging.WARNING and "DEVICE GPU DEGRADED" in r.message
        for r in caplog.records
    ), "entering degraded must log the device-level WARNING"

    # 4. The reporter emits the documented shadow document.
    mss.report(snapshot)
    _assert_reported_document(shadow_accessor, snapshot)


# ---------------------------------------------------------------------------
# Mixed shape — one fallback, one healthy GPU, one record-less model
# (non-degraded)
# ---------------------------------------------------------------------------

def test_mixed_full_device_leg_not_degraded(tmp_path, shadow_accessor):
    """One model falls back, one holds the GPU, one has NO record (a tree
    that never loaded — Decision 6 absence): the entries distinguish the
    three, the aggregation is NOT degraded, and the reporter emits the
    documented non-degraded document.

    Validates: Requirements 2.2, 2.3, 2.4
    """
    repo = str(tmp_path)
    fallback_tree = _load_model(repo, "seg-fallback",
                                active_providers=[CPU_EP])
    healthy_tree = _load_model(repo, "yolo-healthy",
                               active_providers=[CUDA_EP, CPU_EP])
    build_model_tree(repo, "norecord")  # staged but never loaded: no record

    # 1. Records landed for the two loaded models only.
    for tree in (fallback_tree, healthy_tree):
        assert os.path.exists(
            os.path.join(tree["version_dir"], ACTIVE_PROVIDER_RECORD))
    norecord_version = os.path.join(repo, "base_norecord", "1")
    assert not os.path.exists(
        os.path.join(norecord_version, ACTIVE_PROVIDER_RECORD))

    # 2. Entries: fallback vs healthy distinguishable; no record -> no field.
    statuses = {"seg-fallback": "READY", "yolo-healthy": "READY",
                "norecord": "LOADING"}
    entries = _feature_entries(repo, statuses)
    assert set(entries) == set(statuses)
    fallback_info = (entries["seg-fallback"]
                     .defaultConfiguration["executionProviderInfo"])
    assert fallback_info["gpuActive"] is False
    assert fallback_info["gpuFallback"] is True
    healthy_info = (entries["yolo-healthy"]
                    .defaultConfiguration["executionProviderInfo"])
    assert healthy_info["gpuActive"] is True
    assert healthy_info["gpuFallback"] is False
    assert healthy_info["activeProviders"] == [CUDA_EP, CPU_EP]
    assert "executionProviderInfo" not in (
        entries["norecord"].defaultConfiguration), (
        "a record-less model's entry must be signal-free (Decision 6)")

    # 3. Aggregation: one active GPU-chain model -> NOT degraded; the
    #    record-less model is excluded from the per-model map entirely.
    records, snapshot = _aggregate(repo, statuses)
    assert records["norecord"] is None
    assert snapshot["gpuDegraded"] is False
    assert snapshot["gpuChainModels"] == 2
    assert snapshot["gpuActiveModels"] == 1
    assert set(snapshot["models"]) == {"seg-fallback", "yolo-healthy"}
    assert snapshot["models"]["seg-fallback"]["gpuActive"] is False
    assert snapshot["models"]["yolo-healthy"]["gpuActive"] is True

    # 4. The reporter emits the documented non-degraded document.
    mss.report(snapshot)
    _assert_reported_document(shadow_accessor, snapshot)
