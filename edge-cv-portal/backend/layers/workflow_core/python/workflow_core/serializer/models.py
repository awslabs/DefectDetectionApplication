"""Graph model for Workflow_Definitions.

A :class:`WorkflowGraph` is the in-memory form of a Workflow_Definition:
nodes (id, type, canvas position, parameter values) and directed
connections between typed port endpoints. The serializer converts graphs
to canonical JSON documents (task 2.1) and parses documents back into
graphs (task 2.2).

Graph equivalence is order-insensitive: two graphs are equivalent when
they contain the same nodes and the same connections regardless of list
ordering (the round-trip property of Requirement 3.4 is stated in these
terms).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Position:
    """Canvas position of a node (Requirement 3.1: positions are persisted)."""

    x: float
    y: float


@dataclass(frozen=True)
class PortEndpoint:
    """One end of a connection: a port on a specific node."""

    node: str  # node id
    port: str  # port name on that node (e.g. "in", "out")


@dataclass
class Node:
    """A single processing stage placed on the canvas.

    ``type`` is a node type id from the catalog (``workflow_core.catalog``);
    ``parameters`` maps parameter names to JSON-representable values.
    """

    id: str
    type: str
    position: Position
    parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Connection:
    """A directed edge from an output port to an input port.

    The ``source`` endpoint corresponds to the document's ``from`` key
    (``from`` is a Python keyword) and ``target`` to ``to``.
    """

    id: str
    source: PortEndpoint
    target: PortEndpoint


@dataclass
class WorkflowGraph:
    """The full workflow graph: nodes plus connections."""

    nodes: List[Node] = field(default_factory=list)
    connections: List[Connection] = field(default_factory=list)

    def node_by_id(self, node_id: str) -> Optional[Node]:
        """Return the node with ``node_id``, or None."""
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def connection_by_id(self, connection_id: str) -> Optional[Connection]:
        """Return the connection with ``connection_id``, or None."""
        for connection in self.connections:
            if connection.id == connection_id:
                return connection
        return None

    def is_equivalent_to(self, other: "WorkflowGraph") -> bool:
        """Order-insensitive graph equivalence (Requirement 3.4).

        True when both graphs contain the same set of nodes (by full
        content) and the same set of connections, regardless of list
        ordering.
        """
        if not isinstance(other, WorkflowGraph):
            return False
        return (
            _by_id(self.nodes) == _by_id(other.nodes)
            and _by_id(self.connections) == _by_id(other.connections)
        )


def _by_id(items: list) -> dict:
    """Index items by ``.id``; duplicate ids collapse (serialize rejects them)."""
    return {item.id: item for item in items}
