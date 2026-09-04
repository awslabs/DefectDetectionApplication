"""
Labeler read APIs in dda_labeling.py (dda-data-labeling, task 8.1).

Feature: dda-data-labeling

Covers, against the moto-backed stack from conftest.py (real
shared_utils / rbac_middleware, synthetic API Gateway events with
Cognito claims, moto DynamoDB + S3):

- GET /labeler/jobs: only InProgress DDA jobs of teams the caller is a
  current member of in which the caller holds >=1 unsubmitted task,
  with submitted/remaining/withheld counts; empty when none (Req 2.4,
  7.10)
- GET /labeler/jobs/{jobId}/next: presentation gating — Pending
  pre-labels, PresentationFailed and Inactive tasks are withheld while
  Available/Failed/absent pre-labels are served (Req 7.12, 8.6, 8.7);
  15-minute presigned image URL (Req 12.6); pre-label payload when
  Available (Req 8.3); instructions and example-image URLs included and
  omitted when absent (Req 7.2); completion payload with submitted and
  withheld counts when zero presentable tasks remain (Req 7.11)
- Ownership: another labeler's job/task answers 403 carrying no
  resource data plus a labeler_access_denied audit event (Req 2.6); a
  labeler removed from the team is no longer served (Req 2.4)
- GET /labeler/tasks/{taskId}/image-url: a fresh 15-minute presigned
  URL for the caller's own task (Req 12.7)
"""
import json
import sys
import time
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import boto3
import pytest

REGION = "us-east-1"
DATASET_BUCKET = "test-labeler-dataset"
ARTIFACTS_BUCKET = "test-portal-artifacts"  # conftest PORTAL_ARTIFACTS_BUCKET


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, plus
    the dataset bucket the presigned image URLs point at."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=DATASET_BUCKET)
    return SimpleNamespace(module=dda_labeling)


@pytest.fixture
def env(aws_stack, dda):
    """Per-test facade with a fresh Use_Case, team, and one Data_Labeler
    caller who is a member of the team."""
    return LabelerEnv(aws_stack, dda)


class LabelerEnv:
    def __init__(self, stack, dda):
        self.stack = stack
        self.dda = dda
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        # Single-account use case: root cross_account_role_arn makes
        # get_s3_client_for_bucket fall back to default (moto) creds.
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Labeler API Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        self.team_id = f"team-{uuid.uuid4()}"
        stack.tables.labeling_teams.put_item(Item={
            "team_id": self.team_id,
            "sk": "META",
            "usecase_id": self.usecase_id,
            "team_name": "Labeler Team",
            "created_at": 1,
            "created_by": "admin",
        })
        self.labeler = self.make_labeler()

    # ------------------------------------------------------------ setup
    def make_labeler(self, team_id=None, member=True):
        """A DataLabeler-only JWT user; when member=True they are also a
        current member of the team (user_id == member sub)."""
        user_id = f"labeler-{uuid.uuid4()}"
        user = {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": "DataLabeler",
        }
        if member:
            self.add_member(user_id, team_id)
        return user

    def add_member(self, user_id, team_id=None):
        self.stack.tables.labeling_teams.put_item(Item={
            "team_id": team_id or self.team_id,
            "sk": f"MEMBER#{user_id}",
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "added_at": 1,
            "added_by": "admin",
        })

    def remove_member(self, user_id, team_id=None):
        self.stack.tables.labeling_teams.delete_item(Key={
            "team_id": team_id or self.team_id,
            "sk": f"MEMBER#{user_id}",
        })

    def put_job(self, status="InProgress", team_id=None, backend="DDA",
                **attrs):
        job_id = f"labeling-{uuid.uuid4().hex[:8]}"
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": f"job-{job_id}",
            "labeling_backend": backend,
            "status": status,
            "task_type": "Classification",
            "label_set": ["normal", "anomaly"],
            "dataset_bucket": DATASET_BUCKET,
            "team_id": team_id or self.team_id,
            "created_at": 1,
        }
        item.update(attrs)
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def put_task(self, job_id, task_id, assignee, status="Assigned",
                 prelabel_status=None, prelabel_s3_key=None,
                 prelabel_error=None):
        image_key = f"images/{job_id}/{task_id}.jpg"
        item = {
            "job_id": job_id,
            "task_id": task_id,
            "image_s3_uri": f"s3://{DATASET_BUCKET}/{image_key}",
            "image_key": image_key,
            "usecase_id": self.usecase_id,
            "assignee_user_id": assignee,
            "status": status,
        }
        if prelabel_status is not None:
            item["prelabel_status"] = prelabel_status
        if prelabel_s3_key is not None:
            item["prelabel_s3_key"] = prelabel_s3_key
        if prelabel_error is not None:
            item["prelabel_error"] = prelabel_error
        self.stack.tables.labeling_tasks.put_item(Item=item)
        return item

    def put_prelabel(self, job_id, task_id, payload):
        key = f"labeling/{self.usecase_id}/{job_id}/prelabels/{task_id}.json"
        self.s3.put_object(Bucket=ARTIFACTS_BUCKET, Key=key,
                           Body=json.dumps(payload).encode())
        return key

    # ------------------------------------------------------------ invoke
    def event(self, method, resource, user, path_params=None, query=None):
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", value)
        return {
            "httpMethod": method,
            "resource": resource,
            "path": path,
            "pathParameters": path_params or None,
            "queryStringParameters": query,
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

    def invoke(self, method, resource, user=None, path_params=None,
               query=None):
        response = self.dda.module.handler(
            self.event(method, resource, user or self.labeler,
                       path_params, query), None)
        return response["statusCode"], json.loads(response["body"])

    def list_jobs(self, user=None):
        return self.invoke("GET", "/labeler/jobs", user=user)

    def next_task(self, job_id, user=None):
        return self.invoke("GET", "/labeler/jobs/{jobId}/next", user=user,
                           path_params={"jobId": job_id})

    def image_url(self, task_id, user=None, job_id=None):
        return self.invoke(
            "GET", "/labeler/tasks/{taskId}/image-url", user=user,
            path_params={"taskId": task_id},
            query={"job_id": job_id} if job_id else None)

    # ------------------------------------------------------------- audit
    def denial_audit_events(self, user=None):
        response = self.stack.tables.audit_log.scan()
        caller = (user or self.labeler)["user_id"]
        return [item for item in response.get("Items", [])
                if item.get("action") == "labeler_access_denied"
                and item.get("user_id") == caller]


def presigned_expiry_seconds(url):
    """The expiry window of a presigned URL in seconds (SigV4
    X-Amz-Expires, or SigV2 absolute Expires minus now)."""
    params = parse_qs(urlparse(url).query)
    if "X-Amz-Expires" in params:
        return int(params["X-Amz-Expires"][0])
    assert "Expires" in params, f"not a presigned URL: {url}"
    return int(params["Expires"][0]) - int(time.time())


# ------------------------------------------------------------- job listing

class TestListLabelerJobs:
    def test_lists_jobs_with_unsubmitted_tasks_and_counts(self, env):
        """Req 2.4/7.10: jobs where the caller holds >=1 unsubmitted
        task, with the caller's submitted/remaining/withheld counts."""
        caller = env.labeler["user_id"]
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", caller, status="Assigned")
        env.put_task(job_id, "task-0000001", caller, status="Assigned")
        env.put_task(job_id, "task-0000002", caller, status="Submitted")
        env.put_task(job_id, "task-0000003", caller,
                     status="PresentationFailed")
        # Another labeler's tasks in the same job never affect the
        # caller's counts (Req 7.1).
        other = env.make_labeler()
        env.put_task(job_id, "task-0000004", other["user_id"])

        status, body = env.list_jobs()
        assert status == 200
        assert body["count"] == 1
        job = body["jobs"][0]
        assert job["job_id"] == job_id
        assert job["job_name"] == f"job-{job_id}"
        assert job["task_type"] == "Classification"
        assert job["label_set"] == ["normal", "anomaly"]
        assert job["submitted_count"] == 1
        assert job["remaining_count"] == 2
        assert job["withheld_count"] == 1

    def test_empty_list_when_no_assigned_tasks(self, env):
        """Req 2.4: empty result when no Task_Assignments exist."""
        status, body = env.list_jobs()
        assert status == 200
        assert body == {"jobs": [], "count": 0}

    def test_job_with_all_tasks_submitted_not_listed(self, env):
        caller = env.labeler["user_id"]
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", caller, status="Submitted")
        status, body = env.list_jobs()
        assert status == 200
        assert body["jobs"] == []

    def test_non_in_progress_and_non_dda_jobs_not_listed(self, env):
        caller = env.labeler["user_id"]
        stopped = env.put_job(status="Stopped")
        env.put_task(stopped, "task-0000000", caller)
        gt_job = env.put_job(backend="GroundTruth")
        env.put_task(gt_job, "task-0000000", caller)
        status, body = env.list_jobs()
        assert status == 200
        assert body["jobs"] == []

    def test_removed_member_no_longer_served(self, env):
        """Req 2.4: a labeler removed from the team stops seeing the
        team's jobs even while their task assignments still exist."""
        caller = env.labeler["user_id"]
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", caller)

        status, body = env.list_jobs()
        assert status == 200 and body["count"] == 1

        env.remove_member(caller)
        status, body = env.list_jobs()
        assert status == 200
        assert body == {"jobs": [], "count": 0}


# ---------------------------------------------------------------- next task

class TestNextTaskGating:
    def test_serves_presentable_and_withholds_pending_and_failed(self, env):
        """Req 7.12/8.6/8.7: Pending pre-labels, PresentationFailed and
        Inactive tasks are withheld; the lowest presentable task id is
        served."""
        caller = env.labeler["user_id"]
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", caller,
                     prelabel_status="Pending")
        env.put_task(job_id, "task-0000001", caller,
                     status="PresentationFailed")
        env.put_task(job_id, "task-0000002", caller, status="Inactive")
        key = env.put_prelabel(job_id, "task-0000003",
                               {"modality": "Classification",
                                "label": "anomaly"})
        env.put_task(job_id, "task-0000003", caller,
                     prelabel_status="Available", prelabel_s3_key=key)
        env.put_task(job_id, "task-0000004", caller,
                     prelabel_status="Failed")
        env.put_task(job_id, "task-0000005", caller)  # no prelabel field

        served = []
        for _ in range(3):
            status, body = env.next_task(job_id)
            assert status == 200
            assert body["complete"] is False
            served.append(body["task_id"])
            # Mark it submitted so the next call advances.
            env.stack.tables.labeling_tasks.update_item(
                Key={"job_id": job_id, "task_id": body["task_id"]},
                UpdateExpression="SET #s = :submitted",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":submitted": "Submitted"})

        # Available (0003), Failed (0004) and absent (0005) pre-label
        # statuses are all served, in task-id order; Pending (0000),
        # PresentationFailed (0001) and Inactive (0002) never are.
        assert served == ["task-0000003", "task-0000004", "task-0000005"]

    def test_payload_counts_and_presigned_url_expiry(self, env):
        """Req 7.10/12.6: counts ride along; the image URL is a
        presigned GET scoped to the task's image, expiring in <=900 s."""
        caller = env.labeler["user_id"]
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", caller)
        env.put_task(job_id, "task-0000001", caller, status="Submitted")

        before = int(time.time())
        status, body = env.next_task(job_id)
        assert status == 200
        assert body["task_id"] == "task-0000000"
        assert body["job_id"] == job_id
        assert body["task_type"] == "Classification"
        assert body["label_set"] == ["normal", "anomaly"]
        assert body["submitted_count"] == 1
        assert body["remaining_count"] == 1
        assert body["withheld_count"] == 0

        url = body["image_url"]
        assert f"images/{job_id}/task-0000000.jpg" in url
        assert 0 < presigned_expiry_seconds(url) <= 900
        assert before + 900 <= body["image_url_expires_at"] \
            <= int(time.time()) + 900

    def test_prelabel_payload_included_when_available(self, env):
        """Req 8.3: the pre-label loaded from prelabel_s3_key in the
        portal artifacts bucket rides along when Available."""
        caller = env.labeler["user_id"]
        job_id = env.put_job()
        prelabel = {"modality": "Classification", "label": "anomaly"}
        key = env.put_prelabel(job_id, "task-0000000", prelabel)
        env.put_task(job_id, "task-0000000", caller,
                     prelabel_status="Available", prelabel_s3_key=key)

        status, body = env.next_task(job_id)
        assert status == 200
        assert body["prelabel"] == prelabel

    def test_no_prelabel_key_when_not_available(self, env):
        caller = env.labeler["user_id"]
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", caller,
                     prelabel_status="Failed")
        status, body = env.next_task(job_id)
        assert status == 200
        assert "prelabel" not in body

    def test_failed_task_served_bare_with_status_and_reason(self, env):
        """Feature: llm-auto-labeling, task 12.2 (Req 7.5, 10.4): a
        task whose LLM pre-label generation Failed is presented to a
        team labeler as a bare image — no prelabel payload — carrying
        its prelabel_status and retained failure reason."""
        caller = env.labeler["user_id"]
        job_id = env.put_job(
            auto_label={"enabled": True,
                        "model": "llm:us.amazon.nova-pro-v1:0",
                        "detection_prompt": "Find every scratch"})
        reason = "model error: guidance did not parse"
        env.put_task(job_id, "task-0000000", caller,
                     prelabel_status="Failed", prelabel_error=reason)

        status, body = env.next_task(job_id)
        assert status == 200
        assert body["complete"] is False
        assert body["task_id"] == "task-0000000"
        assert "prelabel" not in body
        assert body["prelabel_status"] == "Failed"
        assert body["prelabel_error"] == reason
        # Still a normal presentable task: image URL and label set ride
        # along for annotation from scratch.
        assert f"images/{job_id}/task-0000000.jpg" in body["image_url"]
        assert body["label_set"] == ["normal", "anomaly"]

    def test_available_task_carries_no_prelabel_error(self, env):
        """Req 10.4 counterpart: an Available pre-label rides along
        with prelabel_status Available and no error field."""
        caller = env.labeler["user_id"]
        job_id = env.put_job()
        prelabel = {"modality": "Classification", "label": "anomaly"}
        key = env.put_prelabel(job_id, "task-0000000", prelabel)
        env.put_task(job_id, "task-0000000", caller,
                     prelabel_status="Available", prelabel_s3_key=key)

        status, body = env.next_task(job_id)
        assert status == 200
        assert body["prelabel"] == prelabel
        assert body["prelabel_status"] == "Available"
        assert "prelabel_error" not in body

    def test_instructions_and_example_urls_included(self, env):
        """Req 7.2: stored instructions and good/bad example images are
        presented beside the task."""
        caller = env.labeler["user_id"]
        job_id = env.put_job(
            instructions="Mark every scratch",
            example_images={"good": ["examples/good1.jpg",
                                     "examples/good2.png"],
                            "bad": ["examples/bad1.jpg"]})
        env.put_task(job_id, "task-0000000", caller)

        status, body = env.next_task(job_id)
        assert status == 200
        assert body["instructions"] == "Mark every scratch"
        examples = body["example_images"]
        assert len(examples["good"]) == 2
        assert len(examples["bad"]) == 1
        for url in examples["good"] + examples["bad"]:
            assert "examples/" in url
            assert 0 < presigned_expiry_seconds(url) <= 900

    def test_absent_instructions_and_examples_omitted(self, env):
        """Req 7.2: a job without instructions or examples presents the
        image without the absent items."""
        caller = env.labeler["user_id"]
        job_id = env.put_job(instructions="", example_images={"good": [],
                                                              "bad": []})
        env.put_task(job_id, "task-0000000", caller)

        status, body = env.next_task(job_id)
        assert status == 200
        assert "instructions" not in body
        assert "example_images" not in body

    def test_completion_payload_when_no_presentable_tasks(self, env):
        """Req 7.11: zero presentable unsubmitted tasks -> completion
        payload with the submitted and withheld counts."""
        caller = env.labeler["user_id"]
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", caller, status="Submitted")
        env.put_task(job_id, "task-0000001", caller, status="Submitted")
        env.put_task(job_id, "task-0000002", caller,
                     status="PresentationFailed")

        status, body = env.next_task(job_id)
        assert status == 200
        assert body == {
            "complete": True,
            "job_id": job_id,
            "submitted_count": 2,
            "withheld_count": 1,
            "remaining_count": 0,
        }


# ---------------------------------------------------------------- ownership

class TestOwnershipDenials:
    def test_other_labelers_job_denied_with_audit_event(self, env):
        """Req 2.6: a job in which the caller holds no Task_Assignment
        answers 403 with no resource data plus an audit event."""
        owner = env.labeler
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", owner["user_id"])

        intruder = env.make_labeler()  # team member, but no tasks
        status, body = env.next_task(job_id, user=intruder)
        assert status == 403
        assert body == {"error": "Access denied"}  # no resource data

        events = env.denial_audit_events(user=intruder)
        assert len(events) == 1
        assert events[0]["resource_id"] == job_id
        assert events[0]["result"] == "denied"

    def test_other_labelers_task_image_url_denied_with_audit(self, env):
        """Req 2.6: another labeler's task is indistinguishable from a
        missing one on the image-url route."""
        owner = env.labeler
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", owner["user_id"])

        intruder = env.make_labeler()
        status, body = env.image_url("task-0000000", user=intruder,
                                     job_id=job_id)
        assert status == 403
        assert body == {"error": "Access denied"}
        events = env.denial_audit_events(user=intruder)
        assert len(events) == 1
        assert events[0]["resource_id"] == "task-0000000"

    def test_nonexistent_job_denied(self, env):
        status, body = env.next_task(f"labeling-{uuid.uuid4().hex[:8]}")
        assert status == 403
        assert body == {"error": "Access denied"}
        assert len(env.denial_audit_events()) == 1

    def test_removed_member_denied_on_next(self, env):
        """Req 2.4: current team membership is required even while the
        caller's task assignments still exist."""
        caller = env.labeler["user_id"]
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", caller)
        assert env.next_task(job_id)[0] == 200

        env.remove_member(caller)
        status, body = env.next_task(job_id)
        assert status == 403
        assert body == {"error": "Access denied"}
        assert len(env.denial_audit_events()) == 1

    def test_non_labeler_permission_denied_by_rbac(self, env):
        """A role without labeling:tasks-self is rejected by the
        standard @rbac_check path."""
        viewer = {"user_id": f"user-{uuid.uuid4()}", "email": "v@x.com",
                  "username": "viewer", "role": "Viewer"}
        status, body = env.list_jobs(user=viewer)
        assert status == 403
        assert body["error"] == "Insufficient permissions"


# ---------------------------------------------------------------- image-url

class TestImageUrlRefresh:
    def test_refresh_returns_fresh_15_minute_url(self, env):
        """Req 12.7: a fresh presigned URL for the caller's own task,
        valid <=900 s (Req 12.6)."""
        caller = env.labeler["user_id"]
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", caller)

        before = int(time.time())
        status, body = env.image_url("task-0000000")
        assert status == 200
        assert body["task_id"] == "task-0000000"
        assert body["job_id"] == job_id
        url = body["image_url"]
        assert f"images/{job_id}/task-0000000.jpg" in url
        assert 0 < presigned_expiry_seconds(url) <= 900
        assert body["image_url_expires_at"] >= before + 900

        # A second refresh keeps working (fresh grant each call).
        status, second = env.image_url("task-0000000")
        assert status == 200
        assert 0 < presigned_expiry_seconds(second["image_url"]) <= 900

    def test_job_id_query_param_disambiguates(self, env):
        """The same task id in two of the caller's jobs is resolved by
        the optional job_id query parameter."""
        caller = env.labeler["user_id"]
        job_a = env.put_job()
        job_b = env.put_job()
        env.put_task(job_a, "task-0000000", caller, status="Submitted")
        env.put_task(job_b, "task-0000000", caller, status="Assigned")

        status, body = env.image_url("task-0000000", job_id=job_a)
        assert status == 200
        assert body["job_id"] == job_a

        # Without the parameter the unsubmitted (Assigned) match wins.
        status, body = env.image_url("task-0000000")
        assert status == 200
        assert body["job_id"] == job_b
