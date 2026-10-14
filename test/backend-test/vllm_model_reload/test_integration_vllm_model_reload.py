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
"""Device-leg integration pass (task 4.7) for
vllm-model-reload-after-backend-restart.

One full pass through REAL components, end to end (design Integration
Tests):

- the temp tree is staged by the REAL ``stage_repository()`` from
  ``dda_triton.vllm_model_prep`` (the component Startup's atomic
  staging — not a hand-rolled copy);
- a real ``VllmRuntimeManager`` (fake ``engine_factory`` — the manager's
  public injectable seam);
- a real ``VllmRuntimeServer`` on an ephemeral loopback port;
- a real ``VllmReconciler`` running its ACTUAL HTTP path: the harness
  (fakes.maybe_start_reconciler) injects only ``port`` and a fast
  ``backoff`` — NO ``request_fn`` — so every reconciler load is a real
  ``requests.post`` against the real server's loopback model-control
  endpoint (the Decision 1 invariant, exercised over real sockets here;
  the other 4.x suites use the in-process ``request_fn`` seam);
- the backend restart is simulated by tearing all three down and
  rebuilding them over the SURVIVING directory tree;
- generate (``POST /v2/models/{m}/generate``) and
  ``get_features_vllm()`` are asserted end to end in the restarted
  life.

Honesty guard (design Testing Strategy): on-hardware Session A (task
11) is the REAL integration tier — a genuine ``AsyncLLMEngine``
reconstructing on the post-restart GPU, real docker/container restarts,
real Greengrass lifecycles, and shadow/IPC status propagation are ONLY
provable there. This host-side pass simulates the engine through the
manager's injectable ``engine_factory``, models the restart as object
teardown + reconstruction over the surviving tree, and stops at
``get_features_vllm()``; it proves the ORCHESTRATION wiring (staging →
server → reconciler HTTP re-drive → generate → feature-config), not
CUDA or container lifecycle timing.

Run host-side (portal venv, from the repo root):
    source /home/ubuntu/.venvs/dda-portal-tests/bin/activate
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
        test/backend-test/vllm_model_reload/test_integration_vllm_model_reload.py \
        -q -p no:cacheprovider --noconftest

**Validates: Requirements 2.1, 2.2, 2.3**
"""
import json
import time
from pathlib import Path

import requests

# The REAL component-Startup staging function. The module configures
# logging at import time; importing it directly at module scope is the
# established convention of the pinned suites (deploy_reliability,
# vllm_hf_cache, dda_triton) — mirrored here and in the 4.4 suite.
import dda_triton.vllm_model_prep as prep
from vllm_runtime.manager import ModelState
from vllm_model_reload.fakes import (
    DEFAULT_MODEL_NAME,
    GENERATED_TEXT,
    POLL_INTERVAL_SECONDS,
    WAIT_BUDGET_SECONDS,
    import_with_awsiot_stubs,
    restarted_backend,
)

MODEL_NAME = DEFAULT_MODEL_NAME

#: Load-request timeout for the component-Startup-shaped POST. The fake
#: engine constructs instantly; this only guards against a hang.
LOAD_POST_TIMEOUT_SECONDS = 30


def _build_source_repo(root: Path, model_name: str = MODEL_NAME) -> Path:
    """The model component's UNARCHIVED SOURCE directory — the valid
    Triton_vLLM_Repository layout ``stage_repository()`` copies:
    ``config.pbtxt`` declaring ``backend: "vllm"`` + ``1/model.json``."""
    source = root / "source" / model_name
    (source / "1").mkdir(parents=True, exist_ok=True)
    (source / "config.pbtxt").write_text('backend: "vllm"\n')
    (source / "1" / "model.json").write_text(json.dumps({"model": model_name}))
    return source


def _generate_until_served(base_url: str, model_name: str = MODEL_NAME,
                           budget_seconds: float = WAIT_BUDGET_SECONDS):
    """Poll the real generate endpoint until it serves (riding through
    the reload window's 409s, requirement 2.2) or the budget is
    exhausted. Returns ``(served, attempts, saw_409, last_status,
    last_body)``."""
    url = "{}/v2/models/{}/generate".format(base_url, model_name)
    deadline = time.monotonic() + budget_seconds
    attempts = 0
    saw_409 = False
    last_status = None
    last_body = None
    while time.monotonic() < deadline:
        response = requests.post(url, json={"text_input": "hello"}, timeout=5)
        attempts += 1
        last_status = response.status_code
        last_body = response.text
        if response.status_code == 200:
            assert response.json()["text_output"] == GENERATED_TEXT
            return True, attempts, saw_409, last_status, last_body
        if response.status_code == 409:
            saw_409 = True
        time.sleep(POLL_INTERVAL_SECONDS)
    return False, attempts, saw_409, last_status, last_body


def test_device_leg_full_pass_restart_reload_generate_and_status(tmp_path):
    """The full device-leg integration pass.

    Life 1 (fresh deploy): the tree is staged by the REAL
    ``stage_repository()``; the backend stack comes up (real manager +
    real server + real reconciler on its actual HTTP path); the
    component-Startup-shaped load POST drives the model to READY with
    exactly ONE engine construction even though the reconciler also ran
    (the 3.1 single-load discipline, here over real sockets); generate
    serves. Then the whole stack is torn down — the backend process
    "dies" and only the directory tree survives.

    Life 2 (the restart, requirement 2.1): a rebuilt stack over the
    surviving tree. NOTHING external re-issues the load — the
    reconciler's real HTTP re-drive is the only driver. Generate polls
    from the instant the stack is up and rides any 409 reload window
    through to a served response (2.2); the model reaches READY with
    exactly ONE engine construction in this life; and
    ``get_features_vllm()`` truthfully reports READY end to end (2.3).

    **Validates: Requirements 2.1, 2.2, 2.3**
    """
    source = _build_source_repo(tmp_path)
    model_dir = tmp_path / "models"

    # The REAL component-Startup staging (atomic temp-sibling copy +
    # os.rename), not a hand-rolled tree.
    staged = prep.stage_repository(
        str(source), MODEL_NAME, rewritten_engine_args=None,
        model_repo_dir=str(model_dir))
    assert Path(staged) == model_dir / MODEL_NAME

    feature_utils = import_with_awsiot_stubs("utils.feature_configs_utils")

    # ------------------------------------------------------------------
    # Life 1 — fresh deploy: Startup POST + reconciler, ONE construction
    # ------------------------------------------------------------------
    with restarted_backend(model_dir) as backend:
        assert backend.reconciler is not None, (
            "integration precondition failed: the fixed tree must provide "
            "vllm_runtime.reconciler (fakes.maybe_start_reconciler returned "
            "None)")
        # The component-Startup-shaped load request (the prep
        # request_load shape: a bare POST on the loopback model-control
        # endpoint). The reconciler races it; the manager's idempotency
        # on the server's single event loop keeps construction unique.
        response = requests.post(
            "{}/v2/repository/models/{}/load".format(
                backend.base_url, MODEL_NAME),
            timeout=LOAD_POST_TIMEOUT_SECONDS)
        assert response.status_code == 200, (
            "life-1 component-Startup load POST failed: HTTP {} ({})".format(
                response.status_code, response.text))
        status = backend.wait_for_terminal_state(MODEL_NAME)
        assert status.state is ModelState.READY, (
            "life-1 load did not reach READY (got {}: {})".format(
                status.state.value, status.reason))
        assert backend.factory.call_count == 1, (
            "life-1 fresh deploy must construct the engine exactly ONCE "
            "(Startup POST racing the reconciler) — got {} constructions".format(
                backend.factory.call_count))
        served, attempts, _, last_status, last_body = _generate_until_served(
            backend.base_url)
        assert served, (
            "life-1 generate never served ({} attempts, last HTTP {}: "
            "{})".format(attempts, last_status, last_body))
    # The stack is gone — the backend process died. The staged tree
    # (and nothing else) survives, exactly what a container restart
    # leaves behind.

    # ------------------------------------------------------------------
    # Life 2 — the restart: the reconciler's REAL HTTP path re-drives
    # ------------------------------------------------------------------
    with restarted_backend(model_dir) as backend:
        # Generate polls from the instant the stack is up: it rides the
        # reload window (409 with the loading category while the
        # reconciler's re-driven load is in flight) through to a served
        # response — the workflow-binding ride-through shape (2.2).
        served, attempts, saw_409, last_status, last_body = \
            _generate_until_served(backend.base_url)
        assert served, (
            "counterexample (2.1/2.2): after the backend restart generate "
            "never served within the {}s budget — {} attempts, last HTTP {} "
            "({}); engine_factory calls: {} — the reconciler's HTTP re-drive "
            "did not happen".format(
                WAIT_BUDGET_SECONDS, attempts, last_status, last_body,
                backend.factory.call_count))

        # The re-driven load reached READY with exactly ONE engine
        # construction, and the ONLY driver in this life was the
        # reconciler over its real loopback HTTP path (the test issued
        # no load request; the harness injected no request_fn).
        status = backend.manager.state(MODEL_NAME)
        assert status.state is ModelState.READY, (
            "post-restart state is {} ({}) though generate served".format(
                status.state.value, status.reason))
        assert backend.factory.call_count == 1, (
            "the restarted life must construct the engine exactly ONCE "
            "(the reconciler's bounded sequential re-drive) — got {}".format(
                backend.factory.call_count))

        # Feature-config surface, end to end (2.3): the restarted
        # backend truthfully reports READY — never the incident's
        # eternal LOADING.
        feature_utils.set_vllm_manager(backend.manager)
        try:
            entries = {entry.modelName: entry.status
                       for entry in feature_utils.get_features_vllm()}
        finally:
            feature_utils.set_vllm_manager(None)
        assert entries.get(MODEL_NAME) == "READY", (
            "counterexample (2.3): get_features_vllm() reports {!r} for "
            "the reloaded model (expected READY) — entries: {}".format(
                entries.get(MODEL_NAME), entries))
