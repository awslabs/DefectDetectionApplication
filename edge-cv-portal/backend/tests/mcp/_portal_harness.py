"""Test harness of the portal MCP protocol, transport, RBAC and guardrail
suites (test-only; tasks.md 7.1-7.5).

* `FakeLambda` stands in for `lambda:Invoke` of the backing handlers: it
  records every `(function ARN, event)` and answers from a callable.
* `install(monkeypatch, ...)` points `mcp_portal.dispatch` at a fake Lambda
  and a dispatch map naming every backend key, fixes the principal's email
  lookup, and sets the delivered phases and the mutating switch.
* `access_token(sub)` is the `AccessToken` the verifier would return;
  `in_memory(view, factory, token)` runs `factory(client)` against
  `build_server(view)` with the SDK's in-process `Client` and the principal
  injected through `_principal.set_principal`.
* `load_handler(monkeypatch)` imports `mcp_portal.handler` under a fake
  deployment environment with an in-memory key set, and `signed_token`
  mints RS256 tokens for it; `over_http` runs `factory(client)` against a
  fresh `build_asgi_app()` over `httpx2.ASGITransport` with the lifespan
  open (each root app's session managers run once).

Keys are generated at import time; every identifier is visibly fake.
"""
import contextlib
import importlib
import io
import json
import sys
import time
import uuid
from types import SimpleNamespace
from typing import Callable, Optional

import anyio
import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken

from _principal import set_principal

ISSUER = 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_FAKEPOOL1'
CLIENT_ID = 'fake-mcp-client-id'
API_HOST = 'fakeapi000.execute-api.us-east-1.amazonaws.com'
ORIGIN = f'https://{API_HOST}'
CANONICAL = f'{ORIGIN}/mcp'
SCOPE = f'{CANONICAL}/portal.tools'
HOSTED_UI = 'https://fake-portal.auth.us-east-1.amazoncognito.com'
VIEWS = ('data', 'models', 'devices', 'workflows', 'admin')
ALL_PHASES = (1, 2, 3)
KID = 'fake-kid-1'
ENV = {
    'COGNITO_ISSUER': ISSUER,
    'COGNITO_USER_POOL_ID': 'us-east-1_FAKEPOOL1',
    'COGNITO_HOSTED_UI_ORIGIN': HOSTED_UI,
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
EMAIL = 'person@example.com'

POOL_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
POOL_JWK = jwt.PyJWK.from_dict(
    {**jwt.algorithms.RSAAlgorithm.to_jwk(POOL_KEY.public_key(), as_dict=True),
     'kid': KID, 'alg': 'RS256', 'use': 'sig'})


class FakeJwksClient:
    def get_signing_key_from_jwt(self, _token):
        return POOL_JWK


def ok_answer(_arn, _event):
    return 200, {'ok': True}


class FakeLambda:
    """`lambda:Invoke`: records `(arn, event)` and answers
    `answer(arn, event) -> (status, body)` as a proxy response."""

    def __init__(self, answer: Callable = ok_answer):
        self.answer = answer
        self.calls = []

    @property
    def events(self):
        return [event for _arn, event in self.calls]

    def invoke(self, **kwargs):
        event = json.loads(kwargs['Payload'])
        self.calls.append((kwargs['FunctionName'], event))
        status, body = self.answer(kwargs['FunctionName'], event)
        payload = {'statusCode': status, 'body': json.dumps(body)}
        return {'StatusCode': 200, 'Payload': io.BytesIO(json.dumps(payload).encode())}


def backend_arn(key: str) -> str:
    return f'arn:fake:function:{key}'


def install(monkeypatch, fake_lambda: Optional[FakeLambda] = None, phases=(1,),
            mutating: bool = False) -> FakeLambda:
    """Wire `mcp_portal` to a fake Lambda; returns it."""
    from mcp_portal import catalog, dispatch, principal, server

    fake_lambda = fake_lambda or FakeLambda()
    shared = sys.modules['shared_utils']
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('MCP_MUTATING_TOOLS_ENABLED', 'true' if mutating else 'false')
    monkeypatch.setattr(server, 'DELIVERED_PHASES', tuple(phases))
    monkeypatch.setattr(principal, 'lookup_email', lambda _sub, _username: EMAIL)
    monkeypatch.setattr(shared, '_REQUEST_AUDIT_CONTEXT', None)
    monkeypatch.setattr(dispatch, 'LAMBDA', fake_lambda)
    monkeypatch.setattr(dispatch, 'LAMBDA_CONTEXT', None)
    keys = sorted({s.backend for s in catalog.CATALOG if s.backend})
    mapping = json.dumps({k: backend_arn(k) for k in keys})
    monkeypatch.setattr(dispatch, 'SSM', SimpleNamespace(
        get_parameter=lambda **_: {'Parameter': {'Value': mapping}}))
    dispatch.reset_backend_functions()
    return fake_lambda


def claims_for(sub: str, **overrides) -> dict:
    now = int(time.time())
    claims = {'sub': sub, 'iss': ISSUER, 'exp': now + 600, 'iat': now - 5,
              'token_use': 'access', 'client_id': CLIENT_ID, 'scope': SCOPE,
              'jti': str(uuid.uuid4()), 'username': f'user-{sub[:12]}'}
    claims.update(overrides)
    return claims


def access_token(sub: Optional[str] = None, **overrides) -> AccessToken:
    """The `AccessToken` `CognitoAccessTokenVerifier` returns for a token."""
    claims = claims_for(sub or f'sub-{uuid.uuid4()}', **overrides)
    return AccessToken(token=f'fake-token-{claims["jti"]}', client_id=claims['client_id'],
                       scopes=str(claims['scope']).split(), expires_at=claims['exp'],
                       subject=claims['sub'], claims=claims)


def in_memory(view, factory: Callable, token: Optional[AccessToken] = None, specs=None,
              mode: str = 'auto'):
    """`factory(client)` against `build_server(view)` in process."""
    from mcp_portal import server

    srv = server.build_server(view, specs=specs)

    async def main():
        async with Client(srv, mode=mode) as client:
            return await factory(client)

    with set_principal(token) if token is not None else contextlib.nullcontext():
        return anyio.run(main)


# ------------------------------------------------------------ HTTP path

HANDLER_MODULES = ('mcp_portal.discovery', 'mcp_portal.handler')


def load_handler(monkeypatch):
    """`mcp_portal.handler`, imported fresh under `ENV`, its verifier on the
    in-memory key set. Pair with `unload_handler()`."""
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    unload_handler()
    # A fresh import rebinds the package attribute, so `from mcp_portal
    # import discovery` in the handler sees this environment.
    importlib.import_module('mcp_portal.discovery')
    handler = importlib.import_module('mcp_portal.handler')
    monkeypatch.setattr(handler.VERIFIER, 'jwks_client', FakeJwksClient())
    return handler


def unload_handler():
    for name in HANDLER_MODULES:
        sys.modules.pop(name, None)


def signed_token(key=POOL_KEY, sub: Optional[str] = None, **overrides):
    """(sub, RS256 token) with the claims of `claims_for`."""
    claims = claims_for(sub or f'sub-{uuid.uuid4()}', **overrides)
    return claims['sub'], jwt.encode(claims, key, algorithm='RS256', headers={'kid': KID})


def endpoint_url(view=None) -> str:
    return CANONICAL if view is None else f'{CANONICAL}/{view}'


@contextlib.asynccontextmanager
async def http_client(app, headers: Optional[dict] = None, event_hooks=None):
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                      base_url=ORIGIN, headers=headers or {},
                                      event_hooks=event_hooks or {},
                                      follow_redirects=False) as http:
            yield http


def over_http(make_app: Callable, factory: Callable, bearer: Optional[str], view=None,
              mode: str = 'auto'):
    """`factory(client)` over Streamable HTTP against a fresh `make_app()`
    (a session manager runs once, so every run composes a new root app)."""
    app = make_app()
    headers = {'Authorization': f'Bearer {bearer}'} if bearer else {}

    async def main():
        async with http_client(app, headers) as http:
            async with Client(streamable_http_client(endpoint_url(view), http_client=http),
                              mode=mode) as client:
                return await factory(client)

    return anyio.run(main)


def captured_request(make_app: Callable, bearer: str, method: str, factory: Callable,
                     view=None, mode: str = 'auto'):
    """The headers and body the SDK client sends for the first JSON-RPC
    request named `method` while `factory(client)` runs (for replay)."""
    app = make_app()
    captured = {}

    async def hook(request):
        if request.method != 'POST' or 'body' in captured:
            return
        try:
            sent = json.loads(request.content).get('method')
        except ValueError:
            sent = None
        if sent == method:
            captured['headers'] = {k: v for k, v in request.headers.items()
                                   if k.lower() not in ('content-length', 'host')}
            captured['body'] = bytes(request.content)

    async def main():
        async with http_client(app, {'Authorization': f'Bearer {bearer}'},
                               {'request': [hook]}) as http:
            async with Client(streamable_http_client(endpoint_url(view), http_client=http),
                              mode=mode) as client:
                await factory(client)

    anyio.run(main)
    return captured['headers'], captured['body']


def raw_requests(make_app: Callable, requests) -> list:
    """Send `(method, path, headers, content)` tuples over ASGI to one fresh
    `make_app()`; the responses."""
    app = make_app()

    async def main():
        out = []
        async with http_client(app) as http:
            for method, path, headers, content in requests:
                out.append(await http.request(method, path, headers=headers or {},
                                              content=content))
        return out

    return anyio.run(main)
