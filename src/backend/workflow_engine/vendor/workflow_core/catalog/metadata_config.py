"""Shared validity rules for the Metadata_Node configuration parameters.

Pure helpers over the two string parameters of the ``metadata`` node
type (workflow-manager-gaps Requirement 6.1):

- ``mappings``: a JSON array of ``{"path": ..., "key": ...}`` string
  pairs (0..MAX_MAPPINGS entries) mapping a trigger-payload field path
  to an output metadata key.
- ``static_json``: an optional static JSON object of at most
  MAX_STATIC_JSON_CHARS characters attached alongside the mappings.

These helpers are the single source of truth for what a valid
Metadata_Node configuration is: the Workflow_Validator maps the error
classes below to its finding codes, the Workflow_Compiler parses the
parameters through them when emitting the ``metadata`` executor
binding, and the designer mirrors the same rules in TypeScript
(``metadataConfig.ts``) so all three agree on validity.

Both helpers are total: they never raise on any input and instead
report problems through the returned error list. Each error is a dict
``{"code": <error class>, "message": <human-readable detail>}`` where
the code is one of the ``ERROR_*`` constants.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

#: Maximum number of Metadata_Mappings a single node may declare.
MAX_MAPPINGS = 50

#: Maximum length (in characters) of the raw ``static_json`` parameter.
MAX_STATIC_JSON_CHARS = 10240

# Error classes shared by the validator (one finding code per class),
# the compiler, and the TypeScript mirror.
ERROR_MAPPINGS_INVALID = "mappings_invalid"
ERROR_EMPTY_FIELD_PATH = "empty_field_path"
ERROR_EMPTY_KEY = "empty_key"
ERROR_DUPLICATE_KEY = "duplicate_key"
ERROR_TOO_MANY_MAPPINGS = "too_many_mappings"
ERROR_STATIC_JSON_INVALID = "static_json_invalid"


def _error(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message}


def parse_mappings(raw: Any) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Parse and validate the ``mappings`` parameter value.

    Returns ``(mappings, errors)`` where ``mappings`` is the list of
    ``{"path": ..., "key": ...}`` entries with paths and keys trimmed,
    and ``errors`` lists every violated rule:

    - ``ERROR_MAPPINGS_INVALID``: ``raw`` is not a string, is not
      parseable as JSON, or does not parse to an array whose every
      entry is a JSON object carrying string ``path`` and ``key``
      values (the returned list is empty in this case);
    - ``ERROR_EMPTY_FIELD_PATH``: a mapping whose ``path`` is empty or
      whitespace-only after trimming (one error per such mapping);
    - ``ERROR_EMPTY_KEY``: a mapping whose ``key`` is empty or
      whitespace-only after trimming (one error per such mapping);
    - ``ERROR_DUPLICATE_KEY``: the same trimmed output key appears in
      more than one mapping (one error per duplicated key);
    - ``ERROR_TOO_MANY_MAPPINGS``: more than MAX_MAPPINGS entries.

    ``None`` and empty/whitespace-only strings are treated as the
    descriptor default ``"[]"`` (no mappings, no errors). A valid
    configuration is exactly one for which ``errors`` is empty.
    """
    if raw is None:
        return [], []
    if not isinstance(raw, str):
        return [], [_error(ERROR_MAPPINGS_INVALID,
                           "mappings must be a JSON array string")]
    text = raw.strip()
    if not text:
        return [], []

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return [], [_error(ERROR_MAPPINGS_INVALID,
                           "mappings is not parseable as JSON")]
    if not isinstance(parsed, list):
        return [], [_error(ERROR_MAPPINGS_INVALID,
                           "mappings must be a JSON array of "
                           '{"path", "key"} objects')]

    mappings: List[Dict[str, str]] = []
    for index, entry in enumerate(parsed):
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("key"), str)):
            return [], [_error(
                ERROR_MAPPINGS_INVALID,
                "mappings entry %d must be an object with string "
                '"path" and "key" values' % index)]
        mappings.append({"path": entry["path"].strip(),
                         "key": entry["key"].strip()})

    errors: List[Dict[str, str]] = []
    for index, mapping in enumerate(mappings):
        if not mapping["path"]:
            errors.append(_error(
                ERROR_EMPTY_FIELD_PATH,
                "mapping %d has an empty trigger-payload field path" % index))
        if not mapping["key"]:
            errors.append(_error(
                ERROR_EMPTY_KEY,
                "mapping %d has an empty output metadata key" % index))

    seen: Dict[str, int] = {}
    reported = set()
    for mapping in mappings:
        key = mapping["key"]
        if not key:
            continue  # already reported as ERROR_EMPTY_KEY
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1 and key not in reported:
            reported.add(key)
            errors.append(_error(
                ERROR_DUPLICATE_KEY,
                "output metadata key '%s' is used by more than one "
                "mapping" % key))

    if len(mappings) > MAX_MAPPINGS:
        errors.append(_error(
            ERROR_TOO_MANY_MAPPINGS,
            "at most %d mappings are allowed (got %d)"
            % (MAX_MAPPINGS, len(mappings))))

    return mappings, errors


def parse_static_json(raw: Any) -> Tuple[Optional[dict], List[Dict[str, str]]]:
    """Parse and validate the ``static_json`` parameter value.

    Returns ``(static, errors)`` where ``static`` is the parsed JSON
    object, or ``None`` when no static JSON is configured or the value
    is invalid. Errors carry ``ERROR_STATIC_JSON_INVALID`` when the
    value is not a string, is longer than MAX_STATIC_JSON_CHARS
    characters, is not parseable as JSON, or parses to a value that is
    not a JSON object.

    ``None`` and empty/whitespace-only strings mean "no static JSON"
    (the descriptor default ``""``) and produce no errors.
    """
    if raw is None:
        return None, []
    if not isinstance(raw, str):
        return None, [_error(ERROR_STATIC_JSON_INVALID,
                             "static_json must be a JSON object string")]
    if not raw.strip():
        return None, []
    if len(raw) > MAX_STATIC_JSON_CHARS:
        return None, [_error(
            ERROR_STATIC_JSON_INVALID,
            "static_json exceeds %d characters (got %d)"
            % (MAX_STATIC_JSON_CHARS, len(raw)))]
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None, [_error(ERROR_STATIC_JSON_INVALID,
                             "static_json is not parseable as JSON")]
    if not isinstance(parsed, dict):
        return None, [_error(ERROR_STATIC_JSON_INVALID,
                             "static_json must parse to a JSON object")]
    return parsed, []
