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
"""Export-route captureIdPath anchoring and containment
(localserver-input-handling-hardening Item 3).

Lifts the production ``get_inference_result_data_for_retraining`` and the
``RetrainInputImagesRequest`` model out of ``endpoints/download_file.py`` with
``ast`` (the ``test_overlay_image_serving.py`` pattern) and compiles them
against stub collaborators and a recording ``open``. The validator half is
asserted through a minimal FastAPI app that registers the real
``request_validation_exception_handler``, because the engine's anchoring (not
Python ``re``) is what governs; the oracle is the test's own
``re.fullmatch`` of bugfix 2.3's shape, independent of the code. The
containment half calls the lifted function directly over a seeded temp tree.

The task-1 cases assert the fixed behavior and FAIL on the unfixed tree; the
task-2 cases PASS on the unfixed tree and stay frozen. The authoritative run is
the device container (3.11); the explicit NUL member is skipped below 3.10, and
the zip-building preservation case is skipped where ``inference_results_utils``
is not importable (the host lacks ``awsiot``).
"""
import ast
import builtins
import io
import json
import os
import pathlib
import sys
import zipfile
from datetime import datetime
from typing import List, Optional

import pytest
from pydantic import BaseModel
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.testclient import TestClient
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.status import HTTP_404_NOT_FOUND

from exceptions.handlers import exception_handlers
from utils.constants import CAPTURED_IDS_PATH_PATTERN, DDA_SYSTEM_FOLDER, PREDICTION

try:
    from utils import inference_results_utils as _iru
except Exception:  # noqa: BLE001 - host lacks awsiot; the zip case skips
    _iru = None

_DOWNLOAD_FILE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "src" / "backend" / "endpoints" / "download_file.py"
)
_ROUTE = "get_inference_result_data_for_retraining"
_SHAPE = r"/aws_dda/system/capture-id-path-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}"
_WRITER_SHAPE = "/aws_dda/system/capture-id-path-2025-01-01-00-00-00"


def _production():
    tree = ast.parse(_DOWNLOAD_FILE.read_text(), filename=str(_DOWNLOAD_FILE))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    return funcs, classes


class _TokenRecorder:
    def __init__(self):
        self.calls = 0

    def __call__(self, token):
        self.calls += 1


def _get_db():
    yield None


def _drain(body_iterator):
    """Collect a StreamingResponse body_iterator (async on this starlette)."""
    import asyncio

    async def _collect():
        chunks = []
        async for chunk in body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
        return b"".join(chunks)

    return asyncio.run(_collect())


def _namespace(system_folder, open_calls, token_recorder, result_rows=None):
    def recording_open(path, *args, **kwargs):
        open_calls.append(path)
        return builtins.open(path, *args, **kwargs)

    class _Accessor:
        def list_inference_result_data_for_retraining(self, db, workflow_id, data):
            return result_rows or []

    return {
        # The lifted validate_token_in_query_param calls authorize_credential;
        # recording it counts handler entries (token-check reached).
        "authorize_credential": token_recorder,
        "inference_result_accessor": _Accessor(),
        "inference_results_utils": _iru,
        "HTTPException": HTTPException,
        "HTTP_404_NOT_FOUND": HTTP_404_NOT_FOUND,
        "StreamingResponse": StreamingResponse,
        "Query": Query,
        "Depends": Depends,
        "Session": Session,
        "get_db": _get_db,
        "os": os,
        "io": io,
        "json": json,
        "zipfile": zipfile,
        "datetime": datetime,
        "logger": __import__("logging").getLogger("endpoints.download_file"),
        "open": recording_open,
        "DDA_SYSTEM_FOLDER": system_folder,
        "PREDICTION": PREDICTION,
        "CAPTURED_IDS_PATH_PATTERN": CAPTURED_IDS_PATH_PATTERN,
        "BaseModel": BaseModel,
        "Optional": Optional,
        "List": List,
    }


def _lift(system_folder, open_calls, token_recorder, result_rows=None):
    funcs, classes = _production()
    route = funcs[_ROUTE]
    route.decorator_list = []
    body = [classes["RetrainInputImagesRequest"],
            funcs["validate_token_in_query_param"], route]
    module = ast.Module(body=body, type_ignores=[])
    namespace = _namespace(system_folder, open_calls, token_recorder, result_rows)
    exec(compile(module, str(_DOWNLOAD_FILE), "exec"), namespace)
    return namespace[_ROUTE]


def _mounted(system_folder, token_recorder):
    """The lifted route on a minimal app with the real validation handler."""
    open_calls = []
    route = _lift(system_folder, open_calls, token_recorder)
    router = APIRouter()
    router.get("/workflows/{workflow_id}/results/export")(route)
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(
        __import__("fastapi").exceptions.RequestValidationError,
        exception_handlers.request_validation_exception_handler,
    )
    return TestClient(app), open_calls


def _seed(tmp_path):
    """``<tmp>/aws_dda/system`` with the capture-id-path directory, and a
    ``target.json`` list outside it. Returns the system folder."""
    system = os.path.join(str(tmp_path), "aws_dda", "system")
    os.makedirs(os.path.join(system, "capture-id-path-2025-01-01-00-00-00"))
    with open(os.path.join(str(tmp_path), "target.json"), "w") as handle:
        json.dump([], handle)
    return system


_ESCAPE_VALUE = _WRITER_SHAPE + "/../../../target.json"


class TestItem3Containment:
    """task 1: anchoring rejects at validation; containment 404s before open."""

    def test_a_dotdot_value_is_rejected_at_validation(self, tmp_path):
        # (a) FAILS unfixed: the unanchored pattern accepts, entering the handler.
        system = _seed(tmp_path)
        recorder = _TokenRecorder()
        client, _open_calls = _mounted(system, recorder)
        response = client.get(
            "/workflows/wf/results/export", params={"captureIdPath": _ESCAPE_VALUE})
        assert response.status_code == 400
        assert recorder.calls == 0

    def test_a_dotdot_value_opens_nothing_on_a_direct_call(self, tmp_path):
        # (b) FAILS unfixed: the direct call opens the escaped target.
        system = _seed(tmp_path)
        # Built from the temp system dir so the escaped target.json exists.
        value = os.path.join(system, "capture-id-path-2025-01-01-00-00-00",
                             "..", "..", "..", "target.json")
        open_calls = []
        route = _lift(system, open_calls, _TokenRecorder())
        with pytest.raises(HTTPException) as caught:
            route("wf", captureIdPath=value, token=None, db=None)
        assert caught.value.status_code == 404
        assert open_calls == []

    def test_a_missing_accepted_value_has_the_fixed_404_detail(self, tmp_path):
        # (c) FAILS unfixed: the 404 detail echoes the value.
        system = _seed(tmp_path)
        missing = os.path.join(system, "capture-id-path-2025-01-01-00-00-00",
                               "nope.json")
        open_calls = []
        route = _lift(system, open_calls, _TokenRecorder())
        with pytest.raises(HTTPException) as caught:
            route("wf", captureIdPath=missing, token=None, db=None)
        assert caught.value.status_code == 404
        assert caught.value.detail == (
            "The server can't get the capture id file. Check the path and try again.")
        assert missing not in str(caught.value.detail)

    def test_a_capture_name_symlink_resolving_outward_is_404(self, tmp_path):
        # (d, AC5) FAILS unfixed: it resolves and opens the symlink target.
        system = os.path.join(str(tmp_path), "aws_dda", "system")
        os.makedirs(system)
        outside = os.path.join(str(tmp_path), "outside.json")
        with open(outside, "w") as handle:
            json.dump([{"inputImageFilePath": "x"}], handle)
        link = os.path.join(system, "capture-id-path-2025-01-01-00-00-00")
        os.symlink(outside, link)
        open_calls = []
        route = _lift(system, open_calls, _TokenRecorder())
        with pytest.raises(HTTPException) as caught:
            route("wf", captureIdPath=link, token=None, db=None)
        assert caught.value.status_code == 404
        assert open_calls == []

    @pytest.mark.skipif(sys.version_info < (3, 10),
                        reason="realpath raises on an embedded NUL only on 3.10+")
    def test_embedded_nul_member_is_404_with_the_fixed_detail(self, tmp_path):
        # FAILS unfixed: the 404 detail echoes the value (incl. the NUL).
        system = _seed(tmp_path)
        value = os.path.join(system, "capture-id-path-2025-01-01-00-00-00") + "\x00x"
        open_calls = []
        route = _lift(system, open_calls, _TokenRecorder())
        with pytest.raises(HTTPException) as caught:
            route("wf", captureIdPath=value, token=None, db=None)
        assert caught.value.status_code == 404
        assert caught.value.detail == (
            "The server can't get the capture id file. Check the path and try again.")
        assert open_calls == []

    @settings(max_examples=50, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(prefix=st.sampled_from(["", "x", "/", "..", "."]),
           suffix=st.sampled_from(["", "/../", "token=DUMMY", "x", "/sub", "\t"]),
           digits=st.sampled_from(["2025-01-01-00-00-00", "2025-1-01-00-00-00",
                                   "٢٠٢٥-01-01-00-00-00", "2025-01-01-00-00"]))
    @example(prefix="", suffix="/../../../target.json", digits="2025-01-01-00-00-00")
    def test_property_4_validator(self, tmp_path_factory, prefix, suffix, digits):
        # The validator accepts iff the value fully matches bugfix 2.3's shape.
        # FAILS unfixed for any accepted-but-non-matching value.
        tmp_path = tmp_path_factory.mktemp("item3-val")
        system = _seed(tmp_path)
        value = prefix + "/aws_dda/system/capture-id-path-" + digits + suffix
        recorder = _TokenRecorder()
        client, _open_calls = _mounted(system, recorder)
        response = client.get(
            "/workflows/wf/results/export", params={"captureIdPath": value})
        shape = r"/aws_dda/system/capture-id-path-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}"
        accepted = __import__("re").fullmatch(shape, value) is not None
        if accepted:
            assert response.status_code != 400
            assert recorder.calls == 1
        else:
            assert response.status_code == 400
            assert recorder.calls == 0

    @settings(max_examples=50, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(segments=st.lists(
        st.sampled_from(["capture-id-path-2025-01-01-00-00-00", "..", ".", "sub"]),
        max_size=4),
        filename=st.sampled_from(["target.json", "missing"]))
    @example(segments=["capture-id-path-2025-01-01-00-00-00", "..", "..", ".."],
             filename="target.json")
    def test_property_4_containment(self, tmp_path_factory, segments, filename):
        # Opened iff the resolved path is strictly below the base and exists;
        # otherwise 404 and no open. FAILS unfixed when a value escapes.
        tmp_path = tmp_path_factory.mktemp("item3-con")
        system = _seed(tmp_path)
        value = os.path.join(system, *segments, filename) if segments \
            else os.path.join(system, filename)
        base = os.path.realpath(system)
        resolved = os.path.realpath(value)
        contained = resolved.startswith(base + os.sep)
        # Seed the escape target so the UNFIXED route has a file to open; keep
        # writes inside tmp_path.
        if (not contained and resolved.startswith(os.path.realpath(str(tmp_path)) + os.sep)
                and not os.path.isdir(resolved)):
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            with open(resolved, "w") as handle:
                json.dump([], handle)
        open_calls = []
        route = _lift(system, open_calls, _TokenRecorder())
        exists = os.path.exists(resolved)
        if contained and exists:
            # The guard lets it through to open (then 442/zip/dir-error).
            try:
                route("wf", captureIdPath=value, token=None, db=None)
            except Exception:  # noqa: BLE001 - downstream status is not asserted here
                pass
            assert open_calls, "a contained, existing path must be opened"
        else:
            with pytest.raises(HTTPException) as caught:
                route("wf", captureIdPath=value, token=None, db=None)
            assert caught.value.status_code == 404
            assert open_calls == []


class TestItem3Preservation:
    """task 2: the writer shape is accepted and a seeded list yields the zip."""

    def test_the_exact_writer_shape_enters_the_handler(self, tmp_path):
        system = _seed(tmp_path)
        # Seed the writer-shape file so, once accepted, the handler reads it.
        with open(os.path.join(system, "capture-id-path-2025-01-01-00-00-00",
                               "..", "list.json"), "w") as handle:
            json.dump([], handle)
        recorder = _TokenRecorder()
        client, _open_calls = _mounted(system, recorder)
        # The writer returns the directory-shaped path; here we assert only
        # that the validator accepts the exact shape (the handler is entered).
        response = client.get(
            "/workflows/wf/results/export",
            params={"captureIdPath": _WRITER_SHAPE})
        assert response.status_code != 400
        assert recorder.calls == 1

    @pytest.mark.skipif(_iru is None,
                        reason="inference_results_utils needs the device stack")
    def test_a_seeded_capture_id_path_file_yields_the_zip(self, tmp_path):
        system = _seed(tmp_path)
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
        capture_file = os.path.join(system, "capture-id-path-2025-01-01-00-00-00.json")
        with open(capture_file, "w") as handle:
            json.dump(rows, handle)
        open_calls = []
        route = _lift(system, open_calls, _TokenRecorder())
        response = route("wf", captureIdPath=capture_file, token=None, db=None)
        assert isinstance(response, StreamingResponse)
        body = _drain(response.body_iterator)
        names = zipfile.ZipFile(io.BytesIO(body)).namelist()
        assert "manifest" in names
        assert os.path.basename(image_one) in names
        assert os.path.basename(image_two) in names
