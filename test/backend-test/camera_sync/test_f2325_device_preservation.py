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
"""Preservation for the Edge_Sync_Agent side of finding 23
(rtsp-rtmp-stream-cameras task 30.3; Requirements 5.3, 5.6, 5.13).

Everything here behaves the same before the fix (``ac74c0b``) and after it:

- a change without a ``portalChangeId`` is applied on every delivery: two
  deliveries of one create make two cameras;
- two changes with different ids for one ``cfg-`` camera are both applied,
  and the second's ack is reported;
- each delivery's clear writes exactly ``{"changes": {<csid>: None, ...}}``
  for its batch, and nothing else is written to ``desired`` once it lands;
- task 29's parked change: its redelivery is not applied again and keeps its
  one timer; a newer change supersedes it, and the old timer then does
  nothing;
- with nothing pending, ``pump()`` returns as today: ``None`` after a
  successful report with no new request, and the remaining debounce while a
  report waits.

Self-contained like the bug-condition file: its own dict-backed fakes and the
agent built through a signature filter, so it runs on the unfixed tree.
"""
import contextlib
import copy
import inspect
import itertools
import json
import os
import threading
from types import SimpleNamespace
from typing import Mapping

import pytest

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")

from fastapi import HTTPException  # noqa: E402

from camera_sync import CameraSyncStateStore, EdgeSyncAgent  # noqa: E402
from camera_sync import agent as agent_module  # noqa: E402
from stream_ingest.credential_fetch import CredentialFetchError  # noqa: E402

THING = "thing"
SHADOW = "dda-camera-registry"
SENTINEL = "pw-F2325-PRES-41b7"
REF = {"secretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
                    "dda-portal/stream-camera-credentials/thing/portal-x-AbCdEf",
       "versionId": "0b4f7e7c-1d7a-4f45-9d3a-2d1c5c9f7a11"}


# --- fakes (copied from the bug-condition file, so each file stands alone) ----------


def merge(target, update):
    """AWS IoT shadow update semantics: nested maps merge, a null deletes."""
    for key, value in update.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class MergingShadow:
    """The camera-registry shadow with AWS update semantics; ``fail_desired``
    fails that many of the next desired writes with ``TimeoutError``."""

    def __init__(self, state=None):
        self.state = copy.deepcopy(state) if state else {}
        self.reported = []
        self.desired = []
        self.failed = []
        self.fail_desired = 0
        self._lock = threading.Lock()

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        with self._lock:
            return copy.deepcopy(self.state)

    def update_thing_shadow_state_request(self, thing_name, shadow_name, update):
        with self._lock:
            if "desired" in update and self.fail_desired > 0:
                self.fail_desired -= 1
                self.failed.append(copy.deepcopy(update))
                raise TimeoutError("the desired-entry clear timed out")
            merge(self.state, update)
            if "reported" in update:
                self.reported.append(copy.deepcopy(update["reported"]))
            if "desired" in update:
                self.desired.append(copy.deepcopy(update["desired"]))


class DictAccessor:
    def __init__(self, *sources):
        self.sources = {source["imageSourceId"]: copy.deepcopy(source) for source in sources}
        self.credentials = {}
        self.calls = []
        self._ids = itertools.count(1)

    def list_image_sources(self, request, session):
        return [copy.deepcopy(source) for source in self.sources.values()]

    def create_image_source(self, data, session, managed_stream_settings=None):
        self.calls.append(("create", data.get("type")))
        data = dict(data)
        credentials = data.pop("credentials", None)
        settings = {key: value for key, value in (data.pop("streamSettings", None) or {}).items()
                    if value is not None}
        settings.update({key: value for key, value in (managed_stream_settings or {}).items()
                         if value is not None})
        image_source_id = "is-{}".format(next(self._ids))
        source = dict(data, imageSourceId=image_source_id)
        source["imageSourceConfiguration"] = {"streamSettings": settings} if settings else {}
        self.sources[image_source_id] = source
        if credentials:
            self.credentials[image_source_id] = dict(credentials)
        return {"imageSourceId": image_source_id}

    def get_image_source(self, image_source_id, session):
        self.calls.append(("get", image_source_id))
        source = self.sources.get(image_source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="no image source {}".format(image_source_id))
        settings = (source.get("imageSourceConfiguration") or {}).get("streamSettings")
        return SimpleNamespace(imageSourceConfiguration=SimpleNamespace(streamSettings=settings))

    def update_image_source(self, image_source_id, data, session, managed_stream_settings=None):
        self.calls.append(("update", image_source_id))
        if image_source_id not in self.sources:
            raise HTTPException(status_code=404, detail="no image source {}".format(image_source_id))
        data = dict(data)
        data.pop("credentials", None)
        self.sources[image_source_id].update(data)
        return {"imageSourceId": image_source_id}

    def delete_image_source(self, image_source_id, session):
        self.calls.append(("delete", image_source_id))
        if image_source_id not in self.sources:
            raise HTTPException(
                status_code=404,
                detail="The server can't delete the image source. Error: 'The image source {} "
                       "doesn't exist'. Check the image source ID and try again.".format(image_source_id))
        del self.sources[image_source_id]
        self.credentials.pop(image_source_id, None)
        return {"imageSourceId": image_source_id}


class NoCredentials:
    def configured(self, image_source_id):
        return False


class UnpinnedStore:
    def status(self):
        return {"pinned": False, "metadata": None}


class NoMarkerPinWorker:
    report_inventory = None

    def applied_marker(self):
        return None

    def start(self):
        pass

    def stop(self):
        pass


class ScriptedFetcher:
    """Returns (or raises) the scripted outcomes in order."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, reference):
        self.calls.append(dict(reference))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return dict(outcome)


class RecordingTimer:
    def __init__(self):
        self.pending = []

    def __call__(self, delay, action):
        self.pending.append((delay, action))


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _unpinned_static_cameras(monkeypatch):
    monkeypatch.setattr(agent_module, "get_store", lambda: UnpinnedStore())
    monkeypatch.setattr(agent_module, "get_video_store", lambda: UnpinnedStore())


class World:
    def __init__(self, tmp_path, *sources, outcomes=(), debounce_seconds=0.0):
        self.shadow = MergingShadow()
        self.accessor = DictAccessor(*sources)
        self.fetcher = ScriptedFetcher(*outcomes)
        self.timer = RecordingTimer()
        self.clock = Clock()
        wanted = dict(
            iot_shadow_accessor=self.shadow, image_source_accessor=self.accessor, camera_discovery=None,
            db_session_factory=lambda: contextlib.nullcontext(),
            state_store=CameraSyncStateStore(str(tmp_path / "state.json")),
            thing_name=THING, clock=self.clock, wall_clock=lambda: 1_790_000_000.0,
            debounce_seconds=debounce_seconds, pin_worker=NoMarkerPinWorker(),
            video_pin_worker=NoMarkerPinWorker(), stream_timer=RecordingTimer(),
            credential_fetcher=self.fetcher, credential_store=NoCredentials(),
            change_retry_timer=self.timer)
        accepted = inspect.signature(EdgeSyncAgent.__init__).parameters
        self.agent = EdgeSyncAgent(**{key: value for key, value in wanted.items() if key in accepted})

    def deliver(self, changes, version, fail_clear=False):
        """The Portal's desired write, then the delta that carries it; with
        ``fail_clear`` the agent's clear of that delta times out."""
        self.shadow.update_thing_shadow_state_request(
            THING, SHADOW, {"desired": {"changes": copy.deepcopy(changes)}})
        if fail_clear:
            self.shadow.fail_desired = 1
        self.agent.on_delta({"state": {"changes": copy.deepcopy(changes)}, "version": version})
        self.agent.pump()

    def pump_for(self, seconds, step=2.0):
        """Advance the fake clock ``seconds`` in ``step`` s steps, pumping each."""
        for _ in range(int(seconds / step)):
            self.clock.now += step
            self.agent.pump()


def _camera(image_source_id):
    return {"imageSourceId": image_source_id, "name": "cam", "type": "Camera",
            "cameraId": "camera-" + image_source_id, "imageSourceConfiguration": {}}


def _folder_create(pcid=None, name="Folder"):
    change = {"op": "create", "name": name, "type": "Folder", "params": {"location": "/aws_dda/images"}}
    if pcid is not None:
        change["portalChangeId"] = pcid
    return change


def _rtsp_create(pcid):
    return {"op": "create", "portalChangeId": pcid, "name": "Dock 3", "type": "RTSP",
            "params": {"url": "rtsp://10.0.4.21:554/Streaming/Channels/101", "credentialRef": dict(REF),
                       "credentialsConfigured": True, "credentialsUpdatedAt": 1_790_000_000_123}}


def _cameras(world):
    return sorted((source["type"], source["name"]) for source in world.accessor.sources.values())


# --- applied as today ---------------------------------------------------------------


def test_a_change_without_a_portal_change_id_is_applied_on_every_delivery(tmp_path):
    world = World(tmp_path)
    world.deliver({"portal-a": _folder_create(name="Dock A")}, version=2, fail_clear=True)
    assert world.shadow.failed, "setup: the first clear did not fail"

    world.deliver({"portal-a": _folder_create(name="Dock A")}, version=3)

    assert _cameras(world) == [("Folder", "Dock A"), ("Folder", "Dock A")]
    assert world.shadow.reported[-1]["failures"] == {}


def test_two_changes_with_different_ids_for_one_cfg_camera_are_both_applied(tmp_path):
    world = World(tmp_path, _camera("cam"))
    world.deliver({"cfg-cam": {"op": "update", "portalChangeId": "pc-a", "name": "Line 1"}},
                  version=2, fail_clear=True)
    assert world.shadow.failed, "setup: the first clear did not fail"
    assert world.accessor.sources["cam"]["name"] == "Line 1"
    assert world.shadow.reported[-1]["cameras"]["cfg-cam"]["ack"] == "pc-a"

    world.deliver({"cfg-cam": {"op": "update", "portalChangeId": "pc-b", "name": "Line 2"}}, version=3)

    assert world.accessor.calls == [("update", "cam"), ("update", "cam")]
    assert world.accessor.sources["cam"]["name"] == "Line 2"
    document = world.shadow.reported[-1]
    assert document["cameras"]["cfg-cam"]["ack"] == "pc-b"
    assert document["failures"] == {}


def test_each_clear_writes_exactly_its_batch_and_nothing_else(tmp_path):
    world = World(tmp_path)
    world.agent.apply_desired_changes({"portal-a": _folder_create("pc-1", "Dock A"),
                                       "portal-b": _folder_create("pc-2", "Dock B")})
    world.agent.pump()
    assert world.shadow.desired == [{"changes": {"portal-a": None, "portal-b": None}}]

    world.pump_for(20.0)
    world.agent.apply_desired_changes({"portal-c": _folder_create("pc-3", "Dock C")})
    world.agent.pump()
    world.pump_for(20.0)

    assert world.shadow.desired == [{"changes": {"portal-a": None, "portal-b": None}},
                                    {"changes": {"portal-c": None}}]
    assert len(world.accessor.sources) == 3


# --- task 29's parked change --------------------------------------------------------------


def test_a_parked_change_keeps_its_one_timer_and_a_newer_change_supersedes_it(tmp_path):
    world = World(tmp_path, outcomes=[CredentialFetchError("AccessDeniedException"),
                                      {"username": "viewer", "password": SENTINEL}])
    world.deliver({"portal-x": _rtsp_create("pc-1")}, version=2)
    assert [delay for delay, _ in world.timer.pending] == [2.0]
    assert len(world.fetcher.calls) == 1 and world.accessor.sources == {}
    [(_, old_timer)] = world.timer.pending

    world.deliver({"portal-x": _rtsp_create("pc-1")}, version=3)
    assert len(world.fetcher.calls) == 1, "the parked change's redelivery fetched again"
    assert world.accessor.sources == {}
    assert len(world.timer.pending) == 1, "the parked change's redelivery started a second timer"

    world.deliver({"portal-x": _rtsp_create("pc-2")}, version=4)
    [(image_source_id, source)] = world.accessor.sources.items()
    assert source["type"] == "RTSP" and len(world.fetcher.calls) == 2
    assert world.shadow.reported[-1]["cameras"]["cfg-" + image_source_id]["ack"] == "pc-2"

    old_timer()
    world.agent.pump()
    assert len(world.fetcher.calls) == 2 and len(world.accessor.sources) == 1
    assert len(world.timer.pending) == 1, "the superseded timer started another retry"
    assert SENTINEL not in json.dumps(world.shadow.reported) + json.dumps(world.shadow.desired)


# --- pump() with nothing pending ------------------------------------------------------------


def test_pump_returns_as_today_with_nothing_pending(tmp_path):
    world = World(tmp_path, _camera("cam"), debounce_seconds=5.0)
    assert world.agent.pump() is None, "an idle agent has nothing to do"

    world.agent.report_inventory()
    assert world.agent.pump() is None, "a successful report with no new request"
    assert len(world.shadow.reported) == 1

    world.agent.report_inventory()
    assert world.agent.pump() == pytest.approx(5.0)
    world.clock.now += 2.0
    assert world.agent.pump() == pytest.approx(3.0)
    assert len(world.shadow.reported) == 1
    world.clock.now += 3.0
    assert world.agent.pump() is None
    assert len(world.shadow.reported) == 2
