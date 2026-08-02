"""Unit tests for parse(): descriptive errors and schema migration.

Covers task 2.2 of the workflow-manager spec:
- parse(doc) runs JSON-Schema validation first, reporting the first
  violation with a JSON-pointer path, then constructs the graph
- stepwise migration registry: older supported schemaVersion documents
  upgrade to current with ParseResult.migrations == [from, to];
  unsupported versions return UNSUPPORTED_SCHEMA_VERSION

The dedicated round-trip and rejection property tests are tasks 2.4/2.5;
per-migration fixture tests are task 2.6.

_Requirements: 3.2, 3.3, 3.5_
"""

import json

import pytest

from workflow_core.serializer import (
    ERROR_DUPLICATE_ID,
    ERROR_INVALID_JSON,
    ERROR_MIGRATION_FAILED,
    ERROR_SCHEMA_VIOLATION,
    ERROR_UNKNOWN_NODE_REFERENCE,
    ERROR_UNSUPPORTED_SCHEMA_VERSION,
    SCHEMA_VERSION,
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
    parse,
    register_migration,
    registered_migrations,
    serialize,
    unregister_migration,
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


def _sample_document():
    return json.loads(serialize(_sample_graph()))


class TestParseValidDocuments:
    def test_parse_of_serialized_graph_is_equivalent(self):
        """Requirement 3.2: parsing a valid document produces an
        equivalent graph."""
        graph = _sample_graph()
        result = parse(serialize(graph))
        assert result.ok
        assert result.error is None
        assert result.graph.is_equivalent_to(graph)

    def test_current_version_document_reports_no_migrations(self):
        result = parse(serialize(_sample_graph()))
        assert result.ok
        assert result.migrations is None

    def test_empty_graph_document_parses(self):
        result = parse(json.dumps({"schemaVersion": 1, "nodes": [], "connections": []}))
        assert result.ok
        assert result.graph.is_equivalent_to(WorkflowGraph())

    def test_parsed_graph_preserves_positions_and_parameters(self):
        result = parse(serialize(_sample_graph()))
        node = result.graph.node_by_id("n1")
        assert node.position == Position(x=100, y=200)
        assert node.parameters == {"device": "/dev/video0", "gain": 4}
        connection = result.graph.connection_by_id("c1")
        assert connection.source == PortEndpoint(node="n1", port="out")
        assert connection.target == PortEndpoint(node="n2", port="in")


class TestParseDescriptiveErrors:
    def test_invalid_json_reports_invalid_json(self):
        result = parse("{not json")
        assert not result.ok
        assert result.graph is None
        assert result.error.code == ERROR_INVALID_JSON
        assert "line 1" in result.error.message

    def test_non_object_document_is_a_schema_violation(self):
        result = parse(json.dumps([1, 2, 3]))
        assert not result.ok
        assert result.error.code == ERROR_SCHEMA_VIOLATION
        assert result.error.path == ""

    def test_missing_required_field_reports_pointer(self):
        document = _sample_document()
        del document["nodes"]
        result = parse(json.dumps(document))
        assert not result.ok
        assert result.error.code == ERROR_SCHEMA_VIOLATION
        assert result.error.path == "/nodes"
        assert "nodes" in result.error.message

    def test_wrong_type_reports_pointer_to_violating_value(self):
        document = _sample_document()
        document["nodes"][1]["position"]["x"] = "not a number"
        result = parse(json.dumps(document))
        assert not result.ok
        assert result.error.code == ERROR_SCHEMA_VIOLATION
        assert result.error.path == "/nodes/1/position/x"

    def test_missing_node_field_reports_pointer(self):
        document = _sample_document()
        del document["nodes"][0]["type"]
        result = parse(json.dumps(document))
        assert not result.ok
        assert result.error.code == ERROR_SCHEMA_VIOLATION
        assert result.error.path == "/nodes/0/type"

    def test_empty_id_reports_pointer(self):
        document = _sample_document()
        document["connections"][0]["id"] = ""
        result = parse(json.dumps(document))
        assert not result.ok
        assert result.error.code == ERROR_SCHEMA_VIOLATION
        assert result.error.path == "/connections/0/id"

    def test_additional_property_is_a_violation(self):
        document = _sample_document()
        document["extra"] = True
        result = parse(json.dumps(document))
        assert not result.ok
        assert result.error.code == ERROR_SCHEMA_VIOLATION

    def test_first_violation_only_is_reported(self):
        """Requirement 3.3: the first violation encountered is reported."""
        document = _sample_document()
        document["nodes"][0]["id"] = 42  # first violation in document order
        document["connections"][0]["to"]["port"] = None  # later violation
        result = parse(json.dumps(document))
        assert not result.ok
        assert result.error.path == "/nodes/0/id"

    def test_duplicate_node_id_reported_with_pointer(self):
        document = _sample_document()
        document["nodes"][1]["id"] = "n1"
        document["connections"] = []
        result = parse(json.dumps(document))
        assert not result.ok
        assert result.error.code == ERROR_DUPLICATE_ID
        assert result.error.path == "/nodes/1/id"

    def test_connection_to_unknown_node_reported_with_pointer(self):
        document = _sample_document()
        document["connections"][0]["to"]["node"] = "ghost"
        result = parse(json.dumps(document))
        assert not result.ok
        assert result.error.code == ERROR_UNKNOWN_NODE_REFERENCE
        assert result.error.path == "/connections/0/to/node"
        assert "ghost" in result.error.message

    def test_parse_never_returns_graph_and_error_together(self):
        for doc in ["", "null", "[]", '{"schemaVersion": 1}']:
            result = parse(doc)
            assert result.graph is None
            assert result.error is not None


class TestSchemaVersionHandling:
    def test_future_version_is_unsupported(self):
        document = _sample_document()
        document["schemaVersion"] = SCHEMA_VERSION + 1
        result = parse(json.dumps(document))
        assert not result.ok
        assert result.error.code == ERROR_UNSUPPORTED_SCHEMA_VERSION
        assert result.error.path == "/schemaVersion"

    def test_unknown_older_version_is_unsupported(self):
        document = _sample_document()
        document["schemaVersion"] = 0
        result = parse(json.dumps(document))
        assert not result.ok
        assert result.error.code == ERROR_UNSUPPORTED_SCHEMA_VERSION

    def test_non_integer_version_is_rejected(self):
        document = _sample_document()
        document["schemaVersion"] = "1"
        result = parse(json.dumps(document))
        assert not result.ok
        assert result.error.code in (
            ERROR_SCHEMA_VIOLATION,
            ERROR_UNSUPPORTED_SCHEMA_VERSION,
        )

    def test_boolean_version_is_rejected(self):
        document = _sample_document()
        document["schemaVersion"] = True
        result = parse(json.dumps(document))
        assert not result.ok


@pytest.fixture
def v0_migration():
    """Register a synthetic v0 -> v1 migration for the duration of a test.

    Version 0 documents use 'edges' instead of 'connections'; the
    migration renames the key.
    """
    v0_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["schemaVersion", "nodes", "edges"],
        "additionalProperties": False,
        "properties": {
            "schemaVersion": {"const": 0},
            "nodes": {"type": "array"},
            "edges": {"type": "array"},
        },
    }

    def upgrade(document):
        return {
            "schemaVersion": 1,
            "nodes": document["nodes"],
            "connections": document["edges"],
        }

    register_migration(0, v0_schema, upgrade)
    yield
    unregister_migration(0)


class TestMigration:
    def test_older_supported_version_migrates_and_reports(self, v0_migration):
        """Requirement 3.5: older supported documents migrate to current
        and the parse result reports the migration."""
        current = _sample_document()
        v0_document = {
            "schemaVersion": 0,
            "nodes": current["nodes"],
            "edges": current["connections"],
        }
        result = parse(json.dumps(v0_document))
        assert result.ok
        assert result.migrations == [0, SCHEMA_VERSION]
        assert result.graph.is_equivalent_to(_sample_graph())

    def test_old_document_validated_against_its_own_schema(self, v0_migration):
        # 'connections' is not a valid key at v0 (schema has 'edges').
        result = parse(json.dumps({"schemaVersion": 0, "nodes": [], "connections": []}))
        assert not result.ok
        assert result.error.code == ERROR_SCHEMA_VIOLATION

    def test_failing_migration_reports_migration_failed(self):
        def broken(document):
            raise RuntimeError("boom")

        register_migration(
            0,
            {"type": "object", "properties": {"schemaVersion": {"const": 0}}},
            broken,
        )
        try:
            result = parse(json.dumps({"schemaVersion": 0}))
            assert not result.ok
            assert result.error.code == ERROR_MIGRATION_FAILED
            assert "boom" in result.error.message
        finally:
            unregister_migration(0)

    def test_migration_producing_invalid_document_is_rejected(self):
        register_migration(
            0,
            {"type": "object", "properties": {"schemaVersion": {"const": 0}}},
            lambda document: {"schemaVersion": 1},  # missing nodes/connections
        )
        try:
            result = parse(json.dumps({"schemaVersion": 0}))
            assert not result.ok
            assert result.error.code == ERROR_MIGRATION_FAILED
        finally:
            unregister_migration(0)

    def test_unregistered_version_stays_unsupported(self, v0_migration):
        document = _sample_document()
        document["schemaVersion"] = -1
        result = parse(json.dumps(document))
        assert not result.ok
        assert result.error.code == ERROR_UNSUPPORTED_SCHEMA_VERSION

    def test_registry_rejects_current_and_duplicate_versions(self, v0_migration):
        with pytest.raises(ValueError, match="older than the current"):
            register_migration(SCHEMA_VERSION, {}, lambda d: d)
        with pytest.raises(ValueError, match="already registered"):
            register_migration(0, {}, lambda d: d)
        assert 0 in registered_migrations()
