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
"""rfdetr-seg-support task 10.9 (Requirements 11.4, 17.1, 17.3): the device
export's Instance_Manifest (task 10.6).

Writer shape: ``generate_instance_manifest`` writes one line per row whose
stored Detection_List carries a ``mask_rle`` entry -- ``source-ref`` the image
basename, ``instance-segmentation`` with the image size read from the stored
image and one annotation per mask-carrying entry (``class_id`` into the
export-wide ``class-map``, the box as left/top/width/height ints, ``rle``),
``instance-segmentation-metadata`` with the per-instance confidences and the
fixed job name; a row whose ``detections`` is ``None`` or carries no
``mask_rle`` entry yields no line and the skipped count is logged; the
classification export is byte-identical.

Route level: a row stored through ``inference_result_accessor`` with a
list-form ``detections`` (one entry with ``mask_rle``), exported with
``manifest_format=instance`` through the FastAPI test client -- both the
time-range query and the ``POST /results/export`` -> ``captureIdPath`` ->
``GET`` sequence -- yields exactly one Instance_Manifest line whose ``rle``
equals the stored ``mask_rle``; the classification export of the same row is
byte-identical to today's; the two widened accessor methods return the
seventh key ``detections`` as ``None`` for a row stored without it; an
unknown ``manifest_format`` is rejected at validation (FastAPI's 422, which
the app's validation handler answers as 400).
"""
import io
import json
import logging
import os
import tempfile
import uuid
import zipfile

import pytest
from PIL import Image
from sqlalchemy.orm import Session
from unittest.mock import patch

from local_server_base_test_case import LocalServerBaseTestCase
from utils import constants
from utils.inference_results_utils import (
    INSTANCE_MANIFEST_JOB_NAME,
    INSTANCE_MANIFEST_TYPE,
    build_instance_class_map,
    generate_instance_manifest,
    generate_smgt_format_manifest,
)

RLE = "27 4 8 4 8 4 8 4 125"  # a 4x4 block in a 16x12 frame (column-major, background first)
INSTANCE_ENTRY = {
    "class_index": "0", "class_label": "scratch", "bounding_box": [3, 2, 8, 5],
    "confidence": 0.9, "mask_rle": RLE, "mask_area": 16,
}
DENT_ENTRY = {
    "class_index": "2", "class_label": "dent", "bounding_box": [8.4, 5.6, 13.2, 9.1],
    "confidence": 0.6, "mask_rle": "1 2 3", "mask_area": 30,
}
TRUNCATED_ENTRY = {
    "class_index": "0", "class_label": "scratch", "bounding_box": [1, 1, 2, 2],
    "confidence": 0.5, "mask_area": 1, "mask_truncated": True,
}
PLAIN_ENTRY = {"class_index": "7", "class_label": "truck", "bounding_box": [1, 2, 3, 4], "confidence": 0.8}


async def _collect_body(response):
    """The bytes of a StreamingResponse (its body_iterator is async)."""
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


@pytest.fixture
def image_path():
    folder = tempfile.mkdtemp(prefix="seg-export-")
    path = os.path.join(folder, "cap-1.jpg")
    Image.new("RGB", (16, 12)).save(path)
    return path


def _row(image_path, detections, **overrides):
    row = {
        "inputImageFilePath": image_path, "prediction": constants.DETECTION, "confidence": 0.9,
        "inferenceCreationTime": 1700000000, "humanClassification": None, "textNote": None,
        "detections": detections,
    }
    row.update(overrides)
    return row


class TestWriterShape:
    def test_one_line_per_row_with_a_mask_in_the_instance_manifest_shape(self, image_path):
        rows = [_row(image_path, [DENT_ENTRY, INSTANCE_ENTRY, TRUNCATED_ENTRY])]
        lines = generate_instance_manifest(rows)
        assert len(lines) == 1
        line = lines[0]
        assert list(line) == ["source-ref", "instance-segmentation", "instance-segmentation-metadata"]
        assert line["source-ref"] == "cap-1.jpg"
        assert line["instance-segmentation"]["image_size"] == [{"width": 16, "height": 12, "depth": 3}]
        annotations = line["instance-segmentation"]["annotations"]
        # the truncated entry (no mask_rle) is skipped; the two with masks are kept in row order
        assert annotations == [
            {"class_id": 0, "left": 8, "top": 6, "width": 5, "height": 3, "rle": "1 2 3"},
            {"class_id": 1, "left": 3, "top": 2, "width": 5, "height": 3, "rle": RLE},
        ]
        metadata = line["instance-segmentation-metadata"]
        assert metadata["objects"] == [{"confidence": 0.6}, {"confidence": 0.9}]
        assert metadata["class-map"] == {"0": "dent", "1": "scratch"}
        assert metadata["type"] == INSTANCE_MANIFEST_TYPE == "groundtruth/instance-segmentation"
        assert metadata["human-annotated"] == "no"
        assert metadata["creation-date"] == "2023-11-14T22:13:20"
        assert metadata["job-name"] == INSTANCE_MANIFEST_JOB_NAME == "inference-results-export"

    def test_class_map_is_the_sorted_labels_of_the_whole_export(self, image_path):
        rows = [_row(image_path, [INSTANCE_ENTRY]), _row(image_path, [DENT_ENTRY]),
                _row(image_path, [dict(INSTANCE_ENTRY, class_label="chip")])]
        assert build_instance_class_map(rows) == {"0": "chip", "1": "dent", "2": "scratch"}
        lines = generate_instance_manifest(rows)
        assert [line["instance-segmentation"]["annotations"][0]["class_id"] for line in lines] == [2, 1, 0]
        assert all(line["instance-segmentation-metadata"]["class-map"] == {"0": "chip", "1": "dent", "2": "scratch"}
                   for line in lines)

    def test_rows_without_a_mask_yield_no_line_and_are_counted(self, image_path, caplog):
        rows = [
            _row(image_path, None),
            _row(image_path, []),
            _row(image_path, [PLAIN_ENTRY]),
            _row(image_path, [TRUNCATED_ENTRY]),
            _row(image_path, [INSTANCE_ENTRY]),
        ]
        with caplog.at_level(logging.INFO, logger="utils.inference_results_utils"):
            lines = generate_instance_manifest(rows)
        assert len(lines) == 1
        assert lines[0]["instance-segmentation"]["annotations"][0]["rle"] == RLE
        messages = [record.getMessage() for record in caplog.records if "Instance manifest" in record.getMessage()]
        assert messages, caplog.messages
        assert "1 line(s) written, 4 row(s) without an instance mask skipped" in messages[-1]

    def test_no_rows_with_masks_yields_an_empty_manifest(self, image_path):
        assert generate_instance_manifest([_row(image_path, None), _row(image_path, [PLAIN_ENTRY])]) == []

    def test_unreadable_image_skips_the_row(self):
        rows = [_row("/nonexistent/path/cap.jpg", [INSTANCE_ENTRY])]
        assert generate_instance_manifest(rows) == []

    def test_classification_export_is_byte_identical_with_the_seventh_key(self, image_path):
        with_key = [_row(image_path, [INSTANCE_ENTRY], prediction=constants.ANOMALY, confidence=0.7)]
        without_key = [{k: v for k, v in with_key[0].items() if k != "detections"}]
        assert json.dumps(generate_smgt_format_manifest(with_key)) == json.dumps(generate_smgt_format_manifest(without_key))
        line = generate_smgt_format_manifest(with_key)[0]
        assert set(line) == {"source-ref", "source-ref-metadata", "anomaly-label", "anomaly-label-metadata"}
        assert line["anomaly-label"] == 1


class TestExportRoutes(LocalServerBaseTestCase):
    """The two route entry points against the REAL accessor, DAO and metadata
    database (the writer reads the stored list-form column)."""

    def setUp(self):
        super().setUp()
        self.session = Session(self.metadata_engine)
        self.workflow_id = "wf-" + uuid.uuid4().hex[:8]
        self.dir = tempfile.mkdtemp(prefix="seg-export-route-")
        self.image_path = os.path.join(self.dir, "seg-1.jpg")
        Image.new("RGB", (16, 12)).save(self.image_path)
        self.plain_image_path = os.path.join(self.dir, "plain-1.jpg")
        Image.new("RGB", (16, 12)).save(self.plain_image_path)

    def tearDown(self):
        from dao.sqlite_db.models import InferenceResult
        self.session.query(InferenceResult).filter(InferenceResult.workflowId == self.workflow_id).delete()
        self.session.commit()
        self.session.close()
        super().tearDown()

    def _store(self, capture_id, image_path, detections=None, prediction=constants.DETECTION):
        from utils.server_setup import inference_result_accessor
        row = {
            "captureId": capture_id, "captureType": constants.INFERENCE, "workflowId": self.workflow_id,
            "inferenceCreationTime": 1700000000, "prediction": prediction, "confidence": 0.9,
            "anomalyScore": 0.9, "anomalyThreshod": 1.0, "inputImageFilePath": image_path,
            "outputImageFilePath": image_path, "modelId": "model-seg", "modelName": "seg-model",
            "flagForReview": False, "downloaded": False,
        }
        if detections is not None:
            row["detections"] = detections
        return inference_result_accessor.store_inference_result(self.session, row)

    def _manifest_lines(self, response):
        assert response.status_code == 200, response.text
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            assert "manifest" in names
            text = archive.read("manifest").decode("utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()], names

    def test_time_range_query_writes_the_instance_manifest(self):
        self._store("seg-1", self.image_path, [INSTANCE_ENTRY])
        self._store("plain-1", self.plain_image_path, [PLAIN_ENTRY])
        with patch("endpoints.download_file.DDA_SYSTEM_FOLDER", self.dir):
            response = self.client.get(
                "/workflows/{}/results/export".format(self.workflow_id),
                params={"startTime": 0, "endTime": 2000000000, "inputImageLimit": 10, "manifest_format": "instance"})
            lines, names = self._manifest_lines(response)
        assert len(lines) == 1
        assert lines[0]["source-ref"] == "seg-1.jpg"
        assert lines[0]["instance-segmentation"]["annotations"] == [
            {"class_id": 0, "left": 3, "top": 2, "width": 5, "height": 3, "rle": RLE}]
        assert lines[0]["instance-segmentation-metadata"]["class-map"] == {"0": "scratch"}
        # both images still ship in the zip, as the classification export does
        assert sorted(names) == ["manifest", "plain-1.jpg", "seg-1.jpg"]

    def test_capture_id_path_sequence_writes_the_instance_manifest(self):
        """``POST /results/export`` (through the client) writes the scratch file
        with the seventh key; the GET's ``captureIdPath`` Query pattern pins
        ``/aws_dda/system/...``, which the test container cannot create, so the
        GET half calls the route function directly on the file the POST wrote
        (its containment check still runs against the patched system folder),
        as ``test_export_path_containment.py`` does."""
        import asyncio
        from endpoints import download_file

        self._store("seg-1", self.image_path, [INSTANCE_ENTRY])
        self._store("plain-1", self.plain_image_path, None, prediction=constants.NORMAL)
        with patch("endpoints.download_file.DDA_SYSTEM_FOLDER", self.dir), \
             patch("endpoints.inference_result.DDA_SYSTEM_FOLDER", self.dir):
            posted = self.client.post("/workflows/{}/results/export".format(self.workflow_id),
                                      json={"captureIds": ["seg-1", "plain-1"]})
            assert posted.status_code == 200, posted.text
            capture_id_path = posted.json()["captureIdPath"]
            assert capture_id_path.startswith(self.dir)
            with open(capture_id_path) as handle:
                scratch_rows = json.load(handle)
            by_path = {row["inputImageFilePath"]: row for row in scratch_rows}
            assert by_path[self.image_path]["detections"] == [INSTANCE_ENTRY]
            assert by_path[self.plain_image_path]["detections"] is None
            response = download_file.get_inference_result_data_for_retraining(
                self.workflow_id, captureIdPath=capture_id_path, manifest_format="instance",
                token=None, db=self.session)
            content = asyncio.run(_collect_body(response))
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            lines = [json.loads(line) for line in archive.read("manifest").decode("utf-8").splitlines() if line.strip()]
        assert sorted(names) == ["manifest", "plain-1.jpg", "seg-1.jpg"]
        assert len(lines) == 1
        assert lines[0]["instance-segmentation"]["annotations"][0]["rle"] == RLE
        assert lines[0]["source-ref"] == "seg-1.jpg"
        # the same scratch file through the default writer: the classification manifest, two lines
        with patch("endpoints.download_file.DDA_SYSTEM_FOLDER", self.dir):
            default = download_file.get_inference_result_data_for_retraining(
                self.workflow_id, captureIdPath=capture_id_path, token=None, db=self.session)
            content = asyncio.run(_collect_body(default))
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            lines = [json.loads(line) for line in archive.read("manifest").decode("utf-8").splitlines() if line.strip()]
        assert len(lines) == 2 and all("anomaly-label" in line for line in lines)

    def test_classification_export_of_the_same_rows_is_byte_identical(self):
        self._store("seg-1", self.image_path, [INSTANCE_ENTRY])
        self._store("plain-1", self.plain_image_path, None, prediction=constants.NORMAL)
        with patch("endpoints.download_file.DDA_SYSTEM_FOLDER", self.dir):
            params = {"startTime": 0, "endTime": 2000000000, "inputImageLimit": 10}
            default = self.client.get("/workflows/{}/results/export".format(self.workflow_id), params=params)
            explicit = self.client.get("/workflows/{}/results/export".format(self.workflow_id),
                                       params=dict(params, manifest_format="classification"))
            lines_default, _ = self._manifest_lines(default)
            lines_explicit, _ = self._manifest_lines(explicit)
        assert lines_default == lines_explicit
        assert len(lines_default) == 2
        for line in lines_default:
            assert set(line) == {"source-ref", "source-ref-metadata", "anomaly-label", "anomaly-label-metadata"}
            assert line["anomaly-label-metadata"]["type"] == "groundtruth/image-classification"
        # the writer reads named fields only: the stored list never appears
        assert "mask_rle" not in json.dumps(lines_default)

    def test_unknown_manifest_format_is_rejected_at_validation(self):
        """FastAPI raises its 422 RequestValidationError for a value outside
        the Query pattern; the app's ``request_validation_exception_handler``
        answers it as 400 (the same mapping the ``captureIdPath`` pattern
        rejection takes in ``test_export_path_containment.py``), before the
        handler runs."""
        self._store("seg-1", self.image_path, [INSTANCE_ENTRY])
        with patch("endpoints.download_file.DDA_SYSTEM_FOLDER", self.dir), \
             patch("utils.server_setup.inference_result_accessor.list_inference_result_data_for_retraining") as listing:
            response = self.client.get("/workflows/{}/results/export".format(self.workflow_id),
                                       params={"startTime": 0, "endTime": 2000000000, "manifest_format": "coco"})
        assert response.status_code == 400, response.text
        assert "manifest_format" in response.text
        listing.assert_not_called()

    def test_widened_accessor_methods_return_none_for_a_row_without_a_list(self):
        from utils.server_setup import inference_result_accessor
        self._store("plain-1", self.plain_image_path, None, prediction=constants.NORMAL)
        self._store("seg-1", self.image_path, [INSTANCE_ENTRY])
        by_ids = inference_result_accessor.list_inference_result_data_from_capture_id_list(
            self.session, ["plain-1", "seg-1"])
        by_path = {row["inputImageFilePath"]: row for row in by_ids}
        assert set(by_path[self.plain_image_path]) == {"inputImageFilePath", "prediction", "confidence",
                                                        "inferenceCreationTime", "humanClassification",
                                                        "textNote", "detections"}
        assert by_path[self.plain_image_path]["detections"] is None
        assert by_path[self.image_path]["detections"] == [INSTANCE_ENTRY]
        for_retraining = inference_result_accessor.list_inference_result_data_for_retraining(
            self.session, self.workflow_id, {"inputImageLimit": 10})
        by_path = {row["inputImageFilePath"]: row for row in for_retraining}
        assert by_path[self.plain_image_path]["detections"] is None
        assert by_path[self.image_path]["detections"] == [INSTANCE_ENTRY]
