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
"""Video loop core for the Static_Video_Camera (feature: static-camera-video-loop).

Video_Validation (container sniff + limits + first/last frame decode), the
wall-clock Loop_Position arithmetic, and the per-process decoder that serves
loop frames as the standard packed-RGB frame dict.

Two hard constraints shape this module:

- **Stdlib-only at import time.** OpenCV (``cv2``) is imported lazily inside
  the functions that decode, so importing this module never loads OpenCV's
  native libraries, and the module imports on hosts without it.
- **Vendored verbatim into the Portal.** ``edge-cv-portal/backend/functions/
  video_loop.py`` must stay a byte-identical copy (a parity test compares the
  sha256 of both files), so the Portal's pre-upload validation is exactly the
  device's. Keep this file free of LocalServer imports and edit both copies
  together.

Decoding uses OpenCV's FFmpeg backend (``cv2.CAP_FFMPEG``), which every
LocalServer image and the Portal's video layer ship at the same version
(opencv-python 4.11.x). Seeks with ``CAP_PROP_POS_FRAMES`` are frame-exact on
that build (verified for H.264 incl. long GOPs and B-frames, HEVC, VP9, MJPEG,
and MPEG-4 at 29.97 fps), which is what lets the player mix forward stepping
and seeking and still return the same bytes for the same frame index.

Run as a script (``python video_loop.py probe <path>``) it prints the probe
result as one JSON object; the Portal uses that to validate in a child process
it can time out.
"""
import json
import math
import os
import struct
import sys
from dataclasses import dataclass

#: Supported_Video_Container names, as :func:`sniff_video_container` returns them.
SUPPORTED_VIDEO_CONTAINERS = ("MP4", "MOV", "AVI", "MKV", "WEBM")

#: Supported_Video_Codecs: decodable on every LocalServer platform and in the
#: Portal validator (user-facing names).
SUPPORTED_VIDEO_CODECS = (
    "H.264",
    "H.265/HEVC",
    "MPEG-4 Part 2",
    "Motion JPEG",
    "VP8",
    "VP9",
)

#: Largest accepted Pinned_Video file (Requirement 1.5).
MAX_PIN_VIDEO_BYTES = 100 * 1024 * 1024

#: Largest accepted displayed frame width or height, in pixels (Requirement 1.6).
MAX_VIDEO_DIMENSION = 4096

#: Highest accepted frame rate, in frames per second (Requirement 1.6).
MAX_VIDEO_FPS = 240.0

#: Bytes of the file head :func:`sniff_video_container` inspects.
SNIFF_BYTES = 64

#: FFmpeg log level for OpenCV's FFmpeg backend unless the operator set one:
#: AV_LOG_FATAL (8). Undecodable input (e.g. AV1, which this OpenCV build
#: cannot decode) otherwise floods stderr with per-packet decoder errors; the
#: validation message already names the codec.
_DEFAULT_FFMPEG_LOGLEVEL = "8"

# ISO-BMFF ``ftyp`` major brands of still-image formats (HEIF/AVIF, Canon
# raw) — not videos even though they share the MP4 box structure.
_STILL_IMAGE_BRANDS = frozenset(
    (b"heic", b"heix", b"heim", b"heis", b"mif1", b"avif", b"crx ")
)

# First-atom types of QuickTime files that have no leading ``ftyp`` box.
_QUICKTIME_LEADING_ATOMS = frozenset(
    (b"moov", b"wide", b"mdat", b"free", b"skip", b"pnot")
)

_EBML_MAGIC = b"\x1a\x45\xdf\xa3"

# Normalized codec names by lower-cased FourCC. OpenCV reports the stream's
# codec tag, or a tag derived from FFmpeg's codec name when the container
# carries none, so several spellings map to one codec.
_CODEC_ALIASES = {
    "H264": ("h264", "avc1", "avc3", "x264", "davc", "vssh"),
    "HEVC": ("hevc", "hvc1", "hev1", "h265", "x265"),
    "MPEG4": ("fmp4", "mp4v", "xvid", "divx", "dx50", "mp4s", "m4s2", "3iv2"),
    "MJPEG": ("mjpg", "mjpa", "mjpb", "jpeg", "avrn", "ljpg"),
    "VP8": ("vp80", "vp08"),
    "VP9": ("vp90", "vp09"),
    "AV1": ("av01",),
}
_CODEC_BY_FOURCC = {
    alias: name for name, aliases in _CODEC_ALIASES.items() for alias in aliases
}


class VideoValidationError(Exception):
    """A file failed Video_Validation. The message is user-facing: it names
    the violated limit, or states the file is not a supported video / could
    not be decoded and lists what is supported."""


class VideoDecodeError(Exception):
    """A frame of an already-validated video could not be decoded."""


@dataclass(frozen=True)
class VideoInfo:
    """What :func:`probe_video` learned about an acceptable video.

    ``width``/``height`` are the displayed (post-rotation) dimensions and
    ``frame_count`` is the exact number of decodable frames."""

    container: str
    codec: str
    width: int
    height: int
    fps: float
    frame_count: int

    @property
    def duration_ms(self) -> int:
        """Loop duration: frame count divided by frame rate, in ms."""
        return int(round(self.frame_count * 1000.0 / self.fps))

    def as_metadata(self) -> dict:
        """The Video_Metadata fields this probe determines."""
        return {
            "format": self.container,
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "frameCount": self.frame_count,
            "durationMs": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def not_a_video_message() -> str:
    return (
        "The submitted file is not a supported video. Supported video "
        "formats: {}.".format(", ".join(SUPPORTED_VIDEO_CONTAINERS))
    )


def undecodable_message(codec: str, detail: str = "") -> str:
    return (
        "The video could not be decoded{} (codec: {}). Supported video "
        "codecs: {}.".format(detail, codec, ", ".join(SUPPORTED_VIDEO_CODECS))
    )


def oversize_message(size_bytes: int, limit_bytes: int) -> str:
    return (
        "Submitted video file is {} bytes, which exceeds the maximum accepted "
        "video file size of {} bytes ({:g} MB).".format(
            size_bytes, limit_bytes, limit_bytes / (1024 * 1024)
        )
    )


# ---------------------------------------------------------------------------
# Container sniffing and codec names (pure)
# ---------------------------------------------------------------------------


def sniff_video_container(head):
    """The Supported_Video_Container ``head`` starts with, or ``None``.

    Only the first :data:`SNIFF_BYTES` bytes are inspected:

    - ISO-BMFF ``ftyp`` box: brand ``qt  `` → ``MOV``; a still-image brand
      (HEIF/AVIF) → ``None``; any other brand → ``MP4`` (incl. M4V, 3GP).
    - a leading QuickTime atom (``moov``/``wide``/``mdat``/``free``/``skip``/
      ``pnot``) with a plausible size → ``MOV``.
    - ``RIFF`` … ``AVI `` → ``AVI``.
    - EBML magic → ``WEBM`` when the header names the ``webm`` DocType,
      else ``MKV``.
    """
    head = bytes(head[:SNIFF_BYTES])
    if len(head) >= 12 and head[4:8] == b"ftyp":
        size = struct.unpack(">I", head[0:4])[0]
        if size >= 8 or size in (0, 1):
            brand = head[8:12]
            if brand in _STILL_IMAGE_BRANDS:
                return None
            return "MOV" if brand == b"qt  " else "MP4"
    if len(head) >= 8 and head[4:8] in _QUICKTIME_LEADING_ATOMS:
        size = struct.unpack(">I", head[0:4])[0]
        if size >= 8 or size in (0, 1):
            return "MOV"
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "AVI"
    if head[0:4] == _EBML_MAGIC:
        return "WEBM" if b"webm" in head else "MKV"
    return None


def normalize_codec(fourcc) -> str:
    """Normalized codec name for an OpenCV ``CAP_PROP_FOURCC`` value:
    ``H264``/``HEVC``/``MPEG4``/``MJPEG``/``VP8``/``VP9``/``AV1``, else the
    printable raw FourCC, else ``unknown``. Informational only — acceptance
    is decided by decoding."""
    try:
        value = int(fourcc)
    except (TypeError, ValueError, OverflowError):
        return "unknown"
    if value <= 0:
        return "unknown"
    raw = "".join(chr((value >> (8 * i)) & 0xFF) for i in range(4))
    name = _CODEC_BY_FOURCC.get(raw.lower())
    if name is not None:
        return name
    printable = raw.strip("\x00 ")
    if printable and printable.isprintable():
        return printable
    return "unknown"


# ---------------------------------------------------------------------------
# Loop position (pure)
# ---------------------------------------------------------------------------


def loop_frame_index(now_ms, epoch_ms, fps, frame_count) -> int:
    """The Loop_Position at ``now_ms`` for a loop that started at ``epoch_ms``.

    ``floor(((now - epoch) mod loop) / period)`` with ``period = 1000/fps`` ms
    and ``loop = frame_count * period`` — playback at the native frame rate,
    wrapping to frame 0 right after the last frame's display interval.
    Python's float modulo keeps times before the epoch in range too."""
    frame_count = int(frame_count)
    fps = float(fps)
    if frame_count < 1:
        raise ValueError("frame_count must be at least 1")
    if not (math.isfinite(fps) and fps > 0.0):
        raise ValueError("fps must be a positive finite number")
    if frame_count == 1:
        return 0
    period_ms = 1000.0 / fps
    loop_ms = frame_count * period_ms
    elapsed_ms = (float(now_ms) - float(epoch_ms)) % loop_ms
    index = int(elapsed_ms // period_ms)
    return min(max(index, 0), frame_count - 1)


# ---------------------------------------------------------------------------
# OpenCV access (lazy)
# ---------------------------------------------------------------------------


def _import_cv2():
    """Import OpenCV on first use (never at module import)."""
    os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", _DEFAULT_FFMPEG_LOGLEVEL)
    import cv2  # noqa: WPS433 - deliberate lazy import

    return cv2


def _open_capture(cv2, path):
    """A ``VideoCapture`` on ``path`` through the FFmpeg backend with
    rotation metadata applied (frames come out in display orientation)."""
    capture = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
    if hasattr(cv2, "CAP_PROP_ORIENTATION_AUTO"):
        capture.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
    return capture


# ---------------------------------------------------------------------------
# Video_Validation
# ---------------------------------------------------------------------------


def probe_video(path) -> VideoInfo:
    """Validate the video at ``path`` and describe it (design Decision 5).

    Raises :class:`VideoValidationError` with a user-facing message when the
    file is not a Supported_Video_Container, its frame rate or displayed
    dimensions are out of range, or its first or last frame cannot be
    decoded. The size limit is the caller's check (it is applied before a
    probe, on both device and Portal)."""
    with open(path, "rb") as media:
        head = media.read(SNIFF_BYTES)
    container = sniff_video_container(head)
    if container is None:
        raise VideoValidationError(not_a_video_message())

    cv2 = _import_cv2()
    capture = _open_capture(cv2, path)
    try:
        if not capture.isOpened():
            raise VideoValidationError(undecodable_message("unknown"))
        codec = normalize_codec(capture.get(cv2.CAP_PROP_FOURCC))

        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        if not (math.isfinite(fps) and 0.0 < fps <= MAX_VIDEO_FPS):
            raise VideoValidationError(
                "The video's frame rate ({}) is outside the supported range: "
                "greater than 0 and at most {:g} frames per second.".format(
                    "{:g} fps".format(fps) if math.isfinite(fps) else "unknown",
                    MAX_VIDEO_FPS,
                )
            )

        reported = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        frame_count = int(reported) if math.isfinite(reported) and reported >= 1 else 0
        if frame_count < 1:
            raise VideoValidationError(
                undecodable_message(codec, detail=" (the file reports no frames)")
            )

        ok, first = capture.read()
        if not ok or first is None:
            raise VideoValidationError(undecodable_message(codec))
        height, width = int(first.shape[0]), int(first.shape[1])
        if width > MAX_VIDEO_DIMENSION or height > MAX_VIDEO_DIMENSION:
            raise VideoValidationError(
                "The video's frame size {}x{} exceeds the maximum of {} pixels "
                "per side.".format(width, height, MAX_VIDEO_DIMENSION)
            )

        last_index = _last_decodable_index(cv2, capture, frame_count, fps)
        if last_index is None:
            raise VideoValidationError(
                undecodable_message(
                    codec,
                    detail=" to its last frame; the file may be truncated or damaged",
                )
            )
    finally:
        capture.release()
    return VideoInfo(
        container=container,
        codec=codec,
        width=width,
        height=height,
        fps=fps,
        frame_count=last_index + 1,
    )


def _last_decodable_index(cv2, capture, frame_count, fps):
    """Index of the last decodable frame, searching back from the reported
    last frame by at most ``ceil(fps)`` frames (a container may over-report
    its frame count by a few). ``None`` when none of them decodes."""
    if frame_count == 1:
        return 0
    back_off = max(1, int(math.ceil(fps)))
    lowest = max(0, frame_count - 1 - back_off)
    for index in range(frame_count - 1, lowest - 1, -1):
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = capture.read()
        if ok and frame is not None:
            return index
    return None


# ---------------------------------------------------------------------------
# Loop playback
# ---------------------------------------------------------------------------


class VideoLoopPlayer:
    """One process's decoder for one Pinned_Video (design Decision 4).

    Serves frame ``index`` as ``{'data', 'width', 'height', 'pixel_format':
    'RGB'}`` with packed 24-bit RGB data. The last decoded frame is cached, so
    grabs within one frame interval return the identical dict. Short forward
    moves (up to ``ceil(2 * fps)`` frames) step with ``grab()``; anything
    else — backward jumps, loop wraps, long gaps — seeks, which is
    frame-exact on this OpenCV build. A failed read reopens the capture once
    and retries by seek before raising :class:`VideoDecodeError`.

    Not thread-safe: the owning store serializes access. The capture is
    opened lazily on the first :meth:`frame` call, so nothing decodes until a
    frame is requested.
    """

    def __init__(self, path, fps, frame_count, width=None, height=None,
                 step_window=None):
        self._path = path
        self._fps = float(fps)
        self._frame_count = int(frame_count)
        if self._frame_count < 1:
            raise ValueError("frame_count must be at least 1")
        self._width = int(width) if width else None
        self._height = int(height) if height else None
        if step_window is None:
            step_window = max(1, int(math.ceil(2.0 * self._fps)))
        self._step_window = int(step_window)
        self._capture = None
        self._next_index = 0
        self._cached_index = None
        self._cached_frame = None

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def frame(self, index) -> dict:
        """The RGB frame dict for frame ``index`` (0-based)."""
        index = int(index)
        if not 0 <= index < self._frame_count:
            raise ValueError(
                "frame index {} is outside 0..{}".format(index, self._frame_count - 1)
            )
        if self._cached_index == index and self._cached_frame is not None:
            return self._cached_frame
        cv2 = _import_cv2()
        raw = self._decode(cv2, index)
        frame = self._to_frame(cv2, raw)
        self._cached_index = index
        self._cached_frame = frame
        return frame

    def close(self) -> None:
        """Release the decoder and drop the cached frame. Idempotent."""
        self._release_capture()
        self._cached_index = None
        self._cached_frame = None

    # -- internals ----------------------------------------------------------

    def _release_capture(self):
        capture, self._capture = self._capture, None
        self._next_index = 0
        if capture is not None:
            try:
                capture.release()
            except Exception:  # noqa: BLE001 - best-effort release
                pass

    def _decode(self, cv2, index):
        last_error = "no attempt completed"
        for _attempt in range(2):
            if self._capture is None:
                capture = _open_capture(cv2, self._path)
                if not capture.isOpened():
                    capture.release()
                    last_error = "the video file could not be opened"
                    continue
                self._capture = capture
                self._next_index = 0
            capture = self._capture
            if not self._next_index <= index <= self._next_index + self._step_window:
                capture.set(cv2.CAP_PROP_POS_FRAMES, index)
                self._next_index = index
            stepped = True
            while self._next_index < index:
                if not capture.grab():
                    stepped = False
                    break
                self._next_index += 1
            if stepped:
                ok, raw = capture.read()
                if ok and raw is not None:
                    self._next_index = index + 1
                    return raw
            last_error = "frame {} could not be read".format(index)
            # Reopen once; the retry seeks straight to the target (a fresh
            # capture starts at 0, so any index past the step window seeks).
            self._release_capture()
        raise VideoDecodeError(
            "The pinned video could not be decoded at frame {}: {}".format(
                index, last_error
            )
        )

    def _to_frame(self, cv2, raw):
        rgb = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
        height, width = int(rgb.shape[0]), int(rgb.shape[1])
        if (self._width and self._height
                and (width, height) != (self._width, self._height)):
            # A stream that changes resolution mid-file still honors the
            # frame contract: every frame has the validated dimensions.
            rgb = cv2.resize(rgb, (self._width, self._height),
                             interpolation=cv2.INTER_AREA)
            height, width = self._height, self._width
        return {
            "data": rgb.tobytes(),
            "width": width,
            "height": height,
            "pixel_format": "RGB",
        }


# ---------------------------------------------------------------------------
# Script entry point: `python video_loop.py probe <path>` (Portal child process)
# ---------------------------------------------------------------------------


def _main(argv):
    if len(argv) != 3 or argv[1] != "probe":
        sys.stderr.write("usage: video_loop.py probe <path>\n")
        return 2
    try:
        info = probe_video(argv[2])
    except VideoValidationError as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - reported to the parent as JSON
        result = {"ok": False, "error": undecodable_message("unknown"),
                  "internal": "{}: {}".format(type(exc).__name__, exc)}
    else:
        result = {"ok": True, "info": info.as_metadata()}
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
