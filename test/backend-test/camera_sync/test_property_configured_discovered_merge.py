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
"""Property test for the ``build_inventory`` configured/discovered merge.

**Feature: camera-registry-sync, Property 5: Configured/discovered merge**

*For any* set of configured Image_Sources and any set of discovered cameras,
``build_inventory`` emits exactly one entry per path-matching
configured/discovered pair — carrying the configured id and parameters
combined with the discovered capability metadata — plus one entry per
unmatched member of either set, and no device path appears in two entries.

**Validates: Requirements 2.5**

Generators mirror the real input space: Image_Source ids are unique (SQLite
primary keys) and each source references at most one device path;
enumeration reports at most one camera per device path with a unique
``disc-`` stable id. Device paths are drawn from a small shared pool so
configured/discovered path matches occur frequently.

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
from hypothesis import given
from hypothesis import strategies as st

from camera_discovery import DiscoveredCamera, DiscoveryResult
from camera_sync import (
    ORIGIN_EDGE_CONFIGURED,
    ORIGIN_EDGE_DISCOVERED,
    TYPE_V4L2_DISCOVERED,
    build_inventory,
)

# --- generators --------------------------------------------------------------

# Small shared path pool so configured and discovered paths collide often.
_DEVICE_PATHS = st.integers(min_value=0, max_value=9).map(
    lambda n: "/dev/video{}".format(n)
)

_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=31,
)

_RESOLUTIONS = st.lists(
    st.tuples(
        st.integers(min_value=1, max_value=8192),
        st.integers(min_value=1, max_value=8192),
    ).map(list),
    max_size=3,
)

_FORMATS = st.lists(
    st.fixed_dictionaries(
        {
            "pixel_format": st.text(
                alphabet=st.characters(min_codepoint=33, max_codepoint=126),
                min_size=4,
                max_size=4,
            ),
            "resolutions": _RESOLUTIONS,
        }
    ),
    max_size=3,
)


@st.composite
def _discovered_cameras(draw):
    """Discovered cameras with unique stable ids and unique device paths,
    as the enumeration layer guarantees (one camera per /dev/video node)."""
    paths = draw(st.lists(_DEVICE_PATHS, unique=True, max_size=5))
    return [
        DiscoveredCamera(
            stable_id="disc-{:012d}".format(index),
            device_path=path,
            card_name=draw(_TEXT),
            bus_info=draw(_TEXT),
            driver=draw(st.sampled_from(["uvcvideo", "tegra-video"])),
            kind=draw(st.sampled_from(["v4l2", "csi"])),
            formats=draw(_FORMATS),
        )
        for index, path in enumerate(paths)
    ]


@st.composite
def _image_sources(draw):
    """Configured Image_Sources with unique ids; each references at most one
    device path (unique among the sources), some reference none at all."""
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


def _expected_capabilities(camera):
    """The reported-document capability shape ``build_inventory`` emits."""
    return {
        "formats": [
            {
                "pixelFormat": fmt["pixel_format"],
                "resolutions": [list(r) for r in fmt["resolutions"]],
            }
            for fmt in camera.formats
        ],
        "driver": camera.driver,
        "busInfo": camera.bus_info,
        "kind": camera.kind,
    }


# --- property ----------------------------------------------------------------


@given(sources=_image_sources(), cameras=_discovered_cameras())
def test_configured_discovered_merge(sources, cameras):
    """**Feature: camera-registry-sync, Property 5: Configured/discovered
    merge**

    **Validates: Requirements 2.5**
    """
    entries = build_inventory(sources, DiscoveryResult(cameras=cameras))

    cameras_by_path = {camera.device_path: camera for camera in cameras}
    configured_paths = {
        source["imageSourceConfiguration"]["device"]
        for source in sources
        if "device" in source["imageSourceConfiguration"]
    }
    matched_paths = configured_paths & set(cameras_by_path)

    # Exactly one entry per path-matching pair plus one per unmatched member
    # of either set: every source yields an entry, every camera yields an
    # entry unless merged into a configured one.
    assert len(entries) == len(sources) + len(cameras) - len(matched_paths)

    by_id = {entry.camera_source_id: entry for entry in entries}
    assert len(by_id) == len(entries)  # ids are unique

    # No device path appears in two entries.
    reported_paths = [
        entry.params["devicePath"]
        for entry in entries
        if "devicePath" in entry.params
    ]
    assert len(reported_paths) == len(set(reported_paths))

    # Every configured Image_Source reports under its cfg- id with the
    # configured parameters (2.5).
    for source in sources:
        entry = by_id["cfg-" + source["imageSourceId"]]
        configuration = source["imageSourceConfiguration"]
        device = configuration.get("device")

        assert entry.origin == ORIGIN_EDGE_CONFIGURED
        assert entry.name == source["name"]
        assert entry.type == source["type"]
        assert entry.params.get("cameraId") == source["cameraId"]
        assert entry.params.get("devicePath") == device
        if "gain" in configuration:
            assert entry.params["gain"] == configuration["gain"]
        if "exposure" in configuration:
            assert entry.params["exposure"] == configuration["exposure"]

        if device in cameras_by_path:
            # Path match: the ONE merged entry combines the configured
            # parameters with the discovered capability metadata.
            assert entry.discovered is True
            assert entry.capabilities == _expected_capabilities(
                cameras_by_path[device]
            )
        else:
            # Unmatched configured source reports undiscovered.
            assert entry.discovered is False
            assert entry.capabilities == {}

    # Every unmatched discovered camera reports separately under its stable
    # id with origin edge-discovered; matched cameras never appear twice.
    for camera in cameras:
        if camera.device_path in matched_paths:
            assert camera.stable_id not in by_id
        else:
            entry = by_id[camera.stable_id]
            assert entry.origin == ORIGIN_EDGE_DISCOVERED
            assert entry.type == TYPE_V4L2_DISCOVERED
            assert entry.name == camera.card_name
            assert entry.discovered is True
            assert entry.params == {"devicePath": camera.device_path}
            assert entry.capabilities == _expected_capabilities(camera)
