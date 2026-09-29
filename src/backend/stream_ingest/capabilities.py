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
"""The Device_Stream_Capabilities probe (rtsp-rtmp-stream-cameras
Requirements 7.4, 17.4; design component 11 and "Device_Stream_Capabilities").

    python -m stream_ingest.capabilities

The probe runs in its own process, like a Stream_Worker, so a decoder that
crashes while being probed cannot take the backend down. It checks:

- the GStreamer element factories of both ingest heads and every decode
  chain (``rtspsrc``, the depayloaders and parsers, ``avdec_h264/h265``,
  ``nvv4l2decoder`` with ``nvvidconv``, ``nvh264dec/nvh265dec``);
- that PyAV imports with FFmpeg 6.1 or later, and has the ``flv`` demuxer
  and the ``rtmp`` protocol; and TLS for ``rtsps`` (GIO) and ``rtmps``
  (FFmpeg's ``tls`` protocol);
- that each candidate decoder really decodes: a small H.264 and a small
  H.265 sample, generated with PyAV's encoders (or the ``ffmpeg`` CLI),
  go through every present decoder. A hardware decoder that is present but
  cannot decode (a container without the device nodes, say) is not
  reported.

The parent logs the result once and caches it; the Stream_Worker selects
decoders from it, and the Edge_Sync_Agent reports it (Requirement 16.5).
"""
import ctypes
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: FFmpeg 6.1 is libavformat 60.16: the first with Enhanced RTMP (H.265).
MIN_LIBAVFORMAT = (60, 16)

HEAD_ELEMENTS = {
    "rtsp": ("rtspsrc", "rtph264depay", "rtph265depay"),
    "rtmp": ("appsrc",),
}
TAIL_ELEMENTS = ("h264parse", "h265parse", "identity", "videoscale", "videoconvert",
                 "capsfilter", "appsink")
SOFTWARE_DECODERS = {"h264": "avdec_h264", "h265": "avdec_h265"}
#: Hardware decoders in order of preference, with the elements each needs.
HARDWARE_DECODERS = {
    "h264": (("nvv4l2decoder", ("nvvidconv",)), ("nvh264dec", ())),
    "h265": (("nvv4l2decoder", ("nvvidconv",)), ("nvh265dec", ())),
}

SAMPLE_SIZE = (320, 240)
SAMPLE_FRAMES = 8
DECODE_TIMEOUT_S = 10.0
PROBE_TIMEOUT_S = 90.0


# -- probe (runs in the probe process) ---------------------------------------

def _gstreamer():
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    return Gst


def _has_elements(Gst, names) -> bool:
    return all(Gst.ElementFactory.find(name) is not None for name in names)


def _gio_tls() -> bool:
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
        backend = Gio.TlsBackend.get_default()
        return bool(backend and backend.supports_tls())
    except Exception:  # noqa: BLE001 - reported as absent
        return False


def _loaded_library(fragment: str) -> Optional[str]:
    """The path of a shared library this process mapped, by name fragment."""
    try:
        with open("/proc/self/maps", encoding="utf-8") as maps:
            for line in maps:
                path = line.split()[-1] if line.strip() else ""
                if fragment in os.path.basename(path) and os.path.isfile(path):
                    return path
    except OSError:
        pass
    return None


def _ffmpeg_protocols() -> List[str]:
    """The input protocols of the FFmpeg that PyAV loaded."""
    path = _loaded_library("libavformat")
    if path is None:
        return []
    library = ctypes.CDLL(path)
    enumerate_protocols = library.avio_enum_protocols
    enumerate_protocols.restype = ctypes.c_char_p
    enumerate_protocols.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
    opaque = ctypes.c_void_p(None)
    names = []
    while True:
        name = enumerate_protocols(ctypes.byref(opaque), 0)
        if not name:
            break
        names.append(name.decode("ascii", "replace"))
    return names


def _pyav() -> Dict[str, Any]:
    """PyAV and FFmpeg facts for the RTMP head."""
    facts = {"pyav": None, "ffmpeg": None, "rtmp": False, "rtmps": False}
    try:
        import av
    except Exception:  # noqa: BLE001 - PyAV absent: no RTMP
        return facts
    facts["pyav"] = getattr(av, "__version__", None)
    facts["ffmpeg"] = getattr(av, "ffmpeg_version_info", None)
    versions = getattr(av, "library_versions", {}) or {}
    libavformat = tuple(versions.get("libavformat") or ())[:2]
    recent = bool(libavformat) and libavformat >= MIN_LIBAVFORMAT
    has_flv = "flv" in (getattr(av, "formats_available", None) or ())
    try:
        protocols = set(_ffmpeg_protocols())
    except Exception:  # noqa: BLE001 - treated as unknown
        protocols = set()
    facts["rtmp"] = bool(recent and has_flv and "rtmp" in protocols)
    facts["rtmps"] = bool(facts["rtmp"] and "rtmps" in protocols and "tls" in protocols)
    return facts


def _sample_with_pyav(codec: str) -> Optional[bytes]:
    try:
        import av
        import numpy
    except Exception:  # noqa: BLE001
        return None
    encoder = "libx264" if codec == "h264" else "libx265"
    options = {"x265-params": "log-level=error"} if codec == "h265" else {}
    output = io.BytesIO()
    try:
        with av.open(output, mode="w", format=codec if codec == "h264" else "hevc") as container:
            stream = container.add_stream(encoder, rate=10, options=options)
            stream.width, stream.height = SAMPLE_SIZE
            stream.pix_fmt = "yuv420p"
            for index in range(SAMPLE_FRAMES):
                image = numpy.zeros((SAMPLE_SIZE[1], SAMPLE_SIZE[0], 3), dtype=numpy.uint8)
                image[:, :, index % 3] = 40 + index * 20
                frame = av.VideoFrame.from_ndarray(image, format="rgb24")
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)
    except Exception:  # noqa: BLE001 - encoder missing
        return None
    data = output.getvalue()
    return data or None


def _sample_with_cli(codec: str) -> Optional[bytes]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    encoder, muxer = ("libx264", "h264") if codec == "h264" else ("libx265", "hevc")
    width, height = SAMPLE_SIZE
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             f"testsrc=size={width}x{height}:rate=10", "-frames:v", str(SAMPLE_FRAMES),
             "-c:v", encoder, "-pix_fmt", "yuv420p", "-f", muxer, "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 and result.stdout else None


def sample(codec: str) -> Optional[bytes]:
    """A short Annex-B elementary stream of ``codec``."""
    return _sample_with_pyav(codec) or _sample_with_cli(codec)


def decodes(Gst, codec: str, decoder: str, extra: tuple, data: bytes) -> bool:
    """Whether ``decoder`` turns the ``codec`` sample into frames."""
    parse = "h264parse" if codec == "h264" else "h265parse"
    handle, path = tempfile.mkstemp(prefix="dda-stream-probe-", suffix=f".{codec}")
    try:
        with os.fdopen(handle, "wb") as sample_file:
            sample_file.write(data)
        chain = " ! ".join((decoder,) + tuple(extra))
        pipeline = Gst.parse_launch(
            f"filesrc location={path} ! {parse} ! {chain} ! appsink name=out sync=false")
    except Exception:  # noqa: BLE001 - an element failed to instantiate
        os.unlink(path)
        return False
    appsink = pipeline.get_by_name("out")
    frames = 0
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            return False
        deadline = time.monotonic() + DECODE_TIMEOUT_S
        bus = pipeline.get_bus()
        while time.monotonic() < deadline:
            sample_out = appsink.emit("try-pull-sample", 100 * Gst.MSECOND)
            if sample_out is not None:
                frames += 1
                continue
            message = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message is not None or appsink.get_property("eos"):
                break
        return frames > 0
    finally:
        pipeline.set_state(Gst.State.NULL)
        try:
            os.unlink(path)
        except OSError:
            pass


def probe() -> Dict[str, Any]:
    """The Device_Stream_Capabilities of this device."""
    Gst = _gstreamer()
    tail = _has_elements(Gst, TAIL_ELEMENTS)
    av_facts = _pyav()
    capabilities: Dict[str, Any] = {
        "rtsp": bool(tail and _has_elements(Gst, HEAD_ELEMENTS["rtsp"])),
        "rtmp": bool(tail and _has_elements(Gst, HEAD_ELEMENTS["rtmp"]) and av_facts["rtmp"]),
        "tls": False,
        "rtspTls": False,
        "rtmpTls": bool(av_facts["rtmps"]),
        "codecs": {},
        "gstreamer": Gst.version_string().replace("GStreamer ", ""),
        "pyav": av_facts["pyav"],
        "ffmpeg": av_facts["ffmpeg"],
        "probedAtMs": int(time.time() * 1000),
    }
    capabilities["rtspTls"] = bool(capabilities["rtsp"] and _gio_tls())
    capabilities["tls"] = bool(capabilities["rtspTls"] or capabilities["rtmpTls"])
    for codec in ("h264", "h265"):
        entry = {"hardware": None, "software": None}
        data = sample(codec) if tail else None
        software = SOFTWARE_DECODERS[codec]
        if data and _has_elements(Gst, (software,)) and decodes(Gst, codec, software, (), data):
            entry["software"] = software
        for decoder, extra in HARDWARE_DECODERS[codec]:
            if data and _has_elements(Gst, (decoder,) + extra) and decodes(Gst, codec, decoder, extra, data):
                entry["hardware"] = decoder
                break
        capabilities["codecs"][codec] = entry
    return capabilities


def main() -> int:
    os.environ["GST_DEBUG"] = "1"
    os.environ.pop("GST_DEBUG_FILE", None)
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    result = probe()
    with os.fdopen(protocol_fd, "w", encoding="utf-8") as out:
        out.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


# -- parent side ---------------------------------------------------------------

def unavailable(reason: str) -> Dict[str, Any]:
    """Capabilities of a device whose probe failed: nothing supported."""
    return {"rtsp": False, "rtmp": False, "tls": False, "rtspTls": False, "rtmpTls": False,
            "codecs": {"h264": {"hardware": None, "software": None},
                       "h265": {"hardware": None, "software": None}},
            "gstreamer": None, "pyav": None, "ffmpeg": None,
            "probedAtMs": int(time.time() * 1000), "probeError": reason}


def run_probe(timeout_s: float = PROBE_TIMEOUT_S) -> Dict[str, Any]:
    """Run the probe in its own process and return its result."""
    from stream_ingest.launch import backend_root, worker_environment

    try:
        result = subprocess.run(
            [sys.executable, "-m", "stream_ingest.capabilities"], cwd=backend_root(),
            env=worker_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return unavailable("the capability probe timed out")
    except OSError as error:
        return unavailable(f"the capability probe could not start ({type(error).__name__})")
    lines = [line for line in result.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    try:
        document = json.loads(lines[-1]) if lines else None
    except ValueError:
        document = None
    if not isinstance(document, dict):
        return unavailable(f"the capability probe failed (exit code {result.returncode})")
    return document


class CapabilityCache:
    """Probes once, in the background, and serves the result. Callers that
    need capabilities before the probe finished wait for it."""

    def __init__(self, prober=run_probe):
        self._prober = prober
        self._result: Optional[Dict[str, Any]] = None
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._listeners = []

    def add_listener(self, listener) -> None:
        """Call ``listener(capabilities)`` once the probe finished (at once
        when it already has). The Edge_Sync_Agent reports them then."""
        with self._lock:
            if not self._done.is_set():
                self._listeners.append(listener)
                return
        self._notify(listener)

    def _notify(self, listener) -> None:
        try:
            listener(dict(self._result))
        except Exception:  # noqa: BLE001 - a listener must not break the probe
            logger.exception("A stream capability listener failed")

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._run, name="stream-capability-probe", daemon=True).start()

    def _run(self) -> None:
        try:
            result = self._prober()
        except Exception as error:  # noqa: BLE001 - never raised into callers
            result = unavailable(f"the capability probe failed ({type(error).__name__})")
        with self._lock:
            self._result = result
            self._done.set()
            listeners, self._listeners = self._listeners, []
        # Logged once (design component 11).
        logger.info("Device stream capabilities: %s", json.dumps(result, sort_keys=True))
        for listener in listeners:
            self._notify(listener)

    def get(self, wait_s: float = PROBE_TIMEOUT_S) -> Dict[str, Any]:
        """The capabilities, waiting up to ``wait_s`` for the probe."""
        self.start()
        if not self._done.wait(wait_s):
            return unavailable("the capability probe has not finished")
        return dict(self._result)

    def peek(self) -> Optional[Dict[str, Any]]:
        """The capabilities if the probe finished, else None."""
        return dict(self._result) if self._done.is_set() else None


_cache: Optional[CapabilityCache] = None
_cache_lock = threading.Lock()


def get_capability_cache() -> CapabilityCache:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = CapabilityCache()
        return _cache


def set_capability_cache(cache: Optional[CapabilityCache]) -> None:
    global _cache
    with _cache_lock:
        _cache = cache


if __name__ == "__main__":
    sys.exit(main())
