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
"""Spec-named boundary cases for localserver-input-handling-hardening that the
frozen task-1/2 modules do not pin, kept in a separate module so those files
stay byte-identical to their frozen copies. The reusable lifted-route helpers
are imported from the frozen modules, so the code under test is still the
shipped route compiled with ``ast``.

Covered here:
- AC5: the exact capture-id-path shape plus a trailing newline is rejected at
  validation (400) by the mounted export route, with the handler never entered.
- Requirement 2.2: a contained but malformed input-image jsonl logs exactly one
  WARNING record carrying ``exc_info`` while answering 500 with the fixed
  detail — that record is now the only diagnostic for the failure.
- Requirement 2.4: ``_failure_reason`` for a ``GenerationError`` with a falsy
  reason returns the fixed string and logs one ERROR record.
- Item 3 containment (positive branch): a contained, existing capture file is
  opened on the host, exercising the branch the frozen Property 4 containment
  half never reaches.
- AC6 end to end (device container only): the exact writer-shape value written
  under the real system folder round-trips through the mounted route to the zip.
"""
import io
import json
import logging
import os
import zipfile

import pytest

from fastapi import HTTPException

from test_export_path_containment import (
    _iru as _EX_IRU,
    _lift as _ex_lift,
    _mounted as _ex_mounted,
    _seed as _ex_seed,
    _TokenRecorder as _ExTokenRecorder,
    _WRITER_SHAPE,
)
from test_input_image_containment import (
    _lift as _ii_lift,
    _seed_tree as _ii_seed_tree,
)
from endpoints import text_generation
from vllm_runtime.manager import GenerationError

# ``test_failure_reason`` lives under ``text_generation/`` which is not on the
# workflow_engine test path, so these two values are repeated here rather than
# imported (they match that module's ``_FIXED`` and ``_MODEL``).
_FIXED = "internal error; see the LocalServer log"
_MODEL = "qwen2-vl"
_DOWNLOAD_LOGGER = "endpoints.download_file"


class _CollectingHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _records_from(logger_name, level, run):
    logger = logging.getLogger(logger_name)
    handler = _CollectingHandler()
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        run()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return [record for record in handler.records if record.levelno == level]


class TestExportValidationBoundary:
    """AC5: the mounted export route rejects a writer-shape value with a
    trailing newline before the handler runs."""

    def test_writer_shape_with_a_trailing_newline_is_rejected(self, tmp_path):
        system = _ex_seed(tmp_path)
        recorder = _ExTokenRecorder()
        client, _open_calls = _ex_mounted(system, recorder)
        response = client.get(
            "/workflows/wf/results/export",
            params={"captureIdPath": _WRITER_SHAPE + "\n"})
        assert response.status_code == 400
        assert recorder.calls == 0


class TestInputImageContainedFailureLog:
    """Requirement 2.2: a contained read/parse failure logs one WARNING with
    exc_info and answers 500 with the fixed detail."""

    def test_a_contained_malformed_jsonl_logs_one_warning_with_exc_info(
        self, tmp_path
    ):
        inference_root, _image = _ii_seed_tree(tmp_path)
        with open(os.path.join(inference_root, "wf", "bad.jsonl"), "w") as handle:
            handle.write("{not json")
        open_calls = []
        route = _ii_lift(inference_root, open_calls)
        caught = {}

        def _run():
            with pytest.raises(HTTPException) as info:
                route("wf", "bad", token=None, db=None)
            caught["value"] = info.value

        warnings = _records_from(_DOWNLOAD_LOGGER, logging.WARNING, _run)
        assert caught["value"].status_code == 500
        assert caught["value"].detail == "Server cannot find the input image path."
        assert len(warnings) == 1
        assert warnings[0].exc_info is not None
        assert warnings[0].exc_info[0] is not None


class TestFailureReasonEmptyReason:
    """Requirement 2.4: a GenerationError with a falsy reason returns the fixed
    string and logs one ERROR record carrying the error."""

    def test_empty_reason_generation_error_returns_the_fixed_string(self):
        error = GenerationError(_MODEL, "")
        result = {}

        def _run():
            result["value"] = text_generation._failure_reason(error)

        errors = _records_from(text_generation.logger.name, logging.ERROR, _run)
        assert result["value"] == _FIXED
        assert len(errors) == 1
        assert errors[0].exc_info[1] is error


class TestExportContainmentPositiveBranch:
    """Item 3 containment, positive branch: a contained, existing capture file
    is opened on the host (the frozen Property 4 containment half never seeds a
    contained, existing file, so its opened branch does not run)."""

    def test_a_contained_existing_capture_file_is_opened(self, tmp_path):
        system = _ex_seed(tmp_path)
        # Inside the seeded capture-id-path directory, so realpath keeps it
        # strictly below the system folder.
        contained = os.path.join(
            system, "capture-id-path-2025-01-01-00-00-00", "list.json")
        with open(contained, "w") as handle:
            json.dump([], handle)
        open_calls = []
        route = _ex_lift(system, open_calls, _ExTokenRecorder())
        with pytest.raises(HTTPException) as caught:
            route("wf", captureIdPath=contained, token=None, db=None)
        # The guard let it through to open; an empty list then yields 442.
        assert open_calls, "a contained, existing path must be opened"
        assert caught.value.status_code == 442


class TestExportWriterShapeEndToEnd:
    """AC6 end to end: the exact writer-shape value, written under the real
    system folder, round-trips through the mounted route to the zip. Device
    container only — the real path must resolve under the real system folder,
    and ``inference_results_utils`` needs the device stack."""

    @pytest.mark.skipif(_EX_IRU is None,
                        reason="inference_results_utils needs the device stack")
    def test_writer_shape_round_trips_to_the_zip(self, tmp_path):
        real_system = os.path.realpath(_WRITER_SHAPE).rsplit(
            "/capture-id-path-", 1)[0]
        try:
            os.makedirs(real_system, exist_ok=True)
        except OSError:
            pytest.skip("cannot create the real system folder")
        if not os.access(real_system, os.W_OK):
            pytest.skip("the real system folder is not writable")
        image_one = os.path.join(str(tmp_path), "a.jpg")
        image_two = os.path.join(str(tmp_path), "b.jpg")
        for path in (image_one, image_two):
            with open(path, "wb") as handle:
                handle.write(b"jpeg")
        rows = [
            {"inputImageFilePath": image_one, "humanClassification": None,
             "prediction": "Normal", "confidence": 0.9, "textNote": "",
             "inferenceCreationTime": 1700000000},
            {"inputImageFilePath": image_two, "humanClassification": "Anomaly",
             "prediction": "Anomaly", "confidence": 0.8, "textNote": "n",
             "inferenceCreationTime": 1700000001},
        ]
        writer_file = _WRITER_SHAPE  # /…/system/capture-id-path-2025-01-01-00-00-00
        created = []
        with open(writer_file, "w") as handle:
            json.dump(rows, handle)
        created.append(writer_file)
        try:
            recorder = _ExTokenRecorder()
            client, _open_calls = _ex_mounted(real_system, recorder)
            response = client.get(
                "/workflows/wf/results/export",
                params={"captureIdPath": _WRITER_SHAPE})
            assert response.status_code == 200
            names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
            assert "manifest" in names
            assert os.path.basename(image_one) in names
            assert os.path.basename(image_two) in names
            assert recorder.calls == 1
        finally:
            # Remove the files this test wrote into the shared system folder.
            for name in os.listdir(real_system):
                if name.startswith("manifest-"):
                    created.append(os.path.join(real_system, name))
            for path in created:
                try:
                    os.remove(path)
                except OSError:
                    pass
