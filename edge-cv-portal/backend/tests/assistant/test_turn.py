"""The agent loop (design §8; tasks.md 2.9, 2.11). Requirements 5.3-5.9, 6.3,
6.4, 6.8, 7.5, 7.6, 8.2, 10.1, 11.1-11.3; Properties P2, P7, P8, P10.
"""
import json
import logging

import pytest
from boto3.dynamodb.conditions import Attr
from hypothesis import given, settings as hsettings, strategies as st

from assistant import settings, store, tools, turn as turn_module, worker
from mcp_portal import pipeline

from conftest import USECASE, claims_for, client_error, text_events, tool_use_events


def make_turn(assistant_env, prompt='Which devices do I have?', route='/devices', exp=4102444800,
              claims=None):
    claims = claims or claims_for()
    meta = store.create_conversation(claims['sub'], '', 30)
    turn = store.create_turn(sub=claims['sub'], cid=meta['conversation_id'], prompt=prompt,
                             page_context={'route': route, 'usecase_id': USECASE}, claims=claims,
                             exp=exp, client_label='dda-portal-frontend/abc1234',
                             source_ip='203.0.113.5', user_agent='UA', ttl_days=7)
    store.touch_conversation(claims['sub'], meta['conversation_id'], retention_days=30,
                             live_turn_id=turn['turn_id'])
    store.reserve_turn(claims['sub'], {'turns_per_hour': 100, 'concurrent_turns': 5})
    return claims, meta['conversation_id'], turn['turn_id']


def run_worker(cid, tid, resume=None):
    return worker.handler({'conversation_id': cid, 'turn_id': tid, 'resume': resume}, None)


def audit_rows(env, sub):
    items = env.shared.dynamodb.Table(env.shared.AUDIT_LOG_TABLE).scan(
        FilterExpression=Attr('user_id').eq(sub))['Items']
    return [(i['action'], i['result'], i.get('channel'), i.get('mcp_tool'), i.get('mcp_client'))
            for i in items]


def live_count(env, sub):
    item = env.table.get_item(Key={'pk': f'USER#{sub}', 'sk': 'QUOTA#LIVE'}).get('Item')
    return int(item['count']) if item else 0


# ---------------------------------------------------------------- basics

def test_text_only_turn(assistant_env, fake_bedrock):
    client = fake_bedrock(text_events('You have one device online.'))
    claims, cid, tid = make_turn(assistant_env)
    result = run_worker(cid, tid)
    assert result == {'status': 'completed', 'turn_id': tid}
    turn = store.get_turn(cid, tid)
    assert turn['text'] == 'You have one device online.' and turn['version'] > 2
    assert turn['model_calls'] == 1 and turn['usage']['input'] == 100
    assert turn['pending'] == {'messages': [], 'tool_uses': [], 'results': [], 'extra': [],
                               'activity': []}
    messages = store.list_messages(cid)
    assert [m['role'] for m in messages] == ['user', 'assistant']
    assert messages[1]['display']['status'] == 'completed'
    meta = store.get_meta(cid)
    assert meta['turn_count'] == 1 and meta['live_turn_id'] is None
    assert meta['title'] == 'Which devices do I have?'
    assert live_count(assistant_env, claims['sub']) == 0
    request = client.requests[0]
    assert request['messages'] == [{'role': 'user', 'content': [{'text': 'Which devices do I have?'}]}]
    assert 'cachePoint' in request['system'][1]
    assert request['toolConfig']['tools'][-1] == {'cachePoint': {'type': 'default'}}
    names = [t['toolSpec']['name'] for t in request['toolConfig']['tools'] if 'toolSpec' in t]
    assert 'portal_devices_list' in names and 'journey_status' in names
    assert not any(tools.BY_NAME[n].destructive for n in names if n in tools.BY_NAME)
    assert '15-devices.md' in request['system'][2]['text']
    assert store.tokens_today(claims['sub']) == 120
    # the worker is idempotent on a finished turn
    assert run_worker(cid, tid)['status'] == 'completed'


def test_read_only_tool_turn(assistant_env, fake_bedrock):
    client = fake_bedrock(
        tool_use_events([('tu-1', 'portal_devices_list', {'usecase_id': USECASE})], text='Checking.'),
        text_events('One device: d-1.'))
    claims, cid, tid = make_turn(assistant_env)
    run_worker(cid, tid)
    turn = store.get_turn(cid, tid)
    assert turn['status'] == 'completed' and turn['text'] == 'Checking.One device: d-1.'
    kinds = [e['type'] for e in turn['events']]
    assert kinds == ['tool_start', 'tool_end']
    assert turn['events'][0]['args_summary'] == f'usecase_id={USECASE}'
    assert turn['events'][1]['category'] is None if 'category' in turn['events'][1] else True
    assert turn['tool_calls'] == 1 and turn['pipeline_calls'] == 1
    second = client.requests[1]['messages']
    assert second[-2]['role'] == 'assistant' and 'toolUse' in second[-2]['content'][1]
    result = second[-1]['content'][0]['toolResult']
    assert result['toolUseId'] == 'tu-1' and result['status'] == 'success'
    assert result['content'][0]['json']['result']['devices'] == [{'device_id': 'd-1', 'status': 'online'}]
    [(arn, event)] = assistant_env.fake_lambda.calls
    assert event['mcp']['channel'] == 'assistant'
    assert event['mcp']['client'] == 'dda-portal-frontend/abc1234'
    assert event['mcp']['server'].startswith('dda-portal-assistant@')
    assert event['mcp']['request_id'] == f'{tid}/1'
    assert event['requestContext']['authorizer']['claims'] == claims
    # portal_devices_list's handler writes its own (marked) audit entry; the
    # fake Lambda writes none, so the pipeline adds no envelope here.
    assert audit_rows(assistant_env, claims['sub']) == []
    stored = store.list_messages(cid)[1]
    assert 'Tools used in this turn' in stored['content'][1]['text']
    assert stored['display']['events'][0]['tool'] == 'portal_devices_list'


def test_envelope_entries_carry_the_assistant_marker(assistant_env, fake_bedrock):
    # portal_data_datasets_count has handler_audits=False: the pipeline writes
    # the mcp_tool_call envelope, marked with the assistant channel (P7).
    fake_bedrock(tool_use_events([('tu-1', 'portal_data_datasets_count',
                                   {'usecase_id': USECASE, 'prefix': ''})]),
                 text_events('Three datasets.'))
    claims, cid, tid = make_turn(assistant_env, route='/data')
    run_worker(cid, tid)
    assert store.get_turn(cid, tid)['status'] == 'completed'
    rows = audit_rows(assistant_env, claims['sub'])
    assert rows == [('mcp_tool_call', 'success', 'assistant', 'portal_data_datasets_count',
                     'dda-portal-frontend/abc1234')]


def test_journey_status_local_tool(assistant_env, fake_bedrock):
    fake_bedrock(tool_use_events([('tu-1', 'journey_status', {'usecase_id': USECASE})]),
                 text_events('Next: label your images.'))
    claims, cid, tid = make_turn(assistant_env, prompt='What next?', route='/dashboard')
    run_worker(cid, tid)
    turn = store.get_turn(cid, tid)
    assert turn['status'] == 'completed'
    assert turn['tool_calls'] == 1 and turn['pipeline_calls'] == 8
    result = fake_bedrock.holder['client'].requests[1]['messages'][-1]['content'][0]['toolResult']
    payload = result['content'][0]['json']
    assert payload['summary']['datasets'] == 'some' and payload['summary']['devices'] == 'some'
    assert payload['next_step']['number'] == 3  # labeling jobs: the fake answers an empty list
    assert len(assistant_env.fake_lambda.calls) == 8
    rows = audit_rows(assistant_env, claims['sub'])
    assert rows and all(r[2] == 'assistant' for r in rows)


def test_guide_and_find_tools_local_tools(assistant_env, fake_bedrock):
    client = fake_bedrock(
        tool_use_events([('tu-1', 'guide_read_section', {'chapter': '02-use-cases.md'}),
                         ('tu-2', 'find_tools', {'query': 'audit logs'})]),
        text_events('Done.'))
    claims, cid, tid = make_turn(assistant_env, route='/usecases')
    run_worker(cid, tid)
    results = client.requests[1]['messages'][-1]['content']
    assert results[0]['toolResult']['content'][0]['json']['link'] == 'guide:02-use-cases.md'
    found = results[1]['toolResult']['content'][0]['json']['tools']
    assert any(t['name'] == 'portal_admin_audit_logs_query' for t in found)
    assert store.get_turn(cid, tid)['pipeline_calls'] == 0


# ----------------------------------------------------------- mutating

def test_mutating_tool_is_refused_while_actions_are_undelivered(assistant_env, fake_bedrock):
    client = fake_bedrock(
        tool_use_events([('tu-1', 'portal_devices_deployment_create',
                          {'usecase_id': USECASE, 'target_thing_group': 'tg', 'confirm': 'tg',
                           'components': [{'component_name': 'c'}]})]),
        text_events('I cannot do that here; open Deployments.'))
    claims, cid, tid = make_turn(assistant_env)
    run_worker(cid, tid)
    turn = store.get_turn(cid, tid)
    assert turn['status'] == 'completed' and turn['confirmation'] is None
    result = client.requests[1]['messages'][-1]['content'][0]['toolResult']
    assert result['status'] == 'error'
    assert result['content'][0]['json']['category'] == 'mutating_tools_disabled'
    assert assistant_env.fake_lambda.calls == []
    assert not [i for i in assistant_env.table.scan()['Items'] if i['sk'].startswith('BIND#')]


@pytest.fixture
def actions_delivered(assistant_env, monkeypatch):
    monkeypatch.setattr(tools, 'ACTIONS_DELIVERED', True)
    monkeypatch.setattr(settings, 'ACTIONS_DELIVERED', True)
    monkeypatch.setattr(tools, 'DESTRUCTIVE_ALLOWLIST', ('portal_devices_deployment_create',))
    return True


PROPOSAL = {'usecase_id': USECASE, 'target_thing_group': 'line-1', 'confirm': 'model-supplied',
            'components': [{'component_name': 'fake.component'}]}


def test_mutating_proposal_suspends_then_confirms(assistant_env, fake_bedrock, actions_delivered):
    client = fake_bedrock(
        tool_use_events([('tu-1', 'portal_devices_deployment_create', PROPOSAL)], text='I will deploy.'),
        text_events('Deployment dep-1 created.'))
    claims, cid, tid = make_turn(assistant_env, prompt='Deploy to line-1')
    result = run_worker(cid, tid)
    assert result['status'] == 'awaiting_confirmation'
    turn = store.get_turn(cid, tid)
    card = turn['confirmation']
    assert card['tool'] == 'portal_devices_deployment_create' and card['destructive'] is True
    assert 'confirm' not in card['args'] and card['args']['target_thing_group'] == 'line-1'
    assert card['usecase_id'] == USECASE and card['expires_at'] > store.now_s()
    assert [e['type'] for e in turn['events']] == ['confirmation_requested']
    assert turn['pending']['tool_uses'] == [] or turn['pending']['tool_uses'][0]['name'] == \
        'portal_devices_deployment_create'
    assert assistant_env.fake_lambda.calls == []           # nothing ran yet
    assert store.get_meta(cid)['live_turn_id'] == tid        # still live while pending
    # the API consumes the binding, then the worker resumes
    store.consume_binding(claims['sub'], card['binding_id'], 'confirm')
    store.update_turn(cid, tid, status='running')
    result = run_worker(cid, tid, resume={'binding_id': card['binding_id']})
    assert result['status'] == 'completed'
    turn = store.get_turn(cid, tid)
    assert turn['text'] == 'I will deploy.Deployment dep-1 created.'
    kinds = [e['type'] for e in turn['events']]
    assert kinds == ['confirmation_requested', 'confirmation_resolved', 'tool_start', 'tool_end']
    [(arn, event)] = assistant_env.fake_lambda.calls
    body = json.loads(event['body'])
    assert body['target_thing_group'] == 'line-1' and 'confirm' not in body
    result_block = client.requests[1]['messages'][-1]['content'][0]['toolResult']
    payload = result_block['content'][0]['json']
    assert payload['result'] == {'deployment_id': 'dep-1', 'usecase_id': USECASE}
    assert payload['resource_id'] == 'dep-1'
    assert payload['status_tool'] == 'portal_devices_deployment_for_target_get'
    assert turn['confirmations'] == 1


def test_mutating_proposal_rejected(assistant_env, fake_bedrock, actions_delivered):
    client = fake_bedrock(
        tool_use_events([('tu-1', 'portal_devices_deployment_create', PROPOSAL)]),
        text_events('Understood, I will not deploy.'))
    claims, cid, tid = make_turn(assistant_env)
    run_worker(cid, tid)
    card = store.get_turn(cid, tid)['confirmation']
    store.consume_binding(claims['sub'], card['binding_id'], 'reject')
    store.update_turn(cid, tid, status='running')
    run_worker(cid, tid, resume={'binding_id': card['binding_id']})
    turn = store.get_turn(cid, tid)
    assert turn['status'] == 'completed'
    result = client.requests[1]['messages'][-1]['content'][0]['toolResult']
    assert result['status'] == 'error' and result['content'][0]['json']['declined'] is True
    assert assistant_env.fake_lambda.calls == []
    assert turn['events'][1] == {**turn['events'][1], 'type': 'confirmation_resolved', 'decision': 'reject'}


def test_resume_with_a_foreign_binding_fails_closed(assistant_env, fake_bedrock, actions_delivered):
    fake_bedrock(tool_use_events([('tu-1', 'portal_devices_deployment_create', PROPOSAL)]))
    claims, cid, tid = make_turn(assistant_env)
    run_worker(cid, tid)
    card = store.get_turn(cid, tid)['confirmation']
    other = store.create_binding(sub=claims['sub'], cid=cid, tid=tid, tool='portal_devices_deployment_create',
                                 args=dict(PROPOSAL, target_thing_group='line-2'), seconds=600)
    store.consume_binding(claims['sub'], other['binding_id'], 'confirm')
    store.update_turn(cid, tid, status='running')
    run_worker(cid, tid, resume={'binding_id': other['binding_id']})
    turn = store.get_turn(cid, tid)
    assert turn['status'] == 'failed' and turn['failure']['code'] == 'binding_invalid'
    assert assistant_env.fake_lambda.calls == []
    assert card['binding_id'] != other['binding_id']


@given(args=st.dictionaries(st.sampled_from(['usecase_id', 'target_thing_group', 'confirm',
                                             'deployment_name']),
                            st.text(min_size=1, max_size=10), min_size=1))
@hsettings(max_examples=25, deadline=None,
           suppress_health_check=[__import__('hypothesis').HealthCheck.function_scoped_fixture])
def test_property_model_confirm_is_never_used(assistant_env, fake_bedrock, actions_delivered, args):
    """P2: whatever `confirm` the model supplies, the stored proposal has none
    and the pipeline receives the catalog's expected value."""
    assistant_env.fake_lambda.calls.clear()
    proposal = dict(PROPOSAL, **args)
    fake_bedrock(tool_use_events([('tu-1', 'portal_devices_deployment_create', proposal)]),
                 text_events('ok'))
    claims, cid, tid = make_turn(assistant_env)
    run_worker(cid, tid)
    turn = store.get_turn(cid, tid)
    assert turn['status'] == 'awaiting_confirmation'
    card = turn['confirmation']
    assert 'confirm' not in card['args']
    binding = store.get_binding(claims['sub'], card['binding_id'])
    assert 'confirm' not in binding['args']
    store.consume_binding(claims['sub'], card['binding_id'], 'confirm')
    store.update_turn(cid, tid, status='running')
    spec = tools.BY_NAME['portal_devices_deployment_create']
    expected = pipeline.expected_confirm(spec, binding['args'])
    seen = {}
    original = pipeline.call_tool

    def spy(spec_, args_, *a, **k):
        seen['confirm'] = args_.get('confirm')
        return original(spec_, args_, *a, **k)

    assistant_env.monkeypatch.setattr(turn_module.pipeline, 'call_tool', spy)
    run_worker(cid, tid, resume={'binding_id': card['binding_id']})
    assistant_env.monkeypatch.setattr(turn_module.pipeline, 'call_tool', original)
    assert seen['confirm'] == expected
    assert seen['confirm'] != 'model-supplied' or expected == 'model-supplied'


# ------------------------------------------------------ stop and guards

def test_stop_requested_ends_the_turn(assistant_env, fake_bedrock):
    fake_bedrock(tool_use_events([('tu-1', 'portal_devices_list', {'usecase_id': USECASE})]),
                 text_events('never reached'))
    claims, cid, tid = make_turn(assistant_env)
    store.request_stop(cid, tid)
    run_worker(cid, tid)
    turn = store.get_turn(cid, tid)
    assert turn['status'] == 'stopped' and turn['events'][-1]['type'] == 'stopped'
    assert turn['model_calls'] == 0 and live_count(assistant_env, claims['sub']) == 0


def test_disabled_and_expired_guards(assistant_env, fake_bedrock):
    fake_bedrock(text_events('x'))
    claims, cid, tid = make_turn(assistant_env, exp=1)
    run_worker(cid, tid)
    assert store.get_turn(cid, tid)['failure']['code'] == 'not_authenticated'
    settings.write_configuration({'enabled': False}, {'user_id': 'admin', 'username': 'admin'})
    claims, cid, tid = make_turn(assistant_env)
    run_worker(cid, tid)
    assert store.get_turn(cid, tid)['failure']['code'] == 'assistant_disabled'
    assert fake_bedrock.holder['client'].requests == []


def test_budget_ends_the_turn_with_the_sentence(assistant_env, fake_bedrock):
    assistant_env.monkeypatch.setenv('ASSISTANT_LIMITS_JSON', json.dumps({'model_calls_per_turn': 1}))
    fake_bedrock(tool_use_events([('tu-1', 'portal_devices_list', {'usecase_id': USECASE})], text='Hmm.'))
    claims, cid, tid = make_turn(assistant_env)
    run_worker(cid, tid)
    turn = store.get_turn(cid, tid)
    assert turn['status'] == 'completed' and 'model calls per turn limit' in turn['text']
    assert turn['events'][-1] == {**turn['events'][-1], 'type': 'budget', 'limit': 'model_calls_per_turn'}


def test_tool_call_budget(assistant_env, fake_bedrock):
    assistant_env.monkeypatch.setenv('ASSISTANT_LIMITS_JSON', json.dumps({'tool_calls_per_turn': 1}))
    client = fake_bedrock(
        tool_use_events([('tu-1', 'portal_devices_list', {'usecase_id': USECASE}),
                         ('tu-2', 'portal_devices_list', {'usecase_id': USECASE})]))
    claims, cid, tid = make_turn(assistant_env)
    run_worker(cid, tid)
    turn = store.get_turn(cid, tid)
    assert turn['status'] == 'completed' and len(assistant_env.fake_lambda.calls) == 1
    assert len(client.requests) == 1  # no further model call after the budget
    assert 'tool calls per turn' in turn['text']


# ------------------------------------------------------- model failures

def test_throttling_is_retried_then_succeeds(assistant_env, fake_bedrock):
    client = fake_bedrock(client_error('ThrottlingException'), text_events('fine'))
    claims, cid, tid = make_turn(assistant_env)
    run_worker(cid, tid)
    assert store.get_turn(cid, tid)['status'] == 'completed' and len(client.requests) == 2


def test_persistent_throttling_fails_the_turn(assistant_env, fake_bedrock):
    fake_bedrock(client_error('ThrottlingException'), client_error('ThrottlingException'),
                 client_error('ThrottlingException'))
    claims, cid, tid = make_turn(assistant_env)
    run_worker(cid, tid)
    turn = store.get_turn(cid, tid)
    assert turn['status'] == 'failed' and turn['failure']['code'] == 'bedrock_throttled'
    assert live_count(assistant_env, claims['sub']) == 0
    assert store.list_messages(cid)[1]['content'][0]['text'].startswith('[The assistant did not answer')


@pytest.mark.parametrize('code, failure', [('AccessDeniedException', 'bedrock_authorization'),
                                           ('ResourceNotFoundException', 'bedrock_model_access'),
                                           ('ModelErrorException', 'bedrock_model_error')])
def test_model_errors_fail_with_their_category(assistant_env, fake_bedrock, code, failure):
    fake_bedrock(client_error(code))
    claims, cid, tid = make_turn(assistant_env)
    run_worker(cid, tid)
    turn = store.get_turn(cid, tid)
    assert turn['failure']['code'] == failure and turn['events'][-1]['code'] == failure


def test_tool_use_unsupported_falls_back_to_guidance(assistant_env, fake_bedrock):
    client = fake_bedrock(client_error('ValidationException', 'This model does not support toolConfig'),
                          text_events('Guidance only.'))
    claims, cid, tid = make_turn(assistant_env)
    run_worker(cid, tid)
    turn = store.get_turn(cid, tid)
    assert turn['status'] == 'completed' and turn['text'] == 'Guidance only.'
    assert 'toolConfig' not in client.requests[1]
    assert settings.model_capability(client.requests[0]['modelId'])['tool_use_supported'] is False
    assert turn['events'][0]['code'] == 'tool_use_unsupported'


def test_cache_points_are_stripped_when_rejected(assistant_env, fake_bedrock):
    client = fake_bedrock(client_error('ValidationException', 'cachePoint is not supported'),
                          text_events('ok'))
    claims, cid, tid = make_turn(assistant_env)
    run_worker(cid, tid)
    assert store.get_turn(cid, tid)['status'] == 'completed'
    assert not any('cachePoint' in b for b in client.requests[1]['system'])
    assert not any('cachePoint' in t for t in client.requests[1]['toolConfig']['tools'])
    assert settings.model_capability(client.requests[0]['modelId'])['tool_use_supported'] is True


# ----------------------------------------------------------- history

def test_history_window_and_alternation(assistant_env, fake_bedrock):
    client = fake_bedrock(text_events('second answer'))
    claims, cid, tid = make_turn(assistant_env, prompt='second question')
    store.append_messages(cid, [{'role': 'user', 'content': [{'text': 'first question'}]},
                               {'role': 'assistant', 'content': [{'text': 'first answer'}]}],
                          first_seq=0, turn_id='t0', retention_days=30)
    store.touch_conversation(claims['sub'], cid, retention_days=30, turn_delta=1)
    run_worker(cid, tid)
    messages = client.requests[0]['messages']
    assert [m['role'] for m in messages] == ['user', 'assistant', 'user']
    assert messages[0]['content'] == [{'text': 'first question'}]
    assert messages[-1]['content'] == [{'text': 'second question'}]
    assert [m['seq'] for m in store.list_messages(cid)] == [0, 1, 2, 3]


def test_window_helpers():
    history = [{'seq': i, 'role': 'user' if i % 2 == 0 else 'assistant',
                'content': [{'text': 'x' * 400}]} for i in range(10)]
    window = turn_module.history_window(history, budget_tokens=350)
    assert window and window[0]['role'] == 'user' and len(window) <= 4
    merged = turn_module.merge_alternating([{'role': 'user', 'content': [{'text': 'a'}]},
                                            {'role': 'user', 'content': [{'text': 'b'}]},
                                            {'role': 'assistant', 'content': [{'text': 'c'}]}])
    assert merged == [{'role': 'user', 'content': [{'text': 'a'}, {'text': 'b'}]},
                      {'role': 'assistant', 'content': [{'text': 'c'}]}]
    assert turn_module.args_summary({'usecase_id': 'u', 'prompt_text': 'secret words', 'device_id': 'd'}) \
        == 'device_id=d, prompt_text, usecase_id=u'


# ---------------------------------------------------------- hygiene (P8)

@given(sentinel=st.from_regex(r'\AZQX[a-z]{6}ZQX\Z'))
@hsettings(max_examples=8, deadline=None,
           suppress_health_check=[__import__('hypothesis').HealthCheck.function_scoped_fixture])
def test_property_no_content_in_logs_or_audit(assistant_env, fake_bedrock, caplog, sentinel):
    """Prompt text, model text, tool arguments and results never reach the
    telemetry lines, the EMF lines or the audit details."""
    assistant_env.fake_lambda.calls.clear()
    assistant_env.fake_lambda.answer = lambda _arn, _event: (200, {'devices': [{'name': sentinel}]})
    fake_bedrock(tool_use_events([('tu-1', 'portal_devices_list',
                                   {'usecase_id': USECASE, 'limit': 5})], text=sentinel),
                 text_events('Answer ' + sentinel))
    claims, cid, tid = make_turn(assistant_env, prompt='Prompt ' + sentinel)
    with caplog.at_level(logging.INFO):
        run_worker(cid, tid)
    assistant_env.fake_lambda.answer = __import__('conftest')._default_answer
    turn = store.get_turn(cid, tid)
    assert turn['status'] == 'completed' and sentinel in turn['text']
    for record in caplog.records:
        if record.name.startswith(('assistant', 'mcp_portal', 'root')):
            assert sentinel not in record.getMessage(), record.name
    for item in assistant_env.shared.dynamodb.Table(assistant_env.shared.AUDIT_LOG_TABLE).scan(
            FilterExpression=Attr('user_id').eq(claims['sub']))['Items']:
        assert sentinel not in json.dumps(item, default=str)
    for event in turn['events']:
        assert sentinel not in json.dumps(event)


# ------------------------------------------------------- window (P6)

_history_messages = st.lists(
    st.tuples(st.sampled_from(['user', 'assistant']), st.text(min_size=0, max_size=600)),
    min_size=0, max_size=14)


@given(history=_history_messages, budget=st.integers(min_value=1, max_value=600))
@hsettings(max_examples=150, deadline=None)
def test_property_history_window(history, budget):
    """The window is a contiguous suffix of the history, starts with a user
    message (or is empty), fits the budget unless the single newest message
    is itself larger than the budget, and carries text blocks only (P6)."""
    messages = [{'seq': i, 'role': role, 'content': [{'text': text}], 'turn_id': f't{i}'}
                for i, (role, text) in enumerate(history)]
    window = turn_module.history_window(messages, budget)
    expected_tail = [{'role': m['role'], 'content': m['content']} for m in messages]
    assert window == expected_tail[len(expected_tail) - len(window):]       # a contiguous suffix
    if window:
        assert window[0]['role'] == 'user'
        total = sum(turn_module._estimate_tokens(m) for m in window)
        assert total <= budget or len(window) == 1
        # Nothing but text blocks ever reaches the model from stored history.
        assert all(set(block) == {'text'} for m in window for block in m['content'])
        # Maximal: the next older message would either break the budget, or
        # the window was cut at the user-message boundary just after it.
        older = len(expected_tail) - len(window) - 1
        if older >= 0:
            over = total + turn_module._estimate_tokens(expected_tail[older]) > budget
            assert over or expected_tail[older]['role'] == 'assistant'
    else:
        # Empty only when nothing fits in front of a user message.
        kept_user = [m for m in messages if m['role'] == 'user']
        assert not kept_user or turn_module._estimate_tokens(
            {'role': messages[-1]['role'], 'content': messages[-1]['content']}) > budget \
            or messages[-1]['role'] == 'assistant'


# ------------------------------------------------- status transitions (P10)

ALLOWED_TRANSITIONS = {
    ('queued', 'running'), ('running', 'awaiting_confirmation'), ('awaiting_confirmation', 'running'),
    ('running', 'completed'), ('running', 'stopped'), ('running', 'failed'),
}


def _status_trace(monkeypatch):
    """Record every `status` the worker writes to the Turn item, in order."""
    trace: list = []
    real_update, real_progress = store.update_turn, store.append_progress

    def spy_update(cid, tid, **attrs):
        if 'status' in attrs:
            trace.append(attrs['status'])
        return real_update(cid, tid, **attrs)

    def spy_progress(cid, tid, **kwargs):
        if 'status' in kwargs:
            trace.append(kwargs['status'])
        return real_progress(cid, tid, **kwargs)

    monkeypatch.setattr(store, 'update_turn', spy_update)
    monkeypatch.setattr(store, 'append_progress', spy_progress)
    return trace


SCENARIOS = {
    'text': lambda: [text_events('plain answer')],
    'read_only_tool': lambda: [tool_use_events([('tu-1', 'portal_devices_list', {'usecase_id': USECASE})]),
                               text_events('done')],
    'mutating_confirm': lambda: [tool_use_events([('tu-1', 'portal_devices_deployment_create', PROPOSAL)]),
                                 text_events('deployed')],
    'mutating_reject': lambda: [tool_use_events([('tu-1', 'portal_devices_deployment_create', PROPOSAL)]),
                                text_events('not deployed')],
    'stop': lambda: [tool_use_events([('tu-1', 'portal_devices_list', {'usecase_id': USECASE})]),
                     text_events('never reached')],
    'model_error': lambda: [client_error('AccessDeniedException')],
    'throttled_out': lambda: [client_error('ThrottlingException') for _ in range(3)],
    'budget': lambda: [tool_use_events([('tu-1', 'portal_devices_list', {'usecase_id': USECASE})], text='Hmm.')],
}


@pytest.mark.parametrize('scenario', sorted(SCENARIOS))
def test_property_turn_status_transitions(assistant_env, fake_bedrock, actions_delivered, scenario):
    """Across every worker path, the Turn's status only ever moves along
    queued → running → {awaiting_confirmation ↔ running} → {completed,
    stopped, failed}, and ends in exactly one terminal status (P10)."""
    if scenario == 'budget':
        assistant_env.monkeypatch.setenv('ASSISTANT_LIMITS_JSON', json.dumps({'model_calls_per_turn': 1}))
    fake_bedrock(*SCENARIOS[scenario]())
    trace = _status_trace(assistant_env.monkeypatch)
    claims, cid, tid = make_turn(assistant_env, prompt='Deploy to line-1')
    if scenario == 'stop':
        store.request_stop(cid, tid)
    result = run_worker(cid, tid)
    if scenario.startswith('mutating_'):
        assert result['status'] == 'awaiting_confirmation'
        card = store.get_turn(cid, tid)['confirmation']
        decision = 'confirm' if scenario == 'mutating_confirm' else 'reject'
        store.consume_binding(claims['sub'], card['binding_id'], decision)
        store.update_turn(cid, tid, status='running')          # what the API does on a decision
        result = run_worker(cid, tid, resume={'binding_id': card['binding_id']})
    final = store.get_turn(cid, tid)['status']
    assert result['status'] == final and final in store.TERMINAL_STATUSES
    # Re-writing the current status (the worker re-asserts `running` on resume
    # after the API set it) is not a transition: collapse consecutive repeats.
    path = ['queued']
    for status in trace:
        if status != path[-1]:
            path.append(status)
    assert all((a, b) in ALLOWED_TRANSITIONS for a, b in zip(path, path[1:])), path
    assert [s for s in path if s in store.TERMINAL_STATUSES] == [final]
    assert live_count(assistant_env, claims['sub']) == 0
    # a finished Turn is never re-opened
    before = len(trace)
    run_worker(cid, tid)
    assert trace[before:] == []
