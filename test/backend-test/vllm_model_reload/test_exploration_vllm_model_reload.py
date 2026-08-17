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
"""Bug-condition exploration suite (task 1, device leg) for
vllm-model-reload-after-backend-restart.

**Property 1: Bug Condition — A Backend Restart Never Silently Orphans a
Staged vLLM Model.**

Every case asserts the FIXED expected behavior, so on the UNFIXED tree
all four are EXPECTED TO FAIL — each failure is the counterexample
proving one defect leg of the jetson-thor1 2026-08-16 incident
(qwen3-vl-8b-instruct READY at 21:52:32Z, five backend kills later the
22:33:11Z backend healthy with ``gpuActiveModels: 0, models: {}``,
"LOADING" forever on the feature-config API, every generate 409 forever):

- Case 1 (defect 1.1) — the orphaned-model core: after the backend
  restart, a load must be re-driven for the still-staged model. Unfixed:
  the fresh manager holds zero models, ZERO engine-factory calls, state
  stays STAGED for the whole budget.
- Case 2 (defect 1.3) — 409 forever: a generate request against the
  staged model must eventually serve. Unfixed: 409 with no load in
  flight on every request (``_ready_engine`` performs no lazy load).
- Case 3 (defect 1.4) — eternal LOADING: ``get_features_vllm()`` must
  not report "LOADING" indefinitely while no load is in flight. Unfixed:
  "LOADING" on every read, forever (the broken ``_VLLM_STATUS_MAP``
  STAGED→LOADING assumption).
- Case 4 (defect 1.1, structural) — ``vllm_runtime.reconciler`` imports
  and ``app.py::start_vllm_runtime`` wires it. Unfixed: module absent.

The SAME suite validates the fix when it passes after implementation
(task 3.7).

Honesty guard: GPU-free and host-runnable. The engine is a recording
fake injected through the manager's public ``engine_factory`` seam; the
staged repo lives in ``tmp_path``; the "backend restart" is object
reconstruction over the surviving tree with a REAL ``VllmRuntimeServer``
on an ephemeral loopback port (see fakes.py). No real engine, container,
Greengrass, or account is touched.

Run host-side (portal venv, from the repo root):
    source /home/ubuntu/.venvs/dda-portal-tests/bin/activate
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
        test/backend-test/vllm_model_reload -q -p no:cacheprovider --noconftest

**Validates: Requirements 1.1, 1.3, 1.4**
"""
import ast
import importlib
import time
from pathlib import Path

import pytest
import requests

from vllm_runtime.manager import ModelState
from vllm_model_reload.fakes import (
    DEFAULT_MODEL_NAME,
    GENERATED_TEXT,
    POLL_INTERVAL_SECONDS,
    TERMINAL_STATES,
    WAIT_BUDGET_SECONDS,
    build_staged_repo,
    first_life_load,
    import_with_awsiot_stubs,
    restarted_backend,
)

MODEL_NAME = DEFAULT_MODEL_NAME


# ---------------------------------------------------------------------------
# Case 1 — the orphaned-model core (defect 1.1)
# ---------------------------------------------------------------------------

def test_case_1_backend_restart_redrives_load_to_ready(tmp_path):
    """After a backend restart over a surviving staged repository, a load
    is re-driven for the staged, desired model and it reaches READY.

    Counterexample on the unfixed tree: the fresh manager's tracked model
    table is empty, the engine factory is NEVER called, and the state
    stays STAGED for the whole budget — the ``gpuActiveModels: 0,
    models: {}`` fingerprint of the 22:33:11Z backend.

    **Validates: Requirements 1.1**
    """
    build_staged_repo(tmp_path, MODEL_NAME)
    first_life_load(tmp_path, MODEL_NAME)  # READY once, then the process dies

    with restarted_backend(tmp_path) as backend:
        status = backend.wait_for_terminal_state(MODEL_NAME)
        assert backend.factory.call_count >= 1 and status.state is ModelState.READY, (
            "counterexample (defect 1.1, the orphaned-model core): after the "
            "backend restart NO load was re-issued for the still-staged model "
            "'{}' — engine_factory calls in the restarted backend: {}, state "
            "after {}s budget: {}, list_models(): {} — the fresh "
            "VllmRuntimeManager starts with an empty model table and nothing "
            "ever scans VLLM_MODEL_DIR and re-issues the load (jetson-thor1 "
            "22:33:11Z: healthy backend, gpuActiveModels: 0, models: {{}})".format(
                MODEL_NAME,
                backend.factory.call_count,
                WAIT_BUDGET_SECONDS,
                status.state.value,
                {name: st.state.value
                 for name, st in sorted(backend.manager.list_models().items())},
            )
        )


# ---------------------------------------------------------------------------
# Case 2 — 409 forever (defect 1.3)
# ---------------------------------------------------------------------------

def test_case_2_generate_eventually_serves_after_restart(tmp_path):
    """A generate request naming the staged model after the backend
    restart eventually serves (riding through the reload window's 409s).

    Counterexample on the unfixed tree: HTTP 409 with the model's
    non-READY state on EVERY request, with no load in flight —
    ``_ready_engine`` performs no lazy load, so the 409s never end (the
    workflow LLM binding exhausts its 240 s poll budget and fails
    terminally).

    **Validates: Requirements 1.3**
    """
    build_staged_repo(tmp_path, MODEL_NAME)
    first_life_load(tmp_path, MODEL_NAME)

    with restarted_backend(tmp_path) as backend:
        url = "{}/v2/models/{}/generate".format(backend.base_url, MODEL_NAME)
        deadline = time.monotonic() + WAIT_BUDGET_SECONDS
        attempts = 0
        last_status = None
        last_body = None
        while time.monotonic() < deadline:
            response = requests.post(
                url, json={"text_input": "hello"}, timeout=5
            )
            attempts += 1
            last_status = response.status_code
            last_body = response.text
            if response.status_code == 200:
                assert response.json()["text_output"] == GENERATED_TEXT
                return  # served — the fixed expectation
            time.sleep(POLL_INTERVAL_SECONDS)
        pytest.fail(
            "counterexample (defect 1.3, 409 forever): generate against the "
            "staged model '{}' never served within the {}s budget — {} "
            "attempts, every one answered HTTP {} (last body: {}), while NO "
            "load was in flight (engine_factory calls: {}) — _ready_engine "
            "raises for any non-READY model and no lazy load exists".format(
                MODEL_NAME,
                WAIT_BUDGET_SECONDS,
                attempts,
                last_status,
                last_body,
                backend.factory.call_count,
            )
        )


# ---------------------------------------------------------------------------
# Case 3 — eternal LOADING (defect 1.4)
# ---------------------------------------------------------------------------

def test_case_3_feature_status_does_not_report_loading_forever(tmp_path):
    """The feature-config status of the staged model after a backend
    restart reaches READY (or FAILED with the retained reason) within the
    harness budget — never "LOADING" indefinitely with no load in flight.

    Counterexample on the unfixed tree: ``get_features_vllm()`` reports
    "LOADING" on every read, forever — ``_VLLM_STATUS_MAP`` maps
    STAGED→LOADING on the assumption that a staged model's load request
    "is on the way", exactly the assumption the restart breaks.

    **Validates: Requirements 1.4**
    """
    feature_utils = import_with_awsiot_stubs("utils.feature_configs_utils")

    build_staged_repo(tmp_path, MODEL_NAME)
    first_life_load(tmp_path, MODEL_NAME)

    with restarted_backend(tmp_path) as backend:
        feature_utils.set_vllm_manager(backend.manager)
        try:
            observed = []
            deadline = time.monotonic() + WAIT_BUDGET_SECONDS
            reads = 0
            while time.monotonic() < deadline:
                entries = {
                    entry.modelName: entry.status
                    for entry in feature_utils.get_features_vllm()
                }
                reads += 1
                reported = entries.get(MODEL_NAME)
                observed.append(reported)
                if reported in ("READY", "FAILED"):
                    break
                time.sleep(POLL_INTERVAL_SECONDS)
            assert observed and observed[0] is not None, (
                "harness precondition failed: the staged model '{}' is not "
                "reported by get_features_vllm() at all (entries: {})".format(
                    MODEL_NAME, observed)
            )
            assert observed[-1] in ("READY", "FAILED"), (
                "counterexample (defect 1.4, eternal LOADING): "
                "get_features_vllm() reported {} for the staged model '{}' on "
                "every one of {} reads across the {}s budget while NO load was "
                "in flight (engine_factory calls: {}) — the _VLLM_STATUS_MAP "
                "STAGED→LOADING mapping presents a permanently-loading-but-"
                "healthy model through the feature-config API and shadow "
                "sync".format(
                    sorted(set(observed), key=str),
                    MODEL_NAME,
                    reads,
                    WAIT_BUDGET_SECONDS,
                    backend.factory.call_count,
                )
            )
        finally:
            feature_utils.set_vllm_manager(None)


# ---------------------------------------------------------------------------
# Case 4 — no reconciliation module exists (structural)
# ---------------------------------------------------------------------------

def _start_vllm_runtime_source() -> str:
    """The source segment of ``app.py::start_vllm_runtime`` (structural
    check — app.py is never imported, its import graph needs the device
    container)."""
    import vllm_runtime

    app_py = Path(vllm_runtime.__file__).resolve().parent.parent / "app.py"
    source = app_py.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "start_vllm_runtime":
            return ast.get_source_segment(source, node) or ""
    pytest.fail(
        "harness precondition failed: start_vllm_runtime not found in {}".format(
            app_py)
    )


def test_case_4_reconciler_module_exists_and_is_wired():
    """``vllm_runtime.reconciler`` imports, and ``start_vllm_runtime``
    (the only place the vLLM runtime is wired on backend start) references
    the reconciler.

    Counterexample on the unfixed tree: the module does not exist — the
    vLLM load is a one-shot component-Startup action and NOTHING in the
    codebase re-issues it after a backend restart.

    **Validates: Requirements 1.1**
    """
    try:
        importlib.import_module("vllm_runtime.reconciler")
    except ImportError as err:
        pytest.fail(
            "counterexample (defect 1.1, structural): no reconciliation "
            "module exists — importing vllm_runtime.reconciler failed ({}); "
            "no component, script, or backend code ever re-asserts a vLLM "
            "load after Startup exits 0".format(err)
        )
    wiring = _start_vllm_runtime_source()
    assert "reconciler" in wiring.lower(), (
        "counterexample (defect 1.1, structural): vllm_runtime.reconciler "
        "exists but app.py::start_vllm_runtime does not wire it — the "
        "restarted backend still never re-drives staged loads; "
        "start_vllm_runtime source:\n{}".format(wiring)
    )
