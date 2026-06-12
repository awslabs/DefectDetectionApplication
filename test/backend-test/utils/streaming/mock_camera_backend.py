# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Shared test double for the ``CameraBackend`` protocol.

This module provides the in-memory, hardware-free backend used by the
property-based and unit tests for the concurrent-camera-stream stack
(tasks 3.x / 4.x / 5.x / 7.x / 8.x). It lets those tests exercise the
``StreamSession`` / ``StreamBroadcaster`` / acquisition-worker logic
deterministically, without real Aravis or GStreamer devices.

The double:

* satisfies the :class:`utils.streaming.backends.CameraBackend` Protocol
  (``open`` / ``start_stream`` / ``grab`` / ``apply_features`` /
  ``stop_stream`` / ``close``);
* records the count **and order** of every lifecycle call so tests can assert
  on the exact interaction sequence;
* can simulate an ``open()`` failure (raise), a ``grab()`` timeout/failure
  (return ``None``), a ``stop_stream()`` failure (raise), and — for the
  disconnect/settings properties — ``close()`` / ``apply_features()`` failures;
* serves a scripted / queued sequence of frames (or ``None``/exceptions) from
  ``grab()``;
* tracks whether a ``Device_Claim`` is currently open, optionally registering
  with a shared :class:`ClaimRegistry` so tests can assert the single-claim
  invariant (claim count is 0/1, never overlapping) across many cameras;
* comes in mock **Aravis** and **GStreamer** variants (a ``backend_kind`` flag
  plus :class:`MockAravisBackend` / :class:`MockGStreamerBackend` subclasses) so
  backend-parity property tests can parametrize over both (Req 2.8).

It also ships a controllable :class:`MockClock` so timing-dependent logic
(staleness, freshness, first-frame timeout, stale sweep) is deterministic in
session / broadcaster tests.

The module imports only ``RawFrame`` from ``utils.streaming.backends`` (and
``StreamConfig`` is intentionally not required), which is import-safe on hosts
without the ``gi`` / GenICam / GStreamer stack because those backends keep their
``gi`` imports lazy.

Importing this raises nothing requiring native libraries:

    >>> from mock_camera_backend import MockCameraBackend, MockClock
"""
from __future__ import annotations

from collections import deque
from typing import Callable, Deque, Iterable, List, Optional, Union

from utils.streaming.backends import CameraBackend, RawFrame

__all__ = [
    "MockClock",
    "ClaimRegistry",
    "MockBackendError",
    "MockOpenError",
    "MockStopError",
    "MockCloseError",
    "MockApplyError",
    "MockGrabError",
    "MockCameraBackend",
    "MockAravisBackend",
    "MockGStreamerBackend",
    "BACKEND_KINDS",
    "MOCK_BACKEND_CLASSES",
    "make_backend",
    "make_raw_frame",
]


# --------------------------------------------------------------------------- #
# Injectable clock
# --------------------------------------------------------------------------- #
class MockClock:
    """A controllable, monotonic time source for deterministic timing tests.

    Replaces ``time.time`` / ``time.monotonic`` / ``time.sleep`` in the
    session and broadcaster so freshness (``stale_after_s``), staleness
    (``stale_timeout_s``), first-frame, and sweep windows can be advanced
    explicitly rather than by waiting on a wall clock.

    The clock never goes backwards. ``sleep`` advances the clock by the given
    number of seconds instead of blocking, so worker/loop code under test runs
    instantly while still observing the passage of (virtual) time.
    """

    def __init__(self, start: float = 0.0) -> None:
        """Create a clock positioned at ``start`` (epoch seconds)."""
        self._now = float(start)

    def time(self) -> float:
        """Return the current virtual time in seconds (epoch-style)."""
        return self._now

    # Alias so the clock can stand in for either ``time.time`` or
    # ``time.monotonic`` wherever the code under test expects a callable.
    def monotonic(self) -> float:
        """Return the current virtual time; alias of :meth:`time`."""
        return self._now

    def __call__(self) -> float:
        """Allow the clock instance itself to be used as a ``now()`` callable."""
        return self._now

    def advance(self, seconds: float) -> float:
        """Advance the clock by ``seconds`` (must be non-negative). Returns now."""
        if seconds < 0:
            raise ValueError("MockClock cannot move backwards")
        self._now += float(seconds)
        return self._now

    def set(self, value: float) -> float:
        """Jump the clock to ``value`` seconds (must not move backwards)."""
        value = float(value)
        if value < self._now:
            raise ValueError("MockClock cannot move backwards")
        self._now = value
        return self._now

    def sleep(self, seconds: float) -> None:
        """Stand-in for ``time.sleep`` that advances virtual time instead of blocking."""
        self.advance(seconds)


# --------------------------------------------------------------------------- #
# Shared single-claim registry
# --------------------------------------------------------------------------- #
class ClaimRegistry:
    """Tracks open ``Device_Claim``s per camera across backend instances.

    Pass the same registry to every backend a test creates so the test can
    assert the single-claim invariant (Property 1): at most one open claim per
    camera at any time, never overlapping (a new claim is never opened before
    the prior claim for the same camera is released).

    The registry records, per ``camera_id``, the current number of open claims
    and the maximum ever observed, and flags ``overlap_detected`` if a second
    claim is ever acquired while one is still open.
    """

    def __init__(self) -> None:
        self.open_claims: dict = {}        # camera_id -> current open count
        self.max_concurrent: dict = {}     # camera_id -> max ever observed
        self.overlap_detected: bool = False
        self.released_without_open: bool = False

    def acquire(self, camera_id: str) -> None:
        """Record that a claim was opened for ``camera_id``."""
        count = self.open_claims.get(camera_id, 0) + 1
        self.open_claims[camera_id] = count
        if count > 1:
            self.overlap_detected = True
        self.max_concurrent[camera_id] = max(self.max_concurrent.get(camera_id, 0), count)

    def release(self, camera_id: str) -> None:
        """Record that a claim was released for ``camera_id``."""
        count = self.open_claims.get(camera_id, 0)
        if count <= 0:
            self.released_without_open = True
            return
        self.open_claims[camera_id] = count - 1

    def open_count(self, camera_id: str) -> int:
        """Return the number of claims currently open for ``camera_id``."""
        return self.open_claims.get(camera_id, 0)

    def total_open(self) -> int:
        """Return the total number of open claims across all cameras."""
        return sum(self.open_claims.values())

    @property
    def single_claim_ok(self) -> bool:
        """True iff no overlap was ever seen and no claim is doubly open now."""
        return (
            not self.overlap_detected
            and not self.released_without_open
            and all(count <= 1 for count in self.open_claims.values())
        )

    def assert_single_claim(self) -> None:
        """Assert the single-claim invariant held throughout (test convenience)."""
        assert not self.overlap_detected, (
            f"overlapping Device_Claim detected: {self.max_concurrent}"
        )
        assert not self.released_without_open, "claim released without a matching open"
        for camera_id, count in self.open_claims.items():
            assert count <= 1, f"camera {camera_id} has {count} concurrent claims"


# --------------------------------------------------------------------------- #
# Mock backend errors (import-safe; no gi / native dependency)
# --------------------------------------------------------------------------- #
class MockBackendError(Exception):
    """Base error raised by :class:`MockCameraBackend` failure simulations."""


class MockOpenError(MockBackendError):
    """Raised by ``open()`` when an open failure is simulated (Req 7.6)."""


class MockStopError(MockBackendError):
    """Raised by ``stop_stream()`` when a stop failure is simulated (Req 3.5)."""


class MockCloseError(MockBackendError):
    """Raised by ``close()`` when a claim-release failure is simulated."""


class MockApplyError(MockBackendError):
    """Raised by ``apply_features()`` when a control-apply failure is simulated (Req 5.5)."""


class MockGrabError(MockBackendError):
    """A grab failure that surfaces as an exception (vs. a ``None`` timeout)."""


# A scripted grab result: a frame, ``None`` (timeout/failure), a callable
# producing one of those, or an exception instance/class to raise.
GrabScriptItem = Union[
    RawFrame,
    None,
    BaseException,
    "type[BaseException]",
    Callable[[], Optional[RawFrame]],
]


def make_raw_frame(seq: int = 0, width: int = 4, height: int = 4, data: Optional[bytes] = None) -> RawFrame:
    """Build a deterministic :class:`RawFrame` for scripting ``grab()`` results.

    When ``data`` is omitted, a unique, reproducible byte payload derived from
    ``seq`` is generated so different scripted frames are byte-distinguishable
    (useful for fan-out / latest-frame assertions).
    """
    if data is None:
        data = f"frame-{seq}".encode("utf-8")
    return RawFrame(data=data, width=width, height=height)


# --------------------------------------------------------------------------- #
# The mock backend
# --------------------------------------------------------------------------- #
class MockCameraBackend:
    """In-memory :class:`CameraBackend` double for hardware-free tests.

    Drives the same lifecycle the broadcaster/worker expect — ``open`` ->
    ``start_stream`` -> repeated ``grab`` -> ``stop_stream`` -> ``close`` — while
    recording every call and serving scripted frames. See the module docstring
    for the full capability list.

    Failure simulation knobs (all default to "no failure"):

    * ``open_failures``: number of leading ``open()`` calls that raise before a
      subsequent call succeeds (models the ``<= max_open_attempts`` retry budget).
    * ``fail_open``: when True, **every** ``open()`` raises.
    * ``fail_stop`` / ``fail_close`` / ``fail_apply``: when True the
      corresponding call raises.

    Frame scripting:

    * ``frames``: an initial iterable of scripted ``grab()`` results.
    * ``queue_frame`` / ``queue_frames`` / ``queue_timeout`` / ``queue_error``:
      append results at runtime.
    * ``default_grab``: result returned once the scripted queue is exhausted
      (defaults to ``None``, i.e. a timeout, which the worker treats as a
      disconnect signal).
    """

    #: Identifies which real backend family this double stands in for.
    backend_kind: str = "generic"

    def __init__(
        self,
        camera_id: str = "Fake_1",
        *,
        frames: Optional[Iterable[GrabScriptItem]] = None,
        default_grab: GrabScriptItem = None,
        open_failures: int = 0,
        fail_open: bool = False,
        fail_stop: bool = False,
        fail_close: bool = False,
        fail_apply: bool = False,
        claim_registry: Optional[ClaimRegistry] = None,
        clock: Optional[MockClock] = None,
        open_error: Callable[[], BaseException] = None,
        stop_error: Callable[[], BaseException] = None,
        close_error: Callable[[], BaseException] = None,
        apply_error: Callable[[], BaseException] = None,
    ) -> None:
        self.camera_id = camera_id

        # --- call accounting ---------------------------------------------- #
        self.call_log: List[str] = []
        self.open_count = 0
        self.open_failure_count = 0
        self.close_count = 0
        self.start_stream_count = 0
        self.stop_stream_count = 0
        self.grab_count = 0
        self.apply_features_count = 0

        # --- lifecycle state ---------------------------------------------- #
        self.is_open = False
        self.is_streaming = False
        self.last_grab_timeout_ms: Optional[int] = None

        # --- failure simulation ------------------------------------------- #
        self._pending_open_failures = int(open_failures)
        self.fail_open = bool(fail_open)
        self.fail_stop = bool(fail_stop)
        self.fail_close = bool(fail_close)
        self.fail_apply = bool(fail_apply)
        self._open_error = open_error or (lambda: MockOpenError(f"open failed for {camera_id}"))
        self._stop_error = stop_error or (lambda: MockStopError(f"stop failed for {camera_id}"))
        self._close_error = close_error or (lambda: MockCloseError(f"close failed for {camera_id}"))
        self._apply_error = apply_error or (lambda: MockApplyError(f"apply failed for {camera_id}"))

        # --- frame scripting ---------------------------------------------- #
        self._grab_queue: Deque[GrabScriptItem] = deque(frames or [])
        self.default_grab: GrabScriptItem = default_grab

        # --- single-claim tracking + clock -------------------------------- #
        self._claim_registry = claim_registry
        self.clock = clock

        # --- applied-feature history (for settings properties) ------------ #
        self.applied_features: List[dict] = []
        self.last_applied_features: Optional[dict] = None

    # ----- CameraBackend protocol ----------------------------------------- #
    def open(self) -> None:
        """Acquire the single ``Device_Claim``; may raise to simulate failure.

        Honors ``open_failures`` (raise N times then succeed) and ``fail_open``
        (always raise). On success marks the claim open and registers with the
        shared :class:`ClaimRegistry` if one was provided.
        """
        self._record("open")
        self.open_count += 1

        if self.fail_open or self._pending_open_failures > 0:
            if self._pending_open_failures > 0:
                self._pending_open_failures -= 1
            self.open_failure_count += 1
            raise self._open_error()

        # A correct caller never re-opens an already-open claim; surface it so a
        # bug in the code under test is caught rather than silently masked.
        if self.is_open:
            raise MockBackendError(
                f"open() called on {self.camera_id} while a claim is already open"
            )

        self.is_open = True
        if self._claim_registry is not None:
            self._claim_registry.acquire(self.camera_id)

    def start_stream(self) -> None:
        """Begin continuous acquisition; requires an open claim."""
        self._record("start_stream")
        self.start_stream_count += 1
        self._require_open()
        self.is_streaming = True

    def grab(self, timeout_ms: int) -> Optional[RawFrame]:
        """Return the next scripted frame, ``None`` (timeout/failure), or raise.

        Records the requested ``timeout_ms`` (so timeout-clamping behaviour can
        be asserted). Pops the next scripted item; once the queue is drained the
        ``default_grab`` result is used. A queued exception (class or instance)
        is raised; a callable item is invoked to produce the result.
        """
        self._record("grab")
        self.grab_count += 1
        self.last_grab_timeout_ms = timeout_ms

        if not self.is_open:
            # No claim -> behaves like a failed grab (None), matching the real
            # adapters which return None when the device handle is absent.
            return None

        item: GrabScriptItem = self._grab_queue.popleft() if self._grab_queue else self.default_grab
        return self._resolve_grab_item(item)

    def apply_features(self, features: dict) -> dict:
        """Apply control values to the live stream; echoes accepted values.

        Records each request. When ``fail_apply`` is set the call raises
        (Property 19). Otherwise the supplied feature mapping is normalized to a
        plain dict, stored, and returned as the device-accepted set.
        """
        self._record("apply_features")
        self.apply_features_count += 1
        self.applied_features.append(features)

        if self.fail_apply:
            raise self._apply_error()

        accepted = self._normalize_features(features)
        self.last_applied_features = accepted
        return accepted

    def stop_stream(self) -> None:
        """Stop acquisition without releasing the claim; may raise (Req 3.5)."""
        self._record("stop_stream")
        self.stop_stream_count += 1
        if self.fail_stop:
            raise self._stop_error()
        self.is_streaming = False

    def close(self) -> None:
        """Release the single ``Device_Claim``.

        Even when a close failure is simulated the claim is treated as released
        (state cleared and the registry decremented) *before* the error is
        raised, mirroring the design's "force claim release on stop/close
        failure" handling so the single-claim invariant is preserved.
        """
        self._record("close")
        self.close_count += 1

        was_open = self.is_open
        self.is_open = False
        self.is_streaming = False
        if was_open and self._claim_registry is not None:
            self._claim_registry.release(self.camera_id)

        if self.fail_close:
            raise self._close_error()

    # ----- frame-scripting helpers ---------------------------------------- #
    def queue_frame(self, frame: GrabScriptItem) -> "MockCameraBackend":
        """Append a single scripted ``grab()`` result. Returns self for chaining."""
        self._grab_queue.append(frame)
        return self

    def queue_frames(self, frames: Iterable[GrabScriptItem]) -> "MockCameraBackend":
        """Append several scripted ``grab()`` results. Returns self for chaining."""
        self._grab_queue.extend(frames)
        return self

    def queue_timeout(self, count: int = 1) -> "MockCameraBackend":
        """Append ``count`` grab timeouts (``None`` results). Returns self."""
        self._grab_queue.extend([None] * count)
        return self

    def queue_error(self, error: GrabScriptItem = MockGrabError) -> "MockCameraBackend":
        """Append a grab that raises ``error``. Returns self for chaining."""
        self._grab_queue.append(error)
        return self

    def script_sequence(self, count: int, *, start_seq: int = 0,
                         width: int = 4, height: int = 4) -> List[RawFrame]:
        """Queue ``count`` distinct frames and return the list that was queued.

        Convenience for fan-out / cadence tests that need a known, ordered,
        byte-distinguishable sequence of frames to verify against.
        """
        frames = [
            make_raw_frame(seq=start_seq + i, width=width, height=height)
            for i in range(count)
        ]
        self.queue_frames(frames)
        return frames

    @property
    def remaining_frames(self) -> int:
        """Number of scripted grab results not yet consumed."""
        return len(self._grab_queue)

    # ----- internal helpers ----------------------------------------------- #
    def _record(self, name: str) -> None:
        """Append ``name`` to the ordered call log."""
        self.call_log.append(name)

    def _require_open(self) -> None:
        """Raise if the claim has not been acquired via :meth:`open`."""
        if not self.is_open:
            raise MockBackendError(
                f"{self.camera_id} is not open; call open() before this operation"
            )

    @staticmethod
    def _resolve_grab_item(item: GrabScriptItem) -> Optional[RawFrame]:
        """Turn a scripted grab item into a concrete ``RawFrame`` / ``None`` / raise."""
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        if isinstance(item, BaseException):
            raise item
        if callable(item) and not isinstance(item, RawFrame):
            item = item()
        if item is None or isinstance(item, RawFrame):
            return item
        raise TypeError(f"Unsupported scripted grab item: {item!r}")

    @staticmethod
    def _normalize_features(features) -> dict:
        """Normalize an ``apply_features`` argument into a plain accepted dict."""
        if features is None:
            return {}
        if isinstance(features, dict):
            return dict(features)
        if isinstance(features, list):
            accepted: dict = {}
            for entry in features:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("feature")
                if name is not None:
                    accepted[name] = entry.get("value")
            return accepted
        # Fall back to a best-effort wrapper for unexpected shapes.
        return {"value": features}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"{type(self).__name__}(camera_id={self.camera_id!r}, kind={self.backend_kind!r}, "
            f"open={self.is_open}, streaming={self.is_streaming}, "
            f"opens={self.open_count}, grabs={self.grab_count}, closes={self.close_count})"
        )


class MockAravisBackend(MockCameraBackend):
    """Mock standing in for the Aravis (GenICam / USB3Vision) backend (Req 2.8)."""

    backend_kind = "aravis"


class MockGStreamerBackend(MockCameraBackend):
    """Mock standing in for the GStreamer (NVIDIA CSI / ICAM) backend (Req 2.8)."""

    backend_kind = "gstreamer"


# Parametrization helpers so backend-parity property tests can run over both
# families (e.g. ``@pytest.mark.parametrize("backend_cls", MOCK_BACKEND_CLASSES.values())``).
BACKEND_KINDS = ("aravis", "gstreamer")
MOCK_BACKEND_CLASSES = {
    "aravis": MockAravisBackend,
    "gstreamer": MockGStreamerBackend,
}


def make_backend(kind: str = "aravis", camera_id: str = "Fake_1", **kwargs) -> MockCameraBackend:
    """Construct a mock backend of the requested ``kind`` ("aravis"|"gstreamer").

    Any additional keyword arguments are forwarded to
    :class:`MockCameraBackend` (e.g. ``claim_registry``, ``frames``,
    ``open_failures``, ``fail_stop``).
    """
    try:
        cls = MOCK_BACKEND_CLASSES[kind]
    except KeyError:
        raise ValueError(f"unknown backend kind {kind!r}; expected one of {BACKEND_KINDS}")
    return cls(camera_id, **kwargs)


# Runtime sanity: the mock must satisfy the runtime-checkable CameraBackend
# Protocol. Performed at import so a drift in the protocol surface is caught by
# any test that imports this module.
assert isinstance(
    MockCameraBackend("__protocol_check__"), CameraBackend
), "MockCameraBackend does not satisfy the CameraBackend protocol"
