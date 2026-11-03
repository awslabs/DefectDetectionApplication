"""`pipeline.call_tool` agrees with the MCP server's `on_call_tool`.
Spec: .kiro/specs/portal-ai-assistant/ (design.md D1, Correctness Property
P13; tasks.md 1.1, 1.5). Requirement 5.1 (one pipeline, two transports).

For a read-only call, a confirmed destructive call, an invalid-input call,
a mutating call with the switch off and a denied call, the MCP server (the
SDK's in-process client, `_portal_harness.in_memory`) and a direct
`pipeline.call_tool` with the same principal and arguments produce the same
structured content (`request_id` aside), the same error flag, and the same
audit entries (action, result, channel, tool) for their respective subs.
"""
import sys
import uuid

import pytest
from boto3.dynamodb.conditions import Attr

import _portal_harness as h
from mcp_portal import pipeline
from mcp_portal.catalog import CATALOG
from mcp_portal.dispatch import McpContext
from mcp_portal.principal import principal_from_access_token

BY_NAME = {s.name: s for s in CATALOG}
USECASE = 'uc-equiv-1'


def answer(_arn, event):
    if event['resource'] == '/devices':
        return 200, {'devices': [{'device_id': 'd-1'}], 'usecase_id': USECASE}
    if event['resource'] == '/deployments':
        return 201, {'deployment_id': 'dep-1', 'usecase_id': USECASE}
    return 200, {'ok': True}


@pytest.fixture
def portal(aws_stack, monkeypatch):
    monkeypatch.delenv('MCP_SERVER_VERSION', raising=False)
    monkeypatch.delenv('PORTAL_REGISTRY_ENFORCED', raising=False)
    shared = sys.modules['shared_utils']
    monkeypatch.setattr(shared.rbac_manager, 'has_permission', lambda *_a, **_k: True)
    fake = h.install(monkeypatch, h.FakeLambda(answer), mutating=True)
    fake.shared = shared
    fake.monkeypatch = monkeypatch
    return fake


def audit_rows(shared, sub):
    import boto3
    table = boto3.resource('dynamodb', region_name='us-east-1').Table(shared.AUDIT_LOG_TABLE)
    items = table.scan(FilterExpression=Attr('user_id').eq(sub))['Items']
    return sorted((i['action'], i['result'], i.get('channel'), i.get('mcp_tool'))
                  for i in items)


def via_server(name, args):
    token = h.access_token()
    result = h.in_memory(None, lambda client: client.call_tool(name, args), token)
    content = dict(result.structured_content or {})
    content.pop('request_id', None)
    return token.subject, result.is_error, content


def via_pipeline(name, args, mutating=True):
    token = h.access_token()
    principal = principal_from_access_token(token)
    spec = BY_NAME[name]
    ctx = McpContext(request_id=f'req-{uuid.uuid4()}', tool=spec.name,
                     server='dda-portal-mcp@1.0.0', client='unknown',
                     principal_email=principal.email)
    outcome = pipeline.call_tool(spec, args, principal, ctx, mutating_enabled=mutating,
                                 server_version='1.0.0')
    content = dict(outcome.structured(None))
    content.pop('request_id', None)
    return token.subject, outcome.is_error, content


CASES = {
    'read_only': ('portal_devices_list', {'usecase_id': USECASE}),
    'destructive_confirmed': ('portal_devices_deployment_create', {
        'usecase_id': USECASE, 'target_thing_group': 'tg-1', 'confirm': 'tg-1',
        'components': [{'component_name': 'fake.component'}]}),
    'invalid_input': ('portal_devices_list', {'usecase_id': 42}),
}


@pytest.mark.parametrize('case', sorted(CASES))
def test_server_and_pipeline_agree(portal, case):
    name, args = CASES[case]
    sub_s, error_s, content_s = via_server(name, args)
    sub_p, error_p, content_p = via_pipeline(name, args)
    assert error_s == error_p
    assert content_s == content_p
    assert audit_rows(portal.shared, sub_s) == audit_rows(portal.shared, sub_p)


def test_mutating_switch_off_agrees(portal):
    portal.monkeypatch.setenv('MCP_MUTATING_TOOLS_ENABLED', 'false')
    name, args = CASES['destructive_confirmed']
    sub_s, error_s, content_s = via_server(name, args)
    sub_p, error_p, content_p = via_pipeline(name, args, mutating=False)
    assert error_s and error_p
    assert content_s == content_p
    assert content_s['category'] == 'mutating_tools_disabled'
    assert audit_rows(portal.shared, sub_s) == audit_rows(portal.shared, sub_p)


def test_denial_agrees(portal):
    portal.monkeypatch.setattr(portal.shared.rbac_manager, 'has_permission',
                               lambda *_a, **_k: False)
    portal.monkeypatch.setattr(portal.shared.rbac_manager, 'get_user_role',
                               lambda *_a, **_k: None)
    name, args = CASES['read_only']
    sub_s, error_s, content_s = via_server(name, args)
    sub_p, error_p, content_p = via_pipeline(name, args)
    assert error_s and error_p
    assert content_s == content_p and content_s['category'] == 'not_authorized'
    rows_s, rows_p = audit_rows(portal.shared, sub_s), audit_rows(portal.shared, sub_p)
    assert rows_s == rows_p
    assert rows_s == [('unauthorized_access', 'denied', 'mcp', 'portal_devices_list')]


def test_outcome_fields_for_a_dispatched_call(portal):
    _, _, _ = via_pipeline(*CASES['read_only'])
    token = h.access_token()
    principal = principal_from_access_token(token)
    spec = BY_NAME['portal_devices_list']
    ctx = McpContext(request_id='r-1', tool=spec.name, server='s', client='c',
                     principal_email=principal.email)
    outcome = pipeline.call_tool(spec, {'usecase_id': USECASE}, principal, ctx,
                                 mutating_enabled=False)
    assert outcome.dispatched and outcome.status == 200 and not outcome.is_error
    assert outcome.shaped is not None and outcome.failure is None
    assert outcome.category is None and outcome.duration_ms >= 0
    assert outcome.structured('r-1')['result']['devices'] == [{'device_id': 'd-1'}]
