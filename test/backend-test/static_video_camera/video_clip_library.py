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
"""Synthesized video clips and reference frames for the static-video-camera
suites (feature: static-camera-video-loop).

Clips are generated once per test session with the running image's own
``ffmpeg`` from the ``testsrc2`` source (moving content plus a frame
counter, so neighbouring frames differ). They are tiny (96x64, a few dozen
frames) so a 100-example Hypothesis run stays fast. Encoders missing from
an image's ffmpeg (e.g. libaom on the JetPack 5 image) simply drop that
clip; tests that need a specific clip skip when it is absent.

Reference frames are one sequential OpenCV decode per clip — through the
same FFmpeg backend and rotation setting the device uses — converted to
packed RGB bytes. The player under test reaches frames by stepping and
seeking, so comparing against the sequential reference checks that both
paths return the same bytes. Rotation references come from ffmpeg's own
autorotated decode, an independent oracle for the display orientation.
"""
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional

WIDTH = 96
HEIGHT = 64


@dataclass(frozen=True)
class ClipSpec:
    name: str
    container: str
    codec: str
    rate: str
    seconds: str
    args: tuple
    decodable: bool = True


#: Every decodable Supported_Video_Container x Supported_Video_Codec pairing
#: the suites exercise, plus the undecodable AV1 clip (opens, never decodes).
CLIP_SPECS = (
    ClipSpec("h264.mp4", "MP4", "H264", "12", "2",
             ("-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "12")),
    ClipSpec("h264.mov", "MOV", "H264", "10", "2",
             ("-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10")),
    ClipSpec("h264_bframes.mkv", "MKV", "H264", "25", "1.2",
             ("-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", "-bf", "2")),
    ClipSpec("hevc.mp4", "MP4", "HEVC", "15", "2",
             ("-c:v", "libx265", "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
              "-x265-params", "log-level=error")),
    ClipSpec("mpeg4_2997.mp4", "MP4", "MPEG4", "30000/1001", "1",
             ("-c:v", "mpeg4", "-q:v", "4")),
    ClipSpec("mjpeg.avi", "AVI", "MJPEG", "8", "2",
             ("-c:v", "mjpeg", "-q:v", "5")),
    ClipSpec("vp8.webm", "WEBM", "VP8", "12", "2",
             ("-c:v", "libvpx", "-b:v", "300k", "-deadline", "realtime",
              "-cpu-used", "8")),
    ClipSpec("vp9.webm", "WEBM", "VP9", "12", "2",
             ("-c:v", "libvpx-vp9", "-b:v", "300k", "-deadline", "realtime",
              "-cpu-used", "8")),
)

AV1_SPEC = ClipSpec("av1.mkv", "MKV", "AV1", "12", "1",
                    ("-c:v", "libaom-av1", "-cpu-used", "8", "-b:v", "200k"),
                    decodable=False)

#: Rotation metadata values applied to a copy of h264.mp4.
ROTATIONS = (90, 180, 270)

#: Decodable clips that violate a Video_Validation limit (Requirement 1.6):
#: (name, size, rate, seconds, the limit's wording in the rejection).
LIMIT_SPECS = (
    ("fps300.avi", "32x16", "300", "0.1", "frame rate"),
    ("wide4112.avi", "4112x16", "4", "0.5", "frame size"),
)


@dataclass
class Clip:
    """One generated clip and what the suites expect of it."""

    name: str
    path: str
    container: str
    codec: str
    fps: float = 0.0
    frame_count: int = 0
    width: int = 0
    height: int = 0
    rotation: int = 0
    decodable: bool = True
    #: Sequential-decode reference frames, packed RGB bytes, index = frame.
    frames: List[bytes] = field(default_factory=list)
    #: ffmpeg's autorotated first frame as packed RGB (rotation clips only).
    ffmpeg_first_frame: Optional[bytes] = None

    @property
    def data(self) -> bytes:
        with open(self.path, "rb") as handle:
            return handle.read()


class ClipLibrary:
    """The session's generated clips, by name, plus the limit-violating
    clips (``limit_clips``: name -> (path, limit wording))."""

    def __init__(self, directory: str, clips: Dict[str, Clip], log: List[str],
                 limit_clips: Optional[Dict[str, tuple]] = None):
        self.directory = directory
        self.clips = clips
        self.log = log
        self.limit_clips = limit_clips or {}

    def decodable(self) -> List[Clip]:
        return [clip for clip in self.clips.values()
                if clip.decodable and clip.rotation == 0]

    def rotated(self) -> List[Clip]:
        return [clip for clip in self.clips.values() if clip.rotation]

    def get(self, name: str) -> Optional[Clip]:
        return self.clips.get(name)

    def all_playable(self) -> List[Clip]:
        """Every clip a pin accepts: plain and rotated."""
        return [clip for clip in self.clips.values()
                if clip.decodable and clip.frames]


def tools_available() -> Optional[str]:
    """``None`` when cv2 and ffmpeg are usable, else the reason to skip."""
    try:
        import cv2  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return "OpenCV (cv2) is not importable: {}".format(exc)
    if shutil.which("ffmpeg") is None:
        return "ffmpeg is not on PATH"
    return None


def _ffmpeg(args, log):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + list(args)
    result = subprocess.run(command, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=180)
    if result.returncode != 0:
        log.append("ffmpeg failed ({}): {}".format(
            " ".join(command[-3:]), result.stderr.decode("utf-8", "replace")[-300:]))
        return False
    return True


def _generate(spec: ClipSpec, directory: str, log) -> Optional[str]:
    path = os.path.join(directory, spec.name)
    ok = _ffmpeg(
        ["-f", "lavfi", "-i",
         "testsrc2=size={}x{}:rate={}".format(WIDTH, HEIGHT, spec.rate),
         "-t", spec.seconds, "-an"] + list(spec.args) + [path],
        log,
    )
    return path if ok and os.path.getsize(path) > 0 else None


def _rotate_copy(source: str, rotation: int, directory: str, log) -> Optional[str]:
    """Copy ``source`` with display-rotation metadata ``rotation``.

    ffmpeg >= 6 takes ``-display_rotation`` (counter-clockwise, as an input
    option); older builds take the legacy clockwise ``rotate`` stream tag.
    Either way the reference comes from ffmpeg's own autorotated decode, so
    the suites never depend on which convention produced the file."""
    path = os.path.join(directory, "h264_rot{}.mp4".format(rotation))
    if _ffmpeg(["-display_rotation", str((-rotation) % 360), "-i", source,
                "-c", "copy", path], []):
        return path
    if _ffmpeg(["-i", source, "-c", "copy",
                "-metadata:s:v:0", "rotate={}".format(rotation), path], log):
        return path
    return None


def _sequential_reference(path: str):
    """(fps, frames, width, height) from one sequential decode with the
    device's capture settings."""
    import cv2

    from utils.video_loop import _open_capture

    capture = _open_capture(cv2, path)
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frames = []
        width = height = 0
        while True:
            ok, raw = capture.read()
            if not ok or raw is None:
                break
            rgb = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
            height, width = int(rgb.shape[0]), int(rgb.shape[1])
            frames.append(rgb.tobytes())
    finally:
        capture.release()
    return fps, frames, width, height


def _ffmpeg_first_frame_rgb(path: str, directory: str, log) -> Optional[bytes]:
    out = os.path.join(directory, os.path.basename(path) + ".first.rgb")
    if not _ffmpeg(["-i", path, "-frames:v", "1", "-f", "rawvideo",
                    "-pix_fmt", "rgb24", out], log):
        return None
    with open(out, "rb") as handle:
        return handle.read()


def build_library(directory: str) -> ClipLibrary:
    """Generate every clip the running image can encode, with references."""
    os.makedirs(directory, exist_ok=True)
    clips: Dict[str, Clip] = {}
    log: List[str] = []
    for spec in CLIP_SPECS:
        path = _generate(spec, directory, log)
        if path is None:
            continue
        fps, frames, width, height = _sequential_reference(path)
        if not frames:
            log.append("{}: no frames decoded; dropped".format(spec.name))
            continue
        clips[spec.name] = Clip(
            name=spec.name, path=path, container=spec.container,
            codec=spec.codec, fps=fps, frame_count=len(frames),
            width=width, height=height, frames=frames,
        )
    av1 = _generate(AV1_SPEC, directory, log)
    if av1 is not None:
        clips[AV1_SPEC.name] = Clip(
            name=AV1_SPEC.name, path=av1, container=AV1_SPEC.container,
            codec=AV1_SPEC.codec, decodable=False,
        )
    base = clips.get("h264.mp4")
    if base is not None:
        for rotation in ROTATIONS:
            path = _rotate_copy(base.path, rotation, directory, log)
            if path is None:
                continue
            fps, frames, width, height = _sequential_reference(path)
            reference = _ffmpeg_first_frame_rgb(path, directory, log)
            if not frames or reference is None:
                continue
            clips[os.path.basename(path)] = Clip(
                name=os.path.basename(path), path=path, container="MP4",
                codec="H264", fps=fps, frame_count=len(frames), width=width,
                height=height, rotation=rotation, frames=frames,
                ffmpeg_first_frame=reference,
            )
    limit_clips = {}
    for name, size, rate, seconds, wording in LIMIT_SPECS:
        path = os.path.join(directory, name)
        if _ffmpeg(["-f", "lavfi", "-i",
                    "testsrc2=size={}:rate={}".format(size, rate), "-t", seconds,
                    "-an", "-c:v", "mjpeg", "-q:v", "8", path], log):
            limit_clips[name] = (path, wording)
    return ClipLibrary(directory, clips, log, limit_clips)
