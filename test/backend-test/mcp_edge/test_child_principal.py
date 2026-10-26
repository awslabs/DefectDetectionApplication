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
"""Child principal resolution (``mcp_edge/child/principal.py``) and the role
gate's use of it (``child/authz.py``).

Spec: .kiro/specs/mcp-servers/ (design §5 ``child/principal.py`` and
``child/authz.py``, §7 step 1, Testing Strategy row "16.2 negatives";
tasks.md 11.5). Requirements 9.1, 9.4, 9.5, 10.2-10.6, 16.2.

The Backend_API is an ``httpx2.MockTransport`` answering
``GET /local-auth/whoami`` the way the route does (``{username, role, source,
expiresAt?}`` or 401).
"""
import base64
import json

import httpx2
import pytest

from mcp_edge.child import authz, dispatch, principal
from mcp_edge.child.catalog import CATALOG

BY_NAME = {s.name: s for s in CATALOG}
READ = BY_NAME['edge_system_health_get'] if 'edge_system_health_get' in BY_NAME else next(
    s for s in CATALOG if s.read_only)
MUTATE = next(s for s in CATALOG if not s.read_only and not s.destructive)


class Backend:
    """A scripted whoami: each call pops the next (status, body)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        status, body = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return httpx2.Response(status, json=body)


@pytest.fixture
def backend():
    holder = {}

    def install(*answers):
        handler = Backend(*answers)
        dispatch.set_client(httpx2.Client(base_url='https://127.0.0.1:5443',
                                          transport=httpx2.MockTransport(handler)))
        holder['handler'] = handler
        return handler

    yield install
    dispatch.set_client(None)


def jwt_claiming(role: str) -> str:
    """A token-shaped string whose payload claims a role; the child must not
    read it."""
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')
    return f"{part({'alg': 'none'})}.{part({'role': role, 'sub': 'x'})}.sig"


SESSION = {'username': 'op1', 'role': 'Operator', 'source': 'local_session',
           'expiresAt': 1_900_000_000}


def test_whoami_is_called_with_the_caller_credential_and_request_id(backend):
    handler = backend((200, SESSION))
    result = principal.resolve_principal('Bearer session-token', 'req-7')
    assert result == principal.Principal('op1', 'Operator', 'local_session', 1_900_000_000)
    request = handler.requests[0]
    assert request.method == 'GET' and request.url.path == '/local-auth/whoami'
    assert request.headers['authorization'] == 'Bearer session-token'
    assert request.headers['x-request-id'] == 'req-7'


def test_role_is_taken_only_from_whoami(backend):
    backend((200, {'username': 'viewer1', 'role': 'Viewer', 'source': 'local_session'}))
    result = principal.resolve_principal(f'Bearer {jwt_claiming("PortalAdmin")}', 'r')
    assert result.role == 'Viewer'
    denied = authz.decide(MUTATE, result, mutating_enabled=True, args={})
    assert isinstance(denied, authz.Denied) and denied.category == 'not_authorized'


def test_missing_or_invalid_credential_is_not_authenticated(backend):
    handler = backend((401, {'detail': 'Unauthorized'}))
    for header in (None, 'Bearer nope'):
        result = principal.resolve_principal(header, 'r')
        assert isinstance(result, dispatch.ToolFailure)
        assert result.category == 'not_authenticated' and result.status == 401
    assert 'authorization' not in handler.requests[0].headers


def test_expired_token_is_refused_on_the_next_call(backend):
    backend((200, SESSION), (401, {'detail': 'Token expired'}))
    first = principal.resolve_principal('Bearer t', 'r1')
    second = principal.resolve_principal('Bearer t', 'r2')
    assert isinstance(first, principal.Principal)
    assert isinstance(second, dispatch.ToolFailure) and second.category == 'not_authenticated'


def test_disabled_account_is_refused_on_the_next_call(backend):
    backend((200, SESSION), (401, {'detail': 'Account disabled'}))
    assert isinstance(principal.resolve_principal('Bearer t', 'r1'), principal.Principal)
    result = principal.resolve_principal('Bearer t', 'r2')
    assert result.category == 'not_authenticated'


def test_open_access_is_anonymous_and_reads_only(backend):
    backend((200, {'username': 'anonymous', 'role': None, 'source': 'anonymous'}))
    result = principal.resolve_principal(None, 'r')
    assert result.source == 'anonymous' and result.role is None and result.expires_at is None
    assert authz.decide(READ, result, mutating_enabled=True) is authz.ALLOWED
    assert authz.decide(MUTATE, result, mutating_enabled=True, args={}).category == 'not_authorized'


def test_existing_token_auth_cannot_mutate(backend):
    backend((200, {'username': 'svc', 'role': None, 'source': 'existing_token_auth',
                   'expiresAt': None}))
    result = principal.resolve_principal('Bearer cloud', 'r')
    assert authz.decide(MUTATE, result, mutating_enabled=True, args={}).category == 'not_authorized'


@pytest.mark.parametrize('status,body', [(200, {'unexpected': True}), (200, ['x']),
                                         (500, {'detail': 'boom'})])
def test_unexpected_answers_are_failures(backend, status, body):
    backend((status, body))
    result = principal.resolve_principal('Bearer t', 'r')
    assert isinstance(result, dispatch.ToolFailure)
    assert result.category == 'backend_failure'


def test_unreachable_backend_is_backend_failure():
    def refuse(request):
        raise httpx2.ConnectError('refused', request=request)

    dispatch.set_client(httpx2.Client(base_url='https://127.0.0.1:5443',
                                      transport=httpx2.MockTransport(refuse)))
    try:
        result = principal.resolve_principal('Bearer t', 'r')
    finally:
        dispatch.set_client(None)
    assert result.category == 'backend_failure'


def test_mutating_gate_for_a_session_operator(backend):
    backend((200, SESSION))
    result = principal.resolve_principal('Bearer t', 'r')
    assert authz.decide(MUTATE, result, mutating_enabled=False).category == \
        'mutating_tools_disabled'
    assert authz.decide(MUTATE, result, mutating_enabled=True, args={}) is authz.ALLOWED
