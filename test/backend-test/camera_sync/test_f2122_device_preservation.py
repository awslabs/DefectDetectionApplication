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
"""Preservation for the device side of task 29 (rtsp-rtmp-stream-cameras
29.2 and 29.3; Requirements 5.6, 5.11).

Everything here behaves the same before the fix (``2ae4645``) and after it:

- the refusals that stay: any change to a ``disc-`` id or a static id,
  creates included; an update or delete of an ``arv-`` id; an update or an
  unsupported op on a ``portal-`` id. Each is refused as
  ``discovery-managed`` with its ``portalChangeId``, nothing is fetched, the
  accessor is never called, and the desired entry is cleared;
- creates under ``arv-`` and ``portal-`` ids are applied, acknowledged under
  the new ``cfg-`` key and mirrored under the create's id;
- a delete of a missing ``cfg-`` id fails with the accessor's 404 verbatim,
  and a delete of an existing one removes it with no failure;
- a fetch failure that is not a denial fails the change at once, with exactly
  ``credential retrieval failed: <code>``, creates nothing and starts no
  timer; a credential-free stream create fetches nothing.

Self-contained like the bug-condition file (its own dict-backed fakes, the
agent built through a signature filter), so it runs on the unfixed tree.
"""
import contextlib
import copy
import inspect
import itertools
import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")

from fastapi import HTTPException  # noqa: E402

from camera_sync import REASON_DISCOVERY_MANAGED, CameraSyncStateStore, EdgeSyncAgent  # noqa: E402
from camera_sync import agent as agent_module  # noqa: E402
from stream_ingest.credential_fetch import CredentialFetchError  # noqa: E402

SENTINEL = "pw-F2122-PRES-9a0d"
REF = {"secretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
                    "dda-portal/stream-camera-credentials/thing/portal-x-AbCdEf",
       "versionId": "0b4f7e7c-1d7a-4f45-9d3a-2d1c5c9f7a11"}


# --- fakes (copied from the bug-condition file, so each file stands alone) ----------


class RecordingShadow:
    def __init__(self):
        self.reported = []
        self.desired = []

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        return None

    def update_thing_shadow_state_request(self, thing_name, shadow_name, state):
        if "reported" in state:
            self.reported.append(copy.deepcopy(state["reported"]))
        if "desired" in state:
            self.desired.append(copy.deepcopy(state["desired"]))


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
        if data.get("type") == "Folder" and not data.get("location"):
            raise HTTPException(status_code=400, detail="location is required when image source type is Folder")
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
    def __init__(self, tmp_path, *sources, outcomes=()):
        self.shadow = RecordingShadow()
        self.accessor = DictAccessor(*sources)
        self.fetcher = ScriptedFetcher(*outcomes)
        self.timer = RecordingTimer()
        wanted = dict(
            iot_shadow_accessor=self.shadow, image_source_accessor=self.accessor, camera_discovery=None,
            db_session_factory=lambda: contextlib.nullcontext(),
            state_store=CameraSyncStateStore(str(tmp_path / "state.json")),
            thing_name="thing", clock=Clock(), wall_clock=lambda: 1_790_000_000.0,
            debounce_seconds=0.0, pin_worker=NoMarkerPinWorker(), video_pin_worker=NoMarkerPinWorker(),
            stream_timer=RecordingTimer(), credential_fetcher=self.fetcher,
            credential_store=NoCredentials(), change_retry_timer=self.timer)
        accepted = inspect.signature(EdgeSyncAgent.__init__).parameters
        self.agent = EdgeSyncAgent(**{key: value for key, value in wanted.items() if key in accepted})

    def apply(self, csid, change):
        self.agent.apply_desired_changes({csid: change})
        self.agent.pump()
        return self.shadow.reported[-1]


def _camera(image_source_id):
    return {"imageSourceId": image_source_id, "name": "cam", "type": "Camera",
            "cameraId": "camera-" + image_source_id, "imageSourceConfiguration": {}}


def _change(op, source_type, pcid, credentialed=True):
    if op == "delete":
        return {"op": "delete", "portalChangeId": pcid}
    if source_type == "RTSP":
        params = {"url": "rtsp://10.0.4.21:554/Streaming/Channels/101"}
        if credentialed:
            params.update(credentialRef=dict(REF), credentialsConfigured=True,
                          credentialsUpdatedAt=1_790_000_000_123)
        else:
            params.update(credentialsConfigured=False)
        return {"op": op, "portalChangeId": pcid, "name": "Dock 3", "type": "RTSP", "params": params}
    if source_type == "Folder":
        return {"op": op, "portalChangeId": pcid, "name": "Folder", "type": "Folder",
                "params": {"location": "/aws_dda/images"}}
    return {"op": op, "portalChangeId": pcid, "name": "hijack", "type": "Camera",
            "params": {"devicePath": "/dev/video0", "cameraId": "cam-1"}}


def _no_sentinel(world):
    assert SENTINEL not in json.dumps(world.shadow.reported) + json.dumps(world.shadow.desired)


# --- refusals that stay ---------------------------------------------------------------

_REFUSED = (
    [(csid, op, source_type)
     for csid in ("disc-000000000001", "static-image-camera", "static-video-camera")
     for op in ("create", "update")
     for source_type in ("Camera", "RTSP")]
    + [(csid, "delete", None)
       for csid in ("disc-000000000001", "static-image-camera", "static-video-camera")]
    + [("arv-0123456789ab", "update", source_type) for source_type in ("Camera", "RTSP")]
    + [("arv-0123456789ab", "delete", None)]
    + [("portal-x", op, source_type) for op in ("update", "frobnicate")
       for source_type in ("Camera", "RTSP")]
)


@pytest.mark.parametrize("csid, op, source_type", _REFUSED)
def test_the_refusals_that_stay(tmp_path, csid, op, source_type):
    world = World(tmp_path, outcomes=[{"password": SENTINEL}])
    document = world.apply(csid, _change(op, source_type, "pc-7"))
    assert document["failures"] == {csid: {"reason": REASON_DISCOVERY_MANAGED, "portalChangeId": "pc-7"}}
    assert world.accessor.calls == [] and world.fetcher.calls == [] and world.timer.pending == []
    assert world.shadow.desired == [{"changes": {csid: None}}]


# --- applied as before ------------------------------------------------------------------


@pytest.mark.parametrize("csid", ["arv-0123456789ab", "portal-x"])
def test_a_create_under_an_arv_or_portal_id_is_applied(tmp_path, csid):
    world = World(tmp_path)
    document = world.apply(csid, _change("create", "Folder", "pc-8"))
    [(image_source_id, source)] = world.accessor.sources.items()
    assert source["type"] == "Folder" and source["location"] == "/aws_dda/images"
    assert document["cameras"]["cfg-" + image_source_id]["ack"] == "pc-8"
    assert document["cameras"][csid] == document["cameras"]["cfg-" + image_source_id]
    assert document["failures"] == {}
    assert world.shadow.desired == [{"changes": {csid: None}}]


def test_a_delete_of_a_missing_cfg_id_fails_with_the_accessors_404(tmp_path):
    world = World(tmp_path)
    document = world.apply("cfg-missing", _change("delete", None, "pc-9"))
    assert document["failures"] == {"cfg-missing": {
        "reason": "The server can't delete the image source. Error: 'The image source missing "
                  "doesn't exist'. Check the image source ID and try again.",
        "portalChangeId": "pc-9"}}
    assert world.accessor.calls == [("delete", "missing")]


def test_a_delete_of_an_existing_cfg_camera_removes_it(tmp_path):
    world = World(tmp_path, _camera("cam"))
    world.agent.report_inventory()
    world.agent.pump()
    assert "cfg-cam" in world.shadow.reported[-1]["cameras"]
    document = world.apply("cfg-cam", _change("delete", None, "pc-11"))
    assert world.accessor.sources == {}
    assert document["failures"] == {}
    assert document["cameras"]["cfg-cam"] is None, "the deleted camera's key is retired"


@pytest.mark.parametrize("code", ["ResourceNotFoundException", "MalformedSecret", "InvalidReference"])
def test_a_fetch_failure_that_is_not_a_denial_fails_at_once(tmp_path, code):
    world = World(tmp_path, outcomes=[CredentialFetchError(code)])
    document = world.apply("portal-x", _change("create", "RTSP", "pc-12"))
    assert document["failures"] == {"portal-x": {
        "reason": "credential retrieval failed: " + code, "portalChangeId": "pc-12"}}
    assert world.accessor.sources == {} and world.timer.pending == []
    assert world.fetcher.calls == [REF]
    assert world.shadow.desired == [{"changes": {"portal-x": None}}]


def test_a_credential_free_stream_create_fetches_nothing(tmp_path):
    world = World(tmp_path)
    document = world.apply("portal-x", _change("create", "RTSP", "pc-13", credentialed=False))
    assert world.fetcher.calls == []
    [(image_source_id, source)] = world.accessor.sources.items()
    assert source["type"] == "RTSP" and world.accessor.credentials == {}
    assert document["cameras"]["cfg-" + image_source_id]["ack"] == "pc-13"
    assert document["cameras"]["cfg-" + image_source_id]["params"]["credentialsConfigured"] is False
    assert document["failures"] == {}
    _no_sentinel(world)
