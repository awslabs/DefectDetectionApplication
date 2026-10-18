"""The schema adapters of the workflow telemetry (workflow-telemetry-dashboards
design §4.1-§4.4, §18; tasks.md 3.1).

One adapter per schema, built from ``registry.json`` (the single source of
the field declarations; the unit test asserts the adapters equal the
registry). Validation (§4.4):

- the payload is a JSON object of at most 128 KB and, for Summaries and
  Events, at most 16 KB (``too_large``); an unknown ``schema`` is
  ``schema_unknown``;
- only declared fields are validated: strings at most 256 characters unless
  a tighter limit is given, count maps at most 64 entries with non-negative
  integers, finite numbers, nesting at most 8 levels;
- a declared field of the wrong type is coerced by the rule of
  ``output_bindings._coerce`` (numeric strings to numbers, ``"true"`` /
  ``"false"`` to booleans) and checked again; an optional field that still
  fails is dropped and counted ``FieldDropped`` (the field path, never the
  value); a required field that still fails, or is missing, makes the
  message ``schema_invalid``;
- undeclared fields are kept verbatim in the lake record (``raw``) and never
  validated;
- the Summary rules: the length rule, ``intervalS`` by kind (5-60 for a
  Summary, 30-300 for a Heartbeat), ``emittedAt >= intervalEnd``, the eight
  model states with an out-of-set string coerced to ``UNKNOWN`` and counted
  ``FieldDropped``, the ``ext`` limits, the engine's required blocks;
- the Event ``data`` shapes by type (the ``clipId`` grammar, the
  ``startTs``/``endTs`` bounds);
- the ``dda.live.v1`` slot rule: the key is
  ``dda-telemetry/live/<usecase_id>/<deviceId>/<workflowId>/<seq mod 16>.jpg``.

Block ownership (§4.1): ``drop_unowned_blocks`` removes the blocks a producer
does not own from the typed fields (they stay in the lake record) and names
them, so the ingest counts ``block_not_owned`` per block.
"""
import base64
import binascii
import json
import math
import os
import re
import uuid
from collections import namedtuple
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "REGISTRY", "CATALOG", "TEMPLATES", "ADAPTERS", "KINDS", "SCHEMA_BY_KIND", "KIND_BY_SCHEMA",
    "MODEL_STATES", "MODEL_STATE_ORDER", "PAYLOAD_MAX_BYTES", "MESSAGE_MAX_BYTES",
    "SchemaInvalid", "Validated", "Adapter", "adapter_for", "decode_payload", "coerce",
    "validate_message", "drop_unowned_blocks", "block_owner", "worst_state", "catalog_measures",
    "measure_by_id", "template_measures", "catalog_content_hash", "resolve_path", "live_key_for",
]

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name: str) -> Dict[str, Any]:
    with open(os.path.join(_HERE, name), encoding="utf-8") as fh:
        return json.load(fh)


REGISTRY: Dict[str, Any] = _load("registry.json")
CATALOG: Dict[str, Any] = _load("measure_catalog.json")
TEMPLATES: Dict[str, Dict[str, Any]] = {}
for _name in sorted(os.listdir(os.path.join(_HERE, "templates"))):
    if _name.endswith(".json"):
        _template = _load(os.path.join("templates", _name))
        TEMPLATES[_template["id"]] = _template

KINDS: Tuple[str, ...] = tuple(REGISTRY["kinds"].keys())
SCHEMA_BY_KIND: Dict[str, str] = dict(REGISTRY["kinds"])
KIND_BY_SCHEMA: Dict[str, str] = {schema: kind for kind, schema in SCHEMA_BY_KIND.items()}
MODEL_STATES: Tuple[str, ...] = tuple(REGISTRY["modelStates"])
MODEL_STATE_ORDER: Tuple[str, ...] = tuple(REGISTRY["modelStateOrder"])
_STATE_RANK = {state: index for index, state in enumerate(MODEL_STATE_ORDER)}
PAYLOAD_MAX_BYTES: int = int(REGISTRY["limits"]["payloadMaxBytes"])
MESSAGE_MAX_BYTES: int = int(REGISTRY["limits"]["messageMaxBytes"])
STRING_MAX_LENGTH: int = int(REGISTRY["limits"]["stringMaxLength"])
COUNT_MAP_MAX_ENTRIES: int = int(REGISTRY["limits"]["countMapMaxEntries"])
MAX_DEPTH: int = int(REGISTRY["limits"]["maxDepth"])
LIVE_SLOTS = 16
UNKNOWN_STATE = "UNKNOWN"

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: ``(fields, dropped, kind, schema, raw)``: the typed fields (declared and
#: valid), the dropped field paths, the kind, the schema and the raw payload
#: object (undeclared fields included, for the lake).
Validated = namedtuple("Validated", ["fields", "dropped", "kind", "schema", "raw"])


class SchemaInvalid(ValueError):
    """``reason`` is one of ``invalid_json``, ``too_large``, ``schema_unknown``
    or ``schema_invalid``; ``detail`` names the field or rule, never a value."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__("{0}: {1}".format(reason, detail) if detail else reason)
        self.reason = reason
        self.detail = detail


class _Drop(Exception):
    """An optional field failed validation (internal)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# --------------------------------------------------------------------------- coercion
def coerce(value: Any) -> Any:
    """The rule of ``output_bindings._coerce``: ``'true'``/``'false'`` to
    booleans, numeric strings to numbers, everything else unchanged."""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_int(value: Any) -> Optional[int]:
    if _is_int(value):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    coerced = coerce(value)
    if _is_int(coerced):
        return coerced
    if isinstance(coerced, float) and math.isfinite(coerced) and coerced.is_integer():
        return int(coerced)
    return None


def _as_number(value: Any) -> Optional[float]:
    if _is_int(value):
        return float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    coerced = coerce(value)
    if _is_int(coerced):
        return float(coerced)
    if isinstance(coerced, float) and math.isfinite(coerced):
        return coerced
    return None


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    coerced = coerce(value)
    if isinstance(coerced, bool):
        return coerced
    if _is_int(coerced) and coerced in (0, 1):
        return bool(coerced)
    return None


def _as_string(value: Any, spec: Dict[str, Any]) -> Optional[str]:
    if isinstance(value, str):
        text = value
    elif spec.get("acceptInt") and _is_int(value):
        text = str(value)
    else:
        return None
    min_length = int(spec.get("minLength", 1))
    max_length = int(spec.get("maxLength", STRING_MAX_LENGTH))
    if len(text) < min_length or len(text) > max_length:
        return None
    pattern = spec.get("pattern")
    if pattern and not re.match(pattern, text):
        return None
    return text


def _is_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


# --------------------------------------------------------------------------- the field checker
class _Checker:
    def __init__(self, dropped: List[str]) -> None:
        self.dropped = dropped

    def field(self, value: Any, spec: Dict[str, Any], path: str, depth: int) -> Any:
        """The validated value, or raise ``_Drop`` (the caller decides between
        ``FieldDropped`` and ``schema_invalid`` by the field's requiredness)."""
        if depth > MAX_DEPTH:
            raise _Drop(path + ": too deep")
        if value is None:
            if spec.get("nullable"):
                return None
            raise _Drop(path + ": null")
        kind = spec["type"]
        if kind == "const":
            if value != spec["value"]:
                raise _Drop(path + ": const")
            return value
        if kind == "int":
            number = _as_int(value)
            if number is None:
                raise _Drop(path + ": not an integer")
            if "min" in spec and number < spec["min"]:
                raise _Drop(path + ": below minimum")
            if "max" in spec and number > spec["max"]:
                raise _Drop(path + ": above maximum")
            return number
        if kind == "number":
            number = _as_number(value)
            if number is None:
                raise _Drop(path + ": not a number")
            if "min" in spec and number < spec["min"]:
                raise _Drop(path + ": below minimum")
            if "max" in spec and number > spec["max"]:
                raise _Drop(path + ": above maximum")
            return number
        if kind == "bool":
            flag = _as_bool(value)
            if flag is None:
                raise _Drop(path + ": not a boolean")
            return flag
        if kind == "string":
            text = _as_string(value, spec)
            if text is None:
                raise _Drop(path + ": not a valid string")
            return text
        if kind == "uuid":
            if not _is_uuid(value):
                raise _Drop(path + ": not a UUID")
            return value.lower()
        if kind == "enum":
            if not isinstance(value, str) or value not in spec["values"]:
                raise _Drop(path + ": not in the value set")
            return value
        if kind == "modelState":
            return self.model_state(value, path)
        if kind == "object":
            return self.object(value, spec["fields"], path, depth)
        if kind == "objectmap":
            return self.object_map(value, spec, path, depth)
        if kind == "countmap":
            return self.count_map(value, spec, path)
        if kind == "enummap":
            return self.enum_map(value, spec, path)
        raise _Drop(path + ": unknown declaration")

    def model_state(self, value: Any, path: str) -> str:
        """An out-of-set model state is coerced to ``UNKNOWN`` and counted
        ``FieldDropped`` (the ingest never rejects a Summary for it)."""
        if isinstance(value, str) and value in MODEL_STATES:
            return value
        self.dropped.append(path)
        return UNKNOWN_STATE

    def object(self, value: Any, fields: Dict[str, Any], path: str, depth: int) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise _Drop(path + ": not an object")
        result: Dict[str, Any] = {}
        for name, spec in fields.items():
            child = "{0}.{1}".format(path, name) if path else name
            if name not in value:
                if spec.get("required"):
                    raise _Drop(child + ": missing")
                continue
            try:
                result[name] = self.field(value[name], spec, child, depth + 1)
            except _Drop as problem:
                if spec.get("required"):
                    raise _Drop(problem.detail)
                self.dropped.append(child)
        return result

    def _map_keys(self, value: Any, spec: Dict[str, Any], path: str) -> List[str]:
        if not isinstance(value, dict):
            raise _Drop(path + ": not a map")
        limit = int(spec.get("maxEntries", COUNT_MAP_MAX_ENTRIES))
        if len(value) > limit:
            raise _Drop(path + ": too many entries")
        key_max = int(spec.get("keyMaxLength", STRING_MAX_LENGTH))
        keys = []
        for key in value:
            if not isinstance(key, str) or not key or len(key) > key_max:
                raise _Drop(path + ": invalid key")
            keys.append(key)
        return keys

    def object_map(self, value: Any, spec: Dict[str, Any], path: str, depth: int) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key in self._map_keys(value, spec, path):
            result[key] = self.object(value[key], spec["fields"], "{0}.{1}".format(path, key), depth + 1)
        return result

    def count_map(self, value: Any, spec: Dict[str, Any], path: str) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for key in self._map_keys(value, spec, path):
            number = _as_int(value[key])
            if number is None or number < 0:
                raise _Drop("{0}.{1}: not a count".format(path, key))
            result[key] = number
        return result

    def enum_map(self, value: Any, spec: Dict[str, Any], path: str) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for key in self._map_keys(value, spec, path):
            if spec.get("values") == "modelState":
                result[key] = self.model_state(value[key], "{0}.{1}".format(path, key))
            else:
                entry = value[key]
                if not isinstance(entry, str) or entry not in spec["values"]:
                    raise _Drop("{0}.{1}: not in the value set".format(path, key))
                result[key] = entry
        return result


# --------------------------------------------------------------------------- adapters
class Adapter:
    """One schema's adapter: ``validate(payload, usecase_id=None)`` returns a
    :class:`Validated` or raises :class:`SchemaInvalid`."""

    def __init__(self, schema: str, declaration: Dict[str, Any]) -> None:
        self.schema = schema
        self.kind = declaration["kind"]
        self.max_bytes = int(declaration["maxBytes"])
        self.fields: Dict[str, Any] = declaration["fields"]
        self.declaration = declaration

    # -- the common part ----------------------------------------------------
    def _typed(self, payload: Dict[str, Any], dropped: List[str], *, skip: Iterable[str] = ()) -> Dict[str, Any]:
        checker = _Checker(dropped)
        result: Dict[str, Any] = {}
        skip = set(skip)
        for name, spec in self.fields.items():
            if name in skip:
                continue
            if name not in payload:
                if spec.get("required"):
                    raise SchemaInvalid("schema_invalid", name + ": missing")
                continue
            try:
                result[name] = checker.field(payload[name], spec, name, 1)
            except _Drop as problem:
                if spec.get("required"):
                    raise SchemaInvalid("schema_invalid", problem.detail)
                dropped.append(name)
        return result

    def validate(self, payload: Any, *, usecase_id: Optional[str] = None,
                 text_bytes: Optional[int] = None) -> Validated:
        if not isinstance(payload, dict):
            raise SchemaInvalid("invalid_json", "not an object")
        if text_bytes is not None and text_bytes > self.max_bytes:
            raise SchemaInvalid("too_large", "{0} bytes".format(text_bytes))
        if payload.get("schema") != self.schema:
            raise SchemaInvalid("schema_unknown", str(payload.get("schema"))[:64] if isinstance(payload.get("schema"), str) else "schema")
        dropped: List[str] = []
        if self.kind == "summary":
            fields = self._validate_summary(payload, dropped)
        elif self.kind == "event":
            fields = self._validate_event(payload, dropped)
        else:
            fields = self._validate_live(payload, dropped, usecase_id)
        return Validated(fields, dropped, self.kind, self.schema, payload)

    # -- dda.summary.v1 -----------------------------------------------------
    def _validate_summary(self, payload: Dict[str, Any], dropped: List[str]) -> Dict[str, Any]:
        idle = _as_bool(payload.get("idle"))
        if idle is None:
            raise SchemaInvalid("schema_invalid", "idle: not a boolean")
        summary_only = [name for name, spec in self.fields.items() if spec.get("summaryOnly")]
        fields = self._typed(payload, dropped, skip=["ext"] + (summary_only if idle else []))
        if idle:
            for name in summary_only:
                if name in payload:
                    dropped.append(name)
        else:
            if "ext" in payload:
                try:
                    fields["ext"] = self._extensions(payload["ext"], dropped)
                except _Drop as problem:
                    dropped.append("ext")
        rules = self.declaration["rules"]
        interval_s = fields["intervalS"]
        low, high = rules["intervalSByKind"]["heartbeat" if idle else "summary"]
        if not low <= interval_s <= high:
            raise SchemaInvalid("schema_invalid", "intervalS: outside {0}-{1} for a {2}".format(
                low, high, "Heartbeat" if idle else "Summary"))
        length = fields["intervalEnd"] - fields["intervalStart"]
        if not 1000 <= length <= interval_s * 1000 + 1000:
            raise SchemaInvalid("schema_invalid", "intervalEnd - intervalStart: outside the length rule")
        if rules.get("emittedAtNotBeforeIntervalEnd") and fields["emittedAt"] < fields["intervalEnd"]:
            raise SchemaInvalid("schema_invalid", "emittedAt: before intervalEnd")
        if not idle and fields["producer"]["name"] == "engine":
            for name in rules.get("engineRequiresWhenNotIdle", []):
                if name not in fields:
                    raise SchemaInvalid("schema_invalid", name + ": required for the engine producer")
        ppe = (fields.get("ext") or {}).get("ppe")
        if rules.get("ppeCompliantPlusViolatingAtMostSeen") and isinstance(ppe, dict):
            people = ppe.get("people") or {}
            if people.get("compliant", 0) + people.get("violating", 0) > people.get("seen", 0):
                dropped.append("ext.ppe")
                del fields["ext"]["ppe"]
        return fields

    def _extensions(self, value: Any, dropped: List[str]) -> Dict[str, Any]:
        spec = self.fields["ext"]
        if not isinstance(value, dict):
            raise _Drop("ext: not an object")
        if len(value) > int(spec["maxEntries"]):
            raise _Drop("ext: too many extensions")
        if len(json.dumps(value, separators=(",", ":"))) > int(spec["maxBytes"]):
            raise _Drop("ext: too large")
        pattern = re.compile(spec["keyPattern"])
        checker = _Checker(dropped)
        result: Dict[str, Any] = {}
        for key, entry in value.items():
            if not isinstance(key, str) or not pattern.match(key):
                raise _Drop("ext: invalid extension name")
            known = spec["known"].get(key)
            if known is None:
                # Unknown extensions are kept for the lake and ignored by the fold.
                result[key] = entry
                continue
            try:
                result[key] = checker.object(entry, known["fields"], "ext." + key, 2)
            except _Drop:
                dropped.append("ext." + key)
        return result

    # -- dda.event.v1 -------------------------------------------------------
    def _validate_event(self, payload: Dict[str, Any], dropped: List[str]) -> Dict[str, Any]:
        fields = self._typed(payload, dropped, skip=["data"])
        event_type = fields["type"]
        if "severity" not in fields:
            fields["severity"] = self.declaration["severityDefaults"][event_type]
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SchemaInvalid("schema_invalid", "data: missing or not an object")
        checker = _Checker(dropped)
        try:
            fields["data"] = checker.object(data, self.declaration["data"][event_type], "data", 1)
        except _Drop as problem:
            raise SchemaInvalid("schema_invalid", problem.detail)
        if event_type == "clip_ready":
            rules = self.declaration["rules"]
            clip = fields["data"]
            if rules.get("clipEndNotBeforeStart") and clip["endTs"] < clip["startTs"]:
                raise SchemaInvalid("schema_invalid", "data.endTs: before startTs")
            if clip["endTs"] - clip["startTs"] > int(rules["clipMaxSpanMs"]):
                raise SchemaInvalid("schema_invalid", "data.endTs: more than an hour after startTs")
        return fields

    # -- dda.live.v1 --------------------------------------------------------
    def _validate_live(self, payload: Dict[str, Any], dropped: List[str], usecase_id: Optional[str]) -> Dict[str, Any]:
        fields = self._typed(payload, dropped)
        if usecase_id is not None:
            expected = live_key_for(usecase_id, fields["deviceId"], fields["workflowId"], fields["seq"])
            if fields["frame"]["key"] != expected:
                raise SchemaInvalid("schema_invalid", "frame.key: slot or prefix differs from the rule")
        return fields


def live_key_for(usecase_id: str, device_id: str, workflow_id: str, seq: int) -> str:
    """``dda-telemetry/live/<usecase_id>/<deviceId>/<workflowId>/<seq mod 16>.jpg``
    (the slot is written without leading zeros)."""
    return "dda-telemetry/live/{0}/{1}/{2}/{3}.jpg".format(usecase_id, device_id, workflow_id, int(seq) % LIVE_SLOTS)


ADAPTERS: Dict[str, Adapter] = {
    schema: Adapter(schema, declaration) for schema, declaration in REGISTRY["schemas"].items()
}


def adapter_for(schema: Any) -> Adapter:
    """The adapter of a ``schema`` value; ``SchemaInvalid('schema_unknown')``
    for anything else."""
    if isinstance(schema, str) and schema in ADAPTERS:
        return ADAPTERS[schema]
    raise SchemaInvalid("schema_unknown", schema[:64] if isinstance(schema, str) else "schema")


# --------------------------------------------------------------------------- the message path
def decode_payload(encoded: Any) -> Tuple[Dict[str, Any], str]:
    """The rule's base64 ``p`` -> ``(object, text)``. ``too_large`` over
    128 KB, ``invalid_json`` for anything that is not a JSON object."""
    if not isinstance(encoded, str):
        raise SchemaInvalid("invalid_json", "payload is not a string")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise SchemaInvalid("invalid_json", "payload is not base64")
    if len(raw) > PAYLOAD_MAX_BYTES:
        raise SchemaInvalid("too_large", "{0} bytes".format(len(raw)))
    try:
        text = raw.decode("utf-8")
        document = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise SchemaInvalid("invalid_json", "payload is not JSON")
    if not isinstance(document, dict):
        raise SchemaInvalid("invalid_json", "payload is not an object")
    return document, text


def validate_message(document: Dict[str, Any], text: str, *, kind: str,
                     usecase_id: Optional[str] = None) -> Validated:
    """Validate a decoded message against the adapter its ``schema`` names,
    which must be the schema of the topic's ``kind``."""
    adapter = adapter_for(document.get("schema"))
    if adapter.kind != kind:
        raise SchemaInvalid("schema_invalid", "schema: {0} on a {1} topic".format(adapter.schema, kind))
    return adapter.validate(document, usecase_id=usecase_id, text_bytes=len(text.encode("utf-8")))


# --------------------------------------------------------------------------- block ownership
def block_owner(block: str) -> Optional[Tuple[str, ...]]:
    """The producers allowed to fill a top-level block or ``ext.<name>``;
    ``None`` for an undeclared block (kept, never typed)."""
    if block.startswith("ext."):
        entry = REGISTRY["extensions"].get(block[4:])
        if entry is None:
            return None
        return tuple([entry["owner"]] + list(entry.get("allowed") or []))
    owner = REGISTRY["blocks"].get(block)
    return (owner,) if owner else None


def drop_unowned_blocks(fields: Dict[str, Any], producer_name: str) -> Tuple[Dict[str, Any], List[str]]:
    """The typed fields without the blocks ``producer_name`` may not fill, and
    the names of the dropped blocks (``block_not_owned``, dimension Block)."""
    kept = dict(fields)
    dropped: List[str] = []
    for block, owner in REGISTRY["blocks"].items():
        if block in kept and producer_name != owner:
            del kept[block]
            dropped.append(block)
    ext = kept.get("ext")
    if isinstance(ext, dict):
        trimmed = dict(ext)
        for name in list(trimmed):
            allowed = block_owner("ext." + name)
            if allowed is not None and producer_name not in allowed:
                del trimmed[name]
                dropped.append("ext." + name)
        kept["ext"] = trimmed
    return kept, dropped


def worst_state(states: Iterable[Any]) -> Optional[str]:
    """The worst of the model states under
    ``NONE < READY < UNLOADING < LOADING < INCOMPLETE < NOT_DEPLOYED < UNKNOWN < UNAVAILABLE``;
    an unknown string counts as ``UNKNOWN``; ``None`` for no states."""
    worst = None
    for state in states:
        if state is None:
            continue
        candidate = state if state in _STATE_RANK else UNKNOWN_STATE
        if worst is None or _STATE_RANK[candidate] > _STATE_RANK[worst]:
            worst = candidate
    return worst


# --------------------------------------------------------------------------- the catalog and templates
def catalog_measures() -> List[Dict[str, Any]]:
    return list(CATALOG["measures"])


def measure_by_id(measure_id: str) -> Optional[Dict[str, Any]]:
    for measure in CATALOG["measures"]:
        if measure["id"] == measure_id:
            return measure
    return None


def template_measures(template: Dict[str, Any]) -> List[str]:
    """Every measure id a template's panels name, in order, once."""
    seen: List[str] = []
    for panel in template.get("panels", []):
        for measure_id in panel.get("measures", []):
            if measure_id not in seen:
                seen.append(measure_id)
    return seen


def catalog_content_hash() -> str:
    """SHA-256 of the catalog without its ``version``: the version-bump test."""
    import hashlib
    content = {key: value for key, value in CATALOG.items() if key != "version"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def resolve_path(fields: Dict[str, Any], path: str) -> Any:
    """The value at a dotted catalog path in a typed field document; a ``*``
    segment yields ``{key: value}`` for the map at that level. ``None`` when
    a segment is absent."""
    if not path:
        return None
    parts = path.split(".")

    def walk(node: Any, index: int) -> Any:
        if index == len(parts):
            return node
        part = parts[index]
        if not isinstance(node, dict):
            return None
        if part == "*":
            result = {}
            for key, child in node.items():
                value = walk(child, index + 1)
                if value is not None:
                    result[key] = value
            return result
        if part not in node:
            return None
        return walk(node[part], index + 1)

    return walk(fields, 0)
