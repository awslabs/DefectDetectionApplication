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
"""The ``streamSettings`` migration on a copy of a device database
(rtsp-rtmp-stream-cameras task 15.5 — Requirements 4.1, 18.2).

``f4b7c2e91a3d`` must be strictly additive: a device database at the
previous head, carrying production-shaped rows, upgrades through the REAL
alembic machinery (the repo ``alembic.ini``, run as a subprocess exactly as
the LocalServer does at startup) and afterwards:

- has the one new nullable column, and every other table definition is
  byte-identical;
- keeps every row, with ``streamSettings`` NULL;
- agrees with the ORM model, which reads and writes the column;
- survives a re-run on a device whose column exists but whose recorded
  revision is behind (the idempotency guard), and downgrades cleanly.
"""
import json
import os
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# The DAO package reads COMPONENT_WORK_PATH at import.
os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
import dao.sqlite_db.models as db_models  # noqa: E402

BACKEND_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "backend"))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "alembic", "configuration_database", "versions")
DB_FILENAME = "dda_backend_app.db"

PREVIOUS_HEAD = "e9f2a6c31b84"
STREAM_SETTINGS_REVISION = "f4b7c2e91a3d"
TABLE = "image_source_configuration"
COLUMN = "streamSettings"


def _alembic(work_path, *arguments):
    env = dict(os.environ, COMPONENT_WORK_PATH=str(work_path))
    # The suite conftest may point PYTHONHOME at the running interpreter
    # for Triton's python backend; a spawned CPython fails with it set.
    env.pop("PYTHONHOME", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "-n",
         "database_configuration", *arguments],
        cwd=BACKEND_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True)
    assert result.returncode == 0, "alembic {} failed:\n{}\n{}".format(
        " ".join(arguments), result.stdout, result.stderr)
    return result.stdout


def _query(db_path, sql, parameters=()):
    connection = sqlite3.connect(str(db_path))
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def _schema(db_path):
    return {(kind, name): (table, sql) for kind, name, table, sql in
            _query(db_path, "SELECT type, name, tbl_name, sql FROM sqlite_master")}


def _columns(db_path, table=TABLE):
    return {row[1]: row for row in _query(db_path, f'PRAGMA table_info("{table}")')}


def _data(db_path, tables):
    return {table: _query(db_path, f'SELECT * FROM "{table}" ORDER BY rowid') for table in tables}


def _version(db_path):
    return [row[0] for row in _query(db_path, "SELECT version_num FROM alembic_version")]


def _seed(db_path):
    """Rows a device carries at the previous head: a GenICam camera with
    advanced settings, a folder source, a classic workflow and a
    registration with an execution."""
    connection = sqlite3.connect(str(db_path))
    try:
        with connection:
            connection.executemany(
                "INSERT INTO image_source_configuration (imageSourceConfigId, gain, exposure, "
                "processingPipeline, creationTime, imageCrop, device, deviceName, advancedSettings) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [("isc-1", 12, 20000, "videoconvert ! videoscale", 1700000000,
                  json.dumps({"top": 0, "left": 0, "width": 1920, "height": 1080}),
                  "GenICam", "Basler acA1920", json.dumps({"reverseX": True})),
                 ("isc-2", 0, 0, "", 1700000010, None, None, None, None)])
            connection.executemany(
                "INSERT INTO image_source (imageSourceId, name, type, location, cameraId, "
                "description, creationTime, lastUpdateTime, imageCapturePath, imageSourceConfigId) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [("is-1", "line-3 camera", "Camera", None, "cam-0042", "line 3",
                  1700000001, 1700000500, "/aws_dda/captures/line-3", "isc-1"),
                 ("is-2", "sample images", "Folder", "/aws_dda/images/samples", None, None,
                  1700000011, 1700000011, "/aws_dda/captures/samples", "isc-2")])
            connection.execute(
                "INSERT INTO workflow (workflowId, name, description, creationTime, lastUpdatedTime, "
                "workflowOutputPath, featureConfigurations, inputConfigurations, "
                "outputConfigurations, imageSourceId) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("legacy-wf-1", "line-3 anomaly detection", "classic workflow", 1700000004,
                 1700000600, "/aws_dda/inference_results/legacy-wf-1",
                 json.dumps([{"modelName": "widget-anomaly-v3"}]), json.dumps([]),
                 json.dumps([]), "is-1"))
    finally:
        connection.close()


@pytest.fixture(scope="module")
def upgraded(tmp_path_factory):
    work_path = tmp_path_factory.mktemp("device_db_copy")
    db_path = work_path / DB_FILENAME
    _alembic(work_path, "upgrade", PREVIOUS_HEAD)
    _seed(db_path)
    before_schema = _schema(db_path)
    tables = sorted(name for kind, name in before_schema
                    if kind == "table" and name != "alembic_version")
    before_data = _data(db_path, tables)
    before_columns = _columns(db_path)
    _alembic(work_path, "upgrade", STREAM_SETTINGS_REVISION)
    return {"work_path": work_path, "db_path": db_path, "tables": tables,
            "before_schema": before_schema, "after_schema": _schema(db_path),
            "before_data": before_data, "after_data": _data(db_path, tables),
            "before_columns": before_columns, "after_columns": _columns(db_path)}


class TestUpgrade:
    def test_the_revision_chain_has_one_head_after_the_previous_one(self, tmp_path):
        heads = _alembic(tmp_path, "heads").split()
        assert len([token for token in heads if token != "(head)"]) == 1
        history = _alembic(tmp_path, "history")
        assert f"{PREVIOUS_HEAD} -> {STREAM_SETTINGS_REVISION}" in history

    def test_only_the_stream_settings_column_is_added(self, upgraded):
        added = set(upgraded["after_columns"]) - set(upgraded["before_columns"])
        assert added == {COLUMN}
        _, name, declared_type, not_null, default, primary_key = upgraded["after_columns"][COLUMN]
        assert (declared_type, not_null, default, primary_key) == ("JSON", 0, None, 0)
        for name, column in upgraded["before_columns"].items():
            assert upgraded["after_columns"][name] == column

    def test_every_other_schema_object_is_byte_identical(self, upgraded):
        before, after = upgraded["before_schema"], upgraded["after_schema"]
        assert set(after) == set(before)
        for key, (table, sql) in before.items():
            if key == ("table", TABLE):
                continue
            assert after[key] == (table, sql), key

    def test_every_row_is_intact_and_new_values_are_null(self, upgraded):
        before, after = upgraded["before_data"], upgraded["after_data"]
        for table in upgraded["tables"]:
            if table == TABLE:
                continue
            assert after[table] == before[table], table
        column_index = list(upgraded["after_columns"]).index(COLUMN)
        for old_row, new_row in zip(before[TABLE], after[TABLE]):
            assert new_row[:column_index] + new_row[column_index + 1:] == old_row
            assert new_row[column_index] is None
        assert len(after[TABLE]) == len(before[TABLE]) == 2

    def test_the_version_advances(self, upgraded):
        assert _version(upgraded["db_path"]) == [STREAM_SETTINGS_REVISION]

    def test_the_orm_model_matches_and_round_trips_the_column(self, upgraded, tmp_path):
        model_columns = {column.name for column in db_models.ImageSourceConfiguration.__table__.columns}
        assert model_columns == set(upgraded["after_columns"])

        copy_path = tmp_path / DB_FILENAME
        copy_path.write_bytes(upgraded["db_path"].read_bytes())
        engine = create_engine(f"sqlite:///{copy_path}")
        session = sessionmaker(bind=engine)()
        try:
            row = session.get(db_models.ImageSourceConfiguration, "isc-2")
            assert row.streamSettings is None
            row.streamSettings = {"transport": "tcp", "latencyMs": 200, "decoder": "auto",
                                  "maxFrameDimension": 1920, "stallTimeoutS": 10}
            session.commit()
        finally:
            session.close()
            engine.dispose()
        stored = _query(copy_path, f'SELECT "{COLUMN}" FROM {TABLE} WHERE imageSourceConfigId = ?',
                        ("isc-2",))
        assert json.loads(stored[0][0])["latencyMs"] == 200


class TestRerunAndDowngrade:
    def test_a_rerun_with_the_column_present_is_a_no_op(self, tmp_path):
        db_path = tmp_path / DB_FILENAME
        _alembic(tmp_path, "upgrade", STREAM_SETTINGS_REVISION)
        _seed(db_path)
        # The column exists but the recorded revision is behind it.
        _alembic(tmp_path, "stamp", PREVIOUS_HEAD)
        schema = _schema(db_path)

        _alembic(tmp_path, "upgrade", STREAM_SETTINGS_REVISION)

        assert _version(db_path) == [STREAM_SETTINGS_REVISION]
        assert _schema(db_path) == schema
        assert COLUMN in _columns(db_path)

    def test_a_downgrade_removes_only_the_column_and_keeps_the_rows(self, tmp_path):
        db_path = tmp_path / DB_FILENAME
        _alembic(tmp_path, "upgrade", PREVIOUS_HEAD)
        _seed(db_path)
        before = _data(db_path, [TABLE, "image_source", "workflow"])
        columns = _columns(db_path)
        _alembic(tmp_path, "upgrade", STREAM_SETTINGS_REVISION)

        _alembic(tmp_path, "downgrade", PREVIOUS_HEAD)

        assert _version(db_path) == [PREVIOUS_HEAD]
        assert set(_columns(db_path)) == set(columns)
        assert _data(db_path, [TABLE, "image_source", "workflow"]) == before
