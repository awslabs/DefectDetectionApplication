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
"""The Stream_Worker control protocol (rtsp-rtmp-stream-cameras design
component 11, "Control protocol" and "Frame transport").

Messages are JSON objects, one per line, with an ``op``:

- parent to worker, on stdin: ``config`` (first line only; the only place
  credentials ever travel), ``frame`` (a request for the newest frame after
  a sequence number), ``stop``;
- worker to parent, on stdout: ``health`` (every 2 s and on changes),
  ``frame`` (a reply), ``error`` (a categorized, redacted failure).

Frames travel through a POSIX shared-memory file under ``/dev/shm`` with two
slots. The worker writes the requested Latest_Frame into the slot the parent
is not reading, then replies with the header; the parent copies the frame
out before it asks again. A resolution change makes the worker create a new
segment. Segments are created mode 0600 and named ``dda-stream-*``, so the
parent can sweep the ones a killed worker left behind.
"""
import json
import mmap
import os
import secrets as _secrets
from typing import Any, Dict, Optional

#: Upper bound on one protocol line; the config line is well under 8 KiB.
MAX_LINE_BYTES = 64 * 1024

SHM_DIRECTORY = "/dev/shm"
SHM_PREFIX = "dda-stream-"

OP_CONFIG = "config"
OP_FRAME = "frame"
OP_STOP = "stop"
OP_HEALTH = "health"
OP_ERROR = "error"


def encode(message: Dict[str, Any]) -> bytes:
    """One protocol line."""
    return (json.dumps(message, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def decode(line: Any) -> Optional[Dict[str, Any]]:
    """The message of one protocol line, or None for anything that is not a
    JSON object with a string ``op`` (a stray GStreamer print, a torn line)."""
    if isinstance(line, bytes):
        if len(line) > MAX_LINE_BYTES:
            return None
        line = line.decode("utf-8", "replace")
    if not isinstance(line, str) or not line.strip():
        return None
    try:
        message = json.loads(line)
    except ValueError:
        return None
    if not isinstance(message, dict) or not isinstance(message.get("op"), str):
        return None
    return message


def shm_directory() -> str:
    """Where segments live: ``/dev/shm`` when it is usable, else the
    temporary directory."""
    if os.path.isdir(SHM_DIRECTORY) and os.access(SHM_DIRECTORY, os.W_OK):
        return SHM_DIRECTORY
    import tempfile
    return tempfile.gettempdir()


def is_segment_path(path: Any) -> bool:
    """Whether ``path`` names a frame segment: a ``dda-stream-*`` file
    directly inside the segment directory."""
    if not isinstance(path, str) or not path:
        return False
    directory, name = os.path.split(path)
    return (name.startswith(SHM_PREFIX) and "/" not in name
            and os.path.realpath(directory) == os.path.realpath(shm_directory()))


class FrameSegment:
    """The worker side of a two-slot segment of ``slot_bytes`` per slot."""

    def __init__(self, slot_bytes: int, directory: Optional[str] = None):
        if slot_bytes <= 0:
            raise ValueError("slot_bytes must be positive")
        self.slot_bytes = int(slot_bytes)
        self.path = os.path.join(directory or shm_directory(),
                                 f"{SHM_PREFIX}{os.getpid()}-{_secrets.token_hex(6)}")
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.ftruncate(descriptor, 2 * self.slot_bytes)
            self._map = mmap.mmap(descriptor, 2 * self.slot_bytes)
        except OSError:
            os.close(descriptor)
            self.unlink()
            raise
        os.close(descriptor)
        self._next_slot = 0

    def write(self, data) -> int:
        """Copy ``data`` into the next slot; returns the slot index. Slots
        alternate, so the slot the parent may still be reading is never the
        one written."""
        view = memoryview(data).cast("B")
        if view.nbytes > self.slot_bytes:
            raise ValueError("frame larger than the segment slot")
        slot = self._next_slot
        offset = slot * self.slot_bytes
        self._map[offset:offset + view.nbytes] = view
        self._next_slot = 1 - slot
        return slot

    def unlink(self) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._map.close()
        except (BufferError, ValueError):
            pass
        self.unlink()


class FrameReader:
    """The parent side: maps the segment a frame header names, read-only,
    and copies frames out of it. The mapping is kept until the worker names
    another segment."""

    def __init__(self):
        self._path: Optional[str] = None
        self._map: Optional[mmap.mmap] = None

    def read(self, header: Dict[str, Any]) -> bytes:
        """The packed frame (rows without stride padding) of ``header``.

        Raises ValueError for a header that does not describe a frame inside
        a frame segment, and OSError when the segment is gone.
        """
        path = header.get("shm")
        if not is_segment_path(path):
            raise ValueError("frame header names no frame segment")
        width, height, stride, slot = (int(header.get(key) or 0) for key in ("width", "height", "stride", "slot"))
        channels = int(header.get("channels") or 3)
        row_bytes = width * channels
        if width <= 0 or height <= 0 or stride < row_bytes or slot not in (0, 1):
            raise ValueError("frame header has invalid dimensions")
        if path != self._path:
            self.close()
            descriptor = os.open(path, os.O_RDONLY)
            try:
                self._map = mmap.mmap(descriptor, 0, prot=mmap.PROT_READ)
            finally:
                os.close(descriptor)
            self._path = path
        slot_bytes = len(self._map) // 2
        size = stride * height
        if size > slot_bytes:
            raise ValueError("frame header exceeds the segment")
        offset = slot * slot_bytes
        if stride == row_bytes:
            return bytes(self._map[offset:offset + size])
        # Drop the per-row padding GStreamer adds to align rows to 4 bytes.
        rows = memoryview(self._map)[offset:offset + size]
        packed = bytearray(row_bytes * height)
        for row in range(height):
            start = row * stride
            packed[row * row_bytes:(row + 1) * row_bytes] = rows[start:start + row_bytes]
        rows.release()
        return bytes(packed)

    @property
    def path(self) -> Optional[str]:
        return self._path

    def close(self) -> None:
        if self._map is not None:
            try:
                self._map.close()
            except (BufferError, ValueError):
                pass
        self._map, self._path = None, None


def remove_segment(path: Any) -> None:
    """Unlink a segment a killed worker left behind."""
    if is_segment_path(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def sweep_segments(pids_alive=None) -> int:
    """Remove every segment whose creating worker is gone; returns how many.
    ``pids_alive`` is a predicate over pids (default: the pid exists)."""
    alive = pids_alive or _pid_exists
    directory = shm_directory()
    removed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(SHM_PREFIX):
            continue
        try:
            pid = int(name[len(SHM_PREFIX):].split("-", 1)[0])
        except ValueError:
            continue
        if not alive(pid):
            try:
                os.unlink(os.path.join(directory, name))
                removed += 1
            except OSError:
                pass
    return removed


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
