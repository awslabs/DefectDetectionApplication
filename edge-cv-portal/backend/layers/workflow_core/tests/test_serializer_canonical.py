"""Unit tests for the WorkflowGraph model and canonical serialization.

Covers task 2.1 of the workflow-manager spec:
- WorkflowGraph model (nodes with id/type/position/parameters,
  connections with typed port endpoints)
- Workflow_Definition JSON Schema (schemaVersion 1)
- serialize(graph) canonical output: sorted keys, nodes and connections
  ordered by id

_Requirements: 3.1_
"""

import json

import pytest

from workflow_core.serializer import (
    SCHEMA_VERSION,
    SCHEMAS_BY_VERSION,
    WORKFLOW_DEFINITION_SCHEMA,
    WORKFLOW_DEFINITION_SCHEMA_V1,
    Connection,
    Node,
    PortEndpoint,
    Position,
    SerializationError,
    WorkflowGraph,
    graph_to_document,
    serialize,
)


def _sample_graph():
    """The design.md example graph: camera -> model inference."""
    return WorkflowGraph(
        nodes=[
            Node(
                id="n1",
                type="icam_source",
                position=Position(x=100, y=200),
                parameters={"device": "/dev/video0", "gain": 4},
            ),
            Node(
                id="n2",
                type="model_inference",
                position=Position(x=400, y=200),
                parameters={"modelName": "widget-anomaly-v3"},
            ),
        ],
        connections=[
            Connection(
                id="c1",
                source=PortEndpoint(node="n1", port="out"),
                target=PortEndpoint(node="n2", port="in"),
            ),
        ],
    )


class TestGraphModel:
    def test_node_holds_id_type_position_parameters(self):
        node = Node(
            id="n1",
            type="crop",
            position=Position(x=1.5, y=-2.0),
            parameters={"top": 10},
        )
        assert node.id == "n1"
        assert node.type == "crop"
        assert node.position == Position(x=1.5, y=-2.0)
        assert node.parameters == {"top": 10}

    def test_node_parameters_default_to_empty_dict(self):
        node = Node(id="n1", type="rotate", position=Position(x=0, y=0))
        assert node.parameters == {}

    def test_connection_holds_typed_port_endpoints(self):
        conn = Connection(
            id="c1",
            source=PortEndpoint(node="a", port="out"),
            target=PortEndpoint(node="b", port="in"),
        )
        assert conn.source.node == "a"
        assert conn.source.port == "out"
        assert conn.target.node == "b"
        assert conn.target.port == "in"

    def test_node_by_id_and_connection_by_id(self):
        graph = _sample_graph()
        assert graph.node_by_id("n2").type == "model_inference"
        assert graph.node_by_id("missing") is None
        assert graph.connection_by_id("c1").source.node == "n1"
        assert graph.connection_by_id("missing") is None

    def test_empty_graph_defaults(self):
        graph = WorkflowGraph()
        assert graph.nodes == []
        assert graph.connections == []

    def test_equivalence_is_order_insensitive(self):
        graph = _sample_graph()
        reversed_graph = WorkflowGraph(
            nodes=list(reversed(graph.nodes)),
            connections=list(graph.connections),
        )
        assert graph.is_equivalent_to(reversed_graph)
        assert reversed_graph.is_equivalent_to(graph)

    def test_equivalence_detects_differences(self):
        graph = _sample_graph()
        changed = _sample_graph()
        changed.nodes[0].parameters["gain"] = 99
        assert not graph.is_equivalent_to(changed)
        assert not graph.is_equivalent_to(WorkflowGraph())
        assert not graph.is_equivalent_to("not a graph")


class TestJsonSchema:
    def test_current_schema_version_is_1(self):
        assert SCHEMA_VERSION == 1
        assert SCHEMAS_BY_VERSION[1] is WORKFLOW_DEFINITION_SCHEMA_V1
        assert WORKFLOW_DEFINITION_SCHEMA is WORKFLOW_DEFINITION_SCHEMA_V1

    def test_schema_declares_required_top_level_fields(self):
        schema = WORKFLOW_DEFINITION_SCHEMA_V1
        assert schema["type"] == "object"
        assert set(schema["required"]) == {"schemaVersion", "nodes", "connections"}
        assert schema["properties"]["schemaVersion"] == {"const": 1}
        assert schema["additionalProperties"] is False

    def test_schema_node_shape(self):
        node_schema = WORKFLOW_DEFINITION_SCHEMA_V1["properties"]["nodes"]["items"]
        assert set(node_schema["required"]) == {"id", "type", "position", "parameters"}
        position_schema = node_schema["properties"]["position"]
        assert set(position_schema["required"]) == {"x", "y"}

    def test_schema_connection_shape(self):
        conn_schema = WORKFLOW_DEFINITION_SCHEMA_V1["properties"]["connections"]["items"]
        assert set(conn_schema["required"]) == {"id", "from", "to"}
        endpoint_schema = conn_schema["properties"]["from"]
        assert set(endpoint_schema["required"]) == {"node", "port"}


class TestCanonicalSerialization:
    def test_serialized_document_contains_everything(self):
        """Requirement 3.1: all nodes, configurations, positions,
        connections, and a schema version identifier."""
        document = json.loads(serialize(_sample_graph()))
        assert document["schemaVersion"] == 1
        assert document["nodes"] == [
            {
                "id": "n1",
                "type": "icam_source",
                "position": {"x": 100, "y": 200},
                "parameters": {"device": "/dev/video0", "gain": 4},
            },
            {
                "id": "n2",
                "type": "model_inference",
                "position": {"x": 400, "y": 200},
                "parameters": {"modelName": "widget-anomaly-v3"},
            },
        ]
        assert document["connections"] == [
            {
                "id": "c1",
                "from": {"node": "n1", "port": "out"},
                "to": {"node": "n2", "port": "in"},
            },
        ]

    def test_output_is_independent_of_input_ordering(self):
        graph = _sample_graph()
        shuffled = WorkflowGraph(
            nodes=list(reversed(graph.nodes)),
            connections=list(reversed(graph.connections)),
        )
        assert serialize(graph) == serialize(shuffled)

    def test_nodes_and_connections_ordered_by_id(self):
        graph = WorkflowGraph(
            nodes=[
                Node(id=node_id, type="rotate", position=Position(x=0, y=0))
                for node_id in ["n9", "n10", "a", "z"]
            ],
            connections=[
                Connection(
                    id=conn_id,
                    source=PortEndpoint(node="a", port="out"),
                    target=PortEndpoint(node="z", port="in"),
                )
                for conn_id in ["c2", "c10", "c1"]
            ],
        )
        document = json.loads(serialize(graph))
        # Lexicographic ordering by id (canonical, deterministic).
        assert [n["id"] for n in document["nodes"]] == ["a", "n10", "n9", "z"]
        assert [c["id"] for c in document["connections"]] == ["c1", "c10", "c2"]

    def test_object_keys_sorted_at_every_level(self):
        output = serialize(_sample_graph())
        document = json.loads(output)

        def assert_sorted(obj):
            if isinstance(obj, dict):
                keys = list(obj.keys())
                # json.loads preserves document key order in dicts.
                assert keys == sorted(keys), "unsorted keys: {}".format(keys)
                for value in obj.values():
                    assert_sorted(value)
            elif isinstance(obj, list):
                for item in obj:
                    assert_sorted(item)

        assert_sorted(document)
        # Serialization of the parsed document with sorted keys is a fixpoint.
        assert output == json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True)

    def test_repeated_serialization_is_byte_identical(self):
        graph = _sample_graph()
        assert serialize(graph) == serialize(graph)

    def test_empty_graph_serializes(self):
        document = json.loads(serialize(WorkflowGraph()))
        assert document == {"schemaVersion": 1, "nodes": [], "connections": []}

    def test_nested_parameter_keys_sorted(self):
        graph = WorkflowGraph(
            nodes=[
                Node(
                    id="n1",
                    type="custom_python",
                    position=Position(x=0, y=0),
                    parameters={"zeta": 1, "alpha": {"b": 2, "a": 1}},
                )
            ]
        )
        output = serialize(graph)
        assert output.index('"alpha"') < output.index('"zeta"')
        assert output.index('"a"') < output.index('"b"')

    def test_unicode_parameters_are_ascii_escaped(self):
        graph = WorkflowGraph(
            nodes=[
                Node(
                    id="n1",
                    type="mqtt_publish",
                    position=Position(x=0, y=0),
                    parameters={"topic": "caméra/λ"},
                )
            ]
        )
        output = serialize(graph)
        assert output == output.encode("ascii").decode("ascii")
        assert json.loads(output)["nodes"][0]["parameters"]["topic"] == "caméra/λ"

    def test_graph_to_document_matches_serialize(self):
        graph = _sample_graph()
        assert json.loads(serialize(graph)) == graph_to_document(graph)


class TestSerializationErrors:
    def test_duplicate_node_ids_rejected(self):
        graph = WorkflowGraph(
            nodes=[
                Node(id="n1", type="rotate", position=Position(x=0, y=0)),
                Node(id="n1", type="crop", position=Position(x=1, y=1)),
            ]
        )
        with pytest.raises(SerializationError, match="duplicate node id"):
            serialize(graph)

    def test_duplicate_connection_ids_rejected(self):
        endpoint = PortEndpoint(node="n1", port="out")
        graph = WorkflowGraph(
            connections=[
                Connection(id="c1", source=endpoint, target=endpoint),
                Connection(id="c1", source=endpoint, target=endpoint),
            ]
        )
        with pytest.raises(SerializationError, match="duplicate connection id"):
            serialize(graph)

    def test_empty_node_id_rejected(self):
        graph = WorkflowGraph(
            nodes=[Node(id="", type="rotate", position=Position(x=0, y=0))]
        )
        with pytest.raises(SerializationError, match="non-empty string"):
            serialize(graph)

    def test_non_json_parameter_value_rejected(self):
        graph = WorkflowGraph(
            nodes=[
                Node(
                    id="n1",
                    type="custom_python",
                    position=Position(x=0, y=0),
                    parameters={"callback": object()},
                )
            ]
        )
        with pytest.raises(SerializationError, match="not JSON-representable"):
            serialize(graph)
