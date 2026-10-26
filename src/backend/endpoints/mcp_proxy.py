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
"""``POST /mcp`` and ``GET /mcp/status`` on the Backend_API listener.

Feature: mcp-servers (design §6 ``endpoints/mcp_proxy.py``, D14, Error
Handling (Edge) proxy rows; Requirements 9.1, 9.4, 9.5, 12.3-12.5, 12.7,
12.8). The router comes from ``get_api_router()``, so ``authorize_request``
and the access log apply to both routes exactly as to every other route.

``POST /mcp`` (POST only; other methods are FastAPI's 405):

1. the body is read by the async dependency ``_mcp_body`` (413
   ``payload_too_large`` above 1 MiB) — the route itself is a plain ``def``
   that FastAPI runs in the threadpool, where ``proxy.forward`` blocks;
2. ``exposure_decision`` (pure; Property 12): an ``Origin`` header not in
   the configured allow-list (empty unless ``McpAllowedOrigins`` is set) is
   403 ``invalid_origin``; a non-loopback peer on the plaintext listener is
   403 ``mcp_loopback_only``; a disabled server is 503 ``mcp_disabled`` and a
   child that is not ready 503 ``mcp_unavailable``;
3. otherwise ``proxy.forward`` relays the request to the child.
"""
import logging
from typing import Iterable, Optional, Tuple

from asgi_correlation_id.context import correlation_id
from fastapi import Depends, HTTPException, Request

from endpoints.route.access_log_router import get_api_router
from mcp_edge import __version__ as MCP_EDGE_VERSION
from mcp_edge import proxy, status
from mcp_edge import supervisor as mcp_supervisor
from mcp_edge.config import McpConfig
from utils import utils

logger = logging.getLogger(__name__)

MCP_PROXY_MAX_BODY = 1024 * 1024
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

router = get_api_router()

# Observed when no supervisor was started: always disabled.
_UNSTARTED_CONFIG = McpConfig()


def exposure_decision(peer_host: Optional[str], tls_listener: bool,
                      origin: Optional[str], allowed_origins: Iterable[str],
                      server_enabled: bool, child_ready: bool
                      ) -> Optional[Tuple[int, str]]:
    """``(status, error)`` refusing the request, or None to forward it. Pure
    (Property 12)."""
    if origin is not None and origin not in set(allowed_origins):
        return 403, "invalid_origin"
    if peer_host not in LOOPBACK_HOSTS and not tls_listener:
        return 403, "mcp_loopback_only"
    if not server_enabled:
        return 503, "mcp_disabled"
    if not child_ready:
        return 503, "mcp_unavailable"
    return None


async def _mcp_body(request: Request) -> bytes:
    body = await request.body()
    if len(body) > MCP_PROXY_MAX_BODY:
        raise HTTPException(status_code=413, detail={"error": "payload_too_large"})
    return body


def _current():
    supervisor = mcp_supervisor.active_supervisor()
    config = supervisor.config if supervisor is not None else _UNSTARTED_CONFIG
    return supervisor, config


@router.post("/mcp")
def mcp_proxy(request: Request, body: bytes = Depends(_mcp_body)):
    supervisor, config = _current()
    state = config.state
    peer_host = request.client.host if request.client else None
    refusal = exposure_decision(
        peer_host=peer_host,
        tls_listener=utils.is_authorization_enabled_on_station(),
        origin=request.headers.get("origin"),
        allowed_origins=state.allowed_origins,
        server_enabled=state.server_enabled,
        child_ready=bool(supervisor is not None and supervisor.listening),
    )
    if refusal is not None:
        code, error = refusal
        if code == 403:
            logger.warning("MCP request from %s refused: %s", peer_host, error)
        else:
            logger.info("MCP request refused: %s", error)
        return proxy.error_response(code, error)
    return proxy.forward(body, request.headers, correlation_id.get())


@router.get("/mcp/status")
def get_mcp_status():
    supervisor, config = _current()
    return status.build_status(
        config, supervisor, MCP_EDGE_VERSION,
        supervisor.component_version if supervisor is not None else None)
