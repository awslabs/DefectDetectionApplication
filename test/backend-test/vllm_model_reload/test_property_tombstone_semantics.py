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
"""Tombstone-sequence property suite (task 4.4, Hypothesis) for
vllm-model-reload-after-backend-restart.

**Feature: vllm-model-reload-after-backend-restart, Property 4: Fix
Checking — Tombstone Semantics Across Unload/Load/Re-stage/Cleanup
Sequences**

*For any* sequence of operations on one model drawn from the CLOSED
five-operation alphabet {explicit unload, explicit load,
component-Startup re-stage, ``--cleanup``, backend restart},
reconciliation after a restart reloads the model IF AND ONLY IF its
repository is staged AND the most recent tombstone-affecting operation
re-armed it (explicit load or re-stage) rather than suppressed it
(explicit unload); a ``--cleanup``'d model is NEVER resurrected
(nothing staged, nothing scanned).

# Validates: Requirements 2.4, 3.5

Operation semantics, mapped to the real seams:

- **explicit unload** — ``manager.unload(model)`` (what
  ``POST /v2/repository/models/{m}/unload`` calls; writes the
  Unload_Tombstone when the repo is still staged, Decision 2).
- **explicit load** — ``manager.load(model)`` (what the load endpoint
  calls; FIRST action clears the tombstone — re-arms reconciliation).
- **component-Startup re-stage** — the REAL ``stage_repository()`` from
  ``dda_triton.vllm_model_prep`` against the temp tree (the atomic
  ``shutil.rmtree`` + ``os.rename`` directory replace kills the marker
  with the old directory — ZERO prep changes, Decision 2). The import
  follows the established suite convention (``deploy_reliability`` /
  ``vllm_hf_cache``: ``import dda_triton.vllm_model_prep as prep``).
- **--cleanup** — unload + staged-directory removal (mirrors
  ``vllm_model_prep.cleanup``'s unload-then-remove interleaving; the
  marker leaves with the directory — no litter).
- **backend restart** — the in-process state dies: a FRESH
  ``VllmRuntimeManager`` over the surviving tree plus a REAL
  ``VllmReconciler`` pass with an injected loopback ``request_fn`` (no
  sockets needed for the property core — the reconciler's real HTTP
  path is task 4.7's integration tier) and an EMPTY injected backoff
  (no real sleeps).

Honesty guard (design Testing Strategy): fake engine factory through
the manager's public injectable ``engine_factory`` seam, temp
``VLLM_MODEL_DIR`` trees, restart = object reconstruction. No GPU, no
vLLM, no container, no Greengrass.

No hardcoded ``max_examples`` — profiles come from the environment
(the suite runs ``--noconftest``, so Hypothesis defaults apply).

Run host-side:
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
        test/backend-test/vllm_model_reload/test_property_tombstone_semantics.py \
        -q -p no:cacheprovider --noconftest
"""
import asyncio
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# The REAL component-Startup staging function (task 4.4 mandate). The
# module configures logging at import time; importing it directly at
# module scope is the established convention of the pinned suites
# (deploy_reliability, vllm_hf_cache, dda_triton) — mirrored here.
import dda_triton.vllm_model_prep as prep
from vllm_runtime.constants import UNLOAD_TOMBSTONE_NAME
from vllm_runtime.manager import ModelState, VllmRuntimeManager
from vllm_runtime.reconciler import VllmReconciler

#: The incident's model (jetson-thor1, 2026-08-16).
MODEL_NAME = "qwen3-vl-8b-instruct"

#: The reconciler pass runs a fake load and no sleeps (empty backoff);
#: this bound only guards against a hang.
JOIN_BUDGET_SECONDS = 10.0

#: Loopback port formatted into the reconciler's URL. Never dialed —
#: the injected request_fn answers in-process.
UNUSED_PORT = 65535


# ---------------------------------------------------------------------------
# Module-local fakes (the manager's public injectable engine_factory seam)
# ---------------------------------------------------------------------------


class _FakeEngine:
    """Exactly the surface ``VllmRuntimeManager`` touches on unload:
    ``shutdown_background_loop()`` (recorded — 'frees the engine')."""

    def __init__(self):
        self.shutdown_calls = 0

    def shutdown_background_loop(self):
        self.shutdown_calls += 1


class _RecordingFactory:
    """Recording fake for the injectable ``engine_factory`` seam; one
    call per driven engine construction."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.engines: List[_FakeEngine] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(self, engine_args: Mapping[str, Any]) -> _FakeEngine:
        self.calls.append(dict(engine_args))
        engine = _FakeEngine()
        self.engines.append(engine)
        return engine


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def _loopback_request_fn(manager: VllmRuntimeManager,
                         record: List[Tuple[str, str]]):
    """Injected ``request_fn``: answers the reconciler's model-control
    POSTs in-process against the SAME manager the loopback endpoint
    would reach (design File 1's test seam — no sockets needed for the
    property core; the real HTTP path is task 4.7's tier). Every
    request is recorded as ``(action, model_name)``."""

    def request_fn(url: str, timeout: Optional[float] = None) -> _FakeResponse:
        parts = url.rstrip("/").split("/")
        action, model_name = parts[-1], parts[-2]
        record.append((action, model_name))
        if action == "load":
            status = asyncio.run(manager.load(model_name))
            if status.state is ModelState.READY:
                return _FakeResponse(200)
            return _FakeResponse(
                400,
                json.dumps({"error": status.reason or "load failed"}),
            )
        if action == "unload":
            manager.unload(model_name)
            return _FakeResponse(200)
        return _FakeResponse(400, json.dumps(
            {"error": "unexpected action {!r}".format(action)}))

    return request_fn


# ---------------------------------------------------------------------------
# Tree builders and the one-life backend harness
# ---------------------------------------------------------------------------


def _build_source_repo(root: Path, model_name: str = MODEL_NAME) -> Path:
    """The model component's UNARCHIVED SOURCE directory — the valid
    Triton_vLLM_Repository layout ``stage_repository()`` copies:
    ``config.pbtxt`` declaring ``backend: "vllm"`` + ``1/model.json``."""
    source = root / "source" / model_name
    (source / "1").mkdir(parents=True, exist_ok=True)
    (source / "config.pbtxt").write_text('backend: "vllm"\n')
    (source / "1" / "model.json").write_text(
        json.dumps({"model": model_name}))
    return source


def _restage(source: Path, model_dir: Path,
             model_name: str = MODEL_NAME) -> None:
    """The component Startup's atomic re-stage, through the REAL
    ``stage_repository()`` (task 4.4 mandate): temp-sibling copy +
    ``os.rename`` replace — the old directory (and any Unload_Tombstone
    inside it) is gone wholesale."""
    prep.stage_repository(
        str(source), model_name, rewritten_engine_args=None,
        model_repo_dir=str(model_dir))


class _Backend:
    """One backend life: a fresh manager (EMPTY model table — exactly
    what a process restart does to the in-process state) over the
    surviving ``VLLM_MODEL_DIR`` tree, with a recording factory."""

    def __init__(self, model_dir: Path):
        self.model_dir = model_dir
        self.factory = _RecordingFactory()
        self.manager = VllmRuntimeManager(
            model_dir=model_dir,
            engine_factory=self.factory,
            sampling_params_factory=dict,
        )


def _reconcile(backend: _Backend) -> List[Tuple[str, str]]:
    """One real ``VllmReconciler`` pass over the backend: injected
    loopback ``request_fn`` (no sockets), EMPTY injected backoff (single
    attempt, no ``time.sleep``), the real daemon thread joined to
    completion. Returns the recorded model-control requests."""
    record: List[Tuple[str, str]] = []
    reconciler = VllmReconciler(
        backend.manager,
        port=UNUSED_PORT,
        backoff=(),
        request_fn=_loopback_request_fn(backend.manager, record),
    )
    thread = reconciler.start()
    thread.join(timeout=JOIN_BUDGET_SECONDS)
    assert not thread.is_alive(), (
        "the vllm-reconciler pass did not finish within {}s".format(
            JOIN_BUDGET_SECONDS))
    return record


def _tombstone_path(model_dir: Path, model_name: str = MODEL_NAME) -> Path:
    return model_dir / model_name / UNLOAD_TOMBSTONE_NAME


# ---------------------------------------------------------------------------
# Property 4: reconciliation reloads IFF staged AND re-armed
# ---------------------------------------------------------------------------

#: The CLOSED five-operation alphabet (design Property 4 / fix-check
#: case 4 — operation sequences are drawn from exactly these five).
_OPERATIONS = ("unload", "load", "restage", "cleanup", "restart")

_operation_sequences = st.lists(
    st.sampled_from(_OPERATIONS), min_size=1, max_size=10)


@given(operations=_operation_sequences)
@settings(deadline=None)
def test_reconciliation_reloads_iff_staged_and_rearmed(operations):
    """**Feature: vllm-model-reload-after-backend-restart, Property 4:
    Fix Checking — Tombstone Semantics Across Unload/Load/Re-stage/
    Cleanup Sequences**

    *For any* operation sequence on one model drawn from the closed
    five-operation alphabet, reconciliation after a backend restart
    reloads the model IFF its repository is staged AND the most recent
    tombstone-affecting operation re-armed it (explicit load or
    component-Startup re-stage) rather than suppressed it (explicit
    unload). A ``--cleanup``'d model is NEVER resurrected: nothing is
    staged, nothing is scanned, no load request is issued, no engine is
    constructed. Suppressed-but-staged models report UNLOADED (the
    tombstone honored across the restart); reloaded models reach READY.

    The oracle tracks (staged, re-armed) purely from the operation
    sequence — never by re-reading the disk state the code under test
    reads.

    # Validates: Requirements 2.4, 3.5
    **Validates: Requirements 2.4, 3.5**
    """
    root = Path(tempfile.mkdtemp(prefix="vllm-tombstone-seq-"))
    try:
        source = _build_source_repo(root)
        model_dir = root / "vllm_model_repo"
        # First deploy: the component Startup stages the repository
        # (the REAL stage_repository), i.e. staged and re-armed.
        _restage(source, model_dir)
        staged, rearmed = True, True

        backend = _Backend(model_dir)

        # A trailing restart makes every generated sequence end with a
        # checked reconciliation pass.
        for index, op in enumerate(list(operations) + ["restart"]):
            if op == "unload":
                backend.manager.unload(MODEL_NAME)
                if staged:
                    rearmed = False  # tombstone written (Decision 2)
            elif op == "load":
                asyncio.run(backend.manager.load(MODEL_NAME))
                rearmed = True  # load's FIRST action clears the marker
            elif op == "restage":
                _restage(source, model_dir)
                staged, rearmed = True, True  # fresh dir, marker gone
            elif op == "cleanup":
                # vllm_model_prep.cleanup's interleaving: unload, then
                # remove the staged directory (marker leaves with it).
                backend.manager.unload(MODEL_NAME)
                shutil.rmtree(model_dir / MODEL_NAME, ignore_errors=True)
                staged = False
            else:  # restart
                backend = _Backend(model_dir)
                record = _reconcile(backend)
                load_requests = [
                    entry for entry in record
                    if entry == ("load", MODEL_NAME)
                ]
                expected_reload = staged and rearmed
                if expected_reload:
                    assert load_requests, (
                        "FIX-CHECK FAILURE (Property 4 / 2.4): op #{} "
                        "restart after {!r} — repo staged and re-armed, "
                        "but the reconciler issued NO load request "
                        "(record: {!r})".format(
                            index, operations[:index], record))
                    assert backend.factory.call_count == 1, (
                        "FIX-CHECK FAILURE (Property 4 / 2.4): expected "
                        "exactly ONE engine construction in the "
                        "restarted backend, got {}".format(
                            backend.factory.call_count))
                    state = backend.manager.state(MODEL_NAME).state
                    assert state is ModelState.READY, (
                        "FIX-CHECK FAILURE (Property 4 / 2.4): reloaded "
                        "model should be READY, is {}".format(state))
                else:
                    assert not load_requests, (
                        "FIX-CHECK FAILURE (Property 4 / 2.4, 3.5): op "
                        "#{} restart after {!r} — the model was {} and "
                        "the reconciler still issued a load request "
                        "(record: {!r})".format(
                            index, operations[:index],
                            "explicitly unloaded (tombstoned)"
                            if staged else "--cleanup'd (not staged)",
                            record))
                    assert backend.factory.call_count == 0, (
                        "FIX-CHECK FAILURE (Property 4 / 2.4, 3.5): no "
                        "engine may be constructed for a suppressed or "
                        "cleaned-up model; factory calls: {}".format(
                            backend.factory.call_count))
                    state = backend.manager.state(MODEL_NAME).state
                    if staged:
                        # Suppressed: the tombstone survived the restart
                        # and the model reports UNLOADED (Decision 3).
                        assert _tombstone_path(model_dir).is_file(), (
                            "FIX-CHECK FAILURE (Property 4 / 3.5): the "
                            "Unload_Tombstone should survive the restart")
                        assert state is ModelState.UNLOADED, (
                            "FIX-CHECK FAILURE (Property 4 / 2.4): "
                            "staged-but-tombstoned should report "
                            "UNLOADED, is {}".format(state))
                    else:
                        # --cleanup'd: nothing staged, nothing scanned.
                        assert not (model_dir / MODEL_NAME).exists(), (
                            "FIX-CHECK FAILURE (Property 4 / 2.4): the "
                            "--cleanup'd repository resurfaced on disk")
                        assert state is ModelState.UNKNOWN, (
                            "FIX-CHECK FAILURE (Property 4 / 2.4): a "
                            "--cleanup'd model should be UNKNOWN after "
                            "restart, is {}".format(state))
                        assert not record, (
                            "FIX-CHECK FAILURE (Property 4 / 2.4): "
                            "nothing may be scanned for a --cleanup'd "
                            "model; record: {!r}".format(record))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Marker legs (task 4.4): unload always succeeds, write-failure tolerance,
# re-stage clears, KV-OOM interleaving net-neutral
# ---------------------------------------------------------------------------


class TestMarkerLegs:

    def test_unload_always_succeeds_and_frees_the_engine(self, tmp_path):
        """# Validates: Requirements 2.4, 3.5
        **Validates: Requirements 2.4, 3.5**

        The unload succeeds from every state (3.5 is categorical) and
        frees the engine: READY-tracked → True + engine shutdown +
        tombstone written; untracked-but-staged → False + tombstone
        written (every model-control unload means "stop serving this
        model", Decision 2); never-staged → False + no marker anywhere.
        """
        source = _build_source_repo(tmp_path)
        model_dir = tmp_path / "vllm_model_repo"
        _restage(source, model_dir)
        backend = _Backend(model_dir)

        # READY-tracked: True, engine shut down, marker written.
        status = asyncio.run(backend.manager.load(MODEL_NAME))
        assert status.state is ModelState.READY
        engine = backend.factory.engines[0]
        assert backend.manager.unload(MODEL_NAME) is True
        assert engine.shutdown_calls == 1, (
            "unload must free the engine (shutdown_background_loop)")
        assert _tombstone_path(model_dir).is_file()
        assert backend.manager.state(MODEL_NAME).state is ModelState.UNLOADED

        # Untracked-but-staged (the post-restart shape): False, marker
        # (re)written, still no exception.
        _restage(source, model_dir)  # marker gone with the old dir
        assert not _tombstone_path(model_dir).exists()
        fresh = _Backend(model_dir)  # fresh manager: nothing tracked
        assert fresh.manager.unload(MODEL_NAME) is False
        assert _tombstone_path(model_dir).is_file(), (
            "an untracked-but-staged unload must still tombstone "
            "(Decision 2: it expresses 'stop serving this model')")

        # Never-staged: False, and no marker materializes anywhere.
        assert fresh.manager.unload("never-staged-model") is False
        assert not (model_dir / "never-staged-model").exists()

    @pytest.mark.skipif(
        os.geteuid() == 0,
        reason="directory write permissions are not enforced for root")
    def test_marker_write_failure_never_fails_the_unload(self, tmp_path):
        """# Validates: Requirements 2.4, 3.5
        **Validates: Requirements 2.4, 3.5**

        A tombstone write failure (read-only repository directory) is
        best-effort: the unload still returns True, frees the engine,
        and raises nothing (Requirement 3.5 is categorical). The marker
        is simply absent — the documented degradation is that the next
        backend start reconciles the model.
        """
        source = _build_source_repo(tmp_path)
        model_dir = tmp_path / "vllm_model_repo"
        _restage(source, model_dir)
        backend = _Backend(model_dir)
        status = asyncio.run(backend.manager.load(MODEL_NAME))
        assert status.state is ModelState.READY
        engine = backend.factory.engines[0]

        repo = model_dir / MODEL_NAME
        repo.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP
                   | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)  # 0o555
        try:
            result = backend.manager.unload(MODEL_NAME)
        finally:
            repo.chmod(0o755)
        assert result is True, (
            "FIX-CHECK FAILURE (3.5): a marker write failure must never "
            "fail the unload")
        assert engine.shutdown_calls == 1, "the engine must still be freed"
        assert not _tombstone_path(model_dir).exists(), (
            "no marker can exist in a read-only repository")

    def test_restage_clears_the_marker(self, tmp_path):
        """# Validates: Requirements 2.4, 3.5
        **Validates: Requirements 2.4, 3.5**

        The component Startup's atomic re-stage (the REAL
        ``stage_repository()``) replaces the directory wholesale, so the
        Unload_Tombstone leaves with the old directory — the model is
        re-armed with ZERO ``vllm_model_prep.py`` changes, and the next
        reconciliation pass reloads it.
        """
        source = _build_source_repo(tmp_path)
        model_dir = tmp_path / "vllm_model_repo"
        _restage(source, model_dir)
        backend = _Backend(model_dir)
        asyncio.run(backend.manager.load(MODEL_NAME))
        backend.manager.unload(MODEL_NAME)
        assert _tombstone_path(model_dir).is_file()
        assert backend.manager.state(MODEL_NAME).state is ModelState.UNLOADED

        _restage(source, model_dir)

        assert not _tombstone_path(model_dir).exists(), (
            "FIX-CHECK FAILURE (2.4): the atomic re-stage must clear "
            "the Unload_Tombstone")
        assert backend.manager.state(MODEL_NAME).state is ModelState.STAGED

        # And the re-armed model reloads on the next backend start.
        restarted = _Backend(model_dir)
        record = _reconcile(restarted)
        assert ("load", MODEL_NAME) in record
        assert restarted.manager.state(MODEL_NAME).state is ModelState.READY

    def test_kv_oom_unload_load_interleaving_is_net_neutral(self, tmp_path):
        """# Validates: Requirements 2.4, 3.5
        **Validates: Requirements 2.4, 3.5**

        The validated KV-OOM recovery interleaving — an unload
        immediately followed by a load — is net-neutral for tombstone
        state: the unload writes the marker, the load's FIRST action
        clears it. The model ends READY, un-tombstoned, and still
        desired (a subsequent restart reloads it).
        """
        source = _build_source_repo(tmp_path)
        model_dir = tmp_path / "vllm_model_repo"
        _restage(source, model_dir)
        backend = _Backend(model_dir)
        asyncio.run(backend.manager.load(MODEL_NAME))

        backend.manager.unload(MODEL_NAME)   # recovery unload: marker on
        assert _tombstone_path(model_dir).is_file()
        status = asyncio.run(backend.manager.load(MODEL_NAME))  # marker off

        assert status.state is ModelState.READY
        assert not _tombstone_path(model_dir).exists(), (
            "FIX-CHECK FAILURE (2.4/3.5): the KV-OOM unload→load "
            "interleaving must be net-neutral (marker cleared)")

        # Net effect: none — the next backend restart still reloads.
        restarted = _Backend(model_dir)
        record = _reconcile(restarted)
        assert ("load", MODEL_NAME) in record
        assert restarted.factory.call_count == 1
        assert restarted.manager.state(MODEL_NAME).state is ModelState.READY


# ---------------------------------------------------------------------------
# Unit legs (design Unit Tests): path construction, corrupt marker,
# _tombstoned on a missing repo
# ---------------------------------------------------------------------------


class TestTombstoneUnits:

    def test_tombstone_path_construction(self, tmp_path):
        """# Validates: Requirements 2.4, 3.5
        **Validates: Requirements 2.4, 3.5**

        The marker lives INSIDE the model's staged repository directory
        — ``{model_dir}/{model}/.dda_explicit_unload`` — the in-repo
        placement that makes it self-cleaning under the atomic re-stage
        (Decision 2), built from the shared constant.
        """
        model_dir = tmp_path / "vllm_model_repo"
        backend = _Backend(model_dir)
        assert backend.manager._tombstone_path(MODEL_NAME) == (
            model_dir / MODEL_NAME / UNLOAD_TOMBSTONE_NAME)
        assert UNLOAD_TOMBSTONE_NAME == ".dda_explicit_unload"

    def test_corrupt_marker_still_counts_as_tombstoned(self, tmp_path):
        """# Validates: Requirements 2.4, 3.5
        **Validates: Requirements 2.4, 3.5**

        The marker's content is triage-only JSON and is never parsed:
        a corrupt (non-JSON) marker still counts as tombstoned — the
        model reports UNLOADED and the reconciler does not scan it.
        """
        source = _build_source_repo(tmp_path)
        model_dir = tmp_path / "vllm_model_repo"
        _restage(source, model_dir)
        _tombstone_path(model_dir).write_bytes(b"\x00{{{not-json\xff")

        backend = _Backend(model_dir)
        assert backend.manager._tombstoned(MODEL_NAME) is True
        assert backend.manager.state(MODEL_NAME).state is ModelState.UNLOADED
        assert backend.manager.list_models()[MODEL_NAME].state is (
            ModelState.UNLOADED)

        record = _reconcile(backend)
        assert record == [], (
            "FIX-CHECK FAILURE (2.4): a (corrupt-)tombstoned model must "
            "not be scanned; record: {!r}".format(record))
        assert backend.factory.call_count == 0

    def test_tombstoned_on_missing_repo(self, tmp_path):
        """# Validates: Requirements 2.4, 3.5
        **Validates: Requirements 2.4, 3.5**

        ``_tombstoned`` on a name with no staged repository answers
        False (nothing exists to carry a marker) and the disk-derived
        status stays UNKNOWN — a missing repo is never mistaken for a
        suppressed one.
        """
        model_dir = tmp_path / "vllm_model_repo"
        model_dir.mkdir()
        backend = _Backend(model_dir)
        assert backend.manager._tombstoned("no-such-model") is False
        assert backend.manager.state("no-such-model").state is (
            ModelState.UNKNOWN)
