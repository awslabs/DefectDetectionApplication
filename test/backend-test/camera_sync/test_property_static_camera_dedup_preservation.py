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
"""Preservation for the device track of the Static_Image_Camera
de-duplication (feature
static-image-camera-binding-and-pin-discoverability, Defect 3).

# Property 4: Preservation — device enumeration, discovery mapping, and
# reporting unchanged

**Validates: Requirements 3.13, 3.14, 3.15, 3.16, 3.17, 3.18, 3.19**

**These tests PASS on the UNFIXED code** and must keep passing after the
fix: they are the recorded baseline the de-duplication may not disturb.
Written observation-first — every expectation below was OBSERVED on the
unfixed tree first and only then asserted:

- ``build_inventory()`` for discovery results carrying NO aravis camera
  whose ``camera_id`` is ``static-image-camera`` equals an independent
  pre-fix oracle exactly, over arbitrary V4L2 + Aravis cameras, arbitrary
  configured Image_Sources, and both pin states (Requirements 3.15, 3.17).
- The dedicated static entry's shape: fixed id, ``StaticImage``,
  ``edge-discovered``, ``params: {}``, identity under
  ``capabilities.staticImage``, ``discovered: True`` — plus the absent
  variant (``absent=True`` with the supplied ``absentSince``)
  (Requirement 3.17).
- Device-local enumeration and frame serving: ``getCameras()`` against a
  real temp-dir ``StaticImageStore`` appends the synthetic entry while
  pinned and omits it while unpinned, and
  ``getCamera("static-image-camera")`` returns the static handle while
  pinned and raises the not-found-with-pin-hint while unpinned. The
  de-duplication happens ONLY in the cloud report; the static camera must
  keep enumerating on the bus (Requirement 3.13).
- ``aravis_stable_id()`` and ``enumerate_aravis()`` for physical
  identities, INCLUDING the live-healthy Aravis Fake camera: device
  ``Fake_1`` -> registry ``arv-c9dd20f60ee1``, ``Aravis Fake``, present,
  ``params.cameraId: "Fake_1"``, a SINGLE bindable entry (Requirements
  3.15, 3.16).
- A configured Image_Source of type ``Camera`` with ``cameraId:
  "static-image-camera"`` merges into one ``cfg-{imageSourceId}`` entry
  carrying ``capabilities.aravis`` and the tracked absent state — a
  user's explicitly configured source is never dropped (Requirement
  3.18).
- The report document's shape: ``schemaVersion``, ``reportedAt``, version
  counters, ``failures``, ``acks`` (folded into the camera entry),
  ``aliases`` (mirroring the aliased entry), ``discoveryErrors``
  (Requirement 3.19).
- Binding invariance: both duplicates carry the same bindable id string
  ``static-image-camera``, so de-duplicating the registry invalidates no
  existing workflow binding (Requirement 3.14).

The oracle in :func:`pre_fix_inventory` is an INDEPENDENT re-derivation of
the recorded pre-fix merge contract — it never calls ``build_inventory``,
so it keeps asserting the pre-fix behavior after the fix lands. It was
validated against the unfixed ``build_inventory`` over 400 generated
examples before being committed here.

These existing suites are themselves preservation coverage for this fix
and MUST keep passing untouched (tasks.md task 7): ``camera_sync/
test_build_inventory.py``, ``test_build_inventory_aravis.py``,
``test_property_pin_inventory.py``, ``test_property_pin_equivalence.py``,
``test_property_aravis_configured_discovered_merge.py``,
``test_property_aravis_failure_isolation.py``, ``test_report_timing.py``,
``test_property_reconnect_catch_up.py``, ``test_pin_*.py``, the whole
``camera_discovery/`` suite, and ``static_image_camera/
test_property_enumeration_resilience.py``.

Conventions follow the sibling device suites: ``hypothesis`` (not
fast-check) with the profiles registered in the root conftest (``fast`` =
25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100), the
``DiscoveredAravisCamera`` / ``InventorySnapshot`` / ``TrackedCamera``
fixtures of ``test_build_inventory_aravis.py``, the generators and
``_assert_fixed_identity`` of ``test_property_pin_inventory.py``, the real
temp-dir store plus wired-``getCameras()`` enumeration of
``test_property_pin_equivalence.py``, and the ``FakeShadowAccessor`` agent
wiring of ``test_pin_agent_wiring.py`` / ``pin_worker_support.py``.
Physical bus cameras are real ``model.Camera`` objects fed through the
REAL ``enumerate_aravis``, so every stable id comes from the shipped
derivation rather than from a literal.
"""
import contextlib
import hashlib
import json
import os
import shutil
import tempfile
from unittest import mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import camera_sync.agent as agent_module
import edge_ml1_p_camera_management.aravis_functions as aravis_functions
from camera_discovery import (
    DiscoveredAravisCamera,
    DiscoveredCamera,
    DiscoveryResult,
    InventorySnapshot,
    TrackedCamera,
    aravis_stable_id,
    enumerate_aravis,
)
from camera_sync import (
    ORIGIN_EDGE_CONFIGURED,
    ORIGIN_EDGE_DISCOVERED,
    SCHEMA_VERSION,
    STATIC_IMAGE_CAMERA_NAME,
    TYPE_ARAVIS_DISCOVERED,
    TYPE_STATIC_IMAGE,
    TYPE_V4L2_DISCOVERED,
    CameraSourceState,
    CameraSyncStateStore,
    EdgeSyncAgent,
    build_inventory,
    build_report_document,
)
from exceptions.api.aravis_camera_not_found import AravisCameraNotFound
from model.Camera import Camera
from utils.static_image_camera import (
    STATIC_IMAGE_CAMERA_ID,
    STATIC_IMAGE_CAMERA_IDENTITY,
    StaticImageStore,
)

from pin_worker_support import (
    FakeShadowAccessor,
    fresh_store_dirs,
    image_specs,
    render_image_bytes,
)

#: The DUPLICATE's id in the live ``dda-camera-registry`` shadow for
#: ``jetson-thor1`` — derived, never matched by hardcode (Requirement
#: 2.11). No entry generated by this suite may collide with it.
LIVE_DUPLICATE_ID = "arv-6c84191b7fe6"

#: The live-healthy Aravis Fake camera on the same device: device id
#: ``Fake_1`` -> registry ``arv-c9dd20f60ee1``, name ``Aravis Fake``,
#: present, ``params.cameraId: "Fake_1"``, binds correctly. Explicitly NOT
#: a defect (Requirement 3.16), and it must stay ONE bindable entry.
LIVE_FAKE_STABLE_ID = "arv-c9dd20f60ee1"

#: The Fake camera's identity in the ``model.Camera`` shape
#: ``getCameras()`` returns. ``serial`` is non-empty, so the derived
#: stable id is a function of (vendor, model, serial) alone.
LIVE_FAKE_IDENTITY = {
    "id": "Fake_1",
    "model": "Fake",
    "address": "127.0.0.1",
    "physical_id": "Fake_1",
    "protocol": "Fake",
    "serial": "1",
    "vendor": "Aravis",
}

#: The live ``absentSince`` both duplicated rows carried while nothing was
#: pinned (bugfix.md 1.10).
LIVE_ABSENT_SINCE = 1788839397466


def static_image_aravis_stable_id():
    """The duplicate's key, DERIVED from the shipped identity through the
    real ``aravis_stable_id`` (Requirement 2.11)."""
    return aravis_stable_id(
        STATIC_IMAGE_CAMERA_IDENTITY["vendor"],
        STATIC_IMAGE_CAMERA_IDENTITY["model"],
        STATIC_IMAGE_CAMERA_IDENTITY["serial"],
        STATIC_IMAGE_CAMERA_IDENTITY["physical_id"],
    )


def static_bus_camera():
    """Exactly what ``getCameras()`` appends while pinned: a real
    ``model.Camera`` built from the shipped identity."""
    return Camera(**STATIC_IMAGE_CAMERA_IDENTITY)


def static_registrations(entries):
    """Registrations of the ONE virtual camera, counted by the id a node
    binds to rather than by entry id."""
    return [
        entry
        for entry in entries
        if entry.camera_source_id == STATIC_IMAGE_CAMERA_ID
        or entry.params.get("cameraId") == STATIC_IMAGE_CAMERA_ID
    ]


def describe(entries):
    """Readable rendering for assertion messages."""
    return [
        (entry.camera_source_id, entry.type, entry.absent, dict(entry.params))
        for entry in entries
    ]


def identity_fields(camera):
    """The seven enumeration identity fields of one ``model.Camera``
    (``test_property_pin_equivalence.py`` builds this same tuple)."""
    return (
        camera.id,
        camera.model,
        camera.address,
        camera.physical_id,
        camera.protocol,
        camera.serial,
        camera.vendor,
    )


#: The static camera's enumeration identity tuple, in the order
#: :func:`identity_fields` returns.
STATIC_IDENTITY_TUPLE = (
    STATIC_IMAGE_CAMERA_ID,
    STATIC_IMAGE_CAMERA_IDENTITY["model"],
    STATIC_IMAGE_CAMERA_IDENTITY["address"],
    STATIC_IMAGE_CAMERA_IDENTITY["physical_id"],
    STATIC_IMAGE_CAMERA_IDENTITY["protocol"],
    STATIC_IMAGE_CAMERA_IDENTITY["serial"],
    STATIC_IMAGE_CAMERA_IDENTITY["vendor"],
)


def _assert_fixed_identity(static_image):
    """The fixed Static_Image_Camera identity under
    ``capabilities.staticImage`` (``test_property_pin_inventory.py``'s
    oracle, duplicated here rather than imported across test modules)."""
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


#: The identity keys the entry carries with no pin metadata folded in.
_IDENTITY_KEYS = {
    "id", "model", "address", "physicalId", "protocol", "serial", "vendor",
}


# --- generators (mirroring the sibling camera_sync merge suites) ---------------

_DEVICE_PATHS = st.integers(min_value=0, max_value=9).map(
    lambda n: "/dev/video{}".format(n)
)

_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=15,
)

#: Configured/discovered camera ids come from a small shared pool so
#: overlaps happen often. The pool deliberately EXCLUDES
#: ``static-image-camera``: this suite covers every merge whose discovery
#: input carries NO aravis camera claiming the static id — the inputs the
#: fix must leave byte-for-byte identical.
_CAMERA_IDS = st.integers(min_value=0, max_value=9).map(
    lambda n: "cam-{}".format(n)
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

_PIN_METADATA = st.one_of(
    st.none(),
    st.fixed_dictionaries(
        {
            "width": st.integers(min_value=1, max_value=4096),
            "height": st.integers(min_value=1, max_value=4096),
            "format": st.sampled_from(["JPEG", "PNG", "BMP"]),
            "fileName": _TEXT,
        }
    ),
)

_ABSENT_SINCE = st.one_of(
    st.none(),
    st.integers(min_value=0, max_value=2_000_000_000_000),
)


@st.composite
def _v4l2_cameras(draw):
    """Arbitrary physical V4L2 cameras with their tracked absence flag
    (pin-inventory conventions); device paths are unique."""
    paths = draw(st.lists(_DEVICE_PATHS, unique=True, max_size=3))
    return [
        (
            DiscoveredCamera(
                stable_id="disc-{:012d}".format(index),
                device_path=path,
                card_name=draw(_TEXT),
                bus_info=draw(_TEXT),
                driver="uvcvideo",
                kind="v4l2",
                formats=draw(_FORMATS),
            ),
            draw(st.booleans()),
        )
        for index, path in enumerate(paths)
    ]


@st.composite
def _physical_bus_cameras(draw):
    """Arbitrary PHYSICAL Aravis bus cameras with their tracked absence
    flag, in the ``model.Camera`` shape ``getCameras()`` returns.

    Runtime ids are unique and drawn from the shared pool (so they never
    equal ``static-image-camera``); identities are unique by derived
    stable id and can never derive the static camera's key.
    """
    camera_ids = draw(st.lists(_CAMERA_IDS, unique=True, max_size=3))
    identities = draw(
        st.lists(
            st.tuples(_TEXT, _TEXT, _TEXT, _TEXT),
            min_size=len(camera_ids),
            max_size=len(camera_ids),
            unique_by=lambda t: aravis_stable_id(t[0], t[1], t[2], t[3]),
        ).filter(
            lambda ids: all(
                aravis_stable_id(v, m, s, p) != LIVE_DUPLICATE_ID
                for v, m, s, p in ids
            )
        )
    )
    return [
        (
            Camera(
                id=camera_id,
                model=model,
                address=draw(_TEXT),
                physical_id=physical_id,
                protocol=draw(st.sampled_from(["GigEVision", "USB3Vision"])),
                serial=serial,
                vendor=vendor,
            ),
            draw(st.booleans()),
        )
        for camera_id, (vendor, model, serial, physical_id) in zip(
            camera_ids, identities
        )
    ]


@st.composite
def _image_sources(draw):
    """Arbitrary configured Image_Sources — device-path backed, cameraId
    backed, and pathless — none of them referencing the static camera.

    Device paths and camera ids are unique across the sources (SQLite
    rows referencing one camera each), so every discovered camera merges
    into at most one configured entry.
    """
    device_paths = draw(st.lists(_DEVICE_PATHS, unique=True, max_size=2))
    pathless_count = draw(st.integers(min_value=0, max_value=1))
    devices = list(device_paths) + [None] * pathless_count
    camera_ids = draw(
        st.lists(
            _CAMERA_IDS,
            unique=True,
            min_size=len(devices),
            max_size=len(devices),
        )
    )
    sources = []
    for index, (device, camera_id) in enumerate(zip(devices, camera_ids)):
        configuration = {}
        if device is not None:
            configuration["device"] = device
        if draw(st.booleans()):
            configuration["gain"] = draw(st.integers(min_value=0, max_value=100))
        if draw(st.booleans()):
            configuration["exposure"] = draw(
                st.integers(min_value=1, max_value=10_000_000)
            )
        if draw(st.booleans()):
            configuration["deviceName"] = draw(_TEXT)
        source = {
            "imageSourceId": "is-{}".format(index),
            "name": draw(_TEXT),
            "type": draw(st.sampled_from(["Camera", "ICam", "Folder"])),
            "cameraId": camera_id,
            "imageSourceConfiguration": configuration,
        }
        if draw(st.booleans()):
            source["location"] = draw(_TEXT)
        sources.append(source)
    return sources


# --- the independent pre-fix oracle -------------------------------------------


def _v4l2_capabilities(camera):
    """Discovered V4L2 capability metadata in the reported shape."""
    return {
        "formats": [
            {
                "pixelFormat": fmt.get("pixel_format"),
                "resolutions": [list(r) for r in fmt.get("resolutions", [])],
            }
            for fmt in camera.formats
        ],
        "driver": camera.driver,
        "busInfo": camera.bus_info,
        "kind": camera.kind,
    }


def _aravis_capabilities(camera):
    """Discovered Aravis identity metadata reported under
    ``capabilities.aravis``."""
    return {
        "model": camera.model,
        "address": camera.address,
        "physicalId": camera.physical_id,
        "protocol": camera.protocol,
        "serial": camera.serial,
        "vendor": camera.vendor,
    }


def static_identity_capabilities():
    """The fixed Static_Image_Camera identity under
    ``capabilities.staticImage``."""
    return {
        "id": STATIC_IMAGE_CAMERA_ID,
        "model": STATIC_IMAGE_CAMERA_IDENTITY["model"],
        "address": STATIC_IMAGE_CAMERA_IDENTITY["address"],
        "physicalId": STATIC_IMAGE_CAMERA_IDENTITY["physical_id"],
        "protocol": STATIC_IMAGE_CAMERA_IDENTITY["protocol"],
        "serial": STATIC_IMAGE_CAMERA_IDENTITY["serial"],
        "vendor": STATIC_IMAGE_CAMERA_IDENTITY["vendor"],
    }


def pre_fix_inventory(sources, tracked, pinned, metadata, absent_since):
    """The RECORDED pre-fix ``build_inventory`` contract, re-derived
    independently of the module under test.

    ``tracked`` is ``{stable_id: (camera, absent, absent_since)}``. Valid
    over this suite's generated input space, where device paths and Aravis
    runtime ids are unique, so no merge key is contested and each
    discovered camera merges into at most one configured entry.

    Order (observed, and documented as deterministic): configured entries
    sorted by ``imageSourceId``, then discovered-only entries sorted by
    stable id, then the Static_Image_Camera entry when the pin state calls
    for one.
    """
    by_path, by_camera_id = {}, {}
    for stable_id, (camera, _absent, _since) in tracked.items():
        if isinstance(camera, DiscoveredAravisCamera):
            by_camera_id[camera.camera_id] = stable_id
        else:
            by_path[camera.device_path] = stable_id

    merged = set()
    entries = []
    for source in sorted(sources, key=lambda s: str(s["imageSourceId"] or "")):
        configuration = source.get("imageSourceConfiguration") or {}
        device_path = (
            str(configuration["device"]) if configuration.get("device") else None
        )
        params = {}
        if device_path is not None:
            params["devicePath"] = device_path
        for key in ("cameraId", "location", "description"):
            if source.get(key):
                params[key] = source[key]
        for key in ("gain", "exposure", "deviceName"):
            if configuration.get(key) is not None:
                params[key] = configuration[key]

        path_id = by_path.get(device_path) if device_path is not None else None
        aravis_id = None
        if source["type"] == "Camera" and source.get("cameraId"):
            aravis_id = by_camera_id.get(str(source["cameraId"]))

        camera_source_id = "cfg-" + str(source["imageSourceId"])
        if path_id is None and aravis_id is None:
            entries.append(
                CameraSourceState(
                    camera_source_id=camera_source_id,
                    name=source.get("name") or "",
                    type=source["type"],
                    origin=ORIGIN_EDGE_CONFIGURED,
                    params=params,
                )
            )
            continue

        capabilities, absent, since = {}, False, None
        if path_id is not None:
            merged.add(path_id)
            camera, absent, since = tracked[path_id]
            capabilities = _v4l2_capabilities(camera)
        if aravis_id is not None:
            merged.add(aravis_id)
            camera, aravis_absent, aravis_since = tracked[aravis_id]
            capabilities["aravis"] = _aravis_capabilities(camera)
            if path_id is None:
                absent, since = aravis_absent, aravis_since
        entries.append(
            CameraSourceState(
                camera_source_id=camera_source_id,
                name=source.get("name") or "",
                type=source["type"],
                origin=ORIGIN_EDGE_CONFIGURED,
                params=params,
                capabilities=capabilities,
                discovered=True,
                absent=absent,
                absent_since=since,
            )
        )

    for stable_id in sorted(tracked):
        if stable_id in merged:
            continue
        camera, absent, since = tracked[stable_id]
        if isinstance(camera, DiscoveredAravisCamera):
            entries.append(
                CameraSourceState(
                    camera_source_id=stable_id,
                    name="{} {}".format(camera.vendor, camera.model),
                    type=TYPE_ARAVIS_DISCOVERED,
                    origin=ORIGIN_EDGE_DISCOVERED,
                    params={
                        "cameraId": camera.camera_id,
                        "serial": camera.serial,
                        "protocol": camera.protocol,
                        "address": camera.address,
                    },
                    capabilities={"aravis": _aravis_capabilities(camera)},
                    discovered=True,
                    absent=absent,
                    absent_since=since,
                )
            )
            continue
        entries.append(
            CameraSourceState(
                camera_source_id=stable_id,
                name=camera.card_name,
                type=TYPE_V4L2_DISCOVERED,
                origin=ORIGIN_EDGE_DISCOVERED,
                params={"devicePath": camera.device_path},
                capabilities=_v4l2_capabilities(camera),
                discovered=True,
                absent=absent,
                absent_since=since,
            )
        )

    if pinned:
        static_image = static_identity_capabilities()
        if metadata:
            static_image.update(dict(metadata))
        entries.append(
            CameraSourceState(
                camera_source_id=STATIC_IMAGE_CAMERA_ID,
                name=STATIC_IMAGE_CAMERA_NAME,
                type=TYPE_STATIC_IMAGE,
                origin=ORIGIN_EDGE_DISCOVERED,
                params={},
                capabilities={"staticImage": static_image},
                discovered=True,
            )
        )
    elif absent_since is not None:
        entries.append(
            CameraSourceState(
                camera_source_id=STATIC_IMAGE_CAMERA_ID,
                name=STATIC_IMAGE_CAMERA_NAME,
                type=TYPE_STATIC_IMAGE,
                origin=ORIGIN_EDGE_DISCOVERED,
                params={},
                capabilities={"staticImage": static_identity_capabilities()},
                discovered=True,
                absent=True,
                absent_since=int(absent_since),
            )
        )
    return entries


def _enumerated(bus_cameras):
    """The REAL Aravis discovery mapping over a bus enumeration."""
    result = enumerate_aravis(enumerator=lambda: list(bus_cameras))
    assert result.failures == [], (
        "the bus enumeration must map cleanly; got failures: {}".format(
            result.failures
        )
    )
    return result.cameras


def _tracked(pairs, snapshot, absent_since_ms=1_700_000_000_000):
    """``{stable_id: (camera, absent, absent_since)}`` for the drawn
    ``(camera, absent)`` pairs; a fresh ``DiscoveryResult`` pass tracks
    every camera present."""
    tracked = {}
    for camera, absent in pairs:
        absent = bool(absent) and snapshot
        tracked[camera.stable_id] = (
            camera,
            absent,
            absent_since_ms if absent else None,
        )
    return tracked


def _discovery_input(tracked, snapshot):
    """The tracked cameras as an ``InventorySnapshot`` (absence-carrying)
    or as a fresh ``DiscoveryResult``."""
    if snapshot:
        return InventorySnapshot(
            cameras={
                stable_id: TrackedCamera(
                    camera=camera, absent=absent, absent_since=since
                )
                for stable_id, (camera, absent, since) in tracked.items()
            }
        )
    return DiscoveryResult(cameras=[camera for camera, _, _ in tracked.values()])


# --- the merge is byte-for-byte identical without a static bus camera ---------


@settings(deadline=None)
@given(
    sources=_image_sources(),
    v4l2=_v4l2_cameras(),
    physical=_physical_bus_cameras(),
    snapshot=st.booleans(),
    pinned=st.booleans(),
    metadata=_PIN_METADATA,
    absent_since=_ABSENT_SINCE,
)
def test_inventory_without_static_bus_camera_matches_pre_fix_oracle(
    sources, v4l2, physical, snapshot, pinned, metadata, absent_since
):
    """# Property 4: Preservation — the merge is unchanged for every
    discovery input carrying no static-claiming aravis camera

    **Validates: Requirements 3.15, 3.17**

    Arbitrary physical V4L2 and Aravis cameras (the latter mapped by the
    real ``enumerate_aravis``), arbitrary configured Image_Sources, both
    discovery input forms, and every pin state: the merge output equals
    the independent pre-fix oracle exactly — entry order, ids, names,
    types, origins, params, capabilities, discovered/absent state, and
    the appended Static_Image_Camera entry included.
    """
    pairs = list(v4l2) + list(
        zip(
            _enumerated([camera for camera, _ in physical]),
            [absent for _, absent in physical],
        )
    )
    tracked = _tracked(pairs, snapshot)
    discovery = _discovery_input(tracked, snapshot)

    entries = build_inventory(
        sources,
        discovery,
        static_image_pinned=pinned,
        static_image_metadata=metadata,
        static_image_absent_since=absent_since,
    )
    expected = pre_fix_inventory(sources, tracked, pinned, metadata, absent_since)

    assert entries == expected, (
        "the merge must be byte-for-byte the recorded pre-fix output for a "
        "discovery input with no static-claiming aravis camera;\ngot  {}\n"
        "want {}".format(describe(entries), describe(expected))
    )
    # No aravis-derived static entry can appear when nothing claims the
    # static id, and the dedicated entry's presence still tracks the pin
    # state exactly as before.
    assert not [
        entry
        for entry in entries
        if entry.camera_source_id == static_image_aravis_stable_id()
    ]
    assert len(static_registrations(entries)) == (
        1 if pinned else (1 if absent_since is not None else 0)
    )


# --- the dedicated static entry's shape ---------------------------------------


@settings(deadline=None)
@given(metadata=_PIN_METADATA)
def test_dedicated_static_entry_shape_unchanged_while_pinned(metadata):
    """# Property 4: Preservation — the shipped entry contract

    **Validates: Requirement 3.17**

    Fixed id, ``StaticImage``, ``edge-discovered``, ``params: {}``,
    identity (plus the pin store's metadata) under
    ``capabilities.staticImage``, ``discovered: True``, present.
    """
    (entry,) = build_inventory(
        [],
        DiscoveryResult(cameras=[]),
        static_image_pinned=True,
        static_image_metadata=metadata,
    )

    assert entry.camera_source_id == STATIC_IMAGE_CAMERA_ID
    assert entry.name == STATIC_IMAGE_CAMERA_NAME == "Static Image Camera"
    assert entry.type == TYPE_STATIC_IMAGE == "StaticImage"
    assert entry.origin == ORIGIN_EDGE_DISCOVERED == "edge-discovered"
    assert entry.params == {}
    assert entry.discovered is True
    assert entry.absent is False
    assert entry.absent_since is None

    assert set(entry.capabilities) == {"staticImage"}
    static_image = entry.capabilities["staticImage"]
    _assert_fixed_identity(static_image)
    assert set(static_image) == _IDENTITY_KEYS | set(metadata or {})
    for key, value in (metadata or {}).items():
        assert static_image[key] == value


@settings(deadline=None)
@given(absent_since=st.integers(min_value=0, max_value=2_000_000_000_000))
def test_dedicated_static_entry_shape_unchanged_while_absent(absent_since):
    """# Property 4: Preservation — the explicit ABSENT entry

    **Validates: Requirement 3.17**

    Same fixed identity/type/origin, identity-only capabilities (nothing
    is pinned), ``absent=True`` with the supplied ``absentSince``.
    """
    (entry,) = build_inventory(
        [],
        DiscoveryResult(cameras=[]),
        static_image_pinned=False,
        static_image_absent_since=absent_since,
    )

    assert entry.camera_source_id == STATIC_IMAGE_CAMERA_ID
    assert entry.name == STATIC_IMAGE_CAMERA_NAME
    assert entry.type == TYPE_STATIC_IMAGE
    assert entry.origin == ORIGIN_EDGE_DISCOVERED
    assert entry.params == {}
    assert entry.discovered is True
    assert entry.absent is True
    assert entry.absent_since == absent_since

    static_image = entry.capabilities["staticImage"]
    _assert_fixed_identity(static_image)
    assert set(static_image) == _IDENTITY_KEYS


def test_recorded_static_entries_match_the_live_shadow_shape():
    """The concrete recorded entries (observed on unfixed code).

    **Validates: Requirement 3.17**

    The absent variant is the live ``dda-camera-registry`` row for
    ``jetson-thor1``; the present variant is the same entry with pin
    metadata folded in. Never-reported and unpinned yields no entry.
    """
    identity = {
        "id": "static-image-camera",
        "model": "Static Image Camera",
        "address": "internal",
        "physicalId": "static-image-camera",
        "protocol": "StaticImage",
        "serial": "STATIC-IMAGE-0",
        "vendor": "AWS-DDA",
    }

    assert build_inventory(
        [], DiscoveryResult(cameras=[]), static_image_pinned=True
    ) == [
        CameraSourceState(
            camera_source_id="static-image-camera",
            name="Static Image Camera",
            type="StaticImage",
            origin="edge-discovered",
            params={},
            capabilities={"staticImage": dict(identity)},
            discovered=True,
            absent=False,
            absent_since=None,
        )
    ]

    assert build_inventory(
        [],
        DiscoveryResult(cameras=[]),
        static_image_pinned=False,
        static_image_absent_since=LIVE_ABSENT_SINCE,
    ) == [
        CameraSourceState(
            camera_source_id="static-image-camera",
            name="Static Image Camera",
            type="StaticImage",
            origin="edge-discovered",
            params={},
            capabilities={"staticImage": dict(identity)},
            discovered=True,
            absent=True,
            absent_since=LIVE_ABSENT_SINCE,
        )
    ]

    assert build_inventory([], DiscoveryResult(cameras=[])) == []


# --- device-local enumeration and frame serving ------------------------------


class _FakeAravisBus:
    """Physical-bus double over a list of identity tuples in
    ``Aravis.get_device_*`` order (``test_property_pin_equivalence.py``'s
    empty-bus double, extended with devices)."""

    def __init__(self, devices=()):
        self.devices = list(devices)

    def enable_interface(self, name):
        pass

    def update_device_list(self):
        pass

    def get_n_devices(self):
        return len(self.devices)

    def get_device_id(self, index):
        return self.devices[index][0]

    def get_device_model(self, index):
        return self.devices[index][1]

    def get_device_address(self, index):
        return self.devices[index][2]

    def get_device_physical_id(self, index):
        return self.devices[index][3]

    def get_device_protocol(self, index):
        return self.devices[index][4]

    def get_device_serial_nbr(self, index):
        return self.devices[index][5]

    def get_device_vendor(self, index):
        return self.devices[index][6]


@contextlib.contextmanager
def _wired_enumeration(store, bus):
    """``aravis_functions`` wired to a real store and a bus double."""
    with mock.patch.object(aravis_functions, "Aravis", bus), mock.patch.object(
        aravis_functions, "get_store", lambda: store
    ):
        yield


#: The physical bus used alongside the static camera: the live-healthy
#: Aravis Fake camera (Requirement 3.16).
_FAKE_BUS_DEVICE = (
    LIVE_FAKE_IDENTITY["id"],
    LIVE_FAKE_IDENTITY["model"],
    LIVE_FAKE_IDENTITY["address"],
    LIVE_FAKE_IDENTITY["physical_id"],
    LIVE_FAKE_IDENTITY["protocol"],
    LIVE_FAKE_IDENTITY["serial"],
    LIVE_FAKE_IDENTITY["vendor"],
)


@settings(deadline=None)
@given(spec=image_specs, with_physical=st.booleans())
def test_get_cameras_still_appends_the_static_entry_only_while_pinned(
    spec, with_physical
):
    """# Property 4: Preservation — device-local enumeration untouched

    **Validates: Requirement 3.13**

    Against a real temp-dir ``StaticImageStore``, ``getCameras()``
    appends exactly one synthetic static entry with the fixed identity
    while a Pinned_Image exists and omits it while unpinned — physical
    cameras enumerating unchanged before it. The de-duplication belongs
    to the cloud report only; the static camera must keep enumerating on
    the bus like a GenICam camera.
    """
    tmp_dir = tempfile.mkdtemp(prefix="static-dedup-enum-")
    try:
        store_dir, _marker_path = fresh_store_dirs(tmp_dir)
        store = StaticImageStore(base_dir=store_dir)
        bus = _FakeAravisBus([_FAKE_BUS_DEVICE] if with_physical else [])
        physical = [_FAKE_BUS_DEVICE] if with_physical else []

        with _wired_enumeration(store, bus):
            assert store.is_pinned() is False
            unpinned = [identity_fields(c) for c in aravis_functions.getCameras()]
            assert unpinned == physical

            store.pin_bytes(render_image_bytes(*spec), "preserved.img")
            pinned = [identity_fields(c) for c in aravis_functions.getCameras()]
            assert pinned == physical + [STATIC_IDENTITY_TUPLE]

            store.unpin()
            assert [
                identity_fields(c) for c in aravis_functions.getCameras()
            ] == physical
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@settings(deadline=None)
@given(spec=image_specs)
def test_get_camera_static_short_circuit_unchanged(spec):
    """# Property 4: Preservation — ``getCamera()`` by the fixed id

    **Validates: Requirement 3.13**

    ``getCamera("static-image-camera")`` returns the truthy static handle
    (carrying the fixed vendor/model for the Image_Source default-config
    path) while pinned, and raises the not-found-with-pin-hint while
    unpinned.
    """
    tmp_dir = tempfile.mkdtemp(prefix="static-dedup-getcamera-")
    try:
        store_dir, _marker_path = fresh_store_dirs(tmp_dir)
        store = StaticImageStore(base_dir=store_dir)

        with _wired_enumeration(store, _FakeAravisBus()):
            with pytest.raises(AravisCameraNotFound) as unpinned:
                aravis_functions.getCamera(STATIC_IMAGE_CAMERA_ID)
            message = unpinned.value.message
            assert message == (
                "Static image camera 'static-image-camera' is not available "
                "because no image is pinned. Pin an image through the static "
                "image pin API before using this camera."
            )
            assert unpinned.value.status_code == 404

            store.pin_bytes(render_image_bytes(*spec), "preserved.img")
            handle = aravis_functions.getCamera(STATIC_IMAGE_CAMERA_ID)
            assert bool(handle) is True
            assert handle.get_vendor_name() == STATIC_IMAGE_CAMERA_IDENTITY["vendor"]
            assert handle.get_model_name() == STATIC_IMAGE_CAMERA_IDENTITY["model"]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --- the discovery layer is untouched for every identity ---------------------


def _expected_stable_id(vendor, model, serial, physical_id):
    """The documented derivation, re-implemented independently:
    ``arv-{sha1(vendor|model|serial)[:12]}``, falling back to including
    ``physical_id`` when the serial is empty."""
    parts = (
        (vendor, model, serial)
        if serial
        else (vendor, model, serial, physical_id)
    )
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return "arv-" + digest[:12]


@settings(deadline=None)
@given(physical=_physical_bus_cameras())
def test_aravis_stable_id_and_enumerate_aravis_unchanged(physical):
    """# Property 4: Preservation — the discovery layer for every identity

    **Validates: Requirement 3.15**

    ``aravis_stable_id()`` equals the documented derivation and
    ``enumerate_aravis()`` maps every bus camera to exactly one
    ``DiscoveredAravisCamera`` carrying its runtime id and identity
    fields verbatim, with no failures.
    """
    bus = [camera for camera, _ in physical]
    result = enumerate_aravis(enumerator=lambda: list(bus))

    assert result.failures == []
    assert len(result.cameras) == len(bus)
    for raw, mapped in zip(bus, result.cameras):
        assert mapped == DiscoveredAravisCamera(
            stable_id=_expected_stable_id(
                raw.vendor, raw.model, raw.serial, raw.physical_id
            ),
            camera_id=raw.id,
            model=raw.model,
            address=raw.address,
            physical_id=raw.physical_id,
            protocol=raw.protocol,
            serial=raw.serial,
            vendor=raw.vendor,
        )
        assert aravis_stable_id(
            raw.vendor, raw.model, raw.serial, raw.physical_id
        ) == mapped.stable_id


def test_live_fake_camera_stays_a_single_bindable_entry():
    """The live-healthy Aravis Fake camera (recorded on unfixed code).

    **Validates: Requirements 3.15, 3.16**

    Device ``Fake_1`` derives ``arv-c9dd20f60ee1`` and reports as ONE
    present ``AravisDiscovered`` entry named ``Aravis Fake`` carrying
    ``params.cameraId: "Fake_1"`` — the id a node binds to. A configured
    ``Camera`` source referencing that id merges into a single
    ``cfg-`` entry, so the camera binds either way. Explicitly NOT a
    defect, and untouched by the static-camera de-duplication.
    """
    (fake,) = _enumerated([Camera(**LIVE_FAKE_IDENTITY)])
    assert fake.stable_id == LIVE_FAKE_STABLE_ID
    assert fake.camera_id == "Fake_1"

    assert build_inventory([], DiscoveryResult(cameras=[fake])) == [
        CameraSourceState(
            camera_source_id=LIVE_FAKE_STABLE_ID,
            name="Aravis Fake",
            type="AravisDiscovered",
            origin="edge-discovered",
            params={
                "cameraId": "Fake_1",
                "serial": "1",
                "protocol": "Fake",
                "address": "127.0.0.1",
            },
            capabilities={
                "aravis": {
                    "model": "Fake",
                    "address": "127.0.0.1",
                    "physicalId": "Fake_1",
                    "protocol": "Fake",
                    "serial": "1",
                    "vendor": "Aravis",
                }
            },
            discovered=True,
            absent=False,
            absent_since=None,
        )
    ]

    configured = build_inventory(
        [
            {
                "imageSourceId": "is-fake",
                "name": "Fake line cam",
                "type": "Camera",
                "cameraId": "Fake_1",
                "imageSourceConfiguration": {},
            }
        ],
        DiscoveryResult(cameras=[fake]),
    )
    assert [entry.camera_source_id for entry in configured] == ["cfg-is-fake"]
    assert configured[0].params["cameraId"] == "Fake_1"
    assert configured[0].capabilities["aravis"]["vendor"] == "Aravis"
    assert configured[0].discovered is True
    assert configured[0].absent is False


# --- a user's configured source referencing the static camera ----------------


def _static_referencing_source():
    """A configured Image_Source of type ``Camera`` whose ``cameraId`` is
    the static camera's fixed id."""
    return {
        "imageSourceId": "is-9",
        "name": "Pinned test source",
        "type": "Camera",
        "cameraId": STATIC_IMAGE_CAMERA_ID,
        "imageSourceConfiguration": {"gain": 3, "exposure": 5000},
    }


def test_configured_source_referencing_static_camera_merges_unchanged():
    """# Property 4: Preservation — the configured merge is untouched

    **Validates: Requirement 3.18**

    A configured ``Camera`` source carrying ``cameraId:
    "static-image-camera"`` merges with the aravis-enumerated static
    camera into ONE ``cfg-{imageSourceId}`` entry carrying
    ``capabilities.aravis`` and the tracked absent state, alongside the
    dedicated virtual entry — recorded here in both pin states. The
    exclusion applies only to the discovery entry that would otherwise be
    reported separately, so a user's explicitly configured source is never
    dropped.
    """
    (static_discovered,) = _enumerated([static_bus_camera()])
    aravis_capabilities = {
        "model": "Static Image Camera",
        "address": "internal",
        "physicalId": "static-image-camera",
        "protocol": "StaticImage",
        "serial": "STATIC-IMAGE-0",
        "vendor": "AWS-DDA",
    }

    # Unpinned after having been reported: the tracker still holds the
    # aravis-enumerated static camera as an absent leftover, and that
    # absence rides into the user's configured entry.
    absent_entries = build_inventory(
        [_static_referencing_source()],
        InventorySnapshot(
            cameras={
                static_discovered.stable_id: TrackedCamera(
                    camera=static_discovered,
                    absent=True,
                    absent_since=LIVE_ABSENT_SINCE,
                )
            }
        ),
        static_image_pinned=False,
        static_image_absent_since=LIVE_ABSENT_SINCE,
    )
    assert absent_entries == [
        CameraSourceState(
            camera_source_id="cfg-is-9",
            name="Pinned test source",
            type="Camera",
            origin="edge-configured",
            params={
                "cameraId": "static-image-camera",
                "gain": 3,
                "exposure": 5000,
            },
            capabilities={"aravis": dict(aravis_capabilities)},
            discovered=True,
            absent=True,
            absent_since=LIVE_ABSENT_SINCE,
        ),
        CameraSourceState(
            camera_source_id="static-image-camera",
            name="Static Image Camera",
            type="StaticImage",
            origin="edge-discovered",
            params={},
            capabilities={"staticImage": static_identity_capabilities()},
            discovered=True,
            absent=True,
            absent_since=LIVE_ABSENT_SINCE,
        ),
    ], describe(absent_entries)

    # Pinned: the same single configured entry, present, plus the
    # dedicated entry carrying the pin metadata.
    pinned_entries = build_inventory(
        [_static_referencing_source()],
        DiscoveryResult(cameras=[static_discovered]),
        static_image_pinned=True,
        static_image_metadata={"width": 4, "height": 4},
    )
    configured = [
        entry for entry in pinned_entries if entry.camera_source_id == "cfg-is-9"
    ]
    assert configured == [
        CameraSourceState(
            camera_source_id="cfg-is-9",
            name="Pinned test source",
            type="Camera",
            origin="edge-configured",
            params={
                "cameraId": "static-image-camera",
                "gain": 3,
                "exposure": 5000,
            },
            capabilities={"aravis": dict(aravis_capabilities)},
            discovered=True,
            absent=False,
            absent_since=None,
        )
    ], describe(pinned_entries)
    # The configured source keeps its own entry and the derived aravis id
    # is never reported separately alongside it (it merged).
    assert not [
        entry
        for entry in pinned_entries
        if entry.camera_source_id == static_image_aravis_stable_id()
    ]


# --- binding invariance ------------------------------------------------------


def test_both_duplicates_carry_the_same_bindable_camera_id():
    """# Property 4: Preservation — de-duplication invalidates no binding

    **Validates: Requirement 3.14**

    A workflow node binds by the device-side camera id, and BOTH
    duplicated rows carry the identical string ``static-image-camera`` —
    the ``arv-`` entry through ``params.cameraId``, the dedicated entry
    through ``capabilities.staticImage.id``. Removing one registration
    therefore leaves every existing binding resolving to the same
    device-side camera, which ``getCamera()`` still serves.
    """
    (static_discovered,) = _enumerated([static_bus_camera()])
    entries = build_inventory(
        [],
        DiscoveryResult(cameras=[static_discovered]),
        static_image_pinned=True,
    )

    arv_entries = [
        entry
        for entry in entries
        if entry.camera_source_id == static_image_aravis_stable_id()
    ]
    dedicated = [
        entry
        for entry in entries
        if entry.camera_source_id == STATIC_IMAGE_CAMERA_ID
    ]
    assert len(dedicated) == 1

    # Whether the arv- duplicate is reported (pre-fix) or excluded
    # (post-fix), the bindable id it carried is the dedicated entry's.
    for entry in arv_entries:
        assert entry.params["cameraId"] == STATIC_IMAGE_CAMERA_ID
    assert (
        dedicated[0].capabilities["staticImage"]["id"] == STATIC_IMAGE_CAMERA_ID
    )
    assert STATIC_IMAGE_CAMERA_ID == "static-image-camera"


# --- the report document's shape --------------------------------------------


def test_report_document_shape_unchanged_without_a_static_camera():
    """# Property 4: Preservation — the reported document

    **Validates: Requirement 3.19**

    Recorded on unfixed code for an inventory with no static camera
    involved: exactly the five top-level keys, ``schemaVersion``,
    ``reportedAt``, per-entry version counters, ``failures`` verbatim,
    ``discoveryErrors`` as a list, one-shot ``acks`` folded into the
    acked camera entry, and an ``aliases`` key mirroring the aliased
    entry (its ``ack`` included).
    """
    inventory = [
        CameraSourceState(
            camera_source_id="cfg-is-1",
            name="Line 1 cam",
            type="Camera",
            origin=ORIGIN_EDGE_CONFIGURED,
            params={"devicePath": "/dev/video0", "cameraId": "cam-1"},
            capabilities={
                "formats": [],
                "driver": "uvcvideo",
                "busInfo": "usb-1",
                "kind": "v4l2",
            },
            discovered=True,
        ),
        CameraSourceState(
            camera_source_id="disc-000000000002",
            name="Gone Cam",
            type=TYPE_V4L2_DISCOVERED,
            origin=ORIGIN_EDGE_DISCOVERED,
            params={"devicePath": "/dev/video2"},
            capabilities={
                "formats": [],
                "driver": "uvcvideo",
                "busInfo": "usb-2",
                "kind": "v4l2",
            },
            discovered=True,
            absent=True,
            absent_since=1729990000000,
        ),
    ]

    document = build_report_document(
        inventory,
        {"cfg-is-1": 4, "disc-000000000002": 2},
        reported_at_ms=1_700_000_000_123,
        failures={"cfg-is-7": {"changeId": "chg-7", "reason": "boom"}},
        discovery_errors=[{"devicePath": "/dev/video5", "error": "open failed"}],
        acks={"cfg-is-1": "chg-1"},
        aliases={"new-cam-1": "cfg-is-1"},
    )

    configured_entry = {
        "version": 4,
        "name": "Line 1 cam",
        "type": "Camera",
        "origin": "edge-configured",
        "params": {"devicePath": "/dev/video0", "cameraId": "cam-1"},
        "capabilities": {
            "formats": [],
            "driver": "uvcvideo",
            "busInfo": "usb-1",
            "kind": "v4l2",
        },
        "discovered": True,
        "absent": False,
        "ack": "chg-1",
    }
    assert document == {
        "schemaVersion": 1,
        "reportedAt": 1700000000123,
        "cameras": {
            "cfg-is-1": dict(configured_entry),
            "disc-000000000002": {
                "version": 2,
                "name": "Gone Cam",
                "type": "V4L2Discovered",
                "origin": "edge-discovered",
                "params": {"devicePath": "/dev/video2"},
                "capabilities": {
                    "formats": [],
                    "driver": "uvcvideo",
                    "busInfo": "usb-2",
                    "kind": "v4l2",
                },
                "discovered": True,
                "absent": True,
                "absentSince": 1729990000000,
            },
            "new-cam-1": dict(configured_entry),
        },
        "failures": {"cfg-is-7": {"changeId": "chg-7", "reason": "boom"}},
        "discoveryErrors": [
            {"devicePath": "/dev/video5", "error": "open failed"}
        ],
    }, json.dumps(document, indent=2, sort_keys=True)
    assert SCHEMA_VERSION == 1
    assert STATIC_IMAGE_CAMERA_ID not in document["cameras"]


class _StubPinWorker:
    """StaticImagePinWorker double — the agent only uses these hooks
    (``test_pin_agent_wiring._RecordingPinWorker`` conventions)."""

    def __init__(self, marker=None):
        self.marker = marker
        self.desired_docs = []
        self.report_inventory = None

    def on_desired(self, desired):
        self.desired_docs.append(dict(desired))

    def applied_request_id(self):
        return None

    def applied_marker(self):
        return self.marker

    def start(self):
        pass

    def stop(self):
        pass


class _EmptyImageSourceAccessor:
    def list_image_sources(self, request, session):
        return []


class _FakeStaticStore:
    """``utils.static_image_camera.get_store()`` double with a togglable
    pin state (the agent only calls ``status()``)."""

    def __init__(self, pinned=False, metadata=None):
        self.pinned = pinned
        self.metadata = metadata

    def status(self):
        return {
            "pinned": self.pinned,
            "cameraId": STATIC_IMAGE_CAMERA_ID,
            "metadata": self.metadata,
        }


class _FixedDiscovery:
    """Camera_Discovery double exposing one tracked snapshot."""

    def __init__(self, snapshot):
        self.latest_snapshot = snapshot


def test_agent_report_write_unchanged_without_a_static_camera():
    """# Property 4: Preservation — the agent's report write

    **Validates: Requirement 3.19**

    Recorded on unfixed code: with nothing pinned and no static camera
    ever reported, the agent writes the full inventory under
    ``reported`` with unchanged ``schemaVersion`` / ``reportedAt`` /
    version-counter / ``failures`` / ``discoveryErrors`` semantics, no
    static camera key at all, and no version churn across repeated
    reports.
    """
    camera = DiscoveredCamera(
        stable_id="disc-000000000001",
        device_path="/dev/video0",
        card_name="Fake Cam",
        bus_info="usb-1",
        driver="uvcvideo",
        kind="v4l2",
        formats=[],
    )
    snapshot = InventorySnapshot(
        cameras={camera.stable_id: TrackedCamera(camera=camera)},
        failures=({"device_path": "/dev/video5", "error": "open failed"},),
    )
    shadow = FakeShadowAccessor(get_state={"desired": {}, "reported": {}})

    with tempfile.TemporaryDirectory(prefix="static-dedup-report-") as tmp_dir:
        with mock.patch.object(
            agent_module, "get_store", lambda: _FakeStaticStore(pinned=False)
        ):
            agent = EdgeSyncAgent(
                iot_shadow_accessor=shadow,
                image_source_accessor=_EmptyImageSourceAccessor(),
                camera_discovery=_FixedDiscovery(snapshot),
                db_session_factory=lambda: contextlib.nullcontext(),
                state_store=CameraSyncStateStore(
                    os.path.join(tmp_dir, "camera_sync_state.json")
                ),
                thing_name="jetson-thor1",
                pin_worker=_StubPinWorker(),
                wall_clock=lambda: 1_700_000_000.5,
            )
            agent._refresh_reported_versions()
            assert agent._write_report() is True
            assert agent._write_report() is True

    expected = {
        "reported": {
            "schemaVersion": 1,
            "reportedAt": 1700000000500,
            "cameras": {
                "disc-000000000001": {
                    "version": 1,
                    "name": "Fake Cam",
                    "type": "V4L2Discovered",
                    "origin": "edge-discovered",
                    "params": {"devicePath": "/dev/video0"},
                    "capabilities": {
                        "formats": [],
                        "driver": "uvcvideo",
                        "busInfo": "usb-1",
                        "kind": "v4l2",
                    },
                    "discovered": True,
                    "absent": False,
                }
            },
            "failures": {},
            "discoveryErrors": [
                {"devicePath": "/dev/video5", "error": "open failed"}
            ],
        }
    }
    assert shadow.writes == [expected, expected], json.dumps(
        shadow.writes, indent=2, sort_keys=True
    )
    for write in shadow.writes:
        assert STATIC_IMAGE_CAMERA_ID not in write["reported"]["cameras"]
        assert static_image_aravis_stable_id() not in (
            write["reported"]["cameras"]
        )
