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
"""Fakes for the watchdog, self-heal and run-activity tests
(triton-model-cleanup-wedge task 4).

Honesty guard: nothing here starts Triton, the backend, Greengrass or a
real process restart. Triton is a fake index reader, ``/proc`` and
``/sys`` are trees under ``tmp_path``, the clocks are fakes that only
``sleep`` advances, the restart signal and ``os._exit`` are recorders, and
the continuous runner is the real ``ContinuousRunner`` driven through
``step()`` on a fake stream and store.
"""
import os
import threading
from typing import Any, Dict, List, Optional

from triton_cleanup_wedge.fakes import FakeTime, write_entry

STUB = "triton_python_backend_stub"
MODEL = "model-a"


class FakeIndex:
    """``index_states()`` and ``list_triton_models()`` over mutable states;
    ``raises`` makes the next reads raise."""

    def __init__(self, states: Optional[Dict[str, Any]] = None, model_dir: Optional[str] = None):
        self.states: Dict[str, Any] = dict(states or {})
        self.raises: Optional[BaseException] = None
        self.reads = 0
        self.model_dir = model_dir

    def set(self, name, state, reason=None):
        self.states[name] = (state, reason)

    def index_states(self):
        self.reads += 1
        if self.raises is not None:
            raise self.raises
        return {n: (v if isinstance(v, tuple) else (v, None)) for n, v in self.states.items()}

    def list_triton_models(self, quiet=False):
        out = []
        for name, value in self.states.items():
            state, reason = value if isinstance(value, tuple) else (value, None)
            record = {"model_component": name, "status": state}
            if reason:
                record["reason"] = reason
            out.append(record)
        return out


def wedged_index(model=MODEL, model_dir=None) -> FakeIndex:
    return FakeIndex({model: ("LOADING", None), "base_" + model: ("LOADING", None),
                      "marshal_" + model: ("READY", None)}, model_dir=model_dir)


class FakeTracker:
    def __init__(self):
        self.requests: Dict[str, Any] = {}
        self.ensured = 0
        self.tracked: List[str] = []

    def requested_at(self, model):
        return self.requests.get(model)

    def inflight(self):
        return list(self.tracked)

    def ensure_tracker_thread(self):
        self.ensured += 1


def write_stub(proc_root: str, pid: int, repo: str, step: str, ticks: int = 300,
               name: str = STUB) -> str:
    """A ``/proc/<pid>`` of a python-backend stub serving ``step``, with
    ``ticks`` clock ticks of CPU (utime + stime)."""
    pid_dir = os.path.join(proc_root, str(pid))
    os.makedirs(pid_dir, exist_ok=True)
    argv = [name, os.path.join(repo, step, "1", "model.py"), "shm-region", "1048576"]
    with open(os.path.join(pid_dir, "cmdline"), "wb") as handle:
        handle.write(b"\0".join(a.encode() for a in argv) + b"\0")
    set_stub_ticks(proc_root, pid, ticks, name=name)
    return pid_dir


def set_stub_ticks(proc_root: str, pid: int, ticks: int, name: str = STUB) -> None:
    utime = ticks // 2
    stime = ticks - utime
    fields = ["S"] + ["0"] * 10 + [str(utime), str(stime)] + ["0"] * 30
    with open(os.path.join(proc_root, str(pid), "stat"), "w") as handle:
        # A process name with spaces and parentheses: parsing must use the last ')'.
        handle.write("{0} ({1} (x) y) {2}\n".format(pid, name, " ".join(fields)))


def write_gpu_node(sys_root: str, of_name: str = "gv11b", value: str = "0",
                   entry: str = "17000000.gv11b") -> str:
    """A ``/sys/class/devfreq/<entry>`` whose ``device`` is a symlink to a
    platform device with ``of_node/name`` and ``load``. Returns the load path."""
    device = os.path.join(sys_root, "sys", "devices", "platform", entry)
    os.makedirs(os.path.join(device, "of_node"), exist_ok=True)
    with open(os.path.join(device, "of_node", "name"), "w") as handle:
        handle.write(of_name + "\x00")
    load = os.path.join(device, "load")
    with open(load, "w") as handle:
        handle.write(value + "\n")
    devfreq = os.path.join(sys_root, "sys", "class", "devfreq", entry)
    os.makedirs(devfreq, exist_ok=True)
    link = os.path.join(devfreq, "device")
    if not os.path.lexists(link):
        os.symlink(device, link)
    return os.path.realpath(load)


class Recorder:
    """A callable that records its calls (killer, exiter, reporter)."""

    def __init__(self, result=None, side_effect=None):
        self.calls: List[tuple] = []
        self.result = result
        self.side_effect = side_effect
        self.lock = threading.Lock()

    def __call__(self, *args):
        with self.lock:
            self.calls.append(args)
        if self.side_effect is not None:
            return self.side_effect(*args)
        return self.result


class Scenario:
    """A wedged ``model-a`` on a fake device, and a watchdog over it."""

    def __init__(self, tmp_path, load_watchdog, run_activity_module, *, kill_file=False,
                 config=None, vllm=None, gpu_value=None, stub_ticks=300, entry_age_s=7200.0,
                 clock=None, records=None, **overrides):
        self.tmp = tmp_path
        self.repo = str(tmp_path / "repo")
        write_entry(self.repo, MODEL, age_s=entry_age_s)
        self.proc = str(tmp_path / "proc")
        os.makedirs(self.proc, exist_ok=True)
        write_stub(self.proc, 4242, self.repo, "base_" + MODEL, ticks=stub_ticks)
        self.sys = str(tmp_path / "sys")
        os.makedirs(self.sys, exist_ok=True)
        if gpu_value is not None:
            write_gpu_node(self.sys, value=str(gpu_value))
        self.records = records or str(tmp_path / "records")
        os.makedirs(self.records, exist_ok=True)
        if kill_file:
            open(os.path.join(self.records, "self-heal.disabled"), "w").close()
        self.clock = clock or FakeTime(start=5000.0)
        self.index = wedged_index(model_dir=self.repo)
        self.tracker = FakeTracker()
        self.config = {"StreamShadowName": "s"} if config is None else config
        self.config_reads = 0
        self.vllm = {} if vllm is None else vllm
        self.killer = Recorder()
        self.exiter = Recorder()
        self.reporter = Recorder(result="unchanged")
        self.activity = run_activity_module.RunActivity(clock=self.clock.monotonic)
        kwargs = dict(
            client_provider=lambda: self.index, repo_has_models=lambda: True,
            clock=self.clock.monotonic, wall=self.clock.wall, sleep=self.clock.sleep,
            proc_root=self.proc, sys_root=self.sys, config_reader=self._read_config,
            vllm_states=self._vllm_states, run_activity=self.activity,
            shadow_reader=lambda thing, name: None, killer=self.killer, exiter=self.exiter,
            records_dir=self.records, repo_dir=self.repo, tracker=self.tracker,
            reporter=self.reporter, size_rejection=lambda: False,
            boot_id=lambda: "boot-1", boottime=lambda: self.clock.monotonic(),
            backstop_sleep=lambda s: None)
        kwargs.update(overrides)
        self.kwargs = kwargs
        self.load_watchdog = load_watchdog
        self.watchdog = load_watchdog.LoadWatchdog(**kwargs)

    def _read_config(self):
        self.config_reads += 1
        value = self.config
        if callable(value):
            return value()
        if isinstance(value, BaseException):
            raise value
        return value

    def _vllm_states(self):
        value = self.vllm
        if callable(value):
            value = value()
        if isinstance(value, BaseException):
            raise value
        return value

    def restart(self):
        """A new process: a new watchdog over the same records directory."""
        self.killer = Recorder()
        self.exiter = Recorder()
        self.activity = type(self.activity)(clock=self.clock.monotonic)
        self.kwargs.update(killer=self.killer, exiter=self.exiter, run_activity=self.activity)
        self.watchdog = self.load_watchdog.LoadWatchdog(**self.kwargs)
        return self.watchdog

    def ticks(self, count: int, step: float = 30.0) -> None:
        for _ in range(count):
            self.watchdog.tick()
            if self.watchdog.signalled:
                return
            self.clock.sleep(step)

    def until_stalled(self, limit: int = 40) -> None:
        for _ in range(limit):
            self.watchdog.tick()
            view = self.watchdog.view
            if view is not None and view.stalled:
                return
            self.clock.sleep(30.0)
        raise AssertionError("the scenario never stalled")


# -------------------------------------------------- a continuous runner on fakes

class _Frame:
    def __init__(self, seq):
        self.seq = seq
        self.acquired_at_ms = 1_790_000_000_000
        self.data = b"\x00" * 24


class FakeStream:
    def __init__(self):
        self.seq = 0

    def health(self, camera_key):
        return {"cameraKey": camera_key, "state": "streaming"}

    def latest_frame(self, camera_key, after_seq=0, max_age_ms=None, wait_ms=0):
        self.seq += 1
        return _Frame(self.seq)


class MemoryStore:
    def __init__(self):
        self.rows: Dict[str, Any] = {}
        self.count = 0

    def insert(self, registration_id, context):
        self.count += 1
        execution_id = "exec-{0}".format(self.count)
        self.rows[execution_id] = (registration_id, dict(context))
        return execution_id

    def status(self, execution_id):
        return "completed"


class StepClock:
    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_runner(clock=None):
    """The real ``ContinuousRunner`` on a fake stream and store."""
    from workflow_engine.continuous_runner import ContinuousRunner
    from workflow_engine.stream_feed import StreamFeed

    clock = clock or StepClock()
    feed = StreamFeed(node_id="cam", protocol="rtsp", camera_key="cfg-7", url="rtsp://camera.example/main",
                      processing_mode="continuous", frames_per_second=1.0, max_frame_age_ms=2000,
                      keep_recent_runs=20, keep_notable_runs=200, camera_source_id="cfg-7")
    store = MemoryStore()
    runner = ContinuousRunner("wf-1:3", feed, execute=lambda execution_id: None,
                              stream_manager=FakeStream(), store=store, clock=clock,
                              wall=lambda: 1_790_000_000.0, models=())
    return runner, store, clock


def step_inserts_row(runner, store, clock) -> bool:
    """Whether one ``step()`` (after the schedule is due) inserts a row."""
    before = store.count
    clock.advance(1.0)
    runner.step()
    return store.count > before
