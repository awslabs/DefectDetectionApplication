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

"""Per-execution run-log capture for the WorkflowExecutor (Requirement 2).

``RunLogCapture`` attaches a bounded ``RotatingFileHandler`` to the
``workflow_engine`` and ``gstreamer`` loggers for the duration of a run,
so everything the run logs — the rendered launch string, the per-node
resolution/injection messages, the completion tags, and (on failure) the
underlying element/backend error the ``gstreamer`` logger emits — lands in
one per-execution file (Requirements 2.1, 2.2, 2.3). The file is size
bounded so it can never grow without limit on the device (Requirement
2.4).

The capture is deliberately *additive*: it only adds a handler to the two
named loggers, never removes existing handlers, never touches the root
logger, and leaves ``propagate`` alone so the Greengrass component log
still receives every message exactly as before (Requirement 2.5). The one
concession is the named loggers' *level*: production LocalServer already
logs at INFO (the launch string shows up in the component log today), so
the level is only lowered to INFO when the effective level would otherwise
drop the run's INFO messages, and it is always restored on exit — the
observed level is unchanged after a run.

Every setup/teardown step is wrapped so a handler error can never fail a
run (Requirement 2.6): capture is best-effort and contained, and the file
handler is always detached and closed even if the run raises.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

#: The loggers whose records a run's log should capture. ``workflow_engine``
#: carries the executor's own launch string / resolution / completion
#: messages; ``gstreamer`` carries the element/backend errors a failure's
#: root cause is logged under (Requirement 2.3).
CAPTURED_LOGGERS: Sequence[str] = ("workflow_engine", "gstreamer")

#: Size bound for a single run's log (Requirement 2.4). A rotating handler
#: caps the live file at this many bytes and keeps one rolled-over backup,
#: so a run's on-disk log is bounded at roughly ``maxBytes * (backupCount +
#: 1)`` regardless of how chatty the pipeline is.
DEFAULT_MAX_BYTES = 512 * 1024
DEFAULT_BACKUP_COUNT = 1

#: One line per record: timestamp, level, logger name, message — enough for
#: the log viewer (Requirement 6) to make a failure's error evident.
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RunLogCapture:
    """Capture the ``workflow_engine``/``gstreamer`` logs of one run to a file.

    Usable as a context manager::

        with RunLogCapture(execution_id, log_path):
            ...  # run the pipeline

    or explicitly via :meth:`start` / :meth:`stop` when the surrounding
    method already owns a ``try/finally`` (the executor uses this form so
    the handler is torn down in the same ``finally`` that closes the run's
    session). Either way the handler is always detached and closed, and any
    error in setup or teardown is swallowed so the run never fails because
    of log capture (Requirement 2.6)."""

    def __init__(
        self,
        execution_id: str,
        log_path: str,
        logger_names: Sequence[str] = CAPTURED_LOGGERS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
    ) -> None:
        self.execution_id = execution_id
        self.log_path = log_path
        self._logger_names = tuple(logger_names)
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._handler: Optional[logging.Handler] = None
        # (logger, original_level) pairs to restore on stop; only loggers
        # whose level we actually lowered are recorded.
        self._restored_levels: list = []

    # -- explicit lifecycle -------------------------------------------------

    def start(self) -> "RunLogCapture":
        """Attach the bounded file handler to the named loggers.

        Best-effort and contained: any failure (undeletable dir, handler
        construction error) is logged to the module logger and swallowed,
        leaving the capture inert so the run proceeds normally."""
        try:
            parent = os.path.dirname(self.log_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            handler = RotatingFileHandler(
                self.log_path,
                maxBytes=self._max_bytes,
                backupCount=self._backup_count,
                encoding="utf-8",
            )
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(logging.Formatter(_LOG_FORMAT))
            for name in self._logger_names:
                lg = logging.getLogger(name)
                lg.addHandler(handler)
                # Only ensure INFO records are created when the effective
                # level would otherwise suppress them; restore on stop so
                # the level is observably unchanged after the run
                # (Requirement 2.5). Production already logs at INFO, so
                # this is a no-op there.
                if lg.getEffectiveLevel() > logging.INFO:
                    self._restored_levels.append((lg, lg.level))
                    lg.setLevel(logging.INFO)
            self._handler = handler
        except Exception:  # noqa: BLE001 - log capture is best-effort (R2.6)
            logger.exception(
                "Could not start run-log capture for execution %s at %s; "
                "the run continues without a captured log",
                self.execution_id,
                self.log_path,
            )
            self._safe_teardown()
        return self

    def stop(self) -> None:
        """Detach and close the file handler and restore any lowered level.

        Always safe to call (idempotent), even if :meth:`start` failed or
        was never called."""
        self._safe_teardown()

    def _safe_teardown(self) -> None:
        for lg, level in self._restored_levels:
            try:
                lg.setLevel(level)
            except Exception:  # noqa: BLE001 - contained (R2.6)
                pass
        self._restored_levels = []
        handler = self._handler
        self._handler = None
        if handler is None:
            return
        for name in self._logger_names:
            try:
                logging.getLogger(name).removeHandler(handler)
            except Exception:  # noqa: BLE001 - contained (R2.6)
                pass
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - contained (R2.6)
            pass

    # -- context manager ----------------------------------------------------

    def __enter__(self) -> "RunLogCapture":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.stop()
        # Never suppress the run's own exceptions.
        return False
