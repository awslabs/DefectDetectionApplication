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
"""The ``/mcp`` proxy route and ``GET /mcp/status``.

Feature: mcp-servers (design §5 ``proxy.py``, §6 ``endpoints/mcp_proxy.py``
and ``app.py``, D14, Error Handling (Edge) proxy rows; tasks.md 10.7).
Requirements 9.1, 9.4, 9.5, 11.3, 12.3, 12.4, 12.5, 12.7, 12.8, 16.2.

Through the REAL ``authorize_request`` (the router of ``get_api_router()``):
each decision-matrix row, a disabled account and ``?token=`` at ``/mcp``
are 401; an ``Origin`` header is 403 and never forwarded; a non-loopback
peer on the plaintext listener is 403; the child is replaced by an
``httpx.MockTransport``: a stalled upstream is 504, a body above 4 MiB is
502, a refused connection 503. Nothing listens on a real port.
"""
import contextlib
import time
import types
from unittest import mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from endpoints import mcp_proxy
from local_auth import session_tokens
from mcp_edge import proxy
from mcp_edge import supervisor as mcp_supervisor
from mcp_edge.config import McpState

SECRET = b"\x07" * 32
AUTH_SETTINGS = {
    "clientId": "test-client-id",
    "clientSecret": "test-client-secret",
    "introspectEndpoint": "https://idp.example.com/oauth2/introspect",
}
BEARER = "remote-opaque-bearer-token"
RPC = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
ALLOWED_ORIGIN = "https://hmi.example.com"


def mapping_cache(enabled=True):
    return {"accounts": {"operator1": {"email": "operator1@example.com",
                                       "role": "Operator", "enabled": enabled}}}


class FakeIntrospection:
    def __init__(self, active=()):
        self.active = set(active)
        self.calls = []

    def __call__(self, token, clientId, clientSecret, introspectEndpoint):
        self.calls.append(token)
        body = {"active": token in self.active, "sub": "remote-user"}
        return types.SimpleNamespace(status_code=200, json=lambda body=body: body)


@contextlib.contextmanager
def configured(local_login_enabled, existing_auth, cache=None, introspection=None):
    introspection = introspection or FakeIntrospection()
    with mock.patch(
        "local_auth.config.is_local_login_enabled", return_value=local_login_enabled
    ), mock.patch(
        "utils.utils.is_authorization_enabled_on_station", return_value=existing_auth
    ), mock.patch(
        "utils.utils.get_authorization_settings_from_file",
        return_value=dict(AUTH_SETTINGS),
    ), mock.patch(
        "local_auth.session_tokens.get_or_create_secret", return_value=SECRET
    ), mock.patch(
        "local_auth.credential_cache.get_default_cache",
        return_value=cache if cache is not None else mapping_cache(),
    ), mock.patch("utils.auth.validate_remotely", introspection):
        yield introspection


def session_token(expired=False):
    issued_at = int(time.time())
    if expired:
        issued_at -= session_tokens.TOKEN_LIFETIME_SECONDS + 60
    return session_tokens.issue_token(SECRET, "operator1", "Operator", issued_at)


class FakeSupervisor:
    def __init__(self, enabled=True, listening=True, origins=()):
        self.config = types.SimpleNamespace(
            state=McpState(enabled, False, tuple(origins)))
        self.listening = listening
        self.features = ("vllm",)
        self.restart_count = 0
        self.last_exit_code = None
        self.last_error = None
        self.component_version = "1.0.130"


class Upstream:
    """The child, as an httpx.MockTransport handler."""

    def __init__(self, respond=None):
        self.requests = []
        self.respond = respond or (lambda request: httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}))

    def __call__(self, request):
        self.requests.append(request)
        return self.respond(request)


class _Peer:
    """Sets scope['client'] (AccessLogRoute and the exposure rule read it)."""

    def __init__(self, app, host):
        self.app, self.host = app, host

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["client"] = (self.host, 50000)
        await self.app(scope, receive, send)


@pytest.fixture
def upstream(monkeypatch):
    handler = Upstream()
    monkeypatch.setattr(proxy, "CLIENT", httpx.Client(
        transport=httpx.MockTransport(handler), timeout=proxy.TIMEOUT))
    return handler


@pytest.fixture
def active(monkeypatch):
    def install(supervisor):
        monkeypatch.setattr(mcp_supervisor, "_ACTIVE", supervisor)
        return supervisor
    install(FakeSupervisor())
    return install


def client_for(peer="127.0.0.1"):
    app = FastAPI()
    app.include_router(mcp_proxy.router)
    return TestClient(_Peer(app, peer))


def post(client, headers=None, params=None, content=RPC):
    return client.post("/mcp", content=content, params=params,
                       headers={"Content-Type": "application/json",
                                "Accept": "application/json, text/event-stream",
                                **(headers or {})})


# ------------------------------------------------------------ router shape

def test_routes_are_on_an_authenticated_router():
    paths = {(route.path, tuple(sorted(route.methods)))
             for route in mcp_proxy.router.routes}
    assert paths == {("/mcp", ("POST",)), ("/mcp/status", ("GET",))}
    assert len(mcp_proxy.router.dependencies) == 1


def test_get_and_delete_are_405(active, upstream):
    client = client_for()
    with configured(False, False):
        assert client.get("/mcp").status_code == 405
        assert client.delete("/mcp").status_code == 405
    assert upstream.requests == []


# -------------------------------------------- authorize_request matrix rows

def test_open_access_row_forwards(active, upstream):
    with configured(False, False):
        response = post(client_for())
    assert response.status_code == 200
    assert response.json()["result"] == {"tools": []}
    assert len(upstream.requests) == 1


def test_local_session_token_forwards_the_authorization_header(active, upstream):
    token = session_token()
    with configured(True, False):
        response = post(client_for(), headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    forwarded = upstream.requests[0].headers
    assert forwarded["authorization"] == f"Bearer {token}"
    assert forwarded["x-request-id"]


@pytest.mark.parametrize("credential", [
    lambda: None,
    lambda: session_token(expired=True),
    lambda: "not-a-token",
])
def test_local_login_rejections_are_401(active, upstream, credential):
    value = credential()
    headers = {"Authorization": f"Bearer {value}"} if value else {}
    with configured(True, False):
        response = post(client_for(), headers=headers)
    assert response.status_code == 401
    assert upstream.requests == []


def test_disabled_account_is_401(active, upstream):
    token = session_token()
    with configured(True, False, cache=mapping_cache(enabled=False)):
        response = post(client_for(), headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert upstream.requests == []


def test_existing_token_auth_row(active, upstream):
    introspection = FakeIntrospection(active=(BEARER,))
    with configured(False, True, introspection=introspection):
        accepted = post(client_for("192.0.2.10"),
                        headers={"Authorization": f"Bearer {BEARER}"})
        rejected = post(client_for("192.0.2.10"),
                        headers={"Authorization": "Bearer inactive"})
    assert accepted.status_code == 200
    assert rejected.status_code == 401
    assert len(upstream.requests) == 1


def test_query_token_at_mcp_is_401(active, upstream):
    introspection = FakeIntrospection(active=(BEARER,))
    with configured(False, True, introspection=introspection):
        response = post(client_for(), params={"token": BEARER})
    assert response.status_code == 401
    assert introspection.calls == []
    assert upstream.requests == []


# ------------------------------------------------------- exposure rule

@pytest.mark.parametrize("origin", ["https://evil.example.com", ALLOWED_ORIGIN, "null"])
def test_origin_is_403_with_the_default_empty_allow_list(active, upstream, origin):
    with configured(False, False):
        response = post(client_for(), headers={"Origin": origin})
    assert response.status_code == 403
    assert response.json() == {"error": "invalid_origin"}
    assert upstream.requests == []


def test_allow_listed_origin_is_forwarded_without_the_origin_header(active, upstream):
    active(FakeSupervisor(origins=(ALLOWED_ORIGIN,)))
    with configured(False, False):
        response = post(client_for(), headers={"Origin": ALLOWED_ORIGIN,
                                               "Cookie": "session=1",
                                               "X-Forwarded-For": "198.51.100.1"})
    assert response.status_code == 200
    forwarded = upstream.requests[0].headers
    assert "origin" not in forwarded
    assert "cookie" not in forwarded
    assert "x-forwarded-for" not in forwarded


def test_non_loopback_peer_on_plaintext_listener_is_403(active, upstream):
    with configured(False, False):
        response = post(client_for("192.0.2.10"))
    assert response.status_code == 403
    assert response.json() == {"error": "mcp_loopback_only"}
    assert upstream.requests == []


def test_ipv6_loopback_is_loopback(active, upstream):
    with configured(False, False):
        assert post(client_for("::1")).status_code == 200


def test_non_loopback_peer_on_tls_listener_is_forwarded(active, upstream):
    introspection = FakeIntrospection(active=(BEARER,))
    with configured(False, True, introspection=introspection):
        response = post(client_for("192.0.2.10"),
                        headers={"Authorization": f"Bearer {BEARER}"})
    assert response.status_code == 200


# --------------------------------------------------------- availability

def test_disabled_is_503(active, upstream):
    active(FakeSupervisor(enabled=False))
    with configured(False, False):
        response = post(client_for())
    assert response.status_code == 503
    assert response.json() == {"error": "mcp_disabled"}


def test_not_ready_is_503(active, upstream):
    active(FakeSupervisor(listening=False))
    with configured(False, False):
        response = post(client_for())
    assert response.status_code == 503
    assert response.json() == {"error": "mcp_unavailable"}


def test_never_started_is_503_disabled(active, upstream):
    active(None)
    with configured(False, False):
        response = post(client_for())
    assert response.json() == {"error": "mcp_disabled"}


def test_body_above_one_mib_is_413(active, upstream):
    with configured(False, False):
        response = post(client_for(), content=b"x" * (mcp_proxy.MCP_PROXY_MAX_BODY + 1))
    assert response.status_code == 413
    assert upstream.requests == []


# -------------------------------------------------------------- upstream

class _Stalls(httpx.SyncByteStream):
    def __iter__(self):
        yield b'{"jsonrpc":'
        raise httpx.ReadTimeout("stalled")


@pytest.mark.parametrize("respond,status,error", [
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("stalled", request=r)),
     504, "mcp_timeout"),
    (lambda r: httpx.Response(200, headers={"content-type": "application/json"},
                              stream=_Stalls()), 504, "mcp_timeout"),
    (lambda r: httpx.Response(200, content=b"x" * (proxy.MCP_PROXY_MAX_UPSTREAM_BODY + 1),
                              headers={"content-type": "application/json"}),
     502, "mcp_bad_upstream"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused", request=r)),
     503, "mcp_unavailable"),
])
def test_upstream_failures(active, upstream, respond, status, error):
    upstream.respond = respond
    with configured(False, False):
        response = post(client_for())
    assert response.status_code == status
    assert response.json() == {"error": error}
    assert len(upstream.requests) == 1  # never retried


def test_upstream_status_and_content_type_are_relayed(active, upstream):
    upstream.respond = lambda r: httpx.Response(
        202, content=b"", headers={"content-type": "application/json"})
    with configured(False, False):
        response = post(client_for())
    assert response.status_code == 202


def test_event_stream_is_relayed(active, upstream):
    upstream.respond = lambda r: httpx.Response(
        200, content=b"event: message\ndata: {}\n\n",
        headers={"content-type": "text/event-stream"})
    with configured(False, False):
        response = post(client_for())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == b"event: message\ndata: {}\n\n"


def test_client_has_no_transport_retries():
    assert proxy.CLIENT._transport._pool._retries == 0
    assert proxy.TIMEOUT.read == proxy.MCP_PROXY_UPSTREAM_READ_S == 150.0


# ---------------------------------------------------------------- status

def test_status_document(active, upstream):
    with configured(False, False):
        response = client_for().get("/mcp/status")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True and body["listening"] is True
    assert body["componentVersion"] == "1.0.130"
    assert body["features"] == ["vllm"]
    assert body["endpoint"] == "/mcp"
    assert upstream.requests == []


def test_status_requires_authentication(active, upstream):
    with configured(True, False):
        assert client_for().get("/mcp/status").status_code == 401


# --------------------------------------------- supervisor (10.7 additions)

def test_component_version_fallback_is_cleaned():
    assert mcp_supervisor.clean_component_version("NOT_FOUND") == "unknown"
    assert mcp_supervisor.clean_component_version(None) == "unknown"
    assert mcp_supervisor.clean_component_version("1.0.130") == "1.0.130"
    supervisor = mcp_supervisor.McpSupervisor(
        types.SimpleNamespace(state=McpState(True, False, ())), "http://127.0.0.1:5000",
        component_version="NOT_FOUND")
    assert supervisor.server_version.endswith("+unknown")


def test_readiness_wait_observes_stop():
    supervisor = mcp_supervisor.McpSupervisor(
        types.SimpleNamespace(state=McpState(True, False, ())), "http://127.0.0.1:5000",
        probe=lambda _port: False, sleep=lambda _s: None)
    supervisor._stop_event.set()
    child = types.SimpleNamespace(poll=lambda: None)
    started = time.monotonic()
    assert supervisor._wait_ready(child) is False
    assert time.monotonic() - started < 1.0
