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
"""``StreamLeaseKeeper``: registrations hold their stream cameras open
(rtsp-rtmp-stream-cameras Requirement 10.3; design component 13).

A registrations listener of the :class:`~workflow_engine.watcher.WorkflowWatcher`,
wired in ``runtime.py`` next to the ``TriggerSubscriptionManager``. After
every watcher reconciliation it:

- acquires a Stream_Lease for the stream feed of every ``registered``
  registration, so its camera is connected and a triggered run finds a
  fresh frame;
- releases the lease of a registration that was removed, superseded or
  invalidated, or whose feed now reads another camera;
- records a lease refused at the device session limit. The watcher reads
  the refusal through :meth:`StreamLeaseKeeper.refusal_reason` and marks
  the registration invalid with it. The keeper retries on every
  reconciliation (the watcher polls every few seconds), and when capacity
  frees the registration flips back to registered.

When the set of refusals changes, the keeper asks the watcher to
reconcile again so the status changes at once; the nested notification
finds nothing new to change, so the exchange ends there.
"""
import json
import logging
import os
import threading
from typing import Any, Callable, Dict, Optional, Tuple

from workflow_engine.discovery import COMPILED_PIPELINE_FILE, STATUS_INVALID, STATUS_REGISTERED
from workflow_engine.stream_feed import (
    StreamFeed,
    StreamFeedError,
    load_configured_stream_cameras,
    plan_stream_feeds,
)

logger = logging.getLogger(__name__)


def _default_manager():
    from stream_ingest.manager import get_stream_ingest_manager
    return get_stream_ingest_manager()


def _camera_label(feed: StreamFeed) -> str:
    """The camera a feed reads, for messages: its Camera_Source id, or the
    (credential-free, redacted) Stream_URL of an anonymous stream."""
    if feed.camera_source_id:
        return feed.camera_source_id
    from stream_ingest.health import clean_message
    return clean_message(feed.url)


def _read_document(artifact_path: str) -> Optional[dict]:
    if not isinstance(artifact_path, str) or not artifact_path:
        return None
    try:
        with open(os.path.join(artifact_path, COMPILED_PIPELINE_FILE), "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


class StreamLeaseKeeper:
    """See the module docstring."""

    def __init__(self, session_factory: Optional[Callable] = None,
                 resolution_provider: Optional[Callable[[str], Any]] = None,
                 resync: Optional[Callable[[], None]] = None,
                 manager_provider: Callable[[], Any] = _default_manager,
                 stream_camera_resolver: Callable = load_configured_stream_cameras):
        if session_factory is None:
            from dao.sqlite_db.sqlite_db_operations import SessionLocal
            session_factory = SessionLocal
        self._session_factory = session_factory
        self._resolution_provider = resolution_provider
        self._resync = resync
        self._manager_provider = manager_provider
        self._stream_camera_resolver = stream_camera_resolver
        self._lock = threading.Lock()
        self._in_pass = False
        self._rerun = False
        #: registration id -> (camera key, lease)
        self._held: Dict[str, Tuple[str, Any]] = {}
        #: registration id -> (the feed it could not lease, the reason)
        self._refused: Dict[str, Tuple[StreamFeed, str]] = {}

    # -- queries -------------------------------------------------------------

    def refusal_reason(self, registration_id: str) -> Optional[str]:
        """Why the registration's stream lease was refused, or None."""
        with self._lock:
            refused = self._refused.get(registration_id)
        return refused[1] if refused else None

    def held(self) -> Dict[str, str]:
        """registration id -> the camera key it holds a lease on."""
        with self._lock:
            return {registration_id: key for registration_id, (key, _lease) in self._held.items()}

    # -- the listener ----------------------------------------------------------

    def on_registrations_changed(self) -> None:
        """Reconcile the leases with the registrations (watcher listener)."""
        with self._lock:
            if self._in_pass:
                self._rerun = True
                return
            self._in_pass = True
        refusals_changed = False
        try:
            while True:
                refusals_changed = self._one_pass() or refusals_changed
                with self._lock:
                    if not self._rerun:
                        break
                    self._rerun = False
        finally:
            with self._lock:
                self._in_pass = False
        if refusals_changed and self._resync is not None:
            try:
                self._resync()
            except Exception:  # noqa: BLE001 - the next watch cycle recovers
                logger.exception("Could not re-reconcile registrations after a stream lease change")

    def release_all(self) -> None:
        """Release every lease (backend shutdown, tests)."""
        with self._lock:
            held, self._held = self._held, {}
            self._refused.clear()
        if not held:
            return
        manager = self._manager_provider()
        for _key, lease in held.values():
            manager.release_lease(lease)

    # -- one pass --------------------------------------------------------------

    def _desired_feeds(self) -> Dict[str, StreamFeed]:
        """The stream feed each registration should hold: every registered
        one's, plus the refused ones that are still active."""
        from workflow_engine.models import WorkflowRegistration

        desired: Dict[str, StreamFeed] = {}
        with self._lock:
            refused = dict(self._refused)
        session = self._session_factory()
        try:
            rows = session.query(WorkflowRegistration).filter(
                WorkflowRegistration.status.in_([STATUS_REGISTERED, STATUS_INVALID])).all()
            cameras = None

            def configured_cameras():
                nonlocal cameras
                if cameras is None:
                    cameras = self._stream_camera_resolver(session)
                return cameras

            for row in rows:
                if row.status == STATUS_INVALID:
                    if row.id in refused:
                        desired[row.id] = refused[row.id][0]
                    continue
                resolution = None
                if self._resolution_provider is not None:
                    try:
                        resolution = self._resolution_provider(row.id)
                    except Exception:  # noqa: BLE001 - plan from the on-disk document
                        resolution = None
                document = getattr(resolution, "document", None)
                if not isinstance(document, dict):
                    document = _read_document(row.artifact_path)
                if document is None:
                    continue
                try:
                    feeds = plan_stream_feeds(document, resolution, configured_cameras=configured_cameras)
                except StreamFeedError as error:
                    logger.warning("Registration %s has no usable stream feed: %s", row.id, error)
                    continue
                if feeds:
                    desired[row.id] = feeds[0]
        finally:
            session.close()
        return desired

    def _one_pass(self) -> bool:
        """Release, acquire and retry; True when the refusals changed."""
        from stream_ingest.health import StreamError
        from stream_ingest.manager import SessionLimitError
        from stream_ingest.sources import anonymous_source

        desired = self._desired_feeds()
        with self._lock:
            before = {registration_id: reason for registration_id, (_feed, reason) in self._refused.items()}
            stale = [(registration_id, lease) for registration_id, (key, lease) in self._held.items()
                     if registration_id not in desired or desired[registration_id].camera_key != key]
            for registration_id, _lease in stale:
                del self._held[registration_id]
            for registration_id in [key for key in self._refused if key not in desired]:
                del self._refused[registration_id]
            wanted = [(registration_id, feed) for registration_id, feed in sorted(desired.items())
                      if registration_id not in self._held]
            if not stale and not wanted:
                # Nothing to change: a device without stream workflows never
                # creates the Stream_Ingest_Service.
                return {registration_id: reason
                        for registration_id, (_feed, reason) in self._refused.items()} != before
        manager = self._manager_provider()
        for registration_id, lease in stale:
            manager.release_lease(lease)
            logger.info("Released the stream lease of registration %s", registration_id)
        for registration_id, feed in wanted:
            try:
                source = None
                if feed.camera_source_id is None:
                    source = anonymous_source(feed.source_type, feed.url, feed.settings)
                lease = manager.acquire_lease(feed.camera_key, "registration:{0}".format(registration_id),
                                              source=source)
            except SessionLimitError as error:
                reason = "stream camera {0} could not be opened: {1}".format(_camera_label(feed), error.message)
                with self._lock:
                    self._refused[registration_id] = (feed, reason)
                if before.get(registration_id) != reason:
                    logger.warning("Registration %s cannot run: %s", registration_id, reason)
                continue
            except (StreamError, ValueError) as error:
                logger.warning("Registration %s could not lease its stream camera: %s", registration_id, error)
                continue
            with self._lock:
                self._held[registration_id] = (feed.camera_key, lease)
                self._refused.pop(registration_id, None)
            logger.info("Registration %s holds a stream lease on %s", registration_id, feed.camera_key)
        with self._lock:
            after = {registration_id: reason for registration_id, (_feed, reason) in self._refused.items()}
        return after != before
