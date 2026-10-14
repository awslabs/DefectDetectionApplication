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
"""Preservation property tests (task 2, Hypothesis) for
vllm-model-reload-after-backend-restart.

**Feature: vllm-model-reload-after-backend-restart, Property 2:
Preservation — Everything Outside the Bug Condition Is Unchanged**

Observation-first: the reference models below were transcribed from the
UNFIXED ``vllm_runtime/manager.py`` and ``utils/feature_configs_utils.py``
(2026-08-17, branch spec/jetpack7-support) and act as the recorded
baselines. Both properties run over inputs WITHOUT tombstones — the
fixed-tree ``UNLOADED`` state is unreachable without a tombstone marker,
so fixed behavior must deep-equal these references (design "Preservation
Checking" items 3 and 5).

Properties:

1. **Manager state-machine identity (Requirement 3.2)** — *for any*
   load/unload/fail operation sequence over staged repositories (fake
   factory: success / raise / fail-then-succeed via re-load), the
   manager's returned statuses, ``state()`` answers, and
   ``list_models()`` payloads deep-equal the recorded reference after
   EVERY operation; one model's failure never touches another model
   (failure isolation, pinned by the deep equality of the full table).
2. **Status payload identity (Requirement 3.4)** — *for any* manager
   model set without tombstones (models left STAGED, driven READY, or
   driven FAILED with an arbitrary retained reason),
   ``get_features_vllm()`` deep-equals the recorded per-entry reference
   (sorted names, ``VllmModel`` type, the STAGED→LOADING mapping,
   FAILED-reason retention gated on truthiness).

No hardcoded ``max_examples`` — profiles come from the environment
(the suite runs ``--noconftest``, so Hypothesis defaults apply).

Honesty guard: fake engine factory, temp ``VLLM_MODEL_DIR`` trees, no
GPU/vLLM/container. Helpers are module-local (suite-shared ``fakes.py``
is owned by task 1).

Run host-side:
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
        test/backend-test/vllm_model_reload -q -p no:cacheprovider --noconftest
"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from vllm_runtime.manager import (
    ModelState,
    ModelStatus,
    UNKNOWN_STATUS,
    VllmRuntimeManager,
)

# ---------------------------------------------------------------------------
# Module-local helpers (fakes.py is task 1's file — do not import it here)
# ---------------------------------------------------------------------------


def _stage_repository(model_dir: Path, model_name: str) -> None:
    repo = model_dir / model_name
    (repo / "1").mkdir(parents=True)
    (repo / "config.pbtxt").write_text('backend: "vllm"\n')
    (repo / "1" / "model.json").write_text('{"model": "/fake/weights"}')


class _ScriptedFactory:
    """Engine factory whose next outcome is scripted per load call:
    'success' returns a fresh fake engine, 'fail' raises with the
    scripted reason (the manager retains ``str(err)``)."""

    def __init__(self):
        self.next_outcome = ("success", None)
        self.calls = 0

    def __call__(self, engine_args):
        self.calls += 1
        kind, reason = self.next_outcome
        if kind == "fail":
            raise RuntimeError(reason)
        return _FakeEngine()


class _FakeEngine:
    def __init__(self):
        self.shutdown_calls = 0

    def shutdown_background_loop(self):
        self.shutdown_calls += 1


def _import_with_awsiot_stubs(module_name):
    """test_feature_configs_vllm_merge.py pattern, duplicated
    module-local because fakes.py belongs to task 1."""
    import types
    from unittest.mock import MagicMock

    installed = []

    def _register(name, module):
        if name in sys.modules:
            return
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module
            installed.append(name)

    awsiot = types.ModuleType("awsiot")
    ggipc = types.ModuleType("awsiot.greengrasscoreipc")
    ggipc.connect = MagicMock()
    ggipc_model = types.ModuleType("awsiot.greengrasscoreipc.model")
    ggipc_model.ResourceNotFoundError = type(
        "ResourceNotFoundError", (Exception,), {})
    ggipc_model.UnauthorizedError = type(
        "UnauthorizedError", (Exception,), {})
    ggipc_model.GetConfigurationRequest = MagicMock()
    awsiot.greengrasscoreipc = ggipc
    ggipc.model = ggipc_model
    _register("awsiot", awsiot)
    _register("awsiot.greengrasscoreipc", ggipc)
    _register("awsiot.greengrasscoreipc.model", ggipc_model)

    try:
        module = __import__(module_name, fromlist=["_"])
    finally:
        for name in installed:
            sys.modules.pop(name, None)
        if installed:
            sys.modules.pop(module_name, None)
    return module


# Imported ONCE at module scope; the module object keeps its own stubs
# (the model_gpu_fallback_visibility precedent).
feature_utils = _import_with_awsiot_stubs("utils.feature_configs_utils")


# ---------------------------------------------------------------------------
# Property 1: manager state-machine identity (Requirement 3.2)
# ---------------------------------------------------------------------------

_MODEL_POOL = ("model-alpha", "model-bravo", "model-charlie")
_NEVER_STAGED = "model-never-staged"


class _ReferenceManager:
    """The UNFIXED manager state machine, transcribed 2026-08-17 as the
    recorded preservation reference (no tombstones in the domain):

    - ``state(name)``: the tracked status when tracked; STAGED when the
      repo is on disk untracked; UNKNOWN otherwise.
    - ``load(name, outcome)``: tracked LOADING/READY → idempotent
      current-status return, no change. Otherwise (fresh, FAILED, or
      unloaded): success → READY(None); fail → FAILED(reason). A FAILED
      model is re-loadable directly — the fail-then-succeed path.
    - ``unload(name)``: pops the tracked entry, returns whether one
      existed; the staged repo on disk is untouched.
    - ``list_models()``: every tracked entry plus every staged-on-disk
      untracked repo as STAGED.
    """

    def __init__(self, staged_names):
        self.staged = set(staged_names)
        self.tracked = {}

    def load(self, name, outcome, reason):
        current = self.tracked.get(name)
        if current is not None and current.state in (
                ModelState.LOADING, ModelState.READY):
            return current
        if outcome == "success":
            status = ModelStatus(ModelState.READY)
        else:
            status = ModelStatus(ModelState.FAILED, reason=reason)
        self.tracked[name] = status
        return status

    def unload(self, name):
        return self.tracked.pop(name, None) is not None

    def state(self, name):
        if name in self.tracked:
            return self.tracked[name]
        if name in self.staged:
            return ModelStatus(ModelState.STAGED)
        return UNKNOWN_STATUS

    def list_models(self):
        statuses = dict(self.tracked)
        for name in sorted(self.staged):
            if name not in statuses:
                statuses[name] = ModelStatus(ModelState.STAGED)
        return statuses


_operations = st.lists(
    st.one_of(
        st.tuples(
            st.just("load"),
            st.sampled_from(_MODEL_POOL),
            st.sampled_from(("success", "fail")),
        ),
        st.tuples(
            st.just("unload"),
            st.sampled_from(_MODEL_POOL),
            st.none(),
        ),
    ),
    min_size=1,
    max_size=12,
)


@given(operations=_operations)
@settings(deadline=None)
def test_manager_state_machine_identity_without_tombstones(operations):
    """**Feature: vllm-model-reload-after-backend-restart, Property 2:
    Preservation — manager state-machine identity (no tombstones)**

    *For any* load/unload/fail sequence over staged repositories, the
    real manager's returned statuses, per-name ``state()`` answers, and
    full ``list_models()`` tables deep-equal the reference recorded from
    the unfixed tree, after every single operation. UNLOADED is
    unreachable in this domain; per-model failure isolation is implied
    by the deep equality (a fail op perturbs exactly one entry).

    # Validates: Requirements 3.2
    **Validates: Requirements 3.2**
    """
    model_dir = Path(tempfile.mkdtemp(prefix="vllm-reload-statemachine-"))
    try:
        for name in _MODEL_POOL:
            _stage_repository(model_dir, name)
        factory = _ScriptedFactory()
        manager = VllmRuntimeManager(
            model_dir=model_dir, engine_factory=factory)
        reference = _ReferenceManager(_MODEL_POOL)

        for index, (op, name, outcome) in enumerate(operations):
            if op == "load":
                reason = "injected engine failure #{}".format(index)
                factory.next_outcome = (
                    "fail" if outcome == "fail" else "success", reason)
                got = asyncio.run(manager.load(name))
                expected = reference.load(name, outcome, reason)
                assert got == expected, (
                    "PRESERVATION REGRESSION (Property 2 / 3.2): load "
                    "#{} of '{}' ({}) returned {!r}, reference says "
                    "{!r}".format(index, name, outcome, got, expected))
            else:
                got_unload = manager.unload(name)
                expected_unload = reference.unload(name)
                assert got_unload == expected_unload, (
                    "PRESERVATION REGRESSION (Property 2 / 3.2): unload "
                    "#{} of '{}' returned {!r}, reference says "
                    "{!r}".format(index, name, got_unload,
                                  expected_unload))
                # DOMAIN ENFORCEMENT (task 3.2 triage): this property's
                # documented domain is sequences WITHOUT tombstones
                # (design Preservation Checking item 3 — "UNLOADED is
                # unreachable without a tombstone"). On the FIXED tree an
                # explicit unload of a still-staged repo intentionally
                # writes the Unload_Tombstone (Decision 2) and the repo
                # then reports UNLOADED (Decision 3) — that changed
                # behavior is the bug-condition domain, fix-checked by
                # Properties 3+4 (tasks 4.3/4.4), NOT preservation.
                # Removing the marker here keeps the sequence inside the
                # declared no-tombstone domain; on the unfixed tree no
                # marker ever exists, so this is a no-op and the
                # unload return-value identity above is untouched.
                (model_dir / name / ".dda_explicit_unload").unlink(
                    missing_ok=True)

            # The full observable surface after EVERY operation.
            assert manager.list_models() == reference.list_models(), (
                "PRESERVATION REGRESSION (Property 2 / 3.2): "
                "list_models diverged after op #{} {!r}".format(
                    index, (op, name, outcome)))
            for probe in _MODEL_POOL + (_NEVER_STAGED,):
                assert manager.state(probe) == reference.state(probe), (
                    "PRESERVATION REGRESSION (Property 2 / 3.2): "
                    "state('{}') diverged after op #{} {!r}".format(
                        probe, index, (op, name, outcome)))
    finally:
        shutil.rmtree(model_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 2: status payload identity (Requirement 3.4)
# ---------------------------------------------------------------------------

_STATUS_POOL = tuple(
    "vllm-status-model-{}".format(token)
    for token in ("a", "b", "c", "d", "e"))

#: Drawn model sets: name -> desired pre-read state. FAILED carries an
#: arbitrary retained reason (possibly empty/blank — the unfixed entry
#: builder gates failureReason on truthiness, mirrored in the
#: reference).
_model_sets = st.dictionaries(
    keys=st.sampled_from(_STATUS_POOL),
    values=st.one_of(
        st.just(("staged", None)),
        st.just(("ready", None)),
        st.tuples(
            st.just("failed"),
            st.text(
                alphabet=st.characters(
                    codec="utf-8", exclude_categories=("Cs", "Cc")),
                max_size=40,
            ),
        ),
    ),
    max_size=len(_STATUS_POOL),
)


def _expected_feature_entries(model_states):
    """The UNFIXED ``get_features_vllm`` payload, transcribed 2026-08-17
    as the recorded reference: one VllmModel entry per model, sorted by
    name, STAGED→LOADING / READY→READY / FAILED→FAILED, failureReason
    merged only for FAILED with a truthy reason."""
    mapping = {"staged": "LOADING", "ready": "READY", "failed": "FAILED"}
    expected = []
    for name in sorted(model_states):
        kind, reason = model_states[name]
        status = mapping[kind]
        configuration = {"modelAlias": name}
        if status == "FAILED" and reason:
            configuration["failureReason"] = reason
        expected.append({
            "type": "VllmModel",
            "modelName": name,
            "status": status,
            "defaultConfiguration": configuration,
        })
    return expected


@given(model_states=_model_sets)
@settings(deadline=None)
def test_status_payload_identity_without_tombstones(model_states):
    """**Feature: vllm-model-reload-after-backend-restart, Property 2:
    Preservation — status payload identity (no tombstones)**

    *For any* manager model set without tombstones — models left
    STAGED on disk, driven READY through the injectable factory, or
    driven FAILED with an arbitrary retained reason —
    ``get_features_vllm()`` deep-equals the recorded unfixed reference:
    sorted VllmModel entries, the retained STAGED→"LOADING" mapping,
    FAILED reason retention. The empty set yields the empty list.

    # Validates: Requirements 3.4
    **Validates: Requirements 3.4**
    """
    model_dir = Path(tempfile.mkdtemp(prefix="vllm-reload-status-"))
    factory = _ScriptedFactory()
    manager = VllmRuntimeManager(model_dir=model_dir, engine_factory=factory)
    saved_manager = feature_utils.get_vllm_manager()
    try:
        for name, (kind, reason) in model_states.items():
            _stage_repository(model_dir, name)
            if kind == "ready":
                factory.next_outcome = ("success", None)
                status = asyncio.run(manager.load(name))
                assert status.state is ModelState.READY
            elif kind == "failed":
                factory.next_outcome = ("fail", reason)
                status = asyncio.run(manager.load(name))
                assert status.state is ModelState.FAILED

        feature_utils.set_vllm_manager(manager)
        entries = feature_utils.get_features_vllm()

        got = [{
            "type": entry.type,
            "modelName": entry.modelName,
            "status": entry.status,
            "defaultConfiguration": entry.defaultConfiguration,
        } for entry in entries]
        expected = _expected_feature_entries(model_states)
        assert got == expected, (
            "PRESERVATION REGRESSION (Property 2 / 3.4): "
            "get_features_vllm diverged from the recorded reference:\n"
            "got      {!r}\nexpected {!r}".format(got, expected))
    finally:
        feature_utils.set_vllm_manager(saved_manager)
        shutil.rmtree(model_dir, ignore_errors=True)
