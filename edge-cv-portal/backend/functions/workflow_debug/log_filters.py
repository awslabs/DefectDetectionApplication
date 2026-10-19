"""Server-side filters of ``GET /debug/usecases/{usecase_id}/log`` and the
``next`` termination rule (workflow-debug-mode design §7.2, R6.3).

The API reads one page from CloudWatch Logs (``GetLogEvents`` without an
``execution_id``, ``FilterLogEvents`` with one), turns each event into
``{id, writeTs, receivedMs, ts, record}`` and applies the remaining filters on
the record JSON: ``node_id`` on ``nodes[].nodeId`` (and a ``drop`` record's
own ``nodeId``), ``level``, ``type``, and ``ts_from``/``ts_to`` on the
record's own ``ts``.

``GetLogEvents`` never returns a null token: at the end of the stream it
returns the token it was given, so ``next_token`` yields ``None`` on a
same-token or empty page; ``FilterLogEvents`` omits ``nextToken`` at the end.
"""
import json
from typing import Any, Dict, Iterable, List, Optional

__all__ = ["LEVELS", "TYPES", "to_answer_event", "to_answer_events", "apply", "next_token"]

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
TYPES = ("run", "log", "drop", "session")


def to_answer_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """One CloudWatch event -> ``{id, writeTs, receivedMs, ts, record}``. A
    message that is not a JSON object is shown raw (``type: "raw"``)."""
    message = event.get("message")
    record: Any = None
    if isinstance(message, str):
        try:
            record = json.loads(message)
        except ValueError:
            record = None
    if not isinstance(record, dict):
        record = {"type": "raw", "message": message if isinstance(message, str) else str(message)}
    write_ts = record.get("writeTs")
    if not isinstance(write_ts, int) or isinstance(write_ts, bool):
        write_ts = event.get("timestamp")
    return {
        "id": record.get("id"),
        "writeTs": write_ts,
        "receivedMs": record.get("receivedMs"),
        "ts": record.get("ts"),
        "record": record,
    }


def to_answer_events(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [to_answer_event(event) for event in events]


def _node_ids(record: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    nodes = record.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict) and node.get("nodeId") is not None:
                ids.append(str(node["nodeId"]))
    if record.get("nodeId") is not None:
        ids.append(str(record["nodeId"]))
    return ids


def apply(events: Iterable[Dict[str, Any]], node_id: Optional[str] = None,
          level: Optional[str] = None, type: Optional[str] = None,  # noqa: A002 - the query parameter's name
          ts_from: Optional[int] = None, ts_to: Optional[int] = None) -> List[Dict[str, Any]]:
    """The answer events that pass every given filter."""
    wanted_level = level.upper() if isinstance(level, str) and level else None
    kept = []
    for event in events:
        record = event.get("record") if isinstance(event.get("record"), dict) else {}
        if type and record.get("type") != type:
            continue
        if wanted_level is not None:
            have = record.get("level")
            if not isinstance(have, str) or have.upper() != wanted_level:
                continue
        if node_id and node_id not in _node_ids(record):
            continue
        ts = record.get("ts")
        if ts_from is not None or ts_to is not None:
            if not isinstance(ts, int) or isinstance(ts, bool):
                continue
            if ts_from is not None and ts < ts_from:
                continue
            if ts_to is not None and ts > ts_to:
                continue
        kept.append(event)
    return kept


def next_token(request_next: Optional[str], response: Dict[str, Any], api: str) -> Optional[str]:
    """``None`` when the page is the last one.

    ``api == 'get'`` (``GetLogEvents``): the response's ``nextForwardToken``,
    unless it equals the request's token or the page carries no events.
    ``api == 'filter'`` (``FilterLogEvents``): the response's ``nextToken``
    when present, else ``None``."""
    if api == "get":
        token = response.get("nextForwardToken")
        if not token or token == request_next or not response.get("events"):
            return None
        return token
    if api == "filter":
        return response.get("nextToken") or None
    raise ValueError("unknown log read api: {0!r}".format(api))
