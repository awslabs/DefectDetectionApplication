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

"""The one registration source of the telemetry agents (dashboards design
§5.2 ``registry_view.py``).

:class:`RegistrationView` subscribes to ``watcher.registrations_listeners``
(through ``runtime.get_watcher()``, exactly as the trigger runtime and the
continuous manager do) with one immediate read, and keeps an immutable
snapshot ``{registration_id: RegistrationInfo}`` of the ``WorkflowRegistration``
rows whose status is ``registered``. ``models`` is
``continuous_runner.document_model_names(document)`` over the compiled
document, read once per registration id through
``stream_leases._read_document(artifact_path)`` and cached until the id
leaves the snapshot; ``has_triton = bool(models)``. When the watcher is not
available the view re-reads the rows every 30 s (logged once) and adopts the
watcher as soon as it appears.

:func:`get_or_create` builds the one process-wide instance with the engine
defaults, so both agents share one subscription and one snapshot. The view
holds no engine state of its own and never writes.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["RegistrationInfo", "RegistrationView", "get_or_create"]

#: The fallback re-read interval when no watcher is available (seconds).
FALLBACK_POLL_S = 30.0


@dataclass(frozen=True)
class RegistrationInfo:
    workflow_id: str
    version: str
    models: Tuple[str, ...]
    has_triton: bool


class RegistrationView:
    """See the module docstring.

    ``session_factory()`` returns a SQLAlchemy session over the engine's
    tables; ``watcher_provider()`` returns the ``WorkflowWatcher`` or
    ``None``; ``document_reader(artifact_path)`` returns the compiled
    document or ``None``. ``clock`` and ``fallback_poll_s`` are injectable
    for tests; the fallback poll thread is only started when the provider
    returns ``None``.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        watcher_provider: Callable[[], Any],
        document_reader: Callable[[str], Optional[dict]],
        *,
        clock: Callable[[], float] = time.monotonic,
        fallback_poll_s: float = FALLBACK_POLL_S,
    ) -> None:
        self._session_factory = session_factory
        self._watcher_provider = watcher_provider
        self._document_reader = document_reader
        self._clock = clock
        self._fallback_poll_s = float(fallback_poll_s)
        self._lock = threading.Lock()
        self._snapshot: Dict[str, RegistrationInfo] = {}
        self._models_cache: Dict[str, Tuple[str, ...]] = {}
        self._warned_unreadable: set = set()
        self._listeners: List[Callable[[FrozenSet[str], FrozenSet[str]], None]] = []
        self._watcher = None
        self._subscribed = False
        self._fallback_logged = False
        self._stop = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._started = False
        self._last_read_error_logged: Optional[float] = None
        self.reads = 0

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> "RegistrationView":
        """Subscribe to the watcher (or start the fallback poll) and read
        once. Idempotent."""
        with self._lock:
            if self._started:
                return self
            self._started = True
        self._attach_watcher()
        self.refresh()
        return self

    def stop(self) -> None:
        self._stop.set()
        watcher = self._watcher
        if watcher is not None:
            try:
                watcher.registrations_listeners.remove(self._on_watcher_notification)
            except (ValueError, AttributeError):
                pass

    def _attach_watcher(self) -> bool:
        """Subscribe to the watcher when the provider returns one. Returns
        whether the view is subscribed afterwards."""
        if self._subscribed:
            return True
        try:
            watcher = self._watcher_provider()
        except Exception:  # noqa: BLE001 - treated as "no watcher"
            logger.debug("RegistrationView: watcher provider raised", exc_info=True)
            watcher = None
        if watcher is None:
            if not self._fallback_logged:
                self._fallback_logged = True
                logger.info(
                    "RegistrationView: no workflow watcher is available; re-reading the "
                    "registrations every %.0f s until one appears", self._fallback_poll_s,
                )
            self._start_fallback_poll()
            return False
        listeners = getattr(watcher, "registrations_listeners", None)
        if listeners is None:
            if not self._fallback_logged:
                self._fallback_logged = True
                logger.warning("RegistrationView: the watcher has no registrations_listeners; "
                               "re-reading every %.0f s", self._fallback_poll_s)
            self._start_fallback_poll()
            return False
        listeners.append(self._on_watcher_notification)
        self._watcher = watcher
        self._subscribed = True
        return True

    def _start_fallback_poll(self) -> None:
        if self._poll_thread is not None:
            return
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="registration-view-poll", daemon=True
        )
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        while not self._stop.wait(self._fallback_poll_s):
            try:
                if self._attach_watcher():
                    self.refresh()
                    return  # the watcher's notifications take over
                self.refresh()
            except Exception:  # noqa: BLE001 - the poll never dies
                logger.debug("RegistrationView: fallback poll failed", exc_info=True)

    def _on_watcher_notification(self) -> None:
        try:
            self.refresh()
        except Exception:  # noqa: BLE001 - listener containment
            logger.debug("RegistrationView: refresh failed", exc_info=True)

    # -- the snapshot --------------------------------------------------------------

    def snapshot(self) -> Dict[str, RegistrationInfo]:
        """The current ``{registration_id: RegistrationInfo}`` (a copy)."""
        with self._lock:
            return dict(self._snapshot)

    def add_listener(self, callback: Callable[[FrozenSet[str], FrozenSet[str]], None]) -> None:
        """``callback(added_ids, removed_ids)`` after every snapshot change;
        callbacks must enqueue and return."""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def refresh(self) -> Dict[str, RegistrationInfo]:
        """Re-read the registered rows into a new snapshot and notify the
        listeners of the added and removed ids. Never raises."""
        try:
            rows = self._read_rows()
        except Exception:  # noqa: BLE001 - the snapshot keeps its last value
            self._log_read_error()
            return self.snapshot()
        with self._lock:
            self.reads += 1
            previous = self._snapshot
            current: Dict[str, RegistrationInfo] = {}
            for registration_id, workflow_id, version, artifact_path in rows:
                models = self._models_for(registration_id, artifact_path)
                current[registration_id] = RegistrationInfo(
                    workflow_id=str(workflow_id), version=str(version),
                    models=models, has_triton=bool(models),
                )
            for stale in [rid for rid in self._models_cache if rid not in current]:
                self._models_cache.pop(stale, None)
                self._warned_unreadable.discard(stale)
            self._snapshot = current
            added = frozenset(current) - frozenset(previous)
            removed = frozenset(previous) - frozenset(current)
            listeners = list(self._listeners)
        if added or removed:
            for callback in listeners:
                try:
                    callback(added, removed)
                except Exception:  # noqa: BLE001 - listener containment
                    logger.warning("RegistrationView listener %r raised", callback,
                                   exc_info=True)
        return dict(current)

    def _read_rows(self) -> List[Tuple[str, str, str, str]]:
        from workflow_engine.discovery import STATUS_REGISTERED
        from workflow_engine.models import WorkflowRegistration

        session = self._session_factory()
        try:
            rows = (
                session.query(WorkflowRegistration)
                .filter(WorkflowRegistration.status == STATUS_REGISTERED)
                .all()
            )
            return [(row.id, row.workflow_id, row.version, row.artifact_path) for row in rows]
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass

    def _models_for(self, registration_id: str, artifact_path: str) -> Tuple[str, ...]:
        """The cached model names of a registration; read once per id."""
        cached = self._models_cache.get(registration_id)
        if cached is not None:
            return cached
        from workflow_engine.continuous_runner import document_model_names

        document = None
        try:
            document = self._document_reader(artifact_path)
        except Exception:  # noqa: BLE001 - unreadable
            document = None
        if document is None:
            models: Tuple[str, ...] = ()
            if registration_id not in self._warned_unreadable:
                self._warned_unreadable.add(registration_id)
                logger.warning(
                    "RegistrationView: compiled document of %s at %s is unreadable; "
                    "assuming no Triton model", registration_id, artifact_path,
                )
        else:
            try:
                models = tuple(document_model_names(document))
            except Exception:  # noqa: BLE001
                models = ()
        self._models_cache[registration_id] = models
        return models

    def _log_read_error(self) -> None:
        now = self._clock()
        if (self._last_read_error_logged is not None
                and now - self._last_read_error_logged < 60.0):
            return
        self._last_read_error_logged = now
        logger.warning("RegistrationView: could not read the workflow registrations",
                       exc_info=True)


# ---------------------------------------------------------------------------
# The process-wide instance
# ---------------------------------------------------------------------------

_instance_lock = threading.Lock()
_instance: Optional[RegistrationView] = None


def _default_session_factory():
    from dao.sqlite_db.sqlite_db_operations import SessionLocal

    return SessionLocal()


def _default_watcher_provider():
    from workflow_engine import runtime

    return runtime.get_watcher()


def _default_document_reader(artifact_path: str) -> Optional[dict]:
    from workflow_engine import stream_leases

    return stream_leases._read_document(artifact_path)


def get_or_create() -> RegistrationView:
    """The one process-wide view with the engine defaults (``SessionLocal``,
    ``runtime.get_watcher``, ``stream_leases._read_document``), started on
    first call."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = RegistrationView(
                _default_session_factory, _default_watcher_provider, _default_document_reader
            )
            _instance.start()
        return _instance


def _reset_for_tests() -> None:
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.stop()
        _instance = None
