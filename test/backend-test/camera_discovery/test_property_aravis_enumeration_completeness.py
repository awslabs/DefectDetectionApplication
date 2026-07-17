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
"""Property test for Aravis enumeration completeness with identity capture.

**Feature: aravis-camera-input, Property 2: Aravis discovery enumeration
completeness with identity capture**

*For any* set of enumerated Aravis cameras, every camera SHALL contribute
to exactly one inventory entry, and every discovered-only entry SHALL have
type ``AravisDiscovered``, origin ``edge-discovered``, and
parameters/capabilities carrying all of the camera's identity fields (id,
model, address, physical id, protocol, serial, vendor).

Exercised over generated fake Aravis buses (unicode names, duplicate
models, empty serials) at each layer that owns a piece of the property:

- :func:`camera_discovery.aravis.enumerate_aravis` and the real
  :class:`camera_discovery.CameraDiscovery` tracked snapshot guarantee the
  one-entry-per-camera contribution with all identity fields captured;
- the ``AravisDiscovered`` / ``edge-discovered`` typing lives in
  ``camera_sync.build_inventory`` (read-only import), asserted over the
  snapshot with no configured Image_Sources so every camera is a
  discovered-only entry.

**Validates: Requirements 2.1, 2.3**

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
from types import SimpleNamespace

from fake_v4l2 import FakeV4l2Io
from hypothesis import given
from hypothesis import strategies as st

from camera_discovery import CameraDiscovery
from camera_discovery.aravis import (
    DiscoveredAravisCamera,
    aravis_stable_id,
    enumerate_aravis,
)
from camera_sync.inventory import (
    ORIGIN_EDGE_DISCOVERED,
    TYPE_ARAVIS_DISCOVERED,
    build_inventory,
)

# --- generators --------------------------------------------------------------

# Full-unicode identity strings (hypothesis' default text alphabet excludes
# surrogates): real GenICam vendors/models are not ASCII-only.
_UNICODE_NAMES = st.text(min_size=1, max_size=24)
_SERIALS = st.text(max_size=24)  # may be empty (serial-less cameras)
_ADDRESSES = st.text(max_size=32)
_PROTOCOLS = st.sampled_from(["GigEVision", "USB3Vision", "Fake"])
_RUNTIME_IDS = st.text(min_size=1, max_size=32)


def _stable_id_key(identity):
    """Uniqueness key: the stable id an identity tuple derives."""
    vendor, model, serial, physical_id = identity
    return aravis_stable_id(vendor, model, serial, physical_id)


@st.composite
def _fake_buses(draw):
    """A fake Aravis bus: 1..6 cameras with unicode identity strings,
    models drawn from a small shared pool (forcing duplicate models),
    possibly empty serials, and unique runtime ids.

    Identities are unique by the stable id they derive, so every bus
    camera is a distinct tracked entry.
    """
    model_pool = draw(st.lists(_UNICODE_NAMES, min_size=1, max_size=2, unique=True))
    identities = draw(
        st.lists(
            st.tuples(
                _UNICODE_NAMES,               # vendor
                st.sampled_from(model_pool),  # model (duplicates likely)
                _SERIALS,                     # serial (may be empty)
                _UNICODE_NAMES,               # physical_id
            ),
            min_size=1,
            max_size=6,
            unique_by=_stable_id_key,
        )
    )
    runtime_ids = draw(
        st.lists(
            _RUNTIME_IDS,
            min_size=len(identities),
            max_size=len(identities),
            unique=True,
        )
    )
    return [
        SimpleNamespace(
            id=runtime_id,
            model=model,
            address=draw(_ADDRESSES),
            physical_id=physical_id,
            protocol=draw(_PROTOCOLS),
            serial=serial,
            vendor=vendor,
        )
        for runtime_id, (vendor, model, serial, physical_id) in zip(
            runtime_ids, identities
        )
    ]


def _identity_fields(raw):
    """The full Aravis identity of a fake bus camera, keyed by the
    DiscoveredAravisCamera field names."""
    return {
        "camera_id": raw.id,
        "model": raw.model,
        "address": raw.address,
        "physical_id": raw.physical_id,
        "protocol": raw.protocol,
        "serial": raw.serial,
        "vendor": raw.vendor,
    }


def _captured_fields(camera: DiscoveredAravisCamera):
    return {
        "camera_id": camera.camera_id,
        "model": camera.model,
        "address": camera.address,
        "physical_id": camera.physical_id,
        "protocol": camera.protocol,
        "serial": camera.serial,
        "vendor": camera.vendor,
    }


# --- properties ---------------------------------------------------------------


@given(bus=_fake_buses())
def test_every_camera_enumerated_exactly_once_with_all_identity_fields(bus):
    """**Feature: aravis-camera-input, Property 2: Aravis discovery
    enumeration completeness with identity capture**

    **Validates: Requirements 2.1, 2.3**
    """
    result = enumerate_aravis(lambda: list(bus))

    assert result.failures == []
    # Exactly one enumerated camera per bus camera.
    assert len(result.cameras) == len(bus)

    by_camera_id = {camera.camera_id: camera for camera in result.cameras}
    assert len(by_camera_id) == len(bus)

    for raw in bus:
        camera = by_camera_id[raw.id]
        # All identity fields captured verbatim (2.3).
        assert _captured_fields(camera) == _identity_fields(raw)
        assert camera.stable_id == aravis_stable_id(
            raw.vendor, raw.model, raw.serial, raw.physical_id
        )


@given(bus=_fake_buses())
def test_every_camera_contributes_exactly_one_tracked_snapshot_entry(bus):
    """**Feature: aravis-camera-input, Property 2: Aravis discovery
    enumeration completeness with identity capture**

    **Validates: Requirements 2.1, 2.3**
    """
    discovery = CameraDiscovery(
        v4l2_io=FakeV4l2Io({}), aravis_enumerator=lambda: list(bus)
    )
    snapshot = discovery.run_once()

    assert snapshot.failures == ()
    # Exactly one tracked entry per bus camera, keyed by its stable id.
    assert set(snapshot.cameras) == {
        aravis_stable_id(raw.vendor, raw.model, raw.serial, raw.physical_id)
        for raw in bus
    }
    assert len(snapshot.cameras) == len(bus)

    by_camera_id = {
        entry.camera.camera_id: entry for entry in snapshot.cameras.values()
    }
    for raw in bus:
        entry = by_camera_id[raw.id]
        assert isinstance(entry.camera, DiscoveredAravisCamera)
        assert _captured_fields(entry.camera) == _identity_fields(raw)
        assert entry.absent is False


@given(bus=_fake_buses())
def test_discovered_only_entries_carry_type_origin_and_identity(bus):
    """**Feature: aravis-camera-input, Property 2: Aravis discovery
    enumeration completeness with identity capture**

    **Validates: Requirements 2.1, 2.3**
    """
    discovery = CameraDiscovery(
        v4l2_io=FakeV4l2Io({}), aravis_enumerator=lambda: list(bus)
    )
    snapshot = discovery.run_once()

    # No configured Image_Sources: every camera is a discovered-only entry.
    entries = build_inventory([], snapshot)

    # Exactly one inventory entry per bus camera (2.1).
    assert len(entries) == len(bus)
    assert {entry.camera_source_id for entry in entries} == {
        aravis_stable_id(raw.vendor, raw.model, raw.serial, raw.physical_id)
        for raw in bus
    }

    by_camera_id = {entry.params["cameraId"]: entry for entry in entries}
    for raw in bus:
        entry = by_camera_id[raw.id]

        # Discovered-only typing (2.1).
        assert entry.type == TYPE_ARAVIS_DISCOVERED
        assert entry.origin == ORIGIN_EDGE_DISCOVERED
        assert entry.discovered is True

        # Parameters and capability metadata carry the full identity (2.3).
        assert entry.params == {
            "cameraId": raw.id,
            "serial": raw.serial,
            "protocol": raw.protocol,
            "address": raw.address,
        }
        assert entry.capabilities == {
            "aravis": {
                "model": raw.model,
                "address": raw.address,
                "physicalId": raw.physical_id,
                "protocol": raw.protocol,
                "serial": raw.serial,
                "vendor": raw.vendor,
            }
        }
