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
"""Property test for inventory presence tracking the pin state.

# Feature: cloud-static-camera-provisioning, Property 15: Inventory
# presence tracks the pin state

*For any* combination of configured Image_Sources, discovery snapshot, and
pinned flag, ``build_inventory`` includes exactly one
``static-image-camera`` entry when the flag is true — carrying origin
``edge-discovered`` and the fixed identity — and zero such entries when
false, with all other inventory entries identical to the pre-feature
merge; the flag reflects the store's pinned state regardless of whether
the pin was device- or cloud-initiated.

**Validates: Requirements 6.1, 6.2, 6.5, 7.6**

The pinned flag is derived from a REAL temp-dir ``StaticImageStore``
pinned through the Device_Pin_API primitive (device origin), through a
full ``StaticImagePinWorker`` cycle (cloud origin), or not at all — so the
"regardless of pin origin" half of the property is exercised end to end.

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import shutil
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from camera_discovery import DiscoveredCamera, DiscoveryResult
from camera_sync import (
    ORIGIN_EDGE_DISCOVERED,
    STATIC_IMAGE_CAMERA_NAME,
    TYPE_STATIC_IMAGE,
    build_inventory,
)
from utils.static_image_camera import (
    STATIC_IMAGE_CAMERA_ID,
    STATIC_IMAGE_CAMERA_IDENTITY,
    StaticImageStore,
)

from pin_worker_support import (
    FakeClock,
    FakeS3Client,
    FakeShadowAccessor,
    FakeSleep,
    TEST_BUCKET,
    fresh_store_dirs,
    image_specs,
    make_worker,
    pin_desired,
    render_image_bytes,
)

# --- generators (mirroring the camera-registry-sync merge suites) --------------

_DEVICE_PATHS = st.integers(min_value=0, max_value=9).map(
    lambda n: "/dev/video{}".format(n)
)

_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=15,
)

_FORMATS = st.lists(
    st.fixed_dictionaries(
        {
            "pixel_format": st.text(
                alphabet=st.characters(min_codepoint=33, max_codepoint=126),
                min_size=4,
                max_size=4,
            ),
            "resolutions": st.lists(
                st.tuples(
                    st.integers(min_value=1, max_value=8192),
                    st.integers(min_value=1, max_value=8192),
                ).map(list),
                max_size=2,
            ),
        }
    ),
    max_size=2,
)


@st.composite
def _discovered_cameras(draw):
    paths = draw(st.lists(_DEVICE_PATHS, unique=True, max_size=3))
    return [
        DiscoveredCamera(
            stable_id="disc-{:012d}".format(index),
            device_path=path,
            card_name=draw(_TEXT),
            bus_info=draw(_TEXT),
            driver="uvcvideo",
            kind="v4l2",
            formats=draw(_FORMATS),
        )
        for index, path in enumerate(paths)
    ]


@st.composite
def _image_sources(draw):
    device_paths = draw(st.lists(_DEVICE_PATHS, unique=True, max_size=2))
    pathless_count = draw(st.integers(min_value=0, max_value=1))
    devices = list(device_paths) + [None] * pathless_count
    sources = []
    for index, device in enumerate(devices):
        configuration = {}
        if device is not None:
            configuration["device"] = device
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


#: How the store reaches its pin state: no pin at all, a device-initiated
#: Device_Pin_API pin, or a cloud-initiated worker application (7.6).
_pin_origins = st.one_of(
    st.none(),
    st.tuples(st.sampled_from(["device", "cloud"]), image_specs),
)


def _pin_store(tmp_dir, origin):
    """A real store carried to the drawn pin state; returns the store."""
    store_dir, marker_path = fresh_store_dirs(tmp_dir)
    store = StaticImageStore(base_dir=store_dir)
    if origin is None:
        return store
    kind, spec = origin
    payload = render_image_bytes(*spec)
    if kind == "device":
        store.pin_bytes(payload, "device.img")
        return store
    desired = pin_desired("req-inv-1", payload)
    clock = FakeClock()
    worker = make_worker(
        store,
        FakeShadowAccessor(),
        FakeS3Client({(TEST_BUCKET, desired["key"]): payload}),
        clock,
        FakeSleep(clock),
        marker_path,
    )
    report = worker.process_one(desired)
    assert report["status"] == "applied"
    return store


@settings(deadline=None)
@given(
    sources=_image_sources(),
    cameras=_discovered_cameras(),
    origin=_pin_origins,
)
def test_inventory_presence_tracks_pin_state(sources, cameras, origin):
    """# Feature: cloud-static-camera-provisioning, Property 15: Inventory
    presence tracks the pin state

    **Validates: Requirements 6.1, 6.2, 6.5, 7.6**
    """
    tmp_dir = tempfile.mkdtemp(prefix="pin-inventory-")
    try:
        store = _pin_store(tmp_dir, origin)
        snapshot = DiscoveryResult(cameras=cameras)

        # The flag is the store's pinned state, regardless of pin origin
        # (7.6): true after a device- OR cloud-initiated pin, else false.
        pinned = store.is_pinned()
        assert pinned is (origin is not None)
        metadata = store.status()["metadata"]

        baseline = build_inventory(sources, snapshot)
        entries = build_inventory(
            sources,
            snapshot,
            static_image_pinned=pinned,
            static_image_metadata=metadata,
        )

        static_entries = [
            entry
            for entry in entries
            if entry.camera_source_id == STATIC_IMAGE_CAMERA_ID
        ]
        others = [
            entry
            for entry in entries
            if entry.camera_source_id != STATIC_IMAGE_CAMERA_ID
        ]

        # All other entries are identical to the pre-feature merge.
        assert others == baseline

        if not pinned:
            # Zero entries when unpinned: absent from the full report, so
            # the Portal's existing absence handling applies (6.2).
            assert static_entries == []
            return

        # Exactly one entry, fixed identity, discovery-managed origin
        # (6.1, 6.5).
        assert len(static_entries) == 1
        entry = static_entries[0]
        assert entry.camera_source_id == STATIC_IMAGE_CAMERA_ID
        assert entry.name == STATIC_IMAGE_CAMERA_NAME
        assert entry.type == TYPE_STATIC_IMAGE
        assert entry.origin == ORIGIN_EDGE_DISCOVERED
        assert entry.discovered is True
        assert entry.absent is False
        assert entry.params == {}

        static_image = entry.capabilities["staticImage"]
        assert static_image["id"] == STATIC_IMAGE_CAMERA_ID
        assert static_image["model"] == STATIC_IMAGE_CAMERA_IDENTITY["model"]
        assert static_image["address"] == STATIC_IMAGE_CAMERA_IDENTITY["address"]
        assert (
            static_image["physicalId"]
            == STATIC_IMAGE_CAMERA_IDENTITY["physical_id"]
        )
        assert static_image["protocol"] == STATIC_IMAGE_CAMERA_IDENTITY["protocol"]
        assert static_image["serial"] == STATIC_IMAGE_CAMERA_IDENTITY["serial"]
        assert static_image["vendor"] == STATIC_IMAGE_CAMERA_IDENTITY["vendor"]

        # The pin store's metadata rides along in the capabilities.
        for key, value in metadata.items():
            assert static_image[key] == value
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
