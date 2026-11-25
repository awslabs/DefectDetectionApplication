#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""rfdetr-seg-support task 10.9 (Requirements 10.5, 17.3): the run preview's
``/overlay`` body for instance-segmentation runs (10.5, DD14).

``run_artifacts.read_mask_overlay`` returns the four-key body
``{maskImage, maskBackground, instances, detectionsTruncated}``: ``instances``
is the Detection_List in list form with ``mask_rle`` kept, read from the
capture record or the marshal sidecar; ``detectionsTruncated`` mirrors the
marshal's marker; both are ``null`` for a run without artifacts. The same
standalone FastAPI app + in-memory database pattern as
``test_workflow_run_results_api.py`` serves the route.
"""
import base64
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

_CAPTURE_ID = "wf-1-exec-1"
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQ"
    "DJ3aI2AAAAAElFTkSuQmCC"
)
_MASK_COLOR_MAP = {
    "0": {"class-name": "background", "hex-color": "#ffffff", "total-percentage-area": 0.8},
    "1": {"class-name": "scratch", "hex-color": "#ff0000", "total-percentage-area": 0.2},
}
_INSTANCE_MAP = {
    "0": {"class_index": "0", "class_label": "scratch", "bounding_box": [3, 2, 8, 5],
          "confidence": 0.9, "mask_rle": "27 4 8 4 8 4 8 4 125", "mask_area": 16},
    "1": {"class_index": "2", "class_label": "dent", "bounding_box": [8, 5, 13, 9],
          "confidence": 0.6, "mask_area": 9, "mask_truncated": True},
}
_YOLO_MAP = {"0": {"class_index": "7", "class_label": "truck", "bounding_box": [1, 2, 3, 4], "confidence": 0.8}}
FOUR_KEYS = {"maskImage", "maskBackground", "instances", "detectionsTruncated"}


def _b64_json(payload):
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _write(path, data):
    with open(path, "wb" if isinstance(data, bytes) else "w") as handle:
        handle.write(data)


def _record(block, mask_b64=None):
    outputs = []
    if mask_b64 is not None:
        outputs.append({"observedContentType": "mask.png", "data": mask_b64})
    outputs.extend([
        {"observedContentType": "overlay.jpg", "data-ref": "file:///tmp/overlay.jpg"},
        {"observedContentType": "json_with_base64_encoding", "data": _b64_json(block)},
    ])
    return json.dumps({"eventMetadata": {"inferenceTime": "2025-01-01T00:00:00"},
                       "deviceFleetAuxiliaryOutputs": outputs}) + "\n"


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


def _seed_execution(session_factory, output_dir, execution_id="exec-1", registration_id="wf-1:3"):
    session = session_factory()
    try:
        if session.get(WorkflowRegistration, registration_id) is None:
            session.add(WorkflowRegistration(
                id=registration_id, workflow_id="wf-1", version="3", arch="x86_64",
                artifact_path="/aws_dda/workflows/wf-1:3", status="registered",
                registered_at=int(time.time()),
            ))
        session.add(WorkflowExecution(
            id=execution_id, registration_id=registration_id, started_at=int(time.time()),
            finished_at=int(time.time()), status="completed", has_image_results=True,
            output_dir=output_dir, capture_id=_CAPTURE_ID,
        ))
        session.commit()
    finally:
        session.close()


class TestReadMaskOverlay:
    def test_run_without_artifacts_is_the_four_null_body(self, tmp_path):
        assert run_artifacts.read_mask_overlay(str(tmp_path), _CAPTURE_ID) == {
            "maskImage": None, "maskBackground": None, "instances": None, "detectionsTruncated": None,
        }
        assert run_artifacts.read_mask_overlay(None, _CAPTURE_ID)["instances"] is None
        assert run_artifacts.read_mask_overlay(str(tmp_path), None)["detectionsTruncated"] is None

    def test_instance_record_carries_mask_background_instances_and_marker(self, tmp_path):
        mask_b64 = base64.b64encode(_PNG_BYTES).decode()
        block = {"detections": _INSTANCE_MAP, "mask_color_map": _MASK_COLOR_MAP, "detections_truncated": True}
        _write(os.path.join(str(tmp_path), f"{_CAPTURE_ID}.jsonl"), _record(block, mask_b64))
        body = run_artifacts.read_mask_overlay(str(tmp_path), _CAPTURE_ID)
        assert set(body) == FOUR_KEYS
        assert body["maskImage"] == mask_b64
        assert body["maskBackground"]["rgb-color"] == [255, 255, 255]
        assert body["instances"] == [_INSTANCE_MAP["0"], _INSTANCE_MAP["1"]]
        assert body["instances"][0]["mask_rle"] == "27 4 8 4 8 4 8 4 125"
        assert body["detectionsTruncated"] is True

    def test_untruncated_instance_record_reports_false(self, tmp_path):
        block = {"detections": {"0": _INSTANCE_MAP["0"]}, "mask_color_map": _MASK_COLOR_MAP}
        _write(os.path.join(str(tmp_path), f"{_CAPTURE_ID}.jsonl"),
               _record(block, base64.b64encode(_PNG_BYTES).decode()))
        body = run_artifacts.read_mask_overlay(str(tmp_path), _CAPTURE_ID)
        assert body["detectionsTruncated"] is False
        assert len(body["instances"]) == 1

    def test_sidecar_only_run_reads_the_instances_from_the_sidecar(self, tmp_path):
        out = str(tmp_path)
        _write(os.path.join(out, f"{_CAPTURE_ID}.mask.png"), _PNG_BYTES)
        _write(os.path.join(out, f"{_CAPTURE_ID}.detections.json"),
               json.dumps({"detections": _INSTANCE_MAP, "mask_color_map": _MASK_COLOR_MAP,
                           "detections_truncated": True}))
        body = run_artifacts.read_mask_overlay(out, _CAPTURE_ID)
        assert body["maskImage"] == base64.b64encode(_PNG_BYTES).decode()
        assert body["maskBackground"] is None, "the raw png fallback carries no chroma-key background"
        assert body["instances"] == [_INSTANCE_MAP["0"], _INSTANCE_MAP["1"]]
        assert body["detectionsTruncated"] is True

    def test_detection_run_lists_mask_less_entries_with_no_mask(self, tmp_path):
        _write(os.path.join(str(tmp_path), f"{_CAPTURE_ID}.jsonl"), _record({"detections": _YOLO_MAP}))
        body = run_artifacts.read_mask_overlay(str(tmp_path), _CAPTURE_ID)
        assert body["maskImage"] is None and body["maskBackground"] is None
        assert body["instances"] == [_YOLO_MAP["0"]]
        assert body["detectionsTruncated"] is False

    def test_sentinel_run_lists_no_instances(self, tmp_path):
        _write(os.path.join(str(tmp_path), f"{_CAPTURE_ID}.jsonl"), _record({"detections": {}}))
        body = run_artifacts.read_mask_overlay(str(tmp_path), _CAPTURE_ID)
        assert body["instances"] == [] and body["detectionsTruncated"] is False


class TestOverlayRoute:
    def test_run_without_artifacts_answers_the_four_null_body(self, client, session_factory, tmp_path):
        _seed_execution(session_factory, str(tmp_path))
        response = client.get("/workflows/executions/exec-1/overlay")
        assert response.status_code == 200
        assert response.json() == {"maskImage": None, "maskBackground": None,
                                   "instances": None, "detectionsTruncated": None}

    def test_instance_run_answers_the_instances_with_their_masks(self, client, session_factory, tmp_path):
        out = str(tmp_path)
        mask_b64 = base64.b64encode(_PNG_BYTES).decode()
        _write(os.path.join(out, f"{_CAPTURE_ID}.jsonl"),
               _record({"detections": _INSTANCE_MAP, "mask_color_map": _MASK_COLOR_MAP,
                        "detections_truncated": True}, mask_b64))
        _seed_execution(session_factory, out)
        body = client.get("/workflows/executions/exec-1/overlay").json()
        assert set(body) == FOUR_KEYS
        assert body["maskImage"] == mask_b64
        assert body["instances"] == [_INSTANCE_MAP["0"], _INSTANCE_MAP["1"]]
        assert body["detectionsTruncated"] is True

    def test_sidecar_only_instance_run_through_the_route(self, client, session_factory, tmp_path):
        out = str(tmp_path)
        _write(os.path.join(out, f"{_CAPTURE_ID}.mask.png"), _PNG_BYTES)
        _write(os.path.join(out, f"{_CAPTURE_ID}.detections.json"),
               json.dumps({"detections": {"0": _INSTANCE_MAP["0"]}, "mask_color_map": _MASK_COLOR_MAP}))
        _seed_execution(session_factory, out)
        body = client.get("/workflows/executions/exec-1/overlay").json()
        assert body["instances"] == [_INSTANCE_MAP["0"]]
        assert body["instances"][0]["mask_rle"] == "27 4 8 4 8 4 8 4 125"
        assert body["detectionsTruncated"] is False
        assert body["maskBackground"] is None
