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
"""End-to-end serving of the three Inspections' additive run artifacts
(imts-triple-inspection-hmi task 13.2).

Seeds a run artifact directory the way the executor leaves one for the
target workflow — three Bedrock inspection nodes, each with the
pre-existing ``in`` frame plus the additive ``original`` / ``annotated``
frames — and drives the two LocalServer surfaces the Triple_HMI depends on
through a test client:

* ``GET /workflows/executions/{id}/results`` reports exactly six additive
  node entries (three nodeIds x two new ports) and leaves every
  pre-existing entry, field, and their ordering untouched.
* ``GET /workflows/executions/{id}/node-image?nodeId=&port=`` serves each
  (``nodeId``, ``port``) pair — including the new ports — returning that
  pair's own bytes, so no panel can ever be served another Inspection's or
  another port's image.

Requirement 4.4 is exactly this contract: the new artifacts must be
listable and servable with **zero** LocalServer changes. Accordingly no
production code is touched — the real ``workflow_engine.api`` results route
and the real ``run_artifacts`` inventory/resolution run unmodified, and the
artifact filenames come from the processor's own
``ORIGINAL_FRAME_ARTIFACT_TEMPLATE`` / ``ANNOTATED_FRAME_ARTIFACT_TEMPLATE``
+ ``sanitize_node_id_for_artifact``.

The app is assembled from the routers rather than from ``app.app``, the
standalone-FastAPI + in-memory-database pattern of
``test_workflow_run_results_api.py``: the full LocalServer app pulls in the
device-only stack (GStreamer/gi, greengrass IPC, panorama), which this
suite deliberately runs without. The image route is therefore mounted here
as ``endpoints/download_file.load_workflow_execution_node_image``'s own
body — real ``utils.auth`` token check, real execution lookup, real
``run_artifacts.node_image_path`` resolution, ``FileResponse`` or 404 —
so the serving decision under test is the production one; that route's
mounting and auth parity are covered by the device-container suite
(``api-endpoints/test_workflows_api.py``).

_Requirements: 4.4_
"""
import os
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
from workflow_engine.output_bindings import (
    ANNOTATED_FRAME_ARTIFACT_TEMPLATE,
    ORIGINAL_FRAME_ARTIFACT_TEMPLATE,
    sanitize_node_id_for_artifact,
)

#: The run under test (three Inspections, additive frames present) and a
#: baseline run seeded with only the pre-change artifacts.
_EXECUTION_ID = "exec-triple"
_CAPTURE_ID = "wf-1-exec-triple"
_BASELINE_EXECUTION_ID = "exec-legacy"
_BASELINE_CAPTURE_ID = "wf-1-exec-legacy"

_REGISTRATION_ID = "wf-1:3"

#: Raw binding node ids of the target workflow's three bedrock branches.
_NODE_IDS = ("bedrock_1", "bedrock_2", "bedrock_3")

#: The ports this feature adds, in the order the inventory reports them
#: (unknown ports sort after ``in``/``reference``, alphabetically).
_NEW_PORTS = ("annotated", "original")

#: Ports a run already produced before this feature.
_LEGACY_PORTS = ("in",)


# --------------------------------------------------------------------------- #
# Node-image serving route (production body; see the module docstring)
# --------------------------------------------------------------------------- #

_serving_router = APIRouter()


@_serving_router.get("/workflows/executions/{execution_id}/node-image")
def _serve_node_image(
    execution_id: str,
    nodeId: str = None,
    port: str = None,
    token: str = None,
    db: Session = Depends(workflow_engine_api.get_db),
):
    """The body of ``download_file.load_workflow_execution_node_image``:
    token-in-query authorization, execution lookup, then
    ``run_artifacts.node_image_path`` — which resolves only pairs
    ``list_node_images`` reports, so fabricated names and traversal shapes
    answer 404 by construction."""
    authorize_credential(token)

    execution = db.get(WorkflowExecution, execution_id)
    if execution is None:
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=f"Workflow execution '{execution_id}' was not found",
        )

    image_path = run_artifacts.node_image_path(
        execution.output_dir, execution.capture_id, nodeId, port
    )
    if image_path:
        return FileResponse(image_path, media_type="image/jpeg")

    raise HTTPException(
        status_code=HTTP_404_NOT_FOUND,
        detail=(
            f"Server unable to load node image for execution "
            f"'{execution_id}', node '{nodeId}', port '{port}'. "
            f"Error: 'Image not found'."
        ),
    )


# --------------------------------------------------------------------------- #
# Artifact fixtures (tmp-dir pattern from test_workflow_run_results_api.py)
# --------------------------------------------------------------------------- #


def _write(path, data):
    with open(path, "wb" if isinstance(data, bytes) else "w") as artifact:
        artifact.write(data)


def _frame_bytes(capture_id, node_id, port):
    """Unique per (run, node, port), so a substituted image is detectable."""
    return "frame::{0}::{1}::{2}".format(capture_id, node_id, port).encode()


def _node_artifact_name(capture_id, node_id, port):
    safe_node_id = sanitize_node_id_for_artifact(node_id)
    if port == "original":
        return ORIGINAL_FRAME_ARTIFACT_TEMPLATE.format(
            capture_id=capture_id, safe_node_id=safe_node_id
        )
    if port == "annotated":
        return ANNOTATED_FRAME_ARTIFACT_TEMPLATE.format(
            capture_id=capture_id, safe_node_id=safe_node_id
        )
    return "{0}.node.{1}.{2}.jpg".format(capture_id, safe_node_id, port)


def _seed_run_dir(out, capture_id, inspection_frames=True):
    """A run artifact directory as the executor leaves it: the captured
    frame, the marshal's plate-box overlay, the capture record, and per
    bedrock branch the pre-existing ``in`` frame plus the detection crop.
    With ``inspection_frames`` the additive Original_Image and
    Annotated_Image land too (the post-change executor)."""
    _write(os.path.join(out, "{0}.jpg".format(capture_id)), b"captured-frame")
    _write(os.path.join(out, "{0}.overlay.jpg".format(capture_id)), b"plates")
    _write(os.path.join(out, "{0}.jsonl".format(capture_id)), "{}")

    ports = _LEGACY_PORTS + (_NEW_PORTS if inspection_frames else ())
    for index, node_id in enumerate(_NODE_IDS, start=1):
        # The Detection_Crop artifact is not part of the image inventory.
        _write(
            os.path.join(out, "{0}.crop.det-{1}.jpg".format(capture_id, index)),
            b"crop",
        )
        for port in ports:
            _write(
                os.path.join(out, _node_artifact_name(capture_id, node_id, port)),
                _frame_bytes(capture_id, node_id, port),
            )
    return out


def _expected_node_entries(inspection_frames=True):
    """The node entries ``/results`` must report, in order: nodeId
    ascending, then ``in`` before the additive ports alphabetically."""
    ports = _LEGACY_PORTS + (_NEW_PORTS if inspection_frames else ())
    return [
        {
            "kind": "node",
            "nodeId": sanitize_node_id_for_artifact(node_id),
            "port": port,
            "hasOverlay": False,
        }
        for node_id in sorted(_NODE_IDS)
        for port in ports
    ]


def _seed_execution(session_factory, execution_id, capture_id, output_dir):
    session = session_factory()
    try:
        if session.get(WorkflowRegistration, _REGISTRATION_ID) is None:
            session.add(
                WorkflowRegistration(
                    id=_REGISTRATION_ID,
                    workflow_id="wf-1",
                    version="3",
                    arch="x86_64",
                    artifact_path="/aws_dda/workflows/wf-1/3",
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
    """Test client over the real results route and the node-image serving
    route, backed by a run directory holding the three Inspections and a
    baseline run holding only the pre-change artifacts."""
    session_factory = make_session_factory()

    triple_dir = str(tmp_path / "triple-run")
    baseline_dir = str(tmp_path / "baseline-run")
    os.makedirs(triple_dir)
    os.makedirs(baseline_dir)
    _seed_run_dir(triple_dir, _CAPTURE_ID, inspection_frames=True)
    _seed_run_dir(baseline_dir, _BASELINE_CAPTURE_ID, inspection_frames=False)

    _seed_execution(session_factory, _EXECUTION_ID, _CAPTURE_ID, triple_dir)
    _seed_execution(
        session_factory,
        _BASELINE_EXECUTION_ID,
        _BASELINE_CAPTURE_ID,
        baseline_dir,
    )

    app = FastAPI()
    app.include_router(workflow_engine_api.router)
    app.include_router(_serving_router)

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


def _node_image(client, execution_id, node_id, port):
    return client.get(
        "/workflows/executions/{0}/node-image".format(execution_id),
        params={"nodeId": node_id, "port": port, "token": "local-session.token"},
    )


class TestTripleResultsInventory:
    """``GET /workflows/executions/{id}/results`` over a three-Inspection
    run (Requirement 4.4)."""

    def test_reports_exactly_six_additive_node_entries(self, client):
        body = client.get(
            "/workflows/executions/{0}/results".format(_EXECUTION_ID)
        ).json()

        assert body["hasImageResults"] is True
        assert body["captureId"] == _CAPTURE_ID
        # The whole payload, ordering included: the pre-existing output
        # entry first, then one node entry per persisted frame.
        assert body["images"] == [
            {"kind": "output", "hasOverlay": True}
        ] + _expected_node_entries()

        additive = [
            image
            for image in body["images"]
            if image.get("port") in _NEW_PORTS
        ]
        assert len(additive) == 6
        assert sorted(
            (image["nodeId"], image["port"]) for image in additive
        ) == sorted(
            (sanitize_node_id_for_artifact(node_id), port)
            for node_id in _NODE_IDS
            for port in _NEW_PORTS
        )

    def test_pre_existing_entries_fields_and_ordering_unchanged(self, client):
        """Dropping the additive entries from the run's inventory
        reproduces, exactly and in order, what a run without them reports —
        the six entries are purely additive."""
        body = client.get(
            "/workflows/executions/{0}/results".format(_EXECUTION_ID)
        ).json()
        baseline = client.get(
            "/workflows/executions/{0}/results".format(_BASELINE_EXECUTION_ID)
        ).json()

        assert baseline["images"] == [
            {"kind": "output", "hasOverlay": True}
        ] + _expected_node_entries(inspection_frames=False)
        assert [
            image
            for image in body["images"]
            if image.get("port") not in _NEW_PORTS
        ] == baseline["images"]

        # Field sets are the existing ones — no new keys on any entry.
        for image in body["images"]:
            if image["kind"] == "output":
                assert set(image) == {"kind", "hasOverlay"}
            else:
                assert set(image) == {"kind", "nodeId", "port", "hasOverlay"}
        assert set(body) == {"hasImageResults", "captureId", "images"}


class TestTripleNodeImageServing:
    """Every listed (``nodeId``, ``port``) pair is servable, and only
    listed pairs are (Requirement 4.4)."""

    def test_every_reported_pair_serves_its_own_frame(self, client):
        body = client.get(
            "/workflows/executions/{0}/results".format(_EXECUTION_ID)
        ).json()
        reported = [
            image for image in body["images"] if image["kind"] == "node"
        ]
        assert len(reported) == len(_NODE_IDS) * (
            len(_LEGACY_PORTS) + len(_NEW_PORTS)
        )

        served = {}
        for image in reported:
            response = _node_image(
                client, _EXECUTION_ID, image["nodeId"], image["port"]
            )
            assert response.status_code == 200, (
                image,
                response.status_code,
            )
            assert response.headers["content-type"] == "image/jpeg"
            # Each panel gets its own Inspection's own port's bytes.
            assert response.content == _frame_bytes(
                _CAPTURE_ID, image["nodeId"], image["port"]
            )
            served[(image["nodeId"], image["port"])] = response.content

        # No two panels were served the same bytes.
        assert len(set(served.values())) == len(served)

    def test_additive_ports_of_the_three_inspections_are_servable(self, client):
        for node_id in _NODE_IDS:
            safe_node_id = sanitize_node_id_for_artifact(node_id)
            for port in _NEW_PORTS:
                response = _node_image(
                    client, _EXECUTION_ID, safe_node_id, port
                )
                assert response.status_code == 200
                assert response.content == _frame_bytes(
                    _CAPTURE_ID, safe_node_id, port
                )

    def test_pre_existing_in_frames_still_serve_unchanged(self, client):
        for node_id in _NODE_IDS:
            safe_node_id = sanitize_node_id_for_artifact(node_id)
            response = _node_image(client, _EXECUTION_ID, safe_node_id, "in")
            assert response.status_code == 200
            assert response.content == _frame_bytes(
                _CAPTURE_ID, safe_node_id, "in"
            )

    def test_unlisted_pairs_and_unknown_runs_404(self, client):
        # A port the run never produced, and a node it never had.
        assert (
            _node_image(client, _EXECUTION_ID, "bedrock_1", "reference")
            .status_code
            == 404
        )
        assert (
            _node_image(client, _EXECUTION_ID, "bedrock_9", "original")
            .status_code
            == 404
        )
        # A run predating the additive persists serves none of the new ports.
        for port in _NEW_PORTS:
            assert (
                _node_image(
                    client, _BASELINE_EXECUTION_ID, "bedrock_1", port
                ).status_code
                == 404
            )
        # Unknown execution.
        assert _node_image(client, "nope", "bedrock_1", "original").status_code == 404

    def test_traversal_shapes_404(self, client):
        for node_id, port in (
            ("../{0}".format(_BASELINE_CAPTURE_ID), "jpg"),
            ("..", "/etc/passwd"),
            ("bedrock_1", "../../original"),
        ):
            assert (
                _node_image(client, _EXECUTION_ID, node_id, port).status_code
                == 404
            )
