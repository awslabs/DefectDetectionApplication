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
"""Wedge detection (triton-model-cleanup-wedge task 4.4, design D6,
Property 6): a model is stalled only on positive evidence, never on
``LOADING`` time alone and never during load activity."""
import logging
import os
import threading

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dda_triton import load_watchdog as lw
from dda_triton.repo_entries import EntryInspection
from utils import run_activity as ra
from triton_cleanup_wedge.fakes import write_entry
from triton_cleanup_wedge.watchdog_fakes import (
    MODEL,
    Scenario,
    set_stub_ticks,
    write_gpu_node,
    write_stub,
)

WALL = 1_790_000_000.0


def entry(missing=None, newest=None, newest_path="/repo/model-a/config.pbtxt"):
    return EntryInspection(present=True, blocker=None, missing_path=missing, paths=(),
                           newest_path=newest_path, newest_mtime=newest)


def verdict_of(loading, now=2000.0, progress=0.0, cpu=0.0, covered=True, entries=None,
               pb_stub=None):
    return lw.evaluate_wedge(now, WALL + now, loading, progress, cpu, covered, entries or {},
                             pb_stub or {}, repo="/repo")


def loading_model(since=0.0, stubs=1):
    return lw.LoadingModel(since, WALL + since, stubs)


# ------------------------------------------------------------ evaluate_wedge

def test_e2_alone_stalls_a_candidate():
    v = verdict_of({MODEL: loading_model()}, now=950.0)
    assert v.stalled == (MODEL,)
    assert v.models[MODEL].e2 and not v.models[MODEL].e1
    assert v.models[MODEL].evidence == (
        "[stall] the Triton model index has not changed for 15m50s and the loading models' stub "
        "processes used 0.0 CPU-s in the last 5m (1 stub process(es) alive)",)


def test_loading_time_alone_is_never_enough():
    # LOADING for an hour, but the index changed a minute ago and nothing in
    # the repository moved: no E1, no E2.
    v = verdict_of({MODEL: loading_model()}, now=3600.0, progress=3540.0,
                   entries={MODEL: entry(newest=WALL - 100)})
    assert v.stalled == ()
    assert v.models[MODEL].candidate and not v.models[MODEL].stalled


def test_e1_missing_path():
    v = verdict_of({MODEL: loading_model()}, now=950.0, progress=900.0,
                   entries={MODEL: entry(missing="/repo/base_model-a/1/model.py")})
    assert v.stalled == (MODEL,)
    assert v.models[MODEL].evidence[0] == (
        "[repository] base_model-a/1/model.py was missing at {0}, after the load request at {1}"
        .format(lw.iso(WALL + 950.0), lw.iso(WALL)))


def test_e1_changed_after_the_request():
    v = verdict_of({MODEL: loading_model()}, now=950.0, progress=900.0,
                   entries={MODEL: entry(newest=WALL + 10)})
    assert v.stalled == (MODEL,)
    assert v.models[MODEL].evidence[0] == (
        "[repository] model-a/config.pbtxt changed at {0}, after the load request at {1}"
        .format(lw.iso(WALL + 10), lw.iso(WALL)))
    # One second of slack: a file written in the same second is no evidence.
    v = verdict_of({MODEL: loading_model()}, now=950.0, progress=900.0,
                   entries={MODEL: entry(newest=WALL + 1)})
    assert v.stalled == ()


def test_the_activity_veto_is_device_wide():
    loading = {MODEL: loading_model(), "model-b": loading_model(since=500.0)}
    v = verdict_of(loading, now=1000.0, cpu=5.0)
    assert v.stalled == () and v.veto
    v = verdict_of(loading, now=1000.0, cpu=4.9)
    assert v.stalled == (MODEL,)


def test_an_uncovered_window_means_unknown_activity():
    v = verdict_of({MODEL: loading_model()}, now=2000.0, covered=False)
    assert v.stalled == () and v.veto


def test_the_pb_stub_evidence():
    line = {"line": "Failed to open /repo/base_model-a/1/model.py", "at": "2026-10-14T04:36:32.145678901Z"}
    since = lw.parse_iso("2026-10-14T04:36:00Z")
    loading = {MODEL: lw.LoadingModel(0.0, since, 0)}
    v = lw.evaluate_wedge(950.0, since + 950, loading, 0.0, 0.0, True, {}, {MODEL: line})
    assert v.models[MODEL].evidence[-1] == (
        '[pb_stub] "Failed to open /repo/base_model-a/1/model.py" at 2026-10-14T04:36:32.145678901Z '
        '(captured by the model-a Startup from the backend container log)')
    old = dict(line, at="2026-10-14T04:30:00Z")  # more than 60 s before the request
    v = lw.evaluate_wedge(950.0, since + 950, loading, 0.0, 0.0, True, {}, {MODEL: old})
    assert not any(e.startswith("[pb_stub]") for e in v.models[MODEL].evidence)


def test_culprit_selection():
    loading = {"model-a": loading_model(since=0.0, stubs=0),
               "model-b": loading_model(since=10.0, stubs=1),
               "model-c": loading_model(since=20.0, stubs=0)}
    v = verdict_of(loading, now=2000.0, entries={"model-c": entry(newest=WALL + 100)})
    assert v.culprit == "model-c", "E1 evidence first"
    v = verdict_of(loading, now=2000.0)
    assert v.culprit == "model-b", "then a live stub"
    loading["model-b"] = loading_model(since=10.0, stubs=0)
    v = verdict_of(loading, now=2000.0)
    assert v.culprit == "model-a", "then the earliest loading start"
    assert v.stalled == ("model-a", "model-b", "model-c")


loading_strategy = st.dictionaries(
    st.sampled_from(["model-a", "model-b", "model-c"]),
    st.tuples(st.floats(0, 5000), st.integers(0, 2)), max_size=3)


@settings(max_examples=25, deadline=None)
@given(loading=loading_strategy, now=st.floats(0, 6000), progress=st.floats(0, 6000),
       cpu=st.floats(0, 20), covered=st.booleans(), wall_shift=st.floats(-1e6, 1e6),
       e1_models=st.sets(st.sampled_from(["model-a", "model-b", "model-c"])))
def test_property_6_stalled_needs_evidence_and_no_activity(loading, now, progress, cpu, covered,
                                                          wall_shift, e1_models):
    models = {m: lw.LoadingModel(since, WALL + since, stubs) for m, (since, stubs) in loading.items()}
    entries = {m: entry(missing="/repo/{0}/config.pbtxt".format(m)) for m in e1_models}
    v = lw.evaluate_wedge(now, WALL + now, models, progress, cpu, covered, entries, {})
    for m in v.stalled:
        assert now - models[m].since_mono >= lw.STALL_AFTER_S
        assert covered and cpu < lw.ACTIVITY_CPU_THRESHOLD_S
        assert m in e1_models or now - progress >= lw.STALL_AFTER_S
    if covered and cpu >= lw.ACTIVITY_CPU_THRESHOLD_S:
        assert v.stalled == ()
    # No wall-clock value changes the result except through E1's mtime
    # comparison (here E1 is a missing path, which no clock affects).
    shifted = {m: lw.LoadingModel(i.since_mono, i.since_wall + wall_shift, i.stubs)
               for m, i in models.items()}
    w = lw.evaluate_wedge(now, WALL + now + wall_shift, shifted, progress, cpu, covered, entries, {})
    assert w.stalled == v.stalled


# ------------------------------------------------------------ the watchdog

@pytest.fixture
def scenario(tmp_path, monkeypatch):
    def make(**kwargs):
        s = Scenario(tmp_path, lw, ra, kill_file=kwargs.pop("kill_file", True), **kwargs)
        monkeypatch.setattr(lw, "_WATCHDOG", s.watchdog)
        return s
    return make


def test_a_wedge_is_detected_after_15_minutes(scenario, caplog):
    caplog.set_level(logging.INFO)
    s = scenario()
    s.ticks(30)  # 14m30s
    assert s.watchdog.view.stalled == ()
    assert s.watchdog.view.health[MODEL].state == "loading"
    s.ticks(1)
    assert s.watchdog.view.stalled == (MODEL,)
    health = lw.stall_for(MODEL)
    assert health is not None and health.evidence[0].startswith("[stall]")
    stalled_lines = [r for r in caplog.records if "is stalled:" in r.getMessage()]
    assert len(stalled_lines) == 1 and stalled_lines[0].levelno == logging.WARNING
    assert stalled_lines[0].getMessage().startswith(
        "Triton model model-a is stalled: LOADING for 15m00s with no load progress for 15m00s; "
        "evidence: [stall] ")
    s.ticks(3)
    assert len([r for r in caplog.records if "is stalled:" in r.getMessage()]) == 1, "once per episode"
    s.index.set(MODEL, "READY")
    s.ticks(1)
    assert lw.stall_for(MODEL) is None
    assert "Triton model model-a is no longer stalled (state READY)" in caplog.text


def test_stub_cpu_vetoes_the_stall(scenario):
    s = scenario()
    ticks = 300
    for _ in range(40):
        ticks += 300  # 3 CPU-s per 30 s tick: a TensorRT build, not a wedge
        set_stub_ticks(s.proc, 4242, ticks)
        s.ticks(1)
    assert s.watchdog.view.stalled == ()
    assert s.watchdog.view.health[MODEL].window_cpu_s >= 5.0


def test_index_progress_restarts_the_window(scenario):
    s = scenario()
    s.ticks(20)
    s.index.set("model-other", "READY")  # a new entry appeared: progress
    s.ticks(20)  # 10 min after the progress: no E2 yet
    assert s.watchdog.view.stalled == ()
    s.ticks(12)
    assert s.watchdog.view.stalled == (MODEL,)


def test_a_model_turning_loading_is_not_progress(scenario):
    s = scenario()
    s.index.set("model-b", "UNAVAILABLE", "load failed")
    s.ticks(5)
    s.index.set("model-b", "LOADING")  # a ModelGate retry behind the wedge
    s.ticks(27)
    assert MODEL in s.watchdog.view.stalled


def test_a_wall_clock_jump_never_stalls(scenario):
    s = scenario()
    s.ticks(10)
    s.clock.wall_offset += 6 * 3600  # an NTP step at boot
    s.ticks(10)  # 10 minutes of LOADING in total
    assert s.watchdog.view.stalled == ()
    assert s.watchdog.view.health[MODEL].loading_for_s(s.clock.monotonic()) < lw.STALL_AFTER_S


def test_e1_on_the_device(scenario):
    s = scenario()
    s.ticks(20)
    s.index.set("model-other", "READY")  # keep E2 away
    s.ticks(11)
    os.remove(os.path.join(s.repo, "base_" + MODEL, "1", "model.py"))
    s.ticks(2)
    assert s.watchdog.view.stalled == (MODEL,)
    evidence = s.watchdog.view.health[MODEL].evidence
    assert evidence[0].startswith("[repository] base_model-a/1/model.py was missing at ")


def test_since_ignores_a_request_of_an_earlier_episode(scenario):
    s = scenario()
    t0 = s.clock.monotonic()
    s.tracker.requests[MODEL] = (s.clock.wall() - 7200, t0 - 7200)  # a request 2 h ago
    s.index.set(MODEL, "READY")
    s.ticks(2)
    s.index.set(MODEL, "LOADING")  # LOADING first seen now
    first_seen_wall = s.clock.wall()
    s.ticks(1)
    health = s.watchdog.view.health[MODEL]
    assert health.loading_since_wall == pytest.approx(first_seen_wall)
    s.ticks(28)
    assert s.watchdog.view.stalled == ()
    s.ticks(3)
    assert s.watchdog.view.stalled == (MODEL,)


def test_since_dates_an_episode_from_a_newer_request(scenario):
    s = scenario()
    s.index.set(MODEL, "UNKNOWN")
    s.ticks(1)
    request_mono = s.clock.monotonic() + 1
    s.tracker.requests[MODEL] = (s.clock.wall() + 1, request_mono)
    s.clock.sleep(5)
    s.index.set(MODEL, "LOADING")
    s.ticks(1)
    assert s.watchdog.view.health[MODEL].loading_since_mono == request_mono


# --------------------------------------------------------------- samplers

def test_the_proc_sampler(tmp_path):
    proc = str(tmp_path / "proc")
    repo = "/aws_dda/dda_triton/triton_model_repo"
    write_stub(proc, 10, repo, "base_model-a", ticks=250)
    write_stub(proc, 11, repo, "marshal_model-a", ticks=50)
    write_stub(proc, 12, "/elsewhere", "base_model-a", ticks=999)          # not this repository
    write_stub(proc, 13, repo, "base_model-b", ticks=999, name="python3")   # not a stub
    os.makedirs(os.path.join(proc, "14"))                                    # vanished pid
    os.makedirs(os.path.join(proc, "self"))
    write_stub(proc, 15, repo, "model-c", ticks=10)
    with open(os.path.join(proc, "15", "stat"), "w") as handle:
        handle.write("15 (broken")                                          # unparsable
    ticks = float(os.sysconf("SC_CLK_TCK"))
    sample = lw.sample_stubs(proc, repo)
    assert sample == {10: ("model-a", 250 / ticks), 11: ("model-a", 50 / ticks)}
    with pytest.raises(OSError):
        lw.sample_stubs(str(tmp_path / "no-proc"), repo)


def test_window_cpu():
    samples = [(0.0, {1: ("model-a", 1.0)}), (30.0, {1: ("model-a", 1.5), 2: ("model-b", 9.0)}),
               (60.0, {1: ("model-a", 1.2), 3: ("model-a", 0.5)})]
    assert lw.window_cpu(samples, ["model-a"]) == pytest.approx(0.5 + 0.0 + 0.5)
    assert lw.window_cpu(samples, ["model-b"]) == pytest.approx(9.0)


@pytest.mark.parametrize("of_name", ["gv11b", "ga10b", "gb10b"])
def test_the_gpu_node_is_found(tmp_path, of_name):
    load = write_gpu_node(str(tmp_path), of_name=of_name, value="123")
    assert lw.find_gpu_load_node(str(tmp_path)) == load
    assert lw.read_gpu_burst(load, sleep=lambda s: None) == 123


def test_no_gpu_node_means_unavailable(tmp_path):
    assert lw.find_gpu_load_node(str(tmp_path)) is None
    write_gpu_node(str(tmp_path), of_name="some-other-device")
    assert lw.find_gpu_load_node(str(tmp_path)) is None
    assert lw.read_gpu_burst(None) is None


def test_gpu_reads_skip_failures_and_keep_the_maximum(tmp_path):
    load = write_gpu_node(str(tmp_path), value="0")
    values = iter(["17", "garbage", "1001", "250", "3"])

    def sleep(_seconds):
        with open(load, "w") as handle:
            handle.write(next(values))

    assert lw.read_gpu_burst(load, sleep=sleep) == 250
    os.remove(load)
    assert lw.read_gpu_burst(load, sleep=lambda s: None) is None


def test_gpu_values_never_change_the_verdict(scenario):
    s = scenario(gpu_value=900)
    s.ticks(31)
    assert s.watchdog.view.stalled == (MODEL,)
    health = s.watchdog.view.health[MODEL]
    assert health.gpu_max_permille == 900
    assert s.watchdog.gpu_node is not None


# ------------------------------------------------ containment and the view

def test_a_raising_tick_logs_once_and_the_next_tick_runs(scenario, caplog):
    caplog.set_level(logging.DEBUG)
    s = scenario()
    calls = {"n": 0}
    original = s.watchdog._observe

    def flaky(index):
        calls["n"] += 1
        if calls["n"] in (2, 3):
            raise RuntimeError("tick fault")
        return original(index)

    s.watchdog._observe = flaky
    for _ in range(4):
        s.watchdog.contained_tick()
        s.clock.sleep(30)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR
              and "watchdog tick failed" in r.getMessage()]
    assert len(errors) == 1
    assert errors[0].getMessage() == ("Triton model-load watchdog tick failed (tick fault); detection "
                                      "and self-heal resume at the next tick in 30s")
    assert errors[0].exc_info is not None
    assert calls["n"] == 4


def test_a_stale_view_counts_as_no_watchdog(scenario):
    s = scenario(kill_file=False)
    s.activity.start_run("execution", "busy", bind=False)  # keeps self-heal deferred
    s.ticks(32)
    assert lw.stall_for(MODEL) is not None
    snapshot = lw.build_model_status_snapshot({MODEL: "LOADING"})
    assert snapshot["selfHeal"]["enabled"] is True
    s.clock.sleep(lw.WATCHDOG_STALE_S + 1)  # no tick completed for 301 s
    assert lw.stall_for(MODEL) is None
    snapshot = lw.build_model_status_snapshot({MODEL: "LOADING"})
    assert snapshot["selfHeal"]["enabled"] is False
    assert snapshot["selfHeal"]["disabledBy"] == "watchdog_not_running"


def test_a_failed_index_read_skips_the_tick_with_one_warning(scenario, caplog):
    caplog.set_level(logging.WARNING)
    s = scenario()
    s.ticks(2)
    s.index.raises = RuntimeError("Triton returned an empty model index after 3 attempts")
    s.ticks(3)
    lines = [r for r in caplog.records if "could not read the Triton model index" in r.getMessage()]
    assert len(lines) == 1
    assert s.clock.monotonic() - s.watchdog.view.tick_mono <= 30, "a skipped tick still completes"
    s.index.raises = None
    s.ticks(1)
    assert s.watchdog.view.health[MODEL].state == "loading"


def test_the_empty_repository_stops_the_tick(scenario):
    s = scenario()
    s.ticks(5)
    s.watchdog._repo_has_models = lambda: False
    reads = s.index.reads
    s.ticks(3)
    assert s.index.reads == reads, "no Triton call for an empty repository"
    assert s.watchdog.view.health == {}


def test_readers_only_ever_see_a_complete_view(scenario):
    s = scenario(kill_file=True)
    stop = threading.Event()
    problems = []

    def reader():
        while not stop.is_set():
            view = s.watchdog.view
            if view is None:
                continue
            for health in view.health.values():
                if health.tick_mono != view.tick_mono:
                    problems.append("mixed ticks")
            lw.stall_for(MODEL)
            lw.build_model_status_snapshot({MODEL: "LOADING"})

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        s.ticks(40)
    finally:
        stop.set()
        for t in threads:
            t.join(10)
    assert problems == []


def test_module_import_has_no_side_effects():
    import subprocess
    import sys

    code = ("import sys, threading; import dda_triton.load_watchdog as m; "
            "assert 'panorama' not in sys.modules; "
            "assert m.current() is None; "
            "assert not [t for t in threading.enumerate() if t.name.startswith('triton-')]")
    env = dict(os.environ)
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    env["PYTHONPATH"] = os.pathsep.join([os.path.join(root, "src", "backend"),
                                         os.path.join(root, "test", "backend-test")])
    subprocess.check_call([sys.executable, "-c", code], env=env)


def test_fmt_dur():
    assert lw.fmt_dur(4032) == "1h07m12s"
    assert lw.fmt_dur(902) == "15m02s"
    assert lw.fmt_dur(42) == "42s"
    assert lw.fmt_dur(-3) == "0s"
