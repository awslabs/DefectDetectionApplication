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
"""Property-based tests for the Construction_Watchdog (spec
vllm-jp7-engine-lifecycle).

- Property 5 (Isolation): over generated process tables, the watchdog only
  ever selects, and only ever signals, engine cores that are children of
  the backend and started after the construction did, plus their
  descendants. Triton stubs, stream workers, other children and other
  models' engine cores are never signalled.
- Property 4 (Preservation): a construction that finishes before the
  watchdog fires (success or an ordinary failure) gives the same status,
  retained reason and manager log lines with the watchdog enabled as with
  it disabled, and nothing is signalled.
"""
import asyncio
import logging

from hypothesis import HealthCheck, given, settings, strategies as st

from vllm_jp7_engine_lifecycle.fakes import (
    DEFAULT_MODEL_NAME,
    FIXED_MEMINFO,
    FakeEngine,
    build_staged_repo,
)
from vllm_runtime.construction_watchdog import (
    CLK_TCK,
    CREATE_TIME_TOLERANCE_S,
    ConstructionWatchdog,
    ProcInfo,
    descendants,
    select_engine_cores,
)
from vllm_runtime.manager import VllmRuntimeManager

OWN_PID = 1
T0_TICKS = 500_000

_names = st.sampled_from([
    "VLLM::EngineCore", "VLLM::EngineCore_DP0", "triton_python_backend_stub",
    "python3 stream_worker.py", "gst-launch-1.0", "python3 app.py", "ptxas",
    "",
])
_comms = st.sampled_from(["VLLM::EngineCor", "python3", "tritonserver", ""])


@st.composite
def process_tables(draw):
    count = draw(st.integers(min_value=0, max_value=25))
    pids = draw(st.lists(st.integers(min_value=2, max_value=400), min_size=count,
                         max_size=count, unique=True))
    table = {}
    for pid in pids:
        parent = draw(st.sampled_from([OWN_PID, 999] + pids))
        if parent == pid:
            parent = OWN_PID
        offset_s = draw(st.floats(min_value=-30, max_value=30, allow_nan=False))
        table[pid] = ProcInfo(
            pid=pid, ppid=parent, state="S",
            cpu_seconds=draw(st.floats(min_value=0, max_value=50, allow_nan=False)),
            io_bytes=0,
            start_ticks=int(T0_TICKS + offset_s * CLK_TCK),
            cmdline=draw(_names), comm=draw(_comms))
    return table


def _expected_cores(table):
    floor = T0_TICKS - CREATE_TIME_TOLERANCE_S * CLK_TCK
    return {p.pid for p in table.values()
            if p.ppid == OWN_PID
            and ("EngineCore" in p.cmdline or "EngineCor" in p.comm)
            and p.start_ticks >= floor}


@settings(deadline=None)
@given(table=process_tables())
def test_property5_selection_is_exactly_new_engine_core_children(table):
    selected = {p.pid for p in select_engine_cores(table, OWN_PID, T0_TICKS)}
    assert selected == _expected_cores(table)


@settings(deadline=None)
@given(table=process_tables())
def test_property5_only_selected_trees_are_signalled(table):
    killed = []
    watchdog = ConstructionWatchdog(
        "m", bound_s=600, stall_window_s=60, grace_s=0.0,
        clock=iter(range(0, 10_000, 5)).__next__,
        process_table=lambda: dict(table),
        uptime_reader=lambda: T0_TICKS / CLK_TCK,
        io_reader=lambda pid: 0,
        thread_progress=lambda tid: (0.0, 0.0),
        killer=killed.append,
        engine_core_dumper=lambda pid: None,
        proc_dumper=lambda pid: "",
        sample_period_s=0.0005,
        own_pid=OWN_PID,
    )
    # Engine-core CPU in the table never changes, so this is a stall.
    watchdog.start()
    watchdog.join(10)
    cores = _expected_cores(table)
    allowed = set(cores)
    for core in cores:
        allowed.update(descendants(table, core))
    assert watchdog.fired
    assert set(killed) == allowed
    assert len(killed) == len(set(killed))


# --- Property 4 -----------------------------------------------------------

class _Collector(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages = []

    def emit(self, record):
        if record.name == "vllm_runtime.manager":
            self.messages.append((record.levelno, record.getMessage()))


_outcomes = st.one_of(
    st.just(("ok", None)),
    st.tuples(st.just("raise"),
              st.text(alphabet="abcdefghij klmnop", min_size=1, max_size=40)),
)


@settings(deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(outcome=_outcomes)
def test_property4_fast_constructions_behave_identically(tmp_path_factory, outcome):
    kind, text = outcome
    results = []
    for enabled in (True, False):
        model_dir = tmp_path_factory.mktemp("repo")
        build_staged_repo(model_dir, DEFAULT_MODEL_NAME)
        killed = []

        def factory(args, _kind=kind, _text=text):
            if _kind == "raise":
                raise RuntimeError(_text)
            return FakeEngine(args)

        manager = VllmRuntimeManager(
            model_dir=model_dir, engine_factory=factory, sampling_params_factory=dict,
            memory_reader=lambda: FIXED_MEMINFO,
            construction_bound_s=600.0 if enabled else 0.0,
            stall_window_s=120.0 if enabled else 0.0,
            unblock_grace_s=30.0,
            diagnostics_dir=str(model_dir / "logs"),
            watchdog_options={"killer": killed.append},
            self_restart=lambda: killed.append("restart"),
        )
        collector = _Collector()
        logger = logging.getLogger("vllm_runtime.manager")
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(collector)
        try:
            status = asyncio.run(manager.load(DEFAULT_MODEL_NAME))
        finally:
            logger.removeHandler(collector)
            logger.setLevel(previous)
        results.append((status, collector.messages, killed,
                        sorted(p.name for p in (model_dir / DEFAULT_MODEL_NAME).iterdir()),
                        (model_dir / "logs").exists()))
    with_watchdog, without = results
    assert with_watchdog[0] == without[0]
    assert with_watchdog[1] == without[1]
    assert with_watchdog[2] == [] and without[2] == []
    assert with_watchdog[3] == without[3]
    assert with_watchdog[4] is False and without[4] is False
