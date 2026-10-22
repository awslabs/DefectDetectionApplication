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
"""Relay of the Edge MCP child's stdout and stderr into the application log.

Feature: mcp-servers (design §5 ``log_relay.py`` row, Data Models "Edge
access-log record"; tasks.md 10.5). Requirements 11.4, 9.7.
"""
import io
import json
import logging

import pytest

from mcp_edge import log_relay


class RecordingLogger:
    def __init__(self):
        self.calls = []

    def info(self, event, **fields):
        self.calls.append(("info", event, fields))

    def warning(self, event, **fields):
        self.calls.append(("warning", event, fields))


@pytest.fixture
def access(monkeypatch):
    recorder = RecordingLogger()
    names = []

    def get_logger(name):
        names.append(name)
        return recorder

    monkeypatch.setattr(log_relay.structlog.stdlib, "get_logger", get_logger)
    recorder.names = names
    return recorder


RECORD = {"event": "mcp tool", "channel": "mcp", "tool": "edge_registrations_list",
          "mcp_server": "dda-edge-mcp@1.0.0+1.0.130", "mcp_client": "kiro/1.2.3",
          "request_id": "r-1", "principal": "operator1", "role": "Operator",
          "source": "local_session", "outcome": "success", "category": None,
          "status": 200, "duration_ms": 41, "level": "info"}


def test_json_record_becomes_one_access_event(access):
    log_relay.relay_stdout_line(json.dumps(RECORD) + "\n")
    assert access.names == ["mcp.access"]
    level, event, fields = access.calls[0]
    assert (level, event) == ("info", "mcp tool")
    expected = {k: v for k, v in RECORD.items() if k not in ("event", "level")}
    assert fields == expected


def test_warning_records_stay_warnings(access):
    denied = dict(RECORD, outcome="denied", level="warning")
    log_relay.relay_stdout_line(json.dumps(denied))
    assert access.calls[0][0] == "warning"


@pytest.mark.parametrize("line", ["not json", "[1, 2]", "\"text\"", "{broken"])
def test_malformed_stdout_goes_to_the_child_logger(access, caplog, line):
    caplog.set_level(logging.INFO, logger="mcp.child")
    log_relay.relay_stdout_line(line)
    assert access.calls == []
    assert [r.levelno for r in caplog.records if r.name == "mcp.child"] == [logging.INFO]
    assert caplog.records[-1].getMessage() == line


def test_blank_stdout_lines_are_skipped(access, caplog):
    caplog.set_level(logging.INFO, logger="mcp.child")
    log_relay.relay_stdout_line("\n")
    assert access.calls == [] and caplog.records == []


def test_stderr_goes_to_the_child_logger_at_warning(caplog):
    caplog.set_level(logging.INFO, logger="mcp.child")
    log_relay.relay_stderr_line("Traceback (most recent call last):\n")
    record = caplog.records[-1]
    assert record.name == "mcp.child"
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "Traceback (most recent call last):"


@pytest.mark.parametrize("line", [
    'HTTP Request: GET https://127.0.0.1:5443/images/x?token=abc.def "HTTP/1.1 200 OK"',
    "something TOKEN=secret-value",
    "url ...?Token=abc",
])
def test_credential_bearing_stderr_line_is_replaced(caplog, line):
    caplog.set_level(logging.INFO, logger="mcp.child")
    log_relay.relay_stderr_line(line)
    message = caplog.records[-1].getMessage()
    assert message == log_relay.REDACTED_LINE
    assert "abc" not in caplog.text and "secret-value" not in caplog.text


def test_relay_exceptions_are_swallowed(monkeypatch):
    def explode(name):
        raise RuntimeError("logging broke")

    monkeypatch.setattr(log_relay.structlog.stdlib, "get_logger", explode)
    log_relay.relay_stdout_line(json.dumps(RECORD))
    monkeypatch.setattr(log_relay, "scrub_stderr_line", lambda line: 1 / 0)
    log_relay.relay_stderr_line("x")


def test_relay_stream_reads_until_eof():
    lines = []
    stream = io.BytesIO(b"one\ntwo\n\xff\xfebroken\n")
    log_relay.relay_stream(stream, lines.append)
    assert lines == ["one\n", "two\n", "\ufffd\ufffdbroken\n"]


def test_relay_stream_survives_a_broken_pipe():
    class Broken:
        def readline(self):
            raise OSError("pipe closed")

    log_relay.relay_stream(Broken(), lambda line: None)


def test_start_relays_threads(access):
    out = io.BytesIO((json.dumps(RECORD) + "\n").encode())
    err = io.BytesIO(b"warning line\n")
    threads = log_relay.start_relays(out, err)
    for thread in threads:
        thread.join(5)
        assert thread.daemon is True
    assert len(threads) == 2
    assert access.calls[0][1] == "mcp tool"
    assert log_relay.start_relays(None, None) == []
