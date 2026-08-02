# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Migration safety test for the workflow engine tables (task 13.3).

Requirement 13.5: the Workflow Manager is introduced without requiring
migration or modification of existing data. The alembic migration
``a4f8c2d91e57_create_workflow_engine_tables`` must therefore be
strictly additive.

The test builds a copy of a production-shaped configuration database:

1. Apply every migration up to the previous head (``b2f1a9c4d7e3``) on a
   fresh sqlite file through the REAL alembic machinery (the repo's
   ``alembic.ini`` ``database_configuration`` section, run exactly as on
   a device — a subprocess with ``COMPONENT_WORK_PATH`` locating the db).
2. Seed representative rows into every pre-existing table (image source
   configurations with smart-camera and advanced settings, digital
   input/output configurations, an image source, and a legacy
   Pipeline_Configuration ``workflow`` row).
3. Snapshot the full schema (``sqlite_master``) and all table data.
4. Apply the new workflow engine migration and assert: every
   pre-existing table/index definition is byte-identical, all seeded
   data is intact, and the only additions are the two new tables (plus
   their indexes).
"""
import json
import os
import sqlite3
import subprocess
import sys

import pytest

BACKEND_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "backend")
)
DB_FILENAME = "dda_backend_app.db"

#: Head before the workflow engine migration (add advanced camera settings).
PREVIOUS_HEAD = "b2f1a9c4d7e3"
#: The migration under test (create workflow engine tables).
WORKFLOW_ENGINE_REVISION = "a4f8c2d91e57"

NEW_TABLES = {"workflow_registrations", "workflow_executions"}
NEW_NAMED_INDEXES = {
    "ix_workflow_registrations_id",
    "ix_workflow_registrations_workflow_id",
    "ix_workflow_executions_id",
    "ix_workflow_executions_registration_id",
}


def run_alembic_upgrade(work_path, revision):
    """Upgrade the configuration database exactly as production does:
    the repo alembic.ini's ``database_configuration`` section, with
    ``COMPONENT_WORK_PATH`` resolving the sqlite file location."""
    env = dict(os.environ, COMPONENT_WORK_PATH=str(work_path))
    # The suite conftest points PYTHONHOME at the running interpreter for
    # Triton's python backend; a spawned CPython would fail its own
    # bootstrap with it set (same stripping python_bridge does).
    env.pop("PYTHONHOME", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic.ini",
            "-n",
            "database_configuration",
            "upgrade",
            revision,
        ],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    assert result.returncode == 0, (
        "alembic upgrade {0} failed:\n{1}\n{2}".format(
            revision, result.stdout, result.stderr
        )
    )


def seed_production_data(db_path):
    """Representative rows in every table a production device carries at
    the previous head, foreign keys wired the way LocalServer writes them."""
    connection = sqlite3.connect(str(db_path))
    try:
        with connection:
            connection.execute(
                "INSERT INTO image_source_configuration "
                "(imageSourceConfigId, gain, exposure, processingPipeline, "
                "creationTime, imageCrop, device, deviceName, advancedSettings) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "isc-1",
                    12,
                    20000,
                    "videoconvert ! videoscale",
                    1700000000,
                    json.dumps({"top": 0, "left": 0, "width": 1920, "height": 1080}),
                    "GenICam",
                    "Basler acA1920",
                    json.dumps(
                        {"reverseX": True, "reverseY": False,
                         "balanceWhiteAuto": "Continuous"}
                    ),
                ),
            )
            connection.execute(
                "INSERT INTO image_source "
                "(imageSourceId, name, type, location, cameraId, description, "
                "creationTime, lastUpdateTime, imageCapturePath, imageSourceConfigId) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "is-1",
                    "line-3 camera",
                    "camera",
                    None,
                    "cam-0042",
                    "inspection camera on line 3",
                    1700000001,
                    1700000500,
                    "/aws_dda/captures/line-3",
                    "isc-1",
                ),
            )
            connection.execute(
                "INSERT INTO input_configuration "
                "(inputConfigurationId, creationTime, pin, triggerState, "
                "debounceTime) VALUES (?, ?, ?, ?, ?)",
                ("in-1", 1700000002, "7", "GPIO.RISING", 50),
            )
            connection.execute(
                "INSERT INTO output_configuration "
                "(outputConfigurationId, pin, signalType, pulseWidth, "
                "creationTime, rule) VALUES (?, ?, ?, ?, ?, ?)",
                ("out-1", "245", "GPIO.FALLING", 100, 1700000003, "anomaly"),
            )
            connection.execute(
                "INSERT INTO workflow "
                "(workflowId, name, description, creationTime, lastUpdatedTime, "
                "workflowOutputPath, featureConfigurations, inputConfigurations, "
                "outputConfigurations, imageSourceId) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-wf-1",
                    "line-3 anomaly detection",
                    "existing Pipeline_Configuration - must never be touched",
                    1700000004,
                    1700000600,
                    "/aws_dda/inference_results/legacy-wf-1",
                    json.dumps([{"modelName": "widget-anomaly-v3",
                                 "confidenceThreshold": 0.8}]),
                    json.dumps(["in-1"]),
                    json.dumps(["out-1"]),
                    "is-1",
                ),
            )
    finally:
        connection.close()


def snapshot_schema(db_path):
    """Every schema object exactly as sqlite stores it: (type, name) ->
    (tbl_name, sql). Byte-identical ``sql`` text means the definition
    was not touched."""
    connection = sqlite3.connect(str(db_path))
    try:
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
        ).fetchall()
    finally:
        connection.close()
    return {(r[0], r[1]): (r[2], r[3]) for r in rows}


def snapshot_data(db_path, tables):
    """name -> ordered list of full rows for the given tables."""
    connection = sqlite3.connect(str(db_path))
    try:
        return {
            table: connection.execute(
                'SELECT * FROM "{0}" ORDER BY rowid'.format(table)
            ).fetchall()
            for table in tables
        }
    finally:
        connection.close()


def alembic_version(db_path):
    connection = sqlite3.connect(str(db_path))
    try:
        rows = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall()
    finally:
        connection.close()
    return [r[0] for r in rows]


@pytest.fixture(scope="module")
def migration_run(tmp_path_factory):
    """Build the production-shaped copy, snapshot it, apply the workflow
    engine migration, snapshot again."""
    work_path = tmp_path_factory.mktemp("production_db_copy")
    db_path = work_path / DB_FILENAME

    run_alembic_upgrade(work_path, PREVIOUS_HEAD)
    assert db_path.exists(), "alembic did not create the configuration db"
    seed_production_data(db_path)

    before_schema = snapshot_schema(db_path)
    preexisting_tables = sorted(
        name
        for (kind, name) in before_schema
        if kind == "table" and name != "alembic_version"
    )
    before_data = snapshot_data(db_path, preexisting_tables)

    run_alembic_upgrade(work_path, WORKFLOW_ENGINE_REVISION)

    return {
        "db_path": db_path,
        "before_schema": before_schema,
        "after_schema": snapshot_schema(db_path),
        "preexisting_tables": preexisting_tables,
        "before_data": before_data,
        "after_data": snapshot_data(db_path, preexisting_tables),
    }


class TestWorkflowEngineMigrationIsAdditiveOnly:
    def test_preexisting_schema_definitions_are_byte_identical(
        self, migration_run
    ):
        before = migration_run["before_schema"]
        after = migration_run["after_schema"]
        for key, definition in before.items():
            assert key in after, (
                "migration dropped schema object {0}".format(key)
            )
            assert after[key] == definition, (
                "migration modified schema object {0}:\n"
                "before: {1}\nafter:  {2}".format(key, definition, after[key])
            )

    def test_only_the_workflow_engine_tables_and_indexes_are_added(
        self, migration_run
    ):
        added = {
            key: value
            for key, value in migration_run["after_schema"].items()
            if key not in migration_run["before_schema"]
        }

        # Every addition belongs to the two new tables (their sqlite
        # autoindexes included) — nothing was attached to existing tables.
        for (kind, name), (tbl_name, _) in added.items():
            assert tbl_name in NEW_TABLES, (
                "migration added unexpected {0} '{1}' on table '{2}'".format(
                    kind, name, tbl_name
                )
            )

        added_tables = {name for (kind, name) in added if kind == "table"}
        assert added_tables == NEW_TABLES

        added_named_indexes = {
            name
            for (kind, name) in added
            if kind == "index" and not name.startswith("sqlite_autoindex")
        }
        assert added_named_indexes == NEW_NAMED_INDEXES

    def test_seeded_production_data_is_intact(self, migration_run):
        assert migration_run["preexisting_tables"] == [
            "image_source",
            "image_source_configuration",
            "input_configuration",
            "output_configuration",
            "workflow",
        ]
        for table in migration_run["preexisting_tables"]:
            assert (
                migration_run["after_data"][table]
                == migration_run["before_data"][table]
            ), "migration changed data in table '{0}'".format(table)
            assert migration_run["before_data"][table], (
                "test setup seeded no rows into '{0}'".format(table)
            )

    def test_version_advances_to_the_workflow_engine_revision(
        self, migration_run
    ):
        assert alembic_version(migration_run["db_path"]) == [
            WORKFLOW_ENGINE_REVISION
        ]


# --------------------------------------------------------------------------- #
# Observability-columns migration idempotency (deployed-workflow-run-          #
# observability). The next build's startup ``alembic upgrade head`` must never #
# break a device — including a device that already carries the observability   #
# columns (e.g. after a soft-deploy that applied this migration, or any state  #
# drift where the columns exist but the version pointer is behind).            #
# --------------------------------------------------------------------------- #

#: The observability-columns migration head.
OBSERVABILITY_REVISION = "d3a7b1e94f26"

#: The columns it adds to workflow_executions.
OBSERVABILITY_COLUMNS = {
    "capture_id",
    "output_dir",
    "has_image_results",
    "node_status_json",
    "log_path",
}


def run_alembic_stamp(work_path, revision):
    """Move the alembic version pointer WITHOUT running any migration body
    (``alembic stamp``), exactly as production alembic would — used to
    reproduce a device whose columns exist but whose recorded revision is
    behind the migration head."""
    env = dict(os.environ, COMPONENT_WORK_PATH=str(work_path))
    env.pop("PYTHONHOME", None)
    result = subprocess.run(
        [
            sys.executable, "-m", "alembic", "-c", "alembic.ini",
            "-n", "database_configuration", "stamp", revision,
        ],
        cwd=BACKEND_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )
    assert result.returncode == 0, (
        "alembic stamp {0} failed:\n{1}\n{2}".format(
            revision, result.stdout, result.stderr
        )
    )


def _columns(db_path, table):
    connection = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in connection.execute(
            'PRAGMA table_info("{0}")'.format(table)
        )}
    finally:
        connection.close()


def test_observability_migration_applies_columns_from_previous_head(tmp_path_factory):
    """A device at the pre-observability head upgrades cleanly to the
    observability head, gaining exactly the five nullable columns."""
    work_path = tmp_path_factory.mktemp("obs_fresh")
    db_path = work_path / DB_FILENAME
    run_alembic_upgrade(work_path, WORKFLOW_ENGINE_REVISION)
    seed_production_data(db_path)
    assert not (OBSERVABILITY_COLUMNS & _columns(db_path, "workflow_executions"))

    run_alembic_upgrade(work_path, OBSERVABILITY_REVISION)

    assert OBSERVABILITY_COLUMNS <= _columns(db_path, "workflow_executions")
    assert alembic_version(db_path) == [OBSERVABILITY_REVISION]
    # Seeded legacy Pipeline_Configuration row is untouched (Requirement 13.5).
    rows = snapshot_data(db_path, ["workflow"])["workflow"]
    assert len(rows) == 1


def test_observability_migration_is_idempotent_when_columns_already_exist(
    tmp_path_factory,
):
    """The exact deploy risk this guards: the columns already exist but the
    recorded revision is behind (a soft-deployed device / state drift). A
    plain ``add_column`` would fail with "duplicate column name" and break
    the deploy; the guarded upgrade must succeed as a safe no-op."""
    work_path = tmp_path_factory.mktemp("obs_drift")
    db_path = work_path / DB_FILENAME
    # Columns present (upgraded to head)...
    run_alembic_upgrade(work_path, OBSERVABILITY_REVISION)
    assert OBSERVABILITY_COLUMNS <= _columns(db_path, "workflow_executions")
    # ...but the version pointer rewound to before this migration.
    run_alembic_stamp(work_path, WORKFLOW_ENGINE_REVISION)
    assert alembic_version(db_path) == [WORKFLOW_ENGINE_REVISION]

    # Re-running the deploy's upgrade must NOT raise (idempotent guard) and
    # must land back at head with the columns intact.
    run_alembic_upgrade(work_path, OBSERVABILITY_REVISION)
    assert alembic_version(db_path) == [OBSERVABILITY_REVISION]
    assert OBSERVABILITY_COLUMNS <= _columns(db_path, "workflow_executions")
