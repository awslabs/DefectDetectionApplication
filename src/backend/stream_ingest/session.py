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
"""``StreamSession``: one camera's supervised Stream_Worker
(rtsp-rtmp-stream-cameras design component 11; Requirements 7.5, 8.3-8.8).

States: ``connecting`` -> ``streaming`` -> ``reconnecting`` -> ``streaming``;
``failed`` after a configuration-class failure; ``stopped`` at the end.

- **Retries.** Transient failures back off 1, 2, 4, 8, 16, 30, 30... s, and
  the ladder restarts after 60 s of streaming. Configuration-class
  failures retry every 300 s, or at once when the configuration changes
  (:meth:`StreamSession.restart`). A worker exit is transient.
- **Watchdog.** A worker that sends nothing for 6 s (three missed health
  lines) is SIGKILLed; so is one that does not exit within 5 s of a stop.
  No first frame within the connect timeout is a ``timeout``; a streaming
  worker whose frames stop is a ``stall`` (the worker reports it; the
  session keeps a backstop).
- **Hardware fallback.** ``hardware_decoder_failed`` restarts the worker at
  once with ``failedHardware``, so the ``auto`` policy selects software and
  Stream_Health records ``decoderFallback``. A hardware decoder that takes
  data but yields no frame before the connect timeout counts as failed too.
- **Frames.** Consumers get only the Latest_Frame, with session sequence
  numbers that increase strictly across worker restarts. The session keeps
  one copied frame, and at most one more while a copy is in flight.

Everything time-related uses the injected clock, and the worker is started
through an injected ``spawn``, so the state machine is testable without
processes.
"""
from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from stream_ingest import health, protocol
from stream_ingest.health import StreamError, clean_message, is_configuration_category
from stream_ingest.pipeline import DEFAULT_PUBLISH_FPS

logger = logging.getLogger(__name__)

TRANSIENT_DELAYS_S = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
CONFIGURATION_RETRY_S = 300.0
BACKOFF_RESET_AFTER_S = 60.0
HEARTBEAT_TIMEOUT_S = 6.0
STOP_GRACE_S = 5.0
CONNECT_TIMEOUT_S = 20.0
#: Added to the stall timeout for the session's own stall check, which only
#: backs up the worker's (health lines arrive every 2 s).
STALL_BACKSTOP_MARGIN_S = 5.0
#: How much longer than the requested wait a frame reply may take.
REPLY_MARGIN_S = 1.0


def is_configuration_failure(category: str, has_streamed: bool) -> bool:
    """Whether a failure takes the configuration-class retry (Requirement
    8.6). ``not_found`` does only until the session has streamed under its
    current configuration: after that the path is known to exist, and a
    relay server or NVR answers 404 while its publisher is down, so the
    failure is retried like a transient one."""
    if category == health.NOT_FOUND and has_streamed:
        return False
    return is_configuration_category(category)


class Backoff:
    """The retry schedule of Requirements 8.5 and 8.6."""

    def __init__(self):
        self._attempt = 0

    def delay(self, category: str, streamed_for_s: float = 0.0, configuration: Optional[bool] = None) -> float:
        """The wait before the retry after a ``category`` failure that
        ended ``streamed_for_s`` seconds of streaming. ``configuration``
        overrides the category's retry class (see
        :func:`is_configuration_failure`)."""
        if streamed_for_s >= BACKOFF_RESET_AFTER_S:
            self._attempt = 0
        if configuration is None:
            configuration = is_configuration_category(category)
        if configuration:
            return CONFIGURATION_RETRY_S
        if category == health.HARDWARE_DECODER_FAILED:
            return 0.0
        delay = TRANSIENT_DELAYS_S[min(self._attempt, len(TRANSIENT_DELAYS_S) - 1)]
        self._attempt += 1
        return delay

    def reset(self) -> None:
        """A configuration change: the next failure starts the ladder anew."""
        self._attempt = 0


@dataclass(frozen=True)
class StreamFrame:
    """A Latest_Frame: packed RGB ``data`` of ``width`` x ``height``."""

    seq: int
    data: bytes
    width: int
    height: int
    acquired_at_ms: int

    def age_ms(self, now_ms: float) -> float:
        return max(0.0, now_ms - self.acquired_at_ms)


def _default_spawn(config, on_message, on_exit, secrets):
    from stream_ingest.launch import SubprocessWorker
    return SubprocessWorker(config, on_message, on_exit, secrets)


def _default_timer(delay_s: float, action: Callable[[], None]) -> None:
    timer = threading.Timer(delay_s, action)
    timer.daemon = True
    timer.start()


class StreamSession:
    """One camera's session (see the module docstring).

    ``source_provider()`` returns the ``StreamSource`` to connect to and may
    raise ``StreamError``; ``capabilities_provider()`` returns the
    Device_Stream_Capabilities. Both are called before every worker start.
    ``on_health(camera_key, health)`` is called on every state change.
    """

    def __init__(self, camera_key: str, source_provider: Callable[[], Any],
                 capabilities_provider: Callable[[], Dict[str, Any]],
                 spawn: Callable = _default_spawn,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 on_health: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 publish_fps: int = DEFAULT_PUBLISH_FPS,
                 connect_timeout_s: float = CONNECT_TIMEOUT_S,
                 reader_factory: Callable[[], Any] = protocol.FrameReader,
                 timer: Callable[[float, Callable[[], None]], None] = _default_timer):
        self.camera_key = camera_key
        self._source_provider = source_provider
        self._capabilities_provider = capabilities_provider
        self._spawn = spawn
        self._clock = clock
        self._wall = wall_clock
        self._on_health = on_health
        self._publish_fps = publish_fps
        self._connect_timeout_s = connect_timeout_s
        self._timer = timer

        self._lock = threading.RLock()
        self._reply_cond = threading.Condition(self._lock)
        self._frame_lock = threading.Lock()
        self._reader = reader_factory()

        self._state = health.CONNECTING
        self._started = False
        self._spawning = False
        self._generation = 0
        self._worker = None
        self._worker_segments: Dict[Any, set] = {}
        self._worker_started_at = 0.0
        self._last_line_at = 0.0
        self._last_progress_at = 0.0
        self._streaming_since: Optional[float] = None
        self._next_attempt_at: Optional[float] = None
        self._backoff = Backoff()
        self._failed_hardware = False
        #: Whether the session reached ``streaming`` under its current
        #: configuration (see :func:`is_configuration_failure`).
        self._has_streamed = False
        self._secrets: List[str] = []
        self._stall_timeout_s = 10.0

        self._worker_seq = 0
        self._seq_offset = 0
        self._seq_high = 0
        self._request_id = 0
        self._reply: Optional[Dict[str, Any]] = None
        self._cached: Optional[StreamFrame] = None
        self._in_flight = 0

        self._codec = None
        self._width = None
        self._height = None
        self._frame_width = None
        self._frame_height = None
        self._source_fps = None
        self._source_frames = 0
        self._decoder = None
        self._decoder_element = None
        self._decoder_fallback = False
        self._reconnects = 0
        self._last_frame_at_ms: Optional[int] = None
        self._last_error: Optional[Dict[str, Any]] = None
        self._leases = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the first worker."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._next_attempt_at = self._clock()
        self.tick()

    def restart(self, reason: str = "configuration changed", configuration_changed: bool = True) -> None:
        """Start a new worker now, with the current configuration, keeping
        the leases (Requirement 8.11). Clears a configuration-class wait,
        the backoff and a hardware-decoder fallback, and drops the cached
        frame: it came from the previous configuration (another URL or
        other credentials), so it is never served after a restart.

        ``configuration_changed`` False (a connection test retrying a
        waiting session now) keeps the record that the session streamed,
        which decides the retry class of a later ``not_found``."""
        with self._lock:
            if self._state == health.STOPPED:
                return
            self._generation += 1
            worker, self._worker = self._worker, None
            if worker is not None:
                self._discard_worker(worker)
            self._cached = None
            self._backoff.reset()
            self._failed_hardware = False
            if configuration_changed:
                self._has_streamed = False
            self._state = health.CONNECTING
            self._streaming_since = None
            self._started = True
            self._next_attempt_at = self._clock()
            self._reply_cond.notify_all()
        logger.info("Stream session %s restarting: %s", self.camera_key, reason)
        self._notify_health()
        self.tick()

    def stop(self, reason: str = "stopped") -> None:
        """Stop for good: the worker gets 5 s to exit, then SIGKILL."""
        with self._lock:
            if self._state == health.STOPPED:
                return
            self._state = health.STOPPED
            self._generation += 1
            self._next_attempt_at = None
            worker, self._worker = self._worker, None
            self._reply_cond.notify_all()
        if worker is not None:
            worker.stop()
            self._timer(STOP_GRACE_S, lambda: self._kill_if_alive(worker))
        logger.info("Stream session %s stopped: %s", self.camera_key, reason)
        self._notify_health()

    def _kill_if_alive(self, worker) -> None:
        if worker.alive():
            worker.kill()

    def _discard_worker(self, worker) -> None:
        """SIGKILL a worker that is no longer the session's (caller holds
        the lock). Its segments are removed when it exits."""
        worker.kill()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def set_leases(self, count: int) -> None:
        with self._lock:
            self._leases = int(count)

    # -- supervision -----------------------------------------------------------

    def _connect_failure_category(self) -> str:
        if self._decoder == "hardware" and self._source_frames > 0:
            return health.HARDWARE_DECODER_FAILED
        return health.TIMEOUT

    def tick(self, now: Optional[float] = None) -> None:
        """Supervise: start a worker when one is due, and fail a worker that
        is silent, stalled or cannot connect. Called by the manager's
        supervisor about four times a second."""
        notify = False
        due = False
        with self._lock:
            now = self._clock() if now is None else now
            if self._state == health.STOPPED or not self._started:
                return
            worker = self._worker
            if worker is None:
                due = (self._next_attempt_at is not None and now >= self._next_attempt_at
                       and not self._spawning)
                if due:
                    self._spawning = True
                    generation = self._generation
            elif now - self._last_line_at > HEARTBEAT_TIMEOUT_S:
                notify = self._fail_locked(health.WORKER_EXIT, "the stream worker stopped responding", now)
            elif (self._state == health.STREAMING
                  and now - self._last_progress_at > self._stall_timeout_s + STALL_BACKSTOP_MARGIN_S):
                notify = self._fail_locked(health.STALL, f"no frame arrived for {self._stall_timeout_s:g} s", now)
            elif self._state != health.STREAMING and now - self._worker_started_at > self._connect_timeout_s:
                notify = self._fail_locked(self._connect_failure_category(),
                                           f"no frame within {self._connect_timeout_s:g} s of connecting", now)
        if due:
            prepared = self._prepare_spawn()
            with self._lock:
                self._spawning = False
                if (self._state != health.STOPPED and self._worker is None
                        and generation == self._generation):
                    notify = self._spawn_locked(prepared, self._clock()) or notify
        if notify:
            self._notify_health()

    def _prepare_spawn(self):
        """The source and capabilities for the next worker, or the failure
        that prevents it. Runs without the lock: the first capability probe
        can take seconds."""
        try:
            return self._source_provider(), self._capabilities_provider()
        except StreamError as error:
            return error
        except Exception as error:  # noqa: BLE001 - becomes a transient failure
            return StreamError(health.NETWORK_ERROR,
                               f"the camera configuration could not be read ({type(error).__name__})")

    def _spawn_locked(self, prepared, now: float) -> bool:
        if isinstance(prepared, StreamError):
            return self._fail_locked(prepared.category, prepared.message, now)
        source, capabilities = prepared
        self._secrets = source.secret_values()
        self._stall_timeout_s = float(source.settings.get("stallTimeoutS") or 10)
        config = {
            "op": protocol.OP_CONFIG,
            "protocol": source.protocol,
            "url": source.url,
            "settings": dict(source.settings),
            "credentials": dict(source.credentials),
            "capabilities": capabilities,
            "failedHardware": self._failed_hardware,
            "publishFps": self._publish_fps,
        }
        try:
            worker = self._spawn(config, self._on_message, self._on_exit, list(self._secrets))
        except OSError as error:
            return self._fail_locked(health.WORKER_EXIT,
                                     f"the stream worker could not start ({type(error).__name__})", now)
        finally:
            config.clear()
        self._worker = worker
        self._next_attempt_at = None
        self._worker_segments[worker] = set()
        self._worker_started_at = self._last_line_at = self._last_progress_at = now
        self._worker_seq = 0
        self._seq_offset = self._seq_high
        self._source_frames = 0
        self._decoder = self._decoder_element = None
        if self._state == health.STREAMING:
            self._state = health.RECONNECTING
        return False

    def _fail_locked(self, category: Any, message: Any, now: float) -> bool:
        """Record a failure, drop the worker and schedule the retry (caller
        holds the lock). Returns True: the state changed."""
        category = health.normalize_category(category)
        text = clean_message(message, self._secrets) or category.replace("_", " ")
        worker, self._worker = self._worker, None
        stderr_tail: List[str] = []
        if worker is not None:
            stderr_tail = list(getattr(worker, "stderr_tail", lambda: [])())[-5:]
            self._discard_worker(worker)
        streamed_for = (now - self._streaming_since
                        if self._state == health.STREAMING and self._streaming_since is not None else 0.0)
        if category == health.HARDWARE_DECODER_FAILED:
            self._failed_hardware = True
        configuration = is_configuration_failure(category, self._has_streamed)
        delay = self._backoff.delay(category, streamed_for, configuration=configuration)
        self._next_attempt_at = now + delay
        self._state = health.FAILED if configuration else health.RECONNECTING
        self._streaming_since = None
        self._reconnects += 1
        self._last_error = {"category": category, "message": text, "atMs": int(self._wall() * 1000)}
        self._reply_cond.notify_all()
        logger.warning("Stream session %s: %s (%s); retrying in %g s", self.camera_key, text,
                       category, delay)
        for line in stderr_tail:
            logger.info("Stream session %s worker: %s", self.camera_key, clean_message(line, self._secrets))
        return True

    # -- worker callbacks ------------------------------------------------------

    def _on_message(self, worker, message: Dict[str, Any]) -> None:
        notify = False
        with self._lock:
            segments = self._worker_segments.get(worker)
            if message.get("op") == protocol.OP_FRAME and protocol.is_segment_path(message.get("shm")):
                if segments is not None:
                    segments.add(message["shm"])
            if worker is not self._worker:
                return
            now = self._clock()
            self._last_line_at = now
            op = message.get("op")
            if op == protocol.OP_HEALTH:
                notify = self._apply_health_locked(message, now)
            elif op == protocol.OP_FRAME:
                self._reply = message
                seq = message.get("seq")
                if isinstance(seq, int) and seq > self._worker_seq:
                    self._worker_seq, self._last_progress_at = seq, now
                self._reply_cond.notify_all()
            elif op == protocol.OP_ERROR:
                notify = self._fail_locked(message.get("category"), message.get("message"), now)
        if notify:
            self._notify_health()

    def _apply_health_locked(self, message: Dict[str, Any], now: float) -> bool:
        seq = message.get("seq")
        if isinstance(seq, int) and seq > self._worker_seq:
            self._worker_seq, self._last_progress_at = seq, now
        for attribute, key in (("_codec", "codec"), ("_width", "width"), ("_height", "height"),
                               ("_frame_width", "frameWidth"), ("_frame_height", "frameHeight"),
                               ("_decoder", "decoder"), ("_decoder_element", "decoderElement")):
            value = message.get(key)
            if value is not None:
                setattr(self, attribute, value)
        if message.get("sourceFps") is not None:
            self._source_fps = message.get("sourceFps")
        if isinstance(message.get("sourceFrames"), int):
            self._source_frames = message["sourceFrames"]
        if message.get("decoder") is not None:
            self._decoder_fallback = bool(message.get("decoderFallback"))
        if isinstance(message.get("lastFrameAtMs"), int):
            self._last_frame_at_ms = message["lastFrameAtMs"]
        if message.get("state") == health.STREAMING and self._state != health.STREAMING:
            self._state = health.STREAMING
            self._streaming_since = now
            self._has_streamed = True
            return True
        return False

    def _on_exit(self, worker, code: int) -> None:
        notify = False
        with self._lock:
            segments = self._worker_segments.pop(worker, set())
            if worker is self._worker:
                notify = self._fail_locked(health.WORKER_EXIT,
                                           f"the stream worker exited unexpectedly (exit code {code})",
                                           self._clock())
        for path in segments:
            protocol.remove_segment(path)
        if notify:
            self._notify_health()

    # -- frames ------------------------------------------------------------------

    def _fresh(self, frame: Optional[StreamFrame], after_seq: int, max_age_ms: Optional[float]) -> bool:
        if frame is None or frame.seq <= after_seq:
            return False
        return max_age_ms is None or frame.age_ms(self._wall() * 1000) <= max_age_ms

    def latest_frame(self, after_seq: int = 0, max_age_ms: Optional[float] = None,
                     wait_ms: float = 0) -> Optional[StreamFrame]:
        """The newest frame with a sequence number above ``after_seq`` and,
        when ``max_age_ms`` is set, no older than that; waiting up to
        ``wait_ms`` for one. None when there is none by then."""
        deadline = self._clock() + max(0.0, wait_ms) / 1000.0
        with self._frame_lock:
            while True:
                with self._lock:
                    cached = self._cached
                    satisfied = self._fresh(cached, after_seq, max_age_ms)
                    streaming = self._worker is not None and self._state == health.STREAMING
                remaining_ms = max(0.0, (deadline - self._clock()) * 1000.0)
                if streaming:
                    ask_after = max(after_seq, cached.seq if cached is not None else 0)
                    frame = self._request_frame(ask_after, 0 if satisfied else remaining_ms)
                    if frame is not None and self._fresh(frame, after_seq, max_age_ms):
                        return frame
                if satisfied:
                    return cached
                if self._clock() >= deadline:
                    return None
                if not streaming:
                    # Not streaming: wait for the session to (re)connect.
                    with self._lock:
                        self._reply_cond.wait(min(0.1, max(0.0, deadline - self._clock())))

    def _request_frame(self, after_session_seq: int, wait_ms: float) -> Optional[StreamFrame]:
        """Ask the worker for its newest frame after ``after_session_seq``
        (caller holds the frame lock)."""
        with self._lock:
            worker = self._worker
            if worker is None:
                return None
            offset = self._seq_offset
            self._request_id += 1
            request_id = self._request_id
            self._reply = None
        wait_ms = max(0, int(wait_ms))
        if not worker.send({"op": protocol.OP_FRAME, "id": request_id,
                            "after": max(0, after_session_seq - offset), "waitMs": wait_ms}):
            return None
        with self._lock:
            end = self._clock() + wait_ms / 1000.0 + REPLY_MARGIN_S
            while ((self._reply is None or self._reply.get("id") != request_id)
                   and worker is self._worker):
                remaining = end - self._clock()
                if remaining <= 0:
                    break
                self._reply_cond.wait(remaining)
            reply = self._reply if self._reply is not None and self._reply.get("id") == request_id else None
            self._reply = None
        if reply is None or not isinstance(reply.get("seq"), int):
            return None
        with self._lock:
            self._in_flight += 1
        try:
            data = self._reader.read(reply)
        except (OSError, ValueError):
            return None
        finally:
            with self._lock:
                self._in_flight -= 1
        frame = StreamFrame(seq=offset + reply["seq"], data=data, width=int(reply["width"]),
                            height=int(reply["height"]), acquired_at_ms=int(reply.get("acquiredAtMs") or 0))
        with self._lock:
            if self._cached is None or frame.seq > self._cached.seq:
                self._cached = frame
            self._seq_high = max(self._seq_high, frame.seq)
            if self._last_frame_at_ms is None or frame.acquired_at_ms > self._last_frame_at_ms:
                self._last_frame_at_ms = frame.acquired_at_ms
        return frame

    def newest_seq(self) -> int:
        """The sequence number of the newest frame this session has handed
        out (0 before the first). A caller that must see a frame delivered
        after a point in time, such as a connection test, asks for frames
        after this value instead of taking the cached one, which can
        predate an outage."""
        with self._lock:
            return self._seq_high

    def buffers_held(self) -> int:
        """Frame buffers the session references: the cached frame and any
        copy in flight (Requirement 8.3: at most two)."""
        with self._lock:
            return (1 if self._cached is not None else 0) + self._in_flight

    # -- health ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """The Stream_Health document."""
        with self._lock:
            document = health.empty_health(self.camera_key, self._state)
            document.update({
                "codec": self._codec,
                "width": self._width,
                "height": self._height,
                "frameWidth": self._frame_width,
                "frameHeight": self._frame_height,
                "sourceFps": self._source_fps,
                "decoder": self._decoder,
                "decoderElement": self._decoder_element,
                "decoderFallback": self._decoder_fallback,
                "reconnects": self._reconnects,
                "lastFrameAtMs": self._last_frame_at_ms,
                "lastError": dict(self._last_error) if self._last_error else None,
                "leases": self._leases,
            })
            if self._state in (health.RECONNECTING, health.FAILED) and self._next_attempt_at is not None:
                document["nextAttemptInS"] = round(max(0.0, self._next_attempt_at - self._clock()), 1)
            return document

    def _notify_health(self) -> None:
        if self._on_health is None:
            return
        try:
            self._on_health(self.camera_key, self.health())
        except Exception:  # noqa: BLE001 - a listener must not break supervision
            logger.exception("Stream session %s: a health listener failed", self.camera_key)
