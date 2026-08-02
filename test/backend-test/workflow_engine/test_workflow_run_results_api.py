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
"""Tests for the run-results / overlay endpoints and the run-artifact
resolution helpers (Requirements 4.1, 4.2, 4.6, 4.7, 5.7; Property 7).

Uses the same standalone FastAPI app + in-memory database pattern as
``test_workflow_engine_api.py`` so the tests run without GStreamer, the
full LocalServer app, or a real /aws_dda tree. The base-image FileResponse
route lives on ``download_file``'s unauthenticated_router (token-in-query,
matching the existing capture-image serving); its file-resolution + 404
decision is exercised here through the shared, importable
``run_artifacts`` helper, and its route/auth parity is covered in the full
backend suite (``api-endpoints/test_workflows_api.py``).
"""
import base64
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


def _seed_execution(
    session_factory,
    execution_id="exec-1",
    registration_id="wf-1:3",
    status="completed",
    has_image_results=False,
    output_dir=None,
    capture_id=None,
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
                has_image_results=has_image_results,
                output_dir=output_dir,
                capture_id=capture_id,
            )
        )
        session.commit()
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Artifact fixtures: build a per-run output_dir with capture_id-prefixed files.
# --------------------------------------------------------------------------- #

_CAPTURE_ID = "wf-1-exec-1"

# A tiny 1x1 PNG (valid bytes) used as the on-disk mask artifact.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQ"
    "DJ3aI2AAAAAElFTkSuQmCC"
)


def _write(path, data):
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)


def _make_segmentation_jsonl(mask_b64, hex_color="#ffffff"):
    """A capture record shaped like the marshal output the run writes: a mask
    output entry plus a base64 label block whose anomalies['0'] carries the
    chroma-key background (mirrors utils.inference_results_utils)."""
    label = {
        "anomalies": {
            "0": {"class-name": "background", "hex-color": hex_color},
            "1": {"class-name": "defect", "hex-color": "#ff0000"},
        }
    }
    return json.dumps(
        {
            "eventMetadata": {"inferenceTime": "2025-01-01T00:00:00"},
            "deviceFleetAuxiliaryOutputs": [
                {"observedContentType": "mask.png", "data": mask_b64},
                {
                    "observedContentType": "json_with_base64_encoding",
                    "data": base64.b64encode(
                        json.dumps(label).encode()
                    ).decode(),
                },
            ],
        }
    )


class TestRunResults:
    def test_unknown_execution_404(self, client):
        response = client.get("/workflows/executions/nope/results")
        assert response.status_code == 404

    def test_no_image_results_empty_200(self, client, session_factory):
        _seed_execution(session_factory, has_image_results=False)
        response = client.get("/workflows/executions/exec-1/results")
        assert response.status_code == 200
        assert response.json() == {
            "hasImageResults": False,
            "captureId": None,
            "images": [],
        }

    def test_has_results_without_overlay(self, client, session_factory, tmp_path):
        out = str(tmp_path)
        _write(os.path.join(out, f"{_CAPTURE_ID}.jpg"), b"jpeg-bytes")
        _seed_execution(
            session_factory,
            has_image_results=True,
            output_dir=out,
            capture_id=_CAPTURE_ID,
        )
        response = client.get("/workflows/executions/exec-1/results")
        assert response.status_code == 200
        body = response.json()
        assert body["hasImageResults"] is True
        assert body["captureId"] == _CAPTURE_ID
        assert body["images"] == [{"kind": "output", "hasOverlay": False}]

    def test_has_results_with_overlay_artifact(
        self, client, session_factory, tmp_path
    ):
        out = str(tmp_path)
        _write(os.path.join(out, f"{_CAPTURE_ID}.jpg"), b"jpeg-bytes")
        _write(os.path.join(out, f"{_CAPTURE_ID}.overlay.jpg"), b"overlay")
        _seed_execution(
            session_factory,
            has_image_results=True,
            output_dir=out,
            capture_id=_CAPTURE_ID,
        )
        body = client.get("/workflows/executions/exec-1/results").json()
        assert body["images"] == [{"kind": "output", "hasOverlay": True}]

    def test_has_results_with_mask_artifact_sets_overlay(
        self, client, session_factory, tmp_path
    ):
        out = str(tmp_path)
        _write(os.path.join(out, f"{_CAPTURE_ID}.mask.png"), _PNG_BYTES)
        _seed_execution(
            session_factory,
            has_image_results=True,
            output_dir=out,
            capture_id=_CAPTURE_ID,
        )
        body = client.get("/workflows/executions/exec-1/results").json()
        assert body["images"][0]["hasOverlay"] is True


class TestRunOverlay:
    def test_unknown_execution_404(self, client):
        response = client.get("/workflows/executions/nope/overlay")
        assert response.status_code == 404

    def test_mask_absent_returns_null(self, client, session_factory, tmp_path):
        _seed_execution(
            session_factory,
            has_image_results=True,
            output_dir=str(tmp_path),
            capture_id=_CAPTURE_ID,
        )
        response = client.get("/workflows/executions/exec-1/overlay")
        assert response.status_code == 200
        assert response.json() == {"maskImage": None, "maskBackground": None}

    def test_mask_from_jsonl_with_background(
        self, client, session_factory, tmp_path
    ):
        out = str(tmp_path)
        mask_b64 = base64.b64encode(_PNG_BYTES).decode()
        _write(
            os.path.join(out, f"{_CAPTURE_ID}.jsonl"),
            _make_segmentation_jsonl(mask_b64, hex_color="#ffffff"),
        )
        _seed_execution(
            session_factory,
            has_image_results=True,
            output_dir=out,
            capture_id=_CAPTURE_ID,
        )
        body = client.get("/workflows/executions/exec-1/overlay").json()
        assert body["maskImage"] == mask_b64
        assert body["maskBackground"]["rgb-color"] == [255, 255, 255]

    def test_mask_from_png_fallback_no_background(
        self, client, session_factory, tmp_path
    ):
        out = str(tmp_path)
        # No jsonl -> falls back to the raw .mask.png bytes, null background.
        _write(os.path.join(out, f"{_CAPTURE_ID}.mask.png"), _PNG_BYTES)
        _seed_execution(
            session_factory,
            has_image_results=True,
            output_dir=out,
            capture_id=_CAPTURE_ID,
        )
        body = client.get("/workflows/executions/exec-1/overlay").json()
        assert body["maskImage"] == base64.b64encode(_PNG_BYTES).decode()
        assert body["maskBackground"] is None


class TestBaseOutputImageResolution:
    """The base-image FileResponse route serves whatever
    ``run_artifacts.base_output_image_path`` resolves and 404s when it
    returns None; exercise that decision directly (the route is a thin
    token-auth + FileResponse wrapper around this helper)."""

    def test_prefers_capture_id_jpg(self, tmp_path):
        out = str(tmp_path)
        primary = os.path.join(out, f"{_CAPTURE_ID}.jpg")
        _write(primary, b"base")
        _write(os.path.join(out, f"{_CAPTURE_ID}.overlay.jpg"), b"overlay")
        assert run_artifacts.base_output_image_path(out, _CAPTURE_ID) == primary

    def test_falls_back_to_non_overlay_jpg(self, tmp_path):
        out = str(tmp_path)
        # No {capture_id}.jpg; a differently-named base jpg is produced.
        other = os.path.join(out, "frame-0001.jpg")
        _write(other, b"base")
        _write(os.path.join(out, f"{_CAPTURE_ID}.overlay.jpg"), b"overlay")
        resolved = run_artifacts.base_output_image_path(out, _CAPTURE_ID)
        assert resolved == other

    def test_none_when_no_base_image(self, tmp_path):
        out = str(tmp_path)
        _write(os.path.join(out, f"{_CAPTURE_ID}.overlay.jpg"), b"overlay")
        assert run_artifacts.base_output_image_path(out, _CAPTURE_ID) is None

    def test_none_when_output_dir_missing(self):
        assert run_artifacts.base_output_image_path(None, _CAPTURE_ID) is None
        assert (
            run_artifacts.base_output_image_path("/no/such/dir", _CAPTURE_ID)
            is None
        )


class TestResultsLinkArtifactsEquivalence:
    """Property 7: Results-link and artifacts equivalence.

    **Feature: deployed-workflow-run-observability, Property 7: Results-link
    and artifacts equivalence**
    **Validates: Requirements 5.1, 5.2**

    ``hasImageResults`` (and thus a non-empty results payload / the "View
    results" link) is true if and only if the run routed capture artifacts
    (terminal File_Output_Node), independent of which artifact files happen
    to be present on disk.
    """

    @settings(max_examples=100, deadline=None)
    @given(
        routed=st.booleans(),
        base_present=st.booleans(),
        overlay_present=st.booleans(),
    )
    def test_link_visibility_matches_routing(
        self, routed, base_present, overlay_present, tmp_path_factory
    ):
        session_factory = make_session_factory()
        out = str(tmp_path_factory.mktemp("run"))
        if base_present:
            _write(os.path.join(out, f"{_CAPTURE_ID}.jpg"), b"base")
        if overlay_present:
            _write(os.path.join(out, f"{_CAPTURE_ID}.overlay.jpg"), b"ov")

        _seed_execution(
            session_factory,
            has_image_results=routed,
            output_dir=out if routed else None,
            capture_id=_CAPTURE_ID if routed else None,
        )

        app = FastAPI()
        app.include_router(workflow_engine_api.router)

        def override_get_db():
            db = session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[workflow_engine_api.get_db] = override_get_db
        with TestClient(app) as client:
            body = client.get("/workflows/executions/exec-1/results").json()

        # hasImageResults iff the run routed capture artifacts, and the
        # payload (the link's data) is non-empty exactly in that case.
        assert body["hasImageResults"] is routed
        assert (len(body["images"]) > 0) is routed
        if routed:
            assert body["captureId"] == _CAPTURE_ID
        else:
            assert body["captureId"] is None
