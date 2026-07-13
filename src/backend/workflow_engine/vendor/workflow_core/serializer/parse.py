"""Parsing of Workflow_Definition JSON documents into WorkflowGraphs.

``parse`` runs JSON-Schema validation first, reporting the first
violation encountered with a JSON-pointer path (Requirement 3.3), then
constructs the graph (Requirement 3.2).

Documents declaring an older supported ``schemaVersion`` are upgraded
stepwise through the migration registry and the :class:`ParseResult`
reports ``migrations: [from, to]``; documents declaring a version with
no registered migration path return ``UNSUPPORTED_SCHEMA_VERSION``
(Requirement 3.5).

``parse`` never raises on malformed input: every failure mode is
reported as a :class:`ParseError` carrying an error code, a descriptive
message, and a JSON-pointer ``path`` locating the violation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import jsonschema

from .models import Connection, Node, PortEndpoint, Position, WorkflowGraph
from .schema import SCHEMA_VERSION, SCHEMAS_BY_VERSION

#: The document is not valid JSON at all.
ERROR_INVALID_JSON = "INVALID_JSON"
#: The document violates the JSON Schema for its declared version.
ERROR_SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
#: The declared schemaVersion is neither current nor migratable.
ERROR_UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
#: Two nodes or two connections share an id.
ERROR_DUPLICATE_ID = "DUPLICATE_ID"
#: A connection endpoint references a node id not present in the document.
ERROR_UNKNOWN_NODE_REFERENCE = "UNKNOWN_NODE_REFERENCE"
#: A registered migration failed or produced an invalid document.
ERROR_MIGRATION_FAILED = "MIGRATION_FAILED"


@dataclass(frozen=True)
class ParseError:
    """A descriptive parse failure (Requirement 3.3).

    ``path`` is a JSON pointer (RFC 6901) locating the first violation
    encountered; the empty string denotes the document root.
    """

    code: str
    message: str
    path: str = ""

    def __str__(self) -> str:
        return "{} at {!r}: {}".format(self.code, self.path or "/", self.message)


@dataclass(frozen=True)
class ParseResult:
    """Outcome of :func:`parse`: a graph or a descriptive error.

    ``migrations`` is ``[from, to]`` when the document was upgraded from
    an older supported schema version (Requirement 3.5), else ``None``.
    """

    graph: Optional[WorkflowGraph] = None
    error: Optional[ParseError] = None
    migrations: Optional[List[int]] = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    """A single-step schema upgrade: ``from_version`` -> ``from_version + 1``.

    ``schema`` is the JSON Schema documents at ``from_version`` must
    satisfy before ``upgrade`` runs.
    """

    from_version: int
    schema: Dict[str, Any]
    upgrade: Callable[[Dict[str, Any]], Dict[str, Any]]


#: Registered single-step migrations keyed by from-version.
_MIGRATIONS: Dict[int, Migration] = {}

#: Schema versions whose entry in SCHEMAS_BY_VERSION was added by
#: register_migration (so unregister_migration removes only those).
_REGISTERED_SCHEMAS: set = set()


def register_migration(
    from_version: int,
    schema: Dict[str, Any],
    upgrade: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> None:
    """Register the stepwise migration ``from_version -> from_version + 1``.

    ``schema`` is added to ``SCHEMAS_BY_VERSION`` so parse can validate
    older documents before upgrading them.
    """
    if type(from_version) is not int:
        raise TypeError("from_version must be an int, got {!r}".format(from_version))
    if from_version >= SCHEMA_VERSION:
        raise ValueError(
            "from_version must be older than the current schema version "
            "{}, got {}".format(SCHEMA_VERSION, from_version)
        )
    if from_version in _MIGRATIONS:
        raise ValueError("a migration from version {} is already registered".format(from_version))
    _MIGRATIONS[from_version] = Migration(from_version, schema, upgrade)
    if from_version not in SCHEMAS_BY_VERSION:
        SCHEMAS_BY_VERSION[from_version] = schema
        _REGISTERED_SCHEMAS.add(from_version)


def unregister_migration(from_version: int) -> None:
    """Remove a registered migration (primarily for tests)."""
    _MIGRATIONS.pop(from_version, None)
    if from_version in _REGISTERED_SCHEMAS:
        SCHEMAS_BY_VERSION.pop(from_version, None)
        _REGISTERED_SCHEMAS.discard(from_version)


def registered_migrations() -> Dict[int, Migration]:
    """A snapshot of the migration registry."""
    return dict(_MIGRATIONS)


def _has_migration_path(version: int) -> bool:
    """True when stepwise migrations cover every version up to current."""
    if version >= SCHEMA_VERSION or version not in SCHEMAS_BY_VERSION:
        return False
    return all(v in _MIGRATIONS for v in range(version, SCHEMA_VERSION))


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def parse(doc: str) -> ParseResult:
    """Parse a Workflow_Definition JSON document into a WorkflowGraph.

    Runs JSON-Schema validation first (the first violation is reported
    with a JSON-pointer path, Requirement 3.3), migrates older supported
    schema versions stepwise to the current version (Requirement 3.5),
    then constructs the graph (Requirement 3.2).
    """
    try:
        document = json.loads(doc)
    except json.JSONDecodeError as exc:
        return ParseResult(
            error=ParseError(
                code=ERROR_INVALID_JSON,
                message="invalid JSON: {} (line {}, column {})".format(exc.msg, exc.lineno, exc.colno),
            )
        )

    current_schema = SCHEMAS_BY_VERSION[SCHEMA_VERSION]

    if not isinstance(document, dict):
        violation = _first_violation(document, current_schema)
        return ParseResult(error=violation)

    version = document.get("schemaVersion")
    migrations: Optional[List[int]] = None

    # bool is a subclass of int; exclude it explicitly.
    if type(version) is int and version == SCHEMA_VERSION:
        violation = _first_violation(document, current_schema)
        if violation is not None:
            return ParseResult(error=violation)
    elif type(version) is int and _has_migration_path(version):
        # Validate against the declared version's schema first ...
        violation = _first_violation(document, SCHEMAS_BY_VERSION[version])
        if violation is not None:
            return ParseResult(error=violation)
        # ... then upgrade stepwise to the current version.
        document, error = _migrate(document, version)
        if error is not None:
            return ParseResult(error=error)
        migrations = [version, SCHEMA_VERSION]
    elif type(version) is int:
        return ParseResult(error=_unsupported_version(version))
    else:
        # schemaVersion missing or not an integer: report the schema
        # violation (missing/const mismatch) against the current schema.
        violation = _first_violation(document, current_schema)
        if violation is not None:
            return ParseResult(error=violation)
        # Defensive fallback (e.g. schemaVersion: true satisfying
        # "const: 1" under loose equality in some validator versions).
        return ParseResult(error=_unsupported_version(version))

    graph, error = _build_graph(document)
    if error is not None:
        return ParseResult(error=error)
    return ParseResult(graph=graph, migrations=migrations)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _unsupported_version(version: Any) -> ParseError:
    supported = sorted(v for v in SCHEMAS_BY_VERSION if v == SCHEMA_VERSION or _has_migration_path(v))
    return ParseError(
        code=ERROR_UNSUPPORTED_SCHEMA_VERSION,
        message="unsupported schemaVersion {!r}; supported versions: {}".format(version, supported),
        path="/schemaVersion",
    )


def _escape_pointer_token(token: str) -> str:
    """Escape a JSON-pointer reference token (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def _json_pointer(parts: List[Any]) -> str:
    return "".join("/" + _escape_pointer_token(str(part)) for part in parts)


def _first_violation(document: Any, schema: Dict[str, Any]) -> Optional[ParseError]:
    """The first schema violation encountered, or None when valid.

    ``iter_errors`` traverses the document deterministically (dicts
    preserve document order), so the first yielded error is the first
    violation encountered (Requirement 3.3) and is stable for a given
    document. Leaf errors are preferred over their derived parent
    errors (e.g. the failing item inside an array rather than the
    enclosing "items" error).
    """
    validator = jsonschema.Draft7Validator(schema)
    error = next(iter(validator.iter_errors(document)), None)
    if error is None:
        return None
    # Descend to the deepest context error along the first branch so the
    # pointer identifies the actual violating value.
    while error.context:
        error = error.context[0]
    path_parts: List[Any] = list(error.absolute_path)
    # For "required" violations, extend the pointer with the missing
    # property name for a more precise location.
    if error.validator == "required" and isinstance(error.instance, dict):
        for prop in error.validator_value:
            if prop not in error.instance:
                path_parts.append(prop)
                break
    return ParseError(
        code=ERROR_SCHEMA_VIOLATION,
        message=error.message,
        path=_json_pointer(path_parts),
    )


def _migrate(
    document: Dict[str, Any], from_version: int
) -> Tuple[Optional[Dict[str, Any]], Optional[ParseError]]:
    """Apply registered migrations stepwise from ``from_version`` to current."""
    upgraded = document
    for step in range(from_version, SCHEMA_VERSION):
        migration = _MIGRATIONS[step]
        try:
            upgraded = migration.upgrade(upgraded)
        except Exception as exc:  # noqa: BLE001 - migration code is registered externally
            return None, ParseError(
                code=ERROR_MIGRATION_FAILED,
                message="migration from version {} to {} failed: {}".format(step, step + 1, exc),
            )
    # A migration must produce a document valid at the current version.
    violation = _first_violation(upgraded, SCHEMAS_BY_VERSION[SCHEMA_VERSION])
    if violation is not None:
        return None, ParseError(
            code=ERROR_MIGRATION_FAILED,
            message="migrated document is invalid at version {}: {}".format(
                SCHEMA_VERSION, violation.message
            ),
            path=violation.path,
        )
    return upgraded, None


def _build_graph(
    document: Dict[str, Any]
) -> Tuple[Optional[WorkflowGraph], Optional[ParseError]]:
    """Construct the graph, enforcing structural rules the schema cannot.

    Checks unique node/connection ids and that connection endpoints
    reference nodes present in the document. Port-level checks (port
    existence, direction, type compatibility) belong to the
    Workflow_Validator.
    """
    node_ids = set()
    for index, node_doc in enumerate(document["nodes"]):
        if node_doc["id"] in node_ids:
            return None, ParseError(
                code=ERROR_DUPLICATE_ID,
                message="duplicate node id {!r}".format(node_doc["id"]),
                path=_json_pointer(["nodes", index, "id"]),
            )
        node_ids.add(node_doc["id"])

    connection_ids = set()
    for index, conn_doc in enumerate(document["connections"]):
        if conn_doc["id"] in connection_ids:
            return None, ParseError(
                code=ERROR_DUPLICATE_ID,
                message="duplicate connection id {!r}".format(conn_doc["id"]),
                path=_json_pointer(["connections", index, "id"]),
            )
        connection_ids.add(conn_doc["id"])
        for key in ("from", "to"):
            referenced = conn_doc[key]["node"]
            if referenced not in node_ids:
                return None, ParseError(
                    code=ERROR_UNKNOWN_NODE_REFERENCE,
                    message="connection {!r} references unknown node {!r}".format(
                        conn_doc["id"], referenced
                    ),
                    path=_json_pointer(["connections", index, key, "node"]),
                )

    graph = WorkflowGraph(
        nodes=[
            Node(
                id=node_doc["id"],
                type=node_doc["type"],
                position=Position(x=node_doc["position"]["x"], y=node_doc["position"]["y"]),
                parameters=dict(node_doc["parameters"]),
            )
            for node_doc in document["nodes"]
        ],
        connections=[
            Connection(
                id=conn_doc["id"],
                source=PortEndpoint(node=conn_doc["from"]["node"], port=conn_doc["from"]["port"]),
                target=PortEndpoint(node=conn_doc["to"]["node"], port=conn_doc["to"]["port"]),
            )
            for conn_doc in document["connections"]
        ],
    )
    return graph, None
