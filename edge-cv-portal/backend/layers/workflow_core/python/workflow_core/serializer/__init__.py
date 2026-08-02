"""Workflow_Serializer: canonical JSON serialization and parsing.

Serializes WorkflowGraph objects to canonical Workflow_Definition JSON
documents and parses documents back into graphs, with JSON-Schema
validation, descriptive first-violation errors, and stepwise schema
migration.

Task 2.1 implements the graph model, the Workflow_Definition JSON Schema
(schemaVersion 1), and canonical ``serialize``. Task 2.2 adds ``parse``
with descriptive errors and schema migration.
"""

from .models import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from .parse import (
    ERROR_DUPLICATE_ID,
    ERROR_INVALID_JSON,
    ERROR_MIGRATION_FAILED,
    ERROR_SCHEMA_VIOLATION,
    ERROR_UNKNOWN_NODE_REFERENCE,
    ERROR_UNSUPPORTED_SCHEMA_VERSION,
    Migration,
    ParseError,
    ParseResult,
    parse,
    register_migration,
    registered_migrations,
    unregister_migration,
)
from .schema import (
    SCHEMA_VERSION,
    SCHEMAS_BY_VERSION,
    WORKFLOW_DEFINITION_SCHEMA,
    WORKFLOW_DEFINITION_SCHEMA_V1,
)
from .serialize import (
    SerializationError,
    graph_to_document,
    serialize,
)

__all__ = [
    # graph model
    "Position",
    "PortEndpoint",
    "Node",
    "Connection",
    "WorkflowGraph",
    # JSON Schema
    "SCHEMA_VERSION",
    "SCHEMAS_BY_VERSION",
    "WORKFLOW_DEFINITION_SCHEMA",
    "WORKFLOW_DEFINITION_SCHEMA_V1",
    # serialization
    "serialize",
    "graph_to_document",
    "SerializationError",
    # parsing
    "parse",
    "ParseResult",
    "ParseError",
    "ERROR_INVALID_JSON",
    "ERROR_SCHEMA_VIOLATION",
    "ERROR_UNSUPPORTED_SCHEMA_VERSION",
    "ERROR_DUPLICATE_ID",
    "ERROR_UNKNOWN_NODE_REFERENCE",
    "ERROR_MIGRATION_FAILED",
    # migration registry
    "Migration",
    "register_migration",
    "unregister_migration",
    "registered_migrations",
]
