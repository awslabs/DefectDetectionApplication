"""Fixtures of the assistant suite (spec: .kiro/specs/portal-ai-assistant/,
design "Testing strategy"; tasks.md 2.11).

Builds on the root `conftest.py` (`aws_stack`: moto with the Portal tables)
and the MCP harness (`tests/mcp/_portal_harness.py`: fake backing Lambdas
and the dispatch map), adds the assistant table, a settings table, the real
user guide as the guide directory, and a scripted fake Bedrock client.
"""
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import boto3
import pytest

_HERE = Path(__file__).resolve().parent
_MCP_TESTS = _HERE.parent / 'mcp'
if str(_MCP_TESTS) not in sys.path:
    sys.path.insert(0, str(_MCP_TESTS))

import _portal_harness as h  # noqa: E402

REGION = 'us-east-1'
ASSISTANT_TABLE = 'test-assistant'
SETTINGS_TABLE = 'test-settings'
GUIDE_SOURCE = _HERE.parents[3] / 'docs' / 'user-guide'
USECASE = 'uc-assist-1'


def _create_tables(client) -> None:
    for name, key in ((ASSISTANT_TABLE, [('pk', 'HASH'), ('sk', 'RANGE')]),
                      (SETTINGS_TABLE, [('setting_key', 'HASH')])):
        try:
            client.create_table(
                TableName=name,
                KeySchema=[{'AttributeName': a, 'KeyType': t} for a, t in key],
                AttributeDefinitions=[{'AttributeName': a, 'AttributeType': 'S'} for a, _ in key],
                BillingMode='PAY_PER_REQUEST')
        except client.exceptions.ResourceInUseException:
            pass


@pytest.fixture(scope='session')
def guide_dir(tmp_path_factory):
    target = tmp_path_factory.mktemp('guide')
    for path in GUIDE_SOURCE.glob('*.md'):
        shutil.copy(path, target / path.name)
    return target


@pytest.fixture
def assistant_env(aws_stack, guide_dir, monkeypatch):
    """The assistant wired to moto and the MCP harness; yields a namespace."""
    from assistant import settings, store
    from assistant import api as api_module

    _create_tables(boto3.client('dynamodb', region_name=REGION))
    monkeypatch.setenv('ASSISTANT_TABLE', ASSISTANT_TABLE)
    monkeypatch.setenv('SETTINGS_TABLE', SETTINGS_TABLE)
    monkeypatch.setenv('ASSISTANT_GUIDE_DIR', str(guide_dir))
    monkeypatch.setenv('ASSISTANT_WORKER_FUNCTION', 'test-assistant-worker')
    monkeypatch.delenv('ASSISTANT_LIMITS_JSON', raising=False)
    monkeypatch.delenv('PORTAL_REGISTRY_ENFORCED', raising=False)
    store.reset_table()
    settings.clear_cache()
    api_module.reset_clients()
    shared = sys.modules['shared_utils']
    monkeypatch.setattr(shared.rbac_manager, 'has_permission', lambda *_a, **_k: True)
    monkeypatch.setattr(shared.rbac_manager, 'get_user_role', lambda *_a, **_k: None)
    fake_lambda = h.install(monkeypatch, h.FakeLambda(_default_answer), mutating=True)
    invoked: List[Dict[str, Any]] = []
    monkeypatch.setattr(api_module, '_LAMBDA', SimpleNamespace(
        invoke=lambda **kw: invoked.append(json.loads(kw['Payload']))))
    # wipe the assistant and settings tables between tests
    for name in (ASSISTANT_TABLE, SETTINGS_TABLE):
        table = boto3.resource('dynamodb', region_name=REGION).Table(name)
        items = table.scan().get('Items', [])
        if items:
            with table.batch_writer() as batch:
                for item in items:
                    key = {'pk': item['pk'], 'sk': item['sk']} if 'pk' in item \
                        else {'setting_key': item['setting_key']}
                    batch.delete_item(Key=key)
    yield SimpleNamespace(fake_lambda=fake_lambda, invoked=invoked, shared=shared,
                          monkeypatch=monkeypatch, guide_dir=guide_dir,
                          table=boto3.resource('dynamodb', region_name=REGION).Table(ASSISTANT_TABLE),
                          settings_table=boto3.resource('dynamodb', region_name=REGION).Table(
                              SETTINGS_TABLE))


def _default_answer(_arn, event):
    resource = event.get('resource') or ''
    if resource == '/devices':
        return 200, {'devices': [{'device_id': 'd-1', 'status': 'online'}], 'usecase_id': USECASE}
    if resource.endswith('/count'):
        return 200, {'count': 3, 'usecase_id': USECASE}
    if resource == '/deployments' and event.get('httpMethod') == 'POST':
        return 201, {'deployment_id': 'dep-1', 'usecase_id': USECASE}
    if event.get('httpMethod') == 'GET':
        return 200, {'items': [], 'usecase_id': USECASE}
    return 200, {'ok': True, 'usecase_id': USECASE}


# ------------------------------------------------------------ API events

def claims_for(sub: Optional[str] = None, role: str = 'UseCaseAdmin', **overrides) -> Dict[str, Any]:
    sub = sub or f'sub-{uuid.uuid4()}'
    claims = {'sub': sub, 'cognito:username': f'user-{sub[-6:]}', 'email': f'{sub[-6:]}@example.com',
              'custom:role': role, 'token_use': 'id', 'exp': 4102444800}
    claims.update(overrides)
    return claims


def api_event(method: str, resource: str, claims: Dict[str, Any], *, body: Any = None,
              params: Optional[Dict[str, str]] = None, query: Optional[Dict[str, str]] = None,
              headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    path = resource
    for key, value in (params or {}).items():
        path = path.replace('{' + key + '}', value)
    return {
        'httpMethod': method, 'resource': resource, 'path': path,
        'pathParameters': params or None, 'queryStringParameters': query or None,
        'headers': {'X-Portal-Build': 'abc1234', **(headers or {})},
        'body': json.dumps(body) if body is not None else None,
        'requestContext': {'requestId': f'req-{uuid.uuid4()}',
                           'authorizer': {'claims': claims},
                           'identity': {'sourceIp': '203.0.113.5', 'userAgent': 'Mozilla/5.0 test'}},
    }


def call_api(event: Dict[str, Any]):
    from assistant import api
    response = api.handler(event, None)
    body = json.loads(response['body']) if response.get('body') else None
    return response['statusCode'], body


# ------------------------------------------------------------ fake Bedrock

def text_events(text: str, *, stop: str = 'end_turn', usage=(100, 20, 0, 0), chunk: int = 7):
    events: List[Dict[str, Any]] = [{'messageStart': {'role': 'assistant'}},
                                    {'contentBlockStart': {'contentBlockIndex': 0, 'start': {}}}]
    for start in range(0, len(text), chunk):
        events.append({'contentBlockDelta': {'contentBlockIndex': 0,
                                             'delta': {'text': text[start:start + chunk]}}})
    events.append({'contentBlockStop': {'contentBlockIndex': 0}})
    events.append({'messageStop': {'stopReason': stop}})
    events.append(_metadata(usage))
    return events


def tool_use_events(uses, text: str = '', usage=(120, 30, 0, 0)):
    """`uses`: [(toolUseId, name, input dict)]; an optional leading text block."""
    events: List[Dict[str, Any]] = [{'messageStart': {'role': 'assistant'}}]
    index = 0
    if text:
        events += [{'contentBlockStart': {'contentBlockIndex': index, 'start': {}}},
                   {'contentBlockDelta': {'contentBlockIndex': index, 'delta': {'text': text}}},
                   {'contentBlockStop': {'contentBlockIndex': index}}]
        index += 1
    for tool_use_id, name, payload in uses:
        raw = json.dumps(payload)
        events.append({'contentBlockStart': {
            'contentBlockIndex': index, 'start': {'toolUse': {'toolUseId': tool_use_id, 'name': name}}}})
        half = max(1, len(raw) // 2)
        events.append({'contentBlockDelta': {'contentBlockIndex': index,
                                             'delta': {'toolUse': {'input': raw[:half]}}}})
        events.append({'contentBlockDelta': {'contentBlockIndex': index,
                                             'delta': {'toolUse': {'input': raw[half:]}}}})
        events.append({'contentBlockStop': {'contentBlockIndex': index}})
        index += 1
    events.append({'messageStop': {'stopReason': 'tool_use'}})
    events.append(_metadata(usage))
    return events


def _metadata(usage):
    inp, out, read, write = usage
    return {'metadata': {'usage': {'inputTokens': inp, 'outputTokens': out,
                                   'cacheReadInputTokens': read, 'cacheWriteInputTokens': write},
                         'metrics': {'latencyMs': 42}}}


class FakeBedrock:
    """A scripted `converse_stream`: each call pops the next script - a list of
    stream events, or an exception to raise. Records every request."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests: List[Dict[str, Any]] = []

    def converse_stream(self, **request):
        self.requests.append(request)
        if not self.scripts:
            raise AssertionError('FakeBedrock: no script left for this call')
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        return {'stream': iter(script)}


@pytest.fixture
def fake_bedrock(assistant_env, monkeypatch):
    """Patch the worker to use a FakeBedrock; returns a setter for its scripts."""
    from assistant import bedrock as bedrock_module
    from assistant import turn as turn_module
    import bedrock_common

    holder = {'client': FakeBedrock([])}
    monkeypatch.setattr(bedrock_module, 'client', lambda _config: holder['client'])
    monkeypatch.setattr(bedrock_common, 'get_bedrock_configuration', lambda: {
        'model_id': 'us.anthropic.claude-sonnet-4-5-20250929-v1:0', 'region': REGION,
        'max_tokens': 4096, 'temperature': None, 'top_p': None, 'timeout_seconds': 60})
    monkeypatch.setattr(turn_module, 'THROTTLE_BACKOFF_S', (0.0, 0.0))

    def set_scripts(*scripts):
        holder['client'] = FakeBedrock(scripts)
        return holder['client']

    set_scripts.holder = holder
    return set_scripts


def client_error(code: str, message: str = 'boom'):
    from botocore.exceptions import ClientError
    return ClientError({'Error': {'Code': code, 'Message': message}}, 'ConverseStream')
