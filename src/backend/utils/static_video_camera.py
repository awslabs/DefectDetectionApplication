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
"""Static video camera core: pinning, persistence, and loop frames.

This module owns the Pinned_Video lifecycle for the virtual
Static_Video_Camera (feature: static-camera-video-loop) — the sibling of
the Static_Image_Camera (``utils/static_image_camera.py``), with its own
fixed identity, storage directory, and pin slot, so an image and a video
can be pinned at the same time. Like the image store it is free of ``gi``
imports and treats disk as the source of truth, so a LocalServer restart
needs no init hook.

Video_Validation, the Loop_Position arithmetic, and decoding live in
``utils/video_loop.py`` (OpenCV is imported lazily there). A pin stages
the bytes next to the pinned file, validates the staged copy (OpenCV needs
a path), and only then renames it into place, so a failed pin never
disturbs the prior video, its metadata, or its Loop_Epoch.

Every grab serves the frame at the current Loop_Position:
``loop_frame_index(now, pinnedAtEpochMs, fps, frameCount)`` — a pure
function of the sidecar and the wall clock, so every LocalServer process
(the API process, the digital-input process, the workflow executor) and
every restart agree on the frame without coordinating. Each store
instance owns at most one decoder, keyed on the pinned file's
``(st_ino, st_mtime_ns, st_size)``, so a replace or unpin made by another
process is picked up on this process's next grab.

On-disk layout (under ``$COMPONENT_WORK_PATH/static_video_camera/``):

    pinned_video        original uploaded/referenced video bytes
    pinned_video.json   metadata sidecar (fileName, format, codec, width,
                        height, fps, frameCount, durationMs, fileSizeBytes,
                        pinnedAtEpochMs)
    .tmp-*              transient staging files for atomic os.replace
"""
import json
import logging
import os
import shutil
import tempfile
import threading
import time

from utils.video_loop import (
    MAX_PIN_VIDEO_BYTES,
    VideoLoopPlayer,
    VideoValidationError,
    loop_frame_index,
    oversize_message,
    probe_video,
    undecodable_message,
)

logger = logging.getLogger(__name__)

# Fixed identity (Requirement 2.3). Never derived from content, and distinct
# from the Static_Image_Camera's, so bindings to either camera stay valid
# across pins, replaces, and restarts.
STATIC_VIDEO_CAMERA_ID = "static-video-camera"

# Identity fields for the enumeration entry (Requirement 2.1) — all
# non-empty, matching model.Camera's constructor keyword-for-keyword. The
# vendor is the Static_Image_Camera's, so the classic Image_Source path picks
# the AWS-DDA packed-RGB conversion chain (its ``default`` model entry) for
# this model too (Requirement 4.1).
STATIC_VIDEO_CAMERA_IDENTITY = {
    "id": STATIC_VIDEO_CAMERA_ID,
    "model": "Static Video Camera",
    "address": "internal",
    "physical_id": STATIC_VIDEO_CAMERA_ID,
    "protocol": "StaticVideo",
    "serial": "STATIC-VIDEO-0",
    "vendor": "AWS-DDA",
}

_VIDEO_FILE_NAME = "pinned_video"
_METADATA_FILE_NAME = "pinned_video.json"

#: Metadata keys (and types) a usable video sidecar must carry.
_REQUIRED_METADATA = {
    "fps": (int, float),
    "frameCount": int,
    "width": int,
    "height": int,
    "fileSizeBytes": int,
    "pinnedAtEpochMs": (int, float),
}

_COPY_CHUNK_BYTES = 1 << 20


class StaticVideoPinError(Exception):
    """A video pin / replace / unpin operation failed (validation or
    storage). Raised with the prior pinned state left untouched."""


class StaticVideoUnavailableError(Exception):
    """A frame grab found no usable Pinned_Video (Requirement 3.10)."""


class StaticVideoStore:
    """Owns the Pinned_Video: validation, atomic persistence, loop frames.

    ``base_dir`` defaults to ``$COMPONENT_WORK_PATH/static_video_camera``.
    ``max_file_bytes`` (default 100 MB), ``clock`` (wall-clock seconds;
    default ``time.time``, looked up at call time) and ``probe`` (default
    ``video_loop.probe_video``) are injectable for tests."""

    def __init__(self, base_dir=None, max_file_bytes=MAX_PIN_VIDEO_BYTES,
                 clock=None, probe=None):
        if base_dir is None:
            base_dir = os.path.join(
                os.environ["COMPONENT_WORK_PATH"], "static_video_camera"
            )
        self._base_dir = base_dir
        self._video_path = os.path.join(base_dir, _VIDEO_FILE_NAME)
        self._meta_path = os.path.join(base_dir, _METADATA_FILE_NAME)
        self._max_file_bytes = max_file_bytes
        self._clock = clock
        self._probe = probe
        self._lock = threading.RLock()
        # (meta stat key, metadata) — the parsed sidecar. Guarded by _lock.
        self._meta_cache = None
        # ((video key, meta key), (pinned, metadata)) — inspection result.
        self._inspect_cache = None
        # The decoder and the (video key, meta key) it was opened for.
        self._player = None
        self._player_key = None

    # ------------------------------------------------------------------
    # Pinning
    # ------------------------------------------------------------------

    def pin_bytes(self, data, file_name):
        """Validate and atomically pin ``data`` as the Pinned_Video.

        Returns the Video_Metadata dict (Requirements 1.1, 1.2); raises
        :class:`StaticVideoPinError` with the prior state untouched on any
        failure (Requirements 1.3–1.6, 1.9)."""
        size = len(data)
        if size > self._max_file_bytes:
            raise StaticVideoPinError(oversize_message(size, self._max_file_bytes))

        def write(staging):
            staging.write(data)

        staged = self._stage(write)
        return self._commit(staged, file_name, size)

    def pin_file(self, path, captures_root):
        """Pin an existing on-device video file (Requirement 1.7).

        Rejects any path resolving outside ``captures_root`` (path-traversal
        guard) and missing files, applies the video size limit, then
        stream-copies the file into staging — a 100 MB reference is never
        held in memory — and validates it like an upload."""
        real_root = os.path.realpath(captures_root)
        real_path = os.path.realpath(path)
        try:
            inside = os.path.commonpath([real_path, real_root]) == real_root
        except ValueError:
            inside = False
        if not inside:
            raise StaticVideoPinError(
                "The referenced file path resolves outside the captured "
                "images directory and cannot be pinned: {}".format(path)
            )
        if not os.path.isfile(real_path):
            raise StaticVideoPinError(
                "The referenced video file was not found: {}".format(path)
            )
        size = os.path.getsize(real_path)
        if size > self._max_file_bytes:
            raise StaticVideoPinError(
                "Referenced video file is {} bytes, which exceeds the maximum "
                "accepted video file size of {} bytes.".format(
                    size, self._max_file_bytes
                )
            )

        def copy(staging):
            with open(real_path, "rb") as source:
                shutil.copyfileobj(source, staging, _COPY_CHUNK_BYTES)

        staged = self._stage(copy)
        return self._commit(staged, os.path.basename(real_path), size)

    # ------------------------------------------------------------------
    # Status / lifecycle
    # ------------------------------------------------------------------

    def status(self):
        """Pin status + Video_Metadata (Requirement 1.8)."""
        with self._lock:
            pinned, metadata = self._inspect_locked()
        return {
            "pinned": pinned,
            "cameraId": STATIC_VIDEO_CAMERA_ID,
            "metadata": dict(metadata) if metadata else None,
        }

    def is_pinned(self):
        """Whether a usable Pinned_Video exists (gates the
        Static_Video_Camera's inclusion in camera enumeration)."""
        with self._lock:
            pinned, _ = self._inspect_locked()
        return pinned

    def unpin(self):
        """Delete the Pinned_Video (Requirement 5.3). Raises
        :class:`StaticVideoPinError` when nothing is pinned
        (Requirement 5.4); partial/corrupt state counts as removable."""
        with self._lock:
            video_exists = os.path.isfile(self._video_path)
            meta_exists = os.path.isfile(self._meta_path)
            if not video_exists and not meta_exists:
                raise StaticVideoPinError(
                    "Cannot remove the pinned video: no video is pinned."
                )
            self._reset_locked()
            for path in (self._video_path, self._meta_path):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise StaticVideoPinError(
                        "Failed to remove the pinned video: {}".format(exc)
                    ) from exc

    # ------------------------------------------------------------------
    # Frames
    # ------------------------------------------------------------------

    def get_frame(self):
        """The frame at the current Loop_Position as a standard frame dict:
        ``{'data': packed 24-bit RGB bytes, 'width', 'height',
        'pixel_format': 'RGB'}`` (Requirements 3.1–3.5). Raises
        :class:`StaticVideoUnavailableError` naming the camera when no usable
        Pinned_Video exists (Requirement 3.10)."""
        with self._lock:
            try:
                metadata, keys = self._usable_metadata_locked()
                player = self._player_for_locked(keys, metadata)
                index = loop_frame_index(
                    self._now() * 1000.0,
                    metadata["pinnedAtEpochMs"],
                    metadata["fps"],
                    metadata["frameCount"],
                )
                frame = player.frame(index)
            except Exception as exc:
                self._close_player_locked()
                raise StaticVideoUnavailableError(
                    "Static video camera '{}': no usable pinned video is "
                    "available (pin a video through the video pin API before "
                    "grabbing frames): {}".format(STATIC_VIDEO_CAMERA_ID, exc)
                ) from exc
        # Shallow copy: 'data' is immutable bytes, but callers must not be
        # able to mutate the player's cached dict.
        return dict(frame)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _now(self):
        return (self._clock or time.time)()

    def _probe_video(self, path):
        return (self._probe or probe_video)(path)

    def _stage(self, writer):
        """Write a ``.tmp-*`` staging file with ``writer`` and fsync it."""
        try:
            os.makedirs(self._base_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=self._base_dir)
        except OSError as exc:
            raise StaticVideoPinError(
                "Failed to store the pinned video: {}".format(exc)
            ) from exc
        try:
            with os.fdopen(fd, "wb") as staging:
                writer(staging)
                staging.flush()
                os.fsync(staging.fileno())
        except Exception as exc:
            _remove_quietly(tmp_path)
            raise StaticVideoPinError(
                "Failed to store the pinned video: {}".format(exc)
            ) from exc
        return tmp_path

    def _commit(self, staged_path, file_name, size):
        """Probe the staged copy, then rename it and the sidecar into place.

        The sidecar is staged (and fsynced) before either rename, so the
        commit is two back-to-back renames in the store directory."""
        meta_tmp = None
        try:
            try:
                info = self._probe_video(staged_path)
            except VideoValidationError as exc:
                raise StaticVideoPinError(str(exc)) from exc
            except StaticVideoPinError:
                raise
            except Exception as exc:  # noqa: BLE001 - never leak decoder errors raw
                logger.exception("Static video camera probe failed unexpectedly")
                raise StaticVideoPinError(undecodable_message("unknown")) from exc
            metadata = {"fileName": file_name}
            metadata.update(info.as_metadata())
            metadata["fileSizeBytes"] = int(size)
            metadata["pinnedAtEpochMs"] = int(self._now() * 1000)
            meta_tmp = self._stage(
                lambda staging: staging.write(
                    json.dumps(metadata, indent=2).encode("utf-8")
                )
            )
            with self._lock:
                self._reset_locked()
                os.replace(staged_path, self._video_path)
                staged_path = None
                os.replace(meta_tmp, self._meta_path)
                meta_tmp = None
            return dict(metadata)
        except OSError as exc:
            raise StaticVideoPinError(
                "Failed to store the pinned video: {}".format(exc)
            ) from exc
        finally:
            for leftover in (staged_path, meta_tmp):
                if leftover is not None:
                    _remove_quietly(leftover)

    def _reset_locked(self):
        self._close_player_locked()
        self._meta_cache = None
        self._inspect_cache = None

    def _close_player_locked(self):
        player, self._player, self._player_key = self._player, None, None
        if player is not None:
            player.close()

    @staticmethod
    def _stat_key(path):
        file_stat = os.stat(path)
        return (file_stat.st_ino, file_stat.st_mtime_ns, file_stat.st_size)

    def _load_metadata_locked(self):
        """``(metadata, meta_key)`` for the sidecar, validated; raises when
        it is missing, unreadable, or lacks a required field."""
        meta_key = self._stat_key(self._meta_path)
        if self._meta_cache is not None and self._meta_cache[0] == meta_key:
            return self._meta_cache[1], meta_key
        with open(self._meta_path, "r", encoding="utf-8") as sidecar:
            metadata = json.load(sidecar)
        if not isinstance(metadata, dict):
            raise ValueError("metadata sidecar is not a JSON object")
        for key, kinds in _REQUIRED_METADATA.items():
            value = metadata.get(key)
            if isinstance(value, bool) or not isinstance(value, kinds):
                raise ValueError(
                    "metadata sidecar has no valid '{}' field".format(key)
                )
        if metadata["fps"] <= 0 or metadata["frameCount"] < 1:
            raise ValueError("metadata sidecar has an invalid frame rate or count")
        self._meta_cache = (meta_key, metadata)
        return metadata, meta_key

    def _usable_metadata_locked(self):
        """``(metadata, keys)`` when the stored video and sidecar belong
        together; raises otherwise. ``keys`` identifies this exact pair."""
        metadata, meta_key = self._load_metadata_locked()
        video_key = self._stat_key(self._video_path)
        if video_key[2] != metadata["fileSizeBytes"]:
            raise ValueError(
                "the stored video does not match its metadata sidecar "
                "({} bytes on disk, {} recorded)".format(
                    video_key[2], metadata["fileSizeBytes"]
                )
            )
        return metadata, (video_key, meta_key)

    def _player_for_locked(self, keys, metadata):
        if self._player is None or self._player_key != keys:
            self._close_player_locked()
            self._player = VideoLoopPlayer(
                self._video_path,
                metadata["fps"],
                metadata["frameCount"],
                width=metadata["width"],
                height=metadata["height"],
            )
            self._player_key = keys
        return self._player

    def _inspect_locked(self):
        """``(pinned, metadata)``; must hold ``self._lock``.

        Distinguishes *missing data* (no files → simply not pinned; one of
        the two files → logged) from *undecodable data* (unreadable sidecar,
        a size mismatch, or a video whose first frame does not decode →
        logged). Results are cached per ``(video, sidecar)`` stat pair, so a
        corrupt state is logged once per change rather than on every
        enumeration (Requirement 7.3)."""
        video_exists = os.path.isfile(self._video_path)
        meta_exists = os.path.isfile(self._meta_path)
        if not video_exists and not meta_exists:
            self._inspect_cache = None
            return False, None
        if not video_exists or not meta_exists:
            missing = self._video_path if not video_exists else self._meta_path
            logger.error(
                "Static video camera '%s': pinned video could not be restored "
                "(missing data: %s does not exist).",
                STATIC_VIDEO_CAMERA_ID,
                missing,
            )
            self._inspect_cache = None
            return False, None
        try:
            metadata, keys = self._usable_metadata_locked()
        except FileNotFoundError as exc:
            logger.error(
                "Static video camera '%s': pinned video could not be restored "
                "(missing data: %s).",
                STATIC_VIDEO_CAMERA_ID,
                exc,
            )
            return False, None
        except Exception as exc:  # noqa: BLE001 - corrupt state is contained
            logger.error(
                "Static video camera '%s': pinned video could not be restored "
                "(undecodable data: %s).",
                STATIC_VIDEO_CAMERA_ID,
                exc,
            )
            return False, None
        if self._inspect_cache is not None and self._inspect_cache[0] == keys:
            return self._inspect_cache[1]
        probe_player = VideoLoopPlayer(
            self._video_path,
            metadata["fps"],
            metadata["frameCount"],
            width=metadata["width"],
            height=metadata["height"],
        )
        try:
            probe_player.frame(0)
        except Exception as exc:  # noqa: BLE001 - corrupt state is contained
            logger.error(
                "Static video camera '%s': pinned video could not be restored "
                "(undecodable data: %s).",
                STATIC_VIDEO_CAMERA_ID,
                exc,
            )
            result = (False, None)
        else:
            result = (True, metadata)
        finally:
            probe_player.close()
        self._inspect_cache = (keys, result)
        return result


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


_store = None
_store_lock = threading.Lock()


def get_store():
    """Module-level singleton over the default base directory."""
    global _store
    with _store_lock:
        if _store is None:
            _store = StaticVideoStore()
        return _store
