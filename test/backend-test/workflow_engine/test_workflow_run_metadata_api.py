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
"""Tests for the run-metadata endpoint and its ``read_run_metadata``
helper (Requirements 4.1, 4.2, 4.3).

Uses the same standalone FastAPI app + in-memory database pattern as
``test_workflow_graph_node_status_api.py`` so the tests run without
GStreamer, the full LocalServer app, or a real /aws_dda tree.

- ``GET .../executions/{id}/metadata`` returns the parsed
  ``{output_dir}/{capture_id}.json`` run metadata; ``{}`` (200) when the
  execution has no ``output_dir``/``capture_id`` or the file is missing/
  unreadable/malformed/non-object; 404 for an unknown execution.
"""
import json
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

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


def _seed_registration(session_factory, registration_id="wf-1:3"):
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
        session.commit()
    finally:
        session.close()


def _seed_execution(
    session_factory,
    execution_id="exec-1",
    registration_id="wf-1:3",
    status="completed",
    output_dir=None,
    capture_id=None,
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
                output_dir=output_dir,
                capture_id=capture_id,
            )
        )
        session.commit()
    finally:
        session.close()


def _write_metadata(output_dir, capture_id, payload_text):
    path = os.path.join(output_dir, "{0}.json".format(capture_id))
    with open(path, "w", encoding="utf-8") as metadata_file:
        metadata_file.write(payload_text)
    return path


_METADATA = {
    "llm": {"llm-1": {"generated_text": "The part looks fine."}},
    "is_anomalous": False,
    "confidence": 0.97,
}


class TestRunMetadataEndpoint:
    def test_unknown_execution_404(self, client):
        response = client.get("/workflows/executions/nope/metadata")
        assert response.status_code == 404

    def test_metadata_returned_for_known_execution(
        self, client, session_factory, tmp_path
    ):
        _write_metadata(str(tmp_path), "cap-1", json.dumps(_METADATA))
        _seed_execution(
            session_factory, output_dir=str(tmp_path), capture_id="cap-1"
        )
        response = client.get("/workflows/executions/exec-1/metadata")
        assert response.status_code == 200
        assert response.json() == _METADATA

    def test_missing_artifacts_yield_empty_object_200(
        self, client, session_factory, tmp_path
    ):
        # Execution exists but no {capture_id}.json was written.
        _seed_execution(
            session_factory, output_dir=str(tmp_path), capture_id="cap-1"
        )
        response = client.get("/workflows/executions/exec-1/metadata")
        assert response.status_code == 200
        assert response.json() == {}

    def test_no_recorded_output_dir_yields_empty_object_200(
        self, client, session_factory
    ):
        _seed_execution(session_factory, output_dir=None, capture_id=None)
        response = client.get("/workflows/executions/exec-1/metadata")
        assert response.status_code == 200
        assert response.json() == {}


class TestReadRunMetadataHelper:
    """The best-effort read the metadata endpoint delegates to."""

    def test_missing_output_dir_or_capture_id_yield_empty(self, tmp_path):
        assert run_artifacts.read_run_metadata(None, "cap-1") == {}
        assert run_artifacts.read_run_metadata("", "cap-1") == {}
        assert run_artifacts.read_run_metadata(str(tmp_path), None) == {}
        assert run_artifacts.read_run_metadata(str(tmp_path), "") == {}

    def test_missing_file_yields_empty(self, tmp_path):
        assert run_artifacts.read_run_metadata(str(tmp_path), "cap-1") == {}

    def test_malformed_json_yields_empty(self, tmp_path):
        _write_metadata(str(tmp_path), "cap-1", "{not valid json")
        assert run_artifacts.read_run_metadata(str(tmp_path), "cap-1") == {}

    def test_non_object_top_level_yields_empty(self, tmp_path):
        _write_metadata(str(tmp_path), "cap-1", json.dumps([1, 2, 3]))
        assert run_artifacts.read_run_metadata(str(tmp_path), "cap-1") == {}
        _write_metadata(str(tmp_path), "cap-2", json.dumps("text"))
        assert run_artifacts.read_run_metadata(str(tmp_path), "cap-2") == {}

    def test_parses_object_metadata(self, tmp_path):
        _write_metadata(str(tmp_path), "cap-1", json.dumps(_METADATA))
        assert (
            run_artifacts.read_run_metadata(str(tmp_path), "cap-1")
            == _METADATA
        )


# JSON-serializable values as produced by the run's tag map: scalars plus
# nested arrays/objects with string keys (the shape json.dump can write and
# json.load reads back exactly).
_json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53), max_value=2**53)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=40),
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.text(max_size=20), children, max_size=4),
    max_leaves=20,
)

_json_objects = st.dictionaries(st.text(max_size=20), _json_values, max_size=6)


class TestRunMetadataRoundTripProperty:
    """**Property 5: Run metadata read round trip**

    For any JSON-serializable object written to
    ``{output_dir}/{capture_id}.json``, ``read_run_metadata`` returns an
    equal object.

    **Validates: Requirements 4.1**
    """

    @settings(deadline=None)
    @given(metadata=_json_objects)
    def test_read_run_metadata_round_trips(self, tmp_path_factory, metadata):
        output_dir = str(tmp_path_factory.mktemp("run-metadata"))
        _write_metadata(output_dir, "cap-1", json.dumps(metadata))
        assert (
            run_artifacts.read_run_metadata(output_dir, "cap-1") == metadata
        )
