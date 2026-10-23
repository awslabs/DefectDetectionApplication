"""Protocol-level suite of the Portal_MCP_Server (`mcp_portal/server.py`).

Spec: .kiro/specs/mcp-servers/ (design.md §2 `server.py` row, §4, D8, D9,
D35; Testing Strategy row "16.1 protocol level"; tasks.md 7.1).
Requirements 16.1, 1.2, 1.5, 1.7, 5.1, 5.3, 14.4, 15.3.

The SDK's in-process `Client` against each view's `Server`
(`build_server(view)`), the principal injected through the test-only
`_principal.set_principal`, and the backing handler Lambdas faked by
`_portal_harness.FakeLambda`: `server/discover` fields, a deterministic
`tools/list` with its cache hint, rendered descriptions and annotations,
the deprecated-alias entry and call, one read-only call, one mutating call
(`portal_devices_deployment_create` confirmed with its thing-group name,
review-9 N7), and the error shapes.
"""
import dataclasses
import json
import sys

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

import _portal_harness as h
from _schema_strategy import minimal_args
from mcp_portal import DELIVERED_PHASES, VIEWS, catalog, server
from mcp_portal.catalog import CATALOG

BY_NAME = {s.name: s for s in CATALOG}
ENDPOINTS = (None,) + VIEWS
USECASE = 'uc-fake-1'


@pytest.fixture
def portal(aws_stack, monkeypatch):
    monkeypatch.delenv('MCP_SERVER_VERSION', raising=False)
    monkeypatch.delenv('PORTAL_REGISTRY_ENFORCED', raising=False)
    shared = sys.modules['shared_utils']
    monkeypatch.setattr(shared.rbac_manager, 'has_permission', lambda *_a, **_k: True)
    fake = h.install(monkeypatch)
    fake.monkeypatch = monkeypatch
    return fake


def call(view, name, args, token=None, specs=None):
    token = token or h.access_token()
    return h.in_memory(view, lambda client: client.call_tool(name, args), token, specs)


def listed(view, specs=None):
    return h.in_memory(view, lambda client: client.list_tools(), h.access_token(), specs)


def expected_names(view, phases=DELIVERED_PHASES, mutating=False):
    return {s.name for s in CATALOG
            if (view is None or s.domain == view) and s.phase in phases
            and (s.read_only or mutating)}


# ------------------------------------------------------------- discover

@pytest.mark.parametrize('view', ENDPOINTS, ids=lambda v: v or 'aggregate')
def test_discover_fields(portal, view):
    async def go(client):
        return client.server_info, client.instructions

    info, instructions = h.in_memory(view, go, h.access_token())
    assert info.name == server.server_name(view)
    assert info.name == ('dda-portal-mcp' if view is None else f'dda-portal-mcp/{view}')
    assert info.version == '1.0.0'
    assert instructions == server.instructions_for(view)


# ---------------------------------------------------------------- listing

@pytest.mark.parametrize('view', ENDPOINTS, ids=lambda v: v or 'aggregate')
def test_tools_list_is_deterministic_and_rendered(portal, view):
    async def go(client):
        first = await client.list_tools()
        second = await client.list_tools(cache_mode='refresh')
        return first, second

    first, second = h.in_memory(view, go, h.access_token())
    assert first.ttl_ms == 300_000 and first.cache_scope == 'private'
    assert [t.model_dump() for t in first.tools] == [t.model_dump() for t in second.tools]
    names = [t.name for t in first.tools]
    assert set(names) == expected_names(view)
    keys = [(BY_NAME[n].domain, n) for n in names]
    assert keys == sorted(keys)
    for tool in first.tools:
        spec = BY_NAME[tool.name]
        assert tool.description == catalog.render_description(spec)
        assert tool.description.endswith(catalog.classification_text(spec))
        assert f'Touches: {spec.resource_type}' in tool.description
        assert tool.annotations.read_only_hint is spec.read_only
        assert tool.annotations.destructive_hint is spec.destructive
        assert tool.annotations.idempotent_hint is spec.idempotent
        assert tool.annotations.open_world_hint is False
        assert tool.input_schema == spec.input_schema


def test_aggregate_lists_the_87_phase_one_tools(portal):
    assert len(listed(None).tools) == 87


def test_switch_and_phases_widen_the_list(portal):
    portal.monkeypatch.setattr(server, 'DELIVERED_PHASES', h.ALL_PHASES)
    portal.monkeypatch.setenv('MCP_MUTATING_TOOLS_ENABLED', 'true')
    names = {t.name for t in listed('devices').tools}
    assert names == expected_names('devices', h.ALL_PHASES, mutating=True)
    assert 'portal_devices_deployment_create' in names
    destructive = [t for t in listed('devices').tools if t.name ==
                   'portal_devices_deployment_create'][0]
    assert destructive.description.endswith(
        'MUTATING, DESTRUCTIVE — requires confirm=<target_thing_group | target_devices[0]>.')


# ------------------------------------------------------------------ alias

def alias_specs():
    spec = dataclasses.replace(BY_NAME['portal_devices_list'],
                               deprecated_alias='portal_devices_devices_list',
                               alias_removed_in='2.0.0')
    return [spec if s.name == spec.name else s for s in CATALOG]


def test_alias_is_listed_twice_and_runs_its_target(portal):
    portal.answer = lambda _arn, _event: (200, {'devices': [{'device_id': 'fake-device-1'}]})
    specs = alias_specs()
    tools = listed('devices', specs).tools
    alias = [t for t in tools if t.name == 'portal_devices_devices_list']
    target = [t for t in tools if t.name == 'portal_devices_list']
    assert len(alias) == 1 and len(target) == 1
    assert alias[0].description.startswith(
        'Deprecated alias of portal_devices_list; removed in 2.0.0.')
    assert alias[0].description.endswith('Read-only.')

    result = call('devices', 'portal_devices_devices_list', {'usecase_id': USECASE},
                  specs=specs)
    assert not result.is_error
    assert result.structured_content['result']['devices'] == [{'device_id': 'fake-device-1'}]
    assert result.structured_content['deprecation'] == (
        'portal_devices_devices_list is a deprecated alias of portal_devices_list; '
        'removed in 2.0.0')
    assert any('deprecated alias' in c.text for c in result.content if c.type == 'text')
    [event] = portal.events
    assert event['mcp']['tool'] == 'portal_devices_list'


# -------------------------------------------------------------- RO call

def test_read_only_call_dispatches_once(portal):
    portal.answer = lambda _arn, _event: (200, {'devices': [{'device_id': 'fake-device-1'}]})
    token = h.access_token('sub-protocol-ro')
    result = call('devices', 'portal_devices_list', {'usecase_id': USECASE}, token)
    assert result.is_error is False
    assert result.structured_content['result']['devices'] == [{'device_id': 'fake-device-1'}]
    [(arn, event)] = portal.calls
    assert arn == h.backend_arn('devices')
    assert event['httpMethod'] == 'GET' and event['resource'] == '/devices'
    assert event['queryStringParameters'] == {'usecase_id': USECASE}
    assert event['requestContext']['authorizer']['claims'] == token.claims
    assert event['mcp']['channel'] == 'mcp'
    assert event['mcp']['tool'] == 'portal_devices_list'
    assert event['mcp']['server'] == 'dda-portal-mcp/devices@1.0.0'
    assert event['mcp']['principal_email'] == h.EMAIL


def test_aggregate_endpoint_serves_every_view(portal):
    result = call(None, 'portal_devices_list', {'usecase_id': USECASE})
    assert result.is_error is False
    assert portal.events[-1]['mcp']['server'] == 'dda-portal-mcp@1.0.0'


# ---------------------------------------------------------------- M call

def test_mutating_call_with_the_thing_group_confirm(portal):
    portal.monkeypatch.setattr(server, 'DELIVERED_PHASES', h.ALL_PHASES)
    portal.monkeypatch.setenv('MCP_MUTATING_TOOLS_ENABLED', 'true')
    portal.answer = lambda _arn, _event: (200, {'deployment_id': 'fake-deployment-1'})
    args = {'usecase_id': USECASE, 'components': [{'component_name': 'fake.component'}],
            'target_thing_group': 'fake-line-1', 'confirm': 'fake-line-1'}
    result = call('devices', 'portal_devices_deployment_create', args)
    assert result.is_error is False, result.structured_content
    [(arn, event)] = portal.calls
    assert arn == h.backend_arn('deployments')
    assert event['httpMethod'] == 'POST' and event['path'] == '/deployments'
    body = json.loads(event['body'])
    assert body['target_thing_group'] == 'fake-line-1'
    assert body['usecase_id'] == USECASE
    assert 'confirm' not in body
    assert result.structured_content['result'] == {'deployment_id': 'fake-deployment-1'}


# ---------------------------------------------------------- error shapes

def failure_of(result):
    assert result.is_error is True
    content = result.structured_content
    assert set(content) >= {'category', 'message', 'request_id', 'status'}
    assert result.content[0].type == 'text'
    assert result.content[0].text == f"{content['category']}: {content['message']}"
    return content


def test_invalid_input_names_the_property_not_the_value(portal):
    content = failure_of(call('devices', 'portal_devices_get', {'usecase_id': USECASE}))
    assert content['category'] == 'invalid_input'
    assert content['message'] == 'device_id is required'
    bad = 'not a valid id!'
    content = failure_of(call('devices', 'portal_devices_get',
                              {'usecase_id': USECASE, 'device_id': bad}))
    assert content['category'] == 'invalid_input' and 'device_id' in content['message']
    assert bad not in content['message']
    extra = failure_of(call('devices', 'portal_devices_list',
                            {'usecase_id': USECASE, 'surprise': 1}))
    assert extra['message'] == 'surprise is not an argument of portal_devices_list'
    assert portal.calls == []


def test_confirmation_required(portal):
    portal.monkeypatch.setattr(server, 'DELIVERED_PHASES', h.ALL_PHASES)
    portal.monkeypatch.setenv('MCP_MUTATING_TOOLS_ENABLED', 'true')
    args = {'usecase_id': USECASE, 'components': [{'component_name': 'fake.component'}],
            'target_thing_group': 'fake-line-1'}
    content = failure_of(call('devices', 'portal_devices_deployment_create', args))
    assert content['category'] == 'confirmation_required'
    assert content['message'] == (
        'portal_devices_deployment_create is destructive; pass '
        'confirm=<target_thing_group | target_devices[0]>')
    assert portal.calls == []


def test_mutating_tools_disabled(portal):
    portal.monkeypatch.setattr(server, 'DELIVERED_PHASES', h.ALL_PHASES)
    spec = BY_NAME['portal_devices_deployment_create']
    args = minimal_args(spec, usecase_id=USECASE)
    args['confirm'] = server.expected_confirm(spec, args)
    content = failure_of(call('devices', spec.name, args))
    assert content['category'] == 'mutating_tools_disabled'
    assert portal.calls == []


@pytest.mark.parametrize('name', ['portal_no_such_tool', 'portal_devices_deployment_create',
                                  'portal_models_list'])
def test_unknown_undelivered_or_foreign_view_tool_is_invalid_params(portal, name):
    async def go(client):
        try:
            await client.call_tool(name, {'usecase_id': USECASE})
        except MCPError as error:
            return error
        return None

    # portal_devices_deployment_create is Phase 3 (not delivered);
    # portal_models_list belongs to another view than /mcp/devices.
    error = h.in_memory('devices', go, h.access_token())
    assert isinstance(error, MCPError), name
    assert error.error.code == INVALID_PARAMS == -32602
    assert portal.calls == []


def test_without_a_principal_the_call_is_not_authenticated(portal):
    result = h.in_memory('devices', lambda c: c.call_tool(
        'portal_devices_list', {'usecase_id': USECASE}), token=None)
    assert failure_of(result)['category'] == 'not_authenticated'
    assert portal.calls == []


def test_backend_error_status_is_mapped(portal):
    portal.answer = lambda _arn, _event: (404, {'error': 'Device not found'})
    content = failure_of(call('devices', 'portal_devices_get',
                              {'usecase_id': USECASE, 'device_id': 'fake-device-9'}))
    assert content['category'] == 'not_found' and content['status'] == 404
    assert content['message'] == 'Device not found'
    [event] = portal.events
    assert event['pathParameters'] == {'id': 'fake-device-9'}
