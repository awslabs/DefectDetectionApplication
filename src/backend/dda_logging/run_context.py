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
"""The ``continuous_run`` logging context (rtsp-rtmp-stream-cameras
Requirement 12.6; design component 14).

A continuous workflow can run several times a second, and every run logs
its launch string, frame feed, node timings and completion at INFO. Those
lines belong in the run's own ``run.log`` (``RunLogCapture``), not in the
component log. The Continuous_Runner executes each run inside
:func:`continuous_run`, and :class:`ContinuousRunLogFilter`, installed on
the component log handlers only, drops the INFO and DEBUG records of the
run's loggers while the context is set. Warnings and errors always pass,
and ``RunLogCapture`` attaches its own handler to the loggers, so the
run's log keeps every line.

The context is a :mod:`contextvars` variable, so it applies to the thread
that runs the run (and to nothing else): a concurrent manual run on
another thread logs exactly as before.
"""
import contextlib
import contextvars
import logging
from typing import Iterator, Optional

#: The registration id of the continuous run executing on this thread.
CONTINUOUS_RUN: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "continuous_run", default=None)

#: The logger hierarchies whose per-run INFO lines a continuous run quiets.
QUIETED_LOGGERS = ("workflow_engine", "gstreamer")


@contextlib.contextmanager
def continuous_run(registration_id: str) -> Iterator[None]:
    """Mark the current thread's records as a continuous run's."""
    token = CONTINUOUS_RUN.set(str(registration_id))
    try:
        yield
    finally:
        CONTINUOUS_RUN.reset(token)


def _quieted(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in QUIETED_LOGGERS)


class ContinuousRunLogFilter(logging.Filter):
    """Drops a continuous run's INFO and DEBUG records from the handler it
    is installed on (see the module docstring)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        if CONTINUOUS_RUN.get() is None:
            return True
        return not _quieted(record.name or "")


def install_continuous_run_filter(handlers) -> None:
    """Add one :class:`ContinuousRunLogFilter` to each handler (idempotent)."""
    for handler in handlers:
        if not any(isinstance(existing, ContinuousRunLogFilter) for existing in handler.filters):
            handler.addFilter(ContinuousRunLogFilter())
