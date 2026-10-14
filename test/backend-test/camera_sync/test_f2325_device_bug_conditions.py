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
"""Bug-condition exploration for finding 23 in the Edge_Sync_Agent
(rtsp-rtmp-stream-cameras task 30.3; Requirement 5.13).

On thor1 the clear of an applied change timed out, because it ran on the IPC
callback thread. Until a clear lands, every delta carries the entry again (a
delta is desired minus reported, and ``changes`` is never reported), and the
unfixed agent applies it again. The shadow here merges writes as AWS IoT does
and fails the next N desired writes with ``TimeoutError``, as thor1's clear
did. Each test FAILS on the unfixed agent (``ac74c0b``) on its assertion:

- ``test_f23_redelivered_create_makes_one_camera``: a redelivered create
  makes a second camera.
- ``test_f23_redelivered_delete_reports_no_failure``: a redelivered delete
  fails with "... doesn't exist".
- ``test_f23_catch_up_skips_a_processed_change``: the activation catch-up
  applies a processed create again.
- ``test_f23_failed_clear_is_retried_until_it_lands``: nothing retries a
  failed clear, so the entry stays in ``desired.changes``.
- ``test_f23_worker_wakes_for_the_catch_up_during_a_backoff``: the report
  worker sleeps through its backoff, so the catch-up's report waits 60 s.

Fixed, the agent applies each ``(csid, portalChangeId)`` at most once per
process, retries a failed clear from ``pump()``, and wakes its worker for the
catch-up (design section 5).

Self-contained, so it runs unchanged on the unfixed tree: its own dict-backed
fakes and the agent built through a signature filter (as in
``test_f2122_device_bug_conditions.py``), and the catch-up reached with
``getattr(agent, "on_subscription_active", None)``. The unfixed tree has no
such method: there the catch-up is what ``server_setup`` does at start (the
GET of ``desired.changes``, then ``apply_desired_changes``), and the wake-up
request is ``report_inventory()``.
"""
import contextlib
import copy
import inspect
import itertools
import os
import threading
import time
from typing import Mapping

import pytest

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
os.environ.setdefault("AWS_IOT_THING_NAME", "iot_thing_test")

from fastapi import HTTPException  # noqa: E402

from camera_sync import CameraSyncStateStore, EdgeSyncAgent  # noqa: E402
from camera_sync import agent as agent_module  # noqa: E402

THING = "thing"
SHADOW = "dda-camera-registry"


# --- fakes -------------------------------------------------------------------------


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
    """The camera-registry shadow with AWS update semantics (the merge of
    ``f2122_stream_agent_support.MergingShadow``). ``state`` is the merged
    document; ``reported`` and ``desired`` keep the writes that landed.
    ``fail_desired`` / ``fail_reported`` fail that many of the next desired /
    reported writes with ``TimeoutError``, as thor1's clear failed; a failed
    write changes nothing."""

    def __init__(self, state=None):
        self.state = copy.deepcopy(state) if state else {}
        self.reported = []
        self.desired = []
        self.failed = []
        self.fail_desired = 0
        self.fail_reported = 0
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
            if "reported" in update and self.fail_reported > 0:
                self.fail_reported -= 1
                self.failed.append(copy.deepcopy(update))
                raise TimeoutError("the report timed out")
            merge(self.state, update)
            if "reported" in update:
                self.reported.append(copy.deepcopy(update["reported"]))
            if "desired" in update:
                self.desired.append(copy.deepcopy(update["desired"]))

    def desired_changes(self):
        with self._lock:
            desired = self.state.get("desired") or {}
            return copy.deepcopy(desired.get("changes") or {})


class DictAccessor:
    """The ImageSourceAccessor surface the agent uses, over a dict (the one of
    ``test_f2122_device_bug_conditions.py``)."""

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


def _agent(tmp_path, shadow, accessor, clock, **overrides):
    """The agent, given only the seams this tree's agent accepts."""
    wanted = dict(
        iot_shadow_accessor=shadow, image_source_accessor=accessor, camera_discovery=None,
        db_session_factory=lambda: contextlib.nullcontext(),
        state_store=CameraSyncStateStore(str(tmp_path / "state.json")),
        thing_name=THING, clock=clock, wall_clock=lambda: 1_790_000_000.0,
        debounce_seconds=0.0, pin_worker=NoMarkerPinWorker(), video_pin_worker=NoMarkerPinWorker(),
        stream_timer=RecordingTimer(), credential_store=NoCredentials(),
        change_retry_timer=RecordingTimer())
    wanted.update(overrides)
    accepted = inspect.signature(EdgeSyncAgent.__init__).parameters
    return EdgeSyncAgent(**{key: value for key, value in wanted.items() if key in accepted})


def _create(pcid, name="Folder"):
    return {"op": "create", "portalChangeId": pcid, "name": name, "type": "Folder",
            "params": {"location": "/aws_dda/images"}}


def _delete(pcid):
    return {"op": "delete", "portalChangeId": pcid}


def _camera(image_source_id):
    return {"imageSourceId": image_source_id, "name": "cam", "type": "Camera",
            "cameraId": "camera-" + image_source_id, "imageSourceConfiguration": {}}


def _delta(changes, version):
    """A camera-registry ``update/delta`` document, as on_delta receives it."""
    return {"state": {"changes": copy.deepcopy(changes)}, "version": version}


def _portal_writes(shadow, changes):
    """The Portal's desired write (camera_registry.write_desired_change)."""
    shadow.update_thing_shadow_state_request(THING, SHADOW, {"desired": {"changes": copy.deepcopy(changes)}})


def _catch_up(agent, shadow):
    """The activation catch-up: ``on_subscription_active()`` when this tree's
    agent has it, else what the unfixed tree does at start (``server_setup``'s
    GET of ``desired.changes``, then ``apply_desired_changes``)."""
    on_active = getattr(agent, "on_subscription_active", None)
    if on_active is not None:
        return on_active()
    state = shadow.get_thing_shadow_state_request(agent.thing_name, agent.shadow_name)
    desired = state.get("desired") if isinstance(state, dict) else None
    changes = desired.get("changes") if isinstance(desired, dict) else None
    if isinstance(changes, dict) and changes:
        agent.apply_desired_changes(changes)
    return True


def _acked(shadow, change_id):
    """The cfg- cameras any landed report acknowledged ``change_id`` on."""
    return sorted({csid for document in shadow.reported
                   for csid, entry in (document.get("cameras") or {}).items()
                   if csid.startswith("cfg-") and isinstance(entry, Mapping)
                   and entry.get("ack") == change_id})


# --- the bug conditions -------------------------------------------------------------


def test_f23_redelivered_create_makes_one_camera(tmp_path):
    """A create whose clear failed is carried by the next delta again; it must
    not make a second camera."""
    shadow, accessor = MergingShadow(), DictAccessor()
    agent = _agent(tmp_path, shadow, accessor, Clock())
    _portal_writes(shadow, {"portal-a": _create("pc-1", "Dock A")})
    shadow.fail_desired = 1

    agent.on_delta(_delta({"portal-a": _create("pc-1", "Dock A")}, version=2))
    agent.pump()
    assert len(accessor.sources) == 1 and shadow.failed, "setup: the create's clear did not fail"

    _portal_writes(shadow, {"portal-b": _create("pc-2", "Dock B")})
    agent.on_delta(_delta(shadow.desired_changes(), version=3))
    agent.pump()

    names = sorted(source["name"] for source in accessor.sources.values())
    assert len(accessor.sources) == 2, (
        "the redelivered create of portal-a (pc-1) was applied again: {} cameras {} for two "
        "creates (finding 23)".format(len(accessor.sources), names))
    assert _acked(shadow, "pc-1") == ["cfg-is-1"], (
        "pc-1 was acknowledged on {}".format(_acked(shadow, "pc-1")))
    assert _acked(shadow, "pc-2") == ["cfg-is-2"]


def test_f23_redelivered_delete_reports_no_failure(tmp_path):
    """A delete whose clear failed is carried by the next delta again; the
    camera is already gone, and the redelivery must not fail."""
    shadow, accessor = MergingShadow(), DictAccessor(_camera("cam"))
    agent = _agent(tmp_path, shadow, accessor, Clock())
    agent.report_inventory()
    agent.pump()
    assert "cfg-cam" in shadow.reported[-1]["cameras"], "setup: the camera was never reported"
    _portal_writes(shadow, {"cfg-cam": _delete("pc-3")})
    shadow.fail_desired = 1

    agent.on_delta(_delta({"cfg-cam": _delete("pc-3")}, version=2))
    agent.pump()
    assert accessor.sources == {} and shadow.failed, "setup: the delete's clear did not fail"

    agent.on_delta(_delta(shadow.desired_changes(), version=3))
    agent.pump()

    failure = (shadow.reported[-1].get("failures") or {}).get("cfg-cam")
    assert failure is None, (
        "the redelivered delete pc-3 of cfg-cam was applied again and reported as failed: "
        "{} (finding 23)".format(failure))


def test_f23_catch_up_skips_a_processed_change(tmp_path):
    """The activation catch-up reads ``desired.changes`` again; a create it
    already applied must not make a second camera."""
    shadow, accessor = MergingShadow(), DictAccessor()
    agent = _agent(tmp_path, shadow, accessor, Clock())
    _portal_writes(shadow, {"portal-a": _create("pc-1")})
    shadow.fail_desired = 1

    agent.on_delta(_delta({"portal-a": _create("pc-1")}, version=2))
    agent.pump()
    assert len(accessor.sources) == 1 and shadow.failed, "setup: the create's clear did not fail"

    _catch_up(agent, shadow)
    agent.pump()

    assert len(accessor.sources) == 1, (
        "the activation catch-up applied the processed create pc-1 of portal-a again: {} "
        "cameras (finding 23)".format(len(accessor.sources)))


def test_f23_failed_clear_is_retried_until_it_lands(tmp_path):
    """A clear that failed is retried until it lands, without waiting for a
    delta redelivery."""
    shadow, accessor, clock = MergingShadow(), DictAccessor(), Clock()
    agent = _agent(tmp_path, shadow, accessor, clock)
    _portal_writes(shadow, {"portal-a": _create("pc-1")})
    shadow.fail_desired = 1

    agent.on_delta(_delta({"portal-a": _create("pc-1")}, version=2))
    agent.pump()
    assert shadow.failed and "portal-a" in shadow.desired_changes(), (
        "setup: the create's clear did not fail")

    for _ in range(10):
        if "portal-a" not in shadow.desired_changes():
            break
        clock.now += 2.0
        agent.pump()

    assert "portal-a" not in shadow.desired_changes(), (
        "the failed clear of portal-a (pc-1) was never retried: desired.changes still holds "
        "it after 10 pumps over 20 s (finding 23)")
    assert len(accessor.sources) == 1


def test_f23_worker_wakes_for_the_catch_up_during_a_backoff(tmp_path):
    """While the report worker waits out a 60 s backoff, the catch-up gets a
    report written at once."""
    shadow, accessor, clock = MergingShadow(), DictAccessor(), Clock()
    shadow.fail_reported = 1
    agent = _agent(tmp_path, shadow, accessor, clock,
                   backoff_initial_seconds=60.0, backoff_max_seconds=60.0)
    agent.start()
    try:
        # The first report fails and arms the 60 s backoff: wait until the
        # worker has recorded it, so the catch-up comes after it.
        deadline = time.monotonic() + 5.0
        while agent._not_before <= clock.now and time.monotonic() < deadline:
            time.sleep(0.005)
        assert shadow.failed and agent._not_before > clock.now, (
            "setup: the first report did not fail into its 60 s backoff")

        on_active = getattr(agent, "on_subscription_active", None)
        requested_at = time.monotonic()
        if on_active is not None:
            on_active()
        else:
            agent.report_inventory()
        while not shadow.reported and time.monotonic() - requested_at < 1.0:
            time.sleep(0.01)

        assert shadow.reported, (
            "no report was written within 1 s of the activation catch-up: the report worker "
            "sleeps through its 60 s backoff instead of waking (finding 23)")
    finally:
        agent.stop()
