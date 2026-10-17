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
"""``utils.run_activity`` (triton-model-cleanup-wedge task 4.2, design D7,
Property 8): the registry that makes every workflow run count as in flight
from its start, never holds a run, and lets the self-heal commit only when
every registered run is blocked by the wedge."""
import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from utils import run_activity as ra
from triton_cleanup_wedge.watchdog_fakes import make_runner, step_inserts_row

REFUSAL = ("The LocalServer backend is restarting to clear a wedged Triton model load "
           "(self-heal attempt 1); run the workflow again in a few minutes.")


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def act(monkeypatch):
    clock = Clock()
    activity = ra.RunActivity(clock=clock)
    previous = ra.install(activity)
    token = ra._CURRENT.set(None)
    activity.clock = clock
    yield activity
    ra._CURRENT.reset(token)
    ra.install(previous)


def blocked_by(stalled=("model-a",)):
    return lambda info: ra.is_blocked(info, stalled)


def run_on_thread(fn, timeout=1.0):
    """Run ``fn`` on a fresh thread joined with ``timeout``; returns its
    result and asserts it did not hang."""
    box = {}

    def _target():
        box["value"] = fn()

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "the call was held"
    return box.get("value")


# --------------------------------------------------------------- tickets

def test_a_ticket_is_registered_and_finished(act):
    ticket = ra.start_run("execution", "exec-1", models=("model-a",), workflow="wf:1")
    assert ticket.refused is None and not ticket.noop
    snapshot = ra.snapshot()
    assert [t.run_id for t in snapshot.tickets] == ["exec-1"]
    assert snapshot.tickets[0].models == ("model-a",)
    assert ra.current() is ticket
    ra.finish_run(ticket)
    assert ra.snapshot().tickets == ()
    assert ra.current() is None
    ra.finish_run(ticket)  # idempotent
    ra.finish_run(None)


def test_a_nested_start_run_is_a_noop_and_counts_once(act):
    outer = ra.start_run("execution", "exec-1")
    inner = ra.start_run("pipeline", "image-source-pipeline", models=("model-a",))
    assert inner.noop and inner.refused is None
    assert len(ra.snapshot().tickets) == 1
    ra.finish_run(inner)
    assert len(ra.snapshot().tickets) == 1
    assert ra.current() is outer
    ra.finish_run(outer)
    assert ra.snapshot().tickets == ()


def test_models_are_parsed_from_launch_strings():
    launch = ('appsrc name=src ! videoconvert ! emltriton model="model-a" threshold=0.5 ! '
              'fakesink ! emltriton name=x model=model-b ! emltriton model=model-a')
    assert ra.models_from_launch(launch) == ("model-a", "model-b")
    assert ra.models_from_launch("videotestsrc ! fakesink") == ()
    assert ra.models_from_launch(None) == ()


def test_set_details_fills_in_the_label_and_the_models(act):
    ticket = ra.start_run("execution", "exec-1", workflow="reg-1")
    assert ra.snapshot().tickets[0].workflow == "reg-1"
    ra.set_details(ticket, workflow_label="wf-1 v2")
    ra.set_details(ticket, models=["model-a", None, "model-b"])
    info = ra.snapshot().tickets[0]
    assert info.workflow == "wf-1 v2"
    assert info.models == ("model-a", "model-b")
    ra.set_details(None, models=["x"])
    ra.set_details(ra.start_run("pipeline", "nested"), models=["x"])  # a no-op ticket
    assert ra.snapshot().tickets[0].models == ("model-a", "model-b")
    ra.finish_run(ticket)


def test_blocked_classification(act):
    in_gate = ra.TicketInfo("execution", "a", ("model-a",), None, 0.0, True, False, None, 0.0, True, False)
    gate_only = ra.TicketInfo("gate", "b", ("model-a",), None, 0.0, False, True, 0.0, 0.0, False, False)
    fresh = ra.TicketInfo("pipeline", "c", ("model-a",), None, 0.0, False, False, 0.0, 899.0, False, False)
    old = ra.TicketInfo("pipeline", "d", ("model-a",), None, 0.0, False, False, 0.0, 900.0, False, False)
    other = ra.TicketInfo("pipeline", "e", ("model-b",), None, 0.0, False, False, 0.0, 5000.0, False, False)
    assert ra.is_blocked(in_gate, ())
    assert ra.is_blocked(gate_only, ())
    assert not ra.is_blocked(fresh, ("model-a",))
    assert ra.is_blocked(old, ("model-a",))
    assert not ra.is_blocked(old, ())
    assert not ra.is_blocked(other, ("model-a",))


def test_instrumentation_never_raises(act, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("registry fault")

    monkeypatch.setattr(act, "_locked", boom)
    ticket = ra.start_run("execution", "exec-1")
    assert ticket.noop and ticket.refused is None
    ra.set_details(ra.RunTicket(kind="x", run_id="y"), models=["m"])
    ra.finish_run(ra.RunTicket(kind="x", run_id="y"))
    assert ra.after_gate(ticket) is None
    assert ra.quiescing() is False
    with ra.gate_wait("model-a") as gate:
        assert gate.refused is None
    assert "registry fault" in caplog.text


# ------------------------------------------------------ the context variable

def test_two_threads_hold_their_own_tickets(act):
    barrier = threading.Barrier(2)
    seen = {}

    def worker(name):
        ticket = ra.start_run("execution", name)
        barrier.wait(5)
        seen[name] = (ra.current() is ticket, ra.start_run("pipeline", "nested").noop)
        barrier.wait(5)
        ra.finish_run(ticket)
        seen[name + "-after"] = ra.current()

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("t1", "t2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert seen["t1"] == (True, True) and seen["t2"] == (True, True)
    assert seen["t1-after"] is None and seen["t2-after"] is None
    assert ra.snapshot().tickets == ()


def test_two_asyncio_tasks_hold_their_own_tickets(act):
    async def task(name, started, release):
        ticket = ra.start_run("capture", name)
        started.set()
        await release.wait()
        ok = ra.current() is ticket and ra.start_run("pipeline", "nested").noop
        ra.finish_run(ticket)
        return ok, ra.current()

    async def main():
        s1, s2, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        t1 = asyncio.ensure_future(task("cap-1", s1, release))
        t2 = asyncio.ensure_future(task("cap-2", s2, release))
        await s1.wait()
        await s2.wait()
        assert len(ra.snapshot().tickets) == 2
        release.set()
        return await asyncio.gather(t1, t2)

    loop = asyncio.new_event_loop()
    try:
        results = loop.run_until_complete(main())
    finally:
        loop.close()
    assert results == [(True, None), (True, None)]
    assert ra.snapshot().tickets == ()


def test_a_run_that_changes_threads(act):
    ticket = ra.start_run("pipeline", "wf-dio", models=("model-a",), bind=False)
    assert ra.current() is None, "bind=False leaves the registering context empty"
    assert len(ra.snapshot().tickets) == 1
    pool = ThreadPoolExecutor(max_workers=1)

    def work():
        with ra.bound(ticket):
            nested = ra.start_run("pipeline", "image-source-pipeline")
            inside = ra.current() is ticket
        return nested.noop, inside, ra.current()

    try:
        nested_noop, inside, after = pool.submit(work).result(5)
        assert nested_noop and inside and after is None
        assert len(ra.snapshot().tickets) == 1, "counted once"
        pool.submit(ra.finish_run, ticket).result(5)
        assert ra.snapshot().tickets == ()
        pool.submit(ra.finish_run, ticket).result(5)  # a second finish is a no-op

        def next_task():
            fresh = ra.start_run("pipeline", "next")
            registered = not fresh.noop
            ra.finish_run(fresh)
            return registered

        assert pool.submit(next_task).result(5), "the reused worker registers normally"
    finally:
        pool.shutdown(wait=True)


# --------------------------------------------------------------- the gate

def test_the_gate_decides_whether_a_ticket_is_blocked(act):
    ticket = ra.start_run("execution", "exec-1", models=("model-a",))
    assert ra.begin_quiesce()
    assert ra.commit_restart(blocked_by(), REFUSAL) == "runs"
    with ra.gate_wait("model-a") as gate:
        assert gate.refused is None
        assert ra.snapshot().tickets[0].in_gate
        assert ra.commit_restart(blocked_by(), REFUSAL) == "committed"
    assert ra.after_gate(ticket) == REFUSAL
    ra.finish_run(ticket)


def test_repeated_gate_waits_in_one_run_are_safe(act):
    ticket = ra.start_run("execution", "exec-1")
    with ra.gate_wait("model-a"):
        with ra.gate_wait("model-b"):
            assert ra.snapshot().tickets[0].in_gate
        assert ra.snapshot().tickets[0].in_gate
    info = ra.snapshot().tickets[0]
    assert not info.in_gate and info.outside_gate_for_s == 0.0
    act.clock.advance(30)
    assert ra.snapshot().tickets[0].outside_gate_for_s == 30.0
    ra.finish_run(ticket)


def test_a_gate_wait_without_a_ticket_registers_a_gate_only_ticket(act):
    with ra.gate_wait("model-a"):
        infos = ra.snapshot().tickets
        assert len(infos) == 1 and infos[0].gate_only and infos[0].kind == ra.KIND_GATE
    assert ra.snapshot().tickets == ()


def test_in_restarting_a_gate_wait_is_refused_and_the_gate_returns_at_once(act, monkeypatch, caplog):
    from dda_triton import model_readiness
    from triton_cleanup_wedge.fakes import FakeTritonClient, FakeTime

    assert ra.begin_quiesce()
    assert ra.commit_restart(blocked_by(), REFUSAL) == "committed"
    with ra.gate_wait("model-a") as gate:
        assert gate.refused == REFUSAL
    monkeypatch.setattr(model_readiness, "_repo_has_models", lambda: True)
    fake = FakeTime()
    client = FakeTritonClient(statuses=["LOADING"])
    caplog.set_level(logging.INFO)
    outcome = model_readiness.ensure_model_ready("ppe", "model-a", client=client, sleep=fake.sleep,
                                                 clock=fake.monotonic, stall_lookup=lambda t: None)
    assert not outcome.ready and outcome.message == REFUSAL
    assert fake.sleeps == [], "returned at once"
    assert "Refusing workflow run model-a: " + REFUSAL in caplog.text


# ---------------------------------------------------------------- no hold

def test_start_run_is_never_held(act):
    assert ra.begin_quiesce()
    ticket = run_on_thread(lambda: ra.start_run("execution", "exec-q", bind=False))
    assert ticket.refused is None and not ticket.noop
    assert ra.commit_restart(blocked_by(), REFUSAL) == "runs"
    ra.finish_run(ticket)
    assert ra.commit_restart(blocked_by(), REFUSAL) == "committed"
    refused = run_on_thread(lambda: ra.start_run("execution", "exec-r", bind=False))
    assert refused.refused == REFUSAL
    for state_call in (ra.quiescing, ra.state, ra.snapshot):
        run_on_thread(state_call)


# ------------------------------------------------------------ the expiry

def test_the_quiesce_expires_by_itself(act, caplog):
    caplog.set_level(logging.WARNING)
    assert ra.begin_quiesce()
    act.clock.advance(119)
    assert ra.quiescing() is True
    act.clock.advance(2)
    assert ra.quiescing() is False
    assert ra.state() == ra.STATE_IDLE
    lines = [r.getMessage() for r in caplog.records if "expired" in r.getMessage()]
    assert lines == ["The self-heal quiesce expired after 120s without a commit; continuous "
                     "workflows resume and workflow runs proceed normally"]
    assert ra.commit_restart(blocked_by(), REFUSAL) == "not_quiescing"
    ra.end_quiesce()
    assert ra.state() == ra.STATE_IDLE
    assert ra.begin_quiesce()


def test_restarting_never_expires(act):
    assert ra.begin_quiesce()
    assert ra.commit_restart(blocked_by(), REFUSAL) == "committed"
    act.clock.advance(10_000)
    assert ra.state() == ra.STATE_RESTARTING and ra.quiescing()
    ra.abort_restart()
    assert ra.state() == ra.STATE_IDLE


# ------------------------------------------------------- the state table

def _to(state):
    if state in (ra.STATE_QUIESCING, ra.STATE_RESTARTING):
        assert ra.begin_quiesce()
    if state == ra.STATE_RESTARTING:
        assert ra.commit_restart(blocked_by(), REFUSAL) == "committed"
    assert ra.state() == state


@pytest.mark.parametrize("state", [ra.STATE_IDLE, ra.STATE_QUIESCING, ra.STATE_RESTARTING])
def test_the_state_table_cell_by_cell(act, state):
    _to(state)
    restarting = state == ra.STATE_RESTARTING
    # start_run
    ticket = ra.start_run("execution", "exec-1", bind=False)
    assert (ticket.refused == REFUSAL) if restarting else (ticket.refused is None)
    ra.finish_run(ticket)
    # gate_wait entry
    with ra.gate_wait("model-a") as gate:
        assert (gate.refused == REFUSAL) if restarting else (gate.refused is None)
    # after_gate
    assert ra.after_gate(None) == (REFUSAL if restarting else None)
    # quiescing()
    assert ra.quiescing() is (state != ra.STATE_IDLE)
    # begin_quiesce outside IDLE is a no-op returning False
    if state != ra.STATE_IDLE:
        assert ra.begin_quiesce() is False
        assert ra.state() == state
    # commit_restart
    if state == ra.STATE_QUIESCING:
        blocker = ra.start_run("execution", "exec-2", bind=False)
        assert ra.commit_restart(blocked_by(), REFUSAL) == "runs"
        ra.finish_run(blocker)
        assert ra.commit_restart(blocked_by(), REFUSAL) == "committed"
        assert ra.state() == ra.STATE_RESTARTING
    else:
        assert ra.commit_restart(blocked_by(), REFUSAL) == "not_quiescing"
        assert ra.state() == state
    # end_quiesce is a no-op outside QUIESCING
    current = ra.state()
    if current != ra.STATE_QUIESCING:
        ra.end_quiesce()
        assert ra.state() == current


def test_end_quiesce_and_abort_restart_transitions(act):
    _to(ra.STATE_QUIESCING)
    ra.end_quiesce()
    assert ra.state() == ra.STATE_IDLE
    ra.abort_restart()  # a no-op in IDLE
    assert ra.state() == ra.STATE_IDLE
    _to(ra.STATE_QUIESCING)
    ra.abort_restart()
    assert ra.state() == ra.STATE_IDLE


# ------------------------------------------------- the run stream (Property 8)

def test_a_run_stream_with_a_quiesce_and_commit(act):
    stop = threading.Event()
    committed_at = {}
    violations = []
    lock = threading.Lock()

    def stream(name):
        count = 0
        while not stop.is_set():
            count += 1
            ticket = ra.start_run("execution", "{0}-{1}".format(name, count), bind=False)
            if ticket.refused is None:
                with lock:
                    if "seq" in committed_at:
                        violations.append(ticket.run_id)
                time.sleep(0.02)
                ra.finish_run(ticket)
            time.sleep(0.1)

    threads = [threading.Thread(target=stream, args=("s{0}".format(i),), daemon=True)
               for i in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    assert ra.begin_quiesce()
    outcome = None
    deadline = time.time() + 10
    while time.time() < deadline:
        with lock:
            outcome = ra.commit_restart(blocked_by(), REFUSAL)
            if outcome == "committed":
                committed_at["seq"] = True
                registered = ra.snapshot().tickets
                break
        time.sleep(0.005)
    time.sleep(0.4)
    stop.set()
    for t in threads:
        t.join(5)
    assert outcome == "committed", "the stream left a moment without an unblocked run"
    assert registered == (), "an unblocked run was registered at the commit"
    assert violations == [], "a run registered after the commit"
    assert ra.snapshot().tickets == ()


def test_commit_then_a_failed_write_then_abort_restores_service(act):
    runner, store, clock = make_runner()
    assert step_inserts_row(runner, store, clock)
    assert ra.begin_quiesce()
    assert not step_inserts_row(runner, store, clock)
    assert ra.commit_restart(blocked_by(), REFUSAL) == "committed"
    assert not step_inserts_row(runner, store, clock)
    ra.abort_restart()  # the record write failed (D7, "If steps 6-8 fail")
    ticket = ra.start_run("execution", "exec-1", bind=False)
    assert ticket.refused is None
    ra.finish_run(ticket)
    with ra.gate_wait("model-a") as gate:
        assert gate.refused is None
    assert step_inserts_row(runner, store, clock)


def test_the_continuous_runner_skips_its_ticks_while_quiescing(act):
    runner, store, clock = make_runner()
    assert step_inserts_row(runner, store, clock)
    assert ra.begin_quiesce()
    for _ in range(3):
        before = store.count
        clock.advance(1.0)
        assert runner.step() == 1.0
        assert store.count == before
    ra.end_quiesce()
    before = store.count
    runner.step()  # immediately, without advancing the clock
    assert store.count == before + 1


# ---------------------------------------------------------- interrupt_all

def test_interrupt_all(act, caplog):
    caplog.set_level(logging.WARNING)
    calls = []
    lock_state = []

    def ok(name):
        def _callback(error):
            # Outside the lock: the registry is usable from the callback.
            lock_state.append(len(ra.snapshot().tickets))
            calls.append((name, error))
        return _callback

    def raising(error):
        calls.append(("bad", error))
        raise RuntimeError("database is locked")

    t1 = ra.start_run("execution", "exec-1", on_interrupt=ok("exec-1"), bind=False)
    t2 = ra.start_run("execution", "exec-2", on_interrupt=raising, bind=False)
    t3 = ra.start_run("pipeline", "classic", bind=False)  # no callback
    t4 = ra.start_run("execution", "exec-4", on_interrupt=ok("exec-4"), bind=False)
    report = ra.interrupt_all("Interrupted: test", budget_s=5)
    assert report.marked == ["exec-1", "exec-4"]
    assert report.failed == ["exec-2"]
    assert report.skipped == 0
    assert [c[0] for c in calls] == ["exec-1", "bad", "exec-4"]
    assert all(c[1] == "Interrupted: test" for c in calls)
    assert lock_state == [4, 4]
    assert ("SELF-HEAL could not mark workflow run exec-2 failed (database is locked); it may "
            "show as pending or running after the restart") in caplog.text
    for t in (t1, t2, t3, t4):
        ra.finish_run(t)


def test_interrupt_all_stops_at_the_budget(act):
    release = threading.Event()

    def slow(error):
        release.wait(0.4)

    tickets = [ra.start_run("execution", "exec-{0}".format(i), on_interrupt=slow, bind=False)
               for i in range(5)]
    started = time.time()
    report = ra.interrupt_all("Interrupted", budget_s=1.0)
    elapsed = time.time() - started
    release.set()
    assert elapsed < 2.0
    assert 1 <= len(report.marked) <= 3
    assert report.failed == []
    assert report.skipped == 5 - len(report.marked)
    for t in tickets:
        ra.finish_run(t)


# ---------------------------------------------------------- refused tickets

def test_refused_tickets_are_noops(act):
    _to(ra.STATE_RESTARTING)
    ticket = ra.start_run("execution", "exec-1")
    assert ticket.refused == REFUSAL
    assert ra.current() is None
    assert ra._CURRENT.get() is None, "a refused ticket sets no context variable"
    with ra.bound(ticket):
        assert ra._CURRENT.get() is None
    ra.set_details(ticket, models=["model-a"], workflow_label="wf v1")
    ra.finish_run(ticket)
    assert ra._CURRENT.get() is None
    assert ra.snapshot().tickets == ()
    ra.abort_restart()
    fresh = ra.start_run("execution", "exec-2")
    assert fresh.refused is None and len(ra.snapshot().tickets) == 1
    ra.finish_run(fresh)


def test_the_refusal_is_logged_once_per_refusal(act, caplog):
    caplog.set_level(logging.INFO)
    _to(ra.STATE_RESTARTING)
    ra.start_run("capture", "cap-1")
    ra.start_run("execution", "exec-1")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Refusing")]
    assert lines == ["Refusing capture task cap-1: " + REFUSAL,
                     "Refusing workflow run exec-1: " + REFUSAL]


def test_continuous_starts_are_recorded(act):
    ticket = ra.start_run("execution", "exec-c", workflow="wf-c:1", continuous=True, bind=False)
    snapshot = ra.snapshot()
    assert ra.running_continuous(snapshot) == ["wf-c:1"]
    ra.finish_run(ticket)
    act.clock.advance(61)
    assert ra.running_continuous(ra.snapshot()) == []
