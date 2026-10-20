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
"""The ``run.log`` parser (workflow-debug-mode design §5.1 ``runlog.py``;
tasks.md 2.2).

A fixture with a two-line message and a traceback; a ``LogRecord`` round trip
through ``run_log._LOG_FORMAT`` (local-time ``asctime``); the cut first line;
the 2,000-entry cap with its dropped count; the byte cap.

Validates: debug R4.2, R12.3.
"""
import logging
import time

from workflow_engine import run_log
from workflow_engine.telemetry.debug import runlog

FIXTURE = """2026-10-17 10:15:02,123 INFO workflow_engine.pipeline_executor: Workflow execution exec-1 (wf-1 v3) starting pipeline: videotestsrc ! fakesink
2026-10-17 10:15:02,456 INFO workflow_engine.node_status: node n1 resolved
line two of the same message
2026-10-17 10:15:03,001 ERROR gstreamer: Pipeline failed to change state to PLAYING
Traceback (most recent call last):
  File "x.py", line 1, in <module>
    raise RuntimeError("boom")
RuntimeError: boom
2026-10-17 10:15:03,500 WARNING workflow_engine: completion tags: {}
"""


def _local_ms(date_text, ms):
    return int(time.mktime(time.strptime(date_text, "%Y-%m-%d %H:%M:%S")) * 1000) + ms


def test_fixture_with_two_line_message_and_traceback():
    entries, dropped = runlog.parse(FIXTURE, fallback_ts_ms=0)
    assert dropped == 0
    assert [e["level"] for e in entries] == ["INFO", "INFO", "ERROR", "WARNING"]
    assert [e["logger"] for e in entries] == [
        "workflow_engine.pipeline_executor", "workflow_engine.node_status", "gstreamer",
        "workflow_engine",
    ]
    assert entries[0]["ts"] == _local_ms("2026-10-17 10:15:02", 123)
    assert entries[0]["message"].startswith("Workflow execution exec-1")
    assert entries[1]["message"] == "node n1 resolved\nline two of the same message"
    assert entries[2]["message"] == (
        "Pipeline failed to change state to PLAYING\nTraceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n    raise RuntimeError("boom")\nRuntimeError: boom'
    )
    assert entries[3]["ts"] == _local_ms("2026-10-17 10:15:03", 500)
    assert all(e["src"] == "runlog" for e in entries)
    assert entries[1]["ts"] < entries[2]["ts"] < entries[3]["ts"]


def test_log_record_round_trip_through_run_log_format():
    formatter = logging.Formatter(run_log._LOG_FORMAT)
    record = logging.LogRecord(
        "workflow_engine.pipeline_executor", logging.WARNING, "x.py", 1,
        "resolved %s in %d ms", ("model_1", 42), None,
    )
    text = formatter.format(record)
    entries, dropped = runlog.parse(text, fallback_ts_ms=0)
    assert dropped == 0
    assert len(entries) == 1
    entry = entries[0]
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "workflow_engine.pipeline_executor"
    assert entry["message"] == "resolved model_1 in 42 ms"
    assert abs(entry["ts"] - record.created * 1000) <= 1.0


def test_round_trip_with_exc_info_keeps_the_traceback_in_one_entry():
    formatter = logging.Formatter(run_log._LOG_FORMAT)
    try:
        raise ValueError("bad value")
    except ValueError:
        import sys

        record = logging.LogRecord("gstreamer", logging.ERROR, "x.py", 1, "failed", (),
                                   sys.exc_info())
    text = formatter.format(record)
    entries, _ = runlog.parse(text, fallback_ts_ms=0)
    assert len(entries) == 1
    assert entries[0]["message"].startswith("failed\nTraceback (most recent call last):")
    assert "ValueError: bad value" in entries[0]["message"]


def test_cut_first_line_opens_a_fallback_entry():
    text = "ngine.pipeline_executor: tail of a cut line\n" + FIXTURE
    entries, _ = runlog.parse(text, fallback_ts_ms=1_700_000_000_000)
    assert entries[0] == {"ts": 1_700_000_000_000, "level": "INFO", "logger": "run.log",
                          "message": "ngine.pipeline_executor: tail of a cut line", "src": "runlog"}
    assert len(entries) == 5


def test_matching_line_with_impossible_date_is_a_continuation():
    text = ("2026-10-17 10:15:02,123 INFO workflow_engine: first\n"
            "2026-13-45 10:15:02,123 INFO workflow_engine: not a date\n")
    entries, _ = runlog.parse(text, fallback_ts_ms=0)
    assert len(entries) == 1
    assert entries[0]["message"] == "first\n2026-13-45 10:15:02,123 INFO workflow_engine: not a date"


def test_impossible_date_on_the_first_line_uses_the_fallback():
    entries, _ = runlog.parse("2026-13-45 10:15:02,123 INFO workflow_engine: x\n", fallback_ts_ms=5)
    assert entries[0]["ts"] == 5 and entries[0]["logger"] == "run.log"


def test_3000_lines_yield_2000_entries_and_1000_dropped():
    lines = ["2026-10-17 10:15:%02d,%03d INFO workflow_engine: line %d" % (i // 1000 % 60, i % 1000, i)
             for i in range(3000)]
    entries, dropped = runlog.parse("\n".join(lines), fallback_ts_ms=0)
    assert len(entries) == 2000
    assert dropped == 1000
    assert entries[0]["message"] == "line 1000"  # oldest first were dropped
    assert entries[-1]["message"] == "line 2999"


def test_byte_cap_drops_oldest_first():
    big = "x" * 100_000
    lines = ["2026-10-17 10:15:0%d,000 INFO workflow_engine: %s" % (i, big) for i in range(4)]
    entries, dropped = runlog.parse("\n".join(lines), fallback_ts_ms=0)
    assert sum(len(e["message"]) for e in entries) <= runlog.RUNLOG_MAX_BYTES
    assert len(entries) == 2
    assert dropped == 2
    assert entries[0]["ts"] == _local_ms("2026-10-17 10:15:02", 0)


def test_empty_and_non_string_input():
    assert runlog.parse("", fallback_ts_ms=0) == ([], 0)
    assert runlog.parse(None, fallback_ts_ms=0) == ([], 0)  # type: ignore[arg-type]
    entries, _ = runlog.parse("\n\n", fallback_ts_ms=7)
    assert len(entries) == 1 and entries[0]["ts"] == 7  # two empty lines: one fallback entry


def test_module_is_stdlib_only():
    import inspect

    body = inspect.getsource(runlog).split('"""', 2)[2]
    assert "workflow_engine" not in body
    assert "import logging" not in body
