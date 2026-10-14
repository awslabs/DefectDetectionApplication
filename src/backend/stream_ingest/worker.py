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
"""The Stream_Worker process (rtsp-rtmp-stream-cameras design component 11;
Requirements 6.4, 6.5, 7.1-7.3, 7.6, 7.7, 12.7).

    python -m stream_ingest.worker

One worker runs one camera's ingest pipeline, isolated from the backend: a
decoder crash, a hang or a leak ends this process only. The worker:

1. reads the ``config`` line from stdin, the only place credentials travel
   (never argv, the environment or a pipeline description);
2. builds the ingest head: ``rtspsrc`` with the credentials as element
   properties, or a PyAV RTMP pull feeding ``appsrc``;
3. links the first video track to the decode tail and every other pad to a
   ``fakesink`` (Requirement 7.3), choosing the decoder by the
   Decoder_Policy once the codec is known;
4. scales to the maximum frame dimension from the first negotiated caps,
   again whenever the resolution changes;
5. serves ``frame`` requests from shared memory and reports health every
   2 s and failures as categorized, redacted ``error`` lines.

GStreamer debug output is capped at level 2 and goes to this process's
stderr, which the parent keeps in a bounded buffer and redacts; the
backend's ``gst-debug.log`` is never written. Protocol lines go to a private
copy of stdout, and file descriptor 1 is pointed at stderr, so a stray print
from a library cannot corrupt the protocol.
"""
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

from stream_ingest import classify, health, protocol
from stream_ingest.health import StreamError, clean_message
from stream_ingest.pipeline import (
    APPSINK_NAME,
    DECODER_NAME,
    DEFAULT_PUBLISH_FPS,
    HARDWARE,
    PARSER_NAME,
    RATE_NAME,
    SCALE_CAPSFILTER_NAME,
    RateLimiter,
    codec_label,
    depayloader,
    elementary_caps,
    fit_within,
    normalize_codec,
    rtsp_head,
    scale_caps,
    scaled_format,
    select_decoder,
    tail_description,
)
from workflow_engine.vendor.workflow_core.stream_url import compose_connect_url

EXIT_OK = 0
EXIT_FAILED = 3
EXIT_BAD_CONFIG = 4

HEALTH_INTERVAL_S = 2.0
MAX_FRAME_WAIT_MS = 10_000

#: rtspsrc handles a 404 like a 401 (some cameras hide paths from anonymous
#: clients): with no authentication header to use it first posts this
#: generic error, then the response's own (``Not found``, ``Unauthorized``).
#: Found on hardware, task 25.3: without looking past it, a wrong path was
#: reported as an authentication failure.
RTSP_AUTH_SETUP_ERROR = "no supported authentication protocol"
#: How long to wait for the specific error that follows it; rtspsrc posts
#: both from the same call.
FOLLOWING_ERROR_WAIT_MS = 500
#: Bound on the RTMP elementary-stream queue; a full queue blocks the demux
#: thread, which applies TCP back-pressure instead of growing memory.
APPSRC_MAX_BYTES = 4 * 1024 * 1024

logger = logging.getLogger("stream_ingest.worker")


def make_rate_probe(Gst, limiter: RateLimiter, clock=time.monotonic):
    """The BUFFER probe that applies the publish cap on the ``rate``
    element's sink pad: frames the :class:`RateLimiter` rejects are
    dropped. Every frame is timed by its arrival on ``clock`` (seconds),
    never by its PTS, which real cameras do not always set (see
    :class:`RateLimiter`). Unlike ``videorate``, no duration is needed."""

    def probe(_pad, _info):
        return Gst.PadProbeReturn.OK if limiter.keep(int(clock() * 1_000_000_000)) else Gst.PadProbeReturn.DROP

    return probe


def is_rtsp_auth_setup_error(domain: Optional[str], code: Optional[int], debug: Optional[str]) -> bool:
    """Whether a bus error is rtspsrc's generic "no supported
    authentication protocol" error, which precedes the response's own
    (see :data:`RTSP_AUTH_SETUP_ERROR`)."""
    return (domain == classify.GST_RESOURCE_DOMAIN and code == classify.RESOURCE_OPEN_READ
            and RTSP_AUTH_SETUP_ERROR in (debug or "").lower())


def certificate_problems(errors: Any) -> str:
    """The ``GTlsCertificateFlags`` of a rejected certificate as their
    nicks (``unknown-ca``, ``bad-identity``, ``expired``, ...)."""
    nicks = getattr(errors, "value_nicks", None)
    if nicks:
        return ", ".join(str(nick) for nick in nicks)
    try:
        value = int(errors)
    except (TypeError, ValueError):
        return "unknown reason"
    names = [name for bit, name in _TLS_FLAG_NICKS if value & bit]
    return ", ".join(names) or "unknown reason"


#: ``GTlsCertificateFlags`` bits, for a flags value without nicks.
_TLS_FLAG_NICKS = ((1, "unknown-ca"), (2, "bad-identity"), (4, "not-activated"), (8, "expired"),
                   (16, "revoked"), (32, "insecure"), (64, "generic-error"))


def prepare_environment() -> None:
    """GStreamer debug at WARNING, to stderr only (Requirement 12.7)."""
    os.environ["GST_DEBUG"] = "2"
    os.environ["GST_DEBUG_NO_COLOR"] = "1"
    os.environ.pop("GST_DEBUG_FILE", None)


def load_gstreamer():
    """``(Gst, GstVideo)``, initialized."""
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstApp", "1.0")
    gi.require_version("GstVideo", "1.0")
    from gi.repository import Gst, GstApp, GstVideo  # noqa: F401 - GstApp types the appsink
    Gst.init(None)
    return Gst, GstVideo


class LineWriter:
    """Thread-safe protocol line writer. A closed pipe (the parent is gone)
    is remembered, not raised."""

    def __init__(self, stream):
        self._stream = stream
        self._lock = threading.Lock()
        self.closed = False

    def send(self, message: Dict[str, Any]) -> None:
        data = protocol.encode(message)
        with self._lock:
            if self.closed:
                return
            try:
                self._stream.write(data)
                self._stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.closed = True


def _video_info(GstVideo, caps):
    """``(width, height, stride)`` of raw video ``caps``."""
    info = None
    new_from_caps = getattr(GstVideo.VideoInfo, "new_from_caps", None)
    if new_from_caps is not None:
        try:
            info = new_from_caps(caps)
        except (TypeError, ValueError):
            info = None
    if info is None:
        info = GstVideo.VideoInfo()
        if not info.from_caps(caps):
            raise ValueError("frame caps are not raw video")
    return info.width, info.height, info.stride[0]


class StreamWorker:
    """The ingest pipeline of one camera (see the module docstring)."""

    def __init__(self, config: Dict[str, Any], writer: LineWriter, Gst, GstVideo,
                 clock=time.monotonic, wall_clock=time.time):
        self.Gst = Gst
        self.GstVideo = GstVideo
        self._writer = writer
        self._clock = clock
        self._wall = wall_clock
        self.protocol = str(config.get("protocol") or "").lower()
        self.url = config.get("url")
        self.settings = dict(config.get("settings") or {})
        credentials = config.get("credentials") or {}
        self._username = credentials.get("username") or None
        self._password = credentials.get("password") or None
        self._url_secret = credentials.get("urlSecret") or None
        self._secrets = [value for value in (self._username, self._password, self._url_secret) if value]
        self.capabilities = config.get("capabilities") or {}
        self.policy = self.settings.get("decoder") or "auto"
        self.failed_hardware = bool(config.get("failedHardware"))
        self.max_dim = int(self.settings.get("maxFrameDimension") or 1920)
        self.stall_timeout_s = float(self.settings.get("stallTimeoutS") or 10)
        self.publish_fps = int(config.get("publishFps") or DEFAULT_PUBLISH_FPS)

        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._tail_ready = threading.Event()
        self._state = health.CONNECTING
        self._failure: Optional[tuple] = None
        self._codec: Optional[str] = None
        self._selection = None
        self._source_size = None
        self._scale = None
        self._latest = None
        self._seq = 0
        self._last_frame_at_ms: Optional[int] = None
        self._last_frame_clock: Optional[float] = None
        self._source_frames = 0
        self._fps = None
        self._fps_mark = (clock(), 0)
        self._segment: Optional[protocol.FrameSegment] = None
        self._pipeline = None
        self._appsink = None
        self._appsrc = None
        self._capsfilter = None
        self._decoder_element = None
        self._video_pad_taken = False
        self._demux = None
        #: Why rtspsrc rejected the server certificate, once it has.
        self._tls_rejection: Optional[str] = None

    # -- reporting ---------------------------------------------------------

    def _clean(self, message: Any) -> str:
        return clean_message(message, self._secrets)

    def _report_failure(self, category: str, message: str) -> None:
        """Report the first failure and stop; later ones are dropped."""
        with self._cond:
            if self._failure is not None:
                return
            self._failure = (health.normalize_category(category), self._clean(message))
            self._cond.notify_all()
        self._writer.send({"op": protocol.OP_ERROR, "category": self._failure[0],
                           "message": self._failure[1]})
        self._stop.set()

    def _health_message(self) -> Dict[str, Any]:
        with self._cond:
            now = self._clock()
            mark_time, mark_frames = self._fps_mark
            if now - mark_time >= 1.0:
                self._fps = round((self._source_frames - mark_frames) / (now - mark_time), 2)
                self._fps_mark = (now, self._source_frames)
            selection = self._selection
            source = self._source_size or (None, None)
            scale = self._scale or (None, None)
            return {
                "op": protocol.OP_HEALTH,
                "state": self._state,
                "codec": self._codec,
                "width": source[0],
                "height": source[1],
                "frameWidth": scale[0],
                "frameHeight": scale[1],
                "sourceFps": self._fps or None,
                "sourceFrames": self._source_frames,
                "decoder": selection.kind if selection else None,
                "decoderElement": selection.element if selection else None,
                "decoderFallback": bool(selection and selection.fallback),
                "seq": self._seq,
                "lastFrameAtMs": self._last_frame_at_ms,
            }

    def _send_health(self) -> None:
        self._writer.send(self._health_message())

    # -- pipeline construction --------------------------------------------

    def _make_tail_description(self, codec: str) -> str:
        selection = select_decoder(self.policy, codec, self.capabilities, self.failed_hardware)
        with self._cond:
            self._codec, self._selection = codec, selection
        return tail_description(codec, selection, None, self.publish_fps)

    def _instantiate_failure(self, error) -> StreamError:
        """A decode tail that could not be built: the selected decoder (or
        a converter) failed to instantiate."""
        hardware = bool(self._selection and self._selection.kind == HARDWARE)
        category = health.HARDWARE_DECODER_FAILED if hardware else health.DECODER_UNAVAILABLE
        return StreamError(category, f"the decode pipeline could not be built: {error}")

    def _attach_tail(self, container) -> None:
        """Find the tail's elements in ``container`` and install the probes."""
        Gst = self.Gst
        self._appsink = container.get_by_name(APPSINK_NAME)
        self._capsfilter = container.get_by_name(SCALE_CAPSFILTER_NAME)
        self._decoder_element = container.get_by_name(DECODER_NAME)
        rate = container.get_by_name(RATE_NAME)
        parse = container.get_by_name(PARSER_NAME)
        rate_sink = rate.get_static_pad("sink")
        rate_sink.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_decoded_event)
        rate_sink.add_probe(Gst.PadProbeType.BUFFER, make_rate_probe(Gst, RateLimiter(self.publish_fps), self._clock))
        parse.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._on_source_frame)
        self._tail_ready.set()

    def build_rtsp(self) -> None:
        Gst = self.Gst
        source = Gst.ElementFactory.make("rtspsrc", "src")
        if source is None:
            raise StreamError(health.DECODER_UNAVAILABLE, "RTSP ingest (rtspsrc) is not available on this device")
        self._pipeline = Gst.Pipeline.new("stream")
        # The URL secret suffix, when present, is part of the location; the
        # username and password are element properties (Requirement 6.4).
        source.set_property("location", compose_connect_url(self.url, self._url_secret, None, None, "RTSP"))
        for name, value in rtsp_head(self.settings).items():
            if isinstance(value, str):
                Gst.util_set_object_arg(source, name, value)
            else:
                source.set_property(name, value)
        if self._username:
            source.set_property("user-id", self._username)
        if self._password:
            source.set_property("user-pw", self._password)
        source.connect("pad-added", self._on_rtsp_pad)
        try:
            source.connect("accept-certificate", self._on_accept_certificate)
        except TypeError:  # an rtspsrc without the signal (GStreamer < 1.14)
            pass
        self._pipeline.add(source)

    def _on_accept_certificate(self, _source, _connection, _certificate, errors) -> bool:
        """rtspsrc asks only after its own validation against the system
        trust store failed. The answer is always no (TLS has no insecure
        mode); the reason is kept, so the failure that follows, which
        GStreamer reports only as "Failed to connect", is categorized as
        ``tls_verification_failed`` (found on hardware, task 25.3)."""
        with self._cond:
            self._tls_rejection = certificate_problems(errors)
        return False

    def _link_to_fakesink(self, pad) -> None:
        Gst = self.Gst
        sink = Gst.ElementFactory.make("fakesink", None)
        sink.set_property("async", False)
        sink.set_property("sync", False)
        self._pipeline.add(sink)
        sink.sync_state_with_parent()
        pad.link(sink.get_static_pad("sink"))

    def _on_rtsp_pad(self, _source, pad) -> None:
        """Link the first video pad to the decode tail, and every other pad
        (audio, metadata, further video tracks) to a fakesink."""
        Gst = self.Gst
        caps = pad.get_current_caps() or pad.query_caps(None)
        structure = caps.get_structure(0) if caps is not None and caps.get_size() else None
        media = structure.get_string("media") if structure is not None else None
        encoding = structure.get_string("encoding-name") if structure is not None else None
        with self._cond:
            take = media == "video" and not self._video_pad_taken and self._failure is None
            if take:
                self._video_pad_taken = True
        if not take:
            self._link_to_fakesink(pad)
            return
        codec = normalize_codec(encoding)
        try:
            if codec is None:
                raise StreamError(
                    health.UNSUPPORTED_CODEC,
                    f"the stream's video codec {codec_label(encoding)} is not supported; "
                    f"configure the camera for H.264 or H.265")
            description = f"{depayloader(codec)} ! {self._make_tail_description(codec)}"
            try:
                tail = Gst.parse_bin_from_description(description, True)
            except Exception as error:  # noqa: BLE001 - GLib.Error
                raise self._instantiate_failure(error) from None
        except StreamError as error:
            self._report_failure(error.category, error.message)
            self._link_to_fakesink(pad)
            return
        self._pipeline.add(tail)
        self._attach_tail(tail)
        tail.sync_state_with_parent()
        if pad.link(tail.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            self._report_failure(health.NETWORK_ERROR, "the video track could not be linked to the decoder")

    def build_rtmp(self) -> None:
        from stream_ingest.rtmp_demux import RtmpDemux

        Gst = self.Gst
        connect_url = compose_connect_url(self.url, self._url_secret, self._username, self._password, "RTMP")
        self._demux = RtmpDemux(connect_url, secrets=self._secrets)
        del connect_url
        codec = self._demux.open()
        description = (f"appsrc name=es is-live=true format=time do-timestamp=false block=true "
                       f"max-bytes={APPSRC_MAX_BYTES} caps={elementary_caps(codec)} ! "
                       f"{self._make_tail_description(codec)}")
        try:
            self._pipeline = Gst.parse_launch(description)
        except Exception as error:  # noqa: BLE001 - GLib.Error
            raise self._instantiate_failure(error) from None
        self._appsrc = self._pipeline.get_by_name("es")
        self._attach_tail(self._pipeline)

    # -- probes and threads -------------------------------------------------

    def _on_decoded_event(self, _pad, info):
        """On the decoded caps, fit the published size to the maximum
        dimension; again on every resolution change (Requirement 7.6)."""
        Gst = self.Gst
        event = info.get_event()
        if event is not None and event.type == Gst.EventType.CAPS:
            structure = event.parse_caps().get_structure(0)
            has_width, width = structure.get_int("width")
            has_height, height = structure.get_int("height")
            if has_width and has_height and width >= 2 and height >= 2:
                scale = fit_within(width, height, self.max_dim)
                with self._cond:
                    changed = scale != self._scale
                    self._source_size, self._scale = (width, height), scale
                    selection = self._selection
                if changed and self._capsfilter is not None and selection is not None:
                    self._capsfilter.set_property(
                        "caps", Gst.Caps.from_string(scale_caps(scaled_format(selection), scale)))
        return Gst.PadProbeReturn.OK

    def _on_source_frame(self, _pad, _info):
        self._source_frames += 1
        return self.Gst.PadProbeReturn.OK

    def _pull_loop(self) -> None:
        Gst = self.Gst
        while not self._stop.is_set() and not self._tail_ready.wait(0.2):
            pass
        appsink = self._appsink
        while not self._stop.is_set() and appsink is not None:
            sample = appsink.emit("try-pull-sample", 200 * Gst.MSECOND)
            if sample is None:
                continue
            now_ms = int(self._wall() * 1000)
            became_streaming = False
            with self._cond:
                self._seq += 1
                # Holding the newest sample only: the appsink keeps at most
                # one more, so a slow consumer never queues frames here.
                self._latest = (self._seq, sample, now_ms)
                self._last_frame_at_ms = now_ms
                self._last_frame_clock = self._clock()
                if self._state != health.STREAMING:
                    self._state, became_streaming = health.STREAMING, True
                self._cond.notify_all()
            if became_streaming:
                self._send_health()

    def _push_elementary(self, data: bytes, pts_ns, dts_ns) -> bool:
        Gst = self.Gst
        buffer = Gst.Buffer.new_wrapped(data)
        if pts_ns is not None:
            buffer.pts = pts_ns
        if dts_ns is not None:
            buffer.dts = dts_ns
        return self._appsrc.emit("push-buffer", buffer) == Gst.FlowReturn.OK

    def _demux_loop(self) -> None:
        try:
            self._demux.run(self._push_elementary, self._stop.is_set, self._report_failure)
        finally:
            if self._appsrc is not None and not self._stop.is_set():
                self._appsrc.emit("end-of-stream")

    # -- frames --------------------------------------------------------------

    def _publish(self, sample) -> Dict[str, Any]:
        """Copy ``sample`` into the free shared-memory slot; its header."""
        Gst = self.Gst
        width, height, stride = _video_info(self.GstVideo, sample.get_caps())
        buffer = sample.get_buffer()
        mapped, info = buffer.map(Gst.MapFlags.READ)
        if not mapped:
            raise ValueError("the frame buffer could not be mapped")
        try:
            if info.size == width * 3 * height:
                stride = width * 3
            size = stride * height
            if info.size < size:
                raise ValueError("the frame buffer is smaller than its caps")
            if self._segment is None or self._segment.slot_bytes != size:
                if self._segment is not None:
                    self._segment.close()
                self._segment = protocol.FrameSegment(size)
            slot = self._segment.write(memoryview(info.data)[:size])
        finally:
            buffer.unmap(info)
        return {"width": width, "height": height, "stride": stride, "channels": 3,
                "format": "RGB", "shm": self._segment.path, "slot": slot}

    def _serve_frame(self, request: Dict[str, Any]) -> None:
        """Reply with the newest frame after ``after``, waiting up to
        ``waitMs`` for one; ``seq: null`` when none arrives."""
        request_id = request.get("id")
        try:
            after = int(request.get("after") or 0)
            wait_ms = min(MAX_FRAME_WAIT_MS, max(0, int(request.get("waitMs") or 0)))
        except (TypeError, ValueError):
            after, wait_ms = 0, 0
        deadline = self._clock() + wait_ms / 1000.0
        with self._cond:
            while ((self._latest is None or self._latest[0] <= after)
                   and not self._stop.is_set() and self._failure is None):
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 0.25))
            latest = self._latest if self._latest is not None and self._latest[0] > after else None
        if latest is None:
            self._writer.send({"op": protocol.OP_FRAME, "id": request_id, "seq": None})
            return
        seq, sample, acquired_at_ms = latest
        try:
            header = self._publish(sample)
        except (ValueError, OSError) as error:
            logger.warning("frame %s could not be published: %s", seq, self._clean(error))
            self._writer.send({"op": protocol.OP_FRAME, "id": request_id, "seq": None})
            return
        header.update({"op": protocol.OP_FRAME, "id": request_id, "seq": seq,
                       "acquiredAtMs": acquired_at_ms})
        self._writer.send(header)

    def _control_loop(self, stdin) -> None:
        """Serve the parent's requests until ``stop`` or end of input (the
        parent is gone)."""
        try:
            while not self._stop.is_set():
                line = stdin.readline(protocol.MAX_LINE_BYTES)
                if not line:
                    break
                message = protocol.decode(line)
                if message is None:
                    continue
                if message["op"] == protocol.OP_STOP:
                    break
                if message["op"] == protocol.OP_FRAME:
                    self._serve_frame(message)
        finally:
            self._stop.set()

    # -- main loop ---------------------------------------------------------

    def _on_bus_message(self, message) -> None:
        Gst = self.Gst
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            if is_rtsp_auth_setup_error(error.domain, error.code, debug):
                following = self._pipeline.get_bus().timed_pop_filtered(
                    FOLLOWING_ERROR_WAIT_MS * Gst.MSECOND, Gst.MessageType.ERROR)
                if following is not None:
                    message = following
                    error, debug = message.parse_error()
            with self._cond:
                tls_rejection = self._tls_rejection
            if tls_rejection is not None:
                self._report_failure(health.TLS_VERIFICATION_FAILED,
                                     f"the server certificate failed verification ({tls_rejection})")
                return
            source = message.src
            from_decoder = source is not None and source == self._decoder_element
            hardware = bool(self._selection and self._selection.kind == HARDWARE)
            category = classify.classify_gst_error(error.domain, error.code, error.message, debug,
                                                   from_decoder=from_decoder, hardware_decoder=hardware)
            detail = (debug or "").strip().splitlines()[-1][-160:] if debug else ""
            self._report_failure(category, f"{error.message} ({detail})" if detail else error.message)
        elif message.type == Gst.MessageType.EOS:
            self._report_failure(health.NETWORK_ERROR, "the stream ended")
        elif message.type == Gst.MessageType.WARNING:
            warning, _debug = message.parse_warning()
            logger.warning("stream warning: %s", self._clean(warning.message))

    def _bus_loop(self) -> None:
        Gst = self.Gst
        bus = self._pipeline.get_bus()
        wanted = Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING
        while not self._stop.is_set():
            message = bus.timed_pop_filtered(100 * Gst.MSECOND, wanted)
            if message is not None:
                self._on_bus_message(message)

    def _heartbeat_loop(self) -> None:
        """Health every 2 s from start to stop, including while an RTMP
        connection opens, so the parent can tell a slow camera from a hung
        worker. A streaming pipeline that delivers no frame for the stall
        timeout is a ``stall`` (Requirement 8.4): the worker knows when its
        last frame arrived to the millisecond, the parent only every 2 s."""
        interval = min(HEALTH_INTERVAL_S, max(0.25, self.stall_timeout_s / 4))
        next_health = self._clock()
        while not self._stop.wait(interval):
            with self._cond:
                stalled = (self._state == health.STREAMING and self._last_frame_clock is not None
                           and self._clock() - self._last_frame_clock > self.stall_timeout_s)
            if stalled:
                self._report_failure(health.STALL, f"no frame arrived for {self.stall_timeout_s:g} s")
                return
            if self._clock() >= next_health:
                self._send_health()
                next_health = self._clock() + HEALTH_INTERVAL_S

    def run(self, stdin) -> int:
        Gst = self.Gst
        control = threading.Thread(target=self._control_loop, args=(stdin,), name="control", daemon=True)
        control.start()
        try:
            self._send_health()
            threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True).start()
            if self.protocol == "rtsp":
                self.build_rtsp()
            elif self.protocol == "rtmp":
                self.build_rtmp()
            else:
                raise StreamError(health.NETWORK_ERROR, "the worker received an unknown stream protocol")
            threading.Thread(target=self._pull_loop, name="frames", daemon=True).start()
            if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                error = self._pipeline.get_bus().timed_pop_filtered(Gst.SECOND, Gst.MessageType.ERROR)
                if error is not None:
                    self._on_bus_message(error)
                self._report_failure(health.NETWORK_ERROR, "the stream pipeline could not start")
            if self._demux is not None and not self._stop.is_set():
                threading.Thread(target=self._demux_loop, name="demux", daemon=True).start()
            self._bus_loop()
        except StreamError as error:
            self._report_failure(error.category, error.message)
        except Exception as error:  # noqa: BLE001 - reported, never raised
            logger.error("stream worker failed: %s", self._clean(f"{type(error).__name__}: {error}"))
            self._report_failure(health.NETWORK_ERROR, f"the stream worker failed ({type(error).__name__})")
        finally:
            self._stop.set()
            with self._cond:
                self._cond.notify_all()
            self._shutdown()
        return EXIT_FAILED if self._failure is not None else EXIT_OK

    def _shutdown(self) -> None:
        Gst = self.Gst
        if self._pipeline is not None:
            try:
                self._pipeline.set_state(Gst.State.NULL)
            except Exception:  # noqa: BLE001 - exiting
                pass
        with self._cond:
            self._latest = None
        if self._segment is not None:
            self._segment.close()
        if self._demux is not None:
            self._demux.close()


def _protocol_stream():
    """A private copy of stdout for protocol lines; descriptor 1 then points
    at stderr so stray C-level prints cannot corrupt the protocol."""
    sys.stdout.flush()
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(protocol_fd, "wb", buffering=0)


def main() -> int:
    prepare_environment()
    writer = LineWriter(_protocol_stream())
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    stdin = sys.stdin.buffer
    config = protocol.decode(stdin.readline(protocol.MAX_LINE_BYTES))
    if config is None or config.get("op") != protocol.OP_CONFIG:
        writer.send({"op": protocol.OP_ERROR, "category": health.NETWORK_ERROR,
                     "message": "the stream worker received no configuration"})
        return EXIT_BAD_CONFIG
    try:
        Gst, GstVideo = load_gstreamer()
    except Exception as error:  # noqa: BLE001 - reported to the parent
        writer.send({"op": protocol.OP_ERROR, "category": health.DECODER_UNAVAILABLE,
                     "message": f"GStreamer is not available ({type(error).__name__})"})
        return EXIT_FAILED
    worker = StreamWorker(config, writer, Gst, GstVideo)
    del config
    return worker.run(stdin)


if __name__ == "__main__":
    code = main()
    sys.stderr.flush()
    # Daemon threads may sit in GStreamer or FFmpeg calls; do not join them.
    os._exit(code)
