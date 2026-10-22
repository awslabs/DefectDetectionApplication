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
"""Logging of the Edge MCP child: ``configure_logging()``, the record builder
and ``emit``, and the child's argument parser.

Feature: mcp-servers (design §5 ``child/__main__.py`` and ``child/logging.py``
rows, Data Models "Edge access-log record", Input validation "Child CLI
arguments"; tasks.md 11.2). Requirements 9.7, 11.1, 11.2.

Child-side suite (EDGE_CHILD_PYTEST, the venv's interpreter).
"""
import io
import json
import logging
import sys

import pytest

from mcp_edge.child import __main__ as child_main
from mcp_edge.child import logging as child_logging


@pytest.fixture
def clean_logging():
    root = logging.getLogger()
    saved = (root.level, list(root.handlers),
             {name: logging.getLogger(name).level for name in child_main.QUIET_LOGGERS})
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(saved[0])
    for handler in saved[1]:
        root.addHandler(handler)
    for name, level in saved[2].items():
        logging.getLogger(name).setLevel(level)


def _capture_root(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    child_main.configure_logging()
    return stream


# ----------------------------------------------------- configure_logging

def test_levels_and_single_stderr_handler(clean_logging, monkeypatch):
    stream = _capture_root(monkeypatch)
    root = logging.getLogger()
    assert root.level == logging.WARNING
    for name in ("httpx2", "httpcore2", "uvicorn", "uvicorn.access",
                 "uvicorn.error", "mcp"):
        assert logging.getLogger(name).level == logging.WARNING
    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert handler.stream is stream
    assert any(isinstance(f, child_main.CredentialScrubFilter) for f in handler.filters)


def test_idempotent(clean_logging, monkeypatch):
    _capture_root(monkeypatch)
    child_main.configure_logging()
    assert len(logging.getLogger().handlers) == 1


def test_httpx2_request_line_never_reaches_stderr(clean_logging, monkeypatch):
    stream = _capture_root(monkeypatch)
    url = "https://127.0.0.1:5443/captured-images/x.jpg?token=abc.def"
    logging.getLogger("httpx2").info('HTTP Request: GET %s "HTTP/1.1 200 OK"', url)
    logging.getLogger("httpx2").warning('HTTP Request: GET %s "HTTP/1.1 500"', url)
    assert stream.getvalue() == ""


@pytest.mark.parametrize("message", [
    "calling ?token=abc", "header Authorization: x", "sent BEARER abc",
    "TOKEN=1", "authorization failed for request",
])
def test_filter_drops_credential_markers(clean_logging, monkeypatch, message):
    stream = _capture_root(monkeypatch)
    logging.getLogger("mcp_edge.child.test").error(message)
    assert stream.getvalue() == ""


def test_filter_checks_exception_text(clean_logging, monkeypatch):
    stream = _capture_root(monkeypatch)
    try:
        raise RuntimeError("upstream said Bearer abc")
    except RuntimeError:
        logging.getLogger("mcp_edge.child.test").exception("dispatch failed")
    assert stream.getvalue() == ""


def test_ordinary_warnings_reach_stderr(clean_logging, monkeypatch):
    stream = _capture_root(monkeypatch)
    logging.getLogger("mcp_edge.child.test").warning("backend refused: %d", 503)
    logging.getLogger("mcp_edge.child.test").info("not shown")
    assert stream.getvalue() == "WARNING mcp_edge.child.test: backend refused: 503\n"


# --------------------------------------------------------- record builder

class Principal:
    username = "operator1"
    role = "Operator"
    source = "local_session"


def test_record_has_exactly_the_documented_fields():
    record = child_logging.build_record(
        tool="edge_registrations_list", outcome="success", principal=Principal(),
        request_id="r-1", mcp_server="dda-edge-mcp@1.0.0+1.0.130",
        mcp_client="kiro/1.2.3", status=200, duration_ms=41.7)
    assert record == {
        "event": "mcp tool", "channel": "mcp", "tool": "edge_registrations_list",
        "mcp_server": "dda-edge-mcp@1.0.0+1.0.130", "mcp_client": "kiro/1.2.3",
        "request_id": "r-1", "principal": "operator1", "role": "Operator",
        "source": "local_session", "outcome": "success", "category": None,
        "status": 200, "duration_ms": 41, "level": "info"}
    assert tuple(record) == child_logging.RECORD_FIELDS


def test_anonymous_and_warning_levels():
    record = child_logging.build_record(tool="edge_x_get", outcome="denied",
                                        category="not_authorized")
    assert record["principal"] == "anonymous"
    assert record["role"] is None and record["source"] is None
    assert record["level"] == "warning"
    unauthenticated = child_logging.build_record(tool="edge_x_get",
                                                 outcome="not_authenticated")
    assert unauthenticated["level"] == "warning"


def test_mapping_principal():
    record = child_logging.build_record(
        tool="t", outcome="success",
        principal={"username": "remote-user", "role": None,
                   "source": "existing_token_auth"})
    assert (record["principal"], record["role"], record["source"]) == (
        "remote-user", None, "existing_token_auth")


def test_headers_arguments_and_query_are_never_copied():
    secret = "Bearer abc.def-credential"
    record = child_logging.build_record(
        tool="edge_image_get", outcome="success", principal=Principal(),
        headers={"Authorization": secret}, arguments={"password": "pw-1",
                                                      "body": "x" * 5000},
        query={"token": "abc.def-credential"})
    line = json.dumps(record)
    for value in ("abc.def-credential", "pw-1", "xxxx", "Authorization"):
        assert value not in line


def test_long_values_are_cut():
    record = child_logging.build_record(tool="t" * 5000, outcome="success",
                                        principal={"username": "u" * 5000})
    assert len(record["tool"]) == child_logging.MAX_FIELD_CHARS
    assert len(record["principal"]) == child_logging.MAX_FIELD_CHARS
    assert len(json.dumps(record)) < 1024 + 512


def test_non_numeric_status_and_duration():
    record = child_logging.build_record(tool="t", outcome="success",
                                        status="200", duration_ms=True)
    assert record["status"] is None and record["duration_ms"] is None


def test_emit_writes_one_json_line():
    stream = io.StringIO()
    record = child_logging.build_record(tool="t", outcome="success")
    child_logging.emit(record, stream=stream)
    assert stream.getvalue().endswith("\n")
    assert stream.getvalue().count("\n") == 1
    assert json.loads(stream.getvalue()) == record


def test_emit_defaults_to_stdout(capsys):
    child_logging.emit({"event": "mcp tool"})
    assert capsys.readouterr().out == '{"event": "mcp tool"}\n'


def test_emit_never_raises():
    class Broken:
        def write(self, _text):
            raise OSError("closed")

    child_logging.emit({"event": "mcp tool"}, stream=Broken())
    child_logging.emit({"unserializable": object()}, stream=io.StringIO())


# ------------------------------------------------------------- arguments

def test_full_argument_set():
    args = child_main.parse_args([
        "--port", "5790", "--backend-url", "https://127.0.0.1:5443",
        "--server-version", "1.0.0+1.0.130",
        "--allowed-origins", "https://hmi.example.com,http://localhost:3000",
        "--features", "vllm", "--mutating-enabled"])
    assert args.port == 5790
    assert args.backend_url == "https://127.0.0.1:5443"
    assert args.server_version == "1.0.0+1.0.130"
    assert args.allowed_origins == ["https://hmi.example.com", "http://localhost:3000"]
    assert args.features == ["vllm"]
    assert args.mutating_enabled is True


def test_defaults():
    args = child_main.parse_args(["--backend-url", "http://127.0.0.1:5000"])
    assert args.port == 5790
    assert args.allowed_origins == [] and args.features == []
    assert args.mutating_enabled is False


@pytest.mark.parametrize("argv", [
    ["--backend-url", "http://10.0.0.5:5000"],
    ["--backend-url", "http://localhost:5000"],
    ["--backend-url", "ftp://127.0.0.1:21"],
    ["--backend-url", "http://127.0.0.1:5000/api"],
    ["--backend-url", "http://127.0.0.1:5000", "--port", "80"],
    ["--backend-url", "http://127.0.0.1:5000", "--port", "70000"],
    ["--backend-url", "http://127.0.0.1:5000", "--features", "gpu"],
    ["--backend-url", "http://127.0.0.1:5000", "--server-version", "1.0"],
    ["--backend-url", "http://127.0.0.1:5000", "--server-version", "1.0.0+a b"],
    ["--backend-url", "http://127.0.0.1:5000", "--allowed-origins", "https://a/path"],
    ["--backend-url", "http://127.0.0.1:5000", "--allowed-origins", "not a url"],
    [],
])
def test_invalid_arguments_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as exited:
        child_main.parse_args(argv)
    assert exited.value.code == 2


def test_version_and_help_exit_0(capsys):
    with pytest.raises(SystemExit) as exited:
        child_main.parse_args(["--version"])
    assert exited.value.code == 0
    assert capsys.readouterr().out.strip() == "dda-edge-mcp 1.0.0"
    with pytest.raises(SystemExit) as exited:
        child_main.parse_args(["--help"])
    assert exited.value.code == 0
    assert "--backend-url" in capsys.readouterr().out
