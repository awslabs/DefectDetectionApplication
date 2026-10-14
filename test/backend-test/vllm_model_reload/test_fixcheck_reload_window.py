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
"""Fix-check: reload-window 409 + workflow-binding ride-through (task 4.2,
design Testing Strategy fix-check case 2) for
vllm-model-reload-after-backend-restart.

Requirement 2.2: WHEN a generate request names a staged-but-not-yet-reloaded
vLLM model during the post-restart reconciliation window THEN the system
SHALL answer with the existing 409 state-info mapping reflecting a load
genuinely in progress (the workflow LLM binding's existing 409-loading poll
loop rides through the window instead of failing terminally).

Requirement 3.4: the READY-model generate path — request validation, the
409/422/502 mappings — CONTINUES to serve unchanged once the reload
completes; the SAME request path that answered 409 during the window
serves normally afterwards.

Two legs, both over a REAL ``VllmRuntimeServer`` on an ephemeral loopback
port with an event-controlled slow fake engine factory (the factory returns
an awaitable that waits on a ``threading.Event`` by polling with
``asyncio.sleep``, so the load stays genuinely in flight WITHOUT blocking
the runtime server's event loop — concurrent generate requests during the
window are answered):

1. **Reload-window 409, then serves** — generate against the runtime
   server while the reconciler's re-driven load is in flight → HTTP 409
   with the existing state-info body (``vllm_runtime/server.py``'s
   ``ModelUnavailableError`` mapping), whose ``state`` falls in the
   "loading" category of the REAL Text_Generation_API mapping
   (``endpoints.text_generation.state_category``: STAGED and LOADING both
   → "loading"). After the gate is released and the reload completes, the
   SAME URL serves 200 with the generated text.

2. **Workflow-binding ride-through** — the workflow LLM binding's poll
   loop (``workflow_engine.output_bindings._default_llm_invoker`` —
   ``output_bindings.py`` itself is UNTOUCHED; the poll interval/budget
   module constants are monkeypatched at runtime to a short test budget)
   is pointed at the REAL Text_Generation_API router served on its own
   ephemeral port over the same manager. Its first POST lands inside the
   reconciliation window and is answered 409 ``{"state": "loading"}``; a
   response middleware releases the engine gate on that first armed 409,
   the reconciler's load completes, and a subsequent poll of the SAME loop
   invocation gets 200 — the binding returns the generated text instead of
   exhausting its budget (the incident's terminal-failure path, defect
   1.3, now rides through).

Honesty guard: GPU-free and host-runnable. The engine is a fake injected
through the manager's public ``engine_factory`` seam; the staged repo
lives in ``tmp_path``; the "backend restart" is object reconstruction over
the surviving tree (see fakes.py). No real engine, container, Greengrass,
or account is touched; the real ~60 s reload ride-through is on-hardware
Session A (USER ACTION).

Run host-side (portal venv, from the repo root):
    source /home/ubuntu/.venvs/dda-portal-tests/bin/activate
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
        test/backend-test/vllm_model_reload/test_fixcheck_reload_window.py \
        -q -p no:cacheprovider --noconftest

**Validates: Requirements 2.2, 3.4**
"""
import asyncio
import threading
import time

import requests
import uvicorn
from fastapi import FastAPI

from endpoints import text_generation
from endpoints.text_generation import state_category
from utils.auth import authorize_request
from vllm_runtime.manager import ModelState, VllmRuntimeManager
from vllm_runtime.reconciler import VllmReconciler
from vllm_runtime.server import VllmRuntimeServer
from workflow_engine import output_bindings

from vllm_model_reload.fakes import (
    DEFAULT_MODEL_NAME,
    FAST_TEST_BACKOFF,
    FakeEngine,
    GENERATED_TEXT,
    POLL_INTERVAL_SECONDS,
    WAIT_BUDGET_SECONDS,
    build_staged_repo,
    first_life_load,
    free_port,
)

MODEL_NAME = DEFAULT_MODEL_NAME

#: How long the tests wait for the reconciler's re-driven load to enter
#: the engine factory (the window opening). The reconciler thread POSTs
#: immediately on start, so a passing run needs milliseconds.
WINDOW_OPEN_TIMEOUT_SECONDS = 10.0

#: Short test budget injected into the binding's poll loop (the real
#: LLM_LOADING_BUDGET_SEC is 240 s; the fake reload completes in
#: milliseconds once the gate is released).
BINDING_TEST_BUDGET_SECONDS = 15.0

#: Short poll interval injected into the binding's poll loop (the real
#: LLM_LOADING_POLL_INTERVAL_SEC is 5 s).
BINDING_TEST_POLL_INTERVAL_SECONDS = 0.05


class EventControlledSlowEngineFactory:
    """The task's event-controlled slow fake engine factory: engine
    construction blocks until :attr:`release` is set, keeping the model
    genuinely LOADING for as long as the test needs the reconciliation
    window open.

    The factory returns an AWAITABLE (the manager awaits it on the
    runtime server's event loop, per ``VllmRuntimeManager.load``'s
    ``inspect.isawaitable`` seam) that polls the threading.Event with
    ``asyncio.sleep`` — the event loop stays free, so concurrent
    generate requests during the window are answered with the 409
    state-info mapping instead of hanging.
    """

    def __init__(self):
        self.calls = []
        #: Set by the construction coroutine once the load is genuinely
        #: in flight (manager state LOADING) — the window is open.
        self.entered = threading.Event()
        #: Set by the test to let the construction complete — the
        #: window closes and the model goes READY.
        self.release = threading.Event()

    @property
    def call_count(self):
        return len(self.calls)

    def __call__(self, engine_args):
        self.calls.append(dict(engine_args))

        async def _construct():
            self.entered.set()
            while not self.release.is_set():
                await asyncio.sleep(0.01)
            return FakeEngine(engine_args)

        return _construct()


def _restarted_manager(model_dir, factory):
    """The restarted backend's fresh manager (empty model table) over the
    surviving staged tree, with the slow factory on the injectable
    ``engine_factory`` seam."""
    return VllmRuntimeManager(
        model_dir=model_dir,
        engine_factory=factory,
        sampling_params_factory=dict,
    )


def _wait_for_state(manager, model_name, states,
                    budget_seconds=WAIT_BUDGET_SECONDS):
    """Poll ``manager.state(model_name)`` until one of ``states`` or the
    budget is exhausted; return the last observed status."""
    deadline = time.monotonic() + budget_seconds
    status = manager.state(model_name)
    while status.state not in states and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        status = manager.state(model_name)
    return status


# ---------------------------------------------------------------------------
# Leg 1 — generate during the reconciliation window: 409 in the "loading"
# category, then the SAME path serves (Requirements 2.2, 3.4)
# ---------------------------------------------------------------------------

def test_generate_during_reload_window_gets_409_loading_then_serves(tmp_path):
    """During the post-restart reconciliation window a generate request
    is answered with the EXISTING 409 state-info mapping, and the
    reported state falls in the Text_Generation_API's "loading" category
    for BOTH edges of the window (STAGED before the reconciler's POST,
    LOADING while the re-driven load is in flight). After the reload
    completes, the SAME request path serves normally.

    **Validates: Requirements 2.2, 3.4**
    """
    build_staged_repo(tmp_path, MODEL_NAME)
    first_life_load(tmp_path, MODEL_NAME)  # READY once, then the process dies

    factory = EventControlledSlowEngineFactory()
    manager = _restarted_manager(tmp_path, factory)
    port = free_port()
    server = VllmRuntimeServer(manager, port=port)
    url = "http://127.0.0.1:{}/v2/models/{}/generate".format(port, MODEL_NAME)
    body = {"text_input": "hello"}
    try:
        server.start()

        # Window edge 1: restarted backend, reconciler not yet driving —
        # the staged model answers 409 with state STAGED, which the
        # existing Text_Generation_API mapping puts in the "loading"
        # category (STAGED is known to the device and on its way to
        # serving).
        response = requests.post(url, json=body, timeout=5)
        assert response.status_code == 409, (
            "expected 409 for the staged model before the reconciler's "
            "drive, got {}: {}".format(response.status_code, response.text)
        )
        payload = response.json()
        assert payload["name"] == MODEL_NAME
        assert payload["state"] == ModelState.STAGED.value
        assert "error" in payload
        assert state_category(payload["state"]) == "loading", (
            "the pre-drive window state {} must map to the 'loading' "
            "category (2.2)".format(payload["state"])
        )

        # Open the window proper: the reconciler re-drives the load
        # through the loopback endpoint; the slow factory holds the load
        # in flight.
        VllmReconciler(
            manager, port=port, backoff=FAST_TEST_BACKOFF
        ).start()
        assert factory.entered.wait(WINDOW_OPEN_TIMEOUT_SECONDS), (
            "harness precondition failed: the reconciler never drove the "
            "load into the engine factory within {}s".format(
                WINDOW_OPEN_TIMEOUT_SECONDS)
        )

        # Window edge 2: the load is genuinely in flight — 409 with
        # state LOADING, still the "loading" category (2.2). The request
        # is answered WHILE the load is in progress (the factory gate is
        # still closed), proving the event loop serves during the window.
        response = requests.post(url, json=body, timeout=5)
        assert not factory.release.is_set()
        assert response.status_code == 409, (
            "expected 409 during the in-flight reload, got {}: {}".format(
                response.status_code, response.text)
        )
        payload = response.json()
        assert payload["name"] == MODEL_NAME
        assert payload["state"] == ModelState.LOADING.value
        assert "error" in payload
        assert state_category(payload["state"]) == "loading", (
            "the in-flight window state {} must map to the 'loading' "
            "category (2.2)".format(payload["state"])
        )

        # Close the window: the reload completes and the model is READY.
        factory.release.set()
        status = _wait_for_state(
            manager, MODEL_NAME, (ModelState.READY, ModelState.FAILED)
        )
        assert status.state is ModelState.READY, (
            "the released reload did not reach READY (got {}: {})".format(
                status.state, status.reason)
        )

        # The SAME request path now serves normally (3.4): identical URL,
        # identical body, HTTP 200 with the generated text.
        response = requests.post(url, json=body, timeout=5)
        assert response.status_code == 200, (
            "the same generate path did not serve after the reload "
            "completed: {} {}".format(response.status_code, response.text)
        )
        assert response.json()["text_output"] == GENERATED_TEXT
        # Exactly the reconciler's one re-driven load constructed an
        # engine; the window's generate requests never triggered another.
        assert factory.call_count == 1
    finally:
        factory.release.set()  # never leave the reconciler thread gated
        server.stop()


# ---------------------------------------------------------------------------
# Leg 2 — the workflow LLM binding's poll loop rides through the window
# (Requirement 2.2; output_bindings.py UNTOUCHED)
# ---------------------------------------------------------------------------

class _TextGenApiServer:
    """The REAL Text_Generation_API router (``endpoints.text_generation``)
    served on an ephemeral loopback port over the restarted backend's
    manager — the surface the workflow LLM binding actually polls.

    ``get_runtime`` is dependency-overridden with the manager and
    ``authorize_request`` neutralized, exactly the established
    ``test/backend-test/text_generation`` harness convention. A response
    middleware records every generate status code and — once armed —
    releases the engine gate on the first 409, so the binding
    deterministically experiences at least one 409-loading poll before
    the reload completes.
    """

    def __init__(self, manager, release_gate):
        self.app = FastAPI()
        self.app.include_router(text_generation.router)
        self.app.dependency_overrides[text_generation.get_runtime] = (
            lambda: manager
        )
        self.app.dependency_overrides[authorize_request] = lambda: None
        self.statuses = []
        self.armed = threading.Event()
        self._release_gate = release_gate

        @self.app.middleware("http")
        async def _record(request, call_next):  # noqa: ANN001 - FastAPI seam
            response = await call_next(request)
            if request.url.path.endswith("/generate"):
                self.statuses.append(response.status_code)
                if response.status_code == 409 and self.armed.is_set():
                    # The binding's poll loop has seen the window: let
                    # the in-flight reload complete.
                    self._release_gate()
            return response

        self.port = free_port()
        self._server = None
        self._thread = None

    @property
    def generate_url_template(self):
        return (
            "http://127.0.0.1:{}/text-generation/{{model_name}}/generate"
            .format(self.port)
        )

    def start(self, startup_timeout_seconds=15.0):
        # The VllmRuntimeServer.start pattern: uvicorn on a daemon
        # thread, plain-asyncio loop, wait for the listener.
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            log_config=None,
            access_log=False,
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, name="text-gen-api-test", daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + startup_timeout_seconds
        while not self._server.started:
            if not self._thread.is_alive():
                raise RuntimeError(
                    "Text_Generation_API test server failed to start")
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "Text_Generation_API test server did not start within "
                    "{}s".format(startup_timeout_seconds))
            time.sleep(0.02)

    def stop(self, shutdown_timeout_seconds=10.0):
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(shutdown_timeout_seconds)
        self._server = None
        self._thread = None


def test_workflow_llm_binding_poll_loop_rides_through_reload_window(
    tmp_path, monkeypatch
):
    """The workflow LLM binding's 409-loading poll loop
    (``_default_llm_invoker`` — ``output_bindings.py`` UNTOUCHED) rides
    through the post-restart reconciliation window: its first POST is
    answered 409 ``{"state": "loading"}``, the reload completes while it
    polls, and the SAME loop invocation returns the generated text —
    instead of the incident's terminal budget exhaustion.

    Only the module's URL/interval/budget CONSTANTS are monkeypatched
    (pointing at the ephemeral server with a short test budget); the poll
    loop's code is exercised verbatim.

    **Validates: Requirements 2.2**
    """
    build_staged_repo(tmp_path, MODEL_NAME)
    first_life_load(tmp_path, MODEL_NAME)  # READY once, then the process dies

    factory = EventControlledSlowEngineFactory()
    manager = _restarted_manager(tmp_path, factory)
    runtime_port = free_port()
    runtime_server = VllmRuntimeServer(manager, port=runtime_port)
    api_server = _TextGenApiServer(manager, release_gate=factory.release.set)
    try:
        runtime_server.start()
        api_server.start()

        monkeypatch.setattr(
            output_bindings, "TEXT_GENERATION_URL",
            api_server.generate_url_template,
        )
        monkeypatch.setattr(
            output_bindings, "LLM_LOADING_POLL_INTERVAL_SEC",
            BINDING_TEST_POLL_INTERVAL_SECONDS,
        )
        monkeypatch.setattr(
            output_bindings, "LLM_LOADING_BUDGET_SEC",
            BINDING_TEST_BUDGET_SECONDS,
        )

        # Open the reconciliation window: the reconciler's re-driven load
        # is held in flight by the gated factory.
        VllmReconciler(
            manager, port=runtime_port, backoff=FAST_TEST_BACKOFF
        ).start()
        assert factory.entered.wait(WINDOW_OPEN_TIMEOUT_SECONDS), (
            "harness precondition failed: the reconciler never drove the "
            "load into the engine factory within {}s".format(
                WINDOW_OPEN_TIMEOUT_SECONDS)
        )

        # Unarmed probe: the Text_Generation_API surface the binding
        # polls answers 409 with the literal "loading" category during
        # the window (2.2) — the exact payload the poll loop keys off.
        probe = requests.post(
            api_server.generate_url_template.format(model_name=MODEL_NAME),
            json={"prompt": "probe"},
            timeout=5,
        )
        assert probe.status_code == 409, (
            "expected 409 from the Text_Generation_API during the window, "
            "got {}: {}".format(probe.status_code, probe.text)
        )
        assert probe.json()["state"] == "loading", (
            "the Text_Generation_API 409 body during the window must "
            "report state 'loading', got: {}".format(probe.text)
        )

        # Arm the ride-through: the binding's first 409 releases the
        # gate, so the SAME poll-loop invocation deterministically sees
        # the window AND its close.
        api_server.statuses.clear()
        api_server.armed.set()

        text = output_bindings._default_llm_invoker(
            MODEL_NAME, "Describe the part.", {"max_tokens": 16}
        )

        assert text == GENERATED_TEXT, (
            "the binding's poll loop did not return the generated text "
            "after riding through the reload window (got {!r})".format(text)
        )
        statuses = list(api_server.statuses)
        assert statuses and statuses[0] == 409, (
            "the binding's first POST should land inside the window "
            "(409); observed generate statuses: {}".format(statuses)
        )
        assert statuses[-1] == 200 and set(statuses) <= {409, 200}, (
            "the binding's poll loop should see only 409-loading polls "
            "followed by the winning 200; observed: {}".format(statuses)
        )
        # The window's polls never triggered another engine construction:
        # exactly the reconciler's one re-driven load.
        assert factory.call_count == 1
    finally:
        factory.release.set()  # never leave the reconciler thread gated
        api_server.stop()
        runtime_server.stop()
