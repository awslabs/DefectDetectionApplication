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
"""``ConstructionWatchdog`` — bounds one vLLM engine construction (spec
vllm-jp7-engine-lifecycle, Defect B; bugfix.md 2.3, 2.4).

vLLM's ``wait_for_engine_startup`` waits for the engine core's handshake
with no overall deadline, and the construction blocks the runtime server's
event loop. On jetson-thor1 (2026-10-01) one construction never returned:
the model stayed LOADING, every load and unload request queued behind it,
and after 1 h 45 min the model component went BROKEN and the deployment
rolled back. The cause is unknown (design: H1 a live but stuck engine core,
H2 the backend stuck before the fork, H3 a lost handshake).

The watchdog runs on its own daemon thread next to one construction and
fires on whichever comes first:

- **stall**: over the stall window, the constructing thread plus the engine
  core process tree used under :data:`ENGINE_STALL_CPU_SECONDS` of CPU and
  did under :data:`ENGINE_STALL_IO_BYTES` of I/O. A legitimate construction
  keeps burning CPU (weights, torch.compile, CUDA graph capture, profiling);
  H1, H2 and H3 all sit still.
- **bound**: the hard Construction_Bound, for a construction that keeps
  busy without finishing.

When it fires it:

1. writes one diagnostics file (Python stacks of every backend thread via
   ``faulthandler``, ``py-spy dump`` of each selected engine core when py-spy
   is installed, and ``/proc`` thread states and wait channels), keeping at
   most :data:`ENGINE_DIAGNOSTICS_KEEP` such files;
2. sets the failure reason (it carries :data:`ENGINE_CONSTRUCTION_TIMEOUT_MARKER`);
3. SIGKILLs the engine core processes this construction created, with their
   descendants (Property 5: only children of the backend whose cmdline names
   ``EngineCore``, that were not there when the construction started, and
   that were created after it started). vLLM then sees the dead process at
   once and the construction raises: an ordinary FAILED load that the
   caller may retry, with the runtime server serving again;
4. waits the Unblock_Grace for the construction to return, and when it does
   not (H2, or no engine core to stop) calls ``on_unblock_failed`` with the
   reason (Decision 3, owned by the manager).

Everything that touches the system (clock, ``/proc``, kill, stacks, py-spy)
is injectable, so the host tests drive it without processes or a GPU. Only
the standard library is used; ``/proc`` is read directly (psutil is not a
dependency of every image).
"""
import faulthandler
import logging
import os
import re
import shutil
import signal
import subprocess  # nosec B404 - fixed argv list (py-spy), no shell
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple

from vllm_runtime.constants import (
    ENGINE_DIAGNOSTICS_KEEP,
    ENGINE_STALL_CPU_SECONDS,
    ENGINE_STALL_IO_BYTES,
    ENGINE_WATCHDOG_SAMPLE_PERIOD_S,
)

logger = logging.getLogger(__name__)

#: Category token of a watchdog failure reason. The manager classifies it,
#: and keeps it out of the offline-mode retry (a timed-out construction is
#: never repeated in the same load).
ENGINE_CONSTRUCTION_TIMEOUT_MARKER = "engine-construction-timeout:"

#: Substrings that identify a vLLM engine core process. vLLM sets the
#: process title to ``VLLM::EngineCore`` (``VLLM::EngineCore_DP<n>`` with data
#: parallelism), which shows in ``cmdline``; ``comm`` holds its first 15
#: characters (``VLLM::EngineCor``).
ENGINE_CORE_CMDLINE_TOKEN = "EngineCore"
ENGINE_CORE_COMM_TOKEN = "EngineCor"

#: Triggers.
TRIGGER_STALL = "stall"
TRIGGER_BOUND = "bound"

#: Seconds an engine core may predate the construction start and still be
#: selected. Start times are compared in clock ticks since boot (immune to
#: wall-clock steps); the tolerance absorbs tick rounding.
CREATE_TIME_TOLERANCE_S = 1.0

#: Timeout of one ``py-spy dump``.
PY_SPY_TIMEOUT_S = 20.0

#: Diagnostics filename pattern (``vllm-construction-timeout-<model>-<UTC>.txt``).
DIAGNOSTICS_PREFIX = "vllm-construction-timeout-"

try:
    CLK_TCK = os.sysconf("SCCLK_TCK")
except (AttributeError, ValueError, OSError):  # pragma: no cover - non-POSIX
    CLK_TCK = 100


@dataclass(frozen=True)
class ProcInfo:
    """One process, as read from ``/proc``. ``cpu_seconds`` is user + system
    time including reaped children; ``io_bytes`` is ``rchar + wchar``;
    ``start_ticks`` is the start time in clock ticks since boot."""

    pid: int
    ppid: int
    state: str
    cpu_seconds: float
    io_bytes: int
    start_ticks: int
    cmdline: str
    comm: str = ""

    @property
    def is_engine_core(self) -> bool:
        return (ENGINE_CORE_CMDLINE_TOKEN in self.cmdline
                or ENGINE_CORE_COMM_TOKEN in self.comm)


# --- /proc readers ---------------------------------------------------------

def read_uptime_seconds(proc_root: str = "/proc") -> float:
    """Seconds since boot (``/proc/uptime``), the clock process start times
    are measured on; 0.0 when unreadable (every process then counts as new
    enough, which only widens the selection to all engine cores)."""
    try:
        with open(os.path.join(proc_root, "uptime"), encoding="ascii") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _parse_stat(text: str) -> Optional[Tuple[str, int, float, float, int, str]]:
    """``(state, ppid, own_cpu_seconds, reaped_children_cpu_seconds,
    start_ticks, comm)`` from a ``stat`` line. The comm field may hold
    spaces and parentheses, so split after the LAST ``)``.

    For a THREAD's stat (``/proc/<pid>/task/<tid>/stat``) the children
    fields are the whole process's, so a thread's progress must use the own
    time only."""
    try:
        close = text.rindex(")")
        comm = text[text.index("(") + 1:close]
        rest = text[close + 2:].split()
        state = rest[0]
        ppid = int(rest[1])
        utime, stime, cutime, cstime = (int(v) for v in rest[11:15])
        start_ticks = int(rest[19])
    except (ValueError, IndexError):
        return None
    own = float(utime + stime) / CLK_TCK
    children = float(max(cutime, 0) + max(cstime, 0)) / CLK_TCK
    return state, ppid, own, children, start_ticks, comm


def _read_text(path: str, limit: int = 65536) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def _io_bytes(path: str) -> int:
    text = _read_text(path)
    if not text:
        return 0
    total = 0
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key in ("rchar", "wchar"):
            try:
                total += int(value.strip())
            except ValueError:
                pass
    return total


def read_process_table(own_pid: Optional[int] = None,
                       proc_root: str = "/proc") -> Dict[int, ProcInfo]:
    """Every process visible in ``proc_root``. ``cmdline`` is read only for
    the direct children of ``own_pid`` (the only engine-core candidates);
    ``io_bytes`` is filled in later, only for the processes being measured."""
    own = os.getpid() if own_pid is None else own_pid
    table: Dict[int, ProcInfo] = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return table
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        parsed = _parse_stat(_read_text(os.path.join(proc_root, entry, "stat")) or "")
        if parsed is None:
            continue
        state, ppid, own_cpu, children_cpu, start_ticks, comm = parsed
        # A process's own time plus its reaped children's (the engine core's
        # ptxas and compile-worker subprocesses count as its progress).
        cpu = own_cpu + children_cpu
        cmdline = ""
        if ppid == own:
            raw = _read_text(os.path.join(proc_root, entry, "cmdline")) or ""
            cmdline = raw.replace("\x00", " ").strip()
        table[pid] = ProcInfo(
            pid=pid, ppid=ppid, state=state, cpu_seconds=cpu, io_bytes=0,
            start_ticks=start_ticks, cmdline=cmdline, comm=comm,
        )
    return table


def read_io_bytes(pid: int, proc_root: str = "/proc") -> int:
    """``rchar + wchar`` of one process (0 when unreadable)."""
    return _io_bytes(os.path.join(proc_root, str(pid), "io"))


def read_thread_progress(tid: int, proc_root: str = "/proc") -> Tuple[float, int]:
    """``(cpu_seconds, io_bytes)`` of one thread of THIS process."""
    base = os.path.join(proc_root, "self", "task", str(tid))
    parsed = _parse_stat(_read_text(os.path.join(base, "stat")) or "")
    # Own time only: a thread's children fields are the whole backend's.
    cpu = parsed[2] if parsed is not None else 0.0
    return cpu, _io_bytes(os.path.join(base, "io"))


def descendants(table: Dict[int, ProcInfo], pid: int) -> List[int]:
    """Every descendant pid of ``pid`` in ``table`` (breadth first)."""
    children: Dict[int, List[int]] = {}
    for info in table.values():
        children.setdefault(info.ppid, []).append(info.pid)
    found: List[int] = []
    queue = list(children.get(pid, ()))
    seen = {pid}
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        found.append(current)
        queue.extend(children.get(current, ()))
    return found


def select_engine_cores(table: Dict[int, ProcInfo], own_pid: int,
                        started_ticks: float) -> List[ProcInfo]:
    """The engine cores one construction created (design Property 5): direct
    children of ``own_pid`` whose cmdline names ``EngineCore`` (or whose
    comm holds its truncation) and that started no earlier than ``started_ticks`` (the construction start, in
    clock ticks since boot) minus :data:`CREATE_TIME_TOLERANCE_S`. Another
    model's engine core predates the construction (constructions are
    serialized on the runtime server's loop) and is never selected."""
    floor = started_ticks - CREATE_TIME_TOLERANCE_S * CLK_TCK
    return sorted(
        (info for info in table.values()
         if info.ppid == own_pid
         and info.is_engine_core
         and info.start_ticks >= floor),
        key=lambda info: info.pid,
    )


def proc_fallback_dump(pid: int, proc_root: str = "/proc") -> str:
    """Thread states, wait channels and kernel stacks of one process, for
    when py-spy is missing or fails."""
    lines: List[str] = []
    status = _read_text(os.path.join(proc_root, str(pid), "status")) or ""
    lines.extend(status.splitlines()[:12])
    task_dir = os.path.join(proc_root, str(pid), "task")
    try:
        tids = sorted(int(t) for t in os.listdir(task_dir) if t.isdigit())
    except OSError:
        tids = []
    for tid in tids:
        base = os.path.join(task_dir, str(tid))
        comm = (_read_text(os.path.join(base, "comm")) or "?").strip()
        parsed = _parse_stat(_read_text(os.path.join(base, "stat")) or "")
        state = parsed[0] if parsed is not None else "?"
        wchan = (_read_text(os.path.join(base, "wchan")) or "?").strip() or "0"
        lines.append("tid {} {} state={} wchan={}".format(tid, comm, state, wchan))
        stack = (_read_text(os.path.join(base, "stack"), 4096) or "").strip()
        if stack:
            lines.extend("    " + frame for frame in stack.splitlines()[:16])
    return "\n".join(lines)


def py_spy_dump(pid: int) -> Optional[str]:
    """``py-spy dump --nonblocking`` of one process, or ``None`` when py-spy
    is not installed. ``--nonblocking`` reads the stacks without stopping
    the target, so a stopped or deadlocked process cannot hang the dump."""
    py_spy = shutil.which("py-spy")
    if not py_spy:
        return None
    try:
        result = subprocess.run(  # nosec B603 - fixed argv list, no shell
            [py_spy, "dump", "--nonblocking", "--pid", str(int(pid))],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=PY_SPY_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError) as err:
        return "py-spy dump failed: {}".format(err)
    text = result.stdout.decode("utf-8", "replace")
    if result.returncode != 0:
        text += "\n(py-spy exited {})".format(result.returncode)
    return text


def _dump_backend_stacks(fh) -> None:
    faulthandler.dump_traceback(file=fh, all_threads=True)


def _sigkill(pid: int) -> None:
    os.kill(pid, signal.SIGKILL)


def _format_bytes(value: float) -> str:
    return "{:.1f} MiB".format(float(value) / (1024 * 1024))


# --- the watchdog ----------------------------------------------------------

class ConstructionWatchdog:
    """Bounds ONE engine construction. ``start()`` on the constructing
    thread right before the factory call; ``cancel()`` right after it
    returns or raises (it returns ``False`` when the watchdog already fired,
    in which case the construction must be treated as timed out)."""

    def __init__(
        self,
        model_name: str,
        bound_s: float,
        stall_window_s: float,
        grace_s: float,
        on_unblock_failed: Optional[Callable[[str], None]] = None,
        diagnostics_dir: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
        process_table: Optional[Callable[[], Dict[int, ProcInfo]]] = None,
        uptime_reader: Callable[[], float] = read_uptime_seconds,
        io_reader: Callable[[int], int] = read_io_bytes,
        thread_progress: Callable[[int], Tuple[float, int]] = read_thread_progress,
        killer: Callable[[int], None] = _sigkill,
        stack_dumper: Callable = _dump_backend_stacks,
        engine_core_dumper: Callable[[int], Optional[str]] = py_spy_dump,
        proc_dumper: Callable[[int], str] = proc_fallback_dump,
        sample_period_s: float = ENGINE_WATCHDOG_SAMPLE_PERIOD_S,
        stall_cpu_seconds: float = ENGINE_STALL_CPU_SECONDS,
        stall_io_bytes: float = ENGINE_STALL_IO_BYTES,
        keep_diagnostics: int = ENGINE_DIAGNOSTICS_KEEP,
        own_pid: Optional[int] = None,
    ):
        self.model_name = model_name
        self.bound_s = float(bound_s or 0.0)
        self.stall_window_s = float(stall_window_s or 0.0)
        self.grace_s = float(grace_s or 0.0)
        self._on_unblock_failed = on_unblock_failed
        self._diagnostics_dir = diagnostics_dir
        self._clock = clock
        self._own_pid = own_pid
        self._process_table = process_table
        self._uptime_reader = uptime_reader
        self._io_reader = io_reader
        self._thread_progress = thread_progress
        self._killer = killer
        self._stack_dumper = stack_dumper
        self._engine_core_dumper = engine_core_dumper
        self._proc_dumper = proc_dumper
        self._sample_period_s = float(sample_period_s)
        self._stall_cpu_seconds = float(stall_cpu_seconds)
        self._stall_io_bytes = float(stall_io_bytes)
        self._keep_diagnostics = int(keep_diagnostics)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._returned = threading.Event()
        self._fired = False
        self._cancelled = False
        self._trigger: Optional[str] = None
        self._reason: Optional[str] = None
        self._diagnostics_path: Optional[str] = None
        self._killed: Tuple[int, ...] = ()
        self._thread: Optional[threading.Thread] = None
        self._tid: Optional[int] = None
        self._t0 = 0.0
        self._t0_ticks = 0.0

    # --- read-only state ---------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.bound_s > 0 or self.stall_window_s > 0

    @property
    def fired(self) -> bool:
        with self._lock:
            return self._fired

    @property
    def trigger(self) -> Optional[str]:
        with self._lock:
            return self._trigger

    @property
    def reason(self) -> Optional[str]:
        with self._lock:
            return self._reason

    @property
    def diagnostics_path(self) -> Optional[str]:
        with self._lock:
            return self._diagnostics_path

    @property
    def killed_pids(self) -> Tuple[int, ...]:
        with self._lock:
            return self._killed

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> "ConstructionWatchdog":
        """Arm on the constructing thread (its native id is what the stall
        check measures). Cheap: one ``/proc/uptime`` read and a thread
        start; the first sample is taken one period later, so a fast
        construction never scans ``/proc``. A disabled watchdog (both
        triggers 0) starts no thread."""
        if self._own_pid is None:
            self._own_pid = os.getpid()
        self._t0 = self._clock()
        self._t0_ticks = float(self._uptime_reader()) * CLK_TCK
        self._tid = threading.get_native_id()
        if not self.enabled:
            return self
        self._thread = threading.Thread(
            target=self._run, name="vllm-construction-watchdog", daemon=True)
        self._thread.start()
        return self

    def cancel(self) -> bool:
        """Disarm (the construction returned or raised). ``True`` when the
        watchdog had not fired; ``False`` when it had, so the result must be
        treated as a timeout."""
        with self._lock:
            fired = self._fired
            if not fired:
                self._cancelled = True
        self._returned.set()
        self._stop.set()
        return not fired

    def join(self, timeout: Optional[float] = None) -> None:
        """Wait for the watchdog thread (tests)."""
        if self._thread is not None:
            self._thread.join(timeout)

    # --- the sampling loop -------------------------------------------------

    def _snapshot(self) -> Dict[int, ProcInfo]:
        if self._process_table is not None:
            return self._process_table()
        return read_process_table(self._own_pid)

    def _progress(self) -> Tuple[float, float]:
        """Absolute ``(cpu_seconds, io_bytes)`` of the constructing thread
        plus this construction's engine-core trees."""
        cpu, io = self._thread_progress(self._tid)
        table = self._snapshot()
        for core in select_engine_cores(table, self._own_pid, self._t0_ticks):
            for pid in [core.pid] + descendants(table, core.pid):
                info = table.get(pid)
                if info is None:
                    continue
                cpu += info.cpu_seconds
                io += self._io_reader(pid)
        return float(cpu), float(io)

    def _run(self) -> None:
        try:
            samples: Deque[Tuple[float, float, float]] = deque()
            cpu_total = 0.0
            io_total = 0.0
            last: Optional[Tuple[float, float]] = None
            while True:
                if self._stop.wait(self._sample_period_s):
                    return
                elapsed = self._clock() - self._t0
                raw = self._progress()
                if last is not None:
                    # Positive deltas only: a process leaving the tree (an
                    # exited engine core reaped by the backend) is not a loss
                    # of progress.
                    cpu_total += max(0.0, raw[0] - last[0])
                    io_total += max(0.0, raw[1] - last[1])
                last = raw
                samples.append((elapsed, cpu_total, io_total))
                trigger = self._check(elapsed, samples)
                if trigger is not None:
                    self._fire(trigger, elapsed, list(samples))
                    return
        except Exception:  # noqa: BLE001 - never take the backend down
            logger.exception(
                "vLLM construction watchdog for '%s' failed; this construction "
                "is no longer bounded", self.model_name)

    def _check(self, elapsed: float,
               samples: Deque[Tuple[float, float, float]]) -> Optional[str]:
        if self.bound_s > 0 and elapsed >= self.bound_s:
            return TRIGGER_BOUND
        if self.stall_window_s > 0 and elapsed >= self.stall_window_s:
            horizon = elapsed - self.stall_window_s
            # Keep the newest sample at or before the horizon as the base.
            while len(samples) >= 2 and samples[1][0] <= horizon:
                samples.popleft()
            base = samples[0]
            if base[0] <= horizon:
                d_cpu = samples[-1][1] - base[1]
                d_io = samples[-1][2] - base[2]
                if d_cpu < self._stall_cpu_seconds and d_io < self._stall_io_bytes:
                    return TRIGGER_STALL
        return None

    # --- firing --------------------------------------------------------------

    def _fire(self, trigger: str, elapsed: float,
              samples: List[Tuple[float, float, float]]) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._fired = True
            self._trigger = trigger
        try:
            table = self._snapshot()
            cores = select_engine_cores(table, self._own_pid, self._t0_ticks)
        except Exception:  # noqa: BLE001
            logger.exception("vLLM construction watchdog: could not list the "
                             "engine cores to stop")
            table, cores = {}, []
        targets: List[int] = []
        for core in cores:
            # Descendants first, then the core: nothing is left running
            # under a dead parent.
            targets.extend(pid for pid in descendants(table, core.pid)
                           if pid not in targets)
            targets.append(core.pid)

        diagnostics = self._write_diagnostics(trigger, elapsed, samples, cores, targets)
        reason = self._format_reason(trigger, elapsed, len(cores), diagnostics)
        with self._lock:
            self._reason = reason
            self._diagnostics_path = diagnostics
        logger.error("%s", reason)

        killed: List[int] = []
        for pid in targets:
            try:
                self._killer(pid)
                killed.append(pid)
            except ProcessLookupError:
                pass
            except Exception:  # noqa: BLE001 - keep stopping the rest
                logger.exception("vLLM construction watchdog: could not stop "
                                 "pid %s", pid)
        with self._lock:
            self._killed = tuple(killed)

        if self._returned.wait(self.grace_s):
            return
        logger.critical(
            "vLLM engine construction for '%s' did not return within the %.0f s "
            "Unblock_Grace after the watchdog fired (%s engine core process(es) "
            "were stopped); the vLLM runtime server cannot serve until the "
            "backend restarts. Diagnostics: %s",
            self.model_name, self.grace_s, len(cores), diagnostics)
        if self._on_unblock_failed is not None:
            try:
                self._on_unblock_failed(reason)
            except Exception:  # noqa: BLE001
                logger.exception("vLLM construction watchdog: the unblock "
                                 "failure handler raised")

    def _format_reason(self, trigger: str, elapsed: float, cores: int,
                       diagnostics: Optional[str]) -> str:
        if trigger == TRIGGER_STALL:
            what = ("made no progress for {:.0f} s (under {:g} CPU-second(s) and "
                    "{} of I/O; stalled {:.0f} s after it started; window: {}, "
                    "{:.0f} s)").format(
                        self.stall_window_s, self._stall_cpu_seconds,
                        _format_bytes(self._stall_io_bytes), elapsed,
                        "VLLM_ENGINE_STALL_WINDOW_S", self.stall_window_s)
        else:
            what = "exceeded the {:.0f} s Construction_Bound ({})".format(
                self.bound_s, "VLLM_ENGINE_CONSTRUCTION_TIMEOUT_S")
        return ("{} vLLM engine construction for '{}' {}; stopped {} engine core "
                "process(es); diagnostics: {}").format(
                    ENGINE_CONSTRUCTION_TIMEOUT_MARKER, self.model_name, what,
                    cores, diagnostics or "none written")

    # --- diagnostics ---------------------------------------------------------

    def _write_diagnostics(self, trigger: str, elapsed: float,
                           samples: List[Tuple[float, float, float]],
                           cores: List[ProcInfo], targets: List[int]) -> Optional[str]:
        if not self._diagnostics_dir:
            return None
        try:
            directory = Path(self._diagnostics_dir)
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", self.model_name)[:80]
            path = directory / "{}{}-{}.txt".format(DIAGNOSTICS_PREFIX, safe, stamp)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("vLLM engine construction watchdog fired\n")
                fh.write("model: {}\ntrigger: {}\nelapsed: {:.1f} s\n".format(
                    self.model_name, trigger, elapsed))
                fh.write("bound: {:.0f} s  stall window: {:.0f} s  grace: {:.0f} s\n".format(
                    self.bound_s, self.stall_window_s, self.grace_s))
                fh.write("backend pid: {}  constructing thread: {}\n".format(
                    self._own_pid, self._tid))
                fh.write("engine cores: {}\nprocesses to stop: {}\n".format(
                    ", ".join("{} ({}, state {})".format(c.pid, c.cmdline, c.state)
                              for c in cores) or "none",
                    targets or "none"))
                fh.write("progress samples (elapsed s, cumulative CPU s, cumulative I/O bytes):\n")
                for sample in samples[-30:]:
                    fh.write("  {:.1f} {:.2f} {:.0f}\n".format(*sample))
                fh.write("\n== backend Python stacks (faulthandler, all threads) ==\n")
                fh.flush()
                self._safe(lambda: self._stack_dumper(fh))
                fh.flush()
                fh.write("\n== constructing thread {} (/proc) ==\n".format(self._tid))
                fh.write(self._safe(lambda: self._thread_state()) or "unreadable")
                fh.write("\n")
                for core in cores:
                    fh.write("\n== engine core {}: py-spy dump ==\n".format(core.pid))
                    dumped = self._safe(lambda: self._engine_core_dumper(core.pid))
                    fh.write(dumped if dumped else "py-spy not available\n")
                    fh.write("\n== engine core {}: /proc ==\n".format(core.pid))
                    fh.write(self._safe(lambda: self._proc_dumper(core.pid)) or "unreadable")
                    fh.write("\n")
            self._prune_diagnostics(directory)
            return str(path)
        except Exception:  # noqa: BLE001 - diagnostics never block the recovery
            logger.exception("vLLM construction watchdog: could not write the "
                             "diagnostics file")
            return None

    def _thread_state(self) -> str:
        base = os.path.join("/proc", "self", "task", str(self._tid))
        parsed = _parse_stat(_read_text(os.path.join(base, "stat")) or "")
        wchan = (_read_text(os.path.join(base, "wchan")) or "?").strip() or "0"
        stack = (_read_text(os.path.join(base, "stack"), 4096) or "").strip()
        lines = ["state={} wchan={}".format(parsed[0] if parsed else "?", wchan)]
        if stack:
            lines.extend("    " + frame for frame in stack.splitlines()[:16])
        return "\n".join(lines)

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception as err:  # noqa: BLE001 - best effort, record why
            return "unavailable: {}\n".format(err)

    def _prune_diagnostics(self, directory: Path) -> None:
        files = sorted(directory.glob(DIAGNOSTICS_PREFIX + "*.txt"),
                       key=lambda p: (p.stat().st_mtime, p.name))
        for stale in files[:max(0, len(files) - self._keep_diagnostics)]:
            try:
                stale.unlink()
            except OSError:
                pass
