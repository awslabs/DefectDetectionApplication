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
"""The process-wide ``StreamIngestManager`` (rtsp-rtmp-stream-cameras,
design component 11; Requirements 8.1, 8.2, 8.9-8.11).

Sessions are keyed by camera key: ``cfg-<imageSourceId>`` for a configured
stream Image_Source, or ``url-<sha256(normalized URL)[:16]>`` for an
anonymous stream a workflow names by URL alone.

- **Leases.** Every consumer (a viewer's broadcaster backend, a workflow
  registration, a connection test) holds the camera open with a lease. The
  first lease starts the session; the camera never has more than one.
- **Idle grace.** When the last lease is released the session keeps running
  for 30 s, so a lease that arrives within that time reuses it.
- **Session limit.** A lease that would start a session beyond the device
  limit (``settings.py``, default 4) is refused with ``session_limit``,
  naming the limit; existing sessions are not affected.
- **Configuration changes** restart the camera's session at once with the
  new URL, settings and credentials, keeping its leases; a deleted camera's
  session stops.
- **Health listeners** hear every session state change (the Edge_Sync_Agent
  re-reports stream cameras from them).

A supervisor thread ticks every session four times a second.
"""
import functools
import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from stream_ingest import health
from stream_ingest.health import StreamError
from workflow_engine.vendor.workflow_core.stream_url import normalize_stream_url

logger = logging.getLogger(__name__)

CONFIGURED_KEY_PREFIX = "cfg-"
URL_KEY_PREFIX = "url-"

IDLE_GRACE_S = 30.0
SUPERVISOR_INTERVAL_S = 0.25


def camera_key_for_image_source(image_source_id: str) -> str:
    """The session key of a configured stream Image_Source."""
    return CONFIGURED_KEY_PREFIX + str(image_source_id)


def camera_key_for_url(url: str) -> str:
    """The session key of an anonymous stream, from its normalized URL, so
    two spellings of one stream share a session and the key reveals
    nothing about the URL."""
    digest = hashlib.sha256(normalize_stream_url(url).encode("utf-8")).hexdigest()
    return URL_KEY_PREFIX + digest[:16]


def image_source_id_for_key(camera_key: str) -> Optional[str]:
    """The Image_Source id of a configured camera key, else None."""
    if isinstance(camera_key, str) and camera_key.startswith(CONFIGURED_KEY_PREFIX):
        return camera_key[len(CONFIGURED_KEY_PREFIX):] or None
    return None


@dataclass(frozen=True)
class Lease:
    """A consumer's hold on a camera's session."""

    lease_id: str
    camera_key: str
    holder: str


class SessionLimitError(StreamError):
    """A lease refused at the device session limit (Requirement 8.10)."""

    def __init__(self, limit: int):
        super().__init__(
            health.SESSION_LIMIT,
            f"this device already runs its maximum of {limit} stream camera sessions; "
            f"stop a workflow or live view on another stream camera, or raise the limit")
        self.limit = limit


def _default_limit() -> int:
    from stream_ingest.settings import get_device_settings
    return get_device_settings().limits().max_sessions


def _default_capabilities():
    from stream_ingest.capabilities import get_capability_cache
    return get_capability_cache()


def _default_configured_source(image_source_id: str):
    from stream_ingest.sources import configured_source
    return configured_source(image_source_id)


class StreamIngestManager:
    """Leased stream sessions by camera key (see the module docstring)."""

    def __init__(self, *, session_factory: Optional[Callable[..., Any]] = None,
                 max_sessions: Callable[[], int] = _default_limit,
                 capabilities: Optional[Any] = None,
                 configured_source: Callable[[str], Any] = _default_configured_source,
                 clock: Callable[[], float] = time.monotonic,
                 idle_grace_s: float = IDLE_GRACE_S,
                 supervise: bool = True):
        self._lock = threading.RLock()
        self._session_factory = session_factory
        self._max_sessions = max_sessions
        self._capabilities = capabilities
        self._configured_source = configured_source
        self._clock = clock
        self._idle_grace_s = idle_grace_s
        self._supervise = supervise
        #: camera key -> session (``stream_ingest.session.StreamSession``).
        self._sessions: Dict[str, Any] = {}
        self._leases: Dict[str, Dict[str, Lease]] = {}
        self._idle_since: Dict[str, float] = {}
        self._anonymous_sources: Dict[str, Any] = {}
        self._health_listeners: List[Callable[[str, dict], None]] = []
        self._supervisor: Optional[threading.Thread] = None
        self._shutdown = threading.Event()

    # -- capabilities ----------------------------------------------------------

    def _capability_cache(self):
        if self._capabilities is None:
            self._capabilities = _default_capabilities()
        return self._capabilities

    def on_capabilities(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        """Start the probe and call ``listener(capabilities)`` once it
        finished."""
        cache = self._capability_cache()
        cache.start()
        cache.add_listener(listener)

    def capabilities(self, wait_s: float = 0.0) -> Optional[Dict[str, Any]]:
        """The Device_Stream_Capabilities; None while the probe runs, unless
        ``wait_s`` allows waiting for it."""
        cache = self._capability_cache()
        cache.start()
        if wait_s > 0:
            return cache.get(wait_s)
        return cache.peek()

    def _worker_capabilities(self) -> Dict[str, Any]:
        return self._capability_cache().get()

    # -- sessions and leases ---------------------------------------------------

    def _source_provider(self, camera_key: str) -> Callable[[], Any]:
        image_source_id = image_source_id_for_key(camera_key)
        if image_source_id is not None:
            return functools.partial(self._configured_source, image_source_id)

        def anonymous():
            with self._lock:
                source = self._anonymous_sources.get(camera_key)
            if source is None:
                raise StreamError(health.NOT_FOUND, "the stream is no longer in use")
            return source

        return anonymous

    def _new_session(self, camera_key: str):
        from stream_ingest.session import StreamSession

        factory = self._session_factory or StreamSession
        return factory(camera_key, self._source_provider(camera_key), self._worker_capabilities,
                       clock=self._clock, on_health=self._on_session_health)

    def acquire_lease(self, camera_key: str, holder: str, source: Optional[Any] = None) -> Lease:
        """Hold ``camera_key`` open for ``holder``, starting its session if
        needed. ``source`` is required for an anonymous (``url-``) key.

        Raises :class:`SessionLimitError` when a new session would exceed
        the device limit, and ValueError for a key that names no camera.
        """
        created = None
        with self._lock:
            if self._shutdown.is_set():
                raise StreamError(health.NETWORK_ERROR, "the stream service is shutting down")
            session = self._sessions.get(camera_key)
            if session is None:
                if image_source_id_for_key(camera_key) is None:
                    if not (isinstance(camera_key, str) and camera_key.startswith(URL_KEY_PREFIX)):
                        raise ValueError("the camera key names no stream camera")
                    if source is None:
                        raise ValueError("an anonymous stream needs its source")
                limit = int(self._max_sessions())
                if len(self._sessions) >= limit:
                    raise SessionLimitError(limit)
                if source is not None and image_source_id_for_key(camera_key) is None:
                    self._anonymous_sources[camera_key] = source
                session = created = self._new_session(camera_key)
                self._sessions[camera_key] = session
            lease = Lease(uuid.uuid4().hex, camera_key, str(holder))
            leases = self._leases.setdefault(camera_key, {})
            leases[lease.lease_id] = lease
            self._idle_since.pop(camera_key, None)
            session.set_leases(len(leases))
        if created is not None:
            logger.info("Stream session %s started for %s", camera_key, holder)
            created.start()
        self._ensure_supervisor()
        return lease

    def release_lease(self, lease: Optional[Lease]) -> None:
        """Release ``lease``; the last release starts the idle grace. Safe
        to call twice and for a lease of a deleted camera."""
        if lease is None:
            return
        with self._lock:
            leases = self._leases.get(lease.camera_key)
            if not leases or leases.pop(lease.lease_id, None) is None:
                return
            session = self._sessions.get(lease.camera_key)
            if session is not None:
                session.set_leases(len(leases))
            if not leases:
                self._idle_since[lease.camera_key] = self._clock()

    def lease_count(self, camera_key: str) -> int:
        with self._lock:
            return len(self._leases.get(camera_key) or {})

    def session_keys(self) -> List[str]:
        with self._lock:
            return sorted(self._sessions)

    def session(self, camera_key: str):
        with self._lock:
            return self._sessions.get(camera_key)

    def _remove_locked(self, camera_key: str):
        self._leases.pop(camera_key, None)
        self._idle_since.pop(camera_key, None)
        self._anonymous_sources.pop(camera_key, None)
        return self._sessions.pop(camera_key, None)

    def tick(self, now: Optional[float] = None) -> None:
        """Stop sessions whose idle grace ran out, then tick every session."""
        now = self._clock() if now is None else now
        expired = []
        with self._lock:
            for camera_key, since in list(self._idle_since.items()):
                if now - since >= self._idle_grace_s and not self._leases.get(camera_key):
                    session = self._remove_locked(camera_key)
                    if session is not None:
                        expired.append(session)
            sessions = list(self._sessions.values())
        for session in expired:
            session.stop("no lease for the idle grace")
        for session in sessions:
            try:
                session.tick(now)
            except Exception:  # noqa: BLE001 - one camera must not stop the others
                logger.exception("Stream session %s: supervision failed", session.camera_key)

    def _ensure_supervisor(self) -> None:
        if not self._supervise:
            return
        with self._lock:
            if self._supervisor is not None and self._supervisor.is_alive():
                return
            self._supervisor = threading.Thread(target=self._supervise_loop, name="stream-ingest-supervisor",
                                                daemon=True)
            self._supervisor.start()

    def _supervise_loop(self) -> None:
        while not self._shutdown.wait(SUPERVISOR_INTERVAL_S):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep supervising
                logger.exception("Stream supervisor tick failed")

    def shutdown(self) -> None:
        """Stop every session (backend exit)."""
        self._shutdown.set()
        with self._lock:
            sessions = [self._remove_locked(key) for key in list(self._sessions)]
        for session in sessions:
            if session is not None:
                session.stop("the backend is stopping")

    # -- frames ------------------------------------------------------------------

    def latest_frame(self, camera_key: str, after_seq: int = 0, max_age_ms: Optional[float] = None,
                     wait_ms: float = 0):
        """The session's newest frame after ``after_seq`` (see
        ``StreamSession.latest_frame``); None without a session."""
        session = self.session(camera_key)
        if session is None:
            return None
        return session.latest_frame(after_seq=after_seq, max_age_ms=max_age_ms, wait_ms=wait_ms)

    def newest_seq(self, camera_key: str) -> int:
        """The sequence number of the session's newest delivered frame
        (``StreamSession.newest_seq``); 0 without a session."""
        session = self.session(camera_key)
        return session.newest_seq() if session is not None else 0

    # -- Image_Source CRUD seam (design component 10) -----------------------

    def notify_config_changed(self, image_source_id: str) -> None:
        """A stream Image_Source was created or changed: a running session
        restarts now with the new URL, settings and credentials, keeping
        its leases (Requirement 8.11); with no session there is nothing to
        do until the next lease."""
        session = self.session(camera_key_for_image_source(image_source_id))
        if session is not None:
            session.restart("configuration changed")

    def notify_deleted(self, image_source_id: str) -> None:
        """A stream Image_Source was deleted: its session stops now, leases
        or not (Requirement 4.7)."""
        key = camera_key_for_image_source(image_source_id)
        with self._lock:
            session = self._remove_locked(key)
        if session is not None:
            session.stop("image source deleted")

    # -- health --------------------------------------------------------------

    def health_for_image_source(self, image_source_id: str) -> Optional[dict]:
        """The Stream_Health of a configured camera's session, or None when
        no session runs."""
        return self.health(camera_key_for_image_source(image_source_id))

    def health(self, camera_key: str) -> Optional[dict]:
        """The Stream_Health of the session of ``camera_key``, or None."""
        session = self.session(camera_key)
        return session.health() if session is not None else None

    def all_health(self) -> List[dict]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [session.health() for session in sessions]

    def add_health_listener(self, listener: Callable[[str, dict], None]) -> None:
        with self._lock:
            if listener not in self._health_listeners:
                self._health_listeners.append(listener)

    def remove_health_listener(self, listener: Callable[[str, dict], None]) -> None:
        with self._lock:
            if listener in self._health_listeners:
                self._health_listeners.remove(listener)

    def _on_session_health(self, camera_key: str, document: dict) -> None:
        with self._lock:
            listeners = list(self._health_listeners)
        for listener in listeners:
            try:
                listener(camera_key, document)
            except Exception:  # noqa: BLE001 - a listener must not break a session
                logger.exception("A stream health listener failed for %s", camera_key)


_manager: Optional[StreamIngestManager] = None
_manager_lock = threading.Lock()


def get_stream_ingest_manager() -> StreamIngestManager:
    """The process-wide manager. Creating it removes the frame segments a
    previous backend's workers left behind."""
    global _manager
    with _manager_lock:
        if _manager is None:
            from stream_ingest import protocol
            try:
                removed = protocol.sweep_segments()
                if removed:
                    logger.info("Removed %d stale stream frame segments", removed)
            except Exception:  # noqa: BLE001 - housekeeping only
                pass
            _manager = StreamIngestManager()
        return _manager


def set_stream_ingest_manager(manager: Optional[StreamIngestManager]) -> None:
    """Replace the process-wide manager (tests)."""
    global _manager
    with _manager_lock:
        _manager = manager
