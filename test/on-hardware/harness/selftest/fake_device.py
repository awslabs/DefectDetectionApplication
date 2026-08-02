"""Fake Target_Device: in-process FastAPI imitation of the Backend_API surface.

The Edge_Test_Harness is a pure HTTP client of the device, so its end-to-end
selftests exercise the *real* stages, conftest, client, and results plugin
against this fake served over real HTTP (design: Testing Strategy). The fake
imitates exactly the surface the harness touches:

* ``/system-health`` and ``/dda-component-status`` — health + device identity
  (LocalServer version) for the Results_Bundle;
* ``/feature-configurations`` and ``.../models/{name}/start|stop`` — model
  lifecycle with *scriptable* state transitions: a started
  :class:`FakeModel` walks LOADING (``loading_polls`` observations) into
  READY, or into FAILED carrying a device-reported reason
  (``defaultConfiguration.failureReason``) when ``fail_reason`` is set;
* ``/text-generation/*`` — canned non-streaming generate and ``data:``-framed
  SSE streaming (token events, then ``{"done": true}``);
* ``/workflows*`` — enumeration, scriptable run responses with output
  metadata (including ``llm`` node outcomes), captured images, capture-task;
* optional local-auth — when enabled, every non-``/local-auth`` endpoint
  demands the bearer token issued by ``POST /local-auth/login``.

Every ``start``/``stop``/``run_workflow``/``login`` the harness issues is
recorded in :attr:`FakeDevice.calls`, so selftests can assert restoration
semantics (stop only what the harness started — Reqs 4.3, 6.4, 8.3) from the
device's point of view. The server runs on a uvicorn background thread bound
to an ephemeral localhost port (:func:`serve`); the pytester-driven selftests
run the real harness in a subprocess pointed at that port while sharing this
process's state objects for scripting and post-run assertions.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

#: LocalServer version the fake reports; selftests assert it lands in the
#: Results_Bundle (Req 3.2 via the results-bundle selftest).
FAKE_LOCAL_SERVER_VERSION = "0.0.0-fake-device"

#: Bearer token ``POST /local-auth/login`` issues when auth is enabled.
FAKE_BEARER_TOKEN = "fake-device-bearer-token"

#: Feature-configurations entry ``type`` of vLLM models (mirrors
#: ``conftest.VLLM_FEATURE_TYPE`` on the harness side).
VLLM_FEATURE_TYPE = "VllmModel"

#: How long :func:`serve` waits for the uvicorn thread to come up.
STARTUP_TIMEOUT_S = 15.0


class FakeModel:
    """One feature-configurations entry with a scriptable lifecycle.

    The transition script plays out per *observation* (each
    ``GET /feature-configurations``), mirroring how a real device's state is
    only visible through polling: after :meth:`start`, the next
    ``loading_polls`` observations report ``LOADING``, then the terminal
    state — ``READY``, or ``FAILED`` (with ``fail_reason`` as the
    device-reported ``defaultConfiguration.failureReason``) when
    ``fail_reason`` is set.
    """

    def __init__(
        self,
        name: str,
        model_type: str = "TritonModel",
        status: str = "STOPPED",
        fail_reason: Optional[str] = None,
        loading_polls: int = 1,
    ):
        self.name = name
        self.model_type = model_type
        self.status = status
        self.fail_reason = fail_reason
        self.loading_polls = loading_polls
        self._pending: List[str] = []

    def start(self) -> None:
        """Arm the scripted transition: LOADING x loading_polls, then the
        terminal state (FAILED when ``fail_reason`` is set, else READY)."""
        terminal = "FAILED" if self.fail_reason else "READY"
        self._pending = ["LOADING"] * self.loading_polls + [terminal]

    def stop(self) -> None:
        self._pending = []
        self.status = "STOPPED"

    def observe(self) -> str:
        """The status one enumeration observes; consumes one scripted step."""
        if self._pending:
            self.status = self._pending.pop(0)
        return self.status

    def entry(self) -> Dict[str, Any]:
        """The feature-configurations entry for one enumeration."""
        status = self.observe()
        return {
            "modelName": self.name,
            "type": self.model_type,
            "status": status,
            "defaultConfiguration": {
                "failureReason": self.fail_reason if status == "FAILED" else None
            },
        }


class FakeDevice:
    """Scriptable state behind the fake Backend_API.

    Selftests configure models/workflows/auth before starting the server,
    then read :attr:`calls` (and model statuses) after the harness run to
    assert the device-observed behavior — most importantly which components
    the harness stopped during State_Restoration (Reqs 4.3, 6.4, 8.3).
    """

    def __init__(self, local_server_version: str = FAKE_LOCAL_SERVER_VERSION):
        self.local_server_version = local_server_version
        self.models: "Dict[str, FakeModel]" = {}
        self.workflows: List[Dict[str, Any]] = []
        self.workflow_run_responses: Dict[str, Dict[str, Any]] = {}
        self.workflow_images: Dict[str, Dict[str, Any]] = {}
        self.generated_text = "a canned completion from the fake device"
        self.stream_tokens = ["edge", " devices", " run", " models"]
        #: ``(username, password)`` enabling local-auth; ``None`` disables it.
        self.auth: Optional[Tuple[str, str]] = None
        #: Every mutating call the harness issued: ``(kind, name)`` tuples
        #: with kind in {"start", "stop", "run_workflow", "login"}.
        self.calls: List[Tuple[str, str]] = []
        self.lock = threading.Lock()

    # ------------------------------------------------------------------
    # Scripting surface for selftests
    # ------------------------------------------------------------------

    def add_model(self, name: str, **kwargs) -> FakeModel:
        model = FakeModel(name, **kwargs)
        self.models[name] = model
        return model

    def add_workflow(
        self,
        workflow: Dict[str, Any],
        run_response: Optional[Dict[str, Any]] = None,
        images: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.workflows.append(workflow)
        workflow_id = workflow["workflowId"]
        if run_response is not None:
            self.workflow_run_responses[workflow_id] = run_response
        if images is not None:
            self.workflow_images[workflow_id] = images

    def enable_auth(self, username: str, password: str) -> None:
        self.auth = (username, password)

    def calls_of(self, kind: str) -> List[str]:
        """The names ``kind`` calls were issued against, in order."""
        return [name for called_kind, name in self.calls if called_kind == kind]


def _sse_body(events: List[Dict[str, Any]]) -> str:
    """``data: {json}\\n\\n`` framing for a complete SSE stream."""
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


def build_app(device: FakeDevice) -> FastAPI:
    """The FastAPI app imitating the Backend_API surface over ``device``."""
    app = FastAPI()

    @app.middleware("http")
    async def _require_auth(request: Request, call_next):
        """Optional local-auth: with auth enabled, every non-``/local-auth``
        endpoint demands the issued bearer token (401 otherwise)."""
        if device.auth is not None and not request.url.path.startswith("/local-auth"):
            expected = f"Bearer {FAKE_BEARER_TOKEN}"
            if request.headers.get("authorization") != expected:
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return await call_next(request)

    # -- Health and identity -------------------------------------------

    @app.get("/system-health")
    def system_health():
        return {"status": "ok", "localServerVersion": device.local_server_version}

    @app.get("/dda-component-status")
    def component_status():
        return {
            "status": "HEALTHY",
            "localServerVersion": device.local_server_version,
        }

    # -- Local auth ------------------------------------------------------

    @app.get("/local-auth/status")
    def auth_status():
        return {"localLoginEnabled": device.auth is not None}

    @app.post("/local-auth/login")
    async def login(request: Request):
        payload = await request.json()
        with device.lock:
            device.calls.append(("login", str(payload.get("username"))))
            if (
                device.auth is None
                or (
                    payload.get("username"),
                    payload.get("password"),
                )
                != device.auth
            ):
                return JSONResponse({"detail": "Invalid credentials"}, status_code=401)
        return {"token": FAKE_BEARER_TOKEN, "username": device.auth[0]}

    # -- Model lifecycle -------------------------------------------------

    @app.get("/feature-configurations")
    def feature_configurations():
        with device.lock:
            return [model.entry() for model in device.models.values()]

    @app.get("/feature-configurations/models/{model_name}/start")
    def start_model(model_name: str):
        with device.lock:
            model = device.models.get(model_name)
            if model is None:
                return JSONResponse({"detail": f"Model {model_name!r} not found"}, status_code=404)
            device.calls.append(("start", model_name))
            model.start()
        return {"status": "STARTING"}

    @app.get("/feature-configurations/models/{model_name}/stop")
    def stop_model(model_name: str):
        with device.lock:
            model = device.models.get(model_name)
            if model is None:
                return JSONResponse({"detail": f"Model {model_name!r} not found"}, status_code=404)
            device.calls.append(("stop", model_name))
            model.stop()
        return {"status": "STOPPED"}

    # -- Text generation ---------------------------------------------------

    @app.get("/text-generation/models")
    def textgen_models():
        with device.lock:
            return [
                {"model_name": model.name, "state": model.status}
                for model in device.models.values()
                if model.model_type == VLLM_FEATURE_TYPE
            ]

    @app.post("/text-generation/{model_name}/generate")
    def generate(model_name: str):
        with device.lock:
            if model_name not in device.models:
                return JSONResponse({"detail": f"Model {model_name!r} not found"}, status_code=404)
            text = device.generated_text
        return {"generated_text": text, "token_count": len(text.split())}

    @app.post("/text-generation/{model_name}/generate-stream")
    def generate_stream(model_name: str):
        with device.lock:
            if model_name not in device.models:
                return JSONResponse({"detail": f"Model {model_name!r} not found"}, status_code=404)
            events: List[Dict[str, Any]] = [{"token": token} for token in device.stream_tokens]
        events.append({"done": True})
        return Response(content=_sse_body(events), media_type="text/event-stream")

    # -- Workflows ---------------------------------------------------------

    @app.get("/workflows")
    def workflows():
        with device.lock:
            return list(device.workflows)

    @app.post("/workflows/{workflow_id}/run")
    def run_workflow(workflow_id: str):
        with device.lock:
            device.calls.append(("run_workflow", workflow_id))
            response = device.workflow_run_responses.get(workflow_id)
        if response is None:
            return JSONResponse({"detail": f"Workflow {workflow_id!r} not found"}, status_code=404)
        return response

    @app.get("/workflows/{workflow_id}/images")
    def workflow_images(workflow_id: str):
        with device.lock:
            return device.workflow_images.get(workflow_id, {"images": []})

    @app.get("/workflows/{workflow_id}/capture-task")
    def capture_task(workflow_id: str):
        return {}

    return app


@contextmanager
def serve(device: FakeDevice) -> Iterator[str]:
    """Serve ``device`` over real HTTP on an ephemeral localhost port.

    Runs uvicorn on a daemon thread over a pre-bound socket (no port race)
    so the real ``EdgeApiClient``/``requests`` transport is exercised end to
    end; yields the base URL and shuts the server down on exit.
    """
    app = build_app(device)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("fake device server thread died during startup")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"fake device server did not start within {STARTUP_TIMEOUT_S}s")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=STARTUP_TIMEOUT_S)
