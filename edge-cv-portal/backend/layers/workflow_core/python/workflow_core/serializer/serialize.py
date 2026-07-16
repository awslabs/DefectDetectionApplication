"""Canonical serialization of WorkflowGraph to Workflow_Definition JSON.

``serialize`` emits a canonical document (Requirement 3.1):

- object keys sorted at every level,
- ``nodes`` ordered by node id, ``connections`` ordered by connection id,
- fixed whitespace (2-space indent), ASCII-escaped output.

Canonical output makes the round-trip property "identical JSON structure"
achievable (Requirement 3.4): serializing a parsed serialization is
byte-identical to the original serialization. Advisory node ``data``
(excluded from graph equivalence) is preserved verbatim and omitted when
empty, so definitions without node data serialize exactly as before the
field existed (Requirement 11.5).
"""

from __future__ import annotations

import json
from typing import Any, Dict

from .models import WorkflowGraph
from .schema import SCHEMA_VERSION


class SerializationError(ValueError):
    """A graph cannot be serialized to a well-formed Workflow_Definition."""


def graph_to_document(graph: WorkflowGraph) -> Dict[str, Any]:
    """Convert a graph to its Workflow_Definition document (plain dict).

    Nodes and connections are ordered by id. Raises
    :class:`SerializationError` on empty or duplicate ids, which would
    make the canonical form ambiguous.
    """
    _check_ids("node", [node.id for node in graph.nodes])
    _check_ids("connection", [connection.id for connection in graph.connections])

    return {
        "schemaVersion": SCHEMA_VERSION,
        "nodes": [
            _node_to_document(node)
            for node in sorted(graph.nodes, key=lambda n: n.id)
        ],
        "connections": [
            {
                "id": connection.id,
                "from": {"node": connection.source.node, "port": connection.source.port},
                "to": {"node": connection.target.node, "port": connection.target.port},
            }
            for connection in sorted(graph.connections, key=lambda c: c.id)
        ],
    }


def _node_to_document(node) -> Dict[str, Any]:
    """A node's document form; advisory ``data`` is emitted only when
    non-empty so definitions without node data serialize byte-identically
    to before the field existed (Requirement 11.5)."""
    document = {
        "id": node.id,
        "type": node.type,
        "position": {"x": node.position.x, "y": node.position.y},
        "parameters": dict(node.parameters),
    }
    if node.data:
        document["data"] = dict(node.data)
    return document


def serialize(graph: WorkflowGraph) -> str:
    """Serialize a graph to its canonical Workflow_Definition JSON string.

    The output contains all nodes, node configurations, node positions,
    connections, and the schema version identifier (Requirement 3.1).
    """
    document = graph_to_document(graph)
    try:
        return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise SerializationError(
            "graph contains a parameter value that is not JSON-representable: {}".format(exc)
        ) from exc


def _check_ids(kind: str, ids: list) -> None:
    """Reject empty and duplicate ids so canonical ordering is well-defined."""
    seen = set()
    for item_id in ids:
        if not isinstance(item_id, str) or not item_id:
            raise SerializationError("{} id must be a non-empty string, got {!r}".format(kind, item_id))
        if item_id in seen:
            raise SerializationError("duplicate {} id: {!r}".format(kind, item_id))
        seen.add(item_id)
