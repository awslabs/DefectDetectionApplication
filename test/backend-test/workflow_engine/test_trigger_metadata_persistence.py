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
"""Example test for persisted trigger metadata (Task 6.5).

Through the executor harness: a run whose execution row carries a trigger
context persists Run_Metadata (the ``{output_dir}/{capture_id}.json``
file ``_persist_run_metadata`` writes) containing the seeded ``trigger``
key, so the payload that drove the run is visible in run observability.

_Requirements: 2.8_
"""
import json
import os
import time
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import (
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import gst_plugins, pipeline_executor
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)


class _FakePipelineManager:
    def __init__(self, tag_values):
        self._tag_values = tag_values

    def run_pipeline(self, pipeline_str, frame_data=None,
                     latency_metrics=None, status_sink=None):
        return dict(self._tag_values)


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


def _seed_run(session_factory, artifact_path, trigger_context_json):
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id="wf-1:3",
            workflow_id="wf-1",
            version="3",
            arch="x86_64",
            artifact_path=str(artifact_path),
            status="registered",
            registered_at=int(time.time()),
        ))
        session.add(WorkflowExecution(
            id="exec-1",
            registration_id="wf-1:3",
            started_at=int(time.time()),
            status=EXECUTION_STATUS_PENDING,
            trigger_context_json=trigger_context_json,
        ))
        session.commit()
    finally:
        session.close()
    return "exec-1"


def test_persisted_run_metadata_contains_the_seeded_trigger(
    tmp_path, session_factory
):
    """A run whose execution row carries an MQTT trigger context persists
    Run_Metadata JSON containing the seeded ``trigger`` key — the loaded
    context, with ``payload_json`` derived from the payload string.

    _Requirements: 2.8_
    """
    context = {
        "topic": "factory/line-4/inspect",
        "payload": json.dumps({"part_id": "XYZ-042",
                               "image": "s3://plant-images/xyz-042.png"}),
        "qos": 1,
        "timestamp": 1760000000.25,
    }
    artifact_path = write_artifact_set(tmp_path)
    execution_id = _seed_run(
        session_factory, artifact_path, json.dumps(context)
    )
    capture_root = os.path.join(str(tmp_path), "captures")

    with patch.object(
        pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root
    ):
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=(
                lambda: _FakePipelineManager({"is_anomalous": False})
            ),
        ).execute(execution_id)

    session = session_factory()
    try:
        row = session.get(WorkflowExecution, execution_id)
        assert row.status == EXECUTION_STATUS_COMPLETED
        output_dir = row.output_dir
        capture_id = row.capture_id
    finally:
        session.close()

    metadata_path = os.path.join(output_dir, "{0}.json".format(capture_id))
    with open(metadata_path, "r", encoding="utf-8") as f:
        persisted = json.load(f)

    expected_trigger = dict(context)
    expected_trigger["payload_json"] = {
        "part_id": "XYZ-042",
        "image": "s3://plant-images/xyz-042.png",
    }
    assert persisted["trigger"] == expected_trigger
    # The pipeline-produced metadata is persisted beside it, unchanged.
    assert persisted["is_anomalous"] is False
