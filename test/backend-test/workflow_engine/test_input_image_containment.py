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
"""Input-image inference-results path containment
(localserver-input-handling-hardening Item 2).

``endpoints/download_file.py`` imports the device-only stack, so this module
lifts the production ``load_input_image_from_worflow_by_capture_id`` and
``validate_token_in_query_param`` function definitions out of the file with
``ast`` and compiles them against stub collaborators and a recording ``open``
(the ``test_overlay_image_serving.py`` pattern), so the code under test is
byte-for-byte the shipped route. ``INFERENCE_RESULTS_DIR`` is patched to a temp
root and the ``open`` shim maps the literal ``/aws_dda/inference-results/``
prefix the unfixed route builds onto it, so the same seed is read before and
after the fix.

The task-1 cases assert the fixed behavior and FAIL on the unfixed tree; the
task-2 cases PASS on the unfixed tree and stay frozen. The authoritative run
is the device container (Python 3.11), which raises ``ValueError`` on an
embedded NUL; the host (3.9.5) does not, so the explicit NUL member is skipped
below 3.10 and the Property 3 generator excludes ``\\x00``.
"""
import ast
import builtins
import json
import os
import pathlib
import sys

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from starlette.responses import FileResponse
from starlette.status import HTTP_404_NOT_FOUND

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

_DOWNLOAD_FILE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "src" / "backend" / "endpoints" / "download_file.py"
)
_ROUTE = "load_input_image_from_worflow_by_capture_id"
_INFERENCE_PREFIX = "/aws_dda/inference-results"


class _Row:
    def __init__(self, image_path):
        self.inputImageFilePath = image_path


def _production_functions():
    tree = ast.parse(_DOWNLOAD_FILE.read_text(), filename=str(_DOWNLOAD_FILE))
    return {node.name: node for node in tree.body
            if isinstance(node, ast.FunctionDef)}


def _lift(inference_root, open_calls, row=None):
    """Compile the lifted route into a namespace where the inference-results
    root is ``inference_root`` and ``open`` is recorded + prefix-mapped."""
    functions = _production_functions()
    route = functions[_ROUTE]
    route.decorator_list = []
    module = ast.Module(
        body=[functions["validate_token_in_query_param"], route],
        type_ignores=[],
    )

    def recording_open(path, *args, **kwargs):
        open_calls.append(path)
        mapped = path
        if isinstance(path, str) and path.startswith(_INFERENCE_PREFIX):
            mapped = inference_root + path[len(_INFERENCE_PREFIX):]
        return builtins.open(mapped, *args, **kwargs)

    class _Accessor:
        def get_inference_result(self, db, capture_id):
            return row

    namespace = {
        "validate_token_in_query_param": lambda token: None,
        "authorize_credential": lambda token: None,
        "get_is_triton": lambda: True,
        "inference_result_accessor": _Accessor(),
        "HTTPException": HTTPException,
        "HTTP_404_NOT_FOUND": HTTP_404_NOT_FOUND,
        "FileResponse": FileResponse,
        "Session": Session,
        "Depends": Depends,
        "get_db": None,
        "json": json,
        "os": os,
        "time": __import__("time"),
        "logger": __import__("logging").getLogger("endpoints.download_file"),
        "open": recording_open,
        "INFERENCE_RESULTS_DIR": inference_root,
    }
    exec(compile(module, str(_DOWNLOAD_FILE), "exec"), namespace)
    return namespace[_ROUTE]


def _seed_tree(tmp_path):
    """A temp inference-results root with an image and a valid jsonl for the
    legitimate (wf, cap-1) pair, plus a one-level-up jsonl a ``..`` workflow
    id would reach. Returns (inference_root, image_path)."""
    inference_root = os.path.join(str(tmp_path), "inference-results")
    os.makedirs(os.path.join(inference_root, "wf"))
    image_path = os.path.join(str(tmp_path), "image-cap-1.jpg")
    with open(image_path, "wb") as handle:
        handle.write(b"jpeg-bytes")
    with open(os.path.join(inference_root, "wf", "cap-1.jsonl"), "w") as handle:
        json.dump({"deviceGroundTruthData": [{"source-ref": image_path}]}, handle)
    # One level above the root: /aws_dda/inference-results/../<cid>.jsonl.
    with open(os.path.join(str(tmp_path), "escape.jsonl"), "w") as handle:
        json.dump({"deviceGroundTruthData": [{"source-ref": image_path}]}, handle)
    return inference_root, image_path


class TestItem2Containment:
    """task 1: the fixed route contains the jsonl path and returns fixed text."""

    def test_dotdot_workflow_id_is_404_and_opens_nothing(self, tmp_path):
        # FAILS unfixed: it opens the one-level-up jsonl and serves it.
        inference_root, _image = _seed_tree(tmp_path)
        open_calls = []
        route = _lift(inference_root, open_calls)
        with pytest.raises(HTTPException) as caught:
            route("..", "escape", token=None, db=None)
        assert caught.value.status_code == 404
        assert caught.value.detail == "Server cannot find the input image path."
        assert open_calls == []

    def test_malformed_contained_jsonl_is_500_with_the_fixed_detail(self, tmp_path):
        # FAILS unfixed: the 500 detail carries the exception text and path.
        inference_root, _image = _seed_tree(tmp_path)
        with open(os.path.join(inference_root, "wf", "bad.jsonl"), "w") as handle:
            handle.write("{not json")
        open_calls = []
        route = _lift(inference_root, open_calls)
        with pytest.raises(HTTPException) as caught:
            route("wf", "bad", token=None, db=None)
        assert caught.value.status_code == 500
        assert caught.value.detail == "Server cannot find the input image path."

    @pytest.mark.skipif(sys.version_info < (3, 10),
                        reason="realpath raises on an embedded NUL only on 3.10+")
    def test_embedded_nul_capture_id_is_404_and_opens_nothing(self, tmp_path):
        # FAILS unfixed: the route opens and answers 500 with the NUL error.
        inference_root, _image = _seed_tree(tmp_path)
        open_calls = []
        route = _lift(inference_root, open_calls)
        with pytest.raises(HTTPException) as caught:
            route("wf", "a\x00b", token=None, db=None)
        assert caught.value.status_code == 404
        assert caught.value.detail == "Server cannot find the input image path."
        assert open_calls == []

    @settings(max_examples=50, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        workflow_id=st.one_of(
            st.text(alphabet=st.characters(blacklist_characters="/\x00"), max_size=16),
            st.sampled_from([".", ".."])),
        capture_id=st.one_of(
            st.text(alphabet=st.characters(blacklist_characters="/\x00"), min_size=1, max_size=16),
            st.sampled_from([".", ".."])),
        seed_mode=st.sampled_from(["none", "valid", "malformed", "missing-image"]),
    )
    @example(workflow_id="..", capture_id="cap-1", seed_mode="valid")
    def test_property_3_containment(self, tmp_path_factory, workflow_id, capture_id, seed_mode):
        # The oracle compares outcomes (status, detail, served bytes), not path
        # strings. FAILS unfixed whenever the resolved path escapes the root
        # (the unfixed route opens it) or a contained read fails (the 500 detail
        # differs).
        tmp_path = tmp_path_factory.mktemp("item2")
        inference_root = os.path.join(str(tmp_path), "inference-results")
        os.makedirs(os.path.join(inference_root, "wf"))
        image_path = os.path.join(str(tmp_path), "img.jpg")
        with open(image_path, "wb") as handle:
            handle.write(b"jpeg")
        base = os.path.realpath(inference_root)
        candidate = os.path.join(inference_root, workflow_id, capture_id + ".jsonl")
        resolved = os.path.realpath(candidate)
        contained = resolved.startswith(base + os.sep)

        def _write(target, text):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as handle:
                handle.write(text)

        valid_doc = json.dumps({"deviceGroundTruthData": [{"source-ref": image_path}]})
        missing_doc = json.dumps(
            {"deviceGroundTruthData": [{"source-ref": image_path + ".gone"}]})
        # Seed at the resolved location so the UNFIXED route (which opens it)
        # has something to serve, sharpening the contrast with the fixed 404.
        if seed_mode == "valid":
            _write(resolved, valid_doc)
        elif seed_mode == "malformed":
            _write(resolved, "{not json")
        elif seed_mode == "missing-image":
            _write(resolved, missing_doc)

        open_calls = []
        route = _lift(inference_root, open_calls)

        def _call():
            return route(workflow_id, capture_id, token=None, db=None)

        if not contained:
            with pytest.raises(HTTPException) as caught:
                _call()
            assert caught.value.status_code == 404
            assert caught.value.detail == "Server cannot find the input image path."
            assert open_calls == []
            return
        if seed_mode == "valid":
            response = _call()
            assert isinstance(response, FileResponse)
            assert response.path == image_path
        elif seed_mode == "missing-image":
            with pytest.raises(HTTPException) as caught:
                _call()
            assert caught.value.status_code == 404
        else:  # none or malformed -> read/parse fails -> 500 fixed detail
            with pytest.raises(HTTPException) as caught:
                _call()
            assert caught.value.status_code == 500
            assert caught.value.detail == "Server cannot find the input image path."


class TestItem2Preservation:
    """task 2: legitimate ids serve exactly as today. PASSES unfixed."""

    def test_legitimate_pair_serves_the_seeded_image(self, tmp_path):
        inference_root, image_path = _seed_tree(tmp_path)
        open_calls = []
        route = _lift(inference_root, open_calls)
        response = route("wf", "cap-1", token=None, db=None)
        assert isinstance(response, FileResponse)
        assert response.path == image_path
        assert open_calls  # the jsonl was read

    def test_a_database_row_serves_the_rows_image(self, tmp_path):
        inference_root, image_path = _seed_tree(tmp_path)
        open_calls = []
        route = _lift(inference_root, open_calls, row=_Row(image_path))
        response = route("wf", "cap-1", token=None, db=None)
        assert isinstance(response, FileResponse)
        assert response.path == image_path

    def test_a_contained_id_with_a_missing_image_is_404(self, tmp_path):
        inference_root, _image = _seed_tree(tmp_path)
        missing = os.path.join(str(tmp_path), "gone.jpg")
        with open(os.path.join(inference_root, "wf", "cap-2.jsonl"), "w") as handle:
            json.dump({"deviceGroundTruthData": [{"source-ref": missing}]}, handle)
        open_calls = []
        route = _lift(inference_root, open_calls)
        with pytest.raises(HTTPException) as caught:
            route("wf", "cap-2", token=None, db=None)
        assert caught.value.status_code == 404
