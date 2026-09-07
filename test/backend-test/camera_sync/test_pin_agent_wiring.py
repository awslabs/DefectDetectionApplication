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
"""Agent wiring and headroom example tests (feature:
cloud-static-camera-provisioning, task 7.5).

- ``on_delta`` routing: ``state.staticImagePin`` reaches the owned pin
  worker; ``state.changes`` routing is unchanged (Requirement 5.4 wiring).
- Startup catch-up handoff: ``start()``'s shadow GET hands an unprocessed
  ``desired.staticImagePin`` to the worker; an already-applied requestId
  (marker match) is skipped (Requirement 5.2 wiring).
- Marker corruption recovery: a corrupt idempotence marker is treated as
  no marker — the request re-applies safely (Requirement 3.5).
- Report-size headroom: a full camera report plus both ``staticImagePin``
  sections stays at or below the 8 KB shadow document limit with
  ``MAX_REPORT_BYTES = 6144`` (Requirement 2.3).

_Requirements: 5.2, 3.5, 2.3_
"""
import contextlib
import json
import tempfile

import pytest

from camera_discovery import DiscoveredCamera, DiscoveryResult
from camera_sync import (
    MAX_REPORT_BYTES,
    CameraSyncStateStore,
    CameraSourceState,
    EdgeSyncAgent,
    build_report_document,
)
from camera_sync.inventory import ORIGIN_EDGE_CONFIGURED
from utils.static_image_camera import StaticImageStore

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

# --- fakes ---------------------------------------------------------------------


class _RecordingPinWorker:
    """StaticImagePinWorker double recording the agent's wiring calls."""

    def __init__(self, applied_id=None, marker=None):
        self.desired_docs = []
        self.started = 0
        self.stopped = 0
        self._applied_id = applied_id
        self.marker = marker
        self.report_inventory = None

    def on_desired(self, desired):
        self.desired_docs.append(dict(desired))

    def applied_request_id(self):
        return self._applied_id

    def applied_marker(self):
        return self.marker

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class _FakeShadow:
    """IoTShadowAccessor double with a configurable GET state."""

    def __init__(self, state=None):
        self.state = state
        self.writes = []

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        return self.state

    def update_thing_shadow_state_request(self, thing_name, shadow_name, state):
        self.writes.append(state)


class _FakeImageSourceAccessor:
    def list_image_sources(self, request, session):
        return []


def _make_agent(tmp_path, shadow, pin_worker, wall_clock=None):
    kwargs = {}
    if wall_clock is not None:
        kwargs["wall_clock"] = wall_clock
    return EdgeSyncAgent(
        iot_shadow_accessor=shadow,
        image_source_accessor=_FakeImageSourceAccessor(),
        camera_discovery=None,
        db_session_factory=lambda: contextlib.nullcontext(),
        state_store=CameraSyncStateStore(str(tmp_path / "camera_sync_state.json")),
        thing_name="test-thing",
        pin_worker=pin_worker,
        **kwargs,
    )


_PIN_DOC = {
    "requestId": "req-wire-1",
    "op": "remove",
    "requestedAtEpochMs": 1_730_000_000_000,
}


# --- on_delta routing ------------------------------------------------------------


def test_on_delta_routes_static_image_pin_to_worker(tmp_path):
    """``state.staticImagePin`` goes to the worker; the camera apply path
    sees nothing when the delta carries no ``changes``."""
    worker = _RecordingPinWorker()
    agent = _make_agent(tmp_path, _FakeShadow(), worker)
    applied = []
    agent.apply_desired_changes = lambda changes: applied.append(changes)

    agent.on_delta({"state": {"staticImagePin": dict(_PIN_DOC)}})

    assert worker.desired_docs == [_PIN_DOC]
    assert applied == []


def test_on_delta_changes_routing_unchanged(tmp_path):
    """A delta carrying both sections routes each to its own path; a
    changes-only delta never touches the worker."""
    worker = _RecordingPinWorker()
    agent = _make_agent(tmp_path, _FakeShadow(), worker)
    applied = []
    agent.apply_desired_changes = lambda changes: applied.append(changes)

    changes = {"cfg-is-1": {"op": "update", "name": "renamed"}}
    agent.on_delta(
        {"state": {"staticImagePin": dict(_PIN_DOC), "changes": changes}}
    )
    assert worker.desired_docs == [_PIN_DOC]
    assert applied == [changes]

    worker2 = _RecordingPinWorker()
    agent2 = _make_agent(tmp_path, _FakeShadow(), worker2)
    applied2 = []
    agent2.apply_desired_changes = lambda c: applied2.append(c)
    agent2.on_delta({"state": {"changes": changes}})
    assert worker2.desired_docs == []
    assert applied2 == [changes]


def test_pin_worker_report_inventory_wired_to_agent(tmp_path):
    """The agent wires its inventory-report trigger into the worker so
    terminal pin outcomes publish the camera inventory promptly."""
    worker = _RecordingPinWorker()
    agent = _make_agent(tmp_path, _FakeShadow(), worker)
    assert worker.report_inventory == agent.report_inventory


# --- startup catch-up (Requirement 5.2 wiring) -----------------------------------


def test_startup_hands_unprocessed_desired_pin_to_worker(tmp_path):
    """``start()``'s shadow GET hands ``desired.staticImagePin`` to the
    worker when its requestId differs from the marker, and owns the
    worker's start/stop lifecycle."""
    shadow = _FakeShadow(
        state={"desired": {"staticImagePin": dict(_PIN_DOC)}, "reported": {}}
    )
    worker = _RecordingPinWorker(applied_id="req-some-older")
    agent = _make_agent(tmp_path, shadow, worker)
    try:
        agent.start()
        assert worker.desired_docs == [_PIN_DOC]
        assert worker.started == 1
    finally:
        agent.stop()
    assert worker.stopped == 1


def test_startup_skips_already_applied_request(tmp_path):
    """A desired document whose requestId matches the marker is not handed
    over — the terminal outcome was already recorded."""
    shadow = _FakeShadow(
        state={"desired": {"staticImagePin": dict(_PIN_DOC)}, "reported": {}}
    )
    worker = _RecordingPinWorker(applied_id=_PIN_DOC["requestId"])
    agent = _make_agent(tmp_path, shadow, worker)
    try:
        agent.start()
        assert worker.desired_docs == []
    finally:
        agent.stop()


def test_startup_without_desired_pin_hands_nothing(tmp_path):
    """No desired pin section (or no shadow at all) — nothing reaches the
    worker."""
    for state in (None, {}, {"desired": {}}, {"desired": {"staticImagePin": {}}}):
        worker = _RecordingPinWorker()
        agent = _make_agent(tmp_path, _FakeShadow(state=state), worker)
        try:
            agent.start()
            assert worker.desired_docs == []
        finally:
            agent.stop()


# --- marker corruption recovery (Requirement 3.5) --------------------------------


@pytest.mark.parametrize(
    "corrupt_payload",
    [b"\x00\xffnot json", b"[]", b'{"noRequestId": true}'],
    ids=["binary-garbage", "wrong-type", "missing-requestId"],
)
def test_marker_corruption_treated_as_no_marker(corrupt_payload):
    """A corrupt or malformed marker never blocks processing: the request
    re-applies (safe by construction) and a fresh marker is written."""
    with tempfile.TemporaryDirectory(prefix="pin-marker-corrupt-") as tmp_dir:
        store_dir, marker_path = fresh_store_dirs(tmp_dir)
        with open(marker_path, "wb") as handle:
            handle.write(corrupt_payload)

        store = StaticImageStore(base_dir=store_dir)
        payload = render_image_bytes(4, 4, 7, "PNG")
        desired = pin_desired("req-corrupt-1", payload)
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
        assert store.is_pinned() is True
        # The fresh terminal outcome replaced the corrupt marker.
        with open(marker_path, "r", encoding="utf-8") as handle:
            marker = json.load(handle)
        assert marker["requestId"] == "req-corrupt-1"
        assert marker["status"] == "applied"


# --- report-size headroom (Requirement 2.3) --------------------------------------


def _encoded_size(document):
    return len(json.dumps(document, separators=(",", ":")).encode("utf-8"))


def _fat_inventory(count=12):
    """An inventory big enough to drive the truncation ladder."""
    formats = [
        {
            "pixelFormat": "FMT{}".format(i),
            "resolutions": [[3840 + i, 2160 + j] for j in range(8)],
        }
        for i in range(8)
    ]
    return [
        CameraSourceState(
            camera_source_id="cfg-is-{}".format(index),
            name="Inspection camera {}".format(index),
            type="Camera",
            origin=ORIGIN_EDGE_CONFIGURED,
            params={
                "devicePath": "/dev/video{}".format(index),
                "cameraId": "camera-{:04d}".format(index),
                "location": "line-{} station-{}".format(index, index),
            },
            capabilities={
                "formats": formats,
                "driver": "uvcvideo",
                "busInfo": "usb-0000:00:14.0-{}".format(index),
                "kind": "v4l2",
            },
            discovered=True,
        )
        for index in range(count)
    ]


def test_report_headroom_with_both_pin_sections():
    """A ladder-truncated full camera report plus a worst-case desired
    document AND its reported echo stays within the 8 KB shadow document
    limit, with ``MAX_REPORT_BYTES`` lowered to 6144."""
    assert MAX_REPORT_BYTES == 6 * 1024

    inventory = _fat_inventory()
    versions = {entry.camera_source_id: 7 for entry in inventory}
    report = build_report_document(
        inventory, versions, reported_at_ms=1_730_000_000_000
    )
    assert _encoded_size(report) <= MAX_REPORT_BYTES

    # Worst-case portal desired document: 128-char fileName (the portal's
    # truncation bound), long bucket/key, full-width fixed fields.
    long_file_name = "f" * 128
    desired = {
        "requestId": "20991231T235959-abcdef12",
        "op": "pin",
        "bucket": "dda-component-us-east-1-123456789012-partition-suffix",
        "key": "static-image-pins/edgeml-device-with-a-long-thing-name/"
        "20991231T235959-abcdef12",
        "sha256": "a" * 64,
        "sizeBytes": 52_428_800,
        "format": "JPEG",
        "fileName": long_file_name,
        "requestedAtEpochMs": 1_730_000_000_000,
    }
    echo = dict(desired)
    echo.update(
        {
            "status": "applied",
            "metadata": {
                "width": 65_535,
                "height": 65_535,
                "format": "JPEG",
                "fileName": long_file_name,
            },
            "completedAtEpochMs": 1_730_000_000_000,
        }
    )

    shadow_state = {
        "desired": {"staticImagePin": desired},
        "reported": {**report, "staticImagePin": echo},
    }
    assert _encoded_size(shadow_state) <= 8 * 1024


# --- unpinned-after-reported ABSENT reporting (Requirement 6.2 — second
# --- hardware finding: shadow updates MERGE nested maps, so an omitted
# --- camera key persists in the shadow document; the unpinned static
# --- camera must be reported explicitly absent, never merely omitted) ----


class _FakeStaticStore:
    """utils.static_image_camera.get_store() double with a togglable
    pin state (the agent only calls ``status()``)."""

    def __init__(self, pinned=False, metadata=None):
        self.pinned = pinned
        self.metadata = metadata

    def status(self):
        return {
            "pinned": self.pinned,
            "cameraId": "static-image-camera",
            "metadata": self.metadata,
        }


_REMOVE_MARKER = {
    "requestId": "req-remove-9",
    "op": "remove",
    "status": "applied",
    "metadata": None,
    "completedAtEpochMs": 1_730_000_777_000,
}


def _patch_store(monkeypatch, store):
    import camera_sync.agent as agent_module

    monkeypatch.setattr(agent_module, "get_store", lambda: store)


def _static_entry(document):
    return document["cameras"].get("static-image-camera")


def test_unpinned_never_reported_yields_no_static_entry(
        tmp_path, monkeypatch):
    """Never pinned, never reported: the report carries no static entry
    (nothing exists in the shadow for merge semantics to keep alive)."""
    _patch_store(monkeypatch, _FakeStaticStore(pinned=False))
    agent = _make_agent(tmp_path, _FakeShadow(state=None),
                        _RecordingPinWorker())
    agent._refresh_reported_versions()

    document = agent._build_current_document()

    assert _static_entry(document) is None


def test_unpin_after_runtime_report_yields_stable_absent_entry(
        tmp_path, monkeypatch):
    """Pin -> report -> unpin: the next reports carry the entry
    explicitly ABSENT with one stable absentSince (no timestamp churn,
    no version churn between absent reports), derived from the wall
    clock when no remove marker exists (device-initiated unpin).

    'Previously reported' is answered by the version state store here —
    the entry was first reported at runtime, after the start-time shadow
    GET, so the reported-versions floor alone would not know it."""
    store = _FakeStaticStore(pinned=True,
                             metadata={"width": 4, "height": 4})
    _patch_store(monkeypatch, store)
    wall = FakeClock(start=1_730_000_500.0)
    agent = _make_agent(tmp_path, _FakeShadow(state=None),
                        _RecordingPinWorker(), wall_clock=wall)
    agent._refresh_reported_versions()

    pinned_doc = agent._build_current_document()
    pinned_entry = _static_entry(pinned_doc)
    assert pinned_entry["absent"] is False

    store.pinned = False
    store.metadata = None
    absent_doc = agent._build_current_document()
    entry = _static_entry(absent_doc)
    assert entry is not None, (
        "the unpinned, previously reported static camera must be "
        "reported explicitly absent (Req 6.2)")
    assert entry["absent"] is True
    assert entry["absentSince"] == int(wall.now * 1000)
    assert entry["version"] == pinned_entry["version"] + 1
    # Identity retained; no pin metadata on the absent entry.
    assert entry["capabilities"]["staticImage"]["id"] == "static-image-camera"
    assert "width" not in entry["capabilities"]["staticImage"]

    # Stability: later reports reuse the same timestamp and version.
    wall.advance(3600.0)
    entry2 = _static_entry(agent._build_current_document())
    assert entry2["absentSince"] == entry["absentSince"]
    assert entry2["version"] == entry["version"]


def test_absent_since_uses_remove_marker_timestamp(tmp_path, monkeypatch):
    """A cloud-initiated removal's marker records the exact removal
    instant; the absent entry reports it as absentSince (stable across
    restarts) instead of the wall clock."""
    _patch_store(monkeypatch, _FakeStaticStore(pinned=False))
    shadow = _FakeShadow(state={
        "desired": {},
        "reported": {"cameras": {"static-image-camera": {
            "version": 3, "absent": False}}},
    })
    wall = FakeClock(start=1_730_000_900.0)
    agent = _make_agent(tmp_path, shadow,
                        _RecordingPinWorker(marker=dict(_REMOVE_MARKER)),
                        wall_clock=wall)
    agent._refresh_reported_versions()

    entry = _static_entry(agent._build_current_document())

    assert entry["absent"] is True
    assert entry["absentSince"] == _REMOVE_MARKER["completedAtEpochMs"]
    assert entry["version"] > 3  # absence transition version-bumps


def test_restart_adopts_absent_since_from_shadow(tmp_path, monkeypatch):
    """After a restart, an already-absent shadow entry seeds the
    timestamp: the agent re-reports the SAME absentSince rather than
    inventing a new one (no churn across restarts)."""
    _patch_store(monkeypatch, _FakeStaticStore(pinned=False))
    seeded_since = 1_730_000_600_000
    shadow = _FakeShadow(state={
        "desired": {},
        "reported": {"cameras": {"static-image-camera": {
            "version": 7, "absent": True, "absentSince": seeded_since}}},
    })
    wall = FakeClock(start=1_730_009_999.0)
    agent = _make_agent(tmp_path, shadow, _RecordingPinWorker(),
                        wall_clock=wall)
    agent._refresh_reported_versions()

    entry = _static_entry(agent._build_current_document())

    assert entry["absent"] is True
    assert entry["absentSince"] == seeded_since


def test_repin_restores_present_entry_and_resets_absence(
        tmp_path, monkeypatch):
    """Unpin -> re-pin -> unpin: the re-pin reports the present entry
    again (Req 6.1 restore) and closes the absence episode, so the next
    unpin derives a FRESH absentSince."""
    store = _FakeStaticStore(pinned=True, metadata={"format": "PNG"})
    _patch_store(monkeypatch, store)
    wall = FakeClock(start=1_730_001_000.0)
    agent = _make_agent(tmp_path, _FakeShadow(state=None),
                        _RecordingPinWorker(), wall_clock=wall)
    agent._refresh_reported_versions()
    agent._build_current_document()  # first pinned report

    store.pinned = False
    first_absent = _static_entry(agent._build_current_document())
    assert first_absent["absent"] is True
    first_since = first_absent["absentSince"]

    store.pinned = True
    repinned = _static_entry(agent._build_current_document())
    assert repinned["absent"] is False
    assert "absentSince" not in repinned

    wall.advance(120.0)
    store.pinned = False
    second_absent = _static_entry(agent._build_current_document())
    assert second_absent["absent"] is True
    assert second_absent["absentSince"] == int(wall.now * 1000)
    assert second_absent["absentSince"] != first_since
