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
"""Acquisition worker: the single producer driving one :class:`StreamSession`.

Each active stream session owns exactly one :class:`AcquisitionWorker`. The
worker is the *only* code that touches the session's ``CameraBackend`` handle; it
loops ``grab()`` -> ``session.publish(...)`` and is the single writer of the
session's latest-frame slot (matching the broadcast design's single-producer /
many-reader model).

Loop behaviour (see the "Acquisition worker" section of the design):

* **Grab -> publish.** On every successful ``grab(frame_timeout_ms)`` the raw
  frame is published into the session's latest-frame slot with the current clock
  time as its acquired-at stamp, and the session transitions to ``RUNNING`` on
  the first frame.
* **Rate ceiling, never below device cadence (Req 4.3, 4.4).** After each
  iteration the worker sleeps only the remainder of ``1 / min_refresh_fps`` not
  already consumed by the grab/publish. When the device is slower than that
  ceiling the grab already overruns the interval, so no delay is added and the
  loop tracks the device's own cadence. No delay is ever added beyond what the
  ceiling needs.
* **First-frame timeout (Req 3.10, 3.11).** If no frame is published within
  ``first_frame_timeout_s`` of the loop starting, the session is transitioned to
  ``ERROR`` and the loop stops. Transient failed grabs during start-up are
  tolerated (retried) until this deadline.
* **Disconnect detection (Req 7.1).** Once a stream is established, a ``grab()``
  that returns ``None`` (timeout / acquisition failure) or raises marks the
  camera disconnected, transitions the session to ``ERROR``, and stops the loop.
* **Last-good-frame retention (Req 2.7).** A failed grab never calls
  ``publish``, so the latest-frame slot keeps the last successfully published
  frame; it is not overwritten by the failure.

The worker takes an injected **time function** and **sleep function** (defaulting
to :func:`time.monotonic` / :func:`time.sleep`) plus a stop :class:`threading.Event`,
so the loop can be driven deterministically against a mock backend and a virtual
clock and stopped cleanly. This module is pure logic with no device or ``gi``
dependency, so it is import-safe on hosts without the GenICam / GStreamer stack.
"""
from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Callable, Optional

from utils.streaming.models import SessionState, StreamConfig

logger = logging.getLogger(__name__)


class WorkerStopReason(Enum):
    """Why an :class:`AcquisitionWorker` loop terminated.

    Attributes:
        STOPPED: The stop :class:`~threading.Event` was set (clean shutdown,
            e.g. the last viewer unsubscribed).
        FIRST_FRAME_TIMEOUT: No frame was published within
            ``first_frame_timeout_s`` of the loop starting (Req 3.10, 3.11).
        DISCONNECTED: A grab failed/timed out on an established stream, so the
            camera was marked disconnected (Req 7.1).
    """

    STOPPED = "stopped"
    FIRST_FRAME_TIMEOUT = "first_frame_timeout"
    DISCONNECTED = "disconnected"


class AcquisitionWorker:
    """One acquisition loop bound to a single :class:`StreamSession`.

    The worker reads the session's ``backend`` and ``stream_config`` and drives
    the ``grab -> publish`` loop until a stop condition is met. It is designed to
    run on its own thread (see :meth:`start`) but :meth:`run` can also be called
    inline (e.g. from a test) since all timing is injected.

    Attributes:
        session: The :class:`StreamSession` this worker produces frames for.
        stream_config: The :class:`StreamConfig` governing grab timeout, refresh
            ceiling, and first-frame timeout (defaults to the session's config).
        stop_event: A :class:`threading.Event`; setting it stops the loop after
            the current iteration.
        stop_reason: The :class:`WorkerStopReason` once the loop has terminated,
            or ``None`` while running / before start.
        error: A human-readable description of the failure when the loop stopped
            on an error condition; ``None`` for a clean stop.
    """

    def __init__(
        self,
        session,
        *,
        stream_config: Optional[StreamConfig] = None,
        stop_event: Optional[threading.Event] = None,
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create a worker for ``session``.

        Args:
            session: The :class:`StreamSession` to produce frames for. Its
                ``backend`` is driven by ``grab()`` and its ``publish()`` is the
                only slot writer.
            stream_config: Optional :class:`StreamConfig` override; defaults to
                ``session.stream_config``.
            stop_event: Optional externally-owned stop event so the broadcaster
                can stop the worker; a fresh :class:`threading.Event` is created
                when omitted.
            time_fn: Monotonic time source returning seconds; injected for
                deterministic tests (defaults to :func:`time.monotonic`).
            sleep_fn: Sleep function taking seconds; injected for deterministic
                tests (defaults to :func:`time.sleep`).
        """
        self.session = session
        self.stream_config = stream_config or getattr(session, "stream_config", None) or StreamConfig()
        self.stop_event = stop_event or threading.Event()
        self._time = time_fn
        self._sleep = sleep_fn

        self.stop_reason: Optional[WorkerStopReason] = None
        self.error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None

    # --- lifecycle --------------------------------------------------------

    def start(self) -> threading.Thread:
        """Run :meth:`run` on a dedicated daemon thread and return that thread.

        Convenience for the broadcaster, which runs one worker thread per
        session. Tests typically call :meth:`run` directly with an injected clock
        instead.
        """
        thread = threading.Thread(
            target=self.run,
            name=f"acquisition-worker-{getattr(self.session, 'camera_id', '?')}",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return thread

    def stop(self) -> None:
        """Signal the loop to stop after the current iteration (idempotent)."""
        self.stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        """Join the background thread started by :meth:`start`, if any."""
        if self._thread is not None:
            self._thread.join(timeout)

    # --- the loop ---------------------------------------------------------

    def run(self) -> WorkerStopReason:
        """Drive the ``grab -> publish`` loop until a stop condition is met.

        Returns the :class:`WorkerStopReason` describing why the loop ended. Side
        effects: publishes frames into ``session`` and transitions
        ``session.state`` (``RUNNING`` on the first frame, ``ERROR`` on
        first-frame timeout or disconnect).
        """
        backend = self.session.backend
        frame_timeout_ms = int(self.stream_config.frame_timeout_ms)
        first_frame_timeout_s = float(self.stream_config.first_frame_timeout_s)
        target_interval = self._target_interval()

        start = self._time()
        published_any = False
        camera_id = getattr(self.session, "camera_id", "?")

        while not self.stop_event.is_set():
            iter_start = self._time()

            frame = self._safe_grab(backend, frame_timeout_ms, camera_id)

            if frame is not None:
                # Successful grab: publish into the single latest-frame slot and
                # mark the session running on the first frame (Req 4.3 producer).
                self.session.publish(frame, now=self._time())
                if not published_any:
                    published_any = True
                    self.session.state = SessionState.RUNNING
                    logger.info(f"AcquisitionWorker {camera_id}: first frame published; session RUNNING")
            elif not published_any:
                # Start-up phase: tolerate transient failed grabs until the
                # first-frame deadline, then surface a first-frame timeout so the
                # session does not hang waiting for a stream that never starts
                # (Req 3.10, 3.11). The slot has nothing to retain yet.
                if self._time() - start >= first_frame_timeout_s:
                    self.session.state = SessionState.ERROR
                    self.error = (
                        f"camera {camera_id} produced no frame within "
                        f"{first_frame_timeout_s:g}s of stream start"
                    )
                    self.stop_reason = WorkerStopReason.FIRST_FRAME_TIMEOUT
                    logger.error(f"AcquisitionWorker {camera_id}: {self.error}")
                    return self.stop_reason
                # Otherwise keep trying; do NOT publish (nothing to overwrite).
            else:
                # Established stream lost a frame: mark disconnected and stop. The
                # last good frame stays in the slot because publish was not called
                # (Req 2.7); the broadcaster releases the claim on this ERROR
                # transition (Req 7.x cascade).
                self.session.state = SessionState.ERROR
                self.error = f"camera {camera_id} disconnected: failed to acquire a frame"
                self.stop_reason = WorkerStopReason.DISCONNECTED
                logger.error(f"AcquisitionWorker {camera_id}: {self.error}")
                return self.stop_reason

            # Rate ceiling: sleep only the time not already spent this iteration,
            # so we never exceed min_refresh_fps but never add delay below the
            # device's own cadence (Req 4.3, 4.4).
            if target_interval > 0.0:
                elapsed = self._time() - iter_start
                remaining = target_interval - elapsed
                if remaining > 0.0:
                    self._sleep(remaining)

        # Clean stop requested via the stop event.
        self.stop_reason = WorkerStopReason.STOPPED
        logger.info(f"AcquisitionWorker {camera_id}: stop requested; loop exited cleanly")
        return self.stop_reason

    # --- helpers ----------------------------------------------------------

    def _target_interval(self) -> float:
        """Minimum seconds per loop iteration implied by the refresh ceiling.

        Returns ``1 / min_refresh_fps`` (the rate ceiling) or ``0.0`` when no
        positive ceiling is configured, in which case the worker grabs as fast as
        the device delivers.
        """
        fps = float(getattr(self.stream_config, "min_refresh_fps", 0.0) or 0.0)
        if fps <= 0.0:
            return 0.0
        return 1.0 / fps

    def _safe_grab(self, backend, frame_timeout_ms: int, camera_id: str):
        """Grab one frame, treating an exception as a failed grab (``None``).

        The real backends already convert acquisition failures into ``None``; a
        raised exception is logged and treated identically so a misbehaving
        backend cannot crash the worker thread — it is handled by the same
        disconnect / first-frame-timeout path as a ``None`` result.
        """
        try:
            return backend.grab(frame_timeout_ms)
        except Exception as e:  # pragma: no cover - defensive; mocks may raise
            logger.error(f"AcquisitionWorker {camera_id}: grab raised {e!r}; treating as failed grab")
            return None
