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
"""Property test for Camera_Discovery enumeration completeness.

**Feature: camera-registry-sync, Property 2: Discovery enumeration completeness**

*For any* set of fake capture devices presented to the enumeration layer,
every device carrying the video-capture capability appears in the discovery
result exactly once with its device path, card name, and format/resolution
metadata propagated, and every resulting inventory entry not matching a
configured Image_Source has origin ``edge-discovered`` (at the enumeration
layer every entry is discovery-originated by construction — the ``disc-``
stable-id namespace that ``build_inventory`` maps to origin
``edge-discovered``).

**Validates: Requirements 2.1, 2.2**

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
from hypothesis import given
from hypothesis import strategies as st

from fake_v4l2 import FakeDevice, FakeV4l2Io

from camera_discovery import CameraDiscovery, v4l2
from camera_discovery.discovery import make_stable_id

# --- generators --------------------------------------------------------------

# Printable ASCII without NUL: survives the ctypes c_char round trip exactly.
_TEXT_ALPHABET = st.characters(min_codepoint=32, max_codepoint=126)

# Driver names must fit c_char[16] (<= 15 bytes); mix of Tegra CSI drivers
# and ordinary V4L2 drivers so both kinds are generated.
_DRIVERS = st.sampled_from(
    ["uvcvideo", "em28xx", "v4l2loopback", "tegra-video", "tegra_camera"]
)

# card is c_char[32], bus_info is c_char[32] -> <= 31 bytes.
_CARDS = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=31)
_BUS_INFOS = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=31)

# fourcc codes: exactly four non-space characters so encode/decode is exact;
# unique per device (matching real devices, and the fake resolves framesize
# enumeration by fourcc).
_FOURCCS = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=4,
    max_size=4,
)

_RESOLUTIONS = st.lists(
    st.tuples(
        st.integers(min_value=1, max_value=8192),
        st.integers(min_value=1, max_value=8192),
    ),
    max_size=4,
)

_FORMATS = st.lists(
    st.tuples(_FOURCCS, _RESOLUTIONS),
    max_size=4,
    unique_by=lambda entry: entry[0],
)


@st.composite
def _fake_devices(draw):
    """A ``{device_path: FakeDevice}`` map mixing capture-capable nodes with
    non-capture nodes (metadata/encoder nodes), across both V4L2 capability
    reporting modes (per-node ``device_caps`` and legacy global caps)."""
    is_capture = draw(st.booleans())
    reports_device_caps = draw(st.booleans())

    if reports_device_caps:
        # Per-node caps mode: the global word may advertise capture for the
        # whole hardware while this node itself does not (metadata nodes).
        device_caps = v4l2.V4L2_CAP_VIDEO_CAPTURE if is_capture else 0
        capabilities = (
            v4l2.V4L2_CAP_DEVICE_CAPS | v4l2.V4L2_CAP_VIDEO_CAPTURE
        )
    else:
        device_caps = 0
        capabilities = v4l2.V4L2_CAP_VIDEO_CAPTURE if is_capture else 0

    return FakeDevice(
        driver=draw(_DRIVERS),
        card=draw(_CARDS),
        bus_info=draw(_BUS_INFOS),
        capabilities=capabilities,
        device_caps=device_caps,
        formats=draw(_FORMATS),
    )


_DEVICE_MAPS = st.dictionaries(
    keys=st.integers(min_value=0, max_value=99).map(
        lambda n: "/dev/video{}".format(n)
    ),
    values=_fake_devices(),
    max_size=6,
)


def _has_capture_capability(device):
    """The V4L2 contract the enumerator must honor: ``device_caps`` when the
    driver reports per-node capabilities, else the global capability word."""
    if device.capabilities & v4l2.V4L2_CAP_DEVICE_CAPS:
        effective = device.device_caps
    else:
        effective = device.capabilities
    return bool(effective & v4l2.V4L2_CAP_VIDEO_CAPTURE)


def _expected_formats(device):
    return [
        {
            "pixel_format": code,
            "resolutions": [[width, height] for width, height in resolutions],
        }
        for code, resolutions in device.formats
    ]


# --- property ----------------------------------------------------------------


@given(devices=_DEVICE_MAPS)
def test_discovery_enumeration_completeness(devices):
    """**Feature: camera-registry-sync, Property 2: Discovery enumeration
    completeness**

    **Validates: Requirements 2.1, 2.2**
    """
    io = FakeV4l2Io(devices)
    result = CameraDiscovery(v4l2_io=io).enumerate()

    # No device is configured to fail, so nothing may be reported as failed.
    assert result.failures == []

    # Exactly once: no device path appears twice in the result.
    by_path = {}
    for camera in result.cameras:
        assert camera.device_path not in by_path
        by_path[camera.device_path] = camera

    # Completeness and soundness: the enumerated set is exactly the set of
    # devices carrying the video-capture capability (2.1).
    capture_paths = {
        path for path, device in devices.items()
        if _has_capture_capability(device)
    }
    assert set(by_path) == capture_paths

    for path in capture_paths:
        device = devices[path]
        camera = by_path[path]

        # Metadata propagation (2.2): device path, card name, and
        # format/resolution metadata reported by the device.
        assert camera.device_path == path
        assert camera.card_name == device.card
        assert camera.bus_info == device.bus_info
        assert camera.driver == device.driver
        assert camera.formats == _expected_formats(device)

        # Discovery-originated identity: every enumeration entry lives in
        # the disc- stable-id namespace that build_inventory reports as
        # origin edge-discovered (2.1).
        assert camera.stable_id == make_stable_id(device.bus_info, device.card)
        assert camera.stable_id.startswith("disc-")
