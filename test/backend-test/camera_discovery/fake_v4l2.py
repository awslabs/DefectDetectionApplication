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
"""Fake V4L2 ioctl layer for Camera_Discovery tests.

Implements the same four-method interface as ``camera_discovery.v4l2.V4l2Io``
(``list_device_paths`` / ``open`` / ``close`` / ``ioctl``) over in-memory
fake devices, emulating the kernel's V4L2 enumeration contract (EINVAL ends
each enumeration loop).
"""
import errno

from camera_discovery import v4l2


def fourcc(code: str) -> int:
    """Encode a fourcc string (e.g. ``"YUYV"``) as its V4L2 integer."""
    padded = code.ljust(4)
    value = 0
    for shift, char in zip((0, 8, 16, 24), padded):
        value |= ord(char) << shift
    return value


def _einval():
    return OSError(errno.EINVAL, "Invalid argument")


class FakeDevice:
    """One emulated ``/dev/video*`` node.

    ``formats`` is ``[(fourcc_str, [(w, h), ...]), ...]``. Capability words
    default to a plain capture device reporting per-node ``device_caps``.
    """

    def __init__(
        self,
        driver="uvcvideo",
        card="Fake Camera",
        bus_info="usb-0000:00:14.0-1",
        capabilities=None,
        device_caps=v4l2.V4L2_CAP_VIDEO_CAPTURE,
        formats=(("YUYV", ((1920, 1080), (1280, 720))),),
        open_error=None,
        querycap_error=None,
        enum_fmt_error=None,
    ):
        if capabilities is None:
            capabilities = v4l2.V4L2_CAP_DEVICE_CAPS | device_caps
        self.driver = driver
        self.card = card
        self.bus_info = bus_info
        self.capabilities = capabilities
        self.device_caps = device_caps
        self.formats = [
            (code, [list(res) for res in resolutions])
            for code, resolutions in formats
        ]
        self.open_error = open_error
        self.querycap_error = querycap_error
        self.enum_fmt_error = enum_fmt_error


class FakeV4l2Io:
    """Drop-in fake for ``V4l2Io`` over a ``{path: FakeDevice}`` map."""

    def __init__(self, devices, list_error=None):
        self.devices = dict(devices)
        self.list_error = list_error
        self.open_fds = {}
        self._next_fd = 100

    def list_device_paths(self):
        if self.list_error is not None:
            raise self.list_error
        return sorted(self.devices)

    def open(self, device_path):
        device = self.devices[device_path]
        if device.open_error is not None:
            raise device.open_error
        fd = self._next_fd
        self._next_fd += 1
        self.open_fds[fd] = device
        return fd

    def close(self, fd):
        del self.open_fds[fd]

    def ioctl(self, fd, request, buffer):
        device = self.open_fds[fd]
        if request == v4l2.VIDIOC_QUERYCAP:
            self._querycap(device, buffer)
        elif request == v4l2.VIDIOC_ENUM_FMT:
            self._enum_fmt(device, buffer)
        elif request == v4l2.VIDIOC_ENUM_FRAMESIZES:
            self._enum_framesizes(device, buffer)
        else:
            raise _einval()

    def _querycap(self, device, caps):
        if device.querycap_error is not None:
            raise device.querycap_error
        caps.driver = device.driver.encode("utf-8")
        caps.card = device.card.encode("utf-8")
        caps.bus_info = device.bus_info.encode("utf-8")
        caps.capabilities = device.capabilities
        caps.device_caps = device.device_caps

    def _enum_fmt(self, device, desc):
        if device.enum_fmt_error is not None:
            raise device.enum_fmt_error
        if desc.type != v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE:
            raise _einval()
        if desc.index >= len(device.formats):
            raise _einval()
        code, _ = device.formats[desc.index]
        desc.pixelformat = fourcc(code)
        desc.description = code.encode("utf-8")

    def _enum_framesizes(self, device, frmsize):
        for code, resolutions in device.formats:
            if fourcc(code) == frmsize.pixel_format:
                if frmsize.index >= len(resolutions):
                    raise _einval()
                width, height = resolutions[frmsize.index]
                frmsize.type = v4l2.V4L2_FRMSIZE_TYPE_DISCRETE
                frmsize.discrete.width = width
                frmsize.discrete.height = height
                return
        raise _einval()
