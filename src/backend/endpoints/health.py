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
"""Unauthenticated ``GET /health`` endpoint (edge-deploy-reliability, Defect B).

The docker healthcheck (``src/backend/healthcheck.py``, wired in
``src/docker-compose.yaml``) probes this endpoint every 15 seconds; the
Greengrass recipe Startup (``docker compose up -d --wait``) gates the
component's RUNNING state on the resulting container health, so this endpoint
is the single truth source for "the backend is actually serving".

Semantics (Requirement 2.5):

- **200** when the FastAPI app is serving AND — only if a companion vLLM
  runtime server was actually started in-process (``set_vllm_server`` called
  with the non-None ``start_vllm_runtime()`` result from app.py's ``__main__``
  startup sequence) — the runtime's loopback port (127.0.0.1:8901 by default)
  accepts a short-timeout TCP connect.
- **503** otherwise (started-then-dead vLLM runtime — the incident shape, had
  the backend half-survived).

A *contained* vLLM startup failure (``start_vllm_runtime()`` returns None) or
a vLLM-free image never flips the backend unhealthy: no runtime server was
started, so no 8901 gate applies — preserving the containment semantics of
vllm-triton-inference Requirement 4.3. The probe is a bare TCP connect, never
a model invocation.

Registration: like ``local_auth``'s unauthenticated router, this router
intentionally carries no ``authorize_request`` dependency (the auth dependency
is attached per-router by ``endpoints.route.access_log_router.get_api_router``;
routers built directly from ``APIRouter`` are exempt). A plain ``APIRouter``
(the ``download_file.unauthenticated_router`` shape) is used instead of
``AccessLogRoute`` so the 15-second docker probe does not spam the access log
and the module stays import-light for host-side unit tests.
"""
import logging
import socket
from typing import Optional

from fastapi import APIRouter, Response

logger = logging.getLogger(__name__)

router = APIRouter()

#: Short-timeout budget for the loopback TCP connect to the vLLM runtime.
#: Well below the 10s healthcheck timeout in docker-compose.yaml.
VLLM_PROBE_TIMEOUT_SECONDS = 2.0

#: Fallbacks when the installed server object does not expose host/port
#: attributes; mirror vllm_runtime.constants (VLLM_RUNTIME_HOST /
#: DEFAULT_VLLM_RUNTIME_PORT) without importing the package so this module
#: stays import-light.
DEFAULT_VLLM_HOST = "127.0.0.1"
DEFAULT_VLLM_PORT = 8901

#: The vLLM runtime server started in-process, or None when no runtime was
#: started (vLLM absent from the image, or contained startup failure).
_vllm_server: Optional[object] = None


def set_vllm_server(server: Optional[object]) -> None:
    """Install the ``start_vllm_runtime()`` result for health gating.

    Called once from app.py's ``__main__`` startup sequence. ``None`` (vLLM
    absent, or contained startup failure per vllm-triton-inference 4.3) means
    the vLLM leg of the health gate is skipped entirely.
    """
    global _vllm_server
    _vllm_server = server


def _vllm_runtime_reachable(server: object) -> bool:
    """Whether the started runtime's loopback port accepts a TCP connect.

    A bare short-timeout connect (never a model invocation, never an HTTP
    request that could touch an engine): the gate detects a started-then-dead
    runtime process, not model readiness.
    """
    host = getattr(server, "host", DEFAULT_VLLM_HOST)
    port = getattr(server, "port", DEFAULT_VLLM_PORT)
    try:
        with socket.create_connection((host, port),
                                      timeout=VLLM_PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


@router.get("/health")
def get_health(response: Response) -> dict:
    """Liveness/health probe for the docker healthcheck (Requirement 2.5)."""
    # Reaching this handler proves the FastAPI app is serving; the only
    # additional gate is the conditional vLLM runtime reachability check.
    if _vllm_server is not None and not _vllm_runtime_reachable(_vllm_server):
        response.status_code = 503
        return {
            "status": "unhealthy",
            "reason": "vLLM runtime was started in-process but its loopback "
                      "port no longer accepts connections",
        }
    return {"status": "ok"}
