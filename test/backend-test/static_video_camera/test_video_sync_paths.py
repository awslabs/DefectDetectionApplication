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
"""Example tests for the Static_Video_Camera's sync paths.

Feature: static-camera-video-loop, task 5.7 (Requirements 4.4, 4.6, 4.7,
6.2, 6.4, 8.6, 8.7, 10.5).

- Agent routing: ``staticVideoPin`` reaches the video pin worker and
  ``staticImagePin`` still reaches the image worker, on delta and in the
  startup catch-up; both workers share the agent's lifecycle.
- The production video worker's configuration (slot, cap, marker, store).
- The video worker end to end with a fake S3 and shadow: applied with the
  store's metadata, failed with the store's reason (AV1), remove on empty
  as an applied no-op, marker idempotence, partial-delta resolution from
  the video slot, the download cap, and the bounded echo reason.
- The agent's inventory: the ``StaticVideo`` entry, its own absence
  lifecycle, and isolation from the image camera and from store failures.
- The runtime inventory provider and binding resolution of a
  ``staticVideo`` entry.
- Report-size headroom: a capped report plus both pin slots at their
  worst case stays within the 8 KB shadow limit.
"""
import contextlib
import hashlib
import io
import json
import os
import sys
import types

import pytest

import camera_sync.agent as agent_module
from camera_sync import (
    MAX_REPORT_BYTES,
    CameraSyncStateStore,
    EdgeSyncAgent,
    TYPE_STATIC_VIDEO,
    build_inventory,
    build_report_document,
)
from camera_sync.agent import (
    REASON_DISCOVERY_MANAGED,
    VIDEO_PIN_REASON_MAX_CHARS,
    VIDEO_PIN_SECTION,
    make_video_pin_worker,
)
from camera_sync.pin_worker import StaticImagePinWorker, bound_reason
from utils.static_image_camera import MAX_PIN_FILE_BYTES, STATIC_IMAGE_CAMERA_ID
from utils.static_video_camera import (
    STATIC_VIDEO_CAMERA_ID,
    StaticVideoPinError,
    StaticVideoStore,
)
from utils.video_loop import MAX_PIN_VIDEO_BYTES
from video_shadow_budget import (
    encoded_size,
    fat_inventory,
    shadow_state,
    worst_case_pin_slots,
)
from workflow_engine.camera_binding import STATUS_RESOLVED, resolve_bindings

TEST_BUCKET = "test-component-bucket"
SHADOW = "dda-camera-registry"


# --- fakes -----------------------------------------------------------------------


class FakeClock:
    def __init__(self, start=1_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSleep:
    def __init__(self, clock):
        self.calls = []
        self._clock = clock

    def __call__(self, seconds):
        self.calls.append(seconds)
        self._clock.advance(seconds)


class FakeShadow:
    """Records writes; ``get_state`` is returned by GET (partial deltas)."""

    def __init__(self, get_state=None):
        self.writes = []
        self.gets = 0
        self.get_state = get_state

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        self.gets += 1
        return self.get_state

    def update_thing_shadow_state_request(self, thing_name, shadow_name, state):
        self.writes.append(state)

    def section_reports(self, section):
        return [write["reported"][section] for write in self.writes
                if isinstance(write.get("reported"), dict)
                and section in write["reported"]]


class _Body:
    def __init__(self, data):
        self._buffer = io.BytesIO(data)

    def read(self, n=-1):
        return self._buffer.read(n)


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.calls = []

    def get_object(self, Bucket, Key):
        self.calls.append((Bucket, Key))
        return {"Body": _Body(self.objects[(Bucket, Key)])}


class RecordingPinWorker:
    """Pin worker double recording the agent's wiring calls."""

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


class _NoSources:
    def list_image_sources(self, request, session):
        return []


class FakeStatusStore:
    """A store double for the agent, which only calls ``status()``."""

    def __init__(self, camera_id, pinned=False, metadata=None):
        self.camera_id = camera_id
        self.pinned = pinned
        self.metadata = metadata

    def status(self):
        return {"pinned": self.pinned, "cameraId": self.camera_id,
                "metadata": self.metadata}


def make_agent(tmp_path, shadow, image_worker=None, video_worker=None,
               wall_clock=None):
    kwargs = {}
    if wall_clock is not None:
        kwargs["wall_clock"] = wall_clock
    return EdgeSyncAgent(
        iot_shadow_accessor=shadow,
        image_source_accessor=_NoSources(),
        camera_discovery=None,
        db_session_factory=lambda: contextlib.nullcontext(),
        state_store=CameraSyncStateStore(str(tmp_path / "camera_sync_state.json")),
        thing_name="test-thing",
        pin_worker=image_worker if image_worker is not None else RecordingPinWorker(),
        video_pin_worker=video_worker,
        **kwargs,
    )


def pin_desired(request_id, data, file_name="scene.mp4"):
    return {
        "requestId": request_id,
        "op": "pin",
        "bucket": TEST_BUCKET,
        "key": "static-image-pins/test-device/video/{}".format(request_id),
        "sha256": hashlib.sha256(data).hexdigest(),
        "sizeBytes": len(data),
        "format": "MP4",
        "fileName": file_name,
        "requestedAtEpochMs": 1_790_000_000_000,
    }


def remove_desired(request_id):
    return {"requestId": request_id, "op": "remove",
            "requestedAtEpochMs": 1_790_000_000_000}


_IMAGE_DOC = {"requestId": "req-image-1", "op": "remove",
              "requestedAtEpochMs": 1_790_000_000_000}
_VIDEO_DOC = {"requestId": "req-video-1", "op": "remove",
              "requestedAtEpochMs": 1_790_000_000_001}


# --- agent routing and lifecycle ---------------------------------------------------


def test_on_delta_routes_each_slot_to_its_own_worker(tmp_path):
    image_worker, video_worker = RecordingPinWorker(), RecordingPinWorker()
    agent = make_agent(tmp_path, FakeShadow(), image_worker, video_worker)
    applied = []
    agent.apply_desired_changes = lambda changes: applied.append(changes)

    agent.on_delta({"state": {"staticVideoPin": dict(_VIDEO_DOC)}})
    assert video_worker.desired_docs == [_VIDEO_DOC]
    assert image_worker.desired_docs == []

    changes = {"cfg-is-1": {"op": "update", "name": "renamed"}}
    agent.on_delta({"state": {"staticImagePin": dict(_IMAGE_DOC),
                              "staticVideoPin": dict(_VIDEO_DOC),
                              "changes": changes}})
    assert image_worker.desired_docs == [_IMAGE_DOC]
    assert video_worker.desired_docs == [_VIDEO_DOC, _VIDEO_DOC]
    assert applied == [changes]

    agent.on_delta({"state": {"staticImagePin": dict(_IMAGE_DOC)}})
    assert video_worker.desired_docs == [_VIDEO_DOC, _VIDEO_DOC]
    assert video_worker.report_inventory == agent.report_inventory
    assert image_worker.report_inventory == agent.report_inventory


def test_startup_catch_up_hands_each_slot_to_its_worker(tmp_path):
    shadow = FakeShadow(get_state={
        "desired": {"staticImagePin": dict(_IMAGE_DOC),
                    "staticVideoPin": dict(_VIDEO_DOC)},
        "reported": {},
    })
    image_worker = RecordingPinWorker(applied_id="req-older")
    video_worker = RecordingPinWorker(applied_id="req-older")
    agent = make_agent(tmp_path, shadow, image_worker, video_worker)
    try:
        agent.start()
        assert image_worker.desired_docs == [_IMAGE_DOC]
        assert video_worker.desired_docs == [_VIDEO_DOC]
        assert (image_worker.started, video_worker.started) == (1, 1)
    finally:
        agent.stop()
    assert (image_worker.stopped, video_worker.stopped) == (1, 1)


def test_startup_skips_the_video_request_its_marker_records(tmp_path):
    shadow = FakeShadow(get_state={
        "desired": {"staticImagePin": dict(_IMAGE_DOC),
                    "staticVideoPin": dict(_VIDEO_DOC)},
    })
    image_worker = RecordingPinWorker(applied_id="req-older")
    video_worker = RecordingPinWorker(applied_id=_VIDEO_DOC["requestId"])
    agent = make_agent(tmp_path, shadow, image_worker, video_worker)
    try:
        agent.start()
        assert video_worker.desired_docs == []
        assert image_worker.desired_docs == [_IMAGE_DOC]
    finally:
        agent.stop()


@pytest.mark.parametrize("csid_op", [("static-video-camera", "update"),
                                     ("static-video-camera", "delete"),
                                     ("static-video-camera", "create")])
def test_portal_changes_to_the_video_camera_are_discovery_managed(tmp_path, csid_op):
    csid, op = csid_op
    shadow = FakeShadow()
    agent = make_agent(tmp_path, shadow, RecordingPinWorker(), RecordingPinWorker())
    agent.apply_desired_changes({csid: {"op": op, "name": "x",
                                        "portalChangeId": "pc-1"}})
    assert agent._apply_failures[csid] == {"reason": REASON_DISCOVERY_MANAGED,
                                           "portalChangeId": "pc-1"}
    assert shadow.writes[-1] == {"desired": {"changes": {csid: None}}}


# --- the production video worker ----------------------------------------------------


def test_default_video_worker_serves_the_video_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPONENT_WORK_PATH", str(tmp_path))
    agent = make_agent(tmp_path, FakeShadow(), RecordingPinWorker())
    worker = agent.video_pin_worker
    assert isinstance(worker, StaticImagePinWorker)
    assert worker.section_name == VIDEO_PIN_SECTION == "staticVideoPin"
    assert worker._download_limit() == MAX_PIN_VIDEO_BYTES == 100 * 1024 * 1024
    assert worker._store_factory is agent_module.get_video_store
    assert worker.report_inventory == agent.report_inventory
    assert worker._marker_file() == os.path.join(
        str(tmp_path), "static_video_camera", "applied_pin_request.json")

    # The image worker's defaults are what they always were.
    image_worker = StaticImagePinWorker(FakeShadow(), "test-thing", SHADOW)
    assert image_worker.section_name == "staticImagePin"
    assert image_worker._download_limit() == MAX_PIN_FILE_BYTES
    assert image_worker._marker_file() == os.path.join(
        str(tmp_path), "static_image_camera", "applied_pin_request.json")


# --- the video worker end to end --------------------------------------------------------


def video_worker(store, shadow, s3, marker_path, clock=None, **overrides):
    clock = clock or FakeClock()
    return make_video_pin_worker(
        shadow, "test-thing", SHADOW,
        store_factory=lambda: store,
        s3_client_factory=lambda: s3,
        marker_path=marker_path,
        clock=clock,
        sleep=FakeSleep(clock),
        wall_clock=lambda: 1_790_000_123.5,
        **overrides,
    )


@pytest.fixture
def video_setup(tmp_path):
    store = StaticVideoStore(base_dir=str(tmp_path / "store"))
    shadow = FakeShadow()
    s3 = FakeS3()
    marker_path = str(tmp_path / "applied_pin_request.json")
    return types.SimpleNamespace(store=store, shadow=shadow, s3=s3,
                                 marker_path=marker_path,
                                 worker=video_worker(store, shadow, s3, marker_path))


def test_worker_applies_a_video_and_echoes_the_store_metadata(video_setup, clip_library):
    clip = clip_library.decodable()[0]
    desired = pin_desired("req-pin-1", clip.data, "conveyor.mp4")
    video_setup.s3.objects[(TEST_BUCKET, desired["key"])] = clip.data

    report = video_setup.worker.process_one(desired)

    status = video_setup.store.status()
    assert status["pinned"] is True
    assert report["status"] == "applied"
    assert report["metadata"] == status["metadata"]
    assert report["metadata"]["fileName"] == "conveyor.mp4"
    assert report["metadata"]["frameCount"] == clip.frame_count
    assert report["completedAtEpochMs"] == 1_790_000_123_500
    for field in desired:
        assert report[field] == desired[field]
    assert video_setup.shadow.writes == [{"reported": {"staticVideoPin": report}}]
    with open(video_setup.marker_path, "r", encoding="utf-8") as handle:
        marker = json.load(handle)
    assert (marker["requestId"], marker["status"]) == ("req-pin-1", "applied")


def test_worker_reports_the_store_reason_for_an_undecodable_video(
        video_setup, clip_library):
    av1 = clip_library.get("av1.mkv")
    if av1 is None:
        pytest.skip("this image's ffmpeg has no AV1 encoder; no AV1 clip")
    prior = clip_library.decodable()[0]
    video_setup.store.pin_bytes(prior.data, "prior.mp4")
    prior_status = video_setup.store.status()
    desired = pin_desired("req-av1", av1.data, "av1.mkv")
    video_setup.s3.objects[(TEST_BUCKET, desired["key"])] = av1.data

    report = video_setup.worker.process_one(desired)

    assert report["status"] == "failed"
    assert "could not be decoded" in report["reason"]
    assert "AV1" in report["reason"]
    assert len(json.dumps(report["reason"])) - 2 <= VIDEO_PIN_REASON_MAX_CHARS
    assert video_setup.store.status() == prior_status


def test_remove_on_an_empty_store_is_an_applied_no_op(video_setup, clip_library):
    report = video_setup.worker.process_one(remove_desired("req-rm-empty"))
    assert report["status"] == "applied"
    assert "reason" not in report

    clip = clip_library.decodable()[0]
    video_setup.store.pin_bytes(clip.data, "scene.mp4")
    report = video_setup.worker.process_one(remove_desired("req-rm-2"))
    assert report["status"] == "applied"
    assert video_setup.store.is_pinned() is False


def test_marker_makes_redelivery_idempotent(video_setup, clip_library):
    clip = clip_library.decodable()[0]
    desired = pin_desired("req-once", clip.data)
    video_setup.s3.objects[(TEST_BUCKET, desired["key"])] = clip.data

    first = video_setup.worker.process_one(desired)
    second = video_setup.worker.process_one(dict(desired))

    assert first == second
    assert len(video_setup.s3.calls) == 1
    assert video_setup.worker.applied_request_id() == "req-once"


def test_partial_delta_resolves_from_the_video_slot(video_setup, clip_library):
    clip = clip_library.decodable()[0]
    full = pin_desired("req-partial", clip.data)
    video_setup.s3.objects[(TEST_BUCKET, full["key"])] = clip.data
    # The image slot holds a different request; only the video slot counts.
    video_setup.shadow.get_state = {"desired": {
        "staticImagePin": dict(full, requestId="req-image-other"),
        "staticVideoPin": dict(full),
    }}
    delta = {"requestId": "req-partial", "key": full["key"],
             "sha256": full["sha256"]}

    report = video_setup.worker.process_one(delta)

    assert report["status"] == "applied"
    assert video_setup.shadow.gets == 1
    assert video_setup.shadow.section_reports("staticImagePin") == []


def test_download_cap_fails_the_request_naming_the_limit(tmp_path):
    store = StaticVideoStore(base_dir=str(tmp_path / "store"))
    shadow, s3 = FakeShadow(), FakeS3()
    worker = video_worker(store, shadow, s3, str(tmp_path / "marker.json"),
                          max_download_bytes=1024)
    data = b"\x00" * 4096
    desired = pin_desired("req-big", data)
    s3.objects[(TEST_BUCKET, desired["key"])] = data

    report = worker.process_one(desired)

    assert report["status"] == "failed"
    assert report["reason"] == (
        "retrieval failure: downloaded content exceeds the 1024-byte pin "
        "size limit")
    assert len(s3.calls) == 3  # the unchanged retry policy
    assert store.is_pinned() is False


class _FailingVideoStore:
    def __init__(self, message):
        self.message = message

    def pin_bytes(self, data, file_name):
        raise StaticVideoPinError(self.message)


def test_echo_reason_is_bounded_and_the_marker_keeps_it_whole(tmp_path):
    message = "The video could not be decoded: " + "x" * 1000
    shadow, s3 = FakeShadow(), FakeS3()
    marker_path = str(tmp_path / "marker.json")
    worker = video_worker(_FailingVideoStore(message), shadow, s3, marker_path)
    desired = pin_desired("req-long", b"payload")
    s3.objects[(TEST_BUCKET, desired["key"])] = b"payload"

    report = worker.process_one(desired)

    assert report["status"] == "failed"
    assert report["reason"].endswith("...")
    assert report["reason"].startswith("The video could not be decoded: ")
    assert len(json.dumps(report["reason"])) - 2 == VIDEO_PIN_REASON_MAX_CHARS
    with open(marker_path, "r", encoding="utf-8") as handle:
        assert json.load(handle)["reason"] == message
    # A marker re-report is bounded identically.
    assert worker.process_one(desired) == report


def test_bound_reason_bounds_the_escaped_length():
    assert bound_reason("short", 256) == "short"
    assert bound_reason("x" * 300, None) == "x" * 300
    wide = "é" * 300  # six escaped characters each
    bounded = bound_reason(wide, 256)
    assert len(json.dumps(bounded)) - 2 <= 256
    assert bounded.endswith("...") and bounded.startswith("é")
    assert len(json.dumps(bound_reason("x" * 257, 256))) - 2 == 256


def test_image_worker_echo_reason_is_unbounded(tmp_path):
    """The image slot keeps its unbounded reason (Requirement 6.3)."""
    from utils.static_image_camera import StaticImagePinError

    class _FailingImageStore:
        def pin_bytes(self, data, file_name):
            raise StaticImagePinError("y" * 700)

    shadow, s3 = FakeShadow(), FakeS3()
    worker = StaticImagePinWorker(
        shadow, "test-thing", SHADOW,
        store_factory=_FailingImageStore,
        s3_client_factory=lambda: s3,
        marker_path=str(tmp_path / "marker.json"),
        clock=FakeClock(), sleep=lambda _s: None)
    desired = pin_desired("req-img", b"img")
    s3.objects[(TEST_BUCKET, desired["key"])] = b"img"
    report = worker.process_one(desired)
    assert report["reason"] == "y" * 700
    assert shadow.writes == [{"reported": {"staticImagePin": report}}]


# --- the agent's inventory ---------------------------------------------------------------


_VIDEO_METADATA = {"fileName": "scene.mp4", "format": "MP4", "codec": "H264",
                   "width": 64, "height": 48, "fps": 29.97002997002997,
                   "frameCount": 30, "durationMs": 1001,
                   "fileSizeBytes": 12345, "pinnedAtEpochMs": 1_790_000_000_000}


@pytest.fixture
def stores(monkeypatch):
    image = FakeStatusStore(STATIC_IMAGE_CAMERA_ID)
    video = FakeStatusStore(STATIC_VIDEO_CAMERA_ID)
    monkeypatch.setattr(agent_module, "get_store", lambda: image)
    monkeypatch.setattr(agent_module, "get_video_store", lambda: video)
    return types.SimpleNamespace(image=image, video=video)


def _entry(document, csid):
    return document["cameras"].get(csid)


def test_report_carries_the_video_entry_while_pinned(tmp_path, stores):
    stores.video.pinned, stores.video.metadata = True, dict(_VIDEO_METADATA)
    agent = make_agent(tmp_path, FakeShadow(), video_worker=RecordingPinWorker())
    agent._refresh_reported_versions()

    document = agent._build_current_document()

    entry = _entry(document, STATIC_VIDEO_CAMERA_ID)
    assert entry["type"] == TYPE_STATIC_VIDEO
    assert entry["origin"] == "edge-discovered"
    assert entry["params"] == {}
    assert entry["absent"] is False
    block = entry["capabilities"]["staticVideo"]
    assert block["id"] == STATIC_VIDEO_CAMERA_ID
    assert block["model"] == "Static Video Camera"
    for key, value in _VIDEO_METADATA.items():
        assert block[key] == value
    assert _entry(document, STATIC_IMAGE_CAMERA_ID) is None


def test_video_unpin_reports_a_stable_absence_and_leaves_the_image(tmp_path, stores):
    stores.image.pinned, stores.image.metadata = True, {"fileName": "a.png"}
    stores.video.pinned, stores.video.metadata = True, dict(_VIDEO_METADATA)
    wall = FakeClock(start=1_790_000_500.0)
    agent = make_agent(tmp_path, FakeShadow(), video_worker=RecordingPinWorker(),
                       wall_clock=wall)
    agent._refresh_reported_versions()
    pinned_doc = agent._build_current_document()
    image_before = _entry(pinned_doc, STATIC_IMAGE_CAMERA_ID)

    stores.video.pinned, stores.video.metadata = False, None
    absent = _entry(agent._build_current_document(), STATIC_VIDEO_CAMERA_ID)
    assert absent["absent"] is True
    assert absent["absentSince"] == int(wall.now * 1000)
    assert absent["version"] == _entry(pinned_doc, STATIC_VIDEO_CAMERA_ID)["version"] + 1
    assert "fileName" not in absent["capabilities"]["staticVideo"]

    wall.advance(3600.0)
    later = agent._build_current_document()
    assert _entry(later, STATIC_VIDEO_CAMERA_ID) == absent
    assert _entry(later, STATIC_IMAGE_CAMERA_ID) == image_before

    # Re-pin restores the present entry and ends the absence episode.
    stores.video.pinned, stores.video.metadata = True, dict(_VIDEO_METADATA)
    assert _entry(agent._build_current_document(),
                  STATIC_VIDEO_CAMERA_ID)["absent"] is False


def test_video_absence_uses_its_own_workers_remove_marker(tmp_path, stores):
    video_marker = {"requestId": "req-rm", "op": "remove", "status": "applied",
                    "metadata": None, "completedAtEpochMs": 1_790_000_777_000}
    image_marker = dict(video_marker, completedAtEpochMs=1_790_000_111_000)
    shadow = FakeShadow(get_state={"desired": {}, "reported": {"cameras": {
        STATIC_VIDEO_CAMERA_ID: {"version": 3, "absent": False},
        STATIC_IMAGE_CAMERA_ID: {"version": 2, "absent": False},
    }}})
    agent = make_agent(tmp_path, shadow,
                       image_worker=RecordingPinWorker(marker=image_marker),
                       video_worker=RecordingPinWorker(marker=video_marker),
                       wall_clock=FakeClock(start=1_790_000_900.0))
    agent._refresh_reported_versions()

    document = agent._build_current_document()

    assert _entry(document, STATIC_VIDEO_CAMERA_ID)["absentSince"] == 1_790_000_777_000
    assert _entry(document, STATIC_IMAGE_CAMERA_ID)["absentSince"] == 1_790_000_111_000


def test_restart_adopts_the_video_absent_since_from_the_shadow(tmp_path, stores):
    shadow = FakeShadow(get_state={"desired": {}, "reported": {"cameras": {
        STATIC_VIDEO_CAMERA_ID: {"version": 7, "absent": True,
                                 "absentSince": 1_790_000_600_000},
    }}})
    agent = make_agent(tmp_path, shadow, video_worker=RecordingPinWorker(),
                       wall_clock=FakeClock(start=1_790_009_999.0))
    agent._refresh_reported_versions()

    entry = _entry(agent._build_current_document(), STATIC_VIDEO_CAMERA_ID)

    assert entry["absent"] is True
    assert entry["absentSince"] == 1_790_000_600_000


def test_never_reported_video_camera_yields_no_entry(tmp_path, stores):
    agent = make_agent(tmp_path, FakeShadow(), video_worker=RecordingPinWorker())
    agent._refresh_reported_versions()
    assert _entry(agent._build_current_document(), STATIC_VIDEO_CAMERA_ID) is None


def test_video_store_failure_keeps_the_rest_of_the_report(tmp_path, stores, monkeypatch):
    stores.image.pinned, stores.image.metadata = True, {"fileName": "a.png"}

    def _raising():
        raise KeyError("COMPONENT_WORK_PATH")

    monkeypatch.setattr(agent_module, "get_video_store", _raising)
    agent = make_agent(tmp_path, FakeShadow(), video_worker=RecordingPinWorker())
    agent._refresh_reported_versions()

    document = agent._build_current_document()

    assert _entry(document, STATIC_IMAGE_CAMERA_ID)["absent"] is False
    assert _entry(document, STATIC_VIDEO_CAMERA_ID) is None


# --- runtime inventory provider and binding ---------------------------------------------


@pytest.fixture
def runtime_provider(tmp_path, monkeypatch):
    """``runtime._camera_binding_dependencies()`` with only its on-device
    reach-outs substituted (the workflow_engine provider test's pattern)."""
    monkeypatch.setenv("COMPONENT_WORK_PATH", str(tmp_path))
    import dao.sqlite_db.sqlite_db_operations as dao_module
    import utils
    import utils.static_image_camera as image_module
    import utils.static_video_camera as video_module
    import workflow_engine.camera_binding_store as store_module
    import camera_sync.inventory as inventory_module
    from workflow_engine import runtime

    fake_server_setup = types.ModuleType("utils.server_setup")
    fake_server_setup.iot_shadow_accessor = object()
    fake_server_setup.camera_discovery = None
    fake_server_setup.image_source_accessor = _NoSources()
    monkeypatch.setitem(sys.modules, "utils.server_setup", fake_server_setup)
    monkeypatch.setattr(utils, "server_setup", fake_server_setup, raising=False)

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(dao_module, "SessionLocal", _Session)
    monkeypatch.setattr(store_module, "CameraBindingStore", lambda _accessor: None)

    calls = []
    real_build_inventory = inventory_module.build_inventory

    def recording_build_inventory(*args, **kwargs):
        calls.append(kwargs)
        return real_build_inventory(*args, **kwargs)

    monkeypatch.setattr(inventory_module, "build_inventory", recording_build_inventory)
    image_store = image_module.StaticImageStore(base_dir=str(tmp_path / "image"))
    video_store = StaticVideoStore(base_dir=str(tmp_path / "video"))
    monkeypatch.setattr(image_module, "_store", image_store, raising=False)
    monkeypatch.setattr(video_module, "_store", video_store, raising=False)
    _store, provider = runtime._camera_binding_dependencies()
    return types.SimpleNamespace(provider=provider, calls=calls,
                                 video_store=video_store,
                                 video_module=video_module)


def test_runtime_provider_surfaces_the_pinned_video_camera(runtime_provider, clip_library):
    runtime_provider.video_store.pin_bytes(clip_library.decodable()[0].data, "scene.mp4")

    inventory = runtime_provider.provider()

    (entry,) = [e for e in inventory if e.camera_source_id == STATIC_VIDEO_CAMERA_ID]
    assert entry.type == TYPE_STATIC_VIDEO
    assert entry.capabilities["staticVideo"]["id"] == STATIC_VIDEO_CAMERA_ID
    kwargs = runtime_provider.calls[-1]
    assert kwargs["static_video_pinned"] is True
    assert kwargs["static_video_metadata"]["fileName"] == "scene.mp4"
    assert "static_video_absent_since" not in kwargs
    assert kwargs["static_image_pinned"] is False


def test_runtime_provider_contains_a_video_store_failure(runtime_provider, monkeypatch):
    def _raising():
        raise KeyError("COMPONENT_WORK_PATH")

    monkeypatch.setattr(runtime_provider.video_module, "get_store", _raising)

    inventory = runtime_provider.provider()

    assert STATIC_VIDEO_CAMERA_ID not in [e.camera_source_id for e in inventory]
    assert runtime_provider.calls[-1]["static_video_pinned"] is False


def _aravis_document(node_ids):
    return {
        "schemaVersion": 1,
        "segments": [{"elements": [{"nodeId": node, "type": "appsrc", "args": {}}
                                   for node in node_ids]}],
        "bindingPoints": [
            {"nodeId": node, "nodeType": "aravis_camera_source",
             "parameters": {"camera_id": "Aravis-Fake-GV01", "gain": 4},
             "slots": [], "aravisBinding": True}
            for node in node_ids
        ],
    }


def test_binding_resolves_each_virtual_camera_to_its_own_id():
    inventory = build_inventory(
        [], None,
        static_image_pinned=True, static_image_metadata={"fileName": "a.png"},
        static_video_pinned=True, static_video_metadata=dict(_VIDEO_METADATA),
    )
    bindings = {"node-image": {"cameraSourceId": STATIC_IMAGE_CAMERA_ID},
                "node-video": {"cameraSourceId": STATIC_VIDEO_CAMERA_ID}}

    result = resolve_bindings(_aravis_document(["node-image", "node-video"]),
                              bindings, inventory)

    assert result.status == STATUS_RESOLVED
    assert result.errors == ()
    video = result.aravis_assignments["node-video"]
    assert video["cameraSourceId"] == STATIC_VIDEO_CAMERA_ID
    assert video["params"]["camera_id"] == STATIC_VIDEO_CAMERA_ID
    assert video["params"]["cameraId"] == STATIC_VIDEO_CAMERA_ID
    image = result.aravis_assignments["node-image"]
    assert image["params"]["camera_id"] == STATIC_IMAGE_CAMERA_ID


def test_binding_to_an_unpinned_video_camera_is_missing():
    result = resolve_bindings(
        _aravis_document(["node-video"]),
        {"node-video": {"cameraSourceId": STATIC_VIDEO_CAMERA_ID}},
        build_inventory([], None))
    assert result.status != STATUS_RESOLVED
    assert result.missing == ({"nodeId": "node-video",
                               "cameraSourceId": STATIC_VIDEO_CAMERA_ID},)


# --- report-size headroom (Requirement 10.5) --------------------------------------------
#
# The shared worst-case fixtures live in video_shadow_budget; the same
# budget at raised shadow limits is covered by test_video_report_cap.py.


def test_capped_report_plus_both_pin_slots_fit_the_shadow_limit(tmp_path):
    assert MAX_REPORT_BYTES == 4608
    inventory = fat_inventory()
    report = build_report_document(
        inventory, {e.camera_source_id: 7 for e in inventory},
        reported_at_ms=1_790_000_000_000)
    assert encoded_size(report) <= MAX_REPORT_BYTES

    variants = worst_case_pin_slots(tmp_path)
    video_failed = variants[1][1]["staticVideoPin"]
    assert len(json.dumps(video_failed["reason"])) - 2 == VIDEO_PIN_REASON_MAX_CHARS

    for desired_sections, reported_sections in variants:
        state = shadow_state(report, desired_sections, reported_sections)
        assert encoded_size(state) <= 8 * 1024
        # The budget claim itself: the same two slots next to a report
        # exactly at the cap still fit.
        slots = encoded_size(state) - encoded_size(report)
        assert MAX_REPORT_BYTES + slots <= 8 * 1024
