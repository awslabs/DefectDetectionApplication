"""The assistant's table (design §7, D7): Conversations, messages, Turns,
confirmation bindings and quota counters in one DynamoDB table
(`ASSISTANT_TABLE`, PK `pk`, SK `sk`, TTL attribute `ttl`).

Identifiers are ULIDs generated here. Every number is written as `Decimal`
and read back as `int`/`float` through `plain()`.
"""
import base64
import datetime as dt
import decimal
import hashlib
import json
import os
import secrets
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

TABLE_ENV = 'ASSISTANT_TABLE'
_TABLE = None

TURN_STATUSES = ('queued', 'running', 'awaiting_confirmation', 'completed', 'stopped', 'failed')
LIVE_STATUSES = ('queued', 'running', 'awaiting_confirmation')
TERMINAL_STATUSES = ('completed', 'stopped', 'failed')

_CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'


# ------------------------------------------------------------------ helpers

def table():
    global _TABLE
    if _TABLE is None:
        name = os.environ[TABLE_ENV]
        _TABLE = boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION')).Table(name)
    return _TABLE


def reset_table() -> None:
    global _TABLE
    _TABLE = None


def now_ms() -> int:
    return int(time.time() * 1000)


def now_s() -> int:
    return int(time.time())


def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).isoformat(
        timespec='seconds').replace('+00:00', 'Z')


def new_id(timestamp_ms: Optional[int] = None) -> str:
    """A ULID: 48 bits of milliseconds then 80 random bits, Crockford base32."""
    ts = now_ms() if timestamp_ms is None else int(timestamp_ms)
    value = (ts << 80) | int.from_bytes(secrets.token_bytes(10), 'big')
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return ''.join(reversed(chars))


def plain(value: Any) -> Any:
    """Decimals to ints/floats, recursively (DynamoDB reads)."""
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def _numbers(value: Any) -> Any:
    """Floats to Decimals, recursively (DynamoDB writes)."""
    if isinstance(value, float):
        return decimal.Decimal(str(value))
    if isinstance(value, dict):
        return {k: _numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_numbers(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      default=str)


def binding_hash(tool: str, args: Dict[str, Any], sub: str, cid: str, tid: str) -> str:
    """The Confirmation_Binding hash (design §7): the tool, its canonical
    arguments without `confirm`, the owner, the Conversation and the Turn."""
    clean = {k: v for k, v in (args or {}).items() if k != 'confirm'}
    payload = canonical_json({'tool': tool, 'args': clean}) + '|' + sub + '|' + cid + '|' + tid
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def ttl_at(ms: int, days: float) -> int:
    return int(ms / 1000) + int(days * 86_400)


def size_of(value: Any) -> int:
    return len(canonical_json(value).encode('utf-8'))


# ------------------------------------------------------------ conversations

def _owner_key(sub: str, cid: str) -> Dict[str, str]:
    return {'pk': f'USER#{sub}', 'sk': f'CONV#{cid}'}


def _meta_key(cid: str) -> Dict[str, str]:
    return {'pk': f'CONV#{cid}', 'sk': 'META'}


def create_conversation(sub: str, title: str, retention_days: int) -> Dict[str, Any]:
    cid = new_id()
    ms = now_ms()
    base = {'sub': sub, 'conversation_id': cid, 'title': title[:80] if title else '',
            'created_at': ms, 'updated_at': ms, 'turn_count': 0, 'size_bytes': 0,
            'live_turn_id': None, 'ttl': ttl_at(ms, retention_days)}
    with table().batch_writer() as batch:
        batch.put_item(Item={**_owner_key(sub, cid), **base})
        batch.put_item(Item={**_meta_key(cid), **base})
    return plain(base)


def get_meta(cid: str) -> Optional[Dict[str, Any]]:
    item = table().get_item(Key=_meta_key(cid)).get('Item')
    return plain(item) if item else None


def list_conversations(sub: str, limit: int = 20) -> List[Dict[str, Any]]:
    response = table().query(
        KeyConditionExpression=Key('pk').eq(f'USER#{sub}') & Key('sk').begins_with('CONV#'),
        ScanIndexForward=False, Limit=max(1, min(int(limit), 50)))
    items = [plain(i) for i in response.get('Items', [])]
    for item in items:
        item.pop('pk', None)
        item.pop('sk', None)
    return items


def touch_conversation(sub: str, cid: str, *, retention_days: int, title: Optional[str] = None,
                       turn_delta: int = 0, size_delta: int = 0,
                       live_turn_id: Any = '__keep__') -> None:
    ms = now_ms()
    sets = ['updated_at = :u', '#ttl = :t', 'turn_count = if_not_exists(turn_count, :z) + :td',
            'size_bytes = if_not_exists(size_bytes, :z) + :sd']
    values: Dict[str, Any] = {':u': ms, ':t': ttl_at(ms, retention_days), ':z': 0,
                              ':td': int(turn_delta), ':sd': int(size_delta)}
    names = {'#ttl': 'ttl'}
    if title is not None:
        sets.append('title = :title')
        values[':title'] = title[:80]
    if live_turn_id != '__keep__':
        sets.append('live_turn_id = :live')
        values[':live'] = live_turn_id
    expression = 'SET ' + ', '.join(sets)
    for key in (_owner_key(sub, cid), _meta_key(cid)):
        table().update_item(Key=key, UpdateExpression=expression,
                            ExpressionAttributeValues=values, ExpressionAttributeNames=names)


def _conversation_items(cid: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {'KeyConditionExpression': Key('pk').eq(f'CONV#{cid}'),
                              'ProjectionExpression': 'pk, sk'}
    while True:
        response = table().query(**kwargs)
        items.extend(response.get('Items', []))
        if 'LastEvaluatedKey' not in response:
            return items
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']


def delete_conversation(sub: str, cid: str) -> int:
    """Delete every item of the Conversation and its bindings; returns the count."""
    keys = [{'pk': i['pk'], 'sk': i['sk']} for i in _conversation_items(cid)]
    keys.append(_owner_key(sub, cid))
    bindings = table().query(
        KeyConditionExpression=Key('pk').eq(f'USER#{sub}') & Key('sk').begins_with('BIND#'),
        FilterExpression=Attr('cid').eq(cid), ProjectionExpression='pk, sk')
    keys.extend({'pk': i['pk'], 'sk': i['sk']} for i in bindings.get('Items', []))
    with table().batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key=key)
    return len(keys)


# ----------------------------------------------------------------- messages

def append_messages(cid: str, messages: Iterable[Dict[str, Any]], *, first_seq: int,
                    turn_id: str, retention_days: int) -> int:
    """Store messages as `MSG#<seq>` items from `first_seq`; returns the next seq."""
    ms = now_ms()
    seq = int(first_seq)
    with table().batch_writer() as batch:
        for message in messages:
            item = {'pk': f'CONV#{cid}', 'sk': f'MSG#{seq:06d}', 'seq': seq,
                    'role': message['role'], 'content': message['content'],
                    'turn_id': turn_id, 'created_at': ms, 'ttl': ttl_at(ms, retention_days)}
            if message.get('display') is not None:
                item['display'] = message['display']  # the browser's re-render form
            batch.put_item(Item=_numbers(item))
            seq += 1
    return seq


def list_messages(cid: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {
        'KeyConditionExpression': Key('pk').eq(f'CONV#{cid}') & Key('sk').begins_with('MSG#')}
    while True:
        response = table().query(**kwargs)
        items.extend(plain(i) for i in response.get('Items', []))
        if 'LastEvaluatedKey' not in response:
            break
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']
    return items


# -------------------------------------------------------------------- turns

def _turn_key(cid: str, tid: str) -> Dict[str, str]:
    return {'pk': f'CONV#{cid}', 'sk': f'TURN#{tid}'}


def create_turn(*, sub: str, cid: str, prompt: str, page_context: Dict[str, Any],
                claims: Dict[str, Any], exp: int, client_label: Optional[str],
                source_ip: Optional[str], user_agent: Optional[str], ttl_days: int) -> Dict[str, Any]:
    tid = new_id()
    ms = now_ms()
    item = {
        **_turn_key(cid, tid),
        'sub': sub, 'conversation_id': cid, 'turn_id': tid, 'status': 'queued', 'version': 1,
        'prompt': prompt, 'page_context': page_context, 'claims': claims, 'exp': int(exp),
        'client_label': client_label, 'source_ip': source_ip, 'user_agent': user_agent,
        'text': '', 'events': [], 'confirmation': None,
        'pending': {'messages': [], 'tool_uses': [], 'extra': []},
        'stop_requested': False, 'failure': None,
        'usage': {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0},
        'model_calls': 0, 'tool_calls': 0, 'pipeline_calls': 0, 'confirmations': 0,
        'created_at': ms, 'started_at': None, 'updated_at': ms, 'finished_at': None,
        'ttl': ttl_at(ms, ttl_days),
    }
    table().put_item(Item=_numbers(item))
    return plain(item)


def get_turn(cid: str, tid: str) -> Optional[Dict[str, Any]]:
    item = table().get_item(Key=_turn_key(cid, tid)).get('Item')
    return plain(item) if item else None


def update_turn(cid: str, tid: str, **attrs: Any) -> Dict[str, Any]:
    """SET the given attributes, bump `version` and `updated_at`; returns the
    new item (so the worker reads `stop_requested` back for free)."""
    names = {'#v': 'version', '#u': 'updated_at'}
    values: Dict[str, Any] = {':one': 1, ':u': now_ms()}
    sets = ['#v = #v + :one', '#u = :u']
    for index, (key, value) in enumerate(attrs.items()):
        names[f'#a{index}'] = key
        values[f':a{index}'] = _numbers(value)
        sets.append(f'#a{index} = :a{index}')
    response = table().update_item(
        Key=_turn_key(cid, tid), UpdateExpression='SET ' + ', '.join(sets),
        ExpressionAttributeNames=names, ExpressionAttributeValues=values,
        ReturnValues='ALL_NEW')
    return plain(response['Attributes'])


def append_progress(cid: str, tid: str, *, text: Optional[str] = None,
                    events: Optional[List[Dict[str, Any]]] = None, **attrs: Any) -> Dict[str, Any]:
    """Replace `text`, append `events`, SET other attributes; bump version."""
    names = {'#v': 'version', '#u': 'updated_at'}
    values: Dict[str, Any] = {':one': 1, ':u': now_ms()}
    sets = ['#v = #v + :one', '#u = :u']
    if text is not None:
        names['#text'] = 'text'
        values[':text'] = text
        sets.append('#text = :text')
    if events:
        names['#ev'] = 'events'
        values[':ev'] = _numbers(list(events))
        values[':empty'] = []
        sets.append('#ev = list_append(if_not_exists(#ev, :empty), :ev)')
    for index, (key, value) in enumerate(attrs.items()):
        names[f'#a{index}'] = key
        values[f':a{index}'] = _numbers(value)
        sets.append(f'#a{index} = :a{index}')
    response = table().update_item(
        Key=_turn_key(cid, tid), UpdateExpression='SET ' + ', '.join(sets),
        ExpressionAttributeNames=names, ExpressionAttributeValues=values,
        ReturnValues='ALL_NEW')
    return plain(response['Attributes'])


def request_stop(cid: str, tid: str) -> Dict[str, Any]:
    return update_turn(cid, tid, stop_requested=True)


# ----------------------------------------------------------------- bindings

def _binding_key(sub: str, bid: str) -> Dict[str, str]:
    return {'pk': f'USER#{sub}', 'sk': f'BIND#{bid}'}


def create_binding(*, sub: str, cid: str, tid: str, tool: str, args: Dict[str, Any],
                   seconds: int) -> Dict[str, Any]:
    bid = new_id()
    ms = now_ms()
    clean = {k: v for k, v in (args or {}).items() if k != 'confirm'}
    item = {
        **_binding_key(sub, bid), 'binding_id': bid, 'sub': sub, 'cid': cid, 'tid': tid,
        'tool': tool, 'args': clean, 'hash': binding_hash(tool, clean, sub, cid, tid),
        'created_at': ms, 'expires_at': int(ms / 1000) + int(seconds), 'used': False,
        'decision': None, 'ttl': int(ms / 1000) + int(seconds) + 86_400,
    }
    table().put_item(Item=_numbers(item))
    return plain(item)


def get_binding(sub: str, bid: str) -> Optional[Dict[str, Any]]:
    item = table().get_item(Key=_binding_key(sub, bid)).get('Item')
    return plain(item) if item else None


class BindingUnusable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # 'invalid' | 'used' | 'expired'


def consume_binding(sub: str, bid: str, decision: str, now: Optional[int] = None) -> Dict[str, Any]:
    """Mark the binding used with `decision` iff it exists, is unused and is
    not expired (one conditional write). Raises `BindingUnusable`."""
    now = now_s() if now is None else int(now)
    try:
        response = table().update_item(
            Key=_binding_key(sub, bid),
            UpdateExpression='SET used = :t, decision = :d, decided_at = :n',
            ConditionExpression='attribute_exists(pk) AND used = :f AND expires_at > :n',
            ExpressionAttributeValues={':t': True, ':f': False, ':d': decision, ':n': now},
            ReturnValues='ALL_NEW')
    except ClientError as error:
        if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        current = get_binding(sub, bid)
        if current is None:
            raise BindingUnusable('invalid') from error
        if current.get('used'):
            raise BindingUnusable('used') from error
        raise BindingUnusable('expired') from error
    return plain(response['Attributes'])


# ------------------------------------------------------------------- quotas

def _quota_key(sub: str, suffix: str) -> Dict[str, str]:
    return {'pk': f'USER#{sub}', 'sk': f'QUOTA#{suffix}'}


def _day(now: int) -> str:
    return dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).strftime('%Y-%m-%d')


def _hour(now: int) -> str:
    return dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).strftime('%Y-%m-%dT%H')


def _next_hour(now: int) -> str:
    return iso(((now // 3600) + 1) * 3600 * 1000)


def _next_day(now: int) -> str:
    return iso(((now // 86_400) + 1) * 86_400 * 1000)


def _increment(key: Dict[str, str], attribute: str, amount: int, limit: Optional[int],
               ttl: int) -> bool:
    """Conditionally add `amount` while the result stays <= limit. True on success."""
    values: Dict[str, Any] = {':a': int(amount), ':z': 0, ':ttl': int(ttl)}
    kwargs: Dict[str, Any] = {
        'Key': key,
        'UpdateExpression': f'SET #c = if_not_exists(#c, :z) + :a, #ttl = :ttl',
        'ExpressionAttributeNames': {'#c': attribute, '#ttl': 'ttl'},
        'ExpressionAttributeValues': values,
    }
    if limit is not None:
        values[':max'] = int(limit) - int(amount)
        kwargs['ConditionExpression'] = 'attribute_not_exists(#c) OR #c <= :max'
    try:
        table().update_item(**kwargs)
        return True
    except ClientError as error:
        if error.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False
        raise


def reserve_turn(sub: str, limits: Dict[str, int],
                 now: Optional[int] = None) -> Optional[Tuple[str, str]]:
    """Reserve one Turn against the hour, day and live limits; None when
    allowed, else `(limit_name, resets_at)`. A refused reservation leaves the
    counters as they were (each step is undone on a later refusal)."""
    now = now_s() if now is None else int(now)
    hour_key = _quota_key(sub, f'HOUR#{_hour(now)}')
    day_key = _quota_key(sub, f'DAY#{_day(now)}')
    live_key = _quota_key(sub, 'LIVE')
    if not _increment(hour_key, 'turns', 1, limits['turns_per_hour'], now + 2 * 3600):
        return 'turns_per_hour', _next_hour(now)
    if not _increment(day_key, 'turns', 1, None, now + 2 * 86_400):
        _increment(hour_key, 'turns', -1, None, now + 2 * 3600)
        return 'turns_per_day', _next_day(now)
    if not _increment(live_key, 'count', 1, limits['concurrent_turns'], now + 86_400):
        _increment(hour_key, 'turns', -1, None, now + 2 * 3600)
        _increment(day_key, 'turns', -1, None, now + 2 * 86_400)
        return 'concurrent_turns', None
    return None


def release_live(sub: str, now: Optional[int] = None) -> None:
    now = now_s() if now is None else int(now)
    try:
        table().update_item(
            Key=_quota_key(sub, 'LIVE'),
            UpdateExpression='SET #c = #c - :one, #ttl = :ttl',
            ConditionExpression='attribute_exists(#c) AND #c > :z',
            ExpressionAttributeNames={'#c': 'count', '#ttl': 'ttl'},
            ExpressionAttributeValues={':one': 1, ':z': 0, ':ttl': now + 86_400})
    except ClientError as error:
        if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise


def tokens_today(sub: str, now: Optional[int] = None) -> int:
    now = now_s() if now is None else int(now)
    item = table().get_item(Key=_quota_key(sub, f'DAY#{_day(now)}')).get('Item') or {}
    return int(plain(item.get('tokens', 0)))


def add_tokens(sub: str, amount: int, now: Optional[int] = None) -> int:
    """Add to today's token counter (never refused: the check happens before
    the next Turn starts); returns the new total."""
    now = now_s() if now is None else int(now)
    response = table().update_item(
        Key=_quota_key(sub, f'DAY#{_day(now)}'),
        UpdateExpression='SET tokens = if_not_exists(tokens, :z) + :a, #ttl = :ttl',
        ExpressionAttributeNames={'#ttl': 'ttl'},
        ExpressionAttributeValues={':a': int(amount), ':z': 0, ':ttl': now + 2 * 86_400},
        ReturnValues='ALL_NEW')
    return int(plain(response['Attributes'].get('tokens', 0)))


def day_resets_at(now: Optional[int] = None) -> str:
    return _next_day(now_s() if now is None else int(now))
