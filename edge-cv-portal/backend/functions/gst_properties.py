"""
GStreamer Introspection_Report parsing and serialization (Node Designer,
gst-parameter-prepopulation)

Pure module — deliberately imports no boto3 and touches no AWS at module
scope, so it is directly importable and hypothesis-testable. It defines the
stable JSON shape of the Introspection_Report (version 1) that the build-time
capture script (`dda-gst-introspect`) emits, `plugin_builds.py` validates,
and the `GET /plugins/{id}/versions/{v}/gst-properties` route serves
(Requirements 8.1, 8.3).

Structure (Introspection_Report version 1):

    {
      "reportVersion": 1,
      "status": "captured" | "failed",
      "message": str | null,
      "gstVersion": str | null,
      "capturedAt": str | null,
      "elements": [
        {
          "factory": str,
          "elementGType": str,
          "instantiationError": str | null,
          "properties": [
            { "name": str, "gtype": str, "owner": str, "writable": bool,
              "blurb": str | null, "default": JSON scalar | null,
              "min": number | null, "max": number | null,
              "enumValues": [{"value": int, "nick": str}, ...] | null }
          ]
        }
      ]
    }

`parse_report` raises the typed `ReportError` on ANY non-conforming input
(callers map it to the "introspection_failed" unavailability reason instead
of an internal error, Requirement 8.3). `serialize_report` is its inverse:
for every valid report, `parse_report(serialize_report(report)) == report`
and the serialized form survives a `json.dumps`/`json.loads` cycle unchanged
(Requirements 8.1, 8.2).
"""
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

# The one report shape version this module reads and writes.
REPORT_VERSION = 1

# Capture status values (Requirement 1.4: a failed capture is still a
# well-formed, storable report).
STATUS_CAPTURED = 'captured'
STATUS_FAILED = 'failed'
VALID_STATUSES = (STATUS_CAPTURED, STATUS_FAILED)

# JSON scalar type for property default values.
JsonScalar = Union[str, int, float, bool]


class ReportError(Exception):
    """A stored/received Introspection_Report document is malformed.

    Raised by `parse_report` for any non-conforming input; the API route
    maps it to the "introspection_failed" unavailability reason rather
    than surfacing an internal error (Requirement 8.3).
    """


@dataclass(frozen=True)
class EnumValue:
    """One GEnum value: its integer value and its nick (string identifier)."""
    value: int
    nick: str


@dataclass(frozen=True)
class GstProperty:
    """One GStreamer_Property captured from a GObject pspec.

    `owner` records the GType name of the GObject class that declared the
    property (base-class filtering ground truth, Requirements 1.3, 4.x).
    `min`/`max` are present only for ranged numeric GTypes; `enum_values`
    only for GEnum GTypes (Requirement 1.2).
    """
    name: str
    gtype: str
    owner: str
    writable: bool
    blurb: Optional[str] = None
    default: Optional[JsonScalar] = None
    min: Optional[Union[int, float]] = None
    max: Optional[Union[int, float]] = None
    enum_values: Optional[List[EnumValue]] = None


@dataclass(frozen=True)
class ReportElement:
    """One element factory registered by the introspected Plugin_Artifact."""
    factory: str
    element_gtype: str
    instantiation_error: Optional[str] = None
    properties: List[GstProperty] = field(default_factory=list)


@dataclass(frozen=True)
class Report:
    """A parsed Introspection_Report (version 1)."""
    status: str
    message: Optional[str] = None
    gst_version: Optional[str] = None
    captured_at: Optional[str] = None
    elements: List[ReportElement] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing (JSON document -> Report), Requirement 8.1 / 8.3
# ---------------------------------------------------------------------------

def _require_dict(value: Any, where: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ReportError(f'{where}: expected an object, got {type(value).__name__}')
    return value


def _require_list(value: Any, where: str) -> List[Any]:
    if not isinstance(value, list):
        raise ReportError(f'{where}: expected an array, got {type(value).__name__}')
    return value


def _require_str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ReportError(f'{where}: expected a string, got {type(value).__name__}')
    return value


def _optional_str(value: Any, where: str) -> Optional[str]:
    if value is None:
        return None
    return _require_str(value, where)


def _require_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ReportError(f'{where}: expected a boolean, got {type(value).__name__}')
    return value


def _require_int(value: Any, where: str) -> int:
    # bool is a subclass of int in Python; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReportError(f'{where}: expected an integer, got {type(value).__name__}')
    return value


def _optional_number(value: Any, where: str) -> Optional[Union[int, float]]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportError(f'{where}: expected a number or null, got {type(value).__name__}')
    return value


def _optional_scalar(value: Any, where: str) -> Optional[JsonScalar]:
    if value is None:
        return None
    if not isinstance(value, (str, int, float, bool)):
        raise ReportError(f'{where}: expected a JSON scalar or null, got {type(value).__name__}')
    return value


def _parse_enum_values(value: Any, where: str) -> Optional[List[EnumValue]]:
    if value is None:
        return None
    entries = _require_list(value, where)
    parsed: List[EnumValue] = []
    for index, entry in enumerate(entries):
        entry_where = f'{where}[{index}]'
        entry_dict = _require_dict(entry, entry_where)
        parsed.append(EnumValue(
            value=_require_int(entry_dict.get('value'), f'{entry_where}.value'),
            nick=_require_str(entry_dict.get('nick'), f'{entry_where}.nick'),
        ))
    return parsed


def _parse_property(value: Any, where: str) -> GstProperty:
    prop = _require_dict(value, where)
    return GstProperty(
        name=_require_str(prop.get('name'), f'{where}.name'),
        gtype=_require_str(prop.get('gtype'), f'{where}.gtype'),
        owner=_require_str(prop.get('owner'), f'{where}.owner'),
        writable=_require_bool(prop.get('writable'), f'{where}.writable'),
        blurb=_optional_str(prop.get('blurb'), f'{where}.blurb'),
        default=_optional_scalar(prop.get('default'), f'{where}.default'),
        min=_optional_number(prop.get('min'), f'{where}.min'),
        max=_optional_number(prop.get('max'), f'{where}.max'),
        enum_values=_parse_enum_values(prop.get('enumValues'), f'{where}.enumValues'),
    )


def _parse_element(value: Any, where: str) -> ReportElement:
    element = _require_dict(value, where)
    properties_raw = _require_list(element.get('properties', []), f'{where}.properties')
    return ReportElement(
        factory=_require_str(element.get('factory'), f'{where}.factory'),
        element_gtype=_require_str(element.get('elementGType'), f'{where}.elementGType'),
        instantiation_error=_optional_str(element.get('instantiationError'),
                                          f'{where}.instantiationError'),
        properties=[_parse_property(prop, f'{where}.properties[{i}]')
                    for i, prop in enumerate(properties_raw)],
    )


def parse_report(document: Any) -> Report:
    """Parse a stored Introspection_Report JSON document into a Report.

    Raises ReportError on any non-conforming input — wrong top-level type,
    unsupported reportVersion, unknown status, or any mistyped field at any
    depth (Requirements 8.1, 8.3). Never raises anything else for
    JSON-decodable input.
    """
    doc = _require_dict(document, 'report')

    version = doc.get('reportVersion')
    if isinstance(version, bool) or version != REPORT_VERSION:
        raise ReportError(f'report.reportVersion: expected {REPORT_VERSION}, got {version!r}')

    status = _require_str(doc.get('status'), 'report.status')
    if status not in VALID_STATUSES:
        raise ReportError(f'report.status: expected one of {VALID_STATUSES}, got {status!r}')

    elements_raw = _require_list(doc.get('elements', []), 'report.elements')

    return Report(
        status=status,
        message=_optional_str(doc.get('message'), 'report.message'),
        gst_version=_optional_str(doc.get('gstVersion'), 'report.gstVersion'),
        captured_at=_optional_str(doc.get('capturedAt'), 'report.capturedAt'),
        elements=[_parse_element(element, f'report.elements[{i}]')
                  for i, element in enumerate(elements_raw)],
    )


# ---------------------------------------------------------------------------
# Serialization (Report -> JSON document), Requirement 8.1 / 8.2
# ---------------------------------------------------------------------------

def _serialize_property(prop: GstProperty) -> Dict[str, Any]:
    return {
        'name': prop.name,
        'gtype': prop.gtype,
        'owner': prop.owner,
        'writable': prop.writable,
        'blurb': prop.blurb,
        'default': prop.default,
        'min': prop.min,
        'max': prop.max,
        'enumValues': (None if prop.enum_values is None else
                       [{'value': ev.value, 'nick': ev.nick} for ev in prop.enum_values]),
    }


def _serialize_element(element: ReportElement) -> Dict[str, Any]:
    return {
        'factory': element.factory,
        'elementGType': element.element_gtype,
        'instantiationError': element.instantiation_error,
        'properties': [_serialize_property(prop) for prop in element.properties],
    }


def serialize_report(report: Report) -> Dict[str, Any]:
    """Serialize a Report to its version-1 JSON document shape.

    Inverse of `parse_report`: every field is emitted explicitly (optional
    fields as null) so the document is stable and
    `parse_report(serialize_report(report)) == report` for every valid
    report (Requirements 8.1, 8.2).
    """
    return {
        'reportVersion': REPORT_VERSION,
        'status': report.status,
        'message': report.message,
        'gstVersion': report.gst_version,
        'capturedAt': report.captured_at,
        'elements': [_serialize_element(element) for element in report.elements],
    }


# ---------------------------------------------------------------------------
# Type_Mapping (GstProperty -> Parameter_Suggestion | Skipped)
# Requirements 2.1-2.6, 3.1, 3.2
# ---------------------------------------------------------------------------

# GType -> paramType mapping table (Requirement 2.1).
GTYPE_INT = frozenset({'gint', 'guint', 'gint64', 'guint64', 'glong', 'gulong', 'guchar'})
GTYPE_FLOAT = frozenset({'gfloat', 'gdouble'})
GTYPE_BOOL = 'gboolean'
GTYPE_STRING = 'gchararray'

PARAM_INT = 'int'
PARAM_FLOAT = 'float'
PARAM_BOOL = 'bool'
PARAM_STRING = 'string'
PARAM_ENUM = 'enum'


@dataclass(frozen=True)
class Skipped:
    """A GStreamer_Property excluded from the Parameter_Suggestions.

    Carries the property name and a non-empty human-readable reason
    (Requirement 2.5); serialized on the wire as ``{name, reason}``.
    """
    name: str
    reason: str


def _int_default(prop: GstProperty) -> Optional[int]:
    """Convert the property default to an int, or None when unconvertible
    or outside the property's declared range (no usable default, 3.1)."""
    default = prop.default
    if isinstance(default, bool):
        return None
    if isinstance(default, int):
        converted = default
    elif isinstance(default, float) and default.is_integer():
        converted = int(default)
    else:
        return None
    if prop.min is not None and not converted >= prop.min:
        return None
    if prop.max is not None and not converted <= prop.max:
        return None
    return converted


def _float_default(prop: GstProperty) -> Optional[float]:
    """Convert the property default to a float, or None when unconvertible
    or outside the property's declared range (no usable default, 3.1)."""
    default = prop.default
    if isinstance(default, bool) or not isinstance(default, (int, float)):
        return None
    converted = float(default)
    if prop.min is not None and not converted >= prop.min:
        return None
    if prop.max is not None and not converted <= prop.max:
        return None
    return converted


def _string_default(prop: GstProperty) -> Optional[str]:
    """A string default is usable only when non-NULL and non-empty
    (whitespace-only counts as empty), Requirement 3.1."""
    default = prop.default
    if isinstance(default, str) and default.strip():
        return default
    return None


def _bool_default(prop: GstProperty) -> Optional[bool]:
    return prop.default if isinstance(prop.default, bool) else None


def _enum_default_nick(prop: GstProperty) -> Optional[str]:
    """Resolve the property default to one of the enum's nicks: a string
    default must match a nick, an integer default is looked up by its
    enum value. Anything else is not a usable default (3.1)."""
    default = prop.default
    enum_values = prop.enum_values or []
    if isinstance(default, str):
        for entry in enum_values:
            if entry.nick == default:
                return entry.nick
        return None
    if isinstance(default, int) and not isinstance(default, bool):
        for entry in enum_values:
            if entry.value == default:
                return entry.nick
    return None


def _numeric_constraints(prop: GstProperty) -> Optional[Dict[str, Any]]:
    """min/max constraints for ranged numeric properties (Requirement 2.2)."""
    constraints: Dict[str, Any] = {}
    if prop.min is not None:
        constraints['min'] = prop.min
    if prop.max is not None:
        constraints['max'] = prop.max
    return constraints or None


def _int_example(prop: GstProperty) -> int:
    """Synthesized example for a required int suggestion: the range minimum
    when ranged, else a bound-respecting fallback (Requirement 2.6)."""
    if prop.min is not None:
        return prop.min if isinstance(prop.min, int) else math.ceil(prop.min)
    if prop.max is not None:
        return prop.max if isinstance(prop.max, int) else math.floor(prop.max)
    return 0


def _float_example(prop: GstProperty) -> float:
    if prop.min is not None:
        return float(prop.min)
    if prop.max is not None:
        return float(prop.max)
    return 0.0


def _description(prop: GstProperty) -> str:
    """The blurb when non-empty, else a synthesized description naming the
    property and its GType (Requirement 2.4)."""
    if prop.blurb and prop.blurb.strip():
        return prop.blurb
    return f'{prop.name} ({prop.gtype}) property of the plugin element'


def _suggestion(prop: GstProperty, param_type: str, default: Optional[JsonScalar],
                constraints: Optional[Dict[str, Any]],
                synthesized_example: JsonScalar) -> Dict[str, Any]:
    """Assemble the ParameterDeclaration wire shape.

    ``required`` is true iff there is no usable default (3.1); an optional
    suggestion carries the default and uses it as the example (3.2, 2.3);
    a required suggestion carries a type-appropriate synthesized example so
    every suggestion passes declaration validation (2.6).
    """
    suggestion: Dict[str, Any] = {
        'name': prop.name,
        'paramType': param_type,
        'required': default is None,
    }
    if default is not None:
        suggestion['default'] = default
    if constraints is not None:
        suggestion['constraints'] = constraints
    suggestion['description'] = _description(prop)
    suggestion['examples'] = [default if default is not None else synthesized_example]
    return suggestion


def map_property(prop: GstProperty) -> Union[Dict[str, Any], Skipped]:
    """Apply the Type_Mapping to one GStreamer_Property.

    Returns either a Parameter_Suggestion in the ``ParameterDeclaration``
    wire shape validated by ``workflow_core.catalog.custom`` (``{name,
    paramType, required, default?, constraints?, description, examples}``)
    or a :class:`Skipped` entry with a non-empty reason.

    Mapping table (Requirement 2.1):
      gint/guint/gint64/guint64/glong/gulong/guchar -> int (min/max carried)
      gfloat/gdouble                                -> float (min/max carried)
      gboolean                                      -> bool
      gchararray                                    -> string
      GEnum (enum_values present)                   -> enum ({values: nicks})
      anything else, or writable == False           -> Skipped (2.5)

    Required/optional (3.1, 3.2): required iff the property has no usable
    default — a NULL/empty string default, or a default that cannot be
    converted to the mapped paramType (including numeric defaults outside
    the property's own declared range, which would fail the declaration's
    own validation); otherwise optional with the default carried.
    """
    if not prop.writable:
        return Skipped(name=prop.name, reason='property is not writable')

    if prop.gtype in GTYPE_INT:
        return _suggestion(prop, PARAM_INT, _int_default(prop),
                           _numeric_constraints(prop), _int_example(prop))

    if prop.gtype in GTYPE_FLOAT:
        return _suggestion(prop, PARAM_FLOAT, _float_default(prop),
                           _numeric_constraints(prop), _float_example(prop))

    if prop.gtype == GTYPE_BOOL:
        return _suggestion(prop, PARAM_BOOL, _bool_default(prop), None, False)

    if prop.gtype == GTYPE_STRING:
        return _suggestion(prop, PARAM_STRING, _string_default(prop), None, 'value')

    if prop.enum_values is not None:
        if not prop.enum_values:
            return Skipped(name=prop.name,
                           reason=f"GEnum type '{prop.gtype}' declares no enum values")
        nicks = [entry.nick for entry in prop.enum_values]
        return _suggestion(prop, PARAM_ENUM, _enum_default_nick(prop),
                           {'values': nicks}, nicks[0])

    return Skipped(name=prop.name,
                   reason=f"no parameter type mapping for GType '{prop.gtype}'")


# ---------------------------------------------------------------------------
# Base_Class_Property filtering and per-element suggestion derivation
# Requirements 4.1, 4.2, 1.5
# ---------------------------------------------------------------------------

def is_base_class_property(prop: GstProperty, element_gtype: str) -> bool:
    """True iff the property is a Base_Class_Property of the element.

    A property is a Base_Class_Property when its ``owner`` (the GType name
    of the GObject class that declared it) differs from the element's own
    GType (Requirement 4.1). Owner equality keeps properties the element's
    own class re-declares (overrides/shadows), even when the same name also
    exists on a base class — the introspection records the shadowing pspec
    with the element's own GType as owner (Requirement 4.2).
    """
    return prop.owner != element_gtype


def suggestions_for_element(element: ReportElement) -> Dict[str, List[Dict[str, Any]]]:
    """Derive the Parameter_Scan result for one report element.

    Excludes every Base_Class_Property entirely — base-class plumbing like
    ``name``/``parent``/``qos`` appears neither in the suggestions nor in
    the skipped list (Requirement 4.1) — then applies :func:`map_property`
    to the element's own properties, preserving property order.

    Returns the wire shape served by the gst-properties route
    (Requirement 1.5)::

        {
          "suggestions": [ParameterDeclaration dict, ...],
          "skipped": [{"name": str, "reason": str}, ...]
        }
    """
    suggestions: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for prop in element.properties:
        if is_base_class_property(prop, element.element_gtype):
            continue
        mapped = map_property(prop)
        if isinstance(mapped, Skipped):
            skipped.append({'name': mapped.name, 'reason': mapped.reason})
        else:
            suggestions.append(mapped)
    return {'suggestions': suggestions, 'skipped': skipped}
