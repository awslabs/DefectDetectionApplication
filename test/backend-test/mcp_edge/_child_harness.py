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
"""Test harness of the Edge MCP child's server suites (test-only).

``FakeBackend`` is the Backend_API behind an ``httpx2.MockTransport``: it
answers ``GET /local-auth/whoami`` with a configurable principal (or 401) and
every other route from a table, recording each request with its per-request
timeout. ``in_memory`` runs a coroutine against the child's ``Server`` with
the SDK's in-process ``Client``; ``over_http`` runs it against the real
Starlette app over ``httpx2.ASGITransport`` with the app's lifespan entered.
``minimal_args`` builds a schema-valid argument object from a spec's
required properties.
"""
import base64
import json
import re
from contextlib import asynccontextmanager
from typing import Callable, Dict, Optional, Tuple

import anyio
import httpx2
import jsonschema
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_edge.child import dispatch, server

OPERATOR = {'username': 'op1', 'role': 'Operator', 'source': 'local_session',
            'expiresAt': 1_900_000_000}
ANONYMOUS = {'username': 'anonymous', 'role': None, 'source': 'anonymous'}
VIEWER = {'username': 'viewer1', 'role': 'Viewer', 'source': 'local_session'}
ALL_PHASES = (1, 2, 3)
BASE_URL = 'http://127.0.0.1:5790'
MCP_URL = f'{BASE_URL}/mcp'


def jpeg_b64(decoded_bytes: int) -> str:
    return base64.b64encode(b'\xff\xd8\xff\xe0' + b'\x00' * (decoded_bytes - 4)).decode()


def png_b64(decoded_bytes: int) -> str:
    magic = b'\x89PNG\r\n\x1a\n'
    return base64.b64encode(magic + b'\x00' * (decoded_bytes - len(magic))).decode()


class FakeBackend:
    """The Backend_API: whoami plus a route table of
    ``(method, path) -> response | callable(request) -> response``."""

    def __init__(self, principal: Optional[dict] = None, routes: Optional[Dict] = None):
        self.principal = OPERATOR if principal is None else principal
        self.routes = dict(routes or {})
        self.requests = []          # every non-whoami request
        self.whoami_requests = []
        self.timeouts = []          # (path, read timeout) per non-whoami request

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == '/local-auth/whoami':
            self.whoami_requests.append(request)
            if self.principal == 401:
                return httpx2.Response(401, json={'detail': 'Unauthorized'})
            return httpx2.Response(200, json=self.principal)
        self.requests.append(request)
        timeout = request.extensions.get('timeout', {})
        self.timeouts.append((request.url.path, timeout.get('read')))
        answer = self.routes.get((request.method, request.url.path))
        if answer is None:
            answer = self.routes.get('*')
        if callable(answer):
            return answer(request)
        if answer is None:
            return httpx2.Response(200, json={})
        return answer

    def install(self):
        dispatch.set_client(httpx2.Client(base_url='https://127.0.0.1:5443',
                                          transport=httpx2.MockTransport(self)))
        return self

    def body_of(self, index: int = -1):
        content = self.requests[index].content
        return json.loads(content) if content else None


def json_answer(body, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, json=body)


def options(**overrides) -> server.Options:
    return server.Options(**overrides)


def in_memory(coroutine_factory: Callable, opts: Optional[server.Options] = None,
              specs=None, records: Optional[list] = None):
    """``coroutine_factory(client)`` against the child's ``Server`` in process."""
    srv = server.make_server(opts or options(), specs=specs)

    async def main():
        async with Client(srv) as client:
            return await coroutine_factory(client)

    return _run(main, records)


@asynccontextmanager
async def http_session(app, headers: Optional[dict] = None):
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url=BASE_URL, headers=headers or {}) as http:
            yield http


def over_http(coroutine_factory: Callable, opts: Optional[server.Options] = None,
              headers: Optional[dict] = None, mode: str = 'auto', specs=None,
              records: Optional[list] = None):
    """``coroutine_factory(client)`` against the real app over ASGI."""
    opts = opts or options()
    app = server.build_app(opts, server=server.make_server(opts, specs=specs))

    async def main():
        async with http_session(app, headers) as http:
            async with Client(streamable_http_client(MCP_URL, http_client=http),
                              mode=mode) as client:
                return await coroutine_factory(client)

    return _run(main, records)


def _run(main, records):
    if records is None:
        return anyio.run(main)
    original = server.emit
    server.emit = records.append
    try:
        return anyio.run(main)
    finally:
        server.emit = original


def raw_requests(app, requests) -> list:
    """Send ``(method, path, headers, content)`` tuples over ASGI; statuses."""
    async def main():
        out = []
        async with http_session(app) as http:
            for method, path, headers, content in requests:
                response = await http.request(method, path, headers=headers, content=content)
                out.append(response.status_code)
        return out

    return anyio.run(main)


_captured: Dict[str, Tuple[dict, bytes]] = {}


def captured_tools_list() -> Tuple[dict, bytes]:
    """The headers and body the SDK client sends for one ``tools/list`` over
    Streamable HTTP, captured once and replayed by the transport tests."""
    if 'tools/list' in _captured:
        return _captured['tools/list']
    app = server.build_app(options())

    async def hook(request: httpx2.Request):
        if request.method == 'POST':
            body = request.content
            try:
                method = json.loads(body).get('method')
            except ValueError:
                method = None
            if method == 'tools/list' and 'tools/list' not in _captured:
                headers = {k: v for k, v in request.headers.items()
                           if k.lower() not in ('host', 'content-length')}
                _captured['tools/list'] = (headers, bytes(body))

    async def main():
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                          base_url=BASE_URL,
                                          event_hooks={'request': [hook]}) as http:
                async with Client(streamable_http_client(MCP_URL, http_client=http)) as client:
                    await client.list_tools()

    anyio.run(main)
    return _captured['tools/list']


# ------------------------------------------------------------ arguments

def _sample(prop: dict):
    if 'const' in prop:
        return prop['const']
    if 'enum' in prop:
        return prop['enum'][0]
    kind = prop.get('type')
    if isinstance(kind, list):
        kind = kind[0]
    if kind == 'string':
        pattern = prop.get('pattern', '')
        if 'rtsp' in pattern:
            return 'rtsp://10.0.0.9:554/live'
        return 'x1'
    if kind == 'integer':
        return prop.get('minimum', 1)
    if kind == 'number':
        return prop.get('minimum', 1)
    if kind == 'boolean':
        return False
    if kind == 'array':
        return [_sample(prop.get('items', {})) for _ in range(prop.get('minItems', 0) or 1)]
    if kind == 'object':
        props = prop.get('properties', {})
        return {name: _sample(props[name]) for name in prop.get('required', ())}
    return 'x1'


def minimal_args(spec, **overrides) -> dict:
    """A schema-valid argument object with only the required properties."""
    schema = spec.input_schema
    required = list(schema.get('required', ()))
    for group in ('oneOf', 'anyOf'):
        if schema.get(group):
            required += [r for r in schema[group][0].get('required', ()) if r not in required]
    args = {name: _sample(schema['properties'][name]) for name in required}
    args.update(overrides)
    jsonschema.Draft202012Validator(schema).validate(args)
    return args


def confirm_for(spec, args: dict) -> Optional[str]:
    if spec.confirm_literal is not None:
        return spec.confirm_literal
    target = spec.confirm_target
    if isinstance(target, tuple):
        target = target[0]
    match = re.match(r'^([a-z_][a-z0-9_]*)\[(\d+)\]$', target or '')
    if match:
        return args[match.group(1)][int(match.group(2))]
    return args.get(target)
