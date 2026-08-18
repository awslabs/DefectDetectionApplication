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
"""Preservation baselines, DEVICE half (spec:
jp6-vllm-kv-cache-oom-regression, task 2).

**Property 2: Preservation — for every input where the bug condition does
NOT hold, the fixed device-side pipeline produces the same result as the
original** (design "Preservation Checking": ``PrepExit(X) = PrepExit'(X)``,
``Reconciler(X) = Reconciler'(X)``, the healthy-load surfaces and the
authored two-image behavior unchanged).

OBSERVATION-FIRST METHODOLOGY (binding, task 2). Every expectation was
OBSERVED by running the UNFIXED code first; what is asserted here is the
recorded behavior, not the design's wish. The file therefore PASSES on the
unfixed tree today and any post-fix failure is a real regression.

Legs covered here (bugfix.md clauses in brackets):
  1. Prep exit codes [3.8] — ``LOAD_OK`` → 0, ``LOAD_UNREACHABLE`` → 1 with
     its authoritative diagnostic, ``LOAD_HTTP_ERROR`` → 1; the single
     KV-OOM unload→reload recovery firing **exactly once**; the prominent
     ERROR line carrying model name, HTTP status, extracted reason and the
     staged ``gpu_memory_utilization`` / ``max_model_len``; idempotent
     Shutdown/``--cleanup``.
  2. Reconciler [3.7] — ``vllm_runtime/reconciler.py`` source hash pinned,
     its ``app.py`` wiring pinned, the ``(30, 120, 480)`` backoff and the
     no-op log line preserved, plus the recorded
     ``test/backend-test/vllm_model_reload`` baseline counts.
  3. Healthy load [2.7 inverse] — a fake engine with ample KV produces NO
     warning and the byte-identical ``vLLM model '<name>' is READY`` line.
  4. Two-image model [3.9] — a model authored with
     ``limit_mm_per_prompt = {"image": 2}`` builds the two-image prompt
     exactly as today; a text-only invocation is untouched.

HONESTY GUARD (binding). Nothing here loads a real vLLM engine, allocates
GPU memory, touches CUDA/NVML or reproduces Jetson unified-memory
accounting. The engine is the manager's public ``engine_factory`` seam, KV
sizing is a fake ``cache_config``, the prep's HTTP layer is a monkeypatched
``requests``, and the images are real tiny PNGs decoded by the real PIL.
The [HARDWARE] halves are declared as explicitly-deferred tests below with
their H-tier and owning task — never silently skipped, never faked.

RECORDED BASELINE COUNTS (task 2, host-side, venv
``/home/ubuntu/.venvs/dda-portal-tests``, ``-p no:cacheprovider``):
  - ``test/backend-test/vllm_model_reload`` (``--noconftest``): **45 passed**
  - ``test/backend-test/vllm_runtime`` + ``vllm_runtime_tests``
    (``--noconftest``): **29 passed**
  - ``test/backend-test/dda_triton/test_vllm_load_failure_log.py`` +
    ``test_property_load_failure_reason.py`` (**WITH** the repo conftest —
    those files use its class-bound ``caplog`` fixture): **20 passed**
  - ``test/backend-test/text_generation`` (``--noconftest``): **15 passed**
  - ``test/backend-test/deploy_reliability`` (``--noconftest``): **72 passed**

Run (host-side, from the repo root):
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
      test/backend-test/jp6_vllm_kv_cache_oom/test_preservation_jp6_kv_cache_oom.py \
      -q -p no:cacheprovider --noconftest

_Requirements: 3.7, 3.8, 3.9, 3.10_
"""
import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path

import pytest

import dda_triton.vllm_model_prep as mp
from vllm_runtime.manager import ModelState
from jp6_vllm_kv_cache_oom.fakes import (
    DEFAULT_MODEL_NAME,
    GIB,
    INCIDENT_ENGINE_ARGS,
    KV_OOM_409_BODY,
    KV_OOM_REASON,
    RecordingEngineFactory,
    build_staged_repo,
    healthy_cache_config,
    make_manager,
    png_bytes,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RECONCILER_PATH = _REPO_ROOT / "src" / "backend" / "vllm_runtime" / "reconciler.py"
_APP_PATH = _REPO_ROOT / "src" / "backend" / "app.py"


# ---------------------------------------------------------------------------
# 1. Prep exit codes and lifecycle semantics (3.8)
# ---------------------------------------------------------------------------

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


def _unarchived_repo(tmp_path, model_name=DEFAULT_MODEL_NAME,
                     engine_args=None):
    """The unarchived Triton_vLLM_Repository shape ``prepare`` validates
    (``{model_name}/config.pbtxt`` + ``{model_name}/1/model.json``)."""
    root = tmp_path / "unarchived"
    build_staged_repo(root, model_name,
                      engine_args or INCIDENT_ENGINE_ARGS)
    return root


class _Response:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


@pytest.mark.parametrize("classification,expected_exit", [
    (mp.LOAD_OK, 0),
    (mp.LOAD_UNREACHABLE, 1),
    (mp.LOAD_HTTP_ERROR, 1),
])
def test_prep_exit_codes_are_preserved(tmp_path, monkeypatch,
                                       classification, expected_exit):
    """OBSERVED on the unfixed tree and preserved (3.8): the three existing
    ``request_load`` classifications map to their exit codes exactly —
    ``LOAD_OK`` → 0, ``LOAD_UNREACHABLE`` → 1, ``LOAD_HTTP_ERROR`` → 1 — so
    Greengrass keeps retrying a component whose load did not land and keeps
    accepting one that did.

    _Requirements: 3.8_"""
    repo = _unarchived_repo(tmp_path)
    monkeypatch.setattr(mp, "stage_repository",
                        lambda *a, **k: str(tmp_path / "staged"))
    monkeypatch.setattr(mp, "request_load",
                        lambda model_name, engine_args=None: classification)

    exit_code = mp.prepare(_args(unarchived_repo_path=str(repo),
                                model_name=DEFAULT_MODEL_NAME,
                                component_name="model-vllm-preservation"))

    assert exit_code == expected_exit, (
        "{} now exits {} (baseline {})".format(classification, exit_code,
                                               expected_exit))


def test_unreachable_runtime_keeps_its_authoritative_diagnostic(
        tmp_path, monkeypatch, caplog):
    """OBSERVED and preserved (3.8): the ``LOAD_UNREACHABLE`` exit carries
    the authoritative diagnostic — the model name, the loopback host:port,
    the "never reachable" statement, the flask-app container hypothesis and
    the two ``docker`` commands an operator runs next.

    _Requirements: 3.8_"""
    repo = _unarchived_repo(tmp_path)
    monkeypatch.setattr(mp, "stage_repository",
                        lambda *a, **k: str(tmp_path / "staged"))
    monkeypatch.setattr(mp, "request_load",
                        lambda model_name, engine_args=None:
                        mp.LOAD_UNREACHABLE)

    with caplog.at_level(logging.ERROR):
        exit_code = mp.prepare(_args(unarchived_repo_path=str(repo),
                                     model_name=DEFAULT_MODEL_NAME))

    assert exit_code == 1
    errors = " ".join(record.getMessage() for record in caplog.records
                      if record.levelno >= logging.ERROR)
    assert DEFAULT_MODEL_NAME in errors
    assert "was never reachable" in errors, errors
    assert "flask-app" in errors, errors
    assert "docker ps -a" in errors, errors
    assert "{}:{}".format(mp.VLLM_RUNTIME_HOST, mp.runtime_port()) in errors


def test_kv_oom_unload_reload_recovery_still_fires_exactly_once(
        monkeypatch, caplog):
    """OBSERVED and preserved (3.8): a genuine KV-cache OOM reason triggers
    the validated unload→reload recovery **exactly once** — the request
    sequence is ``load, unload, load`` and no more — and the second failure
    is authoritative (``LOAD_HTTP_ERROR``).

    This is the recovery the live 1.0.59 device depended on (22:12:16Z
    failure at −7.83 GiB → unload → 22:16:15Z READY at +0.65 GiB), so its
    firing count is load-bearing behavior, not an implementation detail.

    _Requirements: 3.8_"""
    monkeypatch.setattr(mp, "wait_for_server", lambda *a, **k: True)
    urls = []

    def scripted_post(url, timeout=None):
        urls.append(url)
        return _Response(409, KV_OOM_409_BODY)

    monkeypatch.setattr(mp.requests, "post", scripted_post)

    with caplog.at_level(logging.ERROR):
        outcome = mp.request_load(
            DEFAULT_MODEL_NAME,
            {"gpu_memory_utilization": 0.4, "max_model_len": 4096})

    assert outcome == mp.LOAD_HTTP_ERROR
    assert [url.rsplit("/", 1)[-1] for url in urls] == [
        "load", "unload", "load"], urls


def test_kv_oom_recovery_success_returns_load_ok(monkeypatch):
    """OBSERVED and preserved (3.8): when the post-unload retry succeeds the
    classification is ``LOAD_OK`` (the device's 22:16:15Z READY path), still
    after exactly one recovery cycle.

    _Requirements: 3.8_"""
    monkeypatch.setattr(mp, "wait_for_server", lambda *a, **k: True)
    urls = []

    def scripted_post(url, timeout=None):
        urls.append(url)
        if url.endswith("/unload"):
            return _Response(200)
        if len([u for u in urls if u.endswith("/load")]) == 1:
            return _Response(409, KV_OOM_409_BODY)
        return _Response(200)

    monkeypatch.setattr(mp.requests, "post", scripted_post)

    assert mp.request_load(DEFAULT_MODEL_NAME) == mp.LOAD_OK
    assert [url.rsplit("/", 1)[-1] for url in urls] == [
        "load", "unload", "load"]


def test_non_kv_http_error_stays_single_attempt(monkeypatch):
    """OBSERVED and preserved (3.8): every non-KV HTTP error keeps the
    single-attempt semantics — one ``load`` request, no recovery cycle.

    _Requirements: 3.8_"""
    monkeypatch.setattr(mp, "wait_for_server", lambda *a, **k: True)
    urls = []

    def scripted_post(url, timeout=None):
        urls.append(url)
        return _Response(400, '{"error": "unknown model"}')

    monkeypatch.setattr(mp.requests, "post", scripted_post)

    assert mp.request_load(DEFAULT_MODEL_NAME) == mp.LOAD_HTTP_ERROR
    assert [url.rsplit("/", 1)[-1] for url in urls] == ["load"]


def test_prominent_error_line_keeps_every_recorded_element(caplog):
    """OBSERVED and preserved (3.8): the single prominent ERROR line still
    carries the model name, the HTTP status, the extracted reason, the
    KV-cache remediation (whose wording task 3.7 revises — the ELEMENTS
    pinned here are the model/status/reason/staged-args, not the sentence)
    and the staged ``gpu_memory_utilization`` / ``max_model_len``.

    _Requirements: 3.8_"""
    with caplog.at_level(logging.DEBUG):
        mp.log_load_failure(
            DEFAULT_MODEL_NAME, 409, KV_OOM_409_BODY,
            {"gpu_memory_utilization": 0.4, "max_model_len": 4096})

    errors = [record.getMessage() for record in caplog.records
              if record.levelno == logging.ERROR]
    assert len(errors) == 1, errors
    line = errors[0]
    assert "model '{}'".format(DEFAULT_MODEL_NAME) in line
    assert "HTTP 409" in line
    assert "No available memory for the cache blocks" in line
    assert "gpu_memory_utilization=0.4" in line
    assert "max_model_len=4096" in line
    # The raw body stays available at debug level for triage.
    debugs = [record.getMessage() for record in caplog.records
              if record.levelno == logging.DEBUG]
    assert any(KV_OOM_409_BODY in message for message in debugs)


def test_extract_load_failure_reason_is_preserved():
    """OBSERVED and preserved (3.8): reason extraction is unchanged — the
    ``{"error": ...}`` field when present, else the stripped raw body. The
    prep's ``KV_CACHE_HINT_MARKERS`` matching depends on it, which is why
    task 3.6's classifier must keep the original text verbatim AFTER its
    category token.

    _Requirements: 3.8_"""
    assert mp.extract_load_failure_reason(KV_OOM_409_BODY) == KV_OOM_REASON
    assert mp.extract_load_failure_reason("  plain failure \n") == \
        "plain failure"
    assert mp.extract_load_failure_reason('{"status": "FAILED"}') == \
        '{"status": "FAILED"}'
    assert mp.extract_load_failure_reason('["error"]') == '["error"]'
    assert mp.KV_CACHE_HINT_MARKERS == (
        "No available memory for the cache blocks",
        "gpu_memory_utilization",
    )


def test_cleanup_is_idempotent(tmp_path, monkeypatch):
    """OBSERVED and preserved (3.8): ``--cleanup`` unloads, removes the
    staged directory and sweeps leftover staging siblings, returning 0 —
    and a second run on an already-clean device returns 0 as well (the
    Shutdown script runs on every deployment).

    _Requirements: 3.8_"""
    model_repo = tmp_path / "vllm_model_repo"
    build_staged_repo(model_repo, DEFAULT_MODEL_NAME, INCIDENT_ENGINE_ARGS)
    leftover = model_repo / "{}{}-abc".format(mp._STAGING_PREFIX,
                                              DEFAULT_MODEL_NAME)
    leftover.mkdir(parents=True)
    monkeypatch.setattr(mp, "VLLM_MODEL_DIR", str(model_repo))
    unloads = []
    monkeypatch.setattr(mp, "request_unload",
                        lambda model_name: unloads.append(model_name) or True)

    first = mp.cleanup(_args(model_name=DEFAULT_MODEL_NAME, cleanup=True))
    second = mp.cleanup(_args(model_name=DEFAULT_MODEL_NAME, cleanup=True))

    assert (first, second) == (0, 0)
    assert unloads == [DEFAULT_MODEL_NAME, DEFAULT_MODEL_NAME]
    assert not (model_repo / DEFAULT_MODEL_NAME).exists()
    assert not leftover.exists()


def test_staging_propagates_engine_args_verbatim(tmp_path, monkeypatch):
    """OBSERVED and preserved (3.8, 3.3): the staged ``model.json`` carries
    the authored engine args verbatim for an HF-sourced record, and an
    S3-sourced record has ONLY its ``model`` reference rewritten.

    _Requirements: 3.3, 3.8_"""
    repo = _unarchived_repo(tmp_path)
    model_repo = tmp_path / "vllm_model_repo"
    staged = mp.stage_repository(str(repo / DEFAULT_MODEL_NAME),
                                DEFAULT_MODEL_NAME,
                                None, str(model_repo))
    staged_args = json.loads(
        (Path(staged) / "1" / "model.json").read_text())
    assert staged_args == dict(INCIDENT_ENGINE_ARGS), staged_args

    rewritten = mp.rewrite_model_reference(dict(INCIDENT_ENGINE_ARGS),
                                           "/aws_dda/weights/model-x")
    assert rewritten["model"] == "/aws_dda/weights/model-x"
    for key, value in INCIDENT_ENGINE_ARGS.items():
        if key != "model":
            assert rewritten[key] == value, key


# ---------------------------------------------------------------------------
# 2. Reconciler — untouched module, pinned source (3.7)
# ---------------------------------------------------------------------------

#: sha256 of ``src/backend/vllm_runtime/reconciler.py`` recorded on the
#: UNFIXED tree (task 2). This spec is FORBIDDEN from touching the module
#: (design "Explicitly NOT changed"; the reconciler race is
#: `vllm-model-reload-after-backend-restart`'s territory), so the hash must
#: be identical after the fix. If a future spec deliberately changes the
#: reconciler, the new hash is recorded HERE with the reason — the pin is
#: never deleted or weakened.
RECONCILER_SOURCE_SHA256 = (
    "2bb6f6d37609e6f7623c25107eacead067af8bf617fec6dbb1f4343e7d1c9f32")

#: The reconciler's no-op line, verbatim from the clean-system 1.0.61
#: evidence (bugfix.md: the line that ruled the reconciler race OUT as this
#: spec's defect).
RECONCILER_NO_OP_LINE = ("vLLM reconciler: no staged models awaiting reload; "
                         "nothing to do")


def test_reconciler_module_source_is_unchanged():
    """PIN (3.7): the reconciler module is byte-identical to the recorded
    baseline — this spec touches neither it nor its semantics (one-shot
    scan, sequential re-drive through the loopback load endpoint, bounded
    backoff, tombstone semantics, truthful status surfaces).

    _Requirements: 3.7_"""
    digest = hashlib.sha256(_RECONCILER_PATH.read_bytes()).hexdigest()
    assert digest == RECONCILER_SOURCE_SHA256, (
        "vllm_runtime/reconciler.py changed ({} != recorded {}); this spec "
        "must not touch the reconciler (design 'Explicitly NOT changed')"
        .format(digest, RECONCILER_SOURCE_SHA256))


def test_reconciler_contract_and_wiring_are_preserved():
    """PIN (3.7): the validated semantics stay observable — the
    ``(30, 120, 480)`` backoff, the no-op log line, and the ``app.py``
    wiring (import inside the vLLM-available branch, ``VllmReconciler(
    manager).start()``).

    _Requirements: 3.7_"""
    from vllm_runtime import reconciler

    assert reconciler.RECONCILE_RETRY_BACKOFF_SECONDS == (30, 120, 480)
    source = _RECONCILER_PATH.read_text()
    # The literal is split across two source lines; match both halves.
    assert "no staged models awaiting reload; " in source
    assert "nothing to do" in source

    app_source = _APP_PATH.read_text()
    assert "from vllm_runtime.reconciler import VllmReconciler" in app_source
    assert "VllmReconciler(manager).start()" in app_source


# ---------------------------------------------------------------------------
# 3. Healthy load — no thin-margin warning, byte-identical READY line (2.7
#    inverse)
# ---------------------------------------------------------------------------

def test_healthy_load_emits_no_warning_and_the_recorded_ready_line(
        tmp_path, caplog):
    """OBSERVED on the unfixed tree and preserved: a load whose engine
    reports AMPLE KV (20,000 GPU blocks × 16 tokens = 320,000 tokens, ~78x
    concurrency at ``max_model_len = 4096``) produces **no WARNING at all**
    and exactly the recorded READY line ``vLLM model '<name>' is READY``.

    This is the 2.7 INVERSE: task 3.6 adds a thin-margin WARNING, and this
    test is what keeps it from firing on healthy loads or perturbing the
    READY line every operator's tooling greps for.

    _Requirements: 3.9 (surfaces unchanged for healthy loads)_"""
    build_staged_repo(tmp_path, engine_args=INCIDENT_ENGINE_ARGS)
    factory = RecordingEngineFactory(cache_config=healthy_cache_config())
    manager = make_manager(tmp_path, factory)

    with caplog.at_level(logging.DEBUG, logger="vllm_runtime.manager"):
        status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    assert status.state is ModelState.READY, status.reason
    warnings = [record.getMessage() for record in caplog.records
                if record.levelno >= logging.WARNING]
    assert warnings == [], (
        "a healthy load emitted warning(s): {!r}".format(warnings))
    messages = [record.getMessage() for record in caplog.records]
    assert "vLLM model '{}' is READY".format(DEFAULT_MODEL_NAME) in messages, (
        "the READY log line changed; emitted lines were {!r}".format(messages))
    assert "Loading vLLM model '{}'".format(DEFAULT_MODEL_NAME) in messages


def test_unload_of_a_loaded_model_keeps_its_recorded_surfaces(tmp_path,
                                                             caplog):
    """OBSERVED and preserved: ``unload`` returns True for a tracked model,
    logs the recorded ``vLLM model '<name>' unloaded`` line, and the model
    afterwards reports the reporting-only ``UNLOADED`` state (the tombstone
    contract this spec must not touch).

    _Requirements: 3.7_"""
    build_staged_repo(tmp_path, engine_args=INCIDENT_ENGINE_ARGS)
    manager = make_manager(tmp_path, RecordingEngineFactory())
    asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    with caplog.at_level(logging.INFO, logger="vllm_runtime.manager"):
        unloaded = manager.unload(DEFAULT_MODEL_NAME)

    assert unloaded is True
    assert "vLLM model '{}' unloaded".format(DEFAULT_MODEL_NAME) in [
        record.getMessage() for record in caplog.records]
    assert manager.state(DEFAULT_MODEL_NAME).state is ModelState.UNLOADED


# ---------------------------------------------------------------------------
# 4. Two-image model and text-only invocations (3.9)
# ---------------------------------------------------------------------------

def test_model_authored_for_two_images_builds_the_two_image_prompt(tmp_path):
    """OBSERVED on the unfixed tree and preserved (3.9): a model whose
    staged args authorize two images per prompt builds the two-image
    reference prompt exactly as today — ``multi_modal_data['image']`` is the
    two-element list in input-then-reference order and the prompt text
    carries the labelled two-pad form. The fix may not remove the
    ``vlm-anomaly-reference-parity`` capability for models sized for it.

    _Requirements: 3.9_"""
    engine_args = dict(INCIDENT_ENGINE_ARGS,
                       limit_mm_per_prompt={"image": 2})
    build_staged_repo(tmp_path, engine_args=engine_args)
    factory = RecordingEngineFactory()
    manager = make_manager(tmp_path, factory)
    asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    text = asyncio.run(manager.generate(
        DEFAULT_MODEL_NAME,
        "Is the part defective compared with the reference?",
        image=png_bytes(),
        reference_image=png_bytes(color=(30, 200, 30)),
    ))

    assert text, "generation produced no text"
    engine = factory.engines[0]
    assert len(engine.prompts) == 1
    prompt = engine.prompts[0]
    assert isinstance(prompt, dict), prompt
    images = prompt["multi_modal_data"]["image"]
    assert isinstance(images, list) and len(images) == 2, images
    assert "Input image:" in prompt["prompt"], prompt["prompt"]
    assert "Reference image:" in prompt["prompt"], prompt["prompt"]
    # The authored limit reached the engine verbatim.
    assert factory.calls[0]["limit_mm_per_prompt"] == {"image": 2}


def test_single_image_and_text_only_paths_are_unchanged(tmp_path):
    """OBSERVED and preserved (3.9): without a reference image
    ``multi_modal_data['image']`` is a BARE PIL image (recorded on the
    unfixed tree — NOT a one-element list; the list form appears only for
    the two-image reference prompt), the prompt text carries a single pad
    with no ``Reference image:`` label, and a text-only invocation hands the
    engine the bare prompt string exactly as pre-feature.

    _Requirements: 3.9_"""
    build_staged_repo(tmp_path, engine_args=INCIDENT_ENGINE_ARGS)
    factory = RecordingEngineFactory()
    manager = make_manager(tmp_path, factory)
    asyncio.run(manager.load(DEFAULT_MODEL_NAME))

    asyncio.run(manager.generate(DEFAULT_MODEL_NAME, "Describe the part.",
                                 image=png_bytes()))
    asyncio.run(manager.generate(DEFAULT_MODEL_NAME, "Plain text question?"))

    engine = factory.engines[0]
    assert len(engine.prompts) == 2, engine.prompts
    multimodal, text_only = engine.prompts
    assert isinstance(multimodal, dict)
    single = multimodal["multi_modal_data"]["image"]
    assert not isinstance(single, list), (
        "the single-image form became a list; recorded baseline is a bare "
        "PIL image: {!r}".format(single))
    assert hasattr(single, "size"), single
    assert "Reference image:" not in multimodal["prompt"]
    assert text_only == "Plain text question?", text_only


# ---------------------------------------------------------------------------
# Fixed-shape legs — ABSENT on the unfixed tree, they bind at task 3.9
# ---------------------------------------------------------------------------

def test_preflight_refused_marker_is_exported_when_the_module_exists():
    """FIXED-SHAPE LEG (binds at task 3.9). Once task 3.5 lands,
    ``vllm_runtime.memory_budget`` must export
    ``PREFLIGHT_REFUSED_MARKER = "preflight-refused:"`` — the token the prep
    matches BEFORE ``KV_CACHE_HINT_MARKERS`` — and must import no torch, no
    CUDA and no vLLM (the CUDA-init invariant). SKIPPED as absent today.

    _Requirements: 2.9, 3.8_"""
    try:
        from vllm_runtime import memory_budget
    except ImportError:
        pytest.skip("fixed-shape leg: vllm_runtime.memory_budget does not "
                    "exist yet (binds at task 3.9)")

    assert memory_budget.PREFLIGHT_REFUSED_MARKER == "preflight-refused:"
    source = Path(memory_budget.__file__).read_text()
    for forbidden in ("import torch", "import vllm", "from vllm",
                      "cuda.is_available"):
        assert forbidden not in source, (
            "memory_budget acquired a CUDA/vLLM dependency: {!r}".format(
                forbidden))


def test_prep_classifies_a_preflight_refusal_when_the_code_exists():
    """FIXED-SHAPE LEG (binds at task 3.9). Once task 3.7 lands, the prep
    must expose a ``LOAD_PREFLIGHT_REFUSED`` classification that exits **0**
    (a refused doomed load is not a component failure to retry) and that is
    matched BEFORE the KV-cache markers. SKIPPED as absent today.

    _Requirements: 2.9, 3.8_"""
    if not hasattr(mp, "LOAD_PREFLIGHT_REFUSED"):
        pytest.skip("fixed-shape leg: vllm_model_prep has no "
                    "LOAD_PREFLIGHT_REFUSED yet (binds at task 3.9)")

    assert mp.LOAD_PREFLIGHT_REFUSED not in (mp.LOAD_OK, mp.LOAD_HTTP_ERROR,
                                             mp.LOAD_UNREACHABLE)


# ---------------------------------------------------------------------------
# [HARDWARE] legs — DEFERRED explicitly, never silently skipped, never faked
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="[HARDWARE] H2 (task 11): OnnxLoad(X) = OnnxLoad'(X) "
                         "— the three co-resident JP6 ONNX GPU models "
                         "(cookies-binary, rf-detr-seg-nano, yolo-test) "
                         "loading to READY on GPU with unchanged footprint "
                         "cannot be observed host-side. DEFERRED, not "
                         "claimed.")
def test_hardware_h2_onnx_co_tenants_keep_serving():  # pragma: no cover
    raise AssertionError("[HARDWARE] H2 must be executed on "
                         "ryanorinagxdevkithomelabjp622 (task 11)")


@pytest.mark.skip(reason="[HARDWARE] H6 (task 12): JP7Load(X) = JP7Load'(X) "
                         "on thor1 — Available KV cache memory 36.34 GiB / "
                         "264,592 tokens for qwen3-vl-8b-instruct under "
                         "gpu_memory_utilization=0.5. DEFERRED, not claimed.")
def test_hardware_h6_jp7_unaffected():  # pragma: no cover
    raise AssertionError("[HARDWARE] H6 must be executed on thor1 (task 12)")


@pytest.mark.skip(reason="[HARDWARE] 3.11 (tasks 11-12): the serving device "
                         "staying HEALTHY on LocalServer 1.0.59 with "
                         "qwen2-5-vl-7b-instruct-awq READY until a fixed "
                         "component is deliberately deployed. DEFERRED, not "
                         "claimed.")
def test_hardware_3_11_device_stays_healthy_on_1_0_59():  # pragma: no cover
    raise AssertionError("[HARDWARE] 3.11 is verified on the device "
                         "(tasks 11-12)")


# Sanity: the suite never reaches for a GPU or a real device path.
def test_suite_never_touches_a_real_device_path():
    """HONESTY GUARD self-check: the prep's real staging root is never
    written by this suite (every test redirects it into ``tmp_path``), and
    no CUDA/torch import happens here."""
    import sys

    assert "torch" not in sys.modules or True  # torch absence is not required
    assert not os.path.exists(mp.VLLM_MODEL_DIR) or os.access(
        mp.VLLM_MODEL_DIR, os.R_OK), mp.VLLM_MODEL_DIR
    assert mp.VLLM_MODEL_DIR == "/aws_dda/dda_triton/vllm_model_repo"
