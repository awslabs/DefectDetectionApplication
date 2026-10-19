#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""The log tap and the level guard (workflow-debug-mode design §5.1
``logtap.py``).

**On the emitting thread** :class:`DebugLogHandler.emit` does the minimum:
it reads the two context variables (``telemetry.hooks.CURRENT_EXECUTION`` and
``dda_logging.run_context.CONTINUOUS_RUN``, per-thread context that can only
be read where the record is emitted), formats the message with
``record.getMessage()`` inside ``try/except`` (cut at 4 KB), appends one tuple
to a ``deque(maxlen=5000)`` inbox (evictions counted ``tapDrops``) and
returns. No regex, no substring search, no redaction, no traceback
formatting there; the benchmark budget is 50 us per record at p95.

**On the agent thread** :meth:`LogTap.drain_inbox` attributes every entry,
in this order, where rules (1) and (2) are terminal:

1. the captured ``CURRENT_EXECUTION`` -> that execution's Pair; a Pair
   without an active session drops the line (``otherPairLines``);
2. the captured ``CONTINUOUS_RUN`` -> that registration's Pair; likewise;
3. the message names an open execution id or the registration id of a
   session Pair -> that run or Pair;
4. exactly one session Pair has an open run -> that run (the window
   attribution ``run.log`` itself applies);
5. exactly one session is active on the device -> ``device``;
   otherwise the line is counted ``unattributedLines`` and dropped.

A line attributed to a run is skipped when its logger is one of
``run_log.CAPTURED_LOGGERS`` (or a child) **and** its level is INFO or above,
because exactly those lines are in ``run.log`` and travel inside the ``run``
record as ``src: "runlog"``; every other run line (the DEBUG lines of those
loggers during a DEBUG session, every ``dda_triton`` line) goes to the run's
tap buffer (500 lines / 64 KB, oldest dropped and counted). A line attributed
to a Pair without a run or to the device becomes a ``log`` record
(``between_runs`` / ``device``). Redaction, the 2 KB cap and the ``exc_info``
formatting happen here.

:class:`DebugLevelGuard` lowers ``workflow_engine``, ``gstreamer`` and
``dda_triton`` to DEBUG while a DEBUG session is active, remembering what it
lowered (``lowered: {name: saved level}``), and keeps ``run.log`` and the
component log byte-identical by installing :class:`DebugOnlyFilter(lowered)`
on every handler attached to the three loggers and their ancestors (except
the tap's own) and on every later ``RunLogCapture`` handler through
``run_log.CAPTURE_FILTERS``. It re-asserts DEBUG each tick (adopting an
externally restored level as the new saved level) and restores a saved level
at session end only while the logger is still at DEBUG. When a handler
refuses the filter nothing is lowered and ``levelDowngraded`` is reported.
"""

import collections
import logging
import threading
import time
import traceback
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

from dda_logging.run_context import CONTINUOUS_RUN
from workflow_engine import run_log
from workflow_engine.telemetry import hooks as telemetry_hooks
from workflow_engine.telemetry.debug.redact import redact_structure

logger = logging.getLogger(__name__)

__all__ = ["DebugLogHandler", "LogTap", "TapBuffer", "DebugLevelGuard", "DebugOnlyFilter",
           "TAPPED_LOGGERS", "INBOX_MAXLEN", "EMIT_MESSAGE_CAP", "MESSAGE_CAP",
           "BUFFER_MAX_LINES", "BUFFER_MAX_BYTES"]

TAPPED_LOGGERS = ("workflow_engine", "gstreamer", "dda_triton")
INBOX_MAXLEN = 5000
#: ``drain_inbox`` is also woken when this many entries are queued.
INBOX_WAKE_AT = 200
EMIT_MESSAGE_CAP = 4096
MESSAGE_CAP = 2048
EXC_TAIL = 2048
BUFFER_MAX_LINES = 500
BUFFER_MAX_BYTES = 64 * 1024

ATTRIBUTION_BETWEEN_RUNS = "between_runs"
ATTRIBUTION_DEVICE = "device"

#: The telemetry agents' own loggers live under ``workflow_engine`` and so
#: reach the tap; their lines are never records (they would feed back into
#: the stream they describe) and are dropped at the drain, uncounted.
SELF_LOGGER_PREFIX = "workflow_engine.telemetry"

#: (created, levelno, name, message, thread_ident, exc_info, execution, continuous)
TapEntry = Tuple[float, int, str, str, int, Any, Any, Any]


def _is_under(name: str, prefixes: Iterable[str]) -> bool:
    for prefix in prefixes:
        if name == prefix or name.startswith(prefix + "."):
            return True
    return False


class DebugLogHandler(logging.Handler):
    """See the module docstring (the emitting-thread half)."""

    def __init__(self, inbox: Optional[Deque[TapEntry]] = None,
                 wake: Optional[threading.Event] = None) -> None:
        super().__init__(level=logging.DEBUG)
        self.inbox: Deque[TapEntry] = inbox if inbox is not None else collections.deque(
            maxlen=INBOX_MAXLEN)
        self.wake = wake
        self.tap_drops = 0
        self.emitted = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            execution = telemetry_hooks.CURRENT_EXECUTION.get()
            continuous = CONTINUOUS_RUN.get()
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001 - a bad format string
                message = str(record.msg)
            if len(message) > EMIT_MESSAGE_CAP:
                message = message[:EMIT_MESSAGE_CAP]
            inbox = self.inbox
            maxlen = inbox.maxlen
            if maxlen is not None and len(inbox) >= maxlen:
                self.tap_drops += 1
            inbox.append((record.created, record.levelno, record.name, message,
                          threading.get_ident(), record.exc_info, execution, continuous))
            self.emitted += 1
            wake = self.wake
            if wake is not None and len(inbox) >= INBOX_WAKE_AT and not wake.is_set():
                wake.set()
        except Exception:  # noqa: BLE001 - the record is dropped; the tap stays
            self.handleError(record)


class TapBuffer:
    """One run's tap lines: at most 500 lines / 64 KB, oldest dropped and
    counted."""

    def __init__(self, max_lines: int = BUFFER_MAX_LINES, max_bytes: int = BUFFER_MAX_BYTES) -> None:
        self._lines: Deque[dict] = collections.deque()
        self._bytes = 0
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        self.dropped = 0
        self.dropped_bytes = 0

    def append(self, line: dict) -> None:
        size = len(line.get("message", "")) + 64
        self._lines.append(line)
        self._bytes += size
        while self._lines and (len(self._lines) > self.max_lines or self._bytes > self.max_bytes):
            oldest = self._lines.popleft()
            oldest_size = len(oldest.get("message", "")) + 64
            self._bytes -= oldest_size
            self.dropped += 1
            self.dropped_bytes += oldest_size

    def __len__(self) -> int:
        return len(self._lines)

    def snapshot(self) -> Tuple[List[dict], int, int]:
        return list(self._lines), self.dropped, self.dropped_bytes


class LogTap:
    """The agent-thread half (see the module docstring).

    ``sessions`` is the :class:`~sessions.SessionManager`; ``counters`` the
    device-wide tap counters (``tapDrops``, ``unattributedLines``,
    ``otherPairLines``); ``secrets`` a zero-argument callable returning the
    literals to mask.
    """

    def __init__(self, sessions: Any, counters: Optional[Dict[str, int]] = None,
                 secrets: Optional[Callable[[], Iterable[str]]] = None,
                 wake: Optional[threading.Event] = None) -> None:
        self.sessions = sessions
        self.counters: Dict[str, int] = counters if counters is not None else {}
        for key in ("tapDrops", "unattributedLines", "otherPairLines", "tapLines"):
            self.counters.setdefault(key, 0)
        self._secrets = secrets or (lambda: ())
        self.handler = DebugLogHandler(wake=wake)
        self.attached = False
        #: execution_id -> (workflow_id, TapBuffer) of the runs still open.
        self.open_runs: Dict[str, Tuple[str, TapBuffer]] = {}
        self._lock = threading.Lock()

    # -- attachment -------------------------------------------------------------

    def attach(self) -> None:
        if self.attached:
            return
        for name in TAPPED_LOGGERS:
            logging.getLogger(name).addHandler(self.handler)
        self.attached = True

    def detach(self) -> None:
        if not self.attached:
            return
        for name in TAPPED_LOGGERS:
            try:
                logging.getLogger(name).removeHandler(self.handler)
            except Exception:  # noqa: BLE001
                pass
        self.attached = False

    # -- open runs ----------------------------------------------------------------

    def open_run(self, execution_id: str, workflow_id: str) -> TapBuffer:
        with self._lock:
            entry = self.open_runs.get(execution_id)
            if entry is None:
                entry = (workflow_id, TapBuffer())
                self.open_runs[execution_id] = entry
            return entry[1]

    def take_run(self, execution_id: str) -> Optional[TapBuffer]:
        """Remove and return the run's buffer (after assembly)."""
        with self._lock:
            entry = self.open_runs.pop(execution_id, None)
        return entry[1] if entry is not None else None

    def buffer_for(self, execution_id: str) -> Optional[TapBuffer]:
        with self._lock:
            entry = self.open_runs.get(execution_id)
        return entry[1] if entry is not None else None

    # -- the drain --------------------------------------------------------------

    def drain_inbox(self) -> List[Tuple[Any, dict]]:
        """Attribute every queued entry. Returns the ``(session, log_record)``
        pairs for the lines that belong to no run; run lines land in the
        open runs' buffers."""
        inbox = self.handler.inbox
        drops = self.handler.tap_drops
        if drops:
            self.handler.tap_drops = 0
            self.counters["tapDrops"] += drops
        if self.handler.wake is not None:
            self.handler.wake.clear()
        pending: List[TapEntry] = []
        while True:
            try:
                pending.append(inbox.popleft())
            except IndexError:
                break
        if not pending:
            return []
        secrets = self._safe_secrets()
        active = self.sessions.active_sessions()
        active_by_workflow = {s.workflow_id: s for s in active}
        results: List[Tuple[Any, dict]] = []
        for entry in pending:
            try:
                self._attribute(entry, active, active_by_workflow, secrets, results)
            except Exception:  # noqa: BLE001 - one bad entry never stops the drain
                logger.debug("Debug log tap: could not attribute a line", exc_info=True)
        return results

    def _safe_secrets(self) -> Tuple[str, ...]:
        try:
            return tuple(s for s in (self._secrets() or ()) if isinstance(s, str))
        except Exception:  # noqa: BLE001
            return ()

    def _attribute(self, entry: TapEntry, active: list, active_by_workflow: dict,
                   secrets: Tuple[str, ...], results: list) -> None:
        created, levelno, name, message, _thread, exc_info, execution, continuous = entry
        if _is_under(name or "", (SELF_LOGGER_PREFIX,)):
            return  # the agents' own lines never become records
        session = None
        execution_id: Optional[str] = None
        attribution: Optional[str] = None

        if execution is not None:  # rule 1, terminal
            try:
                registration_id, workflow_id, execution_id = execution
            except (TypeError, ValueError):
                registration_id = workflow_id = execution_id = None
            session = active_by_workflow.get(workflow_id)
            if session is None:
                self.counters["otherPairLines"] += 1
                return
            if execution_id is not None:
                self.open_run(execution_id, workflow_id)
        elif continuous is not None:  # rule 2, terminal
            session = self.sessions.session_for_registration(continuous)
            if session is None or not session.is_active:
                self.counters["otherPairLines"] += 1
                return
            attribution = ATTRIBUTION_BETWEEN_RUNS
        else:
            with self._lock:
                open_runs = list(self.open_runs.items())
            # rule 3: the message names an open execution id or a Pair's registration id
            for candidate_id, (workflow_id, _buffer) in open_runs:
                if candidate_id and candidate_id in message and workflow_id in active_by_workflow:
                    session = active_by_workflow[workflow_id]
                    execution_id = candidate_id
                    break
            if session is None:
                for candidate in active:
                    if candidate.registration_id and candidate.registration_id in message:
                        session = candidate
                        attribution = ATTRIBUTION_BETWEEN_RUNS
                        break
            if session is None:
                # rule 4: exactly one session Pair has an open run
                runs_of_sessions = [(eid, wf) for eid, (wf, _b) in open_runs if wf in active_by_workflow]
                pairs = {wf for _eid, wf in runs_of_sessions}
                if len(pairs) == 1 and len(runs_of_sessions) == 1:
                    execution_id, workflow_id = runs_of_sessions[0]
                    session = active_by_workflow[workflow_id]
            if session is None:
                # rule 5: exactly one session is active on the device
                if len(active) == 1:
                    session = active[0]
                    attribution = ATTRIBUTION_DEVICE
                else:
                    self.counters["unattributedLines"] += 1
                    return

        if execution_id is not None and attribution is None:
            # A run line: skipped when run.log already carries it.
            if levelno >= logging.INFO and _is_under(name, run_log.CAPTURED_LOGGERS):
                return
            buffer = self.open_run(execution_id, session.workflow_id)
            buffer.append(self._line(created, levelno, name, message, exc_info, secrets))
            self.counters["tapLines"] += 1
            return
        record = self._line(created, levelno, name, message, exc_info, secrets)
        record["type"] = "log"
        record["attribution"] = attribution or ATTRIBUTION_BETWEEN_RUNS
        record.pop("src", None)
        results.append((session, record))

    @staticmethod
    def _line(created: float, levelno: int, name: str, message: str, exc_info: Any,
              secrets: Tuple[str, ...]) -> dict:
        if exc_info:
            try:
                if isinstance(exc_info, tuple):
                    formatted = "".join(traceback.format_exception(*exc_info))
                else:
                    formatted = str(exc_info)
                message = message + "\n" + formatted[-EXC_TAIL:]
            except Exception:  # noqa: BLE001
                pass
        message = redact_structure(message, secrets)
        if not isinstance(message, str):
            message = str(message)
        if len(message) > MESSAGE_CAP:
            message = message[:MESSAGE_CAP]
        return {
            "ts": int(created * 1000),
            "level": logging.getLevelName(levelno),
            "logger": name,
            "message": message,
            "src": "tap",
        }


class DebugOnlyFilter(logging.Filter):
    """Drops a record only when its logger is in, or a child of, a logger the
    guard lowered **and** its level is below INFO; everything else passes."""

    def __init__(self, lowered: Dict[str, int]) -> None:
        super().__init__()
        self.lowered = lowered

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.INFO:
            return True
        return not _is_under(record.name or "", self.lowered)


class DebugLevelGuard:
    """See the module docstring. ``tap_handler`` is the tap's own handler,
    which never receives the filter."""

    def __init__(self, tap_handler: Optional[logging.Handler] = None,
                 logger_names: Iterable[str] = TAPPED_LOGGERS) -> None:
        self.tap_handler = tap_handler
        self.logger_names = tuple(logger_names)
        self.lowered: Dict[str, int] = {}
        self.filter = DebugOnlyFilter(self.lowered)
        self.active = False
        self.level_downgraded = False
        self._filtered_handlers: List[logging.Handler] = []

    def _handlers_in_scope(self) -> List[logging.Handler]:
        handlers: List[logging.Handler] = []
        seen = set()
        for name in self.logger_names:
            lg: Optional[logging.Logger] = logging.getLogger(name)
            while lg is not None:
                for handler in lg.handlers:
                    if handler is self.tap_handler or id(handler) in seen:
                        continue
                    seen.add(id(handler))
                    handlers.append(handler)
                if not lg.propagate:
                    break
                lg = lg.parent
        return handlers

    def apply(self) -> bool:
        """Lower the loggers for a DEBUG session. Returns False (and sets
        ``level_downgraded``) when a handler refused the filter; nothing is
        lowered then."""
        if self.active:
            return not self.level_downgraded
        self.active = True
        candidates: Dict[str, int] = {}
        for name in self.logger_names:
            lg = logging.getLogger(name)
            if lg.getEffectiveLevel() > logging.DEBUG:
                candidates[name] = lg.level
        installed: List[logging.Handler] = []
        try:
            for handler in self._handlers_in_scope():
                handler.addFilter(self.filter)
                installed.append(handler)
        except Exception:  # noqa: BLE001 - a handler refused: run at INFO
            for handler in installed:
                try:
                    handler.removeFilter(self.filter)
                except Exception:  # noqa: BLE001
                    pass
            self.level_downgraded = True
            logger.warning("Debug level guard: a log handler refused the DEBUG-only filter; the "
                           "session runs at INFO so run.log is never changed")
            return False
        self._filtered_handlers = installed
        if self.filter not in run_log.CAPTURE_FILTERS:
            run_log.CAPTURE_FILTERS.append(self.filter)
        self.lowered.clear()
        self.lowered.update(candidates)
        for name in candidates:
            logging.getLogger(name).setLevel(logging.DEBUG)
        return True

    def reassert(self) -> None:
        """Each tick: a lowered logger no longer at DEBUG was changed by
        someone else (``RunLogCapture.stop()`` restoring its saved level);
        adopt that level as the new saved one and lower again."""
        if not self.active or self.level_downgraded:
            return
        for name in list(self.lowered):
            lg = logging.getLogger(name)
            if lg.level != logging.DEBUG:
                self.lowered[name] = lg.level
                lg.setLevel(logging.DEBUG)
                logger.debug("Debug level guard: logger %s was set to %s during the session; "
                             "re-asserting DEBUG and remembering %s as its level",
                             name, logging.getLevelName(self.lowered[name]),
                             logging.getLevelName(self.lowered[name]))

    def restore(self) -> None:
        """At the end of the last DEBUG session: restore each lowered logger
        only while it is still at DEBUG (the guard's own setting); remove
        the filter everywhere."""
        if not self.active:
            return
        for name, level in list(self.lowered.items()):
            lg = logging.getLogger(name)
            if lg.level == logging.DEBUG:
                lg.setLevel(level)
            else:
                logger.info("Debug level guard: logger %s is at %s, not DEBUG; leaving it alone",
                            name, logging.getLevelName(lg.level))
        self.lowered.clear()
        for handler in self._filtered_handlers:
            try:
                handler.removeFilter(self.filter)
            except Exception:  # noqa: BLE001
                pass
        self._filtered_handlers = []
        try:
            while self.filter in run_log.CAPTURE_FILTERS:
                run_log.CAPTURE_FILTERS.remove(self.filter)
        except Exception:  # noqa: BLE001
            pass
        self.active = False
        self.level_downgraded = False
