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
"""Core data models for the broadcast-style camera streaming stack.

These types are the shared vocabulary used by the ``StreamSession`` (latest-frame
slot + viewer registry) and the ``StreamBroadcaster`` (process-wide registry of
sessions). They are intentionally pure data holders with no device or threading
dependencies so the broadcaster/session logic can be exercised deterministically
against a mock backend and an injected clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


@dataclass(frozen=True)
class LatestFrame:
    """The most recently acquired frame held for a single physical camera.

    A new ``LatestFrame`` is published into a session's single-slot buffer
    wholesale on every successful grab, so readers always observe a complete,
    consistent frame (Req 2.2, 2.5).

    Attributes:
        data: Raw image payload (same shape as today's ``get_frame`` payload).
        width: Frame width in pixels.
        height: Frame height in pixels.
        seq: Monotonic per-session sequence number identifying an acquisition
            interval. Two reads observing the same ``seq`` observe identical bytes.
        acquired_at: Epoch seconds when the frame was grabbed from the device.
    """

    data: bytes
    width: int
    height: int
    seq: int
    acquired_at: float


@dataclass
class Viewer:
    """A single client subscription to a camera stream.

    Each browser tab / client polling a camera corresponds to one ``Viewer``.
    Duplicate subscriptions to the same camera produce distinct viewer ids and
    each counts toward the active viewer total (Req 8.5).

    Attributes:
        viewer_id: Server-issued unique id (e.g. a uuid).
        camera_id: Identifier of the physical camera this viewer is watching.
        subscribed_at: Epoch seconds when the viewer subscribed.
        last_active: Epoch seconds of the viewer's most recent heartbeat / frame
            GET; refreshed on activity and used for stale-viewer detection (Req 8.3).
    """

    viewer_id: str
    camera_id: str
    subscribed_at: float
    last_active: float


class SessionState(Enum):
    """Lifecycle state of a stream session for one physical camera."""

    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class FrameStatus(Enum):
    """Status returned to a viewer when it requests the current preview frame."""

    OK = "ok"
    NO_FRAME = "no_frame"          # session up, no frame published yet (Req 2.6, 4.2)
    STALE = "stale"               # latest frame older than the freshness ceiling (Req 4.7)
    DISCONNECTED = "disconnected"  # camera dropped / disconnected (Req 7.5)


@dataclass
class FrameResult:
    """Outcome of a viewer's request for the current preview frame.

    Attributes:
        status: Classification of the result (OK / NO_FRAME / STALE / DISCONNECTED).
        frame: The frame payload when ``status`` is ``OK``; otherwise ``None``.
        error: Optional human-readable error/context for non-OK statuses.
    """

    status: FrameStatus
    frame: Optional[LatestFrame] = None
    error: Optional[str] = None


@dataclass
class SubscribeResult:
    """Outcome of a viewer's subscription attempt.

    Attributes:
        viewer_id: Server-issued viewer id when accepted; ``None`` when rejected.
        accepted: Whether the subscription was accepted.
        reason: Rejection reason when not accepted: ``"viewer_limit"`` (Req 1.6)
            or ``"camera_unavailable"`` (Req 1.7, 3.2); ``None`` when accepted.
        viewer_count: Active viewer count for the camera after the attempt.
    """

    viewer_id: Optional[str]
    accepted: bool
    reason: Optional[str]
    viewer_count: int


# Bounds for timeout clamping (Req 7.1). Configured frame/open timeouts are
# clamped into this inclusive range on construction so a misconfigured value can
# never starve acquisition (too small) or hang the worker indefinitely (too large).
_MIN_TIMEOUT_MS = 500
_MAX_TIMEOUT_MS = 30000


def _clamp_timeout_ms(value: int) -> int:
    """Clamp a millisecond timeout into the accepted [500, 30000] range (Req 7.1)."""
    return max(_MIN_TIMEOUT_MS, min(_MAX_TIMEOUT_MS, value))


@dataclass
class StreamConfig:
    """Configurable bounds governing a stream session's timing and limits.

    These values tune acquisition timeouts, the device-open retry budget, viewer
    capacity, heartbeat/stale lifecycle windows, and frame freshness. They are pure
    data so the broadcaster/session logic can be exercised deterministically against
    a mock backend and an injected clock.

    ``frame_timeout_ms`` and ``open_timeout_ms`` are clamped into the inclusive range
    [500, 30000] ms on construction (Req 7.1), so out-of-range configuration values
    are corrected rather than rejected.

    Attributes:
        frame_timeout_ms: Bounded wait for a single grab; clamped to [500, 30000].
        open_timeout_ms: Bounded wait for a device open attempt; clamped to [500, 30000].
        max_open_attempts: Maximum device-open attempts before reporting the camera
            unavailable (Req 7.6).
        max_viewers: Maximum concurrent viewers per camera (Req 1.6).
        heartbeat_interval_s: Expected interval between viewer heartbeats.
        stale_timeout_s: Viewer is considered stale when inactive longer than this.
        first_frame_timeout_s: Maximum wait for the first published frame after start.
        stale_after_s: Freshness ceiling for served frames; older frames read STALE
            (Req 4.6).
        min_refresh_fps: Minimum target refresh rate for a fast-enough source (Req 4.3).
    """

    frame_timeout_ms: int = 5000      # clamp [500, 30000]
    open_timeout_ms: int = 5000       # clamp [500, 30000]
    max_open_attempts: int = 3
    max_viewers: int = 8
    heartbeat_interval_s: int = 10
    stale_timeout_s: int = 30
    first_frame_timeout_s: int = 10
    stale_after_s: float = 2.0        # freshness ceiling for served frames (Req 4.6)
    min_refresh_fps: float = 5.0      # Req 4.3

    def __post_init__(self) -> None:
        """Clamp the frame and open timeouts into [500, 30000] ms (Req 7.1)."""
        self.frame_timeout_ms = _clamp_timeout_ms(self.frame_timeout_ms)
        self.open_timeout_ms = _clamp_timeout_ms(self.open_timeout_ms)
