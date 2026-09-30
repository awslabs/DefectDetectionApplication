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
"""Pure pipeline builders of the Stream_Worker (rtsp-rtmp-stream-cameras
design component 11, "Inside a Stream_Worker"; Requirements 7.1, 7.4-7.7).

Nothing here imports GStreamer or touches a credential, so every rule is
testable on any host:

- :func:`fit_within` computes the scaled frame size (Requirement 7.6);
- :func:`select_decoder` applies the Decoder_Policy to the probed
  Device_Stream_Capabilities (Requirements 7.4, 7.5, 7.7);
- :func:`rtsp_head` gives the non-secret ``rtspsrc`` properties
  (Requirement 7.1); the worker sets the location and credentials itself;
- :func:`decoder_chain` and :func:`tail_description` build the decode tail
  shared by both ingest heads.

The tail is built so the publish cap runs right after the decoder: decoding
keeps the source rate, because inter frames depend on their predecessors,
but scaling, color conversion and frame copies run at the publish rate
only. Software and x86 NVIDIA chains scale before converting, which is
cheaper than converting the full-size frame.

The publish cap is a pad probe (:class:`RateLimiter`, which times frames
by arrival) on a passthrough ``identity`` named ``rate``. It was
``videorate drop-only=true max-rate=N`` until hardware verification
(task 25.3): ``videorate`` in
drop-only mode asserts that every frame carries a duration, and aborts the
worker on GStreamer 1.20 (JP6) and 1.24 (JP7) when it does not. Real IP
cameras send H.264 without VUI timing (an Amcrest PTZ did), so their
decoded frames have no duration.
"""
from dataclasses import dataclass
import os
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from stream_ingest.health import DECODER_UNAVAILABLE, UNSUPPORTED_CODEC, StreamError

H264 = "h264"
H265 = "h265"
SUPPORTED_CODECS = (H264, H265)

_CODEC_ALIASES = {
    "h264": H264, "avc": H264, "avc1": H264, "x-h264": H264,
    "h265": H265, "hevc": H265, "hvc1": H265, "hev1": H265, "x-h265": H265,
}

HARDWARE = "hardware"
SOFTWARE = "software"
DECODER_POLICIES = ("auto", HARDWARE, SOFTWARE)

#: The Jetson hardware decoder; its output is NVMM memory that
#: ``nvvidconv`` (the VIC) scales and copies to system memory.
JETSON_DECODER = "nvv4l2decoder"

#: The GStreamer libav (FFmpeg) software decoders are ``avdec_*``.
SOFTWARE_DECODER_PREFIX = "avdec_"

#: Decoding threads a software decoder may use at most. This was found on
#: hardware (task 25.3, fix 15). With ``max-threads`` left at 0
#: (automatic), gst-libav starts one frame thread per CPU. On the 80-CPU
#: amd64 test host, FFmpeg 4.2's HEVC decoder (GStreamer 1.16, the Ubuntu
#: 20.04 base) then crashed the worker with SIGSEGV, SIGABRT or SIGFPE on
#: every H.265 stream. 16 threads held and 24 crashed, and FFmpeg itself
#: warns above 16. One thread already decoded 1080p15 H.265 in real time
#: there, and fewer frame threads also mean less decode latency.
SOFTWARE_DECODER_MAX_THREADS = 8


def software_decoder_threads(cpu_count: Optional[int] = None) -> int:
    """The ``max-threads`` a software decoder gets: one per CPU, at most
    :data:`SOFTWARE_DECODER_MAX_THREADS`, and never 0 (automatic).
    ``cpu_count`` defaults to :func:`os.cpu_count`."""
    count = os.cpu_count() if cpu_count is None else cpu_count
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        count = 1
    return min(count, SOFTWARE_DECODER_MAX_THREADS)

#: Frames published per second at most (the node maximum).
DEFAULT_PUBLISH_FPS = 10

#: Element names the worker looks up: the appsink it pulls from, the
#: capsfilter whose caps carry the scaled size (updated when the source
#: resolution changes), the rate limiter whose sink pad sees the decoded
#: size and carries the publish-cap probe, the parser whose output counts
#: source frames, and the decoder.
APPSINK_NAME = "frames"
SCALE_CAPSFILTER_NAME = "scale"
RATE_NAME = "rate"
PARSER_NAME = "parse"
DECODER_NAME = "decoder"

_RTSP_PROTOCOLS = {"tcp": "tcp", "udp": "udp", "auto": "udp-mcast+udp+tcp"}
_LABEL_RE = re.compile(r"[^A-Za-z0-9._+-]")


def normalize_codec(name: Any) -> Optional[str]:
    """``h264`` or ``h265`` for a codec name from RTSP caps
    (``H264``, ``H265``), FFmpeg (``h264``, ``hevc``) or MP4 tags, else None."""
    if not isinstance(name, str):
        return None
    return _CODEC_ALIASES.get(name.strip().lower().replace("video/", ""))


def codec_label(name: Any) -> str:
    """A codec name as it may appear in a message: printable, short, and
    never more than the stream sent."""
    label = _LABEL_RE.sub("", str(name if name is not None else ""))[:32]
    return label or "unknown"


# -- scaling -----------------------------------------------------------------

def _even_floor(value: int) -> int:
    return max(2, value - value % 2)


def fit_within(width: int, height: int, max_dim: int) -> Tuple[int, int]:
    """The frame size published for a ``width`` x ``height`` source.

    The longer edge is at most ``max_dim``, the aspect ratio is kept to the
    nearest even pixel, nothing is ever upscaled, and both dimensions are
    even (chroma-subsampled formats and the VIC need even sizes). Sources
    and bounds under 2 pixels are rejected.
    """
    for name, value in (("width", width), ("height", height), ("max_dim", max_dim)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 2:
            raise ValueError(f"{name} must be an integer of at least 2")
    landscape = width >= height
    longer, shorter = (width, height) if landscape else (height, width)
    new_longer = _even_floor(min(longer, max_dim))
    ideal_shorter = shorter * new_longer / longer
    new_shorter = min(_even_floor(shorter), max(2, 2 * round(ideal_shorter / 2)))
    return (new_longer, new_shorter) if landscape else (new_shorter, new_longer)


# -- decoder selection -------------------------------------------------------

@dataclass(frozen=True)
class DecoderSelection:
    """The decoder a worker uses: ``kind`` is ``hardware`` or ``software``,
    ``element`` the GStreamer element, and ``fallback`` whether ``auto``
    chose software because the hardware decoder failed."""

    codec: str
    kind: str
    element: str
    fallback: bool = False


def available_decoders(capabilities: Optional[Mapping[str, Any]], codec: str) -> Tuple[Optional[str], Optional[str]]:
    """The ``(hardware, software)`` element names the capabilities hold
    for ``codec``; None where there is none."""
    codecs = (capabilities or {}).get("codecs") if isinstance(capabilities, Mapping) else None
    entry = codecs.get(codec) if isinstance(codecs, Mapping) else None
    if not isinstance(entry, Mapping):
        return None, None

    def element(kind):
        value = entry.get(kind)
        return value if isinstance(value, str) and value else None

    return element(HARDWARE), element(SOFTWARE)


def select_decoder(policy: str, codec: Any, capabilities: Optional[Mapping[str, Any]],
                   failed_hardware: bool = False) -> DecoderSelection:
    """Apply the Decoder_Policy (Requirements 7.4, 7.5, 7.7).

    ============  ==========================================================
    ``auto``      hardware, if the capabilities have it and it has not
                  failed in this session; otherwise software
    ``hardware``  hardware, or ``decoder_unavailable``
    ``software``  software
    ============  ==========================================================

    A decoder absent from the capabilities is never selected: with none
    left the result is ``decoder_unavailable``. A codec other than H.264
    and H.265 is ``unsupported_codec``, naming the codec.
    """
    if policy not in DECODER_POLICIES:
        raise ValueError(f"unknown Decoder_Policy {policy!r}")
    normalized = normalize_codec(codec)
    if normalized is None:
        raise StreamError(
            UNSUPPORTED_CODEC,
            f"the stream's video codec {codec_label(codec)} is not supported; "
            f"configure the camera for H.264 or H.265")
    hardware, software = available_decoders(capabilities, normalized)
    label = "H.264" if normalized == H264 else "H.265"
    if policy == SOFTWARE:
        if software:
            return DecoderSelection(normalized, SOFTWARE, software)
        raise StreamError(DECODER_UNAVAILABLE, f"no software {label} decoder is available on this device")
    usable_hardware = hardware if hardware and not failed_hardware else None
    if policy == HARDWARE:
        if usable_hardware:
            return DecoderSelection(normalized, HARDWARE, usable_hardware)
        if hardware:
            raise StreamError(DECODER_UNAVAILABLE, f"the hardware {label} decoder failed on this stream")
        raise StreamError(DECODER_UNAVAILABLE, f"no hardware {label} decoder is available on this device")
    if usable_hardware:
        return DecoderSelection(normalized, HARDWARE, usable_hardware)
    if software:
        return DecoderSelection(normalized, SOFTWARE, software, fallback=bool(hardware and failed_hardware))
    raise StreamError(DECODER_UNAVAILABLE, f"no {label} decoder is available on this device")


# -- pipeline descriptions ---------------------------------------------------

def rtsp_protocols(transport: Any) -> str:
    """The ``rtspsrc`` ``protocols`` flags for a Stream_Settings transport."""
    return _RTSP_PROTOCOLS.get(transport, _RTSP_PROTOCOLS["tcp"])


def rtsp_head(settings: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The non-secret ``rtspsrc`` properties for ``settings`` (Requirement
    7.1). The worker sets ``location``, ``user-id`` and ``user-pw`` itself,
    as element properties, so no credential enters a pipeline description.

    Certificates and host names are always verified for ``rtsps``; the
    keep-alive keeps sessions with cameras that time idle RTSP sessions out.
    """
    settings = settings or {}
    latency = settings.get("latencyMs", 200)
    if isinstance(latency, bool) or not isinstance(latency, int) or latency < 0:
        latency = 200
    return {
        "protocols": rtsp_protocols(settings.get("transport", "tcp")),
        "latency": latency,
        "tls-validation-flags": "validate-all",
        "do-rtsp-keep-alive": True,
    }


def depayloader(codec: str) -> str:
    """The RTP depayloader of ``codec``."""
    return "rtph264depay" if normalize_codec(codec) == H264 else "rtph265depay"


def parser(codec: str) -> str:
    """The bitstream parser of ``codec``."""
    return "h264parse" if normalize_codec(codec) == H264 else "h265parse"


def elementary_caps(codec: str) -> str:
    """The ``appsrc`` caps of an RTMP worker's Annex-B elementary stream."""
    media = "video/x-h264" if normalize_codec(codec) == H264 else "video/x-h265"
    return f"{media},stream-format=byte-stream,alignment=au"


def scale_caps(raw_format: str, scale: Optional[Tuple[int, int]]) -> str:
    """Caps for the scaling capsfilter: the format, plus the size once the
    source resolution is known."""
    caps = f"video/x-raw,format={raw_format}"
    if scale:
        width, height = scale
        caps += f",width={int(width)},height={int(height)}"
    return caps


def decoder_chain(codec: str, decoder: DecoderSelection, scale: Optional[Tuple[int, int]] = None,
                  publish_fps: int = DEFAULT_PUBLISH_FPS) -> str:
    """The decode chain from the parsed stream to packed RGB.

    ``scale`` is the :func:`fit_within` size, or None before the source
    resolution is known (the capsfilter then only fixes the format and is
    updated later). The ``identity`` named ``rate`` right after the
    decoder carries the worker's :class:`RateLimiter` probe, which drops
    frames above ``publish_fps`` (see the module docstring for why this is
    not ``videorate``). ``publish_fps`` is the probe's; it does not appear
    in the description. A software decoder gets a bounded ``max-threads``
    (:func:`software_decoder_threads`).
    """
    rate = f"identity name={RATE_NAME} silent=true"
    decode = f"{decoder.element} name={DECODER_NAME}"
    if decoder.element.startswith(SOFTWARE_DECODER_PREFIX):
        # Bounded frame threads (fix 15, see SOFTWARE_DECODER_MAX_THREADS).
        decode += f" max-threads={software_decoder_threads()}"
    if decoder.element == JETSON_DECODER:
        # The VIC scales while copying out of NVMM memory; it outputs RGBA.
        return (f"{decode} ! {rate} ! nvvidconv ! capsfilter name={SCALE_CAPSFILTER_NAME} "
                f"caps={scale_caps('RGBA', scale)} ! videoconvert ! video/x-raw,format=RGB")
    return (f"{decode} ! {rate} ! videoscale ! videoconvert ! "
            f"capsfilter name={SCALE_CAPSFILTER_NAME} caps={scale_caps('RGB', scale)}")


class RateLimiter:
    """Keeps at most ``max_fps`` frames per second, timed by their arrival
    (monotonic ns): a token bucket whose tokens accrue at ``max_fps`` per
    second up to :attr:`BURST`, and each kept frame spends one.

    A source faster than the cap is thinned evenly (15 fps capped at 10
    keeps two frames in three). One at or below the cap keeps every frame
    as long as its arrivals jitter by less than half an interval, because an
    early frame spends what a late one left. After a stall, at most
    :attr:`BURST` frames pass back to back. A time earlier than the last
    one adds nothing.

    Frames are timed by arrival, never by PTS (found on hardware, task
    25.3). A real camera's decoded frames can lack a PTS: a third of an
    Amcrest PTZ's 30 fps sub stream did. Timing those by the clock and the
    rest by PTS mixed two timebases, every switch between them looked like
    a restarted stream, and the cap passed 17-21 frames per second instead
    of 10. Durations are never needed, unlike ``videorate``'s.
    """

    BURST = 2.0

    def __init__(self, max_fps: int = DEFAULT_PUBLISH_FPS):
        self.interval_ns = int(1_000_000_000 / max(1, int(max_fps)))
        self._tokens = self.BURST
        self._last_ns: Optional[int] = None

    def keep(self, now_ns: int) -> bool:
        """Whether to keep the frame arriving at ``now_ns``."""
        if self._last_ns is None or now_ns > self._last_ns:
            if self._last_ns is not None:
                accrued = (now_ns - self._last_ns) / self.interval_ns
                self._tokens = min(self.BURST, self._tokens + accrued)
            self._last_ns = now_ns
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


def scaled_format(decoder: DecoderSelection) -> str:
    """The raw format the scaling capsfilter carries for ``decoder``."""
    return "RGBA" if decoder.element == JETSON_DECODER else "RGB"


def tail_description(codec: str, decoder: DecoderSelection, scale: Optional[Tuple[int, int]] = None,
                     publish_fps: int = DEFAULT_PUBLISH_FPS) -> str:
    """Parser, decoder chain and appsink: the tail both heads share.

    The appsink keeps one buffer and drops older ones, so a slow consumer
    never makes frames queue up in the worker (Requirement 8.3).
    """
    return (f"{parser(codec)} name={PARSER_NAME} ! {decoder_chain(codec, decoder, scale, publish_fps)} ! "
            f"appsink name={APPSINK_NAME} max-buffers=1 drop=true sync=false emit-signals=false")
