"""Error results and result shaping (`mcp_portal/errors.py`, `mcp_portal/shaping.py`).

Spec: .kiro/specs/mcp-servers/ (design.md §2 `errors.py` and `shaping.py`
rows, D32, D36, §4 step 9, Error Handling "Backend status", "Use_Case
binding", "Result shaping", Data Models "Tool error result"; tasks.md 6.5).
Requirements 1.4, 1.5, 1.6, 3.3, 2.7.
"""
import json

import pytest

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
