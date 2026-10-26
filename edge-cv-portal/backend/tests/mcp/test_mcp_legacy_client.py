"""A handshake-era client against the Portal_MCP_Server.

Spec: .kiro/specs/mcp-servers/ (design.md Testing Strategy row "16.9 legacy
revision", Decision point 2; tasks.md 7.3). Requirements 7.2, 16.9.

`Client(streamable_http_client(...), mode="legacy")` over
`httpx2.ASGITransport` against `build_asgi_app()` (stateless, JSON
responses) completes `initialize`, `tools/list` and one `tools/call`. A
version the server does not serve, pinned in the `MCP-Protocol-Version`
header and the request envelope, is refused with the JSON-RPC
`UNSUPPORTED_PROTOCOL_VERSION` error whose data
(`UnsupportedProtocolVersionErrorData`) lists the served versions; mcp
2.3.0 has no exception class of that name, so the error is asserted by its
code and data, and the SDK client refuses to be pinned to such a version.
"""
import json
import sys

import pytest
from mcp.client import Client
from mcp_types import UnsupportedProtocolVersionErrorData
from mcp_types.jsonrpc import UNSUPPORTED_PROTOCOL_VERSION
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

import _portal_harness as h
from mcp_portal import PROTOCOL_REVISIONS

UNKNOWN = '2099-01-01'


@pytest.fixture
def portal(aws_stack, monkeypatch):
    monkeypatch.delenv('MCP_SERVER_VERSION', raising=False)
    monkeypatch.delenv('PORTAL_REGISTRY_ENFORCED', raising=False)
    handler = h.load_handler(monkeypatch)
    fake = h.install(monkeypatch)
    fake.answer = lambda _arn, _event: (200, {'devices': [{'device_id': 'fake-device-1'}]})
    shared = sys.modules['shared_utils']
    monkeypatch.setattr(shared.rbac_manager, 'has_permission', lambda *_a, **_k: True)
    yield handler, fake
    h.unload_handler()


def test_legacy_initialize_list_and_call(portal):
    handler, fake = portal
    _, token = h.signed_token()

    async def go(client):
        tools = await client.list_tools()
        result = await client.call_tool('portal_devices_list', {'usecase_id': 'uc-fake-1'})
        return client.protocol_version, client.server_info, tools, result

    version, info, tools, result = h.over_http(handler.build_asgi_app, go, token,
                                               view='devices', mode='legacy')
    assert version in HANDSHAKE_PROTOCOL_VERSIONS and version in PROTOCOL_REVISIONS
    assert info.name == 'dda-portal-mcp/devices' and info.version == '1.0.0'
    assert any(t.name == 'portal_devices_list' for t in tools.tools)
    assert result.is_error is False
    assert result.structured_content['result']['devices'] == [{'device_id': 'fake-device-1'}]
    [event] = fake.events
    assert event['mcp']['tool'] == 'portal_devices_list'


def test_legacy_initialize_sends_no_session_id(portal):
    handler, _ = portal
    _, token = h.signed_token()
    headers, body = h.captured_request(handler.build_asgi_app, token, 'initialize',
                                       lambda client: client.list_tools(), mode='legacy')
    [response] = h.raw_requests(handler.build_asgi_app, [('POST', '/mcp', headers, body)])
    assert response.status_code == 200
    assert 'mcp-session-id' not in response.headers
    assert response.json()['result']['protocolVersion'] in HANDSHAKE_PROTOCOL_VERSIONS


def test_pinned_unknown_version_is_unsupported(portal):
    handler, _ = portal
    _, token = h.signed_token()
    headers, body = h.captured_request(handler.build_asgi_app, token, 'tools/list',
                                       lambda client: client.list_tools())
    message = json.loads(body)
    message['params']['_meta']['io.modelcontextprotocol/protocolVersion'] = UNKNOWN
    [response] = h.raw_requests(handler.build_asgi_app, [(
        'POST', '/mcp', dict(headers, **{'mcp-protocol-version': UNKNOWN}),
        json.dumps(message).encode())])
    assert response.status_code == 400
    error = response.json()['error']
    assert error['code'] == UNSUPPORTED_PROTOCOL_VERSION
    data = UnsupportedProtocolVersionErrorData.model_validate(error['data'])
    assert data.requested == UNKNOWN
    assert data.supported == ['2026-07-28']
    assert set(data.supported) <= set(PROTOCOL_REVISIONS)


def test_client_cannot_be_pinned_to_an_unknown_version():
    with pytest.raises(ValueError, match='mode must be'):
        Client('https://fake.example/mcp', mode=UNKNOWN)
