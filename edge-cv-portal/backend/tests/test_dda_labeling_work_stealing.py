"""
Work-stealing example tests for the steal and pool routes in
dda_labeling.py.

Spec: labeling-job-cleanup-work-stealing-and-podium, task 1.9.

Covers, against the moto-backed stack from conftest.py (real
shared_utils / rbac_middleware, synthetic API Gateway events with
Cognito claims, moto DynamoDB + S3 — the denial-pattern conventions of
test_dda_labeling_labeler_apis.py):

- Denial posture on BOTH POST /labeler/jobs/{jobId}/steal and
  GET /labeler/jobs/{jobId}/pool: a non-member caller, a missing job,
  and a Ground Truth job are indistinguishable — 403 carrying no
  resource data plus a labeler_access_denied audit event (Req 5.7,
  6.1)
- The task_stolen audit event carries the caller, the Donor
  (stolen_from), the task id, and the job id (Req 5.8)
- Steal then GET /labeler/jobs/{jobId}/next serves the stolen task as
  the caller's own through the untouched next-task flow, presigned
  image URL machinery included (Req 5.9)
- The shipped /next completion payload stays byte-identical with the
  pool route present: the exact dict pinned by
  test_dda_labeling_labeler_apis.py::TestNextTaskGating::
  test_completion_payload_when_no_presentable_tasks (Req 10.1)
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
# Distinct from the labeler-apis suite's bucket so the two module
# fixtures never contend over one moto bucket in a combined run.
DATASET_BUCKET = "test-steal-example-dataset"


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
    caller who is a current member of the team."""
    return StealEnv(aws_stack, dda)


class StealEnv:
    def __init__(self, stack, dda):
        self.stack = stack
        self.dda = dda
        self.usecase_id = f"uc-{uuid.uuid4()}"
        # Single-account use case: root cross_account_role_arn makes
        # get_s3_client_for_bucket fall back to default (moto) creds.
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Work Stealing Example Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        self.team_id = f"team-{uuid.uuid4()}"
        stack.tables.labeling_teams.put_item(Item={
            "team_id": self.team_id,
            "sk": "META",
            "usecase_id": self.usecase_id,
            "team_name": "Steal Example Team",
            "created_at": 1,
            "created_by": "admin",
        })
        self.labeler = self.make_labeler()

    # ------------------------------------------------------------ setup
    def make_labeler(self, member=True):
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
            self.add_member(user_id)
        return user

    def add_member(self, user_id):
        self.stack.tables.labeling_teams.put_item(Item={
            "team_id": self.team_id,
            "sk": f"MEMBER#{user_id}",
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "added_at": 1,
            "added_by": "admin",
        })

    def put_job(self, status="InProgress", backend="DDA", **attrs):
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
            "team_id": self.team_id,
            "created_at": 1,
        }
        item.update(attrs)
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def put_task(self, job_id, task_id, assignee, status="Assigned"):
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
        self.stack.tables.labeling_tasks.put_item(Item=item)
        return item

    def get_task(self, job_id, task_id):
        return self.stack.tables.labeling_tasks.get_item(
            Key={"job_id": job_id, "task_id": task_id}).get("Item")

    # ------------------------------------------------------------ invoke
    def event(self, method, resource, user, path_params=None):
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", value)
        return {
            "httpMethod": method,
            "resource": resource,
            "path": path,
            "pathParameters": path_params or None,
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

    def invoke(self, method, resource, user=None, path_params=None):
        response = self.dda.module.handler(
            self.event(method, resource, user or self.labeler,
                       path_params), None)
        return response["statusCode"], json.loads(response["body"])

    def steal(self, job_id, user=None):
        return self.invoke("POST", "/labeler/jobs/{jobId}/steal",
                           user=user, path_params={"jobId": job_id})

    def pool(self, job_id, user=None):
        return self.invoke("GET", "/labeler/jobs/{jobId}/pool",
                           user=user, path_params={"jobId": job_id})

    def next_task(self, job_id, user=None):
        return self.invoke("GET", "/labeler/jobs/{jobId}/next",
                           user=user, path_params={"jobId": job_id})

    # ------------------------------------------------------------- audit
    def audit_events(self, action, user=None):
        response = self.stack.tables.audit_log.scan()
        caller = (user or self.labeler)["user_id"]
        return [item for item in response.get("Items", [])
                if item.get("action") == action
                and item.get("user_id") == caller]


def presigned_expiry_seconds(url):
    """The expiry window of a presigned URL in seconds (SigV4
    X-Amz-Expires, or SigV2 absolute Expires minus now)."""
    params = parse_qs(urlparse(url).query)
    if "X-Amz-Expires" in params:
        return int(params["X-Amz-Expires"][0])
    assert "Expires" in params, f"not a presigned URL: {url}"
    return int(params["Expires"][0]) - int(time.time())


# --------------------------------------------------------- denial posture

class TestDenialPosture:
    """Req 5.7 / 6.1: both new routes carry the labeler-route denial
    posture — a non-member caller, a missing job, and a Ground Truth
    job are indistinguishable: 403 with no resource data plus a
    labeler_access_denied audit event."""

    @pytest.mark.parametrize("route", ["steal", "pool"])
    def test_non_member_denied_with_audit_event(self, env, route):
        """A caller who is not a current member of the job's team is
        denied with nothing changed."""
        donor = env.make_labeler()
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", donor["user_id"])

        outsider = env.make_labeler(member=False)
        status, body = getattr(env, route)(job_id, user=outsider)
        assert status == 403
        assert body == {"error": "Access denied"}  # no resource data

        events = env.audit_events("labeler_access_denied", user=outsider)
        assert len(events) == 1
        assert events[0]["resource_id"] == job_id
        assert events[0]["result"] == "denied"

        # Nothing changed: the donor still holds the task, unstolen.
        task = env.get_task(job_id, "task-0000000")
        assert task["assignee_user_id"] == donor["user_id"]
        assert "stolen_from" not in task

    @pytest.mark.parametrize("route", ["steal", "pool"])
    def test_missing_job_denied_with_audit_event(self, env, route):
        """A job that does not exist answers the same 403."""
        missing = f"labeling-{uuid.uuid4().hex[:8]}"
        status, body = getattr(env, route)(missing)
        assert status == 403
        assert body == {"error": "Access denied"}

        events = env.audit_events("labeler_access_denied")
        assert len(events) == 1
        assert events[0]["resource_id"] == missing
        assert events[0]["result"] == "denied"

    @pytest.mark.parametrize("route", ["steal", "pool"])
    def test_ground_truth_job_denied_indistinguishably(self, env, route):
        """A Ground Truth job answers the exact same 403 as a missing
        one — even for a current team member."""
        gt_job = env.put_job(backend="GroundTruth")

        status, body = getattr(env, route)(gt_job)
        assert status == 403
        assert body == {"error": "Access denied"}  # same body as missing

        events = env.audit_events("labeler_access_denied")
        assert len(events) == 1
        assert events[0]["resource_id"] == gt_job
        assert events[0]["result"] == "denied"


# ------------------------------------------------------ task_stolen audit

class TestTaskStolenAudit:
    def test_audit_event_carries_caller_donor_task_and_job(self, env):
        """Req 5.8: a transfer writes a task_stolen audit event with
        the caller, the Donor (stolen_from), the task id, and the job
        id."""
        caller = env.labeler["user_id"]
        donor = env.make_labeler()
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", caller, status="Submitted")
        env.put_task(job_id, "task-0000001", donor["user_id"])

        status, body = env.steal(job_id)
        assert status == 200
        assert body["task_id"] == "task-0000001"
        assert body["stolen_from"] == donor["user_id"]

        events = env.audit_events("task_stolen")
        assert len(events) == 1
        event = events[0]
        assert event["user_id"] == caller                       # caller
        assert event["details"]["stolen_from"] == donor["user_id"]  # Donor
        assert event["resource_id"] == "task-0000001"           # task id
        assert event["details"]["job_id"] == job_id             # job id
        assert event["resource_type"] == "labeling_task"
        assert event["result"] == "success"


# -------------------------------------------------- steal then next flow

class TestStolenTaskServedByNextFlow:
    def test_steal_then_next_serves_the_stolen_task(self, env):
        """Req 5.9: full flow — the caller out of own work steals a
        teammate's task, and the untouched next-task flow serves it as
        the caller's own, presigned image URL machinery included."""
        caller = env.labeler["user_id"]
        donor = env.make_labeler()
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", caller, status="Submitted")
        env.put_task(job_id, "task-0000001", donor["user_id"])

        # The caller finished their own work: /next answers completion.
        status, body = env.next_task(job_id)
        assert status == 200
        assert body["complete"] is True

        # Steal: exactly one task moves to the caller (Req 5.1 shape).
        status, stolen = env.steal(job_id)
        assert status == 200
        assert stolen == {
            "task_id": "task-0000001",
            "job_id": job_id,
            "stolen_from": donor["user_id"],
            "stealable_count": 0,
        }

        # The stolen task is now served as the caller's own through the
        # existing next-task flow, presigned image included (<=900 s).
        status, body = env.next_task(job_id)
        assert status == 200
        assert body["complete"] is False
        assert body["task_id"] == "task-0000001"
        assert body["job_id"] == job_id
        assert body["submitted_count"] == 1
        assert body["remaining_count"] == 1
        url = body["image_url"]
        assert f"images/{job_id}/task-0000001.jpg" in url
        assert 0 < presigned_expiry_seconds(url) <= 900


# --------------------------------------------- pinned completion payload

class TestCompletionPayloadPreserved:
    def test_completion_payload_byte_identical_with_pool_route(self, env):
        """Req 10.1: the /next completion payload pinned by
        test_dda_labeling_labeler_apis.py (TestNextTaskGating::
        test_completion_payload_when_no_presentable_tasks) stays
        byte-identical while the sibling pool route is present and
        live — same seeding, same exact-dict assertion."""
        caller = env.labeler["user_id"]
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", caller, status="Submitted")
        env.put_task(job_id, "task-0000001", caller, status="Submitted")
        env.put_task(job_id, "task-0000002", caller,
                     status="PresentationFailed")

        # The pool route is present and answers the caller...
        status, pool_body = env.pool(job_id)
        assert status == 200
        assert pool_body["job_id"] == job_id

        # ...and the shipped completion payload is EXACTLY the pinned
        # dict — no podium, stealable_count, or any other new key.
        status, body = env.next_task(job_id)
        assert status == 200
        assert body == {
            "complete": True,
            "job_id": job_id,
            "submitted_count": 2,
            "withheld_count": 1,
            "remaining_count": 0,
        }
