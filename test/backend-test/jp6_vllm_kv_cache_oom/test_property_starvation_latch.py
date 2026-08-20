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
"""Fix-checking PROPERTIES for the Starvation_Latch (spec:
jp6-vllm-kv-cache-oom-regression, task 4.5).

# Validates: Requirements 2.5

**Property 6: Bug Condition — No retry into a starved device** (design
"Correctness Properties"). _For any_ sequence of injected before/after
memory readings around a failed load where the available memory does not
return to within the reclaim tolerance, the fixed manager SHALL set the
Starvation_Latch, log the two readings, and refuse subsequent load
attempts in that backend life with a diagnostic naming the starved
condition, until an explicit unload clears it.

Properties in this file:
  P6-A **Latched iff the memory did not come back** [2.5] — over generated
       (before, after) readings around a failed load, the latch is set
       **iff** ``available_after < available_before −
       RECLAIM_TOLERANCE_BYTES``; when set, a prominent WARNING carries
       BOTH readings and the model name, and the next load in this backend
       life is refused (P3) with a diagnostic naming the starved condition
       and the readings; a recovering reading NEVER sets it and the next
       load proceeds to engine construction.
  P6-B **An explicit ``unload()`` clears the latch** [2.5] — after a
       latching failure, ``unload`` logs the clearance with both readings
       and the following load is measured afresh (the engine factory is
       called again).
  P6-C **Per-backend-life, never persisted** [2.5] — latching writes
       NOTHING to disk: the staged repository is byte-listing-identical,
       and a fresh manager over the very same model_dir (a new backend
       life) loads without any latch refusal.
  P6-D **The cascade's stopping condition** [2.5] — incident-shaped: a
       KV-OOM failure whose memory does not come back (23.00 GiB → 3.00
       GiB, the observed ``26 GB used / 3 GB free``) latches; a plain
       retry is refused by the latch with ZERO further engine
       constructions; the prep's single KV-OOM unload → reload recovery is
       PRESERVED (the first failure's reason still carries the verbatim
       KV-OOM text its markers match, and the unload → reload sequence is
       permitted), but the reload — the recovery's second attempt — is
       refused by the measured-availability arm before any allocation
       when the device is demonstrably starved.
  P6-E **A recovering reading never latches** [2.5] — the measured KV-OOM
       reclaim shape (memory came back and MORE) leaves no latch and the
       retry constructs a second engine, exactly the single unload → reload
       recovery 1.0.59 survived by.
  P6-F **The latch SETTLES before it believes a starved reading** [2.5] —
       defect (b) of task 11's ninth OUTCOME block: the latch sampled
       `/proc/meminfo` before the driver's ASYNCHRONOUS release, reported
       `9.45 GiB did not come back`, and ~5.2 GiB came back unaided in the
       SAME backend life with no restart (a later occurrence reported
       12.35 GiB). For any first reading that looks starved but whose LATER
       reading recovers, the fixed manager takes no starvation verdict, logs
       one INFO line saying the memory came back after N seconds, and the
       next load reaches engine construction. The healthy path (a first
       reading within tolerance) sleeps NOT AT ALL, and the injected sleep
       proves no host test ever really sleeps.
  P6-G **When every sample is short it latches on the SETTLED figure**
       [2.5] — the WARNING reports the BEST (highest) `available_after`
       observed and how many samples were taken over what period, so the
       recorded shortfall is the settled one rather than the transient
       first reading; the message keeps both readings, the lost bytes, the
       tolerance, the cascade explanation and the container-restart
       recovery.

HONESTY GUARD (binding, design "Honesty Guard"). This file proves the
**decision logic only**, over INJECTED ``/proc/meminfo`` readings and the
manager's public ``engine_factory`` seam — no GPU, no CUDA/NVML, no real
reclaim. Whether memory is ACTUALLY reclaimed across the NVML-assert path
is **[HARDWARE] H4** (task 11); "refused in seconds" is asserted here as
"refused with zero engine constructions" (the ~4 min profiling never
starts) — the wall-clock claim is hardware's.

Hypothesis conventions for the device suites (``--noconftest``, so no
profile is registered): ``@settings(deadline=None)`` with **no hardcoded
``max_examples``**, per-example unique tmp dirs (Hypothesis re-enters the
test body while ``tmp_path`` is function-scoped, hence the suppressed
health check), matching the sibling device suites.

Run (host-side, from the repo root):
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
      test/backend-test/jp6_vllm_kv_cache_oom/test_property_starvation_latch.py \
      -q -p no:cacheprovider --noconftest

_Requirements: 2.5_
"""
import asyncio
import contextlib
import itertools
import logging
import os

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

import vllm_runtime.memory_budget as mb
import vllm_runtime.manager as manager_module
from vllm_runtime.manager import ModelState
from jp6_vllm_kv_cache_oom.fakes import (
    DEFAULT_MODEL_NAME,
    DEVICE_TOTAL_BYTES,
    GIB,
    INCIDENT_ENGINE_ARGS,
    KV_OOM_REASON,
    FailingEngineFactory,
    FakeMeminfoReader,
    RecordingEngineFactory,
    RecordingSleeper,
    build_staged_repo,
    make_manager,
    weight_tree,
)

MIB = 1024 ** 2

#: The manager's prominent-WARNING signature (Decision 5 step 3).
STARVED_WARNING_SIGNATURE = "STARVED DEVICE"

#: Per-example unique directory names (tmp_path is function-scoped while
#: Hypothesis re-enters the test body many times).
_dir_counter = itertools.count()

#: Engine args whose ``model`` is NOT on disk: the weights are
#: undeterminable, so the (verified-arithmetic) preflight arms never
#: enforce and EVERY generated reading reaches engine construction —
#: which is exactly what isolates the latch from the budget math. The
#: latch arm needs no weight estimate and is enforced either way.
_UNSIZABLE_ARGS = dict(INCIDENT_ENGINE_ARGS,
                       model="example/never-pulled-model")


@contextlib.contextmanager
def _collected_logs(level=logging.INFO):
    """Collect every log record emitted in the block (root handler +
    root level lowered so INFO/WARNING pass the effective-level check),
    restoring the previous configuration afterwards. Per-example safe."""
    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    collector = _Collector()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(collector)
    root.setLevel(level)
    try:
        yield records
    finally:
        root.setLevel(previous_level)
        root.removeHandler(collector)


def _starved_warnings(records):
    return [record.getMessage() for record in records
            if record.levelno == logging.WARNING
            and STARVED_WARNING_SIGNATURE in record.getMessage()]


def _file_listing(root):
    """Every file under ``root`` relative to it, sorted — the
    nothing-was-persisted oracle."""
    listing = []
    for dirpath, _dirnames, filenames in os.walk(str(root)):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            listing.append(os.path.relpath(full, str(root)))
    return sorted(listing)


# ---------------------------------------------------------------------------
# Generators — everything in whole MiB so the kB rendering of the fake
# /proc/meminfo round-trips EXACTLY (meminfo_text writes kB; the parser
# multiplies back): the latch compares the very numbers this oracle uses.
# ---------------------------------------------------------------------------

@st.composite
def reading_pairs(draw):
    """(total, available_before, available_after) around one failed load:
    totals from small boards to Thor class, before anywhere in the total,
    after from fully-recovered-and-more down to a deep shortfall — both
    sides of the tolerance are exercised."""
    total_mib = draw(st.integers(min_value=8 * 1024, max_value=128 * 1024))
    before_mib = draw(st.integers(min_value=1024, max_value=total_mib))
    delta_mib = draw(st.integers(min_value=-4096, max_value=16 * 1024))
    after_mib = min(max(before_mib - delta_mib, 0), total_mib)
    return total_mib * MIB, before_mib * MIB, after_mib * MIB


@st.composite
def starved_reading_pairs(draw):
    """Reading pairs constrained to a GUARANTEED latch: the after reading
    is short of the before reading by strictly more than the tolerance."""
    total_mib = draw(st.integers(min_value=8 * 1024, max_value=128 * 1024))
    before_mib = draw(st.integers(min_value=2 * 1024, max_value=total_mib))
    tolerance_mib = mb.RECLAIM_TOLERANCE_BYTES // MIB
    lost_mib = draw(st.integers(min_value=tolerance_mib + 1,
                                max_value=before_mib))
    return total_mib * MIB, before_mib * MIB, (before_mib - lost_mib) * MIB


def _build_failing_manager(tmp_path, readings, sleeper=None):
    """A manager over a freshly staged repo (per-example unique dir) whose
    engine factory raises the verbatim KV-OOM reason and whose memory
    reader is the scripted fake. ``sleeper`` is the latch's settle delay —
    injected, so no host test ever really sleeps."""
    index = next(_dir_counter)
    repo = tmp_path / "repo-{}".format(index)
    build_staged_repo(repo, engine_args=_UNSIZABLE_ARGS)
    reader = FakeMeminfoReader(readings)
    factory = FailingEngineFactory(KV_OOM_REASON)
    manager = make_manager(repo, factory, memory_reader=reader,
                           sleeper=sleeper)
    return manager, factory, reader, repo


def _settle_infos(records):
    """The INFO line the latch logs when a later sample recovered."""
    return [record.getMessage() for record in records
            if record.levelno == logging.INFO
            and "CAME BACK" in record.getMessage()]


# ---------------------------------------------------------------------------
# P6-A — latched iff after < before − RECLAIM_TOLERANCE_BYTES  [2.5]
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
# The exact boundary: after == before − tolerance is NOT starved (the
# condition is strict), one MiB further is.
@example(pair=(30 * GIB, 20 * GIB, 20 * GIB - mb.RECLAIM_TOLERANCE_BYTES))
@example(pair=(30 * GIB, 20 * GIB,
               20 * GIB - mb.RECLAIM_TOLERANCE_BYTES - MIB))
# A recovering reading (memory came back and MORE) never latches.
@example(pair=(30 * GIB, 10 * GIB, 22 * GIB))
@given(pair=reading_pairs())
def test_property_latch_is_set_iff_memory_did_not_return(tmp_path, pair):
    """P6-A. For any (before, after) readings around a failed load, the
    latch is set **iff** ``after < before − RECLAIM_TOLERANCE_BYTES``:
    when set, the prominent WARNING carries both readings and the model
    name and the next load is refused (P3, zero further engine
    constructions) with a diagnostic naming the starved condition and the
    readings; when not set — including every recovering reading — no such
    WARNING exists and the next load reaches engine construction.

    # Validates: Requirements 2.5
    """
    total, before, after = pair
    manager, factory, reader, _repo = _build_failing_manager(
        tmp_path, [(total, before), (total, after)])

    with _collected_logs() as records:
        first = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert first.state is ModelState.FAILED, (
        "harness precondition: the first load must fail, got {}".format(
            first.state))
    assert factory.call_count == 1, (
        "harness precondition: the first load must reach engine "
        "construction (weights are undeterminable, so no verified arm "
        "may enforce); factory called {} times".format(factory.call_count))

    should_latch = after < before - mb.RECLAIM_TOLERANCE_BYTES
    warnings = _starved_warnings(records)

    if should_latch:
        assert warnings, (
            "the memory did not come back (before={}, after={}, "
            "tolerance={}) but no prominent starvation WARNING was "
            "logged [readings observed: {}]".format(
                mb.format_gib(before), mb.format_gib(after),
                mb.format_gib(mb.RECLAIM_TOLERANCE_BYTES),
                reader.describe()))
        message = warnings[0]
        assert mb.format_gib(before) in message, message
        assert mb.format_gib(after) in message, message
        assert DEFAULT_MODEL_NAME in message, message
    else:
        assert not warnings, (
            "a recovering/within-tolerance reading (before={}, after={}) "
            "set the starvation latch: {!r}".format(
                mb.format_gib(before), mb.format_gib(after), warnings))

    second = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    if should_latch:
        assert factory.call_count == 1, (
            "the manager retried into a starved device: the engine "
            "factory was called {} times [readings observed: {}]".format(
                factory.call_count, reader.describe()))
        assert second.state is ModelState.FAILED, second.state
        assert second.reason is not None
        assert second.reason.startswith(mb.PREFLIGHT_REFUSED_MARKER), \
            second.reason
        assert "starv" in second.reason.lower(), (
            "the refusal does not name the starved condition: "
            "{!r}".format(second.reason))
        assert mb.format_gib(before) in second.reason, second.reason
        assert mb.format_gib(after) in second.reason, second.reason
    else:
        assert factory.call_count == 2, (
            "a load after a fully-reclaimed failure must be measured "
            "afresh and reach engine construction; factory called {} "
            "times [readings observed: {}]".format(
                factory.call_count, reader.describe()))


# ---------------------------------------------------------------------------
# P6-B — an explicit unload() clears the latch  [2.5]
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(pair=starved_reading_pairs())
def test_property_explicit_unload_clears_the_latch(tmp_path, pair):
    """P6-B. For any latching failure, the latch refuses the plain retry —
    and an explicit ``unload()`` clears it (logged, with both readings),
    so the following load is measured afresh and reaches engine
    construction again. An operator-initiated cycle is allowed to try
    again; the latch is a memory of a failed attempt, not a permanent
    verdict.

    # Validates: Requirements 2.5
    """
    total, before, after = pair
    manager, factory, reader, _repo = _build_failing_manager(
        tmp_path, [(total, before), (total, after)])

    first = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert first.state is ModelState.FAILED
    assert factory.call_count == 1

    refused = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert refused.state is ModelState.FAILED
    assert refused.reason.startswith(mb.PREFLIGHT_REFUSED_MARKER)
    assert factory.call_count == 1, (
        "harness precondition: the plain retry must be refused by the "
        "latch; factory called {} times".format(factory.call_count))

    with _collected_logs() as records:
        was_tracked = manager.unload(DEFAULT_MODEL_NAME)
    assert was_tracked is True
    cleared = [record.getMessage() for record in records
               if "starvation latch" in record.getMessage().lower()
               and "clear" in record.getMessage().lower()]
    assert cleared, (
        "the explicit unload did not log the latch clearance: {!r}".format(
            [record.getMessage() for record in records]))
    assert mb.format_gib(before) in cleared[0], cleared[0]
    assert mb.format_gib(after) in cleared[0], cleared[0]

    third = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert factory.call_count == 2, (
        "after an explicit unload the next load must be measured afresh "
        "and reach engine construction; factory called {} times "
        "[readings observed: {}]".format(factory.call_count,
                                         reader.describe()))
    assert third.state is ModelState.FAILED  # the fake engine still fails
    assert not third.reason.startswith(mb.PREFLIGHT_REFUSED_MARKER), (
        "the latch survived the explicit unload: {!r}".format(third.reason))


# ---------------------------------------------------------------------------
# P6-C — per-backend-life, never persisted  [2.5]
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(pair=starved_reading_pairs())
def test_property_latch_is_per_backend_life_and_never_persisted(tmp_path,
                                                                pair):
    """P6-C. For any latching failure, NOTHING about the latch reaches
    disk (the staged repository's file listing is unchanged by the
    latching failure and its refused retry — no new persisted contract,
    no tombstone interaction), and a NEW manager over the very same
    ``model_dir`` — a new backend life — carries no latch: its load
    proceeds straight to engine construction.

    # Validates: Requirements 2.5
    """
    total, before, after = pair
    manager, factory, reader, repo = _build_failing_manager(
        tmp_path, [(total, before), (total, after)])
    listing_before = _file_listing(repo)

    first = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert first.state is ModelState.FAILED
    refused = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert refused.reason.startswith(mb.PREFLIGHT_REFUSED_MARKER)
    assert factory.call_count == 1, (
        "harness precondition: the latch must be set and refusing in the "
        "first backend life")

    assert _file_listing(repo) == listing_before, (
        "the starvation latch persisted something to disk; the latch is "
        "per-backend-life state ONLY")

    # A new backend life over the SAME staged repository: no latch.
    fresh_reader = FakeMeminfoReader([(total, before)])
    fresh_factory = RecordingEngineFactory()
    fresh_manager = make_manager(repo, fresh_factory,
                                 memory_reader=fresh_reader)
    status = asyncio.run(fresh_manager.load(DEFAULT_MODEL_NAME))
    assert fresh_factory.call_count == 1, (
        "a fresh backend life inherited the previous life's starvation "
        "latch: the engine factory was never called")
    assert status.state is ModelState.READY, (status.state, status.reason)


# ---------------------------------------------------------------------------
# P6-D — the cascade's stopping condition, incident-shaped  [2.5]
# ---------------------------------------------------------------------------

def test_cascade_stopping_condition_kv_oom_recovery_preserved_but_refused(
        tmp_path):
    """P6-D. The incident's cascade, host-side: a KV-OOM engine failure
    whose memory does NOT come back (23.00 GiB available before, 3.00 GiB
    after — the observed ``26 GB used / 3 GB free with no model loaded``)
    sets the latch. A plain retry is refused by the latch (P3). The
    prep's single KV-OOM unload → reload recovery is PRESERVED — the first
    failure's reason still carries the verbatim KV-OOM text its
    ``KV_CACHE_HINT_MARKERS`` match, and the unload → reload sequence is
    permitted — but the reload (the recovery's second attempt) is refused
    by the measured-availability arm (P1: the weights ARE sizable on
    disk, so the refusal is enforced) with ZERO further engine
    constructions: refused in the time of one ``/proc/meminfo`` read
    instead of another ~4 min doomed profiling. That is the cascade's
    stopping condition.

    (The staged utilization is 0.7 here, NOT the incident's 0.4: under
    the fixed arithmetic the incident configuration is refused by the
    preflight before any engine exists — task 4.4's territory — and this
    test needs the FIRST attempt to reach the engine so the latch path
    itself is exercised.)

    # Validates: Requirements 2.5
    """
    weights_dir = weight_tree(tmp_path / "weights", int(6.5 * GIB))
    engine_args = dict(INCIDENT_ENGINE_ARGS, model=str(weights_dir),
                       gpu_memory_utilization=0.7)
    repo = tmp_path / "repo"
    build_staged_repo(repo, engine_args=engine_args)

    reader = FakeMeminfoReader([
        (DEVICE_TOTAL_BYTES, 23 * GIB),  # before the first attempt
        (DEVICE_TOTAL_BYTES, 3 * GIB),   # after it failed: not reclaimed
    ])
    factory = FailingEngineFactory(KV_OOM_REASON)
    manager = make_manager(repo, factory, memory_reader=reader)

    # 1. The first attempt reaches the engine and fails with the verbatim
    #    KV-OOM reason (so the prep's recovery markers still match: the
    #    single unload -> reload recovery is preserved, 3.8) and latches.
    with _collected_logs() as records:
        first = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert first.state is ModelState.FAILED
    assert factory.call_count == 1
    assert KV_OOM_REASON in first.reason, (
        "the KV-OOM reason must survive verbatim so the prep's recovery "
        "markers keep matching: {!r}".format(first.reason))
    assert not first.reason.startswith(mb.PREFLIGHT_REFUSED_MARKER)
    warnings = _starved_warnings(records)
    assert warnings, "the 20 GiB shortfall did not latch"
    assert mb.format_gib(23 * GIB) in warnings[0]
    assert mb.format_gib(3 * GIB) in warnings[0]

    # 2. A plain retry is refused by the latch (P3): zero further engine
    #    constructions, the diagnostic names the starved condition and
    #    carries both readings.
    retry = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert factory.call_count == 1, (
        "the manager retried into the starved device")
    assert retry.state is ModelState.FAILED
    assert retry.reason.startswith(mb.PREFLIGHT_REFUSED_MARKER)
    assert "starv" in retry.reason.lower(), retry.reason
    assert mb.format_gib(23 * GIB) in retry.reason, retry.reason
    assert mb.format_gib(3 * GIB) in retry.reason, retry.reason

    # 3. The prep's recovery sequence (unload -> reload) is permitted: the
    #    explicit unload clears the latch...
    assert manager.unload(DEFAULT_MODEL_NAME) is True

    # 4. ...and the reload — the recovery's second attempt — is refused
    #    BEFORE any allocation by the measured-availability arm (P1),
    #    because the device is demonstrably starved: 3.00 GiB available
    #    against a 12.38 GiB requirement the sized weights make verified.
    reload_status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert factory.call_count == 1, (
        "the recovery's second attempt constructed an engine on a "
        "demonstrably starved device (the ~4 min doomed profiling the "
        "fix exists to prevent)")
    assert reload_status.state is ModelState.FAILED
    assert reload_status.reason.startswith(mb.PREFLIGHT_REFUSED_MARKER), \
        reload_status.reason
    assert "starvation" in reload_status.reason, reload_status.reason
    assert mb.format_gib(3 * GIB) in reload_status.reason, \
        reload_status.reason


# ---------------------------------------------------------------------------
# P6-E — the measured KV-OOM reclaim shape never latches  [2.5]
# ---------------------------------------------------------------------------

def test_recovering_reading_never_latches_and_the_retry_proceeds(tmp_path):
    """P6-E. The KV-OOM path the device measured on 1.0.59 (the reclaim
    cleared an 8.34 GiB non-torch swing and the retry reached READY):
    memory that came back — and MORE — after the failed attempt leaves no
    latch and logs no starvation WARNING, and the retry reaches engine
    construction. The fix stops the cascade without touching the single
    recovery that 1.0.59 demonstrably survived by.

    # Validates: Requirements 2.5
    """
    manager, factory, reader, _repo = _build_failing_manager(
        tmp_path, [
            (DEVICE_TOTAL_BYTES, 12 * GIB),  # before the first attempt
            (DEVICE_TOTAL_BYTES, 21 * GIB),  # after: reclaimed, and more
        ])

    with _collected_logs() as records:
        first = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert first.state is ModelState.FAILED
    assert factory.call_count == 1
    assert not _starved_warnings(records), (
        "a recovering reading latched: {!r}".format(
            _starved_warnings(records)))

    second = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert factory.call_count == 2, (
        "the retry after a fully-reclaimed failure was refused; the "
        "single unload -> reload recovery must stay available [readings "
        "observed: {}]".format(reader.describe()))
    assert second.state is ModelState.FAILED  # the fake engine still fails
    assert not second.reason.startswith(mb.PREFLIGHT_REFUSED_MARKER)


# ---------------------------------------------------------------------------
# P6-F — the latch SETTLES before it believes a starved reading  [2.5]
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(recovering_sample=st.integers(
    min_value=2, max_value=manager_module.STARVATION_SETTLE_RESAMPLES + 1))
def test_property_memory_that_comes_back_later_never_latches(
        tmp_path, recovering_sample):
    """P6-F. For any sample at which the memory comes back, the latch takes
    NO starvation verdict: the driver releases ASYNCHRONOUSLY, so a first
    reading taken immediately after ``_shutdown_engine`` +
    ``_reclaim_gpu_memory`` can be a transient. Measured on device: the
    latch reported ``9.45 GiB did not come back`` and ~5.2 GiB came back
    unaided in the SAME backend life with no restart (defect (b) of task
    11's ninth OUTCOME block).

    Asserted: no starvation WARNING, exactly one INFO line saying the
    memory came back after N seconds and that no starvation verdict is
    recorded, one injected sleep per re-sample (and NO real sleeping), and
    the next load reaches engine construction — it is not refused.

    # Validates: Requirements 2.5
    """
    before = 20 * GIB
    starved = 8 * GIB
    readings = ([(DEVICE_TOTAL_BYTES, before)]
                + [(DEVICE_TOTAL_BYTES, starved)] * (recovering_sample - 1)
                + [(DEVICE_TOTAL_BYTES, before)])
    sleeper = RecordingSleeper()
    manager, factory, reader, _repo = _build_failing_manager(
        tmp_path, readings, sleeper=sleeper)

    with _collected_logs() as records:
        first = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert first.state is ModelState.FAILED
    assert factory.call_count == 1

    assert not _starved_warnings(records), (
        "a reading that recovered on sample {} still latched: {!r} "
        "[readings observed: {}]".format(
            recovering_sample, _starved_warnings(records),
            reader.describe()))

    infos = _settle_infos(records)
    assert len(infos) == 1, (
        "expected exactly one 'memory came back' INFO line for a recovery "
        "on sample {}, got {!r}".format(recovering_sample, infos))
    message = infos[0]
    assert DEFAULT_MODEL_NAME in message, message
    expected_seconds = manager_module.STARVATION_SETTLE_DELAY_SECONDS * (
        recovering_sample - 1)
    assert "{:.1f} seconds".format(expected_seconds) in message, message
    assert "no starvation verdict" in message.lower(), message

    # The delay was requested, never really slept: one sleep per re-sample.
    assert sleeper.delays == [
        manager_module.STARVATION_SETTLE_DELAY_SECONDS
    ] * (recovering_sample - 1), sleeper.delays

    second = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert factory.call_count == 2, (
        "a load after memory that DID come back was refused; the settle "
        "re-sampling exists precisely to avoid that [readings observed: "
        "{}]".format(reader.describe()))
    assert not second.reason.startswith(mb.PREFLIGHT_REFUSED_MARKER), \
        second.reason


@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(pair=reading_pairs())
def test_property_the_healthy_path_never_sleeps(tmp_path, pair):
    """P6-F, healthy-path half. A first reading already within
    ``RECLAIM_TOLERANCE_BYTES`` returns immediately: no settle delay is
    ever requested, so the fix adds NO latency to a failure whose memory
    came straight back — the re-sampling cost is paid only where the
    reading looks starved.

    # Validates: Requirements 2.5
    """
    total, before, after = pair
    sleeper = RecordingSleeper()
    manager, factory, _reader, _repo = _build_failing_manager(
        tmp_path, [(total, before), (total, after)], sleeper=sleeper)

    asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert factory.call_count == 1

    if after >= before - mb.RECLAIM_TOLERANCE_BYTES:
        assert sleeper.call_count == 0, (
            "the healthy path slept {} time(s) (before={}, after={})".format(
                sleeper.call_count, mb.format_gib(before),
                mb.format_gib(after)))
    else:
        # A starved-looking reading settles, but only ever a bounded number
        # of times — the worst case is stated in the manager's constants.
        assert sleeper.call_count == \
            manager_module.STARVATION_SETTLE_RESAMPLES, sleeper.delays
        assert sleeper.total == (
            manager_module.STARVATION_SETTLE_RESAMPLES
            * manager_module.STARVATION_SETTLE_DELAY_SECONDS)


# ---------------------------------------------------------------------------
# P6-G — latching reports the SETTLED (best) figure  [2.5]
# ---------------------------------------------------------------------------

def test_latching_reports_the_best_reading_and_the_sampling_window(tmp_path):
    """P6-G. When EVERY sample is short the latch fires — and it reports the
    BEST (highest) ``available_after`` observed plus how many samples were
    taken over what period, so the recorded shortfall is the SETTLED figure
    rather than the transient first reading (the defect: 9.45 GiB reported
    against a genuinely-stranded ~4 GiB). The message keeps every existing
    element: both readings, the lost bytes, the tolerance, the cascade
    explanation and the backend-container-restart recovery.

    # Validates: Requirements 2.5
    """
    before = 20 * GIB
    transient = 5 * GIB   # the first, unsettled reading
    best = 9 * GIB        # the best the driver ever gave back
    sleeper = RecordingSleeper()
    manager, factory, reader, _repo = _build_failing_manager(
        tmp_path,
        [(DEVICE_TOTAL_BYTES, before),
         (DEVICE_TOTAL_BYTES, transient),
         (DEVICE_TOTAL_BYTES, best),
         (DEVICE_TOTAL_BYTES, 7 * GIB),
         (DEVICE_TOTAL_BYTES, 6 * GIB)],
        sleeper=sleeper)

    with _collected_logs() as records:
        first = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert first.state is ModelState.FAILED
    assert factory.call_count == 1

    warnings = _starved_warnings(records)
    assert warnings, "every sample was short but nothing latched"
    message = warnings[0]
    # The SETTLED figure, not the transient one.
    assert mb.format_gib(best) in message, message
    assert mb.format_gib(transient) not in message, (
        "the WARNING reported the transient first reading instead of the "
        "best observed: {!r}".format(message))
    assert mb.format_gib(before) in message, message
    # The sampling window is stated.
    assert "4 sample" in message, message
    assert "{:.1f} seconds".format(
        manager_module.STARVATION_SETTLE_RESAMPLES
        * manager_module.STARVATION_SETTLE_DELAY_SECONDS) in message, message
    # ...and every pre-existing element survives.
    assert mb.format_gib(before - best) in message, message
    assert mb.format_gib(mb.RECLAIM_TOLERANCE_BYTES) in message, message
    assert "cascade" in message, message
    assert "BACKEND CONTAINER RESTART" in message, message
    assert sleeper.delays == [
        manager_module.STARVATION_SETTLE_DELAY_SECONDS
    ] * manager_module.STARVATION_SETTLE_RESAMPLES, sleeper.delays

    # The refusal carries the settled figure too.
    refused = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert factory.call_count == 1
    assert refused.reason.startswith(mb.PREFLIGHT_REFUSED_MARKER)
    assert mb.format_gib(best) in refused.reason, refused.reason


def test_settle_constants_are_proposed_and_bounded():
    """P6-G, constants half. The two settle constants are PROPOSED
    thresholds (task 14 / H8 owns their calibration, exactly like
    ``RECLAIM_TOLERANCE_BYTES`` and ``THIN_MARGIN_CONCURRENCY``), pinned
    here so a silent drift is visible — and the worst-case added delay
    stays modest, because this runs on a failure path that must not stall
    the runtime server for long.

    # Validates: Requirements 2.5
    """
    assert manager_module.STARVATION_SETTLE_DELAY_SECONDS == 1.5
    assert manager_module.STARVATION_SETTLE_RESAMPLES == 3
    worst_case = (manager_module.STARVATION_SETTLE_RESAMPLES
                  * manager_module.STARVATION_SETTLE_DELAY_SECONDS)
    assert worst_case == 4.5
    assert worst_case <= 10.0, (
        "the settle window grew beyond a modest bound: {} s".format(
            worst_case))
