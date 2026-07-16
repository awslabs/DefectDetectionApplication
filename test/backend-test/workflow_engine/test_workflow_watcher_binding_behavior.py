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
"""Watcher binding behavior at the trigger and execution seams (task 14.6).

Feature: camera-registry-sync (Requirements 10.2, 11.1).

Complements ``test_workflow_binding_store.py`` (task 14.4: store
semantics and watcher status flips) at the two seams it does not touch:

- **Trigger rejection (10.2)**: a registration invalidated by binding
  resolution (missing camera source, bindings unavailable) is rejected
  by ``POST /workflows/registrations/{id}/trigger`` exactly like any
  other invalid registration — 409, the resolver's reason in the detail,
  and no execution row created.
- **Pre-feature documents (11.1)**: a compiled document without
  ``bindingPoints`` registers unchanged whether the bindings shadow is
  unreadable, empty, or even carries stray bindings for its key;
  ``binding_resolution`` stays None, so the executor loads the stored
  artifact untouched and runs the compiled-in parameter values —
  launch string and on-disk bytes identical to pre-feature behavior.
"""
import os
import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_workflow_binding_store import (
    BOUND_COMPILED,
    bindings_state,
    get_row,
    make_store,
)
from test_workflow_pipeline_executor import FakePipelineManager
from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    make_watcher,
    write_artifact_set,
)

from workflow_engine import api as workflow_engine_api
from workflow_engine import runtime
from workflow_engine.models import WorkflowExecution
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)
from workflow_engine.watcher import REASON_BINDINGS_UNAVAILABLE

#: A pre-feature compiled document: fully rendered element args, no
#: ``bindingPoints`` section — exactly what every component packaged
#: before this feature carries (11.1).
LEGACY_COMPILED = {
    "schemaVersion": 1,
    "workflowId": "wf-legacy",
    "workflowVersion": "1",
    "targetArch": DEVICE_ARCH,
    "segments": [
        {
            "name": "s0",
            "elements": [
                {
                    "nodeId": "n1",
                    "factory": "v4l2src",
                    "args": {"device": "/dev/video0"},
                },
                {"nodeId": None, "factory": "fakesink", "args": {}},
            ],
        }
    ],
    "executorBindings": [],
    "pluginDependencies": [],
}

#: The launch string those compiled-in values rendered before this
#: feature existed — the executor must still produce it byte for byte.
EXPECTED_LEGACY_LAUNCH = "v4l2src device=/dev/video0 ! fakesink"

LEGACY_ID = "wf-legacy:1"


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture
def client(session_factory):
    """A standalone app with only the workflow engine router, sharing the
    watcher's database (mirrors test_workflow_engine_api.py)."""
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


def execution_count(session_factory):
    session = session_factory()
    try:
        return session.query(WorkflowExecution).count()
    finally:
        session.close()


def seed_pending_execution(session_factory, registration_id, execution_id="exec-1"):
    session = session_factory()
    try:
        session.add(
            WorkflowExecution(
                id=execution_id,
                registration_id=registration_id,
                started_at=int(time.time()),
                status=EXECUTION_STATUS_PENDING,
            )
        )
        session.commit()
    finally:
        session.close()
    return execution_id


class TestInvalidRegistrationTriggerRejection:
    """10.2: binding-invalidated registrations reject triggers through the
    same 409 path as any other invalid registration."""

    def test_missing_camera_source_rejects_trigger(
        self, tmp_path, session_factory, client
    ):
        write_artifact_set(tmp_path, "wf-1", "3", compiled=BOUND_COMPILED)
        store = make_store(bindings_state({"n1": {"cameraSourceId": "cfg-gone"}}))
        watcher = make_watcher(
            tmp_path, session_factory,
            binding_store=store, inventory_provider=lambda: {},
        )
        watcher.sync_once()
        assert get_row(session_factory, "wf-1:3").status == "invalid"

        with patch.object(runtime, "_watcher", watcher):
            response = client.post("/workflows/registrations/wf-1:3/trigger")

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "cannot be run" in detail
        assert "missing camera source cfg-gone" in detail
        assert execution_count(session_factory) == 0

    def test_bindings_unavailable_rejects_trigger(
        self, tmp_path, session_factory, client
    ):
        # An unreadable bindings shadow invalidates binding-point
        # documents (10.2, 11.1); the trigger rejection carries that
        # reason too.
        write_artifact_set(tmp_path, "wf-1", "3", compiled=BOUND_COMPILED)
        watcher = make_watcher(
            tmp_path, session_factory,
            binding_store=make_store(None), inventory_provider=lambda: {},
        )
        watcher.sync_once()
        assert get_row(session_factory, "wf-1:3").status == "invalid"

        with patch.object(runtime, "_watcher", watcher):
            response = client.post("/workflows/registrations/wf-1:3/trigger")

        assert response.status_code == 409
        assert REASON_BINDINGS_UNAVAILABLE in response.json()["detail"]
        assert execution_count(session_factory) == 0


#: Bindings-shadow states a legacy document must be indifferent to:
#: unreadable, readable with nothing bound, and readable with stray
#: bindings under the legacy document's own key (no bindingPoints means
#: nothing to substitute — resolve_bindings leaves it unchanged).
LEGACY_STORE_STATES = {
    "unreadable-shadow": None,
    "no-bindings": bindings_state({}, workflow_id="wf-legacy", version="1"),
    "stray-bindings": bindings_state(
        {"n1": {"cameraSourceId": "cfg-1"}}, workflow_id="wf-legacy", version="1"
    ),
}


class TestLegacyDocumentBehavior:
    """11.1: pre-feature compiled documents register and execute with
    their compiled-in values exactly as before this feature."""

    @pytest.mark.parametrize("state_name", sorted(LEGACY_STORE_STATES))
    def test_registers_with_no_binding_resolution(
        self, tmp_path, session_factory, state_name
    ):
        write_artifact_set(tmp_path, "wf-legacy", "1", compiled=LEGACY_COMPILED)
        watcher = make_watcher(
            tmp_path, session_factory,
            binding_store=make_store(LEGACY_STORE_STATES[state_name]),
            inventory_provider=lambda: {},
        )

        watcher.sync_once()

        assert get_row(session_factory, LEGACY_ID).status == "registered"
        assert watcher.invalid_reason(LEGACY_ID) is None
        # No resolution recorded: the executor path falls back to the
        # stored artifact untouched.
        assert watcher.binding_resolution(LEGACY_ID) is None

    @pytest.mark.parametrize(
        "state_name", ["unreadable-shadow", "stray-bindings"]
    )
    def test_executes_compiled_in_values_byte_identically(
        self, tmp_path, session_factory, state_name
    ):
        version_dir = write_artifact_set(
            tmp_path, "wf-legacy", "1", compiled=LEGACY_COMPILED
        )
        compiled_path = os.path.join(version_dir, "compiled_pipeline.json")
        with open(compiled_path, "rb") as f:
            original_bytes = f.read()

        watcher = make_watcher(
            tmp_path, session_factory,
            binding_store=make_store(LEGACY_STORE_STATES[state_name]),
            inventory_provider=lambda: {},
        )
        watcher.sync_once()
        assert get_row(session_factory, LEGACY_ID).status == "registered"
        assert watcher.binding_resolution(LEGACY_ID) is None

        execution_id = seed_pending_execution(session_factory, LEGACY_ID)
        manager = FakePipelineManager(tag_values={"is_anomalous": False})
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        ).execute(execution_id)

        # The run rendered exactly the compiled-in values (11.1).
        assert manager.calls == [EXPECTED_LEGACY_LAUNCH]
        session = session_factory()
        try:
            row = session.get(WorkflowExecution, execution_id)
            assert row.status == EXECUTION_STATUS_COMPLETED
            assert row.error is None
        finally:
            session.close()

        # The stored artifact was never rewritten: byte-identical.
        with open(compiled_path, "rb") as f:
            assert f.read() == original_bytes
