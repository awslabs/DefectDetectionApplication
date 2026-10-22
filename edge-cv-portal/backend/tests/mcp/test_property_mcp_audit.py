"""Property test: marked audit entries per completion.
**Feature: mcp-servers, Property 6: Marked audit entries per completion**
Spec: .kiro/specs/mcp-servers/ (design.md §2 `audit.py` row, D6, N8, D36,
§4 steps 5-9, Correctness Property 6; tasks.md 6.9).
Validates: Requirements 4.1, 4.2, 4.3, 4.5, 16.4.

For any catalog tool, `handler_audits` / `handler_audits_denials` flags,
`k in {0, 1, 2}` entries written by a fake backing handler through the real
`log_audit_event` (without `identity`/`event`, under its own trace id), and
any outcome (success, backend error, handler-side failure, denial,
authorization unavailable, pre-dispatch failure, and for the
`bind_usecase_path` spec the D36 refusal of a 2xx body):

* every entry written for the completion carries the five marker
  attributes and no denylisted detail key;
* success with honest flags (`handler_audits == (k >= 1)`) leaves exactly
  `max(k, 1)` entries, the envelope present iff `k == 0`; with dishonest
  flags the count is `k + (not handler_audits)`;
* every pre-dispatch outcome leaves exactly one entry (the envelope, or the
  marked denial / unavailability entry `authz` wrote);
* a handler-side failure leaves at least one entry;
* a D36 refusal leaves `k + 1`, the extra one the `denied` envelope with
  `details.reason='usecase_binding'`, whatever `handler_audits` says.
"""
import dataclasses
import io
import json
import sys
import time
import uuid
from types import SimpleNamespace

import pytest
from boto3.dynamodb.conditions import Attr
from hypothesis import HealthCheck, assume, event, given, settings
from hypothesis import strategies as st

from _schema_strategy import arguments
from mcp_portal import audit, authz, dispatch, shaping
from mcp_portal import catalog as cat
from mcp_portal.dispatch import McpContext
from mcp_portal.errors import ToolFailure
from mcp_portal.principal import Principal

SETTINGS = settings(max_examples=100, deadline=None,
                    suppress_health_check=[HealthCheck.function_scoped_fixture,
                                           HealthCheck.too_slow,
                                           HealthCheck.filter_too_much])
MARKER = ('channel', 'mcp_tool', 'mcp_server', 'mcp_client', 'mcp_request_id')
DENYLISTED = ('password', 'verifier', 'hash')     # plus the `temp` prefix
BIND_SPEC = next(s for s in cat.CATALOG if s.bind_usecase_path)
PRE_DISPATCH = ('invalid_input', 'confirmation_required', 'mutating_tools_disabled',
                'timeout', 'backend_failure')
ERROR_STATUSES = (400, 401, 403, 404, 409, 422, 429, 500, 502, 503)
OUTCOMES = ('success', 'backend_error', 'handler_crash', 'denied', 'unavailable',
            'pre_dispatch', 'refusal')


@pytest.fixture
def env(aws_stack, monkeypatch):
    shared = sys.modules['shared_utils']
    monkeypatch.setattr(shared, '_REQUEST_AUDIT_CONTEXT', None)
    monkeypatch.delenv('PORTAL_REGISTRY_ENFORCED', raising=False)
    monkeypatch.setenv('MCP_BACKEND_FUNCTIONS_PARAMETER', '/dda/mcp/backend-functions')
    monkeypatch.setattr(dispatch, 'LAMBDA_CONTEXT', None)
    dispatch.reset_backend_functions()
    keys = {s.backend for s in cat.CATALOG if s.backend}
    monkeypatch.setattr(dispatch, 'SSM', SimpleNamespace(get_parameter=lambda **_: {
        'Parameter': {'Value': json.dumps({k: f'arn:fn:{k}' for k in keys})}}))
    yield SimpleNamespace(shared=shared, stack=aws_stack, monkeypatch=monkeypatch)
    dispatch.reset_backend_functions()


class InProcessLambda:
    """`lambda:Invoke` running the handler under its own trace id; a raising
    handler is reported the way Lambda reports it (`FunctionError`)."""

    def __init__(self, monkeypatch, handler):
        self.monkeypatch, self.handler = monkeypatch, handler

    def invoke(self, **kwargs):
        self.monkeypatch.setenv('_X_AMZN_TRACE_ID', f'Root=1-{uuid.uuid4().hex}')
        try:
            response = self.handler(json.loads(kwargs['Payload']), None)
            reply = {'StatusCode': 200,
                     'Payload': io.BytesIO(json.dumps(response).encode())}
        except RuntimeError as error:
            reply = {'StatusCode': 200, 'FunctionError': 'Unhandled',
                     'Payload': io.BytesIO(json.dumps({'errorMessage': str(error)}).encode())}
        self.monkeypatch.setenv('_X_AMZN_TRACE_ID', 'Root=1-mcp-lambda')
        return reply


def fake_handler(k, status, body, crash=False):
    def handler(event, _context):
        shared = sys.modules['shared_utils']
        user = shared.get_user_from_event(event)
        for index in range(k):
            shared.log_audit_event(user['user_id'], f'fake_action_{index}', 'fixture',
                                   f'r{index}', 'success' if status < 300 else 'failure',
                                   {'index': index})
            time.sleep(0.002)   # the table key is f'{user_id}_{ms}'
        if crash:
            raise RuntimeError('handler failed after writing its entries')
        return {'statusCode': status, 'body': json.dumps(body)}
    return handler


def nested(path, value):
    """`{'a': {'b': value}}` for the dotted path `a.b`."""
    out = value
    for segment in reversed(path.split('.')):
        out = {segment: out}
    return out


def principal_for(sub):
    claims = {'sub': sub, 'username': f'user-{sub[:8]}', 'client_id': 'mcp-client',
              'token_use': 'access', 'scope': 'dda-portal/portal.tools'}
    return Principal(sub=sub, username=claims['username'], email='person@example.com',
                     claims=claims)


def context_for(spec):
    return McpContext(request_id=f'req-{uuid.uuid4().hex[:8]}', tool=spec.name,
                      server=f'dda-portal-mcp/{spec.domain}@1.0.0', client='kiro/1.2.3',
                      source_ip='203.0.113.7', user_agent='kiro/1.2.3',
                      principal_email='person@example.com', identity_source='registry')


@st.composite
def cases(draw):
    outcome = draw(st.sampled_from(OUTCOMES))
    base = BIND_SPEC if outcome == 'refusal' else draw(st.sampled_from(cat.CATALOG))
    spec = dataclasses.replace(base, handler_audits=draw(st.booleans()),
                               handler_audits_denials=draw(st.booleans()))
    args = draw(arguments(base))
    k = draw(st.integers(0, 2))
    status = draw(st.sampled_from(ERROR_STATUSES)) if outcome == 'backend_error' else 200
    return SimpleNamespace(outcome=outcome, spec=spec, args=args, k=k, status=status,
                           pre_category=draw(st.sampled_from(PRE_DISPATCH)),
                           refusal_value=draw(st.sampled_from([None, 'other-usecase'])))


def run_case(env, case):
    """§4 steps 5-9 for one call; returns the subject and the envelope flag."""
    spec, args = case.spec, case.args
    sub = f'p6-{uuid.uuid4()}'
    principal, ctx = principal_for(sub), context_for(spec)
    event = dispatch.build_backend_event(spec, args, principal, ctx)
    usecase_id = args.get('usecase_id')

    if case.outcome in ('denied', 'unavailable'):
        shared = env.shared

        def has_permission(*_a, **_k):
            if case.outcome == 'unavailable':
                raise shared.RegistryUnavailable('registry read failed')
            return False
        assume(spec.permissions)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(shared.rbac_manager, 'has_permission', has_permission)
            patch.setattr(shared.rbac_manager, 'get_user_role', lambda *_a, **_k: None)
            decision = authz.authorize(spec, principal, usecase_id, mcp_event=event)
        assert not decision.allowed
        category = 'not_authorized' if case.outcome == 'denied' else 'authorization_unavailable'
        outcome = audit.Outcome(dispatched=False, category=category, args=args,
                                usecase_id=usecase_id)
        return sub, audit.write_envelope(spec, principal, ctx, outcome, 3)

    if case.outcome == 'pre_dispatch':
        outcome = audit.Outcome(dispatched=False, category=case.pre_category, args=args,
                                usecase_id=usecase_id)
        return sub, audit.write_envelope(spec, principal, ctx, outcome, 3)

    if case.outcome == 'refusal':
        body = {} if case.refusal_value is None else nested(spec.bind_usecase_path,
                                                            case.refusal_value)
        assume(usecase_id != case.refusal_value)
    elif case.outcome == 'success' and spec.bind_usecase_path:
        body = nested(spec.bind_usecase_path, usecase_id)
    elif case.outcome == 'backend_error':
        body = {'error': 'backend said no'}
    else:
        body = {}
    handler = fake_handler(case.k, case.status, body, crash=case.outcome == 'handler_crash')
    env.monkeypatch.setattr(dispatch, 'LAMBDA', InProcessLambda(env.monkeypatch, handler))
    response = dispatch.invoke(spec, event)
    if isinstance(response, ToolFailure):
        shaped, status = shaping.Shaped(failure=response), None
    else:
        shaped, status = shaping.shape(spec, args, response), response.status
    if case.outcome == 'success':
        assume(shaped.failure is None)     # e.g. an over-large body is not this clause
    if case.outcome == 'refusal':
        assert shaped.binding_refused
    outcome = audit.Outcome(dispatched=True,
                            category=shaped.failure.category if shaped.failure else None,
                            binding_refused=shaped.binding_refused, args=args,
                            usecase_id=usecase_id)
    written = audit.write_envelope(spec, principal, ctx, outcome, 12, status,
                                   shaped.resource_id, mcp_event=event)
    return sub, written


def entries(env, sub):
    return env.stack.tables.audit_log.scan(FilterExpression=Attr('user_id').eq(sub))['Items']


@SETTINGS
@given(case=cases())
def test_marked_audit_entries_per_completion(env, case):
    spec, k = case.spec, case.k
    if case.outcome == 'backend_error' and case.status == 403 and spec.handler_audits_denials:
        assume(k >= 1)     # an honest denial-auditing route wrote its denial
    sub, written = run_case(env, case)
    event(f'outcome={case.outcome} k={k}')
    items = entries(env, sub)
    envelopes = [i for i in items if i['action'] == audit.ACTION]
    assert len(envelopes) == int(written)

    for item in items:
        assert item['channel'] == 'mcp', item['action']
        assert item['mcp_tool'] == spec.name
        for field in MARKER:
            assert item[field] and item[field] != 'unknown', (item['action'], field)
        assert not [key for key in item.get('details', {})
                    if any(term in str(key).lower() for term in DENYLISTED)
                    or str(key).lower().startswith('temp')]

    if case.outcome == 'success':
        if spec.handler_audits == (k >= 1):
            assert len(items) == max(k, 1)
            assert bool(envelopes) == (k == 0)
        else:
            assert len(items) == k + (0 if spec.handler_audits else 1)
        assert all(e['result'] == 'success' for e in envelopes)
    elif case.outcome in ('denied', 'unavailable', 'pre_dispatch'):
        assert len(items) == 1
        expected = {'denied': authz.UNAUTHORIZED_ACTION,
                    'unavailable': authz.AUTHORIZATION_UNAVAILABLE_ACTION,
                    'pre_dispatch': audit.ACTION}[case.outcome]
        assert items[0]['action'] == expected
    elif case.outcome in ('backend_error', 'handler_crash'):
        assert len(items) >= 1
        assert len(items) == k + int(written)
        assert all(e['result'] == 'failure' for e in envelopes)
    else:  # refusal
        assert len(items) == k + 1
        [envelope] = envelopes
        assert envelope['result'] == 'denied'
        assert envelope['details']['reason'] == audit.BINDING_REASON
        assert envelope['details']['status'] == 200
