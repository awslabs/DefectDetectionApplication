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
"""Process-wide ``StreamBroadcaster``: the registry of per-camera stream sessions.

The :class:`StreamBroadcaster` is the single place that mediates device access for
the broadcast-model live preview. It holds a ``{camera_id -> StreamSession}`` map
and enforces the **single device-claim invariant**: while a camera has at least one
subscribed viewer there is exactly one open backend claim for it, and when the last
viewer leaves the claim is released (Req 1.2, 2.1, 3.4).

Lifecycle (this module, task 5.1):

* :meth:`subscribe` — *start-on-first-viewer*. The first viewer for a camera with no
  session creates the session, opens the backend (acquiring the single claim),
  starts the acquisition worker, and registers the viewer. Subsequent viewers reuse
  the existing session and claim (Req 1.2, 3.1, 8.1).
* :meth:`unsubscribe` — *stop-on-last-viewer*. Removing the last viewer stops the
  worker, stops the stream and closes the backend (releasing the claim), and removes
  the session from the registry (Req 3.3, 3.7, 8.7, 8.8).
* :meth:`viewer_count` — the active viewer count for a camera (Req 8.4).
* :meth:`apply_settings` — *live edit-settings*. Applies gain/exposure/advanced
  controls to a running session's claim, keeping it RUNNING with its viewer set
  unchanged (task 8.1). On any control failure it retains (and best-effort restores)
  the prior in-effect values, keeps the session active, and raises
  :class:`SettingsApplyError` naming the failed control (task 8.2, Req 5.5).

Thread-safety: a single re-entrant lock guards the registry dict and the per-session
viewer mutations, so concurrent subscribe/unsubscribe calls can never create two
sessions (two claims) for the same camera or race the start/stop transitions.

This module is pure orchestration logic with **no** ``gi`` / device imports at module
load (``backends.py`` keeps its GenICam / GStreamer imports lazy), so it stays
importable on hosts without the camera stack. It is made testable by injecting a
``backend_factory`` (to supply a mock :class:`CameraBackend`) and a ``time_fn`` clock.

:meth:`preview_with_override` (task 8.3) serves the single-frame override preview used
by ``POST /image-sources/{id}/preview``: it returns a preview frame reflecting a
per-request config override **without** mutating the live session's applied control
values or the shared latest-frame slot other viewers read (Req 5.4, Property 20).

:meth:`get_inference_frame` (task 7.1) reuses an active session's claim for the
inference/capture path and falls back to a single dedicated claim when no session
exists, so the broadcaster mediates *all* device access for a camera.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Callable, Optional

from utils.streaming.backends import AravisBackend, CameraBackend, GStreamerBackend
from utils.streaming.models import (
    FrameResult,
    FrameStatus,
    SessionState,
    StreamConfig,
    SubscribeResult,
    Viewer,
)
from utils.streaming.session import StreamSession
from utils.streaming.worker import AcquisitionWorker

logger = logging.getLogger(__name__)


class NoActiveSessionError(Exception):
    """Raised when a live-session-only operation targets a camera with no session.

    :meth:`StreamBroadcaster.apply_settings` applies controls to the *running* claim
    and must never open a new claim itself (subscribing is the only path that starts a
    session / acquires a claim). When no session is active for the requested camera this
    is raised so the caller gets a clear "camera is not streaming" indication rather than
    a second claim being opened behind its back (Req 5.1).
    """


class SettingsApplyError(Exception):
    """Raised when applying a camera control to the live session fails (Req 5.5).

    :meth:`StreamBroadcaster.apply_settings` applies gain / exposure / advanced
    GenICam controls to the running claim. If the backend's ``apply_features`` raises
    for any control, the broadcaster surfaces this descriptive error which **names the
    failed control(s)** (the ``control`` attribute), retains the camera control values
    that were in effect *before* the failed request (the ``retained`` attribute, also
    best-effort re-applied to the device), and leaves the session active (RUNNING) with
    its viewer set unchanged (Property 19).

    Attributes:
        camera_id: The camera whose live apply failed.
        control: Human-readable name(s) of the control(s) in the failed request.
        retained: The prior in-effect device-accepted control values that were
            retained (the session's ``applied_features`` snapshot before the request).
    """

    def __init__(
        self,
        message: str,
        *,
        camera_id: str,
        control: str,
        retained: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.camera_id = camera_id
        self.control = control
        self.retained = retained or {}


def _describe_failed_controls(features) -> str:
    """Return a human-readable name for the control(s) in an apply request.

    Used to name the failed control in :class:`SettingsApplyError` (Req 5.5). Accepts
    either an image-source-style dict (``gain`` / ``exposure`` / ``advancedSettings``)
    or a device-feature list (``[{"feature","value"}, ...]``) — the same shapes the
    backend's ``apply_features`` accepts — and extracts the control names. Falls back to
    a generic label when no name can be derived so the error is always descriptive.
    """
    names: list[str] = []
    if isinstance(features, dict):
        for key, value in features.items():
            if key == "advancedSettings" and isinstance(value, (list, tuple)):
                for entry in value:
                    if isinstance(entry, dict):
                        name = entry.get("feature") or entry.get("name")
                        if name:
                            names.append(str(name))
            else:
                names.append(str(key))
    elif isinstance(features, (list, tuple)):
        for entry in features:
            if isinstance(entry, dict):
                name = entry.get("feature") or entry.get("name")
                if name:
                    names.append(str(name))
    # De-duplicate while preserving order.
    seen: set[str] = set()
    ordered = [n for n in names if not (n in seen or seen.add(n))]
    return ", ".join(ordered) if ordered else "<unknown control>"


# Image-source ``type`` values used to pick a backend in the default factory. These
# mirror ``model.image_source.ImageSourceType`` but are kept as literals so this
# module does not depend on the model layer. ``Camera`` (GenICam / USB3Vision) maps
# to Aravis; NVIDIA CSI and ICAM smart cameras map to the GStreamer pipeline path.
_ARAVIS_SOURCE_TYPE = "Camera"
_GSTREAMER_SOURCE_TYPES = ("NvidiaCSI", "ICam")


class StreamBroadcaster:
    """Process-wide registry of per-camera :class:`StreamSession`s.

    Owns the ``{camera_id -> StreamSession}`` map and the worker bound to each
    session, and is the single enforcement point for the one-open-claim-per-camera
    invariant. Construct directly with an injected ``backend_factory`` / ``time_fn``
    for tests; use :func:`get_broadcaster` for the process-wide singleton.

    Attributes:
        stream_config: Default :class:`StreamConfig` applied to new sessions /
            backends when a subscribe does not imply its own.
    """

    def __init__(
        self,
        *,
        stream_config: Optional[StreamConfig] = None,
        backend_factory: Optional[Callable[[str, Optional[dict]], CameraBackend]] = None,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        """Create a broadcaster.

        Args:
            stream_config: Default :class:`StreamConfig` for new sessions; defaults
                are used when omitted.
            backend_factory: Callable ``(camera_id, config) -> CameraBackend`` used
                to build the backend for a new session. Injected by tests to supply a
                mock backend; defaults to :meth:`_default_backend_factory`, which
                selects :class:`AravisBackend` vs :class:`GStreamerBackend` from the
                config's image-source ``type``.
            time_fn: Wall-clock source returning epoch seconds, used to stamp viewer
                ``subscribed_at`` / ``last_active``; injected for deterministic tests
                (defaults to :func:`time.time`).
        """
        self.stream_config = stream_config or StreamConfig()
        self._backend_factory = backend_factory or self._default_backend_factory
        self._time = time_fn

        # The registry and the per-session worker handles. Both are only ever
        # mutated while holding ``_lock`` (re-entrant so internal helpers that also
        # take the lock can be called from already-locked sections).
        self._sessions: dict[str, StreamSession] = {}
        self._workers: dict[str, AcquisitionWorker] = {}
        self._lock = threading.RLock()

    # --- subscription lifecycle ------------------------------------------

    def subscribe(self, camera_id: str, config: Optional[dict] = None) -> SubscribeResult:
        """Register a new viewer for ``camera_id``, starting the session if needed.

        Start-on-first-viewer: when no session exists for the camera, one is created
        — the backend is built via the factory, :meth:`CameraBackend.open` acquires
        the single device claim, :meth:`CameraBackend.start_stream` begins continuous
        acquisition, and an :class:`AcquisitionWorker` is started as the session's
        single producer. A new :class:`Viewer` with a server-issued uuid is then
        registered (Req 1.2, 2.1, 3.1, 8.1).

        Args:
            camera_id: Identifier of the physical camera to subscribe to.
            config: Optional image-source / stream configuration used by the backend
                factory when a new session is created. Ignored when a session already
                exists (the existing claim is reused).

        Rejection handling (task 5.2):

        * **Viewer limit** — when the camera already has ``max_viewers`` active
          viewers the subscribe is rejected with reason ``"viewer_limit"`` and the
          (unchanged) current viewer count, without adding a viewer and without
          disturbing the existing viewers or session (Req 1.3, 1.6).
        * **Open failure** — when starting a new session fails (the backend's
          ``open()`` raises after its internal ``<= max_open_attempts`` retries,
          Req 7.6), the subscribe is rejected with reason ``"camera_unavailable"``
          and a viewer count of 0, leaving no session and zero claims for that
          camera and not affecting any other camera (Req 1.7, 3.2).

        Returns:
            A :class:`SubscribeResult` carrying the new ``viewer_id`` and the active
            viewer count after registration, or — on rejection — ``viewer_id=None``
            with ``accepted=False`` and the ``"viewer_limit"`` / ``"camera_unavailable"``
            reason.
        """
        with self._lock:
            session = self._sessions.get(camera_id)
            if session is not None:
                # Reuse the existing claim, but enforce the per-camera viewer cap
                # first: at the limit, reject without touching existing viewers or
                # the running session (Req 1.3, 1.6).
                current = session.viewer_count()
                if current >= self.stream_config.max_viewers:
                    logger.info(
                        f"StreamBroadcaster {camera_id}: subscribe rejected (viewer_limit) "
                        f"at {current}/{self.stream_config.max_viewers} viewers"
                    )
                    return SubscribeResult(
                        viewer_id=None,
                        accepted=False,
                        reason="viewer_limit",
                        viewer_count=current,
                    )
            else:
                # Start-on-first-viewer. A failed open leaves no session and zero
                # claims; surface it as a graceful camera_unavailable rejection
                # rather than propagating the exception (Req 1.7, 3.2, 7.6).
                session = self._start_session(camera_id, config)
                if session is None:
                    return SubscribeResult(
                        viewer_id=None,
                        accepted=False,
                        reason="camera_unavailable",
                        viewer_count=0,
                    )

            viewer = Viewer(
                viewer_id=str(uuid.uuid4()),
                camera_id=camera_id,
                subscribed_at=self._time(),
                last_active=self._time(),
            )
            session.add_viewer(viewer)
            count = session.viewer_count()
            logger.info(
                f"StreamBroadcaster {camera_id}: viewer {viewer.viewer_id} subscribed "
                f"(viewer_count={count})"
            )
            return SubscribeResult(
                viewer_id=viewer.viewer_id,
                accepted=True,
                reason=None,
                viewer_count=count,
            )

    def unsubscribe(self, camera_id: str, viewer_id: str) -> None:
        """Deregister a viewer; stop the session if it was the last one.

        Stop-on-last-viewer: removing the final viewer stops the acquisition worker,
        stops the stream and closes the backend (releasing the single device claim),
        and removes the session from the registry so a future subscribe starts fresh
        (Req 3.3, 3.7, 8.7, 8.8). Removing an unknown camera or viewer id is a no-op.

        Args:
            camera_id: Identifier of the physical camera.
            viewer_id: Id of the viewer to remove.
        """
        with self._lock:
            session = self._sessions.get(camera_id)
            if session is None:
                return

            now_empty = session.remove_viewer(viewer_id)
            logger.info(
                f"StreamBroadcaster {camera_id}: viewer {viewer_id} unsubscribed "
                f"(viewer_count={session.viewer_count()})"
            )
            if now_empty:
                self._stop_session(camera_id)

    def viewer_count(self, camera_id: str) -> int:
        """Return the active viewer count for ``camera_id`` (0 when no session)."""
        with self._lock:
            session = self._sessions.get(camera_id)
            return session.viewer_count() if session is not None else 0

    # --- internal session start/stop -------------------------------------

    def _start_session(self, camera_id: str, config: Optional[dict]) -> Optional[StreamSession]:
        """Create, open, and start a session for ``camera_id`` (caller holds lock).

        Builds the backend via the factory, opens it (acquiring the single claim),
        starts continuous acquisition, launches the worker, and registers the session.
        The backend's ``open()`` itself retries up to ``max_open_attempts`` internally
        (Req 7.6); this method only needs to catch the resulting failure.

        On any failure the partially-created session is torn down (claim released if it
        was opened) and **not** left in the registry, so the single-claim invariant
        holds even on a failed start. Rather than re-raising, ``None`` is returned so
        :meth:`subscribe` can surface a graceful ``camera_unavailable`` rejection
        (Req 1.7, 3.2). Other cameras are untouched because nothing was registered.

        Returns:
            The started :class:`StreamSession`, or ``None`` when the start failed.
        """
        backend = self._backend_factory(camera_id, config)
        session = StreamSession(
            camera_id=camera_id,
            backend=backend,
            stream_config=self.stream_config,
            state=SessionState.STARTING,
        )
        try:
            backend.open()           # acquire the single Device_Claim (retries internally)
            backend.start_stream()   # begin continuous acquisition
            # Bind the worker to the broadcaster's clock so each published frame's
            # acquired_at is stamped with the SAME time source the broadcaster uses
            # for read_latest / heartbeats. Otherwise the worker would default to
            # time.monotonic while the broadcaster reads with time.time (wall clock),
            # mixing two unrelated epochs in the OK/STALE freshness comparison and
            # corrupting StreamConfig.stale_after_s semantics in production. sleep_fn
            # is left at its default; StreamSession/worker stay injectable for tests.
            worker = AcquisitionWorker(
                session, stream_config=self.stream_config, time_fn=self._time
            )
            worker.start()
        except Exception:
            # Roll back any partial start so no orphaned claim / session survives,
            # then report the failure to subscribe as a graceful rejection.
            logger.exception(
                f"StreamBroadcaster {camera_id}: failed to start session; rejecting as "
                f"camera_unavailable"
            )
            self._safe_close_backend(camera_id, backend)
            return None

        self._sessions[camera_id] = session
        self._workers[camera_id] = worker
        logger.info(f"StreamBroadcaster {camera_id}: session started (claim acquired)")
        return session

    def _stop_session(self, camera_id: str) -> None:
        """Stop the worker, release the claim, and drop the session (caller holds lock).

        Drives the full teardown: signal/join the worker, ``stop_stream()`` then
        ``close()`` the backend to release the single claim, and remove the session and
        worker from the registry. The claim release is best-effort-guaranteed: even if
        ``stop_stream()`` raises, ``close()`` is still attempted (design "force claim
        release"), so the registry never retains a session whose claim leaked.
        """
        session = self._sessions.get(camera_id)
        if session is None:
            return

        session.state = SessionState.STOPPING
        worker = self._workers.get(camera_id)
        if worker is not None:
            try:
                worker.stop()
                worker.join(timeout=self.stream_config.frame_timeout_ms / 1000.0)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"StreamBroadcaster {camera_id}: error stopping worker: {e}")

        backend = session.backend
        try:
            if backend is not None:
                backend.stop_stream()
        except Exception as e:
            logger.warning(f"StreamBroadcaster {camera_id}: stop_stream failed: {e}")
        finally:
            self._safe_close_backend(camera_id, backend)

        session.state = SessionState.STOPPED
        self._sessions.pop(camera_id, None)
        self._workers.pop(camera_id, None)
        logger.info(f"StreamBroadcaster {camera_id}: session stopped (claim released)")

    def _safe_close_backend(self, camera_id: str, backend: Optional[CameraBackend]) -> None:
        """Best-effort ``close()`` to release the device claim, swallowing errors."""
        if backend is None:
            return
        try:
            backend.close()
        except Exception as e:  # pragma: no cover - best-effort cleanup
            logger.warning(f"StreamBroadcaster {camera_id}: backend close failed: {e}")

    # --- backend factory --------------------------------------------------

    def _default_backend_factory(self, camera_id: str, config: Optional[dict]) -> CameraBackend:
        """Build the default :class:`CameraBackend` for ``camera_id`` from ``config``.

        Selects the adapter from the image-source ``type`` in ``config``: ``Camera``
        (GenICam / USB3Vision) -> :class:`AravisBackend`; ``NvidiaCSI`` / ``ICam`` ->
        :class:`GStreamerBackend`. Defaults to :class:`AravisBackend` when the type is
        absent or unrecognized. Tests inject their own factory to bypass this and
        supply a mock backend.
        """
        source_type = (config or {}).get("type")
        if source_type in _GSTREAMER_SOURCE_TYPES:
            return GStreamerBackend(camera_id, image_source=config, stream_config=self.stream_config)
        return AravisBackend(
            camera_id,
            image_source_config=(config or {}).get("imageSourceConfiguration"),
            stream_config=self.stream_config,
        )

    # --- seams for later tasks -------------------------------------------
    # The following methods are intentionally unimplemented in task 5.1. They are
    # declared here so the class shape is stable and later tasks fill in the logic
    # against the registry/lock established above.

    def heartbeat(self, camera_id: str, viewer_id: str) -> bool:
        """Refresh a viewer's last-active timestamp to "now" (Req 3.8, 8.2, 8.3).

        Looks the viewer up under the registry lock and stamps its ``last_active``
        with the current clock value (``self._time()``), which is exactly the time
        the stale-viewer sweep compares against. A viewer that keeps heartbeating
        therefore never crosses the stale window, so abandoned tabs (which stop
        heartbeating) are the only ones the sweep reaps.

        Args:
            camera_id: Identifier of the physical camera the viewer is watching.
            viewer_id: Id of the viewer to refresh.

        Returns:
            ``True`` if the viewer exists and was refreshed; ``False`` when the
            camera has no session or the viewer id is unknown / already expired
            (e.g. swept away), so callers can treat a ``False`` as "re-subscribe".
        """
        with self._lock:
            session = self._sessions.get(camera_id)
            if session is None:
                return False
            viewer = session.viewers.get(viewer_id)
            if viewer is None:
                return False
            viewer.last_active = self._time()
            return True

    def sweep_stale_viewers(self, now: float) -> None:
        """Deregister viewers past the stale timeout; stop emptied sessions (Req 3.8, 8.6, 8.7).

        Backstop for abandoned tabs that subscribed but stopped heartbeating /
        polling. Across every session, a viewer is removed exactly when it has been
        inactive *longer than* the configured window — ``now - last_active >
        stale_timeout_s`` (:class:`StreamConfig.stale_timeout_s`, default 30). The
        boundary ``now - last_active == stale_timeout_s`` is **not** stale (strict
        ``>``), matching the heartbeat/staleness contract pinned by the property test
        in ``test_session_heartbeat.py`` (task 3.6).

        Removing stale viewers decrements the active count accordingly; when a
        session is emptied as a result it is stopped via :meth:`_stop_session`, which
        releases the single device claim (Req 8.7) — the same stop-on-last teardown
        an explicit unsubscribe drives. The whole sweep runs under the registry lock
        so it never races subscribe/unsubscribe.

        Args:
            now: Current epoch seconds (injected clock) to compare against each
                viewer's ``last_active``.
        """
        with self._lock:
            stale_timeout_s = self.stream_config.stale_timeout_s
            # Snapshot the camera ids: _stop_session mutates self._sessions, so we
            # must not iterate the dict directly while emptying it.
            for camera_id in list(self._sessions.keys()):
                session = self._sessions.get(camera_id)
                if session is None:
                    continue

                stale_viewer_ids = [
                    viewer_id
                    for viewer_id, viewer in session.viewers.items()
                    if (now - viewer.last_active) > stale_timeout_s
                ]
                if not stale_viewer_ids:
                    continue

                now_empty = session.is_empty()
                for viewer_id in stale_viewer_ids:
                    now_empty = session.remove_viewer(viewer_id)
                logger.info(
                    f"StreamBroadcaster {camera_id}: swept {len(stale_viewer_ids)} stale "
                    f"viewer(s) (viewer_count={session.viewer_count()})"
                )
                if now_empty:
                    self._stop_session(camera_id)

    def get_frame(self, camera_id: str, viewer_id: str) -> FrameResult:
        """Return the latest frame + state for a viewer, refreshing its heartbeat.

        The frame GET doubles as a heartbeat: when the viewer exists its
        ``last_active`` is stamped with the current clock as a side effect, so a
        viewer that keeps polling stays alive without a separate heartbeat call
        (Req 8.3). The result classifies the latest-frame slot against ``now``:

        * ``OK`` with the :class:`~utils.streaming.models.LatestFrame` when a fresh
          frame is available,
        * ``NO_FRAME`` when the session is up but nothing has been published yet
          (Req 2.6),
        * ``STALE`` when the latest frame is older than the freshness ceiling
          (Req 4.7),
        * ``DISCONNECTED`` (no payload) when the camera has dropped or there is no
          session for the camera (Req 7.5).

        Disconnection cascade (Req 3.5, 3.11, 7.2, 7.3, 7.4, 7.5): the acquisition
        worker transitions the session to :attr:`SessionState.ERROR` when a grab
        times out / fails on an established stream (``WorkerStopReason.DISCONNECTED``)
        or no first frame ever arrives (``FIRST_FRAME_TIMEOUT``). On observing that
        ERROR state here, the session is torn down via :meth:`_stop_session` — which
        releases the single device claim **before** this call returns — and
        ``DISCONNECTED`` is reported with ``frame=None``. A previously cached frame
        is **never** served once the camera is disconnected. Because the session is
        dropped from the registry by the cascade, every subsequent ``get_frame`` for
        that (now absent) camera also returns ``DISCONNECTED``. A camera that never
        had a session likewise returns ``DISCONNECTED`` (it is not streaming).

        Args:
            camera_id: Identifier of the physical camera.
            viewer_id: Id of the requesting viewer; used to refresh its heartbeat.

        Returns:
            A :class:`FrameResult` carrying the status and, when ``OK``, the frame.
        """
        with self._lock:
            session = self._sessions.get(camera_id)
            if session is None:
                # No session => the camera is not streaming (never started, or
                # already torn down by a prior disconnection cascade): DISCONNECTED.
                return FrameResult(status=FrameStatus.DISCONNECTED, frame=None)

            # Disconnection takes priority over any cached frame. The worker marks
            # the session ERROR on a disconnect / first-frame timeout; cascade the
            # teardown so the claim is released BEFORE we report completion, and
            # never hand back a stale cached payload (Req 7.2-7.5, 3.5, 3.11).
            if session.state == SessionState.ERROR:
                logger.info(
                    f"StreamBroadcaster {camera_id}: disconnect detected (session ERROR); "
                    f"stopping session and reporting DISCONNECTED"
                )
                self._stop_session(camera_id)
                return FrameResult(status=FrameStatus.DISCONNECTED, frame=None)

            # Healthy session: the frame GET doubles as a heartbeat (Req 8.3).
            viewer = session.viewers.get(viewer_id)
            if viewer is not None:
                viewer.last_active = self._time()

            # Classify the latest-frame slot (OK / NO_FRAME / STALE).
            return session.read_latest(self._time())

    def get_inference_frame(
        self, camera_id: str, config: Optional[dict] = None
    ) -> Optional[dict]:
        """Return a single frame for the inference/capture path, sharing the claim.

        This is the broadcaster's mediation of *all* non-viewer device access, so the
        one-open-claim-per-camera invariant stays enforced in a single place. The
        return value matches the legacy ``camera_manager.get_camera_frame`` payload —
        an unpickled ``{"data", "height", "width"}`` dict (or ``None``) — so existing
        callers (``digital_input_*`` / ``workflow`` / capture) are unaffected once
        :meth:`camera_manager.get_camera_frame` is rewired onto this method (task 7.2).

        Two paths, both honoring the single-claim invariant:

        * **Active session (Req 6.1, 6.4):** when a session is running for the camera,
          the running claim is *reused* — the session's current latest frame is read
          from the latest-frame slot and converted to the legacy dict shape. **No
          second** ``Device_Claim`` is opened. The read is a non-blocking slot copy
          (a pure function of the slot, like :meth:`get_frame`); if no frame has been
          published yet ``None`` is returned. Whatever the outcome, the session and its
          claim are left intact (Req 6.6) — this method never tears the session down.
        * **No session (Req 6.3):** the legacy dedicated-claim fallback runs so
          inference still works when nobody is watching. Exactly one claim is opened
          via the backend factory (``open`` -> ``start_stream`` -> one ``grab`` ->
          ``stop_stream`` -> ``close``) and is always released in a ``finally`` (the
          claim count returns to 0), even when the grab fails.

        The whole operation runs under the registry lock so the no-session fallback
        cannot race a concurrent :meth:`subscribe` into opening a second claim for the
        same camera (which would violate the single-claim invariant).

        Args:
            camera_id: Identifier of the physical camera.
            config: Optional image-source / per-capture configuration used only by the
                dedicated-claim fallback when no session exists. Ignored while a
                session is active (the running claim — and its already-applied config —
                is reused).

        Returns:
            The frame as a legacy ``{"data", "height", "width"}`` dict, or ``None`` when
            no frame is available (no frame published yet on an active session, or the
            dedicated grab failed). On failure with an active session, ``None`` is
            returned while the session + claim are left intact (Req 6.6).
        """
        with self._lock:
            session = self._sessions.get(camera_id)
            if session is not None:
                # Active session: reuse the running claim. Read the latest-frame slot
                # (non-blocking, no device grab, no second claim) and leave the
                # session + claim untouched regardless of outcome (Req 6.1, 6.4, 6.6).
                return self._inference_frame_from_session(camera_id, session)

            # No session: legacy dedicated-claim fallback (Req 6.3). Held under the
            # registry lock so a concurrent subscribe cannot open a second claim for
            # this camera while the dedicated claim is in flight.
            return self._dedicated_inference_frame(camera_id, config)

    def _inference_frame_from_session(
        self, camera_id: str, session: StreamSession
    ) -> Optional[dict]:
        """Read the active session's latest frame as a legacy dict (caller holds lock).

        Reuses the running claim: the frame is copied out of the session's latest-frame
        slot (a pure slot read — no device grab, no second claim). Returns ``None`` when
        no frame has been published yet. Any unexpected error is swallowed and reported
        as ``None`` so the session and its claim are left intact (Req 6.6).
        """
        try:
            result = session.read_latest(self._time())
        except Exception:
            # Defensive: a slot read should not fail, but never let an inference
            # request tear down or disturb the live session (Req 6.6).
            logger.exception(
                f"StreamBroadcaster {camera_id}: inference slot read failed; "
                f"leaving session intact and returning None"
            )
            return None

        frame = result.frame
        if frame is None:
            # Session is up but nothing published yet (NO_FRAME). Keep it simple and
            # non-blocking: report no frame; the session stays active (Req 6.6).
            logger.info(
                f"StreamBroadcaster {camera_id}: inference reused session claim but no "
                f"frame is available yet"
            )
            return None

        return {"data": frame.data, "height": frame.height, "width": frame.width}

    def _dedicated_inference_frame(
        self, camera_id: str, config: Optional[dict]
    ) -> Optional[dict]:
        """Open one dedicated claim, grab a single frame, and release it (caller holds lock).

        The legacy fallback for when no session exists (Req 6.3): builds a backend via
        the factory and drives ``open`` -> ``start_stream`` -> one ``grab`` ->
        ``stop_stream`` -> ``close``. The claim release is guaranteed in a ``finally``
        so the claim count returns to 0 even when the open/grab fails. Returns the
        grabbed frame as a legacy ``{"data", "height", "width"}`` dict, or ``None``.
        """
        backend = self._backend_factory(camera_id, config)
        frame: Optional[dict] = None
        opened = False
        try:
            backend.open()  # acquire exactly one dedicated Device_Claim
            opened = True
            backend.start_stream()
            raw = backend.grab(self.stream_config.frame_timeout_ms)
            if raw is not None:
                frame = {"data": raw.data, "height": raw.height, "width": raw.width}
            else:
                logger.warning(
                    f"StreamBroadcaster {camera_id}: dedicated inference grab returned no frame"
                )
        except Exception:
            logger.exception(
                f"StreamBroadcaster {camera_id}: dedicated inference grab failed"
            )
            frame = None
        finally:
            # Always release the dedicated claim (count returns to 0), even on failure.
            if opened:
                try:
                    backend.stop_stream()
                except Exception as e:
                    logger.warning(
                        f"StreamBroadcaster {camera_id}: dedicated inference stop_stream failed: {e}"
                    )
            self._safe_close_backend(camera_id, backend)
        return frame

    def apply_settings(self, camera_id: str, features: dict) -> dict:
        """Apply gain/exposure/advanced controls to the live session (Req 5.1-5.3).

        Edit-settings path: when a session is active for ``camera_id`` the supplied
        camera control values (gain / exposure / advanced GenICam features) are applied
        to the *live* claim via :meth:`CameraBackend.apply_features`, so the change takes
        effect on the running stream without tearing it down or re-opening the device.
        The session is left fully intact (Req 5.3): its lifecycle state is **not** moved
        away from ``RUNNING`` on success, its viewer set is untouched, and the
        latest-frame slot is never cleared, so every subscribed viewer keeps reading the
        live stream throughout the apply.

        The whole operation runs under the registry lock so it cannot race a concurrent
        subscribe/unsubscribe/stop that would otherwise change the session or its claim
        underneath the apply. The backend's ``apply_features`` returns the values the
        device actually accepted (which may be coerced/clamped), and those are returned
        verbatim so callers can reflect what the hardware applied.

        This deliberately does **not** open a new claim when no session exists —
        subscribing is the only path that starts a session / acquires a claim. A request
        to apply settings to a camera that is not streaming is therefore rejected with a
        :class:`NoActiveSessionError` rather than silently opening a second claim.

        Args:
            camera_id: Identifier of the physical camera whose live session to adjust.
            features: Camera control values to apply. Accepts either an image-source
                style dict (``gain`` / ``exposure`` / ``advancedSettings``) or a device
                feature list; the backend normalizes the shape.

        Returns:
            The device-accepted control values (exactly what
            :meth:`CameraBackend.apply_features` returned).

        Raises:
            NoActiveSessionError: When ``camera_id`` has no active session (the camera is
                not streaming). No claim is opened.
            SettingsApplyError: When the backend fails to apply a control. The error
                names the failed control(s); the prior in-effect values are retained
                (and best-effort re-applied to the device) and the session is left
                active (RUNNING) with its viewer set unchanged (Req 5.5).
        """
        with self._lock:
            session = self._sessions.get(camera_id)
            if session is None:
                # No active session: do NOT open a new claim here (subscribing is the
                # path that starts sessions). Surface a clear "no active session"
                # indication to the caller (Req 5.1).
                raise NoActiveSessionError(
                    f"no active stream session for camera {camera_id}; "
                    f"subscribe before applying settings"
                )

            # Capture the control values in effect *before* this request so we can
            # retain (and best-effort restore) them if the apply fails (Req 5.5).
            prior = dict(session.applied_features)

            # Apply the controls to the live claim. The session is left intact: state is
            # not moved away from RUNNING, the viewer registry is untouched, and the
            # latest-frame slot is never cleared, so viewers keep reading throughout
            # (Req 5.2, 5.3).
            try:
                accepted = session.backend.apply_features(features)
            except Exception as exc:
                # A control failed to apply. Do not leave partial state: keep the
                # session active (its state and viewer set are untouched here), retain
                # the prior in-effect values, and surface a descriptive error naming
                # the failed control (Req 5.5, Property 19).
                control = _describe_failed_controls(features)
                logger.warning(
                    f"StreamBroadcaster {camera_id}: failed to apply control(s) "
                    f"[{control}] to running session; retaining prior values and keeping "
                    f"session active (state={session.state.value}): {exc}"
                )
                # Best-effort restore of the prior in-effect values so the device does
                # not retain a partially-applied control set. session.applied_features is
                # deliberately NOT updated, so the recorded in-effect values stay at the
                # prior set regardless of whether the device restore succeeds.
                self._restore_prior_features(camera_id, session, prior)
                raise SettingsApplyError(
                    f"failed to apply control(s) [{control}] for camera {camera_id}: {exc}",
                    camera_id=camera_id,
                    control=control,
                    retained=prior,
                ) from exc

            # Success: record the new device-accepted values as the in-effect set so a
            # later failed apply can retain them.
            if isinstance(accepted, dict):
                session.applied_features.update(accepted)
            logger.info(
                f"StreamBroadcaster {camera_id}: applied live settings to running session "
                f"(state={session.state.value}, viewer_count={session.viewer_count()})"
            )
            return accepted

    def _restore_prior_features(
        self, camera_id: str, session: StreamSession, prior: dict
    ) -> None:
        """Best-effort re-apply of the prior in-effect control values (caller holds lock).

        Invoked after a failed apply so the device does not retain a partially-applied
        control set: the values that were in effect before the failed request are
        re-applied. This is strictly best-effort — any error here is logged and
        swallowed so a restore failure can never tear down or further disturb the live
        session (the recorded in-effect values remain the prior set regardless).
        """
        if not prior:
            return
        try:
            session.backend.apply_features(dict(prior))
        except Exception as e:  # pragma: no cover - best-effort restore
            logger.warning(
                f"StreamBroadcaster {camera_id}: best-effort restore of prior control "
                f"values failed (session left active, prior values retained as recorded): {e}"
            )

    def preview_with_override(
        self, camera_id: str, override_config: Optional[dict] = None
    ) -> Optional[dict]:
        """Return a single preview frame reflecting a per-request config override.

        Backs ``POST /image-sources/{id}/preview`` (wired in task 9.2). The point of
        this path is **isolation** (Req 5.4, Property 20): producing a preview that
        reflects ``override_config`` must NOT mutate the live session's applied control
        values (``session.applied_features``) and must NOT alter the shared latest-frame
        slot that other viewers read. The hard post-condition this method guarantees is:
        *after it returns, the session's ``applied_features`` and its latest-frame slot
        are exactly what they were before the call* — for any subscribed viewer the
        override is invisible.

        Two paths, both honoring the single-claim invariant and the isolation
        post-condition, run under the registry lock so neither can race a concurrent
        subscribe/unsubscribe/apply:

        * **Active session — isolated, non-mutating read (documented limitation).**
          A second ``Device_Claim`` on the same camera while a session is active would
          violate the single-claim invariant, and applying the override to the *live*
          claim (then restoring) would briefly disturb the running session's controls
          and the frames the acquisition worker publishes into the shared slot — exactly
          what Req 5.4 forbids. With the current single-claim backends there is no way
          to grab an override-reflecting frame in true isolation while a session holds
          the only claim. The correct, isolation-preserving behavior is therefore to
          return the session's **current** latest frame via a pure slot read (the same
          non-blocking copy :meth:`get_frame` / inference reuse perform) and leave both
          ``applied_features`` and the slot untouched. **Limitation:** while a session is
          active the returned preview reflects the session's in-effect controls, *not*
          the requested override; the override cannot be applied in isolation without
          breaking the single-claim invariant or disturbing other viewers. Returns
          ``None`` when no frame has been published yet.

        * **No active session — dedicated-claim override grab.** With no session there is
          nothing to disturb, so a dedicated short-lived claim is opened to produce a
          genuinely override-reflecting frame: build a backend from ``override_config``
          via the factory, ``open`` (acquire exactly one claim) -> ``start_stream`` ->
          ``apply_features(override_config)`` (apply the override) -> one ``grab`` ->
          ``stop_stream`` -> ``close``. The claim release is guaranteed in a ``finally``
          so the claim count returns to 0 even on failure. Returns the grabbed frame as a
          legacy ``{"data", "height", "width"}`` dict, or ``None`` when the grab/apply
          fails (e.g. an out-of-range override is rejected by validation).

        Args:
            camera_id: Identifier of the physical camera to preview.
            override_config: The per-request image-source / control override
                (``gain`` / ``exposure`` / ``advancedSettings`` ...). With an active
                session it is intentionally *not* applied (see the limitation above);
                with no session it configures the dedicated claim and is applied to it.

        Returns:
            The preview frame as a legacy ``{"data", "height", "width"}`` dict, or
            ``None`` when no frame is available. The live session (if any) — its
            ``applied_features`` and its latest-frame slot — is left exactly unchanged.
        """
        with self._lock:
            session = self._sessions.get(camera_id)
            if session is not None:
                # Active session: isolation wins over reflecting the override. Return a
                # pure slot read of the current latest frame WITHOUT touching the live
                # claim, applied_features, or the shared slot (Req 5.4, Property 20).
                return self._isolated_preview_from_session(camera_id, session)

            # No session: safe to open a dedicated short-lived claim and actually apply
            # the override to produce an override-reflecting preview frame. Held under
            # the registry lock so a concurrent subscribe cannot open a second claim for
            # this camera while the dedicated preview claim is in flight.
            return self._dedicated_override_preview(camera_id, override_config)

    def _isolated_preview_from_session(
        self, camera_id: str, session: StreamSession
    ) -> Optional[dict]:
        """Return the live session's current frame without disturbing it (caller holds lock).

        A pure read of the latest-frame slot — no device grab, no second claim, and no
        mutation of ``session.applied_features`` or the slot — so the isolation
        post-condition (Property 20) holds by construction. The override is deliberately
        NOT applied here (see :meth:`preview_with_override` for the documented
        limitation). Returns ``None`` when nothing has been published yet, or on any
        unexpected read error (which is swallowed so the live session is never disturbed).
        """
        try:
            result = session.read_latest(self._time())
        except Exception:
            # Defensive: a slot read should not fail, but never let a preview request
            # disturb the live session (Req 5.4).
            logger.exception(
                f"StreamBroadcaster {camera_id}: override-preview slot read failed; "
                f"leaving session intact and returning None"
            )
            return None

        logger.info(
            f"StreamBroadcaster {camera_id}: override preview served the live session's "
            f"current frame (override not applied while a session is active; isolation "
            f"preserved — applied_features and latest slot unchanged)"
        )
        frame = result.frame
        if frame is None:
            return None
        return {"data": frame.data, "height": frame.height, "width": frame.width}

    def _dedicated_override_preview(
        self, camera_id: str, override_config: Optional[dict]
    ) -> Optional[dict]:
        """Open one dedicated claim, apply the override, grab a frame, release it.

        The no-session path (caller holds lock): builds a backend from
        ``override_config`` and drives ``open`` -> ``start_stream`` ->
        ``apply_features(override_config)`` -> one ``grab`` -> ``stop_stream`` ->
        ``close``. Because no session exists there is nothing to disturb, so the override
        can be applied for real. The claim release is guaranteed in a ``finally`` (the
        claim count returns to 0) even when the open / apply / grab fails. Returns the
        grabbed frame as a legacy ``{"data", "height", "width"}`` dict, or ``None``.
        """
        backend = self._backend_factory(camera_id, override_config)
        frame: Optional[dict] = None
        opened = False
        try:
            backend.open()  # acquire exactly one dedicated Device_Claim
            opened = True
            backend.start_stream()
            # Apply the per-request override to this isolated claim so the preview
            # reflects it. An out-of-range override raises here and is reported as None.
            if override_config:
                backend.apply_features(override_config)
            raw = backend.grab(self.stream_config.frame_timeout_ms)
            if raw is not None:
                frame = {"data": raw.data, "height": raw.height, "width": raw.width}
            else:
                logger.warning(
                    f"StreamBroadcaster {camera_id}: dedicated override preview grab "
                    f"returned no frame"
                )
        except Exception:
            logger.exception(
                f"StreamBroadcaster {camera_id}: dedicated override preview failed"
            )
            frame = None
        finally:
            # Always release the dedicated claim (count returns to 0), even on failure.
            if opened:
                try:
                    backend.stop_stream()
                except Exception as e:
                    logger.warning(
                        f"StreamBroadcaster {camera_id}: dedicated override preview "
                        f"stop_stream failed: {e}"
                    )
            self._safe_close_backend(camera_id, backend)
        return frame


# --- process-wide singleton ----------------------------------------------

_broadcaster: Optional[StreamBroadcaster] = None
_broadcaster_lock = threading.Lock()


def get_broadcaster() -> StreamBroadcaster:
    """Return the process-wide :class:`StreamBroadcaster` singleton.

    Lazily constructs the singleton on first use (with default config, the default
    backend factory, and the wall clock). The stream API endpoints and the
    inference/capture path share this one instance so all device access for a camera
    is mediated by a single registry, which is what enforces the one-claim-per-camera
    invariant process-wide. Tests construct their own :class:`StreamBroadcaster`
    instances with injected dependencies instead of using this singleton.
    """
    global _broadcaster
    if _broadcaster is None:
        with _broadcaster_lock:
            if _broadcaster is None:
                _broadcaster = StreamBroadcaster()
    return _broadcaster
