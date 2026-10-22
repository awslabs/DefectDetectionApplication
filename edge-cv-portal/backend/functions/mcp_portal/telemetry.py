"""The per-invocation `mcp_tool` log line (design §2 `telemetry.py` row, Data
Models "Portal per-invocation log line", Requirement 6.3).

`log_tool_completion` emits exactly one JSON line per tool completion
(success, error result, denial, availability failure) through the Lambda
logger, with exactly the documented keys: never the arguments, the token or
the email. A D36 refusal is an error result with category `not_found`,
logged with the backend's real 2xx status while the tool result carries 404.
"""
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

EVENT = 'mcp_tool'
OUTCOMES = ('success', 'error', 'denied', 'unavailable')
FIELDS = ('event', 'sub', 'username', 'tool', 'view', 'outcome', 'category',
          'status', 'duration_ms', 'request_id', 'client', 'server_version')


def outcome_for(category: Optional[str]) -> str:
    """`success`, or the outcome class of an error category."""
    if category is None:
        return 'success'
    if category == 'not_authorized':
        return 'denied'
    if category == 'authorization_unavailable':
        return 'unavailable'
    return 'error'


def log_tool_completion(*, principal, tool: str, view: Optional[str],
                        category: Optional[str], status: Optional[int],
                        duration_ms: int, request_id: Optional[str],
                        client: Optional[str], server_version: str) -> dict:
    """Emit the line and return the record that was logged."""
    record = {
        'event': EVENT,
        'sub': principal.sub,
        'username': principal.username,
        'tool': tool,
        'view': view,   # None on the aggregate /mcp endpoint
        'outcome': outcome_for(category),
        'category': category,
        'status': status,
        'duration_ms': int(duration_ms),
        'request_id': request_id,
        'client': client,
        'server_version': server_version,
    }
    logger.info(json.dumps(record, separators=(',', ':'), ensure_ascii=True))
    return record
