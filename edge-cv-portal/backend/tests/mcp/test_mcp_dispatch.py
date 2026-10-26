"""Dispatch client, map, budget and event (`mcp_portal/dispatch.py`).

Spec: .kiro/specs/mcp-servers/ (design.md §2 `dispatch.py` row, Data Models
"Synthesized backend event", D30, D32, Error Handling `ssm:GetParameter`,
"Pre-dispatch time budget", `lambda:InvokeFunction`; Testing Strategy row
"D30/D32 dispatch client, map and budget"; tasks.md 6.3).
Requirements 1.3, 1.4, 3.9, 6.2, 2.7.
"""
import io
import json
import sys
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

from mcp_portal import catalog as cat
from mcp_portal import dispatch
from mcp_portal.errors import ToolFailure
from mcp_portal.principal import Principal

BY_NAME = {s.name: s for s in cat.CATALOG}
RO = BY_NAME['portal_devices_list']
M = BY_NAME['portal_devices_deployment_create']
PARAM = '/dda/mcp/backend-functions'


class FakeSSM:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def get_parameter(self, **kwargs):
        self.calls.append(kwargs)
        value = self.values[min(len(self.calls), len(self.values)) - 1]
        if isinstance(value, Exception):
            raise value
        return {'Parameter': {'Value': value}}


class FakeLambda:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def ok_payload(status=200, body=None):
    payload = {'statusCode': status, 'body': json.dumps(body if body is not None else {'ok': True})}
    return {'StatusCode': 200, 'Payload': io.BytesIO(json.dumps(payload).encode())}


def client_error(code):
    return ClientError({'Error': {'Code': code, 'Message': code}}, 'Invoke')


MAP = json.dumps({'devices': 'arn:fn:devices:1', 'deployments': 'arn:fn:deployments:1'})
MAP2 = json.dumps({'devices': 'arn:fn:devices:2', 'deployments': 'arn:fn:deployments:2'})


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('MCP_BACKEND_FUNCTIONS_PARAMETER', PARAM)
    dispatch.reset_backend_functions()
    monkeypatch.setattr(dispatch, 'LAMBDA_CONTEXT', None)
    yield monkeypatch
    dispatch.reset_backend_functions()


def install(monkeypatch, ssm_values=(MAP,), outcomes=None):
    ssm = FakeSSM(ssm_values)
    lam = FakeLambda(outcomes or [ok_payload()])
    monkeypatch.setattr(dispatch, 'SSM', ssm)
    monkeypatch.setattr(dispatch, 'LAMBDA', lam)
    return ssm, lam


EVENT = {'requestContext': {'requestId': 'req-1'}}


# ----------------------------------------------------------------- client

def test_lambda_client_makes_exactly_one_attempt_with_the_d32_timeouts():
    config = dispatch.LAMBDA.meta.config
    assert config.retries['total_max_attempts'] == 1
    assert config.read_timeout == 20
    assert config.connect_timeout == 2


def test_budget_constants():
    assert dispatch.MIN_DISPATCH_BUDGET_MS == 24_000
    assert 2_000 + 20_000 < dispatch.MIN_DISPATCH_BUDGET_MS


# ----------------------------------------------------------------- budget

def test_budget_guard_refuses_without_invoking(env):
    ssm, lam = install(env)
    env.setattr(dispatch, 'LAMBDA_CONTEXT',
                SimpleNamespace(get_remaining_time_in_millis=lambda: 10_000))
    result = dispatch.invoke(M, EVENT)
    assert isinstance(result, ToolFailure) and result.category == 'timeout'
    assert result.message == 'insufficient time budget to start the backing operation; retry'
    structured = result.structured('r')
    assert 'effect' not in structured and 'status_tool' not in structured
    assert lam.calls == [] and ssm.calls == []


@pytest.mark.parametrize('context', [
    SimpleNamespace(get_remaining_time_in_millis=lambda: 24_000), None])
def test_budget_boundary_and_no_context_proceed(env, context):
    _ssm, lam = install(env)
    env.setattr(dispatch, 'LAMBDA_CONTEXT', context)
    result = dispatch.invoke(M, EVENT)
    assert isinstance(result, dispatch.BackendResponse) and len(lam.calls) == 1


# ------------------------------------------------------------- dispatch map

def test_map_is_read_once_lazily(env):
    ssm, lam = install(env, outcomes=[ok_payload(), ok_payload()])
    assert ssm.calls == []
    dispatch.invoke(RO, EVENT)
    dispatch.invoke(M, EVENT)
    assert ssm.calls == [{'Name': PARAM}]
    assert [c['FunctionName'] for c in lam.calls] == ['arn:fn:devices:1', 'arn:fn:deployments:1']
    assert all(c['InvocationType'] == 'RequestResponse' for c in lam.calls)


def test_unknown_backend_key_is_backend_failure_without_invoke(env):
    _ssm, lam = install(env, ssm_values=(json.dumps({'other': 'arn'}),))
    result = dispatch.invoke(RO, EVENT)
    assert result.category == 'backend_failure' and 'devices' in result.message
    assert lam.calls == []


@pytest.mark.parametrize('value', [client_error('ParameterNotFound'), 'not json', '[1, 2]'])
def test_unavailable_map_is_backend_failure_without_invoke(env, value):
    _ssm, lam = install(env, ssm_values=(value,))
    result = dispatch.invoke(M, EVENT)
    assert result.category == 'backend_failure'
    assert result.message == 'dispatch map unavailable'
    assert 'effect' not in result.structured(None)
    assert lam.calls == []


# ----------------------------------------------------------------- invoke

def test_read_timeout_on_a_mutating_tool_has_unknown_effect(env):
    _ssm, lam = install(env, outcomes=[ReadTimeoutError(endpoint_url='https://lambda')])
    result = dispatch.invoke(M, EVENT)
    assert result.category == 'timeout'
    assert result.text == (f'timeout: the operation may have been applied; call '
                           f'{M.status_tool} before retrying')
    assert result.structured('r')['effect'] == 'unknown'
    assert result.structured('r')['status_tool'] == M.status_tool
    assert len(lam.calls) == 1


def test_read_timeout_on_a_read_only_tool_is_plain(env):
    _ssm, lam = install(env, outcomes=[ReadTimeoutError(endpoint_url='https://lambda')])
    result = dispatch.invoke(RO, EVENT)
    assert result.category == 'timeout' and 'may have been applied' not in result.message
    assert 'effect' not in result.structured('r')
    assert len(lam.calls) == 1


@pytest.mark.parametrize('outcome', [
    {'StatusCode': 200, 'FunctionError': 'Unhandled', 'Payload': io.BytesIO(b'{}')},
    {'StatusCode': 200, 'Payload': io.BytesIO(b'not json')},
    {'StatusCode': 200, 'Payload': io.BytesIO(b'{"no": "status"}')},
    EndpointConnectionError(endpoint_url='https://lambda'),
    client_error('ServiceException'),
])
def test_post_dispatch_failures_are_backend_failure_with_unknown_effect(env, outcome):
    _ssm, lam = install(env, outcomes=[outcome])
    result = dispatch.invoke(M, EVENT)
    assert result.category == 'backend_failure'
    assert result.structured(None).get('effect') == 'unknown'
    assert len(lam.calls) == 1


def test_throttle_is_throttled(env):
    _ssm, lam = install(env, outcomes=[client_error('TooManyRequestsException')])
    result = dispatch.invoke(M, EVENT)
    assert result.category == 'throttled' and len(lam.calls) == 1


def test_replaced_function_refreshes_the_map_once(env):
    ssm, lam = install(env, ssm_values=(MAP, MAP2),
                       outcomes=[client_error('ResourceNotFoundException'), ok_payload()])
    result = dispatch.invoke(RO, EVENT)
    assert isinstance(result, dispatch.BackendResponse) and result.status == 200
    assert [c['FunctionName'] for c in lam.calls] == ['arn:fn:devices:1', 'arn:fn:devices:2']
    assert len(ssm.calls) == 2


def test_second_resource_not_found_ends_without_a_third_call(env):
    ssm, lam = install(env, ssm_values=(MAP, MAP2),
                       outcomes=[client_error('ResourceNotFoundException'),
                                 client_error('ResourceNotFoundException'), ok_payload()])
    result = dispatch.invoke(M, EVENT)
    assert result.category == 'backend_failure'
    assert len(lam.calls) == 2 and len(ssm.calls) == 2


def test_payload_parsing():
    response = dispatch._parse(json.dumps({'statusCode': 201, 'body': 'plain text',
                                           'headers': {'A': 'b'}}).encode())
    assert response.status == 201 and response.body_json == {'text': 'plain text'}
    response = dispatch._parse(json.dumps({'statusCode': 200, 'body': '{"a": 1}'}).encode())
    assert response.body_json == {'a': 1}


# ------------------------------------------------------------------ event

def principal_for(claims):
    return Principal(sub=claims['sub'], username=claims['username'],
                     email='person@example.com', claims=claims)


CLAIMS = {'sub': 'sub-1', 'iss': 'https://issuer', 'client_id': 'mcp', 'token_use': 'access',
          'scope': 'res/portal.tools', 'username': 'jdoe', 'exp': 2, 'iat': 1, 'jti': 'j'}
CTX = dispatch.McpContext(request_id='req-9', tool='portal_devices_list',
                          server='dda-portal-mcp/devices@1.0.0', client='kiro/1.2.3',
                          source_ip='203.0.113.7', user_agent='kiro/1.2.3 mcp',
                          principal_email='person@example.com', identity_source='registry')


def test_event_shape_for_a_get_route():
    p = principal_for(CLAIMS)
    event = dispatch.build_backend_event(RO, {'usecase_id': 'uc-1', 'limit': 5, 'offset': 2}, p, CTX)
    assert event['httpMethod'] == 'GET' and event['resource'] == '/devices'
    assert event['path'] == '/devices' and event['pathParameters'] is None
    # limit/offset are dispatcher-sliced here, so only usecase_id travels
    assert event['queryStringParameters'] == {'usecase_id': 'uc-1'}
    assert event['body'] is None and event['isBase64Encoded'] is False
    assert event['requestContext']['authorizer']['claims'] is p.claims
    assert event['requestContext']['requestId'] == 'req-9'
    assert event['requestContext']['identity'] == {'sourceIp': '203.0.113.7',
                                                   'userAgent': 'kiro/1.2.3 mcp'}
    assert event['mcp'] == {'channel': 'mcp', 'tool': 'portal_devices_list',
                            'server': 'dda-portal-mcp/devices@1.0.0', 'client': 'kiro/1.2.3',
                            'request_id': 'req-9', 'principal_email': 'person@example.com',
                            'identity_source': 'registry'}


def test_path_parameters_are_percent_encoded():
    spec = BY_NAME['portal_devices_get']
    event = dispatch.build_backend_event(spec, {'usecase_id': 'uc-1', 'device_id': 'a:b+c=d'},
                                         principal_for(CLAIMS), CTX)
    assert event['path'] == '/devices/a%3Ab%2Bc%3Dd'
    assert event['pathParameters'] == {'id': 'a:b+c=d'}


def test_body_for_a_post_route_and_confirm_is_not_forwarded():
    args = {'usecase_id': 'uc-1', 'components': [{'name': 'c', 'version': '1.0.0'}],
            'target_thing_group': 'group-1', 'confirm': 'group-1'}
    event = dispatch.build_backend_event(M, args, principal_for(CLAIMS), CTX)
    body = json.loads(event['body'])
    assert 'confirm' not in body and body['target_thing_group'] == 'group-1'
    assert body['usecase_id'] == 'uc-1'


def test_server_consumed_args_travel_only_when_mapped():
    logs = BY_NAME['portal_workflows_build_logs_get']
    args = {'usecase_id': 'uc-1', 'build_id': 'b-1', 'limit': 10, 'next_token': 'tok'}
    event = dispatch.build_backend_event(logs, args, principal_for(CLAIMS), CTX)
    assert event['queryStringParameters']['limit'] == '10'
    assert event['queryStringParameters']['nextToken'] == 'tok'


def test_backend_reads_the_synthesized_event_as_the_principal(aws_stack, monkeypatch):
    shared = sys.modules['shared_utils']
    monkeypatch.setattr(shared, '_REQUEST_AUDIT_CONTEXT', None)
    p = principal_for(CLAIMS)
    event = dispatch.build_backend_event(RO, {'usecase_id': 'uc-1'}, p, CTX)
    user = shared.get_user_from_event(event)
    assert user == {'user_id': 'sub-1', 'email': 'jdoe', 'username': 'jdoe', 'role': 'Viewer'}
    attribution = shared.attribution_from(event, user, usecase_id='uc-1')
    assert attribution['email'] == 'person@example.com'
    assert attribution['identity_source'] == 'registry'
    assert attribution['channel'] == 'mcp' and attribution['mcp_tool'] == 'portal_devices_list'
