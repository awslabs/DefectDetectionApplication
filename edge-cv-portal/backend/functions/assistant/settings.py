"""The assistant's settings record in `SETTINGS_TABLE` (design §9, D8;
Requirement 12).

`assistant_configuration` = {enabled, actions_enabled, retention_days,
guardrail_id, guardrail_version, updated_at, updated_by}; absent fields take
the defaults. `assistant_model_capability` records whether the configured
model accepted tool definitions (design D12).
"""
import logging
import os
import time
from typing import Any, Dict, Optional

import boto3

from assistant import (ACTIONS_DELIVERED, CONFIGURATION_SETTING_KEY,
                       MODEL_CAPABILITY_SETTING_KEY)

logger = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    'enabled': True,
    'actions_enabled': True,
    'retention_days': 30,
    'guardrail_id': None,
    'guardrail_version': None,
}
FIELDS = tuple(DEFAULTS)

_cache: Dict[str, Any] = {'value': None, 'at': 0.0}


def _table():
    return boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION')).Table(
        os.environ['SETTINGS_TABLE'])


def _coerce(record: Dict[str, Any]) -> Dict[str, Any]:
    config = dict(DEFAULTS)
    for key in FIELDS:
        if key not in record or record[key] is None and key in ('enabled', 'actions_enabled',
                                                                'retention_days'):
            continue
        value = record[key]
        if key in ('enabled', 'actions_enabled'):
            config[key] = bool(value) if not isinstance(value, str) \
                else value.strip().lower() == 'true'
        elif key == 'retention_days':
            try:
                days = int(value)
            except (TypeError, ValueError):
                continue
            config[key] = min(365, max(1, days))
        else:
            config[key] = str(value) if value is not None else None
    for meta in ('updated_at', 'updated_by'):
        if meta in record:
            config[meta] = record[meta]
    return config


def read_configuration(cache_seconds: float = 0.0) -> Dict[str, Any]:
    """The effective configuration. `cache_seconds` > 0 reuses an in-process
    read that recent (the API's state route); the worker passes 0."""
    now = time.monotonic()
    if cache_seconds > 0 and _cache['value'] is not None \
            and now - _cache['at'] < cache_seconds:
        return dict(_cache['value'])
    try:
        item = _table().get_item(Key={'setting_key': CONFIGURATION_SETTING_KEY}).get('Item') or {}
    except Exception as error:  # noqa: BLE001 - an unreadable record means the defaults
        logger.error('assistant configuration unreadable: %s', type(error).__name__)
        item = {}
    config = _coerce(item)
    _cache['value'] = dict(config)
    _cache['at'] = now
    return config


def clear_cache() -> None:
    _cache['value'] = None
    _cache['at'] = 0.0


def actions_enabled(config: Dict[str, Any]) -> bool:
    """The effective Actions_Switch: delivered and on."""
    return bool(ACTIONS_DELIVERED and config.get('actions_enabled'))


def write_configuration(changes: Dict[str, Any], user: Dict[str, Any],
                        event: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Apply `changes` (already schema-validated) and write one audit entry per
    changed field (Requirement 12.7). Returns the new configuration."""
    import shared_utils  # the SharedLayer; imported where it is needed

    current = read_configuration(0)
    updated = dict(current)
    changed = []
    for key in FIELDS:
        if key in changes and changes[key] != current.get(key):
            updated[key] = changes[key]
            changed.append((key, current.get(key), changes[key]))
    if not changed:
        return current
    ms = int(time.time() * 1000)
    updated['updated_at'] = ms
    updated['updated_by'] = user.get('username') or user.get('user_id')
    item = {'setting_key': CONFIGURATION_SETTING_KEY}
    item.update({k: updated[k] for k in FIELDS})
    item['updated_at'] = ms
    item['updated_by'] = updated['updated_by']
    _table().put_item(Item=item)
    clear_cache()
    for key, previous, new in changed:
        shared_utils.log_audit_event(
            user_id=user['user_id'], action='assistant_settings_changed',
            resource_type='setting', resource_id=CONFIGURATION_SETTING_KEY, result='success',
            details={'setting': key, 'previous': previous, 'new': new}, event=event)
    return updated


# --------------------------------------------------------- model capability

def model_capability(model_id: str) -> Dict[str, Any]:
    """{model_id, tool_use_supported: True|False|None, checked_at}."""
    try:
        item = _table().get_item(Key={'setting_key': MODEL_CAPABILITY_SETTING_KEY}).get('Item')
    except Exception as error:  # noqa: BLE001
        logger.error('model capability unreadable: %s', type(error).__name__)
        item = None
    if not item or item.get('model_id') != model_id:
        return {'model_id': model_id, 'tool_use_supported': None, 'checked_at': None}
    supported = item.get('tool_use_supported')
    return {'model_id': model_id, 'tool_use_supported': None if supported is None else bool(supported),
            'checked_at': int(item['checked_at']) if item.get('checked_at') is not None else None}


def record_model_capability(model_id: str, tool_use_supported: bool) -> None:
    try:
        _table().put_item(Item={'setting_key': MODEL_CAPABILITY_SETTING_KEY, 'model_id': model_id,
                                'tool_use_supported': bool(tool_use_supported),
                                'checked_at': int(time.time() * 1000)})
    except Exception as error:  # noqa: BLE001 - capability is advisory
        logger.error('model capability not recorded: %s', type(error).__name__)
