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
"""Structured tool-completion records of the Edge MCP child.

``build_record`` is a whitelisting record builder: it copies only the fields
of the Data Models "Edge access-log record" (``mcp.access``) and never the
caller's headers, arguments, query strings or bodies, so a record cannot
carry the ``Authorization`` value, a ``token`` query value, a password or a
large body (Requirements 11.1, 11.2, 9.7). ``emit`` writes one JSON line to
stdout, which the supervisor's log relay turns into an ``mcp.access``
event. Library ``logging`` output is not handled here; that is
``configure_logging()`` in ``__main__``.
"""
import json
import sys
from typing import Any, Mapping, Optional

EVENT = "mcp tool"
CHANNEL = "mcp"
# Each text field is cut to this many characters.
MAX_FIELD_CHARS = 256
# Outcomes logged at warning level.
WARNING_OUTCOMES = ("denied", "not_authenticated")

RECORD_FIELDS = ("event", "channel", "tool", "mcp_server", "mcp_client",
                 "request_id", "principal", "role", "source", "outcome",
                 "category", "status", "duration_ms", "level")


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text[:MAX_FIELD_CHARS]


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _principal_field(principal: Any, name: str) -> Any:
    if principal is None:
        return None
    if isinstance(principal, Mapping):
        return principal.get(name)
    return getattr(principal, name, None)


def build_record(*, tool: str, outcome: str, principal: Any = None,
                 request_id: Optional[str] = None,
                 mcp_server: Optional[str] = None,
                 mcp_client: Optional[str] = None,
                 category: Optional[str] = None, status: Any = None,
                 duration_ms: Any = None, headers: Any = None,
                 arguments: Any = None, query: Any = None) -> dict:
    """The ``mcp.access`` record of one tool completion.

    ``principal`` is the resolved principal (``username``, ``role``,
    ``source``), or None before it is known. ``headers``, ``arguments`` and
    ``query`` are accepted so call sites can pass what they hold, and are
    never copied: the record is built from the whitelisted fields only.
    """
    del headers, arguments, query  # never logged
    username = _principal_field(principal, "username")
    return {
        "event": EVENT,
        "channel": CHANNEL,
        "tool": _text(tool),
        "mcp_server": _text(mcp_server),
        "mcp_client": _text(mcp_client),
        "request_id": _text(request_id),
        "principal": _text(username) if username else "anonymous",
        "role": _text(_principal_field(principal, "role")),
        "source": _text(_principal_field(principal, "source")),
        "outcome": _text(outcome),
        "category": _text(category),
        "status": _integer(status),
        "duration_ms": _integer(duration_ms),
        "level": "warning" if outcome in WARNING_OUTCOMES else "info",
    }


def emit(record: dict, stream=None) -> None:
    """Write one record as one JSON line on stdout; never raises."""
    try:
        out = stream if stream is not None else sys.stdout
        out.write(json.dumps(record) + "\n")
        out.flush()
    except Exception:  # noqa: BLE001 — logging never fails a tool call
        pass
