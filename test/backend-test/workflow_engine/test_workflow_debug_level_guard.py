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
"""The level guard (workflow-debug-mode design §5.1 ``logtap.py``
``DebugLevelGuard``; tasks.md 2.6).

An identical fixture run with and without a DEBUG session yields
byte-identical ``run.log`` and no DEBUG record at the root handler;
``gstreamer`` pre-set to DEBUG keeps its DEBUG lines in ``run.log`` and at the
root while a ``workflow_engine`` DEBUG line does not reach them; with the root
at WARNING, a run started before the session and ended during it leaves the
logger at DEBUG after one tick and at NOTSET (the capture's restore) after
the session, not INFO; a handler refusing the filter leaves levels untouched
and sets ``levelDowngraded``.

Validates: debug R4.2, R13.1, R12.3.
"""
import logging
import re

import pytest

from workflow_engine import run_log
from workflow_engine.run_log import RunLogCapture
from workflow_engine.telemetry.debug.logtap import (
    DebugLevelGuard,
    DebugLogHandler,
    DebugOnlyFilter,
)

ROOT = logging.getLogger()
WE = logging.getLogger("workflow_engine")
WE_CHILD = logging.getLogger("workflow_engine.level_guard_test")
GST = logging.getLogger("gstreamer")
TRITON = logging.getLogger("dda_triton")
TRITON_CHILD = logging.getLogger("dda_triton.model_readiness")

_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} ")


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class RefusingHandler(logging.Handler):
    def addFilter(self, filter):
        raise RuntimeError("no filters here")

    def emit(self, record):
        pass


@pytest.fixture
def clean_logging():
    """Save and restore the levels and handlers of the loggers the guard
    touches, and the root; the test installs its own root handler."""
    loggers = (ROOT, WE, WE_CHILD, GST, TRITON, TRITON_CHILD)
    saved = [(lg, lg.level, list(lg.handlers)) for lg in loggers]
    saved_filters = {id(h): list(h.filters) for lg in loggers for h in lg.handlers}
    saved_capture_filters = list(run_log.CAPTURE_FILTERS)
    root_handler = ListHandler()
    ROOT.addHandler(root_handler)
    yield root_handler
    for lg, level, handlers in saved:
        lg.setLevel(level)
        for handler in list(lg.handlers):
            if handler not in handlers:
                lg.removeHandler(handler)
        for handler in handlers:
            if handler not in lg.handlers:
                lg.addHandler(handler)
            handler.filters = saved_filters.get(id(handler), handler.filters)
    del run_log.CAPTURE_FILTERS[:]
    run_log.CAPTURE_FILTERS.extend(saved_capture_filters)


def fixture_run(log_path):
    """An identical fixture run: lines at every level on the three loggers
    inside a RunLogCapture."""
    capture = RunLogCapture("exec-guard", str(log_path))
    capture.start()
    try:
        WE_CHILD.debug("workflow_engine DEBUG line")
        WE_CHILD.info("workflow_engine INFO line")
        GST.debug("gstreamer DEBUG line")
        GST.info("gstreamer INFO line")
        GST.error("gstreamer ERROR line")
        TRITON_CHILD.debug("dda_triton DEBUG line")
        TRITON_CHILD.info("dda_triton INFO line")
    finally:
        capture.stop()
    return _strip_timestamps(log_path.read_text(encoding="utf-8"))


def _strip_timestamps(text):
    return "\n".join(_TIMESTAMP.sub("", line) for line in text.splitlines())


def _debug_records(handler):
    return [(r.name, r.getMessage()) for r in handler.records if r.levelno < logging.INFO]


class TestByteIdentity:
    def test_run_log_is_byte_identical_with_and_without_a_debug_session(self, clean_logging, tmp_path):
        root_handler = clean_logging
        ROOT.setLevel(logging.INFO)  # production: the root at INFO
        for lg in (WE, GST, TRITON, WE_CHILD, TRITON_CHILD):
            lg.setLevel(logging.NOTSET)

        without = fixture_run(tmp_path / "without.log")
        root_without = [(r.name, r.getMessage()) for r in root_handler.records]
        root_handler.records.clear()

        tap = DebugLogHandler()
        for name in ("workflow_engine", "gstreamer", "dda_triton"):
            logging.getLogger(name).addHandler(tap)
        guard = DebugLevelGuard(tap)
        assert guard.apply() is True
        assert set(guard.lowered) == {"workflow_engine", "gstreamer", "dda_triton"}
        assert all(level == logging.NOTSET for level in guard.lowered.values())
        assert WE.level == GST.level == TRITON.level == logging.DEBUG
        assert guard.filter in run_log.CAPTURE_FILTERS
        assert guard.filter in root_handler.filters

        with_session = fixture_run(tmp_path / "with.log")
        root_with = [(r.name, r.getMessage()) for r in root_handler.records]

        assert with_session == without
        assert "DEBUG" not in with_session
        assert root_with == root_without
        assert _debug_records(root_handler) == []
        # The tap received the DEBUG lines the run.log and the root did not.
        tapped = [e[3] for e in tap.inbox if e[1] < logging.INFO]
        assert tapped == ["workflow_engine DEBUG line", "gstreamer DEBUG line", "dda_triton DEBUG line"]

        guard.restore()
        assert WE.level == GST.level == TRITON.level == logging.NOTSET
        assert guard.lowered == {}
        assert guard.filter not in run_log.CAPTURE_FILTERS
        assert guard.filter not in root_handler.filters
        after = fixture_run(tmp_path / "after.log")
        assert after == without

    def test_gstreamer_preset_to_debug_keeps_its_debug_lines(self, clean_logging, tmp_path):
        root_handler = clean_logging
        ROOT.setLevel(logging.INFO)
        for lg in (WE, TRITON, WE_CHILD, TRITON_CHILD):
            lg.setLevel(logging.NOTSET)
        GST.setLevel(logging.DEBUG)  # an operator's non-production configuration

        before = fixture_run(tmp_path / "before.log")
        assert "gstreamer: gstreamer DEBUG line" in before
        root_handler.records.clear()

        tap = DebugLogHandler()
        for name in ("workflow_engine", "gstreamer", "dda_triton"):
            logging.getLogger(name).addHandler(tap)
        guard = DebugLevelGuard(tap)
        guard.apply()
        assert set(guard.lowered) == {"workflow_engine", "dda_triton"}  # gstreamer was not lowered

        during = fixture_run(tmp_path / "during.log")
        assert during == before
        assert "gstreamer DEBUG line" in during
        assert "workflow_engine DEBUG line" not in during
        assert _debug_records(root_handler) == [("gstreamer", "gstreamer DEBUG line")]
        tapped = [e[3] for e in tap.inbox if e[1] < logging.INFO]
        assert "workflow_engine DEBUG line" in tapped and "gstreamer DEBUG line" in tapped

        guard.restore()
        assert GST.level == logging.DEBUG  # never touched
        assert WE.level == logging.NOTSET


class TestInterleavingWithRunLogCapture:
    def test_root_at_warning_run_spanning_the_session_start(self, clean_logging, tmp_path):
        root_handler = clean_logging
        ROOT.setLevel(logging.WARNING)
        for lg in (WE, GST, TRITON, WE_CHILD, TRITON_CHILD):
            lg.setLevel(logging.NOTSET)

        # A run starts before the session: the capture lowers workflow_engine to INFO.
        capture = RunLogCapture("exec-span", str(tmp_path / "span.log"))
        capture.start()
        assert WE.level == logging.INFO
        tap = DebugLogHandler()
        for name in ("workflow_engine", "gstreamer", "dda_triton"):
            logging.getLogger(name).addHandler(tap)
        guard = DebugLevelGuard(tap)
        guard.apply()  # saves INFO, sets DEBUG
        assert guard.lowered["workflow_engine"] == logging.INFO
        assert WE.level == logging.DEBUG
        WE_CHILD.debug("during the run and the session")
        # The run ends during the session: the capture restores NOTSET.
        capture.stop()
        assert WE.level == logging.NOTSET
        # One agent tick: the guard adopts NOTSET as the saved level and re-asserts DEBUG.
        guard.reassert()
        assert WE.level == logging.DEBUG
        assert guard.lowered["workflow_engine"] == logging.NOTSET
        # The session ends: the logger goes back to NOTSET (the capture's restore), not INFO.
        guard.restore()
        assert WE.level == logging.NOTSET
        assert _debug_records(root_handler) == []

    def test_restore_leaves_a_logger_someone_else_changed_alone(self, clean_logging, caplog):
        ROOT.setLevel(logging.INFO)
        for lg in (WE, GST, TRITON):
            lg.setLevel(logging.NOTSET)
        guard = DebugLevelGuard(DebugLogHandler())
        guard.apply()
        WE.setLevel(logging.WARNING)  # an operator changed it under the session
        with caplog.at_level(logging.INFO, logger="workflow_engine.telemetry.debug.logtap"):
            guard.restore()
        assert WE.level == logging.WARNING
        assert GST.level == logging.NOTSET
        assert any("leaving it alone" in r.getMessage() for r in caplog.records)

    def test_capture_handler_created_during_the_session_carries_the_filter(self, clean_logging, tmp_path):
        ROOT.setLevel(logging.INFO)
        for lg in (WE, GST, TRITON, WE_CHILD):
            lg.setLevel(logging.NOTSET)
        guard = DebugLevelGuard(DebugLogHandler())
        guard.apply()
        capture = RunLogCapture("exec-late", str(tmp_path / "late.log"))
        capture.start()
        try:
            assert guard.filter in capture._handler.filters
            WE_CHILD.debug("must not be written")
            WE_CHILD.info("must be written")
        finally:
            capture.stop()
        text = (tmp_path / "late.log").read_text(encoding="utf-8")
        assert "must be written" in text and "must not be written" not in text
        guard.restore()


class TestRefusedFilter:
    def test_a_handler_refusing_the_filter_leaves_levels_untouched_and_downgrades(self, clean_logging, caplog):
        root_handler = clean_logging
        ROOT.setLevel(logging.INFO)
        for lg in (WE, GST, TRITON):
            lg.setLevel(logging.NOTSET)
        refusing = RefusingHandler()
        GST.addHandler(refusing)
        guard = DebugLevelGuard(DebugLogHandler())
        with caplog.at_level(logging.WARNING, logger="workflow_engine.telemetry.debug.logtap"):
            assert guard.apply() is False
        assert guard.level_downgraded is True
        assert guard.lowered == {}
        assert WE.level == GST.level == TRITON.level == logging.NOTSET
        assert guard.filter not in root_handler.filters  # rolled back
        assert guard.filter not in run_log.CAPTURE_FILTERS
        assert any("refused" in r.getMessage() for r in caplog.records)
        guard.reassert()  # a no-op while downgraded
        assert WE.level == logging.NOTSET
        guard.restore()
        assert guard.active is False and guard.level_downgraded is False


class TestDebugOnlyFilter:
    def test_drops_only_sub_info_records_of_lowered_loggers_and_children(self):
        lowered = {"workflow_engine": logging.NOTSET}
        f = DebugOnlyFilter(lowered)

        def record(name, level):
            return logging.LogRecord(name, level, "f", 1, "m", (), None)

        assert f.filter(record("workflow_engine", logging.DEBUG)) is False
        assert f.filter(record("workflow_engine.pipeline_executor", logging.DEBUG)) is False
        assert f.filter(record("workflow_engine", logging.INFO)) is True
        assert f.filter(record("gstreamer", logging.DEBUG)) is True  # not lowered
        assert f.filter(record("workflow_engineering", logging.DEBUG)) is True  # not a child
        lowered["gstreamer"] = logging.NOTSET  # the live dict
        assert f.filter(record("gstreamer", logging.DEBUG)) is False
