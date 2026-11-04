"""Request schemas of the assistant API (design §4 `schemas.py`, Requirement
10.6). Every client message is validated against one of these before any
store, model or tool call."""
from typing import Any, Dict, Optional

import jsonschema

from assistant.errors import invalid_input

ULID_PATTERN = '^[0-9A-HJKMNP-TV-Z]{26}$'
PROMPT_MAX_CHARS = 4000

ULID = {'type': 'string', 'pattern': ULID_PATTERN}

PAGE_CONTEXT = {
    'type': 'object',
    'properties': {
        'route': {'type': 'string', 'minLength': 1, 'maxLength': 512, 'pattern': '^/'},
        'params': {
            'type': 'object',
            'maxProperties': 12,
            'additionalProperties': {'type': 'string', 'maxLength': 256},
        },
        'usecase_id': {'type': ['string', 'null'], 'maxLength': 128},
    },
    'required': ['route'],
    'additionalProperties': False,
}

TURN_START = {
    'type': 'object',
    'properties': {
        'prompt': {'type': 'string', 'minLength': 1, 'maxLength': PROMPT_MAX_CHARS},
        'page_context': PAGE_CONTEXT,
    },
    'required': ['prompt', 'page_context'],
    'additionalProperties': False,
}

CONVERSATION_CREATE = {
    'type': 'object',
    'properties': {'title': {'type': 'string', 'maxLength': 80}},
    'additionalProperties': False,
}

CONFIRMATION = {
    'type': 'object',
    'properties': {
        'binding_id': ULID,
        'decision': {'type': 'string', 'enum': ['confirm', 'reject']},
    },
    'required': ['binding_id', 'decision'],
    'additionalProperties': False,
}

SETTINGS_PUT = {
    'type': 'object',
    'properties': {
        'enabled': {'type': 'boolean'},
        'actions_enabled': {'type': 'boolean'},
        'retention_days': {'type': 'integer', 'minimum': 1, 'maximum': 365},
        'guardrail_id': {'type': ['string', 'null'], 'pattern': '^[a-z0-9]{1,64}$'},
        'guardrail_version': {'type': ['string', 'null'], 'pattern': '^(DRAFT|[0-9]{1,8})$'},
    },
    'minProperties': 1,
    'additionalProperties': False,
}

LIST_QUERY = {
    'type': 'object',
    'properties': {'limit': {'type': 'string', 'pattern': '^[0-9]{1,2}$'}},
    'additionalProperties': True,
}

POLL_QUERY = {
    'type': 'object',
    'properties': {'after': {'type': 'string', 'pattern': '^[0-9]{1,12}$'}},
    'additionalProperties': True,
}


def validate(schema: Dict[str, Any], value: Any, what: str = 'body') -> Any:
    """Return `value` or raise `invalid_input` naming the first failing field
    (never its value)."""
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(value), key=lambda e: (list(e.path), e.validator))
    if not errors:
        return value
    error = errors[0]
    base = '.'.join(str(p) for p in error.path)
    instance = error.instance if isinstance(error.instance, dict) else {}
    if error.validator == 'required':
        missing = [r for r in error.validator_value if r not in instance]
        leaf: Optional[str] = missing[0] if missing else None
    elif error.validator == 'additionalProperties':
        known = (error.schema or {}).get('properties', {}) if isinstance(error.schema, dict) else {}
        extra = [k for k in instance if k not in known]
        leaf = extra[0] if extra else None
    else:
        leaf = None
    if base and leaf:
        field = f'{base}.{leaf}'
    else:
        field = base or leaf or what
    if error.validator == 'required':
        raise invalid_input(f'{field} is required', field)
    if error.validator == 'additionalProperties':
        raise invalid_input(f'{field} is not accepted', field)
    raise invalid_input(f'{field} fails {error.validator}', field)


def validate_prompt(prompt: str) -> str:
    """A prompt must carry at least one non-whitespace character
    (Requirement 3.1)."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise invalid_input('a prompt is required', 'prompt')
    return prompt
