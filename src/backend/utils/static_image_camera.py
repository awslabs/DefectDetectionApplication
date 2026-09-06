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
"""Static image camera core: pinning, persistence, and frame synthesis.

This module owns the Pinned_Image lifecycle for the virtual
Static_Image_Camera (feature: static-image-camera-source). It is
deliberately free of ``gi`` imports so it is importable (and testable) on
any host without the Aravis/GLib stack.

Disk is the source of truth: every public method re-reads the on-disk
state (a cheap ``stat`` plus an ``(inode, mtime, size)``-keyed decode
cache), so a LocalServer restart needs no init hook — the first request
after restart simply reads the same files. Replacement is atomic via
write-to-temp + ``os.replace``.

On-disk layout (under ``$COMPONENT_WORK_PATH/static_image_camera/``):

    pinned_image        original uploaded/referenced file bytes
    pinned_image.json   metadata sidecar (fileName, format, width, height,
                        fileSizeBytes, pinnedAtEpochMs)
    .tmp-*              transient staging files for atomic os.replace
"""
import io
import json
import logging
import os
import tempfile
import threading
import time

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Fixed identity (Requirement 2.4). Never derived from image content: the
# identifier must be character-for-character stable across pin, replace,
# and restart so Image_Source records and workflow camera bindings stay
# valid.
STATIC_IMAGE_CAMERA_ID = "static-image-camera"

# Identity fields for the enumeration entry (Requirement 2.1) — all
# non-empty, matching model.Camera's constructor keyword-for-keyword.
STATIC_IMAGE_CAMERA_IDENTITY = {
    "id": STATIC_IMAGE_CAMERA_ID,
    "model": "Static Image Camera",
    "address": "internal",
    "physical_id": STATIC_IMAGE_CAMERA_ID,
    "protocol": "StaticImage",
    "serial": "STATIC-IMAGE-0",
    "vendor": "AWS-DDA",
}

MAX_PIN_FILE_BYTES = 50 * 1024 * 1024  # Requirement 1.4
SUPPORTED_FORMATS = ("JPEG", "PNG", "BMP")  # Requirement 1.3

_IMAGE_FILE_NAME = "pinned_image"
_METADATA_FILE_NAME = "pinned_image.json"


class StaticImagePinError(Exception):
    """A pin / replace / unpin operation failed (validation or storage).

    Raised with the prior pinned state left untouched (Requirements 1.3,
    1.4, 1.8, 5.3, 5.5)."""


class StaticImageUnavailableError(Exception):
    """A frame grab found no usable Pinned_Image (Requirement 3.5)."""


class StaticImageStore:
    """Owns the Pinned_Image: validation, atomic persistence, frames.

    ``base_dir`` defaults to ``$COMPONENT_WORK_PATH/static_image_camera``.
    ``max_file_bytes`` is injectable for tests (Requirement 1.4 uses the
    production default)."""

    def __init__(self, base_dir=None, max_file_bytes=MAX_PIN_FILE_BYTES):
        if base_dir is None:
            base_dir = os.path.join(
                os.environ["COMPONENT_WORK_PATH"], "static_image_camera"
            )
        self._base_dir = base_dir
        self._image_path = os.path.join(base_dir, _IMAGE_FILE_NAME)
        self._meta_path = os.path.join(base_dir, _METADATA_FILE_NAME)
        self._max_file_bytes = max_file_bytes
        self._lock = threading.RLock()
        # (st_ino, st_mtime_ns, st_size) -> frame dict. Guarded by _lock.
        self._decode_cache = None

    # ------------------------------------------------------------------
    # Pinning
    # ------------------------------------------------------------------

    def pin_bytes(self, data, file_name):
        """Validate and atomically pin ``data`` as the Pinned_Image.

        All validation (size, decodability, format) happens before any
        replace, so a failed pin never disturbs the prior state
        (Requirements 1.1, 1.3, 1.4, 5.1, 5.3). Returns the metadata dict
        (Requirement 1.5)."""
        data = bytes(data)
        if len(data) > self._max_file_bytes:
            raise StaticImagePinError(
                "Submitted image file is {} bytes, which exceeds the maximum "
                "accepted file size of {} bytes.".format(
                    len(data), self._max_file_bytes
                )
            )
        img_format, width, height = self._validate_decode(data)
        metadata = {
            "fileName": file_name,
            "format": img_format,
            "width": width,
            "height": height,
            "fileSizeBytes": len(data),
            "pinnedAtEpochMs": int(time.time() * 1000),
        }
        with self._lock:
            try:
                os.makedirs(self._base_dir, exist_ok=True)
                self._atomic_write(self._image_path, data)
                self._atomic_write(
                    self._meta_path,
                    json.dumps(metadata, indent=2).encode("utf-8"),
                )
            except OSError as exc:
                raise StaticImagePinError(
                    "Failed to store the pinned image: {}".format(exc)
                ) from exc
            self._decode_cache = None
        return dict(metadata)

    def pin_file(self, path, captures_root):
        """Pin an existing on-device captured image (Requirement 1.7).

        Rejects any path resolving outside ``captures_root``
        (path-traversal guard) and missing files (Requirement 1.8), then
        applies the same validation as :meth:`pin_bytes`."""
        real_root = os.path.realpath(captures_root)
        real_path = os.path.realpath(path)
        try:
            inside = os.path.commonpath([real_path, real_root]) == real_root
        except ValueError:
            inside = False
        if not inside:
            raise StaticImagePinError(
                "The referenced captured image path resolves outside the "
                "captured images directory and cannot be pinned: {}".format(path)
            )
        if not os.path.isfile(real_path):
            raise StaticImagePinError(
                "The referenced captured image was not found: {}".format(path)
            )
        size = os.path.getsize(real_path)
        if size > self._max_file_bytes:
            raise StaticImagePinError(
                "Referenced image file is {} bytes, which exceeds the maximum "
                "accepted file size of {} bytes.".format(
                    size, self._max_file_bytes
                )
            )
        with open(real_path, "rb") as source:
            data = source.read()
        return self.pin_bytes(data, os.path.basename(real_path))

    # ------------------------------------------------------------------
    # Status / lifecycle
    # ------------------------------------------------------------------

    def status(self):
        """Pin status + metadata (Requirements 1.6, 6.2, 6.4)."""
        with self._lock:
            pinned, metadata = self._inspect_locked()
        return {
            "pinned": pinned,
            "cameraId": STATIC_IMAGE_CAMERA_ID,
            "metadata": metadata,
        }

    def is_pinned(self):
        """Whether a usable Pinned_Image currently exists (gates the
        Static_Image_Camera's inclusion in camera enumeration)."""
        with self._lock:
            pinned, _ = self._inspect_locked()
        return pinned

    def unpin(self):
        """Delete the Pinned_Image (Requirement 5.4).

        Raises :class:`StaticImagePinError` when nothing is pinned
        (Requirement 5.5). Partial/corrupt on-disk state counts as
        removable so unpin can always return the store to a clean slate."""
        with self._lock:
            image_exists = os.path.isfile(self._image_path)
            meta_exists = os.path.isfile(self._meta_path)
            if not image_exists and not meta_exists:
                raise StaticImagePinError(
                    "Cannot remove the pinned image: no image is pinned."
                )
            for path in (self._image_path, self._meta_path):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise StaticImagePinError(
                        "Failed to remove the pinned image: {}".format(exc)
                    ) from exc
            self._decode_cache = None

    # ------------------------------------------------------------------
    # Frames
    # ------------------------------------------------------------------

    def get_frame(self):
        """Return the Pinned_Image as a standard frame dict.

        ``{'data': packed 24-bit RGB bytes, 'width', 'height',
        'pixel_format': 'RGB'}`` with ``len(data) == 3 * width * height``
        (Requirements 3.1, 3.2, 3.3, 3.7). The snapshot is taken under the
        store lock so no grab can observe mixed content across a replace
        (Requirement 5.1). Raises :class:`StaticImageUnavailableError`
        naming the camera when no usable Pinned_Image exists
        (Requirement 3.5)."""
        with self._lock:
            try:
                frame = self._decode_frame_locked()
            except Exception as exc:
                raise StaticImageUnavailableError(
                    "Static image camera '{}': no usable pinned image is "
                    "available (pin an image through the pin API before "
                    "grabbing frames): {}".format(STATIC_IMAGE_CAMERA_ID, exc)
                ) from exc
        # Shallow copy: 'data' is immutable bytes, but callers must not be
        # able to mutate the cached dict itself.
        return dict(frame)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _validate_decode(self, data):
        """Fully decode ``data``; return (format, width, height).

        Dimensions are post-EXIF-transpose so EXIF-rotated JPEGs report
        the orientation the user sees. Raises StaticImagePinError
        enumerating the supported formats on any failure."""
        try:
            with Image.open(io.BytesIO(data)) as img:
                img_format = img.format
                if img_format not in SUPPORTED_FORMATS:
                    raise StaticImagePinError(
                        "The submitted file could not be decoded as a "
                        "supported image format. Supported formats: "
                        "{}.".format(", ".join(SUPPORTED_FORMATS))
                    )
                transposed = ImageOps.exif_transpose(img)
                transposed.load()  # force a full decode before any replace
                width, height = transposed.size
        except StaticImagePinError:
            raise
        except Exception as exc:
            raise StaticImagePinError(
                "The submitted file could not be decoded as a supported "
                "image format. Supported formats: {}. ({})".format(
                    ", ".join(SUPPORTED_FORMATS), exc
                )
            ) from exc
        return img_format, width, height

    def _atomic_write(self, dest_path, payload):
        """Write ``payload`` to a .tmp-* staging file, fsync, os.replace."""
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=self._base_dir)
        try:
            with os.fdopen(fd, "wb") as staging:
                staging.write(payload)
                staging.flush()
                os.fsync(staging.fileno())
            os.replace(tmp_path, dest_path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def _decode_frame_locked(self):
        """Decode the pinned file to a packed-RGB frame dict (cached).

        Must be called with ``self._lock`` held. The cache key is
        ``(st_ino, st_mtime_ns, st_size)`` so any replacement or in-place
        modification of the pinned file invalidates it. Determinism
        (Requirements 3.3, 6.3): the decode of a given file with a given
        Pillow build is deterministic, and the cache additionally
        guarantees byte-identity within a process lifetime."""
        file_stat = os.stat(self._image_path)
        cache_key = (file_stat.st_ino, file_stat.st_mtime_ns, file_stat.st_size)
        if self._decode_cache is not None and self._decode_cache[0] == cache_key:
            return self._decode_cache[1]
        with open(self._image_path, "rb") as pinned:
            raw = pinned.read()
        with Image.open(io.BytesIO(raw)) as img:
            rgb = ImageOps.exif_transpose(img).convert("RGB")
            frame = {
                "data": rgb.tobytes(),
                "width": rgb.width,
                "height": rgb.height,
                "pixel_format": "RGB",
            }
        self._decode_cache = (cache_key, frame)
        return frame

    def _inspect_locked(self):
        """Return ``(pinned, metadata)``; must hold ``self._lock``.

        Distinguishes *missing data* (no files at all → simply not pinned)
        from *corrupt state* (partial files, unreadable sidecar, or
        undecodable image bytes → log an error identifying the cause
        category, report not pinned). Neither blocks the caller
        (Requirements 6.4, 6.5)."""
        image_exists = os.path.isfile(self._image_path)
        meta_exists = os.path.isfile(self._meta_path)
        if not image_exists and not meta_exists:
            return False, None
        if not image_exists or not meta_exists:
            missing = self._image_path if not image_exists else self._meta_path
            logger.error(
                "Static image camera '%s': pinned image could not be "
                "restored (missing data: %s does not exist).",
                STATIC_IMAGE_CAMERA_ID,
                missing,
            )
            return False, None
        try:
            with open(self._meta_path, "r", encoding="utf-8") as sidecar:
                metadata = json.load(sidecar)
        except Exception as exc:
            logger.error(
                "Static image camera '%s': pinned image could not be "
                "restored (undecodable data: metadata sidecar unreadable: "
                "%s).",
                STATIC_IMAGE_CAMERA_ID,
                exc,
            )
            return False, None
        try:
            self._decode_frame_locked()
        except FileNotFoundError as exc:
            logger.error(
                "Static image camera '%s': pinned image could not be "
                "restored (missing data: %s).",
                STATIC_IMAGE_CAMERA_ID,
                exc,
            )
            return False, None
        except Exception as exc:
            logger.error(
                "Static image camera '%s': pinned image could not be "
                "restored (undecodable data: %s).",
                STATIC_IMAGE_CAMERA_ID,
                exc,
            )
            return False, None
        return True, metadata


_store = None
_store_lock = threading.Lock()


def get_store():
    """Module-level singleton over the default base directory."""
    global _store
    with _store_lock:
        if _store is None:
            _store = StaticImageStore()
        return _store
