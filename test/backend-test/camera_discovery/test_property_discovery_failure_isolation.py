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
"""Property test for Camera_Discovery failure isolation.

**Feature: camera-registry-sync, Property 3: Discovery failure isolation**

*For any* set of fake capture devices and any subset chosen to fail
enumeration, the discovery result contains every non-failing device, one
failure record per failing device, and no exception escapes the
enumeration call.

**Validates: Requirements 2.6**

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import errno

from hypothesis import given
from hypothesis import strategies as st

from fake_v4l2 import FakeDevice, FakeV4l2Io

from camera_discovery import CameraDiscovery, v4l2

# --- generators --------------------------------------------------------------

# Printable ASCII without NUL: survives the ctypes c_char round trip exactly
# (card is c_char[32], bus_info is c_char[32] -> <= 31 bytes).
_TEXT_ALPHABET = st.characters(min_codepoint=32, max_codepoint=126)
_CARDS = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=31)
_BUS_INFOS = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=31)

# Exceptions a failing node may raise. Injected at open or QUERYCAP these
# may be any exception type; injected at ENUM_FMT they must be non-OSError,
# because an OSError (EINVAL) from ENUM_FMT is the kernel's normal
# end-of-enumeration signal, not a failure.
_ANY_ERRORS = st.sampled_from(
    [
        OSError(errno.EBUSY, "Device or resource busy"),
        OSError(errno.EIO, "Input/output error"),
        PermissionError(errno.EACCES, "Permission denied"),
        RuntimeError("driver wedged"),
    ]
)
_NON_OSERROR_ERRORS = st.sampled_from(
    [RuntimeError("driver wedged"), ValueError("corrupt descriptor")]
)

_FAILURE_INJECTIONS = st.one_of(
    st.builds(lambda e: {"open_error": e}, _ANY_ERRORS),
    st.builds(lambda e: {"querycap_error": e}, _ANY_ERRORS),
    st.builds(lambda e: {"enum_fmt_error": e}, _NON_OSERROR_ERRORS),
)


@st.composite
def _device_specs(draw):
    """One ``(FakeDevice, fails)`` pair.

    Healthy devices mix capture-capable and non-capture nodes (metadata /
    encoder nodes are skipped, not failed). Failing devices are always
    capture-capable so every configured failure point is actually reached
    during enumeration (an ENUM_FMT fault on a non-capture node would be
    skipped before the fault fires).
    """
    fails = draw(st.booleans())
    injection = draw(_FAILURE_INJECTIONS) if fails else {}
    is_capture = True if fails else draw(st.booleans())
    device = FakeDevice(
        card=draw(_CARDS),
        bus_info=draw(_BUS_INFOS),
        device_caps=v4l2.V4L2_CAP_VIDEO_CAPTURE if is_capture else 0,
        **injection,
    )
    return device, fails


_DEVICE_MAPS = st.dictionaries(
    keys=st.integers(min_value=0, max_value=99).map(
        lambda n: "/dev/video{}".format(n)
    ),
    values=_device_specs(),
    max_size=8,
)


def _is_capture(device):
    return bool(device.device_caps & v4l2.V4L2_CAP_VIDEO_CAPTURE)


# --- property ----------------------------------------------------------------


@given(specs=_DEVICE_MAPS)
def test_discovery_failure_isolation(specs):
    """**Feature: camera-registry-sync, Property 3: Discovery failure
    isolation**

    **Validates: Requirements 2.6**
    """
    devices = {path: device for path, (device, _) in specs.items()}
    failing_paths = {path for path, (_, fails) in specs.items() if fails}
    healthy_capture_paths = {
        path
        for path, (device, fails) in specs.items()
        if not fails and _is_capture(device)
    }

    # No exception escapes the enumeration call (2.6): any raise here fails
    # the property directly.
    result = CameraDiscovery(v4l2_io=FakeV4l2Io(devices)).enumerate()

    # Every non-failing capture device is in the result, exactly once, and
    # nothing else is (failing nodes and non-capture nodes are not cameras).
    camera_paths = [camera.device_path for camera in result.cameras]
    assert len(camera_paths) == len(set(camera_paths))
    assert set(camera_paths) == healthy_capture_paths

    # One failure record per failing device, carrying its device path and a
    # non-empty error description; no healthy device is reported failed.
    failure_paths = [failure["device_path"] for failure in result.failures]
    assert len(failure_paths) == len(set(failure_paths))
    assert set(failure_paths) == failing_paths
    for failure in result.failures:
        assert failure["error"]
