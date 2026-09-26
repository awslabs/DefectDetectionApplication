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
"""Serving of a run's server-rendered overlay image
(run-detection-visibility Requirements 4.1-4.3, 4.5).

``endpoints/download_file.py`` imports the device-only stack (GStreamer,
Greengrass IPC, panorama), so it cannot be imported on a development host,
and the full-app route tests (``api-endpoints/test_workflows_api.py``) run
only in the device-container suite. Rather than re-typing the route body
here (the ``test_triple_node_image_serving.py`` pattern), this module lifts
the production ``load_workflow_execution_overlay_image`` and
``validate_token_in_query_param`` function definitions out of
``download_file.py`` with ``ast`` and compiles them against the real
``utils.auth``, ``run_artifacts`` and ``WorkflowExecution``. The serving
decision under test is therefore byte-for-byte the shipped code.
"""
import ast
import os
import pathlib
import time

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.responses import FileResponse
from starlette.status import HTTP_404_NOT_FOUND

from workflow_engine_test_utils import make_session_factory

from utils.auth import authorize_credential
from workflow_engine import api as workflow_engine_api
from workflow_engine import run_artifacts
from workflow_engine.models import WorkflowExecution, WorkflowRegistration

_DOWNLOAD_FILE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "src"
    / "backend"
    / "endpoints"
    / "download_file.py"
)
_ROUTE_FUNCTION = "load_workflow_execution_overlay_image"
_ROUTE_PATH = "/workflows/executions/{execution_id}/overlay-image"

_EXECUTION_ID = "exec-ppe"
_CAPTURE_ID = "wf-ppe-exec-ppe"
_NO_OVERLAY_EXECUTION_ID = "exec-plain"
_NO_OVERLAY_CAPTURE_ID = "wf-ppe-exec-plain"
_REGISTRATION_ID = "wf-ppe:1"

_BASE_BYTES = b"captured-frame"
_OVERLAY_BYTES = b"frame-with-boxes"


def _production_functions():
    """The download_file.py module AST and its top-level function defs."""
    tree = ast.parse(_DOWNLOAD_FILE.read_text(), filename=str(_DOWNLOAD_FILE))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def _load_production_route():
    """Compile the shipped route (and its token check) into a namespace of
    real host-importable collaborators, without its router decorator."""
    functions = _production_functions()
    route = functions[_ROUTE_FUNCTION]
    route.decorator_list = []
    module = ast.Module(
        body=[functions["validate_token_in_query_param"], route],
        type_ignores=[],
    )
    namespace = {
        "authorize_credential": authorize_credential,
        "run_artifacts": run_artifacts,
        "WorkflowExecution": WorkflowExecution,
        "HTTPException": HTTPException,
        "HTTP_404_NOT_FOUND": HTTP_404_NOT_FOUND,
        "FileResponse": FileResponse,
        "Session": Session,
        "Depends": Depends,
        "get_db": workflow_engine_api.get_db,
    }
    exec(compile(module, str(_DOWNLOAD_FILE), "exec"), namespace)
    return namespace[_ROUTE_FUNCTION]


def _write(path, data):
    with open(path, "wb") as artifact:
        artifact.write(data)


def _seed(session_factory, execution_id, capture_id, output_dir):
    session = session_factory()
    try:
        if session.get(WorkflowRegistration, _REGISTRATION_ID) is None:
            session.add(
                WorkflowRegistration(
                    id=_REGISTRATION_ID,
                    workflow_id="wf-ppe",
                    version="1",
                    arch="arm64_jp7",
                    artifact_path="/aws_dda/workflows/wf-ppe/1",
                    status="registered",
                    registered_at=int(time.time()),
                )
            )
        session.add(
            WorkflowExecution(
                id=execution_id,
                registration_id=_REGISTRATION_ID,
                started_at=int(time.time()),
                finished_at=int(time.time()),
                status="completed",
                has_image_results=True,
                output_dir=output_dir,
                capture_id=capture_id,
            )
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def client(tmp_path):
    """The production route over a detection run (base + overlay) and a run
    without an overlay image (base only)."""
    session_factory = make_session_factory()
    detection_dir = str(tmp_path / "detection-run")
    plain_dir = str(tmp_path / "plain-run")
    os.makedirs(detection_dir)
    os.makedirs(plain_dir)
    _write(os.path.join(detection_dir, f"{_CAPTURE_ID}.jpg"), _BASE_BYTES)
    _write(
        os.path.join(detection_dir, f"{_CAPTURE_ID}.overlay.jpg"),
        _OVERLAY_BYTES,
    )
    _write(os.path.join(plain_dir, f"{_NO_OVERLAY_CAPTURE_ID}.jpg"), _BASE_BYTES)
    _seed(session_factory, _EXECUTION_ID, _CAPTURE_ID, detection_dir)
    _seed(
        session_factory,
        _NO_OVERLAY_EXECUTION_ID,
        _NO_OVERLAY_CAPTURE_ID,
        plain_dir,
    )

    router = APIRouter()
    router.get(_ROUTE_PATH)(_load_production_route())
    app = FastAPI()
    app.include_router(router)

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


def _get(client, execution_id):
    return client.get(
        _ROUTE_PATH.format(execution_id=execution_id),
        params={"token": "local-session.token"},
    )


class TestOverlayImageRoute:
    def test_route_is_on_the_download_router_at_the_documented_path(self):
        """The shipped definition is mounted on ``unauthenticated_router``
        (token-in-query, like ``/output-image``) at the path the frontend's
        ``workflowExecutionOverlayImageUrl`` builds (Requirements 4.1, 4.3)."""
        route = _production_functions()[_ROUTE_FUNCTION]
        decorators = [
            (ast.unparse(decorator.func), [ast.literal_eval(arg) for arg in decorator.args])
            for decorator in route.decorator_list
            if isinstance(decorator, ast.Call)
        ]
        assert decorators == [("unauthenticated_router.get", [_ROUTE_PATH])]

    def test_route_checks_the_token_before_anything_else(self):
        """The first statement after the docstring is the shared
        token-in-query check (Requirement 4.3)."""
        route = _production_functions()[_ROUTE_FUNCTION]
        first = route.body[1]
        assert ast.unparse(first) == "validate_token_in_query_param(token)"

    def test_serves_the_overlay_bytes_not_the_base_frame(self, client):
        response = _get(client, _EXECUTION_ID)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content == _OVERLAY_BYTES

    def test_unknown_execution_is_404(self, client):
        assert _get(client, "no-such-execution").status_code == 404

    def test_run_without_an_overlay_image_is_404(self, client):
        """No fallback to the base frame or any other file (4.2, 4.5)."""
        response = _get(client, _NO_OVERLAY_EXECUTION_ID)
        assert response.status_code == 404
        assert "overlay image" in response.json()["detail"]
