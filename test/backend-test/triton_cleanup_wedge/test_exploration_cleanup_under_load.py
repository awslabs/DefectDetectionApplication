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
"""Exploration, Property 1 (triton-model-cleanup-wedge, task 2.2).

The model component's Shutdown (``convert_model_cleanup.py``) runs while
the backend reports the model ``LOADING``. The fixed cleanup must leave the
three repository directories in place (it waits, then skips with a
WARNING). On the unfixed code the Shutdown logs the refused ``/stop`` and
``rmtree``s the directories under the in-flight load, which is the wedge's
first link (bugfix.md 1.1).

_Requirements: 1.1, 2.1, 2.2_
"""
import os
import runpy
import sys

import dda_triton.constants as constants
import dda_triton.model_autostart_utils as autostart
import requests

from triton_cleanup_wedge.fakes import (
    STATE_LOADING,
    FakeBackend,
    FakeTime,
    published_dirs,
    write_entry,
)

MODEL = "model-explore-cleanup-jetson-xavier-jp5"


def test_cleanup_leaves_the_entries_of_a_loading_model_in_place(tmp_path, monkeypatch):
    repo = str(tmp_path / "dda_triton" / "triton_model_repo")
    write_entry(repo, MODEL)
    backend = FakeBackend(states={MODEL: STATE_LOADING}, stop_status=403)
    FakeTime().install(monkeypatch)
    monkeypatch.setattr(constants, "TRITON_MODEL_DIR", repo)
    monkeypatch.setattr(autostart, "wait_for_server", lambda *a, **k: True)
    monkeypatch.setattr(requests, "get", backend.get)
    monkeypatch.setattr(sys, "argv", ["convert_model_cleanup.py", "--model_name", MODEL])
    monkeypatch.delitem(sys.modules, "dda_triton.convert_model_cleanup", raising=False)

    exit_code = 0
    try:
        runpy.run_module("dda_triton.convert_model_cleanup", run_name="__main__")
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)

    surviving = [path for path in published_dirs(repo, MODEL) if os.path.isdir(path)]
    assert surviving == published_dirs(repo, MODEL), (
        "the Shutdown removed repository entries of a model that was LOADING: "
        "surviving {0}".format(surviving))
    assert exit_code == 0
