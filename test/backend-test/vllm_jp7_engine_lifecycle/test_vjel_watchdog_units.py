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
"""Unit tests for ``vllm_runtime.construction_watchdog`` (spec
vllm-jp7-engine-lifecycle, design change 2), driven entirely through its
injectable seams: a step clock (each sample advances it by a fixed amount),
scripted thread progress, a fake ``/proc`` table and a recording killer. No
process is started or signalled."""
import threading
import time

import pytest

from vllm_runtime import construction_watchdog as cw
from vllm_runtime.construction_watchdog import (
    CLK_TCK,
    ENGINE_CONSTRUCTION_TIMEOUT_MARKER,
    TRIGGER_BOUND,
    TRIGGER_STALL,
    ConstructionWatchdog,
    ProcInfo,
    descendants,
    select_engine_cores,
)

OWN_PID = 1
#: Construction start: 1000 s after boot.
UPTIME_S = 1000.0
T0_TICKS = int(UPTIME_S * CLK_TCK)


class StepClock:
    """``clock()`` advances by ``step`` on every call after the first."""

    def __init__(self, step):
        self.step = step
        self.calls = 0

    def __call__(self):
        value = self.calls * self.step
        self.calls += 1
        return value


class Progress:
    """Scripted ``thread_progress``: cumulative CPU/I-O grow by fixed
    increments per sample."""

    def __init__(self, cpu_step=0.0, io_step=0.0):
        self.cpu = 0.0
        self.io = 0.0
        self.cpu_step = cpu_step
        self.io_step = io_step

    def __call__(self, _tid):
        self.cpu += self.cpu_step
        self.io += self.io_step
        return self.cpu, self.io


def proc(pid, ppid=OWN_PID, cmdline="", comm="", cpu=0.0, start_ticks=T0_TICKS + 10,
         state="S"):
    return ProcInfo(pid=pid, ppid=ppid, state=state, cpu_seconds=cpu, io_bytes=0,
                    start_ticks=start_ticks, cmdline=cmdline, comm=comm)


class Killer:
    def __init__(self, on_kill=None):
        self.pids = []
        self.on_kill = on_kill

    def __call__(self, pid):
        self.pids.append(pid)
        if self.on_kill is not None:
            self.on_kill(pid)


def make(tmp_path=None, bound_s=600.0, stall_window_s=120.0, grace_s=0.05,
         step=5.0, progress=None, table=None, killer=None, on_unblock_failed=None,
         io_reader=None, engine_core_dumper=None, proc_dumper=None, keep=5):
    return ConstructionWatchdog(
        "qwen3-vl-8b-instruct",
        bound_s=bound_s, stall_window_s=stall_window_s, grace_s=grace_s,
        on_unblock_failed=on_unblock_failed,
        diagnostics_dir=str(tmp_path) if tmp_path is not None else None,
        clock=StepClock(step),
        process_table=table or (lambda: {}),
        uptime_reader=lambda: UPTIME_S,
        io_reader=io_reader or (lambda pid: 0),
        thread_progress=progress or Progress(),
        killer=killer or Killer(),
        engine_core_dumper=engine_core_dumper or (lambda pid: "py-spy says {}".format(pid)),
        proc_dumper=proc_dumper or (lambda pid: "proc state of {}".format(pid)),
        sample_period_s=0.005,
        keep_diagnostics=keep,
        own_pid=OWN_PID,
    )


def wait_fired(watchdog, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not watchdog.fired and time.monotonic() < deadline:
        time.sleep(0.005)
    return watchdog.fired


# --- arming and disarming ---------------------------------------------------

def test_cancel_before_firing_is_a_clean_finish(tmp_path):
    killer = Killer()
    watchdog = make(tmp_path, bound_s=10_000, stall_window_s=10_000, step=0.0,
                    killer=killer).start()
    time.sleep(0.05)
    assert watchdog.cancel() is True
    watchdog.join(2)
    assert not watchdog.fired
    assert watchdog.reason is None
    assert killer.pids == []
    assert list(tmp_path.iterdir()) == []


def test_disabled_watchdog_starts_no_thread():
    watchdog = make(bound_s=0, stall_window_s=0).start()
    assert not watchdog.enabled
    assert watchdog._thread is None
    assert watchdog.cancel() is True


def test_cancel_after_firing_reports_the_timeout(tmp_path):
    watchdog = make(tmp_path, stall_window_s=10, step=5.0, grace_s=5.0).start()
    assert wait_fired(watchdog)
    assert watchdog.cancel() is False
    watchdog.join(5)


# --- triggers ---------------------------------------------------------------

def test_stall_fires_after_the_window_without_progress(tmp_path):
    failed = []
    watchdog = make(tmp_path, stall_window_s=120, step=5.0,
                    on_unblock_failed=failed.append).start()
    watchdog.join(5)
    assert watchdog.trigger == TRIGGER_STALL
    reason = watchdog.reason
    assert reason.startswith(ENGINE_CONSTRUCTION_TIMEOUT_MARKER)
    assert "qwen3-vl-8b-instruct" in reason
    assert "made no progress for 120 s" in reason
    assert "stopped 0 engine core process(es)" in reason
    # Nothing returned within the grace: Decision 3 gets the same reason.
    assert failed == [reason]


def test_cpu_progress_is_not_a_stall_and_the_bound_still_applies(tmp_path):
    watchdog = make(tmp_path, bound_s=300, stall_window_s=120, step=5.0,
                    progress=Progress(cpu_step=2.0)).start()
    watchdog.join(5)
    assert watchdog.trigger == TRIGGER_BOUND
    assert "exceeded the 300 s Construction_Bound" in watchdog.reason


def test_io_progress_is_not_a_stall(tmp_path):
    watchdog = make(tmp_path, bound_s=300, stall_window_s=120, step=5.0,
                    progress=Progress(io_step=2 * 1024 * 1024)).start()
    watchdog.join(5)
    assert watchdog.trigger == TRIGGER_BOUND


def test_tiny_progress_below_both_thresholds_is_a_stall(tmp_path):
    # 0.01 CPU-s and 1 KiB per 5 s sample: 0.24 CPU-s and 24 KiB per window.
    watchdog = make(tmp_path, bound_s=300, stall_window_s=120, step=5.0,
                    progress=Progress(cpu_step=0.01, io_step=1024)).start()
    watchdog.join(5)
    assert watchdog.trigger == TRIGGER_STALL


def test_engine_core_cpu_counts_as_progress(tmp_path):
    state = {"cpu": 0.0}

    def table():
        state["cpu"] += 3.0
        return {10: proc(10, cmdline="VLLM::EngineCore", cpu=state["cpu"])}

    watchdog = make(tmp_path, bound_s=200, stall_window_s=60, step=5.0,
                    table=table).start()
    watchdog.join(5)
    assert watchdog.trigger == TRIGGER_BOUND


def test_cpu_of_processes_outside_the_construction_does_not_count(tmp_path):
    state = {"cpu": 0.0}

    def table():
        state["cpu"] += 3.0
        return {
            # Another model's engine core, started before this construction.
            20: proc(20, cmdline="VLLM::EngineCore", cpu=state["cpu"],
                     start_ticks=T0_TICKS - 100 * CLK_TCK),
            # A busy non-engine child (a Triton stub, a stream worker).
            21: proc(21, cmdline="python3 stream_worker.py", cpu=state["cpu"]),
            # A busy unrelated process.
            22: proc(22, ppid=999, cmdline="VLLM::EngineCore", cpu=state["cpu"]),
        }

    watchdog = make(tmp_path, bound_s=600, stall_window_s=60, step=5.0,
                    table=table).start()
    watchdog.join(5)
    assert watchdog.trigger == TRIGGER_STALL


# --- what it stops ------------------------------------------------------------

def test_kills_only_this_constructions_engine_core_tree(tmp_path):
    killer = Killer()
    rows = {
        10: proc(10, cmdline="VLLM::EngineCore"),                 # selected
        11: proc(11, ppid=10, cmdline="compile_worker"),          # its child
        12: proc(12, ppid=11, cmdline="ptxas"),                   # grandchild
        20: proc(20, cmdline="VLLM::EngineCore",
                 start_ticks=T0_TICKS - 100 * CLK_TCK),           # older core
        21: proc(21, cmdline="triton_python_backend_stub"),       # other child
        22: proc(22, ppid=999, cmdline="VLLM::EngineCore"),       # not ours
        30: proc(30, comm="VLLM::EngineCor"),                     # comm match
    }
    watchdog = make(tmp_path, stall_window_s=60, step=5.0, killer=killer,
                    table=lambda: dict(rows)).start()
    watchdog.join(5)
    assert sorted(killer.pids) == [10, 11, 12, 30]
    # Descendants before their engine core.
    assert killer.pids.index(12) < killer.pids.index(10)
    assert killer.pids.index(11) < killer.pids.index(10)
    assert "stopped 2 engine core process(es)" in watchdog.reason
    assert watchdog.killed_pids == tuple(killer.pids)


def test_a_core_that_already_exited_is_not_an_error(tmp_path):
    def gone(pid):
        raise ProcessLookupError(pid)

    watchdog = make(tmp_path, stall_window_s=60, step=5.0, killer=gone,
                    table=lambda: {10: proc(10, cmdline="VLLM::EngineCore")}).start()
    watchdog.join(5)
    assert watchdog.fired
    assert watchdog.killed_pids == ()


# --- the unblock grace ----------------------------------------------------------

def test_return_within_the_grace_skips_decision_3(tmp_path):
    failed = []
    holder = {}

    def killer(pid):
        # The construction notices its dead engine core and returns.
        holder["watchdog"].cancel()

    watchdog = make(tmp_path, stall_window_s=60, step=5.0, grace_s=5.0,
                    killer=killer, on_unblock_failed=failed.append,
                    table=lambda: {10: proc(10, cmdline="VLLM::EngineCore")})
    holder["watchdog"] = watchdog
    started = time.monotonic()
    watchdog.start()
    watchdog.join(10)
    assert watchdog.fired
    assert failed == []
    assert time.monotonic() - started < 4.0, "waited the whole grace"


def test_no_return_within_the_grace_calls_decision_3_once(tmp_path):
    failed = []
    watchdog = make(tmp_path, stall_window_s=60, step=5.0, grace_s=0.05,
                    on_unblock_failed=failed.append,
                    table=lambda: {10: proc(10, cmdline="VLLM::EngineCore")}).start()
    watchdog.join(5)
    assert failed == [watchdog.reason]


# --- diagnostics ----------------------------------------------------------------

def test_diagnostics_file_contents(tmp_path):
    watchdog = make(tmp_path, stall_window_s=60, step=5.0,
                    table=lambda: {10: proc(10, cmdline="VLLM::EngineCore")}).start()
    watchdog.join(5)
    files = sorted(tmp_path.glob("vllm-construction-timeout-*.txt"))
    assert len(files) == 1
    assert watchdog.diagnostics_path == str(files[0])
    assert str(files[0]) in watchdog.reason
    text = files[0].read_text()
    assert "model: qwen3-vl-8b-instruct" in text
    assert "trigger: stall" in text
    assert "== backend Python stacks (faulthandler, all threads) ==" in text
    # faulthandler really dumped this process's threads.
    assert "Thread 0x" in text or "Current thread 0x" in text
    assert "== engine core 10: py-spy dump ==" in text
    assert "py-spy says 10" in text
    assert "== engine core 10: /proc ==" in text
    assert "proc state of 10" in text
    assert "progress samples" in text


def test_diagnostics_fall_back_when_py_spy_is_missing(tmp_path):
    watchdog = make(tmp_path, stall_window_s=60, step=5.0,
                    engine_core_dumper=lambda pid: None,
                    table=lambda: {10: proc(10, cmdline="VLLM::EngineCore")}).start()
    watchdog.join(5)
    text = sorted(tmp_path.glob("vllm-construction-timeout-*.txt"))[0].read_text()
    assert "py-spy not available" in text
    assert "proc state of 10" in text


def test_diagnostics_keep_at_most_five_files(tmp_path):
    for index in range(6):
        old = tmp_path / "vllm-construction-timeout-old-{}.txt".format(index)
        old.write_text("old")
        stamp = time.time() - 1000 + index
        import os
        os.utime(old, (stamp, stamp))
    watchdog = make(tmp_path, stall_window_s=60, step=5.0).start()
    watchdog.join(5)
    files = sorted(tmp_path.glob("vllm-construction-timeout-*.txt"))
    assert len(files) == 5
    assert watchdog.diagnostics_path in [str(f) for f in files]
    names = {f.name for f in files}
    assert "vllm-construction-timeout-old-0.txt" not in names
    assert "vllm-construction-timeout-old-1.txt" not in names


def test_no_diagnostics_dir_still_fires(tmp_path):
    watchdog = make(None, stall_window_s=60, step=5.0).start()
    watchdog.join(5)
    assert watchdog.fired
    assert watchdog.diagnostics_path is None
    assert "diagnostics: none written" in watchdog.reason


def test_a_failing_dumper_does_not_stop_the_recovery(tmp_path):
    def broken(_pid):
        raise RuntimeError("py-spy exploded")

    killer = Killer()
    watchdog = make(tmp_path, stall_window_s=60, step=5.0, killer=killer,
                    engine_core_dumper=broken,
                    table=lambda: {10: proc(10, cmdline="VLLM::EngineCore")}).start()
    watchdog.join(5)
    assert killer.pids == [10]
    text = sorted(tmp_path.glob("vllm-construction-timeout-*.txt"))[0].read_text()
    assert "unavailable: py-spy exploded" in text


# --- /proc helpers ----------------------------------------------------------------

def test_parse_stat_handles_spaces_and_parentheses_in_comm():
    line = ("1234 (VLLM::Engine (x) y) S 1 1234 1234 0 -1 4194560 100 0 0 0 "
            "250 50 30 20 20 0 40 0 99999 0 0")
    state, ppid, own, children, start_ticks, comm = cw._parse_stat(line)
    assert (state, ppid, start_ticks, comm) == ("S", 1, 99999, "VLLM::Engine (x) y")
    assert own == pytest.approx(300.0 / CLK_TCK)
    assert children == pytest.approx(50.0 / CLK_TCK)


def test_read_process_table_sees_this_process_and_its_thread_progress():
    import os
    table = cw.read_process_table(own_pid=os.getppid())
    me = table[os.getpid()]
    assert me.ppid == os.getppid()
    assert me.cpu_seconds >= 0
    cpu, io = cw.read_thread_progress(threading.get_native_id())
    assert cpu >= 0 and io >= 0
    assert cw.read_uptime_seconds() > 0


def test_descendants_walks_the_whole_tree():
    rows = {p.pid: p for p in [proc(10), proc(11, ppid=10), proc(12, ppid=11),
                                proc(13, ppid=10), proc(14, ppid=99)]}
    assert sorted(descendants(rows, 10)) == [11, 12, 13]
    assert descendants(rows, 14) == []


def test_select_engine_cores_uses_boot_ticks_with_a_tolerance():
    rows = {
        1: proc(1, cmdline="VLLM::EngineCore", start_ticks=T0_TICKS - int(0.5 * CLK_TCK)),
        2: proc(2, cmdline="VLLM::EngineCore", start_ticks=T0_TICKS - int(2 * CLK_TCK)),
    }
    assert [p.pid for p in select_engine_cores(rows, OWN_PID, T0_TICKS)] == [1]
