"""Structured log lines and CloudWatch EMF metrics of the assistant (design §4
`telemetry.py`, Requirements 9.5, 13.2).

Every record is built from a fixed key list: identifiers, categories,
sizes, durations and token counts. No prompt, response, argument or tool
result text ever enters a record; `test_property_log_hygiene.py` pins that.
"""
import json
import logging
import time
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

NAMESPACE = 'DDA/Assistant'

TURN_FIELDS = ('event', 'sub', 'conversation_id', 'turn_id', 'status', 'failure_code',
               'model_calls', 'tool_calls', 'pipeline_calls', 'input_tokens', 'output_tokens',
               'cache_read_tokens', 'cache_write_tokens', 'duration_ms', 'text_chars',
               'confirmations', 'client', 'version')
SETTINGS_FIELDS = ('event', 'sub', 'setting', 'previous', 'new')


def _emit(record: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(json.dumps(record, separators=(',', ':'), ensure_ascii=True, default=str))
    return record


def log_turn(*, sub: str, conversation_id: str, turn_id: str, status: str,
             failure_code: Optional[str], model_calls: int, tool_calls: int,
             pipeline_calls: int, usage: Dict[str, int], duration_ms: int, text_chars: int,
             confirmations: int, client: Optional[str], version: str) -> Dict[str, Any]:
    record = {
        'event': 'assistant_turn', 'sub': sub, 'conversation_id': conversation_id,
        'turn_id': turn_id, 'status': status, 'failure_code': failure_code,
        'model_calls': int(model_calls), 'tool_calls': int(tool_calls),
        'pipeline_calls': int(pipeline_calls),
        'input_tokens': int(usage.get('input', 0)), 'output_tokens': int(usage.get('output', 0)),
        'cache_read_tokens': int(usage.get('cache_read', 0)),
        'cache_write_tokens': int(usage.get('cache_write', 0)),
        'duration_ms': int(duration_ms), 'text_chars': int(text_chars),
        'confirmations': int(confirmations), 'client': client, 'version': version,
    }
    assert tuple(record) == TURN_FIELDS
    return _emit(record)


def log_settings_change(*, sub: str, setting: str, previous: Any, new: Any) -> Dict[str, Any]:
    record = {'event': 'assistant_settings', 'sub': sub, 'setting': setting,
              'previous': previous, 'new': new}
    assert tuple(record) == SETTINGS_FIELDS
    return _emit(record)


def metrics(values: Dict[str, float], dimensions: Optional[Dict[str, str]] = None,
            units: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """One CloudWatch Embedded Metric Format line. `values` maps a metric
    name to its value; dimensions are strings with a bounded vocabulary
    (status, category), never identifiers."""
    dimensions = dict(dimensions or {})
    units = dict(units or {})
    record: Dict[str, Any] = {
        '_aws': {
            'Timestamp': int(time.time() * 1000),
            'CloudWatchMetrics': [{
                'Namespace': NAMESPACE,
                'Dimensions': [sorted(dimensions)] if dimensions else [[]],
                'Metrics': [{'Name': name, 'Unit': units.get(name, 'Count')}
                            for name in sorted(values)],
            }],
        },
    }
    record.update(dimensions)
    record.update({name: float(value) for name, value in values.items()})
    print(json.dumps(record, separators=(',', ':')))  # EMF is read from stdout
    return record


def metric_names(records: Iterable[Dict[str, Any]]) -> set:
    names = set()
    for record in records:
        for group in record.get('_aws', {}).get('CloudWatchMetrics', []):
            names.update(m['Name'] for m in group['Metrics'])
    return names
