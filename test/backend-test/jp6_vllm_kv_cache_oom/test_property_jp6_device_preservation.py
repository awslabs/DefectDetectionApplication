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
"""Preservation PROPERTIES, DEVICE half (spec:
jp6-vllm-kv-cache-oom-regression, task 2).

**Property 2: Preservation — for every input where the bug condition does
NOT hold, the fixed device-side pipeline produces the same result as the
original** (design "Preservation Checking":
``StagedArgs(X)|5keys = StagedArgs'(X)|5keys``,
``PrepExit(X) = PrepExit'(X)``).

Why property-based here (task 2, design "Testing Approach"): the preserved
device surface is a wide input space — arbitrary staged engine-arg overlays,
arbitrary Triton error bodies, arbitrary load classifications, arbitrary KV
geometries — where hand-picked examples miss edge cases. The example-based
baselines live in the sibling
``test_preservation_jp6_kv_cache_oom.py``; this file generalizes them.

Properties (each holds on the UNFIXED tree today and must keep holding):
  P-A **Authored args reach the engine verbatim** [3.3, 3.8] — every key the
       staged ``model.json`` carries appears in the constructed engine args
       with the identical value. (Deliberately NOT "no extra keys": the
       injected ``limit_mm_per_prompt`` default is the DEFECT, owned by the
       exploration suite's case 4 — a preservation test must not freeze it.)
  P-B **Prep exit-code mapping** [3.8] — ``LOAD_OK`` → 0, every other
       existing classification → 1.
  P-C **KV-OOM recovery fires exactly once** [3.8] — for any 409 body whose
       reason matches ``KV_CACHE_HINT_MARKERS`` the request sequence is
       ``load, unload, load``; for any body that does not, it is ``load``.
  P-D **Reason extraction** [3.8] — the ``{"error": ...}`` field when
       present and non-empty, else the stripped raw body; never an
       exception.
  P-E **Healthy loads stay silent** [2.7 inverse] — for any AMPLE KV
       geometry the load reaches READY with the byte-identical READY line
       and NO warning.
  P-F **Text-only invocations are untouched** [3.9] — with no image the
       engine receives the bare prompt string, character-identical.

HONESTY GUARD (binding). No real vLLM engine, no GPU allocation, no
CUDA/NVML, no Jetson unified-memory simulation: the engine is the manager's
public ``engine_factory`` seam, KV sizing is a fake ``cache_config``, and
the prep's HTTP layer is a monkeypatched ``requests``. The GPU-only claims
are the [HARDWARE] H1-H8 tasks'.

Hypothesis conventions for the device suites (``--noconftest``, so no
profile is registered): ``@settings(deadline=None)`` with **no hardcoded
``max_examples``**, matching
``test/backend-test/vllm_model_reload/test_property_*.py``.

Run (host-side, from the repo root):
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
      test/backend-test/jp6_vllm_kv_cache_oom/test_property_jp6_device_preservation.py \
      -q -p no:cacheprovider --noconftest

_Requirements: 3.3, 3.7, 3.8, 3.9_
"""
import argparse
import asyncio
import json
import logging

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import dda_triton.vllm_model_prep as mp
from vllm_runtime.manager import ModelState
from jp6_vllm_kv_cache_oom.fakes import (
    DEFAULT_MODEL_NAME,
    INCIDENT_ENGINE_ARGS,
    KV_OOM_REASON,
    RecordingEngineFactory,
    build_staged_repo,
    healthy_cache_config,
    make_manager,
)

#: The five PRE-EXISTING engine settings (the authored contract this spec
#: must not disturb). ``limit_mm_per_prompt`` is deliberately absent: it
#: becomes an authored setting in task 3.1 and is additive.
PRE_EXISTING_ENGINE_KEYS = ("dtype", "gpu_memory_utilization",
                            "max_model_len", "tensor_parallel_size",
                            "enforce_eager")


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_dtypes = st.sampled_from(("auto", "float16", "bfloat16", "float32"))
_utilizations = st.floats(min_value=0.05, max_value=1.0,
                          allow_nan=False, allow_infinity=False
                          ).map(lambda x: round(x, 3))
_model_lens = st.integers(min_value=256, max_value=32768)


@st.composite
def staged_engine_args(draw):
    """A staged ``model.json`` object: the ``model`` reference plus an
    arbitrary subset of the five pre-existing settings (the shapes
    packaging.py actually emits), optionally authoring
    ``limit_mm_per_prompt`` explicitly."""
    args = {"model": draw(st.sampled_from((
        "Qwen/Qwen2.5-VL-7B-Instruct-AWQ",
        "example/small-llm",
        "/aws_dda/weights/model-x",
    )))}
    if draw(st.booleans()):
        args["dtype"] = draw(_dtypes)
    if draw(st.booleans()):
        args["gpu_memory_utilization"] = draw(_utilizations)
    if draw(st.booleans()):
        args["max_model_len"] = draw(_model_lens)
    if draw(st.booleans()):
        args["tensor_parallel_size"] = draw(st.integers(min_value=1,
                                                        max_value=4))
    if draw(st.booleans()):
        args["enforce_eager"] = draw(st.booleans())
    if draw(st.booleans()):
        args["limit_mm_per_prompt"] = {"image": draw(
            st.integers(min_value=1, max_value=4))}
    return args


#: Reasons that DO match the prep's KV-cache markers (the recovery arm) and
#: reasons that do NOT (the single-attempt arm).
_kv_reasons = st.one_of(
    st.just(KV_OOM_REASON),
    st.just("load failed for model 'm': version 1 is at UNAVAILABLE state: "
            "Internal: ValueError: No available memory for the cache blocks."),
    st.text(max_size=20).map(
        lambda prefix: prefix + " try increasing `gpu_memory_utilization`"),
)
_non_kv_reasons = st.sampled_from((
    "unknown model",
    "load failed for model 'm': version 1 is at UNAVAILABLE state: "
    "Internal: unexpected error",
    'NVML_SUCCESS == r INTERNAL ASSERT FAILED at '
    '"/opt/pytorch/c10/cuda/CUDACachingAllocator.cpp":1131',
    "invalid argument",
))


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


# ---------------------------------------------------------------------------
# P-A — authored args reach the engine verbatim (3.3, 3.8)
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(engine_args=staged_engine_args())
def test_property_authored_engine_args_reach_the_engine_verbatim(
        tmp_path, engine_args):
    """P-A. OBSERVED on the unfixed tree and preserved: for any staged
    ``model.json``, every authored key arrives at the engine factory with
    the identical value — the load path never rewrites or drops an authored
    setting. (What it may currently ADD — the unbudgeted
    ``limit_mm_per_prompt`` default — is the defect, asserted by the
    exploration suite; freezing it here would preserve the bug.)

    _Requirements: 3.3, 3.8_"""
    model_dir = tmp_path / "repo-{}".format(abs(hash(json.dumps(
        engine_args, sort_keys=True))))
    build_staged_repo(model_dir, engine_args=engine_args)
    factory = RecordingEngineFactory()
    manager = make_manager(model_dir, factory)

    status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    assert status.state is ModelState.READY, status.reason
    assert factory.call_count == 1
    recorded = factory.calls[0]
    for key, value in engine_args.items():
        assert key in recorded, (
            "authored engine setting {!r} never reached the engine: "
            "{!r}".format(key, recorded))
        assert recorded[key] == value, (
            "authored engine setting {!r} was rewritten: staged {!r} -> "
            "engine {!r}".format(key, value, recorded[key]))
    # The manager's tracked args (what request validation reads) agree.
    tracked = manager.engine_args(DEFAULT_MODEL_NAME)
    for key, value in engine_args.items():
        assert tracked[key] == value, key


# ---------------------------------------------------------------------------
# P-B — prep exit-code mapping (3.8)
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(classification=st.sampled_from((mp.LOAD_OK, mp.LOAD_UNREACHABLE,
                                       mp.LOAD_HTTP_ERROR)),
       engine_args=staged_engine_args())
def test_property_prep_exit_code_mapping_is_preserved(
        tmp_path, monkeypatch, classification, engine_args):
    """P-B. OBSERVED and preserved (3.8): whatever the staged args,
    ``LOAD_OK`` exits 0 and ``LOAD_UNREACHABLE`` exits 1 — the contract
    Greengrass' Startup retry behavior depends on — and the staged args
    still travel into the load path verbatim.

    CONSCIOUS REPOINT (task 14 H11 dispatch; task 11 OUTCOME block 18):
    ``LOAD_HTTP_ERROR`` now exits **0**. Verbatim original::

        \"\"\"P-B. OBSERVED and preserved (3.8): whatever the staged args,
        ``LOAD_OK`` exits 0 and every other existing classification exits 1 —
        the contract Greengrass' Startup retry behavior depends on.

        _Requirements: 3.8_\"\"\"
        ...
        assert exit_code == (0 if classification == mp.LOAD_OK else 1), (
            "{} -> exit {}".format(classification, exit_code))

    Reason: an authoritative runtime answer is a MODEL failure and is
    reported as one; failing the COMPONENT took two HARD-dependent workflows
    and the whole device down for a transient DNS fault. The
    ``LOAD_UNREACHABLE`` -> 1 arm is unchanged and still asserted.

    _Requirements: 3.8_"""
    repo = tmp_path / "unarchived"
    build_staged_repo(repo, DEFAULT_MODEL_NAME, engine_args)
    monkeypatch.setattr(mp, "stage_repository",
                        lambda *a, **k: str(tmp_path / "staged"))
    seen = {}

    def fake_request_load(model_name, staged=None):
        seen["staged"] = staged
        return classification

    monkeypatch.setattr(mp, "request_load", fake_request_load)

    exit_code = mp.prepare(_args(unarchived_repo_path=str(repo),
                                 model_name=DEFAULT_MODEL_NAME))

    assert exit_code == (1 if classification == mp.LOAD_UNREACHABLE else 0), (
        "{} -> exit {}".format(classification, exit_code))
    # The staged args travel into the load path verbatim (Requirement 4.4
    # of the sibling spec; the failure log reads them).
    assert seen["staged"] == engine_args, seen["staged"]


# ---------------------------------------------------------------------------
# P-C — the KV-OOM recovery fires exactly once (3.8)
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(reason=_kv_reasons)
def test_property_kv_oom_reasons_trigger_exactly_one_recovery_cycle(reason):
    """P-C (recovery arm). OBSERVED and preserved (3.8): any authoritative
    409 whose reason matches ``KV_CACHE_HINT_MARKERS`` produces exactly the
    sequence ``load, unload, load`` — one recovery cycle, never two.

    Patching runs inside a per-example ``MonkeyPatch`` context (not the
    function-scoped fixture) so every generated input starts from clean
    module state.

    _Requirements: 3.8_"""
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

    assert outcome == mp.LOAD_HTTP_ERROR
    assert [url.rsplit("/", 1)[-1] for url in urls] == [
        "load", "unload", "load"], (reason, urls)


@settings(deadline=None)
@given(reason=_non_kv_reasons, status=st.sampled_from((400, 404, 409, 500)))
def test_property_non_kv_reasons_stay_single_attempt(reason, status):
    """P-C (single-attempt arm). OBSERVED and preserved (3.8): a reason that
    does not match the KV markers is authoritative on the first response —
    exactly one ``load`` request, no unload, no retry. This is also what
    keeps task 3.6's category tokens honest: the classifier must leave the
    original reason text intact AFTER its token, or this property flips.

    _Requirements: 3.8_"""
    urls = []

    def scripted_post(url, timeout=None):
        urls.append(url)
        return _Response(status, json.dumps({"error": reason}))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mp, "wait_for_server", lambda *a, **k: True)
        patch.setattr(mp.requests, "post", scripted_post)
        outcome = mp.request_load(DEFAULT_MODEL_NAME)

    assert outcome == mp.LOAD_HTTP_ERROR
    assert [url.rsplit("/", 1)[-1] for url in urls] == ["load"], (reason, urls)


# ---------------------------------------------------------------------------
# P-D — reason extraction (3.8)
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(body=st.one_of(
    st.text(max_size=120),
    st.dictionaries(st.text(max_size=8), st.text(max_size=40), max_size=4
                    ).map(json.dumps),
    st.lists(st.text(max_size=8), max_size=3).map(json.dumps),
    st.text(max_size=60).map(lambda reason: json.dumps({"error": reason})),
))
def test_property_reason_extraction_is_preserved(body):
    """P-D. OBSERVED and preserved (3.8): extraction returns the ``error``
    field when the body is a JSON object carrying a non-empty ``error``,
    otherwise the stripped raw body — and never raises, for any body.

    _Requirements: 3.8_"""
    reason = mp.extract_load_failure_reason(body)
    assert isinstance(reason, str)

    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("error"):
        assert reason == str(parsed["error"])
    else:
        assert reason == body.strip()


# ---------------------------------------------------------------------------
# P-E — healthy loads stay silent (2.7 inverse)
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(num_gpu_blocks=st.integers(min_value=8000, max_value=60000),
       block_size=st.sampled_from((16, 32)))
def test_property_ample_kv_loads_emit_no_warning(tmp_path, num_gpu_blocks,
                                                 block_size):
    """P-E. OBSERVED and preserved: for any AMPLE KV geometry (≥ 8,000
    blocks — ≥ 128,000 tokens, ≥ 31x concurrency at ``max_model_len =
    4096``) the load reaches READY, emits the byte-identical
    ``vLLM model '<name>' is READY`` line, and emits NO warning.

    Task 3.6 adds a thin-margin WARNING; this property is the fence that
    keeps it off healthy loads.

    _Requirements: 3.9_"""
    model_dir = tmp_path / "repo-{}-{}".format(num_gpu_blocks, block_size)
    build_staged_repo(model_dir, engine_args=INCIDENT_ENGINE_ARGS)
    factory = RecordingEngineFactory(
        cache_config=healthy_cache_config(num_gpu_blocks=num_gpu_blocks,
                                          block_size=block_size))
    manager = make_manager(model_dir, factory)

    logger = logging.getLogger("vllm_runtime.manager")
    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collector()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    assert status.state is ModelState.READY, status.reason
    warnings = [record.getMessage() for record in records
                if record.levelno >= logging.WARNING]
    assert warnings == [], (
        "an ample-KV load ({} blocks × {} tokens) emitted warning(s): "
        "{!r}".format(num_gpu_blocks, block_size, warnings))
    messages = [record.getMessage() for record in records]
    assert "vLLM model '{}' is READY".format(DEFAULT_MODEL_NAME) in messages, \
        messages


# ---------------------------------------------------------------------------
# P-F — text-only invocations are untouched (3.9)
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(prompt=st.text(min_size=1, max_size=120),
       engine_args=staged_engine_args())
def test_property_text_only_prompts_are_passed_through_unchanged(
        tmp_path, prompt, engine_args):
    """P-F. OBSERVED and preserved (3.9): an image-less generate hands the
    engine the bare prompt STRING, character-identical, for any staged args
    — including args authoring a multimodal limit. No multimodal change may
    perturb the text-only path.

    _Requirements: 3.9_"""
    model_dir = tmp_path / "repo-textonly"
    build_staged_repo(model_dir, engine_args=engine_args)
    factory = RecordingEngineFactory()
    manager = make_manager(model_dir, factory)
    asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    asyncio.run(manager.generate(DEFAULT_MODEL_NAME, prompt))

    engine = factory.engines[0]
    assert engine.prompts == [prompt], (
        "the text-only prompt was transformed: {!r}".format(engine.prompts))


# ---------------------------------------------------------------------------
# Fixed-shape leg — ABSENT on the unfixed tree, binds at task 3.9
# ---------------------------------------------------------------------------

@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(engine_args=staged_engine_args())
def test_property_no_key_is_ever_injected_once_the_fix_lands(tmp_path,
                                                            engine_args):
    """FIXED-SHAPE LEG (binds at task 3.9; design Property 4). Once task 3.6
    removes the ``setdefault``, the constructed engine args must contain
    NOTHING the staged ``model.json`` did not author. SKIPPED as absent
    today: on the unfixed tree ``limit_mm_per_prompt = {"image": 2}`` is
    injected whenever the staged args omit it — which is exactly defect 1.4
    and is asserted by the exploration suite's case 4.

    _Requirements: 2.4, 3.9_"""
    model_dir = tmp_path / "repo-injection"
    build_staged_repo(model_dir, engine_args=engine_args)
    factory = RecordingEngineFactory()
    manager = make_manager(model_dir, factory)
    asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    recorded = factory.calls[0]

    injected = sorted(set(recorded) - set(engine_args))
    if injected == ["limit_mm_per_prompt"] and \
            "limit_mm_per_prompt" not in engine_args:
        pytest.skip("fixed-shape leg: the unbudgeted limit_mm_per_prompt "
                    "default is still injected (binds at task 3.9)")
    assert injected == [], (
        "the load path injected engine args the staged model.json never "
        "authored: {}".format(injected))
