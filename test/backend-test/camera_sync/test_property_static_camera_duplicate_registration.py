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
"""Bug condition exploration for the DUPLICATE Static_Image_Camera
registration (feature static-image-camera-binding-and-pin-discoverability,
Defect 3).

# Property 3: Bug Condition — one virtual camera, one registration

**Validates: Requirements 2.7, 2.8, 2.9, 2.10, 2.11**

**These tests MUST FAIL on the unfixed code.** The failure is the proof
that the Static_Image_Camera is registered TWICE in the Camera_Registry:

- ``getCameras()`` appends ``Camera(**STATIC_IMAGE_CAMERA_IDENTITY)`` to
  the Aravis bus enumeration while a Pinned_Image exists (base spec
  static-image-camera-source, Requirements 1.2, 2.1-2.3, 2.5-2.7), so
  ``enumerate_aravis()`` / ``_map_camera()`` derive the stable id
  ``arv-6c84191b7fe6`` for it and ``build_inventory`` reports it as an
  ``AravisDiscovered`` entry carrying ``params.cameraId:
  "static-image-camera"`` (bugfix.md 1.7).
- ``build_inventory`` ALSO appends its own dedicated entry under
  ``STATIC_IMAGE_CAMERA_ID`` (cloud-static-camera-provisioning
  Requirement 6.1) with nothing excluding the aravis-enumerated one
  (bugfix.md 1.8).

Two entries, two absence lifecycles, two picker options for one virtual
camera — live on ``jetson-thor1`` (bugfix.md 1.9, 1.10).

The fourth part of the bug condition is the migration half: a device that
has ALREADY published ``arv-6c84191b7fe6`` cannot converge by omission,
because AWS IoT shadow updates MERGE nested maps — the key stays alive in
the shadow document, every documents event keeps carrying it, and the
Portal's missing-from-report deletion path never fires (bugfix.md 1.11).
The report must retire the key explicitly ONCE with a ``null`` value
(Requirement 2.9).

Conventions follow the sibling device suites: ``hypothesis`` (not
fast-check) with the profiles registered in the root conftest (``fast`` =
25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100), the
``DiscoveredAravisCamera`` / ``InventorySnapshot`` / ``TrackedCamera``
fixtures of ``test_build_inventory_aravis.py``, the generators of
``test_property_pin_inventory.py``, and the ``FakeShadowAccessor`` agent
wiring of ``test_pin_agent_wiring.py`` / ``pin_worker_support.py``.

The discovery input is built the way ``getCameras()`` really produces it:
a REAL ``model.Camera`` carrying ``STATIC_IMAGE_CAMERA_IDENTITY`` fed
through the REAL ``enumerate_aravis(enumerator=...)``, so the ``arv-``
stable id comes from the shipped derivation rather than from a literal
(Requirement 2.11).
"""
import contextlib
import os
import tempfile
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

import camera_sync.agent as agent_module
from camera_discovery import (
    DiscoveredCamera,
    DiscoveryResult,
    InventorySnapshot,
    TrackedCamera,
    aravis_stable_id,
    enumerate_aravis,
)
from camera_sync import (
    ORIGIN_EDGE_DISCOVERED,
    STATIC_IMAGE_CAMERA_NAME,
    TYPE_ARAVIS_DISCOVERED,
    TYPE_STATIC_IMAGE,
    CameraSyncStateStore,
    EdgeSyncAgent,
    build_inventory,
)
from model.Camera import Camera
from utils.static_image_camera import (
    STATIC_IMAGE_CAMERA_ID,
    STATIC_IMAGE_CAMERA_IDENTITY,
)

from pin_worker_support import FakeShadowAccessor

#: The duplicate's id in the LIVE ``dda-camera-registry`` shadow for
#: ``jetson-thor1`` (``GET /devices/jetson-thor1/cameras``). Pinned here
#: so a future change to the shipped enumeration identity is visible
#: rather than silent (Requirement 2.11).
LIVE_DUPLICATE_ID = "arv-6c84191b7fe6"

#: The live ``absentSince`` both duplicated rows carried while nothing was
#: pinned (bugfix.md 1.10).
LIVE_ABSENT_SINCE = 1788839397466


def static_image_aravis_stable_id():
    """The retired duplicate's key, DERIVED from the shipped identity
    through the real ``aravis_stable_id`` (Requirement 2.11)."""
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


def count_static_registrations(entries):
    """Registrations of the ONE virtual camera, counted by the id a node
    binds to rather than by entry id — this is what makes the duplicate
    visible (the existing inventory-presence property counts only
    ``camera_source_id == STATIC_IMAGE_CAMERA_ID``, so it never saw it)."""
    return len(static_registrations(entries))


def static_registrations(entries):
    return [
        entry
        for entry in entries
        if entry.camera_source_id == STATIC_IMAGE_CAMERA_ID
        or entry.params.get("cameraId") == STATIC_IMAGE_CAMERA_ID
    ]


def without_static_registrations(entries):
    static = set(id(entry) for entry in static_registrations(entries))
    return [entry for entry in entries if id(entry) not in static]


def describe(entries):
    """Readable counterexample rendering for assertion messages."""
    return [
        (
            entry.camera_source_id,
            entry.type,
            entry.absent,
            dict(entry.params),
        )
        for entry in entries
    ]


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
#: ``static-image-camera``: the bug condition covers the static bus camera
#: that no configured Image_Source references, because a user's explicitly
#: configured source referencing it keeps merging exactly as today
#: (Requirement 3.18, task 8.1).
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
    st.just({"width": 4, "height": 4, "format": "PNG",
             "fileName": "sample.png"}),
)


@st.composite
def _v4l2_cameras(draw):
    """Arbitrary physical V4L2 cameras (pin-inventory conventions)."""
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


def _bus_identity_key(identity):
    """Uniqueness key: the stable id an identity tuple derives."""
    camera_id, vendor, model, serial, physical_id = identity
    return aravis_stable_id(vendor, model, serial, physical_id)


@st.composite
def _physical_bus_cameras(draw):
    """Arbitrary PHYSICAL Aravis bus cameras, in the ``model.Camera``
    shape ``getCameras()`` returns.

    Identities are unique by derived stable id and can never collide with
    the static camera's id or identity, so the only static-claiming camera
    in the discovery input is the synthetic one under test.
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
        Camera(
            id=camera_id,
            model=model,
            address=draw(_TEXT),
            physical_id=physical_id,
            protocol=draw(st.sampled_from(["GigEVision", "USB3Vision"])),
            serial=serial,
            vendor=vendor,
        )
        for camera_id, (vendor, model, serial, physical_id) in zip(
            camera_ids, identities
        )
    ]


@st.composite
def _image_sources(draw):
    """Arbitrary configured Image_Sources — device-path backed, cameraId
    backed, and pathless — none of them referencing the static camera."""
    device_paths = draw(st.lists(_DEVICE_PATHS, unique=True, max_size=2))
    pathless_count = draw(st.integers(min_value=0, max_value=1))
    devices = list(device_paths) + [None] * pathless_count
    sources = []
    for index, device in enumerate(devices):
        configuration = {}
        if device is not None:
            configuration["device"] = device
        if draw(st.booleans()):
            configuration["gain"] = draw(st.integers(min_value=0,
                                                     max_value=100))
        sources.append(
            {
                "imageSourceId": "is-{}".format(index),
                "name": draw(_TEXT),
                "type": draw(st.sampled_from(["Camera", "ICam", "Folder"])),
                "cameraId": draw(_CAMERA_IDS),
                "imageSourceConfiguration": configuration,
            }
        )
    return sources


# --- discovery-input builders --------------------------------------------------


def _enumerated(bus_cameras):
    """The REAL Aravis discovery mapping over a bus enumeration."""
    result = enumerate_aravis(enumerator=lambda: list(bus_cameras))
    assert result.failures == [], (
        "the bus enumeration must map cleanly; got failures: {}".format(
            result.failures
        )
    )
    return result.cameras


def _is_static(camera):
    return getattr(camera, "camera_id", None) == STATIC_IMAGE_CAMERA_ID


def _discovery_input(cameras, snapshot, static_absent, tracker_absent_since):
    """Wrap discovered cameras as a fresh ``DiscoveryResult`` or as a
    tracked ``InventorySnapshot``; in the snapshot form the static camera
    carries the absent-leftover state the tracker really holds after an
    unpin."""
    if not snapshot:
        return DiscoveryResult(cameras=list(cameras))
    tracked = {}
    for camera in cameras:
        absent = bool(static_absent) if _is_static(camera) else False
        tracked[camera.stable_id] = TrackedCamera(
            camera=camera,
            absent=absent,
            absent_since=tracker_absent_since if absent else None,
        )
    return InventorySnapshot(cameras=tracked)


# --- part 3: exactly one registration in every pin state ----------------------


def test_derived_static_image_aravis_stable_id_matches_live_registry():
    """The retired key, recomputed from the shipped constants, is the id
    the live registry holds (Requirement 2.11).

    Supporting pin, not a bug-condition assertion: it holds before and
    after the fix, and turns a future identity change into a visible test
    failure instead of a silent un-match.
    """
    assert static_image_aravis_stable_id() == LIVE_DUPLICATE_ID

    (discovered,) = _enumerated([static_bus_camera()])
    assert discovered.stable_id == LIVE_DUPLICATE_ID
    assert discovered.camera_id == STATIC_IMAGE_CAMERA_ID
    assert discovered.vendor == "AWS-DDA"
    assert discovered.model == "Static Image Camera"
    assert discovered.serial == "STATIC-IMAGE-0"


@settings(deadline=None)
@given(
    sources=_image_sources(),
    v4l2=_v4l2_cameras(),
    physical=_physical_bus_cameras(),
    pinned=st.booleans(),
    fresh_pass=st.booleans(),
    metadata=_PIN_METADATA,
    absent_since=st.one_of(
        st.none(),
        st.integers(min_value=0, max_value=2_000_000_000_000),
    ),
    tracker_absent_since=st.integers(min_value=0,
                                     max_value=2_000_000_000_000),
)
def test_static_image_camera_is_registered_exactly_once(
    sources,
    v4l2,
    physical,
    pinned,
    fresh_pass,
    metadata,
    absent_since,
    tracker_absent_since,
):
    """# Property 3: Bug Condition — one virtual camera, one registration

    **Validates: Requirements 2.7, 2.8, 2.11**

    The discovery input is what ``getCameras()`` really feeds Camera
    Discovery: arbitrary physical V4L2 and Aravis cameras plus the
    synthetic static bus camera, mapped by the real ``enumerate_aravis``.
    Merged with arbitrary configured Image_Sources, ``build_inventory``
    must report EXACTLY ONE registration for the one virtual camera —
    present while pinned, explicitly absent once unpinned after having
    been reported, none when never reported — and NO entry under the
    derived ``arv-`` id in any state.

    Unfixed code reports two.
    """
    # After an unpin the static camera has left the bus, so the only way
    # it is still in the discovery input is as the tracker's absent
    # leftover — which is precisely the state that duplicates.
    snapshot = True if not pinned else fresh_pass
    cameras = list(v4l2) + _enumerated(list(physical) + [static_bus_camera()])
    discovery = _discovery_input(
        cameras, snapshot, not pinned, tracker_absent_since
    )

    entries = build_inventory(
        sources,
        discovery,
        static_image_pinned=pinned,
        static_image_metadata=metadata,
        static_image_absent_since=absent_since,
    )

    expected = 1 if pinned else (1 if absent_since is not None else 0)
    registrations = static_registrations(entries)
    assert len(registrations) == expected, (
        "one virtual Static_Image_Camera must yield exactly {} "
        "registration(s) (pinned={}, absentSince={}); got {}: {}".format(
            expected, pinned, absent_since, len(registrations),
            describe(registrations),
        )
    )
    assert not [
        entry
        for entry in entries
        if entry.camera_source_id == static_image_aravis_stable_id()
    ], (
        "no entry may be reported under the derived aravis id {}; got "
        "{}".format(static_image_aravis_stable_id(), describe(entries))
    )

    if expected:
        (registration,) = registrations
        # The surviving registration is the dedicated entry — the one
        # carrying the pin metadata and the explicit-absence lifecycle
        # the derived arv- entry cannot (Requirement 2.7).
        assert registration.camera_source_id == STATIC_IMAGE_CAMERA_ID
        assert registration.name == STATIC_IMAGE_CAMERA_NAME
        assert registration.type == TYPE_STATIC_IMAGE
        assert registration.origin == ORIGIN_EDGE_DISCOVERED
        assert registration.params == {}
        assert registration.capabilities["staticImage"]["id"] == (
            STATIC_IMAGE_CAMERA_ID
        )
        if pinned:
            assert registration.absent is False
        else:
            assert registration.absent is True
            assert registration.absent_since == absent_since

    # Everything that is not a static-camera registration is byte-for-byte
    # the pre-fix merge output over the same inputs with the static bus
    # camera removed (Requirements 3.15, 3.18 stay intact).
    oracle = build_inventory(
        sources,
        _discovery_input(
            [camera for camera in cameras if not _is_static(camera)],
            snapshot,
            not pinned,
            tracker_absent_since,
        ),
        static_image_pinned=pinned,
        static_image_metadata=metadata,
        static_image_absent_since=absent_since,
    )
    assert without_static_registrations(entries) == (
        without_static_registrations(oracle)
    ), "non-static entries must be identical to the pre-fix merge output"


def test_live_jetson_thor1_duplicate_pair_is_a_single_registration():
    """The concrete live counterexample (bugfix.md 1.9, 1.10).

    **Validates: Requirements 2.7, 2.8, 2.10**

    ``GET /devices/jetson-thor1/cameras`` returns 9 cameras, two of which
    are the same virtual camera: ``arv-6c84191b7fe6``
    (``AravisDiscovered``, ``AWS-DDA Static Image Camera``,
    ``params.cameraId: "static-image-camera"``) and
    ``static-image-camera`` (``StaticImage``, ``params: {}``) — both
    absent while nothing is pinned.
    """
    (static_discovered,) = _enumerated([static_bus_camera()])
    snapshot = InventorySnapshot(
        cameras={
            static_discovered.stable_id: TrackedCamera(
                camera=static_discovered,
                absent=True,
                absent_since=LIVE_ABSENT_SINCE,
            )
        }
    )

    entries = build_inventory(
        [],
        snapshot,
        static_image_pinned=False,
        static_image_absent_since=LIVE_ABSENT_SINCE,
    )

    assert describe(static_registrations(entries)) == [
        (STATIC_IMAGE_CAMERA_ID, TYPE_STATIC_IMAGE, True, {}),
    ], (
        "the live jetson-thor1 pair must collapse to the single dedicated "
        "registration; got {}".format(describe(entries))
    )


def test_live_jetson_thor1_pinned_merge_is_a_single_registration():
    """The same live pair in the PINNED state (bugfix.md 1.7, 1.8).

    **Validates: Requirements 2.7, 2.8**

    While a Pinned_Image exists the static camera enumerates on the bus,
    so the merge sees the derived ``AravisDiscovered`` entry AND appends
    its own dedicated entry.
    """
    static_discovered, = _enumerated([static_bus_camera()])
    entries = build_inventory(
        [],
        DiscoveryResult(cameras=[static_discovered]),
        static_image_pinned=True,
        static_image_metadata={"width": 640, "height": 480,
                               "format": "PNG", "fileName": "target.png"},
    )

    duplicates = [
        entry for entry in entries
        if entry.camera_source_id == LIVE_DUPLICATE_ID
    ]
    assert duplicates == [], (
        "the aravis-enumerated static camera must be excluded from the "
        "reported discovery entries; got {}".format(describe(duplicates))
    )
    assert describe(static_registrations(entries)) == [
        (STATIC_IMAGE_CAMERA_ID, TYPE_STATIC_IMAGE, False, {}),
    ], "got {}".format(describe(entries))
    # Sanity on the shape the duplicate WOULD have carried, so the
    # counterexample above is unambiguous when it fires.
    assert TYPE_ARAVIS_DISCOVERED == "AravisDiscovered"


# --- part 4: the already-published duplicate is retired exactly once ----------


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


def _published_reported_state(already_published):
    """The device's current shadow reported state.

    ``already_published`` mirrors ``jetson-thor1``: BOTH rows live in the
    shadow document, the derived ``arv-`` duplicate among them. Otherwise
    only the dedicated entry was ever published (a device that never
    reported the duplicate must not receive a retirement write at all).
    """
    cameras = {
        STATIC_IMAGE_CAMERA_ID: {
            "version": 6,
            "name": STATIC_IMAGE_CAMERA_NAME,
            "type": TYPE_STATIC_IMAGE,
            "origin": ORIGIN_EDGE_DISCOVERED,
            "params": {},
            "capabilities": {"staticImage": {"id": STATIC_IMAGE_CAMERA_ID}},
            "discovered": True,
            "absent": True,
            "absentSince": LIVE_ABSENT_SINCE,
        }
    }
    if already_published:
        cameras[LIVE_DUPLICATE_ID] = {
            "version": 4,
            "name": "AWS-DDA Static Image Camera",
            "type": TYPE_ARAVIS_DISCOVERED,
            "origin": ORIGIN_EDGE_DISCOVERED,
            "params": {
                "cameraId": STATIC_IMAGE_CAMERA_ID,
                "serial": "STATIC-IMAGE-0",
                "protocol": "StaticImage",
                "address": "internal",
            },
            "capabilities": {"aravis": {"vendor": "AWS-DDA"}},
            "discovered": True,
            "absent": True,
            "absentSince": LIVE_ABSENT_SINCE,
        }
    return {"desired": {}, "reported": {"schemaVersion": 1,
                                        "cameras": cameras}}


def _make_agent(state_dir, shadow):
    return EdgeSyncAgent(
        iot_shadow_accessor=shadow,
        image_source_accessor=_EmptyImageSourceAccessor(),
        camera_discovery=None,
        db_session_factory=lambda: contextlib.nullcontext(),
        state_store=CameraSyncStateStore(
            os.path.join(state_dir, "camera_sync_state.json")
        ),
        thing_name="jetson-thor1",
        pin_worker=_StubPinWorker(),
    )


def _reported_cameras(write):
    reported = write.get("reported") if isinstance(write, dict) else None
    cameras = reported.get("cameras") if isinstance(reported, dict) else None
    return cameras if isinstance(cameras, dict) else {}


def _carries_retirement(write, key):
    """An explicit shadow deletion: the key present in the reported
    cameras map with a ``null`` value. Nested-map merge semantics make
    this the only way to remove an already-published key."""
    cameras = _reported_cameras(write)
    return key in cameras and cameras[key] is None


@settings(deadline=None)
@given(
    already_published=st.booleans(),
    pinned=st.booleans(),
    report_count=st.integers(min_value=1, max_value=4),
)
def test_published_duplicate_key_is_retired_exactly_once(
    already_published, pinned, report_count
):
    """# Property 3: Bug Condition, part 4 — one-shot retirement

    **Validates: Requirements 2.9, 2.10, 2.11**

    A device that already published ``arv-6c84191b7fe6`` cannot converge
    by omission: shadow updates MERGE nested maps, so the key stays alive
    in the shadow document and the Portal's missing-from-report deletion
    path never fires (bugfix.md 1.11). The next report must therefore
    carry the derived key explicitly with a ``null`` value — exactly once
    across any number of reports, never churning. A device that never
    published it must receive no retirement write at all.

    Unfixed code never writes the key at all.
    """
    key = static_image_aravis_stable_id()
    shadow = FakeShadowAccessor(
        get_state=_published_reported_state(already_published)
    )
    store = _FakeStaticStore(
        pinned=pinned,
        metadata={"width": 4, "height": 4} if pinned else None,
    )
    with tempfile.TemporaryDirectory(prefix="static-dup-retire-") as tmp_dir:
        with mock.patch.object(agent_module, "get_store", lambda: store):
            agent = _make_agent(tmp_dir, shadow)
            agent._refresh_reported_versions()
            for _ in range(report_count):
                assert agent._write_report() is True

    retirements = [
        index
        for index, write in enumerate(shadow.writes)
        if _carries_retirement(write, key)
    ]
    expected = 1 if already_published else 0
    assert len(retirements) == expected, (
        "the already-published duplicate {} must be retired exactly {} "
        "time(s) across {} report(s) (alreadyPublished={}); retirement "
        "writes: {}; reported camera keys per write: {}".format(
            key, expected, report_count, already_published, retirements,
            [sorted(_reported_cameras(w)) for w in shadow.writes],
        )
    )
    # The retirement is a deletion, never a resurrection: the key must
    # never be written with a live entry value.
    live = [
        _reported_cameras(write)[key]
        for write in shadow.writes
        if key in _reported_cameras(write)
        and _reported_cameras(write)[key] is not None
    ]
    assert live == [], (
        "the derived duplicate key must never be reported as a live "
        "entry; got {}".format(live)
    )
