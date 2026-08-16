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
"""Bug-condition exploration tests (Task 1) for model-gpu-fallback-visibility.

Property 1: Bug Condition — Silent CPU Fallback Becomes Visible.

**All four cases assert the FIXED expectation, so they are EXPECTED TO FAIL
on the unfixed tree.** The failures are the counterexamples confirming
defects 1.1-1.3 (the jetson-thor1 Aug 14-15 incident mechanism):

- Case 1 (defect 1.1): a CUDA-requested load whose session comes up CPU-only
  logs NO fallback WARNING — the unfixed ``OnnxRunner.__init__`` never calls
  ``session.get_providers()`` after ``ort.InferenceSession(...)`` (the
  textual fingerprint of the defect), so the fallback is invisible in logs.
- Case 2 (defect 1.1): the same load writes NO ``dda_active_providers.json``
  record into the model VERSION dir — nothing exports the provider state
  from the stub process.
- Case 3 (defect 1.2): ``get_features_triton`` entries never carry
  ``defaultConfiguration.executionProviderInfo`` even with a seeded record —
  a CPU-fallback model's entry is byte-identical to a healthy one.
- Case 4 (defect 1.3): ``dda_triton.provider_visibility.device_gpu_status``
  does not exist — there is no device-level degraded-GPU signal at all.

The SAME suite is re-run in task 3.8 against the fixed tree, where all four
cases must PASS.

Honesty guard: every check is GPU-free and host-runnable. CPU fallback is
SIMULATED via the fake ort's ``get_providers()`` (see fakes.py); no real
ORT, Triton, or Greengrass IPC is exercised.

Validates: Requirements 1.1, 1.2, 1.3
"""
import inspect
import json
import logging
import os
from unittest.mock import MagicMock, patch

import pytest

from model_gpu_fallback_visibility.fakes import (
    ACTIVE_PROVIDER_RECORD,
    CPU_EP,
    CUDA_EP,
    build_model_tree,
    import_with_awsiot_stubs,
    installed_fake_ort,
    make_record,
    seed_active_provider_record,
)

RUNNER_MODULE = "dda_triton.resources_for_copy.inference_runtimes"

MODEL_NAME = "yolo_test"


def _load_fallback_runner(tmp_path, caplog):
    """Construct OnnxRunner under the bug condition: CUDA in the available
    (compiled-in) set, requested chain CUDA→CPU (device unset), but the
    created session comes up CPU-only — the incident's silent fallback,
    simulated through the fake ort. Returns (runner_module, fake_ort, tree).
    """
    import dda_triton.resources_for_copy.inference_runtimes as runners

    tree = build_model_tree(str(tmp_path), MODEL_NAME)
    caplog.set_level(logging.DEBUG, logger=RUNNER_MODULE)
    with installed_fake_ort(
        available_providers=[CUDA_EP, CPU_EP],
        active_providers=[CPU_EP],
    ) as fake_ort:
        runner = runners.OnnxRunner(
            model_id=MODEL_NAME,
            model_dir=tree["stage_dir"],
            device=None,  # default chain: CUDA -> CPU
        )
    # The bug-condition load itself must succeed (fallback is a feature —
    # this is a precondition of C(X), not the assertion under test).
    assert runner is not None
    assert len(fake_ort.sessions) == 1
    session = fake_ort.sessions[0]
    assert session.providers == [CUDA_EP, CPU_EP], (
        "precondition: the requested chain must be CUDA->CPU; got "
        f"{session.providers!r}")
    return runners, fake_ort, tree


# ---------------------------------------------------------------------------
# Case 1 — no fallback WARNING on a CUDA-requested / CPU-only-session load
# (defect 1.1). EXPECTED TO FAIL on the unfixed tree: nothing is logged
# after session creation.
# ---------------------------------------------------------------------------

def test_case1_fallback_load_logs_prominent_warning(tmp_path, caplog):
    """Expected behavior 2.1: a GPU-requested load whose session has no
    active GPU provider logs a prominent WARNING naming the requested chain
    and the active provider(s).

    Validates: Requirements 1.1
    """
    runners, fake_ort, _ = _load_fallback_runner(tmp_path, caplog)
    session = fake_ort.sessions[0]

    init_source = inspect.getsource(runners.OnnxRunner.__init__)
    introspects = "get_providers" in init_source

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    fallback_warnings = [
        r for r in warnings
        if CUDA_EP in r.getMessage() and CPU_EP in r.getMessage()
    ]
    assert fallback_warnings, (
        "COUNTEREXAMPLE (defect 1.1): a CUDA-requested load whose session "
        "came up CPU-only logged NO WARNING naming the requested chain "
        f"({[CUDA_EP, CPU_EP]!r}) and the active provider(s) "
        f"({[CPU_EP]!r}). The unfixed OnnxRunner.__init__ never introspects "
        "the created session: session.get_providers() was called "
        f"{session.get_providers_calls} time(s), and the __init__ body "
        f"{'contains' if introspects else 'CONTAINS NO'} 'get_providers' "
        "between session creation and the input-name read — the textual "
        "fingerprint of the defect. The only log lines emitted were: "
        f"{[r.getMessage() for r in caplog.records]!r} — the pre-session "
        "'loading ONNX model ... with providers [...]' INFO line logs the "
        "REQUESTED chain and nothing is logged about the ACTIVE one, so "
        "silent CPU fallback is indistinguishable from healthy GPU "
        "operation in the stub log (jetson-thor1 Aug 14-15: three such "
        "loads, zero DDA-visible signal)")


# ---------------------------------------------------------------------------
# Case 2 — no Active_Provider_Record written into the VERSION dir
# (defect 1.1). EXPECTED TO FAIL on the unfixed tree: nothing is written.
# ---------------------------------------------------------------------------

def test_case2_fallback_load_writes_active_provider_record(tmp_path, caplog):
    """Expected behavior 2.1/2.2 (Decision 1): the load writes an atomic
    ``dda_active_providers.json`` into the model VERSION dir (the parent of
    the runner's stage dir) with ``gpuRequested: true, gpuActive: false``.

    Validates: Requirements 1.1
    """
    _, _, tree = _load_fallback_runner(tmp_path, caplog)

    record_path = os.path.join(tree["version_dir"], ACTIVE_PROVIDER_RECORD)
    assert os.path.exists(record_path), (
        "COUNTEREXAMPLE (defect 1.1): after a CUDA-requested / CPU-only "
        f"fallback load, no {ACTIVE_PROVIDER_RECORD} record exists in the "
        f"model version dir {tree['version_dir']!r} (the parent of the "
        "runner's stage dir). The stub exports NOTHING about the provider "
        "state, so the backend has no channel to ever learn about the "
        "fallback. Version dir contents: "
        f"{sorted(os.listdir(tree['version_dir']))!r}")

    with open(record_path, encoding="utf-8") as fh:
        record = json.load(fh)
    assert record.get("gpuRequested") is True, (
        f"record {record!r} must carry gpuRequested: true — a GPU provider "
        "was in the requested chain")
    assert record.get("gpuActive") is False, (
        f"record {record!r} must carry gpuActive: false — no GPU provider "
        "is active on the created session")


# ---------------------------------------------------------------------------
# Case 3 — status surface blind: get_features_triton entries carry no
# executionProviderInfo even with a seeded record (defect 1.2).
# EXPECTED TO FAIL on the unfixed tree.
# ---------------------------------------------------------------------------

_DEFAULT_CONFIGS = {
    "modelAlias": MODEL_NAME,
    "modelMetaData": {},
    "modelVersion": "1.0.0",
    "modelConfidenceThresholds": {},
}


def test_case3_feature_config_entry_carries_execution_provider_info(tmp_path):
    """Expected behavior 2.2: with a valid Active_Provider_Record seeded in
    the model's version dir, the model's ``/feature-configurations`` entry
    carries ``defaultConfiguration.executionProviderInfo`` so CPU fallback
    is queryable and distinguishable from GPU operation.

    Fixed-tree contract: the reader resolves records under the module-level
    ``TRITON_MODEL_DIR`` of ``dda_triton.provider_visibility``, which this
    test points at the temp repo. On the unfixed tree the module does not
    exist and the entry lacks the field regardless.

    Validates: Requirements 1.2
    """
    feature_utils = import_with_awsiot_stubs("utils.feature_configs_utils")

    # Seed a valid fallback record where the (fixed) reader will look:
    # TRITON_MODEL_DIR/base_yolo_test/1/dda_active_providers.json
    tree = build_model_tree(str(tmp_path), MODEL_NAME)
    seed_active_provider_record(
        tree["version_dir"],
        make_record(f"base_{MODEL_NAME}", gpu_requested=True,
                    gpu_active=False),
    )

    fake_triton_server = MagicMock()
    fake_triton_server.list_triton_models.return_value = [
        {"model_component": MODEL_NAME, "status": "READY"},
    ]

    # Point the (fixed) provider_visibility reader at the temp repo; absent
    # module (unfixed tree) → nothing to point, the assertion fails below.
    patch_ctx = None
    try:
        import dda_triton.provider_visibility as pv  # noqa: F401
        if hasattr(pv, "TRITON_MODEL_DIR"):
            patch_ctx = patch.object(pv, "TRITON_MODEL_DIR", str(tmp_path))
    except ImportError:
        pass

    with patch.object(feature_utils, "get_default_configs_lfv",
                      return_value=dict(_DEFAULT_CONFIGS)):
        if patch_ctx is not None:
            with patch_ctx:
                results = feature_utils.get_features_triton(
                    fake_triton_server)
        else:
            results = feature_utils.get_features_triton(fake_triton_server)

    assert len(results) == 1 and results[0].modelName == MODEL_NAME
    entry_config = results[0].defaultConfiguration
    assert "executionProviderInfo" in entry_config, (
        "COUNTEREXAMPLE (defect 1.2): with a valid Active_Provider_Record "
        f"(gpuRequested: true, gpuActive: false) seeded in "
        f"{tree['version_dir']!r}, the model's feature-configuration entry "
        "still carries NO defaultConfiguration.executionProviderInfo — the "
        f"entry is {entry_config!r}, byte-identical to a healthy-GPU "
        "model's entry. The status surface has no channel to the stub's "
        "provider state: CPU fallback is indistinguishable from GPU "
        "inference for every API consumer, monitor, and the portal")
    info = entry_config["executionProviderInfo"]
    assert info.get("gpuActive") is False, (
        f"executionProviderInfo {info!r} must reflect the seeded fallback "
        "record (gpuActive: false)")


# ---------------------------------------------------------------------------
# Case 4 — no device-level degraded-GPU signal: the aggregator module does
# not exist (defect 1.3). EXPECTED TO FAIL on the unfixed tree with the
# import error as the counterexample.
# ---------------------------------------------------------------------------

def test_case4_device_gpu_status_reports_degraded_for_all_fallback(tmp_path):
    """Expected behavior 2.4: ``dda_triton.provider_visibility`` exposes
    ``device_gpu_status``, and for an all-fallback record set (the
    jetson-thor1 signature: every GPU-chain model loaded, none holding a
    GPU) it reports ``gpuDegraded: true``.

    Validates: Requirements 1.3
    """
    try:
        import dda_triton.provider_visibility as pv
    except ImportError as e:
        pytest.fail(
            "COUNTEREXAMPLE (defect 1.3): dda_triton.provider_visibility "
            f"does not exist (import error: {e}). There is NO device-level "
            "aggregation anywhere in the tree: with every loaded GPU-chain "
            "ONNX model on CPU fallback (the device-wide outage signature), "
            "the system reports N individually healthy-looking READY models "
            "and no degraded-GPU signal — the outage is only discoverable "
            "outside DDA (kernel logs, empty nvidia-smi)")

    assert hasattr(pv, "device_gpu_status"), (
        "COUNTEREXAMPLE (defect 1.3): dda_triton.provider_visibility exists "
        "but exposes no device_gpu_status aggregator")

    # The incident record set: three GPU-chain models, all fallen back.
    records = {
        name: make_record(f"base_{name}", gpu_requested=True,
                          gpu_active=False)
        for name in ("yolo_test", "rf-detr-seg-nano", "cookies-segmentation")
    }
    statuses = {name: "READY" for name in records}

    status = pv.device_gpu_status(records, statuses)
    assert status.get("gpuDegraded") is True, (
        f"device_gpu_status returned {status!r} for an all-fallback record "
        "set — the device-wide GPU outage signature must surface as "
        "gpuDegraded: true (requirement 2.4)")
