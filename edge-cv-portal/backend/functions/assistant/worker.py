"""The Turn worker Lambda (design §8): invoked asynchronously by the API with
`{conversation_id, turn_id}` to run a Turn, or with `resume: {binding_id}`
to continue one after a confirmation. Idempotent: a finished Turn is left
as it is.
"""
import logging
from typing import Any, Dict

from assistant import turn as turn_module

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    cid = str(event.get('conversation_id') or '')
    tid = str(event.get('turn_id') or '')
    resume = event.get('resume') or None
    if not cid or not tid:
        logger.error('worker invoked without conversation_id/turn_id')
        return {'status': 'ignored'}

    def remaining_ms() -> int:
        try:
            return int(context.get_remaining_time_in_millis())
        except Exception:  # noqa: BLE001 - tests pass a bare object
            return 10 ** 9

    try:
        result = turn_module.run(cid, tid, resume=resume, remaining_ms=remaining_ms)
    except LookupError as error:
        logger.error('worker: %s', error)
        return {'status': 'not_found'}
    return {'status': result.get('status'), 'turn_id': tid}
