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
"""Tests for the workflow-graph and node-status endpoints and their
run-artifact resolution helpers (Requirements 4.4, 4.5, 4.6).

Uses the same standalone FastAPI app + in-memory database pattern as
``test_workflow_run_results_api.py`` / ``test_workflow_run_log_api.py`` so
the tests run without GStreamer, the full LocalServer app, or a real
/aws_dda tree.

- ``GET .../registrations/{id}/graph`` returns the parsed ``workflow.json``
  from the registration's ``artifact_path``; 404 for an unknown
  registration and for a missing/malformed ``workflow.json`` (never 500).
- ``GET .../executions/{id}/node-status`` returns the
  ``{nodeId:{status,detail?}}`` map from ``node_status_json``; ``{}`` when
  ``None``/empty/malformed; 404 for an unknown execution.
"""
import json
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


_GRAPH = {
    "schemaVersion": 1,
    "nodes": [
        {"id": "n1", "type": "camera", "position": {"x": 0, "y": 0}},
        {"id": "n2", "type": "capture", "position": {"x": 200, "y": 40}},
    ],
    "connections": [{"from": "n1", "to": "n2"}],
}


def _seed_registration(session_factory, registration_id="wf-1:3", artifact_path=None):
    session = session_factory()
    try:
        if session.get(WorkflowRegistration, registration_id) is None:
            session.add(
                WorkflowRegistration(
                    id=registration_id,
                    workflow_id=registration_id.split(":")[0],
                    version=registration_id.split(":")[1],
                    arch="x86_64",
                    artifact_path=artifact_path
                    or f"/aws_dda/workflows/{registration_id}",
                    status="registered",
                    registered_at=int(time.time()),
                )
            )
        session.commit()
    finally:
        session.close()


def _seed_execution(
    session_factory,
    execution_id="exec-1",
    registration_id="wf-1:3",
    status="completed",
    node_status_json=None,
):
    _seed_registration(session_factory, registration_id=registration_id)
    session = session_factory()
    try:
        session.add(
            WorkflowExecution(
                id=execution_id,
                registration_id=registration_id,
                started_at=int(time.time()),
                finished_at=int(time.time()),
                status=status,
                node_status_json=node_status_json,
            )
        )
        session.commit()
    finally:
        session.close()


class TestRegistrationGraphEndpoint:
    def test_unknown_registration_404(self, client):
        response = client.get("/workflows/registrations/nope/graph")
        assert response.status_code == 404

    def test_graph_returned_from_workflow_json(
        self, client, session_factory, tmp_path
    ):
        artifact_path = str(tmp_path)
        with open(os.path.join(artifact_path, "workflow.json"), "w") as graph_file:
            json.dump(_GRAPH, graph_file)
        _seed_registration(session_factory, artifact_path=artifact_path)

        response = client.get("/workflows/registrations/wf-1:3/graph")
        assert response.status_code == 200
        assert response.json() == _GRAPH

    def test_missing_workflow_json_404(self, client, session_factory, tmp_path):
        # Registration exists, but its artifact set has no workflow.json.
        _seed_registration(session_factory, artifact_path=str(tmp_path))
        response = client.get("/workflows/registrations/wf-1:3/graph")
        assert response.status_code == 404

    def test_malformed_workflow_json_404(
        self, client, session_factory, tmp_path
    ):
        artifact_path = str(tmp_path)
        with open(os.path.join(artifact_path, "workflow.json"), "w") as graph_file:
            graph_file.write("{not valid json")
        _seed_registration(session_factory, artifact_path=artifact_path)
        response = client.get("/workflows/registrations/wf-1:3/graph")
        assert response.status_code == 404

    def test_non_object_workflow_json_404(
        self, client, session_factory, tmp_path
    ):
        artifact_path = str(tmp_path)
        with open(os.path.join(artifact_path, "workflow.json"), "w") as graph_file:
            json.dump([1, 2, 3], graph_file)
        _seed_registration(session_factory, artifact_path=artifact_path)
        response = client.get("/workflows/registrations/wf-1:3/graph")
        assert response.status_code == 404


class TestNodeStatusEndpoint:
    def test_unknown_execution_404(self, client):
        response = client.get("/workflows/executions/nope/node-status")
        assert response.status_code == 404

    def test_node_status_map_returned(self, client, session_factory):
        node_status = {
            "n1": {"status": "success"},
            "n2": {"status": "failure", "detail": "element error"},
        }
        _seed_execution(
            session_factory, node_status_json=json.dumps(node_status)
        )
        response = client.get("/workflows/executions/exec-1/node-status")
        assert response.status_code == 200
        assert response.json() == node_status

    def test_none_node_status_is_empty_map_200(self, client, session_factory):
        _seed_execution(session_factory, node_status_json=None)
        response = client.get("/workflows/executions/exec-1/node-status")
        assert response.status_code == 200
        assert response.json() == {}

    def test_empty_string_node_status_is_empty_map_200(
        self, client, session_factory
    ):
        _seed_execution(session_factory, node_status_json="")
        response = client.get("/workflows/executions/exec-1/node-status")
        assert response.status_code == 200
        assert response.json() == {}

    def test_malformed_node_status_is_empty_map_200(
        self, client, session_factory
    ):
        _seed_execution(session_factory, node_status_json="{not json")
        response = client.get("/workflows/executions/exec-1/node-status")
        assert response.status_code == 200
        assert response.json() == {}

    def test_non_object_node_status_is_empty_map_200(
        self, client, session_factory
    ):
        _seed_execution(session_factory, node_status_json="[1, 2, 3]")
        response = client.get("/workflows/executions/exec-1/node-status")
        assert response.status_code == 200
        assert response.json() == {}


class TestReadWorkflowGraphHelper:
    """The best-effort resolution the graph endpoint delegates to."""

    def test_none_and_missing_yield_none(self, tmp_path):
        assert run_artifacts.read_workflow_graph(None) is None
        assert run_artifacts.read_workflow_graph("") is None
        assert (
            run_artifacts.read_workflow_graph(str(tmp_path / "nope.json"))
            is None
        )

    def test_parses_object_document(self, tmp_path):
        path = str(tmp_path / "workflow.json")
        with open(path, "w") as graph_file:
            json.dump(_GRAPH, graph_file)
        assert run_artifacts.read_workflow_graph(path) == _GRAPH

    def test_malformed_and_non_object_yield_none(self, tmp_path):
        bad = str(tmp_path / "bad.json")
        with open(bad, "w") as graph_file:
            graph_file.write("{nope")
        assert run_artifacts.read_workflow_graph(bad) is None

        arr = str(tmp_path / "arr.json")
        with open(arr, "w") as graph_file:
            json.dump([1, 2], graph_file)
        assert run_artifacts.read_workflow_graph(arr) is None


class TestParseNodeStatusHelper:
    """The best-effort parse the node-status endpoint delegates to."""

    def test_none_empty_malformed_yield_empty_map(self):
        assert run_artifacts.parse_node_status(None) == {}
        assert run_artifacts.parse_node_status("") == {}
        assert run_artifacts.parse_node_status("{not json") == {}
        assert run_artifacts.parse_node_status("[1, 2, 3]") == {}

    def test_parses_object_map(self):
        node_status = {"n1": {"status": "warning", "detail": "recoverable"}}
        assert (
            run_artifacts.parse_node_status(json.dumps(node_status))
            == node_status
        )
