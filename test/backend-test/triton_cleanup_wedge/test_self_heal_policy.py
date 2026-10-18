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
"""Self-heal (triton-model-cleanup-wedge task 4.5, design D7, Property 7):
on by default, with the per-device opt-out; conservative, rate-limited and
never looping; every way out before the commit ends the quiesce.

Honesty guard: the restart signal (``os.kill``) and ``os._exit`` are
recorders; nothing restarts. The run registry and the continuous runner
are the real ones, on fake clocks and fake streams."""
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import types
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dda_triton import load_watchdog as lw
from utils import run_activity as ra
from triton_cleanup_wedge.fakes import FakeTime
from triton_cleanup_wedge.watchdog_fakes import (
    MODEL,
    Recorder,
    Scenario,
    make_runner,
    set_stub_ticks,
    step_inserts_row,
    write_gpu_node,
)

ON = {"StreamShadowName": "s"}


class SpyActivity:
    """A real ``RunActivity`` that records the self-heal calls."""

    def __init__(self, real):
        self.real = real
        self.calls = []
        self.begin_result = None

    def __getattr__(self, name):
        attr = getattr(self.real, name)
        if name in ("begin_quiesce", "end_quiesce", "commit_restart", "abort_restart",
                    "interrupt_all"):
            def wrapper(*args, **kwargs):
                self.calls.append(name)
                if name == "begin_quiesce" and self.begin_result is not None:
                    return self.begin_result
                return attr(*args, **kwargs)
            return wrapper
        return attr

    def count(self, name):
        return self.calls.count(name)


@pytest.fixture
def make(tmp_path, monkeypatch):
    """A scenario whose watchdog uses a spied real registry, installed as the
    process registry so a real ``ContinuousRunner`` sees the same state."""
    def _make(**kwargs):
        clock = FakeTime(start=5000.0)
        real = ra.RunActivity(clock=clock.monotonic)
        spy = SpyActivity(real)
        s = Scenario(tmp_path, lw, ra, clock=clock, run_activity=spy, **kwargs)
        s.activity = real
        s.spy = spy
        previous = ra.install(real)
        monkeypatch.setattr(lw, "_WATCHDOG", s.watchdog)
        s._previous_activity = previous
        return s
    yield _make
    # Restore the process registry.
    ra.install(ra.RunActivity())


def record_of(s):
    path = os.path.join(s.records, "self_heal.json")
    if not os.path.exists(path):
        return None
    with open(path) as handle:
        return json.load(handle)


def attempts_of(s):
    record = record_of(s)
    return [] if record is None else record["attempts"]


def to_stall(s, ticks=31):
    s.ticks(ticks)


def messages(caplog, level=None):
    return [r.getMessage() for r in caplog.records if level is None or r.levelno == level]


# ------------------------------------------------------------ the switch

@pytest.mark.parametrize("config,expected", [
    ({}, (True, None, True)),
    ({"ModelLoadSelfHealEnabled": "true"}, (True, "true", True)),
    ({"ModelLoadSelfHealEnabled": True}, (True, "True", True)),
    ({"ModelLoadSelfHealEnabled": " Enabled "}, (True, "Enabled", True)),
    ({"ModelLoadSelfHealEnabled": "1"}, (True, "1", True)),
    ({"ModelLoadSelfHealEnabled": "false"}, (False, "false", True)),
    ({"ModelLoadSelfHealEnabled": False}, (False, "False", True)),
    ({"ModelLoadSelfHealEnabled": "off"}, (False, "off", True)),
    ({"ModelLoadSelfHealEnabled": "maybe"}, (False, "maybe", False)),
])
def test_parse_self_heal_switch(config, expected):
    assert lw.parse_self_heal_switch(config) == expected


def _switch(reader, tmp_path, clock=None):
    clock = clock or FakeTime()
    return lw._Switch(reader, clock.monotonic, str(tmp_path / "self-heal.disabled")), clock


def test_the_shipped_true_and_a_missing_key_mean_on(tmp_path):
    for config in (dict(ON, ModelLoadSelfHealEnabled="true"), dict(ON)):
        switch, _ = _switch(lambda: config, tmp_path)
        switch.refresh(force=True)
        assert switch.state() == lw.SwitchState(True, None, config.get("ModelLoadSelfHealEnabled"),
                                                True, switch.kill_file)


def test_false_opts_out_and_an_unrecognized_value_is_off(tmp_path, caplog):
    switch, _ = _switch(lambda: dict(ON, ModelLoadSelfHealEnabled="false"), tmp_path)
    switch.refresh(force=True)
    assert switch.state().enabled is False and switch.state().disabled_by == "config"
    caplog.set_level(logging.WARNING)
    switch, _ = _switch(lambda: dict(ON, ModelLoadSelfHealEnabled="perhaps"), tmp_path)
    switch.refresh(force=True)
    switch.refresh(force=True)
    assert switch.state().disabled_by == "config"
    assert messages(caplog).count("ModelLoadSelfHealEnabled has an unrecognized value 'perhaps'; "
                                  "self-heal is off (treated as an opt-out)") == 1


@pytest.mark.parametrize("first", [dict(ON, ModelLoadSelfHealEnabled="true"), dict(ON)])
def test_a_failed_read_keeps_the_last_on_value(tmp_path, first):
    answers = [first, {}]
    switch, _ = _switch(lambda: answers.pop(0) if len(answers) > 1 else answers[0], tmp_path)
    switch.refresh(force=True)
    switch.refresh(force=True)
    assert switch.state().enabled is True


def test_a_failed_read_keeps_the_last_off_value(tmp_path):
    answers = [dict(ON, ModelLoadSelfHealEnabled="false"), {}]
    switch, _ = _switch(lambda: answers.pop(0) if len(answers) > 1 else answers[0], tmp_path)
    switch.refresh(force=True)
    switch.refresh(force=True)
    assert switch.state().enabled is False and switch.state().disabled_by == "config"


@pytest.mark.parametrize("reader", [
    lambda: {},
    lambda: {"ModelLoadSelfHealEnabled": "true"},  # no StreamShadowName
    lambda: ["not", "a", "dict"],
    lambda: (_ for _ in ()).throw(RuntimeError("IPC timeout")),
])
def test_failed_reads_before_any_success_mean_off(tmp_path, reader, caplog):
    caplog.set_level(logging.WARNING)
    switch, _ = _switch(reader, tmp_path)
    switch.refresh(force=True)
    switch.refresh(force=True)
    state = switch.state()
    assert state.enabled is False and state.disabled_by == "config_unreadable"
    warnings = [m for m in messages(caplog) if m.startswith("Self-heal configuration could not be read")]
    assert len(warnings) == 1, "one WARNING per streak"
    assert warnings[0].endswith("; self-heal stays off until a read succeeds")


def test_the_kill_file_wins(tmp_path):
    for config in (dict(ON, ModelLoadSelfHealEnabled="true"), dict(ON)):
        switch, _ = _switch(lambda: config, tmp_path)
        switch.refresh(force=True)
        open(switch.kill_file, "w").close()
        assert switch.state().enabled is False and switch.state().disabled_by == "file"
        os.remove(switch.kill_file)
        assert switch.state().enabled is True


def test_a_failed_read_is_retried_no_sooner_than_60s(tmp_path):
    calls = {"n": 0}

    def reader():
        calls["n"] += 1
        raise RuntimeError("IPC timeout")

    switch, clock = _switch(reader, tmp_path)
    switch.refresh(force=False)
    assert calls["n"] == 1
    for _ in range(5):
        clock.sleep(10)
        switch.refresh(force=False)
    assert calls["n"] == 1, "the periodic refresh waits 60 s after a failure"
    clock.sleep(11)
    switch.refresh(force=False)
    assert calls["n"] == 2
    switch.refresh(force=True)  # an attempt's reads are always made
    assert calls["n"] == 3


def test_a_successful_value_is_refreshed_every_300s(tmp_path):
    calls = {"n": 0}

    def reader():
        calls["n"] += 1
        return dict(ON)

    switch, clock = _switch(reader, tmp_path)
    switch.refresh()
    clock.sleep(299)
    switch.refresh()
    assert calls["n"] == 1
    clock.sleep(2)
    switch.refresh()
    assert calls["n"] == 2


def test_the_module_level_reader_is_created_once(monkeypatch):
    class Operation:
        def __init__(self, ipc, kind):
            self.ipc, self.kind = ipc, kind

        def activate(self, request):
            self.ipc.calls.append(self.kind)

        def get_response(self):
            ipc = self.ipc

            class _Future:
                def result(self_inner, timeout):
                    if self.kind == "list":
                        component = types.SimpleNamespace(component_name="aws.edgeml.dda.LocalServer.arm64JP5")
                        return types.SimpleNamespace(components=[component])
                    return types.SimpleNamespace(value=dict(ON))
            return _Future()

        def close(self):
            pass

    class FakeIpc:
        def __init__(self):
            self.calls = []

        def new_list_components(self):
            return Operation(self, "list")

        def new_get_configuration(self):
            return Operation(self, "get")

    ipc = FakeIpc()
    awsiot = types.ModuleType("awsiot")
    ggipc = types.ModuleType("awsiot.greengrasscoreipc")
    model = types.ModuleType("awsiot.greengrasscoreipc.model")
    model.GetConfigurationRequest = MagicMock()
    model.ListComponentsRequest = MagicMock()
    model.UnauthorizedError = type("UnauthorizedError", (Exception,), {})
    awsiot.greengrasscoreipc = ggipc
    ggipc.model = model
    deprecated = types.ModuleType("deprecated")
    deprecated.deprecated = lambda *a, **k: (lambda fn: fn)
    ipc_client = types.ModuleType("utils.ipc_client")
    ipc_client.get_ipc_client = lambda: ipc
    for name, module in (("awsiot", awsiot), ("awsiot.greengrasscoreipc", ggipc),
                         ("awsiot.greengrasscoreipc.model", model), ("deprecated", deprecated),
                         ("utils.ipc_client", ipc_client)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "defect_detection_config.defect_detection_config", raising=False)
    monkeypatch.setattr(lw, "_SWITCH_READER", None)
    assert lw._read_localserver_config() == ON
    assert lw._read_localserver_config() == ON
    assert ipc.calls.count("list") == 1, "the component name is discovered once"
    assert ipc.calls.count("get") == 2, "get_component_config is uncached"
    reader = lw._SWITCH_READER
    lw._read_localserver_config()
    assert lw._SWITCH_READER is reader


# ------------------------------------------------------------ the policy

def _verdict(stalled=(MODEL,)):
    models = {m: lw.ModelVerdict(m, True, True, False, True, ("[stall] x",), 1000.0) for m in stalled}
    return lw.WedgeVerdict(models=models, stalled=tuple(stalled), culprit=stalled[0] if stalled else None,
                           veto=False, window_cpu_s=0.0, window_covered=True, no_progress_s=1000.0)


ENABLED = lw.SwitchState(True, None, None, True)
NOW = 1_790_000_000.0


def _attempt(age_s, boot="boot-1", boottime=100_000.0, action="backend_restart"):
    return {"attempt": 1, "requestedAt": lw.iso(NOW - age_s), "bootId": boot,
            "bootSeconds": boottime - age_s, "action": action}


def decide(history=(), switch=ENABLED, activity=None, gpu=lw.GpuWindow(False), vllm=None,
           local=lw.LocalHoldoffs(), verdict=None, error=None, boot="boot-1", boottime=100_000.0,
           now_wall=NOW, now_mono=50_000.0):
    return lw.decide_self_heal(verdict or _verdict(), switch,
                               lw.History(tuple(history), error, boot, boottime),
                               activity, gpu, vllm or lw.VllmView(), local, now_wall, now_mono)


def test_the_policy_table_in_order():
    assert decide(verdict=_verdict(())).kind == "idle"
    assert decide(switch=lw.SwitchState(False, "config", "false", True)).kind == "disabled"
    assert decide(error="is unreadable (x)").kind == "blocked_record"
    exhausted = decide(history=[_attempt(4000), _attempt(8000), _attempt(12000)])
    assert exhausted.kind == "exhausted" and exhausted.attempts_in_24h == 3
    assert exhausted.rearms_at_wall == pytest.approx(NOW + 86400 - 12000)
    cooldown = decide(history=[_attempt(600)])
    assert cooldown.kind == "cooldown" and cooldown.until_wall == pytest.approx(NOW + 1200)
    act = decide(history=[_attempt(4000), _attempt(90_000)])
    assert act.kind == "act" and act.attempt == 2


def test_not_performed_attempts_count_toward_the_limit():
    history = [_attempt(4000, action="not_performed"), _attempt(8000), _attempt(12000)]
    assert decide(history=history).kind == "exhausted"


def test_an_attempt_dated_in_the_future_counts_against_both_limits():
    future = {"attempt": 1, "requestedAt": lw.iso(NOW + 3600), "action": "backend_restart"}
    assert decide(history=[future], boot="other-boot").kind == "cooldown"
    assert decide(history=[future, future, future], boot="other-boot").kind == "exhausted"


def test_same_boot_ages_ignore_a_wall_clock_step():
    attempt = _attempt(600)
    # A forward wall step of hours does not end the cooldown within one boot.
    assert decide(history=[attempt], now_wall=NOW + 6 * 3600).kind == "cooldown"
    # In another boot the wall clock decides.
    assert decide(history=[attempt], boot="boot-2", now_wall=NOW + 6 * 3600).kind == "act"


def test_a_local_abort_holds_off_for_30_minutes():
    local = lw.LocalHoldoffs(aborted_at=50_000.0 - 1700)
    assert decide(local=local).kind == "cooldown"
    local = lw.LocalHoldoffs(aborted_at=50_000.0 - 1801)
    assert decide(local=local).kind == "act"


def test_rule_6_vllm():
    assert decide(vllm=lw.VllmView({"llm": ("STAGED", "LOADING")})).deferred_by == "vllm"
    assert decide(vllm=lw.VllmView({"llm": ("LOADING", "LOADING")})).deferred_by == "vllm"
    assert decide(vllm=lw.VllmView({"llm": ("READY", "READY")})).kind == "act"
    raising = decide(vllm=lw.VllmView(error="manager offline"))
    assert raising.kind == "deferred" and raising.deferred_by == "vllm"
    assert lw.recovery_action(raising, MODEL, ENABLED) == (
        "Self-heal will restart the LocalServer backend once the vLLM runtime can list its models again.")


def test_default_vllm_states():
    class Status:
        def __init__(self, state):
            self.state = types.SimpleNamespace(value=state)
            self.reason = None

    fcu = types.SimpleNamespace(get_vllm_manager=lambda: None)
    assert lw._default_vllm_states(fcu) == {}, "no vLLM runtime means no vLLM models"
    mapping = {"STAGED": "LOADING", "READY": "READY", "FAILED": "FAILED", "UNLOADED": "STOPPED"}
    manager = types.SimpleNamespace(list_models=lambda: {
        "a": Status("READY"), "b": Status("STAGED"), "c": Status("FAILED"), "d": Status("UNLOADED")})
    fcu = types.SimpleNamespace(
        get_vllm_manager=lambda: manager,
        _vllm_feature_status=lambda s: (mapping[s.state.value], None))
    assert lw._default_vllm_states(fcu) == {"a": ("READY", "READY"), "b": ("STAGED", "LOADING"),
                                            "c": ("FAILED", "FAILED"), "d": ("UNLOADED", "STOPPED")}

    def boom():
        raise RuntimeError("down")

    fcu = types.SimpleNamespace(get_vllm_manager=lambda: types.SimpleNamespace(list_models=boom))
    with pytest.raises(RuntimeError):
        lw._default_vllm_states(fcu)


def test_rule_7_gpu():
    busy = decide(gpu=lw.GpuWindow(True, 1))
    assert busy.kind == "deferred" and busy.deferred_by == "gpu"
    assert lw.recovery_action(busy, MODEL, ENABLED).startswith(
        "Self-heal will restart the LocalServer backend once the GPU shows no load for 5 min "
        "(it reached 0.1% in the last 5 min")
    assert decide(gpu=lw.GpuWindow(True, 0)).kind == "act"
    assert decide(gpu=lw.GpuWindow(False, None)).kind == "act"


def _activity(tickets=(), continuous=None, labels=None, now=1000.0):
    return ra.ActivitySnapshot(tickets=tuple(tickets), continuous_starts=dict(continuous or {}),
                               now=now, state="IDLE", continuous_labels=dict(labels or {}))


def _ticket(run_id="exec-1", in_gate=False, outside=10.0, models=(MODEL,), workflow="wf-1 v2",
            continuous=False):
    return ra.TicketInfo("execution", run_id, models, workflow, 0.0, in_gate, False,
                         None if in_gate else 0.0, 0.0 if in_gate else outside, True, continuous)


def test_rule_8_runs_and_continuous_workflows():
    deferred = decide(activity=_activity([_ticket()]))
    assert deferred.kind == "deferred" and deferred.deferred_by == "runs"
    assert deferred.runs == ("wf-1 v2",)
    assert decide(activity=_activity([_ticket(in_gate=True)])).kind == "act"
    assert decide(activity=_activity([_ticket(outside=901.0)])).kind == "act"
    running = decide(activity=_activity(continuous={"wf-c:1": 990.0}, labels={"wf-c:1": "wf-c v1"}))
    assert running.kind == "deferred" and running.continuous == ("wf-c v1",)
    assert lw.recovery_action(running, MODEL, ENABLED) == (
        "Self-heal will restart the LocalServer backend once no workflow run or capture task is in "
        "flight (0 in flight: none) and no continuous workflow is running (running: wf-c v1). Pause "
        "those workflows, or restart the LocalServer backend manually, to clear the wedge.")
    assert decide(activity=_activity(continuous={"wf-c:1": 910.0})).kind == "act"


def test_a_deferral_after_quiescing_holds_off_for_300s():
    local = lw.LocalHoldoffs(deferred_at=50_000.0 - 299, deferred_by="runs", deferred_recovery="R")
    held = decide(local=local)
    assert held.kind == "deferred" and held.deferred_by == "runs" and held.recovery == "R"
    local = lw.LocalHoldoffs(deferred_at=50_000.0 - 301, deferred_by="gpu")
    assert decide(local=local).kind == "act"


def test_recovery_texts():
    assert lw.recovery_action(decide(), MODEL, ENABLED) == (
        "Self-heal is restarting the LocalServer backend (attempt 1 of 3 in 24 h).")
    cooldown = decide(history=[_attempt(600)])
    assert lw.recovery_action(cooldown, MODEL, ENABLED) == (
        "Self-heal will restart the LocalServer backend after {0} (at most one restart per 30 min)."
        .format(lw.iso(NOW + 1200)))
    exhausted = decide(history=[_attempt(4000), _attempt(8000), _attempt(12000)])
    assert lw.recovery_action(exhausted, MODEL, ENABLED) == (
        "Self-heal is exhausted: it restarted the backend 3 times in the last 24 h. Restart the "
        "LocalServer backend manually and investigate model-a; self-heal re-arms at {0}."
        .format(lw.iso(NOW + 86400 - 12000)))
    off = lw.recovery_action(lw.SelfHealDecision(kind="disabled"), MODEL,
                             lw.SwitchState(False, "config", "false", True))
    assert off == ("Self-heal is off on this device (ModelLoadSelfHealEnabled=false). Restart the "
                   "LocalServer backend: LocalServer UI, Application health overview, Application logs "
                   "and restart; or on the device `sudo docker restart <backend container>`.")


# ------------------------------------------------------------ the record

def test_a_corrupted_or_oversized_record_blocks_self_heal(make, caplog):
    caplog.set_level(logging.ERROR)
    s = make()
    path = os.path.join(s.records, "self_heal.json")
    with open(path, "w") as handle:
        handle.write("{not json")
    to_stall(s)
    assert s.watchdog.view.decision.kind == "blocked_record"
    assert s.spy.count("begin_quiesce") == 0
    errors = [m for m in messages(caplog, logging.ERROR) if m.startswith("SELF-HEAL off: the record")]
    assert len(errors) == 1 and errors[0].endswith("; delete or repair it to re-enable self-heal")
    with open(path, "w") as handle:
        handle.write(json.dumps({"schema": 1, "attempts": [], "pad": "x" * 70000}))
    s.ticks(2)
    assert s.watchdog.view.decision.kind == "blocked_record"
    assert open(path).read().startswith('{"schema": 1, "attempts": [], "pad"'), "left untouched"


def test_a_failing_write_probe_blocks_before_any_quiesce(make, caplog, monkeypatch):
    caplog.set_level(logging.ERROR)
    s = make()
    monkeypatch.setattr(lw, "write_record", lambda d, doc: "Read-only file system")
    to_stall(s)
    assert s.watchdog.view.decision.kind == "blocked_record"
    assert s.spy.count("begin_quiesce") == 0
    assert any(m.startswith("SELF-HEAL off: the record {0} cannot be written (Read-only file system)"
                            .format(os.path.join(s.records, "self_heal.json"))) for m in messages(caplog))
    assert s.killer.calls == []


def test_attempt_caps_on_write(make, monkeypatch):
    s = make()
    to_stall(s, 30)
    obs = s.watchdog._last_obs
    loading = {"model-{0:02d}".format(i): lw.LoadingModel(obs.now_mono - 1000 - i, obs.now_wall - 1000 - i,
                                                          1 if i == 7 else 0) for i in range(30)}
    models = {m: lw.ModelVerdict(m, True, True, False, True,
                                 tuple("[stall] {0} item {1}".format(m, k) for k in range(12)), 1000.0)
              for m in loading}
    verdict = lw.WedgeVerdict(models=models, stalled=tuple(sorted(loading)), culprit="model-07",
                              veto=False, window_cpu_s=0.0, window_covered=True, no_progress_s=1000.0)
    s.watchdog._last_obs = lw._Observation(obs.now_mono, obs.now_wall, {}, {}, loading, 0.0, True,
                                           lw.GpuWindow(False), verdict)
    attempt, _snapshot = s.watchdog._build_attempt(1)
    assert len(attempt["models"]) == 10
    assert attempt["models"][0] == {
        "name": "model-07", "suspectedCulprit": True,
        "loadingForSeconds": int(s.clock.monotonic() - loading["model-07"].since_mono)}
    assert [m["name"] for m in attempt["models"][1:4]] == ["model-29", "model-28", "model-27"]
    assert len(attempt["evidence"]) == 5
    assert attempt["evidence"][0] == "[stall] model-07 item 0"


def _long_attempt(age_s, boottime, n_vllm=20):
    text = "x" * 400
    return {
        "attempt": 1, "requestedAt": lw.iso(NOW - age_s), "bootId": "boot-1",
        "bootSeconds": boottime - age_s, "restartedAt": lw.iso(NOW - age_s + 60),
        "models": [{"name": text + str(i), "loadingForSeconds": 4032, "suspectedCulprit": i == 0}
                   for i in range(10)],
        "evidence": [text + str(i) for i in range(5)], "action": "backend_restart",
        "runsBlocked": 99,
        "runsInterrupted": [{"kind": "execution", "id": text + str(i), "workflow": text}
                            for i in range(10)],
        "vllmModelsDropped": [{"name": text + str(i), "state": "READY"} for i in range(n_vllm)],
        "budget": {"maxPer24h": 3, "minIntervalSeconds": 1800},
    }


def test_pruning_and_the_largest_writable_record_passes_the_reader(tmp_path):
    boottime = 200_000.0
    old = [lw._cut_all(_long_attempt(90_000 + i * 100, boottime), lw.SELF_HEAL_TEXT_MAX)
           for i in range(19)]
    recent = [lw._cut_all(_long_attempt(age, boottime), lw.SELF_HEAL_TEXT_MAX)
              for age in (7200, 3600)]
    document = {"schema": 1, "attempts": old + recent}
    new = lw._cut_all(_long_attempt(0, boottime), lw.SELF_HEAL_TEXT_MAX)
    document["attempts"] = document["attempts"] + [new]
    pruned = lw.prune_record(document, "boot-1", boottime, NOW)
    assert len(pruned["attempts"]) <= lw.SELF_HEAL_HISTORY_MAX
    ages = [lw.attempt_age_s(a, "boot-1", boottime, NOW) for a in pruned["attempts"]]
    assert sum(1 for a in ages if a < 86400) == 3, "the attempts under 24 h are kept"
    assert lw.write_record(str(tmp_path), pruned) is None
    document_read, error = lw.read_self_heal_record(str(tmp_path))
    assert error is None and document_read == pruned
    size = os.path.getsize(os.path.join(str(tmp_path), "self_heal.json"))
    assert size <= lw.SELF_HEAL_RECORD_READ_MAX_BYTES
    # Old attempts alone are dropped down to 48 KiB.
    small = {"schema": 1, "attempts": [lw._cut_all(_long_attempt(90_000 + i, boottime, n_vllm=0),
                                                   lw.SELF_HEAL_TEXT_MAX) for i in range(20)]}
    pruned = lw.prune_record(small, "boot-1", boottime, NOW)
    assert len(lw.serialize_record(pruned)) <= lw.SELF_HEAL_RECORD_WRITE_MAX_BYTES
    assert pruned["attempts"][-1] == small["attempts"][-1], "the oldest are dropped first"


def test_record_validation():
    assert lw.validate_record({"schema": 1, "attempts": []}) is None
    assert lw.validate_record({"schema": 2, "attempts": []})
    assert lw.validate_record({"schema": 1, "attempts": [{"requestedAt": "yesterday"}]})
    assert lw.validate_record({"schema": 1, "attempts": [dict(_attempt(1), action="reboot")]})
    assert lw.validate_record({"schema": 1, "attempts": [_attempt(1)] * 21})
    assert lw.validate_record({"schema": 1, "attempts": [dict(_attempt(1), runsInterrupted=[1])]})
    # A malformed bootId or bootSeconds only makes that age wall-clock based.
    assert lw.validate_record({"schema": 1, "attempts": [dict(_attempt(1), bootId=5,
                                                              bootSeconds="x")]}) is None


# --------------------------------------------------------------- the action

def test_the_happy_path_restarts_once_with_one_error_line(make, caplog):
    caplog.set_level(logging.INFO)
    s = make()

    def at_signal(pid, sig):
        attempts = attempts_of(s)
        assert len(attempts) == 1 and attempts[0]["action"] == "backend_restart", \
            "the record holds the attempt when the signal is sent"
        assert s.activity.state() == ra.STATE_RESTARTING

    s.killer.side_effect = at_signal
    to_stall(s)
    assert s.watchdog.signalled
    import signal as signal_module
    assert s.killer.calls == [(os.getpid(), signal_module.SIGTERM)]
    errors = [m for m in messages(caplog, logging.ERROR) if m.startswith("SELF-HEAL: restarting")]
    assert len(errors) == 1
    assert errors[0].startswith(
        "SELF-HEAL: restarting the LocalServer backend to clear a wedged Triton model load. Models "
        "LOADING: model-a for 15m00s (suspected culprit). Evidence: [stall] the Triton model index "
        "has not changed for 15m00s")
    assert errors[0].endswith(
        "Action: backend process restart (attempt 1 of at most 3 per 24h, at least 30m apart; 0 "
        "run(s) blocked by the wedge are interrupted, 0 of them to be marked failed; vLLM models "
        "dropped: none). Record: {0}".format(os.path.join(s.records, "self_heal.json")))
    assert any(m.startswith("SELF-HEAL quiescing: the Triton model load is wedged (models LOADING: "
                            "model-a for 15m00s (suspected culprit)); pausing continuous workflows and "
                            "waiting up to 60s for 0 run(s) not blocked by the wedge to finish; new "
                            "workflow runs count as in flight") for m in messages(caplog))
    attempt = attempts_of(s)[0]
    assert attempt["models"] == [{"name": MODEL, "loadingForSeconds": 900, "suspectedCulprit": True}]
    assert attempt["bootId"] == "boot-1" and attempt["restartedAt"] is None
    assert attempt["budget"] == {"maxPer24h": 3, "minIntervalSeconds": 1800}
    assert s.watchdog.view.decision.state == "restarting"
    s.ticks(3)
    assert len(s.killer.calls) == 1, "nothing more after the signal"
    # The next process closes the attempt out.
    caplog.clear()
    s.restart()
    s.watchdog.startup()
    assert attempts_of(s)[0]["restartedAt"] is not None
    assert any(m.startswith("SELF-HEAL: the backend restarted after self-heal attempt 1 (requested ")
               for m in messages(caplog))


def test_the_start_line(make, caplog):
    caplog.set_level(logging.INFO)
    make().watchdog.startup()
    assert ("Triton model-load watchdog started (self-heal on (default); GPU load signal unavailable; "
            "stall after 15m, at most 3 restarts per 24h and 30m apart)") in messages(caplog)


def test_an_opt_out_never_acts(make, caplog):
    caplog.set_level(logging.WARNING)
    s = make(config=dict(ON, ModelLoadSelfHealEnabled="false"))
    to_stall(s)
    s.ticks(100)
    assert s.killer.calls == [] and s.spy.count("begin_quiesce") == 0
    lines = [m for m in messages(caplog) if m.startswith("Triton model load wedge detected")]
    assert len(lines) == 1
    assert lines[0].endswith("self-heal is off on this device (ModelLoadSelfHealEnabled=false). "
                             "Restart the LocalServer backend manually.")


def test_an_unread_configuration_never_acts(make):
    s = make(config={})
    to_stall(s)
    s.ticks(20)
    assert s.killer.calls == []
    assert s.watchdog.view.switch.disabled_by == "config_unreadable"


@pytest.mark.parametrize("step", ["building", "recording", "logging"])
def test_a_failure_after_the_commit_aborts_and_holds_off(make, caplog, monkeypatch, step):
    caplog.set_level(logging.INFO)
    s = make()
    if step == "building":
        monkeypatch.setattr(s.watchdog, "_build_attempt", MagicMock(side_effect=RuntimeError("boom")))
    elif step == "recording":
        monkeypatch.setattr(s.watchdog, "_persist_attempt", MagicMock(side_effect=OSError("disk full")))
    else:
        monkeypatch.setattr(lw, "_flush_logging", MagicMock(side_effect=RuntimeError("handler")))
    to_stall(s)
    assert s.killer.calls == [] and s.exiter.calls == []
    assert s.spy.count("abort_restart") == 1
    assert s.activity.state() == ra.STATE_IDLE
    assert s.activity.start_run("execution", "after", bind=False).refused is None
    errors = [m for m in messages(caplog, logging.ERROR) if m.startswith("SELF-HEAL not performed: " + step)]
    assert len(errors) == 1
    attempts = attempts_of(s)
    if step == "logging":
        assert attempts[0]["action"] == "not_performed"
        assert attempts[0]["notPerformedReason"].startswith("logging the attempt failed")
        assert "is recorded as not performed" in errors[0]
    else:
        assert attempts == []
        assert "could not be recorded" in errors[0]
    s.ticks(59)  # 29.5 min
    assert s.watchdog.view.decision.kind == "cooldown"
    assert s.killer.calls == []
    if step == "logging":
        # The close-out at the next start skips a not-performed attempt.
        s.restart()
        s.watchdog.startup()
        assert attempts_of(s)[0]["restartedAt"] is None


def _assert_way_out(s, *, decision_kind, deferred_by=None):
    assert s.spy.count("end_quiesce") >= 1
    assert s.activity.state() == ra.STATE_IDLE
    runner, store, clock = make_runner()
    assert step_inserts_row(runner, store, clock), "continuous workflows resume"
    assert attempts_of(s) == []
    assert s.killer.calls == []
    assert s.watchdog.view.decision.kind == decision_kind
    if deferred_by:
        assert s.watchdog.view.decision.deferred_by == deferred_by


def test_a_drain_that_raises(make, caplog, monkeypatch):
    caplog.set_level(logging.ERROR)
    s = make()
    monkeypatch.setattr(s.watchdog, "_drain", MagicMock(side_effect=RuntimeError("drain fault")))
    to_stall(s)
    errors = [r for r in caplog.records if r.getMessage().startswith("SELF-HEAL not performed: the drain")]
    assert len(errors) == 1 and errors[0].exc_info is not None
    assert errors[0].getMessage() == (
        "SELF-HEAL not performed: the drain failed before the commit (drain fault); continuous "
        "workflows resume, and self-heal waits 30m before it acts again. Restart the LocalServer "
        "backend manually to clear the wedge.")
    _assert_way_out(s, decision_kind="cooldown")
    s.ticks(59)
    assert s.watchdog.view.decision.kind == "cooldown" and s.spy.count("begin_quiesce") == 1
    s.ticks(2)
    assert s.spy.count("begin_quiesce") == 2, "after 30 min it may act again"


@pytest.mark.parametrize("what", ["index", "proc"])
def test_a_recheck_read_that_fails(make, caplog, monkeypatch, what):
    caplog.set_level(logging.WARNING)
    s = make()

    def drain():
        if what == "index":
            s.index.raises = RuntimeError("empty model index")
        else:
            shutil.rmtree(s.proc)
        return []

    monkeypatch.setattr(s.watchdog, "_drain", drain)
    to_stall(s)
    text = ("the Triton model index could not be read (empty model index)" if what == "index"
            else "the stub processes under /proc could not be read (")
    lines = [m for m in messages(caplog) if m.startswith("SELF-HEAL deferred at commit: " + text)]
    assert len(lines) == 1
    assert lines[0].endswith("; the quiesce ends and continuous workflows resume. Not quiescing again for 5m")
    _assert_way_out(s, decision_kind="deferred", deferred_by="recheck")
    assert s.spy.count("commit_restart") == 0
    s.index.raises = None
    if what == "proc":
        os.makedirs(s.proc)
    s.ticks(9)
    assert s.spy.count("begin_quiesce") == 1, "no new quiesce for 300 s"


@pytest.mark.parametrize("where", ["decide", "commit"])
def test_an_exception_in_the_recheck_or_the_commit(make, caplog, monkeypatch, where):
    caplog.set_level(logging.ERROR)
    s = make()
    if where == "decide":
        original = lw.decide_self_heal
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            if s.activity.state() == ra.STATE_QUIESCING:
                raise RuntimeError("policy fault")
            return original(*args, **kwargs)

        monkeypatch.setattr(lw, "decide_self_heal", flaky)
        step = "the re-check"
    else:
        monkeypatch.setattr(s.spy.real, "commit_restart", MagicMock(side_effect=RuntimeError("commit fault")))
        step = "the commit"
    to_stall(s)
    errors = [m for m in messages(caplog, logging.ERROR)
              if m.startswith("SELF-HEAL not performed: {0} failed before the commit".format(step))]
    assert len(errors) == 1
    _assert_way_out(s, decision_kind="cooldown")


def test_begin_quiesce_refused(make, caplog):
    caplog.set_level(logging.WARNING)
    s = make()
    s.spy.begin_result = False
    to_stall(s)
    assert ("SELF-HEAL: run activity is IDLE, not IDLE; not quiescing, and self-heal waits 30m "
            "before it acts again") in messages(caplog)
    assert s.spy.count("end_quiesce") == 0
    assert s.watchdog.view.decision.kind == "cooldown"


def test_a_recheck_stretched_past_120s(make, caplog):
    caplog.set_level(logging.WARNING)
    calls = {"n": 0}
    s = None

    def vllm():
        calls["n"] += 1
        if s is not None and s.activity.state() == ra.STATE_QUIESCING:
            s.clock.sleep(125)  # the re-check's list_models() hangs
        return {}

    s = make(vllm=vllm)
    to_stall(s)
    assert ("SELF-HEAL deferred at commit: the quiesce expired after 120s before the commit; the "
            "quiesce ends and continuous workflows resume. Not quiescing again for 5m") in messages(caplog)
    _assert_way_out(s, decision_kind="deferred", deferred_by="recheck")


def test_unblocked_runs_defer_and_the_drain_defers_after_quiescing(make, caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    s = make()
    to_stall(s, 30)
    ticket = s.activity.start_run("execution", "exec-1", models=("model-b",), workflow="wf-1",
                                  bind=False)
    s.activity.set_details(ticket, workflow_label="wf-1 v2")
    s.ticks(1)
    assert s.watchdog.view.decision.kind == "deferred"
    assert s.watchdog.view.decision.runs == ("wf-1 v2",)
    assert any(m.startswith("SELF-HEAL deferred: the Triton model load is wedged") and
               "waiting because 1 workflow run(s) or capture task(s) not blocked by the wedge are in "
               "flight (wf-1 v2)" in m for m in messages(caplog))
    assert s.spy.count("begin_quiesce") == 0
    s.activity.finish_run(ticket)

    # A run that starts after the quiesce began keeps the drain waiting.
    original = s.spy.real.begin_quiesce

    def begin():
        result = original()
        s.activity.start_run("capture", "cap-1", workflow="wf-cap", bind=False)
        return result

    monkeypatch.setattr(s.spy.real, "begin_quiesce", begin)
    s.ticks(1)
    assert ("SELF-HEAL deferred after quiescing: 1 run(s) or capture task(s) not blocked by the "
            "wedge were still in flight after 60s (wf-cap); the quiesce ends and continuous "
            "workflows resume. Not quiescing again for 5m") in messages(caplog)
    _assert_way_out(s, decision_kind="deferred", deferred_by="runs")
    assert s.spy.count("begin_quiesce") == 1
    for t in s.activity.snapshot().tickets:
        pass
    s.ticks(8)
    assert s.spy.count("begin_quiesce") == 1, "no new quiesce for 300 s"


def test_a_continuous_workflow_defers_and_is_named(make):
    s = make()
    to_stall(s, 30)
    ticket = s.activity.start_run("execution", "exec-c", workflow="wf-c:1", continuous=True,
                                  bind=False)
    s.activity.set_details(ticket, workflow_label="wf-c v1")
    s.activity.finish_run(ticket)
    s.clock.sleep(10)
    s.ticks(1)
    view = s.watchdog.view
    assert view.decision.kind == "deferred" and view.decision.continuous == ("wf-c v1",)
    assert "(running: wf-c v1)" in view.recovery_action
    s.clock.sleep(60)
    s.ticks(1)
    assert s.killer.calls, "90 s after its last execution it no longer defers"


@pytest.mark.parametrize("change,reason,holdoff", [
    ("kill_file", "the kill file {kill} exists", False),
    ("opt_out", "ModelLoadSelfHealEnabled=false", False),
    ("vllm_staged", "the vLLM model llm is STAGED", True),
    ("vllm_raising", "the vLLM runtime could not list its models (manager offline)", True),
    ("gpu", "the GPU was busy (up to 0.1%)", True),
    ("ready", "no model is stalled any more (model-a is READY)", False),
    ("record", "the self-heal record changed (cooldown)", False),
])
def test_the_recheck_before_the_commit(make, caplog, monkeypatch, change, reason, holdoff):
    caplog.set_level(logging.WARNING)
    state = {"vllm": {}, "config": dict(ON)}
    s = make(vllm=lambda: state["vllm"] if not isinstance(state["vllm"], Exception)
             else (_ for _ in ()).throw(state["vllm"]),
             config=lambda: state["config"])
    if change == "gpu":
        load = write_gpu_node(s.sys, value="0")
        s.watchdog = s.load_watchdog.LoadWatchdog(**s.kwargs)
        monkeypatch.setattr(lw, "_WATCHDOG", s.watchdog)

    def drain():
        if change == "kill_file":
            open(os.path.join(s.records, "self-heal.disabled"), "w").close()
        elif change == "opt_out":
            state["config"] = dict(ON, ModelLoadSelfHealEnabled="false")
        elif change == "vllm_staged":
            state["vllm"] = {"llm": ("STAGED", "LOADING")}
        elif change == "vllm_raising":
            state["vllm"] = RuntimeError("manager offline")
        elif change == "gpu":
            with open(load, "w") as handle:
                handle.write("1")
        elif change == "ready":
            s.index.set(MODEL, "READY")
        elif change == "record":
            lw.write_record(s.records, {"schema": 1, "attempts": [
                {"attempt": 1, "requestedAt": lw.iso(s.clock.wall() - 60), "action": "backend_restart"}]})
        return []

    monkeypatch.setattr(s.watchdog, "_drain", drain)
    to_stall(s)
    expected = reason.format(kill=os.path.join(s.records, "self-heal.disabled"))
    lines = [m for m in messages(caplog) if m.startswith("SELF-HEAL deferred at commit: " + expected)]
    assert len(lines) == 1, messages(caplog)
    assert lines[0].endswith(". Not quiescing again for 5m") is holdoff
    assert s.spy.count("commit_restart") == 0
    assert s.spy.count("end_quiesce") >= 1
    assert s.activity.state() == ra.STATE_IDLE
    if change == "record":
        assert len(attempts_of(s)) == 1, "no attempt of its own was written"
    else:
        assert attempts_of(s) == []
    assert s.killer.calls == []
    if holdoff:
        if change == "gpu":
            with open(load, "w") as handle:
                handle.write("0")
        state["vllm"] = {}
        s.ticks(9)
        assert s.spy.count("begin_quiesce") == 1, "no new quiesce for 300 s"


def test_vllm_models_dropped(make, caplog):
    caplog.set_level(logging.WARNING)
    calls = {"n": 0}

    def vllm():
        calls["n"] += 1
        states = {"a": ("READY", "READY"), "b": ("STAGED", "LOADING"), "c": ("FAILED", "FAILED"),
                  "d": ("UNLOADED", "STOPPED")}
        if s.activity.state() != ra.STATE_RESTARTING:
            # Before the commit only READY models: nothing defers.
            return {"a": states["a"], "c": states["c"], "d": states["d"]}
        if s.watchdog.signalled is False and calls.get("built"):
            return dict(states, e=("READY", "READY"))  # appeared before the signal
        calls["built"] = True
        return dict(states)

    s = make(vllm=vllm)
    to_stall(s)
    assert s.killer.calls
    dropped = attempts_of(s)[0]["vllmModelsDropped"]
    assert dropped == [{"name": "a", "state": "READY"}, {"name": "b", "state": "STAGED"},
                       {"name": "e", "state": "READY"}]


def test_vllm_models_dropped_unknown_when_listing_raises(make, caplog):
    caplog.set_level(logging.WARNING)

    def vllm():
        if s.activity.state() == ra.STATE_RESTARTING:
            raise RuntimeError("manager offline")
        return {}

    s = make(vllm=vllm)
    to_stall(s)
    assert s.killer.calls, "the attempt goes on"
    assert attempts_of(s)[0]["vllmModelsDropped"] is None
    assert ("SELF-HEAL could not list the vLLM models (manager offline); the attempt records "
            "vllmModelsDropped as unknown") in messages(caplog)


def test_blocked_runs_are_interrupted_after_the_backstop_is_armed(make, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(lw, "INTERRUPT_BUDGET_S", 0.5)
    release = threading.Event()
    order = []
    s = make(backstop_sleep=lambda seconds: release.wait(10))

    def callback(name, hang=False, fail=False):
        def _cb(error):
            order.append((name, any(t.name == "self-heal-backstop" and t.is_alive()
                                    for t in threading.enumerate()), error))
            if fail:
                raise RuntimeError("db locked")
            if hang:
                release.wait(5)
        return _cb

    # Registered at the start; 15 min later they are outside a gate for over
    # 900 s with a stalled model, so the wedge blocks them.
    s.activity.start_run("execution", "exec-1", models=(MODEL,), workflow="wf-1 v1",
                         on_interrupt=callback("exec-1"), bind=False)
    s.activity.start_run("execution", "exec-2", models=(MODEL,), workflow="wf-2 v1",
                         on_interrupt=callback("exec-2", fail=True), bind=False)
    s.activity.start_run("execution", "exec-3", models=(MODEL,), workflow="wf-3 v1",
                         on_interrupt=callback("exec-3", hang=True), bind=False)
    s.activity.start_run("execution", "exec-4", models=(MODEL,), workflow="wf-4 v1",
                         on_interrupt=callback("exec-4"), bind=False)
    s.ticks(30)
    s.clock.sleep(1)
    s.ticks(1)
    try:
        assert s.killer.calls, "a raising or hanging callback does not stop the signal"
        assert [o[0] for o in order] == ["exec-1", "exec-2", "exec-3"]
        assert all(alive for _n, alive, _e in order), "the backstop is armed before the callbacks"
        assert order[0][2] == ("Interrupted: self-heal attempt 1 restarted the LocalServer backend to "
                               "clear a wedged Triton model load; run the workflow again.")
        assert ("SELF-HEAL marked 1 interrupted workflow run(s) failed: exec-1 (wf-1 v1)"
                in messages(caplog))
        assert ("SELF-HEAL skipped marking 2 interrupted workflow run(s): the 10s budget ran out"
                in messages(caplog)) or any("skipped marking 2" in m for m in messages(caplog))
        attempt = attempts_of(s)[0]
        assert attempt["runsBlocked"] == 4
        assert [r["id"] for r in attempt["runsInterrupted"]] == ["exec-1", "exec-2", "exec-3", "exec-4"]
    finally:
        release.set()


def test_no_backstop_means_exit_instead_of_sigterm(make, monkeypatch):
    s = make()
    monkeypatch.setattr(s.watchdog, "_arm_backstop", lambda: False)
    to_stall(s)
    assert s.killer.calls == [] and s.exiter.calls == [(86,)]


def test_a_raising_kill_exits_at_once(make):
    release = threading.Event()
    s = make(backstop_sleep=lambda seconds: release.wait(10))
    s.killer.side_effect = lambda pid, sig: (_ for _ in ()).throw(OSError("EPERM"))
    try:
        to_stall(s)
        assert s.exiter.calls == [(86,)], "os._exit(86) at once, before the backstop"
    finally:
        release.set()


# ----------------------------------------------- Property 7: random timelines

def test_a_persistent_wedge_gets_three_restarts_in_24h_then_exhausted(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    s = Scenario(tmp_path, lw, ra)
    signals = []
    s.killer.side_effect = lambda pid, sig: signals.append(s.clock.monotonic())

    def run(ticks):
        for _ in range(ticks):
            s.ticks(1)
            if s.watchdog.signalled:
                s.restart()  # a new process: a new watchdog, the same record
                s.killer.side_effect = lambda pid, sig: signals.append(s.clock.monotonic())

    run(2 * 24 * 60)  # 24 h of 30 s ticks
    assert len(signals) == 3
    assert signals[1] - signals[0] >= lw.SELF_HEAL_MIN_INTERVAL_S
    assert signals[2] - signals[1] >= lw.SELF_HEAL_MIN_INTERVAL_S
    assert s.watchdog.view.decision.kind == "exhausted"
    exhausted = [r for r in caplog.records if r.getMessage().startswith("SELF-HEAL EXHAUSTED")]
    assert exhausted and exhausted[0].levelno == logging.ERROR
    assert all(r.levelno == logging.WARNING for r in exhausted[1:]), "then WARNING every 60 min"
    run(60)  # 30 min more: the first attempt ages out of the 24 h window
    assert len(signals) == 4, "self-heal re-arms"
    assert signals[3] - signals[0] >= 86400
    assert len(attempts_of(s)) == 4


EVENTS = st.lists(st.one_of(
    st.tuples(st.just("ticks"), st.integers(1, 120)),
    st.tuples(st.just("restart"), st.just(0)),
    st.tuples(st.just("config"), st.sampled_from(["true", "false", "missing", "garbage", "fail"])),
    st.tuples(st.just("kill_file"), st.booleans()),
    st.tuples(st.just("run"), st.booleans()),
    st.tuples(st.just("vllm"), st.booleans()),
    st.tuples(st.just("gpu"), st.booleans()),
    st.tuples(st.just("wall_step"), st.integers(-7200, 7200)),
    st.tuples(st.just("progress"), st.just(0)),
), min_size=1, max_size=10)


@settings(max_examples=15, deadline=None)
@given(events=EVENTS)
def test_property_7_self_heal_is_bounded_and_conservative(events):
    root = tempfile.mkdtemp(prefix="selfheal-p7-")
    import pathlib
    try:
        state = {"config": dict(ON), "vllm": {}, "kill": False, "run": None, "signals": [],
                 "last_success": "on"}

        def config():
            value = state["config"]
            if isinstance(value, Exception):
                raise value
            return value

        s = Scenario(pathlib.Path(root), lw, ra, config=config, vllm=lambda: state["vllm"],
                     records=os.path.join(root, "records"))
        load = write_gpu_node(s.sys, value="0")
        s.restart()

        def on_signal(pid, sig):
            switch = s.watchdog._switch.state()
            assert switch.enabled and not os.path.exists(os.path.join(s.records, "self-heal.disabled"))
            assert state["last_success"] == "on"
            assert state["run"] is None, "never while an unblocked run is in flight"
            assert not state["vllm"], "never while a vLLM model is loading"
            with open(load) as handle:
                assert handle.read().strip() == "0", "never while the GPU shows load"
            assert attempts_of(s), "the attempt is persisted before the signal"
            state["signals"].append(s.clock.monotonic())

        def install():
            s.killer.side_effect = on_signal

        install()
        for kind, value in events:
            if kind == "ticks":
                for _ in range(value):
                    s.ticks(1)
                    if s.watchdog.signalled:
                        state["run"] = None
                        s.restart()
                        install()
            elif kind == "restart":
                if state["run"] is not None:
                    state["run"] = None
                s.restart()
                install()
            elif kind == "config":
                if value == "fail":
                    state["config"] = RuntimeError("IPC down")
                else:
                    state["config"] = dict(ON) if value == "missing" else dict(ON, ModelLoadSelfHealEnabled=value)
                    state["last_success"] = "on" if value in ("true", "missing") else "off"
            elif kind == "kill_file":
                path = os.path.join(s.records, "self-heal.disabled")
                if value:
                    open(path, "w").close()
                elif os.path.exists(path):
                    os.remove(path)
            elif kind == "run":
                if value and state["run"] is None:
                    state["run"] = s.activity.start_run("execution", "busy", models=("model-b",),
                                                        bind=False)
                elif not value and state["run"] is not None:
                    s.activity.finish_run(state["run"])
                    state["run"] = None
            elif kind == "vllm":
                state["vllm"] = {"llm": ("LOADING", "LOADING")} if value else {}
            elif kind == "gpu":
                with open(load, "w") as handle:
                    handle.write("5" if value else "0")
            elif kind == "wall_step":
                s.clock.wall_offset += value
            elif kind == "progress":
                s.index.set("model-extra-{0}".format(len(s.index.states)), "READY")
        for _ in range(240):  # two more hours of the timeline's final state
            s.ticks(1)
            if s.watchdog.signalled:
                state["run"] = None
                s.restart()
                install()
        # Bounded: at most 3 per 24 h, at least 1800 s apart (by the boot clock).
        signals = state["signals"]
        for a, b in zip(signals, signals[1:]):
            assert b - a >= lw.SELF_HEAL_MIN_INTERVAL_S
        for i, t in enumerate(signals):
            assert len([u for u in signals[i:] if u - t < 86400]) <= 3
        assert len(attempts_of(s)) >= len(signals)
    finally:
        shutil.rmtree(root, ignore_errors=True)
