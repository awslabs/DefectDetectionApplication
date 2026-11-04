"""The Phase-4 account-lifecycle tools: self-protection, audit counts and the
dispatched event.

Spec: .kiro/specs/mcp-user-operations/ (design.md A4, A10, the Architecture's
pipeline step 5b, Audit rows, Data Models "The email lookup result and the
fresh read", Correctness Properties 2, 4 and 8, Testing Strategy; tasks.md
2.8, 2.10 and 2.14).

* **Feature: mcp-user-operations, Property 2: Self-protection decision** -
  `pipeline.self_protection_failure` as a pure function, under Hypothesis
  and in fixed cases; step 5b through `on_call_tool` (the SDK's in-process
  client and the harness), with a recorder in place of the harness's
  `principal.lookup_email` patch that keeps the `refresh` keyword of every
  call; and one call through `pipeline.call_tool` in the Portal assistant's
  call shape (owner decision 2a). Validates: Requirements 3.1, 3.2, 3.3, 5.4.
* **Feature: mcp-user-operations, Property 4: Marked audit entries per
  Phase-4 completion** - the design's audit table as a count table per tool
  and outcome, the fake backing handler writing `k` entries through the
  real `log_audit_event` under its own trace id. Validates: Requirements
  5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.8.
* **Feature: mcp-user-operations, Property 8: Dispatch event faithfulness
  for the four rows** - fixed cases of the event the fake Lambda receives
  (the Hypothesis property is in `test_property_mcp_dispatch_event.py`).
  Validates: Requirements 1.3, 2.6, 6.7.

The registry is the moto session table under `PORTAL_REGISTRY_ENFORCED=true`
(the deployed mode); every test seeds fresh `sub`s with a `global` row.
"""
import dataclasses
import json
import logging
import sys
import time
import uuid

import pytest
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

import _portal_harness as h
from mcp_portal import dispatch, pipeline, principal
from mcp_portal.catalog import CATALOG
from mcp_portal.dispatch import McpContext
from mcp_portal.errors import BUDGET_MESSAGE, ToolFailure, category_for
from mcp_portal.pipeline import (EMAIL_UNRESOLVED_MESSAGE, SELF_PROTECTION_MESSAGE,
                                 self_protection_failure)
from mcp_portal.principal import EmailLookup, Principal

BY_NAME = {s.name: s for s in CATALOG}
CREATE = BY_NAME['portal_admin_account_create']
DISABLE = BY_NAME['portal_admin_account_disable']
ENABLE = BY_NAME['portal_admin_account_enable']
DELETE = BY_NAME['portal_admin_account_delete']
ACCOUNT_TOOLS = (CREATE, DISABLE, ENABLE, DELETE)
PROTECTED = (DISABLE, DELETE)
UNPROTECTED = (ENABLE, CREATE)
USERNAME = 'alice-x1'
OTHER = 'bob-x1'
TARGET = 'fake-target-1'
TARGET_EMAIL = 'fake-user-1@example.com'
MARKER = ('channel', 'mcp_tool', 'mcp_server', 'mcp_client', 'mcp_request_id')
DENYLISTED = ('password', 'verifier', 'hash')


def tool_id(spec):
    return spec.name


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def portal(aws_stack, monkeypatch):
    monkeypatch.setenv('PORTAL_REGISTRY_ENFORCED', 'true')
    monkeypatch.delenv('_X_AMZN_TRACE_ID', raising=False)
    fake = h.install(monkeypatch, phases=h.ALL_PHASES, mutating=True)
    fake.stack, fake.monkeypatch = aws_stack, monkeypatch
    return fake


def seeded_token(stack, role='PortalAdmin', username=USERNAME):
    """An access token for a fresh `sub` with a `global` registry row."""
    sub = f'acct-{uuid.uuid4()}'
    stack.tables.user_roles.put_item(Item={'user_id': sub, 'usecase_id': 'global',
                                           'role': role, 'status': 'enabled'})
    return h.access_token(sub, username=username)


def call(spec, args, token, view='admin'):
    return h.in_memory(view, lambda client: client.call_tool(spec.name, args), token)


def args_for(spec, target):
    if spec is CREATE:
        return {'account_username': target, 'account_email': TARGET_EMAIL, 'role': 'Viewer'}
    return {'account_username': target, 'confirm': target}


class Recorder:
    """`principal.lookup_email`: answers `cached` to a plain lookup (step 1)
    and `fresh` to `refresh=True` (step 5b), recording `refresh` per call."""

    def __init__(self, cached=EmailLookup(h.EMAIL, False), fresh=None):
        self.cached, self.fresh, self.calls = cached, fresh or cached, []

    def __call__(self, sub, username, refresh=False):
        self.calls.append(refresh)
        return self.fresh if refresh else self.cached


def record(portal, cached=EmailLookup(h.EMAIL, False), fresh=None):
    recorder = Recorder(cached, fresh)
    portal.monkeypatch.setattr(principal, 'lookup_email', recorder)
    return recorder


def audit_rows(stack, sub):
    kwargs = {'FilterExpression': Attr('user_id').eq(sub)}
    items = []
    while True:
        page = stack.tables.audit_log.scan(**kwargs)
        items.extend(page['Items'])
        if 'LastEvaluatedKey' not in page:
            return items
        kwargs['ExclusiveStartKey'] = page['LastEvaluatedKey']


def envelopes(items):
    return [i for i in items if i['action'] == 'mcp_tool_call']


def assert_marked(items, spec, channel='mcp'):
    for item in items:
        assert item['channel'] == channel, item['action']
        assert item['mcp_tool'] == spec.name
        for field in MARKER:
            assert item[field] and item[field] != 'unknown', (item['action'], field)
        assert not [key for key in item.get('details', {})
                    if any(term in str(key).lower() for term in DENYLISTED)
                    or str(key).lower().startswith('temp')]


def assert_one_refusal_envelope(items, spec, target, category):
    """Exactly one marked `mcp_tool_call` failure envelope naming `target`."""
    assert len(items) == 1, items
    [envelope] = items
    assert envelope['action'] == 'mcp_tool_call' and envelope['result'] == 'failure'
    assert envelope['resource_type'] == 'account' and envelope['resource_id'] == target
    assert envelope['details']['status'] is None
    assert envelope['details']['error_category'] == category
    assert envelope['details']['account_username'] == target
    assert_marked(items, spec)


# ------------------------------------- Property 2: the decision, pure function

IDENT = st.from_regex(r'[A-Za-z][A-Za-z0-9._+=-]{0,11}', fullmatch=True)
ADDRESS = st.builds(lambda local, domain: f'{local}@{domain}.example',
                    st.from_regex(r'[A-Za-z0-9]{1,8}', fullmatch=True),
                    st.from_regex(r'[A-Za-z]{1,8}', fullmatch=True))
LOOKUPS = st.one_of(ADDRESS.map(lambda address: EmailLookup(address, False)),
                    st.just(EmailLookup('unknown', False)),
                    st.just(EmailLookup('unknown', True)))


@st.composite
def decisions(draw):
    spec = draw(st.sampled_from(ACCOUNT_TOOLS))
    username, sub = draw(IDENT), draw(IDENT)
    cached = draw(st.one_of(ADDRESS, st.just('unknown')))
    lookup = draw(LOOKUPS)
    named = [username, sub, cached, lookup.email, 'unknown']
    target = draw(st.one_of(st.sampled_from(named),
                            st.sampled_from(named).map(str.swapcase),
                            IDENT, ADDRESS))
    confirm = draw(st.one_of(st.none(), st.just(target), IDENT))
    caller = Principal(sub=sub, username=username, email=cached,
                       claims={'sub': sub, 'username': username})
    return spec, caller, lookup, target, confirm


@settings(max_examples=100, deadline=None)
@given(case=decisions(), other_email=st.one_of(ADDRESS, st.just('unknown')))
def test_self_protection_decision(case, other_email):
    """**Feature: mcp-user-operations, Property 2: Self-protection decision**"""
    spec, caller, lookup, target, confirm = case
    args = {'account_username': target}
    if confirm is not None:
        args['confirm'] = confirm
    failure = self_protection_failure(spec, args, caller, lookup)
    if spec.self_protect_arg is None:          # enable and create
        assert failure is None
        return
    by_identity = target in (caller.username, caller.sub)
    by_address = (not lookup.failed and lookup.email != 'unknown'
                  and target == lookup.email)
    if by_identity or by_address:
        assert failure == ToolFailure('conflict', SELF_PROTECTION_MESSAGE, status=409)
    elif lookup.failed:
        assert failure == ToolFailure('backend_failure', EMAIL_UNRESOLVED_MESSAGE)
        assert failure.status is None
    else:
        assert failure is None
    # Neither the cached step-1 address nor `confirm` plays a part.
    assert self_protection_failure(spec, args, dataclasses.replace(caller, email=other_email),
                                   lookup) == failure
    assert self_protection_failure(spec, {'account_username': target}, caller,
                                   lookup) == failure


CALLER = Principal(sub='sub-x1', username=USERNAME, email='old@example.com', claims={})
FRESH = EmailLookup('new@example.com', False)


def decided(spec, target, lookup=FRESH):
    failure = self_protection_failure(spec, {'account_username': target, 'confirm': target},
                                      CALLER, lookup)
    return failure.category if failure is not None else None


@pytest.mark.parametrize('spec', PROTECTED, ids=tool_id)
def test_a_lookup_that_did_not_fail(spec):
    assert decided(spec, USERNAME) == decided(spec, 'sub-x1') == 'conflict'
    assert decided(spec, 'new@example.com') == 'conflict'     # the address read now
    assert decided(spec, 'old@example.com') is None           # the cached address only
    assert decided(spec, 'unknown') is None                   # the literal
    for variant in ('ALICE-X1', 'SUB-X1', 'New@example.com'):
        assert decided(spec, variant) is None, variant         # exact comparison
    assert decided(spec, OTHER) is None


@pytest.mark.parametrize('spec', PROTECTED, ids=tool_id)
def test_an_account_without_an_email_attribute(spec):
    none = EmailLookup('unknown', False)
    assert decided(spec, USERNAME, none) == decided(spec, 'sub-x1', none) == 'conflict'
    for target in ('unknown', 'new@example.com', 'old@example.com', OTHER):
        assert decided(spec, target, none) is None, target


@pytest.mark.parametrize('spec', PROTECTED, ids=tool_id)
def test_a_failed_lookup_fails_closed_except_for_the_username_and_sub(spec):
    failed = EmailLookup('unknown', True)
    assert decided(spec, USERNAME, failed) == decided(spec, 'sub-x1', failed) == 'conflict'
    for target in ('old@example.com', 'new@example.com', 'unknown', 'ALICE-X1', 'SUB-X1',
                   OTHER):
        failure = self_protection_failure(spec, {'account_username': target}, CALLER, failed)
        assert failure == ToolFailure('backend_failure', EMAIL_UNRESOLVED_MESSAGE), target


@pytest.mark.parametrize('spec', ACCOUNT_TOOLS + (BY_NAME['portal_admin_account_role_change'],),
                         ids=tool_id)
def test_an_absent_or_non_string_target_is_not_decided(spec):
    failed = EmailLookup('unknown', True)
    for args in (None, {}, {'confirm': USERNAME}, {'account_username': None},
                 {'account_username': 7}, {'account_username': [USERNAME]}):
        assert self_protection_failure(spec, args, CALLER, failed) is None, args


def test_enable_create_and_other_rows_are_never_refused():
    for spec in UNPROTECTED + (BY_NAME['portal_admin_account_role_change'],):
        assert spec.self_protect_arg is None
        for target in (USERNAME, 'sub-x1', 'new@example.com'):
            for lookup in (FRESH, EmailLookup('unknown', True)):
                assert self_protection_failure(spec, {'account_username': target},
                                               CALLER, lookup) is None


# ------------------------------- Property 2: step 5b through on_call_tool

def self_target(token, kind):
    return {'username': token.claims['username'], 'sub': token.subject,
            'email': h.EMAIL}[kind]


@pytest.mark.parametrize('kind', ['username', 'sub', 'email'])
@pytest.mark.parametrize('spec', PROTECTED, ids=tool_id)
def test_naming_the_caller_is_refused_before_dispatch(portal, caplog, spec, kind):
    recorder = record(portal)
    token = seeded_token(portal.stack)
    target = self_target(token, kind)
    with caplog.at_level(logging.INFO, logger='mcp_portal.pipeline'):
        result = call(spec, {'account_username': target, 'confirm': target}, token)
    content = result.structured_content
    assert result.is_error is True
    assert content['category'] == 'conflict' and content['status'] == 409
    assert content['message'] == SELF_PROTECTION_MESSAGE
    assert 'effect' not in content and 'status_tool' not in content
    assert portal.calls == []
    assert recorder.calls == [False, True]      # step 1, then step 5b's fresh read
    assert_one_refusal_envelope(audit_rows(portal.stack, token.subject), spec, target,
                                'conflict')
    lines = [r.getMessage() for r in caplog.records if r.name == 'mcp_portal.pipeline']
    assert lines == [f'conflict for {spec.name}: {SELF_PROTECTION_MESSAGE}']
    assert target not in lines[0]


@pytest.mark.parametrize('spec', UNPROTECTED, ids=tool_id)
def test_enable_and_create_naming_the_caller_are_dispatched(portal, spec):
    recorder = record(portal)
    token = seeded_token(portal.stack)
    result = call(spec, args_for(spec, token.claims['username']), token)
    assert result.is_error is False, result.structured_content
    assert len(portal.calls) == 1
    assert recorder.calls == [False]


@pytest.mark.parametrize('step', ['confirmation_required', 'mutating_tools_disabled',
                                  'not_authorized'])
@pytest.mark.parametrize('spec', PROTECTED, ids=tool_id)
def test_a_self_target_refused_before_step_5b_reads_nothing(portal, spec, step):
    recorder = record(portal)
    token = seeded_token(portal.stack, role='Viewer' if step == 'not_authorized'
                         else 'PortalAdmin')
    target = token.claims['username']
    args = {'account_username': target, 'confirm': target}
    if step == 'confirmation_required':
        args['confirm'] = 'wrong-x1'
    if step == 'mutating_tools_disabled':
        portal.monkeypatch.setenv('MCP_MUTATING_TOOLS_ENABLED', 'false')
    content = call(spec, args, token).structured_content
    assert content['category'] == step
    assert portal.calls == []
    assert recorder.calls == [False]
    items = audit_rows(portal.stack, token.subject)
    if step == 'not_authorized':
        assert content['status'] == 403
        [entry] = items
        assert entry['action'] == 'unauthorized_access' and entry['result'] == 'denied'
        assert_marked(items, spec)
    else:
        assert_one_refusal_envelope(items, spec, target, step)


@pytest.mark.parametrize('spec', PROTECTED, ids=tool_id)
def test_a_failed_email_read_is_a_retryable_backend_failure(portal, caplog, spec):
    recorder = record(portal, fresh=EmailLookup('unknown', True))
    token = seeded_token(portal.stack)
    with caplog.at_level(logging.INFO, logger='mcp_portal.pipeline'):
        result = call(spec, args_for(spec, OTHER), token)
    content = result.structured_content
    assert content['category'] == 'backend_failure'
    assert content['message'] == EMAIL_UNRESOLVED_MESSAGE
    assert content['status'] is None and 'effect' not in content
    assert portal.calls == [] and recorder.calls == [False, True]
    assert_one_refusal_envelope(audit_rows(portal.stack, token.subject), spec, OTHER,
                                'backend_failure')
    lines = [r.getMessage() for r in caplog.records if r.name == 'mcp_portal.pipeline']
    assert lines == [f'backend_failure for {spec.name}: {EMAIL_UNRESOLVED_MESSAGE}']
    # The username and sub comparisons need no read: still conflict.
    for target in (token.claims['username'], token.subject):
        assert call(spec, args_for(spec, target), token).structured_content['category'] == \
            'conflict'
    assert portal.calls == []


@pytest.mark.parametrize('spec', PROTECTED, ids=tool_id)
def test_without_an_email_attribute_only_the_username_and_sub_decide(portal, spec):
    record(portal, cached=EmailLookup('unknown', False))
    token = seeded_token(portal.stack)
    for target in (token.claims['username'], token.subject):
        assert call(spec, args_for(spec, target), token).structured_content['category'] == \
            'conflict'
    assert portal.calls == []
    for target in ('unknown', OTHER):
        result = call(spec, args_for(spec, target), token)
        assert result.is_error is False, result.structured_content
    assert [e['pathParameters'] for e in portal.events] == [
        {'username': 'unknown'}, {'username': OTHER}]


def test_the_rule_reads_the_address_on_the_call_not_the_cache(portal):
    """The stale-cache case: the cache holds the caller's former address."""
    recorder = record(portal, cached=EmailLookup('old@example.com', False),
                      fresh=EmailLookup('new@example.com', False))
    token = seeded_token(portal.stack)
    refused = call(DISABLE, args_for(DISABLE, 'new@example.com'), token)
    assert refused.structured_content['category'] == 'conflict'
    assert portal.calls == []
    # The former address no longer names the caller: dispatched. Every
    # attribution of that call still shows the step-1 (cached) address.
    other = seeded_token(portal.stack)
    dispatched = call(DISABLE, args_for(DISABLE, 'old@example.com'), other)
    assert dispatched.is_error is False, dispatched.structured_content
    [event] = portal.events
    assert event['mcp']['principal_email'] == 'old@example.com'
    [envelope] = envelopes(audit_rows(portal.stack, other.subject))
    assert envelope['email'] == 'old@example.com' and envelope['result'] == 'success'
    assert recorder.calls == [False, True, False, True]


@pytest.mark.parametrize('spec', PROTECTED, ids=tool_id)
def test_a_case_variant_of_the_username_or_sub_is_dispatched(portal, spec):
    record(portal)
    token = seeded_token(portal.stack)
    for target in (token.claims['username'].upper(), token.subject.upper()):
        result = call(spec, args_for(spec, target), token)
        assert result.is_error is False, (target, result.structured_content)
    assert len(portal.calls) == 2


@pytest.mark.parametrize('spec', PROTECTED, ids=tool_id)
def test_the_budget_guard_refuses_before_the_read(portal, spec):
    recorder = record(portal)
    portal.monkeypatch.setattr(dispatch, '_remaining_ms',
                               lambda: dispatch.MIN_DISPATCH_BUDGET_MS - 1)
    token = seeded_token(portal.stack)
    content = call(spec, args_for(spec, OTHER), token).structured_content
    assert content['category'] == 'timeout' and content['message'] == BUDGET_MESSAGE
    assert content['status'] is None and 'effect' not in content
    assert portal.calls == [] and recorder.calls == [False]
    assert_one_refusal_envelope(audit_rows(portal.stack, token.subject), spec, OTHER,
                                'timeout')
    # The guard comes before the comparisons; with the budget left, the read
    # happens and the call is dispatched.
    assert call(spec, args_for(spec, token.subject), token).structured_content['category'] == \
        'timeout'
    portal.monkeypatch.setattr(dispatch, '_remaining_ms',
                               lambda: dispatch.MIN_DISPATCH_BUDGET_MS + 1000)
    assert call(spec, args_for(spec, OTHER), token).is_error is False
    assert len(portal.calls) == 1 and recorder.calls == [False, False, False, True]


def test_the_portal_assistant_call_shape_runs_step_5b(portal):
    """Owner decision 2a: `pipeline.call_tool` as `assistant/turn.py`
    calls it (an ID-token principal, `channel='assistant'`)."""
    recorder = record(portal)
    sub = f'acct-{uuid.uuid4()}'
    portal.stack.tables.user_roles.put_item(Item={'user_id': sub, 'usecase_id': 'global',
                                                  'role': 'PortalAdmin', 'status': 'enabled'})
    caller = principal.principal_from_claims({'sub': sub, 'cognito:username': USERNAME,
                                              'email': 'alice@example.com',
                                              'token_use': 'id'})
    ctx = McpContext(request_id=f'turn-{uuid.uuid4().hex[:8]}/1', tool=DISABLE.name,
                     server='dda-portal-assistant@1.0.0', client='dda-portal-frontend/unknown',
                     principal_email=caller.email, channel='assistant')
    outcome = pipeline.call_tool(DISABLE, {'account_username': USERNAME, 'confirm': USERNAME},
                                 caller, ctx, mutating_enabled=True, event_name='assistant_tool')
    assert outcome.category == 'conflict' and outcome.failure.status == 409
    assert outcome.dispatched is False and outcome.envelope_written is True
    assert portal.calls == []
    items = audit_rows(portal.stack, sub)
    assert len(items) == 1
    assert items[0]['action'] == 'mcp_tool_call' and items[0]['channel'] == 'assistant'
    assert items[0]['details']['error_category'] == 'conflict'
    assert_marked(items, DISABLE, channel='assistant')
    # With an `email` claim the principal needs no lookup: step 5b's read only.
    assert recorder.calls == [True]


# ------------------------------- Property 4: marked audit entries per completion

def handler_writing(portal, writes, status, body):
    """The fake backing handler: `writes` entries through the real
    `log_audit_event` (marked by the request audit context, under the
    handler's own trace id), then `(status, body)`."""
    def answer(_arn, event):
        shared = sys.modules['shared_utils']
        portal.monkeypatch.setenv('_X_AMZN_TRACE_ID', f'Root=1-{uuid.uuid4().hex}')
        try:
            user = shared.get_user_from_event(event)
            username = (event.get('pathParameters') or {}).get('username') or \
                json.loads(event.get('body') or '{}').get('username')
            for action, result, details in writes:
                shared.log_audit_event(user['user_id'], action, 'user_account', username,
                                       result, details)
                time.sleep(0.002)   # the table key is f'{user_id}_{ms}'
        finally:
            portal.monkeypatch.setenv('_X_AMZN_TRACE_ID', 'Root=1-mcp-lambda')
        return status, body
    portal.answer = answer


GUARD = f'{TARGET} is the last remaining enabled PortalAdmin account'
CREATED_DETAILS = {'username': TARGET, 'email': TARGET_EMAIL, 'role': 'Viewer',
                   'created_user_id': 'fake-sub-new-1',
                   'registry_entry': {'user_id': 'fake-sub-new-1', 'usecase_id': 'global',
                                      'role': 'Viewer'}}
# (id, spec, the handler's entries, status, body, total entries, envelope result)
COUNTS = [
    ('create-201', CREATE, [('account_create', 'success', CREATED_DETAILS)], 201,
     {'message': 'created', 'username': TARGET, 'email': TARGET_EMAIL, 'role': 'Viewer'},
     1, None),
    ('create-400', CREATE, [], 400,
     {'error': 'Invalid account request', 'field': 'email', 'message': 'email is invalid'},
     1, 'failure'),
    ('create-409', CREATE, [('account_create', 'failure', {'reason': 'username exists'})],
     409, {'error': 'username already exists', 'message': f'{TARGET} exists'}, 2, 'failure'),
    ('create-500', CREATE, [], 500,
     {'error': 'Audit log unavailable', 'message': 'The action was not applied'},
     1, 'failure'),
    ('create-502', CREATE, [('account_create', 'failure', {'reason': 'pool error'})], 502,
     {'error': 'account was not created'}, 2, 'failure'),
    ('create-403', CREATE, [('unauthorized_access', 'denied', {'required': 'PortalAdmin'})],
     403, {'error': 'Access denied: PortalAdmin role required'}, 1, None),
    ('disable-changed', DISABLE, [('account_disable', 'success', {'enabled': False})], 200,
     {'message': 'disabled', 'username': TARGET, 'enabled': False, 'changed': True},
     2, 'success'),
    ('disable-no-op', DISABLE, [], 200,
     {'message': 'already disabled', 'username': TARGET, 'enabled': False, 'changed': False},
     1, 'success'),
    ('disable-guard-409', DISABLE, [('account_disable', 'rejected', {'reason': GUARD})], 409,
     {'error': 'Disable rejected', 'message': GUARD}, 2, 'failure'),
    ('disable-404', DISABLE, [], 404, {'error': 'User not found'}, 1, 'failure'),
    ('disable-502', DISABLE, [('account_disable', 'failure', {'partial_state': 'registry'})],
     502, {'error': 'disable incomplete', 'message': 'retry the disable'}, 2, 'failure'),
    ('disable-403', DISABLE, [('unauthorized_access', 'denied', {'required': 'PortalAdmin'})],
     403, {'error': 'Access denied: PortalAdmin role required'}, 1, None),
    ('enable-changed', ENABLE, [('account_enable', 'success', {'enabled': True})], 200,
     {'message': 'enabled', 'username': TARGET, 'enabled': True, 'changed': True},
     2, 'success'),
    ('enable-no-op', ENABLE, [], 200,
     {'message': 'already enabled', 'username': TARGET, 'enabled': True, 'changed': False},
     1, 'success'),
    ('enable-500', ENABLE, [], 500, {'error': 'Internal server error'}, 1, 'failure'),
    ('enable-502', ENABLE, [('account_enable', 'failure', {'partial_state': 'registry'})],
     502, {'error': 'enable incomplete', 'message': 'retry the enable'}, 2, 'failure'),
    ('delete-200', DELETE, [('account_delete', 'success',
                             {'email': TARGET_EMAIL, 'role': 'Viewer'})], 200,
     {'message': 'deleted', 'username': TARGET, 'email': TARGET_EMAIL, 'role': 'Viewer',
      'deleted': True}, 1, None),
    ('delete-guard-409', DELETE, [('account_delete', 'rejected', {'reason': GUARD})], 409,
     {'error': 'Deletion rejected', 'message': GUARD}, 2, 'failure'),
    ('delete-404', DELETE, [], 404,
     {'error': 'User not found', 'message': f'The account {TARGET} was not found'},
     1, 'failure'),
    ('delete-502-partial', DELETE, [('account_delete', 'success',
                                     {'email': TARGET_EMAIL, 'role': 'Viewer',
                                      'partial_cleanup': 'verifier record'})], 502,
     {'error': 'partial deletion', 'message': 'removed on a subsequent attempt'},
     2, 'failure'),
]


@pytest.mark.parametrize('case', COUNTS, ids=[c[0] for c in COUNTS])
def test_marked_entries_per_completion(portal, case):
    """**Feature: mcp-user-operations, Property 4: Marked audit entries per Phase-4 completion**"""
    label, spec, writes, status, body, total, envelope_result = case
    token = seeded_token(portal.stack)
    handler_writing(portal, writes, status, body)
    result = call(spec, args_for(spec, TARGET), token)
    assert len(portal.calls) == 1
    assert result.is_error is (status >= 300), result.structured_content
    items = audit_rows(portal.stack, token.subject)
    assert len(items) == total, [(i['action'], i['result']) for i in items]
    assert_marked(items, spec)
    found = envelopes(items)
    if envelope_result is None:
        assert found == []
    else:
        [envelope] = found
        assert envelope['result'] == envelope_result
        assert envelope['details']['status'] == status
        assert envelope['resource_type'] == 'account' and envelope['resource_id'] == TARGET
        assert envelope['details']['account_username'] == TARGET
        if spec is CREATE:
            assert envelope['details']['role'] == 'Viewer'
        if envelope_result == 'failure':
            assert envelope['details']['error_category'] == category_for(status)
    if label == 'create-201':
        [entry] = items
        assert (entry['action'], entry['result']) == ('account_create', 'success')
        assert {'email', 'role', 'created_user_id', 'registry_entry'} <= set(entry['details'])


PRE_DISPATCH = (
    [(spec, category) for spec in ACCOUNT_TOOLS
     for category in ('invalid_input', 'mutating_tools_disabled', 'not_authorized')]
    + [(spec, 'confirmation_required') for spec in (DISABLE, ENABLE, DELETE)]
    + [(spec, 'conflict') for spec in PROTECTED])


@pytest.mark.parametrize('spec,category', PRE_DISPATCH,
                         ids=[f'{s.name}-{c}' for s, c in PRE_DISPATCH])
def test_every_pre_dispatch_refusal_leaves_exactly_one_entry(portal, spec, category):
    token = seeded_token(portal.stack, role='Viewer' if category == 'not_authorized'
                         else 'PortalAdmin')
    args = args_for(spec, TARGET)
    if category == 'invalid_input':
        args['account_username'] = 'bad/name'
    elif category == 'confirmation_required':
        del args['confirm']
    elif category == 'mutating_tools_disabled':
        portal.monkeypatch.setenv('MCP_MUTATING_TOOLS_ENABLED', 'false')
    elif category == 'conflict':
        args = args_for(spec, token.claims['username'])
    assert call(spec, args, token).structured_content['category'] == category
    assert portal.calls == []
    [item] = audit_rows(portal.stack, token.subject)
    assert_marked([item], spec)
    if category == 'not_authorized':
        assert item['action'] == 'unauthorized_access' and item['result'] == 'denied'
        return
    assert item['action'] == 'mcp_tool_call' and item['result'] == 'failure'
    assert item['details']['error_category'] == category
    assert item['details']['status'] is None
    assert item['resource_id'] == item['details']['account_username'] == args['account_username']
    if spec is CREATE:
        assert item['details']['role'] == 'Viewer'


class ThrottledLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        raise ClientError({'Error': {'Code': 'TooManyRequestsException',
                                     'Message': 'Rate exceeded'}}, 'Invoke')


@pytest.mark.parametrize('spec', ACCOUNT_TOOLS, ids=tool_id)
def test_a_throttled_dispatch_leaves_one_envelope_with_a_null_status(portal, spec):
    throttled = ThrottledLambda()
    portal.monkeypatch.setattr(dispatch, 'LAMBDA', throttled)
    token = seeded_token(portal.stack)
    content = call(spec, args_for(spec, TARGET), token).structured_content
    assert content['category'] == 'throttled' and content['status'] == 429
    assert len(throttled.calls) == 1
    [envelope] = audit_rows(portal.stack, token.subject)
    assert envelope['details']['status'] is None
    assert envelope['details']['error_category'] == 'throttled'
    assert envelope['resource_id'] == envelope['details']['account_username'] == TARGET


# ---------------------------------------- Property 8: the dispatched event

def _keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _keys(nested)


def assert_clean_event(event, token, spec):
    assert event['requestContext']['authorizer'] == {'claims': token.claims}
    assert event['mcp']['tool'] == spec.name and event['mcp']['channel'] == 'mcp'
    assert 'confirm' not in set(_keys(event))
    assert token.token not in json.dumps(event)


def test_an_email_alias_target_is_percent_encoded_in_the_path(portal):
    token = seeded_token(portal.stack)
    alias = 'alice@example.com'
    assert call(DISABLE, args_for(DISABLE, alias), token).is_error is False
    [event] = portal.events
    assert (event['httpMethod'], event['resource']) == ('POST', '/admin/users/{username}/disable')
    assert event['path'] == '/admin/users/alice%40example.com/disable'
    assert event['pathParameters'] == {'username': alias}
    assert event['body'] == '{}' and event['queryStringParameters'] is None
    assert_clean_event(event, token, DISABLE)


def test_create_sends_the_three_fields_in_the_body(portal):
    token = seeded_token(portal.stack)
    assert call(CREATE, args_for(CREATE, TARGET), token).is_error is False
    [event] = portal.events
    assert (event['httpMethod'], event['path']) == ('POST', '/admin/users')
    assert event['pathParameters'] is None and event['queryStringParameters'] is None
    assert json.loads(event['body']) == {'username': TARGET, 'email': TARGET_EMAIL,
                                         'role': 'Viewer'}
    assert_clean_event(event, token, CREATE)


def test_delete_sends_no_body(portal):
    token = seeded_token(portal.stack)
    assert call(DELETE, args_for(DELETE, TARGET), token).is_error is False
    [event] = portal.events
    assert (event['httpMethod'], event['path']) == ('DELETE', f'/admin/users/{TARGET}')
    assert event['pathParameters'] == {'username': TARGET} and event['body'] is None
    assert_clean_event(event, token, DELETE)
