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
"""Reconciler lifecycle property suite — fix-check case 1 (spec:
vllm-model-reload-after-backend-restart, design Testing Strategy
"Fix Checking" case 1; task 4.1).

**Property 1: Bug Condition/Expected Behavior — every desired model is
re-driven, bounded, sequential, isolated.**

The property core runs WITHOUT sockets (the HTTP path is covered at
4.7): the reconciler's injectable ``request_fn`` seam is driven by
:class:`_LoopbackModelControl`, an in-process stand-in for
``requests.post`` against the runtime server's model-control endpoints
that drives the REAL ``VllmRuntimeManager`` exactly the way
``VllmRuntimeServer``'s handlers do (load → ``manager.load`` on a fresh
event loop, 200 + status payload on READY, 409 + status payload
otherwise; unload → ``manager.unload``, always 200) and records every
request. Per-model outcomes (success, permanent failure,
fail-then-succeed, KV-OOM-marker failure) are scripted through the
manager's public injectable ``engine_factory`` seam.

No real sleeps run anywhere: the property injects an all-zero backoff
schedule through the reconciler's ``backoff`` seam, and the
backoff-arithmetic unit legs swap the reconciler module's ``time``
binding for a recorder.

Hypothesis conventions per the suite: ``@settings(deadline=None)``, NO
hardcoded ``max_examples`` (the suite runs ``--noconftest``, so
Hypothesis defaults/profiles apply).

# Validates: Requirements 2.1, 3.2
**Validates: Requirements 2.1, 3.2**
"""
import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hypothesis import given, settings
from hypothesis import strategies as st

import vllm_runtime.reconciler as reconciler_module
from vllm_runtime.constants import UNLOAD_TOMBSTONE_NAME
from vllm_runtime.manager import ModelState, ModelStatus, VllmRuntimeManager
from vllm_runtime.reconciler import (
    KV_CACHE_HINT_MARKERS,
    RECONCILE_RETRY_BACKOFF_SECONDS,
    VllmReconciler,
)
from vllm_model_reload.fakes import build_staged_repo

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

#: Sorted-order pool the property draws model sets from (sorted names ==
#: pool order, so sequential-order assertions are direct).
_MODEL_POOL = ("model-alpha", "model-bravo", "model-charlie", "model-delta")

#: The bounded schedule gives len(backoff) + 1 = 4 attempts (Decision 1).
_ATTEMPTS = len(RECONCILE_RETRY_BACKOFF_SECONDS) + 1

#: All-zero backoff injected in the property so no real sleeps run.
_NO_SLEEP_BACKOFF = (0, 0, 0)

#: How long a (deterministic, fake-driven) reconciliation pass may take.
_JOIN_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Module-local helpers (task 4.1's file only — concurrent tasks own the
# other files in this directory; fakes.py is reused read-only)
# ---------------------------------------------------------------------------


class _FakeEngine:
    """Minimal engine object; the reconciler lifecycle never generates."""

    def shutdown_background_loop(self):
        pass


@dataclass
class _OutcomePlan:
    """One model's scripted engine-factory behavior.

    kind:
      - ``success``: every construction succeeds.
      - ``fail-then-succeed``: the first ``failures`` constructions raise
        a transient (non-KV) reason, then success.
      - ``permanent-failure``: every construction raises the same non-KV
        reason.
      - ``kv-oom-recovers``: the first construction raises a reason
        carrying a KV-cache marker; the recovery reload succeeds.
      - ``kv-oom-permanent``: every construction raises the KV-marker
        reason (initial attempts AND recovery reloads).
    """

    kind: str
    failures: int = 0
    marker: str = ""

    def transient_reason(self, model_name: str, call_number: int) -> str:
        return "transient engine-construction failure #{} for {}".format(
            call_number, model_name
        )

    def permanent_reason(self, model_name: str) -> str:
        return "permanent engine-construction failure for {}".format(
            model_name
        )

    def kv_reason(self, model_name: str) -> str:
        return "{}: injected KV-cache failure for {}".format(
            self.marker, model_name
        )


class _PerModelOutcomeFactory:
    """Recording engine factory dispatching on the model name carried in
    the parsed engine args (``build_staged_repo`` writes ``model.json``
    as ``{"model": <name>}``) and following each model's
    :class:`_OutcomePlan`."""

    def __init__(self, plans: Dict[str, _OutcomePlan]):
        self.plans = plans
        self.calls: Dict[str, int] = {}

    def __call__(self, engine_args):
        model_name = engine_args["model"]
        self.calls[model_name] = self.calls.get(model_name, 0) + 1
        call_number = self.calls[model_name]
        plan = self.plans[model_name]
        if plan.kind == "success":
            return _FakeEngine()
        if plan.kind == "fail-then-succeed":
            if call_number <= plan.failures:
                raise RuntimeError(
                    plan.transient_reason(model_name, call_number)
                )
            return _FakeEngine()
        if plan.kind == "permanent-failure":
            raise RuntimeError(plan.permanent_reason(model_name))
        if plan.kind == "kv-oom-recovers":
            if call_number == 1:
                raise RuntimeError(plan.kv_reason(model_name))
            return _FakeEngine()
        # kv-oom-permanent
        raise RuntimeError(plan.kv_reason(model_name))


class _FakeResponse:
    """The two attributes the reconciler reads from a requests response."""

    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.text = json.dumps(payload)


def _status_payload(model_name: str, status: ModelStatus) -> dict:
    """Byte-for-byte the runtime server's ``_status_payload`` shape."""
    payload = {"name": model_name, "state": status.state.value}
    if status.reason:
        payload["reason"] = status.reason
    return payload


class _LoopbackModelControl:
    """Socket-free ``request_fn``: parses the model-control URL and
    answers exactly like ``VllmRuntimeServer``'s handlers over the REAL
    manager — load returns 200 + status payload iff the load reached
    READY, else 409 + status payload (name/state/reason); unload always
    returns 200 with the idempotent unload result. Every request is
    recorded as ``(op, model_name)`` in arrival order."""

    def __init__(self, manager: VllmRuntimeManager):
        self.manager = manager
        self.requests: List[Tuple[str, str]] = []

    def __call__(self, url: str, timeout: Optional[float] = None):
        head, _, op = url.rpartition("/")
        _, _, model_name = head.rpartition("/")
        assert op in ("load", "unload"), "unexpected model-control URL: {}".format(url)
        self.requests.append((op, model_name))
        if op == "load":
            status = asyncio.run(self.manager.load(model_name))
            if status.state is ModelState.READY:
                return _FakeResponse(200, _status_payload(model_name, status))
            return _FakeResponse(409, _status_payload(model_name, status))
        unloaded = self.manager.unload(model_name)
        return _FakeResponse(200, {"name": model_name, "unloaded": unloaded})


def _expected_ops(plan: _OutcomePlan) -> List[str]:
    """The exact model-control op sequence one model's reconciliation
    produces under its plan: bounded attempts (4), exactly ONE
    unload→reload recovery per KV-OOM-failed attempt, stop on success."""
    if plan.kind == "success":
        return ["load"]
    if plan.kind == "fail-then-succeed":
        return ["load"] * (plan.failures + 1)
    if plan.kind == "permanent-failure":
        return ["load"] * _ATTEMPTS
    if plan.kind == "kv-oom-recovers":
        return ["load", "unload", "load"]
    # kv-oom-permanent: every attempt is load-fail → unload → reload-fail
    return ["load", "unload", "load"] * _ATTEMPTS


def _run_reconciler(
    manager: VllmRuntimeManager,
    control: _LoopbackModelControl,
    backoff=_NO_SLEEP_BACKOFF,
) -> None:
    """Run one reconciliation pass through the public start() seam and
    wait for the daemon thread to finish."""
    reconciler = VllmReconciler(
        manager, backoff=backoff, request_fn=control
    )
    thread = reconciler.start()
    thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
    assert not thread.is_alive(), (
        "the reconciliation pass did not finish within {}s — the bounded "
        "schedule must terminate".format(_JOIN_TIMEOUT_SECONDS)
    )


# ---------------------------------------------------------------------------
# Hypothesis strategies (generators constrain to the real input space:
# valid staged layouts, the five designed per-model outcome kinds, the
# real prep KV-cache markers)
# ---------------------------------------------------------------------------

_plan_strategy = st.one_of(
    st.builds(_OutcomePlan, kind=st.just("success")),
    st.builds(
        _OutcomePlan,
        kind=st.just("fail-then-succeed"),
        failures=st.integers(min_value=1, max_value=_ATTEMPTS - 1),
    ),
    st.builds(_OutcomePlan, kind=st.just("permanent-failure")),
    st.builds(
        _OutcomePlan,
        kind=st.just("kv-oom-recovers"),
        marker=st.sampled_from(KV_CACHE_HINT_MARKERS),
    ),
    st.builds(
        _OutcomePlan,
        kind=st.just("kv-oom-permanent"),
        marker=st.sampled_from(KV_CACHE_HINT_MARKERS),
    ),
)

_staged_sets = st.dictionaries(
    keys=st.sampled_from(_MODEL_POOL),
    values=_plan_strategy,
    min_size=1,
    max_size=len(_MODEL_POOL),
)


# ---------------------------------------------------------------------------
# Property 1: every desired model re-driven, bounded, sequential, isolated
# ---------------------------------------------------------------------------


@given(plans=_staged_sets)
@settings(deadline=None)
def test_reconciler_lifecycle_property(plans):
    """**Feature: vllm-model-reload-after-backend-restart, Property 1:
    Bug Condition/Expected Behavior — a backend restart never silently
    orphans a staged vLLM model.**

    *For any* generated set of staged repos with per-model factory
    outcomes (success, permanent failure, fail-then-succeed,
    KV-OOM-marker failure), after one reconciliation pass:

    - every desired model ends READY or FAILED-with-retained-reason;
    - loads are issued strictly sequentially in sorted name order (each
      model's requests form one contiguous block, blocks sorted);
    - per-model attempt counts never exceed the backoff schedule
      (4 attempts);
    - a KV-OOM-marker failure triggers exactly ONE unload→reload
      recovery per attempt;
    - one model's exhaustion never affects another model's
      reconciliation (every model's op sequence and terminal state match
      its OWN plan exactly, whatever its neighbors did).

    # Validates: Requirements 2.1, 3.2
    **Validates: Requirements 2.1, 3.2**
    """
    model_dir = Path(tempfile.mkdtemp(prefix="vllm-reconciler-lifecycle-"))
    try:
        for name in plans:
            build_staged_repo(model_dir, name)
        factory = _PerModelOutcomeFactory(plans)
        manager = VllmRuntimeManager(
            model_dir=model_dir,
            engine_factory=factory,
            sampling_params_factory=dict,
        )
        control = _LoopbackModelControl(manager)

        _run_reconciler(manager, control)

        sorted_names = sorted(plans)
        ops = control.requests

        # --- strictly sequential, sorted name order ---------------------
        first_seen_order = []
        for _, name in ops:
            if name not in first_seen_order:
                first_seen_order.append(name)
        assert first_seen_order == sorted_names, (
            "loads were not issued in sorted name order: saw {} "
            "expected {}".format(first_seen_order, sorted_names)
        )
        for name in sorted_names:
            indices = [i for i, (_, n) in enumerate(ops) if n == name]
            assert indices == list(
                range(indices[0], indices[0] + len(indices))
            ), (
                "model '{}' was interleaved with another model's "
                "reconciliation (request indices {}); one model per step, "
                "sequentially".format(name, indices)
            )

        for name in sorted_names:
            plan = plans[name]
            model_ops = [op for op, n in ops if n == name]

            # --- exact per-model request sequence (bounded attempts,
            # exactly ONE unload→reload recovery per KV-OOM attempt) ----
            assert model_ops == _expected_ops(plan), (
                "model '{}' (outcome '{}'): request sequence {} != "
                "expected {}".format(
                    name, plan.kind, model_ops, _expected_ops(plan)
                )
            )

            # --- attempt bound: never more than the schedule allows ----
            loads = model_ops.count("load")
            unloads = model_ops.count("unload")
            assert loads <= 2 * _ATTEMPTS and unloads <= _ATTEMPTS, (
                "model '{}' exceeded the bounded schedule: {} loads / {} "
                "unloads for a {}-attempt schedule".format(
                    name, loads, unloads, _ATTEMPTS
                )
            )

            # --- terminal state matches the plan ------------------------
            status = manager.state(name)
            if plan.kind in ("success", "fail-then-succeed",
                             "kv-oom-recovers"):
                assert status.state is ModelState.READY, (
                    "model '{}' (outcome '{}') should have been re-driven "
                    "to READY; got {} (reason: {})".format(
                        name, plan.kind, status.state, status.reason
                    )
                )
            elif plan.kind == "permanent-failure":
                assert status.state is ModelState.FAILED
                assert status.reason == plan.permanent_reason(name), (
                    "model '{}': FAILED reason not retained (got "
                    "{!r})".format(name, status.reason)
                )
            else:  # kv-oom-permanent
                assert status.state is ModelState.FAILED
                assert status.reason is not None and plan.marker in (
                    status.reason
                ), (
                    "model '{}': the KV-OOM reason was not retained (got "
                    "{!r})".format(name, status.reason)
                )
    finally:
        shutil.rmtree(model_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Unit leg 1: candidate snapshot (design Unit Tests — "Reconciler
# candidate snapshot: STAGED included, UNLOADED/LOADING/READY/
# FAILED-tracked excluded; empty VLLM_MODEL_DIR; dir absent")
# ---------------------------------------------------------------------------


class _StubStatusManager:
    """Duck-typed manager exposing exactly the ``list_models()`` feed the
    candidate snapshot reads — the one place a synchronous unit test can
    present a LOADING entry without racing a real in-flight load."""

    def __init__(self, statuses: Dict[str, ModelStatus]):
        self._statuses = statuses

    def list_models(self) -> Dict[str, ModelStatus]:
        return dict(self._statuses)


class TestCandidateSnapshot:
    """# Validates: Requirements 2.1, 3.2"""

    def test_staged_included_all_other_states_excluded(self):
        """Only STAGED entries are reload candidates: UNLOADED
        (tombstoned), LOADING, READY, FAILED-tracked, and UNKNOWN entries
        are all excluded from the snapshot.

        # Validates: Requirements 2.1, 3.2
        """
        stub = _StubStatusManager({
            "m-staged": ModelStatus(ModelState.STAGED),
            "m-unloaded": ModelStatus(ModelState.UNLOADED),
            "m-loading": ModelStatus(ModelState.LOADING),
            "m-ready": ModelStatus(ModelState.READY),
            "m-failed": ModelStatus(ModelState.FAILED, reason="boom"),
            "m-unknown": ModelStatus(ModelState.UNKNOWN),
        })
        reconciler = VllmReconciler(stub, request_fn=_forbidden_request_fn)
        assert reconciler._candidates() == ["m-staged"]

    def test_real_manager_staged_vs_tombstoned_vs_terminal(self, tmp_path):
        """Over a REAL manager and tree: a plainly staged repo is a
        candidate; a staged-but-tombstoned repo (UNLOADED, Decision 3)
        is not; READY and FAILED tracked models are not; candidates come
        back sorted.

        # Validates: Requirements 2.1, 3.2
        """
        for name in ("staged-b", "staged-a", "tombstoned", "ready", "failed"):
            build_staged_repo(tmp_path, name)
        (tmp_path / "tombstoned" / UNLOAD_TOMBSTONE_NAME).write_text(
            '{"marker": "explicit unload"}'
        )
        plans = {
            "ready": _OutcomePlan(kind="success"),
            "failed": _OutcomePlan(kind="permanent-failure"),
        }
        manager = VllmRuntimeManager(
            model_dir=tmp_path,
            engine_factory=_PerModelOutcomeFactory(plans),
            sampling_params_factory=dict,
        )
        assert asyncio.run(manager.load("ready")).state is ModelState.READY
        assert asyncio.run(manager.load("failed")).state is ModelState.FAILED

        reconciler = VllmReconciler(
            manager, request_fn=_forbidden_request_fn
        )
        assert reconciler._candidates() == ["staged-a", "staged-b"]

    def test_empty_model_dir_yields_no_candidates(self, tmp_path):
        """An empty VLLM_MODEL_DIR yields an empty snapshot and the pass
        issues zero requests.

        # Validates: Requirements 2.1, 3.2
        """
        manager = VllmRuntimeManager(
            model_dir=tmp_path,
            engine_factory=_PerModelOutcomeFactory({}),
            sampling_params_factory=dict,
        )
        control = _LoopbackModelControl(manager)
        reconciler = VllmReconciler(manager, request_fn=control)
        assert reconciler._candidates() == []
        _run_reconciler(manager, control)
        assert control.requests == []

    def test_absent_model_dir_yields_no_candidates(self, tmp_path):
        """A missing VLLM_MODEL_DIR (never staged on this device) yields
        an empty snapshot and the pass issues zero requests.

        # Validates: Requirements 2.1, 3.2
        """
        manager = VllmRuntimeManager(
            model_dir=tmp_path / "does-not-exist",
            engine_factory=_PerModelOutcomeFactory({}),
            sampling_params_factory=dict,
        )
        control = _LoopbackModelControl(manager)
        reconciler = VllmReconciler(manager, request_fn=control)
        assert reconciler._candidates() == []
        _run_reconciler(manager, control)
        assert control.requests == []


def _forbidden_request_fn(url, timeout=None):
    raise AssertionError(
        "no model-control request expected here (got {})".format(url)
    )


# ---------------------------------------------------------------------------
# Unit leg 2: backoff arithmetic and attempt accounting (design Unit
# Tests — no real sleeps: the reconciler module's `time` binding is
# swapped for a recorder)
# ---------------------------------------------------------------------------


class _RecordingTime:
    """Stand-in for the reconciler module's ``time`` binding (the module
    only calls ``time.sleep``); records every requested delay."""

    def __init__(self):
        self.sleeps: List[float] = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)


class TestBackoffArithmetic:
    """# Validates: Requirements 2.1, 3.2"""

    def test_schedule_shape_gives_four_attempts(self):
        """The designed schedule is (30, 120, 480): 4 attempts total
        (initial + one per backoff entry) — bounded, never a retry storm.

        # Validates: Requirements 3.2
        """
        assert RECONCILE_RETRY_BACKOFF_SECONDS == (30, 120, 480)
        assert len(RECONCILE_RETRY_BACKOFF_SECONDS) + 1 == 4

    def test_permanent_failure_exhausts_default_schedule(
        self, tmp_path, monkeypatch
    ):
        """A permanently failing model gets exactly len(backoff) + 1 = 4
        load attempts, sleeps exactly the schedule entries in order
        between attempts, and never sleeps after the final attempt; the
        model is LEFT FAILED with its retained reason.

        # Validates: Requirements 2.1, 3.2
        """
        fake_time = _RecordingTime()
        monkeypatch.setattr(reconciler_module, "time", fake_time)

        name = "model-permanent"
        build_staged_repo(tmp_path, name)
        plan = _OutcomePlan(kind="permanent-failure")
        manager = VllmRuntimeManager(
            model_dir=tmp_path,
            engine_factory=_PerModelOutcomeFactory({name: plan}),
            sampling_params_factory=dict,
        )
        control = _LoopbackModelControl(manager)
        # DEFAULT schedule — the arithmetic under test.
        reconciler = VllmReconciler(manager, request_fn=control)
        thread = reconciler.start()
        thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
        assert not thread.is_alive()

        assert control.requests == [("load", name)] * 4
        assert fake_time.sleeps == list(RECONCILE_RETRY_BACKOFF_SECONDS)
        status = manager.state(name)
        assert status.state is ModelState.FAILED
        assert status.reason == plan.permanent_reason(name)

    def test_fail_then_succeed_stops_the_schedule_early(
        self, tmp_path, monkeypatch
    ):
        """Attempt accounting: a model that succeeds on attempt 3 gets
        exactly 3 load attempts and sleeps only the first 2 entries of
        the injected schedule — success stops the schedule immediately.

        # Validates: Requirements 2.1, 3.2
        """
        fake_time = _RecordingTime()
        monkeypatch.setattr(reconciler_module, "time", fake_time)

        name = "model-flaky"
        build_staged_repo(tmp_path, name)
        plans = {name: _OutcomePlan(kind="fail-then-succeed", failures=2)}
        manager = VllmRuntimeManager(
            model_dir=tmp_path,
            engine_factory=_PerModelOutcomeFactory(plans),
            sampling_params_factory=dict,
        )
        control = _LoopbackModelControl(manager)
        backoff = (7, 11, 13)
        reconciler = VllmReconciler(
            manager, backoff=backoff, request_fn=control
        )
        thread = reconciler.start()
        thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
        assert not thread.is_alive()

        assert control.requests == [("load", name)] * 3
        assert fake_time.sleeps == [7, 11]
        assert manager.state(name).state is ModelState.READY


# ---------------------------------------------------------------------------
# Unit leg 3: KV-OOM marker parity with the prep markers (design Unit
# Tests — "KV-OOM marker matching reuses the prep markers verbatim")
# ---------------------------------------------------------------------------


class TestKvOomMarkerParity:
    """# Validates: Requirements 2.1, 3.2"""

    def test_markers_equal_the_prep_markers_verbatim(self):
        """The reconciler's KV-cache hint markers are the prep script's
        markers VERBATIM (``dda_triton.vllm_model_prep.
        KV_CACHE_HINT_MARKERS``) — the reconciler mirrors request_load's
        marker-driven unload→reload recovery and must never drift from
        it.

        # Validates: Requirements 2.1, 3.2
        """
        import dda_triton.vllm_model_prep as prep

        assert tuple(KV_CACHE_HINT_MARKERS) == tuple(
            prep.KV_CACHE_HINT_MARKERS
        )

    def test_marker_match_drives_exactly_one_recovery_per_attempt(
        self, tmp_path, monkeypatch
    ):
        """A load failure whose extracted reason carries a prep marker
        triggers exactly ONE unload→reload recovery inside the same
        attempt — never a second recovery when the reload fails again
        with the same marker (its reason is deliberately not re-matched).

        # Validates: Requirements 2.1, 3.2
        """
        fake_time = _RecordingTime()
        monkeypatch.setattr(reconciler_module, "time", fake_time)

        name = "model-kv-permanent"
        build_staged_repo(tmp_path, name)
        plans = {
            name: _OutcomePlan(
                kind="kv-oom-permanent", marker=KV_CACHE_HINT_MARKERS[0]
            )
        }
        manager = VllmRuntimeManager(
            model_dir=tmp_path,
            engine_factory=_PerModelOutcomeFactory(plans),
            sampling_params_factory=dict,
        )
        control = _LoopbackModelControl(manager)
        reconciler = VllmReconciler(manager, request_fn=control)
        thread = reconciler.start()
        thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
        assert not thread.is_alive()

        # 4 attempts, each exactly: load-fail → unload → reload-fail.
        assert control.requests == [
            ("load", name), ("unload", name), ("load", name)
        ] * 4
        assert fake_time.sleeps == list(RECONCILE_RETRY_BACKOFF_SECONDS)
        status = manager.state(name)
        assert status.state is ModelState.FAILED
        assert status.reason is not None
        assert KV_CACHE_HINT_MARKERS[0] in status.reason
