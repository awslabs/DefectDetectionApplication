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
"""Bug-condition exploration test (Task 1, case 4) for edge-deploy-reliability.

Property 4: Bug Condition — Never-reachable runtime failures are actionable
(Defect D, `isBugCondition_D`).

**These tests assert the FIXED (post-fix) diagnostics, so they are EXPECTED
TO FAIL on the UNFIXED tree.** The failure is the counterexample confirming
the defect: when every load attempt dies at the connection level (the vLLM
runtime on 127.0.0.1:8901 never reachable — the incident's ~70s of connection
refused against the SIGKILLed backend), the unfixed `prepare` ends with the
literal generic "staged but the load request did not succeed; exiting
non-zero so the component retries" message that names no cause.

The SAME tests are re-run in task 3.5 against the fixed script, where they
must PASS: exit stays non-zero, and the terminal output names the LocalServer
backend container (image 'flask-app') as the likely cause with concrete
verification steps (`docker ps` for the Exited container, `docker logs`).

Both never-reachable shapes of `isBugCondition_D` are covered:
  * `wait_for_server` succeeds but every POST raises ConnectionError, and
  * `wait_for_server` never succeeds at all.
An authoritative HTTP error response (e.g. 409) is NOT the bug condition and
is deliberately not exercised here (preservation, task 2 territory).

Runs on the host: `requests` and `time.sleep` are stubbed so the 3/6/12/24/48s
backoff never really elapses; staging is stubbed to keep the test off
/aws_dda (the diagnostics seam under test is `prepare`'s terminal message,
which the fix changes; validation and staging are untouched by it).

Requires PYTHONPATH=src/backend (repo convention for host-runnable
backend tests).

Validates: Requirements 1.9
"""
import json
import logging
import os
from argparse import Namespace

import pytest
import requests as real_requests

import dda_triton.vllm_model_prep as prep

MODEL_NAME = "opt125m-smoke"
COMPONENT_NAME = "model-vllm-opt125m-smoke"


class _RecordingHandler(logging.Handler):
    """Root-logger capture (the parent conftest repurposes `caplog` for
    class-based suites, so this suite records the prep script's terminal
    output with its own handler)."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    @property
    def text(self):
        return "\n".join(r.getMessage() for r in self.records)


@pytest.fixture
def log_capture():
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


@pytest.fixture
def valid_repo(tmp_path):
    """A minimal valid Triton_vLLM_Repository (HF-sourced: no weights_path),
    so `prepare` gets past validation to the load request."""
    model_dir = tmp_path / MODEL_NAME
    version_dir = model_dir / prep.VERSION_DIR_NAME
    version_dir.mkdir(parents=True)
    (model_dir / prep.CONFIG_PBTXT_NAME).write_text(
        'backend: "vllm"\n', encoding="utf-8")
    (version_dir / prep.MODEL_JSON_NAME).write_text(
        json.dumps({"model": "facebook/opt-125m"}), encoding="utf-8")
    return str(tmp_path)


@pytest.fixture
def prep_args(valid_repo):
    return Namespace(
        model_name=MODEL_NAME,
        component_name=COMPONENT_NAME,
        unarchived_repo_path=valid_repo,
        weights_path=None,
        cleanup=False,
    )


@pytest.fixture
def never_reachable_env(monkeypatch, tmp_path):
    """Stub the environment so every attempt is connection-level dead:
    backoff sleeps are recorded (never elapse), staging never touches
    /aws_dda."""
    sleeps = []
    monkeypatch.setattr(prep.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        prep, "stage_repository",
        lambda *a, **k: str(tmp_path / "staged" / MODEL_NAME))
    return sleeps


def _assert_actionable_terminal_output(log_capture, exit_code):
    text = log_capture.text
    assert exit_code == 1, (
        "prepare must still exit non-zero on a never-reachable runtime "
        "(got {})".format(exit_code))
    assert "flask-app" in text, (
        "COUNTEREXAMPLE (Defect D): the terminal output never names the "
        "LocalServer backend container (image 'flask-app') as the likely "
        "cause of a never-reachable vLLM runtime. Terminal log:\n{}".format(
            "\n".join(r.getMessage() for r in log_capture.records[-5:])))
    assert "docker ps" in text, (
        "COUNTEREXAMPLE (Defect D): the terminal output includes no "
        "`docker ps` verification step for the dead backend container")
    assert "docker logs" in text, (
        "COUNTEREXAMPLE (Defect D): the terminal output includes no "
        "`docker logs` verification step for the dead backend container")


def test_all_connection_errors_yield_actionable_cause(
        monkeypatch, log_capture, prep_args, never_reachable_env):
    """isBugCondition_D shape 1: the port accepts a probe but every POST dies
    in `requests.ConnectionError` (connection refused) — no HTTP response is
    ever received across the full retry window.

    Validates: Requirements 1.9 (expected behavior 2.10)
    """
    monkeypatch.setattr(prep, "wait_for_server", lambda *a, **k: True)

    post_attempts = []

    def refused_post(url, timeout=None):
        post_attempts.append(url)
        raise real_requests.ConnectionError(
            "HTTPConnectionPool(host='127.0.0.1', port=8901): "
            "connection refused")

    monkeypatch.setattr(prep.requests, "post", refused_post)

    exit_code = prep.prepare(prep_args)

    # All retry attempts really were exhausted at the connection level.
    assert len(post_attempts) == len(prep.LOAD_RETRY_BACKOFF_SECONDS) + 1
    _assert_actionable_terminal_output(log_capture, exit_code)


def test_server_never_reachable_yields_actionable_cause(
        monkeypatch, log_capture, prep_args, never_reachable_env):
    """isBugCondition_D shape 2: `wait_for_server` never succeeds — the
    runtime port never even accepts a TCP connection (the incident shape:
    backend container Exited(137), nothing listening on 8901).

    Validates: Requirements 1.9 (expected behavior 2.10)
    """
    monkeypatch.setattr(prep, "wait_for_server", lambda *a, **k: False)
    monkeypatch.setattr(
        prep.requests, "post",
        lambda *a, **k: pytest.fail("no POST must be issued when the server "
                                    "is never reachable"))

    exit_code = prep.prepare(prep_args)

    _assert_actionable_terminal_output(log_capture, exit_code)
