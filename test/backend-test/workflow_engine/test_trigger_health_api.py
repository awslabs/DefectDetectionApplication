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
"""Example tests for Trigger_Health surfacing on the registration detail
endpoint (Requirements 9.1, 9.2; task 9.2).

Uses the same standalone-FastAPI-app pattern as
``test_workflow_engine_api.py`` and patches
``workflow_engine.runtime.trigger_health`` (the api.py source of the
additive ``triggerHealth`` field) so no manager threads or transports
are needed.
"""
import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from workflow_engine_test_utils import make_session_factory

from workflow_engine import api as workflow_engine_api
from workflow_engine.models import WorkflowRegistration

REGISTRATION_ID = "wf-health:1"

#: Representative Trigger_Health wire records across states (design
#: "Trigger_Health record" shape): a subscribed MQTT node (no mechanism
#: key — mechanism is OPC UA only), a polling OPC UA node entered by
#: auto-fallback, and a failed node carrying lastError/reconnectAttempts.
HEALTH_RECORDS = [
    {
        "nodeId": "trig-mqtt",
        "state": "subscribed",
        "reconnectAttempts": 0,
        "lastError": None,
    },
    {
        "nodeId": "trig-opcua",
        "state": "polling",
        "mechanism": "poll",
        "autoFallback": True,
        "reconnectAttempts": 0,
        "lastError": None,
    },
    {
        "nodeId": "trig-failed",
        "state": "failed",
        "reconnectAttempts": 5,
        "lastError": (
            "Trigger 'trig-failed' failed after 5 reconnect attempts: "
            "factory/line1 — connection refused"
        ),
    },
]


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


def seed_registration(session_factory, registration_id=REGISTRATION_ID):
    session = session_factory()
    try:
        session.add(
            WorkflowRegistration(
                id=registration_id,
                workflow_id=registration_id.split(":")[0],
                version=registration_id.split(":")[1],
                arch="x86_64",
                artifact_path=f"/aws_dda/workflows/{registration_id.replace(':', '/')}",
                status="registered",
                registered_at=int(time.time()),
            )
        )
        session.commit()
    finally:
        session.close()


def get_detail(client):
    response = client.get(f"/workflows/registrations/{REGISTRATION_ID}")
    assert response.status_code == 200
    return response.json()


class TestTriggerHealthSurfacing:
    def test_triggerless_registration_response_unchanged(
        self, client, session_factory
    ):
        # Requirement 9.2: no trigger nodes → no triggerHealth key and the
        # response byte-identical to the un-patched baseline.
        seed_registration(session_factory)
        baseline = get_detail(client)

        with patch("workflow_engine.runtime.trigger_health", return_value=[]):
            body = get_detail(client)

        assert "triggerHealth" not in body
        assert body == baseline

    def test_trigger_health_records_surfaced_verbatim(
        self, client, session_factory
    ):
        # Requirement 9.1: the additive triggerHealth field carries the
        # manager's records verbatim across representative states.
        seed_registration(session_factory)

        with patch(
            "workflow_engine.runtime.trigger_health",
            return_value=HEALTH_RECORDS,
        ) as mock_health:
            body = get_detail(client)

        mock_health.assert_called_once_with(REGISTRATION_ID)
        assert body["triggerHealth"] == HEALTH_RECORDS

        subscribed, polling, failed = body["triggerHealth"]
        # MQTT subscribed record: no mechanism key (OPC UA only).
        assert subscribed["state"] == "subscribed"
        assert "mechanism" not in subscribed
        # OPC UA polling record entered by auto-fallback.
        assert polling["state"] == "polling"
        assert polling["mechanism"] == "poll"
        assert polling["autoFallback"] is True
        # Failed record carries the actionable error and attempt count.
        assert failed["state"] == "failed"
        assert failed["reconnectAttempts"] == 5
        assert "trig-failed" in failed["lastError"]

    def test_existing_fields_unchanged_when_health_present(
        self, client, session_factory
    ):
        # Requirement 9.2: triggerHealth is additive — every existing
        # response field stays exactly as in the un-patched baseline.
        seed_registration(session_factory)
        baseline = get_detail(client)

        with patch(
            "workflow_engine.runtime.trigger_health",
            return_value=HEALTH_RECORDS,
        ):
            body = get_detail(client)

        health = body.pop("triggerHealth")
        assert health == HEALTH_RECORDS
        assert body == baseline

    def test_manager_not_running_response_unchanged(
        self, client, session_factory
    ):
        # Requirement 9.2: no manager → runtime.trigger_health returns []
        # through its real code path and the response stays unchanged.
        seed_registration(session_factory)
        baseline = get_detail(client)

        with patch(
            "workflow_engine.runtime.get_trigger_manager", return_value=None
        ):
            body = get_detail(client)

        assert "triggerHealth" not in body
        assert body == baseline
