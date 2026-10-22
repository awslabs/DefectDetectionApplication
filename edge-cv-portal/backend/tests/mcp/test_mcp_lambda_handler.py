"""Lambda entry point of the Portal_MCP_Server (`mcp_portal/handler.py`).
Spec: .kiro/specs/mcp-servers/ (design.md §2 `handler.py` and `server.py`
rows, Decision point 2 "Root-app composition", D26, Testing Strategy row
"H3 regression"; tasks.md 6.10). Requirements 7.1, 7.2, 7.3, 1.7, 15.3, 6.2.

`handler(event, context)` builds a new root app (fresh session managers)
per invocation and serves the event through Mangum with `lifespan='auto'`.
This test sends HTTP API v2 events through it three times in one process
(twice the same tool call, then other paths) and checks that every call
answers as expected with no `RuntimeError`/500, that a tool call succeeds
while the lifespan is open, and that `SERVERS`/`VERIFIER` are the same
objects across invocations. Keys are generated at test time; every
identifier is visibly fake.
"""
import base64
import importlib
import io
import json
import sys
import time
import uuid
from types import SimpleNamespace

import anyio
import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

ISSUER = 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_FAKEPOOL1'
CLIENT_ID = 'fake-mcp-client-id'
API_HOST = 'fakeapi000.execute-api.us-east-1.amazonaws.com'
ORIGIN = f'https://{API_HOST}'
CANONICAL = f'{ORIGIN}/mcp'
SCOPE = f'{CANONICAL}/portal.tools'
VIEWS = ('data', 'models', 'devices', 'workflows', 'admin')
KID = 'fake-kid-1'
ENV = {
    'COGNITO_ISSUER': ISSUER,
    'COGNITO_USER_POOL_ID': 'us-east-1_FAKEPOOL1',
    'COGNITO_HOSTED_UI_ORIGIN': 'https://fake-portal.auth.us-east-1.amazoncognito.com',
    'MCP_CLIENT_ID': CLIENT_ID,
    'MCP_REQUIRED_SCOPE': SCOPE,
    'MCP_ALLOWED_AUDIENCES': ','.join([CANONICAL] + [f'{CANONICAL}/{v}' for v in VIEWS]),
    'MCP_AS_ISSUER': ORIGIN,
    'MCP_CANONICAL_URL': CANONICAL,
    'MCP_API_HOST': API_HOST,
    'MCP_ALLOWED_ORIGINS': '',
    'MCP_MUTATING_TOOLS_ENABLED': 'false',
    'MCP_BACKEND_FUNCTIONS_PARAMETER': '/dda/mcp/backend-functions',
}
POOL_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
POOL_JWK = jwt.PyJWK.from_dict(
    {**jwt.algorithms.RSAAlgorithm.to_jwk(POOL_KEY.public_key(), as_dict=True),
     'kid': KID, 'alg': 'RS256', 'use': 'sig'})


class FakeJwksClient:
    def get_signing_key_from_jwt(self, _token):
        return POOL_JWK


class FakeLambda:
    """`lambda:Invoke` of the backing handler: always 200 with no devices."""

    def __init__(self):
        self.events = []

    def invoke(self, **kwargs):
        self.events.append(json.loads(kwargs['Payload']))
        body = json.dumps({'devices': [{'device_id': 'fake-device-1'}]})
        return {'StatusCode': 200,
                'Payload': io.BytesIO(json.dumps({'statusCode': 200, 'body': body}).encode())}


class FakeContext:
    aws_request_id = 'fake-lambda-request'

    def get_remaining_time_in_millis(self):
        return 28_000


def access_token():
    now = int(time.time())
    payload = {'sub': str(uuid.uuid4()), 'iss': ISSUER, 'exp': now + 600, 'iat': now - 5,
               'token_use': 'access', 'client_id': CLIENT_ID, 'scope': SCOPE,
               'jti': str(uuid.uuid4()), 'username': 'fake-user'}
    return jwt.encode(payload, POOL_KEY, algorithm='RS256', headers={'kid': KID})


@pytest.fixture
def portal(aws_stack, monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    for name in ('mcp_portal.discovery', 'mcp_portal.handler'):
        sys.modules.pop(name, None)
    # A fresh import rebinds the package attribute, so `from mcp_portal
    # import discovery` in the handler sees this environment.
    importlib.import_module('mcp_portal.discovery')
    handler = importlib.import_module('mcp_portal.handler')
    from mcp_portal import dispatch, principal
    shared = sys.modules['shared_utils']
    monkeypatch.setattr(handler.VERIFIER, 'jwks_client', FakeJwksClient())
    monkeypatch.setattr(principal, 'lookup_email', lambda _sub, _username: 'person@example.com')
    monkeypatch.setattr(shared, '_REQUEST_AUDIT_CONTEXT', None)
    monkeypatch.setattr(shared.rbac_manager, 'has_permission', lambda *_a, **_k: True)
    fake_lambda = FakeLambda()
    monkeypatch.setattr(dispatch, 'LAMBDA', fake_lambda)
    monkeypatch.setattr(dispatch, 'SSM', SimpleNamespace(get_parameter=lambda **_: {
        'Parameter': {'Value': json.dumps({'devices': 'arn:fn:devices'})}}))
    dispatch.reset_backend_functions()
    yield SimpleNamespace(handler=handler, dispatch=dispatch, fake_lambda=fake_lambda)
    dispatch.reset_backend_functions()
    monkeypatch.setattr(dispatch, 'LAMBDA_CONTEXT', None)
    for name in ('mcp_portal.discovery', 'mcp_portal.handler'):
        sys.modules.pop(name, None)


def captured_tool_call(handler, token, view, tool, arguments):
    """Call one tool through the SDK client against `build_asgi_app()` (the
    lifespan open) and capture the request it sent, for the Lambda events."""
    app = handler.build_asgi_app()
    captured = {}

    async def hook(request):
        if request.method == 'POST':
            try:
                method = json.loads(request.content).get('method')
            except ValueError:
                method = None
            if method == 'tools/call':
                captured['headers'] = {k: v for k, v in request.headers.items()
                                       if k.lower() not in ('content-length',)}
                captured['body'] = request.content.decode()

    async def main():
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app), base_url=ORIGIN,
                    headers={'Authorization': f'Bearer {token}'},
                    event_hooks={'request': [hook]}) as http:
                async with Client(streamable_http_client(f'{CANONICAL}/{view}',
                                                         http_client=http)) as client:
                    return await client.call_tool(tool, arguments)

    result = anyio.run(main)
    return result, captured


def v2_event(method, path, headers=None, body=None):
    headers = {'host': API_HOST, 'user-agent': 'fake-mcp-client/0.1',
               **{k.lower(): v for k, v in (headers or {}).items()}}
    return {
        'version': '2.0', 'routeKey': f'{method} {path}', 'rawPath': path,
        'rawQueryString': '', 'headers': headers, 'isBase64Encoded': False,
        'body': body,
        'requestContext': {
            'accountId': 'anonymous', 'apiId': 'fakeapi000', 'domainName': API_HOST,
            'requestId': f'fake-apigw-{uuid.uuid4().hex[:8]}', 'stage': '$default',
            'routeKey': f'{method} {path}', 'timeEpoch': int(time.time() * 1000),
            'http': {'method': method, 'path': path, 'protocol': 'HTTP/1.1',
                     'sourceIp': '203.0.113.9', 'userAgent': 'fake-mcp-client/0.1'}},
    }


def body_of(response):
    raw = response.get('body') or ''
    if response.get('isBase64Encoded'):
        raw = base64.b64decode(raw).decode()
    return json.loads(raw) if raw else None


def test_three_invocations_in_one_process(portal):
    handler = portal.handler
    servers, verifier = handler.SERVERS, handler.VERIFIER
    token = access_token()

    result, captured = captured_tool_call(handler, token, 'devices', 'portal_devices_list',
                                          {'usecase_id': 'uc-fake-1'})
    assert result.is_error is False
    assert result.structured_content['result']['devices'] == [{'device_id': 'fake-device-1'}]
    assert captured['body'] and 'portal_devices_list' in captured['body']

    call = v2_event('POST', '/mcp/devices', captured['headers'], captured['body'])
    responses = []
    for _ in range(2):
        responses.append(handler.handler(call, FakeContext()))
        assert handler.SERVERS is servers and handler.VERIFIER is verifier
    for response in responses:
        assert response['statusCode'] == 200, response
        answer = body_of(response)
        assert answer['result']['isError'] is False
        assert answer['result']['structuredContent']['result']['devices'] == [
            {'device_id': 'fake-device-1'}]
        assert 'mcp-session-id' not in {k.lower() for k in response.get('headers', {})}
    # Each invocation dispatched exactly once, with the Lambda context set.
    assert len(portal.fake_lambda.events) == 3
    assert portal.dispatch.LAMBDA_CONTEXT is not None

    discovery = handler.handler(v2_event('GET', '/.well-known/oauth-protected-resource'),
                                FakeContext())
    assert discovery['statusCode'] == 200
    assert body_of(discovery)['resource'] == CANONICAL
    unauthenticated = handler.handler(
        v2_event('POST', '/mcp', {'content-type': 'application/json'}, '{}'), FakeContext())
    assert unauthenticated['statusCode'] == 401
    assert handler.SERVERS is servers and handler.VERIFIER is verifier


def test_six_endpoints_and_middleware_order(portal):
    from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
    from starlette.middleware.authentication import AuthenticationMiddleware
    from mcp_portal.auth_audit import AuthFailureAuditMiddleware

    app = portal.handler.build_asgi_app()
    posts = sorted(r.path for r in app.routes if r.methods and 'POST' in r.methods)
    assert posts == ['/mcp'] + [f'/mcp/{v}' for v in sorted(VIEWS)]
    assert [m.cls for m in app.user_middleware] == [
        AuthFailureAuditMiddleware, AuthenticationMiddleware, AuthContextMiddleware]
    assert set(portal.handler.SERVERS) == {None, *VIEWS}
    assert portal.handler.SERVERS['devices'].name == 'dda-portal-mcp/devices'
    assert portal.handler.SERVERS[None].name == 'dda-portal-mcp'
    # Each build composes fresh session managers.
    other = portal.handler.build_asgi_app()
    first = {r.path: r.app for r in app.routes if r.path.startswith('/mcp')}
    second = {r.path: r.app for r in other.routes if r.path.startswith('/mcp')}
    assert all(first[p] is not second[p] for p in first)
