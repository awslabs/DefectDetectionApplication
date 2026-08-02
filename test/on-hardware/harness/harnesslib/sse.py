"""Minimal server-sent-events parser for the Edge_Test_Harness (Req 5.3).

The Text_Generation_API streams ``data: {json}\\n\\n`` frames (one event per
token, then a terminal ``{"done": true}``). This module parses that framing
over a line iterator (``requests.Response.iter_lines``) without pulling in an
SSE dependency:

- ``data:`` lines accumulate into the current event; a blank line dispatches
  it (multiple ``data:`` lines join with newlines, per the SSE spec).
- Comment lines (leading ``:``) and unknown fields (``event:``, ``id:``, ...)
  are ignored, as the spec requires.
- Clean-termination detection: a stream that ends with an undispatched event
  still buffered was truncated mid-event and raises :class:`SseStreamError`,
  so a connection dropped between ``data:`` and its blank line can never be
  mistaken for a complete stream.
"""

from __future__ import annotations

from typing import Iterable, Iterator, List, Union


class SseStreamError(Exception):
    """Raised when an SSE stream is malformed or ends mid-event."""


def _decode(line: Union[bytes, str]) -> str:
    if isinstance(line, bytes):
        return line.decode("utf-8", errors="replace")
    return line


def iter_data_events(lines: Iterable[Union[bytes, str]]) -> Iterator[str]:
    """Yield the ``data`` payload of each complete event in ``lines``.

    ``lines`` is an iterable of newline-stripped lines (bytes or str), as
    produced by ``requests.Response.iter_lines()``.

    :raises SseStreamError: when the stream ends with a partially-received
        event still buffered (truncated stream).
    """
    data_lines: List[str] = []
    for raw in lines:
        line = _decode(raw)
        if line == "":
            # Blank line: dispatch the buffered event, if any.
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue  # comment
        field, sep, value = line.partition(":")
        if field == "data":
            # The spec strips exactly one leading space from the value.
            if sep and value.startswith(" "):
                value = value[1:]
            data_lines.append(value if sep else "")
        # Other fields (event:, id:, retry:, unknown) are ignored.
    if data_lines:
        raise SseStreamError(
            "SSE stream ended mid-event (no terminating blank line); "
            f"buffered data: {data_lines[0][:200]!r}"
        )
