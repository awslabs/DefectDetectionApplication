# Copyright 2026 Amazon Web Services, Inc.
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
"""Exploration, Property 5 (triton-model-cleanup-wedge, task 2.2).

A model component's Startup whose model never reaches READY must leave a
load outcome record and still exit 0. On the unfixed code the Startup
ignores ``start_model``'s result: it exits 0 and records nothing, so the
deployment reports COMPLETED with nothing anywhere saying the model is not
ready (bugfix.md 1.4).

_Requirements: 1.4, 2.7_
"""
import os
import runpy
import signal
import subprocess
import sys

import dda_triton.constants as constants
import dda_triton.model_autostart_utils as autostart
import pytest
import requests

from triton_cleanup_wedge.fakes import (
    STATE_LOADING,
    FakeBackend,
    FakeTime,
    records_in,
    write_fixture_model,
)

MODEL = "model-explore-startup-jetson-xavier-jp5"


@pytest.fixture
def restore_sigterm():
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def _no_docker(*_a, **_k):
    raise FileNotFoundError("docker")


def test_a_startup_that_cannot_confirm_ready_records_it_and_exits_0(
        tmp_path, monkeypatch, restore_sigterm):
    repo = str(tmp_path / "dda_triton" / "triton_model_repo")
    records_dir = str(tmp_path / "dda_triton" / "model_status")
    deployed = write_fixture_model(str(tmp_path / "deployed" / "explore"))
    backend = FakeBackend(states={MODEL: STATE_LOADING})
    FakeTime().install(monkeypatch)
    monkeypatch.setattr(constants, "TRITON_MODEL_DIR", repo)
    monkeypatch.setattr(autostart, "wait_for_server", lambda *a, **k: True)
    monkeypatch.setattr(requests, "get", backend.get)
    monkeypatch.setattr(subprocess, "run", _no_docker)
    monkeypatch.setattr(subprocess, "Popen", _no_docker)
    try:
        import dda_triton.model_status_records as records
        monkeypatch.setattr(records, "MODEL_STATUS_DIR", records_dir)
    except ImportError:
        pass  # the unfixed tree has no record module, so nothing can be recorded
    monkeypatch.setattr(sys, "argv", [
        "model_convertor.py", "--unarchived_model_path", deployed,
        "--model_version", "8.0.0", "--model_name", MODEL])
    monkeypatch.delitem(sys.modules, "dda_triton.model_convertor", raising=False)

    exit_code = 0
    try:
        runpy.run_module("dda_triton.model_convertor", run_name="__main__")
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)

    assert os.path.isdir(os.path.join(repo, MODEL)), "the conversion itself must succeed"
    assert exit_code == 0
    recorded = records_in(records_dir)
    assert MODEL in recorded, (
        "the Startup ended with the model not READY but recorded no outcome "
        "(records: {0})".format(sorted(recorded)))
    assert recorded[MODEL]["schema"] == 1
    assert recorded[MODEL]["outcome"] == "not_ready"
    assert recorded[MODEL]["state"] == STATE_LOADING
