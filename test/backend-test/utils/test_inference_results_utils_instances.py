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
"""rfdetr-seg-support task 10.9 (Requirements 10.5, 17.1, 17.3): the device
readers of an instance-segmentation capture.

* Live dict (10.3): ``save_image_object`` keeps ``detections`` as the map with
  the instance keys passing through, copies ``detections_truncated`` only when
  the block carries it (a block without the marker yields exactly today's
  six-key dict; ``detections_truncated: true`` yields ``True``), and sets
  ``mask_image`` / ``mask_background`` / ``anomalies`` from the mask and the
  block's ``mask_color_map`` (the mask alone when the map is absent).
* DB row (10.4): ``convert_inference_res_to_save_in_db`` writes the list form
  in ascending integer key order for a non-empty map and no key for an empty
  one; ``InferenceResultSchema`` and the plain class accept ``detections``;
  the Alembic revision adds and drops the column on a scratch SQLite.
* Routes: ``GET /workflows/{id}/results`` and ``/results/{capture_id}`` return
  the stored list through the FastAPI test client.
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid

import pytest
from sqlalchemy.orm import Session
from unittest.mock import patch

from local_server_base_test_case import LocalServerBaseTestCase
from model.inference_result import InferenceResult as PlainInferenceResult
from model.inference_result import InferenceResultSchema
from data_models.common import InferenceResultHistoryModel
from utils import constants
from utils.inference_results_utils import (
    GetInferenceResults,
    convert_inference_res_to_save_in_db,
)

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_INFERENCE_TIME = "2025-01-01T00:00:21"
_MODEL_CONFIG = {
    "modelAlias": "seg-model",
    "modelMetaData": "someMetadata",
    "modelVersion": "1",
    "modelConfidenceThresholds": {"AnomalyThreshold": "0.9", "NormalThreshold": "0.8"},
}
_FILE_BYTES = {
    "input.jpg": b"\xff\xd8IN\xff\xd9",
    "overlay.jpg": b"\xff\xd8OV\xff\xd9",
    "mask.png": b"\x89PNG\r\n\x1a\nMASK",
}
_MASK_COLOR_MAP = {
    "0": {"class-name": "background", "hex-color": "#ffffff", "total-percentage-area": 0.8},
    "1": {"class-name": "scratch", "hex-color": "#ff0000", "total-percentage-area": 0.12},
    "2": {"class-name": "dent", "hex-color": "#00ff00", "total-percentage-area": 0.08},
}
_INSTANCE_MAP = {
    "0": {"class_index": "0", "class_label": "scratch", "bounding_box": [3, 2, 8, 5],
          "confidence": 0.9, "mask_rle": "27 4 8 4 8 4 8 4 125", "mask_area": 16},
    "1": {"class_index": "2", "class_label": "dent", "bounding_box": [8, 5, 13, 9],
          "confidence": 0.6, "mask_area": 9, "mask_truncated": True},
}
_YOLO_MAP = {"0": {"class_index": "7", "class_label": "truck", "bounding_box": [1, 2, 3, 4], "confidence": 0.8}}
SIX_KEYS = ["confidence", "inference_result", "anomaly_score", "anomaly_threshold", "detections", "detection_count"]


def _b64_json(payload):
    return base64.b64encode(json.dumps(payload).encode()).decode()


class _CaptureRecords:
    """Capture records shaped like the marshal's output, on a scratch folder."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="seg-readers-")
        self.paths = {}
        for name, content in _FILE_BYTES.items():
            path = os.path.join(self.dir, name)
            with open(path, "wb") as handle:
                handle.write(content)
            self.paths[name] = path
        self.jsonl = os.path.join(self.dir, "cap-1.jsonl")

    def ref(self, name, content_type):
        return {"data-ref": "file://" + self.paths[name], "encoding": "NONE", "observedContentType": content_type}

    def record(self, block, with_mask):
        outputs = []
        if with_mask:
            outputs.append(self.ref("mask.png", "mask.png"))
        outputs.extend([
            self.ref("overlay.jpg", constants.INFERENCE_OUTPUT_OVERLAY_CONTENT_TYPE),
            {"data": _b64_json({"Inference status": "success", "Inference result": constants.DETECTION,
                                "Detection_count": len(block.get("detections", {})), "Confidence": 0.9,
                                "Anomaly_score": 0.9, "Anomaly_threshold": 1.0, "Error msg": ""}),
             "encoding": "BASE64", "observedContentType": "json"},
            {"data": _b64_json(block), "encoding": "BASE64", "observedContentType": "json_with_base64_encoding"},
        ])
        return {
            "deviceFleetAuxiliaryInputs": [self.ref("input.jpg", constants.INFERENCE_INPUT_IMAGE_CONTENT_TYPE)],
            "deviceFleetAuxiliaryOutputs": outputs,
            "eventMetadata": {"capture_folder": self.dir, "eventId": "cap-1", "deviceFleetName": "fleet-A",
                              "modelName": "seg-model", "modelVersion": "1", "inferenceTime": _INFERENCE_TIME},
            "eventVersion": "0",
        }

    def live(self, block, with_mask):
        with patch("utils.inference_results_utils.get_default_configs_lfv", return_value=_MODEL_CONFIG):
            reader = GetInferenceResults(stream_id="wf-1", sort="desc", starting_point=0, max_results=1)
            return reader.save_image_object(self.record(block, with_mask), self.jsonl, capture_id="cap-1")


@pytest.fixture
def records(monkeypatch):
    # ``GetInferenceResults`` resolves the stream's em-agent config path under
    # COMPONENT_WORK_PATH (LocalServerBaseTestCase patches it for its tests).
    monkeypatch.setenv("COMPONENT_WORK_PATH", "/tmp")
    return _CaptureRecords()


def _workflow():
    return {"workflowId": "wf-1", "featureConfigurations": [{"modelName": "seg-model"}]}


def _db_row(live):
    with patch("utils.inference_results_utils.get_default_configs_lfv", return_value=_MODEL_CONFIG):
        return convert_inference_res_to_save_in_db(dict(live, captureType=constants.INFERENCE), _workflow())


class TestLiveDict:
    def test_instance_capture_carries_masks_background_and_legend(self, records):
        block = {"detections": _INSTANCE_MAP, "mask_color_map": _MASK_COLOR_MAP, "detections_truncated": True}
        live = records.live(block, with_mask=True)
        result = live["inferenceResult"]
        assert list(result) == SIX_KEYS + ["detections_truncated", "mask_image", "mask_background", "anomalies"]
        assert result["detections"] == _INSTANCE_MAP, "the map form with the instance keys passing through"
        assert result["detections"]["0"]["mask_rle"] == "27 4 8 4 8 4 8 4 125"
        assert result["detections_truncated"] is True
        assert result["mask_image"] == base64.b64encode(_FILE_BYTES["mask.png"]).decode()
        assert result["mask_background"] == {"class-name": "background", "rgb-color": [255, 255, 255],
                                             "total-percentage-area": 0.8}
        assert result["anomalies"] == {"1": _MASK_COLOR_MAP["1"], "2": _MASK_COLOR_MAP["2"]}
        assert live["imageDataFilePath"] == records.paths["overlay.jpg"]

    def test_marker_present_without_truncation_is_false(self, records):
        live = records.live({"detections": _YOLO_MAP, "detections_truncated": False}, with_mask=False)
        assert live["inferenceResult"]["detections_truncated"] is False

    def test_block_without_the_marker_yields_exactly_the_six_key_dict(self, records):
        live = records.live({"detections": _YOLO_MAP}, with_mask=False)
        assert list(live["inferenceResult"]) == SIX_KEYS
        assert "detections_truncated" not in live["inferenceResult"]
        assert live["inferenceResult"]["detections"] == _YOLO_MAP

    def test_mask_without_a_color_map_sets_the_mask_alone(self, records):
        live = records.live({"detections": _INSTANCE_MAP}, with_mask=True)
        result = live["inferenceResult"]
        assert list(result) == SIX_KEYS + ["mask_image"]
        assert "mask_background" not in result and "anomalies" not in result

    def test_sentinel_capture_is_the_empty_map(self, records):
        live = records.live({"detections": {}}, with_mask=False)
        assert list(live["inferenceResult"]) == SIX_KEYS
        assert live["inferenceResult"]["detections"] == {}


class TestDbRow:
    def test_non_empty_map_is_stored_as_the_list_in_key_order(self, records):
        shuffled = {"2": _INSTANCE_MAP["1"], "10": _YOLO_MAP["0"], "0": _INSTANCE_MAP["0"]}
        live = records.live({"detections": shuffled, "mask_color_map": _MASK_COLOR_MAP}, with_mask=True)
        row = _db_row(live)
        assert row["detections"] == [_INSTANCE_MAP["0"], _INSTANCE_MAP["1"], _YOLO_MAP["0"]]
        assert row["detections"][0]["mask_rle"] == "27 4 8 4 8 4 8 4 125"
        assert row["maskImage"] and row["maskBackground"]["rgb-color"] == [255, 255, 255]
        assert [label["class-name"] for label in row["anomalyLabels"]] == ["scratch", "dent"]
        assert row["prediction"] == constants.DETECTION

    def test_yolo_row_also_carries_the_list(self, records):
        row = _db_row(records.live({"detections": _YOLO_MAP}, with_mask=False))
        assert row["detections"] == [_YOLO_MAP["0"]]
        assert "maskImage" not in row and "anomalyLabels" not in row

    def test_empty_map_writes_no_key(self, records):
        row = _db_row(records.live({"detections": {}}, with_mask=False))
        assert "detections" not in row

    def test_schema_and_plain_class_accept_detections(self, records):
        row = _db_row(records.live({"detections": _INSTANCE_MAP, "mask_color_map": _MASK_COLOR_MAP}, with_mask=True))
        stored = dict(row, inferenceCreationTime=1, anomalyScore=0.9, anomalyThreshod=1.0)
        loaded = InferenceResultSchema().load(stored)
        assert loaded.detections == row["detections"]
        plain = PlainInferenceResult(**{k: v for k, v in InferenceResultSchema().dump(loaded).items()})
        assert plain.detections == row["detections"]
        history = InferenceResultHistoryModel.model_validate(loaded, from_attributes=True)
        assert history.detections == row["detections"]
        # the default is None, not a new required field
        assert PlainInferenceResult(**{k: v for k, v in stored.items() if k != "detections"}).detections is None


class TestAlembicMigration:
    def test_upgrade_adds_and_downgrade_drops_the_detections_column(self):
        """Runs ``alembic upgrade head`` / ``downgrade -1`` against a scratch
        SQLite in a subprocess (the backend's session module reads the metadata
        URL at import, so the URL is patched there before alembic loads)."""
        with tempfile.TemporaryDirectory() as folder:
            db_path = os.path.join(folder, "meta.db")
            script = r'''
import os, sys, sqlite3
sys.path.insert(0, os.path.join(%(root)r, "src/backend"))
os.environ.setdefault("COMPONENT_WORK_PATH", %(folder)r)
import dao.sqlite_db.sqlite_db_operations as ops
ops.SQLALCHEMY_METADATA_DATABASE_URL = "sqlite:///%(db)s"
from alembic.config import Config
from alembic import command
cfg = Config(os.path.join(%(root)r, "src/backend/alembic.ini"), ini_section="database_metadata")
cfg.set_main_option("script_location", os.path.join(%(root)r, "src/backend/alembic/metadata_database"))
command.upgrade(cfg, "head")
con = sqlite3.connect(%(db)r)
cols = [r[1] for r in con.execute("PRAGMA table_info(inference_result_metadata)")]
ver = con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
print("UP", "detections" in cols, ver)
command.downgrade(cfg, "-1")
cols = [r[1] for r in con.execute("PRAGMA table_info(inference_result_metadata)")]
ver = con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
print("DOWN", "detections" in cols, ver)
''' % {"root": _ROOT, "folder": folder, "db": db_path}
            # The test conftest exports PYTHONHOME for Triton's embedded
            # interpreter; a child CPython must not inherit it (the backend's
            # python_bridge drops it the same way).
            env = {key: value for key, value in os.environ.items() if key != "PYTHONHOME"}
            result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                    cwd=os.path.join(_ROOT, "src", "backend"), timeout=300, env=env)
            output = result.stdout + result.stderr
            assert result.returncode == 0, output[-3000:]
            assert "UP True fe9a273bbe2c" in output, output[-3000:]
            assert "DOWN False 37b6e36641e7" in output, output[-3000:]

    def test_revision_chains_to_the_pre_seg_head(self):
        path = os.path.join(_ROOT, "src", "backend", "alembic", "metadata_database", "versions",
                            "fe9a273bbe2c_add_detections_to_inference_result.py")
        with open(path) as handle:
            text = handle.read()
        assert re.search(r"^down_revision(?::\s*[^=]+)?\s*=\s*'37b6e36641e7'", text, re.MULTILINE), text[:800]
        assert re.search(r"^revision(?::\s*[^=]+)?\s*=\s*'fe9a273bbe2c'", text, re.MULTILINE)
        assert "op.add_column('inference_result_metadata', sa.Column('detections', sa.JSON(), nullable=True))" in text
        assert "drop_column('detections')" in text


class TestResultRoutes(LocalServerBaseTestCase):
    """The stored list reaches both results routes."""

    def setUp(self):
        super().setUp()
        self.session = Session(self.metadata_engine)
        self.workflow_id = "wf-" + uuid.uuid4().hex[:8]

    def tearDown(self):
        from dao.sqlite_db.models import InferenceResult
        self.session.query(InferenceResult).filter(InferenceResult.workflowId == self.workflow_id).delete()
        self.session.commit()
        self.session.close()
        super().tearDown()

    def _store(self, capture_id, detections, prediction=constants.DETECTION):
        from dao.sqlite_db.models import InferenceResult
        row = {
            "captureId": capture_id, "captureType": constants.INFERENCE, "workflowId": self.workflow_id,
            "inferenceCreationTime": 12345, "prediction": prediction, "confidence": 0.9,
            "anomalyScore": 0.9, "anomalyThreshod": 1.0, "inputImageFilePath": "in.jpg",
            "outputImageFilePath": "out.jpg", "modelId": "model-seg", "modelName": "seg-model",
            "flagForReview": False, "downloaded": False,
        }
        if detections is not None:
            row["detections"] = detections
        self.session.add(InferenceResult(**row))
        self.session.commit()

    def test_list_and_single_row_return_the_stored_list(self):
        instance_list = [_INSTANCE_MAP["0"], _INSTANCE_MAP["1"]]
        self._store("seg-1", instance_list)
        self._store("plain-1", None, prediction=constants.NORMAL)
        with patch("utils.server_setup.workflow_accessor.get_workflow_by_id", return_value={}):
            listing = self.client.get("/workflows/{}/results".format(self.workflow_id))
            assert listing.status_code == 200
            by_id = {item["captureId"]: item for item in listing.json()["results"]}
            assert by_id["seg-1"]["detections"] == instance_list
            assert by_id["seg-1"]["detections"][0]["mask_rle"] == "27 4 8 4 8 4 8 4 125"
            assert "detections" in by_id["plain-1"] and by_id["plain-1"]["detections"] is None
            single = self.client.get("/workflows/{}/results/seg-1".format(self.workflow_id))
            assert single.status_code == 200
            assert single.json()["detections"] == instance_list
            plain = self.client.get("/workflows/{}/results/plain-1".format(self.workflow_id))
            assert plain.status_code == 200
            assert plain.json()["detections"] is None
