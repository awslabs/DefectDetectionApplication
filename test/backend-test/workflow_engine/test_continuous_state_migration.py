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
"""The ``workflow_continuous_state`` migration on a device database copy
(rtsp-rtmp-stream-cameras task 20.1; Requirements 11.6, 18.1).

``c7e3a9f15d42`` must be strictly additive: a device database at the
previous head, with a registration and executions, upgrades through the
real alembic machinery (the repo ``alembic.ini``, as a subprocess, exactly
as the LocalServer does at startup) and afterwards has the one new table,
matching the ORM model, with every other schema object and row unchanged.
A re-run over an existing table is a no-op, and a downgrade drops only it.
"""
import json
import os
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")

from workflow_engine.models import WorkflowContinuousState  # noqa: E402

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "backend"))
DB_FILENAME = "dda_backend_app.db"
PREVIOUS_HEAD = "b8d4f0a26c57"
REVISION = "c7e3a9f15d42"
TABLE = "workflow_continuous_state"


def _alembic(work_path, *arguments):
    env = dict(os.environ, COMPONENT_WORK_PATH=str(work_path))
    env.pop("PYTHONHOME", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "-n", "database_configuration", *arguments],
        cwd=BACKEND_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    assert result.returncode == 0, "alembic {} failed:\n{}\n{}".format(" ".join(arguments), result.stdout,
                                                                      result.stderr)
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


def _data(db_path, tables):
    return {table: _query(db_path, f'SELECT * FROM "{table}" ORDER BY rowid') for table in tables}


def _seed(db_path):
    connection = sqlite3.connect(str(db_path))
    try:
        with connection:
            connection.execute(
                "INSERT INTO workflow_registrations (id, workflow_id, version, arch, artifact_path, status, "
                "registered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("wf-1:3", "wf-1", "3", "aarch64", "/aws_dda/workflows/wf-1/3", "registered", 1790000000))
            connection.executemany(
                "INSERT INTO workflow_executions (id, registration_id, started_at, finished_at, status, "
                "trigger_context_json) VALUES (?, ?, ?, ?, ?, ?)",
                [("exec-1", "wf-1:3", 1790000001, 1790000002, "completed", None),
                 ("exec-2", "wf-1:3", 1790000003, 1790000004, "failed", json.dumps({"source": "mqtt"}))])
    finally:
        connection.close()


@pytest.fixture(scope="module")
def upgraded(tmp_path_factory):
    work_path = tmp_path_factory.mktemp("device_db_copy")
    db_path = work_path / DB_FILENAME
    _alembic(work_path, "upgrade", PREVIOUS_HEAD)
    _seed(db_path)
    before_schema = _schema(db_path)
    tables = sorted(name for kind, name in before_schema if kind == "table" and name != "alembic_version")
    before_data = _data(db_path, tables)
    _alembic(work_path, "upgrade", REVISION)
    return {"db_path": db_path, "tables": tables, "before_schema": before_schema,
            "after_schema": _schema(db_path), "before_data": before_data, "after_data": _data(db_path, tables)}


class TestUpgrade:
    def test_the_revision_follows_the_previous_head_and_is_the_only_head(self, tmp_path):
        heads = [token for token in _alembic(tmp_path, "heads").split() if token != "(head)"]
        assert heads == [REVISION]
        assert f"{PREVIOUS_HEAD} -> {REVISION}" in _alembic(tmp_path, "history")

    def test_only_the_new_table_is_added(self, upgraded):
        before, after = upgraded["before_schema"], upgraded["after_schema"]
        added = set(after) - set(before)
        assert {name for kind, name in added if kind == "table"} == {TABLE}
        for key, value in before.items():
            assert after[key] == value, key

    def test_every_row_is_intact(self, upgraded):
        assert upgraded["after_data"] == upgraded["before_data"]
        assert len(upgraded["after_data"]["workflow_executions"]) == 2

    def test_the_table_matches_the_orm_model_and_round_trips(self, upgraded, tmp_path):
        columns = {row[1]: row for row in _query(upgraded["db_path"], f'PRAGMA table_info("{TABLE}")')}
        assert set(columns) == {column.name for column in WorkflowContinuousState.__table__.columns}
        assert columns["registration_id"][5] == 1  # primary key
        assert columns["paused"][3] == 1 and columns["updated_at"][3] == 1  # not null
        assert columns["paused_at"][3] == 0 and columns["counters_json"][3] == 0

        copy_path = tmp_path / DB_FILENAME
        copy_path.write_bytes(upgraded["db_path"].read_bytes())
        engine = create_engine(f"sqlite:///{copy_path}")
        session = sessionmaker(bind=engine)()
        try:
            session.add(WorkflowContinuousState(registration_id="wf-1:3", paused=True, paused_at=1790000000123,
                                                counters_json=json.dumps({"started": 4}), updated_at=1790000001))
            session.commit()
            stored = session.get(WorkflowContinuousState, "wf-1:3")
            assert (stored.paused, stored.paused_at) == (True, 1790000000123)
        finally:
            session.close()
            engine.dispose()


class TestRerunAndDowngrade:
    def test_a_rerun_with_the_table_present_is_a_no_op(self, tmp_path):
        db_path = tmp_path / DB_FILENAME
        _alembic(tmp_path, "upgrade", REVISION)
        _alembic(tmp_path, "stamp", PREVIOUS_HEAD)
        schema = _schema(db_path)

        _alembic(tmp_path, "upgrade", REVISION)

        assert _schema(db_path) == schema
        assert [row[0] for row in _query(db_path, "SELECT version_num FROM alembic_version")] == [REVISION]

    def test_a_downgrade_drops_only_the_table(self, tmp_path):
        db_path = tmp_path / DB_FILENAME
        _alembic(tmp_path, "upgrade", PREVIOUS_HEAD)
        _seed(db_path)
        before = _schema(db_path)
        _alembic(tmp_path, "upgrade", REVISION)

        _alembic(tmp_path, "downgrade", PREVIOUS_HEAD)

        assert _schema(db_path) == before
