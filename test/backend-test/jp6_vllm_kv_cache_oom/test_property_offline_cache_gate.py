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
"""Fix-checking PROPERTIES for the OFFLINE-CACHE GATE around engine
construction (spec: jp6-vllm-kv-cache-oom-regression; task 11's eighteenth
OUTCOME block / this session's device leg, item 1).

# Validates: Requirements 2.9, 3.8

**The defect.** On 2026-08-19 the JP6 model component went **BROKEN** after
three consecutive load failures (12:00:47Z, 12:02:09Z, 12:03:22Z, each
``Startup script exited. {exitCode=1}``) whose reason was, verbatim,
``MaxRetryError('HTTPSConnectionPool(host='huggingface.co', port=443) …
Failed to resolve 'huggingface.co' ([Errno -3] Temporary failure in name
resolution)')`` — with the repository **already staged locally**
(``gpu_memory_utilization=0.55``, ``max_model_len=4096``) and the weights
already in the HF cache. Two deployed workflows HARD-depend on that
component, so they were left stuck at ``INSTALLED``: a transient DNS fault
became a workflow outage. The network call is vLLM/transformers resolving
the repo id, not our code.

**The fix under test.** Immediately before engine construction the manager
asks :func:`memory_budget.estimate_weights_on_disk` whether the weights are
already on disk and, when they are, constructs the engine with Hugging Face
offline mode enabled (``HF_HUB_OFFLINE=1``, ``TRANSFORMERS_OFFLINE=1``) for
the DURATION of construction only, restoring the exact prior state
afterwards.

Properties in this file:
  O-A **Offline mode is applied when the weights are on disk, and ONLY for
       the construction** — the factory observes both variables set to
       ``"1"``; afterwards the environment is byte-identical to what it was,
       including variables that were previously ABSENT (deleted again, never
       left as ``""``) and variables that carried a prior value (restored
       verbatim). One INFO line names the model, the located weight total
       and the offline construction.
  O-B **No environment manipulation when the weights are NOT locatable** —
       an un-pulled repo id leaves both variables exactly as they were
       (absent stays absent), the factory sees no offline mode, and there is
       exactly ONE construction attempt.
  O-C **The single cache-miss retry** — when the offline attempt fails
       (an incomplete snapshot ``estimate_weights_on_disk`` cannot detect,
       because verifying it against the repo manifest needs the network),
       the load is retried EXACTLY ONCE with the offline variables restored
       to their original values, and the retry's success is a normal READY.
  O-D **At most once, and never for a device-memory failure** — a second
       failure is passed to ``_fail`` with no third attempt, and a
       KV-cache-exhaustion / allocator-NVML failure is NOT retried at all
       (retrying it would repeat ~4 min of doomed profiling on a device with
       less memory than the first attempt had — defect 1.5's cascade).
  O-E **The environment is restored even when construction raises** — the
       restore lives in a ``finally``, so an exception cannot leak offline
       mode into unrelated work in the same process.

HONESTY GUARD (binding, design "Honesty Guard"). Nothing here loads a real
vLLM engine, allocates GPU memory, resolves a hostname or touches the
network: the engine is the manager's public ``engine_factory`` seam, the
"HF cache" is a sparse-file tree under ``tmp_path`` (an N-GiB
``*.safetensors`` costs no disk), and memory is the injected
``/proc/meminfo`` reader. What is proved here is the gate's decision logic
and its environment discipline — that an unreachable ``huggingface.co``
cannot fail an already-staged model is a **[HARDWARE]** claim.

Hypothesis conventions for the device suites (``--noconftest``, so no
profile is registered): ``@settings(deadline=None)`` with **no hardcoded
``max_examples``**, per-example unique tmp dirs.

Run (host-side, from the repo root):
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
      test/backend-test/jp6_vllm_kv_cache_oom/test_property_offline_cache_gate.py \
      -q -p no:cacheprovider --noconftest

_Requirements: 2.9, 3.8_
"""
import asyncio
import contextlib
import itertools
import logging
import os

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import vllm_runtime.manager as manager_module
from vllm_runtime.manager import ModelState, ModelStatus
from jp6_vllm_kv_cache_oom.fakes import (
    DEFAULT_MODEL_NAME,
    DEVICE_TOTAL_BYTES,
    GIB,
    HF_NAME_RESOLUTION_REASON,
    HF_OFFLINE_ENV_VARS,
    INCIDENT_ENGINE_ARGS,
    KV_OOM_REASON,
    NVML_ASSERT_REASON,
    FakeMeminfoReader,
    OfflineProbingEngineFactory,
    RecordingEngineFactory,
    build_staged_repo,
    hf_cache_tree,
    make_manager,
    observed_hf_offline_env,
    weight_tree,
)

#: The device's own model id (an HF repo id, the shape whose resolution
#: needs DNS) and its measured weight size.
REPO_ID = "Qwen/Qwen2.5-VL-7B-Instruct-AWQ"
WEIGHT_BYTES = int(6.45 * GIB)

#: Generous readings so the preflight never decides the outcome here.
_GENEROUS_READINGS = [(DEVICE_TOTAL_BYTES, 24 * GIB)]

_dir_counter = itertools.count()


@contextlib.contextmanager
def _collected_logs(level=logging.DEBUG):
    """Collect every log record emitted in the block, restoring the previous
    root configuration afterwards. Per-example safe."""
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


def _messages(records, level=None, source="vllm_runtime.manager"):
    return [record.getMessage() for record in records
            if (level is None or record.levelno == level)
            and record.name == source]


@contextlib.contextmanager
def _environment(values):
    """Set the offline-mode variables to ``values`` (``None`` == absent) for
    the block and restore the process environment exactly afterwards, so a
    test can start from a known prior state without leaking it."""
    saved = {name: os.environ.get(name) for name in HF_OFFLINE_ENV_VARS}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _staged_with_cached_weights(tmp_path, monkeypatch, model=REPO_ID):
    """A staged repository for ``model`` whose weights ARE in a fake HF hub
    cache (``models--{org}--{name}/snapshots/{rev}/``), with ``HF_HUB_CACHE``
    pointed at it — the "already staged, already pulled" device shape."""
    index = next(_dir_counter)
    root = tmp_path / "case-{}".format(index)
    cache_root = root / "hf-cache"
    hf_cache_tree(cache_root, model, WEIGHT_BYTES)
    monkeypatch.setenv("HF_HUB_CACHE", str(cache_root))
    repo = root / "repo"
    # 0.7, NOT the incident's 0.4: with the weights sized on disk the
    # device preflight legitimately refuses 0.4 before any engine exists
    # (task 4.4's territory), and these cases need construction to happen.
    build_staged_repo(repo, engine_args=dict(INCIDENT_ENGINE_ARGS,
                                             model=model,
                                             gpu_memory_utilization=0.7))
    return repo


def _staged_without_weights(tmp_path, model="example/never-pulled-model"):
    """A staged repository whose ``model`` is a repo id NOT on disk."""
    index = next(_dir_counter)
    repo = tmp_path / "uncached-{}".format(index) / "repo"
    build_staged_repo(repo, engine_args=dict(INCIDENT_ENGINE_ARGS,
                                             model=model))
    return repo


def _manager(repo, factory):
    return make_manager(repo, factory,
                        memory_reader=FakeMeminfoReader(_GENEROUS_READINGS))


# ---------------------------------------------------------------------------
# O-A — applied for the construction, restored exactly afterwards
# ---------------------------------------------------------------------------

#: Prior environment states the restore must reproduce EXACTLY: absent
#: (deleted again, never left as ""), an explicit off switch, an explicit
#: on switch, and an empty string.
_PRIOR_STATES = (
    {"HF_HUB_OFFLINE": None, "TRANSFORMERS_OFFLINE": None},
    {"HF_HUB_OFFLINE": "0", "TRANSFORMERS_OFFLINE": "0"},
    {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": None},
    {"HF_HUB_OFFLINE": "", "TRANSFORMERS_OFFLINE": "0"},
)


@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(prior=st.sampled_from(_PRIOR_STATES))
def test_property_offline_mode_is_applied_then_restored_exactly(
        tmp_path, monkeypatch, prior):
    """O-A. For any prior state of the two offline variables, a load whose
    weights are already in the HF cache constructs the engine with BOTH
    variables set to ``"1"`` and then restores the prior state EXACTLY — a
    variable that was absent is absent again (never ``""``), a variable that
    had a value gets that value back.

    # Validates: Requirements 2.9, 3.8
    """
    repo = _staged_with_cached_weights(tmp_path, monkeypatch)
    factory = RecordingEngineFactory()
    manager = _manager(repo, factory)

    with _environment(prior):
        with _collected_logs() as records:
            status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
        after = observed_hf_offline_env()

    assert status == ModelStatus(ModelState.READY), status
    assert factory.call_count == 1, factory.call_count
    # Applied INSIDE the construction...
    assert factory.observed_env[0] == {name: "1"
                                      for name in HF_OFFLINE_ENV_VARS}, \
        factory.observed_env
    # ...and the prior state is back, byte for byte.
    assert after == prior, (
        "the offline variables were not restored exactly: {!r} != {!r}"
        .format(after, prior))
    for name, value in prior.items():
        if value is None:
            assert name not in os.environ, (
                "{} was left behind (as {!r}) after construction".format(
                    name, os.environ.get(name)))

    infos = _messages(records, logging.INFO)
    offline_lines = [message for message in infos
                     if "offline mode" in message]
    assert len(offline_lines) == 1, offline_lines
    message = offline_lines[0]
    assert DEFAULT_MODEL_NAME in message, message
    assert "6.45 GiB" in message, (
        "the INFO line does not name the located weight total: {!r}".format(
            message))
    assert "huggingface.co" in message, message
    for name in HF_OFFLINE_ENV_VARS:
        assert name in message, message


def test_local_weights_directory_also_enables_offline_mode(tmp_path):
    """O-A, local-directory leg. The other staged ``model`` shape — an
    S3-sourced rewritten local path — is equally "already on disk", so it
    takes the same offline construction.

    # Validates: Requirements 2.9
    """
    root = tmp_path / "local-{}".format(next(_dir_counter))
    weights = weight_tree(root / "weights", WEIGHT_BYTES)
    repo = root / "repo"
    build_staged_repo(repo, engine_args=dict(INCIDENT_ENGINE_ARGS,
                                             model=str(weights),
                                             gpu_memory_utilization=0.7))
    factory = RecordingEngineFactory()
    manager = _manager(repo, factory)

    with _environment({name: None for name in HF_OFFLINE_ENV_VARS}):
        status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
        after = observed_hf_offline_env()

    assert status == ModelStatus(ModelState.READY), status
    assert factory.observed_env[0] == {name: "1"
                                       for name in HF_OFFLINE_ENV_VARS}
    assert after == {name: None for name in HF_OFFLINE_ENV_VARS}


# ---------------------------------------------------------------------------
# O-B — no environment manipulation when the weights are absent
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(prior=st.sampled_from(_PRIOR_STATES))
def test_property_uncached_weights_leave_the_environment_untouched(
        tmp_path, prior):
    """O-B. When the weights cannot be located on disk the behaviour is
    UNCHANGED: no environment manipulation at all (the factory observes
    exactly the prior state), a single construction attempt, and no offline
    INFO line. A first-time download must not be forced offline.

    # Validates: Requirements 2.9, 3.8
    """
    repo = _staged_without_weights(tmp_path)
    factory = RecordingEngineFactory()
    manager = _manager(repo, factory)

    with _environment(prior):
        with _collected_logs() as records:
            status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
        after = observed_hf_offline_env()

    assert status == ModelStatus(ModelState.READY), status
    assert factory.call_count == 1, factory.call_count
    assert factory.observed_env[0] == prior, (
        "the load manipulated the offline variables for a model whose "
        "weights are not on disk: {!r}".format(factory.observed_env))
    assert after == prior, after
    assert [message for message in _messages(records, logging.INFO)
            if "offline mode" in message] == []


# ---------------------------------------------------------------------------
# O-C / O-D — the single cache-miss retry, and its bounds
# ---------------------------------------------------------------------------

def test_cache_miss_is_retried_exactly_once_with_the_environment_restored(
        tmp_path, monkeypatch):
    """O-C. ``estimate_weights_on_disk`` sizes weight FILES; it does not
    verify the snapshot against the repo manifest (that needs the network),
    so an incomplete cache is possible. When the offline attempt fails, the
    load is retried EXACTLY ONCE with the offline variables restored to
    their original values — the retry sees no offline mode, reaches READY,
    and a WARNING records why the retry happened.

    # Validates: Requirements 2.9, 3.8
    """
    repo = _staged_with_cached_weights(tmp_path, monkeypatch)
    factory = OfflineProbingEngineFactory(
        fail_times=1, reason=HF_NAME_RESOLUTION_REASON)
    manager = _manager(repo, factory)

    with _environment({name: None for name in HF_OFFLINE_ENV_VARS}):
        with _collected_logs() as records:
            status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
        after = observed_hf_offline_env()

    assert status == ModelStatus(ModelState.READY), status
    assert factory.call_count == 2, (
        "expected exactly one retry after the offline attempt failed, got "
        "{} construction(s)".format(factory.call_count))
    # Attempt 1 offline, attempt 2 with the ORIGINAL (absent) values back.
    assert factory.observed_env[0] == {name: "1"
                                       for name in HF_OFFLINE_ENV_VARS}
    assert factory.observed_env[1] == {name: None
                                       for name in HF_OFFLINE_ENV_VARS}, (
        "the retry did not restore the original offline values: {!r}"
        .format(factory.observed_env))
    assert after == {name: None for name in HF_OFFLINE_ENV_VARS}, after

    warnings = [message for message in _messages(records, logging.WARNING)
                if "offline mode" in message]
    assert len(warnings) == 1, warnings
    assert DEFAULT_MODEL_NAME in warnings[0], warnings[0]
    assert "retried ONCE" in warnings[0], warnings[0]


def test_a_second_failure_is_final_and_there_is_no_third_attempt(
        tmp_path, monkeypatch):
    """O-D. The retry happens AT MOST once: when the second construction
    fails too, only that failure is passed to ``_fail`` (FAILED with the
    backend reason retained) and no third attempt is made — a retry loop
    here would reintroduce the ~4 min profiling cost repeatedly.

    # Validates: Requirements 2.9, 3.8
    """
    repo = _staged_with_cached_weights(tmp_path, monkeypatch)
    factory = OfflineProbingEngineFactory(
        fail_times=5, reason=HF_NAME_RESOLUTION_REASON)
    manager = _manager(repo, factory)

    with _environment({name: None for name in HF_OFFLINE_ENV_VARS}):
        status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
        after = observed_hf_offline_env()

    assert status.state is ModelState.FAILED, status
    assert factory.call_count == 2, (
        "expected exactly two construction attempts (offline + one retry), "
        "got {}".format(factory.call_count))
    assert "Failed to resolve" in status.reason, status.reason
    assert after == {name: None for name in HF_OFFLINE_ENV_VARS}, (
        "offline mode leaked out of a failed load: {!r}".format(after))


@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(reason=st.sampled_from((KV_OOM_REASON, NVML_ASSERT_REASON)))
def test_property_device_memory_failures_are_never_retried(
        tmp_path, monkeypatch, reason):
    """O-D, scoping half. A KV-cache exhaustion or an allocator/NVML fault
    is a DEVICE-MEMORY failure, not a cache miss: it is NOT retried, so the
    "no retry into a starved device" contract (defect 1.5, Decision 5)
    holds and no second ~4 min profiling is spent on a doomed load.

    # Validates: Requirements 2.5, 2.9
    """
    repo = _staged_with_cached_weights(tmp_path, monkeypatch)
    factory = OfflineProbingEngineFactory(fail_times=5, reason=reason)
    manager = _manager(repo, factory)

    with _environment({name: None for name in HF_OFFLINE_ENV_VARS}):
        status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
        after = observed_hf_offline_env()

    assert status.state is ModelState.FAILED, status
    assert factory.call_count == 1, (
        "a device-memory failure ({!r}) was retried: {} construction(s)"
        .format(reason[:40], factory.call_count))
    assert reason in status.reason, status.reason
    assert after == {name: None for name in HF_OFFLINE_ENV_VARS}, after


# ---------------------------------------------------------------------------
# O-E — the restore survives an exception, and the constant is documented
# ---------------------------------------------------------------------------

def test_offline_mode_never_leaks_when_construction_explodes(
        tmp_path, monkeypatch):
    """O-E. The restore lives in a ``finally``: an engine factory that
    raises a non-``Exception`` control-flow error (the shape that skips
    every ``except Exception``) still leaves the process environment exactly
    as it was, so offline mode cannot leak into unrelated work in the same
    process.

    # Validates: Requirements 2.9, 3.8
    """
    repo = _staged_with_cached_weights(tmp_path, monkeypatch)

    seen = {}

    def exploding_factory(engine_args):
        seen.update(observed_hf_offline_env())
        raise KeyboardInterrupt("operator interrupt mid-construction")

    manager = _manager(repo, exploding_factory)

    with _environment({"HF_HUB_OFFLINE": "0", "TRANSFORMERS_OFFLINE": None}):
        try:
            asyncio.run(manager.load(DEFAULT_MODEL_NAME))
        except KeyboardInterrupt:
            pass
        after = observed_hf_offline_env()

    assert seen == {name: "1" for name in HF_OFFLINE_ENV_VARS}, seen
    assert after == {"HF_HUB_OFFLINE": "0",
                     "TRANSFORMERS_OFFLINE": None}, after


def test_the_offline_variables_are_named_in_one_documented_constant():
    """O-E, provenance half. The two variables live in a single module-level
    constant so the next reader finds the defect that motivated them
    (three ``Failed to resolve 'huggingface.co'`` load failures took the
    component BROKEN with the model already staged) instead of
    "simplifying" the gate away.

    # Validates: Requirements 2.9
    """
    assert manager_module.HF_OFFLINE_ENV_VARS == ("HF_HUB_OFFLINE",
                                                  "TRANSFORMERS_OFFLINE")
    source = manager_module.__file__
    with open(source, "r") as handle:
        text = handle.read()
    marker = text.split("HF_OFFLINE_ENV_VARS = ")[0]
    assert "huggingface.co" in marker, (
        "the offline-mode constant carries no provenance comment citing "
        "this defect")
    assert "name resolution" in marker, marker[-2000:]
