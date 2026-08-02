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
"""Tests for the additive WorkflowExecution run-observability columns.

Task 1 of deployed-workflow-run-observability: the new nullable columns
(capture_id, output_dir, has_image_results, node_status_json, log_path)
persist, and ``execution_to_dict`` exposes the new keys
(hasImageResults, captureId, outputDir) while preserving the existing
execution fields unchanged (Requirements 1.4, 8.3).
"""
import time

from workflow_engine_test_utils import make_session_factory

from workflow_engine.api import execution_to_dict
from workflow_engine.models import WorkflowExecution, WorkflowRegistration

# The four fields the /workflows/executions endpoints returned before this
# feature; they must remain present and unchanged (Requirement 8.3).
EXISTING_EXECUTION_KEYS = {
    "executionId",
    "registrationId",
    "status",
    "startedAt",
    "finishedAt",
    "failingNodeId",
    "error",
}
NEW_EXECUTION_KEYS = {"hasImageResults", "captureId", "outputDir"}


def _seed_registration(session, registration_id="wf-1:3"):
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


class TestObservabilityColumnsPersist:
    def test_new_columns_default_to_null_or_false_when_unset(self):
        session = make_session_factory()()
        try:
            _seed_registration(session)
            execution = WorkflowExecution(
                id="exec-defaults",
                registration_id="wf-1:3",
                status="pending",
                started_at=int(time.time()),
            )
            session.add(execution)
            session.commit()

            row = session.get(WorkflowExecution, "exec-defaults")
            assert row.capture_id is None
            assert row.output_dir is None
            assert row.node_status_json is None
            assert row.log_path is None
            # Boolean default False (client-side default applied on insert).
            assert bool(row.has_image_results) is False
        finally:
            session.close()

    def test_new_columns_persist_values(self):
        session = make_session_factory()()
        try:
            _seed_registration(session)
            execution = WorkflowExecution(
                id="exec-full",
                registration_id="wf-1:3",
                status="completed",
                started_at=1700000000,
                finished_at=1700000005,
                capture_id="wf-1-exec-full",
                output_dir="/aws_dda/captures/wf-1/exec-full",
                has_image_results=True,
                node_status_json='{"n1": {"status": "success"}}',
                log_path="/aws_dda/captures/wf-1/exec-full/run.log",
            )
            session.add(execution)
            session.commit()

            row = session.get(WorkflowExecution, "exec-full")
            assert row.capture_id == "wf-1-exec-full"
            assert row.output_dir == "/aws_dda/captures/wf-1/exec-full"
            assert bool(row.has_image_results) is True
            assert row.node_status_json == '{"n1": {"status": "success"}}'
            assert row.log_path == "/aws_dda/captures/wf-1/exec-full/run.log"
        finally:
            session.close()


class TestExecutionToDict:
    def test_includes_new_keys_and_preserves_existing(self):
        execution = WorkflowExecution(
            id="exec-1",
            registration_id="wf-1:3",
            status="completed",
            started_at=1700000000,
            finished_at=1700000005,
            failing_node_id=None,
            error=None,
            capture_id="wf-1-exec-1",
            output_dir="/aws_dda/captures/wf-1/exec-1",
            has_image_results=True,
        )

        payload = execution_to_dict(execution)

        # Existing shape unchanged (Requirement 8.3): all prior keys present.
        assert EXISTING_EXECUTION_KEYS.issubset(payload.keys())
        assert payload["executionId"] == "exec-1"
        assert payload["registrationId"] == "wf-1:3"
        assert payload["status"] == "completed"
        assert payload["startedAt"] == 1700000000
        assert payload["finishedAt"] == 1700000005
        assert payload["failingNodeId"] is None
        assert payload["error"] is None

        # New additive keys present with expected values.
        assert NEW_EXECUTION_KEYS.issubset(payload.keys())
        assert payload["hasImageResults"] is True
        assert payload["captureId"] == "wf-1-exec-1"
        assert payload["outputDir"] == "/aws_dda/captures/wf-1/exec-1"

    def test_has_image_results_is_false_when_unset(self):
        execution = WorkflowExecution(
            id="exec-2",
            registration_id="wf-1:3",
            status="pending",
        )

        payload = execution_to_dict(execution)

        assert payload["hasImageResults"] is False
        assert payload["captureId"] is None
        assert payload["outputDir"] is None
