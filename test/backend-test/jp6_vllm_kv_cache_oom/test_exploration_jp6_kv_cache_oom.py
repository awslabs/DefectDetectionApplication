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
"""Bug-condition exploration, DEVICE half (spec:
jp6-vllm-kv-cache-oom-regression, task 1).

**Property 1: Bug Condition — the device applies an unbudgeted multimodal
default, never checks the engine args against actual memory, retries into a
starved device, and reports thin margins and distinct faults
indistinguishably.**

Every case asserts the FIXED expected behavior, so on the UNFIXED tree all
six are EXPECTED TO FAIL — each failure is the counterexample for one
defect leg of the ryanorinagxdevkithomelabjp622 2026-08-17 incident
(``model weights take 6.59GiB; non_torch_memory takes 8.29GiB; PyTorch
activation peak memory takes 4.93GiB; the rest of the memory reserved for
KV Cache is -7.83GiB`` → HTTP 409 ``No available memory for the cache
blocks``; three failed loads → 26 GB used / 3 GB free with NO model
loaded):

- Case 4 (defect 1.4) — ``manager.load`` with staged args that omit
  ``limit_mm_per_prompt`` must not inject one. Unfixed: commit ``086c251``'s
  ``setdefault`` puts ``{"image": 2}`` into the engine args, so 1.0.61
  profiles a vision-language engine for TWO images where 1.0.59 profiled
  for one, inside the same 11.98 GiB budget.
- Case 5 (defects 1.10, 2.9) — with 3 GB available, ``load`` must refuse
  before engine construction. Unfixed: no preflight exists at all; the
  factory is called and the device pays ~4 min of profiling for a doomed
  load while the runtime server's event loop is blocked.
- Case 6 (defects 1.5, 2.5) — two consecutive failing loads whose memory
  does not come back must set the Starvation_Latch and refuse the second.
  Unfixed: no latch; every retry starts with less memory than the last.
- Case 7 (defects 1.7, 2.7) — a load that reaches READY below the KV floor
  must WARN. Unfixed: only the unqualified ``is READY`` INFO line.
- Case 8 (defects 1.6, 2.6) — the NVML allocator assert and KV-cache
  exhaustion must carry distinct category tokens. Unfixed: both are raw
  reasons, indistinguishable to every consumer.
- Case 9 (expected 2.4, preservation 3.9, edge case) — a reference-image
  request against a model authored for ONE image must fail truthfully.
  Unfixed: recorded exactly as it behaves.

HONESTY GUARD (binding). Nothing here loads a real vLLM engine, allocates
GPU memory, touches CUDA/NVML, or reproduces Jetson unified-memory
accounting. Memory is INJECTED through a fake ``/proc/meminfo`` reader, the
engine is the manager's public ``engine_factory`` seam, KV sizing is a fake
``cache_config``, and the images are real tiny PNGs decoded by the real
PIL. These cases prove decision logic, message content and classification
— nothing about a GPU. The GPU claims belong to the [HARDWARE] tasks
(H1-H5).

Run (host-side, from the repo root):
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
      test/backend-test/jp6_vllm_kv_cache_oom/test_exploration_jp6_kv_cache_oom.py \
      -q -p no:cacheprovider --noconftest

_Requirements: 1.4, 1.5, 1.6, 1.7, 1.10_
"""
import asyncio
import logging

import pytest

from vllm_runtime.manager import GenerationError, ModelState
from jp6_vllm_kv_cache_oom.fakes import (
    DEFAULT_MODEL_NAME,
    DEVICE_TOTAL_BYTES,
    GIB,
    INCIDENT_ENGINE_ARGS,
    KV_OOM_REASON,
    NVML_ASSERT_REASON,
    FailingEngineFactory,
    FakeMeminfoReader,
    RecordingEngineFactory,
    build_staged_repo,
    install_memory_reader,
    make_manager,
    png_bytes,
    thin_cache_config,
    weight_tree,
)

#: The incident's measured weights (``Model loading took 6.4689 GiB``).
INCIDENT_WEIGHTS_BYTES = int(6.5 * GIB)


# ---------------------------------------------------------------------------
# Case 4 — the unbudgeted multimodal default (defect 1.4)
# ---------------------------------------------------------------------------

def test_case4_staged_args_omitting_limit_mm_per_prompt_stay_without_it(
        tmp_path):
    """The staged ``model.json`` on the device does NOT set
    ``limit_mm_per_prompt`` (bugfix.md "Staged engine args, verbatim"), so
    the engine must be constructed without the key — the effective limit is
    vLLM's own default (one image), which is what 1.0.59 profiled for and
    what the published configuration was sized against.

    UNFIXED counterexample: ``manager.load`` runs
    ``engine_args.setdefault("limit_mm_per_prompt", {"image": 2})``
    (commit ``086c251``), so the engine is asked to profile for two images
    inside an unchanged ``gpu_memory_utilization = 0.4`` budget whose
    one-image activation peak was already 4.92 GiB of 11.98 GiB.
    """
    build_staged_repo(tmp_path, engine_args=INCIDENT_ENGINE_ARGS)
    factory = RecordingEngineFactory()
    manager = make_manager(tmp_path, factory)

    status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    assert status.state is ModelState.READY, (
        "harness precondition: the fake load should reach READY, got "
        "{}: {}".format(status.state, status.reason))
    assert factory.call_count == 1
    recorded = factory.calls[0]
    assert "limit_mm_per_prompt" not in recorded, (
        "the runtime injected a multimodal limit the staged model.json never "
        "specified: recorded engine args carry limit_mm_per_prompt={!r} "
        "(staged args: {!r})".format(
            recorded.get("limit_mm_per_prompt"), dict(INCIDENT_ENGINE_ARGS)))
    assert manager.engine_args(DEFAULT_MODEL_NAME).get(
        "limit_mm_per_prompt") is None, (
        "the tracked engine args carry an injected multimodal limit: "
        "{!r}".format(manager.engine_args(DEFAULT_MODEL_NAME)))


# ---------------------------------------------------------------------------
# Case 5 — no device-side preflight (defects 1.10, 2.9)
# ---------------------------------------------------------------------------

def test_case5_load_refuses_before_engine_construction_when_memory_is_short(
        tmp_path, monkeypatch):
    """With an injected reading of ~3 GB available on a ~30 GiB device, a
    6.5 GiB model at ``gpu_memory_utilization = 0.4`` cannot possibly load
    (required ≈ weights 6.5 + activation ≈4.88 + KV floor 1 = 12.38 GiB
    against both a 3 GiB measured availability and a 11.98 GiB budget). The
    load must be refused BEFORE engine construction, with a reason carrying
    the ``preflight-refused:`` marker, the measured availability and the
    setting to change.

    UNFIXED counterexample: no code path anywhere reads free/total device
    memory before requesting a load (defect 1.10), so the engine factory is
    called and the device pays the full ~4 min profiling run — with the
    runtime server unresponsive throughout (``/v2/repository/index`` empty
    for ~12 min on the 21:53Z attempt) — before failing the deployment.
    """
    weights = weight_tree(tmp_path / "weights", INCIDENT_WEIGHTS_BYTES)
    engine_args = dict(INCIDENT_ENGINE_ARGS, model=str(weights))
    build_staged_repo(tmp_path / "repo", engine_args=engine_args)

    reader = FakeMeminfoReader([(DEVICE_TOTAL_BYTES, 3 * GIB)])
    seams = install_memory_reader(monkeypatch, reader, tmp_path)
    factory = RecordingEngineFactory()
    manager = make_manager(tmp_path / "repo", factory, memory_reader=reader)

    status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    assert factory.call_count == 0, (
        "the engine factory was called with only 3.00 GiB available "
        "(required ≈12.38 GiB): no preflight refused the doomed load "
        "[memory seams installed: {}; readings observed: {}]".format(
            seams or "NONE — the tree has no memory-reading seam",
            reader.describe()))
    assert status.state is ModelState.FAILED, (
        "expected a refusal, got {}: {}".format(status.state, status.reason))
    assert status.reason is not None
    assert status.reason.startswith("preflight-refused:"), (
        "refusal reason does not carry the preflight marker: {!r}".format(
            status.reason))
    assert "3.00 GiB" in status.reason, (
        "refusal reason does not name the measured available memory: "
        "{!r}".format(status.reason))
    assert "gpu_memory_utilization" in status.reason \
        or "max_model_len" in status.reason, (
        "refusal reason names no engine setting to change: {!r}".format(
            status.reason))


# ---------------------------------------------------------------------------
# Case 6 — no retry into a starved device (defects 1.5, 2.5)
# ---------------------------------------------------------------------------

def test_case6_second_load_is_refused_when_memory_did_not_come_back(
        tmp_path, monkeypatch):
    """A failed load whose allocations were NOT reclaimed must latch that
    fact and refuse the next attempt with a diagnostic naming the starved
    condition, instead of retrying into a device that has less memory than
    the last attempt did.

    Injected readings: 20.00 GiB available before the first attempt,
    14.00 GiB after it failed (a 6 GiB shortfall, far beyond any reclaim
    tolerance) — the host-side analogue of the measured cascade (three
    failed loads → **26 GB used / 3 GB free with NO model loaded**; only a
    backend container restart returned the device to 6 GB used / 23 GB
    free).

    UNFIXED counterexample: no latch exists; the second ``load`` constructs
    another engine (``call_count == 2``) exactly as the device did on
    13:36:30Z / 13:39:38Z / 21:44Z, each attempt starting with less memory
    than the last.
    """
    # A small model so the (fixed) preflight itself admits both attempts:
    # this case isolates the latch, not the budget math.
    weights = weight_tree(tmp_path / "weights", 2 * GIB)
    engine_args = dict(INCIDENT_ENGINE_ARGS, model=str(weights))
    build_staged_repo(tmp_path / "repo", engine_args=engine_args)

    reader = FakeMeminfoReader([
        (DEVICE_TOTAL_BYTES, 20 * GIB),   # before the first attempt
        (DEVICE_TOTAL_BYTES, 14 * GIB),   # after it failed: not reclaimed
    ])
    seams = install_memory_reader(monkeypatch, reader, tmp_path)
    factory = FailingEngineFactory(KV_OOM_REASON)
    manager = make_manager(tmp_path / "repo", factory, memory_reader=reader)

    first = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
    assert first.state is ModelState.FAILED, (
        "harness precondition: the first load must fail, got {}".format(
            first.state))

    second = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    assert factory.call_count == 1, (
        "the manager retried into a starved device: the engine factory was "
        "called {} times although memory dropped from 20.00 GiB to 14.00 "
        "GiB across the failed attempt [memory seams installed: {}; "
        "readings observed: {}]".format(
            factory.call_count,
            seams or "NONE — the tree has no memory-reading seam",
            reader.describe()))
    assert second.state is ModelState.FAILED
    assert second.reason is not None
    assert "starv" in second.reason.lower(), (
        "the refusal does not name the starved condition: {!r}".format(
            second.reason))
    assert "14.00 GiB" in second.reason and "20.00 GiB" in second.reason, (
        "the refusal does not carry the two readings: {!r}".format(
            second.reason))


# ---------------------------------------------------------------------------
# Case 7 — thin margins are invisible (defects 1.7, 2.7)
# ---------------------------------------------------------------------------

def test_case7_ready_below_the_kv_floor_warns(tmp_path, caplog):
    """The retry that "worked" reached READY with ``the rest of the memory
    reserved for KV Cache is 0.65GiB`` against a 1 GiB floor — one retry and
    0.65 GiB from failing. That margin must surface as a WARNING, not as an
    unqualified success.

    The fake engine reports a KV sizing below the margin (340 GPU blocks ×
    16 tokens = 5,440 tokens, i.e. 1.33x concurrency at
    ``max_model_len = 4096``, ≈0.29 GiB of KV).

    UNFIXED counterexample: the only line emitted is
    ``vLLM model '<name>' is READY`` at INFO — a device that is one retry
    from failing looks perfectly healthy.
    """
    build_staged_repo(tmp_path, engine_args=INCIDENT_ENGINE_ARGS)
    factory = RecordingEngineFactory(cache_config=thin_cache_config())
    manager = make_manager(tmp_path, factory)

    with caplog.at_level(logging.DEBUG, logger="vllm_runtime.manager"):
        status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    assert status.state is ModelState.READY
    warnings = [record for record in caplog.records
                if record.levelno >= logging.WARNING]
    assert warnings, (
        "a load that reached READY with a KV margin below the floor "
        "(5,440 tokens ≈ 1.33x concurrency at max_model_len=4096, ≈0.29 GiB "
        "of KV against a 1 GiB floor) produced no WARNING; the only "
        "emitted lines were: {!r}".format(
            [record.getMessage() for record in caplog.records]))
    thin = [record.getMessage() for record in warnings
            if "KV" in record.getMessage()
            or "margin" in record.getMessage().lower()]
    assert thin, (
        "no WARNING names the thin KV margin; warnings were: {!r}".format(
            [record.getMessage() for record in warnings]))
    assert any(DEFAULT_MODEL_NAME in message for message in thin), (
        "the thin-margin WARNING does not name the model: {!r}".format(thin))


# ---------------------------------------------------------------------------
# Case 8 — indistinguishable symptoms (defects 1.6, 2.6)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason,expected_token", [
    (KV_OOM_REASON, "kv-cache-exhaustion:"),
    (NVML_ASSERT_REASON, "allocator-nvml-fault:"),
])
def test_case8_failure_reasons_carry_distinct_category_tokens(
        tmp_path, reason, expected_token):
    """The KV-cache exhaustion path (21:59:50Z, 22:12:16Z) and the NVML
    allocator INTERNAL ASSERT path (13:36:30Z, 13:39:38Z, 21:44Z) are
    different symptoms — whether the assert is the same exhaustion seen
    from the allocator or a distinct CUDA/NVML fault is UNRESOLVED
    [HARDWARE: H7]. They must at least be reported distinguishably, with
    the original reason text preserved verbatim so existing marker matching
    (the prep's ``KV_CACHE_HINT_MARKERS``) keeps working.

    UNFIXED counterexample: both reasons are stored and logged raw, so no
    consumer can tell an accounting fault from a budget fault.
    """
    build_staged_repo(tmp_path, engine_args=INCIDENT_ENGINE_ARGS)
    factory = FailingEngineFactory(reason)
    manager = make_manager(tmp_path, factory)

    status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    assert status.state is ModelState.FAILED
    assert status.reason is not None
    assert status.reason.startswith(expected_token), (
        "failure reason carries no category token: expected it to start "
        "with {!r}, got {!r}".format(expected_token, status.reason))
    assert reason in status.reason, (
        "the original backend reason was not preserved verbatim: "
        "{!r}".format(status.reason))


# ---------------------------------------------------------------------------
# Case 9 — edge case: a two-image request against a one-image model
# ---------------------------------------------------------------------------

def test_case9_reference_image_request_against_one_image_model_fails_truthfully(
        tmp_path):
    """When the multimodal limit becomes an authored, sized setting, a model
    published with ``limit_mm_per_prompt = {"image": 1}`` cannot serve a
    two-image anomaly-reference request. That request must fail with a
    ``GenerationError`` naming the model, the effective limit and the
    remediation — before the engine is invoked. Silently answering the
    one-image question would return a confident verdict about a DIFFERENT
    question, which is worse in a defect-detection product than a loud
    failure (design Decision 1).

    UNFIXED behavior, recorded exactly as observed (this case "fails
    differently", as design predicted): NOTHING is raised. The manager never
    consults ``limit_mm_per_prompt`` anywhere, so with the authored
    ``{"image": 1}`` staged (which the ``setdefault`` correctly leaves
    alone) the two-image prompt is still built —
    ``multi_modal_data["image"]`` is a TWO-element list and the prompt text
    is the ``Input image: … Reference image: …`` two-pad form — handed to
    the engine, and ``generate`` returns a normal completion. On the device
    that is a confident answer to a question the model was not sized for.
    """
    engine_args = dict(INCIDENT_ENGINE_ARGS,
                       limit_mm_per_prompt={"image": 1})
    build_staged_repo(tmp_path, engine_args=engine_args)
    factory = RecordingEngineFactory()
    manager = make_manager(tmp_path, factory)
    asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    with pytest.raises(GenerationError) as excinfo:
        asyncio.run(manager.generate(
            DEFAULT_MODEL_NAME,
            "Is the part defective compared with the reference?",
            image=png_bytes(),
            reference_image=png_bytes(color=(30, 200, 30)),
        ))

    message = str(excinfo.value)
    assert "limit_mm_per_prompt" in message, (
        "the failure does not name the authored limit: {!r}".format(message))
    assert DEFAULT_MODEL_NAME in message
    assert "re-publish" in message or "engine configuration" in message, (
        "the failure carries no remediation: {!r}".format(message))
    # The engine must never have seen the rejected two-image prompt.
    engine = factory.engines[0]
    assert engine.prompts == [], (
        "the engine was invoked with a two-image prompt the model is not "
        "sized for: {!r}".format(engine.prompts))
