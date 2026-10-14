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
"""The RTMP ingest head: PyAV pulls the stream inside an RTMP Stream_Worker
(rtsp-rtmp-stream-cameras Requirements 7.2, 7.3; design "Inside a
Stream_Worker").

FFmpeg (bundled with PyAV, 6.1 or later) reads RTMP and RTMPS as a client,
including H.265 carried as Enhanced RTMP. The first video stream is
converted to Annex-B with ``h264_mp4toannexb`` / ``hevc_mp4toannexb`` and
handed, with its timestamps, to the worker's ``appsrc``; audio, data and
every other stream are never demuxed.

TLS always verifies the server certificate and host name against the system
trust store. Imported only by RTMP workers.
"""
import os
from typing import Callable, Iterable, List, Optional

from stream_ingest import classify, health
from stream_ingest.health import StreamError, clean_message
from stream_ingest.pipeline import H264, codec_label, normalize_codec

#: System CA bundles, in the order they are tried.
SYSTEM_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
)

OPEN_TIMEOUT_S = 15.0
READ_TIMEOUT_S = 10.0

#: The Enhanced RTMP codecs this client advertises in its ``connect``
#: command (``fourCcList``). Servers that follow E-RTMP v2, MediaMTX among
#: them, send an enhanced track only to a client that lists its FourCC, so
#: without ``hvc1`` an H.265 path fails as an opaque I/O error. AV1 and VP9
#: are listed too, so such a stream arrives and is reported as an
#: unsupported codec by name. H.264 is legacy RTMP and needs no entry. These
#: three are the FourCCs every FFmpeg with the option accepts.
ENHANCED_CODECS = "hvc1,av01,vp09"

_NANOSECONDS = 1_000_000_000


def system_ca_bundle() -> Optional[str]:
    """The first system CA bundle that exists."""
    for path in SYSTEM_CA_BUNDLES:
        if os.path.isfile(path):
            return path
    return None


def is_secure(url: str) -> bool:
    return isinstance(url, str) and url.lower().startswith("rtmps://")


class RtmpDemux:
    """Opens ``connect_url`` and yields the first video stream as Annex-B.

    ``secrets`` are the worker's credential values: every message this
    class produces is redacted with them, so a connect URL that carries
    user information or a stream key never leaves the worker.
    """

    def __init__(self, connect_url: str, secrets: Iterable[str] = (),
                 open_timeout_s: float = OPEN_TIMEOUT_S, read_timeout_s: float = READ_TIMEOUT_S):
        self._url = connect_url
        self._secrets: List[str] = [value for value in secrets if value]
        self._open_timeout_s = open_timeout_s
        self._read_timeout_s = read_timeout_s
        self._container = None
        self._stream = None
        self._filter = None
        self.codec: Optional[str] = None

    def _options(self) -> dict:
        options = {"rw_timeout": str(int(self._read_timeout_s * 1_000_000)),
                   "rtmp_enhanced_codecs": ENHANCED_CODECS}
        if is_secure(self._url):
            bundle = system_ca_bundle()
            if bundle is None:
                raise StreamError(health.TLS_VERIFICATION_FAILED,
                                  "no system CA bundle is available to verify the server certificate")
            options.update({"tls_verify": "1", "ca_file": bundle})
        return options

    def _failure(self, error: BaseException, log_lines) -> StreamError:
        text = str(error)
        category = classify.classify_av_error(getattr(error, "errno", None), text, log_lines)
        detail = "; ".join(line for line in log_lines if line)[-200:]
        message = clean_message(f"{type(error).__name__}: {text}" + (f" ({detail})" if detail else ""),
                                self._secrets)
        return StreamError(category, message)

    def open(self) -> str:
        """Connect and select the first video stream; returns its codec
        (``h264`` or ``h265``). Raises :class:`StreamError`."""
        import av
        from av.bitstream import BitStreamFilterContext

        log_lines: List[str] = []
        try:
            with _captured_ffmpeg_log(log_lines):
                self._container = av.open(self._url, mode="r", options=self._options(),
                                          timeout=(self._open_timeout_s, self._read_timeout_s))
        except StreamError:
            raise
        except Exception as error:  # noqa: BLE001 - every failure is categorized
            raise self._failure(error, log_lines) from None
        video = next(iter(self._container.streams.video), None)
        if video is None:
            raise StreamError(health.UNSUPPORTED_CODEC, "the stream has no video track")
        name = video.codec_context.name
        codec = normalize_codec(name)
        if codec is None:
            raise StreamError(
                health.UNSUPPORTED_CODEC,
                f"the stream's video codec {codec_label(name)} is not supported; "
                f"configure the camera for H.264 or H.265")
        self._stream = video
        self._filter = BitStreamFilterContext(
            "h264_mp4toannexb" if codec == H264 else "hevc_mp4toannexb", video)
        self.codec = codec
        return codec

    def run(self, push: Callable[[bytes, Optional[int], Optional[int]], bool],
            should_stop: Callable[[], bool], on_error: Callable[[str, str], None]) -> None:
        """Demux until ``should_stop`` or a failure. ``push(data, pts_ns,
        dts_ns)`` returns False once the pipeline no longer accepts data.
        A failure, or the end of the stream, is reported once through
        ``on_error(category, message)``."""
        time_base = self._stream.time_base
        origin = None

        def nanoseconds(value):
            nonlocal origin
            if value is None or time_base is None:
                return None
            ns = int(value * time_base * _NANOSECONDS)
            if origin is None:
                origin = ns
            return max(0, ns - origin)

        # No log capture here: a capture collects every line for as long as
        # it is active, which for a stream is unbounded. Mid-stream failures
        # are network failures, which the exception describes.
        try:
            for packet in self._container.demux(self._stream):
                if should_stop():
                    return
                if packet.size == 0:
                    continue
                for annexb in self._filter.filter(packet):
                    pts = annexb.pts if annexb.pts is not None else annexb.dts
                    if not push(bytes(annexb), nanoseconds(pts), nanoseconds(annexb.dts)):
                        return
        except Exception as error:  # noqa: BLE001 - reported, the worker then exits
            if not should_stop():
                failure = self._failure(error, [])
                on_error(failure.category, failure.message)
            return
        if not should_stop():
            on_error(health.NETWORK_ERROR, "the RTMP stream ended")

    def close(self) -> None:
        container, self._container = self._container, None
        if container is not None:
            try:
                container.close()
            except Exception:  # noqa: BLE001 - closing at exit
                pass


class _captured_ffmpeg_log:
    """Collect FFmpeg's log lines (RTMP servers report refusals only there)
    into ``lines`` while the block runs. Best effort: without
    ``av.logging.Capture`` nothing is collected."""

    def __init__(self, lines: List[str]):
        self._lines = lines
        self._capture = None
        self._records = None

    def __enter__(self):
        try:
            import av.logging as av_logging
            av_logging.set_level(av_logging.WARNING)
            # Only this thread's lines: the open runs on it.
            self._capture = av_logging.Capture(local=True)
            self._records = self._capture.__enter__()
        except Exception:  # noqa: BLE001 - diagnostics only
            self._capture = None
        return self

    def __exit__(self, *exc):
        if self._capture is not None:
            try:
                self._capture.__exit__(*exc)
            except Exception:  # noqa: BLE001
                pass
            for record in self._records or ():
                message = record[2] if isinstance(record, tuple) and len(record) > 2 else str(record)
                self._lines.append(str(message).strip())
        return False
