"""Unit tests for schema migration fixtures.

Covers task 2.6 of the workflow-manager spec: fixture documents per
registered migration, asserting the migrated output and the reported
migration path (``ParseResult.migrations == [from, to]``).

Two layers of coverage:

1. A fixtures-driven test parameterized over ``registered_migrations()``
   captured at import time. The current schema version is 1 and no
   production migrations exist yet, so the parameterized test is empty
   today; when a production migration is registered, the guard test
   fails until a fixture document is added to
   ``PRODUCTION_MIGRATION_FIXTURES``, keeping future migrations
   automatically covered.
2. Explicit synthetic fixtures exercising the migration machinery now:
   a single-step v0 -> v1 upgrade and a multi-step v-1 -> v0 -> v1 path,
   each asserting the migrated document content and the reported path.

Synthetic migrations registered here are always cleaned up with
``unregister_migration`` so concurrent test files stay isolated.

_Requirements: 3.5_
"""

import json

import pytest

from workflow_core.serializer import (
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

# ---------------------------------------------------------------------------
# Fixtures-driven coverage of registered (production) migrations
# ---------------------------------------------------------------------------

#: Fixture documents per registered production migration, keyed by
#: from-version. Each entry maps a from-version to a document at that
#: version and the canonical document expected after migration to the
#: current schema version. Add an entry here for every migration
#: registered in workflow_core itself.
PRODUCTION_MIGRATION_FIXTURES = {
    # from_version: {"document": {...}, "expected_document": {...}},
}

#: Production migrations visible at import time, before any test
#: registers synthetic migrations.
_PRODUCTION_MIGRATIONS = sorted(registered_migrations())


def test_every_production_migration_has_a_fixture():
    """Every migration registered by workflow_core has a fixture document.

    Fails when a new production migration lands without a corresponding
    fixture, so future migrations are automatically covered here.
    """
    missing = set(_PRODUCTION_MIGRATIONS) - set(PRODUCTION_MIGRATION_FIXTURES)
    assert not missing, (
        "registered migrations without fixture documents: {}; add entries "
        "to PRODUCTION_MIGRATION_FIXTURES in {}".format(sorted(missing), __file__)
    )


def test_no_stale_production_fixtures():
    stale = set(PRODUCTION_MIGRATION_FIXTURES) - set(_PRODUCTION_MIGRATIONS)
    assert not stale, "fixtures for unregistered migrations: {}".format(sorted(stale))


@pytest.mark.parametrize("from_version", _PRODUCTION_MIGRATIONS)
def test_production_migration_fixture(from_version):
    """Requirement 3.5: each registered migration upgrades its fixture
    document to the current version and reports the migration path."""
    fixture = PRODUCTION_MIGRATION_FIXTURES[from_version]
    result = parse(json.dumps(fixture["document"]))
    assert result.ok, result.error
    assert result.migrations == [from_version, SCHEMA_VERSION]
    assert json.loads(serialize(result.graph)) == fixture["expected_document"]


# ---------------------------------------------------------------------------
# Synthetic single-step migration: v0 -> v1
# ---------------------------------------------------------------------------

# Version 0 documents store node positions as [x, y] arrays and allow
# omitting "parameters"; the migration converts positions to objects and
# fills missing parameters with {}.
_V0_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["schemaVersion", "nodes", "connections"],
    "additionalProperties": False,
    "properties": {
        "schemaVersion": {"const": 0},
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "type", "position"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "type": {"type": "string", "minLength": 1},
                    "position": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 2,
                        "items": {"type": "number"},
                    },
                    "parameters": {"type": "object"},
                },
            },
        },
        "connections": {"type": "array"},
    },
}


def _upgrade_v0_to_v1(document):
    return {
        "schemaVersion": 1,
        "nodes": [
            {
                "id": node["id"],
                "type": node["type"],
                "position": {"x": node["position"][0], "y": node["position"][1]},
                "parameters": node.get("parameters", {}),
            }
            for node in document["nodes"]
        ],
        "connections": document["connections"],
    }


#: A v0 fixture document exercising both migration behaviors: array
#: positions and an omitted "parameters" key (node n2).
V0_FIXTURE_DOCUMENT = {
    "schemaVersion": 0,
    "nodes": [
        {
            "id": "n1",
            "type": "icam_source",
            "position": [100, 200],
            "parameters": {"device": "/dev/video0", "gain": 4},
        },
        {
            "id": "n2",
            "type": "capture",
            "position": [400, 200],
        },
    ],
    "connections": [
        {"id": "c1", "from": {"node": "n1", "port": "out"}, "to": {"node": "n2", "port": "in"}},
    ],
}


def _v0_expected_graph():
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
                type="capture",
                position=Position(x=400, y=200),
                parameters={},
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


@pytest.fixture
def v0_migration():
    """Register the synthetic v0 -> v1 migration; always unregister."""
    register_migration(0, _V0_SCHEMA, _upgrade_v0_to_v1)
    try:
        yield
    finally:
        unregister_migration(0)


class TestSingleStepMigrationFixture:
    def test_migrated_output_matches_expected_document(self, v0_migration):
        """Requirement 3.5: the v0 fixture migrates to the documented
        v1 content (positions converted, missing parameters filled)."""
        result = parse(json.dumps(V0_FIXTURE_DOCUMENT))
        assert result.ok, result.error
        assert result.graph.is_equivalent_to(_v0_expected_graph())
        assert serialize(result.graph) == serialize(_v0_expected_graph())

    def test_migration_path_is_reported(self, v0_migration):
        """Requirement 3.5: the parse result reports [from, to]."""
        result = parse(json.dumps(V0_FIXTURE_DOCUMENT))
        assert result.ok, result.error
        assert result.migrations == [0, SCHEMA_VERSION]

    def test_migrated_document_round_trips_without_further_migration(self, v0_migration):
        """The migrated graph serializes to a current-version document
        that parses cleanly with no migration reported."""
        migrated = parse(json.dumps(V0_FIXTURE_DOCUMENT)).graph
        result = parse(serialize(migrated))
        assert result.ok, result.error
        assert result.migrations is None
        assert result.graph.is_equivalent_to(migrated)


# ---------------------------------------------------------------------------
# Synthetic multi-step migration: v-1 -> v0 -> v1
# ---------------------------------------------------------------------------

# Version -1 documents use "vertices" instead of "nodes"; the -1 -> 0
# migration renames the key, and the registered v0 -> v1 migration
# completes the path.
_V_MINUS_1_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["schemaVersion", "vertices", "connections"],
    "additionalProperties": False,
    "properties": {
        "schemaVersion": {"const": -1},
        "vertices": {"type": "array"},
        "connections": {"type": "array"},
    },
}


V_MINUS_1_FIXTURE_DOCUMENT = {
    "schemaVersion": -1,
    "vertices": V0_FIXTURE_DOCUMENT["nodes"],
    "connections": V0_FIXTURE_DOCUMENT["connections"],
}


@pytest.fixture
def multi_step_migrations():
    """Register v-1 -> v0 and v0 -> v1; record the steps applied."""
    applied = []

    def upgrade_v_minus_1_to_v0(document):
        applied.append(-1)
        return {
            "schemaVersion": 0,
            "nodes": document["vertices"],
            "connections": document["connections"],
        }

    def upgrade_v0_to_v1_recording(document):
        applied.append(0)
        return _upgrade_v0_to_v1(document)

    register_migration(-1, _V_MINUS_1_SCHEMA, upgrade_v_minus_1_to_v0)
    try:
        register_migration(0, _V0_SCHEMA, upgrade_v0_to_v1_recording)
        try:
            yield applied
        finally:
            unregister_migration(0)
    finally:
        unregister_migration(-1)


class TestMultiStepMigrationFixture:
    def test_multi_step_path_migrates_and_reports_endpoints(self, multi_step_migrations):
        """Requirement 3.5: a v-1 document upgrades stepwise to the
        current version and the result reports [-1, current]."""
        result = parse(json.dumps(V_MINUS_1_FIXTURE_DOCUMENT))
        assert result.ok, result.error
        assert result.migrations == [-1, SCHEMA_VERSION]
        assert result.graph.is_equivalent_to(_v0_expected_graph())

    def test_steps_apply_in_ascending_version_order(self, multi_step_migrations):
        applied = multi_step_migrations
        result = parse(json.dumps(V_MINUS_1_FIXTURE_DOCUMENT))
        assert result.ok, result.error
        assert applied == [-1, 0]

    def test_incomplete_migration_chain_is_unsupported(self):
        """A version with a registered step but a gap in the chain to
        the current version is reported as unsupported, not migrated."""
        register_migration(-1, _V_MINUS_1_SCHEMA, lambda document: document)
        try:
            # v0 -> v1 is NOT registered, so -1 has no complete path.
            result = parse(json.dumps(V_MINUS_1_FIXTURE_DOCUMENT))
            assert not result.ok
            assert result.error.code == ERROR_UNSUPPORTED_SCHEMA_VERSION
            assert result.migrations is None
        finally:
            unregister_migration(-1)
