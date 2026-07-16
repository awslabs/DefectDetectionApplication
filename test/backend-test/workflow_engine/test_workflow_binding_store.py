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
"""CameraBindingStore and WorkflowWatcher binding integration (task 14.4).

Feature: camera-registry-sync (Requirements 10.2, 10.4, 11.1).

- The store reads ``desired.bindings`` from the ``dda-camera-bindings``
  shadow, caches successful reads, never caches failures, and refreshes
  on delta invalidation.
- The watcher marks a registration invalid with reason ``missing camera
  source {csid}`` when a bound camera has no local inventory entry
  (10.2), and ``bindings unavailable`` when the shadow is unreadable and
  the document carries binding points (11.1); legacy documents without
  binding points register as today either way.
- Invalid registrations flip to registered on discovery ``on_change``
  and bindings-shadow delta re-resolution (10.4).
"""
import copy

import pytest

from workflow_engine_test_utils import (
    VALID_COMPILED,
    make_session_factory,
    make_watcher,
    write_artifact_set,
)

from workflow_engine.camera_binding_store import (
    BINDINGS_SHADOW_NAME,
    CameraBindingStore,
    binding_key,
    bindings_delta_topic_prefix,
)
from workflow_engine.models import WorkflowRegistration
from workflow_engine.watcher import REASON_BINDINGS_UNAVAILABLE

THING = "test-thing"

#: A compiled document whose n1 element exposes a device slot.
BOUND_COMPILED = dict(
    copy.deepcopy(VALID_COMPILED),
    bindingPoints=[
        {
            "nodeId": "n1",
            "nodeType": "camera_source",
            "parameters": {"device": "/dev/video0"},
            "slots": [{"param": "device", "segment": 0, "element": 0, "arg": "device"}],
        }
    ],
)


class FakeShadowAccessor:
    """IoTShadowAccessor contract: state dict on success, ``False`` when
    the shadow does not exist, ``None`` on swallowed transport errors."""

    def __init__(self, state):
        self.state = state
        self.get_calls = 0

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        assert shadow_name == BINDINGS_SHADOW_NAME
        self.get_calls += 1
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


def bindings_state(version_bindings, workflow_id="wf-1", version="3"):
    return {
        "desired": {
            "bindings": {binding_key(workflow_id, version): version_bindings}
        }
    }


def make_store(state):
    return CameraBindingStore(FakeShadowAccessor(state), thing_name=THING)


def get_row(session_factory, registration_id):
    session = session_factory()
    try:
        row = session.get(WorkflowRegistration, registration_id)
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


@pytest.fixture
def session_factory():
    return make_session_factory()


class TestCameraBindingStore:
    def test_reads_bindings_for_version(self):
        store = make_store(bindings_state({"n1": {"cameraSourceId": "cfg-1"}}))

        assert store.bindings_for("wf-1", "3") == {
            "n1": {"cameraSourceId": "cfg-1"}
        }
        assert store.bindings_for("wf-other", "9") == {}

    def test_missing_shadow_is_readable_with_no_bindings(self):
        # A device that never received bindings has no shadow at all —
        # that is "no bindings", not "unavailable" (11.1).
        store = make_store(False)

        assert store.bindings_for("wf-1", "3") == {}

    def test_unreadable_shadow_returns_none_and_is_not_cached(self):
        accessor = FakeShadowAccessor(None)
        store = CameraBindingStore(accessor, thing_name=THING)

        assert store.bindings_for("wf-1", "3") is None
        # Failures never cache: the next read retries and recovers (10.4).
        accessor.state = bindings_state({"n1": {"cameraSourceId": "cfg-1"}})
        assert store.bindings_for("wf-1", "3") == {
            "n1": {"cameraSourceId": "cfg-1"}
        }

    def test_raising_accessor_is_unavailable(self):
        store = make_store(RuntimeError("ipc down"))

        assert store.bindings_for("wf-1", "3") is None

    def test_successful_reads_are_cached_until_invalidated(self):
        accessor = FakeShadowAccessor(bindings_state({}))
        store = CameraBindingStore(accessor, thing_name=THING)

        store.bindings_for("wf-1", "3")
        store.bindings_for("wf-1", "3")
        assert accessor.get_calls == 1

        accessor.state = bindings_state({"n1": {"cameraSourceId": "cfg-1"}})
        store.on_delta({"state": {"bindings": {}}})
        assert store.bindings_for("wf-1", "3") == {
            "n1": {"cameraSourceId": "cfg-1"}
        }
        assert accessor.get_calls == 2

    def test_delta_topic_prefix(self):
        assert bindings_delta_topic_prefix(THING) == (
            "$aws/things/test-thing/shadow/name/dda-camera-bindings/update/"
        )


class TestWatcherBindingIntegration:
    def test_missing_camera_source_marks_registration_invalid(
        self, tmp_path, session_factory
    ):
        # 10.2: unresolved cameraSourceId -> invalid with the resolver's
        # reason, so the existing invalid path rejects triggers.
        write_artifact_set(tmp_path, "wf-1", "3", compiled=BOUND_COMPILED)
        store = make_store(bindings_state({"n1": {"cameraSourceId": "cfg-gone"}}))
        watcher = make_watcher(
            tmp_path, session_factory,
            binding_store=store, inventory_provider=lambda: {},
        )

        watcher.sync_once()

        assert get_row(session_factory, "wf-1:3").status == "invalid"
        assert watcher.invalid_reason("wf-1:3") == "missing camera source cfg-gone"
        assert watcher.binding_resolution("wf-1:3") is None

    def test_resolved_bindings_register_with_substituted_document(
        self, tmp_path, session_factory
    ):
        write_artifact_set(tmp_path, "wf-1", "3", compiled=BOUND_COMPILED)
        store = make_store(bindings_state({"n1": {"cameraSourceId": "cfg-1"}}))
        inventory = {"cfg-1": {"params": {"devicePath": "/dev/video7"}}}
        watcher = make_watcher(
            tmp_path, session_factory,
            binding_store=store, inventory_provider=lambda: inventory,
        )

        watcher.sync_once()

        assert get_row(session_factory, "wf-1:3").status == "registered"
        resolution = watcher.binding_resolution("wf-1:3")
        element = resolution.document["segments"][0]["elements"][0]
        assert element["args"]["device"] == "/dev/video7"

    def test_unreadable_shadow_invalidates_binding_point_documents_only(
        self, tmp_path, session_factory
    ):
        # 11.1: bindings unavailable -> binding-point documents invalid,
        # legacy documents without bindingPoints register as today.
        write_artifact_set(tmp_path, "wf-1", "3", compiled=BOUND_COMPILED)
        write_artifact_set(tmp_path, "wf-legacy", "1")
        watcher = make_watcher(
            tmp_path, session_factory,
            binding_store=make_store(None), inventory_provider=lambda: {},
        )

        watcher.sync_once()

        assert get_row(session_factory, "wf-1:3").status == "invalid"
        assert watcher.invalid_reason("wf-1:3") == REASON_BINDINGS_UNAVAILABLE
        assert get_row(session_factory, "wf-legacy:1").status == "registered"

    def test_no_store_registers_binding_point_documents_as_today(
        self, tmp_path, session_factory
    ):
        write_artifact_set(tmp_path, "wf-1", "3", compiled=BOUND_COMPILED)
        watcher = make_watcher(tmp_path, session_factory)

        watcher.sync_once()

        assert get_row(session_factory, "wf-1:3").status == "registered"

    def test_discovery_change_flips_invalid_to_registered(
        self, tmp_path, session_factory
    ):
        # 10.4: the bound camera appears in the local inventory and the
        # discovery on_change re-resolution flips the registration.
        write_artifact_set(tmp_path, "wf-1", "3", compiled=BOUND_COMPILED)
        store = make_store(bindings_state({"n1": {"cameraSourceId": "cfg-1"}}))
        inventory = {}
        watcher = make_watcher(
            tmp_path, session_factory,
            binding_store=store, inventory_provider=lambda: dict(inventory),
        )

        watcher.sync_once()
        assert get_row(session_factory, "wf-1:3").status == "invalid"

        inventory["cfg-1"] = {"params": {"devicePath": "/dev/video7"}}
        watcher.on_discovery_change()

        assert get_row(session_factory, "wf-1:3").status == "registered"
        assert watcher.invalid_reason("wf-1:3") is None

    def test_bindings_delta_refreshes_cache_and_reresolves(
        self, tmp_path, session_factory
    ):
        # 10.4: a shadow delta invalidates the cached bindings; the rescan
        # resolves against the new desired document.
        write_artifact_set(tmp_path, "wf-1", "3", compiled=BOUND_COMPILED)
        accessor = FakeShadowAccessor(
            bindings_state({"n1": {"cameraSourceId": "cfg-gone"}})
        )
        store = CameraBindingStore(accessor, thing_name=THING)
        watcher = make_watcher(
            tmp_path, session_factory,
            binding_store=store, inventory_provider=lambda: {},
        )

        watcher.sync_once()
        assert get_row(session_factory, "wf-1:3").status == "invalid"

        # The portal re-binds to nothing (bindings pruned); without a
        # delta the cache still serves the stale map.
        accessor.state = bindings_state({})
        watcher.sync_once()
        assert get_row(session_factory, "wf-1:3").status == "invalid"

        watcher.on_bindings_delta({"state": {"bindings": None}})
        assert get_row(session_factory, "wf-1:3").status == "registered"
