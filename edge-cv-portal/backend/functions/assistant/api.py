"""The assistant REST API (design §6, §9; Requirements 1.5, 1.6, 3.1-3.3, 6.1,
6.6, 9.1-9.2, 10.2-10.3, 10.6, 12).

Routes (every one behind the stack's Cognito authorizer):

    GET    /assistant/state
    GET    /assistant/settings                  PUT /assistant/settings
    GET    /assistant/conversations             POST /assistant/conversations
    GET    /assistant/conversations/{cid}       DELETE /assistant/conversations/{cid}
    POST   /assistant/conversations/{cid}/turns
    GET    /assistant/conversations/{cid}/turns/{tid}
    POST   /assistant/conversations/{cid}/turns/{tid}/confirmation
    POST   /assistant/conversations/{cid}/turns/{tid}/stop

The identity is the gateway's verified claims (`get_user_from_event`); the
client never names a tool (10.2) and never supplies identity (6.6). This
Lambda calls no model and no catalog tool: Turns run in the worker.
"""
import json
import logging
import os
import re
from typing import Any, Dict, Optional

import boto3

from assistant import ACTIONS_DELIVERED, __version__
from assistant import errors, limits as limits_module, schemas, settings, store
from assistant.errors import AssistantError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CLIENT_LABEL = re.compile(r'^[A-Za-z0-9._-]{1,40}$')
CLIENT_LABEL_PREFIX = os.environ.get('ASSISTANT_CLIENT_LABEL_PREFIX', 'dda-portal-frontend')
_LAMBDA = None


def _lambda():
    global _LAMBDA
    if _LAMBDA is None:
        _LAMBDA = boto3.client('lambda', region_name=os.environ.get('AWS_REGION'))
    return _LAMBDA


def reset_clients() -> None:
    global _LAMBDA
    _LAMBDA = None


# ----------------------------------------------------------------- helpers

def _response(status: int, body: Any) -> Dict[str, Any]:
    import shared_utils  # the SharedLayer; imported where it is needed

    headers = {'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Portal-Build'}
    if status == 204:
        response = shared_utils.create_response(204, '', headers)
        response['body'] = ''
        return response
    return shared_utils.create_response(status, body, headers)


def _body(event: Dict[str, Any]) -> Dict[str, Any]:
    raw = event.get('body')
    if raw in (None, ''):
        return {}
    if len(raw) > 65_536:
        raise errors.invalid_input('the request body is too large', 'body')
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise errors.invalid_input('the request body is not valid JSON', 'body') from error
    if not isinstance(parsed, dict):
        raise errors.invalid_input('the request body must be an object', 'body')
    return parsed


def _query(event: Dict[str, Any]) -> Dict[str, str]:
    return dict(event.get('queryStringParameters') or {})


def _params(event: Dict[str, Any]) -> Dict[str, str]:
    return dict(event.get('pathParameters') or {})


def _header(event: Dict[str, Any], name: str) -> Optional[str]:
    for key, value in (event.get('headers') or {}).items():
        if key.lower() == name.lower():
            return value
    return None


def _client_label(event: Dict[str, Any]) -> str:
    build = (_header(event, 'X-Portal-Build') or 'unknown').strip()
    if not CLIENT_LABEL.match(build):
        build = 'unknown'
    return f'{CLIENT_LABEL_PREFIX}/{build}'


def _identity(event: Dict[str, Any]) -> Dict[str, Optional[str]]:
    identity = (event.get('requestContext') or {}).get('identity') or {}
    return {'source_ip': identity.get('sourceIp'), 'user_agent': identity.get('userAgent')}


def _claims(event: Dict[str, Any]) -> Dict[str, Any]:
    claims = ((event.get('requestContext') or {}).get('authorizer') or {}).get('claims') or {}
    return dict(claims)


def _ulid(value: str, what: str) -> str:
    if not re.match(schemas.ULID_PATTERN, value or ''):
        raise errors.not_found(what)
    return value


def _is_portal_admin(user: Dict[str, Any]) -> bool:
    import shared_utils

    try:
        return shared_utils.rbac_manager.has_permission(
            user['user_id'], 'global', shared_utils.Permission.SYSTEM_ADMIN, user_info=user)
    except shared_utils.RegistryUnavailable as error:
        raise errors.registry_unavailable() from error


def _is_labeler_only(user: Dict[str, Any]) -> bool:
    """A principal whose effective global role is DataLabeler (Requirement 1.6,
    owner decision 1), resolved as the web UI's redirect resolves it."""
    import shared_utils

    try:
        role = shared_utils.rbac_manager.get_user_role(user['user_id'], 'global', user_info=user)
    except shared_utils.RegistryUnavailable:
        role = None
    value = role.value if role is not None else user.get('role')
    return value == 'DataLabeler'


def _require_admin(user: Dict[str, Any], event: Dict[str, Any]) -> None:
    import shared_utils

    if _is_portal_admin(user):
        return
    shared_utils.log_audit_event(
        user_id=user['user_id'], action='unauthorized_access', resource_type='setting',
        resource_id='assistant_configuration', result='denied',
        details={'required_permissions': [shared_utils.Permission.SYSTEM_ADMIN.value],
                 'method': event.get('httpMethod'), 'path': event.get('path')},
        event=event)
    raise errors.forbidden(shared_utils.Permission.SYSTEM_ADMIN.value)


def _ensure_enabled(user: Dict[str, Any]) -> Dict[str, Any]:
    config = settings.read_configuration(cache_seconds=0)
    if not config.get('enabled', True):
        raise errors.assistant_disabled()
    if _is_labeler_only(user):
        raise errors.role_excluded()
    return config


def _owned(cid: str, sub: str) -> Dict[str, Any]:
    meta = store.get_meta(_ulid(cid, 'conversation'))
    if meta is None or meta.get('sub') != sub:
        raise errors.not_found('conversation')
    return meta


def _public_turn(turn: Dict[str, Any], full: bool = True) -> Dict[str, Any]:
    public = {'turn_id': turn['turn_id'], 'conversation_id': turn['conversation_id'],
              'status': turn['status'], 'version': int(turn.get('version') or 0)}
    if full:
        public.update({
            'text': turn.get('text') or '', 'events': turn.get('events') or [],
            'confirmation': turn.get('confirmation'), 'failure': turn.get('failure'),
            'usage': turn.get('usage'), 'created_at': turn.get('created_at'),
            'finished_at': turn.get('finished_at'), 'prompt': turn.get('prompt'),
        })
    return public


def _reap(turn: Dict[str, Any], limits: Dict[str, int]) -> Dict[str, Any]:
    """Lazy reap (design §8.4): a running Turn without progress for
    `worker_lost_seconds` failed; an awaiting Turn whose binding expired stopped."""
    now_ms = store.now_ms()
    status = turn.get('status')
    if status in ('queued', 'running') and \
            now_ms - int(turn.get('updated_at') or now_ms) > limits['worker_lost_seconds'] * 1000:
        turn = store.update_turn(turn['conversation_id'], turn['turn_id'], status='failed',
                                 failure={'code': 'worker_lost',
                                          'message': 'the assistant did not finish; try again'},
                                 finished_at=now_ms)
        _release(turn)
    elif status == 'awaiting_confirmation':
        confirmation = turn.get('confirmation') or {}
        if int(confirmation.get('expires_at') or 0) * 1000 < now_ms:
            turn = store.append_progress(
                turn['conversation_id'], turn['turn_id'],
                events=[{'seq': len(turn.get('events') or []) + 1, 'ts': now_ms,
                         'type': 'confirmation_expired', 'text_offset': len(turn.get('text') or '')}],
                status='stopped', confirmation=None, finished_at=now_ms)
            _release(turn)
    return turn


def _release(turn: Dict[str, Any]) -> None:
    config = settings.read_configuration(cache_seconds=30)
    store.touch_conversation(turn['sub'], turn['conversation_id'],
                             retention_days=int(config.get('retention_days') or 30),
                             live_turn_id=None)
    store.release_live(turn['sub'])


def _invoke_worker(payload: Dict[str, Any]) -> None:
    _lambda().invoke(FunctionName=os.environ['ASSISTANT_WORKER_FUNCTION'],
                     InvocationType='Event', Payload=json.dumps(payload).encode('utf-8'))


# ------------------------------------------------------------------ routes

def handle_state(event, user, params, body):
    config = settings.read_configuration(cache_seconds=30)
    enabled = bool(config.get('enabled', True))
    reason = None
    if not enabled:
        reason = 'disabled_by_admin'
    elif _is_labeler_only(user):
        enabled, reason = False, 'role_excluded'
    result = {'enabled': enabled, 'actions_enabled': settings.actions_enabled(config),
              'version': __version__}
    if reason:
        result['reason'] = reason
    return _response(200, result)


def _settings_view(config: Dict[str, Any]) -> Dict[str, Any]:
    import bedrock_common

    bedrock = bedrock_common.get_bedrock_configuration()
    capability = settings.model_capability(bedrock['model_id'])
    return {
        'enabled': bool(config.get('enabled', True)),
        'actions_enabled': bool(config.get('actions_enabled', True)),
        'actions_delivered': ACTIONS_DELIVERED,
        'retention_days': int(config.get('retention_days') or 30),
        'guardrail_id': config.get('guardrail_id'),
        'guardrail_version': config.get('guardrail_version'),
        'model': {'model_id': bedrock['model_id'], 'region': bedrock['region'],
                  'tool_use_supported': capability['tool_use_supported'],
                  'checked_at': capability['checked_at']},
        'limits': limits_module.limits(),
        'updated_at': config.get('updated_at'), 'updated_by': config.get('updated_by'),
        'version': __version__,
    }


def handle_settings_get(event, user, params, body):
    _require_admin(user, event)
    return _response(200, _settings_view(settings.read_configuration(0)))


def handle_settings_put(event, user, params, body):
    _require_admin(user, event)
    changes = schemas.validate(schemas.SETTINGS_PUT, body)
    if 'actions_enabled' in changes and not ACTIONS_DELIVERED:
        raise errors.actions_not_delivered()
    config = settings.write_configuration(changes, user, event)
    return _response(200, _settings_view(config))


def handle_conversations_list(event, user, params, body):
    _ensure_enabled(user)
    query = schemas.validate(schemas.LIST_QUERY, _query(event), 'query')
    limit = int(query.get('limit') or 20)
    items = store.list_conversations(user['user_id'], limit)
    return _response(200, {'items': [
        {'conversation_id': i['conversation_id'], 'title': i.get('title') or '',
         'created_at': i.get('created_at'), 'updated_at': i.get('updated_at'),
         'turn_count': i.get('turn_count', 0), 'live_turn_id': i.get('live_turn_id')}
        for i in items]})


def handle_conversation_create(event, user, params, body):
    config = _ensure_enabled(user)
    data = schemas.validate(schemas.CONVERSATION_CREATE, body)
    meta = store.create_conversation(user['user_id'], data.get('title') or '',
                                     int(config.get('retention_days') or 30))
    return _response(201, {'conversation_id': meta['conversation_id']})


def handle_conversation_get(event, user, params, body):
    _ensure_enabled(user)
    meta = _owned(params.get('cid', ''), user['user_id'])
    messages = []
    for item in store.list_messages(meta['conversation_id']):
        blocks = []
        display = item.get('display')
        if item['role'] == 'assistant' and display:
            blocks.append({'type': 'assistant', 'text': display.get('text') or '',
                           'events': display.get('events') or [], 'status': display.get('status'),
                           'failure': display.get('failure')})
        else:
            text = ' '.join(b.get('text', '') for b in item.get('content') or [] if 'text' in b)
            blocks.append({'type': item['role'], 'text': text})
        messages.append({'seq': item['seq'], 'role': item['role'], 'turn_id': item.get('turn_id'),
                         'created_at': item.get('created_at'), 'blocks': blocks})
    return _response(200, {'conversation_id': meta['conversation_id'], 'title': meta.get('title'),
                           'messages': messages, 'live_turn_id': meta.get('live_turn_id'),
                           'turn_count': meta.get('turn_count', 0)})


def handle_conversation_delete(event, user, params, body):
    _ensure_enabled(user)
    meta = _owned(params.get('cid', ''), user['user_id'])
    live = meta.get('live_turn_id')
    if live:
        turn = store.get_turn(meta['conversation_id'], live)
        if turn and _reap(turn, limits_module.limits()).get('status') in store.LIVE_STATUSES:
            raise errors.turn_in_progress(live)
    store.delete_conversation(user['user_id'], meta['conversation_id'])
    return _response(204, '')


def handle_turn_start(event, user, params, body):
    config = _ensure_enabled(user)
    limits = limits_module.limits()
    meta = _owned(params.get('cid', ''), user['user_id'])
    data = schemas.validate(schemas.TURN_START, body)
    schemas.validate_prompt(data['prompt'])
    live = meta.get('live_turn_id')
    if live:
        turn = store.get_turn(meta['conversation_id'], live)
        if turn and _reap(turn, limits).get('status') in store.LIVE_STATUSES:
            raise errors.turn_in_progress(live)
    if int(meta.get('turn_count') or 0) >= limits['conversation_turns'] \
            or int(meta.get('size_bytes') or 0) >= limits['conversation_bytes']:
        raise errors.conversation_full()
    if store.tokens_today(user['user_id']) >= limits['tokens_per_day']:
        raise errors.quota_exceeded('tokens_per_day', store.day_resets_at())
    refused = store.reserve_turn(user['user_id'], limits)
    if refused:
        raise errors.quota_exceeded(*refused)
    claims = _claims(event)
    try:
        exp = int(claims.get('exp') or 0)
    except (TypeError, ValueError):
        exp = 0
    identity = _identity(event)
    turn = store.create_turn(
        sub=user['user_id'], cid=meta['conversation_id'], prompt=data['prompt'],
        page_context=data['page_context'], claims=claims, exp=exp,
        client_label=_client_label(event), source_ip=identity['source_ip'],
        user_agent=identity['user_agent'], ttl_days=limits['turn_ttl_days'])
    store.touch_conversation(user['user_id'], meta['conversation_id'],
                             retention_days=int(config.get('retention_days') or 30),
                             live_turn_id=turn['turn_id'])
    try:
        _invoke_worker({'conversation_id': meta['conversation_id'], 'turn_id': turn['turn_id']})
    except Exception as error:  # noqa: BLE001 - the Turn must not stay live
        logger.error('worker invocation failed: %s', type(error).__name__)
        store.update_turn(meta['conversation_id'], turn['turn_id'], status='failed',
                          failure={'code': 'worker_unavailable',
                                   'message': 'the assistant could not start; try again'},
                          finished_at=store.now_ms())
        _release(turn)
        raise AssistantError(503, 'worker_unavailable', 'the assistant could not start; try again')
    return _response(202, {'turn_id': turn['turn_id']})


def handle_turn_get(event, user, params, body):
    _ensure_enabled(user)
    meta = _owned(params.get('cid', ''), user['user_id'])
    turn = store.get_turn(meta['conversation_id'], _ulid(params.get('tid', ''), 'turn'))
    if turn is None or turn.get('sub') != user['user_id']:
        raise errors.not_found('turn')
    turn = _reap(turn, limits_module.limits())
    query = schemas.validate(schemas.POLL_QUERY, _query(event), 'query')
    after = int(query['after']) if query.get('after') else None
    unchanged = after is not None and after >= int(turn.get('version') or 0) \
        and turn.get('status') in store.LIVE_STATUSES
    return _response(200, _public_turn(turn, full=not unchanged))


def handle_turn_confirmation(event, user, params, body):
    _ensure_enabled(user)
    meta = _owned(params.get('cid', ''), user['user_id'])
    turn = store.get_turn(meta['conversation_id'], _ulid(params.get('tid', ''), 'turn'))
    if turn is None or turn.get('sub') != user['user_id']:
        raise errors.not_found('turn')
    data = schemas.validate(schemas.CONFIRMATION, body)
    turn = _reap(turn, limits_module.limits())
    if turn.get('status') != 'awaiting_confirmation':
        raise errors.not_awaiting_confirmation()
    confirmation = turn.get('confirmation') or {}
    if confirmation.get('binding_id') != data['binding_id']:
        raise errors.binding_invalid()
    try:
        store.consume_binding(user['user_id'], data['binding_id'], data['decision'])
    except store.BindingUnusable as unusable:
        if unusable.reason == 'used':
            raise errors.binding_used() from unusable
        if unusable.reason == 'expired':
            raise errors.binding_expired() from unusable
        raise errors.binding_invalid() from unusable
    store.update_turn(meta['conversation_id'], turn['turn_id'], status='running')
    _invoke_worker({'conversation_id': meta['conversation_id'], 'turn_id': turn['turn_id'],
                    'resume': {'binding_id': data['binding_id']}})
    return _response(202, {'turn_id': turn['turn_id'], 'decision': data['decision']})


def handle_turn_stop(event, user, params, body):
    _ensure_enabled(user)
    meta = _owned(params.get('cid', ''), user['user_id'])
    turn = store.get_turn(meta['conversation_id'], _ulid(params.get('tid', ''), 'turn'))
    if turn is None or turn.get('sub') != user['user_id']:
        raise errors.not_found('turn')
    if turn.get('status') in store.LIVE_STATUSES:
        turn = store.request_stop(meta['conversation_id'], turn['turn_id'])
        if turn.get('status') == 'awaiting_confirmation':
            # nothing is running: close the Turn here
            now_ms = store.now_ms()
            turn = store.append_progress(
                meta['conversation_id'], turn['turn_id'],
                events=[{'seq': len(turn.get('events') or []) + 1, 'ts': now_ms, 'type': 'stopped',
                         'text_offset': len(turn.get('text') or '')}],
                status='stopped', confirmation=None, finished_at=now_ms)
            _release(turn)
    return _response(202, {'turn_id': turn['turn_id'], 'status': turn['status']})


ROUTES = {
    ('GET', '/assistant/state'): handle_state,
    ('GET', '/assistant/settings'): handle_settings_get,
    ('PUT', '/assistant/settings'): handle_settings_put,
    ('GET', '/assistant/conversations'): handle_conversations_list,
    ('POST', '/assistant/conversations'): handle_conversation_create,
    ('GET', '/assistant/conversations/{cid}'): handle_conversation_get,
    ('DELETE', '/assistant/conversations/{cid}'): handle_conversation_delete,
    ('POST', '/assistant/conversations/{cid}/turns'): handle_turn_start,
    ('GET', '/assistant/conversations/{cid}/turns/{tid}'): handle_turn_get,
    ('POST', '/assistant/conversations/{cid}/turns/{tid}/confirmation'): handle_turn_confirmation,
    ('POST', '/assistant/conversations/{cid}/turns/{tid}/stop'): handle_turn_stop,
}


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    import shared_utils  # the SharedLayer; imported where it is needed

    method = (event.get('httpMethod') or '').upper()
    resource = event.get('resource') or ''
    if method == 'OPTIONS':
        return _response(200, {})
    route = ROUTES.get((method, resource))
    if route is None:
        return _response(404, {'error': {'code': 'not_found', 'message': 'no such route'}})
    try:
        user = shared_utils.get_user_from_event(event)
        if user.get('user_id') in (None, '', 'unknown'):
            raise AssistantError(401, 'not_authenticated', 'no verified identity')
        return route(event, user, _params(event), _body(event) if method in ('POST', 'PUT') else {})
    except AssistantError as error:
        return _response(error.status, error.body())
    except Exception as error:  # noqa: BLE001 - never leak a traceback
        logger.error('assistant api failure on %s %s: %s', method, resource, type(error).__name__,
                     exc_info=True)
        return _response(500, {'error': {'code': 'internal_error', 'message': 'internal error'}})
