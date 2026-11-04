"""The assistant REST API (design §6, §9; tasks.md 2.10). Requirements 1.5,
1.6, 3.1-3.3, 6.1, 6.6, 9.1-9.2, 10.2-10.3, 10.6, 12.1-12.8.
"""
import json

import pytest
from boto3.dynamodb.conditions import Attr

from assistant import api, settings, store

from conftest import api_event, call_api, claims_for

CONVS = '/assistant/conversations'
CONV = '/assistant/conversations/{cid}'
TURNS = '/assistant/conversations/{cid}/turns'
TURN = '/assistant/conversations/{cid}/turns/{tid}'


def create_conversation(claims, title='My chat'):
    status, body = call_api(api_event('POST', CONVS, claims, body={'title': title}))
    assert status == 201, body
    return body['conversation_id']


def start_turn(claims, cid, prompt='What is this page?', route='/devices'):
    status, body = call_api(api_event('POST', TURNS, claims, params={'cid': cid},
                                      body={'prompt': prompt,
                                            'page_context': {'route': route, 'usecase_id': 'uc-1'}}))
    return status, body


# ---------------------------------------------------------------- state

def test_state_for_a_regular_user(assistant_env):
    status, body = call_api(api_event('GET', '/assistant/state', claims_for()))
    assert status == 200
    # Phase 2: actions are delivered and the Actions_Switch defaults to on (Requirement 10.4)
    assert body == {'enabled': True, 'actions_enabled': True, 'version': api.__version__}


def test_state_when_disabled_and_for_labelers(assistant_env):
    settings.write_configuration({'enabled': False},
                                 {'user_id': 'admin', 'username': 'admin'})
    status, body = call_api(api_event('GET', '/assistant/state', claims_for()))
    assert body['enabled'] is False and body['reason'] == 'disabled_by_admin'
    settings.write_configuration({'enabled': True}, {'user_id': 'admin', 'username': 'admin'})
    settings.clear_cache()
    status, body = call_api(api_event('GET', '/assistant/state', claims_for(role='DataLabeler')))
    assert body['enabled'] is False and body['reason'] == 'role_excluded'
    status, body = call_api(api_event('POST', CONVS, claims_for(role='DataLabeler'), body={}))
    assert status == 403 and body['error']['code'] == 'role_excluded'


def test_registry_role_wins_over_the_claimed_role(assistant_env):
    class Role:
        value = 'DataLabeler'
    assistant_env.monkeypatch.setattr(assistant_env.shared.rbac_manager, 'get_user_role',
                                      lambda *_a, **_k: Role())
    status, body = call_api(api_event('GET', '/assistant/state', claims_for(role='PortalAdmin')))
    assert body['enabled'] is False and body['reason'] == 'role_excluded'


def test_unknown_route_and_missing_identity(assistant_env):
    status, body = call_api(api_event('GET', '/assistant/nope', claims_for()))
    assert status == 404
    event = api_event('GET', '/assistant/state', {})
    status, body = call_api(event)
    assert status == 401 and body['error']['code'] == 'not_authenticated'
    status, _ = call_api({**api_event('GET', '/assistant/state', claims_for()), 'httpMethod': 'OPTIONS'})
    assert status == 200


# ------------------------------------------------------------- settings

def test_settings_require_portal_admin_and_are_audited(assistant_env):
    assistant_env.monkeypatch.setattr(assistant_env.shared.rbac_manager, 'has_permission',
                                      lambda *_a, **_k: False)
    user = claims_for(role='UseCaseAdmin')
    status, body = call_api(api_event('GET', '/assistant/settings', user))
    assert status == 403 and body['error']['code'] == 'forbidden'
    denied = assistant_env.shared.dynamodb.Table(assistant_env.shared.AUDIT_LOG_TABLE).scan(
        FilterExpression=Attr('user_id').eq(user['sub']))['Items']
    assert [d['action'] for d in denied] == ['unauthorized_access']
    assistant_env.monkeypatch.setattr(assistant_env.shared.rbac_manager, 'has_permission',
                                      lambda *_a, **_k: True)
    admin = claims_for(role='PortalAdmin')
    status, body = call_api(api_event('GET', '/assistant/settings', admin))
    assert status == 200 and body['actions_delivered'] is True
    assert body['destructive_allowlist'] == sorted(api.DESTRUCTIVE_ALLOWLIST)
    assert body['model']['model_id'] and body['limits']['turns_per_hour'] == 60
    status, body = call_api(api_event('PUT', '/assistant/settings', admin,
                                      body={'retention_days': 14, 'guardrail_id': 'abc123',
                                            'guardrail_version': '2'}))
    assert status == 200 and body['retention_days'] == 14 and body['guardrail_version'] == '2'
    # the Actions_Switch (Phase 2): off hides every mutating tool from the model
    status, body = call_api(api_event('PUT', '/assistant/settings', admin,
                                      body={'actions_enabled': False}))
    assert status == 200 and body['actions_enabled'] is False
    status, state = call_api(api_event('GET', '/assistant/state', claims_for()))
    assert state['actions_enabled'] is False
    status, body = call_api(api_event('PUT', '/assistant/settings', admin, body={'actions_enabled': True}))
    assert status == 200 and body['actions_enabled'] is True
    # while actions are undelivered the switch is refused (the Phase 1 posture)
    assistant_env.monkeypatch.setattr(api, 'ACTIONS_DELIVERED', False)
    status, body = call_api(api_event('PUT', '/assistant/settings', admin,
                                      body={'actions_enabled': True}))
    assert status == 400 and body['error']['code'] == 'actions_not_delivered'
    assistant_env.monkeypatch.setattr(api, 'ACTIONS_DELIVERED', True)
    status, body = call_api(api_event('PUT', '/assistant/settings', admin, body={'retention_days': 0}))
    assert status == 400 and body['error']['details']['field'] == 'retention_days'
    audited = assistant_env.shared.dynamodb.Table(assistant_env.shared.AUDIT_LOG_TABLE).scan(
        FilterExpression=Attr('user_id').eq(admin['sub']))['Items']
    # one entry per changed field: the three fields of the first PUT plus the
    # two Actions_Switch writes (off, then on)
    assert sorted(a['details']['setting'] for a in audited) == ['actions_enabled', 'actions_enabled',
                                                                'guardrail_id', 'guardrail_version',
                                                                'retention_days']


# -------------------------------------------------------- conversations

def test_conversations_are_private_to_their_owner(assistant_env):
    alice, bob = claims_for(), claims_for()
    cid = create_conversation(alice)
    status, body = call_api(api_event('GET', CONVS, alice))
    assert status == 200 and [c['conversation_id'] for c in body['items']] == [cid]
    status, body = call_api(api_event('GET', CONVS, bob))
    assert body['items'] == []
    for method, resource in (('GET', CONV), ('DELETE', CONV)):
        status, body = call_api(api_event(method, resource, bob, params={'cid': cid}))
        assert status == 404, (method, body)
    status, body = start_turn(bob, cid)
    assert status == 404
    status, body = call_api(api_event('GET', CONV, alice, params={'cid': cid}))
    assert status == 200 and body['messages'] == [] and body['title'] == 'My chat'
    status, body = call_api(api_event('DELETE', CONV, alice, params={'cid': cid}))
    assert status == 204
    status, _ = call_api(api_event('GET', CONV, alice, params={'cid': cid}))
    assert status == 404
    status, body = call_api(api_event('GET', CONV, alice, params={'cid': 'not-a-ulid'}))
    assert status == 404


def test_conversation_list_limit_validation(assistant_env):
    status, body = call_api(api_event('GET', CONVS, claims_for(), query={'limit': 'abc'}))
    assert status == 400 and body['error']['details']['field'] == 'limit'


# ---------------------------------------------------------------- turns

def test_turn_start_records_the_turn_and_invokes_the_worker(assistant_env):
    alice = claims_for()
    cid = create_conversation(alice)
    status, body = start_turn(alice, cid, prompt='  Where am I?  ')
    assert status == 202, body
    tid = body['turn_id']
    assert assistant_env.invoked == [{'conversation_id': cid, 'turn_id': tid}]
    turn = store.get_turn(cid, tid)
    assert turn['status'] == 'queued' and turn['sub'] == alice['sub']
    assert turn['claims'] == alice and turn['exp'] == alice['exp']
    assert turn['client_label'] == 'dda-portal-frontend/abc1234'
    assert turn['source_ip'] == '203.0.113.5' and turn['page_context']['usecase_id'] == 'uc-1'
    assert store.get_meta(cid)['live_turn_id'] == tid
    # a second prompt while the turn is live is refused
    status, body = start_turn(alice, cid)
    assert status == 409 and body['error']['code'] == 'turn_in_progress'
    # the poll
    status, body = call_api(api_event('GET', TURN, alice, params={'cid': cid, 'tid': tid}))
    assert status == 200 and body['status'] == 'queued' and body['text'] == ''
    status, body = call_api(api_event('GET', TURN, alice, params={'cid': cid, 'tid': tid},
                                      query={'after': str(body['version'])}))
    assert set(body) == {'turn_id', 'conversation_id', 'status', 'version'}
    # stop
    status, body = call_api(api_event('POST', TURN + '/stop', alice, params={'cid': cid, 'tid': tid}))
    assert status == 202 and store.get_turn(cid, tid)['stop_requested'] is True


@pytest.mark.parametrize('raw, expected', [
    (4102444800, 4102444800),                       # number (Lambda authorizer, fixtures)
    ('4102444800', 4102444800),                     # numeric string
    ('Tue Nov 03 17:16:29 UTC 2026', 1793726189),   # the REST Cognito authorizer's Java Date
    ('Fri Jan 01 00:00:00 GMT 2027', 1798761600),
    ('Tue Nov 03 17:16:29 PST 2026', None),         # a zone the parser does not know
    ('Nov 03 2026', None), ('', None), (None, None), (True, None), (1.5, 1),
    ('Xyz Foo 99 17:16:29 UTC 2026', None),
])
def test_token_expiry_forms(raw, expected):
    assert api.token_expiry({'exp': raw}) == expected


def test_turn_start_reads_the_gateway_date_form_of_exp(assistant_env):
    """The deployed REST authorizer hands `exp` as `Tue Nov 03 17:16:29 UTC
    2026`; the Turn must carry the epoch seconds the worker compares against
    (found on the first deployed smoke check, 2026-11-03)."""
    alice = claims_for(exp='Fri Jan 01 00:00:00 UTC 2100')
    cid = create_conversation(alice)
    status, body = start_turn(alice, cid)
    assert status == 202, body
    assert store.get_turn(cid, body['turn_id'])['exp'] == 4102444800


def test_turn_start_refuses_an_unreadable_exp_without_reserving(assistant_env):
    alice = claims_for(exp='not a date')
    cid = create_conversation(alice)
    status, body = start_turn(alice, cid)
    assert status == 401 and body['error']['code'] == 'not_authenticated'
    assert assistant_env.invoked == []
    assert store.get_meta(cid)['live_turn_id'] is None
    live = assistant_env.table.get_item(Key={'pk': f"USER#{alice['sub']}", 'sk': 'QUOTA#LIVE'}).get('Item')
    assert not live or int(live['count']) == 0


def test_turn_start_validation_and_bad_client_label(assistant_env):
    alice = claims_for()
    cid = create_conversation(alice)
    status, body = call_api(api_event('POST', TURNS, alice, params={'cid': cid},
                                      body={'prompt': '   ', 'page_context': {'route': '/x'}}))
    assert status == 400 and body['error']['details']['field'] == 'prompt'
    status, body = call_api(api_event('POST', TURNS, alice, params={'cid': cid}, body={'prompt': 'x'}))
    assert status == 400 and body['error']['details']['field'] == 'page_context'
    event = api_event('POST', TURNS, alice, params={'cid': cid},
                      body={'prompt': 'x', 'page_context': {'route': '/x'}},
                      headers={'X-Portal-Build': 'bad label with spaces'})
    status, body = call_api(event)
    assert status == 202
    assert store.get_turn(cid, body['turn_id'])['client_label'] == 'dda-portal-frontend/unknown'


def test_turn_start_quotas(assistant_env):
    assistant_env.monkeypatch.setenv('ASSISTANT_LIMITS_JSON', json.dumps({'turns_per_hour': 1}))
    alice = claims_for()
    cid = create_conversation(alice)
    status, body = start_turn(alice, cid)
    assert status == 202
    store.update_turn(cid, body['turn_id'], status='completed')
    store.touch_conversation(alice['sub'], cid, retention_days=30, live_turn_id=None)
    store.release_live(alice['sub'])
    status, body = start_turn(alice, cid)
    assert status == 429 and body['error']['details']['limit'] == 'turns_per_hour'
    assistant_env.monkeypatch.setenv('ASSISTANT_LIMITS_JSON', json.dumps({'tokens_per_day': 10}))
    store.add_tokens(alice['sub'], 11)
    status, body = start_turn(alice, cid)
    assert status == 429 and body['error']['details']['limit'] == 'tokens_per_day'


def test_conversation_full(assistant_env):
    assistant_env.monkeypatch.setenv('ASSISTANT_LIMITS_JSON', json.dumps({'conversation_turns': 1}))
    alice = claims_for()
    cid = create_conversation(alice)
    store.touch_conversation(alice['sub'], cid, retention_days=30, turn_delta=1)
    status, body = start_turn(alice, cid)
    assert status == 409 and body['error']['code'] == 'conversation_full'


def test_lazy_reap_of_a_lost_worker(assistant_env):
    alice = claims_for()
    cid = create_conversation(alice)
    status, body = start_turn(alice, cid)
    tid = body['turn_id']
    store.update_turn(cid, tid, status='running')
    assistant_env.table.update_item(Key={'pk': f'CONV#{cid}', 'sk': f'TURN#{tid}'},
                                    UpdateExpression='SET updated_at = :old',
                                    ExpressionAttributeValues={':old': 1})
    status, body = call_api(api_event('GET', TURN, alice, params={'cid': cid, 'tid': tid}))
    assert body['status'] == 'failed' and body['failure']['code'] == 'worker_lost'
    assert store.get_meta(cid)['live_turn_id'] is None
    status, body = start_turn(alice, cid)  # the conversation is usable again
    assert status == 202


def test_worker_invocation_failure_fails_the_turn(assistant_env):
    def boom(**_):
        raise RuntimeError('no lambda')
    assistant_env.monkeypatch.setattr(api, '_LAMBDA', type('L', (), {'invoke': staticmethod(boom)})())
    alice = claims_for()
    cid = create_conversation(alice)
    status, body = start_turn(alice, cid)
    assert status == 503 and body['error']['code'] == 'worker_unavailable'
    assert store.get_meta(cid)['live_turn_id'] is None


# ------------------------------------------------------- confirmations

def test_confirmation_route(assistant_env):
    alice = claims_for()
    cid = create_conversation(alice)
    status, body = start_turn(alice, cid)
    tid = body['turn_id']
    status, body = call_api(api_event('POST', TURN + '/confirmation', alice,
                                      params={'cid': cid, 'tid': tid},
                                      body={'binding_id': store.new_id(), 'decision': 'confirm'}))
    assert status == 409 and body['error']['code'] == 'not_awaiting_confirmation'
    binding = store.create_binding(sub=alice['sub'], cid=cid, tid=tid, tool='portal_devices_delete',
                                   args={'usecase_id': 'uc-1', 'device_id': 'd-1'}, seconds=600)
    store.update_turn(cid, tid, status='awaiting_confirmation',
                      confirmation={'binding_id': binding['binding_id'], 'tool': 'portal_devices_delete',
                                    'expires_at': binding['expires_at']})
    status, body = call_api(api_event('POST', TURN + '/confirmation', alice,
                                      params={'cid': cid, 'tid': tid},
                                      body={'binding_id': store.new_id(), 'decision': 'confirm'}))
    assert status == 404 and body['error']['code'] == 'binding_invalid'
    status, body = call_api(api_event('POST', TURN + '/confirmation', alice,
                                      params={'cid': cid, 'tid': tid},
                                      body={'binding_id': binding['binding_id'], 'decision': 'confirm'}))
    assert status == 202, body
    assert assistant_env.invoked[-1] == {'conversation_id': cid, 'turn_id': tid,
                                         'resume': {'binding_id': binding['binding_id']}}
    assert store.get_turn(cid, tid)['status'] == 'running'
    store.update_turn(cid, tid, status='awaiting_confirmation')
    status, body = call_api(api_event('POST', TURN + '/confirmation', alice,
                                      params={'cid': cid, 'tid': tid},
                                      body={'binding_id': binding['binding_id'], 'decision': 'reject'}))
    assert status == 409 and body['error']['code'] == 'binding_used'


def test_expired_confirmation_stops_the_turn(assistant_env):
    alice = claims_for()
    cid = create_conversation(alice)
    status, body = start_turn(alice, cid)
    tid = body['turn_id']
    binding = store.create_binding(sub=alice['sub'], cid=cid, tid=tid, tool='x', args={}, seconds=600)
    store.update_turn(cid, tid, status='awaiting_confirmation',
                      confirmation={'binding_id': binding['binding_id'], 'tool': 'x', 'expires_at': 1})
    status, body = call_api(api_event('GET', TURN, alice, params={'cid': cid, 'tid': tid}))
    assert body['status'] == 'stopped' and body['events'][-1]['type'] == 'confirmation_expired'
    assert store.get_meta(cid)['live_turn_id'] is None


def test_stop_while_awaiting_confirmation_closes_the_turn(assistant_env):
    alice = claims_for()
    cid = create_conversation(alice)
    status, body = start_turn(alice, cid)
    tid = body['turn_id']
    store.update_turn(cid, tid, status='awaiting_confirmation',
                      confirmation={'binding_id': store.new_id(), 'tool': 'x',
                                    'expires_at': store.now_s() + 600})
    status, body = call_api(api_event('POST', TURN + '/stop', alice, params={'cid': cid, 'tid': tid}))
    assert status == 202 and body['status'] == 'stopped'
    assert store.get_meta(cid)['live_turn_id'] is None
    status, body = call_api(api_event('DELETE', CONV, alice, params={'cid': cid}))
    assert status == 204
