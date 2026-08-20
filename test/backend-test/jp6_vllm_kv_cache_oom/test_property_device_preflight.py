# Copyright 2026 Amazon Web Services, Inc.
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
"""Fix-checking PROPERTIES for the device memory preflight (spec:
jp6-vllm-kv-cache-oom-regression, task 4.4).

# Validates: Requirements 2.9, 3.8

**Property 5: Bug Condition — Device preflight fails fast and truthfully**
(design "Correctness Properties"). _For any_ injected memory reading and
staged args where the requirement exceeds either the measured available
memory or the device-computed budget, the fixed manager refuses BEFORE
engine construction with a reason carrying the ``preflight-refused:``
marker, the measured available bytes, the computed requirement with its
terms, and the specific setting to change; and the prep classifies that
outcome as ``LOAD_PREFLIGHT_REFUSED``, skips the KV-OOM unload -> reload
recovery, and exits 0 while every other classification keeps its current
exit code.

Properties in this file:
  P5-A **Refuse iff the requirement exceeds the measured minimum** [2.9] —
       over generated ``/proc/meminfo`` readings x staged args,
       ``evaluate_device_fit`` refuses **iff**
       ``required > min(reading.available, util x reading.total)``.
  P5-B **The refusal reason is a complete diagnostic** [2.9] — it starts
       with ``preflight-refused:``, names the measured available bytes,
       the computed requirement WITH its terms (weights + activation
       allowance, labelled an ESTIMATE, + KV floor), and the specific
       engine settings to change.
  P5-C **Undeterminable weights degrade honestly** [2.9] — the verdict is
       marked ``unverified`` and rests on the documented
       ``ACTIVATION_FLOOR + KV floor`` LOWER BOUND, never a guessed weight.
  P5-D **On an enforced refusal the engine factory is never called**
       [2.9] — at the manager level, with the weights sizable on disk, a
       doomed load FAILS with the marked reason and ZERO engine
       constructions; a load the arithmetic admits constructs exactly one.
  P5-E **The prep classifies the refusal BEFORE the KV markers** [2.9,
       3.8] — a refusal body legitimately contains the string
       ``gpu_memory_utilization`` (and may even embed the full KV-OOM
       sentence); it must return ``LOAD_PREFLIGHT_REFUSED`` after exactly
       ONE load request — never the unload -> reload recovery.
  P5-F **``prepare()`` exits 0 on a preflight refusal, loudly** [2.9,
       3.8] — the prominent ERROR carries the full diagnostic verbatim.
  P5-G **Only a never-reachable runtime fails the component** [3.8] —
       ``LOAD_OK`` -> 0 and ``LOAD_UNREACHABLE`` -> 1 unchanged;
       ``LOAD_HTTP_ERROR`` -> **0** as of the task 14 H11 dispatch (the
       verbatim original mapping is recorded at the test).
  P5-H **The duplicated marker stays in lockstep** [3.8] —
       ``vllm_model_prep.PREFLIGHT_REFUSED_MARKER`` equals
       ``memory_budget.PREFLIGHT_REFUSED_MARKER``.

HONESTY GUARD (binding, design "Honesty Guard"). This file proves the
**decision logic and classification** over INJECTED readings only: the
"device" is crafted ``/proc/meminfo`` text, the engine is the manager's
public ``engine_factory`` seam, weights are sparse files, and the prep's
HTTP layer is a monkeypatched ``requests``. That the refusal is actually
fast on device, that the runtime server stays responsive, and that the
Greengrass deployment succeeds are **[HARDWARE] H3** claims, owned by
task 11 — no assertion here claims them.

Hypothesis conventions for the device suites (``--noconftest``, so no
profile is registered): ``@settings(deadline=None)`` with **no hardcoded
``max_examples``**, matching the sibling device suites.

Run (host-side, from the repo root):
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
      test/backend-test/jp6_vllm_kv_cache_oom/test_property_device_preflight.py \
      -q -p no:cacheprovider --noconftest

_Requirements: 2.9, 3.8_
"""
import argparse
import asyncio
import itertools
import json
import logging

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import dda_triton.vllm_model_prep as mp
import vllm_runtime.memory_budget as mb
from vllm_runtime.manager import ModelState
from jp6_vllm_kv_cache_oom.fakes import (
    DEFAULT_MODEL_NAME,
    GIB,
    KV_OOM_REASON,
    FakeMeminfoReader,
    RecordingEngineFactory,
    build_staged_repo,
    make_manager,
    weight_tree,
)

MIB = 1024 ** 2

#: Per-example unique directory names (tmp_path is function-scoped while
#: Hypothesis re-enters the test body many times).
_dir_counter = itertools.count()


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
# Everything is generated in whole MiB so the kB rendering of the fake
# /proc/meminfo round-trips EXACTLY (meminfo_text writes kB; the parser
# multiplies back) — the oracle below must see the same numbers the code
# under test sees.

_utilizations = st.floats(min_value=0.05, max_value=1.0,
                          allow_nan=False, allow_infinity=False
                          ).map(lambda x: round(x, 3))


@st.composite
def preflight_inputs(draw):
    """A generated /proc/meminfo reading x staged args x weights: totals
    from small boards to Thor class, availability anywhere up to the total,
    weights from tiny to oversized — so BOTH verdicts are exercised."""
    total_mib = draw(st.integers(min_value=8 * 1024, max_value=128 * 1024))
    available_mib = draw(st.integers(min_value=64, max_value=total_mib))
    util = draw(_utilizations)
    weights_bytes = draw(st.integers(min_value=1, max_value=40 * 1024)) * MIB
    args = {
        "model": "example/generated-model",
        "gpu_memory_utilization": util,
    }
    if draw(st.booleans()):
        args["max_model_len"] = draw(st.integers(min_value=256,
                                                 max_value=32768))
    # BOTH authoring shapes, with and without the `video` bound: the device
    # now sizes from TOTAL multimodal units like the portal, so an unauthored
    # `video` costs a full extra unit (task 14 / H8+H9; the mirror previously
    # counted images only).
    #
    # SUPERSEDED, recorded verbatim:
    #     images = 1
    #     if draw(st.booleans()):
    #         images = draw(st.integers(min_value=1, max_value=4))
    #         args["limit_mm_per_prompt"] = {"image": images}
    #     return (total_mib * MIB, available_mib * MIB, util, images,
    #             weights_bytes, args)
    images = 1
    videos = mb.DEFAULT_VIDEOS_PER_PROMPT  # unauthored: vLLM's own default
    if draw(st.booleans()):
        images = draw(st.integers(min_value=1, max_value=4))
        limit = {"image": images}
        if draw(st.booleans()):
            videos = draw(st.integers(min_value=0, max_value=2))
            limit["video"] = videos
        args["limit_mm_per_prompt"] = limit
    return (total_mib * MIB, available_mib * MIB, util, images + videos,
            weights_bytes, args)


@st.composite
def refusing_inputs(draw):
    """Inputs constrained to a GUARANTEED refusal (weights alone dwarf the
    availability), for the message-content property."""
    total_mib = draw(st.integers(min_value=8 * 1024, max_value=64 * 1024))
    available_mib = draw(st.integers(min_value=64, max_value=2 * 1024))
    util = draw(_utilizations)
    weights_bytes = draw(st.integers(min_value=4 * 1024,
                                     max_value=20 * 1024)) * MIB
    args = {
        "model": "example/generated-model",
        "gpu_memory_utilization": util,
        "max_model_len": draw(st.integers(min_value=256, max_value=32768)),
    }
    images = 1
    videos = mb.DEFAULT_VIDEOS_PER_PROMPT
    if draw(st.booleans()):
        images = draw(st.integers(min_value=1, max_value=4))
        limit = {"image": images}
        if draw(st.booleans()):
            videos = draw(st.integers(min_value=0, max_value=2))
            limit["video"] = videos
        args["limit_mm_per_prompt"] = limit
    return (total_mib * MIB, available_mib * MIB, util, images + videos,
            weights_bytes, args)


def _required(weights_bytes, units):
    """The design's requirement, composed from the module's own CONSTANTS (not
    from ``mb.required_bytes``, so a drift in that function is still caught):
    weights + non-torch allowance + activation allowance + KV VIABILITY floor.

    REPOINTED 2026-08-19 (spec task 14 / H8 + H9). SUPERSEDED ORACLE, recorded
    verbatim before the change::

        def _required(weights_bytes, images):
            \"\"\"The design's requirement, composed from the module's own
            terms: weights + activation allowance + KV floor.\"\"\"
            return (weights_bytes
                    + mb.activation_allowance(weights_bytes, images)
                    + mb.MINIMUM_KV_CACHE_BYTES)

    Two deliberate changes, neither a weakening: ``NON_TORCH_ALLOWANCE_BYTES``
    is now charged (it is subtracted from the same budget on every load and
    was omitted entirely — task 11's ninth OUTCOME block, defect (a)), and the
    hard KV term is the small VIABILITY floor rather than the 1 GiB
    serving-margin floor, which is now the thin-margin WARNING threshold (H9 —
    charging it hard refused the configuration 1.0.59 demonstrably served).
    The scaling term is TOTAL multimodal units, not the image count.
    """
    return (weights_bytes + mb.NON_TORCH_ALLOWANCE_BYTES
            + mb.activation_allowance(weights_bytes, units)
            + mb.KV_VIABILITY_FLOOR_BYTES)


# ---------------------------------------------------------------------------
# P5-A — refuse iff required > min(available, util x total)  [2.9]
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(inputs=preflight_inputs())
def test_property_refuses_iff_requirement_exceeds_the_measured_minimum(
        inputs):
    """P5-A. For any reading and staged args, the verdict refuses **iff**
    the requirement exceeds ``min(available, util x total)`` — never a
    false pass (the incident: a doomed load costing ~4 min of profiling)
    and never a false refusal (a load the device can hold must proceed).

    # Validates: Requirements 2.9
    """
    total, available, util, units, weights_bytes, args = inputs
    reading = mb.MemoryReading(total_bytes=total, available_bytes=available)

    verdict = mb.evaluate_device_fit(args, reading,
                                     weights_bytes=weights_bytes)

    required = _required(weights_bytes, units)
    assert verdict.terms["required_bytes"] == required, verdict.terms
    assert verdict.terms["multimodal_units"] == units, verdict.terms
    assert verdict.terms["available_bytes"] == available, verdict.terms
    assert verdict.unverified is False

    budget = int(util * total)  # the module's own budget arithmetic
    should_refuse = required > min(available, budget)
    assert verdict.ok == (not should_refuse), (
        "verdict.ok={} but required={} vs min(available={}, budget={})"
        .format(verdict.ok, required, available, budget))

    if should_refuse:
        assert verdict.refusal_reason is not None
        assert verdict.refusal_reason.startswith(
            mb.PREFLIGHT_REFUSED_MARKER), verdict.refusal_reason
        expected_failed = set()
        if available < required:
            expected_failed.add("starvation")
        if budget < required:
            expected_failed.add("budget")
        assert set(verdict.terms["failed_conditions"]) == expected_failed
    else:
        assert verdict.refusal_reason is None
        assert verdict.terms["failed_conditions"] == []


# ---------------------------------------------------------------------------
# P5-B — the refusal reason is a complete diagnostic  [2.9]
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(inputs=refusing_inputs())
def test_property_refusal_reason_names_the_terms_and_the_settings(inputs):
    """P5-B. Every generated refusal reason starts with the marker and
    names: the measured available bytes (as MemAvailable), the computed
    requirement WITH its four terms (weights, non-torch allowance labelled an
    ESTIMATE, activation allowance labelled an ESTIMATE, KV VIABILITY floor),
    and the specific engine settings to change (2.9: "naming the measured
    available memory, the computed requirement, and the specific engine
    setting to change").

    REPOINTED 2026-08-19 (task 14 / H8+H9). SUPERSEDED assertions, recorded
    verbatim — the term set grew, so this is strictly stronger::

        assert mb.format_gib(mb.MINIMUM_KV_CACHE_BYTES) in reason, reason
        assert "ESTIMATE" in reason, reason  # the allowance is labelled one

    # Validates: Requirements 2.9
    """
    total, available, util, units, weights_bytes, args = inputs
    reading = mb.MemoryReading(total_bytes=total, available_bytes=available)

    verdict = mb.evaluate_device_fit(args, reading,
                                     weights_bytes=weights_bytes)

    assert verdict.ok is False, (
        "refusing_inputs generated a passing configuration: {}".format(
            verdict.terms))
    reason = verdict.refusal_reason
    assert reason.startswith(mb.PREFLIGHT_REFUSED_MARKER), reason

    required = _required(weights_bytes, units)
    activation = mb.activation_allowance(weights_bytes, units)
    # The measured available bytes, named as the kernel field.
    assert mb.format_gib(available) in reason, reason
    assert "MemAvailable" in reason, reason
    # The computed requirement with its terms.
    assert mb.format_gib(required) in reason, reason
    assert mb.format_gib(weights_bytes) in reason, reason
    assert mb.format_gib(activation) in reason, reason
    assert mb.format_gib(mb.NON_TORCH_ALLOWANCE_BYTES) in reason, reason
    assert mb.format_gib(mb.KV_VIABILITY_FLOOR_BYTES) in reason, reason
    # BOTH estimated terms are labelled ESTIMATEs, by name.
    assert "non-torch allowance {} (ESTIMATE)".format(
        mb.format_gib(mb.NON_TORCH_ALLOWANCE_BYTES)) in reason, reason
    assert "activation allowance {} (ESTIMATE".format(
        mb.format_gib(activation)) in reason, reason
    # The specific settings to change (Decision 3's ordered menu).
    assert "limit_mm_per_prompt.image" in reason, reason
    assert "max_model_len" in reason, reason
    assert "gpu_memory_utilization" in reason, reason


# ---------------------------------------------------------------------------
# P5-C — undeterminable weights: unverified + the documented lower bound [2.9]
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(inputs=preflight_inputs())
def test_property_undeterminable_weights_use_the_lower_bound_unverified(
        inputs):
    """P5-C. With ``weights_bytes=None`` the verdict is marked
    ``unverified`` and its requirement is the documented
    ``NON_TORCH + ACTIVATION_FLOOR + KV viability floor`` LOWER BOUND (scaled
    only by the authored multimodal units) — never a guessed weight; a refusal
    says so.

    REPOINTED 2026-08-19 (task 14 / H8+H9). SUPERSEDED lower bound, recorded
    verbatim::

        lower_bound = (mb.activation_allowance(0, images)
                       + mb.MINIMUM_KV_CACHE_BYTES)
        ...
        if images == 1:
            assert lower_bound == (mb.ACTIVATION_FLOOR_BYTES
                                   + mb.MINIMUM_KV_CACHE_BYTES)

    # Validates: Requirements 2.9
    """
    total, available, util, units, _weights, args = inputs
    reading = mb.MemoryReading(total_bytes=total, available_bytes=available)

    verdict = mb.evaluate_device_fit(args, reading, weights_bytes=None)

    assert verdict.unverified is True
    assert verdict.terms["weights_bytes"] is None, (
        "an undeterminable weight was invented: {}".format(verdict.terms))
    lower_bound = (mb.NON_TORCH_ALLOWANCE_BYTES
                   + mb.activation_allowance(0, units)
                   + mb.KV_VIABILITY_FLOOR_BYTES)
    assert verdict.terms["required_bytes"] == lower_bound, verdict.terms
    if units == 1:
        assert lower_bound == (mb.NON_TORCH_ALLOWANCE_BYTES
                               + mb.ACTIVATION_FLOOR_BYTES
                               + mb.KV_VIABILITY_FLOOR_BYTES)

    budget = int(util * total)
    should_refuse = lower_bound > min(available, budget)
    assert verdict.ok == (not should_refuse)
    if should_refuse:
        reason = verdict.refusal_reason
        assert reason.startswith(mb.PREFLIGHT_REFUSED_MARKER), reason
        assert "UNVERIFIED" in reason, reason
        assert "lower bound" in reason, reason


# ---------------------------------------------------------------------------
# P5-D — on an enforced refusal the engine factory is NEVER called  [2.9]
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(inputs=preflight_inputs())
def test_property_engine_factory_is_never_called_on_a_refusal(tmp_path,
                                                              inputs):
    """P5-D. Manager level, weights sizable on disk (sparse files): a load
    the arithmetic refuses FAILS with the marked reason and ZERO engine
    constructions — the fix's whole point is that the ~4 min doomed
    profiling never starts; a load the arithmetic admits constructs
    exactly one engine and reaches READY.

    # Validates: Requirements 2.9
    """
    total, available, util, units, weights_bytes, args = inputs
    index = next(_dir_counter)
    weights_dir = weight_tree(tmp_path / "weights-{}".format(index),
                              weights_bytes)
    engine_args = dict(args, model=str(weights_dir))
    repo = tmp_path / "repo-{}".format(index)
    build_staged_repo(repo, engine_args=engine_args)

    reader = FakeMeminfoReader([(total, available)])
    factory = RecordingEngineFactory()
    manager = make_manager(repo, factory, memory_reader=reader)

    status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    # The oracle sees exactly what the code sees: the fake meminfo text is
    # written in kB, and every generated figure is a whole MiB, so the
    # round-trip is exact.
    required = _required(weights_bytes, units)
    budget = int(util * total)
    should_refuse = required > min(available, budget)

    if should_refuse:
        assert factory.call_count == 0, (
            "the engine factory was called for a load the preflight "
            "arithmetic refuses (required={}, available={}, budget={}) "
            "[readings observed: {}]".format(
                required, available, budget, reader.describe()))
        assert status.state is ModelState.FAILED, (status.state,
                                                   status.reason)
        assert status.reason is not None
        assert status.reason.startswith(mb.PREFLIGHT_REFUSED_MARKER), \
            status.reason
    else:
        assert factory.call_count == 1, (
            "a load the preflight arithmetic admits (required={}, "
            "available={}, budget={}) did not construct exactly one "
            "engine: {} constructions".format(
                required, available, budget, factory.call_count))
        assert status.state is ModelState.READY, (status.state,
                                                  status.reason)


# ---------------------------------------------------------------------------
# Prep side — helpers
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _args(**kwargs):
    namespace = argparse.Namespace(
        unarchived_repo_path=None,
        weights_path=None,
        model_name=None,
        component_name=None,
        cleanup=False,
    )
    for key, value in kwargs.items():
        setattr(namespace, key, value)
    return namespace


#: Realistic refusal diagnostics: the marker first, arbitrary middle text,
#: and the string 'gpu_memory_utilization' (every real refusal spells the
#: budget out as util x MemTotal — the very string that would trigger the
#: KV-OOM recovery if the classification order were wrong).
_middles = st.text(max_size=40)


def _refusal_reason(middle, embed_kv_sentence=False):
    reason = (
        "{} vLLM model '{}' cannot be loaded on this device now: measured "
        "available memory 3.00 GiB (MemAvailable) and device budget "
        "11.98 GiB (gpu_memory_utilization=0.4 x MemTotal 29.95 GiB) {}"
    ).format(mp.PREFLIGHT_REFUSED_MARKER, DEFAULT_MODEL_NAME, middle)
    if embed_kv_sentence:
        reason += " | " + KV_OOM_REASON
    return reason


# ---------------------------------------------------------------------------
# P5-E — classified BEFORE the KV markers: no unload -> reload  [2.9, 3.8]
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(middle=_middles, embed_kv_sentence=st.booleans())
def test_property_refusal_is_classified_before_the_kv_markers(
        middle, embed_kv_sentence):
    """P5-E. For any refusal body — including one embedding the FULL
    KV-OOM sentence on top of the ever-present 'gpu_memory_utilization'
    string — ``request_load`` returns ``LOAD_PREFLIGHT_REFUSED`` after
    exactly ONE load request: the unload -> reload recovery (which the KV
    markers would trigger) never fires, because nothing was allocated and
    a retry would be refused identically.

    # Validates: Requirements 2.9, 3.8
    """
    reason = _refusal_reason(middle, embed_kv_sentence)
    urls = []

    def scripted_post(url, timeout=None):
        urls.append(url)
        return _Response(409, json.dumps({"error": reason}))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mp, "wait_for_server", lambda *a, **k: True)
        patch.setattr(mp.requests, "post", scripted_post)
        outcome = mp.request_load(DEFAULT_MODEL_NAME,
                                  {"gpu_memory_utilization": 0.4,
                                   "max_model_len": 4096})

    assert outcome == mp.LOAD_PREFLIGHT_REFUSED, (outcome, reason)
    assert [url.rsplit("/", 1)[-1] for url in urls] == ["load"], (
        "a preflight refusal drove the unload -> reload recovery: "
        "{}".format(urls))


def test_kv_oom_body_without_the_marker_still_recovers_once():
    """P5-E contrast (the order, seen from the other side): a genuine
    KV-OOM body WITHOUT the marker still fires the single unload -> reload
    recovery exactly as before — the preflight branch narrowed nothing.

    # Validates: Requirements 3.8
    """
    urls = []

    def scripted_post(url, timeout=None):
        urls.append(url)
        return _Response(409, json.dumps({"error": KV_OOM_REASON}))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mp, "wait_for_server", lambda *a, **k: True)
        patch.setattr(mp.requests, "post", scripted_post)
        outcome = mp.request_load(DEFAULT_MODEL_NAME,
                                  {"gpu_memory_utilization": 0.4})

    assert outcome == mp.LOAD_HTTP_ERROR
    assert [url.rsplit("/", 1)[-1] for url in urls] == [
        "load", "unload", "load"], urls


# ---------------------------------------------------------------------------
# P5-F — prepare() exits 0 on a refusal, with the full diagnostic  [2.9, 3.8]
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(middle=_middles)
def test_property_prepare_exits_zero_and_logs_the_full_diagnostic(tmp_path,
                                                                  middle):
    """P5-F. For any refusal diagnostic, ``prepare()`` returns **0** (the
    one authoritative failure that must NOT take the Greengrass deployment
    BROKEN -> rolled back, defect 1.9) and the prominent ERROR line
    carries the full diagnostic verbatim — exit 0 is never silent.

    # Validates: Requirements 2.9, 3.8
    """
    index = next(_dir_counter)
    repo = tmp_path / "unarchived-{}".format(index)
    build_staged_repo(repo, DEFAULT_MODEL_NAME)
    reason = _refusal_reason(middle)

    def scripted_post(url, timeout=None):
        return _Response(409, json.dumps({"error": reason}))

    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collector()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(mp, "stage_repository",
                          lambda *a, **k: str(tmp_path / "staged"))
            patch.setattr(mp, "wait_for_server", lambda *a, **k: True)
            patch.setattr(mp.requests, "post", scripted_post)
            exit_code = mp.prepare(_args(unarchived_repo_path=str(repo),
                                         model_name=DEFAULT_MODEL_NAME))
    finally:
        root.removeHandler(handler)

    assert exit_code == 0, (
        "a preflight refusal must exit 0 (a component retry cannot change "
        "a pre-allocation refusal); got {}".format(exit_code))
    errors = [record.getMessage() for record in records
              if record.levelno >= logging.ERROR]
    refused_lines = [line for line in errors
                     if "REFUSED by the device memory preflight" in line]
    assert refused_lines, (
        "exit 0 with no prominent ERROR naming the refusal: {!r}".format(
            errors))
    assert reason in refused_lines[0], (
        "the prominent ERROR does not carry the full diagnostic: "
        "{!r}".format(refused_lines[0]))


# ---------------------------------------------------------------------------
# P5-G — every other classification keeps its exit code  [3.8]
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(classification=st.sampled_from((mp.LOAD_OK, mp.LOAD_UNREACHABLE,
                                       mp.LOAD_HTTP_ERROR)))
def test_property_existing_classifications_keep_their_exit_codes(
        tmp_path, classification):
    """P5-G. ``LOAD_OK`` -> 0 and ``LOAD_UNREACHABLE`` -> 1 are byte-for-byte
    the pre-fix contract Greengrass' Startup retry behavior depends on
    (3.8); the ``LOAD_PREFLIGHT_REFUSED`` -> 0 branch changed neither.

    CONSCIOUS REPOINT (task 14 H11 dispatch; task 11 OUTCOME block 18):
    ``LOAD_HTTP_ERROR`` -> **0**. Verbatim original::

        expected = 0 if classification == mp.LOAD_OK else 1

        \"\"\"P5-G. ``LOAD_OK`` -> 0, ``LOAD_UNREACHABLE`` -> 1,
        ``LOAD_HTTP_ERROR`` -> 1 — byte-for-byte the pre-fix contract
        Greengrass' Startup retry behavior depends on (3.8): the new
        ``LOAD_PREFLIGHT_REFUSED`` -> 0 branch changed none of them.

        # Validates: Requirements 3.8
        \"\"\"

    Reason: a model-load failure must not be able to mark the COMPONENT
    broken (three transient-DNS failures -> BROKEN -> two HARD-dependent
    workflows stuck at INSTALLED -> device UNHEALTHY). ``LOAD_UNREACHABLE``
    deliberately keeps exit 1 and is still asserted here, so the property
    still distinguishes the one classification a component retry can fix
    from the ones it cannot.

    # Validates: Requirements 3.8
    """
    index = next(_dir_counter)
    repo = tmp_path / "unarchived-{}".format(index)
    build_staged_repo(repo, DEFAULT_MODEL_NAME)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mp, "stage_repository",
                      lambda *a, **k: str(tmp_path / "staged"))
        patch.setattr(mp, "request_load",
                      lambda *a, **k: classification)
        exit_code = mp.prepare(_args(unarchived_repo_path=str(repo),
                                     model_name=DEFAULT_MODEL_NAME))

    expected = 1 if classification == mp.LOAD_UNREACHABLE else 0
    assert exit_code == expected, (
        "{} -> exit {} (expected {})".format(classification, exit_code,
                                             expected))


# ---------------------------------------------------------------------------
# P5-H — the duplicated marker stays in lockstep  [3.8]
# ---------------------------------------------------------------------------

def test_prep_marker_equals_memory_budgets_marker():
    """P5-H. ``vllm_model_prep`` duplicates the marker literal on purpose
    (the prep is seeded standalone to /aws_dda and cannot import
    ``vllm_runtime`` in every context); this pin keeps the two copies in
    lockstep so the prep's classification never silently stops matching
    the runtime's refusal reasons.

    # Validates: Requirements 3.8
    """
    assert mp.PREFLIGHT_REFUSED_MARKER == mb.PREFLIGHT_REFUSED_MARKER
    assert mb.PREFLIGHT_REFUSED_MARKER == "preflight-refused:"
