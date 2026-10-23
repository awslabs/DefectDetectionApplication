"""Confirmation and mutating-switch guardrails of the Portal_MCP_Server.

Spec: .kiro/specs/mcp-servers/ (design.md §4 steps 3-4, D9, D13; Input
validation; Testing Strategy row "16.5 confirmation/switch", review-9 N7,
review-10 N5; tasks.md 7.5). Requirements 5.3, 5.4, 16.5.

Every `M!` tool refuses a missing or mismatching `confirm` before any
dispatch and runs with the matching one. `portal_devices_deployment_create`
is checked on both `oneOf` branches: `confirm` must equal
`target_thing_group` or `target_devices[0]`, whichever is present; the
other branch's value and the former literal `"deploy"` are
`confirmation_required`, and `target_devices: []` / `target_thing_group: ""`
are `invalid_input` before the comparison. With the switch off every `M`
tool is absent from `tools/list` and refused. All phases are delivered here
and the role check always allows, so only the guardrails decide.
"""
import sys

import pytest

import _portal_harness as h
from _schema_strategy import minimal_args
from mcp_portal import VIEWS, server
from mcp_portal.catalog import CATALOG

DESTRUCTIVE = [s for s in CATALOG if s.destructive]
MUTATING = [s for s in CATALOG if not s.read_only]
DEPLOY = next(s for s in CATALOG if s.name == 'portal_devices_deployment_create')
USECASE = 'uc-fake-1'
COMPONENTS = [{'component_name': 'fake.component'}]


@pytest.fixture
def portal(aws_stack, monkeypatch):
    shared = sys.modules['shared_utils']
    monkeypatch.setattr(shared.rbac_manager, 'has_permission', lambda *_a, **_k: True)
    monkeypatch.delenv('PORTAL_REGISTRY_ENFORCED', raising=False)
    fake = h.install(monkeypatch, phases=h.ALL_PHASES, mutating=True)
    fake.monkeypatch = monkeypatch
    return fake


def call(spec, args):
    return h.in_memory(spec.domain, lambda client: client.call_tool(spec.name, args),
                       h.access_token())


def category(result):
    return (result.structured_content or {}).get('category')


def args_for(spec, branch=0):
    overrides = {'usecase_id': USECASE} if 'usecase_id' in spec.input_schema.get(
        'properties', {}) and spec.scope != 'global' else {}
    return minimal_args(spec, branch=branch, **overrides)


def test_every_destructive_tool_has_a_confirm_argument():
    assert len(DESTRUCTIVE) == 28
    for spec in DESTRUCTIVE:
        assert 'confirm' in spec.input_schema['properties'], spec.name
        assert spec.confirm_target or spec.confirm_literal, spec.name


@pytest.mark.parametrize('spec', DESTRUCTIVE, ids=lambda s: s.name)
def test_destructive_tool_requires_the_matching_confirm(portal, spec):
    args = args_for(spec)
    expected = server.expected_confirm(spec, args)
    assert expected, spec.name
    missing = call(spec, args)
    assert category(missing) == 'confirmation_required'
    assert missing.structured_content['message'].startswith(f'{spec.name} is destructive')
    wrong = call(spec, dict(args, confirm=f'{expected}-other'))
    assert category(wrong) == 'confirmation_required'
    assert portal.calls == []
    allowed = call(spec, dict(args, confirm=expected))
    assert category(allowed) != 'confirmation_required', allowed.structured_content
    assert allowed.is_error is False, allowed.structured_content
    assert len(portal.calls) == 1


# ------------------------------------- portal_devices_deployment_create

def deploy(**extra):
    return call(DEPLOY, {'usecase_id': USECASE, 'components': COMPONENTS, **extra})


def test_deployment_create_thing_group_branch(portal):
    assert category(deploy(target_thing_group='fake-line-1')) == 'confirmation_required'
    for confirm in ('fake-device-1', 'deploy', 'target_thing_group', 'fake-line-1 '):
        result = deploy(target_thing_group='fake-line-1', confirm=confirm)
        assert category(result) == 'confirmation_required', confirm
    assert portal.calls == []
    result = deploy(target_thing_group='fake-line-1', confirm='fake-line-1')
    assert result.is_error is False
    assert len(portal.calls) == 1


def test_deployment_create_device_branch(portal):
    assert category(deploy(target_devices=['fake-device-1'])) == 'confirmation_required'
    for confirm in ('fake-line-1', 'deploy', 'target_devices[0]'):
        result = deploy(target_devices=['fake-device-1'], confirm=confirm)
        assert category(result) == 'confirmation_required', confirm
    assert portal.calls == []
    result = deploy(target_devices=['fake-device-1'], confirm='fake-device-1')
    assert result.is_error is False
    assert len(portal.calls) == 1


@pytest.mark.parametrize('target', [{'target_devices': []}, {'target_thing_group': ''}],
                         ids=['empty-devices', 'empty-thing-group'])
def test_empty_targets_are_invalid_input_before_the_comparison(portal, target):
    # `confirm` "matches" the empty value; the schema refuses it first.
    confirm = '' if 'target_thing_group' in target else 'fake-device-1'
    for args in ({}, {'confirm': confirm}):
        result = deploy(**target, **args)
        assert category(result) == 'invalid_input', (target, args)
        name = next(iter(target))
        assert name in result.structured_content['message']
    assert portal.calls == []


def test_both_branches_at_once_are_invalid_input(portal):
    result = deploy(target_thing_group='fake-line-1', target_devices=['fake-device-1'],
                    confirm='fake-line-1')
    assert category(result) == 'invalid_input'
    assert portal.calls == []


# ------------------------------------------------------- mutating switch

def listed_names(view):
    return {t.name for t in h.in_memory(view, lambda c: c.list_tools(),
                                        h.access_token()).tools}


@pytest.mark.parametrize('view', (None,) + VIEWS, ids=lambda v: v or 'aggregate')
def test_switch_off_lists_no_mutating_tool(portal, view):
    in_view = [s for s in CATALOG if view is None or s.domain == view]
    on = listed_names(view)
    assert {s.name for s in in_view} == on
    portal.monkeypatch.setenv('MCP_MUTATING_TOOLS_ENABLED', 'false')
    off = listed_names(view)
    assert off == {s.name for s in in_view if s.read_only}
    assert not off & {s.name for s in MUTATING}


@pytest.mark.parametrize('value', ['false', '', 'TRUE ', 'yes', '1'])
def test_only_a_lower_cased_true_turns_the_switch_on(portal, value):
    portal.monkeypatch.setenv('MCP_MUTATING_TOOLS_ENABLED', value)
    assert server.mutating_enabled() is (value.strip().lower() == 'true')


@pytest.mark.parametrize('spec', MUTATING, ids=lambda s: s.name)
def test_switch_off_refuses_every_mutating_tool(portal, spec):
    portal.monkeypatch.setenv('MCP_MUTATING_TOOLS_ENABLED', 'false')
    args = args_for(spec)
    if spec.destructive:
        args['confirm'] = server.expected_confirm(spec, args)
    result = call(spec, args)
    assert category(result) == 'mutating_tools_disabled'
    assert result.structured_content['message'] == (
        f'{spec.name} is mutating and mutating tools are disabled on this deployment')
    assert portal.calls == []
