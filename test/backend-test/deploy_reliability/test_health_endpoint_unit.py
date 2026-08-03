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
"""Unit tests for the GET /health endpoint logic (task 3.2, Defect B fix).

``endpoints/health.py`` is deliberately import-light (fastapi + socket only),
so unlike app.py it IS importable on the host — the tests exercise the real
module: the conditional vLLM gate (``set_vllm_server``), the 200/503
semantics, and the loopback TCP reachability probe against a real ephemeral
listener (no mocks on the socket path).

Validates: Requirements 2.5
"""
import socket
import threading

import pytest
from fastapi import Response

from endpoints import health


@pytest.fixture(autouse=True)
def _reset_vllm_server():
    """Each test starts with no vLLM runtime installed."""
    health.set_vllm_server(None)
    yield
    health.set_vllm_server(None)


class _FakeServer:
    """Duck-typed stand-in for VllmRuntimeServer (host/port attributes)."""

    def __init__(self, host, port):
        self.host = host
        self.port = port


def _serve_one_connection(listener):
    """Accept connections until the listener closes (background thread)."""
    try:
        while True:
            conn, _ = listener.accept()
            conn.close()
    except OSError:
        pass  # listener closed — thread exits


def test_health_returns_200_when_no_vllm_runtime_started():
    """No runtime installed (vLLM-free image, or contained startup failure
    returning None): the app serving is the only gate — 200."""
    response = Response()
    body = health.get_health(response)
    assert response.status_code != 503
    assert body == {"status": "ok"}


def test_health_returns_200_when_started_vllm_runtime_is_reachable():
    """A started runtime whose loopback port accepts a TCP connect: 200."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    thread = threading.Thread(
        target=_serve_one_connection, args=(listener,), daemon=True)
    thread.start()
    try:
        health.set_vllm_server(_FakeServer("127.0.0.1", port))
        response = Response()
        body = health.get_health(response)
        assert response.status_code != 503
        assert body == {"status": "ok"}
    finally:
        listener.close()
        thread.join(timeout=5)


def test_health_returns_503_when_started_vllm_runtime_is_dead():
    """The incident shape (had the backend half-survived): a runtime that WAS
    started in-process but whose port no longer accepts connections — 503."""
    # Grab an ephemeral port and close it so nothing listens there.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    health.set_vllm_server(_FakeServer("127.0.0.1", dead_port))
    response = Response()
    body = health.get_health(response)
    assert response.status_code == 503
    assert body["status"] == "unhealthy"


def test_health_router_is_unauthenticated_and_serves_get_health():
    """The router mirrors local_auth's unauthenticated shape: a single GET
    /health route with no router-level dependencies (exempt from
    authorize_request, which get_api_router attaches per-router)."""
    routes = [r for r in health.router.routes if r.path == "/health"]
    assert len(routes) == 1
    assert routes[0].methods == {"GET"}
    # No auth dependency on the route (plain APIRouter, no Depends attached).
    assert not routes[0].dependencies
