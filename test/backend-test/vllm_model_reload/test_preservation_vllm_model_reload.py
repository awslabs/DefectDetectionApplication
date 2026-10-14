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
"""Preservation pins (task 2, example-style) for
vllm-model-reload-after-backend-restart.

**Feature: vllm-model-reload-after-backend-restart, Property 2:
Preservation — Everything Outside the Bug Condition Is Unchanged**

Observation-first: every value below was OBSERVED on the UNFIXED tree
(2026-08-17, branch spec/jetpack7-support) and recorded here as a golden.
These tests PASS on the unfixed tree and must keep passing after the fix
(task 3.7 re-runs them at baseline counts).

Pins in this file (the Hypothesis properties live in
``test_property_vllm_reload_preservation.py``):

1. ``vllm_model_prep.py`` sha256 pin (Requirements 3.1, 3.7) — the file
   is explicitly NOT modified by this spec; the hash is the strongest
   available statement that first-deploy Startup semantics (validation →
   exit 1, LOAD_UNREACHABLE → exit 1 + diagnostic, LOAD_HTTP_ERROR →
   exit 1 + authoritative log, KV-OOM single unload→reload recovery,
   atomic staging) cannot have changed.
2. Unload identity (Requirement 3.5) — return values and engine-freeing
   behavior for tracked/untracked/READY/FAILED models; after the fix the
   ONLY permissible filesystem addition is the tombstone marker.
3. vLLM-free inertness (Requirement 3.6) — with ``VLLM_AVAILABLE``
   forced false the REAL ``start_vllm_runtime`` source returns None
   without importing ``vllm_runtime.reconciler`` and without starting a
   ``vllm-reconciler`` thread. The module-side-effect leg is
   skip-as-absent on the unfixed tree (the module does not exist yet;
   binds at task 3.7).
4. Constants/bind identity (Requirement 3.8) —
   ``VLLM_RUNTIME_HOST``/``VLLM_RUNTIME_PORT`` values and
   ``VllmRuntimeServer`` bind arguments.
5. Status-mapping dict-subset pins (Requirement 3.4) — the observed
   ``_VLLM_STATUS_MAP`` / ``_STATE_CATEGORY`` entries must stay
   byte-identical; encoded as dict-subset so the fix's ADDITIVE
   ``UNLOADED`` mappings pass.

Honesty guard: no real vLLM engine, GPU, container, or Greengrass —
the manager runs on the injectable ``engine_factory`` seam over temp
``VLLM_MODEL_DIR`` trees. Helpers are module-local (the suite-shared
``fakes.py`` is owned by task 1 and may not exist yet).

Run host-side:
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
        test/backend-test/vllm_model_reload -q -p no:cacheprovider --noconftest
"""
import ast
import asyncio
import hashlib
import importlib.util
import logging
import os
import shutil
import sys
import tempfile
import threading
import traceback as _traceback_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from vllm_runtime import constants as vllm_constants
from vllm_runtime.manager import ModelState, ModelStatus, VllmRuntimeManager

# Repo root: this file lives at test/backend-test/vllm_model_reload/.
_REPO_ROOT = Path(__file__).resolve().parents[3]

_PREP_PATH = _REPO_ROOT / "src" / "backend" / "dda_triton" / "vllm_model_prep.py"
_APP_PATH = _REPO_ROOT / "src" / "backend" / "app.py"
_CONSTANTS_PATH = (
    _REPO_ROOT / "src" / "backend" / "vllm_runtime" / "constants.py"
)

#: sha256 of src/backend/dda_triton/vllm_model_prep.py, recorded verbatim
#: on the UNFIXED tree (2026-08-17). The file is on the design's
#: "Explicitly NOT changed" list — its LOAD_UNREACHABLE "stays staged for
#: the next LocalServer start" diagnostic becomes TRUE via this fix.
PREP_SHA256 = "9f3b148a19c9f91e2bdd389428437246deabf8d533c08c0477951f196f7271b9"

#: Decision 2 tombstone marker name (design File 3, lands at task 3.3).
#: On the unfixed tree no unload writes it; after the fix it is the ONLY
#: file unload may add to a still-staged repository.
UNLOAD_TOMBSTONE_NAME = ".dda_explicit_unload"

#: Observed _VLLM_STATUS_MAP on the UNFIXED feature_configs_utils
#: (2026-08-17): these entries must survive the fix byte-identical; the
#: fix may only ADD (the reporting-only UNLOADED → "STOPPED" mapping).
BASELINE_VLLM_STATUS_MAP = {
    "STAGED": "LOADING",
    "LOADING": "LOADING",
    "READY": "READY",
    "FAILED": "FAILED",
}

#: Observed _STATE_CATEGORY on the UNFIXED endpoints/text_generation
#: (2026-08-17): same dict-subset discipline (UNLOADED → "unloaded" is
#: the only permissible addition).
BASELINE_STATE_CATEGORY = {
    "READY": "ready",
    "STAGED": "loading",
    "LOADING": "loading",
    "FAILED": "failed",
    "UNKNOWN": "unknown",
}


# ---------------------------------------------------------------------------
# Module-local helpers (fakes.py is task 1's file — do not import it here)
# ---------------------------------------------------------------------------


def _stage_repository(model_dir: Path, model_name: str) -> None:
    """Minimal valid Triton_vLLM_Repository: config.pbtxt declaring the
    vllm backend and a 1/model.json engine-args object."""
    repo = model_dir / model_name
    (repo / "1").mkdir(parents=True)
    (repo / "config.pbtxt").write_text('backend: "vllm"\n')
    (repo / "1" / "model.json").write_text('{"model": "/fake/weights"}')


class _FakeEngine:
    """Recording fake of the ``AsyncLLMEngine`` surface the manager
    shuts down on unload."""

    def __init__(self):
        self.shutdown_calls = 0

    def shutdown_background_loop(self):
        self.shutdown_calls += 1


def _repo_files(model_dir: Path, model_name: str):
    """Every path (relative) currently under one staged repository."""
    repo = model_dir / model_name
    return {
        str(path.relative_to(repo))
        for path in repo.rglob("*")
    }


def _import_with_awsiot_stubs(module_name):
    """Import a backend module with the runtime-image-only ``awsiot``
    modules stubbed, then drop the stubs AND the module from
    ``sys.modules`` so nothing leaks (the established
    test_feature_configs_vllm_merge.py pattern, duplicated module-local
    because fakes.py belongs to task 1)."""
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


@pytest.fixture
def model_dir():
    path = Path(tempfile.mkdtemp(prefix="vllm-reload-preservation-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. vllm_model_prep.py hash pin (Requirements 3.1, 3.7)
# ---------------------------------------------------------------------------


class TestPrepScriptHashPin:

    def test_vllm_model_prep_sha256_unchanged(self):
        """# Validates: Requirements 3.1, 3.7

        The component Startup script is explicitly NOT modified by this
        spec: pin its exact bytes. An intended future edit must
        consciously rebaseline this hash with its own justification.
        """
        assert _PREP_PATH.is_file(), (
            "vllm_model_prep.py moved or vanished: {}".format(_PREP_PATH))
        digest = hashlib.sha256(_PREP_PATH.read_bytes()).hexdigest()
        assert digest == PREP_SHA256, (
            "PRESERVATION REGRESSION (Property 2 / 3.1, 3.7): "
            "src/backend/dda_triton/vllm_model_prep.py changed "
            "(sha256 {} != recorded {}); this spec must not touch the "
            "component Startup script".format(digest, PREP_SHA256))


# ---------------------------------------------------------------------------
# 2. Unload identity (Requirement 3.5)
# ---------------------------------------------------------------------------


class TestUnloadIdentity:

    def test_unload_of_never_staged_name_returns_false(self, model_dir):
        """# Validates: Requirements 3.5"""
        manager = VllmRuntimeManager(model_dir=model_dir)
        assert manager.unload("never-staged") is False

    def test_unload_of_staged_untracked_model_returns_false_and_leaves_repo(
            self, model_dir):
        """# Validates: Requirements 3.5

        A staged repository the manager never loaded is not "tracked":
        unload returns False today. After the fix the return value must
        stay False and the repository tree must gain at most the
        tombstone marker.
        """
        _stage_repository(model_dir, "staged-only")
        before = _repo_files(model_dir, "staged-only")
        manager = VllmRuntimeManager(model_dir=model_dir)
        assert manager.unload("staged-only") is False
        after = _repo_files(model_dir, "staged-only")
        assert after - before <= {UNLOAD_TOMBSTONE_NAME}, (
            "PRESERVATION REGRESSION (3.5): unload added unexpected "
            "files {} to a staged repo".format(after - before))
        assert before <= after, (
            "PRESERVATION REGRESSION (3.5): unload REMOVED staged repo "
            "files {}".format(before - after))

    def test_unload_of_ready_model_frees_engine_and_returns_true(
            self, model_dir):
        """# Validates: Requirements 3.5

        READY → unload: True, engine shutdown exactly once, the model
        falls back to the disk-derived state (the repo is still
        staged), and the repo tree gains at most the tombstone marker.
        """
        _stage_repository(model_dir, "ready-model")
        engine = _FakeEngine()
        manager = VllmRuntimeManager(
            model_dir=model_dir, engine_factory=lambda args: engine)
        status = asyncio.run(manager.load("ready-model"))
        assert status.state is ModelState.READY
        before = _repo_files(model_dir, "ready-model")

        assert manager.unload("ready-model") is True
        assert engine.shutdown_calls == 1

        after = _repo_files(model_dir, "ready-model")
        assert after - before <= {UNLOAD_TOMBSTONE_NAME}
        assert before <= after
        # Untracked again; the still-staged repo drives the state answer.
        # Unfixed: STAGED. (The fixed tree reports the tombstoned repo as
        # UNLOADED — a REPORTING-only change task 3.2 owns; the identity
        # pinned HERE is return value + engine freeing + disk safety,
        # which must hold on both trees.)
        assert manager.state("ready-model").state in (
            ModelState.STAGED, getattr(ModelState, "UNLOADED", None)), (
            "unload must leave a still-staged repo in a disk-derived "
            "state, got {}".format(manager.state("ready-model")))

    def test_unload_of_failed_model_returns_true_and_retains_no_entry(
            self, model_dir):
        """# Validates: Requirements 3.5, 3.2

        FAILED (engine-construction failure, no engine object) →
        unload: True (the manager tracked it), no engine shutdown to
        perform, entry gone afterwards.
        """
        _stage_repository(model_dir, "failed-model")

        def _raise(args):
            raise RuntimeError("injected constructor failure")

        manager = VllmRuntimeManager(
            model_dir=model_dir, engine_factory=_raise)
        status = asyncio.run(manager.load("failed-model"))
        assert status == ModelStatus(
            ModelState.FAILED, reason="injected constructor failure")

        assert manager.unload("failed-model") is True
        # Second unload: no longer tracked.
        assert manager.unload("failed-model") is False

    def test_unload_is_idempotent_for_ready_models(self, model_dir):
        """# Validates: Requirements 3.5"""
        _stage_repository(model_dir, "twice")
        engine = _FakeEngine()
        manager = VllmRuntimeManager(
            model_dir=model_dir, engine_factory=lambda args: engine)
        asyncio.run(manager.load("twice"))
        assert manager.unload("twice") is True
        assert manager.unload("twice") is False
        assert engine.shutdown_calls == 1


# ---------------------------------------------------------------------------
# 3. vLLM-free inertness (Requirement 3.6)
# ---------------------------------------------------------------------------


def _load_start_vllm_runtime_with_flag_false():
    """Compile the REAL ``start_vllm_runtime`` function out of
    src/backend/app.py (no app import — app.py's module scope needs the
    device container) and bind it to a namespace where
    ``VLLM_AVAILABLE`` is False. Behaviorally identical to a vLLM-free
    image's call."""
    tree = ast.parse(_APP_PATH.read_text())
    function_node = next(
        (node for node in tree.body
         if isinstance(node, ast.FunctionDef)
         and node.name == "start_vllm_runtime"),
        None)
    assert function_node is not None, (
        "start_vllm_runtime not found at app.py module scope")
    module = ast.Module(body=[function_node], type_ignores=[])
    namespace = {
        "VLLM_AVAILABLE": False,
        "logger": logging.getLogger("test-inertness"),
        "traceback": _traceback_module,
        # Referenced only inside the try block, which the gate skips;
        # provided so a future body change cannot NameError instead of
        # honestly failing an assertion.
        "text_generation": SimpleNamespace(set_runtime=lambda m: None),
        "health": SimpleNamespace(set_vllm_server=lambda s: None),
    }
    exec(compile(module, str(_APP_PATH), "exec"), namespace)  # nosec B102
    return namespace["start_vllm_runtime"]


class TestVllmFreeInertness:

    def test_start_vllm_runtime_returns_none_without_reconciler_activity(
            self):
        """# Validates: Requirements 3.6

        With ``VLLM_AVAILABLE`` forced false, the real
        ``start_vllm_runtime`` body returns None, never imports
        ``vllm_runtime.reconciler``, and starts no ``vllm-reconciler``
        thread — the byte-identical pre-feature startup sequence for
        JP5-default/x86 images.
        """
        start_vllm_runtime = _load_start_vllm_runtime_with_flag_false()
        sys.modules.pop("vllm_runtime.reconciler", None)

        result = start_vllm_runtime()

        assert result is None, (
            "PRESERVATION REGRESSION (3.6): start_vllm_runtime must "
            "return None on vLLM-free images, got {!r}".format(result))
        assert "vllm_runtime.reconciler" not in sys.modules, (
            "PRESERVATION REGRESSION (3.6): the reconciler module was "
            "imported on a vLLM-free startup")
        reconciler_threads = [
            thread for thread in threading.enumerate()
            if thread.name == "vllm-reconciler"]
        assert reconciler_threads == [], (
            "PRESERVATION REGRESSION (3.6): a vllm-reconciler thread "
            "exists after a vLLM-free startup: {}".format(
                reconciler_threads))

    def test_reconciler_module_import_has_no_side_effects(self):
        """# Validates: Requirements 3.6, 3.2

        Skip-as-absent leg (binds at task 3.7): once
        ``vllm_runtime.reconciler`` exists, merely importing it must
        start no thread and issue no request — package convention: no
        module-scope side effects, no ``vllm`` import.
        """
        if importlib.util.find_spec("vllm_runtime.reconciler") is None:
            pytest.skip(
                "vllm_runtime.reconciler absent on the unfixed tree "
                "(created by task 3.1); this leg binds at task 3.7")
        threads_before = set(threading.enumerate())
        import vllm_runtime.reconciler  # noqa: F401
        new_threads = set(threading.enumerate()) - threads_before
        assert new_threads == set(), (
            "importing vllm_runtime.reconciler started threads: "
            "{}".format(new_threads))


# ---------------------------------------------------------------------------
# 4. Constants / bind identity (Requirement 3.8)
# ---------------------------------------------------------------------------


def _fresh_constants(env_value=None):
    """Load a FRESH copy of vllm_runtime/constants.py under a private
    module name with VLLM_RUNTIME_PORT optionally set — never touching
    the cached ``vllm_runtime.constants``."""
    saved = os.environ.pop("VLLM_RUNTIME_PORT", None)
    if env_value is not None:
        os.environ["VLLM_RUNTIME_PORT"] = env_value
    try:
        spec = importlib.util.spec_from_file_location(
            "_preservation_fresh_vllm_constants", _CONSTANTS_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.pop("VLLM_RUNTIME_PORT", None)
        if saved is not None:
            os.environ["VLLM_RUNTIME_PORT"] = saved


class TestConstantsAndBindIdentity:

    def test_constants_values_pinned(self):
        """# Validates: Requirements 3.8"""
        assert vllm_constants.VLLM_RUNTIME_HOST == "127.0.0.1"
        assert vllm_constants.DEFAULT_VLLM_RUNTIME_PORT == 8901
        assert vllm_constants.VLLM_MODEL_DIR == \
            "/aws_dda/dda_triton/vllm_model_repo"
        assert vllm_constants.VLLM_BACKEND_NAME == "vllm"

    def test_port_env_override_semantics_pinned(self):
        """# Validates: Requirements 3.8

        Unset → 8901; parseable override → the override; unparseable →
        the default (the observed try/except ValueError semantics).
        """
        assert _fresh_constants(None).VLLM_RUNTIME_PORT == 8901
        assert _fresh_constants("9155").VLLM_RUNTIME_PORT == 9155
        assert _fresh_constants("not-a-port").VLLM_RUNTIME_PORT == 8901

    def test_runtime_server_bind_arguments_pinned(self, model_dir):
        """# Validates: Requirements 3.8

        ``VllmRuntimeServer`` binds VLLM_RUNTIME_HOST (never
        configurable) and defaults the port to VLLM_RUNTIME_PORT with an
        explicit ``port=`` honored — pinned WITHOUT starting a server.
        """
        from vllm_runtime.server import VllmRuntimeServer

        manager = VllmRuntimeManager(model_dir=model_dir)
        server = VllmRuntimeServer(manager)
        assert server.host == vllm_constants.VLLM_RUNTIME_HOST
        assert server.port == vllm_constants.VLLM_RUNTIME_PORT

        explicit = VllmRuntimeServer(manager, port=8977)
        assert explicit.host == "127.0.0.1"
        assert explicit.port == 8977


# ---------------------------------------------------------------------------
# 5. Status-mapping dict-subset pins (Requirement 3.4)
# ---------------------------------------------------------------------------


class TestStatusMappingSubsets:

    def test_vllm_status_map_existing_entries_byte_identical(self):
        """# Validates: Requirements 3.4

        The fix's _VLLM_STATUS_MAP change must be a PURE ADDITION
        (UNLOADED → "STOPPED"): every observed entry stays
        byte-identical. Dict-subset assertion by design.
        """
        feature_utils = _import_with_awsiot_stubs(
            "utils.feature_configs_utils")
        status_map = feature_utils._VLLM_STATUS_MAP
        assert BASELINE_VLLM_STATUS_MAP.items() <= status_map.items(), (
            "PRESERVATION REGRESSION (3.4): existing _VLLM_STATUS_MAP "
            "entries changed: {!r} (baseline {!r})".format(
                status_map, BASELINE_VLLM_STATUS_MAP))

    def test_state_category_existing_entries_byte_identical(self):
        """# Validates: Requirements 3.4

        Same dict-subset discipline for the Text_Generation_API's
        _STATE_CATEGORY (UNLOADED is the only permissible addition).
        """
        from endpoints.text_generation import _STATE_CATEGORY

        assert BASELINE_STATE_CATEGORY.items() <= _STATE_CATEGORY.items(), (
            "PRESERVATION REGRESSION (3.4): existing _STATE_CATEGORY "
            "entries changed: {!r} (baseline {!r})".format(
                _STATE_CATEGORY, BASELINE_STATE_CATEGORY))

    def test_get_features_vllm_without_manager_is_empty(self):
        """# Validates: Requirements 3.4, 3.6

        No installed manager (vLLM-free images) → the feature-config
        merge contributes nothing, identical to pre-feature.
        """
        feature_utils = _import_with_awsiot_stubs(
            "utils.feature_configs_utils")
        saved = feature_utils.get_vllm_manager()
        feature_utils.set_vllm_manager(None)
        try:
            assert feature_utils.get_features_vllm() == []
        finally:
            feature_utils.set_vllm_manager(saved)
