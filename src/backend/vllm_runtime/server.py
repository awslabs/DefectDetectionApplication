# Copyright 2025 Amazon Web Services, Inc.
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
"""Triton generate-extension HTTP server over the companion vLLM runtime
(design section 9; Requirements 4.1, 4.8, 5.2).

The server exposes, on loopback only, the same repository layout and
generate interface as the real Triton vLLM backend, so the runtime stays
swappable for an official Triton vLLM container later:

- ``POST /v2/models/{m}/generate`` — Triton generate extension:
  ``{"text_input": ..., "parameters": {...}}`` -> ``{"text_output": ...}``
- ``POST /v2/models/{m}/generate_stream`` — SSE stream of incremental
  ``{"text_output": ...}`` events; a mid-stream failure emits one
  ``{"error": ...}`` event and ends the stream
- ``GET /v2/models/{m}/ready`` — 200 iff the model is READY
- ``GET /v2/repository/index`` — every known model with its state
- ``POST /v2/repository/models/{m}/load`` / ``.../unload`` — Triton
  model-control extension, invoked by ``vllm_model_prep.py`` after
  staging / on component shutdown (Requirement 4.8)

Error mapping: :class:`~vllm_runtime.manager.ModelUnavailableError`
becomes ``409`` carrying the model's actual state (loading / failed /
unknown — the Text_Generation_API's Requirement 5.5 feed), and
:class:`~vllm_runtime.manager.GenerationError` becomes ``502`` carrying
the backend reason.

Two entry points:

- :func:`create_app` — the FastAPI app over a manager, directly usable
  with ``fastapi.testclient.TestClient`` (no port bound).
- :class:`VllmRuntimeServer` — wraps the app in a uvicorn server on a
  daemon thread with ``start()``/``stop()``, bound to 127.0.0.1 only,
  for ``app.py`` wiring.

Like the rest of this package, importing this module never imports
``vllm``: FastAPI/uvicorn are existing LocalServer dependencies and the
manager defers every vLLM import to engine construction.
"""
import base64
import binascii
import json
import logging
import threading
import time
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from vllm_runtime.constants import VLLM_RUNTIME_HOST, VLLM_RUNTIME_PORT
from vllm_runtime.manager import (
    GenerationError,
    ModelState,
    ModelUnavailableError,
    VllmRuntimeManager,
)

logger = logging.getLogger(__name__)

#: Keys of the generate-extension ``parameters`` object that steer the
#: Triton transport rather than vLLM sampling, exactly the keys the real
#: vllm_backend pops before building SamplingParams. Everything else
#: (temperature, top_p, max_tokens, ...) passes through to the manager's
#: sampling-params factory.
NON_SAMPLING_PARAMETER_KEYS = frozenset({"stream", "exclude_input_in_output"})


def sampling_params_from(parameters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The vLLM sampling parameters of a generate-extension ``parameters``
    object: the mapping minus the Triton transport keys. Pure."""
    return {
        key: value
        for key, value in (parameters or {}).items()
        if key not in NON_SAMPLING_PARAMETER_KEYS
    }


class GenerateRequest(BaseModel):
    """Body of ``/v2/models/{m}/generate[_stream]`` (Triton generate
    extension). ``image`` optionally carries one base64-encoded JPEG for
    multimodal generation (Requirement 4.8)."""

    text_input: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    image: Optional[str] = None


def _decoded_image(image: Optional[str]) -> Optional[bytes]:
    """The decoded bytes of an optional base64 ``image`` field, or None
    when absent. Undecodable content is the caller's schema violation ->
    422, matching FastAPI's validation status (Requirement 4.8)."""
    if image is None:
        return None
    try:
        return base64.b64decode(image, validate=True)
    except (binascii.Error, ValueError) as err:
        raise HTTPException(
            status_code=422,
            detail="image is not valid base64: {}".format(err),
        )


def _status_payload(model_name: str, status) -> Dict[str, Any]:
    """The state-info body shared by every non-READY response
    (Requirement 5.5 distinguishes loading / failed / unknown)."""
    payload: Dict[str, Any] = {"name": model_name, "state": status.state.value}
    if status.reason:
        payload["reason"] = status.reason
    return payload


def _sse_event(payload: Dict[str, Any]) -> str:
    """One server-sent event carrying a JSON payload, the framing the
    real Triton generate_stream endpoint uses."""
    return "data: {}\n\n".format(json.dumps(payload))


def create_app(manager: VllmRuntimeManager) -> FastAPI:
    """The Triton generate-extension + model-control app over one
    manager. No port is bound here, so tests drive it with the FastAPI
    ``TestClient`` directly."""
    app = FastAPI(title="vLLM companion runtime", docs_url=None, redoc_url=None)

    @app.exception_handler(ModelUnavailableError)
    async def _model_unavailable(request: Request, exc: ModelUnavailableError):
        # 409: the request conflicts with the model's serving state; the
        # body carries the state so callers can distinguish loading /
        # failed / unknown (Requirement 5.5).
        return JSONResponse(
            status_code=409,
            content={"error": str(exc), **_status_payload(exc.model_name, exc.status)},
        )

    @app.exception_handler(GenerationError)
    async def _generation_failed(request: Request, exc: GenerationError):
        # 502: the backend engine failed; the reason is retained
        # (Requirements 4.6, 5.7).
        return JSONResponse(
            status_code=502,
            content={"error": str(exc), "name": exc.model_name, "reason": exc.reason},
        )

    # --- generate extension (Requirement 5.2) ------------------------------

    @app.post("/v2/models/{model_name}/generate")
    async def generate(model_name: str, body: GenerateRequest):
        text = await manager.generate(
            model_name,
            body.text_input,
            sampling_params_from(body.parameters),
            image=_decoded_image(body.image),
        )
        return {"model_name": model_name, "text_output": text}

    @app.post("/v2/models/{model_name}/generate_stream")
    async def generate_stream(model_name: str, body: GenerateRequest):
        # READY is checked before the response starts so a non-READY
        # model still gets the 409 state-info mapping; once streaming
        # has begun, failures surface as one in-stream error event with
        # already-delivered tokens never retracted (Requirement 5.4).
        status = manager.state(model_name)
        if status.state is not ModelState.READY:
            raise ModelUnavailableError(model_name, status)
        params = sampling_params_from(body.parameters)
        # Decoded before the response starts so invalid base64 still gets
        # the 422 mapping rather than a mid-stream error event.
        image = _decoded_image(body.image)

        async def events():
            try:
                async for delta in manager.generate_stream(
                    model_name, body.text_input, params, image=image
                ):
                    yield _sse_event(
                        {"model_name": model_name, "text_output": delta}
                    )
            except ModelUnavailableError as err:
                yield _sse_event(
                    {"error": str(err), **_status_payload(model_name, err.status)}
                )
            except GenerationError as err:
                yield _sse_event(
                    {"error": str(err), "name": model_name, "reason": err.reason}
                )

        return StreamingResponse(events(), media_type="text/event-stream")

    # --- readiness / repository index ---------------------------------------

    @app.get("/v2/models/{model_name}/ready")
    async def ready(model_name: str):
        status = manager.state(model_name)
        if status.state is ModelState.READY:
            return Response(status_code=200)
        raise ModelUnavailableError(model_name, status)

    @app.get("/v2/repository/index")
    async def repository_index():
        return [
            _status_payload(name, status)
            for name, status in sorted(manager.list_models().items())
        ]

    # --- model control (Requirement 4.8) ------------------------------------

    @app.post("/v2/repository/models/{model_name}/load")
    async def load(model_name: str):
        status = await manager.load(model_name)
        if status.state is ModelState.READY:
            return _status_payload(model_name, status)
        # FAILED (validation/engine failure, reason retained) or UNKNOWN
        # (unloaded mid-flight): the load did not produce a serving model.
        return JSONResponse(
            status_code=409, content=_status_payload(model_name, status)
        )

    @app.post("/v2/repository/models/{model_name}/unload")
    async def unload(model_name: str):
        # Idempotent, like the manager: unloading an untracked name is
        # a no-op success so component Shutdown scripts can always run it.
        unloaded = manager.unload(model_name)
        return {"name": model_name, "unloaded": unloaded}

    return app


class VllmRuntimeServer:
    """The runtime HTTP server: uvicorn on a daemon thread, loopback
    only, started/stopped programmatically by ``app.py``.

    The bind host is fixed to 127.0.0.1 (``VLLM_RUNTIME_HOST``) by
    design — the generate interface is a device-internal contract, never
    a LAN service — and only the port is configurable
    (``VLLM_RUNTIME_PORT``, environment-overridable).
    """

    def __init__(
        self,
        manager: VllmRuntimeManager,
        port: int = VLLM_RUNTIME_PORT,
    ):
        self.app = create_app(manager)
        self.host = VLLM_RUNTIME_HOST
        self.port = port
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, startup_timeout_seconds: float = 15.0) -> None:
        """Start serving on ``127.0.0.1:{port}`` and return once the
        listener is up. Raises ``RuntimeError`` when the server does not
        come up (port already bound, ...). Idempotent while running."""
        if self._thread is not None and self._thread.is_alive():
            return
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_config=None,
            access_log=False,
            # Pin the plain-asyncio loop like app.py's main server: with the
            # vllm wheel installed, uvloop is present and uvicorn's default
            # loop="auto" would install the uvloop event-loop policy
            # process-wide, breaking the main thread's later
            # asyncio.get_event_loop() call in app.py (RuntimeError: no
            # current event loop) and taking LocalServer down at startup.
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, name="vllm-runtime-http", daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + startup_timeout_seconds
        while not self._server.started:
            if not self._thread.is_alive():
                raise RuntimeError(
                    "vLLM runtime server failed to start on {}:{}".format(
                        self.host, self.port
                    )
                )
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "vLLM runtime server did not start within {}s on {}:{}".format(
                        startup_timeout_seconds, self.host, self.port
                    )
                )
            time.sleep(0.02)
        logger.info(
            "vLLM runtime server listening on %s:%s", self.host, self.port
        )

    def stop(self, shutdown_timeout_seconds: float = 10.0) -> None:
        """Signal shutdown and wait for the server thread to exit.
        Safe to call when never started or already stopped."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(shutdown_timeout_seconds)
            if self._thread.is_alive():
                logger.warning(
                    "vLLM runtime server thread did not exit within %ss",
                    shutdown_timeout_seconds,
                )
        self._server = None
        self._thread = None
