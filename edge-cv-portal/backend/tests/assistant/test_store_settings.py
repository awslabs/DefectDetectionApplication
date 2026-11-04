"""The assistant table and settings record (design §7, §9, D7; tasks.md 2.2,
2.3). Requirements 5.5, 9.1-9.4, 9.6, 10.3, 12.3, 12.7; Properties P3, P4, P9.
"""
import json

import pytest
from hypothesis import given, settings as hsettings, strategies as st

from assistant import settings, store

from conftest import claims_for


def test_ulid_shape_and_ordering():
    first, second = store.new_id(1_000), store.new_id(2_000)
    assert len(first) == 26 and first < second
    import re
    assert re.match('^[0-9A-HJKMNP-TV-Z]{26}$', first)


def test_conversation_lifecycle(assistant_env):
    meta = store.create_conversation('sub-a', 'Hello', 30)
    cid = meta['conversation_id']
    assert store.get_meta(cid)['sub'] == 'sub-a'
    assert [c['conversation_id'] for c in store.list_conversations('sub-a')] == [cid]
    assert store.list_conversations('sub-b') == []
    store.append_messages(cid, [{'role': 'user', 'content': [{'text': 'q'}]},
                               {'role': 'assistant', 'content': [{'text': 'a'}]}],
                          first_seq=0, turn_id='t1', retention_days=30)
    assert [m['role'] for m in store.list_messages(cid)] == ['user', 'assistant']
    store.touch_conversation('sub-a', cid, retention_days=2, turn_delta=1, size_delta=10,
                             live_turn_id=None, title='First prompt')
    meta = store.get_meta(cid)
    assert meta['turn_count'] == 1 and meta['size_bytes'] == 10 and meta['title'] == 'First prompt'
    assert meta['ttl'] - meta['updated_at'] // 1000 == 2 * 86_400
    deleted = store.delete_conversation('sub-a', cid)
    assert deleted >= 4 and store.get_meta(cid) is None and store.list_messages(cid) == []


def test_turn_progress_reads_stop_back(assistant_env):
    meta = store.create_conversation('sub-a', '', 30)
    turn = store.create_turn(sub='sub-a', cid=meta['conversation_id'], prompt='p',
                             page_context={'route': '/'}, claims=claims_for('sub-a'), exp=99,
                             client_label='dda-portal-frontend/x', source_ip=None, user_agent=None,
                             ttl_days=7)
    assert turn['status'] == 'queued' and turn['version'] == 1
    item = store.append_progress(meta['conversation_id'], turn['turn_id'], text='hel',
                                 events=[{'seq': 1, 'type': 'tool_start'}], status='running')
    assert item['version'] == 2 and item['text'] == 'hel' and len(item['events']) == 1
    store.request_stop(meta['conversation_id'], turn['turn_id'])
    item = store.append_progress(meta['conversation_id'], turn['turn_id'], text='hello',
                                 events=[{'seq': 2, 'type': 'tool_end'}])
    assert item['stop_requested'] is True and item['version'] == 4 and len(item['events']) == 2


# ----------------------------------------------------------------- bindings

def test_binding_single_use_and_expiry(assistant_env):
    binding = store.create_binding(sub='s', cid='c', tid='t', tool='portal_devices_delete',
                                   args={'usecase_id': 'u', 'device_id': 'd', 'confirm': 'd'},
                                   seconds=600)
    assert 'confirm' not in binding['args']
    assert binding['hash'] == store.binding_hash('portal_devices_delete',
                                                 {'usecase_id': 'u', 'device_id': 'd'}, 's', 'c', 't')
    used = store.consume_binding('s', binding['binding_id'], 'confirm')
    assert used['used'] is True and used['decision'] == 'confirm'
    with pytest.raises(store.BindingUnusable) as info:
        store.consume_binding('s', binding['binding_id'], 'reject')
    assert info.value.reason == 'used'
    with pytest.raises(store.BindingUnusable) as info:
        store.consume_binding('s', store.new_id(), 'confirm')
    assert info.value.reason == 'invalid'
    expired = store.create_binding(sub='s', cid='c', tid='t', tool='x', args={}, seconds=1)
    with pytest.raises(store.BindingUnusable) as info:
        store.consume_binding('s', expired['binding_id'], 'confirm', now=store.now_s() + 5)
    assert info.value.reason == 'expired'
    with pytest.raises(store.BindingUnusable) as info:  # another user's binding is invalid
        store.consume_binding('other', expired['binding_id'], 'confirm')
    assert info.value.reason == 'invalid'


_json_scalars = st.one_of(st.integers(), st.booleans(), st.text(max_size=12), st.none())
_args = st.dictionaries(st.text(min_size=1, max_size=8), _json_scalars, max_size=5)


@given(tool=st.text(min_size=1, max_size=20), args=_args, sub=st.text(min_size=1, max_size=8),
       cid=st.text(min_size=1, max_size=8), tid=st.text(min_size=1, max_size=8))
@hsettings(max_examples=60)
def test_property_binding_hash_binds_every_field(tool, args, sub, cid, tid):
    base = store.binding_hash(tool, args, sub, cid, tid)
    assert base == store.binding_hash(tool, dict(args, confirm='anything'), sub, cid, tid)
    assert base == store.binding_hash(tool, dict(reversed(list(args.items()))), sub, cid, tid)
    assert base != store.binding_hash(tool + 'x', args, sub, cid, tid)
    assert base != store.binding_hash(tool, args, sub + 'x', cid, tid)
    assert base != store.binding_hash(tool, args, sub, cid + 'x', tid)
    assert base != store.binding_hash(tool, args, sub, cid, tid + 'x')
    changed = dict(args, _new_key='v') if '_new_key' not in args else {k: v for k, v in args.items()
                                                                        if k != '_new_key'}
    assert base != store.binding_hash(tool, changed, sub, cid, tid)


# ------------------------------------------------------------------- quotas

def test_quota_reservation_limits(assistant_env):
    limits = {'turns_per_hour': 2, 'concurrent_turns': 1}
    assert store.reserve_turn('q', limits, now=3_600) is None
    assert store.reserve_turn('q', limits, now=3_600) == ('concurrent_turns', None)
    store.release_live('q')
    assert store.reserve_turn('q', limits, now=3_600) is None
    store.release_live('q')
    refused = store.reserve_turn('q', limits, now=3_600)
    assert refused[0] == 'turns_per_hour' and refused[1].endswith('T02:00:00Z')
    # the next hour has its own counter
    assert store.reserve_turn('q', limits, now=7_200) is None
    store.release_live('q')
    store.release_live('q')  # never goes negative
    assert store.reserve_turn('q', limits, now=7_200) is None
    assert store.add_tokens('q', 10, now=7_200) == 10 and store.tokens_today('q', now=7_200) == 10


@given(st.lists(st.sampled_from(['reserve', 'release']), min_size=1, max_size=25))
@hsettings(max_examples=40, deadline=None,
           suppress_health_check=[__import__('hypothesis').HealthCheck.function_scoped_fixture])
def test_property_live_counter_stays_within_bounds(assistant_env, ops):
    # The fixture is shared across examples on purpose: every example uses a
    # fresh `sub`, so the table rows never collide.
    sub = f'p-{store.new_id()}'
    limit = 2
    live = 0
    for op in ops:
        if op == 'reserve':
            refused = store.reserve_turn(sub, {'turns_per_hour': 10_000, 'concurrent_turns': limit})
            if refused is None:
                live += 1
            else:
                assert refused[0] == 'concurrent_turns' and live == limit
        else:
            store.release_live(sub)
            live = max(0, live - 1)
        item = assistant_env.table.get_item(Key={'pk': f'USER#{sub}', 'sk': 'QUOTA#LIVE'}).get('Item')
        count = int(item['count']) if item else 0
        assert 0 <= count <= limit and count == live


# ----------------------------------------------------------------- settings

def test_settings_defaults_write_and_audit(assistant_env):
    config = settings.read_configuration(0)
    assert config['enabled'] is True and config['retention_days'] == 30
    assert settings.actions_enabled(config) is True   # Phase 2: delivered, switch default on
    assert settings.actions_enabled({**config, 'actions_enabled': False}) is False
    user = {'user_id': 'admin-1', 'username': 'admin', 'email': 'a@example.com', 'role': 'PortalAdmin'}
    updated = settings.write_configuration({'enabled': False, 'retention_days': 7}, user)
    assert updated['enabled'] is False and updated['retention_days'] == 7
    assert updated['updated_by'] == 'admin'
    settings.clear_cache()
    assert settings.read_configuration(0)['enabled'] is False
    unchanged = settings.write_configuration({'enabled': False}, user)
    assert unchanged['retention_days'] == 7
    entries = assistant_env.shared.dynamodb.Table(assistant_env.shared.AUDIT_LOG_TABLE).scan(
        FilterExpression=__import__('boto3').dynamodb.conditions.Attr('user_id').eq('admin-1'))['Items']
    changes = sorted((e['details']['setting'], str(e['details']['previous']), str(e['details']['new']))
                     for e in entries if e['action'] == 'assistant_settings_changed')
    assert changes == [('enabled', 'True', 'False'), ('retention_days', '30', '7')]


def test_settings_cache_and_coercion(assistant_env, monkeypatch):
    assistant_env.settings_table.put_item(Item={'setting_key': 'assistant_configuration',
                                                'enabled': 'false', 'retention_days': '9999',
                                                'guardrail_id': 'abc'})
    settings.clear_cache()
    config = settings.read_configuration(0)
    assert config['enabled'] is False and config['retention_days'] == 365
    assert config['guardrail_id'] == 'abc'
    cached = settings.read_configuration(30)
    assistant_env.settings_table.put_item(Item={'setting_key': 'assistant_configuration',
                                                'enabled': True})
    assert settings.read_configuration(30)['enabled'] is False  # served from the cache
    assert settings.read_configuration(0)['enabled'] is True    # the worker never caches
    assert cached['enabled'] is False


def test_model_capability_record(assistant_env):
    assert settings.model_capability('m-1')['tool_use_supported'] is None
    settings.record_model_capability('m-1', False)
    assert settings.model_capability('m-1')['tool_use_supported'] is False
    assert settings.model_capability('m-2')['tool_use_supported'] is None
