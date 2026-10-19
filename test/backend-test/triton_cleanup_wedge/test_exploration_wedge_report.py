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
"""Exploration (triton-model-cleanup-wedge task 4.1, Property 6): a wedged
Triton load is reported as ``stalled``.

The scenario is the 2026-10-14 MIC-730 wedge: ``model-a`` has been
``LOADING`` for more than 15 minutes, the Triton model index has not
changed since, and the model's python-backend stub uses no CPU. The test
feeds it to the ``/feature-configurations`` builder
(``feature_configs_utils.get_features_triton``) and to the
``dda-model-status`` snapshot builder, and asserts that both report the
model as stalled.

On the unfixed code it FAILS: nothing watches Triton loads, so
``loadHealth`` is absent from ``/feature-configurations`` and the shadow
says nothing about the stall.
"""
import importlib
import os

import pytest

from triton_cleanup_wedge.fakes import (
    FakeTime,
    import_with_awsiot_stubs,
    write_entry,
)

MODEL = "model-a"
STUB = "triton_python_backend_stub"


class _Index:
    """A Triton index frozen in the wedged state."""

    def __init__(self):
        self.states = {MODEL: ("LOADING", None), "base_" + MODEL: ("LOADING", None),
                       "marshal_" + MODEL: ("READY", None)}

    def index_states(self):
        return dict(self.states)

    def list_triton_models(self, quiet=False):
        return [{"model_component": name, "status": state}
                for name, (state, _reason) in self.states.items()]


class _Tracker:
    def requested_at(self, model):
        return None

    def inflight(self):
        return []

    def ensure_tracker_thread(self):
        return None


def _fake_proc(root, repo):
    """One live, idle stub of ``base_model-a`` (pid 4242, 3.0 CPU-s)."""
    pid_dir = os.path.join(root, "4242")
    os.makedirs(pid_dir)
    argv = [STUB, os.path.join(repo, "base_" + MODEL, "1", "model.py"), "shm", "1048576"]
    with open(os.path.join(pid_dir, "cmdline"), "wb") as handle:
        handle.write(b"\0".join(a.encode() for a in argv) + b"\0")
    fields = ["S"] + ["0"] * 10 + ["200", "100"] + ["0"] * 30
    with open(os.path.join(pid_dir, "stat"), "w") as handle:
        handle.write("4242 ({0}) {1}\n".format(STUB, " ".join(fields)))


def test_a_wedged_load_is_reported_as_stalled(tmp_path, monkeypatch):
    try:
        load_watchdog = importlib.import_module("dda_triton.load_watchdog")
    except ImportError as exc:
        pytest.fail("loadHealth is absent: nothing detects a wedged Triton load "
                    "(dda_triton.load_watchdog does not exist: {0})".format(exc))

    repo = str(tmp_path / "repo")
    write_entry(repo, MODEL, age_s=7200.0)
    proc = str(tmp_path / "proc")
    _fake_proc(proc, repo)
    records = str(tmp_path / "records")
    os.makedirs(records)
    # The device opted out with the kill file, so the stall is reported and
    # no restart is attempted.
    open(os.path.join(records, "self-heal.disabled"), "w").close()

    clock = FakeTime(start=5000.0)
    index = _Index()
    watchdog = load_watchdog.LoadWatchdog(
        client_provider=lambda: index, repo_has_models=lambda: True,
        clock=clock.monotonic, wall=clock.wall, sleep=clock.sleep,
        proc_root=proc, sys_root=str(tmp_path / "sys"),
        config_reader=lambda: {"StreamShadowName": "s"}, vllm_states=lambda: {},
        shadow_reader=lambda thing, name: None, records_dir=records, repo_dir=repo,
        tracker=_Tracker(), reporter=lambda snapshot: "unchanged",
        killer=lambda pid, sig: pytest.fail("no restart may be signalled"),
        exiter=lambda code: pytest.fail("no exit may happen"))
    monkeypatch.setattr(load_watchdog, "_WATCHDOG", watchdog)
    for _ in range(32):  # 16 minutes of ticks
        watchdog.tick()
        clock.sleep(30.0)

    fcu = import_with_awsiot_stubs("utils.feature_configs_utils")
    monkeypatch.setattr(fcu, "get_default_configs_lfv", lambda m: {"modelAlias": m})
    entries = fcu.get_features_triton(index)
    entry = next(e for e in entries if e.modelName == MODEL)
    health = (entry.defaultConfiguration or {}).get("loadHealth")
    assert health is not None, "loadHealth is absent from /feature-configurations"
    assert health["state"] == "stalled"
    assert health["evidence"], "a stalled model carries its evidence"

    snapshot = load_watchdog.build_model_status_snapshot({MODEL: "LOADING"})
    readiness = snapshot["modelReadiness"]
    assert [m for m in readiness["models"] if m["model"] == MODEL][0]["stalled"] is True
    not_ready = [m for m in readiness["notReady"] if m["model"] == MODEL]
    assert not_ready and not_ready[0]["stalled"] is True
    assert snapshot["selfHeal"]["enabled"] is False
    assert snapshot["selfHeal"]["disabledBy"] == "file"
