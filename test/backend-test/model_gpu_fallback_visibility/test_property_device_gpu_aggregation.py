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
"""Device-aggregation property suite (task 4.2) for
model-gpu-fallback-visibility.

Property 4: Fix Checking — Device-Level Aggregation and Portal Display
(device leg, design fix-check case 5 + the design PBT list):

- **Aggregation**: _for any_ generated map of Active_Provider_Records
  (gpuRequested/gpuActive combinations, ``None``s for absent records),
  ``device_gpu_status`` reports ``gpuDegraded`` true IFF at least one
  recorded GPU-chain model exists AND none of them is gpuActive; models
  without records contribute nothing in either direction (Decision 6).
- **Transition logging**: the WARNING fires exactly on ENTERING the
  degraded state and the INFO exactly on recovery — steady state is
  silent. ``provider_visibility._last_gpu_degraded`` is MODULE state, so
  it is reset between Hypothesis examples.
- **Round-trip**: _for any_ requested/active provider list pair, the
  record written by the runner's ``_write_active_provider_record`` reads
  back unchanged through ``read_active_provider_record``, and the shaped
  ``executionProviderInfo`` satisfies
  ``gpuFallback == gpuRequested AND NOT gpuActive``.

Honesty guard: GPU-free, host-runnable — no real ORT/Triton/IPC; the
writer/reader pair is exercised against real temp filesystem trees built
in the ``model_convertor`` layout (suite-shared ``fakes.build_model_tree``).

# Validates: Requirements 2.4
"""
import contextlib
import datetime
import logging
import tempfile

from hypothesis import given, settings, strategies as st
from unittest.mock import patch

from model_gpu_fallback_visibility.fakes import (
    CPU_EP,
    CUDA_EP,
    TRT_EP,
    build_model_tree,
    make_record,
)

import dda_triton.provider_visibility as pv
import dda_triton.resources_for_copy.inference_runtimes as runners


# ---------------------------------------------------------------------------
# Log capture (no caplog: function-scoped fixtures do not reset between
# Hypothesis examples, so records are captured with a handler attached
# directly to the provider_visibility logger per example)
# ---------------------------------------------------------------------------

class _RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def _capture_pv_logs():
    handler = _RecordingHandler()
    previous_level = pv.log.level
    pv.log.addHandler(handler)
    pv.log.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        pv.log.removeHandler(handler)
        pv.log.setLevel(previous_level)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_model_name_st = st.from_regex(r"[a-z][a-z0-9_-]{0,10}", fullmatch=True)

# Writer-reachable (gpu_requested, gpu_active) combinations: the writer
# computes the model-level gpuActive as "every GPU-requesting stage obtained
# a GPU", which is False whenever gpuRequested is False — (False, True) is
# unreachable, so the generator constrains to the real input space.
_record_flags_st = st.sampled_from([(True, True), (True, False),
                                    (False, False)])

# None simulates a model WITHOUT a record (pre-fix runner copy, Decision 6).
_record_spec_st = st.one_of(st.none(), _record_flags_st)

_status_st = st.sampled_from(["READY", "UNAVAILABLE", "LOADING", "UNKNOWN"])

_models_map_st = st.dictionaries(
    _model_name_st, st.tuples(_record_spec_st, _status_st), max_size=6)


def _reset_transition_state():
    pv._last_gpu_degraded = None


# ---------------------------------------------------------------------------
# Aggregation PBT (design fix-check case 5)
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(models=_models_map_st)
def test_property_gpu_degraded_iff_no_recorded_gpu_chain_model_active(models):
    """For any generated map of records (gpuRequested/gpuActive
    combinations, Nones for absent records), ``gpuDegraded`` is true IFF
    at least one recorded GPU-chain model exists AND none of them is
    gpuActive. Absent-record models are excluded entirely and CPU-by-design
    models never count toward the GPU-chain totals — neither can cause nor
    mask degradation.

    # Validates: Requirements 2.4
    """
    records = {}
    statuses = {}
    for name, (spec, status) in models.items():
        statuses[name] = status
        if spec is None:
            records[name] = None
        else:
            gpu_requested, gpu_active = spec
            records[name] = make_record(
                f"base_{name}_1", gpu_requested=gpu_requested,
                gpu_active=gpu_active)

    recorded = {n: r for n, r in records.items() if r is not None}
    chain = [n for n, r in recorded.items() if r["gpuRequested"]]
    active = [n for n in chain if recorded[n]["gpuActive"]]
    expected_degraded = len(chain) > 0 and len(active) == 0

    _reset_transition_state()  # module-level transition state
    try:
        result = pv.device_gpu_status(records, statuses)
    finally:
        _reset_transition_state()

    assert result["gpuDegraded"] == expected_degraded, (
        f"gpuDegraded must be true IFF >=1 recorded GPU-chain model exists "
        f"and none is gpuActive; models={models!r}, got {result!r}")
    assert result["gpuChainModels"] == len(chain)
    assert result["gpuActiveModels"] == len(active)

    # Per-model map: exactly the recorded models (absent records excluded),
    # each carrying status/runtime/flags.
    assert set(result["models"]) == set(recorded)
    for name in recorded:
        assert result["models"][name] == {
            "status": statuses[name],
            "runtime": "onnx",
            "gpuRequested": recorded[name]["gpuRequested"],
            "gpuActive": recorded[name]["gpuActive"],
        }

    # updatedAt in the writer's timestamp format.
    datetime.datetime.strptime(result["updatedAt"], "%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Transition-logging PBT (design fix-check case 5, second half)
# ---------------------------------------------------------------------------

def _records_for_state(degraded, variant):
    """A records map whose aggregation lands in the requested degraded
    state. ``variant`` exercises both non-degraded shapes: a healthy
    GPU-chain model vs no GPU-chain model at all (empty map)."""
    if degraded:
        records = {"m": make_record("base_m_1", gpu_requested=True,
                                    gpu_active=False)}
    elif variant:
        records = {"m": make_record("base_m_1", gpu_requested=True,
                                    gpu_active=True)}
    else:
        records = {}
    return records, {name: "READY" for name in records}


@settings(deadline=None)
@given(steps=st.lists(st.tuples(st.booleans(), st.booleans()),
                      min_size=1, max_size=8))
def test_property_transition_warning_on_entering_info_on_recovery(steps):
    """For any sequence of degraded/non-degraded aggregation states, the
    DEVICE GPU DEGRADED WARNING fires exactly once per ENTERING of the
    degraded state (including from the initial unknown state) and the
    recovery INFO exactly once per degraded->non-degraded transition;
    steady state logs nothing.

    # Validates: Requirements 2.4
    """
    expected_warnings = sum(
        1 for i, (degraded, _) in enumerate(steps)
        if degraded and (i == 0 or not steps[i - 1][0]))
    expected_infos = sum(
        1 for i, (degraded, _) in enumerate(steps)
        if not degraded and i > 0 and steps[i - 1][0])

    _reset_transition_state()
    try:
        with _capture_pv_logs() as records:
            for degraded, variant in steps:
                record_map, statuses = _records_for_state(degraded, variant)
                result = pv.device_gpu_status(record_map, statuses)
                assert result["gpuDegraded"] == degraded
    finally:
        _reset_transition_state()

    warnings = [r for r in records
                if r.levelno == logging.WARNING
                and "DEVICE GPU DEGRADED" in r.getMessage()]
    infos = [r for r in records
             if r.levelno == logging.INFO
             and "recovered" in r.getMessage()]

    assert len(warnings) == expected_warnings, (
        f"WARNING must fire exactly on entering degraded; steps={steps!r}, "
        f"expected {expected_warnings}, got "
        f"{[r.getMessage() for r in warnings]!r}")
    assert len(infos) == expected_infos, (
        f"INFO must fire exactly on recovery; steps={steps!r}, "
        f"expected {expected_infos}, got "
        f"{[r.getMessage() for r in infos]!r}")


# ---------------------------------------------------------------------------
# Record round-trip PBT (design PBT list)
# ---------------------------------------------------------------------------

_PROVIDER_POOL = [CUDA_EP, TRT_EP, CPU_EP, "OpenVINOExecutionProvider"]

_TRT_OPTIONS = {
    "trt_engine_cache_enable": True,
    "trt_engine_cache_path": "/tmp/trt_cache",
    "trt_timing_cache_enable": True,
}


@st.composite
def _requested_and_active(draw):
    """A requested provider chain (TRT optionally in its real
    ``(name, options)`` tuple shape) plus an active list constrained to a
    non-empty subset of the requested names in chain order — a created ORT
    session always reports at least one active provider drawn from its
    requested chain."""
    names = draw(st.lists(st.sampled_from(_PROVIDER_POOL),
                          min_size=1, max_size=4, unique=True))
    requested = []
    for name in names:
        if name == TRT_EP and draw(st.booleans()):
            requested.append((TRT_EP, dict(_TRT_OPTIONS)))
        else:
            requested.append(name)
    flags = draw(st.lists(st.booleans(), min_size=len(names),
                          max_size=len(names)))
    active = [n for n, keep in zip(names, flags) if keep]
    if not active:
        active = [names[-1]]
    return requested, active


@settings(deadline=None)
@given(shape=_requested_and_active(),
       model_name=_model_name_st,
       stage=st.sampled_from(["stage_model", "stage_pre", "stage_post"]))
def test_property_record_round_trip_and_gpu_fallback_derivation(
        shape, model_name, stage):
    """For any requested/active provider list pair, the record written by
    the runner's ``_write_active_provider_record`` round-trips write->read
    UNCHANGED through ``read_active_provider_record`` (stage record
    byte-equal, model-level aggregates per the writer's rule), and the
    shaped ``executionProviderInfo`` satisfies
    ``gpuFallback == gpuRequested AND NOT gpuActive``.

    # Validates: Requirements 2.2, 2.4
    """
    requested, active = shape
    requested_names = runners._provider_names(requested)
    gpu_requested = bool(runners.GPU_PROVIDERS & set(requested_names))
    gpu_active = bool(runners.GPU_PROVIDERS & set(active))
    # The exact stage record the fixed runner writes after introspection.
    stage_record = {
        "requestedProviders": list(requested_names),
        "activeProviders": list(active),
        "gpuRequested": gpu_requested,
        "gpuActive": gpu_active,
    }

    with tempfile.TemporaryDirectory() as repo:
        tree = build_model_tree(repo, model_name, stage=stage)
        runners._write_active_provider_record(
            f"base_{model_name}_1", tree["stage_dir"], stage_record)
        with patch.object(pv, "TRITON_MODEL_DIR", repo):
            record = pv.read_active_provider_record(model_name)

    assert record is not None, "written record must read back"
    # Round-trips unchanged: the stage record exactly as written.
    assert record["stages"] == {stage: stage_record}
    assert record["modelId"] == f"base_{model_name}_1"
    assert record["runtime"] == "onnx"
    # Model-level aggregates per the writer's rule (single stage):
    # gpuRequested = any stage requested; gpuActive = every GPU-requesting
    # stage obtained one (False when nothing requested a GPU).
    assert record["gpuRequested"] == gpu_requested
    assert record["gpuActive"] == (gpu_requested and gpu_active)
    datetime.datetime.strptime(record["updatedAt"], "%Y-%m-%dT%H:%M:%SZ")

    info = pv.execution_provider_info(record)
    assert info["gpuFallback"] == (gpu_requested and not gpu_active), (
        f"gpuFallback must equal gpuRequested AND NOT gpuActive for "
        f"requested={requested!r}, active={active!r}; info={info!r}")
    assert info["requestedProviders"] == list(requested_names)
    assert info["activeProviders"] == list(active)
    assert info["gpuRequested"] == record["gpuRequested"]
    assert info["gpuActive"] == record["gpuActive"]
    assert info["updatedAt"] == record["updatedAt"]
