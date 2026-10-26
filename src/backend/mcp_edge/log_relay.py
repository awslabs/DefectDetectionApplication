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
"""Relay of the Edge MCP child's output into the application log
(backend process; design §5 ``log_relay.py``, Data Models "Edge access-log
record").

- stdout carries only the child's JSON tool-completion records: each line
  that parses as a JSON object becomes one ``mcp.access`` structlog event
  (at WARNING when the record says ``"level": "warning"``, else INFO); any
  other line goes to the ``mcp.child`` logger at INFO.
- stderr carries the child's ``logging`` output: each line goes to
  ``mcp.child`` at WARNING, except that a line containing ``token=``
  (case-insensitive) is replaced by a fixed redaction marker first, a last
  line of defence for a credential-bearing loopback URL.

A relay failure never propagates (Requirement 11.4).
"""
import json
import logging
import threading
from typing import IO, Callable, Optional

import structlog

ACCESS_LOGGER_NAME = "mcp.access"
CHILD_LOGGER_NAME = "mcp.child"
REDACTED_LINE = "<redacted: credential-bearing line dropped>"
DEFAULT_EVENT = "mcp tool"

child_logger = logging.getLogger(CHILD_LOGGER_NAME)


def _access_logger():
    return structlog.stdlib.get_logger(ACCESS_LOGGER_NAME)


def relay_stdout_line(line: str) -> None:
    """One stdout line of the child; never raises."""
    try:
        text = line.rstrip("\r\n")
        if not text.strip():
            return
        try:
            record = json.loads(text)
        except ValueError:
            record = None
        if not isinstance(record, dict):
            child_logger.info("%s", text)
            return
        fields = dict(record)
        event = fields.pop("event", DEFAULT_EVENT)
        level = fields.pop("level", "info")
        logger = _access_logger()
        if level == "warning":
            logger.warning(event, **fields)
        else:
            logger.info(event, **fields)
    except Exception:  # noqa: BLE001 — the relay never fails the backend
        pass


def scrub_stderr_line(line: str) -> str:
    """The stderr line to log: the redaction marker for a line carrying
    ``token=`` (case-insensitive), else the line itself. Pure."""
    text = line.rstrip("\r\n")
    if "token=" in text.lower():
        return REDACTED_LINE
    return text


def relay_stderr_line(line: str) -> None:
    """One stderr line of the child; never raises."""
    try:
        text = scrub_stderr_line(line)
        if text.strip():
            child_logger.warning("%s", text)
    except Exception:  # noqa: BLE001 — the relay never fails the backend
        pass


def relay_stream(stream: IO[bytes], handle_line: Callable[[str], None]) -> None:
    """Relay every line of a child pipe until EOF; never raises."""
    try:
        for raw in iter(stream.readline, b""):
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            handle_line(raw)
    except Exception:  # noqa: BLE001 — a closed or broken pipe ends the relay
        pass


def start_relays(stdout: Optional[IO[bytes]], stderr: Optional[IO[bytes]]) -> list:
    """Start one daemon thread per pipe; returns the threads."""
    threads = []
    for stream, handler, name in ((stdout, relay_stdout_line, "mcp-child-stdout"),
                                  (stderr, relay_stderr_line, "mcp-child-stderr")):
        if stream is None:
            continue
        thread = threading.Thread(target=relay_stream, args=(stream, handler),
                                  name=name, daemon=True)
        thread.start()
        threads.append(thread)
    return threads
