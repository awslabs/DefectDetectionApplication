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
"""Unit tests for the Camera_Discovery V4L2/CSI enumeration core.

Feature: camera-registry-sync (Requirements 2.1, 2.2, 2.6, 11.2).
"""
import hashlib

from fake_v4l2 import FakeDevice, FakeV4l2Io

from camera_discovery import CameraDiscovery, DiscoveredCamera
from camera_discovery import v4l2
from camera_discovery.discovery import make_stable_id


class TestEnumerationBasics:
    def test_capture_device_fields_captured(self):
        # Requirement 2.2: device path, name, and capability metadata
        io = FakeV4l2Io(
            {
                "/dev/video0": FakeDevice(
                    driver="uvcvideo",
                    card="Logitech C920",
                    bus_info="usb-0000:00:14.0-2",
                    formats=(
                        ("YUYV", ((1920, 1080), (1280, 720))),
                        ("MJPG", ((3840, 2160),)),
                    ),
                )
            }
        )
        result = CameraDiscovery(v4l2_io=io).enumerate()

        assert result.failures == []
        (camera,) = result.cameras
        assert camera.device_path == "/dev/video0"
        assert camera.card_name == "Logitech C920"
        assert camera.bus_info == "usb-0000:00:14.0-2"
        assert camera.driver == "uvcvideo"
        assert camera.kind == "v4l2"
        assert camera.formats == [
            {"pixel_format": "YUYV", "resolutions": [[1920, 1080], [1280, 720]]},
            {"pixel_format": "MJPG", "resolutions": [[3840, 2160]]},
        ]

    def test_stable_id_derivation(self):
        io = FakeV4l2Io(
            {
                "/dev/video0": FakeDevice(
                    card="Cam A", bus_info="usb-0000:00:14.0-1"
                )
            }
        )
        (camera,) = CameraDiscovery(v4l2_io=io).enumerate().cameras

        expected = (
            "disc-"
            + hashlib.sha1("usb-0000:00:14.0-1Cam A".encode()).hexdigest()[:12]
        )
        assert camera.stable_id == expected
        assert camera.stable_id == make_stable_id("usb-0000:00:14.0-1", "Cam A")

    def test_stable_id_independent_of_device_path(self):
        # Stable across /dev/videoN renumbering
        device = dict(card="Cam A", bus_info="usb-0000:00:14.0-1")
        io0 = FakeV4l2Io({"/dev/video0": FakeDevice(**device)})
        io5 = FakeV4l2Io({"/dev/video5": FakeDevice(**device)})

        (cam0,) = CameraDiscovery(v4l2_io=io0).enumerate().cameras
        (cam5,) = CameraDiscovery(v4l2_io=io5).enumerate().cameras
        assert cam0.stable_id == cam5.stable_id

    def test_discovered_camera_is_frozen(self):
        io = FakeV4l2Io({"/dev/video0": FakeDevice()})
        (camera,) = CameraDiscovery(v4l2_io=io).enumerate().cameras
        try:
            camera.device_path = "/dev/video9"
            raised = False
        except AttributeError:
            raised = True
        assert raised
        assert isinstance(camera, DiscoveredCamera)


class TestCaptureCapabilityFilter:
    def test_non_capture_node_skipped(self):
        # Metadata node: global caps advertise capture, per-node caps do not
        io = FakeV4l2Io(
            {
                "/dev/video0": FakeDevice(device_caps=v4l2.V4L2_CAP_VIDEO_CAPTURE),
                "/dev/video1": FakeDevice(
                    capabilities=(
                        v4l2.V4L2_CAP_DEVICE_CAPS | v4l2.V4L2_CAP_VIDEO_CAPTURE
                    ),
                    device_caps=0,  # e.g. V4L2_CAP_META_CAPTURE only
                ),
            }
        )
        result = CameraDiscovery(v4l2_io=io).enumerate()

        assert result.failures == []
        assert [c.device_path for c in result.cameras] == ["/dev/video0"]

    def test_global_caps_used_without_device_caps_flag(self):
        io = FakeV4l2Io(
            {
                "/dev/video0": FakeDevice(
                    capabilities=v4l2.V4L2_CAP_VIDEO_CAPTURE, device_caps=0
                )
            }
        )
        result = CameraDiscovery(v4l2_io=io).enumerate()
        assert [c.device_path for c in result.cameras] == ["/dev/video0"]


class TestCsiClassification:
    def test_tegra_driver_classified_csi(self):
        io = FakeV4l2Io(
            {
                "/dev/video0": FakeDevice(
                    driver="tegra-video",
                    card="vi-output, imx219 10-0010",
                    bus_info="platform:tegra-capture-vi:2",
                )
            }
        )
        (camera,) = CameraDiscovery(v4l2_io=io).enumerate().cameras
        assert camera.kind == "csi"

    def test_non_tegra_driver_classified_v4l2(self):
        io = FakeV4l2Io({"/dev/video0": FakeDevice(driver="uvcvideo")})
        (camera,) = CameraDiscovery(v4l2_io=io).enumerate().cameras
        assert camera.kind == "v4l2"


class TestFailureIsolation:
    def test_per_node_failure_recorded_and_enumeration_continues(self):
        # Requirement 2.6
        io = FakeV4l2Io(
            {
                "/dev/video0": FakeDevice(card="Good A"),
                "/dev/video1": FakeDevice(
                    querycap_error=OSError(5, "ioctl EIO")
                ),
                "/dev/video2": FakeDevice(card="Good B"),
            }
        )
        result = CameraDiscovery(v4l2_io=io).enumerate()

        assert [c.card_name for c in result.cameras] == ["Good A", "Good B"]
        (failure,) = result.failures
        assert failure["device_path"] == "/dev/video1"
        assert "EIO" in failure["error"] or "ioctl" in failure["error"]

    def test_open_failure_recorded(self):
        io = FakeV4l2Io(
            {
                "/dev/video0": FakeDevice(
                    open_error=PermissionError(13, "Permission denied")
                ),
                "/dev/video1": FakeDevice(card="Good"),
            }
        )
        result = CameraDiscovery(v4l2_io=io).enumerate()

        assert [c.card_name for c in result.cameras] == ["Good"]
        assert result.failures[0]["device_path"] == "/dev/video0"

    def test_fds_closed_even_on_format_enumeration_failure(self):
        io = FakeV4l2Io(
            {
                "/dev/video0": FakeDevice(
                    enum_fmt_error=RuntimeError("driver wedged")
                ),
                "/dev/video1": FakeDevice(card="Good"),
            }
        )
        result = CameraDiscovery(v4l2_io=io).enumerate()

        assert io.open_fds == {}  # every opened fd released
        assert [c.card_name for c in result.cameras] == ["Good"]
        assert result.failures[0]["device_path"] == "/dev/video0"

    def test_total_failure_yields_empty_result_not_exception(self):
        # Requirement 11.2: never an exception into LocalServer startup
        io = FakeV4l2Io({}, list_error=RuntimeError("glob exploded"))
        result = CameraDiscovery(v4l2_io=io).enumerate()

        assert result.cameras == []
        assert result.failures == []

    def test_no_devices_yields_empty_result(self):
        result = CameraDiscovery(v4l2_io=FakeV4l2Io({})).enumerate()
        assert result.cameras == []
        assert result.failures == []
