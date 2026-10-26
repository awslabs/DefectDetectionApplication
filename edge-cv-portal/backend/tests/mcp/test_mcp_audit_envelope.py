"""Audit envelope and the per-invocation log line (`audit.py`, `telemetry.py`).

Spec: .kiro/specs/mcp-servers/ (design.md §2 `audit.py` and `telemetry.py`
rows, D6, N8, D36, §4 steps 7-9, Data Models "Audit log entry" and "Portal
per-invocation log line", Error Handling "Use_Case binding" and "Audit
envelope write"; Testing Strategy row "16.4 audit, H6"; tasks.md 6.8).
Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 6.3, 16.4.

The backing handler is a fake that runs in process behind a fake Lambda
client and uses the real `shared_utils.get_user_from_event`,
`log_audit_event` (without `identity`/`event`) and
`record_audit_event_strict`, under its own `_X_AMZN_TRACE_ID`, so the
request audit context marks its entries end to end. The audit table is the
moto session table.
"""
import dataclasses
import io
import json
import logging
import sys
import time
import uuid
from types import SimpleNamespace

import pytest
from boto3.dynamodb.conditions import Attr

from mcp_portal import audit, dispatch, shaping, telemetry
from mcp_portal import catalog as cat
from mcp_portal.dispatch import BackendResponse, McpContext
from mcp_portal.errors import ToolFailure
from mcp_portal.principal import Principal

BY_NAME = {s.name: s for s in cat.CATALOG}
JOB_GET = BY_NAME['portal_data_labeling_job_get']      # bind_usecase_path, audits nothing
DEVICES = BY_NAME['portal_devices_list']               # handler_audits
CAMERAS = BY_NAME['portal_devices_cameras_list']       # handler_audits_denials only
MARKER = ('channel', 'mcp_tool', 'mcp_server', 'mcp_client', 'mcp_request_id')
DENYLISTED = ('password', 'verifier', 'hash')
TOKEN = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl'


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
    """`lambda:Invoke` that runs `handler(event, None)` under a fresh trace id."""

    def __init__(self, monkeypatch, handler):
        self.monkeypatch, self.handler, self.calls = monkeypatch, handler, 0

    def invoke(self, **kwargs):
        self.calls += 1
        self.monkeypatch.setenv('_X_AMZN_TRACE_ID', f'Root=1-{uuid.uuid4().hex}')
        response = self.handler(json.loads(kwargs['Payload']), None)
        self.monkeypatch.setenv('_X_AMZN_TRACE_ID', 'Root=1-mcp-lambda')
        return {'StatusCode': 200, 'Payload': io.BytesIO(json.dumps(response).encode())}


def fake_handler(k=0, status=200, body=None, result='success'):
    """A backing handler writing `k` entries without `identity`/`event`."""
    def handler(event, _context):
        shared = sys.modules['shared_utils']
        user = shared.get_user_from_event(event)
        for index in range(k):
            shared.log_audit_event(user['user_id'], f'fake_action_{index}', 'fixture',
                                   f'r{index}', result,
                                   {'index': index})
            time.sleep(0.002)   # the table key is f'{user_id}_{ms}'
        return {'statusCode': status, 'body': json.dumps(body if body is not None else {})}
    return handler


def principal_for(sub):
    claims = {'sub': sub, 'username': f'user-{sub[:8]}', 'client_id': 'mcp-client',
              'token_use': 'access', 'scope': 'dda-portal/portal.tools'}
    return Principal(sub=sub, username=claims['username'], email='person@example.com',
                     claims=claims)


def context_for(spec, identity_source='registry'):
    return McpContext(request_id=f'req-{uuid.uuid4().hex[:8]}', tool=spec.name,
                      server=f'dda-portal-mcp/{spec.domain}@1.0.0', client='kiro/1.2.3',
                      source_ip='203.0.113.7', user_agent='kiro/1.2.3',
                      principal_email='person@example.com',
                      identity_source=identity_source)


def run_call(env, spec, args, handler):
    """§4 steps 6-9 for one call: dispatch, binding check and shaping, envelope."""
    sub = f'env-{uuid.uuid4()}'
    principal, ctx = principal_for(sub), context_for(spec)
    event = dispatch.build_backend_event(spec, args, principal, ctx)
    env.monkeypatch.setattr(dispatch, 'LAMBDA', InProcessLambda(env.monkeypatch, handler))
    response = dispatch.invoke(spec, event)
    if isinstance(response, ToolFailure):
        shaped, status = shaping.Shaped(failure=response), None
    else:
        shaped, status = shaping.shape(spec, args, response), response.status
    category = shaped.failure.category if shaped.failure else None
    outcome = audit.Outcome(dispatched=True, category=category,
                            binding_refused=shaped.binding_refused, args=args,
                            usecase_id=args.get('usecase_id'))
    written = audit.write_envelope(spec, principal, ctx, outcome, 12, status,
                                   shaped.resource_id, mcp_event=event)
    return SimpleNamespace(sub=sub, shaped=shaped, status=status, written=written,
                           result=shaped.to_result(ctx.request_id), ctx=ctx)


def entries(env, sub):
    return env.stack.tables.audit_log.scan(FilterExpression=Attr('user_id').eq(sub))['Items']


def envelopes(items):
    return [i for i in items if i['action'] == audit.ACTION]


def assert_marked(items, spec):
    for item in items:
        assert item['channel'] == 'mcp', item['action']
        assert item['mcp_tool'] == spec.name
        for field in MARKER:
            assert item[field] and item[field] != 'unknown', (item['action'], field)
        assert not [k for k in item['details']
                    if any(term in str(k).lower() for term in DENYLISTED)]


# --------------------------------------------------------- count contract

def test_no_handler_entry_yields_exactly_one_envelope(env):
    call = run_call(env, JOB_GET, {'usecase_id': 'A', 'job_id': 'j1'},
                    fake_handler(0, 200, {'job': {'job_id': 'j1', 'usecase_id': 'A'}}))
    items = entries(env, call.sub)
    assert len(items) == 1 and call.written
    envelope = items[0]
    assert envelope['action'] == 'mcp_tool_call' and envelope['result'] == 'success'
    assert envelope['resource_type'] == JOB_GET.resource_type
    assert envelope['resource_id'] == 'j1'
    assert envelope['details'] == {'tool': JOB_GET.name, 'status': 200, 'duration_ms': 12,
                                   'usecase_id': 'A'}
    assert envelope['email'] == 'person@example.com'
    assert envelope['identity_source'] == 'registry'
    assert_marked(items, JOB_GET)


@pytest.mark.parametrize('k', [1, 2])
def test_handler_entries_replace_the_envelope(env, k):
    call = run_call(env, DEVICES, {'usecase_id': 'uc-1'},
                    fake_handler(k, 200, {'devices': []}))
    items = entries(env, call.sub)
    assert not call.written and envelopes(items) == []
    assert len(items) == max(k, 1) == k
    assert_marked(items, DEVICES)


def test_envelope_details_are_sanitized(env):
    spec = dataclasses.replace(DEVICES, handler_audits=False,
                               audit_args=('device_id', 'temp_code'))
    sub = f'env-{uuid.uuid4()}'
    args = {'usecase_id': 'uc-1', 'device_id': 'd1', 'temp_code': 'x'}
    outcome = audit.Outcome(dispatched=True, args=args, usecase_id='uc-1')
    assert audit.write_envelope(spec, principal_for(sub), context_for(spec), outcome, 1, 200)
    [item] = entries(env, sub)
    assert item['details']['device_id'] == 'd1' and 'temp_code' not in item['details']
    assert_marked([item], spec)


def test_backend_error_writes_a_failure_envelope(env):
    call = run_call(env, DEVICES, {'usecase_id': 'uc-1'},
                    fake_handler(1, 500, {'error': 'Traceback (most recent call last)'},
                                 result='failure'))
    items = entries(env, call.sub)
    [envelope] = envelopes(items)
    assert envelope['result'] == 'failure'
    assert envelope['details']['error_category'] == 'backend_failure'
    assert envelope['details']['status'] == 500
    assert len(items) == 2      # the handler's own (marked) failure entry is kept
    assert_marked(items, DEVICES)


def test_403_from_a_denial_auditing_route_writes_no_envelope(env):
    assert CAMERAS.handler_audits_denials and not CAMERAS.handler_audits
    call = run_call(env, CAMERAS, {'usecase_id': 'uc-1'},
                    fake_handler(1, 403, {'error': 'Insufficient permissions'},
                                 result='denied'))
    items = entries(env, call.sub)
    assert not call.written and envelopes(items) == [] and len(items) == 1
    assert call.shaped.failure.category == 'not_authorized'
    assert_marked(items, CAMERAS)


def test_403_from_another_route_writes_an_envelope(env):
    assert not DEVICES.handler_audits_denials
    call = run_call(env, DEVICES, {'usecase_id': 'uc-1'},
                    fake_handler(0, 403, {'error': 'no'}))
    [envelope] = envelopes(entries(env, call.sub))
    assert envelope['result'] == 'failure'
    assert envelope['details']['error_category'] == 'not_authorized'


def test_strict_audit_failure_aborts_the_effect(env):
    effects = []

    def handler(event, _context):
        shared = sys.modules['shared_utils']
        user = shared.get_user_from_event(event)
        try:
            shared.record_audit_event_strict(user['user_id'], 'fake_effect', 'fixture', 'r')
        except Exception:  # noqa: BLE001 - the strict write raised: no effect
            return {'statusCode': 500, 'body': json.dumps({'error': 'audit unavailable'})}
        effects.append('applied')
        return {'statusCode': 200, 'body': '{}'}

    shared = env.shared

    class _Broken:
        def put_item(self, **_kwargs):
            raise RuntimeError('audit table unavailable')

    real = shared.dynamodb

    class _Resource:
        def Table(self, name):  # noqa: N802 - boto3 resource API
            return _Broken() if name == shared.AUDIT_LOG_TABLE else real.Table(name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(shared, 'dynamodb', _Resource())
        call = run_call(env, DEVICES, {'usecase_id': 'uc-1'}, handler)
    assert effects == []
    assert call.shaped.failure.category == 'backend_failure'
    assert call.result.is_error is True and call.written


def test_invoke_failure_writes_an_envelope(env):
    def handler(_event, _context):
        raise AssertionError('not reached')

    class _Timeout:
        def invoke(self, **_kwargs):
            from botocore.exceptions import ReadTimeoutError
            raise ReadTimeoutError(endpoint_url='https://lambda')

    sub = f'env-{uuid.uuid4()}'
    principal, ctx = principal_for(sub), context_for(DEVICES)
    event = dispatch.build_backend_event(DEVICES, {'usecase_id': 'uc-1'}, principal, ctx)
    env.monkeypatch.setattr(dispatch, 'LAMBDA', _Timeout())
    failure = dispatch.invoke(DEVICES, event)
    assert failure.category == 'timeout'
    outcome = audit.Outcome(dispatched=True, category='timeout', args={'usecase_id': 'uc-1'},
                            usecase_id='uc-1')
    assert audit.write_envelope(DEVICES, principal, ctx, outcome, 20_000, None,
                                mcp_event=event)
    [envelope] = envelopes(entries(env, sub))
    assert envelope['details']['error_category'] == 'timeout'
    assert envelope['details']['status'] is None


# ------------------------------------------------------------ pre-dispatch

@pytest.mark.parametrize('category', ['invalid_input', 'confirmation_required',
                                      'mutating_tools_disabled', 'timeout',
                                      'backend_failure'])
def test_pre_dispatch_outcomes_write_one_envelope(env, category):
    sub = f'env-{uuid.uuid4()}'
    principal, ctx = principal_for(sub), context_for(DEVICES, identity_source=None)
    outcome = audit.Outcome(dispatched=False, category=category,
                            args={'usecase_id': 'uc-1'}, usecase_id='uc-1')
    assert audit.write_envelope(DEVICES, principal, ctx, outcome, 3)
    items = entries(env, sub)
    assert len(items) == 1 and items[0]['result'] == 'failure'
    assert items[0]['details']['error_category'] == category
    assert items[0]['resource_id'] == 'collection'
    assert_marked(items, DEVICES)


@pytest.mark.parametrize('category', ['not_authorized', 'authorization_unavailable'])
def test_authz_outcomes_write_no_envelope(env, category):
    sub = f'env-{uuid.uuid4()}'
    outcome = audit.Outcome(dispatched=False, category=category)
    assert not audit.write_envelope(DEVICES, principal_for(sub), context_for(DEVICES),
                                    outcome, 3)
    assert entries(env, sub) == []


def test_envelope_required_rule():
    ok = audit.Outcome(dispatched=True)
    refused = audit.Outcome(dispatched=True, category='not_found', binding_refused=True)
    assert audit.envelope_required(JOB_GET, ok, 200)
    assert not audit.envelope_required(DEVICES, ok, 200)
    assert audit.envelope_required(DEVICES, refused, 200)       # D36 even when it audits
    assert audit.envelope_required(DEVICES, ok, 500)
    assert audit.envelope_required(DEVICES, ok, 403)
    assert not audit.envelope_required(CAMERAS, ok, 403)
    assert audit.envelope_required(CAMERAS, ok, 404)
    assert audit.envelope_required(DEVICES, ok, None)


# ------------------------------------------------------------- D36 binding

def test_other_usecase_object_is_refused_and_audited_denied(env):
    call = run_call(env, JOB_GET, {'usecase_id': 'A', 'job_id': 'j1'},
                    fake_handler(0, 200, {'job': {'job_id': 'j1', 'usecase_id': 'B',
                                                  'name': 'secret-name'}}))
    assert call.shaped.binding_refused and call.result.is_error
    rendered = json.dumps(call.result.model_dump(mode='json'))
    assert 'secret-name' not in rendered and '"B"' not in rendered and 'job' not in \
        json.dumps(call.result.structured_content)
    [envelope] = entries(env, call.sub)
    assert envelope['action'] == 'mcp_tool_call' and envelope['result'] == 'denied'
    assert envelope['details']['reason'] == 'usecase_binding'
    assert envelope['details']['status'] == 200
    assert envelope['details']['error_category'] == 'not_found'


def test_matching_usecase_returns_the_job(env):
    body = {'job': {'job_id': 'j1', 'usecase_id': 'B'}}
    call = run_call(env, JOB_GET, {'usecase_id': 'B', 'job_id': 'j1'},
                    fake_handler(0, 200, body))
    assert call.result.is_error is False
    assert call.result.structured_content == {'result': body}
    [envelope] = entries(env, call.sub)
    assert envelope['result'] == 'success' and 'reason' not in envelope['details']


def test_body_without_the_path_is_refused_alike(env):
    call = run_call(env, JOB_GET, {'usecase_id': 'A', 'job_id': 'j1'},
                    fake_handler(0, 200, {'job': {'job_id': 'j1'}}))
    assert call.shaped.binding_refused
    [envelope] = entries(env, call.sub)
    assert envelope['result'] == 'denied' and envelope['details']['reason'] == 'usecase_binding'


@pytest.mark.parametrize('k', [0, 1, 2])
def test_refusal_with_handler_entries_leaves_k_plus_one(env, k):
    call = run_call(env, JOB_GET, {'usecase_id': 'A', 'job_id': 'j1'},
                    fake_handler(k, 200, {'job': {'usecase_id': 'B'}}))
    items = entries(env, call.sub)
    assert len(items) == k + 1 and len(envelopes(items)) == 1
    assert_marked(items, JOB_GET)


def test_missing_id_and_other_usecase_results_are_identical(env):
    missing = run_call(env, JOB_GET, {'usecase_id': 'A', 'job_id': 'nope'},
                       fake_handler(0, 404, {'error': 'Labeling job not found'}))
    other = run_call(env, JOB_GET, {'usecase_id': 'A', 'job_id': 'j1'},
                     fake_handler(0, 200, {'job': {'job_id': 'j1', 'usecase_id': 'B'}}))
    first, second = missing.result, other.result
    assert first.is_error is second.is_error is True
    assert first.content == second.content
    assert first.model_dump(exclude={'structured_content'}) == \
        second.model_dump(exclude={'structured_content'})
    strip = lambda r: {k: v for k, v in r.structured_content.items() if k != 'request_id'}
    assert strip(first) == strip(second)
    assert first.structured_content['status'] == 404 == second.structured_content['status']
    # The real backend status is in the envelope only.
    [m] = entries(env, missing.sub)
    [o] = entries(env, other.sub)
    assert m['details']['status'] == 404 and o['details']['status'] == 200


# ------------------------------------------------------ browser after MCP

def test_browser_invocation_after_mcp_is_unmarked(env):
    run_call(env, DEVICES, {'usecase_id': 'uc-1'}, fake_handler(1, 200, {'devices': []}))
    shared = env.shared
    env.monkeypatch.setenv('_X_AMZN_TRACE_ID', f'Root=1-{uuid.uuid4().hex}')
    web_sub = f'web-{uuid.uuid4()}'
    shared.get_user_from_event({'requestContext': {'authorizer': {'claims': {
        'sub': web_sub, 'cognito:username': 'web-user', 'email': 'web@example.com'}},
        'identity': {'sourceIp': '198.51.100.4', 'userAgent': 'Mozilla/5.0'}}})
    shared.log_audit_event(web_sub, 'list_devices', 'device', 'collection', 'success', {})
    [item] = entries(env, web_sub)
    assert item['channel'] == 'api'
    assert not [f for f in MARKER[1:] if f in item]


# ----------------------------------------------------------- telemetry line

def test_one_log_line_per_completion_without_arguments_token_or_email(env, caplog):
    principal = principal_for(f'tel-{uuid.uuid4()}')
    with caplog.at_level(logging.INFO, logger='mcp_portal.telemetry'):
        record = telemetry.log_tool_completion(
            principal=principal, tool=DEVICES.name, view='devices', category=None,
            status=200, duration_ms=412, request_id='req-9', client='kiro/1.2.3',
            server_version='1.0.0')
    lines = [r.getMessage() for r in caplog.records if r.name == 'mcp_portal.telemetry']
    assert len(lines) == 1
    logged = json.loads(lines[0])
    assert logged == record
    assert tuple(logged) == telemetry.FIELDS
    assert logged['event'] == 'mcp_tool' and logged['outcome'] == 'success'
    assert logged['sub'] == principal.sub and logged['username'] == principal.username
    assert 'person@example.com' not in lines[0] and TOKEN not in lines[0]
    assert 'usecase_id' not in lines[0] and 'args' not in logged


@pytest.mark.parametrize('category,outcome', [
    (None, 'success'), ('not_found', 'error'), ('not_authorized', 'denied'),
    ('authorization_unavailable', 'unavailable'), ('invalid_input', 'error')])
def test_log_line_outcome(category, outcome):
    assert telemetry.outcome_for(category) == outcome


def test_refusal_log_line_carries_the_real_status(caplog):
    principal = principal_for(f'tel-{uuid.uuid4()}')
    with caplog.at_level(logging.INFO, logger='mcp_portal.telemetry'):
        record = telemetry.log_tool_completion(
            principal=principal, tool=JOB_GET.name, view='data', category='not_found',
            status=200, duration_ms=5, request_id='req-10', client='kiro/1.2.3',
            server_version='1.0.0')
    assert record['category'] == 'not_found' and record['status'] == 200
    assert record['outcome'] == 'error'
