"""Streamable HTTP transport suite of the Portal_MCP_Server
(`mcp_portal.handler.build_asgi_app()` over `httpx2.ASGITransport`).

Spec: .kiro/specs/mcp-servers/ (design.md §2 `handler.py` and
`discovery.py` rows, D26, D27, H2; Data Models "Protected Resource
Metadata" and "Authorization Server Metadata facade"; Testing Strategy row
"16.1 over HTTP, 2.4, 7.3, 7.5"; tasks.md 7.2). Requirements 2.2, 2.4, 7.3,
7.5, 16.1, 16.2.

The real SDK client transport (`streamable_http_client`) and raw requests
against a fresh root app per run, with RS256 tokens minted against an
in-memory key set. The 401 challenge asserted here is the SDK's behaviour
upstream of API Gateway; the deployed header is the smoke check's (task
18.2). The verifier itself refuses a Cognito token without the required
scope with a 401 (task 3.4, its `scope` rule, audited by
`test_mcp_auth_failure_audit.py`); the 403 `insufficient_scope` case
therefore feeds `RequireAuthMiddleware` a verified token without the scope,
which is the second check every endpoint route carries.
"""
import json
import sys

import anyio
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from mcp_types.jsonrpc import HEADER_MISMATCH, UNSUPPORTED_PROTOCOL_VERSION

import _portal_harness as h

PRM = '/.well-known/oauth-protected-resource'
AS_METADATA = '/.well-known/oauth-authorization-server'
ENDPOINT_PATHS = ['/mcp'] + [f'/mcp/{v}' for v in h.VIEWS]


@pytest.fixture
def portal(aws_stack, monkeypatch):
    monkeypatch.delenv('MCP_SERVER_VERSION', raising=False)
    monkeypatch.delenv('PORTAL_REGISTRY_ENFORCED', raising=False)
    handler = h.load_handler(monkeypatch)
    fake = h.install(monkeypatch)
    fake.answer = lambda _arn, _event: (200, {'devices': [{'device_id': 'fake-device-1'}]})
    shared = sys.modules['shared_utils']
    monkeypatch.setattr(shared.rbac_manager, 'has_permission', lambda *_a, **_k: True)
    yield handler, fake, monkeypatch
    h.unload_handler()


def raw(handler, *requests):
    return h.raw_requests(handler.build_asgi_app, list(requests))


def bearer_headers(token):
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream'}


# --------------------------------------------------------- discovery

def test_protected_resource_metadata_documents(portal):
    handler, _, _ = portal
    paths = [PRM] + [f'{PRM}{p}' for p in ENDPOINT_PATHS]
    responses = raw(handler, *[('GET', p, None, None) for p in paths])
    resources = []
    for path, response in zip(paths, responses):
        assert response.status_code == 200, path
        assert response.headers['cache-control'] == 'public, max-age=3600'
        document = response.json()
        assert document['authorization_servers'] == [h.ORIGIN]
        assert document['scopes_supported'] == [h.SCOPE]
        assert document['bearer_methods_supported'] == ['header']
        resources.append(document['resource'])
    # Root = canonical; each suffixed document names its own endpoint URL.
    assert resources == [h.CANONICAL, h.CANONICAL] + [
        f'{h.CANONICAL}/{v}' for v in h.VIEWS]


def test_authorization_server_facade(portal):
    handler, _, _ = portal
    [response] = raw(handler, ('GET', AS_METADATA, None, None))
    assert response.status_code == 200
    document = response.json()
    # String equality, no trailing slash (Requirement 7.5).
    assert document['issuer'] == h.ORIGIN and not document['issuer'].endswith('/')
    assert document['authorization_endpoint'] == f'{h.HOSTED_UI}/oauth2/authorize'
    assert document['token_endpoint'] == f'{h.HOSTED_UI}/oauth2/token'
    assert document['revocation_endpoint'] == f'{h.HOSTED_UI}/oauth2/revoke'
    assert document['jwks_uri'] == f'{h.ISSUER}/.well-known/jwks.json'
    assert document['code_challenge_methods_supported'] == ['S256']


@pytest.mark.parametrize('method', ['OPTIONS', 'POST'])
@pytest.mark.parametrize('path', [PRM, f'{PRM}/mcp/devices', AS_METADATA])
def test_discovery_routes_are_get_only(portal, method, path):
    handler, _, _ = portal
    [response] = raw(handler, (method, path, {'Content-Type': 'application/json'},
                               b'{}' if method == 'POST' else None))
    assert response.status_code == 405


# ------------------------------------------------------ authentication

@pytest.mark.parametrize('path', ENDPOINT_PATHS)
def test_post_without_a_token_is_challenged(portal, path):
    handler, fake, _ = portal
    [response] = raw(handler, ('POST', path, {'Content-Type': 'application/json'}, b'{}'))
    assert response.status_code == 401
    challenge = response.headers['www-authenticate']
    assert challenge.startswith('Bearer ')
    assert 'error="invalid_token"' in challenge
    assert f'resource_metadata="{h.ORIGIN}{PRM}{path}"' in challenge
    assert fake.calls == []


def test_token_in_a_query_parameter_is_ignored(portal):
    handler, fake, _ = portal
    _, token = h.signed_token()
    [response] = raw(handler, ('POST', f'/mcp/devices?access_token={token}',
                               {'Content-Type': 'application/json'}, b'{}'))
    assert response.status_code == 401
    assert fake.calls == []


def test_verified_token_without_the_scope_is_insufficient_scope(portal):
    handler, fake, monkeypatch = portal
    without_scope = AccessToken(token='fake-token', client_id=h.CLIENT_ID, scopes=['openid'],
                                subject='sub-no-scope', claims=h.claims_for('sub-no-scope'))
    monkeypatch.setattr(handler.VERIFIER, 'verify', lambda _token: without_scope)
    for path in ENDPOINT_PATHS:
        [response] = raw(handler, ('POST', path, bearer_headers('anything'), b'{}'))
        assert response.status_code == 403, path
        assert 'error="insufficient_scope"' in response.headers['www-authenticate']
    assert fake.calls == []


def test_cognito_token_without_the_scope_is_refused_by_the_verifier(portal):
    handler, fake, _ = portal
    _, token = h.signed_token(scope='openid')
    [response] = raw(handler, ('POST', '/mcp/devices', bearer_headers(token), b'{}'))
    assert response.status_code == 401
    assert fake.calls == []


# -------------------------------------------------- full client session

def test_client_session_lists_and_calls_without_redirects_or_sessions(portal):
    handler, fake, _ = portal
    _, token = h.signed_token()
    responses = []

    async def record(response):
        responses.append(response)

    async def main():
        app = handler.build_asgi_app()
        out = {}
        async with h.http_client(app, {'Authorization': f'Bearer {token}'},
                                 {'response': [record]}) as http:
            for view in (None, 'devices'):
                async with Client(streamable_http_client(h.endpoint_url(view),
                                                         http_client=http)) as client:
                    tools = await client.list_tools()
                    result = await client.call_tool('portal_devices_list',
                                                    {'usecase_id': 'uc-fake-1'})
                    out[view] = (client.protocol_version, tools, result)
        return out

    out = anyio.run(main)
    for view, (version, tools, result) in out.items():
        assert version == '2026-07-28'
        assert any(t.name == 'portal_devices_list' for t in tools.tools)
        assert result.is_error is False
        assert result.structured_content['result']['devices'] == [
            {'device_id': 'fake-device-1'}]
    assert len(fake.calls) == 2
    assert {r.request.url.path for r in responses} == {'/mcp', '/mcp/devices'}
    for response in responses:
        assert response.status_code == 200, response.request.url
        assert 'location' not in response.headers
        assert 'mcp-session-id' not in response.headers
        assert response.request.headers['mcp-protocol-version'] == '2026-07-28'


def test_get_and_delete_on_an_endpoint_are_405(portal):
    handler, _, _ = portal
    _, token = h.signed_token()
    responses = raw(handler, ('GET', '/mcp', {'Authorization': f'Bearer {token}'}, None),
                    ('GET', '/mcp/devices', None, None),
                    ('DELETE', '/mcp', {'Authorization': f'Bearer {token}'}, None))
    assert [r.status_code for r in responses] == [405, 405, 405]


# ------------------------------------------------- transport security

def captured_tools_list(handler, token):
    return h.captured_request(handler.build_asgi_app, token, 'tools/list',
                              lambda client: client.list_tools(), view='devices')


def test_origin_and_host_are_checked(portal):
    handler, _, monkeypatch = portal
    _, token = h.signed_token()
    headers, body = captured_tools_list(handler, token)
    responses = raw(
        handler,
        ('POST', '/mcp/devices', headers, body),
        ('POST', '/mcp/devices', dict(headers, Origin='https://fake-ide.example'), body),
        ('POST', '/mcp/devices', dict(headers, Origin=h.ORIGIN), body),
        ('POST', '/mcp/devices', dict(headers, Host='elsewhere.example'), body))
    assert [r.status_code for r in responses] == [200, 403, 403, 421]
    # MCP_ALLOWED_ORIGINS (empty by default) admits exactly the named origins.
    monkeypatch.setenv('MCP_ALLOWED_ORIGINS', 'https://fake-ide.example')
    responses = raw(
        handler,
        ('POST', '/mcp/devices', dict(headers, Origin='https://fake-ide.example'), body),
        ('POST', '/mcp/devices', dict(headers, Origin='https://other-ide.example'), body))
    assert [r.status_code for r in responses] == [200, 403]


def _with_envelope_version(body: bytes, version: str) -> bytes:
    message = json.loads(body)
    message['params']['_meta']['io.modelcontextprotocol/protocolVersion'] = version
    return json.dumps(message).encode()


def test_mcp_protocol_version_header(portal):
    handler, _, _ = portal
    _, token = h.signed_token()
    headers, body = captured_tools_list(handler, token)
    assert headers['mcp-protocol-version'] == '2026-07-28'
    unknown = '2099-01-01'
    no_header = {k: v for k, v in headers.items() if k != 'mcp-protocol-version'}
    responses = raw(
        handler,
        # header disagrees with the request envelope
        ('POST', '/mcp/devices', dict(headers, **{'mcp-protocol-version': unknown}), body),
        # a version the server does not serve, in both the header and the envelope
        ('POST', '/mcp/devices', dict(headers, **{'mcp-protocol-version': unknown}),
         _with_envelope_version(body, unknown)),
        # no header: answered on the handshake-era path (revision 2025-03-26 rule)
        ('POST', '/mcp/devices', no_header, body))
    mismatch, unsupported, handshake = responses
    assert mismatch.status_code == 400
    assert mismatch.json()['error']['code'] == HEADER_MISMATCH
    assert unsupported.status_code == 400
    error = unsupported.json()['error']
    assert error['code'] == UNSUPPORTED_PROTOCOL_VERSION
    assert error['data'] == {'supported': ['2026-07-28'], 'requested': unknown}
    assert handshake.status_code == 200
    assert 'tools' in handshake.json()['result']
