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
"""Preservation surface tests (Task 2) for model-gpu-fallback-visibility.

Property 2: Preservation — Everything Outside the Visibility Surface Is
Unchanged. Observation-first baselines captured from the UNFIXED tree
(2026-08-16); every test here PASSES on the unfixed tree and must KEEP
passing after the fix (skip-as-absent legs bind at task 3.9):

- Session-construction identity (3.2): the exact InferenceSession call
  surface (model path, sess_options thread counts, providers list, no extra
  kwargs) is baselined; after the fix, ``get_providers()`` must be the ONLY
  new session interaction.
- Fallback still serves (3.1): a CPU-only-session load completes to a
  WORKING runner — visibility never converts fallback into failure.
- CPU-by-design never flagged (3.3): ``device: "cpu"`` and CUDA-unavailable
  chains are CPU-only with NO warning; the post-fix ``gpuRequested: false``
  record semantics are skip-as-absent.
- TensorRT chain shape (3.7): the (name, options) tuple + trt_cache dir
  handling, including the unwritable-cache-dir degradation leg.
- DLR/Torch untouched (3.5): sha256 pins of the DlrRunner / TorchRunner /
  make_runner / __select_providers source segments
  (goldens/inference_runtimes_pins.json) plus make_runner dispatch behavior.

Honesty guard: GPU-free, host-runnable; CPU fallback is SIMULATED via the
fake ort's ``get_providers()``; no real ORT/Triton/IPC.

# Validates: Requirements 3.1, 3.2, 3.3, 3.5, 3.7
"""
import hashlib
import inspect
import json
import logging
import os

import numpy as np
import pytest

from model_gpu_fallback_visibility.fakes import (
    ACTIVE_PROVIDER_RECORD,
    CPU_EP,
    CUDA_EP,
    TRT_EP,
    build_model_tree,
    installed_fake_ort,
)
from model_gpu_fallback_visibility.test_property_gpu_fallback_preservation import (
    reference_select_providers,
    select_providers_under_test,
)

import dda_triton.resources_for_copy.inference_runtimes as runners

RUNNER_MODULE = "dda_triton.resources_for_copy.inference_runtimes"
MODEL_NAME = "yolo_test"

GOLDENS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "goldens")


# ---------------------------------------------------------------------------
# Session-construction identity (3.2) — Preservation test plan 2
# ---------------------------------------------------------------------------

def _construct_runner(tmp_path, available, active, device=None,
                      record_calls=None):
    """Construct OnnxRunner against the fake ort; optionally instrument the
    session class so every public-method access lands in ``record_calls``."""
    tree = build_model_tree(str(tmp_path), MODEL_NAME)
    with installed_fake_ort(available_providers=available,
                            active_providers=active) as fake_ort:
        if record_calls is not None:
            base_cls = fake_ort.InferenceSession
            data_attrs = {"model_path", "sess_options", "providers",
                          "kwargs", "get_providers_calls"}

            class RecordingSession(base_cls):
                def __getattribute__(self, name):
                    if not name.startswith("_") and name not in data_attrs:
                        record_calls.append(name)
                    return super().__getattribute__(name)

            fake_ort.InferenceSession = RecordingSession
        runner = runners.OnnxRunner(
            model_id=MODEL_NAME,
            model_dir=tree["stage_dir"],
            device=device,
        )
    return runner, fake_ort, tree


def test_session_construction_identity_baseline(tmp_path):
    """UNFIXED baseline of the InferenceSession call surface, asserted
    exactly: one session, positional model path, explicit thread counts
    (intra = cpu_count, inter = 1 — the restricted-cpuset affinity fix),
    the requested CUDA→CPU chain, and NO extra kwargs. The fix must leave
    every one of these construction args byte-identical (3.2).

    # Validates: Requirements 3.2
    """
    _, fake_ort, tree = _construct_runner(
        tmp_path, available=[CUDA_EP, CPU_EP], active=[CPU_EP])

    assert len(fake_ort.sessions) == 1, (
        "exactly ONE InferenceSession construction per load (baseline)")
    session = fake_ort.sessions[0]
    assert session.model_path == os.path.join(tree["stage_dir"],
                                              "model.onnx")
    assert session.sess_options is not None
    assert session.sess_options.intra_op_num_threads == (os.cpu_count() or 1)
    assert session.sess_options.inter_op_num_threads == 1
    assert session.providers == [CUDA_EP, CPU_EP]
    assert session.kwargs == {}, (
        f"unexpected extra InferenceSession kwargs: {session.kwargs!r}")


def test_session_interaction_surface(tmp_path):
    """The unfixed runner touches the created session ONLY through
    ``get_inputs()`` (input name + dtype reads). After the fix, the
    read-only ``get_providers()`` must be the ONLY new session interaction
    — no other new calls, ever (3.2). Passes on both trees.

    # Validates: Requirements 3.2
    """
    calls = []
    _construct_runner(tmp_path, available=[CUDA_EP, CPU_EP],
                      active=[CPU_EP], record_calls=calls)

    assert "get_inputs" in calls, (
        "init must complete through the get_inputs-based input-name read")
    allowed = {"get_inputs", "get_providers"}
    assert set(calls) <= allowed, (
        f"NEW session interaction(s) beyond the unfixed surface + the "
        f"allowed read-only get_providers(): {sorted(set(calls) - allowed)!r}"
        f" (full call sequence: {calls!r})")


# ---------------------------------------------------------------------------
# Fallback still serves (3.1) — Preservation test plan 3
# ---------------------------------------------------------------------------

def test_fallback_load_completes_to_working_runner(tmp_path):
    """A CPU-only-session (fallback) load completes to a WORKING runner:
    construction succeeds, the get_inputs-based init completed (input name
    + dtype resolved), and inference runs through the session. Visibility
    must never convert the fallback into a load failure (3.1).

    # Validates: Requirements 3.1
    """
    runner, fake_ort, _ = _construct_runner(
        tmp_path, available=[CUDA_EP, CPU_EP], active=[CPU_EP])

    assert runner is not None
    frame = np.ones((1, 3, 4, 4), dtype=np.float64)
    outputs = runner(frame)
    assert isinstance(outputs, list) and len(outputs) == 1
    # The fake session echoes the feed value; the runner must have mapped
    # the input onto the graph's declared name and dtype (tensor(float)).
    assert outputs[0].dtype == np.float32
    assert outputs[0].shape == frame.shape


# ---------------------------------------------------------------------------
# CPU-by-design never flagged (3.3) — Preservation test plan 5
# ---------------------------------------------------------------------------

def test_cpu_by_design_chain_is_cpu_only(tmp_path):
    """``device: "cpu"`` (any casing) yields the CPU-only requested chain
    even with every GPU provider compiled in; a CUDA-unavailable set (the
    plain x86 image) yields CPU-only for the default/gpu/cuda devices.
    Checked against the pinned unfixed reference.

    # Validates: Requirements 3.3
    """
    model_dir = str(tmp_path)
    everything = [TRT_EP, CUDA_EP, CPU_EP]
    for device in ("cpu", "CPU", "Cpu"):
        assert select_providers_under_test(everything, device, model_dir) \
            == reference_select_providers(everything, device, model_dir) \
            == [CPU_EP]

    cpu_only_image = [CPU_EP]  # CUDA not compiled in
    for device in (None, "gpu", "cuda", "GPU"):
        assert select_providers_under_test(cpu_only_image, device, model_dir) \
            == reference_select_providers(cpu_only_image, device, model_dir) \
            == [CPU_EP]


def _load_cpu_by_design(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger=RUNNER_MODULE)
    return _construct_runner(
        tmp_path, available=[CUDA_EP, CPU_EP], active=[CPU_EP], device="cpu")


def test_cpu_by_design_load_never_warns(tmp_path, caplog):
    """A CPU-by-design load (manifest ``device: "cpu"``, CUDA compiled in
    but deliberately not requested) logs NO warning — on the unfixed tree
    nothing is logged post-session, and the fixed tree must not flag a
    fallback when no GPU provider was REQUESTED (3.3).

    # Validates: Requirements 3.3
    """
    _, fake_ort, _ = _load_cpu_by_design(tmp_path, caplog)
    assert fake_ort.sessions[0].providers == [CPU_EP], (
        "precondition: CPU-by-design requested chain must be CPU-only")

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, (
        "CPU-by-design load must never warn (no GPU provider was "
        f"requested); got: {[r.getMessage() for r in warnings]!r}")


def test_cpu_by_design_record_semantics(tmp_path, caplog):
    """SKIP-AS-ABSENT (binds at task 3.9): once the fix writes
    Active_Provider_Records, a CPU-by-design load's record must carry
    ``gpuRequested: false`` — computed from the CHOSEN chain, never the
    manifest — so it can never contribute a fallback flag or a degraded
    signal (3.3). On the unfixed tree no record exists → skip.

    # Validates: Requirements 3.3
    """
    _, _, tree = _load_cpu_by_design(tmp_path, caplog)
    record_path = os.path.join(tree["version_dir"], ACTIVE_PROVIDER_RECORD)
    if not os.path.exists(record_path):
        pytest.skip(
            "no Active_Provider_Record written (unfixed tree) — the "
            "gpuRequested:false semantics bind at task 3.9")

    with open(record_path, encoding="utf-8") as fh:
        record = json.load(fh)
    assert record.get("gpuRequested") is False, (
        f"CPU-by-design record {record!r} must carry gpuRequested: false "
        "(computed from the chosen CPU-only chain)")


# ---------------------------------------------------------------------------
# TensorRT opt-in chain shape (3.7) — the (name, options) tuple + trt_cache
# ---------------------------------------------------------------------------

def test_trt_chain_tuple_shape_and_cache_dir(tmp_path):
    """``device: "tensorrt"`` with everything available yields
    TRT-tuple → CUDA → CPU with the engine/timing-cache options pointing at
    ``{model_dir}/trt_cache`` (created); identical to the pinned reference.

    # Validates: Requirements 3.7
    """
    model_dir = str(tmp_path)
    everything = [TRT_EP, CUDA_EP, CPU_EP]
    expected = [
        (TRT_EP, {
            "trt_engine_cache_enable": True,
            "trt_engine_cache_path": os.path.join(model_dir, "trt_cache"),
            "trt_timing_cache_enable": True,
        }),
        CUDA_EP,
        CPU_EP,
    ]
    for device in ("tensorrt", "trt", "TensorRT", "TRT"):
        actual = select_providers_under_test(everything, device, model_dir)
        assert actual == reference_select_providers(
            everything, device, model_dir) == expected
    assert os.path.isdir(os.path.join(model_dir, "trt_cache"))


def test_trt_chain_unwritable_cache_dir_degrades_to_plain_entry(tmp_path):
    """When the trt_cache dir cannot be created (unwritable model dir), the
    TRT entry degrades to the PLAIN provider-name string with no options —
    the unfixed OSError leg, byte-identical to the reference.

    # Validates: Requirements 3.7
    """
    if os.geteuid() == 0:
        pytest.skip("running as root — read-only dir cannot deny makedirs")
    model_dir = os.path.join(str(tmp_path), "readonly_model")
    os.makedirs(model_dir)
    os.chmod(model_dir, 0o555)
    try:
        everything = [TRT_EP, CUDA_EP, CPU_EP]
        actual = select_providers_under_test(everything, "tensorrt",
                                             model_dir)
        expected = reference_select_providers(everything, "tensorrt",
                                              model_dir)
        assert actual == expected == [TRT_EP, CUDA_EP, CPU_EP]
    finally:
        os.chmod(model_dir, 0o755)


# ---------------------------------------------------------------------------
# DLR/Torch untouched (3.5) — Preservation test plan 6: hash pins + dispatch
# ---------------------------------------------------------------------------

def _pins():
    with open(os.path.join(GOLDENS_DIR, "inference_runtimes_pins.json"),
              encoding="utf-8") as fh:
        return json.load(fh)["pins"]


@pytest.mark.parametrize("segment,obj_getter", [
    ("DlrRunner", lambda: runners.DlrRunner),
    ("TorchRunner", lambda: runners.TorchRunner),
    ("make_runner", lambda: runners.make_runner),
    ("OnnxRunner.__select_providers",
     lambda: runners.OnnxRunner._OnnxRunner__select_providers),
])
def test_untouched_source_segments_hash_pins(segment, obj_getter):
    """The DLR and Torch runners, the make_runner factory, and
    ``__select_providers`` are 'Explicitly NOT changed' (design): their
    source segments must match the sha256 pins captured from the unfixed
    tree (goldens/inference_runtimes_pins.json, 2026-08-16).

    # Validates: Requirements 3.5, 3.7
    """
    pin = _pins()[segment]
    src = inspect.getsource(obj_getter())
    actual = hashlib.sha256(src.encode("utf-8")).hexdigest()
    assert actual == pin["sha256"], (
        f"{segment} source segment CHANGED from the pinned unfixed tree "
        f"(sha256 {actual} != golden {pin['sha256']}; "
        f"{len(src.splitlines())} lines vs golden {pin['lines']}). "
        "This segment is out of scope for the fix (requirement 3.5/3.7) — "
        "revert it, or if the change is a deliberate scope amendment, "
        "rebaseline the golden and say so in the task OUTCOME.")


def test_make_runner_dispatch_behavior(monkeypatch):
    """make_runner dispatch on the unfixed tree: case-insensitive runtime
    match, empty/None defaulting to DLR, artifact/device forwarded ONLY to
    the onnx/pytorch engines, and ValueError for unknown runtimes.

    # Validates: Requirements 3.5
    """
    calls = []

    def _stub(name):
        class _Stub:
            def __init__(self, model_id, model_dir, device_id=0,
                         artifact=None, device=None):
                calls.append((name, model_id, model_dir, device_id,
                              artifact, device))
        return _Stub

    monkeypatch.setattr(runners, "DlrRunner", _stub("dlr"))
    monkeypatch.setattr(runners, "OnnxRunner", _stub("onnx"))
    monkeypatch.setattr(runners, "TorchRunner", _stub("torch"))

    # DLR default: None and "" both dispatch to DlrRunner, positional-only
    # (no artifact/device forwarded — the DLR signature has neither).
    for runtime in (None, "", "dlr", "DLR"):
        runners.make_runner(runtime, "m", "/d", 1,
                            artifact="a.bin", device="cpu")
        assert calls.pop() == ("dlr", "m", "/d", 1, None, None)

    for runtime in ("onnx", "ONNX"):
        runners.make_runner(runtime, "m", "/d", 2,
                            artifact="a.onnx", device="tensorrt")
        assert calls.pop() == ("onnx", "m", "/d", 2, "a.onnx", "tensorrt")

    for runtime in ("pytorch", "PyTorch"):
        runners.make_runner(runtime, "m", "/d", 3,
                            artifact="a.pt", device="cpu")
        assert calls.pop() == ("torch", "m", "/d", 3, "a.pt", "cpu")

    with pytest.raises(ValueError):
        runners.make_runner("tensorflow", "m", "/d")
    assert not calls
