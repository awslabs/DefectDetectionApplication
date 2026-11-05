"""Error results and result shaping (`mcp_portal/errors.py`, `mcp_portal/shaping.py`).

Spec: .kiro/specs/mcp-servers/ (design.md §2 `errors.py` and `shaping.py`
rows, D32, D36, §4 step 9, Error Handling "Backend status", "Use_Case
binding", "Result shaping", Data Models "Tool error result"; tasks.md 6.5).
Requirements 1.4, 1.5, 1.6, 3.3, 2.7.

The account-tool cases at the end are .kiro/specs/mcp-user-operations/
(design A7, Property 5; tasks.md 2.11): the handler's `message` reaches
the caller through the rows' `error_detail_key`.
"""
import dataclasses
import json
import sys

import pytest
from boto3.dynamodb.conditions import Attr

import _portal_harness as h
from mcp_portal import catalog as cat
from mcp_portal import errors, shaping
from mcp_portal.dispatch import BackendResponse
from mcp_portal.errors import ToolFailure

BY_NAME = {s.name: s for s in cat.CATALOG}
JOB_GET = BY_NAME['portal_data_labeling_job_get']
DEVICES = BY_NAME['portal_devices_list']           # dispatcher-sliced collection
BUILD_LOGS = BY_NAME['portal_workflows_build_logs_get']   # forwarded cursor
DEPLOY = BY_NAME['portal_devices_deployment_create']


def response(status, body):
    return BackendResponse(status, body)


# ----------------------------------------------------------------- errors

def test_tool_failure_result_shape():
    failure = ToolFailure('not_authorized', 'Operator role or higher required', status=403)
    result = failure.to_result('req-1')
    assert result.is_error is True
    assert result.content[0].text == 'not_authorized: Operator role or higher required'
    assert result.structured_content == {'category': 'not_authorized',
                                         'message': 'Operator role or higher required',
                                         'request_id': 'req-1', 'status': 403}


def test_effect_unknown_message_names_the_status_tool():
    text = errors.effect_unknown_message(DEPLOY, 'timeout')
    assert text == (f'timeout: the operation may have been applied; call '
                    f'{DEPLOY.status_tool} before retrying')
    failure = errors.effect_unknown_failure(DEPLOY, 'backend_failure', 'plain')
    assert failure.text == errors.effect_unknown_message(DEPLOY, 'backend_failure')
    assert failure.structured('r')['effect'] == 'unknown'
    assert failure.structured('r')['status_tool'] == DEPLOY.status_tool
    plain = errors.effect_unknown_failure(DEVICES, 'backend_failure', 'plain')
    assert plain.text == 'backend_failure: plain' and 'effect' not in plain.structured('r')


@pytest.mark.parametrize('status,category', [
    (400, 'invalid_input'), (422, 'invalid_input'), (401, 'not_authenticated'),
    (403, 'not_authorized'), (404, 'not_found'), (409, 'conflict'), (429, 'throttled'),
    (500, 'backend_failure'), (502, 'backend_failure'), (503, 'backend_failure'),
])
def test_backend_status_mapping(status, category):
    failure = errors.map_backend_status(status, {'error': 'handler said no'})
    assert failure.category == category and failure.status == status
    assert failure.message == 'handler said no'


def test_scrub_removes_tracebacks_and_bearer_strings():
    trace = 'Traceback (most recent call last):\n  File "/var/task/x.py", line 1'
    assert errors.map_backend_status(500, {'error': trace}).message == 'backend_failure'
    token = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln'
    message = errors.map_backend_status(400, {'error': f'bad Bearer {token} here'}).message
    assert token not in message and 'Bearer eyJ' not in message
    assert errors.map_backend_status(400, {'error': f'jwt {token}'}).message.count('eyJ') == 0
    assert errors.map_backend_status(500, 'not json at all').message == 'not json at all'
    assert errors.map_backend_status(500, None).message == 'backend_failure'


# ------------------------------------------------------------- D36 binding

def test_binding_match_returns_the_object():
    body = {'job': {'job_id': 'j1', 'usecase_id': 'uc-1'}}
    shaped = shaping.shape(JOB_GET, {'usecase_id': 'uc-1', 'job_id': 'j1'}, response(200, body))
    assert shaped.failure is None and shaped.result == body and not shaped.binding_refused


@pytest.mark.parametrize('body', [
    {'job': {'job_id': 'j1', 'usecase_id': 'uc-other'}},   # another Use_Case
    {'job': {'job_id': 'j1'}},                              # absent value
    {'job': {'job_id': 'j1', 'usecase_id': None}},
    {'jobs': []},
])
def test_binding_mismatch_and_absent_value_drop_the_body(body):
    shaped = shaping.shape(JOB_GET, {'usecase_id': 'uc-1', 'job_id': 'j1'}, response(200, body))
    assert shaped.binding_refused is True and shaped.result is None
    assert shaped.failure == ToolFailure('not_found', 'no such object in the named Use_Case',
                                         status=404)
    rendered = json.dumps(shaped.to_result('r').model_dump(mode='json'))
    assert 'uc-other' not in rendered and 'j1' not in rendered


def test_backend_404_on_a_bound_spec_is_identical_to_the_refusal():
    args = {'usecase_id': 'uc-1', 'job_id': 'missing'}
    missing = shaping.shape(JOB_GET, args, response(404, {'error': 'Labeling job missing not found'}))
    refused = shaping.shape(JOB_GET, args, response(200, {'job': {'usecase_id': 'uc-2'}}))
    assert not missing.binding_refused and refused.binding_refused
    first, second = missing.to_result('a'), refused.to_result('b')
    assert first.content == second.content
    without_id = lambda r: {k: v for k, v in r.structured_content.items() if k != 'request_id'}
    assert without_id(first) == without_id(second)
    assert first.structured_content['status'] == 404 == second.structured_content['status']


def test_unbound_spec_404_keeps_the_handler_text():
    shaped = shaping.shape(DEVICES, {'usecase_id': 'uc-1'}, response(404, {'error': 'gone'}))
    assert shaped.failure.category == 'not_found' and shaped.failure.message == 'gone'


# ---------------------------------------------------------- collections

def test_dispatcher_slices_with_offset():
    items = [{'id': i} for i in range(10)]
    shaped = shaping.shape(DEVICES, {'usecase_id': 'uc', 'limit': 3, 'offset': 4},
                           response(200, {'devices': items, 'count': 10}))
    assert shaped.result['devices'] == items[4:7]
    assert shaped.result['returned'] == 3 and shaped.result['total_hint'] == 10
    assert shaped.result['count'] == 10
    assert not shaped.truncated


def test_dispatcher_default_limit():
    items = [{'id': i} for i in range(80)]
    shaped = shaping.shape(DEVICES, {'usecase_id': 'uc'}, response(200, {'devices': items}))
    assert len(shaped.result['devices']) == DEVICES.input_schema['properties']['limit']['default']


def test_oversized_collection_is_truncated_from_the_end():
    items = [{'id': i, 'pad': 'x' * 5_000} for i in range(100)]
    shaped = shaping.shape(DEVICES, {'usecase_id': 'uc', 'limit': 100},
                           response(200, {'devices': items}))
    assert shaped.truncated and shaped.result['truncated'] is True
    kept = shaped.result['returned']
    assert 0 < kept < 100 and shaped.result['devices'] == items[:kept]
    assert len(json.dumps(shaped.result, separators=(',', ':')).encode()) <= shaping.MAX_RESULT_BYTES
    assert shaped.structured['truncated'] is True


def test_forwarded_page_is_never_cut():
    events = [{'message': 'y' * 5_000} for _ in range(60)]
    assert BUILD_LOGS.result_shape == 'collection' and BUILD_LOGS.collection_key == 'events'
    shaped = shaping.shape(BUILD_LOGS, {'usecase_id': 'uc', 'build_id': 'b', 'limit': 60},
                           response(200, {'events': events, 'nextToken': 't'}))
    assert shaped.failure is not None and shaped.failure.category == 'result_too_large'
    assert 'limit <=' in shaped.failure.message
    # a page that fits is returned whole, never sliced by the dispatcher
    small = shaping.shape(BUILD_LOGS, {'usecase_id': 'uc', 'build_id': 'b', 'limit': 2},
                          response(200, {'events': events[:5], 'nextToken': 't'}))
    assert small.result['events'] == events[:5] and small.result['nextToken'] == 't'


def test_oversized_object_is_result_too_large():
    shaped = shaping.shape(BY_NAME['portal_devices_get'], {'usecase_id': 'uc', 'device_id': 'd'},
                           response(200, {'blob': 'z' * 250_000}))
    assert shaped.failure.category == 'result_too_large'


# ------------------------------------------------------------- results

def test_success_result_shape_and_non_json_text():
    shaped = shaping.shape(BY_NAME['portal_devices_get'], {'usecase_id': 'uc', 'device_id': 'd'},
                           response(200, {'text': 'plain'}))
    result = shaped.to_result('r')
    assert result.is_error is False
    assert result.structured_content == {'result': {'text': 'plain'}}
    assert json.loads(result.content[0].text) == {'text': 'plain'}


def test_resource_id_from_well_known_keys():
    shaped = shaping.shape(DEPLOY, {'usecase_id': 'uc'},
                           response(201, {'deployment_id': 'dep-1', 'status': 'CREATED'}))
    assert shaped.resource_id == 'dep-1'
    assert shaped.structured['resource_id'] == 'dep-1'


def test_resource_id_from_a_nested_path():
    spec = BY_NAME['portal_workflows_test_run_start']
    assert spec.resource_id_path == 'test_run.test_run_id'
    shaped = shaping.shape(spec, {'usecase_id': 'uc'},
                           response(201, {'test_run': {'test_run_id': 'tr-1'}}))
    assert shaped.resource_id == 'tr-1'
    assert shaping.resolve({'jobs': [{'build_job_id': 'b1'}]}, 'jobs[0].build_job_id') == 'b1'


def test_read_only_results_carry_no_resource_id():
    shaped = shaping.shape(BY_NAME['portal_devices_get'], {'usecase_id': 'uc', 'device_id': 'd'},
                           response(200, {'deployment_id': 'dep-1'}))
    assert shaped.resource_id is None


# ------------------- account tools: the error detail key (mcp-user-operations)

TARGET = 'fake-target-1'
GUARD_REASON = (f'{TARGET} is the last remaining enabled PortalAdmin account; the portal '
                'must retain at least one enabled PortalAdmin')
# The partial-state and guard bodies of `user_admin.py` (design Key research
# findings), each with the text its `message` carries.
HANDLER_BODIES = [
    ('portal_admin_account_create', 502, {
        'error': 'account was not provisioned',
        'message': f'The Cognito account {TARGET} was created but its portal registry '
                   'entry was not written, so it has no portal access; delete the account '
                   'and retry'}, 'backend_failure'),
    ('portal_admin_account_disable', 502, {
        'error': 'disable incomplete',
        'message': f'The account {TARGET} was disabled in the user pool but its portal '
                   'registry entry was not updated, so its portal access is unchanged; '
                   'retry the disable'}, 'backend_failure'),
    ('portal_admin_account_enable', 502, {
        'error': 'enable incomplete',
        'message': f'The account {TARGET} was enabled in the user pool but its portal '
                   'registry entry was not updated, so its portal access is unchanged; '
                   'retry the enable'}, 'backend_failure'),
    ('portal_admin_account_delete', 502, {
        'error': 'partial deletion',
        'message': f'The account {TARGET} was deleted but its verifier record was not '
                   'removed; it will be removed on a subsequent attempt'}, 'backend_failure'),
    ('portal_admin_account_delete', 409, {
        'error': 'Deletion rejected', 'message': GUARD_REASON}, 'conflict'),
]


@pytest.mark.parametrize('name,status,body,category', HANDLER_BODIES,
                         ids=[f'{b[0]}-{b[2]["error"]}' for b in HANDLER_BODIES])
def test_account_tool_errors_carry_the_handler_message(name, status, body, category):
    spec = BY_NAME[name]
    args = {'account_username': TARGET}
    failure = shaping.shape(spec, args, response(status, body)).failure
    assert failure.category == category and failure.status == status
    assert failure.message == f"{body['error']}: {body['message']}"
    assert failure.effect is None
    # Without the row's detail key only the error label would reach the caller.
    plain = dataclasses.replace(spec, error_detail_key=None)
    assert shaping.shape(plain, args, response(status, body)).failure.message == body['error']


@pytest.fixture
def portal(aws_stack, monkeypatch):
    shared = sys.modules['shared_utils']
    monkeypatch.setattr(shared.rbac_manager, 'has_permission', lambda *_a, **_k: True)
    monkeypatch.delenv('PORTAL_REGISTRY_ENFORCED', raising=False)
    fake = h.install(monkeypatch, phases=h.ALL_PHASES, mutating=True)
    fake.stack = aws_stack
    return fake


def account_call(portal, name, status, body):
    """One confirmed account-tool call through `on_call_tool`, the fake
    Lambda answering `(status, body)`; (structured content, audit entries)."""
    portal.answer = lambda _arn, _event: (status, body)
    token = h.access_token()
    result = h.in_memory('admin', lambda client: client.call_tool(
        name, {'account_username': TARGET, 'confirm': TARGET}), token)
    items = portal.stack.tables.audit_log.scan(
        FilterExpression=Attr('user_id').eq(token.subject))['Items']
    assert result.is_error is True and len(portal.calls) == 1
    return result.structured_content, items


def test_a_partial_state_502_carries_the_recovery_step(portal):
    body = dict(HANDLER_BODIES[1][2])
    content, items = account_call(portal, 'portal_admin_account_disable', 502, body)
    assert content['category'] == 'backend_failure' and content['status'] == 502
    assert 'retry the disable' in content['message']
    assert 'effect' not in content and 'status_tool' not in content
    [envelope] = items
    assert envelope['details']['status'] == 502 and envelope['resource_id'] == TARGET


def test_the_last_portal_admin_guard_409_carries_the_reason(portal):
    content, items = account_call(portal, 'portal_admin_account_delete', 409,
                                  {'error': 'Deletion rejected', 'message': GUARD_REASON})
    assert content['category'] == 'conflict' and content['status'] == 409
    assert content['message'] == f'Deletion rejected: {GUARD_REASON}'
    [envelope] = items
    assert envelope['details']['error_category'] == 'conflict'


def test_the_handler_catch_all_500_is_a_backend_failure(portal):
    content, items = account_call(portal, 'portal_admin_account_enable', 500,
                                  {'error': 'Internal server error'})
    assert content['category'] == 'backend_failure' and content['status'] == 500
    assert content['message'] == 'Internal server error'
    assert 'effect' not in content and 'status_tool' not in content
    [envelope] = items
    assert envelope['action'] == 'mcp_tool_call' and envelope['result'] == 'failure'
    assert envelope['details']['status'] == 500
