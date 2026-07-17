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
"""Property test for the Aravis branch of the ``build_inventory`` merge.

**Feature: aravis-camera-input, Property 4: Configured/discovered Aravis
merge by camera id**

*For any* combination of configured Image_Sources and discovered Aravis
cameras, each ``Camera``-type Image_Source whose ``cameraId`` equals a
discovered camera's id SHALL yield exactly one inventory entry under the
configured identifier combining configured parameters with discovered
identity metadata, and no discovered Aravis camera SHALL contribute to
more than one entry.

**Validates: Requirements 2.4**

Generators mirror the real input space (same conventions as the sibling
V4L2 merge property in test_property_configured_discovered_merge.py):
Image_Source ids are unique (SQLite primary keys) and each source
references at most one camera id; the Aravis bus reports at most one
camera per runtime id, with identities unique by the stable id they
derive. Camera ids are drawn from a small shared pool so configured /
discovered cameraId overlap occurs frequently.

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
from hypothesis import given
from hypothesis import strategies as st

from camera_discovery import DiscoveredAravisCamera, DiscoveryResult, aravis_stable_id
from camera_sync import (
    ORIGIN_EDGE_CONFIGURED,
    ORIGIN_EDGE_DISCOVERED,
    TYPE_ARAVIS_DISCOVERED,
    build_inventory,
)

# --- generators --------------------------------------------------------------

# Small shared camera-id pool so configured and discovered ids collide often.
_CAMERA_IDS = st.integers(min_value=0, max_value=9).map(
    lambda n: "cam-{}".format(n)
)

# Full-unicode identity strings: real GenICam vendors/models are not
# ASCII-only (same alphabet as the discovery-side Aravis property tests).
_NAMES = st.text(min_size=1, max_size=24)
_SERIALS = st.text(max_size=24)  # may be empty (serial-less cameras)
_ADDRESSES = st.text(max_size=32)
_PROTOCOLS = st.sampled_from(["GigEVision", "USB3Vision", "Fake"])

_SOURCE_NAMES = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=31,
)


def _stable_id_key(identity):
    """Uniqueness key: the stable id an identity tuple derives."""
    vendor, model, serial, physical_id = identity
    return aravis_stable_id(vendor, model, serial, physical_id)


@st.composite
def _discovered_aravis_cameras(draw):
    """Discovered Aravis cameras with unique runtime camera ids (drawn
    from the shared pool) and identities unique by derived stable id."""
    camera_ids = draw(st.lists(_CAMERA_IDS, unique=True, max_size=5))
    identities = draw(
        st.lists(
            st.tuples(_NAMES, _NAMES, _SERIALS, _NAMES),
            min_size=len(camera_ids),
            max_size=len(camera_ids),
            unique_by=_stable_id_key,
        )
    )
    return [
        DiscoveredAravisCamera(
            stable_id=aravis_stable_id(vendor, model, serial, physical_id),
            camera_id=camera_id,
            model=model,
            address=draw(_ADDRESSES),
            physical_id=physical_id,
            protocol=draw(_PROTOCOLS),
            serial=serial,
            vendor=vendor,
        )
        for camera_id, (vendor, model, serial, physical_id) in zip(
            camera_ids, identities
        )
    ]


@st.composite
def _image_sources(draw):
    """Configured Image_Sources with unique ids, each referencing one
    camera id (unique among the sources, drawn from the shared pool).
    Types mix ``Camera`` (Aravis-backed, merge-eligible) with other
    source types that must never merge by camera id."""
    camera_ids = draw(st.lists(_CAMERA_IDS, unique=True, max_size=4))
    sources = []
    for index, camera_id in enumerate(camera_ids):
        configuration = {}
        if draw(st.booleans()):
            configuration["gain"] = draw(st.integers(min_value=0, max_value=100))
        if draw(st.booleans()):
            configuration["exposure"] = draw(
                st.integers(min_value=1, max_value=10_000_000)
            )
        sources.append(
            {
                "imageSourceId": "is-{}".format(index),
                "name": draw(_SOURCE_NAMES),
                "type": draw(st.sampled_from(["Camera", "Folder"])),
                "cameraId": camera_id,
                "imageSourceConfiguration": configuration,
            }
        )
    return sources


def _identity(camera):
    """The discovered identity metadata ``build_inventory`` reports under
    ``capabilities.aravis``."""
    return {
        "model": camera.model,
        "address": camera.address,
        "physicalId": camera.physical_id,
        "protocol": camera.protocol,
        "serial": camera.serial,
        "vendor": camera.vendor,
    }


# --- property ----------------------------------------------------------------


@given(sources=_image_sources(), cameras=_discovered_aravis_cameras())
def test_configured_discovered_aravis_merge_by_camera_id(sources, cameras):
    """**Feature: aravis-camera-input, Property 4: Configured/discovered
    Aravis merge by camera id**

    **Validates: Requirements 2.4**
    """
    entries = build_inventory(sources, DiscoveryResult(cameras=cameras))

    cameras_by_id = {camera.camera_id: camera for camera in cameras}
    matched_camera_ids = {
        source["cameraId"]
        for source in sources
        if source["type"] == "Camera"
    } & set(cameras_by_id)

    # One entry per merged configured/discovered pair, plus one per
    # unmatched member of either set.
    assert len(entries) == len(sources) + len(cameras) - len(matched_camera_ids)

    by_id = {entry.camera_source_id: entry for entry in entries}
    assert len(by_id) == len(entries)  # ids are unique

    # Every configured Image_Source reports under its cfg- id carrying its
    # configured parameters.
    for source in sources:
        entry = by_id["cfg-" + source["imageSourceId"]]
        configuration = source["imageSourceConfiguration"]

        assert entry.origin == ORIGIN_EDGE_CONFIGURED
        assert entry.name == source["name"]
        assert entry.type == source["type"]
        assert entry.params.get("cameraId") == source["cameraId"]
        if "gain" in configuration:
            assert entry.params["gain"] == configuration["gain"]
        if "exposure" in configuration:
            assert entry.params["exposure"] == configuration["exposure"]

        if source["type"] == "Camera" and source["cameraId"] in cameras_by_id:
            # cameraId match: exactly ONE entry under the configured
            # identifier, combining configured parameters with the
            # discovered identity metadata (2.4).
            camera = cameras_by_id[source["cameraId"]]
            assert entry.discovered is True
            assert entry.capabilities == {"aravis": _identity(camera)}
            assert entry.absent is False
            assert entry.absent_since is None
        else:
            # Non-matching (or non-Camera-type) configured source reports
            # undiscovered, with no discovered metadata attached.
            assert entry.discovered is False
            assert entry.capabilities == {}

    # No discovered Aravis camera contributes to more than one entry: a
    # merged camera never also reports under its stable id, an unmerged
    # camera reports exactly once as a discovered-only entry, and each
    # camera's identity metadata appears in exactly one entry.
    for camera in cameras:
        if camera.camera_id in matched_camera_ids:
            assert camera.stable_id not in by_id
        else:
            entry = by_id[camera.stable_id]
            assert entry.type == TYPE_ARAVIS_DISCOVERED
            assert entry.origin == ORIGIN_EDGE_DISCOVERED
            assert entry.name == "{} {}".format(camera.vendor, camera.model)
            assert entry.discovered is True
            assert entry.params == {
                "cameraId": camera.camera_id,
                "serial": camera.serial,
                "protocol": camera.protocol,
                "address": camera.address,
            }
            assert entry.capabilities == {"aravis": _identity(camera)}

        contributions = [
            entry
            for entry in entries
            if entry.capabilities.get("aravis") == _identity(camera)
        ]
        assert len(contributions) == 1
