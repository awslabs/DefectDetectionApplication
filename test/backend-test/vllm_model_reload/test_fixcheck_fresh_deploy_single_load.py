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
"""Fix-check: fresh-deploy single-load (task 4.5, design fix-check case 5)
for vllm-model-reload-after-backend-restart.

**Design claim under test (Decision 1, Requirement 3.1 interaction):** a
Greengrass fresh deployment recreates the backend container AND restarts
the model component, so the component Startup requests the load AND the
reconciler sees the staged repository — two load drivers for the SAME
model. The fix's claim: exactly ONE engine construction, by construction —
(a) every load arrives via the loopback HTTP endpoint, so both load
coroutines land on the runtime server's single uvicorn event loop where
the manager's load body runs WITHOUT an await point between entry
creation, ``parse_repository``, the LOADING transition, and the
(synchronous) engine construction — full serialization; (b)
``manager.load()`` is idempotent for LOADING/READY entries; (c) the
reconciler additionally skips names that are LOADING/READY at snapshot or
re-check time.

Both drivers here are REAL and executable:

- the component-Startup load is an HTTP POST with the prep
  ``request_load`` path shape — a bare ``requests.post`` against
  ``/v2/repository/models/{m}/load`` with a load-class timeout, exactly
  what ``dda_triton/vllm_model_prep.py::request_load`` issues
  (``vllm_model_prep`` itself is deliberately NOT imported: it configures
  logging at import time, and its exit-code semantics are NOT re-tested
  here — the file is unmodified and hash-pinned by the preservation
  suite, and the ``deploy_reliability`` suites pin its classifications);
- the reconciler's load is the real ``VllmReconciler`` driving its own
  POST through the same loopback endpoint (its injectable ``request_fn``
  seam is only WRAPPED for observation — the wrapper delegates to
  ``requests.post`` unchanged).

The race is made real (not just hoped for) with a gate: the engine
factory blocks on a ``threading.Event`` while holding the server's event
loop, which pins the first load inside engine construction so the second
POST provably arrives while the first is mid-flight. Releasing the gate
lets the serialized loop drain both.

Honesty guard: GPU-free and host-runnable. The engine is a fake injected
through the manager's public ``engine_factory`` seam; the staged repo
lives in ``tmp_path``; the server is a REAL ``VllmRuntimeServer`` on an
ephemeral loopback port; the reconciler is the REAL ``VllmReconciler``.
No real engine, container, Greengrass, or account is touched — the real
Greengrass-lifecycle version of this claim is Session A step 1 (task 11).

Run host-side (portal venv, from the repo root):
    source /home/ubuntu/.venvs/dda-portal-tests/bin/activate
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
        test/backend-test/vllm_model_reload/test_fixcheck_fresh_deploy_single_load.py \
        -q -p no:cacheprovider --noconftest

**Validates: Requirements 3.1**
"""
import threading
import time
from typing import Any, Dict, List, Mapping

import requests

from vllm_runtime.manager import ModelState, VllmRuntimeManager
from vllm_runtime.reconciler import VllmReconciler
from vllm_runtime.server import VllmRuntimeServer
from vllm_model_reload.fakes import (
    DEFAULT_MODEL_NAME,
    FAST_TEST_BACKOFF,
    POLL_INTERVAL_SECONDS,
    FakeEngine,
    build_staged_repo,
    free_port,
)

MODEL_NAME = DEFAULT_MODEL_NAME

#: Budget for each polled precondition (factory entered, POST dispatched,
#: threads joined). Generous ceiling — passing runs spend a few poll
#: intervals, never the budget.
STEP_BUDGET_SECONDS = 5.0

#: Ceiling on the factory gate wait so a test bug can never wedge the
#: server's event loop past teardown.
GATE_TIMEOUT_SECONDS = 10.0

#: The component-Startup POST's timeout. The prep uses its load-class
#: LOAD_REQUEST_TIMEOUT_SECONDS (1500 s); the shape (bare POST with a
#: timeout kwarg) is what matters — the test budget stays small.
STARTUP_POST_TIMEOUT_SECONDS = 30


def _poll_until(predicate, budget_seconds: float = STEP_BUDGET_SECONDS) -> bool:
    """Short-interval poll until ``predicate()`` or the budget runs out."""
    deadline = time.monotonic() + budget_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return bool(predicate())


class GatedRecordingFactory:
    """Recording fake for the manager's injectable ``engine_factory``
    seam whose construction BLOCKS on a gate. Because the manager calls
    the factory synchronously on the runtime server's event loop, a
    closed gate pins the in-flight load inside engine construction and
    holds the loop — the window in which the racing second POST arrives.
    ``entered`` is set on call entry so the test can observe "the first
    load is now inside engine construction"."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.gate = threading.Event()
        self.entered = threading.Event()

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(self, engine_args: Mapping[str, Any]) -> FakeEngine:
        self.calls.append(dict(engine_args))
        self.entered.set()
        # Bounded: a test bug must never wedge the event loop forever.
        self.gate.wait(GATE_TIMEOUT_SECONDS)
        return FakeEngine(engine_args)


class RecordingPost:
    """Observation wrapper for the reconciler's injectable ``request_fn``
    seam: records each dispatched URL, then delegates to the REAL
    ``requests.post`` — the reconciler's HTTP path is exercised
    unchanged."""

    def __init__(self):
        self.urls: List[str] = []
        self._lock = threading.Lock()

    @property
    def load_posts(self) -> List[str]:
        return [url for url in self.urls if url.endswith("/load")]

    def __call__(self, url, **kwargs):
        with self._lock:
            self.urls.append(url)
        return requests.post(url, **kwargs)


class FreshDeployBackend:
    """The fresh-deploy backend: a manager over the just-staged tree
    (the model has NEVER been loaded — this is a first deployment, not a
    restart), the REAL ``VllmRuntimeServer`` on an ephemeral loopback
    port, and the REAL ``VllmReconciler`` (constructed by the test at the
    moment its interleaving requires)."""

    def __init__(self, model_dir):
        self.factory = GatedRecordingFactory()
        self.manager = VllmRuntimeManager(
            model_dir=model_dir,
            engine_factory=self.factory,
            sampling_params_factory=dict,
        )
        self.port = free_port()
        self.server = VllmRuntimeServer(self.manager, port=self.port)
        self.recording_post = RecordingPost()
        self.reconciler = VllmReconciler(
            self.manager,
            port=self.port,
            backoff=FAST_TEST_BACKOFF,
            request_fn=self.recording_post,
        )

    @property
    def load_url(self) -> str:
        return "http://127.0.0.1:{}/v2/repository/models/{}/load".format(
            self.port, MODEL_NAME
        )

    def startup_shaped_post(self, sink: Dict[str, Any]) -> threading.Thread:
        """Dispatch the component-Startup load on its own thread: the
        prep ``request_load`` path shape — a bare POST against the
        loopback model-control load endpoint with a load-class timeout
        (mirrors ``vllm_model_prep.request_load``'s
        ``requests.post(url, timeout=LOAD_REQUEST_TIMEOUT_SECONDS)``)."""

        def _post():
            try:
                sink["response"] = requests.post(
                    self.load_url, timeout=STARTUP_POST_TIMEOUT_SECONDS
                )
            except Exception as err:  # noqa: BLE001 - surfaced by the test
                sink["error"] = err

        thread = threading.Thread(target=_post, name="component-startup-post")
        thread.start()
        return thread

    def __enter__(self) -> "FreshDeployBackend":
        self.server.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Always release the gate first: a closed gate holds the server's
        # event loop and would stall the shutdown.
        self.factory.gate.set()
        self.server.stop()


def _assert_startup_response_ready(sink: Dict[str, Any]) -> None:
    assert "error" not in sink, (
        "component-Startup-shaped POST failed at the connection level: "
        "{!r}".format(sink.get("error"))
    )
    response = sink["response"]
    assert response.status_code == 200, (
        "component-Startup-shaped POST did not succeed: HTTP {} body {}".format(
            response.status_code, response.text
        )
    )
    assert response.json().get("state") == ModelState.READY.value, (
        "component-Startup-shaped POST returned 200 but not READY: {}".format(
            response.text
        )
    )


# ---------------------------------------------------------------------------
# The race: reconciler POST in flight, Startup POST arrives mid-construction
# ---------------------------------------------------------------------------

def test_startup_post_racing_reconciler_post_single_engine_construction(tmp_path):
    """Component-Startup-shaped POST racing the reconciler's POST for the
    same freshly staged model → exactly ONE engine construction.

    Deterministic interleaving: the reconciler wins the race and its load
    is pinned inside engine construction (gated factory holding the
    single event loop); the Startup POST is then dispatched so BOTH loads
    are provably in flight at once; releasing the gate drains the loop —
    the reconciler's load completes to READY, the Startup POST's load
    coroutine then runs, hits the manager's LOADING/READY idempotency
    short-circuit, and answers 200 READY WITHOUT a second construction
    (Decision 1: single-event-loop serialization + manager idempotency).

    **Validates: Requirements 3.1**
    """
    build_staged_repo(tmp_path, MODEL_NAME)  # fresh deploy: never loaded

    with FreshDeployBackend(tmp_path) as backend:
        # The reconciler sees the staged repo and re-drives the load
        # through its own loopback POST.
        backend.reconciler.start()
        assert _poll_until(lambda: len(backend.recording_post.load_posts) >= 1), (
            "harness precondition failed: the reconciler never dispatched "
            "a load POST for the staged model (recorded URLs: {})".format(
                backend.recording_post.urls
            )
        )
        assert _poll_until(backend.factory.entered.is_set), (
            "harness precondition failed: the reconciler's load never "
            "reached engine construction"
        )

        # The reconciler's load is now pinned INSIDE engine construction,
        # holding the server's single event loop. Dispatch the
        # component-Startup load: a second, concurrent POST for the SAME
        # model.
        sink: Dict[str, Any] = {}
        startup_thread = backend.startup_shaped_post(sink)
        # Short bounded window for the Startup POST to reach the server
        # socket while the loop is held (it cannot complete: completion
        # requires the loop the factory is blocking).
        _poll_until(lambda: False, budget_seconds=0.3)
        assert "response" not in sink and "error" not in sink, (
            "harness precondition failed: the Startup POST completed while "
            "the event loop was held inside engine construction — the race "
            "window was not established (sink: {!r})".format(sink)
        )

        backend.factory.gate.set()
        startup_thread.join(STEP_BUDGET_SECONDS)
        assert not startup_thread.is_alive(), (
            "component-Startup-shaped POST did not complete after the gate "
            "opened"
        )

        _assert_startup_response_ready(sink)
        assert backend.manager.state(MODEL_NAME).state is ModelState.READY
        assert backend.factory.call_count == 1, (
            "fix-check case 5 violated: the component-Startup POST racing "
            "the reconciler's POST produced {} engine constructions "
            "(engine args per call: {}) — Decision 1 promises exactly ONE "
            "(single-event-loop serialization + manager LOADING/READY "
            "idempotency)".format(
                backend.factory.call_count, backend.factory.calls
            )
        )
        # And the reconciler drove exactly one load POST — no retry storm
        # was triggered by the race.
        assert len(backend.recording_post.load_posts) == 1, (
            "the reconciler dispatched {} load POSTs during the race; "
            "expected exactly 1 (URLs: {})".format(
                len(backend.recording_post.load_posts),
                backend.recording_post.urls,
            )
        )


# ---------------------------------------------------------------------------
# The mirror interleaving: Startup wins, the reconciler skips
# ---------------------------------------------------------------------------

def test_reconciler_skips_model_when_component_startup_won_the_race(tmp_path):
    """When the component-Startup load is already in flight (LOADING) at
    reconciler-snapshot time, the reconciler issues NO load POST for the
    model at all — the snapshot excludes non-STAGED names and the
    pre-POST re-check skips LOADING/READY — so the fresh-deploy outcome
    is still exactly ONE engine construction.

    **Validates: Requirements 3.1**
    """
    build_staged_repo(tmp_path, MODEL_NAME)  # fresh deploy: never loaded

    with FreshDeployBackend(tmp_path) as backend:
        # The component Startup gets there first and its load is pinned
        # inside engine construction (state: LOADING).
        sink: Dict[str, Any] = {}
        startup_thread = backend.startup_shaped_post(sink)
        assert _poll_until(backend.factory.entered.is_set), (
            "harness precondition failed: the Startup POST's load never "
            "reached engine construction"
        )
        assert backend.manager.state(MODEL_NAME).state is ModelState.LOADING

        # NOW the reconciler runs its one-shot pass: the model is LOADING,
        # so the candidate snapshot excludes it and the pass finishes
        # without a single POST.
        reconciler_thread = backend.reconciler.start()
        reconciler_thread.join(STEP_BUDGET_SECONDS)
        assert not reconciler_thread.is_alive(), (
            "the reconciler pass did not finish within {}s while the model "
            "was LOADING".format(STEP_BUDGET_SECONDS)
        )
        assert backend.recording_post.urls == [], (
            "the reconciler dispatched requests for a model whose load the "
            "component Startup already had in flight: {} — the STAGED-only "
            "snapshot / LOADING-READY skip discipline is broken".format(
                backend.recording_post.urls
            )
        )

        backend.factory.gate.set()
        startup_thread.join(STEP_BUDGET_SECONDS)
        assert not startup_thread.is_alive(), (
            "component-Startup-shaped POST did not complete after the gate "
            "opened"
        )

        _assert_startup_response_ready(sink)
        assert backend.manager.state(MODEL_NAME).state is ModelState.READY
        assert backend.factory.call_count == 1, (
            "fix-check case 5 violated (Startup-wins interleaving): {} "
            "engine constructions; expected exactly ONE".format(
                backend.factory.call_count
            )
        )
