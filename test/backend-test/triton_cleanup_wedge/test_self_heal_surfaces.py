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
"""The surfaces (triton-model-cleanup-wedge task 4.6, design D8,
Properties 12 and 16): ``/feature-configurations``' additive fields, the
``dda-model-status`` shadow keys, their size budget, the report cadence and
the size-rejection retirement.

Honesty guard: the shadow is a fake accessor that applies AWS IoT shadow
update semantics (objects merge key by key, arrays are replaced, ``null``
deletes a key) and, where a test says so, ShadowManager's size rejection.
Nothing talks to Greengrass."""
import copy
import json
import logging
import os
import sys
import tempfile
import types
from unittest.mock import MagicMock

import pytest

from dda_triton import load_watchdog as lw
from dda_triton import model_status_records as msr
from utils import model_status_shadow as mss
from utils import run_activity as ra
from triton_cleanup_wedge.fakes import FakeTime, import_with_awsiot_stubs, write_entry
from triton_cleanup_wedge.watchdog_fakes import MODEL, FakeIndex, Scenario, write_gpu_node

os.environ.setdefault("COMPONENT_WORK_PATH", tempfile.mkdtemp(prefix="dda-wedge-surfaces-"))

READINESS_KEYS = {"updatedAt", "readyModels", "coldModels", "models", "notReady", "truncated"}
SELF_HEAL_KEYS = {"enabled", "disabledBy", "state", "deferredBy", "attemptsIn24h", "maxPer24h",
                  "minIntervalSeconds", "rearmsAt", "last"}
LAST_KEYS = {"attempt", "requestedAt", "restartedAt", "action", "models", "evidence",
             "vllmModelsDropped"}


class SizeRejected(Exception):
    """ShadowManager's InvalidArgumentsError for an oversized document."""

    def __init__(self):
        super().__init__("")
        self.message = "The payload exceeds the maximum size allowed (413)"


def merge(base, patch):
    """AWS IoT shadow update semantics."""
    for key, value in patch.items():
        if value is None:
            base.pop(key, None)
        elif isinstance(value, dict):
            base[key] = merge(base.get(key) if isinstance(base.get(key), dict) else {}, value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def strip_nulls(value):
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    return value


class ShadowAccessor:
    def __init__(self, document=None, limit=None):
        self.document = copy.deepcopy(document or {})
        self.limit = limit
        self.writes = []
        self.rejections = 0
        self.read_raises = None

    def update_thing_shadow_state_request(self, thing, name, payload):
        assert name == "dda-model-status"
        merged = merge(copy.deepcopy(self.document), payload["reported"])
        if self.limit is not None and len(json.dumps(merged)) > self.limit:
            self.rejections += 1
            raise SizeRejected()
        self.document = merged
        self.writes.append(payload["reported"])

    def get_thing_shadow_state_request(self, thing, name):
        if self.read_raises is not None:
            raise self.read_raises
        return {"reported": copy.deepcopy(self.document)}


@pytest.fixture
def shadow(monkeypatch):
    accessor = ShadowAccessor()
    monkeypatch.setenv("AWS_IOT_THING_NAME", "thing-under-test")
    monkeypatch.setattr(mss, "_accessor_override", accessor)
    mss._reset_state()
    yield accessor
    mss._reset_state()


def join_write():
    thread = mss._write_thread
    if thread is not None:
        thread.join(10)


@pytest.fixture
def make(tmp_path, monkeypatch):
    def _make(**kwargs):
        clock = kwargs.pop("clock", None) or FakeTime(start=5000.0)
        s = Scenario(tmp_path, lw, ra, clock=clock, kill_file=kwargs.pop("kill_file", True), **kwargs)
        monkeypatch.setattr(lw, "_WATCHDOG", s.watchdog)
        monkeypatch.setattr(mss, "_clock", clock.monotonic)
        return s
    return _make


def fcu_module(monkeypatch):
    fcu = import_with_awsiot_stubs("utils.feature_configs_utils")
    monkeypatch.setattr(fcu, "get_default_configs_lfv", lambda m: {"modelAlias": m})
    return fcu


def entry_for(entries, name):
    return next(e for e in entries if e.modelName == name)


# ------------------------------------------- /feature-configurations fields

def test_no_fields_without_a_reason(make, monkeypatch):
    s = make()
    s.index.set(MODEL, "READY")
    s.ticks(1)
    fcu = fcu_module(monkeypatch)
    entry = entry_for(fcu.get_features_triton(s.index), MODEL)
    assert entry.defaultConfiguration == {"modelAlias": MODEL}
    assert entry.status == "READY"


def test_load_health_appears_after_120s_and_details_once_stalled(make, monkeypatch):
    s = make()
    fcu = fcu_module(monkeypatch)
    s.ticks(3)  # 90 s of LOADING
    assert "loadHealth" not in entry_for(fcu.get_features_triton(s.index), MODEL).defaultConfiguration
    s.ticks(1)
    health = entry_for(fcu.get_features_triton(s.index), MODEL).defaultConfiguration["loadHealth"]
    assert health["state"] == "loading"
    assert set(health) == {"state", "loadingSince", "loadingForSeconds", "noLoadProgressForSeconds",
                           "activity"}
    assert health["activity"]["windowSeconds"] == 300
    s.ticks(27)
    health = entry_for(fcu.get_features_triton(s.index), MODEL).defaultConfiguration["loadHealth"]
    assert health["state"] == "stalled"
    assert set(health) == {"state", "loadingSince", "loadingForSeconds", "noLoadProgressForSeconds",
                           "activity", "evidence", "recoveryAction", "selfHeal"}
    assert health["selfHeal"] == {"state": "disabled", "until": None}
    assert health["activity"]["stubProcesses"] == 1
    assert health["activity"]["gpuMaxPercentLastWindow"] is None


def test_gpu_max_percent_is_the_permille_maximum_divided_by_10(make, monkeypatch, tmp_path):
    s = make()
    write_gpu_node(s.sys, value="137")
    s.watchdog = lw.LoadWatchdog(**s.kwargs)
    monkeypatch.setattr(lw, "_WATCHDOG", s.watchdog)
    s.ticks(6)
    fcu = fcu_module(monkeypatch)
    health = entry_for(fcu.get_features_triton(s.index), MODEL).defaultConfiguration["loadHealth"]
    assert health["activity"]["gpuMaxPercentLastWindow"] == 13.7


def test_load_outcome_presence_and_a_ready_model_with_a_stale_record(make, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    s = make()
    assert msr.write_load_outcome(MODEL, state="LOADING", reason="Model did not reach READY",
                                  records_dir=s.records)
    s.index.set(MODEL, "UNAVAILABLE", "load failed")
    s.ticks(1)
    fcu = fcu_module(monkeypatch)
    config = entry_for(fcu.get_features_triton(s.index), MODEL).defaultConfiguration
    assert config["loadOutcome"]["outcome"] == "not_ready"
    assert config["loadOutcome"]["reason"] == "Model did not reach READY"
    assert config["failureReason"] == "load failed"
    s.index.set(MODEL, "READY")
    assert "loadOutcome" not in entry_for(fcu.get_features_triton(s.index), MODEL).defaultConfiguration
    s.ticks(1)
    assert msr.read_load_outcomes(s.records) == {}
    assert ("Load outcome for model-a resolved: the model is READY; removed {0}".format(
        os.path.join(s.records, MODEL + ".json"))) in caplog.text


def _seed_attempt(records, model, age_s, now_wall, **extra):
    attempt = {"attempt": 1, "requestedAt": lw.iso(now_wall - age_s), "restartedAt": None,
               "models": [{"name": model, "loadingForSeconds": 4032, "suspectedCulprit": True}],
               "evidence": ["[stall] e1", "[repository] e2"], "action": "backend_restart",
               "vllmModelsDropped": [{"name": "llm", "state": "READY"}]}
    attempt.update(extra)
    document, _ = lw.read_self_heal_record(records)
    document = document or {"schema": 1, "attempts": []}
    document["attempts"].append(attempt)
    assert lw.write_record(records, document) is None
    return attempt


def test_last_self_heal_is_visible_for_24_hours(make, monkeypatch):
    s = make()
    s.index.set(MODEL, "READY")
    s.ticks(1)
    fcu = fcu_module(monkeypatch)
    _seed_attempt(s.records, MODEL, 23 * 3600, s.clock.wall())
    config = entry_for(fcu.get_features_triton(s.index), MODEL).defaultConfiguration
    assert config["lastSelfHeal"] == {"attempt": 1, "requestedAt": lw.iso(s.clock.wall() - 23 * 3600),
                                      "restartedAt": None, "loadingForSeconds": 4032,
                                      "evidence": ["[stall] e1", "[repository] e2"],
                                      "action": "backend_restart"}
    os.remove(os.path.join(s.records, "self_heal.json"))
    _seed_attempt(s.records, MODEL, 25 * 3600, s.clock.wall())
    assert "lastSelfHeal" not in entry_for(fcu.get_features_triton(s.index), MODEL).defaultConfiguration


def test_a_merge_failure_means_no_field_never_an_error(make, monkeypatch, caplog):
    s = make()
    fcu = fcu_module(monkeypatch)
    monkeypatch.setattr(lw, "model_annotations", MagicMock(side_effect=RuntimeError("bug")))
    entries = fcu.get_features_triton(s.index)
    assert entry_for(entries, MODEL).defaultConfiguration == {"modelAlias": MODEL}
    assert "Load status merge skipped for model-a: bug" in caplog.text


def test_property_16_record_backed_entries(make, monkeypatch):
    s = make()
    fcu = fcu_module(monkeypatch)
    write_entry(s.repo, "model-x", age_s=600)
    msr.write_load_outcome("model-x", state=None, reason="the load request was refused",
                           backend_reachable=True, records_dir=s.records)
    msr.write_load_outcome("model-gone", state=None, reason="r", records_dir=s.records)
    vllm = types.SimpleNamespace(list_models=lambda: {"llm": "READY"})
    fcu.set_vllm_manager(vllm)
    try:
        entries = fcu.get_features_triton(s.index)
    finally:
        fcu.set_vllm_manager(None)
    names = [e.modelName for e in entries]
    assert names == [MODEL, "model-x", "llm"], "after the listed models, before the vLLM entries"
    x = entry_for(entries, "model-x")
    assert x.type == "TritonModel" and x.status == "UNKNOWN"
    assert x.defaultConfiguration["modelAlias"] == "model-x"
    assert x.defaultConfiguration["loadOutcome"]["reason"] == "the load request was refused"
    snapshot = lw.build_model_status_snapshot({MODEL: "LOADING"})
    readiness = snapshot["modelReadiness"]
    assert {"model": "model-x", "state": "UNKNOWN", "stalled": False} in readiness["models"]
    assert [m["model"] for m in readiness["notReady"]] == [MODEL, "model-x"]
    assert "model-gone" not in json.dumps(snapshot), "a record whose directory is gone adds nothing"


# ------------------------------------------------------------ the shadow keys

def test_the_fixed_key_sets_and_the_default_switch(make):
    s = make(kill_file=False)
    s.index.set(MODEL, "READY")
    s.ticks(1)
    snapshot = lw.build_model_status_snapshot({MODEL: "READY"})
    assert {"gpuDegraded", "gpuChainModels", "gpuActiveModels", "models", "updatedAt"} <= set(snapshot)
    assert set(snapshot["modelReadiness"]) == READINESS_KEYS
    assert set(snapshot["selfHeal"]) == SELF_HEAL_KEYS
    assert snapshot["selfHeal"]["enabled"] is True and snapshot["selfHeal"]["disabledBy"] is None
    assert snapshot["selfHeal"]["state"] == "idle"
    assert snapshot["modelReadiness"]["models"] == [{"model": MODEL, "state": "READY", "stalled": False}]
    assert snapshot["modelReadiness"]["notReady"] == []
    assert snapshot["modelReadiness"]["readyModels"] == 1


def test_without_a_watchdog(tmp_path, monkeypatch):
    monkeypatch.setattr(lw, "_WATCHDOG", None)
    snapshot = lw.build_model_status_snapshot({"model-a": "UNKNOWN", "base_model-a": "UNKNOWN"},
                                              records_dir=str(tmp_path))
    assert snapshot["selfHeal"]["enabled"] is False
    assert snapshot["selfHeal"]["disabledBy"] == "watchdog_not_running"
    assert snapshot["modelReadiness"]["coldModels"] == 1
    assert snapshot["modelReadiness"]["notReady"] == [], "an UNKNOWN model without a record is not listed"


def test_the_last_attempt_has_a_fixed_key_set(make):
    s = make()
    s.ticks(1)
    _seed_attempt(s.records, MODEL, 600, s.clock.wall(), vllmModelsDropped=None)
    last = lw.build_model_status_snapshot({MODEL: "LOADING"})["selfHeal"]["last"]
    assert set(last) == LAST_KEYS and last["vllmModelsDropped"] is None


def test_the_size_ladder(make):
    s = make()
    s.ticks(1)
    for i in range(30):
        write_entry(s.repo, "model-{0:02d}-{1}".format(i, "x" * 60), age_s=600)
    statuses = {}
    for i in range(30):
        name = "model-{0:02d}-{1}".format(i, "x" * 60)
        statuses[name] = "UNAVAILABLE" if i % 2 else "READY"
        if i % 2:
            msr.write_load_outcome(name, state="UNAVAILABLE", reason="r" * 1500, records_dir=s.records)
    _seed_attempt(s.records, MODEL, 600, s.clock.wall(),
                  evidence=["[stall] " + "e" * 290 for _ in range(5)],
                  models=[{"name": "model-{0}-{1}".format(i, "y" * 250), "loadingForSeconds": 1}
                          for i in range(10)])
    snapshot = lw.build_model_status_snapshot(statuses)
    parts = {"modelReadiness": snapshot["modelReadiness"], "selfHeal": snapshot["selfHeal"]}
    assert len(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()) <= 4096
    readiness = snapshot["modelReadiness"]
    assert readiness["truncated"] is True
    assert readiness["readyModels"] == 15 and len(readiness["models"]) <= 20
    assert [m["model"] for m in readiness["models"]] == sorted(m["model"] for m in readiness["models"])
    assert all(len(json.dumps(v)) <= 202 for m in readiness["notReady"] for v in m.values()
               if isinstance(v, str))
    # A small device is not truncated.
    small = lw.build_model_status_snapshot({MODEL: "READY"}, records_dir=str(s.tmp / "empty"))
    assert small["modelReadiness"]["truncated"] is False


def test_the_shadow_converges_under_merge_semantics(make, shadow):
    s = make(reporter=mss.report, size_rejection=mss.take_size_rejection)
    s.ticks(1)
    _seed_attempt(s.records, MODEL, 4000, s.clock.wall())
    snapshots = []

    def report(statuses):
        snapshot = lw.build_model_status_snapshot(statuses)
        merged = merge(copy.deepcopy(shadow.document), snapshot)
        shadow.document = merged
        snapshots.append(snapshot)
        for key in ("modelReadiness", "selfHeal"):
            assert merged[key] == strip_nulls(snapshot[key])
        return merged

    merged = report({MODEL: "LOADING", "model-b": "UNAVAILABLE"})
    assert [m["model"] for m in merged["modelReadiness"]["notReady"]] == [MODEL, "model-b"]
    merged = report({MODEL: "READY", "model-b": "UNAVAILABLE"})
    assert [m["model"] for m in merged["modelReadiness"]["notReady"]] == ["model-b"]
    assert {"model": MODEL, "state": "READY", "stalled": False} in merged["modelReadiness"]["models"]
    merged = report({MODEL: "READY"})
    assert "model-b" not in json.dumps(merged["modelReadiness"])
    assert merged["selfHeal"]["last"]["vllmModelsDropped"] == [{"name": "llm", "state": "READY"}]
    # A newer attempt replaces selfHeal.last and leaves no stale key.
    _seed_attempt(s.records, MODEL, 60, s.clock.wall(), vllmModelsDropped=None, evidence=["[stall] n"])
    merged = report({MODEL: "READY"})
    assert "vllmModelsDropped" not in merged["selfHeal"]["last"]
    assert merged["selfHeal"]["last"]["evidence"] == ["[stall] n"]
    assert len(json.dumps({k: merged[k] for k in ("modelReadiness", "selfHeal")},
                          sort_keys=True, separators=(",", ":"))) <= 4096


def test_the_gpu_status_endpoint_reports_both_keys_on_an_empty_repository(monkeypatch):
    installed = []

    def register(name, module):
        if name in sys.modules:
            return
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module
            installed.append(name)

    server_setup = types.ModuleType("utils.server_setup")
    for attr in ("input_cfg_accessor", "output_cfg_accessor", "lfv_edge_agent", "iot_shadow_accessor"):
        setattr(server_setup, attr, MagicMock())
    register("utils.server_setup", server_setup)
    tec = types.ModuleType("dda_triton.triton_edge_client")
    tec.TritonEdgeClient = MagicMock()
    register("dda_triton.triton_edge_client", tec)
    try:
        feature_config = import_with_awsiot_stubs("endpoints.feature_config")
    finally:
        for name in installed:
            sys.modules.pop(name, None)
    monkeypatch.delenv("AWS_IOT_THING_NAME", raising=False)
    monkeypatch.setattr(lw, "_WATCHDOG", None)
    monkeypatch.setattr(feature_config, "get_is_triton", lambda: True)
    monkeypatch.setattr(feature_config.feature_configs_utils, "triton_repo_has_models", lambda: False)
    response = feature_config.get_gpu_status()
    assert response["modelReadiness"]["models"] == [] and response["modelReadiness"]["notReady"] == []
    assert set(response["selfHeal"]) == SELF_HEAL_KEYS
    assert response["models"] == {} and response["gpuDegraded"] is False
    tec.TritonEdgeClient.get_instance.assert_not_called()


# ---------------------------------------------------------- report cadence

def test_the_report_cadence(make, shadow, monkeypatch):
    monkeypatch.setattr(mss, "DEBOUNCE_SECONDS", 45.0)
    s = make(reporter=mss.report, size_rejection=mss.take_size_rejection)
    s.index.set(MODEL, "READY")
    s.ticks(1)
    join_write()
    assert len(shadow.writes) == 1
    s.ticks(10)
    join_write()
    assert len(shadow.writes) == 1, "a healthy, unchanging device gets no watchdog writes"
    # Two transitions 30 s apart: the second lands inside the debounce window.
    s.index.set(MODEL, "UNAVAILABLE", "load failed")
    s.ticks(1)
    join_write()
    assert len(shadow.writes) == 2
    s.index.set("model-b", "UNAVAILABLE", "another failure")
    results = []
    original = mss.report

    def spy(snapshot):
        result = original(snapshot)
        results.append(result)
        return result

    s.watchdog._reporter = spy
    s.ticks(1)
    join_write()
    assert results == ["dropped"] and len(shadow.writes) == 2
    s.ticks(1)
    join_write()
    assert results == ["dropped", "scheduled"] and len(shadow.writes) == 3
    assert [m["model"] for m in shadow.document["modelReadiness"]["notReady"]] == [MODEL, "model-b"]
    # The heartbeat while a model is not ready.
    s.ticks(28)
    join_write()
    assert len(shadow.writes) == 3
    s.ticks(3)
    join_write()
    assert len(shadow.writes) == 4


# ---------------------------------------------------------- size rejection

def _legacy(name):
    return {"status": "READY", "runtime": "onnx-tensorrt-gpu-fallback-chain-padding",
            "gpuRequested": True, "gpuActive": True}


def test_a_size_rejection_retires_stale_entries(make, shadow, caplog):
    caplog.set_level(logging.WARNING)
    live = ["model-live-{0}".format(i) for i in range(10)]
    gone = ["model-gone-{0:02d}".format(i) for i in range(50)]
    legacy = {"models": {name: _legacy(name) for name in live + gone}, "gpuDegraded": False}
    shadow.document = copy.deepcopy(legacy)
    shadow.limit = 8192
    assert len(json.dumps(legacy)) < 8192
    s = make(reporter=mss.report, size_rejection=mss.take_size_rejection,
             shadow_reader=shadow.get_thing_shadow_state_request)
    s.index.states = {name: ("READY", None) for name in live}
    s.ticks(1)
    join_write()
    assert shadow.rejections == 1 and shadow.writes == []
    s.ticks(1)
    join_write()
    lines = [m for m in (r.getMessage() for r in caplog.records)
             if m.startswith("dda-model-status: ShadowManager rejected a report for its size")]
    assert lines == ["dda-model-status: ShadowManager rejected a report for its size; retiring 50 "
                     "GPU-fallback entries of models that are no longer in the Triton repository: "
                     + ", ".join(gone)]
    assert len(shadow.writes) == 1
    assert sorted(shadow.document["models"]) == live
    s.ticks(3)
    join_write()
    assert len([m for m in (r.getMessage() for r in caplog.records) if "retiring" in m]) == 1


def test_a_failed_read_retires_nothing(make, shadow, caplog):
    caplog.set_level(logging.WARNING)
    shadow.document = {"models": {"model-gone-{0:02d}".format(i): _legacy(i) for i in range(70)}}
    shadow.limit = len(json.dumps(shadow.document)) + 50
    shadow.read_raises = RuntimeError("GetThingShadow timed out")
    s = make(reporter=mss.report, size_rejection=mss.take_size_rejection,
             shadow_reader=shadow.get_thing_shadow_state_request)
    s.index.states = {"model-live-0": ("READY", None)}
    # The first tick schedules the report; the writer thread records the size
    # rejection. Join it before the second tick, which takes the flag and
    # attempts the read (as test_a_size_rejection_retires_stale_entries does).
    s.ticks(1)
    join_write()
    s.ticks(1)
    join_write()
    assert ("dda-model-status: ShadowManager rejected a report for its size, but no stale "
            "GPU-fallback entries could be retired (the reported document could not be read "
            "(GetThingShadow timed out)); the next rejection retries") in caplog.text
    assert s.watchdog.retirements == ()


def test_the_reporter_results_and_the_rejection_flag(shadow, monkeypatch):
    clock = FakeTime()
    monkeypatch.setattr(mss, "_clock", clock.monotonic)
    assert mss.report({"a": 1}) == "scheduled"
    join_write()
    assert mss.report({"a": 1}) == "unchanged"
    assert mss.report({"a": 2}) == "dropped"  # inside the debounce window
    clock.sleep(31)
    shadow.limit = 1
    assert mss.report({"a": 3}) == "scheduled"
    join_write()
    assert mss.take_size_rejection() is True
    assert mss.take_size_rejection() is False
    monkeypatch.delenv("AWS_IOT_THING_NAME")
    assert mss.report({"a": 4}) == "dropped"
    assert mss._is_size_rejection(RuntimeError("thing-413x")) is False
    assert mss._is_size_rejection(RuntimeError("HTTP 413")) is True
