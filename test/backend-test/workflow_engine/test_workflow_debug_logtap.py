# Copyright 2025 Amazon Web Services, Inc.
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
"""The log tap (workflow-debug-mode design §5.1 ``logtap.py``; tasks.md 2.6).

Each of the five attribution rules; rules (1) and (2) are terminal (a line of
another Pair's run or continuous registration is counted ``otherPairLines``
and appears nowhere); the ``CAPTURED_LOGGERS``/INFO skip rule; a ``dda_triton``
INFO line of an open run is buffered; a line between runs becomes a ``log``
record; an unattributed line with two sessions is counted and dropped; the
2 KB cap; the 500-line buffer cap; the 5,000-entry inbox cap and ``tapDrops``;
``emit`` performs no redaction and no substring search; a DEBUG line emitted
5 ms before ``notify_run_finished`` is in that run's ``logs`` (through the
agent's tick ordering).

Validates: debug R4.2, R5.5, R7.3, R13.1, R12.3.
"""
import logging
import threading
from unittest import mock

import pytest

from dda_logging.run_context import continuous_run
from workflow_engine.telemetry import hooks
from workflow_engine.telemetry.debug import logtap as logtap_module
from workflow_engine.telemetry.debug.logtap import DebugLogHandler, LogTap, TapBuffer
from workflow_engine.telemetry.debug.sessions import SessionManager
from workflow_engine.telemetry.registry_view import RegistrationInfo

THING = "thing-1"
UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF_A = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
WF_B = "9b8a7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"
REG_A = "reg-a:1"
REG_B = "reg-b:1"
NOW_MS = 1_792_075_062_000

LOG_WE = logging.getLogger("workflow_engine.test_tap")
LOG_GST = logging.getLogger("gstreamer")
LOG_TRITON = logging.getLogger("dda_triton.model_readiness")


class FakeView:
    def __init__(self):
        self.rows = {}

    def snapshot(self):
        return dict(self.rows)


def entry(workflow_id, session_id, level="debug"):
    return {
        "sessionId": session_id, "usecaseId": UC, "enabledAt": NOW_MS - 60_000,
        "expiresAt": NOW_MS + 3_600_000, "level": level, "images": False, "imageMaxKb": 150,
        "prefix": "dda-telemetry/debug/%s/%s/%s/%s/" % (UC, THING, workflow_id, session_id),
        "maxMessagesPerSecond": 10, "maxBytesPerSecond": 262144,
    }


@pytest.fixture
def world():
    """A session manager with workflow A registered; ``activate`` turns on a
    session for A (and optionally B); every logger is at DEBUG for the test."""
    view = FakeView()
    view.rows[REG_A] = RegistrationInfo(WF_A, "1", (), False)
    view.rows[REG_B] = RegistrationInfo(WF_B, "1", (), False)
    manager = SessionManager(THING, view, wall=lambda: NOW_MS / 1000.0, agent_started_ms=NOW_MS)
    counters = {}
    tap = LogTap(manager, counters, secrets=lambda: ["s3cretvalue"])
    saved = [(lg, lg.level) for lg in (logging.getLogger("workflow_engine"), LOG_GST,
                                       logging.getLogger("dda_triton"))]
    for lg, _ in saved:
        lg.setLevel(logging.DEBUG)
    tap.attach()

    def activate(*workflows):
        desired = {}
        if WF_A in workflows:
            desired[WF_A] = entry(WF_A, SESSION_A)
        if WF_B in workflows:
            desired[WF_B] = entry(WF_B, SESSION_B)
        manager.apply_desired(desired, manager._last_version + 1 if manager._last_version else 1)
        for session in manager.active_sessions():
            session.batcher = object()  # a live stand-in: records are collected by the test

    yield manager, tap, counters, activate
    tap.detach()
    for lg, level in saved:
        lg.setLevel(level)
    assert hooks.CURRENT_EXECUTION.get() is None


def within_run(registration_id, workflow_id, execution_id):
    return hooks.CURRENT_EXECUTION.set((registration_id, workflow_id, execution_id))


class TestAttributionRules:
    def test_rule_1_current_execution_lands_in_that_runs_buffer(self, world):
        manager, tap, counters, activate = world
        activate(WF_A)
        token = within_run(REG_A, WF_A, "exec-1")
        try:
            LOG_WE.debug("resolving model %s", "plates")
            LOG_TRITON.info("model plates is READY after 0.2 s")
            LOG_WE.info("an INFO line that run.log already carries")
        finally:
            hooks.CURRENT_EXECUTION.reset(token)
        assert tap.drain_inbox() == []
        buffer = tap.buffer_for("exec-1")
        lines, dropped, _ = buffer.snapshot()
        assert dropped == 0
        assert [(l["level"], l["logger"], l["message"]) for l in lines] == [
            ("DEBUG", "workflow_engine.test_tap", "resolving model plates"),
            ("INFO", "dda_triton.model_readiness", "model plates is READY after 0.2 s"),
        ]
        assert all(l["src"] == "tap" for l in lines)
        assert counters["tapLines"] == 2

    def test_rule_1_is_terminal_for_another_pairs_run(self, world):
        manager, tap, counters, activate = world
        activate(WF_A)  # a session on A only
        token = within_run(REG_B, WF_B, "exec-b")
        try:
            LOG_WE.debug("a DEBUG line of B's run")
        finally:
            hooks.CURRENT_EXECUTION.reset(token)
        token = within_run(REG_A, WF_A, "exec-a")  # A has an open run, so rule 4 would match
        try:
            LOG_WE.debug("a line of A")
        finally:
            hooks.CURRENT_EXECUTION.reset(token)
        logs = tap.drain_inbox()
        assert logs == []
        assert counters["otherPairLines"] == 1
        assert tap.buffer_for("exec-b") is None
        assert [l["message"] for l in tap.buffer_for("exec-a").snapshot()[0]] == ["a line of A"]

    def test_rule_2_continuous_run_of_the_pair_is_a_between_runs_log_record(self, world):
        manager, tap, counters, activate = world
        activate(WF_A)
        with continuous_run(REG_A):
            LOG_WE.warning("Continuous workflow %s: stream camera is not connected", REG_A)
        logs = tap.drain_inbox()
        assert len(logs) == 1
        session, record = logs[0]
        assert session.workflow_id == WF_A
        assert record["type"] == "log" and record["attribution"] == "between_runs"
        assert record["level"] == "WARNING"
        assert "src" not in record

    def test_rule_2_is_terminal_for_another_registration(self, world):
        manager, tap, counters, activate = world
        activate(WF_A)
        with continuous_run(REG_B):
            LOG_WE.debug("B's continuous line")
        assert tap.drain_inbox() == []
        assert counters["otherPairLines"] == 1

    def test_rule_3_content_match_on_an_open_execution_id_and_a_registration_id(self, world):
        manager, tap, counters, activate = world
        activate(WF_A)
        tap.open_run("exec-77", WF_A)
        LOG_TRITON.info("Workflow execution exec-77 waits for model plates")  # no context var: a worker thread
        LOG_WE.warning("Trigger of %s fired while busy", REG_A)
        logs = tap.drain_inbox()
        assert [l["message"] for l in tap.buffer_for("exec-77").snapshot()[0]] == [
            "Workflow execution exec-77 waits for model plates"]
        assert len(logs) == 1 and logs[0][1]["attribution"] == "between_runs"
        assert "reg-a:1" in logs[0][1]["message"]

    def test_rule_4_single_open_run_window_attribution(self, world):
        manager, tap, counters, activate = world
        activate(WF_A, WF_B)
        tap.open_run("exec-a", WF_A)
        LOG_GST.debug("a thread-less gstreamer line during A's run")
        assert tap.drain_inbox() == []
        assert [l["message"] for l in tap.buffer_for("exec-a").snapshot()[0]] == [
            "a thread-less gstreamer line during A's run"]

    def test_rule_4_needs_exactly_one_pair_with_an_open_run(self, world):
        manager, tap, counters, activate = world
        activate(WF_A, WF_B)
        tap.open_run("exec-a", WF_A)
        tap.open_run("exec-b", WF_B)
        LOG_GST.debug("ambiguous")
        assert tap.drain_inbox() == []
        assert counters["unattributedLines"] == 1

    def test_rule_5_single_session_device_attribution(self, world):
        manager, tap, counters, activate = world
        activate(WF_A)
        LOG_TRITON.warning("Triton reports model helmets UNAVAILABLE")
        logs = tap.drain_inbox()
        assert len(logs) == 1
        session, record = logs[0]
        assert session.workflow_id == WF_A
        assert record["attribution"] == "device" and record["type"] == "log"

    def test_unattributed_with_two_sessions_is_counted_and_dropped(self, world):
        manager, tap, counters, activate = world
        activate(WF_A, WF_B)
        LOG_TRITON.warning("which session?")
        assert tap.drain_inbox() == []
        assert counters["unattributedLines"] == 1


class TestLoggerAndLevelRules:
    def test_info_of_a_captured_logger_is_skipped_and_its_debug_buffered(self, world):
        manager, tap, counters, activate = world
        activate(WF_A)
        token = within_run(REG_A, WF_A, "exec-1")
        try:
            LOG_WE.info("in run.log")
            LOG_GST.error("in run.log too")
            LOG_WE.debug("only in the tap")
            LOG_GST.debug("only in the tap too")
        finally:
            hooks.CURRENT_EXECUTION.reset(token)
        tap.drain_inbox()
        lines = tap.buffer_for("exec-1").snapshot()[0]
        assert [l["message"] for l in lines] == ["only in the tap", "only in the tap too"]

    def test_dda_triton_info_of_an_open_run_is_buffered_with_src_tap(self, world):
        manager, tap, counters, activate = world
        activate(WF_A)
        token = within_run(REG_A, WF_A, "exec-1")
        try:
            LOG_TRITON.info("Model %s READY", "plates")
            LOG_TRITON.warning("Model %s LOADING", "helmets")
        finally:
            hooks.CURRENT_EXECUTION.reset(token)
        tap.drain_inbox()
        lines = tap.buffer_for("exec-1").snapshot()[0]
        assert [(l["level"], l["src"]) for l in lines] == [("INFO", "tap"), ("WARNING", "tap")]

    def test_skip_rule_reads_captured_loggers(self, world, monkeypatch):
        manager, tap, counters, activate = world
        activate(WF_A)
        monkeypatch.setattr(logtap_module.run_log, "CAPTURED_LOGGERS", ("workflow_engine", "gstreamer", "dda_triton"))
        token = within_run(REG_A, WF_A, "exec-1")
        try:
            LOG_TRITON.info("now in run.log")
        finally:
            hooks.CURRENT_EXECUTION.reset(token)
        tap.drain_inbox()
        assert tap.buffer_for("exec-1").snapshot()[0] == []


class TestCapsAndRedaction:
    def test_message_is_redacted_capped_and_exc_info_formatted_on_the_agent_thread(self, world):
        manager, tap, counters, activate = world
        activate(WF_A)
        token = within_run(REG_A, WF_A, "exec-1")
        try:
            LOG_WE.debug("connect rtsp://u:s3cretvalue@cam/x with password=abc")
            LOG_WE.debug("x" * 5000)
            try:
                raise ValueError("bad s3cretvalue here")
            except ValueError:
                LOG_WE.debug("failed", exc_info=True)
        finally:
            hooks.CURRENT_EXECUTION.reset(token)
        tap.drain_inbox()
        lines = tap.buffer_for("exec-1").snapshot()[0]
        assert lines[0]["message"] == "connect rtsp://***@cam/x with password=***"
        assert len(lines[1]["message"]) == logtap_module.MESSAGE_CAP
        assert lines[2]["message"].startswith("failed\nTraceback (most recent call last)")
        assert "ValueError: bad *** here" in lines[2]["message"]
        assert "s3cretvalue" not in lines[2]["message"]

    def test_emit_caps_at_4kb_and_does_no_redaction_or_search(self, world):
        manager, tap, counters, activate = world
        activate(WF_A)
        tap.open_run("exec-1", WF_A)
        with mock.patch.object(logtap_module, "redact_structure", wraps=logtap_module.redact_structure) as spy, \
                mock.patch.object(tap, "open_runs", wraps=tap.open_runs) as runs_spy:
            token = within_run(REG_A, WF_A, "exec-1")
            try:
                LOG_WE.debug("secret rtsp://u:p@h/ " + "y" * 10_000)
            finally:
                hooks.CURRENT_EXECUTION.reset(token)
            assert spy.call_count == 0  # nothing redacted on the emitting thread
            raw = tap.handler.inbox[0]
            assert len(raw[3]) == logtap_module.EMIT_MESSAGE_CAP
            assert "rtsp://u:p@h/" in raw[3]  # unredacted in the inbox
            assert raw[6] == (REG_A, WF_A, "exec-1") and raw[7] is None
            assert not runs_spy.items.called and not runs_spy.get.called
        with mock.patch.object(logtap_module, "redact_structure", wraps=logtap_module.redact_structure) as spy:
            tap.drain_inbox()
            assert spy.call_count == 1  # the drain, on the agent thread, redacts
        line = tap.buffer_for("exec-1").snapshot()[0][0]
        assert line["message"].startswith("secret rtsp://***@h/")
        assert len(line["message"]) == logtap_module.MESSAGE_CAP

    def test_buffer_caps_at_500_lines_and_64_kb(self):
        buffer = TapBuffer()
        for i in range(600):
            buffer.append({"ts": i, "level": "DEBUG", "logger": "l", "message": "m%d" % i, "src": "tap"})
        lines, dropped, dropped_bytes = buffer.snapshot()
        assert len(lines) == 500 and dropped == 100 and dropped_bytes > 0
        assert lines[0]["message"] == "m100"
        big = TapBuffer()
        for i in range(100):
            big.append({"ts": i, "level": "DEBUG", "logger": "l", "message": "x" * 2000, "src": "tap"})
        lines, dropped, _ = big.snapshot()
        assert dropped > 0 and len(lines) < 100
        assert sum(len(l["message"]) + 64 for l in lines) <= logtap_module.BUFFER_MAX_BYTES

    def test_inbox_caps_at_5000_entries_and_counts_tap_drops(self, world):
        manager, tap, counters, activate = world
        activate(WF_A)
        token = within_run(REG_A, WF_A, "exec-1")
        try:
            for i in range(5100):
                LOG_WE.debug("line %d", i)
        finally:
            hooks.CURRENT_EXECUTION.reset(token)
        assert len(tap.handler.inbox) == logtap_module.INBOX_MAXLEN
        assert tap.handler.tap_drops == 100
        tap.drain_inbox()
        assert counters["tapDrops"] == 100
        assert tap.handler.tap_drops == 0
        lines, dropped, _ = tap.buffer_for("exec-1").snapshot()
        assert len(lines) == 500 and dropped == 4500  # the 5,000 drained lines, capped by the buffer
        assert lines[-1]["message"] == "line 5099"

    def test_handler_is_attached_only_to_the_three_loggers(self, world):
        manager, tap, counters, activate = world
        for name in logtap_module.TAPPED_LOGGERS:
            assert tap.handler in logging.getLogger(name).handlers
        assert tap.handler not in logging.getLogger().handlers
        tap.detach()
        for name in logtap_module.TAPPED_LOGGERS:
            assert tap.handler not in logging.getLogger(name).handlers
        tap.attach()

    def test_emit_failure_takes_the_handle_error_path_and_the_tap_stays(self, world):
        manager, tap, counters, activate = world
        handler = tap.handler
        record = logging.LogRecord("workflow_engine.x", logging.DEBUG, "f", 1, "%d", ("not-int",), None)
        class BrokenContext:
            def get(self):
                raise RuntimeError("ctx")

        class BrokenHooks:
            CURRENT_EXECUTION = BrokenContext()

        with mock.patch.object(DebugLogHandler, "handleError") as handle_error:
            handler.emit(record)  # getMessage raises; emit falls back to str(msg)
            assert handle_error.call_count == 0
            with mock.patch.object(logtap_module, "telemetry_hooks", BrokenHooks()):
                handler.emit(record)
            assert handle_error.call_count == 1
        assert handler in logging.getLogger("workflow_engine").handlers

    def test_wake_event_is_set_at_200_entries(self):
        wake = threading.Event()
        handler = DebugLogHandler(wake=wake)
        for i in range(199):
            handler.emit(logging.LogRecord("x", logging.DEBUG, "f", 1, "m", (), None))
        assert not wake.is_set()
        handler.emit(logging.LogRecord("x", logging.DEBUG, "f", 1, "m", (), None))
        assert wake.is_set()


class TestTickOrdering:
    def test_a_debug_line_emitted_5ms_before_the_hook_is_in_the_run_record(self, world, tmp_path):
        """Through the agent's tick ordering: the drain of the tick after the
        one that saw the RunRecord arrive runs before the record is assembled."""
        import time

        from workflow_engine.telemetry.debug import agent as agent_module

        manager, tap, counters, activate = world
        published = []

        class Publisher:
            failures_by_reason = {"denied": 0, "spool_full": 0, "other": 0}
            publish_failures = 0
            publish_successes = 0

            def publish(self, topic, payload, qos):
                published.append((topic, payload, qos))

            def status(self):
                return {}

        class Shadow:
            def __init__(self, *args, **kwargs):
                self.reports = []

            def start(self):
                pass

            def close(self):
                pass

            def report(self, state):
                self.reports.append(state)

        tap.detach()  # the agent owns its own tap
        hooks._clear_observers()
        agent = agent_module.DebugAgent(
            thing_name=THING, publisher=Publisher(), shadow_factory=Shadow,
            registry_view=manager._view, wall=lambda: NOW_MS / 1000.0, run_thread=False,
        )
        agent.start()
        try:
            agent._on_desired({"v": 1, "sessions": {WF_A: entry(WF_A, SESSION_A)}}, 1)
            agent.tick()
            assert agent.sessions.active_sessions()[0].batcher is not None
            token = within_run(REG_A, WF_A, "exec-5ms")
            try:
                LOG_WE.debug("the last DEBUG line of the run")
                time.sleep(0.005)
            finally:
                hooks.CURRENT_EXECUTION.reset(token)
            record = hooks.RunRecord(
                registration_id=REG_A, workflow_id=WF_A, workflow_version="1", execution_id="exec-5ms",
                status="completed", error=None, failing_node_id=None, traceback=None, capture_id=None,
                output_dir=None, log_path=None, enqueued_at_s=NOW_MS // 1000, accepted_ms=NOW_MS,
                run_started_ms=NOW_MS + 10, finished_ms=NOW_MS + 500, queued_ms=0, setup_ms=10,
                readiness_wait_ms=0, trigger_context={}, node_status=None, tag_values={},
                detections=None, detection_count=0, continuous=False,
            )
            hooks.notify_run_finished(record)
            agent.tick()  # the tick that sees the record arrive
            assert agent._run_inbox  # not assembled yet
            agent.tick()  # drains, then assembles
            assert not agent._run_inbox
            agent.tick()
            session = agent.sessions.active_sessions()[0]
            session.batcher.drain(force=True)
            import json

            records = [r for m in published for r in json.loads(m[1])["records"]]
            runs = [r for r in records if r["type"] == "run"]
            assert len(runs) == 1
            tap_lines = [l for l in runs[0]["logs"] if l["src"] == "tap"]
            assert [l["message"] for l in tap_lines] == ["the last DEBUG line of the run"]
        finally:
            agent.stop()
            hooks._clear_observers()
