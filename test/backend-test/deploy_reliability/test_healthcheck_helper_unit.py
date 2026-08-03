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
"""Unit tests for the docker healthcheck helper src/backend/healthcheck.py
(task 3.2, Defect B fix).

The helper is stdlib-only, so the tests run it against REAL local HTTP
servers on ephemeral loopback ports (no mocks): exit 0 iff any probed URL
answers 200, exit 1 for non-200 answers and refused connections, and the
fallback ordering (first URL down, second up → still healthy).

Validates: Requirements 2.5
"""
import http.server
import socket
import threading

import pytest

import healthcheck


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    """Answers every GET with the status code the server was created with."""

    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # keep test output clean
        pass


@pytest.fixture
def http_server_factory():
    """Start real loopback HTTP servers answering a fixed status code."""
    servers = []

    def _start(status):
        handler = type("Handler", (_StatusHandler,), {"status": status})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return "http://127.0.0.1:{}/health".format(server.server_address[1])

    yield _start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _dead_url():
    """A loopback URL nothing listens on."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return "http://127.0.0.1:{}/health".format(port)


def test_exit_0_when_first_url_returns_200(http_server_factory):
    url = http_server_factory(200)
    assert healthcheck.main(urls=(url,)) == 0


def test_exit_0_on_fallback_when_first_url_is_down(http_server_factory):
    """The 5000→5443 fallback shape: first listener refused, second healthy."""
    up = http_server_factory(200)
    assert healthcheck.main(urls=(_dead_url(), up)) == 0


def test_exit_1_when_endpoint_reports_unhealthy(http_server_factory):
    """A serving backend that reports 503 (dead vLLM runtime) is UNHEALTHY —
    the helper must not treat 'port open' as healthy."""
    url = http_server_factory(503)
    assert healthcheck.main(urls=(url,)) == 1


def test_exit_1_when_nothing_listens():
    assert healthcheck.main(urls=(_dead_url(), _dead_url())) == 1


def test_probe_timeout_budget_fits_compose_healthcheck():
    """Two sequential probes must fit the compose healthcheck timeout (10s)."""
    assert 2 * healthcheck.PROBE_TIMEOUT_SECONDS < 10
