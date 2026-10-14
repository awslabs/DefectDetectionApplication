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
"""Property test for Aravis failure isolation and no-Aravis identity.

**Feature: aravis-camera-input, Property 6: Aravis failure isolation and
no-Aravis identity**

*For any* configured and V4L2-discovered inventory, building the inventory
with a failing or unavailable Aravis enumerator SHALL produce entries
identical to the pre-feature output for the same inputs, record the
failure, and raise no exception.

**Validates: Requirements 2.6, 7.2**

The failing pass runs the REAL discovery layer (``CameraDiscovery`` over
the fake V4L2 ioctl layer with a raising Aravis enumerator) into the REAL
``build_inventory``. The pre-feature oracle is ``build_inventory`` over a
snapshot produced from the identical V4L2 devices with an Aravis
enumerator returning zero cameras — by Requirement 7.2 (pinned verbatim by
test_build_inventory_aravis.py's no-Aravis identity test) that output is
the pre-feature output for the same configured + V4L2 inputs.

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import sys
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from camera_discovery import CameraDiscovery
from camera_sync import build_inventory

# The fake V4L2 ioctl layer lives beside the camera_discovery suites
# (test/backend-test/camera_discovery is not a package, so pytest's
# rootdir-based sys.path insertion does not cover it from here).
_FAKES_DIR = str(Path(__file__).resolve().parent.parent / "camera_discovery")
if _FAKES_DIR not in sys.path:
    sys.path.insert(0, _FAKES_DIR)

from fake_v4l2 import FakeDevice, FakeV4l2Io  # noqa: E402

# --- generators --------------------------------------------------------------

# Small shared path pool so configured and discovered paths collide often
# (same convention as test_property_configured_discovered_merge.py).
_DEVICE_PATHS = st.integers(min_value=0, max_value=9).map(
    lambda n: "/dev/video{}".format(n)
)

# ASCII identity strings: the V4L2 capability struct fields are fixed-size
# 32-byte buffers.
_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=31,
)

_FOURCCS = st.sampled_from(["YUYV", "MJPG", "NV12", "RG10"])

_RESOLUTIONS = st.lists(
    st.tuples(
        st.integers(min_value=1, max_value=8192),
        st.integers(min_value=1, max_value=8192),
    ),
    min_size=1,
    max_size=3,
    unique=True,
)


@st.composite
def _fake_devices(draw):
    """A ``{path: FakeDevice}`` map of capture nodes with unique device
    paths and unique bus infos (hence unique discovery stable ids)."""
    paths = draw(st.lists(_DEVICE_PATHS, unique=True, max_size=4))
    devices = {}
    for index, path in enumerate(paths):
        formats = draw(
            st.lists(
                st.tuples(_FOURCCS, _RESOLUTIONS),
                max_size=2,
                unique_by=lambda fmt: fmt[0],
            )
        )
        devices[path] = FakeDevice(
            driver=draw(st.sampled_from(["uvcvideo", "tegra-video"])),
            card=draw(_TEXT),
            bus_info="usb-0000:00:14.0-{}".format(index),
            formats=formats,
        )
    return devices


@st.composite
def _image_sources(draw):
    """Configured Image_Sources with unique ids; each references at most
    one device path (unique among the sources), some reference none."""
    device_paths = draw(st.lists(_DEVICE_PATHS, unique=True, max_size=4))
    pathless_count = draw(st.integers(min_value=0, max_value=2))
    devices = list(device_paths) + [None] * pathless_count

    sources = []
    for index, device in enumerate(devices):
        configuration = {}
        if device is not None:
            configuration["device"] = device
        if draw(st.booleans()):
            configuration["gain"] = draw(st.integers(min_value=0, max_value=48))
        if draw(st.booleans()):
            configuration["exposure"] = draw(
                st.integers(min_value=1, max_value=10_000_000)
            )
        sources.append(
            {
                "imageSourceId": "is-{}".format(index),
                "name": draw(_TEXT),
                "type": draw(st.sampled_from(["Camera", "ICam", "NvidiaCSI"])),
                "cameraId": draw(_TEXT),
                "imageSourceConfiguration": configuration,
            }
        )
    return sources


# A raising enumerator (bus failure) and an unavailable runtime (the
# lazy-import failure mode surfaces as ImportError).
_ERRORS = st.sampled_from(
    [
        RuntimeError("aravis bus enumeration failed"),
        ImportError("No module named 'gi'"),
    ]
)


# --- property ----------------------------------------------------------------


@given(devices=_fake_devices(), sources=_image_sources(), error=_ERRORS)
def test_aravis_failure_isolation_and_no_aravis_identity(
    devices, sources, error
):
    """**Feature: aravis-camera-input, Property 6: Aravis failure isolation
    and no-Aravis identity**

    **Validates: Requirements 2.6, 7.2**
    """

    def raising_enumerator():
        raise error

    # Discovery pass with the failing/unavailable Aravis enumerator over
    # the configured + V4L2 inventory. Nothing may raise (2.6).
    failing_discovery = CameraDiscovery(
        v4l2_io=FakeV4l2Io(devices), aravis_enumerator=raising_enumerator
    )
    snapshot = failing_discovery.run_once()

    # Pre-feature oracle: the identical V4L2 devices with zero Aravis
    # cameras (Requirement 7.2 makes this the pre-feature output).
    baseline_discovery = CameraDiscovery(
        v4l2_io=FakeV4l2Io(devices), aravis_enumerator=lambda: []
    )
    baseline_snapshot = baseline_discovery.run_once()

    # The failure is recorded at the discovery layer (2.6); the clean pass
    # records none.
    assert {"error": str(error)} in list(snapshot.failures)
    assert baseline_snapshot.failures == ()

    # The tracked V4L2 inventory is untouched by the Aravis failure.
    assert snapshot.cameras == baseline_snapshot.cameras

    # The reported inventory is identical to the pre-feature output for
    # the same configured + V4L2 inputs (7.2).
    entries = build_inventory(sources, snapshot)
    assert entries == build_inventory(sources, baseline_snapshot)
