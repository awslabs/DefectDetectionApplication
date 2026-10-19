"""The ``dda.debug.v1`` envelope and its validation at ingest
(workflow-debug-mode design §4.1, §4.3).

    {"schema": "dda.debug.v1", "sessionId": "<uuid>", "instance": "<uuid>",
     "deviceId": "<thing>", "workflowId": "<uuid>", "workflowVersion": "1",
     "seq": 17, "sentAt": 1792075062610, "records": [ ... 1-200 records ... ]}

``(sessionId, instance, seq)`` is the ingest's dedup key. Each record carries
a ``type`` (``run``, ``log``, ``drop``, ``session``; unknown types are written
as they are) and an integer ``ts``. A message is at most 128 KB on the wire
(IoT's limit), so the ingest never cuts a record. Failures: ``invalid_json``,
``too_large``, ``schema_invalid``.
"""
import base64
import binascii
import json
import re
from typing import Any, Dict, List

__all__ = [
    "SCHEMA",
    "MAX_MESSAGE_BYTES",
    "MAX_RECORDS",
    "KNOWN_RECORD_TYPES",
    "EnvelopeError",
    "decode_payload",
    "validate_envelope",
]

SCHEMA = "dda.debug.v1"
MAX_MESSAGE_BYTES = 128 * 1024
MAX_RECORDS = 200
KNOWN_RECORD_TYPES = ("run", "log", "drop", "session")

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
#: An IoT thing name (``[A-Za-z0-9:_-]+``).
_THING_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
#: A workflow id: a Portal UUID in practice; refused only when it would
#: change a topic's level structure (the same rule as ``topics.py``).
_ID_RE = re.compile(r"^[^/+#\x00\s]{1,128}$")


class EnvelopeError(ValueError):
    """``reason`` is ``invalid_json``, ``too_large`` or ``schema_invalid``;
    ``detail`` names the field for the logs (never a payload value)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__("{0}: {1}".format(reason, detail) if detail else reason)
        self.reason = reason
        self.detail = detail


def decode_payload(p: Any) -> Dict[str, Any]:
    """The rule's ``encode(*, 'base64') AS p`` back to the JSON object the
    device published; ``too_large`` above 128 KB, ``invalid_json`` otherwise."""
    if not isinstance(p, str) or not p:
        raise EnvelopeError("invalid_json", "payload missing")
    try:
        raw = base64.b64decode(p, validate=True)
    except (binascii.Error, ValueError):
        raise EnvelopeError("invalid_json", "payload is not base64")
    if len(raw) > MAX_MESSAGE_BYTES:
        raise EnvelopeError("too_large", "{0} bytes".format(len(raw)))
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise EnvelopeError("invalid_json", "payload is not a JSON document")
    if not isinstance(document, dict):
        raise EnvelopeError("invalid_json", "payload is not a JSON object")
    return document


def _require_str(envelope: Dict[str, Any], field: str, pattern: "re.Pattern") -> str:
    value = envelope.get(field)
    if not isinstance(value, str) or not pattern.match(value):
        raise EnvelopeError("schema_invalid", field)
    return value


def _require_int(envelope: Dict[str, Any], field: str, minimum: int) -> int:
    value = envelope.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EnvelopeError("schema_invalid", field)
    return value


def validate_envelope(envelope: Any) -> Dict[str, Any]:
    """The envelope with every required field checked (§4.1, §4.3); returns
    it unchanged (``workflowVersion`` normalized to a string) or raises
    ``EnvelopeError('schema_invalid', <field>)``."""
    if not isinstance(envelope, dict):
        raise EnvelopeError("schema_invalid", "envelope")
    if envelope.get("schema") != SCHEMA:
        raise EnvelopeError("schema_invalid", "schema")
    _require_str(envelope, "sessionId", _UUID_RE)
    _require_str(envelope, "instance", _UUID_RE)
    _require_str(envelope, "deviceId", _THING_RE)
    _require_str(envelope, "workflowId", _ID_RE)
    version = envelope.get("workflowVersion")
    if isinstance(version, bool) or not isinstance(version, (str, int)) or not str(version).strip():
        raise EnvelopeError("schema_invalid", "workflowVersion")
    envelope["workflowVersion"] = str(version)
    _require_int(envelope, "seq", 1)
    _require_int(envelope, "sentAt", 0)
    records = envelope.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_RECORDS:
        raise EnvelopeError("schema_invalid", "records")
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise EnvelopeError("schema_invalid", "records[{0}]".format(index))
        if not isinstance(record.get("type"), str) or not record["type"]:
            raise EnvelopeError("schema_invalid", "records[{0}].type".format(index))
        ts = record.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, int):
            raise EnvelopeError("schema_invalid", "records[{0}].ts".format(index))
    return envelope


def record_types(records: List[Dict[str, Any]]) -> Dict[str, int]:
    """How many records of each type a validated envelope carries."""
    counts: Dict[str, int] = {}
    for record in records:
        kind = str(record.get("type"))
        counts[kind] = counts.get(kind, 0) + 1
    return counts
