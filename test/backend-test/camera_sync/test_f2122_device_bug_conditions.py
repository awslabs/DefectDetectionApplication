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
"""Bug-condition exploration for finding 21 on the device
(rtsp-rtmp-stream-cameras tasks 29.2 and 29.3; Requirements 5.6, 5.11).

Both tests FAIL on the unfixed agent (``2ae4645``), on their assertions:

- ``test_f21a_denied_first_fetch_is_retried_not_failed``: the first
  credentialed change of a use case is denied while the device read grant
  propagates, and the unfixed agent fails the change at once, for good.
  Fixed, the change is parked and retried 2 s later, and succeeds.
- ``test_f21c_delete_of_a_failed_create_is_acknowledged``: the unfixed agent
  refuses the delete of a create it never applied as ``discovery-managed``,
  so the Portal entry can never be removed. Fixed, the delete is
  acknowledged and the old failure key is nulled in the next report.

The file is self-contained, so it runs unchanged on the unfixed tree: its own
dict-backed fakes (no ``utils.camera_manager`` import, which a 3.14 host
cannot do), and the agent is built through a signature filter, so a seam the
unfixed agent lacks (``change_retry_timer``) is simply not passed there.
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

from camera_sync import CameraSyncStateStore, EdgeSyncAgent  # noqa: E402
from camera_sync import agent as agent_module  # noqa: E402
from stream_ingest.credential_fetch import CredentialFetchError  # noqa: E402

SENTINEL = "pw-F2122-BUG-5c1e"
REF = {"secretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
                    "dda-portal/stream-camera-credentials/thing/portal-x-AbCdEf",
       "versionId": "0b4f7e7c-1d7a-4f45-9d3a-2d1c5c9f7a11"}


# --- fakes -------------------------------------------------------------------------


class RecordingShadow:
    """Keeps the raw reported and desired writes."""

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
    """The ImageSourceAccessor surface the agent uses, over a dict. The
    credentials a create or update carries are kept apart, the way the real
    accessor puts them in the Credential_Store, never in the record."""

    def __init__(self):
        self.sources = {}
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


def _agent(tmp_path, shadow, accessor, fetcher, retry_timer):
    """The agent, given only the seams this tree's agent accepts."""
    wanted = dict(
        iot_shadow_accessor=shadow, image_source_accessor=accessor, camera_discovery=None,
        db_session_factory=lambda: contextlib.nullcontext(),
        state_store=CameraSyncStateStore(str(tmp_path / "state.json")),
        thing_name="thing", clock=Clock(), wall_clock=lambda: 1_790_000_000.0,
        debounce_seconds=0.0, pin_worker=NoMarkerPinWorker(), video_pin_worker=NoMarkerPinWorker(),
        stream_timer=RecordingTimer(), credential_fetcher=fetcher, credential_store=NoCredentials(),
        change_retry_timer=retry_timer)
    accepted = inspect.signature(EdgeSyncAgent.__init__).parameters
    return EdgeSyncAgent(**{key: value for key, value in wanted.items() if key in accepted})


def _rtsp_create(pcid):
    return {"op": "create", "portalChangeId": pcid, "name": "Dock 3", "type": "RTSP",
            "params": {"url": "rtsp://10.0.4.21:554/Streaming/Channels/101", "credentialRef": dict(REF),
                       "credentialsConfigured": True, "credentialsUpdatedAt": 1_790_000_000_123}}


def _no_sentinel(shadow):
    assert SENTINEL not in json.dumps(shadow.reported) + json.dumps(shadow.desired)


# --- the bug conditions -------------------------------------------------------------


def test_f21a_denied_first_fetch_is_retried_not_failed(tmp_path):
    """Finding 21 (a): a denied first fetch is retried, not failed for good."""
    shadow, accessor, timer = RecordingShadow(), DictAccessor(), RecordingTimer()
    fetcher = ScriptedFetcher(CredentialFetchError("AccessDeniedException"),
                              {"username": "viewer", "password": SENTINEL})
    agent = _agent(tmp_path, shadow, accessor, fetcher, timer)

    agent.apply_desired_changes({"portal-x": _rtsp_create("pc-1")})
    agent.pump()

    assert shadow.desired[-1] == {"changes": {"portal-x": None}}
    assert all("portal-x" not in document["failures"] for document in shadow.reported), (
        "the denied first fetch failed the change: {}".format(
            [document["failures"] for document in shadow.reported]))
    assert [delay for delay, _ in timer.pending] == [2.0]

    _, action = timer.pending.pop()
    action()
    agent.pump()

    [source] = accessor.sources.values()
    assert source["type"] == "RTSP"
    assert accessor.credentials[source["imageSourceId"]]["password"] == SENTINEL
    document = shadow.reported[-1]
    assert document["cameras"]["cfg-" + source["imageSourceId"]]["ack"] == "pc-1"
    assert document["cameras"]["portal-x"]["ack"] == "pc-1"
    assert document["failures"] == {}
    _no_sentinel(shadow)


def test_f21c_delete_of_a_failed_create_is_acknowledged(tmp_path):
    """Finding 21 (c): the delete of a create the device never applied is
    acknowledged, and the create's failure key is removed from the shadow."""
    shadow, accessor, timer = RecordingShadow(), DictAccessor(), RecordingTimer()
    fetcher = ScriptedFetcher(CredentialFetchError("ResourceNotFoundException"))
    agent = _agent(tmp_path, shadow, accessor, fetcher, timer)

    agent.apply_desired_changes({"portal-x": _rtsp_create("pc-1")})
    agent.pump()
    assert shadow.reported[-1]["failures"]["portal-x"] == {
        "reason": "credential retrieval failed: ResourceNotFoundException", "portalChangeId": "pc-1"}
    assert accessor.sources == {} and timer.pending == []

    agent.apply_desired_changes({"portal-x": {"op": "delete", "portalChangeId": "pc-2"}})
    agent.pump()

    assert shadow.desired[-1] == {"changes": {"portal-x": None}}
    failures = shadow.reported[-1]["failures"]
    assert "portal-x" in failures and failures["portal-x"] is None, (
        "the delete was not acknowledged: failures = {}".format(failures))
    assert accessor.sources == {}
    _no_sentinel(shadow)
