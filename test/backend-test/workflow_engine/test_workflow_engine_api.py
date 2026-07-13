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
"""Tests for the workflow engine /workflows endpoints (Requirements 9.1, 13.3).

Uses a standalone FastAPI app containing only the workflow engine router
with an in-memory database, so the tests run without GStreamer or the
full LocalServer app import.
"""
import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from workflow_engine_test_utils import make_session_factory

from workflow_engine import api as workflow_engine_api
from workflow_engine import executor
from workflow_engine.models import WorkflowExecution, WorkflowRegistration


@pytest.fixture
def session_factory():
    return make_session_factory()


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


def seed_registration(session_factory, registration_id="wf-1:3", status="registered"):
    session = session_factory()
    try:
        session.add(
            WorkflowRegistration(
                id=registration_id,
                workflow_id=registration_id.split(":")[0],
                version=registration_id.split(":")[1],
                arch="x86_64",
                artifact_path=f"/aws_dda/workflows/{registration_id.replace(':', '/')}",
                status=status,
                registered_at=int(time.time()),
            )
        )
        session.commit()
    finally:
        session.close()


class TestListRegistrations:
    def test_empty_list(self, client):
        response = client.get("/workflows/registrations")
        assert response.status_code == 200
        assert response.json() == []

    def test_lists_registrations(self, client, session_factory):
        seed_registration(session_factory, "wf-a:1", "registered")
        seed_registration(session_factory, "wf-b:2", "invalid")

        with patch(
            "workflow_engine.runtime.invalid_reason", return_value="bad manifest"
        ):
            response = client.get("/workflows/registrations")

        assert response.status_code == 200
        body = response.json()
        assert [r["registrationId"] for r in body] == ["wf-a:1", "wf-b:2"]
        valid, invalid = body
        assert valid["status"] == "registered"
        assert "invalidReason" not in valid
        assert invalid["status"] == "invalid"
        assert invalid["invalidReason"] == "bad manifest"


class TestGetRegistration:
    def test_unknown_registration_404(self, client):
        response = client.get("/workflows/registrations/nope:1")
        assert response.status_code == 404

    def test_returns_registration_with_executions(self, client, session_factory):
        seed_registration(session_factory, "wf-a:1")
        trigger = client.post("/workflows/registrations/wf-a:1/trigger")
        assert trigger.status_code == 200

        response = client.get("/workflows/registrations/wf-a:1")
        assert response.status_code == 200
        body = response.json()
        assert body["registrationId"] == "wf-a:1"
        assert len(body["executions"]) == 1
        assert body["executions"][0]["status"] == "pending"


class TestTrigger:
    def test_trigger_creates_pending_execution(self, client, session_factory):
        seed_registration(session_factory, "wf-a:1")

        response = client.post("/workflows/registrations/wf-a:1/trigger")

        assert response.status_code == 200
        body = response.json()
        assert body["registrationId"] == "wf-a:1"
        assert body["status"] == "pending"
        assert body["executionId"]

        session = session_factory()
        try:
            row = session.get(WorkflowExecution, body["executionId"])
            assert row is not None
            assert row.status == "pending"
        finally:
            session.close()

    def test_trigger_invalid_registration_rejected(self, client, session_factory):
        # Invalid registrations can never be run (9.1, 13.3)
        seed_registration(session_factory, "wf-bad:1", status="invalid")

        with patch(
            "workflow_engine.runtime.invalid_reason", return_value="wrong arch"
        ):
            response = client.post("/workflows/registrations/wf-bad:1/trigger")

        assert response.status_code == 409
        assert "wrong arch" in response.json()["detail"]

        session = session_factory()
        try:
            assert session.query(WorkflowExecution).count() == 0
        finally:
            session.close()

    def test_trigger_unknown_registration_404(self, client):
        response = client.post("/workflows/registrations/nope:1/trigger")
        assert response.status_code == 404

    def test_trigger_dispatches_to_registered_executor(
        self, client, session_factory
    ):
        import threading

        seed_registration(session_factory, "wf-a:1")
        received = []
        done = threading.Event()

        def fake_executor(execution_id):
            received.append(execution_id)
            done.set()

        executor.set_executor(fake_executor)
        try:
            response = client.post("/workflows/registrations/wf-a:1/trigger")
            assert response.status_code == 200
            assert done.wait(timeout=5)
            assert received == [response.json()["executionId"]]
        finally:
            executor.set_executor(None)


class TestExecutionStatus:
    def test_unknown_execution_404(self, client):
        response = client.get("/workflows/executions/nope")
        assert response.status_code == 404

    def test_returns_execution(self, client, session_factory):
        seed_registration(session_factory, "wf-a:1")
        execution_id = client.post(
            "/workflows/registrations/wf-a:1/trigger"
        ).json()["executionId"]

        response = client.get(f"/workflows/executions/{execution_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["executionId"] == execution_id
        assert body["status"] == "pending"
        assert body["failingNodeId"] is None
        assert body["error"] is None


class TestExecutorHook:
    def test_dispatch_without_executor_returns_false(self):
        executor.set_executor(None)
        assert executor.dispatch("exec-1") is False

    def test_dispatch_contains_executor_exceptions(self):
        import threading

        done = threading.Event()

        def failing_executor(execution_id):
            done.set()
            raise RuntimeError("boom")

        executor.set_executor(failing_executor)
        try:
            assert executor.dispatch("exec-1") is True
            assert done.wait(timeout=5)
        finally:
            executor.set_executor(None)
