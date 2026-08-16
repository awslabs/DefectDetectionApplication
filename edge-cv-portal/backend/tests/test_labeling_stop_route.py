"""
POST /labeling/{id}/stop in labeling.py (dda-data-labeling, task 9.1).

Feature: dda-data-labeling

Covers, against the moto-backed stack from conftest.py (real
shared_utils / rbac_middleware, synthetic API Gateway events with
Cognito claims):

- InProgress DDA job -> Stopped with `stopped_at` recorded and a
  `job_stopped` audit event carrying the acting user, job id, event
  type, and timestamp (Req 11.4, 11.7)
- Stop targets a non-InProgress DDA job (Stopped / Completed / Failed)
  -> 400 validation error, status unchanged (Req 11.9)
- Stop targets a Ground Truth job -> 400 validation error, status
  unchanged (DDA jobs only)
- Submitted annotations (task items) are retained untouched by a stop
  (Req 11.4)
- Stop failure (conditional write loses to a concurrent status change)
  -> job not overwritten, validation error per the concurrent status
  (Req 11.5/11.9 boundary)
- Caller without MANAGE_LABELING_JOBS (Viewer) -> 403 via the real
  @rbac_check path, status unchanged
- Unknown job id -> 404
"""
import json
import sys
import uuid

import boto3 as real_boto3
import pytest
from boto3.dynamodb.conditions import Attr

REGION = "us-east-1"


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def labeling(aws_stack):
    """The real labeling module imported inside the moto mock."""
    sys.modules.pop("labeling", None)
    import labeling

    return labeling


class StopEnv:
    """Per-test helper facade with a fresh Use_Case id."""

    def __init__(self, stack, labeling):
        self.stack = stack
        self.labeling = labeling
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.user = {
            "user_id": f"user-{uuid.uuid4()}",
            "email": "creator@example.com",
            "username": "creator",
            "role": "DataScientist",
        }

    # ------------------------------------------------------------ events
    def stop_event(self, job_id, user=None):
        user = user or self.user
        return {
            "httpMethod": "POST",
            "resource": "/labeling/{id}/stop",
            "path": f"/v1/labeling/{job_id}/stop",
            "pathParameters": {"id": job_id},
            "queryStringParameters": None,
            "body": None,
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": user["user_id"],
                        "email": user["email"],
                        "cognito:username": user["username"],
                        "custom:role": user["role"],
                    }
                }
            },
        }

    def stop(self, job_id, user=None):
        response = self.labeling.handler(self.stop_event(job_id, user), None)
        return response["statusCode"], json.loads(response["body"])

    # ------------------------------------------------------------- store
    def put_job(self, **attrs):
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": "seeded job",
            "created_at": 1,
            "status": "InProgress",
            "labeling_backend": "DDA",
            "task_type": "Classification",
            "image_count": 2,
        }
        item.update(attrs)
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def put_task(self, job_id, index, assignee, status="Assigned", **attrs):
        item = {
            "job_id": job_id,
            "task_id": f"task-{index:06d}",
            "usecase_id": self.usecase_id,
            "assignee_user_id": assignee,
            "status": status,
        }
        item.update(attrs)
        self.stack.tables.labeling_tasks.put_item(Item=item)
        return item

    def get_job(self, job_id):
        return self.stack.tables.labeling_jobs.get_item(
            Key={"job_id": job_id}).get("Item")

    def get_tasks(self, job_id):
        return self.stack.tables.labeling_tasks.query(
            KeyConditionExpression=real_boto3.dynamodb.conditions.Key(
                "job_id").eq(job_id)).get("Items", [])

    def audit_events(self, action, job_id):
        return self.stack.tables.audit_log.scan(
            FilterExpression=(Attr("action").eq(action)
                              & Attr("resource_id").eq(job_id))
        ).get("Items", [])


@pytest.fixture
def env(aws_stack, labeling):
    return StopEnv(aws_stack, labeling)


# ----------------------------------------------------------- happy path

class TestStopInProgress:
    def test_in_progress_becomes_stopped_with_stopped_at(self, env):
        """Req 11.4: InProgress -> Stopped, stop timestamp recorded."""
        job_id = env.put_job()

        status, response = env.stop(job_id)
        assert status == 200, response
        assert response["status"] == "Stopped"
        assert isinstance(response["stopped_at"], int)

        job = env.get_job(job_id)
        assert job["status"] == "Stopped"
        assert int(job["stopped_at"]) == response["stopped_at"]

    def test_stop_writes_job_stopped_audit_event(self, env):
        """Req 11.7: audit event with acting user, job id, event type,
        and timestamp."""
        job_id = env.put_job()

        status, _ = env.stop(job_id)
        assert status == 200

        events = env.audit_events("job_stopped", job_id)
        assert len(events) == 1
        event = events[0]
        assert event["user_id"] == env.user["user_id"]
        assert event["resource_type"] == "labeling_job"
        assert event["resource_id"] == job_id
        assert event["result"] == "success"
        assert int(event["timestamp"]) > 0
        assert event["details"]["usecase_id"] == env.usecase_id

    def test_submitted_annotations_are_retained(self, env):
        """Req 11.4: every task item (including submitted annotations)
        is untouched by the stop."""
        job_id = env.put_job(image_count=3)
        labeler = f"labeler-{uuid.uuid4().hex[:8]}"
        seeded = [
            env.put_task(job_id, 0, labeler, status="Submitted",
                         annotation={"label": "anomaly"},
                         submitted_by=labeler, submitted_at=42),
            env.put_task(job_id, 1, labeler, status="Submitted",
                         annotation={"label": "normal"},
                         submitted_by=labeler, submitted_at=43),
            env.put_task(job_id, 2, labeler, status="Assigned"),
        ]

        status, _ = env.stop(job_id)
        assert status == 200

        tasks = sorted(env.get_tasks(job_id), key=lambda t: t["task_id"])
        assert tasks == sorted(seeded, key=lambda t: t["task_id"])


# ------------------------------------------------------ validation errors

class TestStopValidation:
    @pytest.mark.parametrize("current_status",
                             ["Stopped", "Completed", "Failed"])
    def test_non_in_progress_rejected_status_unchanged(self, env,
                                                       current_status):
        """Req 11.9: non-InProgress -> 400 naming the status; the job
        status is unchanged and no audit event is written."""
        job_id = env.put_job(status=current_status, stopped_at=7)

        status, response = env.stop(job_id)
        assert status == 400
        assert current_status in response["error"]
        assert response["status"] == current_status

        job = env.get_job(job_id)
        assert job["status"] == current_status
        assert int(job["stopped_at"]) == 7  # untouched
        assert env.audit_events("job_stopped", job_id) == []

    def test_ground_truth_job_rejected(self, env):
        """DDA jobs only: a Ground Truth job (explicit or legacy without
        the attribute) is rejected with a validation error."""
        explicit_id = env.put_job(labeling_backend="GroundTruth")
        legacy_item = {
            "job_id": f"job-{uuid.uuid4().hex[:12]}",
            "usecase_id": env.usecase_id,
            "job_name": "legacy gt job",
            "created_at": 1,
            "status": "InProgress",
        }
        env.stack.tables.labeling_jobs.put_item(Item=legacy_item)

        for job_id in (explicit_id, legacy_item["job_id"]):
            status, response = env.stop(job_id)
            assert status == 400
            assert response["labeling_backend"] == "GroundTruth"
            assert env.get_job(job_id)["status"] == "InProgress"
            assert env.audit_events("job_stopped", job_id) == []

    def test_unknown_job_returns_404(self, env):
        status, response = env.stop(f"job-{uuid.uuid4().hex[:12]}")
        assert status == 404
        assert "not found" in response["error"].lower()

    def test_concurrent_status_change_is_not_overwritten(self, env,
                                                         monkeypatch):
        """Req 11.5/11.9 boundary: the conditional write makes the
        transition atomic — a status change between the read and the
        write is answered as a validation error and never clobbered."""
        job_id = env.put_job()
        table = env.stack.tables.labeling_jobs
        original_update = env.labeling.labeling_jobs_table.update_item

        def racing_update(**kwargs):
            # A concurrent writer completes the job just before the
            # conditional stop write lands.
            table.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET #status = :c",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":c": "Completed"},
            )
            return original_update(**kwargs)

        monkeypatch.setattr(env.labeling.labeling_jobs_table,
                            "update_item", racing_update)

        status, response = env.stop(job_id)
        assert status == 400
        assert response["status"] == "Completed"
        assert env.get_job(job_id)["status"] == "Completed"
        assert env.audit_events("job_stopped", job_id) == []


# ----------------------------------------------------------- authorization

class TestStopAuthorization:
    def test_viewer_denied_with_403_status_unchanged(self, env):
        """MANAGE_LABELING_JOBS is required: a Viewer is denied through
        the real @rbac_check path and the job is unchanged."""
        job_id = env.put_job()
        viewer = {
            "user_id": f"user-{uuid.uuid4()}",
            "email": "viewer@example.com",
            "username": "viewer",
            "role": "Viewer",
        }

        status, response = env.stop(job_id, user=viewer)
        assert status == 403
        assert env.get_job(job_id)["status"] == "InProgress"
        assert env.audit_events("job_stopped", job_id) == []
