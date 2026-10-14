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
* the stream camera surface (off until :meth:`FakeDevice.enable_streams`):
  ``/streams/capabilities`` (optionally 503 first, as while the probe
  runs), RTSP/RTMP ``/image-sources`` CRUD with write-only credentials,
  connection tests answered per URL by scripted :class:`FakeStreamServer`
  entries, stream-health and preview; plus Workflow_Engine registrations,
  triggered executions with run metadata, and continuous status with
  counters that advance in real time until paused;
* optional local-auth — when enabled, every non-``/local-auth`` endpoint
  demands the bearer token issued by ``POST /local-auth/login``.

Every ``start``/``stop``/``run_workflow``/``login`` the harness issues is
recorded in :attr:`FakeDevice.calls` (as are Image_Source creates, updates,
deletes and connection tests, triggers, pauses and resumes), so selftests
can assert restoration semantics (stop only what the harness started —
Reqs 4.3, 6.4, 8.3) from the device's point of view. The server runs on a
uvicorn background thread bound to an ephemeral localhost port
(:func:`serve`); the pytester-driven selftests run the real harness in a
subprocess pointed at that port while sharing this process's state objects
for scripting and post-run assertions.
"""

from __future__ import annotations

import base64
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

#: Bytes standing in for the JPEG a stream preview returns (the harness
#: checks only that the image is non-empty base64).
FAKE_PREVIEW_IMAGE_B64 = base64.b64encode(b"\xff\xd8\xff\xe0fake-preview-jpeg").decode("ascii")


def default_stream_capabilities() -> Dict[str, Any]:
    """The Device_Stream_Capabilities of a stream-capable fake (JP6-like:
    hardware H.264, software-only H.265)."""
    return {
        "rtsp": True,
        "rtmp": True,
        "tls": True,
        "rtspTls": True,
        "rtmpTls": True,
        "codecs": {
            "h264": {"hardware": "nvv4l2decoder", "software": "avdec_h264"},
            "h265": {"hardware": None, "software": "avdec_h265"},
        },
        "gstreamer": "1.20.3",
        "pyav": "17.1.0",
        "ffmpeg": "7.1.1",
        "probedAtMs": 1790000000000,
    }


class FakeStreamServer:
    """One stream URL the fake device can reach, and how its connection
    test answers: streaming with the given media facts, failing with
    ``failure`` (a category), or demanding ``credentials``
    (``(username, password)``) and failing ``authentication_failed``
    without them."""

    def __init__(
        self,
        codec: str = "h264",
        width: int = 1280,
        height: int = 720,
        fps: float = 25.0,
        decoder: str = "hardware",
        decoder_element: str = "nvv4l2decoder",
        failure: Optional[str] = None,
        credentials: Optional[Tuple[str, str]] = None,
    ):
        self.codec = codec
        self.width = width
        self.height = height
        self.fps = fps
        self.decoder = decoder
        self.decoder_element = decoder_element
        self.failure = failure
        self.credentials = credentials


class FakeImageSource:
    """One RTSP/RTMP Image_Source with its write-only credentials and the
    Stream_Health of its (imagined) session."""

    def __init__(self, image_source_id: str, body: Dict[str, Any]):
        self.image_source_id = image_source_id
        self.type = body.get("type")
        self.name = body.get("name")
        self.description = body.get("description")
        self.location = body.get("location")
        self.stream_settings = dict(body.get("streamSettings") or {})
        self.credentials: Optional[Dict[str, str]] = None
        self.set_credentials(body.get("credentials"))
        self.state = "stopped"
        self.server: Optional[FakeStreamServer] = None
        self.last_error: Optional[Dict[str, Any]] = None

    def set_credentials(self, credentials: Any) -> None:
        """Store the non-empty fields; an all-blank object keeps what is
        stored (device semantics)."""
        fields = {
            key: value
            for key, value in (credentials or {}).items()
            if key in ("username", "password", "urlSecret") and value
        }
        if fields:
            self.credentials = fields

    def health(self) -> Dict[str, Any]:
        server = self.server if self.state == "streaming" else None
        return {
            "cameraKey": f"cfg-{self.image_source_id}",
            "state": self.state,
            "codec": server.codec if server else None,
            "width": server.width if server else None,
            "height": server.height if server else None,
            "frameWidth": server.width if server else None,
            "frameHeight": server.height if server else None,
            "sourceFps": server.fps if server else None,
            "decoder": server.decoder if server else None,
            "decoderElement": server.decoder_element if server else None,
            "decoderFallback": False,
            "reconnects": 0,
            "lastFrameAtMs": 1790000000123 if server else None,
            "lastError": self.last_error,
            "leases": 0,
        }


class FakeContinuous:
    """The Continuous_Runner of one registration: ``completed`` advances at
    ``fps`` in real time while running and freezes while paused."""

    def __init__(self, registration_id: str, fps: float = 5.0, paused: bool = False):
        self.registration_id = registration_id
        self.fps = fps
        self.paused = paused
        #: Simulated device bug: runs go on while the status says paused.
        self.ignores_pause = False
        self.paused_at_ms: Optional[int] = 1790000000000 if paused else None
        self._base = {
            "started": 0,
            "completed": 0,
            "failed": 0,
            "skippedBusy": 0,
            "skippedNoNewFrame": 0,
            "notable": 0,
            "outputsSent": 0,
            "streamUnavailable": 0,
        }
        self._since = time.monotonic()

    def counters(self) -> Dict[str, int]:
        counters = dict(self._base)
        if not self.paused or self.ignores_pause:
            runs = int((time.monotonic() - self._since) * self.fps)
            counters["started"] += runs
            counters["completed"] += runs
        return counters

    def pause(self) -> None:
        if not self.paused:
            if not self.ignores_pause:
                self._base = self.counters()
            self.paused = True
            self.paused_at_ms = int(time.time() * 1000)

    def resume(self) -> None:
        if self.paused:
            if not self.ignores_pause:
                self._since = time.monotonic()
            self.paused = False
            self.paused_at_ms = None

    def status(self) -> Dict[str, Any]:
        return {
            "registrationId": self.registration_id,
            "state": "paused" if self.paused else "running",
            "configuredFps": self.fps,
            "effectiveFps": 0.0 if self.paused else self.fps,
            "counters": self.counters(),
            "streamHealth": {"state": "streaming", "codec": "h264"},
            "pausedAtMs": self.paused_at_ms,
            "cameraSourceId": "cfg-fake-camera",
            "runInProgress": False,
        }


class FakeExecution:
    """One triggered Workflow_Engine run walking scripted statuses, one per
    observation (``GET /workflows/executions/{id}``)."""

    def __init__(
        self,
        execution_id: str,
        registration_id: str,
        statuses: List[str],
        metadata: Dict[str, Any],
    ):
        self.execution_id = execution_id
        self.registration_id = registration_id
        self.status = "pending"
        self._pending = list(statuses)
        self.metadata = metadata

    def document(self, observe: bool = False) -> Dict[str, Any]:
        if observe and self._pending:
            self.status = self._pending.pop(0)
        terminal = self.status in ("completed", "failed")
        return {
            "executionId": self.execution_id,
            "registrationId": self.registration_id,
            "status": self.status,
            "startedAt": 1790000000,
            "finishedAt": 1790000002 if terminal else None,
            "failingNodeId": None,
            "error": None,
            "hasImageResults": False,
            "captureId": f"capture-{self.execution_id}",
            "outputDir": f"/aws_dda/captures/{self.execution_id}",
        }


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
        #: with kind in {"start", "stop", "run_workflow", "login",
        #: "create_image_source", "update_image_source", "delete_image_source",
        #: "connection_test", "trigger", "pause", "resume"}.
        self.calls: List[Tuple[str, str]] = []
        self.lock = threading.Lock()
        # -- stream camera surface (off until enable_streams) -------------
        #: Device_Stream_Capabilities; ``None`` answers 404 (no feature).
        self.stream_capabilities: Optional[Dict[str, Any]] = None
        #: Capability requests still to answer 503 (probe running).
        self.capabilities_503_remaining = 0
        #: Stream URL -> how its connection test answers; an unknown URL
        #: fails ``not_found``.
        self.stream_servers: Dict[str, FakeStreamServer] = {}
        self.image_sources: Dict[str, FakeImageSource] = {}
        #: Every create/PATCH body as received (credentials included), for
        #: asserting what the harness sent.
        self.image_source_requests: List[Dict[str, Any]] = []
        #: DELETEs still to fail with a 500 (restoration selftests).
        self.fail_deletes = 0
        #: Simulated device bug: GET /image-sources/{id} echoes the stored
        #: credentials inside a URL.
        self.leak_credentials = False
        self.registrations: List[Dict[str, Any]] = []
        self.continuous: Dict[str, FakeContinuous] = {}
        #: Continuous status reads answer 500 while paused, cutting the
        #: harness's pause check short (restoration selftests).
        self.fail_status_while_paused = False
        self.run_scripts: Dict[str, Dict[str, Any]] = {}
        self.executions: Dict[str, FakeExecution] = {}
        self._next_id = 0

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

    def enable_streams(
        self, capabilities: Optional[Dict[str, Any]] = None, probe_503: int = 0
    ) -> None:
        """Turn the stream camera surface on; the first ``probe_503``
        capability requests answer 503, as while the startup probe runs."""
        self.stream_capabilities = (
            capabilities if capabilities is not None else default_stream_capabilities()
        )
        self.capabilities_503_remaining = probe_503

    def add_stream_server(self, url: str, **kwargs) -> FakeStreamServer:
        server = FakeStreamServer(**kwargs)
        self.stream_servers[url] = server
        return server

    def add_registration(
        self,
        workflow_id: str,
        version: str,
        status: str = "registered",
        continuous_fps: Optional[float] = None,
        continuous_paused: bool = False,
        run_statuses: Tuple[str, ...] = ("running", "completed"),
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """A Workflow_Engine registration; ``continuous_fps`` makes it a
        continuous one. Returns its registration id."""
        registration_id = f"{workflow_id}-v{version}-arm64_jp6"
        entry = {
            "registrationId": registration_id,
            "workflowId": workflow_id,
            "name": workflow_id,
            "version": version,
            "arch": "arm64_jp6",
            "artifactPath": f"/aws_dda/workflows/{workflow_id}/{version}",
            "status": status,
            "registeredAt": 1790000000,
        }
        if status != "registered":
            entry["invalidReason"] = "scripted invalid registration"
        self.registrations.append(entry)
        if continuous_fps is not None:
            self.continuous[registration_id] = FakeContinuous(
                registration_id, fps=continuous_fps, paused=continuous_paused
            )
        self.run_scripts[registration_id] = {
            "statuses": list(run_statuses),
            "metadata": metadata if metadata is not None else {},
        }
        return registration_id

    def new_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id:04d}"

    def connect(self, source: FakeImageSource) -> Dict[str, Any]:
        """The connection test of ``source`` (caller holds the lock)."""
        server = self.stream_servers.get(source.location)
        category = None
        message = "Connected: the camera is streaming"
        if server is None:
            category, message = "not_found", "no stream is available on the path"
        elif server.failure:
            category, message = server.failure, f"scripted {server.failure}"
        elif server.credentials is not None:
            stored = source.credentials or {}
            if (stored.get("username"), stored.get("password")) != server.credentials:
                category = "authentication_failed"
                message = "the camera rejected the credentials (401 Unauthorized)"
        if category is None:
            source.state, source.server = "streaming", server
        else:
            source.state, source.server = "failed", None
            source.last_error = {"category": category, "message": message, "atMs": 1790000000500}
        return {
            "ok": category is None,
            "category": category,
            "message": message,
            "streamHealth": source.health(),
            "image": FAKE_PREVIEW_IMAGE_B64 if category is None else None,
            "imageError": None,
        }


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

    _add_stream_routes(app, device)
    return app


def _not_found(what: str) -> JSONResponse:
    return JSONResponse({"detail": f"{what} was not found"}, status_code=404)


def _add_stream_routes(app: FastAPI, device: FakeDevice) -> None:
    """The stream camera and Workflow_Engine surface (see the module
    docstring); answers 404 throughout until ``enable_streams``."""

    def stream_source(image_source_id: str) -> Optional[FakeImageSource]:
        if device.stream_capabilities is None:
            return None
        return device.image_sources.get(image_source_id)

    def source_document(source: FakeImageSource) -> Dict[str, Any]:
        document = {
            "imageSourceId": source.image_source_id,
            "name": source.name,
            "type": source.type,
            "description": source.description,
            "location": source.location,
            "imageSourceConfiguration": {"streamSettings": dict(source.stream_settings)},
            "streamHealth": source.health(),
            "credentialsConfigured": source.credentials is not None,
        }
        if device.leak_credentials and source.credentials:
            scheme, _, rest = (source.location or "").partition("://")
            document["debugLocation"] = (
                f"{scheme}://{source.credentials.get('username')}:"
                f"{source.credentials.get('password')}@{rest}"
            )
        return document

    @app.get("/streams/capabilities")
    def stream_capabilities():
        with device.lock:
            if device.stream_capabilities is None:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            if device.capabilities_503_remaining > 0:
                device.capabilities_503_remaining -= 1
                return JSONResponse(
                    {"detail": "The stream capability probe is still running"}, status_code=503
                )
            return dict(device.stream_capabilities)

    @app.post("/image-sources")
    async def create_image_source(request: Request):
        body = await request.json()
        with device.lock:
            if device.stream_capabilities is None:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            device.image_source_requests.append(body)
            if "@" in str(body.get("location", "")).partition("://")[2].split("/")[0]:
                return JSONResponse(
                    {"detail": "location: must not contain user information"}, status_code=400
                )
            image_source_id = device.new_id("src")
            device.image_sources[image_source_id] = FakeImageSource(image_source_id, body)
            device.calls.append(("create_image_source", image_source_id))
        return {"imageSourceId": image_source_id}

    @app.patch("/image-sources/{image_source_id}")
    async def update_image_source(image_source_id: str, request: Request):
        body = await request.json()
        with device.lock:
            source = stream_source(image_source_id)
            if source is None:
                return _not_found(f"Image source {image_source_id!r}")
            device.image_source_requests.append(body)
            device.calls.append(("update_image_source", image_source_id))
            source.stream_settings.update(body.get("streamSettings") or {})
            if body.get("clearCredentials"):
                source.credentials = None
            source.set_credentials(body.get("credentials"))
            if body.get("location"):
                source.location = body["location"]
            # A configuration change restarts the session.
            source.state, source.server = "connecting", None
        return {"imageSourceId": image_source_id}

    @app.delete("/image-sources/{image_source_id}")
    def delete_image_source(image_source_id: str):
        with device.lock:
            if stream_source(image_source_id) is None:
                return _not_found(f"Image source {image_source_id!r}")
            device.calls.append(("delete_image_source", image_source_id))
            if device.fail_deletes > 0:
                device.fail_deletes -= 1
                return JSONResponse({"detail": "scripted delete failure"}, status_code=500)
            del device.image_sources[image_source_id]
        return {"imageSourceId": image_source_id}

    @app.get("/image-sources")
    def list_image_sources(type: Optional[str] = None):
        with device.lock:
            return [
                source_document(source)
                for source in device.image_sources.values()
                if type is None or source.type == type
            ]

    @app.get("/image-sources/{image_source_id}")
    def get_image_source(image_source_id: str):
        with device.lock:
            source = stream_source(image_source_id)
            if source is None:
                return _not_found(f"Image source {image_source_id!r}")
            return source_document(source)

    @app.post("/image-sources/{image_source_id}/test-connection")
    def connection_test(image_source_id: str):
        with device.lock:
            source = stream_source(image_source_id)
            if source is None:
                return _not_found(f"Image source {image_source_id!r}")
            device.calls.append(("connection_test", image_source_id))
            return device.connect(source)

    @app.get("/image-sources/{image_source_id}/stream-health")
    def stream_health(image_source_id: str):
        with device.lock:
            source = stream_source(image_source_id)
            if source is None:
                return _not_found(f"Image source {image_source_id!r}")
            document = source.health()
            document["credentialsConfigured"] = source.credentials is not None
            return document

    @app.post("/image-sources/{image_source_id}/preview")
    def preview(image_source_id: str):
        with device.lock:
            source = stream_source(image_source_id)
            if source is None:
                return _not_found(f"Image source {image_source_id!r}")
            if source.state != "streaming":
                return JSONResponse(
                    {"detail": f"The stream camera has no frame to show (state {source.state})"},
                    status_code=503,
                )
        return {"image": FAKE_PREVIEW_IMAGE_B64, "imageFileName": None}

    # -- Workflow_Engine registrations -----------------------------------

    @app.get("/workflows/registrations")
    def registrations():
        with device.lock:
            return [
                entry
                for entry in device.registrations
                if entry["status"] in ("registered", "invalid")
            ]

    @app.post("/workflows/registrations/{registration_id}/trigger")
    def trigger(registration_id: str):
        with device.lock:
            script = device.run_scripts.get(registration_id)
            if script is None:
                return _not_found(f"Workflow registration {registration_id!r}")
            continuous = device.continuous.get(registration_id)
            if continuous is not None and not continuous.paused:
                return JSONResponse(
                    {
                        "detail": "CONTINUOUS_WORKFLOW_RUNNING: workflow registration "
                        f"'{registration_id}' processes its stream camera continuously"
                    },
                    status_code=409,
                )
            device.calls.append(("trigger", registration_id))
            execution = FakeExecution(
                device.new_id("exec"), registration_id, script["statuses"], script["metadata"]
            )
            device.executions[execution.execution_id] = execution
            return execution.document()

    @app.get("/workflows/executions/{execution_id}")
    def execution(execution_id: str):
        with device.lock:
            run = device.executions.get(execution_id)
            if run is None:
                return _not_found(f"Workflow execution {execution_id!r}")
            return run.document(observe=True)

    @app.get("/workflows/executions/{execution_id}/metadata")
    def execution_metadata(execution_id: str):
        with device.lock:
            run = device.executions.get(execution_id)
            if run is None:
                return _not_found(f"Workflow execution {execution_id!r}")
            return run.metadata

    def continuous_or_404(registration_id: str):
        runner = device.continuous.get(registration_id)
        if runner is None:
            return None, JSONResponse(
                {
                    "detail": f"Workflow registration '{registration_id}' does not process "
                    "a stream camera continuously"
                },
                status_code=404,
            )
        return runner, None

    @app.get("/workflows/registrations/{registration_id}/continuous")
    def continuous_status(registration_id: str):
        with device.lock:
            runner, missing = continuous_or_404(registration_id)
            if runner is None:
                return missing
            if runner.paused and device.fail_status_while_paused:
                return JSONResponse({"detail": "scripted status failure"}, status_code=500)
            return runner.status()

    @app.post("/workflows/registrations/{registration_id}/continuous/pause")
    def pause(registration_id: str):
        with device.lock:
            runner, missing = continuous_or_404(registration_id)
            if runner is None:
                return missing
            device.calls.append(("pause", registration_id))
            runner.pause()
            return runner.status()

    @app.post("/workflows/registrations/{registration_id}/continuous/resume")
    def resume(registration_id: str):
        with device.lock:
            runner, missing = continuous_or_404(registration_id)
            if runner is None:
                return missing
            device.calls.append(("resume", registration_id))
            runner.resume()
            return runner.status()


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
