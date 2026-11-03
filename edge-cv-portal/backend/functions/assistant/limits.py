"""Limits and budgets (design D8, Requirements 7.5, 10.3, 9.6).

Defaults below; a deployment overrides any of them through the environment
variable `ASSISTANT_LIMITS_JSON` (a JSON object with the same keys and
positive integer values). Unknown keys and non-positive values are ignored
with a log line, never a failure.
"""
import json
import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)

DEFAULTS: Dict[str, int] = {
    # per principal
    'concurrent_turns': 2,
    'turns_per_hour': 60,
    'tokens_per_day': 1_500_000,
    # per turn (Turn_Budget)
    'model_calls_per_turn': 10,
    'tool_calls_per_turn': 10,
    'pipeline_calls_per_turn': 24,
    'output_tokens_per_turn': 6_000,
    'turn_seconds': 240,
    # per conversation
    'conversation_turns': 60,
    'conversation_bytes': 400_000,
    'window_tokens': 24_000,
    # bindings and liveness
    'binding_seconds': 600,
    'worker_lost_seconds': 120,
    'turn_ttl_days': 7,
}

ENV_VAR = 'ASSISTANT_LIMITS_JSON'


def limits() -> Dict[str, int]:
    """The effective limits: DEFAULTS overridden by the environment."""
    values = dict(DEFAULTS)
    raw = os.environ.get(ENV_VAR, '').strip()
    if not raw:
        return values
    try:
        overrides = json.loads(raw)
    except ValueError:
        logger.warning('%s is not valid JSON; using the default limits', ENV_VAR)
        return values
    if not isinstance(overrides, dict):
        logger.warning('%s is not a JSON object; using the default limits', ENV_VAR)
        return values
    for key, value in overrides.items():
        if key not in DEFAULTS:
            logger.warning('%s: unknown limit %r ignored', ENV_VAR, key)
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            logger.warning('%s: limit %r must be a positive integer; default kept', ENV_VAR, key)
            continue
        values[key] = value
    return values
