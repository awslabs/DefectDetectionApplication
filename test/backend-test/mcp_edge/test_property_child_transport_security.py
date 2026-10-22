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
"""Property test: Edge child transport security.

**Feature: mcp-servers, Property 18: Edge child transport security**

Spec: .kiro/specs/mcp-servers/ (design.md §5 ``child/server.py``, Testing
Strategy row "12.5 / child transport security", Correctness Property 18;
tasks.md 11.12). Validates: Requirements 12.5, 9.1.

For any ``Origin`` (absent, or any scheme/host/port, the loopback ones
included), any ``Host``, any body size and any ``--allowed-origins`` list,
the child's real Starlette app (``build_app`` with the production
``TransportSecuritySettings``, over ``httpx2.ASGITransport``) answers

* 413 when the body exceeds 1 MiB (checked first, before any header);
* else 421 when ``Host`` is neither ``127.0.0.1:5790`` nor ``localhost:5790``;
* else 403 when an ``Origin`` is present and not in the allowed list (any
  ``Origin`` with the default empty list);
* else exactly what a proxied request gets: 200 and the same JSON-RPC
  response bytes as the baseline request the SDK client sends.

The request is a captured SDK ``tools/list`` (no Backend_API call), its JSON
body padded with whitespace to the drawn size.
"""
import anyio
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

import _child_harness as h
from mcp_edge.child import server

SETTINGS = settings(max_examples=100, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow,
                                           HealthCheck.function_scoped_fixture])
MAX_BODY = server.MAX_REQUEST_BODY_BYTES
VALID_HOSTS = ('127.0.0.1:5790', 'localhost:5790')

_label = st.text(alphabet='abcdefghijklmnopqrstuvwxyz0123456789-', min_size=1, max_size=12)
_port = st.one_of(st.none(), st.sampled_from([80, 443, 5000, 5443, 5790, 8080]),
                  st.integers(1, 65535))
HOSTNAMES = st.one_of(st.sampled_from(['127.0.0.1', 'localhost', '10.0.0.5', '0.0.0.0',
                                       '[::1]', 'device.example.com']),
                      _label.map(lambda s: f'{s}.example'))


def _authority(host, port):
    return host if port is None else f'{host}:{port}'


ORIGINS = st.builds(
    lambda scheme, host, port: f'{scheme}://{_authority(host, port)}',
    st.sampled_from(['http', 'https', 'ftp', 'chrome-extension', 'null-scheme']),
    HOSTNAMES, _port)
HOSTS = st.one_of(st.sampled_from(VALID_HOSTS),
                  st.builds(_authority, HOSTNAMES, _port))
SIZES = st.one_of(st.just(None),                                   # the captured body
                  st.integers(1, 4096).map(lambda n: ('pad', n)),
                  st.sampled_from([MAX_BODY, MAX_BODY + 1, MAX_BODY + 4096]).map(
                      lambda n: ('total', n)))


@st.composite
def cases(draw):
    allowed = draw(st.lists(ORIGINS, max_size=3, unique=True))
    origin = draw(st.one_of(st.none(), ORIGINS,
                            st.sampled_from(allowed) if allowed else st.nothing(),
                            st.sampled_from(['http://127.0.0.1:5000',
                                             'http://localhost:5790'])))
    return allowed, origin, draw(HOSTS), draw(SIZES)


def _body(base: bytes, size) -> bytes:
    if size is None:
        return base
    kind, n = size
    if kind == 'pad':
        return base + b' ' * n
    return base + b' ' * max(0, n - len(base))


def _send(app, headers: dict, body: bytes):
    async def main():
        async with h.http_session(app) as http:
            response = await http.request('POST', '/mcp', headers=headers, content=body)
            return response.status_code, response.content
    return anyio.run(main)


_baseline = {}


def baseline():
    """The status and bytes a proxied ``tools/list`` gets (no ``Origin``,
    loopback ``Host``)."""
    if not _baseline:
        headers, body = h.captured_tools_list()
        app = server.build_app(h.options())
        _baseline['result'] = _send(app, dict(headers), body)
    return _baseline['result']


def expected_status(allowed, origin, host, size: int) -> int:
    if size > MAX_BODY:
        return 413
    if host not in VALID_HOSTS:
        return 421
    if origin and origin not in allowed:
        return 403
    return 200


@SETTINGS
@given(case=cases())
def test_child_transport_security(case):
    allowed, origin, host, size = case
    captured_headers, captured_body = h.captured_tools_list()
    base_status, base_content = baseline()
    assert base_status == 200

    body = _body(captured_body, size)
    headers = {k: v for k, v in captured_headers.items() if k.lower() != 'origin'}
    headers['Host'] = host
    if origin is not None:
        headers['Origin'] = origin
    app = server.build_app(h.options(allowed_origins=tuple(allowed)))
    status, content = _send(app, headers, body)
    event(f'status={status}')

    assert status == expected_status(allowed, origin, host, len(body)), (
        allowed, origin, host, len(body))
    if status == 200:
        assert content == base_content


def test_default_list_refuses_both_loopback_origins():
    """The fixed cases of the property: with no ``--allowed-origins`` the two
    loopback origins the SDK defaults would admit are refused."""
    headers, body = h.captured_tools_list()
    for origin in ('http://127.0.0.1:5000', 'http://localhost:5790'):
        app = server.build_app(h.options())     # one lifespan per app
        status, _ = _send(app, dict(headers, Origin=origin), body)
        assert status == 403
