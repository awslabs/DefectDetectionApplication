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
"""StreamLeaseKeeper, watcher and manual-trigger tests.

Feature: rtsp-rtmp-stream-cameras (Requirements 8.10, 10.3, 11.7).

The keeper runs against the real WorkflowWatcher (temp artifact root,
sqlite) and the real StreamIngestManager with an inert fake session, so
lease accounting and the device session limit are the production ones:

- a registered stream workflow holds a lease on its camera, which is
  released when the registration is removed, superseded or invalidated,
  and moved when its camera changes;
- a lease refused at the session limit reports the registration invalid,
  with the refusal reason, until capacity frees;
- a device without stream workflows never creates the service;
- ``POST /workflows/registrations/{id}/trigger`` returns 409
  ``CONTINUOUS_WORKFLOW_RUNNING`` for a continuous registration that is
  not paused, and triggers every other registration exactly as before.
"""
import copy
import shutil
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_workflow_binding_store import bindings_state, get_row, make_store
from test_workflow_stream_executor import CAMERA_URL, make_stream_document
from workflow_engine_test_utils import (
    VALID_MANIFEST,
    make_session_factory,
    make_watcher,
    write_artifact_set,
)

from camera_sync import CameraSourceState
from stream_ingest.manager import StreamIngestManager, camera_key_for_url
from workflow_engine import api as workflow_engine_api
from workflow_engine import executor as executor_module
from workflow_engine import gst_plugins, runtime
from workflow_engine.models import WorkflowExecution
from workflow_engine.stream_leases import StreamLeaseKeeper

ANONYMOUS_KEY = camera_key_for_url(CAMERA_URL)


class InertSession:
    """A StreamSession stand-in: records its lease count and stop."""

    def __init__(self, camera_key, source_provider, capabilities, clock=None, on_health=None):
        self.camera_key = camera_key
        self.leases = 0
        self.stopped = None

    def start(self):
        pass

    def set_leases(self, count):
        self.leases = count

    def stop(self, reason):
        self.stopped = reason

    def restart(self, reason):
        pass

    def tick(self, now):
        pass

    def health(self):
        return {"cameraKey": self.camera_key, "state": "streaming"}


@pytest.fixture(autouse=True)
def no_registry_scan():
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture
def limit():
    return {"sessions": 4}


@pytest.fixture
def manager(limit):
    return StreamIngestManager(session_factory=InertSession, max_sessions=lambda: limit["sessions"],
                               capabilities=object(), idle_grace_s=0.0, supervise=False)


def wire(tmp_path, session_factory, manager, **watcher_kwargs):
    """A watcher with the keeper wired exactly as runtime.py wires it."""
    watcher = make_watcher(tmp_path, session_factory, **watcher_kwargs)
    keeper = StreamLeaseKeeper(
        session_factory=session_factory,
        resolution_provider=watcher.binding_resolution,
        resync=watcher._resync_for_bindings,
        manager_provider=lambda: manager,
    )
    watcher.lease_refusal_lookup = keeper.refusal_reason
    watcher.registrations_listeners.append(keeper.on_registrations_changed)
    return watcher, keeper


def manifest(version):
    return dict(VALID_MANIFEST, componentVersion="{0}.0.0".format(version), workflowVersion=int(version))


def write_stream_workflow(root, version="3", **kwargs):
    document = make_stream_document(**kwargs)
    document["workflowVersion"] = version
    return write_artifact_set(root, "wf-1", version, manifest=manifest(version), compiled=document)


class TestLeaseLifecycle:
    def test_a_registered_stream_workflow_holds_a_lease(self, tmp_path, session_factory, manager):
        write_stream_workflow(tmp_path)
        watcher, keeper = wire(tmp_path, session_factory, manager)

        watcher.sync_once()

        assert get_row(session_factory, "wf-1:3").status == "registered"
        assert keeper.held() == {"wf-1:3": ANONYMOUS_KEY}
        assert manager.lease_count(ANONYMOUS_KEY) == 1
        assert manager.session(ANONYMOUS_KEY).leases == 1

    def test_another_pass_changes_nothing(self, tmp_path, session_factory, manager):
        write_stream_workflow(tmp_path)
        watcher, keeper = wire(tmp_path, session_factory, manager)

        watcher.sync_once()
        watcher.sync_once()
        keeper.on_registrations_changed()

        assert manager.lease_count(ANONYMOUS_KEY) == 1

    def test_removal_releases_the_lease(self, tmp_path, session_factory, manager):
        path = write_stream_workflow(tmp_path)
        watcher, keeper = wire(tmp_path, session_factory, manager)
        watcher.sync_once()

        shutil.rmtree(path)
        watcher.sync_once()

        assert get_row(session_factory, "wf-1:3").status == "removed"
        assert keeper.held() == {}
        assert manager.lease_count(ANONYMOUS_KEY) == 0
        # With the idle grace spent, the camera disconnects.
        manager.tick()
        assert manager.session_keys() == []

    def test_supersession_releases_the_old_lease(self, tmp_path, session_factory, manager):
        write_stream_workflow(tmp_path, "3")
        watcher, keeper = wire(tmp_path, session_factory, manager)
        watcher.sync_once()

        write_stream_workflow(tmp_path, "4", url="rtsp://10.0.0.4/main")
        watcher.sync_once()

        new_key = camera_key_for_url("rtsp://10.0.0.4/main")
        assert get_row(session_factory, "wf-1:3").status == "superseded"
        assert keeper.held() == {"wf-1:4": new_key}
        assert manager.lease_count(ANONYMOUS_KEY) == 0
        assert manager.lease_count(new_key) == 1

    def test_invalidation_releases_the_lease(self, tmp_path, session_factory, manager):
        write_stream_workflow(tmp_path)
        store = make_store(bindings_state({}))
        watcher, keeper = wire(tmp_path, session_factory, manager, binding_store=store,
                               inventory_provider=lambda: [])
        watcher.sync_once()
        assert keeper.held() == {"wf-1:3": ANONYMOUS_KEY}

        store._shadow.state = bindings_state({"cam": {"cameraSourceId": "cfg-gone"}})
        watcher.on_bindings_delta()

        assert get_row(session_factory, "wf-1:3").status == "invalid"
        assert "missing camera source cfg-gone" in watcher.invalid_reason("wf-1:3")
        assert keeper.held() == {}
        assert manager.lease_count(ANONYMOUS_KEY) == 0

    def test_a_bound_camera_is_leased_and_a_rebinding_moves_the_lease(self, tmp_path, session_factory, manager):
        write_stream_workflow(tmp_path)
        inventory = [CameraSourceState(camera_source_id="cfg-7", name="Dock", type="RTSP",
                                       origin="edge-configured", params={"url": "rtsp://10.0.0.7/main"})]
        store = make_store(bindings_state({"cam": {"cameraSourceId": "cfg-7"}}))
        watcher, keeper = wire(tmp_path, session_factory, manager, binding_store=store,
                               inventory_provider=lambda: inventory)

        watcher.sync_once()
        assert keeper.held() == {"wf-1:3": "cfg-7"}

        store._shadow.state = bindings_state({})
        watcher.on_bindings_delta()

        assert keeper.held() == {"wf-1:3": ANONYMOUS_KEY}
        assert manager.lease_count("cfg-7") == 0
        assert manager.lease_count(ANONYMOUS_KEY) == 1

    def test_release_all(self, tmp_path, session_factory, manager):
        write_stream_workflow(tmp_path)
        watcher, keeper = wire(tmp_path, session_factory, manager)
        watcher.sync_once()

        keeper.release_all()

        assert keeper.held() == {}
        assert manager.lease_count(ANONYMOUS_KEY) == 0


class TestSessionLimit:
    def test_a_refused_lease_marks_the_registration_invalid_until_capacity_frees(
            self, tmp_path, session_factory, manager, limit):
        """8.10 / 10.3: a lease refused at the device session limit makes
        the registration invalid, with a reason naming the limit; the
        keeper retries on every pass and the registration returns once a
        session frees."""
        limit["sessions"] = 1
        live_view = manager.acquire_lease("cfg-other", "live-view")
        write_stream_workflow(tmp_path)
        watcher, keeper = wire(tmp_path, session_factory, manager)

        watcher.sync_once()

        assert get_row(session_factory, "wf-1:3").status == "invalid"
        reason = watcher.invalid_reason("wf-1:3")
        assert reason == keeper.refusal_reason("wf-1:3")
        assert reason.startswith("stream camera {0} could not be opened: ".format(
            "rtsp://192.168.1.64/Streaming/Channels/101"))
        assert "maximum of 1 stream camera sessions" in reason
        assert keeper.held() == {}
        # Retried on every pass, still refused.
        watcher.sync_once()
        assert get_row(session_factory, "wf-1:3").status == "invalid"

        manager.release_lease(live_view)
        manager.tick()
        watcher.sync_once()

        assert get_row(session_factory, "wf-1:3").status == "registered"
        assert watcher.invalid_reason("wf-1:3") is None
        assert keeper.refusal_reason("wf-1:3") is None
        assert keeper.held() == {"wf-1:3": ANONYMOUS_KEY}

    def test_a_refused_registration_that_is_removed_is_forgotten(self, tmp_path, session_factory, manager, limit):
        limit["sessions"] = 0
        path = write_stream_workflow(tmp_path)
        watcher, keeper = wire(tmp_path, session_factory, manager)
        watcher.sync_once()
        assert keeper.refusal_reason("wf-1:3") is not None

        shutil.rmtree(path)
        watcher.sync_once()

        assert keeper.refusal_reason("wf-1:3") is None
        assert get_row(session_factory, "wf-1:3").status == "removed"

    def test_workflows_on_one_camera_share_its_session(self, tmp_path, session_factory, manager, limit):
        limit["sessions"] = 1
        write_stream_workflow(tmp_path)
        other = make_stream_document()
        write_artifact_set(tmp_path, "wf-2", "1", manifest=dict(VALID_MANIFEST, workflowId="wf-2",
                                                                 workflowVersion=1), compiled=other)
        watcher, keeper = wire(tmp_path, session_factory, manager)

        watcher.sync_once()

        assert keeper.held() == {"wf-1:3": ANONYMOUS_KEY, "wf-2:1": ANONYMOUS_KEY}
        assert manager.lease_count(ANONYMOUS_KEY) == 2
        assert get_row(session_factory, "wf-2:1").status == "registered"


class TestIsolation:
    def test_a_device_without_stream_workflows_never_creates_the_service(self, tmp_path, session_factory):
        write_artifact_set(tmp_path)
        created = []
        watcher = make_watcher(tmp_path, session_factory)
        keeper = StreamLeaseKeeper(session_factory=session_factory,
                                   resolution_provider=watcher.binding_resolution,
                                   resync=watcher._resync_for_bindings,
                                   manager_provider=lambda: created.append(1))
        watcher.lease_refusal_lookup = keeper.refusal_reason
        watcher.registrations_listeners.append(keeper.on_registrations_changed)

        watcher.sync_once()
        keeper.release_all()

        assert created == []
        assert get_row(session_factory, "wf-1:3").status == "registered"

    def test_a_failing_refusal_lookup_leaves_the_status_unchanged(self, tmp_path, session_factory):
        write_stream_workflow(tmp_path)
        watcher = make_watcher(tmp_path, session_factory)

        def broken(registration_id):
            raise RuntimeError("keeper is gone")

        watcher.lease_refusal_lookup = broken
        watcher.sync_once()

        assert get_row(session_factory, "wf-1:3").status == "registered"

    def test_an_unwired_watcher_registers_as_before(self, tmp_path, session_factory):
        write_stream_workflow(tmp_path)
        watcher = make_watcher(tmp_path, session_factory)

        watcher.sync_once()

        assert watcher.lease_refusal_lookup is None
        assert get_row(session_factory, "wf-1:3").status == "registered"


# --- manual trigger guard (Requirement 11.7) ----------------------------------


@pytest.fixture
def client(session_factory):
    app = FastAPI()
    app.include_router(workflow_engine_api.router)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[workflow_engine_api.get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides = {}


class FakeContinuousManager:
    def __init__(self, statuses):
        self.statuses = statuses
        self.asked = []

    def status(self, registration_id):
        self.asked.append(registration_id)
        status = self.statuses.get(registration_id)
        return copy.deepcopy(status) if status is not None else None


def executions(session_factory):
    session = session_factory()
    try:
        return session.query(WorkflowExecution).count()
    finally:
        session.close()


class TestManualTriggerGuard:
    @pytest.fixture
    def registered(self, tmp_path, session_factory):
        write_stream_workflow(tmp_path, processing_mode="continuous")
        watcher = make_watcher(tmp_path, session_factory)
        watcher.sync_once()
        return watcher

    @pytest.mark.parametrize("state", ["running", "waiting_for_stream"])
    def test_a_running_continuous_workflow_rejects_a_manual_trigger(
            self, registered, client, session_factory, state):
        continuous = FakeContinuousManager({"wf-1:3": {"registrationId": "wf-1:3", "state": state}})
        with patch.object(runtime, "_watcher", registered), \
                patch.object(runtime, "_continuous_manager", continuous):
            response = client.post("/workflows/registrations/wf-1:3/trigger")

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert isinstance(detail, str)
        assert detail.startswith("CONTINUOUS_WORKFLOW_RUNNING: ")
        assert "pause it" in detail
        assert executions(session_factory) == 0

    def test_a_paused_continuous_workflow_accepts_a_manual_trigger(self, registered, client, session_factory):
        continuous = FakeContinuousManager({"wf-1:3": {"registrationId": "wf-1:3", "state": "paused"}})
        with patch.object(runtime, "_watcher", registered), \
                patch.object(runtime, "_continuous_manager", continuous), \
                patch.object(executor_module, "_executor", None):
            response = client.post("/workflows/registrations/wf-1:3/trigger")

        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        assert executions(session_factory) == 1

    @pytest.mark.parametrize("wired", [True, False])
    def test_a_registration_without_continuous_status_triggers_as_before(
            self, registered, client, session_factory, wired):
        continuous = FakeContinuousManager({}) if wired else None
        with patch.object(runtime, "_watcher", registered), \
                patch.object(runtime, "_continuous_manager", continuous), \
                patch.object(executor_module, "_executor", None):
            response = client.post("/workflows/registrations/wf-1:3/trigger")

        assert response.status_code == 200
        assert executions(session_factory) == 1
        if wired:
            assert continuous.asked == ["wf-1:3"]

    def test_an_invalid_registration_keeps_its_own_409(self, tmp_path, session_factory, client, manager, limit):
        limit["sessions"] = 0
        write_stream_workflow(tmp_path)
        watcher, _keeper = wire(tmp_path, session_factory, manager)
        watcher.sync_once()
        continuous = FakeContinuousManager({"wf-1:3": {"state": "running"}})

        with patch.object(runtime, "_watcher", watcher), \
                patch.object(runtime, "_continuous_manager", continuous):
            response = client.post("/workflows/registrations/wf-1:3/trigger")

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "is invalid and cannot be run" in detail
        assert "maximum of 0 stream camera sessions" in detail
        assert continuous.asked == []
