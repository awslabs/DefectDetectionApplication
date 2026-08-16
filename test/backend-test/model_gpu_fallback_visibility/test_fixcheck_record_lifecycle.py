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
"""Fix-checking tests (Task 4.1) for model-gpu-fallback-visibility.

Property 3: Fix Checking — The Record Tracks the Loaded Instance.

Design "Fix Checking" cases 1-4 plus the design "Unit Tests" reader/
normalization legs:

- **Record lifecycle (Property 3 / 2.3)**: a fallback load followed by a
  healthy reload of the SAME model dir transitions the record's
  ``gpuActive`` false→true (and back on a renewed outage load) — each
  ``initialize()`` rewrites the record for the CURRENTLY loaded instance.
- **Atomicity (fix-check case 1)**: the record only ever appears via
  rename; a concurrent reader loop never observes torn/invalid JSON
  during rewrites.
- **Failure isolation (fix-check case 2, 3.1)**: a read-only version dir
  cannot take the record write, yet the load completes with a warning
  noting the failure and the runner still serves inference.
- **Multi-stage aggregation (fix-check case 3)**: two stages, one on GPU
  and one fallen back → model-level ``gpuActive: false`` with both stage
  records present.
- **TRT normalization (fix-check case 4, 3.7)**: a requested chain
  containing the ``("TensorrtExecutionProvider", {...})`` tuple yields
  ``gpuRequested: true``; a CUDA-only active set yields
  ``gpuActive: true`` and NO warning (TRT-requested-CUDA-active is not
  degraded).
- **Unit legs (design Unit Tests)**: version-dir picking (numeric max,
  non-numeric ignored), missing base dir, corrupt/empty JSON, permission
  errors → ``None`` via ``provider_visibility.read_active_provider_record``;
  ``_provider_names`` tuple/string normalization.

Honesty guard: every check is GPU-free and host-runnable — CPU fallback and
GPU health are SIMULATED via the fake ort's ``get_providers()`` (fakes.py);
no real ORT, Triton, or Greengrass IPC is exercised.

Validates: Requirements 2.1, 2.3, 3.1, 3.7
"""
import json
import logging
import os
import stat
import threading
from unittest.mock import patch

import numpy as np
import pytest

import dda_triton.provider_visibility as pv
import dda_triton.resources_for_copy.inference_runtimes as runners
from model_gpu_fallback_visibility.fakes import (
    ACTIVE_PROVIDER_RECORD,
    CPU_EP,
    CUDA_EP,
    TRT_EP,
    build_model_tree,
    installed_fake_ort,
    make_record,
    seed_active_provider_record,
)

RUNNER_MODULE = "dda_triton.resources_for_copy.inference_runtimes"

MODEL_NAME = "yolo_test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_runner(stage_dir, active, available=None, device=None,
                 model_id=MODEL_NAME):
    """Construct OnnxRunner against the fake ort; returns (runner, fake_ort)."""
    if available is None:
        available = [CUDA_EP, CPU_EP]
    with installed_fake_ort(available_providers=available,
                            active_providers=active) as fake_ort:
        runner = runners.OnnxRunner(
            model_id=model_id, model_dir=stage_dir, device=device)
    return runner, fake_ort


def _read_record(version_dir):
    with open(os.path.join(version_dir, ACTIVE_PROVIDER_RECORD),
              encoding="utf-8") as fh:
        return json.load(fh)


def _add_stage(tree, model_name, stage, artifact="model.onnx"):
    """Add a second stage to a build_model_tree() tree the way
    ``model_convertor.create_sym_links`` does: a real deployed-artifact dir
    symlinked into the version dir under the stage name.
    """
    deployed_dir = os.path.join(tree["repo"], "deployed_artifacts",
                                model_name, stage)
    os.makedirs(deployed_dir)
    with open(os.path.join(deployed_dir, artifact), "wb") as fh:
        fh.write(b"\x08\x01")
    stage_dir = os.path.join(tree["version_dir"], stage)
    os.symlink(deployed_dir, stage_dir)
    return stage_dir


# ---------------------------------------------------------------------------
# Record lifecycle (Property 3 / 2.3): each initialize() rewrites the record
# for the CURRENTLY loaded instance — fallback → healthy reload flips
# gpuActive false→true (and a renewed outage load flips it back).
# ---------------------------------------------------------------------------

def test_record_lifecycle_fallback_then_healthy_reload(tmp_path, caplog):
    """A model loaded during an outage reports the fallback until it is
    reloaded; a post-recovery reload of the SAME model dir reports GPU again.

    Validates: Requirements 2.1, 2.3
    """
    caplog.set_level(logging.INFO, logger=RUNNER_MODULE)
    tree = build_model_tree(str(tmp_path), MODEL_NAME)

    # Load 1 — outage: CUDA requested (compiled-in), session came up CPU-only.
    _load_runner(tree["stage_dir"], active=[CPU_EP])
    record1 = _read_record(tree["version_dir"])
    assert record1["gpuRequested"] is True
    assert record1["gpuActive"] is False
    stage1 = record1["stages"]["stage_model"]
    assert stage1["activeProviders"] == [CPU_EP]
    assert stage1["gpuActive"] is False
    # The fallback load logs the prominent WARNING (2.1).
    assert any(r.levelno >= logging.WARNING and "GPU FALLBACK" in
               r.getMessage() for r in caplog.records)

    # Load 2 — recovery reload of the SAME model dir: CUDA active.
    caplog.clear()
    _load_runner(tree["stage_dir"], active=[CUDA_EP, CPU_EP])
    record2 = _read_record(tree["version_dir"])
    assert record2["gpuRequested"] is True
    assert record2["gpuActive"] is True, (
        "post-recovery reload must rewrite the record for the currently "
        f"loaded instance — got {record2!r}")
    assert record2["stages"]["stage_model"]["activeProviders"] == \
        [CUDA_EP, CPU_EP]
    # Healthy reload: INFO active-providers line, NO fallback warning.
    assert any("active" in r.getMessage() and str([CUDA_EP, CPU_EP]) in
               r.getMessage() for r in caplog.records
               if r.levelno == logging.INFO)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    # Load 3 — renewed outage: the record tracks the loaded instance both ways.
    _load_runner(tree["stage_dir"], active=[CPU_EP])
    record3 = _read_record(tree["version_dir"])
    assert record3["gpuActive"] is False


# ---------------------------------------------------------------------------
# Atomicity (design fix-check case 1): the record only ever appears via
# rename — a concurrent reader loop never observes torn/invalid JSON.
# ---------------------------------------------------------------------------

def test_record_rewrites_are_atomic_reader_never_sees_torn_json(tmp_path):
    """A reader polling the record path across many rewrites sees either no
    file (before the first write) or a complete, valid JSON dict — never a
    partial write, because the record lands only via tempfile + os.replace.

    Validates: Requirements 2.3
    """
    tree = build_model_tree(str(tmp_path), MODEL_NAME)
    record_path = os.path.join(tree["version_dir"], ACTIVE_PROVIDER_RECORD)

    stop = threading.Event()
    torn = []          # any content that failed to parse as a dict record
    good_reads = [0]

    def reader():
        while not stop.is_set():
            try:
                with open(record_path, encoding="utf-8") as fh:
                    content = fh.read()
            except FileNotFoundError:
                continue  # not yet appeared — the only other legal state
            try:
                parsed = json.loads(content)
                if not isinstance(parsed, dict) or "stages" not in parsed:
                    torn.append(content)
                else:
                    good_reads[0] += 1
            except ValueError:
                torn.append(content)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for i in range(300):
            gpu_active = (i % 2 == 0)
            runners._write_active_provider_record(
                MODEL_NAME,
                tree["stage_dir"],
                {
                    "requestedProviders": [CUDA_EP, CPU_EP],
                    "activeProviders":
                        [CUDA_EP, CPU_EP] if gpu_active else [CPU_EP],
                    "gpuRequested": True,
                    "gpuActive": gpu_active,
                },
            )
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()

    assert not torn, (
        f"reader observed {len(torn)} torn/invalid record state(s) during "
        f"rewrites — the write is not atomic. First bad content: "
        f"{torn[0]!r}")
    assert good_reads[0] > 0, (
        "vacuous run: the reader never observed a complete record")
    # No temp-file droppings: the write path cleans up after itself.
    leftovers = [e for e in os.listdir(tree["version_dir"])
                 if e.startswith(".dda_active_providers.")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Failure isolation (design fix-check case 2, 3.1): a read-only version dir
# breaks the record write, yet the load completes with a warning and the
# runner serves.
# ---------------------------------------------------------------------------

def test_readonly_version_dir_load_completes_with_warning_and_serves(
        tmp_path, caplog):
    """Visibility never converts a load into a failure: when the record
    cannot be written, a warning notes the failure and inference still works.

    Validates: Requirements 3.1
    """
    if os.geteuid() == 0:
        pytest.skip("running as root: read-only dirs are not enforceable")

    caplog.set_level(logging.INFO, logger=RUNNER_MODULE)
    tree = build_model_tree(str(tmp_path), MODEL_NAME)
    ro_mode = stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP
    os.chmod(tree["version_dir"], ro_mode)
    try:
        # Healthy-GPU load so the ONLY expected warning is the write failure.
        runner, _ = _load_runner(tree["stage_dir"],
                                 active=[CUDA_EP, CPU_EP])
    finally:
        os.chmod(tree["version_dir"],
                 ro_mode | stat.S_IWUSR)  # restore for tmp_path cleanup

    # Load completed and the runner serves inference.
    tensor = np.ones((1, 3, 4, 4), dtype=np.float32)
    outputs = runner(tensor)
    assert isinstance(outputs, list) and len(outputs) == 1
    np.testing.assert_array_equal(outputs[0], tensor)

    # No record could land...
    assert not os.path.exists(
        os.path.join(tree["version_dir"], ACTIVE_PROVIDER_RECORD))
    # ...and a warning notes the visibility failure, nothing else warned.
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the record-write failure must be warned about"
    assert any("provider-visibility bookkeeping failed" in r.getMessage()
               and MODEL_NAME in r.getMessage() for r in warnings)
    assert not any("GPU FALLBACK" in r.getMessage() for r in warnings), (
        "a write failure on a healthy-GPU load must not masquerade as a "
        "GPU fallback")


# ---------------------------------------------------------------------------
# Multi-stage aggregation (design fix-check case 3): one stage on GPU, one
# fallen back → model-level gpuActive false, both stage records present.
# ---------------------------------------------------------------------------

def test_multi_stage_one_fallback_degrades_model_level(tmp_path):
    """A single fallen-back stage degrades the model: gpuActive is true only
    if EVERY GPU-requesting stage obtained a GPU provider.

    Validates: Requirements 2.1, 2.3
    """
    tree = build_model_tree(str(tmp_path), MODEL_NAME, stage="stage_1")
    stage2_dir = _add_stage(tree, MODEL_NAME, "stage_2")

    # Stage 1 holds the GPU; stage 2 fell back to CPU (stages initialize
    # sequentially inside one stub — two loads against the same version dir).
    _load_runner(os.path.join(tree["version_dir"], "stage_1"),
                 active=[CUDA_EP, CPU_EP])
    _load_runner(stage2_dir, active=[CPU_EP])

    record = _read_record(tree["version_dir"])
    assert set(record["stages"]) == {"stage_1", "stage_2"}, (
        f"both stage records must be present — got {record['stages']!r}")
    assert record["stages"]["stage_1"]["gpuActive"] is True
    assert record["stages"]["stage_2"]["gpuActive"] is False
    assert record["gpuRequested"] is True
    assert record["gpuActive"] is False, (
        "model-level gpuActive must be false when any GPU-requesting stage "
        f"fell back — got {record!r}")


# ---------------------------------------------------------------------------
# TRT normalization (design fix-check case 4, 3.7): the (name, options)
# tuple in the requested chain counts as a GPU request; an active CUDA-only
# session satisfies it — gpuActive true, NO warning.
# ---------------------------------------------------------------------------

def test_trt_tuple_chain_cuda_active_is_not_degraded(tmp_path, caplog):
    """device: "tensorrt" builds TRT (as a (name, options) tuple) → CUDA →
    CPU; when the session comes up CUDA-only, "a GPU provider was obtained":
    gpuRequested true, gpuActive true, and no fallback warning.

    Validates: Requirements 3.7
    """
    caplog.set_level(logging.INFO, logger=RUNNER_MODULE)
    tree = build_model_tree(str(tmp_path), MODEL_NAME)

    _, fake_ort = _load_runner(
        tree["stage_dir"],
        active=[CUDA_EP, CPU_EP],
        available=[TRT_EP, CUDA_EP, CPU_EP],
        device="tensorrt",
    )

    # Precondition: the requested chain really carried the TRT tuple.
    session = fake_ort.sessions[0]
    assert isinstance(session.providers[0], tuple)
    assert session.providers[0][0] == TRT_EP
    assert session.providers[1:] == [CUDA_EP, CPU_EP]

    record = _read_record(tree["version_dir"])
    stage = record["stages"]["stage_model"]
    assert stage["requestedProviders"] == [TRT_EP, CUDA_EP, CPU_EP], (
        "the tuple entry must be normalized to its provider name — got "
        f"{stage['requestedProviders']!r}")
    assert record["gpuRequested"] is True
    assert record["gpuActive"] is True, (
        "TRT-requested-CUDA-active means a GPU provider WAS obtained — "
        f"not degraded; got {record!r}")
    assert not any(r.levelno >= logging.WARNING for r in caplog.records), (
        "no warning on a TRT-requested load that obtained CUDA: "
        f"{[r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]!r}")


# ---------------------------------------------------------------------------
# Unit legs (design Unit Tests): read_active_provider_record absence
# tolerance + version picking; _provider_names normalization.
# ---------------------------------------------------------------------------

def test_read_record_picks_numeric_max_ignores_non_numeric(tmp_path):
    """Version-dir picking: highest INTEGER version wins ("10" > "9"),
    non-numeric entries and numeric-named plain FILES are ignored.

    Validates: Requirements 2.3
    """
    base_dir = os.path.join(str(tmp_path), f"base_{MODEL_NAME}")
    for version in ("1", "9", "10", "abc", "v2"):
        os.makedirs(os.path.join(base_dir, version))
    # Numeric-named plain file: isdigit but not a directory — ignored.
    with open(os.path.join(base_dir, "12"), "w") as fh:
        fh.write("not a version dir")

    seed_active_provider_record(
        os.path.join(base_dir, "9"),
        make_record(f"base_{MODEL_NAME}", gpu_requested=True,
                    gpu_active=True, updated_at="2026-08-14T00:00:00Z"))
    expected = make_record(f"base_{MODEL_NAME}", gpu_requested=True,
                           gpu_active=False,
                           updated_at="2026-08-15T00:00:00Z")
    seed_active_provider_record(os.path.join(base_dir, "10"), expected)

    with patch.object(pv, "TRITON_MODEL_DIR", str(tmp_path)):
        record = pv.read_active_provider_record(MODEL_NAME)
    assert record == expected, (
        f"must read version '10' (numeric max), not '9' — got {record!r}")


def test_read_record_missing_base_dir_returns_none(tmp_path):
    """Validates: Requirements 2.3"""
    with patch.object(pv, "TRITON_MODEL_DIR", str(tmp_path)):
        assert pv.read_active_provider_record("no_such_model") is None


@pytest.mark.parametrize("content", [
    "{not json",        # corrupt
    "",                 # empty file
    "{}",               # empty document
], ids=["corrupt-json", "empty-file", "empty-document"])
def test_read_record_corrupt_or_empty_json_returns_none(tmp_path, content):
    """Corrupt or empty sidecar JSON means "no information" (Decision 6).

    Validates: Requirements 2.3
    """
    tree = build_model_tree(str(tmp_path), MODEL_NAME)
    with open(os.path.join(tree["version_dir"], ACTIVE_PROVIDER_RECORD),
              "w", encoding="utf-8") as fh:
        fh.write(content)
    with patch.object(pv, "TRITON_MODEL_DIR", str(tmp_path)):
        assert pv.read_active_provider_record(MODEL_NAME) is None


def test_read_record_permission_error_returns_none(tmp_path):
    """Validates: Requirements 2.3"""
    if os.geteuid() == 0:
        pytest.skip("running as root: file permissions are not enforceable")
    tree = build_model_tree(str(tmp_path), MODEL_NAME)
    record_path = seed_active_provider_record(
        tree["version_dir"], make_record(f"base_{MODEL_NAME}"))
    os.chmod(record_path, 0)
    try:
        with patch.object(pv, "TRITON_MODEL_DIR", str(tmp_path)):
            assert pv.read_active_provider_record(MODEL_NAME) is None
    finally:
        os.chmod(record_path, stat.S_IRUSR | stat.S_IWUSR)


def test_provider_names_tuple_and_string_normalization():
    """``_provider_names`` maps (name, options) tuples (and list-shaped
    entries) to plain names, passes strings through, tolerates None/empty.

    Validates: Requirements 3.7
    """
    assert runners._provider_names(
        [(TRT_EP, {"trt_engine_cache_enable": True}), CUDA_EP, CPU_EP]
    ) == [TRT_EP, CUDA_EP, CPU_EP]
    assert runners._provider_names([[TRT_EP, {}]]) == [TRT_EP]
    assert runners._provider_names([CPU_EP]) == [CPU_EP]
    assert runners._provider_names([]) == []
    assert runners._provider_names(None) == []
