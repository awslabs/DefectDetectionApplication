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
"""Preservation property tests (Task 2) for edge-deploy-reliability.

Property 8: Preservation — Prep script's specific error paths unchanged.

**Validates: Requirements 3.2, 3.9**

Observation-first (observed on UNFIXED src/backend/dda_triton/
vllm_model_prep.py and asserted here as the golden behavior):

  * repository validation defects -> exit 1 with the exact
    "vLLM repository validation defect for model '{m}': {defect}" messages;
    nothing staged, no load request ever issued;
  * unresolvable --weights_path -> exit 1 with the exact
    "Model '{m}' FAILED: weights path does not exist or is not readable:
    {abspath}" message; nothing staged, no load request;
  * an authoritative HTTP error response from the runtime (even after
    leading connection refusals — refused-then-409 is NOT the bug
    condition) -> single-attempt semantics on the HTTP response (no retry
    after it), the exact "VllmLoadModel: Request failed with status code:
    {code}" + response-body messages, the exact generic terminal
    "staged but the load request did not succeed" message, exit 1 — and
    NEVER the never-reachable diagnostic (naming flask-app), which the
    Defect D fix reserves for isBugCondition_D;
  * HTTP 200 -> exit 0 with the exact "Model '{m}' loaded successfully!"
    message.

These tests PASS on the unfixed tree and must still PASS after the Defect D
fix (task 3.4), which changes ONLY the terminal message of the
never-reachable classification (LOAD_UNREACHABLE).

PROPERTY-BASED (Hypothesis): the classification boundary is universal —
"for any attempt sequence of k connection refusals followed by an HTTP
response, the HTTP path (not the unreachable path) governs" — so Hypothesis
generates the refusal count and the terminal HTTP status.

Runs on the host with `requests`, `time.sleep`, `wait_for_server`, and
staging stubbed (the seam under test is `prepare`'s messages and exit
codes). Requires PYTHONPATH=src/backend (repo convention).
"""
import json
import logging
import os
import shutil
import tempfile
from argparse import Namespace
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import pytest
import requests as real_requests
from hypothesis import given, settings
from hypothesis import strategies as st

import dda_triton.vllm_model_prep as prep

MODEL_NAME = "opt125m-smoke"
COMPONENT_NAME = "model-vllm-opt125m-smoke"

# ---------------------------------------------------------------------------
# Golden messages recorded from the UNFIXED script (exact strings).
# ---------------------------------------------------------------------------

VALIDATION_DEFECT_TEMPLATE = (
    "vLLM repository validation defect for model '{model}': {defect}")
MISSING_REPO_DEFECT = (
    "unarchived repository path does not exist or is not a directory: {path}")
WRONG_BACKEND_DEFECT = (
    'config.pbtxt must declare backend: "vllm" (found {found!r}): {path}')
UNEXPECTED_ENTRIES_DEFECT = (
    "unexpected entries {entries} in model repository (expected exactly "
    "'config.pbtxt' and '1/'): {path}")
WEIGHTS_FAILED_MESSAGE = (
    "Model '{model}' FAILED: weights path does not exist or is not "
    "readable: {path}")
HTTP_ERROR_MESSAGE = "VllmLoadModel: Request failed with status code: {code}"
GENERIC_TERMINAL_MESSAGE = (
    "Model '{model}' staged but the load request did not succeed; "
    "exiting non-zero so the component retries")
SUCCESS_MESSAGE = "Model '{model}' loaded successfully!"

#: The Defect D fix introduces a never-reachable diagnostic naming the
#: LocalServer backend container image. It must NEVER appear on these
#: non-bug-condition paths (Requirement 3.9 / design Property 8).
UNREACHABLE_DIAGNOSTIC_MARKER = "flask-app"


class _RecordingHandler(logging.Handler):
    """Root-logger capture (the parent conftest repurposes `caplog` for
    class-based suites; this suite records terminal output itself)."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    @property
    def messages(self):
        return [r.getMessage() for r in self.records]

    @property
    def text(self):
        return "\n".join(self.messages)


@contextmanager
def capture_logs():
    handler = _RecordingHandler()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def make_valid_repo(root):
    """A minimal valid Triton_vLLM_Repository (HF-sourced) under `root`."""
    model_dir = os.path.join(root, MODEL_NAME)
    version_dir = os.path.join(model_dir, prep.VERSION_DIR_NAME)
    os.makedirs(version_dir)
    with open(os.path.join(model_dir, prep.CONFIG_PBTXT_NAME), "w",
              encoding="utf-8") as f:
        f.write('backend: "vllm"\n')
    with open(os.path.join(version_dir, prep.MODEL_JSON_NAME), "w",
              encoding="utf-8") as f:
        json.dump({"model": "facebook/opt-125m"}, f)
    return root


def make_args(repo_path, weights_path=None):
    return Namespace(
        model_name=MODEL_NAME,
        component_name=COMPONENT_NAME,
        unarchived_repo_path=repo_path,
        weights_path=weights_path,
        cleanup=False,
    )


def _fail_if_called(label):
    def _fail(*args, **kwargs):
        pytest.fail("{} must not be reached on this error path".format(label))
    return _fail


@contextmanager
def stubbed_environment(post, wait_for_server=True, allow_staging=True):
    """requests.post / wait_for_server / time.sleep / staging stubs; yields
    the recorded backoff sleeps."""
    sleeps = []
    staging_dir = tempfile.mkdtemp(prefix="prep-preservation-staged-")
    stage = (lambda *a, **k: os.path.join(staging_dir, MODEL_NAME)) \
        if allow_staging else _fail_if_called("stage_repository")
    try:
        with mock.patch.object(prep.time, "sleep", sleeps.append), \
                mock.patch.object(prep, "stage_repository", stage), \
                mock.patch.object(prep, "wait_for_server",
                                  lambda *a, **k: wait_for_server), \
                mock.patch.object(prep.requests, "post", post):
            yield sleeps
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Repository validation defects (Requirement 3.9)
# ---------------------------------------------------------------------------

class TestValidationDefectPathsUnchanged:

    def test_missing_repository_directory_exact_message_and_exit_code(
            self, tmp_path):
        """Golden: a nonexistent repo path exits 1 with the exact defect
        message; nothing staged, no load request issued.

        Validates: Requirements 3.9
        """
        missing = str(tmp_path / "does-not-exist")
        with capture_logs() as logs, stubbed_environment(
                post=_fail_if_called("requests.post"),
                allow_staging=False):
            exit_code = prep.prepare(make_args(missing))

        assert exit_code == 1
        assert VALIDATION_DEFECT_TEMPLATE.format(
            model=MODEL_NAME,
            defect=MISSING_REPO_DEFECT.format(path=missing),
        ) in logs.messages

    def test_wrong_backend_exact_message_and_exit_code(self, tmp_path):
        """Golden: a config.pbtxt declaring a non-vllm backend exits 1 with
        the exact defect message; nothing staged, no load request.

        Validates: Requirements 3.9
        """
        repo = make_valid_repo(str(tmp_path))
        config_path = os.path.join(repo, MODEL_NAME, prep.CONFIG_PBTXT_NAME)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write('backend: "onnx"\n')

        with capture_logs() as logs, stubbed_environment(
                post=_fail_if_called("requests.post"),
                allow_staging=False):
            exit_code = prep.prepare(make_args(repo))

        assert exit_code == 1
        assert VALIDATION_DEFECT_TEMPLATE.format(
            model=MODEL_NAME,
            defect=WRONG_BACKEND_DEFECT.format(found="onnx",
                                               path=config_path),
        ) in logs.messages

    def test_unexpected_entries_exact_message_and_exit_code(self, tmp_path):
        """Golden: a rogue file in the model directory exits 1 with the
        exact defect message; nothing staged, no load request.

        Validates: Requirements 3.9
        """
        repo = make_valid_repo(str(tmp_path))
        model_dir = os.path.join(repo, MODEL_NAME)
        with open(os.path.join(model_dir, "rogue.txt"), "w",
                  encoding="utf-8") as f:
            f.write("unexpected")

        with capture_logs() as logs, stubbed_environment(
                post=_fail_if_called("requests.post"),
                allow_staging=False):
            exit_code = prep.prepare(make_args(repo))

        assert exit_code == 1
        assert VALIDATION_DEFECT_TEMPLATE.format(
            model=MODEL_NAME,
            defect=UNEXPECTED_ENTRIES_DEFECT.format(entries=["rogue.txt"],
                                                    path=model_dir),
        ) in logs.messages


# ---------------------------------------------------------------------------
# Unresolvable weights path (Requirement 3.9)
# ---------------------------------------------------------------------------

class TestWeightsPathFailurePathUnchanged:

    def test_unresolvable_weights_path_exact_message_and_exit_code(
            self, tmp_path):
        """Golden: an S3-sourced record whose weights path does not resolve
        exits 1 with the exact FAILED message naming the model and the
        absolute unresolved path; nothing staged, no load request.

        Validates: Requirements 3.9
        """
        repo = make_valid_repo(str(tmp_path / "repo"))
        missing_weights = str(tmp_path / "weights" / "gone")

        with capture_logs() as logs, stubbed_environment(
                post=_fail_if_called("requests.post"),
                allow_staging=False):
            exit_code = prep.prepare(
                make_args(repo, weights_path=missing_weights))

        assert exit_code == 1
        assert WEIGHTS_FAILED_MESSAGE.format(
            model=MODEL_NAME,
            path=os.path.abspath(missing_weights),
        ) in logs.messages


# ---------------------------------------------------------------------------
# Authoritative HTTP error + success paths (Requirements 3.2, 3.9)
# ---------------------------------------------------------------------------

def run_prepare_with_outcomes(tmp_path_str, refusals, terminal_status,
                              response_text="simulated runtime response"):
    """Run `prepare` against a valid repo where the first `refusals` POSTs
    die in ConnectionError and the next POST returns `terminal_status`.
    Returns (exit_code, logs, post_count, sleeps)."""
    repo = make_valid_repo(tmp_path_str)
    attempts = []

    def scripted_post(url, timeout=None):
        attempts.append(url)
        if len(attempts) <= refusals:
            raise real_requests.ConnectionError(
                "HTTPConnectionPool(host='127.0.0.1', port=8901): "
                "connection refused")
        return SimpleNamespace(status_code=terminal_status,
                               text=response_text)

    with capture_logs() as logs, \
            stubbed_environment(post=scripted_post) as sleeps:
        exit_code = prep.prepare(make_args(repo))
    return exit_code, logs, len(attempts), sleeps


class TestHttpAndSuccessPathsUnchanged:

    def test_refused_then_409_keeps_authoritative_http_error_path(
            self, tmp_path):
        """Golden (design edge case, NOT isBugCondition_D): attempt 1 is
        connection-refused but attempt 2 receives an authoritative HTTP 409.
        The HTTP response is terminal (single-attempt semantics — no retry
        after it), the exact status-code + body + generic terminal messages
        are emitted, exit code is 1, and the never-reachable diagnostic
        never appears.

        Validates: Requirements 3.9
        """
        exit_code, logs, posts, sleeps = run_prepare_with_outcomes(
            str(tmp_path), refusals=1, terminal_status=409,
            response_text="model load rejected: FAILED")

        assert exit_code == 1
        assert posts == 2, "the HTTP response must be authoritative (no " \
                           "further attempts after it)"
        assert sleeps == [prep.LOAD_RETRY_BACKOFF_SECONDS[0]], \
            "exactly the first backoff sleep precedes the second attempt"
        assert HTTP_ERROR_MESSAGE.format(code=409) in logs.messages
        assert "model load rejected: FAILED" in logs.messages
        assert GENERIC_TERMINAL_MESSAGE.format(model=MODEL_NAME) \
            in logs.messages
        assert UNREACHABLE_DIAGNOSTIC_MARKER not in logs.text, (
            "PRESERVATION REGRESSION (Property 8): the never-reachable "
            "diagnostic leaked into the authoritative-HTTP-error path")

    def test_http_200_success_path_unchanged(self, tmp_path):
        """Golden: a first-attempt HTTP 200 exits 0 with the exact success
        message and no error/terminal messages.

        Validates: Requirements 3.2, 3.9
        """
        exit_code, logs, posts, sleeps = run_prepare_with_outcomes(
            str(tmp_path), refusals=0, terminal_status=200)

        assert exit_code == 0
        assert posts == 1
        assert sleeps == []
        assert SUCCESS_MESSAGE.format(model=MODEL_NAME) in logs.messages
        assert GENERIC_TERMINAL_MESSAGE.format(model=MODEL_NAME) \
            not in logs.messages
        assert UNREACHABLE_DIAGNOSTIC_MARKER not in logs.text

    @settings(max_examples=25, deadline=None)
    @given(
        refusals=st.integers(min_value=0,
                             max_value=len(prep.LOAD_RETRY_BACKOFF_SECONDS)),
        terminal_status=st.sampled_from([200, 400, 409, 422, 500, 503]),
    )
    def test_refused_then_http_always_takes_the_http_path(
            self, refusals, terminal_status):
        """Property 8 (classification boundary): for ANY attempt sequence of
        k connection refusals followed by an HTTP response, the HTTP path
        governs — the response is terminal after exactly k+1 attempts and k
        backoff sleeps; HTTP 200 exits 0 with the success message, a non-200
        exits 1 with the exact status-code and generic terminal messages —
        and the never-reachable diagnostic NEVER appears (it is reserved for
        isBugCondition_D, where no HTTP response is ever received).

        Validates: Requirements 3.2, 3.9
        """
        tmp_root = tempfile.mkdtemp(prefix="prep-preservation-repo-")
        try:
            exit_code, logs, posts, sleeps = run_prepare_with_outcomes(
                tmp_root, refusals=refusals, terminal_status=terminal_status)
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

        assert posts == refusals + 1, (
            "the first HTTP response must be terminal: expected {} attempts "
            "({} refusals + 1), got {}".format(refusals + 1, refusals, posts))
        assert sleeps == list(
            prep.LOAD_RETRY_BACKOFF_SECONDS[:refusals]), (
            "backoff sleeps must precede exactly the refused attempts")

        if terminal_status == 200:
            assert exit_code == 0
            assert SUCCESS_MESSAGE.format(model=MODEL_NAME) in logs.messages
            assert GENERIC_TERMINAL_MESSAGE.format(model=MODEL_NAME) \
                not in logs.messages
        else:
            assert exit_code == 1
            assert HTTP_ERROR_MESSAGE.format(code=terminal_status) \
                in logs.messages
            assert GENERIC_TERMINAL_MESSAGE.format(model=MODEL_NAME) \
                in logs.messages

        assert UNREACHABLE_DIAGNOSTIC_MARKER not in logs.text, (
            "PRESERVATION REGRESSION (Property 8): the never-reachable "
            "diagnostic appeared although an HTTP response was received "
            "(NOT isBugCondition_D)")
