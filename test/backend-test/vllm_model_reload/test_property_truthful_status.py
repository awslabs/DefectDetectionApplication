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
"""Truthful-status fix-check property tests (task 4.3, Hypothesis) for
vllm-model-reload-after-backend-restart.

**Feature: vllm-model-reload-after-backend-restart, Property 3:
Fix Checking — Status Surfaces Are Truthful Within Bounded Time**

# Validates: Requirements 2.3, 3.2

Two properties over the two status surfaces — the feature-config merge
(``utils.feature_configs_utils._VLLM_STATUS_MAP`` via
``get_features_vllm()``) and the Text_Generation_API 409 category
(``endpoints.text_generation.state_category``):

1. **Truth-table property** — *for any* manager/tombstone state
   combination (models driven READY, driven FAILED with an arbitrary
   retained reason, left desired-STAGED, held genuinely in-flight
   LOADING through a gated fake factory, tombstoned by an explicit
   unload from tracked or untracked state or by a marker surviving from
   a previous backend life, or never staged), the
   ``(feature-config status, 409 category)`` pair matches the design
   Decision 3 truth table:

   ========================  =====================  ==============
   manager/tombstone state   feature-config status  409 category
   ========================  =====================  ==============
   READY                     READY                  ready
   LOADING / desired-STAGED  LOADING                loading
   tombstoned (UNLOADED)     STOPPED                unloaded
   FAILED                    FAILED (+ reason)      failed
   UNKNOWN (never staged)    absent                 unknown
   ========================  =====================  ==============

   "LOADING" is reported ONLY for models whose load is genuinely in
   flight (the gated factory) or queued behind the reconciler's bounded
   drive (desired-STAGED) — the in-flight models are released at the end
   and asserted READY, proving the load really was in flight.

2. **Bounded-time clause** — *for any* set of staged models whose
   reloads terminally fail (arbitrary non-KV-OOM backend reasons), the
   REAL ``VllmReconciler`` (injectable ``request_fn`` — no sockets;
   zero-second test backoff — no real sleeps) exhausts its bounded
   schedule and the surfaces then report ``(FAILED + retained reason,
   "failed")`` — NEVER an indefinite LOADING with no load in flight:
   the reconciler thread is dead, per-model load-request counts equal
   exactly the schedule (initial + one per backoff entry), and no
   surface anywhere reports LOADING. Interleaved succeeding models end
   ``(READY, "ready")`` after exactly one request (failure isolation
   stays visible on the status surfaces).

No hardcoded ``max_examples`` — profiles come from the environment
(the suite runs ``--noconftest``, so Hypothesis defaults apply).

Honesty guard: fake engine factory through the manager's public
injectable seam, temp ``VLLM_MODEL_DIR`` trees, injectable reconciler
``request_fn`` — no GPU, no vLLM, no container, no sockets, no real
sleeps. Shadow/IPC propagation of these statuses is device-only
(design Testing Strategy) — host coverage stops at
``get_features_vllm()`` and ``state_category``.

Run host-side:
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
        test/backend-test/vllm_model_reload/test_property_truthful_status.py \
        -q -p no:cacheprovider --noconftest
"""
import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from endpoints.text_generation import state_category
from vllm_model_reload.fakes import import_with_awsiot_stubs
from vllm_runtime.constants import UNLOAD_TOMBSTONE_NAME
from vllm_runtime.manager import ModelState, VllmRuntimeManager
from vllm_runtime.reconciler import KV_CACHE_HINT_MARKERS, VllmReconciler

# Imported ONCE at module scope with the runtime-image-only awsiot
# modules stubbed; the module object keeps its own stubs and its own
# ``_vllm_manager`` global (the established suite pattern).
feature_utils = import_with_awsiot_stubs("utils.feature_configs_utils")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _stage_repository(model_dir: Path, model_name: str) -> None:
    """A valid staged Triton_vLLM_Repository whose ``model.json`` carries
    the model name, so the per-model factory can key outcomes off the
    engine args it receives."""
    repo = model_dir / model_name
    (repo / "1").mkdir(parents=True, exist_ok=True)
    (repo / "config.pbtxt").write_text('backend: "vllm"\n')
    (repo / "1" / "model.json").write_text(json.dumps({"model": model_name}))


class _FakeEngine:
    def shutdown_background_loop(self):
        pass


class _PerModelFactory:
    """Engine factory keyed by model name (read from the parsed engine
    args) — outcome ordering is independent of load scheduling:

    - ``("success", None)``  -> a fresh fake engine
    - ``("fail", reason)``   -> raises ``RuntimeError(reason)`` (the
      manager retains ``str(err)`` — the reason, verbatim)
    - ``("block", event)``   -> a coroutine suspended on ``event`` (the
      manager awaits awaitable factory results), holding the model
      genuinely in-flight LOADING until the test releases the gate
    """

    def __init__(self):
        self.outcomes = {}
        self.calls = []

    def __call__(self, engine_args):
        name = engine_args["model"]
        self.calls.append(name)
        kind, payload = self.outcomes.get(name, ("success", None))
        if kind == "fail":
            raise RuntimeError(payload)
        if kind == "block":
            async def _blocked_construction():
                await payload.wait()
                return _FakeEngine()

            return _blocked_construction()
        return _FakeEngine()


def _probe_surfaces(manager):
    """One snapshot of BOTH status surfaces: the feature-config entries
    (keyed by model name) and a callable giving the 409 category the
    Text_Generation_API would report for a name right now."""
    entries = {
        entry.modelName: entry for entry in feature_utils.get_features_vllm()
    }

    def category(model_name):
        status = manager.state(model_name)
        return state_category(getattr(status, "state", status))

    return entries, category


#: Retained failure reasons: arbitrary non-empty printable text. The
#: KV-OOM hint markers are excluded so a generated reason never triggers
#: the reconciler's unload->reload recovery (that interleaving belongs
#: to tasks 4.1/4.4) — statistically impossible anyway, filtered for
#: rigor.
_reasons = st.text(
    alphabet=st.characters(codec="utf-8", exclude_categories=("Cs", "Cc")),
    min_size=1,
    max_size=40,
).filter(
    lambda text: not any(marker in text for marker in KV_CACHE_HINT_MARKERS)
)


# ---------------------------------------------------------------------------
# Property leg 1: the (feature-config status, 409 category) truth table
# ---------------------------------------------------------------------------

_MODEL_POOL = tuple(
    "truthful-model-{}".format(token) for token in ("a", "b", "c", "d", "e", "f")
)

#: The design Decision 3 truth table, keyed by the drawn state kind.
_TRUTH_TABLE = {
    "ready": ("READY", "ready"),
    "loading": ("LOADING", "loading"),
    "staged": ("LOADING", "loading"),
    "tombstone-fresh-unload": ("STOPPED", "unloaded"),
    "tombstone-after-ready": ("STOPPED", "unloaded"),
    "tombstone-previous-life": ("STOPPED", "unloaded"),
    "failed": ("FAILED", "failed"),
}

_state_kinds = st.one_of(
    st.just(("ready", None)),
    st.tuples(st.just("failed"), _reasons),
    st.just(("staged", None)),
    st.just(("loading", None)),
    # Tombstoned three ways: explicit unload of a never-tracked staged
    # repo, explicit unload after READY, and a marker surviving on disk
    # from a previous backend life (the restart case).
    st.just(("tombstone-fresh-unload", None)),
    st.just(("tombstone-after-ready", None)),
    st.just(("tombstone-previous-life", None)),
    st.just(("unknown", None)),
)

_model_state_sets = st.dictionaries(
    keys=st.sampled_from(_MODEL_POOL),
    values=_state_kinds,
    min_size=1,
    max_size=len(_MODEL_POOL),
)


async def _drive_states(manager, factory, model_dir, model_states):
    """Drive every model to its drawn state through the manager's REAL
    public seams; return the (task, gate) pairs of the genuinely
    in-flight LOADING models."""
    in_flight = []
    for name in sorted(model_states):
        kind, reason = model_states[name]
        if kind == "unknown":
            continue  # never staged, never touched
        _stage_repository(model_dir, name)
        if kind == "ready":
            factory.outcomes[name] = ("success", None)
            status = await manager.load(name)
            assert status.state is ModelState.READY, "harness precondition"
        elif kind == "failed":
            factory.outcomes[name] = ("fail", reason)
            status = await manager.load(name)
            assert status.state is ModelState.FAILED, "harness precondition"
        elif kind == "loading":
            gate = asyncio.Event()
            factory.outcomes[name] = ("block", gate)
            task = asyncio.ensure_future(manager.load(name))
            # Yield (no real sleeps) until the load is genuinely in
            # flight — suspended inside engine construction.
            for _ in range(1000):
                if manager.state(name).state is ModelState.LOADING:
                    break
                await asyncio.sleep(0)
            assert manager.state(name).state is ModelState.LOADING, (
                "harness precondition: load never reached LOADING")
            in_flight.append((name, task, gate))
        elif kind == "staged":
            pass  # desired-STAGED: staged on disk, no load requested yet
        elif kind == "tombstone-fresh-unload":
            manager.unload(name)  # untracked; writes the tombstone
        elif kind == "tombstone-after-ready":
            factory.outcomes[name] = ("success", None)
            status = await manager.load(name)
            assert status.state is ModelState.READY, "harness precondition"
            assert manager.unload(name) is True
        elif kind == "tombstone-previous-life":
            (model_dir / name / UNLOAD_TOMBSTONE_NAME).write_text(
                json.dumps({"marker": "explicit unload",
                            "unloaded_at_utc": "2026-08-16T22:33:11+00:00"}))
        else:  # pragma: no cover - strategy and driver must stay in sync
            raise AssertionError("undriven state kind: {}".format(kind))
    return in_flight


def _assert_truth_table(manager, model_states):
    """Both surfaces, checked against the truth table for EVERY model."""
    entries, category = _probe_surfaces(manager)
    for name in sorted(model_states):
        kind, reason = model_states[name]
        got_category = category(name)
        if kind == "unknown":
            assert name not in entries, (
                "TRUTHFULNESS VIOLATION (Property 3 / 2.3): never-staged "
                "'{}' appeared in the feature-config entries as "
                "{!r}".format(name, entries.get(name)))
            assert got_category == "unknown", (
                "TRUTHFULNESS VIOLATION (Property 3 / 2.3): never-staged "
                "'{}' reported 409 category {!r}, expected "
                "'unknown'".format(name, got_category))
            continue
        expected_status, expected_category = _TRUTH_TABLE[kind]
        assert name in entries, (
            "TRUTHFULNESS VIOLATION (Property 3 / 2.3): staged model '{}' "
            "({}) is absent from the feature-config entries".format(
                name, kind))
        entry = entries[name]
        assert (entry.status, got_category) == (
            expected_status, expected_category), (
            "TRUTHFULNESS VIOLATION (Property 3 / 2.3): model '{}' in "
            "drawn state '{}' reported (feature-config status, 409 "
            "category) == ({!r}, {!r}), truth table says ({!r}, "
            "{!r})".format(name, kind, entry.status, got_category,
                           expected_status, expected_category))
        if kind == "failed":
            retained = manager.state(name).reason
            assert retained == reason, (
                "TRUTHFULNESS VIOLATION (Property 3 / 2.3): FAILED model "
                "'{}' retained reason {!r}, expected the backend reason "
                "{!r}".format(name, retained, reason))
            assert entry.defaultConfiguration.get("failureReason") == reason, (
                "TRUTHFULNESS VIOLATION (Property 3 / 2.3): FAILED model "
                "'{}' feature-config entry carries failureReason {!r}, "
                "expected {!r}".format(
                    name, entry.defaultConfiguration.get("failureReason"),
                    reason))
        else:
            assert "failureReason" not in entry.defaultConfiguration, (
                "TRUTHFULNESS VIOLATION (Property 3 / 2.3): non-FAILED "
                "model '{}' ({}) carries a failureReason".format(name, kind))


@given(model_states=_model_state_sets)
@settings(deadline=None)
def test_status_surfaces_match_truth_table(model_states):
    """**Feature: vllm-model-reload-after-backend-restart, Property 3:
    Fix Checking — Status Surfaces Are Truthful Within Bounded Time
    (truth table)**

    *For any* manager/tombstone state combination, the
    ``(feature-config status, 409 category)`` pair matches the truth
    table: READY→(READY, ready); LOADING/desired-STAGED→(LOADING,
    loading); tombstoned→(STOPPED, unloaded); FAILED→(FAILED + retained
    reason, failed); UNKNOWN→(absent, unknown). The in-flight LOADING
    models are then released and asserted READY — their "LOADING" was
    reported while a load was GENUINELY in flight.

    # Validates: Requirements 2.3, 3.2
    **Validates: Requirements 2.3, 3.2**
    """
    model_dir = Path(tempfile.mkdtemp(prefix="vllm-truthful-status-"))
    factory = _PerModelFactory()
    manager = VllmRuntimeManager(model_dir=model_dir, engine_factory=factory)
    saved_manager = feature_utils.get_vllm_manager()
    feature_utils.set_vllm_manager(manager)

    async def _run():
        in_flight = await _drive_states(
            manager, factory, model_dir, model_states)
        try:
            _assert_truth_table(manager, model_states)
        finally:
            # Release the gated constructions and drain the load tasks
            # regardless of the assertion outcome (no leaked tasks).
            for _, _, gate in in_flight:
                gate.set()
            for name, task, _ in in_flight:
                status = await task
                if status.state is not ModelState.READY:
                    raise AssertionError(
                        "TRUTHFULNESS VIOLATION (Property 3 / 2.3): "
                        "in-flight model '{}' did not reach READY after "
                        "its gated construction was released (got {}) — "
                        "its reported LOADING had no load genuinely in "
                        "flight".format(name, status.state))

    try:
        asyncio.run(_run())
    finally:
        feature_utils.set_vllm_manager(saved_manager)
        shutil.rmtree(model_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property leg 2: bounded time — terminal reload failure reports FAILED
# after schedule exhaustion, never an indefinite LOADING
# ---------------------------------------------------------------------------

_RELOAD_POOL = tuple(
    "reload-model-{}".format(token) for token in ("a", "b", "c", "d"))

#: Zero-second backoff: the REAL 4-attempt schedule shape (initial + one
#: per entry) with no real sleeps.
_ZERO_BACKOFF = (0, 0, 0)

_reload_outcome_sets = st.dictionaries(
    keys=st.sampled_from(_RELOAD_POOL),
    values=st.one_of(
        st.tuples(st.just("terminal-failure"), _reasons),
        st.just(("success", None)),
    ),
    min_size=1,
    max_size=len(_RELOAD_POOL),
).filter(
    lambda outcomes: any(
        kind == "terminal-failure" for kind, _ in outcomes.values())
)


class _FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class _LoopbackModelControl:
    """The reconciler's injectable ``request_fn`` (design File 1 seam —
    no sockets): drives the REAL ``manager.load``/``manager.unload`` and
    answers with the model-control response shapes the reconciler reads
    (200 on success; a Triton-style ``{"error": reason}`` body
    otherwise)."""

    def __init__(self, manager):
        self._manager = manager
        self.load_calls = {}

    def __call__(self, url, timeout=None):
        action = url.rstrip("/").rsplit("/", 1)[-1]
        model_name = url.rstrip("/").split("/")[-2]
        if action == "load":
            self.load_calls[model_name] = (
                self.load_calls.get(model_name, 0) + 1)
            status = asyncio.run(self._manager.load(model_name))
            if status.state is ModelState.READY:
                return _FakeResponse(200, "")
            return _FakeResponse(
                400, json.dumps({"error": status.reason or ""}))
        if action == "unload":
            self._manager.unload(model_name)
            return _FakeResponse(200, "")
        raise AssertionError("unexpected model-control URL: " + url)


@given(reload_outcomes=_reload_outcome_sets)
@settings(deadline=None)
def test_terminal_reload_failure_reports_failed_after_schedule_exhaustion(
        reload_outcomes):
    """**Feature: vllm-model-reload-after-backend-restart, Property 3:
    Fix Checking — Status Surfaces Are Truthful Within Bounded Time
    (bounded-time clause)**

    *For any* staged model set containing at least one terminally
    failing reload (arbitrary retained reasons), after the REAL
    reconciler's pass completes: every terminally failing model reports
    ``(FAILED + retained reason, "failed")`` on both surfaces with its
    load requested exactly schedule-many times (initial + one per
    backoff entry — exhaustion, bounded); every succeeding model reports
    ``(READY, "ready")`` after exactly one request; NO surface reports
    LOADING and no load is in flight (the reconciler thread is dead) —
    never an indefinite LOADING with no load in flight.

    # Validates: Requirements 2.3, 3.2
    **Validates: Requirements 2.3, 3.2**
    """
    model_dir = Path(tempfile.mkdtemp(prefix="vllm-truthful-exhaustion-"))
    factory = _PerModelFactory()
    manager = VllmRuntimeManager(model_dir=model_dir, engine_factory=factory)
    saved_manager = feature_utils.get_vllm_manager()
    feature_utils.set_vllm_manager(manager)
    try:
        for name, (kind, reason) in reload_outcomes.items():
            _stage_repository(model_dir, name)
            factory.outcomes[name] = (
                ("fail", reason) if kind == "terminal-failure"
                else ("success", None))

        control = _LoopbackModelControl(manager)
        reconciler = VllmReconciler(
            manager, backoff=_ZERO_BACKOFF, request_fn=control)
        thread = reconciler.start()
        thread.join(timeout=30.0)
        assert not thread.is_alive(), (
            "BOUNDED-TIME VIOLATION (Property 3 / 3.2): the reconciler "
            "pass did not finish within the test budget")

        schedule_attempts = len(_ZERO_BACKOFF) + 1
        entries, category = _probe_surfaces(manager)
        for name, (kind, reason) in reload_outcomes.items():
            entry = entries[name]
            pair = (entry.status, category(name))
            if kind == "terminal-failure":
                assert pair == ("FAILED", "failed"), (
                    "TRUTHFULNESS VIOLATION (Property 3 / 2.3): model "
                    "'{}' whose reload terminally failed reports {!r} "
                    "after schedule exhaustion, expected ('FAILED', "
                    "'failed') — an indefinite LOADING with no load in "
                    "flight is the bug's fingerprint".format(name, pair))
                retained = manager.state(name).reason
                assert retained == reason, (
                    "TRUTHFULNESS VIOLATION (Property 3 / 2.3): model "
                    "'{}' retained reason {!r}, expected the backend "
                    "reason {!r}".format(name, retained, reason))
                assert entry.defaultConfiguration.get(
                    "failureReason") == reason
                assert control.load_calls.get(name) == schedule_attempts, (
                    "BOUNDED-TIME VIOLATION (Property 3 / 3.2): model "
                    "'{}' saw {} load request(s), expected exactly the "
                    "bounded schedule of {}".format(
                        name, control.load_calls.get(name),
                        schedule_attempts))
            else:
                assert pair == ("READY", "ready"), (
                    "PRESERVATION VIOLATION (Property 3 / 3.2): "
                    "succeeding model '{}' reports {!r}, expected "
                    "('READY', 'ready')".format(name, pair))
                assert control.load_calls.get(name) == 1, (
                    "BOUNDED-TIME VIOLATION (Property 3 / 3.2): "
                    "succeeding model '{}' saw {} load request(s), "
                    "expected exactly 1".format(
                        name, control.load_calls.get(name)))
        # The categorical clause: nothing anywhere reports LOADING.
        loading_reports = sorted(
            name for name, entry in entries.items()
            if entry.status == "LOADING" or category(name) == "loading")
        assert not loading_reports, (
            "TRUTHFULNESS VIOLATION (Property 3 / 2.3): after the "
            "reconciler pass finished (no load in flight), these models "
            "still report LOADING: {}".format(loading_reports))
    finally:
        feature_utils.set_vllm_manager(saved_manager)
        shutil.rmtree(model_dir, ignore_errors=True)
