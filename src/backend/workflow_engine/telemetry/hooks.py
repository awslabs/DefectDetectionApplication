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

"""The engine hooks every telemetry agent observes (dashboards design §5.2
``hooks.py``; debug design §5.2).

The workflow engine calls the ``notify_*`` functions at its hook points
(``WorkflowExecutor.execute``'s ``finally``, the trigger gate's discard
points, the continuous runner's counters and model waits, the per-run
readiness gate) and the agents register observers. Every ``notify_*``:

- calls each registered observer with the same arguments, in registration
  order;
- catches every exception an observer raises and logs it at WARNING at
  most once per minute per observer, so a misbehaving observer can never
  fail or slow a run; observers must enqueue and return.

:class:`RunRecord` is the frozen per-run document the executor builds in
its ``finally`` block; :data:`CURRENT_EXECUTION` is the context variable the
executor sets around a run and the debug log tap reads on the emitting
thread.
"""

import contextvars
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "RunRecord",
    "CURRENT_EXECUTION",
    "register_run_observer",
    "notify_run_finished",
    "register_drop_observer",
    "notify_trigger_dropped",
    "register_continuous_observer",
    "notify_continuous_tick",
    "register_model_observer",
    "notify_model_readiness",
    "notify_model_wait",
]

#: ``(registration_id, workflow_id, execution_id)`` of the run executing on
#: this thread, set by ``WorkflowExecutor.execute`` once the registration is
#: loaded and reset in its ``finally``; ``None`` outside a run.
CURRENT_EXECUTION: "contextvars.ContextVar[Optional[Tuple[str, str, str]]]" = (
    contextvars.ContextVar("telemetry_current_execution", default=None)
)

#: At most this many detection items travel in a :class:`RunRecord`; the
#: uncapped length is ``detection_count``.
_MAX_DETECTIONS = 1000

#: The ``source`` value the continuous runner writes into its
#: Trigger_Context (``continuous_runner.CONTINUOUS_SOURCE``; a literal here
#: so this module imports nothing of the engine).
_CONTINUOUS_SOURCE = "continuous"

#: Observer failures are logged at most this often per observer (seconds).
_WARN_INTERVAL_S = 60.0

#: Timing source for the rate limit; module attribute so tests replace it.
_monotonic = time.monotonic


@dataclass(frozen=True)
class RunRecord:
    """One finished run as the executor saw it (dashboards design §5.2).

    ``status`` is the terminal row's status (``completed``/``failed``);
    ``traceback`` is the text the executor captured in the ``except`` block
    that led to the failure, cut to its last 8 KB, ``None`` for refusals
    that raised nothing. The timing stamps are epoch milliseconds except
    ``enqueued_at_s`` (the row's one-second enqueue time); ``run_started_ms``
    is ``None`` for a run refused before the RUNNING transition and so are
    ``setup_ms`` and the latency sample derived from it. ``detections``
    carries at most 1,000 items; ``detection_count`` the uncapped length.
    """

    registration_id: str
    workflow_id: str
    workflow_version: str
    execution_id: str
    status: str
    error: Optional[str]
    failing_node_id: Optional[str]
    traceback: Optional[str]
    capture_id: Optional[str]
    output_dir: Optional[str]
    log_path: Optional[str]
    enqueued_at_s: Optional[int]
    accepted_ms: Optional[int]
    run_started_ms: Optional[int]
    finished_ms: int
    queued_ms: int
    setup_ms: Optional[int]
    readiness_wait_ms: int
    trigger_context: dict
    node_status: Optional[dict]
    tag_values: Optional[dict]
    detections: Optional[list]
    detection_count: int
    continuous: bool

    @classmethod
    def from_execution(
        cls,
        execution: Any,
        registration: Any,
        collector: Any,
        tag_values: Optional[dict],
        trigger_context: Optional[dict],
        enqueued_at_s: Optional[int],
        accepted_ms: Optional[int],
        run_started_ms: Optional[int],
        finished_ms: int,
        readiness_wait_ms: int,
        failure_traceback: Optional[str],
    ) -> "RunRecord":
        """Build the record from the terminal execution row, the
        registration, the node-status collector and the executor's locals.

        ``queued_ms = max(0, accepted_ms - enqueued_at_s * 1000)`` (0 when
        either stamp is missing); ``setup_ms = run_started_ms - accepted_ms``
        or ``None``; ``node_status`` is the collector's map when a collector
        exists, else the row's persisted ``node_status_json`` parsed, else
        ``None``; the detections are the Run_Metadata list under
        ``tag_values["detections"]`` capped at 1,000 with the uncapped count.
        """
        if accepted_ms is None or enqueued_at_s is None:
            queued_ms = 0
        else:
            queued_ms = max(0, int(accepted_ms) - int(enqueued_at_s) * 1000)
        if run_started_ms is None or accepted_ms is None:
            setup_ms: Optional[int] = None
        else:
            setup_ms = int(run_started_ms) - int(accepted_ms)

        node_status: Optional[dict] = None
        if collector is not None:
            try:
                node_status = collector.to_map()
            except Exception:  # noqa: BLE001 - the record is best-effort
                node_status = None
        if node_status is None:
            node_status = _parse_node_status_json(
                getattr(execution, "node_status_json", None)
            )

        detections: Optional[list] = None
        detection_count = 0
        if isinstance(tag_values, dict):
            candidate = tag_values.get("detections")
            if isinstance(candidate, list):
                detection_count = len(candidate)
                detections = list(candidate[:_MAX_DETECTIONS])

        context = trigger_context if isinstance(trigger_context, dict) else {}
        return cls(
            registration_id=str(getattr(registration, "id", "")),
            workflow_id=str(getattr(registration, "workflow_id", "")),
            workflow_version=str(getattr(registration, "version", "")),
            execution_id=str(getattr(execution, "id", "")),
            status=str(getattr(execution, "status", "") or ""),
            error=getattr(execution, "error", None),
            failing_node_id=getattr(execution, "failing_node_id", None),
            traceback=failure_traceback,
            capture_id=getattr(execution, "capture_id", None),
            output_dir=getattr(execution, "output_dir", None),
            log_path=getattr(execution, "log_path", None),
            enqueued_at_s=enqueued_at_s,
            accepted_ms=accepted_ms,
            run_started_ms=run_started_ms,
            finished_ms=int(finished_ms),
            queued_ms=queued_ms,
            setup_ms=setup_ms,
            readiness_wait_ms=int(readiness_wait_ms or 0),
            trigger_context=context,
            node_status=node_status,
            tag_values=tag_values if isinstance(tag_values, dict) else None,
            detections=detections,
            detection_count=detection_count,
            continuous=context.get("source") == _CONTINUOUS_SOURCE,
        )


def _parse_node_status_json(raw: Optional[str]) -> Optional[dict]:
    """The row's persisted node-status map, or ``None`` (no engine import:
    the same tolerant parse ``run_artifacts.parse_node_status`` does)."""
    if not raw:
        return None
    try:
        import json

        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Observer registries
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_run_observers: List[Callable] = []
_drop_observers: List[Callable] = []
_continuous_observers: List[Callable] = []
_model_observers: List[Callable] = []
#: observer id -> monotonic time of its last logged failure.
_last_warned: Dict[int, float] = {}


def _register(registry: List[Callable], observer: Callable) -> None:
    if not callable(observer):
        raise TypeError("observer must be callable")
    with _lock:
        if observer not in registry:
            registry.append(observer)


def _unregister(registry: List[Callable], observer: Callable) -> None:
    with _lock:
        if observer in registry:
            registry.remove(observer)


def _clear_observers() -> None:
    """Forget every observer (tests only)."""
    with _lock:
        del _run_observers[:]
        del _drop_observers[:]
        del _continuous_observers[:]
        del _model_observers[:]
        _last_warned.clear()


def _notify(registry: List[Callable], hook: str, *args: Any) -> None:
    """Call every observer in ``registry``; contain and rate-limit
    failures. Never raises, never blocks beyond the observers' own work."""
    with _lock:
        observers = list(registry)
    for observer in observers:
        try:
            observer(*args)
        except Exception:  # noqa: BLE001 - observers never fail the engine
            _warn_once_per_minute(observer, hook)


def _warn_once_per_minute(observer: Callable, hook: str) -> None:
    try:
        now = _monotonic()
        key = id(observer)
        with _lock:
            last = _last_warned.get(key)
            if last is not None and now - last < _WARN_INTERVAL_S:
                return
            _last_warned[key] = now
        logger.warning(
            "Telemetry observer %r raised in %s; further failures of this "
            "observer are logged at most once per minute",
            observer,
            hook,
            exc_info=True,
        )
    except Exception:  # noqa: BLE001 - logging must never raise into the engine
        pass


# -- run ------------------------------------------------------------------


def register_run_observer(observer: Callable[[RunRecord], None]) -> None:
    """``observer(record)`` for every finished run."""
    _register(_run_observers, observer)


def notify_run_finished(record: RunRecord) -> None:
    _notify(_run_observers, "notify_run_finished", record)


# -- trigger drops ----------------------------------------------------------


def register_drop_observer(
    observer: Callable[[Optional[str], Optional[str], str], None]
) -> None:
    """``observer(registration_id, node_id, reason)`` for every discarded
    trigger firing (``drop_policy`` or ``queue_full``)."""
    _register(_drop_observers, observer)


def notify_trigger_dropped(
    registration_id: Optional[str], node_id: Optional[str], reason: str
) -> None:
    _notify(_drop_observers, "notify_trigger_dropped", registration_id, node_id, reason)


# -- continuous counters ------------------------------------------------------


def register_continuous_observer(
    observer: Callable[[str, str, int], None]
) -> None:
    """``observer(registration_id, key, amount)`` for every continuous
    runner counter increment."""
    _register(_continuous_observers, observer)


def notify_continuous_tick(registration_id: str, key: str, amount: int) -> None:
    _notify(_continuous_observers, "notify_continuous_tick", registration_id, key, amount)


# -- model readiness and waits --------------------------------------------------


def register_model_observer(observer: Callable[..., None]) -> None:
    """``observer(kind, *args)`` where ``kind`` is ``"readiness"`` with
    ``(registration_id, execution_id, model, state, waited_ms, ready)`` or
    ``"wait"`` with ``(registration_id, wait_document_or_None)``."""
    _register(_model_observers, observer)


def notify_model_readiness(
    registration_id: str,
    execution_id: str,
    model: str,
    state: str,
    waited_ms: int,
    ready: bool,
) -> None:
    _notify(
        _model_observers,
        "notify_model_readiness",
        "readiness",
        registration_id,
        execution_id,
        model,
        state,
        waited_ms,
        ready,
    )


def notify_model_wait(registration_id: str, wait_document: Optional[dict]) -> None:
    _notify(_model_observers, "notify_model_wait", "wait", registration_id, wait_document)
