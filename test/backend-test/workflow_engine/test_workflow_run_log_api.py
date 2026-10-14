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
"""Tests for the run-log endpoint and the ``read_run_log`` helper
(Requirements 4.3, 4.6, 6.4).

Uses the same standalone FastAPI app + in-memory database pattern as
``test_workflow_engine_api.py`` / ``test_workflow_run_results_api.py`` so
the tests run without GStreamer, the full LocalServer app, or a real
/aws_dda tree. The ``GET .../log`` route is a thin ``PlainTextResponse``
wrapper around the shared, importable ``run_artifacts.read_run_log``
helper; both the route (text / empty-but-200 / 404) and the helper's
best-effort resolution (missing/empty/rotated-backup files) are exercised
here.
"""
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from workflow_engine_test_utils import make_session_factory

from workflow_engine import api as workflow_engine_api
from workflow_engine import run_artifacts
from workflow_engine.models import WorkflowExecution, WorkflowRegistration


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture
def client(session_factory):
    app = FastAPI()
    app.include_router(workflow_engine_api.router)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[workflow_engine_api.get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides = {}


def _seed_execution(
    session_factory,
    execution_id="exec-1",
    registration_id="wf-1:3",
    status="completed",
    log_path=None,
):
    session = session_factory()
    try:
        if session.get(WorkflowRegistration, registration_id) is None:
            session.add(
                WorkflowRegistration(
                    id=registration_id,
                    workflow_id=registration_id.split(":")[0],
                    version=registration_id.split(":")[1],
                    arch="x86_64",
                    artifact_path=f"/aws_dda/workflows/{registration_id}",
                    status="registered",
                    registered_at=int(time.time()),
                )
            )
        session.add(
            WorkflowExecution(
                id=execution_id,
                registration_id=registration_id,
                started_at=int(time.time()),
                finished_at=int(time.time()),
                status=status,
                log_path=log_path,
            )
        )
        session.commit()
    finally:
        session.close()


class TestRunLogEndpoint:
    def test_unknown_execution_404(self, client):
        response = client.get("/workflows/executions/nope/log")
        assert response.status_code == 404

    def test_populated_log_returned_as_text_plain(
        self, client, session_factory, tmp_path
    ):
        log_path = str(tmp_path / "run.log")
        with open(log_path, "w", encoding="utf-8") as log_file:
            log_file.write("starting pipeline: videotestsrc ! fakesink\n")
            log_file.write("execution exec-1 completed\n")
        _seed_execution(session_factory, log_path=log_path)

        response = client.get("/workflows/executions/exec-1/log")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "starting pipeline:" in response.text
        assert "completed" in response.text

    def test_log_path_none_is_empty_200(self, client, session_factory):
        _seed_execution(session_factory, log_path=None)
        response = client.get("/workflows/executions/exec-1/log")
        assert response.status_code == 200
        assert response.text == ""

    def test_missing_log_file_is_empty_200(
        self, client, session_factory, tmp_path
    ):
        # log_path recorded, but the file was never written.
        _seed_execution(
            session_factory, log_path=str(tmp_path / "absent.log")
        )
        response = client.get("/workflows/executions/exec-1/log")
        assert response.status_code == 200
        assert response.text == ""

    def test_empty_log_file_is_empty_200(
        self, client, session_factory, tmp_path
    ):
        log_path = str(tmp_path / "run.log")
        open(log_path, "w", encoding="utf-8").close()
        _seed_execution(session_factory, log_path=log_path)
        response = client.get("/workflows/executions/exec-1/log")
        assert response.status_code == 200
        assert response.text == ""


class TestReadRunLogHelper:
    """The best-effort resolution the endpoint delegates to."""

    def test_none_and_missing_and_empty_yield_empty_string(self, tmp_path):
        assert run_artifacts.read_run_log(None) == ""
        assert run_artifacts.read_run_log("") == ""
        assert run_artifacts.read_run_log(str(tmp_path / "nope.log")) == ""

    def test_live_file_only(self, tmp_path):
        log_path = str(tmp_path / "run.log")
        with open(log_path, "w", encoding="utf-8") as log_file:
            log_file.write("only-live\n")
        assert run_artifacts.read_run_log(log_path) == "only-live\n"

    def test_rotated_backup_precedes_live_file_chronologically(self, tmp_path):
        # RotatingFileHandler keeps the oldest lines in ``.1`` and the newest
        # in the live file; read_run_log concatenates backup then live.
        log_path = str(tmp_path / "run.log")
        with open(log_path + ".1", "w", encoding="utf-8") as backup:
            backup.write("older-lines\n")
        with open(log_path, "w", encoding="utf-8") as live:
            live.write("newer-lines\n")
        assert run_artifacts.read_run_log(log_path) == (
            "older-lines\nnewer-lines\n"
        )
