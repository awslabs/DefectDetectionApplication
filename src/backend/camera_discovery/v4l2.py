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
"""V4L2 ioctl layer for Camera_Discovery.

Defines the ctypes mirrors of the kernel V4L2 structures used by
enumeration (``VIDIOC_QUERYCAP`` / ``VIDIOC_ENUM_FMT`` /
``VIDIOC_ENUM_FRAMESIZES``), the corresponding ioctl request codes, and
:class:`V4l2Io` — the injectable I/O layer that talks to real ``/dev/video*``
nodes via ``fcntl.ioctl``. Tests supply a fake object with the same four
methods to emulate arbitrary devices (Requirements 2.1, 2.2).
"""
import ctypes
import fcntl
import glob
import os
from typing import List

# --- ioctl request-code construction (linux asm-generic/ioctl.h) ------------

_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = 8
_IOC_SIZESHIFT = 16
_IOC_DIRSHIFT = 30

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction, type_char, nr, size):
    return (
        (direction << _IOC_DIRSHIFT)
        | (ord(type_char) << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


# --- V4L2 structures ---------------------------------------------------------


class V4l2Capability(ctypes.Structure):
    """struct v4l2_capability (videodev2.h)."""

    _fields_ = [
        ("driver", ctypes.c_char * 16),
        ("card", ctypes.c_char * 32),
        ("bus_info", ctypes.c_char * 32),
        ("version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("device_caps", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


class V4l2FmtDesc(ctypes.Structure):
    """struct v4l2_fmtdesc (videodev2.h)."""

    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("description", ctypes.c_char * 32),
        ("pixelformat", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 4),
    ]


class _FrmSizeDiscrete(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
    ]


class _FrmSizeStepwise(ctypes.Structure):
    _fields_ = [
        ("min_width", ctypes.c_uint32),
        ("max_width", ctypes.c_uint32),
        ("step_width", ctypes.c_uint32),
        ("min_height", ctypes.c_uint32),
        ("max_height", ctypes.c_uint32),
        ("step_height", ctypes.c_uint32),
    ]


class _FrmSizeUnion(ctypes.Union):
    _fields_ = [
        ("discrete", _FrmSizeDiscrete),
        ("stepwise", _FrmSizeStepwise),
    ]


class V4l2FrmSizeEnum(ctypes.Structure):
    """struct v4l2_frmsizeenum (videodev2.h)."""

    _anonymous_ = ("size",)
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("pixel_format", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("size", _FrmSizeUnion),
        ("reserved", ctypes.c_uint32 * 2),
    ]


# --- Request codes and flags -------------------------------------------------

VIDIOC_QUERYCAP = _ioc(_IOC_READ, "V", 0, ctypes.sizeof(V4l2Capability))
VIDIOC_ENUM_FMT = _ioc(
    _IOC_READ | _IOC_WRITE, "V", 2, ctypes.sizeof(V4l2FmtDesc)
)
VIDIOC_ENUM_FRAMESIZES = _ioc(
    _IOC_READ | _IOC_WRITE, "V", 74, ctypes.sizeof(V4l2FrmSizeEnum)
)

#: v4l2_capability.capabilities / device_caps flags.
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_DEVICE_CAPS = 0x80000000

#: v4l2_fmtdesc.type / buffer type for capture enumeration.
V4L2_BUF_TYPE_VIDEO_CAPTURE = 1

#: v4l2_frmsizeenum.type values.
V4L2_FRMSIZE_TYPE_DISCRETE = 1
V4L2_FRMSIZE_TYPE_CONTINUOUS = 2
V4L2_FRMSIZE_TYPE_STEPWISE = 3


def fourcc_to_str(pixelformat: int) -> str:
    """Decode a V4L2 fourcc integer (e.g. ``0x56595559`` -> ``"YUYV"``)."""
    chars = [
        chr((pixelformat >> shift) & 0xFF) for shift in (0, 8, 16, 24)
    ]
    return "".join(chars).strip()


class V4l2Io:
    """Real V4L2 I/O against ``/dev/video*`` device nodes.

    Camera_Discovery only calls these four methods, so tests inject a fake
    object exposing the same interface to emulate arbitrary devices.
    """

    def list_device_paths(self) -> List[str]:
        """Return the sorted ``/dev/video*`` node paths present."""
        return sorted(glob.glob("/dev/video*"))

    def open(self, device_path: str) -> int:
        """Open a device node read-only and non-blocking; return its fd."""
        return os.open(device_path, os.O_RDONLY | os.O_NONBLOCK)

    def close(self, fd: int) -> None:
        os.close(fd)

    def ioctl(self, fd: int, request: int, buffer) -> None:
        """Issue an ioctl, mutating ``buffer`` in place.

        Raises ``OSError`` exactly as ``fcntl.ioctl`` does (``EINVAL``
        terminates V4L2 enumeration loops).
        """
        fcntl.ioctl(fd, request, buffer)
