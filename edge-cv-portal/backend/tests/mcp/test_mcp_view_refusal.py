"""Out-of-view and nonexistent tool refusal at `tools/call` (18.2 finding 6).

Spec: .kiro/specs/mcp-servers/ (design.md §4 step 2 "Lookup", Error Handling
row "`tools/call` lookup", D8; tasks.md 18.2 finding 6).
Requirements 1.2, 17.3.

A view endpoint (`/mcp/{data,models,devices,workflows,admin}`) serves only the
tools of its own domain. A `tools/call` naming a delivered tool of another
view, or a name no catalog spec carries, is refused at the lookup step with
the JSON-RPC protocol error `INVALID_PARAMS` (-32602) and the message
`Unknown tool: <name>`, before validation, the switch, the role check and
dispatch: the backing Lambda is never invoked, nothing is written to the
Audit_Log, no `mcp_tool` telemetry line is emitted (the Error Handling row
says INFO log only), and the server logs one INFO line naming the tool and
the endpoint. The same tool is accepted on its own view and on the aggregate
`/mcp`, so what is refused is the endpoint's view, not the tool. All phases
are delivered and the switch is on, so only the view decides.
"""
import logging
import sys
import uuid

import pytest
from boto3.dynamodb.conditions import Attr
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

import _portal_harness as h
from _schema_strategy import minimal_args
from mcp_portal import VIEWS, server
from mcp_portal.catalog import CATALOG

BY_NAME = {s.name: s for s in CATALOG}
USECASE = 'uc-fake-1'
# One delivered, dispatched, Use_Case-scoped read-only tool per view (the
# first in catalog order), so the accepted call is one backend invocation.
RO_OF_VIEW = {view: next(s for s in CATALOG if s.domain == view and s.read_only
                         and s.backend and s.scope == 'usecase'
                         and 'usecase_id' in s.input_schema.get('properties', {}))
              for view in VIEWS}
DEPLOY = BY_NAME['portal_devices_deployment_create']     # M!, devices view
# Names no catalog spec carries: a made-up one and the deferred model
# deletion tool (test_mcp_catalog_permissions.py asserts its absence).
NONEXISTENT = ('portal_no_such_tool', 'portal_models_delete')


@pytest.fixture
def portal(aws_stack, monkeypatch):
    shared = sys.modules['shared_utils']
    monkeypatch.setattr(shared.rbac_manager, 'has_permission', lambda *_a, **_k: True)
    monkeypatch.delenv('PORTAL_REGISTRY_ENFORCED', raising=False)
    fake = h.install(monkeypatch, phases=h.ALL_PHASES, mutating=True)
    fake.stack = aws_stack
    return fake


def refusal(view, name, args, token):
    """The `MCPError` a `tools/call` of `name` on `view` raises, or the result."""
    async def go(client):
        try:
            return await client.call_tool(name, args)
        except MCPError as error:
            return error

    return h.in_memory(view, go, token)


def audit_rows(stack, sub):
    return stack.tables.audit_log.scan(FilterExpression=Attr('user_id').eq(sub))['Items']


def assert_refused_unknown(portal, caplog, outcome, name, view, sub):
    assert isinstance(outcome, MCPError), (name, view, outcome)
    assert outcome.error.code == INVALID_PARAMS == -32602
    assert outcome.error.message == f'Unknown tool: {name}'
    # Nothing ran: no dispatch, no mutation, no audit row, no telemetry line.
    assert portal.calls == []
    assert audit_rows(portal.stack, sub) == []
    assert [r for r in caplog.records if r.name == 'mcp_portal.telemetry'] == []
    info = [r for r in caplog.records
            if r.name == 'mcp_portal.server' and r.levelno == logging.INFO]
    assert len(info) == 1
    assert name in info[0].getMessage()
    assert server.server_name(view) in info[0].getMessage()


# ------------------------------------------------------ tool of another view

@pytest.mark.parametrize('view', VIEWS)
def test_a_delivered_tool_of_every_other_view_is_unknown_on_this_view(portal, caplog, view):
    for other, spec in RO_OF_VIEW.items():
        if other == view:
            continue
        sub = f'view-{uuid.uuid4()}'
        caplog.clear()
        with caplog.at_level(logging.INFO, logger='mcp_portal'):
            outcome = refusal(view, spec.name, minimal_args(spec, usecase_id=USECASE),
                              h.access_token(sub))
        assert_refused_unknown(portal, caplog, outcome, spec.name, view, sub)


@pytest.mark.parametrize('view', VIEWS)
def test_the_same_tool_is_accepted_on_its_own_view_and_on_the_aggregate(portal, view):
    spec = RO_OF_VIEW[view]
    args = minimal_args(spec, usecase_id=USECASE)
    for endpoint in (view, None):
        outcome = refusal(endpoint, spec.name, args, h.access_token())
        assert not isinstance(outcome, MCPError), (spec.name, endpoint, outcome)
        assert outcome.is_error is False, outcome.structured_content
    assert len(portal.calls) == 2
    assert {event['mcp']['server'] for event in portal.events} == {
        f'{server.server_name(view)}@1.2.0', 'dda-portal-mcp@1.2.0'}


@pytest.mark.parametrize('view', [v for v in VIEWS if v != DEPLOY.domain])
def test_a_confirmed_mutating_tool_of_another_view_is_refused_before_it_runs(portal, caplog,
                                                                             view):
    """Valid arguments, the matching `confirm` and the switch on: the lookup
    still refuses it on a foreign view, so nothing is created."""
    args = {'usecase_id': USECASE, 'components': [{'component_name': 'fake.component'}],
            'target_thing_group': 'fake-line-1', 'confirm': 'fake-line-1'}
    sub = f'view-{uuid.uuid4()}'
    with caplog.at_level(logging.INFO, logger='mcp_portal'):
        outcome = refusal(view, DEPLOY.name, args, h.access_token(sub))
    assert_refused_unknown(portal, caplog, outcome, DEPLOY.name, view, sub)
    # The same call on its own view dispatches once.
    accepted = refusal(DEPLOY.domain, DEPLOY.name, args, h.access_token())
    assert not isinstance(accepted, MCPError) and accepted.is_error is False
    assert len(portal.calls) == 1


def test_a_deprecated_alias_of_a_tool_of_another_view_is_refused_too(portal, caplog):
    import dataclasses

    target = BY_NAME['portal_devices_list']
    spec = dataclasses.replace(target, deprecated_alias='portal_devices_devices_list',
                               alias_removed_in='2.0.0')
    specs = [spec if s.name == spec.name else s for s in CATALOG]
    sub = f'view-{uuid.uuid4()}'

    async def go(client):
        try:
            return await client.call_tool('portal_devices_devices_list', {'usecase_id': USECASE})
        except MCPError as error:
            return error

    with caplog.at_level(logging.INFO, logger='mcp_portal'):
        outcome = h.in_memory('models', go, h.access_token(sub), specs=specs)
    assert_refused_unknown(portal, caplog, outcome, 'portal_devices_devices_list', 'models',
                           sub)


# ---------------------------------------------------------- nonexistent name

@pytest.mark.parametrize('endpoint', (None,) + VIEWS, ids=lambda v: v or 'aggregate')
@pytest.mark.parametrize('name', NONEXISTENT)
def test_a_nonexistent_name_is_refused_the_same_way_on_every_endpoint(portal, caplog,
                                                                       endpoint, name):
    assert name not in BY_NAME
    sub = f'view-{uuid.uuid4()}'
    with caplog.at_level(logging.INFO, logger='mcp_portal'):
        outcome = refusal(endpoint, name, {'usecase_id': USECASE, 'confirm': 'x'},
                          h.access_token(sub))
    assert_refused_unknown(portal, caplog, outcome, name, endpoint, sub)
