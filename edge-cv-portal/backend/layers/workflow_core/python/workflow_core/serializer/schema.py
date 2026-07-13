"""Workflow_Definition JSON Schema (draft-07).

Authoritative schema for the Workflow_Definition interchange document
(Requirement 3.1). ``SCHEMA_VERSION`` is the current version; parsing
(task 2.2) validates documents against the schema for their declared
version and migrates older supported versions stepwise to the current
one, so schemas are kept per-version in ``SCHEMAS_BY_VERSION``.

Structural rules that JSON Schema cannot express conveniently (unique
node/connection ids, connection endpoints referencing nodes present in
the document) are enforced during graph construction in ``parse``;
port-level checks belong to the Workflow_Validator.
"""

from __future__ import annotations

#: Current Workflow_Definition schema version.
SCHEMA_VERSION = 1

_POSITION_SCHEMA = {
    "type": "object",
    "description": "Canvas position of the node.",
    "required": ["x", "y"],
    "additionalProperties": False,
    "properties": {
        "x": {"type": "number"},
        "y": {"type": "number"},
    },
}

_NODE_SCHEMA = {
    "type": "object",
    "description": "A single processing stage: id, catalog type, canvas position, and parameter values.",
    "required": ["id", "type", "position", "parameters"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "type": {"type": "string", "minLength": 1},
        "position": _POSITION_SCHEMA,
        "parameters": {
            "type": "object",
            "description": "Parameter name to JSON value; keys and value types are declared by the node type's catalog descriptor.",
        },
    },
}

_PORT_ENDPOINT_SCHEMA = {
    "type": "object",
    "description": "A typed port endpoint: a port name on a specific node.",
    "required": ["node", "port"],
    "additionalProperties": False,
    "properties": {
        "node": {"type": "string", "minLength": 1},
        "port": {"type": "string", "minLength": 1},
    },
}

_CONNECTION_SCHEMA = {
    "type": "object",
    "description": "A directed edge from an output port ('from') to an input port ('to').",
    "required": ["id", "from", "to"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "from": _PORT_ENDPOINT_SCHEMA,
        "to": _PORT_ENDPOINT_SCHEMA,
    },
}

#: JSON Schema for Workflow_Definition documents at schemaVersion 1.
WORKFLOW_DEFINITION_SCHEMA_V1 = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "https://aws-samples.github.io/defect-detection/schemas/workflow-definition-v1.json",
    "title": "Workflow_Definition",
    "description": (
        "Serializable workflow graph document: all nodes with their "
        "configurations and canvas positions, all connections, and a "
        "schema version identifier (Requirement 3.1)."
    ),
    "type": "object",
    "required": ["schemaVersion", "nodes", "connections"],
    "additionalProperties": False,
    "properties": {
        "schemaVersion": {"const": 1},
        "nodes": {"type": "array", "items": _NODE_SCHEMA},
        "connections": {"type": "array", "items": _CONNECTION_SCHEMA},
    },
}

#: Schema for the current version.
WORKFLOW_DEFINITION_SCHEMA = WORKFLOW_DEFINITION_SCHEMA_V1

#: Per-version schemas; parse (task 2.2) selects by declared schemaVersion.
SCHEMAS_BY_VERSION = {
    1: WORKFLOW_DEFINITION_SCHEMA_V1,
}
