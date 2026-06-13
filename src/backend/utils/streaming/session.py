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
"""Per-camera ``StreamSession``: latest-frame slot + viewer registry.

A :class:`StreamSession` owns exactly one physical camera's broadcast state:

* a **single-writer / many-reader latest-frame slot**. The acquisition worker is
  the only writer; it calls :meth:`publish` to replace the slot wholesale under a
  brief writer lock, assigning a monotonic sequence number and an acquired-at
  timestamp. Any number of viewers call :meth:`read_latest`, which copies the
  current reference out under the same lock and releases immediately — readers
  never hold the lock across I/O and never touch the device. Because the slot is
  replaced wholesale, two reads observing the same ``seq`` observe byte-identical
  frames (Req 2.2, 2.5) and repeated reads with no intervening publish return the
  same frame without a device grab (Req 2.4).
* a **viewer registry** (``add_viewer`` / ``remove_viewer`` / ``is_empty``) used by
  the broadcaster to drive start-on-first / stop-on-last lifecycle.
* the session **lifecycle state** (:class:`SessionState`) and a reference to the
  owning ``CameraBackend``.

:meth:`read_latest` classifies the slot against an injected ``now``:
``NO_FRAME`` when nothing has been published yet (Req 2.6), ``STALE`` when the
latest frame is older than ``stream_config.stale_after_s`` (Req 4.6, 4.7), and
``OK`` with the :class:`LatestFrame` otherwise.

This module is intentionally pure logic with no device or ``gi`` dependencies so
the session can be exercised deterministically against a mock backend and an
injected clock.
"""
from __future__ import annotations

import threading
from typing import Optional, Union

from utils.streaming.backends import RawFrame
from utils.streaming.models import (
    FrameResult,
    FrameStatus,
    LatestFrame,
    SessionState,
    StreamConfig,
    Viewer,
)


class StreamSession:
    """Owns one camera's latest-frame slot, viewer registry, and lifecycle state.

    The acquisition worker is the single producer driving :meth:`publish`; viewers
    are the many readers calling :meth:`read_latest`. Producer and readers are
    decoupled by a brief writer lock held only for the slot swap / reference copy,
    so readers never block each other or the producer (Req 2.4, 4.5).

    Attributes:
        camera_id: Identifier of the physical camera this session serves.
        backend: The ``CameraBackend`` holding this camera's single device claim.
            Stored as an opaque reference; the session never drives it directly.
        state: Current :class:`SessionState` lifecycle value.
        viewers: Mapping ``{viewer_id -> Viewer}`` of active subscriptions.
        latest: The current :class:`LatestFrame`, or ``None`` before the first
            publish.
        applied_features: The device-accepted camera control values currently in
            effect on the live claim, maintained by the broadcaster's settings-apply
            path so a failed apply can retain the prior in-effect values (Req 5.5).
    """

    def __init__(
        self,
        camera_id: str,
        backend: object = None,
        stream_config: Optional[StreamConfig] = None,
        state: SessionState = SessionState.STARTING,
    ) -> None:
        """Create a session for one physical camera.

        Args:
            camera_id: Identifier of the physical camera.
            backend: The ``CameraBackend`` for this camera (opaque reference).
            stream_config: :class:`StreamConfig` governing freshness
                (``stale_after_s``); defaults are used when omitted.
            state: Initial lifecycle state (defaults to ``STARTING``).
        """
        self.camera_id = camera_id
        self.backend = backend
        self.stream_config = stream_config or StreamConfig()
        self.state = state
        self.viewers: dict[str, Viewer] = {}
        self.latest: Optional[LatestFrame] = None

        # The camera control values currently in effect on the live claim (the
        # device-accepted gain / exposure / advanced GenICam values from the most
        # recent successful apply). Maintained by
        # ``StreamBroadcaster.apply_settings`` so that, when a later apply fails, the
        # broadcaster can both report which control failed and retain (and best-effort
        # restore) the values that were in effect *before* the failed request
        # (Req 5.5, Property 19). Empty until the first successful apply.
        self.applied_features: dict = {}

        # Guards both the latest-frame slot and the monotonic sequence counter.
        # Held only for the brief slot swap (writer) or reference copy (reader).
        self._slot_lock = threading.Lock()
        self._seq = 0

    # --- viewer registry --------------------------------------------------

    def add_viewer(self, viewer: Viewer) -> None:
        """Register a viewer in this session's registry.

        Duplicate subscriptions produce distinct viewer ids upstream, so each
        ``viewer.viewer_id`` is unique and counts toward the active total (Req 8.5).
        """
        self.viewers[viewer.viewer_id] = viewer

    def remove_viewer(self, viewer_id: str) -> bool:
        """Deregister a viewer by id.

        Args:
            viewer_id: The id of the viewer to remove. Removing an unknown id is a
                no-op.

        Returns:
            ``True`` if the registry is now empty (the caller should stop the
            session / release the claim), ``False`` otherwise.
        """
        self.viewers.pop(viewer_id, None)
        return self.is_empty()

    def is_empty(self) -> bool:
        """Return ``True`` when no viewers are currently registered."""
        return len(self.viewers) == 0

    def viewer_count(self) -> int:
        """Return the number of currently registered viewers."""
        return len(self.viewers)

    # --- latest-frame slot ------------------------------------------------

    def publish(
        self,
        frame: Union[RawFrame, bytes],
        width: Optional[int] = None,
        height: Optional[int] = None,
        now: float = 0.0,
    ) -> LatestFrame:
        """Publish a newly acquired frame into the single latest-frame slot.

        The single producer (acquisition worker) calls this on every successful
        grab. A monotonic sequence number and the supplied ``now`` (acquired-at)
        timestamp are assigned, and the slot is replaced wholesale under a brief
        writer lock so readers always observe a complete, consistent frame
        (Req 2.2, 2.5).

        Args:
            frame: Either a :class:`RawFrame` (carrying ``data`` / ``width`` /
                ``height``) or the raw payload ``bytes``. When ``bytes`` are passed,
                ``width`` and ``height`` must also be supplied.
            width: Frame width in pixels; required when ``frame`` is ``bytes``,
                ignored when ``frame`` is a :class:`RawFrame`.
            height: Frame height in pixels; required when ``frame`` is ``bytes``,
                ignored when ``frame`` is a :class:`RawFrame`.
            now: Epoch seconds when the frame was grabbed (injected clock).

        Returns:
            The :class:`LatestFrame` that was published (with its assigned ``seq``).
        """
        if isinstance(frame, RawFrame):
            data, frame_width, frame_height = frame.data, frame.width, frame.height
        else:
            if width is None or height is None:
                raise ValueError("width and height are required when publishing raw bytes")
            data, frame_width, frame_height = frame, width, height

        with self._slot_lock:
            self._seq += 1
            published = LatestFrame(
                data=data,
                width=frame_width,
                height=frame_height,
                seq=self._seq,
                acquired_at=now,
            )
            self.latest = published
        return published

    def read_latest(self, now: float) -> FrameResult:
        """Read the current latest frame and classify its freshness.

        Copies the current slot reference out under the writer lock and releases
        immediately (readers never hold the lock across work, never re-grab — the
        result is a pure function of the slot, Req 2.4). Classification:

        * ``NO_FRAME`` when no frame has been published yet (Req 2.6, 4.2).
        * ``STALE`` when ``now - acquired_at > stale_after_s`` (Req 4.6, 4.7).
        * ``OK`` with the :class:`LatestFrame` otherwise.

        Args:
            now: Current epoch seconds (injected clock) used for the freshness check.

        Returns:
            A :class:`FrameResult` carrying the status and, when ``OK``, the frame.
        """
        with self._slot_lock:
            frame = self.latest

        if frame is None:
            return FrameResult(status=FrameStatus.NO_FRAME, frame=None)

        if now - frame.acquired_at > self.stream_config.stale_after_s:
            return FrameResult(status=FrameStatus.STALE, frame=frame)

        return FrameResult(status=FrameStatus.OK, frame=frame)
