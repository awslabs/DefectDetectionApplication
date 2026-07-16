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
"""Unit tests for the Edge_Sync_Agent portal-change apply path (task 2.6).

Example-based tests with fakes covering ``on_delta``:

- each ``desired.changes[csid]`` is applied through the injected accessor
  (create/update/delete), preserving the accessors' contract (5.2, 11.3),
- success reports the applied state with the change's ``portal_change_id``
  echoed in ``ack`` (5.3); a create's placeholder csid is mirrored for one
  report so the Portal reducer can match its pending entry,
- ``ValidationError``/``HTTPException`` reports ``{csid, status: failed}``
  semantics via the ``failures`` map with the message verbatim (5.4),
- changes targeting origin ``edge-discovered`` (``disc-`` stable ids, or
  update/delete of anything that is not a ``cfg-`` configured source) are
  refused with reason ``discovery-managed`` (5.6),
- applied or failed desired entries are cleared by writing ``null``.

_Requirements: 5.2, 5.3, 5.4, 5.6, 11.3_

The agent is driven deterministically through :meth:`EdgeSyncAgent.pump`
with an injectable clock, like the other camera_sync suites.
"""
import contextlib
import itertools

from fastapi import HTTPException

from camera_discovery import DiscoveredCamera, DiscoveryResult
from camera_sync import (
    REASON_DISCOVERY_MANAGED,
    CameraSyncStateStore,
    EdgeSyncAgent,
    change_to_image_source_data,
)

# --- fakes -------------------------------------------------------------------


class _FakeClock:
    def __init__(self, start: float = 1_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeShadowAccessor:
    """Records reported documents and desired writes separately."""

    def __init__(self):
        self.reported_writes = []
        self.desired_writes = []

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        return None

    def update_thing_shadow_state_request(self, thing_name, shadow_name, state):
        if "reported" in state:
            self.reported_writes.append(state["reported"])
        if "desired" in state:
            self.desired_writes.append(state["desired"])


class _FakeImageSourceAccessor:
    """Mimics the real accessor's surface and error contract: CRUD against
    an in-memory source map, raising ``HTTPException`` with a descriptive
    detail exactly like ``ImageSourceAccessor`` does on validation
    failures and missing sources."""

    _ids = itertools.count(1)

    def __init__(self, sources=None):
        # {imageSourceId: source-dict shaped for build_inventory}
        self.sources = {s["imageSourceId"]: dict(s) for s in (sources or [])}
        self.calls = []

    def list_image_sources(self, request, session):
        return [dict(source) for source in self.sources.values()]

    def create_image_source(self, data, session):
        self.calls.append(("create", dict(data)))
        if data.get("type") == "Folder" and not data.get("location"):
            raise HTTPException(
                status_code=400,
                detail="The server can't create the image source. Error: "
                       "'location is required when image source type is "
                       "Folder'. Check the error message and try again.",
            )
        image_source_id = "is-new-{}".format(next(self._ids))
        source = dict(data)
        source["imageSourceId"] = image_source_id
        source.setdefault("imageSourceConfiguration", {})
        self.sources[image_source_id] = source
        return {"imageSourceId": image_source_id}

    def update_image_source(self, image_source_id, data, session):
        self.calls.append(("update", image_source_id, dict(data)))
        source = self.sources.get(image_source_id)
        if source is None:
            raise HTTPException(
                status_code=404,
                detail="The server can't find the image source. Error: 'The "
                       "image source {} doesn't exist'. Check the image "
                       "source ID and try again.".format(image_source_id),
            )
        data = dict(data)
        configuration = data.pop("imageSourceConfiguration", None)
        if configuration is not None:
            # The real accessor creates a fresh configuration row from
            # exactly the provided values.
            source["imageSourceConfiguration"] = dict(configuration)
        source.update(data)
        return {"imageSourceId": image_source_id}

    def delete_image_source(self, image_source_id, session):
        self.calls.append(("delete", image_source_id))
        if image_source_id not in self.sources:
            raise HTTPException(
                status_code=404,
                detail="The server can't delete the image source. Error: "
                       "'The image source {} doesn't exist'. Check the image "
                       "source ID and try again.".format(image_source_id),
            )
        del self.sources[image_source_id]
        return {"imageSourceId": image_source_id}


class _FakeDiscovery:
    def __init__(self, snapshot=None):
        self.latest_snapshot = snapshot if snapshot is not None else DiscoveryResult()


_CONFIGURED_SOURCE = {
    "imageSourceId": "is-1",
    "name": "Line 1 inspection cam",
    "type": "Camera",
    "cameraId": "cam-1",
    "imageSourceConfiguration": {"device": "/dev/video0", "gain": 4},
}

_DISCOVERED_CAMERA = DiscoveredCamera(
    stable_id="disc-000000000001",
    device_path="/dev/video1",
    card_name="USB Camera",
    bus_info="usb-0000:00:14.0-1",
    driver="uvcvideo",
    kind="v4l2",
    formats=[{"pixel_format": "YUYV", "resolutions": [[1920, 1080]]}],
)


def _make_agent(tmp_path, sources=(), snapshot=None):
    clock = _FakeClock()
    shadow = _FakeShadowAccessor()
    accessor = _FakeImageSourceAccessor(sources)
    agent = EdgeSyncAgent(
        iot_shadow_accessor=shadow,
        image_source_accessor=accessor,
        camera_discovery=_FakeDiscovery(snapshot),
        db_session_factory=lambda: contextlib.nullcontext(),
        state_store=CameraSyncStateStore(str(tmp_path / "state.json")),
        thing_name="test-thing",
        clock=clock,
        wall_clock=clock,
    )
    return agent, shadow, accessor, clock


def _flush(agent, clock, max_iterations=10):
    for _ in range(max_iterations):
        delay = agent.pump()
        if delay is None:
            return
        clock.advance(delay + 0.001)
    raise AssertionError("agent never went idle")


def _delta(changes):
    return {"state": {"changes": changes}, "version": 5}


# --- change -> Image_Source data mapping (Requirement 5.2) ---------------------


def test_change_params_map_back_to_accessor_data():
    """The reported params shape inverts to accessor-shaped data:
    devicePath -> configuration.device, camera settings into the
    configuration, identity fields top-level. _Requirements: 5.2_"""
    data = change_to_image_source_data({
        "op": "update",
        "name": "Cam A",
        "type": "Camera",
        "params": {
            "devicePath": "/dev/video2",
            "cameraId": "cam-9",
            "location": "/data/folder",
            "description": "north line",
            "gain": 8,
            "exposure": 1000,
            "deviceName": "usb-cam",
        },
    })
    assert data == {
        "name": "Cam A",
        "type": "Camera",
        "cameraId": "cam-9",
        "location": "/data/folder",
        "description": "north line",
        "imageSourceConfiguration": {
            "device": "/dev/video2",
            "gain": 8,
            "exposure": 1000,
            "deviceName": "usb-cam",
        },
    }


# --- update (Requirements 5.2, 5.3) --------------------------------------------


def test_update_applies_through_accessor_and_acks(tmp_path):
    """A desired update is applied via the accessor; the next report
    carries the applied state with the portal_change_id echoed in ack,
    and the desired entry is cleared with null.
    _Requirements: 5.2, 5.3, 11.3_"""
    agent, shadow, accessor, clock = _make_agent(
        tmp_path, sources=[_CONFIGURED_SOURCE]
    )

    agent.on_delta(_delta({
        "cfg-is-1": {
            "op": "update",
            "portalChangeId": "pc-123",
            "baseVersion": 1,
            "name": "Renamed cam",
            "type": "Camera",
            "params": {"devicePath": "/dev/video7", "cameraId": "cam-1",
                       "gain": 12},
        },
    }))
    _flush(agent, clock)

    # Applied through the accessor (schema/side-effect path preserved).
    kinds = [call[0] for call in accessor.calls]
    assert kinds == ["update"]
    assert accessor.sources["is-1"]["name"] == "Renamed cam"
    assert accessor.sources["is-1"]["imageSourceConfiguration"] == {
        "device": "/dev/video7", "gain": 12,
    }

    # The applied state is reported with the ack echoed (5.3).
    assert shadow.reported_writes
    document = shadow.reported_writes[-1]
    entry = document["cameras"]["cfg-is-1"]
    assert entry["ack"] == "pc-123"
    assert entry["name"] == "Renamed cam"
    assert entry["params"]["devicePath"] == "/dev/video7"
    assert entry["params"]["gain"] == 12
    assert document["failures"] == {}

    # The desired entry was cleared by writing null.
    assert shadow.desired_writes == [{"changes": {"cfg-is-1": None}}]

    # The ack is one-shot: a later report no longer carries it.
    agent.report_inventory()
    _flush(agent, clock)
    assert "ack" not in shadow.reported_writes[-1]["cameras"]["cfg-is-1"]


# --- create (Requirements 5.2, 5.3) --------------------------------------------


def test_create_applies_and_mirrors_placeholder_csid(tmp_path):
    """A desired create yields a new configured source; the report carries
    the new cfg- entry with the ack, plus a one-shot mirror under the
    portal's placeholder csid so the Portal reducer can match its pending
    entry. _Requirements: 5.2, 5.3_"""
    agent, shadow, accessor, clock = _make_agent(tmp_path)

    agent.on_delta(_delta({
        "portal-abc": {
            "op": "create",
            "portalChangeId": "pc-777",
            "name": "New cam",
            "type": "Camera",
            "params": {"devicePath": "/dev/video3", "cameraId": "cam-2",
                       "gain": 2},
        },
    }))
    _flush(agent, clock)

    # Created through the accessor; the supplied configuration was applied
    # with the follow-up accessor update (the real create path builds the
    # type default itself).
    assert [call[0] for call in accessor.calls] == ["create", "update"]
    (new_id, source), = accessor.sources.items()
    assert source["name"] == "New cam"
    assert source["imageSourceConfiguration"] == {"device": "/dev/video3",
                                                  "gain": 2}

    document = shadow.reported_writes[-1]
    new_csid = "cfg-{}".format(new_id)
    assert document["cameras"][new_csid]["ack"] == "pc-777"
    # One-shot mirror under the placeholder csid, identical content.
    assert document["cameras"]["portal-abc"] == document["cameras"][new_csid]
    assert shadow.desired_writes == [{"changes": {"portal-abc": None}}]

    # The mirror disappears from the next report so the registry converges
    # to the real cfg- entry.
    agent.report_inventory()
    _flush(agent, clock)
    later = shadow.reported_writes[-1]
    assert "portal-abc" not in later["cameras"]
    assert new_csid in later["cameras"]


# --- delete (Requirement 5.2) ---------------------------------------------------


def test_delete_removes_source_and_reports_full_state(tmp_path):
    """A desired delete removes the source; the following full report no
    longer contains it (the Portal reducer resolves the agreement), and
    the desired entry is cleared. _Requirements: 5.2, 5.3_"""
    agent, shadow, accessor, clock = _make_agent(
        tmp_path, sources=[_CONFIGURED_SOURCE]
    )

    agent.on_delta(_delta({
        "cfg-is-1": {"op": "delete", "portalChangeId": "pc-9",
                     "baseVersion": 3},
    }))
    _flush(agent, clock)

    assert accessor.sources == {}
    document = shadow.reported_writes[-1]
    assert document["cameras"] == {}
    assert document["failures"] == {}
    assert shadow.desired_writes == [{"changes": {"cfg-is-1": None}}]


# --- validation failures (Requirement 5.4) ---------------------------------------


def test_failed_apply_reports_reason_verbatim(tmp_path):
    """An accessor rejection (HTTPException) is reported through the
    failures map with the message verbatim and the portalChangeId; the
    desired entry is still cleared so the delta does not re-fire.
    _Requirements: 5.4_"""
    agent, shadow, accessor, clock = _make_agent(tmp_path)

    agent.on_delta(_delta({
        "portal-bad": {
            "op": "create",
            "portalChangeId": "pc-55",
            "name": "Broken folder",
            "type": "Folder",
            "params": {},  # missing location -> accessor rejects
        },
    }))
    _flush(agent, clock)

    assert accessor.sources == {}  # device state unchanged
    document = shadow.reported_writes[-1]
    failure = document["failures"]["portal-bad"]
    assert failure["portalChangeId"] == "pc-55"
    assert failure["reason"] == (
        "The server can't create the image source. Error: 'location is "
        "required when image source type is Folder'. Check the error "
        "message and try again."
    )
    assert "portal-bad" not in document["cameras"]
    assert shadow.desired_writes == [{"changes": {"portal-bad": None}}]


def test_failed_update_keeps_source_out_of_cameras_for_that_report(tmp_path):
    """A failed update reports the source through failures (not cameras)
    in that report — the Portal keeps its entry and marks it failed —
    and the source reappears in the next report.
    _Requirements: 5.4_"""
    agent, shadow, accessor, clock = _make_agent(
        tmp_path, sources=[_CONFIGURED_SOURCE]
    )

    agent.on_delta(_delta({
        "cfg-missing": {
            "op": "update",
            "portalChangeId": "pc-404",
            "name": "Ghost",
            "type": "Camera",
            "params": {},
        },
    }))
    _flush(agent, clock)

    document = shadow.reported_writes[-1]
    assert "doesn't exist" in document["failures"]["cfg-missing"]["reason"]
    # The unrelated existing source still reports normally.
    assert "cfg-is-1" in document["cameras"]

    # Failures are one-shot: the next report reflects plain current state.
    agent.report_inventory()
    _flush(agent, clock)
    assert shadow.reported_writes[-1]["failures"] == {}


# --- discovery-managed refusal (Requirement 5.6) ----------------------------------


def test_discovery_managed_changes_are_refused(tmp_path):
    """Changes targeting origin edge-discovered sources (disc- stable ids)
    never reach the accessor and are refused with reason
    discovery-managed. _Requirements: 5.6_"""
    snapshot = DiscoveryResult(cameras=[_DISCOVERED_CAMERA])
    agent, shadow, accessor, clock = _make_agent(tmp_path, snapshot=snapshot)

    agent.on_delta(_delta({
        "disc-000000000001": {
            "op": "update",
            "portalChangeId": "pc-2",
            "name": "hijack",
            "type": "Camera",
            "params": {},
        },
    }))
    _flush(agent, clock)

    assert accessor.calls == []  # the accessor was never touched
    document = shadow.reported_writes[-1]
    failure = document["failures"]["disc-000000000001"]
    assert failure["reason"] == REASON_DISCOVERY_MANAGED
    assert failure["portalChangeId"] == "pc-2"
    assert shadow.desired_writes == [
        {"changes": {"disc-000000000001": None}}
    ]


def test_update_of_non_configured_csid_is_refused(tmp_path):
    """Update/delete of anything that is not a cfg- configured source is
    refused as discovery-managed (defense in depth). _Requirements: 5.6_"""
    agent, shadow, accessor, clock = _make_agent(tmp_path)

    agent.on_delta(_delta({
        "portal-xyz": {"op": "delete", "portalChangeId": "pc-3"},
    }))
    _flush(agent, clock)

    assert accessor.calls == []
    failure = shadow.reported_writes[-1]["failures"]["portal-xyz"]
    assert failure["reason"] == REASON_DISCOVERY_MANAGED


# --- multiple changes in one delta -----------------------------------------------


def test_mixed_delta_applies_each_change_and_clears_all(tmp_path):
    """A delta carrying several changes applies each independently: one
    success and one failure, all cleared with null in one desired write.
    _Requirements: 5.2, 5.3, 5.4_"""
    agent, shadow, accessor, clock = _make_agent(
        tmp_path, sources=[_CONFIGURED_SOURCE]
    )

    agent.on_delta(_delta({
        "cfg-is-1": {
            "op": "update", "portalChangeId": "pc-a",
            "name": "OK cam", "type": "Camera",
            "params": {"devicePath": "/dev/video0", "cameraId": "cam-1"},
        },
        "cfg-nope": {
            "op": "update", "portalChangeId": "pc-b",
            "name": "Ghost", "type": "Camera", "params": {},
        },
    }))
    _flush(agent, clock)

    document = shadow.reported_writes[-1]
    assert document["cameras"]["cfg-is-1"]["ack"] == "pc-a"
    assert document["failures"]["cfg-nope"]["portalChangeId"] == "pc-b"
    assert shadow.desired_writes == [
        {"changes": {"cfg-is-1": None, "cfg-nope": None}}
    ]


def test_null_entries_in_delta_are_ignored(tmp_path):
    """Cleared (null) desired entries carry no work: no accessor calls, no
    desired write, no report."""
    agent, shadow, accessor, clock = _make_agent(tmp_path)

    agent.on_delta(_delta({"cfg-is-1": None}))
    _flush(agent, clock)

    assert accessor.calls == []
    assert shadow.desired_writes == []
    assert shadow.reported_writes == []
