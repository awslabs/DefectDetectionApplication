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
"""Item 3 (export route) spec-named cases for
localserver-input-handling-hardening that the frozen task-1/2 module does not
pin. They live in their own module so the frozen file stays byte-identical,
and they import only Item 3's frozen helpers (``test_export_path_containment``),
so Item 3's tests revert together with Item 3's fix. The lifted route is still
the shipped route compiled with ``ast``.

Covered here:
- AC5: the exact capture-id-path shape plus a trailing newline is rejected at
  validation (400) by the mounted export route, with the handler never entered.
- Item 3 containment (positive branch): a contained, existing capture file is
  opened on the host, exercising the branch the frozen Property 4 containment
  half never reaches.
- AC6 end to end (device container only): the exact writer-shape value written
  under the real system folder round-trips through the mounted route to the zip.
  It skips when the writer-shape file already exists there, and removes only
  the files it created.
"""
import io
import json
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
    and ``inference_results_utils`` needs the device stack. The test keeps its
    footprint in that shared folder to what it wrote: it skips when the
    writer-shape file already exists, and afterwards removes only that file and
    the ``manifest-*`` entries that were not there before the request."""

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
        writer_file = _WRITER_SHAPE  # /…/system/capture-id-path-2025-01-01-00-00-00
        if os.path.lexists(writer_file):
            pytest.skip("the writer-shape file already exists in the system folder")
        # Snapshot the real listing before anything is written or requested,
        # so the cleanup removes only entries this test created.
        listed_before = set(os.listdir(real_system))
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
            # Remove only what this test wrote into the shared system folder:
            # the writer-shape file and any manifest-* entry that is new since
            # the snapshot.
            for name in os.listdir(real_system):
                if name.startswith("manifest-") and name not in listed_before:
                    created.append(os.path.join(real_system, name))
            for path in created:
                try:
                    os.remove(path)
                except OSError:
                    pass
