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
"""Deleted camera sources leave the camera-registry shadow
(rtsp-rtmp-stream-cameras hardware finding).

A shadow update MERGES nested maps: a camera key omitted from a full report
stays in the shadow document, and only an explicit ``null`` removes it. On
real devices every configured camera deleted on the device therefore lived
on in the shadow, and the Portal's missing-from-report deletion path never
fired. Stream cameras made it acute: after a handful of stream cameras had
been added and deleted, the merged document outgrew ShadowManager's 8 KB
limit and every later camera report of the device was rejected
(``InvalidArgumentsError``), so no camera change reached the Portal any more.

The agent now retires every key it published that the current inventory no
longer holds — a deleted configured camera, a one-shot create alias after
its report, a stale key an earlier process left behind — exactly once, and
never a key whose disappearance is reported as absence (discovered
hardware, the virtual static cameras).

The shadow fake applies real update semantics (nested maps merge, arrays and
scalars replace, a null deletes) and, like ShadowManager, rejects an update
whose merged state exceeds the size limit.
"""
import contextlib
import copy
import json
import os

import pytest

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")

from camera_sync import CameraSyncStateStore, EdgeSyncAgent  # noqa: E402
from camera_sync import agent as agent_module  # noqa: E402
from camera_sync.agent import (  # noqa: E402
    ABSENCE_TRACKED_IDS,
    ABSENCE_TRACKED_PREFIXES,
    deleted_source_retirements,
)
from utils.static_image_camera import STATIC_IMAGE_CAMERA_ID  # noqa: E402
from utils.static_video_camera import STATIC_VIDEO_CAMERA_ID  # noqa: E402

SHADOW_LIMIT = 8192


# --- fakes -----------------------------------------------------------------------


class InvalidArgumentsError(Exception):
    """ShadowManager's size rejection, as Greengrass IPC raises it."""

    def __init__(self):
        super().__init__()
        self.message = "The payload exceeds the maximum size allowed (413)"


def _merge(target, update):
    for key, value in update.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _size(state):
    return len(json.dumps(state, separators=(",", ":")))


class MergingShadow:
    """The camera-registry shadow with AWS IoT update semantics."""

    def __init__(self, state=None, limit_bytes=SHADOW_LIMIT):
        self.state = copy.deepcopy(state) if state else {}
        self.limit = limit_bytes
        self.failing = False
        self.writes = []
        self.rejections = 0

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        return copy.deepcopy(self.state) if self.state else None

    def update_thing_shadow_state_request(self, thing_name, shadow_name, update):
        if self.failing:
            raise ConnectionError("shadow offline")
        merged = copy.deepcopy(self.state)
        _merge(merged, update)
        if self.limit is not None and _size(merged) > self.limit:
            self.rejections += 1
            raise InvalidArgumentsError()
        self.state = merged
        self.writes.append(copy.deepcopy(update))

    @property
    def cameras(self):
        return (self.state.get("reported") or {}).get("cameras") or {}

    def nulls(self, write_index):
        cameras = self.writes[write_index]["reported"]["cameras"]
        return sorted(csid for csid, entry in cameras.items() if entry is None)


class Sources:
    """The device's Image_Sources behind the accessor the agent reads."""

    def __init__(self, *sources):
        self.by_id = {source["imageSourceId"]: dict(source) for source in sources}

    def list_image_sources(self, request, session):
        return [dict(source) for source in self.by_id.values()]


class UnpinnedStore:
    def status(self):
        return {"pinned": False, "metadata": None}


class NoMarkerPinWorker:
    report_inventory = None

    def applied_marker(self):
        return None


class NoCredentials:
    def configured(self, image_source_id):
        return False


def _camera(image_source_id, name="cam"):
    return {"imageSourceId": image_source_id, "name": name, "type": "Camera",
            "cameraId": "camera-" + image_source_id, "imageSourceConfiguration": {}}


def _stream(image_source_id, name="rtsp cam"):
    return {"imageSourceId": image_source_id, "name": name, "type": "RTSP",
            "location": "rtsp://camera.example:8554/h264", "imageSourceConfiguration": {}}


@pytest.fixture(autouse=True)
def _unpinned_static_cameras(monkeypatch):
    monkeypatch.setattr(agent_module, "get_store", lambda: UnpinnedStore())
    monkeypatch.setattr(agent_module, "get_video_store", lambda: UnpinnedStore())


def _agent(tmp_path, shadow, sources, **overrides):
    arguments = dict(
        iot_shadow_accessor=shadow, image_source_accessor=sources, camera_discovery=None,
        db_session_factory=lambda: contextlib.nullcontext(),
        state_store=CameraSyncStateStore(str(tmp_path / "camera_sync_state.json")),
        thing_name="thing", wall_clock=lambda: 1_790_000_000.0,
        pin_worker=NoMarkerPinWorker(), video_pin_worker=NoMarkerPinWorker(),
        credential_store=NoCredentials())
    arguments.update(overrides)
    agent = EdgeSyncAgent(**arguments)
    agent._refresh_reported_versions()  # what start() does first
    return agent


# --- the pure rule -------------------------------------------------------------------


def test_rule_retires_vanished_keys_except_absence_tracked_ones():
    published = {"cfg-live", "cfg-gone", "portal-3f9c", "disc-000000000001",
                 "arv-0123456789ab", STATIC_IMAGE_CAMERA_ID, STATIC_VIDEO_CAMERA_ID}
    assert deleted_source_retirements(published, {"cfg-live"}) == {"cfg-gone", "portal-3f9c"}
    assert deleted_source_retirements(published, published) == set()
    assert deleted_source_retirements((), {"cfg-live"}) == set()


def test_absence_tracked_keys_are_the_discovery_and_static_ones():
    from camera_discovery.aravis import STABLE_ID_PREFIX
    from camera_discovery.discovery import make_stable_id

    assert make_stable_id("usb-1", "cam").startswith(ABSENCE_TRACKED_PREFIXES)
    assert STABLE_ID_PREFIX in ABSENCE_TRACKED_PREFIXES
    assert ABSENCE_TRACKED_IDS == {STATIC_IMAGE_CAMERA_ID, STATIC_VIDEO_CAMERA_ID}


# --- the agent -----------------------------------------------------------------------


def test_a_camera_deleted_on_the_device_leaves_the_shadow_exactly_once(tmp_path):
    shadow = MergingShadow()
    sources = Sources(_camera("keep"), _stream("gone"))
    agent = _agent(tmp_path, shadow, sources)
    assert agent._write_report()
    assert set(shadow.cameras) == {"cfg-keep", "cfg-gone"}

    del sources.by_id["gone"]
    assert agent._write_report()
    assert shadow.nulls(-1) == ["cfg-gone"]
    assert set(shadow.cameras) == {"cfg-keep"}

    assert agent._write_report()
    assert shadow.nulls(-1) == []


def test_stale_keys_an_earlier_process_left_are_retired_by_the_first_report(tmp_path):
    """The jetson Orin incident: deleted stream cameras had pushed the merged
    shadow past its 8 KB limit, so every report was rejected. The first
    report of the fixed agent deletes them and is accepted; discovered and
    static cameras stay."""
    stale = {"cfg-stale{:02d}".format(index): {
        "version": 1, "name": "harness-stream-{:02d}-connect".format(index), "type": "RTSP",
        "origin": "edge-configured",
        "params": {"url": "rtsp://camera.example:8554/h265", "transport": "tcp",
                   "latencyMs": 200, "decoder": "auto", "maxFrameDimension": 1920,
                   "stallTimeoutS": 10, "credentialsConfigured": False},
        "capabilities": {"stream": {"state": "streaming", "codec": "h265", "width": 1920,
                                    "height": 1080, "decoder": "hardware"}},
        "discovered": False, "absent": False} for index in range(20)}
    kept = {
        "cfg-live": {"version": 3, "name": "cam", "type": "Camera", "origin": "edge-configured",
                     "params": {"cameraId": "camera-live"}, "capabilities": {},
                     "discovered": False, "absent": False},
        "arv-0123456789ab": {"version": 2, "name": "Basler", "type": "AravisDiscovered",
                             "origin": "edge-discovered", "params": {}, "capabilities": {},
                             "discovered": True, "absent": True, "absentSince": 1},
        STATIC_IMAGE_CAMERA_ID: {"version": 5, "name": "Static Image Camera",
                                 "type": "StaticImage", "origin": "edge-discovered",
                                 "params": {}, "capabilities": {}, "discovered": True,
                                 "absent": True, "absentSince": 1},
    }
    shadow = MergingShadow({"reported": {"schemaVersion": 1, "cameras": {**stale, **kept}}})
    assert _size(shadow.state) > SHADOW_LIMIT

    agent = _agent(tmp_path, shadow, Sources(_camera("live")))
    assert agent._write_report()

    assert shadow.rejections == 0
    assert shadow.nulls(-1) == sorted(stale)
    assert set(shadow.cameras) == set(kept)
    assert _size(shadow.state) < SHADOW_LIMIT // 2


def test_a_source_that_never_reached_the_shadow_gets_no_null(tmp_path):
    shadow = MergingShadow()
    sources = Sources(_camera("keep"))
    agent = _agent(tmp_path, shadow, sources)
    assert agent._write_report()

    shadow.failing = True
    sources.by_id["brief"] = _camera("brief")
    assert not agent._write_report()
    del sources.by_id["brief"]
    shadow.failing = False
    assert agent._write_report()
    assert shadow.nulls(-1) == []
    assert set(shadow.cameras) == {"cfg-keep"}


def test_a_deletion_made_offline_is_carried_by_the_catch_up(tmp_path):
    shadow = MergingShadow()
    sources = Sources(_camera("keep"), _camera("gone"))
    agent = _agent(tmp_path, shadow, sources)
    assert agent._write_report()

    shadow.failing = True
    del sources.by_id["gone"]
    assert not agent._write_report()
    assert not agent._write_report()
    shadow.failing = False
    assert agent._write_report()
    assert shadow.nulls(-1) == ["cfg-gone"]
    assert set(shadow.cameras) == {"cfg-keep"}


def test_a_source_recreated_before_the_retirement_was_written_is_not_deleted(tmp_path):
    shadow = MergingShadow()
    sources = Sources(_camera("flaky"))
    agent = _agent(tmp_path, shadow, sources)
    assert agent._write_report()

    shadow.failing = True
    del sources.by_id["flaky"]
    assert not agent._write_report()
    sources.by_id["flaky"] = _camera("flaky")
    shadow.failing = False
    assert agent._write_report()
    assert shadow.nulls(-1) == []
    assert set(shadow.cameras) == {"cfg-flaky"}


def test_a_source_with_an_outstanding_apply_failure_is_not_retired(tmp_path):
    shadow = MergingShadow()
    agent = _agent(tmp_path, shadow, Sources(_camera("cam")))
    assert agent._write_report()

    agent._record_failure("cfg-cam", "rejected", "pc-1")
    assert agent._write_report()
    assert shadow.nulls(-1) == []
    assert "cfg-cam" in shadow.cameras
    assert shadow.state["reported"]["failures"]["cfg-cam"]["reason"] == "rejected"


def test_a_create_alias_is_published_once_then_retired_once(tmp_path):
    shadow = MergingShadow()
    agent = _agent(tmp_path, shadow, Sources(_camera("made")))
    with agent._lock:
        agent._create_aliases["portal-3f9c"] = "cfg-made"
        agent._pending_acks["cfg-made"] = "pc-9"

    assert agent._write_report()
    assert shadow.cameras["portal-3f9c"]["ack"] == "pc-9"

    assert agent._write_report()
    assert shadow.nulls(-1) == ["portal-3f9c"]
    assert set(shadow.cameras) == {"cfg-made"}

    assert agent._write_report()
    assert shadow.nulls(-1) == []


def test_a_readable_shadow_is_the_record_of_what_was_published(tmp_path):
    """The previous process built a report holding a source but never wrote
    it; after the restart the source is gone. The shadow never held it, so
    the first report deletes nothing — the version state store alone would
    have said otherwise."""
    shadow = MergingShadow()
    sources = Sources(_camera("keep"))
    previous = _agent(tmp_path, shadow, sources)
    assert previous._write_report()
    shadow.failing = True
    sources.by_id["brief"] = _camera("brief")
    assert not previous._write_report()

    del sources.by_id["brief"]
    shadow.failing = False
    agent = _agent(tmp_path, shadow, sources)
    assert agent._write_report()
    assert shadow.nulls(-1) == []
    assert set(shadow.cameras) == {"cfg-keep"}


def test_without_a_readable_shadow_the_state_store_seeds_what_was_published(tmp_path):
    """A start whose shadow GET failed still retires a source the previous
    process reported: the version state store remembers it."""
    shadow = MergingShadow()
    sources = Sources(_camera("keep"), _camera("gone"))
    assert _agent(tmp_path, shadow, sources)._write_report()

    del sources.by_id["gone"]
    unreadable = MergingShadow()
    unreadable.state = copy.deepcopy(shadow.state)
    unreadable.get_thing_shadow_state_request = lambda thing, name: None
    agent = _agent(tmp_path, unreadable, sources)
    assert agent._write_report()
    assert unreadable.nulls(-1) == ["cfg-gone"]
    assert set(unreadable.cameras) == {"cfg-keep"}
