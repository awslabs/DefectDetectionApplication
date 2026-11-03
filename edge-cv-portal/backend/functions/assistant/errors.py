"""Error codes of the assistant API (design "Error handling")."""
from typing import Any, Dict, Optional


class AssistantError(Exception):
    """An error the API answers with the Portal envelope
    `{error: {code, message, details}}`."""

    def __init__(self, status: int, code: str, message: str,
                 details: Optional[Dict[str, Any]] = None):
        super().__init__(f'{code}: {message}')
        self.status = status
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def body(self) -> Dict[str, Any]:
        error: Dict[str, Any] = {'code': self.code, 'message': self.message}
        if self.details:
            error['details'] = self.details
        return {'error': error}


def invalid_input(message: str, field: Optional[str] = None) -> AssistantError:
    details = {'field': field} if field else None
    return AssistantError(400, 'invalid_input', message, details)


def not_found(what: str = 'resource') -> AssistantError:
    return AssistantError(404, 'not_found', f'{what} not found')


def assistant_disabled() -> AssistantError:
    return AssistantError(403, 'assistant_disabled',
                          'the assistant is disabled by an administrator')


def role_excluded() -> AssistantError:
    return AssistantError(403, 'role_excluded',
                          'the assistant is not available for this role')


def forbidden(permission: str) -> AssistantError:
    return AssistantError(403, 'forbidden', f'{permission} is required',
                          {'required_permissions': [permission]})


def quota_exceeded(limit: str, resets_at: Optional[str]) -> AssistantError:
    return AssistantError(429, 'quota_exceeded', f'the {limit} limit is reached',
                          {'limit': limit, 'resets_at': resets_at})


def turn_in_progress(turn_id: str) -> AssistantError:
    return AssistantError(409, 'turn_in_progress',
                          'a turn is already in progress in this conversation',
                          {'turn_id': turn_id})


def conversation_full() -> AssistantError:
    return AssistantError(409, 'conversation_full',
                          'this conversation has reached its size limit; start a new one')


def binding_invalid() -> AssistantError:
    return AssistantError(404, 'binding_invalid', 'no such pending confirmation')


def binding_used() -> AssistantError:
    return AssistantError(409, 'binding_used', 'this confirmation was already answered')


def binding_expired() -> AssistantError:
    return AssistantError(410, 'binding_expired', 'this confirmation has expired')


def not_awaiting_confirmation() -> AssistantError:
    return AssistantError(409, 'not_awaiting_confirmation',
                          'this turn is not waiting for a confirmation')


def actions_not_delivered() -> AssistantError:
    return AssistantError(400, 'actions_not_delivered',
                          'actions are not available in this release')


def registry_unavailable() -> AssistantError:
    return AssistantError(503, 'authorization_unavailable',
                          'the Portal identity registry is unavailable; retry')
