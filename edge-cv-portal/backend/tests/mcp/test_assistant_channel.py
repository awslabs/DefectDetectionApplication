"""The assistant audit channel and the claims-built principal.
Spec: .kiro/specs/portal-ai-assistant/ (design.md D3, §2, §3; tasks.md 1.2-1.5).
Requirements 6.1, 6.4.

`McpContext.channel` reaches the dispatched event's `mcp` block and the
`marker_event`; `shared_utils.attribution_from` copies a recognized channel
and keeps treating a block without one as MCP; `_attribution_attributes`
adds the four marker attributes for both marked channels and for neither
other value; `principal_from_claims` prefers the ID token's email.
"""
import sys
import uuid

import pytest

import _portal_harness as h
from mcp_portal import audit, dispatch, principal
from mcp_portal.catalog import CATALOG
from mcp_portal.dispatch import McpContext

BY_NAME = {s.name: s for s in CATALOG}
LIST_DEVICES = BY_NAME['portal_devices_list']


def _claims(sub=None, **overrides):
    claims = {'sub': sub or str(uuid.uuid4()), 'cognito:username': 'alice',
              'email': 'alice@example.com', 'custom:role': 'Viewer',
              'token_use': 'id', 'exp': 4102444800}
    claims.update(overrides)
    return claims


def _ctx(channel):
    return McpContext(request_id='turn-1/3', tool=LIST_DEVICES.name,
                     server='dda-portal-assistant@1.0.0', client='dda-portal-frontend/abc',
                     source_ip='203.0.113.9', user_agent='Mozilla/5.0',
                     principal_email='alice@example.com', channel=channel)


@pytest.fixture
def shared(aws_stack, monkeypatch):
    module = sys.modules['shared_utils']
    monkeypatch.setattr(module, '_REQUEST_AUDIT_CONTEXT', None)
    return module


def test_context_channel_defaults_to_mcp():
    ctx = McpContext(request_id='r', tool='t', server='s', client='c')
    assert ctx.channel == 'mcp'
    event = dispatch.build_backend_event(
        LIST_DEVICES, {'usecase_id': 'uc-1'}, principal.principal_from_claims(_claims()), ctx)
    assert event['mcp']['channel'] == 'mcp'


@pytest.mark.parametrize('channel', ['mcp', 'assistant'])
def test_dispatched_event_and_marker_event_carry_the_channel(channel):
    p = principal.principal_from_claims(_claims())
    event = dispatch.build_backend_event(LIST_DEVICES, {'usecase_id': 'uc-1'}, p, _ctx(channel))
    assert event['mcp']['channel'] == channel
    assert event['requestContext']['authorizer']['claims'] == p.claims
    marker = audit.marker_event(p, _ctx(channel))
    assert marker['mcp']['channel'] == channel
    assert marker['mcp']['request_id'] == 'turn-1/3'


@pytest.mark.parametrize('channel', ['mcp', 'assistant'])
def test_attribution_copies_a_recognized_channel(shared, channel):
    p = principal.principal_from_claims(_claims())
    event = dispatch.build_backend_event(LIST_DEVICES, {'usecase_id': 'uc-1'}, p, _ctx(channel))
    identity = shared.attribution_from(event, p.user, usecase_id='uc-1')
    assert identity['channel'] == channel
    assert identity['mcp_tool'] == LIST_DEVICES.name
    assert identity['mcp_server'] == 'dda-portal-assistant@1.0.0'
    assert identity['mcp_client'] == 'dda-portal-frontend/abc'
    assert identity['mcp_request_id'] == 'turn-1/3'
    attributes = shared._attribution_attributes(identity=identity)
    assert attributes['channel'] == channel
    for field in shared.MCP_MARKER_FIELDS[1:]:
        assert attributes[field] == identity[field]


def test_block_without_channel_or_with_an_unknown_one_is_mcp(shared):
    p = principal.principal_from_claims(_claims())
    for block_channel in (None, 'browser'):
        event = dispatch.build_backend_event(LIST_DEVICES, {'usecase_id': 'uc-1'}, p, _ctx('mcp'))
        if block_channel is None:
            del event['mcp']['channel']
        else:
            event['mcp']['channel'] = block_channel
        identity = shared.attribution_from(event, p.user, usecase_id='uc-1')
        assert identity['channel'] == 'mcp'


def test_api_entries_carry_no_marker_attributes(shared):
    attributes = shared._attribution_attributes(identity={'username': 'u', 'email': 'e'})
    assert attributes['channel'] == 'api'
    assert not any(field in attributes for field in shared.MCP_MARKER_FIELDS[1:])


def test_marked_channels_constant():
    shared = sys.modules['shared_utils']
    assert shared.AUDIT_CHANNEL_ASSISTANT == 'assistant'
    assert shared.MARKED_CHANNELS == ('mcp', 'assistant')


# ------------------------------------------------------- principal_from_claims

def test_principal_from_claims_uses_the_id_token_email(monkeypatch):
    calls = []
    monkeypatch.setattr(principal, 'lookup_email', lambda s, u: calls.append((s, u)) or 'x')
    claims = _claims(sub='sub-1')
    p = principal.principal_from_claims(claims)
    assert (p.sub, p.username, p.email) == ('sub-1', 'alice', 'alice@example.com')
    assert p.claims == claims and p.claims is not claims
    assert p.user == {'user_id': 'sub-1', 'email': 'alice@example.com',
                      'username': 'alice', 'role': 'Viewer'}
    assert calls == []


def test_principal_from_claims_falls_back_to_the_lookup_without_an_email(monkeypatch):
    monkeypatch.setattr(principal, 'lookup_email', lambda s, u: f'{u}@looked.up')
    p = principal.principal_from_claims(_claims(sub='sub-2', email='  '))
    assert p.email == 'alice@looked.up'
    p = principal.principal_from_claims({'sub': 'sub-3'})
    assert (p.username, p.email) == ('sub-3', 'sub-3@looked.up')


def test_principal_from_claims_tolerates_none():
    p = principal.principal_from_claims(None) if False else principal.principal_from_claims({})
    assert p.sub == 'unknown' and p.claims == {}
