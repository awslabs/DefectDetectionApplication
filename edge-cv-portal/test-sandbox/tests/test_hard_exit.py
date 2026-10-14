"""Hard-exit and hard-watchdog behavior (timeout robustness).

Background: emltriton starts an in-process Triton whose threads can
block normal interpreter shutdown, and a blocking GStreamer state
change wedges the main thread before the GLib watchdog can ever
dispatch. Either way the Fargate task idled until the Step Functions
10-minute timeout, whose generic message displaced the per-node error
already flushed to S3. These tests pin the two defenses:

* ``exit_now`` terminates via ``os._exit`` after flushing stdio, so
  lingering non-daemon threads cannot hold the task open;
* ``make_hard_watchdog`` flushes an explicit run-level timeout error
  (skipping unfinished nodes) and then exits — from a plain thread
  timer, independent of the GLib main loop.
"""

from harness.harness import exit_now, make_hard_watchdog
from harness.results import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    ResultsStore,
)


class FlushRecorder:
    def __init__(self):
        self.snapshots = []

    def __call__(self, document):
        self.snapshots.append(document)

    @property
    def last(self):
        return self.snapshots[-1]


class ExitRecorder:
    def __init__(self):
        self.codes = []

    def __call__(self, code):
        self.codes.append(code)


class TestExitNow:
    def test_terminates_via_os_exit_with_the_given_code(self, monkeypatch):
        codes = []
        monkeypatch.setattr("harness.harness.os._exit", codes.append)
        exit_now(3)
        assert codes == [3]

    def test_flushes_stdio_before_exiting(self, monkeypatch):
        order = []
        monkeypatch.setattr("harness.harness.os._exit",
                            lambda code: order.append("exit"))
        monkeypatch.setattr("harness.harness.sys.stdout",
                            type("O", (), {"flush": lambda self: order.append("stdout")})())
        monkeypatch.setattr("harness.harness.sys.stderr",
                            type("E", (), {"flush": lambda self: order.append("stderr")})())
        exit_now(0)
        assert order.index("stdout") < order.index("exit")
        assert order.index("stderr") < order.index("exit")


class TestHardWatchdog:
    def test_flushes_run_error_skips_nodes_and_exits_1(self):
        flush = FlushRecorder()
        store = ResultsStore(["src", "inf", "out"], flush)
        store.set_statuses(["src", "inf"], STATUS_RUNNING)
        exit_fn = ExitRecorder()

        make_hard_watchdog(store, exit_fn=exit_fn, timeout_sec=540)()

        assert exit_fn.codes == [1]
        nodes = flush.last["nodes"]
        run_error = next(n for n in nodes if n["nodeId"] is None)
        assert run_error["error"]["code"] == "PIPELINE_EXECUTION_TIMEOUT"
        assert "540s" in run_error["error"]["message"]
        by_id = {n["nodeId"]: n for n in nodes if n["nodeId"]}
        assert by_id["src"]["status"] == STATUS_SKIPPED
        assert by_id["inf"]["status"] == STATUS_SKIPPED
        assert by_id["out"]["status"] == STATUS_SKIPPED

    def test_completed_nodes_retained_untouched(self):
        flush = FlushRecorder()
        store = ResultsStore(["src", "inf"], flush)
        store.set_status("src", STATUS_COMPLETED)
        exit_fn = ExitRecorder()

        make_hard_watchdog(store, exit_fn=exit_fn)()

        by_id = {n["nodeId"]: n for n in flush.last["nodes"] if n["nodeId"]}
        assert by_id["src"]["status"] == STATUS_COMPLETED
        assert by_id["inf"]["status"] == STATUS_SKIPPED

    def test_exits_even_when_the_flush_fails(self):
        def broken_flush(document):
            raise OSError("S3 unreachable")

        store = ResultsStore(["src"], broken_flush)
        exit_fn = ExitRecorder()

        make_hard_watchdog(store, exit_fn=exit_fn)()

        assert exit_fn.codes == [1]
