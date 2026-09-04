"""
Labeling_Backend switch, merged listing, and DDA job detail in
labeling.py (dda-data-labeling, task 5.1).

Feature: dda-data-labeling

Covers, against the moto-backed stack from conftest.py (real
shared_utils, synthetic API Gateway events with Cognito claims):

- POST /labeling: missing / invalid labeling_backend rejected with a
  400 identifying the backend value; no job record and no backend
  resources persisted (Req 1.6)
- POST /labeling with GroundTruth: existing SageMaker flow runs and the
  job item is persisted with labeling_backend='GroundTruth' (Req 1.2,
  1.4)
- POST /labeling with DDA: delegated to dda_labeling.create_dda_job
  (body, user) with no SageMaker call (Req 1.3; the callee is task 5.3)
- GET /labeling: single merged list carrying each job's persisted
  labeling_backend (legacy items default to GroundTruth), with the
  SageMaker status-sync loop skipped for labeling_backend='DDA' items
  (Req 1.5)
- GET /labeling/{id} for DDA jobs: submitted count, progress percentage
  rounded to the nearest whole number, per-member submitted/remaining
  counts, unassigned count, blocked flag, notification state, and the
  skip-verification progress substitution (Req 11.1, 11.2, 11.10)
- GET /labeling/{id} pre-label outcome counts and LLM auto-label
  surfacing (llm-auto-labeling, task 11.2, Req 10.1, 10.3):
  prelabel_available_count / prelabel_failed_count over active tasks
  only, zeros without auto-labeling, and the model identifier plus the
  full untruncated Detection_Prompt returned on the job item

DDA job/task/team rows are seeded directly in DynamoDB (moto) — the
tests do not depend on create_dda_job. SageMaker is a recording fake
(moto has no Ground Truth labeling-job support).
"""
import json
import sys
import uuid
from types import SimpleNamespace

import boto3 as real_boto3
import pytest

REGION = "us-east-1"


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def labeling(aws_stack):
    """The real labeling module imported inside the moto mock."""
    sys.modules.pop("labeling", None)
    import labeling

    return labeling


class FakeSageMakerClient:
    """Recording fake for the SageMaker APIs the Ground Truth flow uses."""

    def __init__(self):
        self.created = []

    def create_labeling_job(self, **params):
        self.created.append(params)
        return {"LabelingJobArn":
                f"arn:aws:sagemaker:{REGION}:123456789012:labeling-job/"
                f"{params['LabelingJobName']}"}


class LabelingEnv:
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
    def event(self, method, resource, path=None, path_params=None,
              query=None, body=None):
        return {
            "httpMethod": method,
            "resource": resource,
            "path": path or f"/v1{resource}",
            "pathParameters": path_params or None,
            "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": self.user["user_id"],
                        "email": self.user["email"],
                        "cognito:username": self.user["username"],
                        "custom:role": self.user["role"],
                    }
                }
            },
        }

    def invoke(self, event):
        response = self.labeling.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def create_job(self, body):
        return self.invoke(self.event("POST", "/labeling", body=body))

    def list_jobs(self):
        return self.invoke(self.event(
            "GET", "/labeling", query={"usecase_id": self.usecase_id}))

    def get_job(self, job_id):
        return self.invoke(self.event(
            "GET", "/labeling/{id}", path=f"/v1/labeling/{job_id}",
            path_params={"id": job_id}))

    # ------------------------------------------------------------- store
    def usecase_jobs(self):
        response = self.stack.tables.labeling_jobs.query(
            IndexName="usecase-jobs-index",
            KeyConditionExpression=real_boto3.dynamodb.conditions.Key(
                "usecase_id").eq(self.usecase_id))
        return response.get("Items", [])

    def put_job(self, **attrs):
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": "seeded job",
            "created_at": 1,
            "status": "InProgress",
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

    def put_team(self, member_ids):
        team_id = f"team-{uuid.uuid4().hex[:12]}"
        self.stack.tables.labeling_teams.put_item(Item={
            "team_id": team_id, "sk": "META",
            "usecase_id": self.usecase_id,
            "team_name": "Team", "created_at": 1,
        })
        for user_id in member_ids:
            self.stack.tables.labeling_teams.put_item(Item={
                "team_id": team_id, "sk": f"MEMBER#{user_id}",
                "user_id": user_id,
                "email": f"{user_id}@example.com",
                "added_at": 1,
            })
        return team_id

    # ------------------------------------------- Ground Truth path setup
    def seed_groundtruth_usecase(self, bucket, prefix, image_count=2):
        """Single-account use case (root ARN -> no STS assume) with a
        moto S3 bucket holding PNG images under the dataset prefix."""
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "GT UC",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "external_id": "test-external-id",
            "s3_bucket": bucket,
        })
        s3 = real_boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=bucket)
        for index in range(image_count):
            s3.put_object(Bucket=bucket, Key=f"{prefix}img-{index}.png",
                          Body=b"fake-png")

    def groundtruth_body(self, prefix):
        return {
            "labeling_backend": "GroundTruth",
            "usecase_id": self.usecase_id,
            "job_name": "gt job",
            "dataset_prefix": prefix,
            "task_type": "Classification",
            "label_categories": ["normal", "anomaly"],
            "workforce_arn": ("arn:aws:sagemaker:us-east-1:123456789012:"
                              "workteam/private-crowd/team"),
        }


@pytest.fixture
def env(aws_stack, labeling):
    return LabelingEnv(aws_stack, labeling)


@pytest.fixture
def fake_sagemaker(labeling, monkeypatch):
    """Route boto3.client('sagemaker') inside labeling.py to a recording
    fake; every other service keeps its real (moto-backed) client."""
    fake = FakeSageMakerClient()

    def client(service, **kwargs):
        if service == "sagemaker":
            return fake
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return real_boto3.client(service, **clean)

    monkeypatch.setattr(labeling, "boto3", SimpleNamespace(
        client=client, resource=real_boto3.resource))
    return fake


# ------------------------------------------------------ backend validation

class TestBackendValidation:
    def test_missing_backend_rejected_nothing_persisted(self, env):
        """Req 1.6: omitted labeling_backend -> 400, no job record."""
        body = env.groundtruth_body("datasets/")
        del body["labeling_backend"]

        status, response = env.create_job(body)
        assert status == 400
        assert "labeling_backend" in response["error"]
        assert env.usecase_jobs() == []

    def test_invalid_backend_rejected_identifying_value(self, env):
        """Req 1.6: an unknown backend value -> 400 naming the value,
        nothing persisted."""
        body = env.groundtruth_body("datasets/")
        body["labeling_backend"] = "SageMaker"

        status, response = env.create_job(body)
        assert status == 400
        assert "SageMaker" in response["error"]
        assert response["labeling_backend"] == "SageMaker"
        assert env.usecase_jobs() == []


# ------------------------------------------------------- GroundTruth path

class TestGroundTruthPath:
    def test_groundtruth_job_persists_backend(self, env, fake_sagemaker):
        """Req 1.2/1.4: the existing SageMaker flow runs unchanged and
        the job item carries labeling_backend='GroundTruth'."""
        bucket = f"gt-bucket-{uuid.uuid4().hex[:8]}"
        env.seed_groundtruth_usecase(bucket, "datasets/")

        status, response = env.create_job(env.groundtruth_body("datasets/"))
        assert status == 201, response

        assert len(fake_sagemaker.created) == 1
        jobs = env.usecase_jobs()
        assert len(jobs) == 1
        assert jobs[0]["labeling_backend"] == "GroundTruth"
        assert jobs[0]["job_id"] == response["job_id"]


# ---------------------------------------------------------- DDA delegation

class TestDdaDelegation:
    def test_dda_delegates_to_create_dda_job(self, env, monkeypatch):
        """Req 1.3: the DDA branch delegates to
        dda_labeling.create_dda_job(body, user) and returns its response
        verbatim; no SageMaker API is touched."""
        import dda_labeling
        from shared_utils import create_response

        calls = []

        def stub_create_dda_job(body, user):
            calls.append((body, user))
            return create_response(201, {"job_id": "job-dda-1",
                                         "status": "InProgress"})

        # create_dda_job is implemented by task 5.3; the stub stands in
        # for the delegation contract regardless of merge order.
        monkeypatch.setattr(dda_labeling, "create_dda_job",
                            stub_create_dda_job, raising=False)

        body = {"labeling_backend": "DDA",
                "usecase_id": env.usecase_id,
                "job_name": "dda job"}
        status, response = env.create_job(body)

        assert status == 201
        assert response["job_id"] == "job-dda-1"
        assert len(calls) == 1
        delegated_body, delegated_user = calls[0]
        assert delegated_body["labeling_backend"] == "DDA"
        assert delegated_body["job_name"] == "dda job"
        assert delegated_user["user_id"] == env.user["user_id"]
        assert delegated_user["role"] == env.user["role"]


# ----------------------------------------------------------- merged listing

class TestMergedListing:
    def test_listing_merges_both_backends_with_persisted_value(self, env):
        """Req 1.5: one list containing both backends, each item carrying
        its persisted labeling_backend; legacy Ground Truth items (created
        before the attribute existed) default to GroundTruth."""
        gt_id = env.put_job(status="Completed",
                            labeling_backend="GroundTruth")
        legacy_id = env.put_job(status="Completed")  # no labeling_backend
        dda_id = env.put_job(status="InProgress", labeling_backend="DDA",
                             task_type="Segmentation")

        status, response = env.list_jobs()
        assert status == 200
        assert response["count"] == 3
        backends = {j["job_id"]: j["labeling_backend"]
                    for j in response["jobs"]}
        assert backends == {gt_id: "GroundTruth",
                            legacy_id: "GroundTruth",
                            dda_id: "DDA"}

    def test_dda_items_skip_sagemaker_status_sync(self, env, monkeypatch):
        """Req 1.5 + design: the SageMaker status-sync loop never runs
        for labeling_backend='DDA' items, even when one (adversarially)
        carries a sagemaker_job_name."""
        job_id = env.put_job(status="InProgress", labeling_backend="DDA",
                             sagemaker_job_name="sm-should-not-sync")

        sync_calls = []
        monkeypatch.setattr(
            env.labeling, "get_usecase",
            lambda usecase_id: sync_calls.append(usecase_id))

        status, response = env.list_jobs()
        assert status == 200
        assert sync_calls == []  # sync loop never initialized a client
        assert response["jobs"][0]["job_id"] == job_id
        assert response["jobs"][0]["status"] == "InProgress"


# ----------------------------------------------------------- DDA job detail

class TestDdaJobDetail:
    def test_detail_returns_progress_members_and_unassigned(self, env):
        """Req 11.1/11.2: submitted count, rounded progress percentage,
        per-member submitted/remaining counts, and the unassigned count."""
        member_a = f"labeler-{uuid.uuid4().hex[:8]}"
        member_b = f"labeler-{uuid.uuid4().hex[:8]}"
        team_id = env.put_team([member_a, member_b])
        job_id = env.put_job(
            labeling_backend="DDA", team_id=team_id,
            task_type="Segmentation", image_count=4,
            notification_failures=[{"email": "x@example.com",
                                    "reason": "bounced"}])
        env.put_task(job_id, 0, member_a, status="Submitted")
        env.put_task(job_id, 1, member_a, status="Assigned")
        env.put_task(job_id, 2, member_b, status="Submitted")
        env.put_task(job_id, 3, "UNASSIGNED", status="Assigned")

        status, response = env.get_job(job_id)
        assert status == 200
        job = response["job"]
        assert job["labeling_backend"] == "DDA"
        assert job["team_id"] == team_id
        assert job["task_type"] == "Segmentation"
        assert job["image_count"] == 4
        assert job["submitted_count"] == 2
        assert job["progress_percent"] == 50
        assert job["unassigned_count"] == 1
        assert job["blocked"] is False
        assert job["notifications_skipped"] is False
        assert job["notification_failures"] == [
            {"email": "x@example.com", "reason": "bounced"}]

        progress = {m["user_id"]: m for m in job["member_progress"]}
        assert progress[member_a] == {
            "user_id": member_a, "email": f"{member_a}@example.com",
            "submitted": 1, "remaining": 1}
        assert progress[member_b] == {
            "user_id": member_b, "email": f"{member_b}@example.com",
            "submitted": 1, "remaining": 0}

    def test_progress_percentage_rounds_to_nearest_whole(self, env):
        """Req 11.1: 2/3 submitted -> 66.67% displays as 67."""
        member = f"labeler-{uuid.uuid4().hex[:8]}"
        team_id = env.put_team([member])
        job_id = env.put_job(labeling_backend="DDA", team_id=team_id,
                             task_type="Classification", image_count=3)
        env.put_task(job_id, 0, member, status="Submitted")
        env.put_task(job_id, 1, member, status="Submitted")
        env.put_task(job_id, 2, member, status="Assigned")

        status, response = env.get_job(job_id)
        assert status == 200
        assert response["job"]["submitted_count"] == 2
        assert response["job"]["progress_percent"] == 67

    def test_blocked_job_reports_blocked_and_unassigned(self, env):
        """Req 5.4 surface: blocked flag and unassigned count shown."""
        team_id = env.put_team([])
        job_id = env.put_job(labeling_backend="DDA", team_id=team_id,
                             task_type="ObjectDetection", image_count=2,
                             blocked=True, notifications_skipped=True)
        env.put_task(job_id, 0, "UNASSIGNED", status="Assigned")
        env.put_task(job_id, 1, "UNASSIGNED", status="Assigned")

        status, response = env.get_job(job_id)
        assert status == 200
        job = response["job"]
        assert job["blocked"] is True
        assert job["unassigned_count"] == 2
        assert job["notifications_skipped"] is True
        assert job["submitted_count"] == 0
        assert job["progress_percent"] == 0
        assert job["member_progress"] == []

    def test_skip_verification_substitutes_autolabel_completed_count(
            self, env):
        """Req 11.10: completed auto-label attempts stand in for the
        submitted count; 5/8 -> 62.5% displays as 63 (nearest whole)."""
        job_id = env.put_job(
            labeling_backend="DDA", task_type="Classification",
            image_count=8, skip_verification=True,
            autolabel_completed_count=5)
        # 'AUTO' result items never appear in member/unassigned counts.
        env.put_task(job_id, 0, "AUTO", status="Assigned",
                     prelabel_status="Available")

        status, response = env.get_job(job_id)
        assert status == 200
        job = response["job"]
        assert job["submitted_count"] == 5
        assert job["progress_percent"] == 63
        assert job["unassigned_count"] == 0
        assert job["member_progress"] == []

    def test_groundtruth_detail_defaults_backend(self, env):
        """Legacy Ground Truth items expose labeling_backend='GroundTruth'
        in the detail view too (Req 1.5 consistency)."""
        job_id = env.put_job(status="Completed")  # no backend attribute

        status, response = env.get_job(job_id)
        assert status == 200
        assert response["job"]["labeling_backend"] == "GroundTruth"


# ---------------------------------------- DDA job detail pre-label counts

class TestDdaJobDetailPrelabelCounts:
    """Feature: llm-auto-labeling, task 11.2 (Req 10.1, 10.3).

    prelabel_available_count / prelabel_failed_count on the DDA job
    detail view, derived from active tasks only, plus the model
    identifier and full untruncated Detection_Prompt on the job item.
    """

    def test_mixed_statuses_produce_the_two_counts(self, env):
        """Req 10.3: Pending / Available / Failed / absent mix across
        active tasks yields the right Available and Failed counts."""
        member = f"labeler-{uuid.uuid4().hex[:8]}"
        team_id = env.put_team([member])
        job_id = env.put_job(
            labeling_backend="DDA", team_id=team_id,
            task_type="ObjectDetection", image_count=6,
            auto_label={"enabled": True, "model": "llm:us.amazon.nova-pro-v1:0",
                        "detection_prompt": "find scratches"})
        env.put_task(job_id, 0, member, prelabel_status="Pending")
        env.put_task(job_id, 1, member, prelabel_status="Available")
        env.put_task(job_id, 2, member, prelabel_status="Available")
        env.put_task(job_id, 3, member, prelabel_status="Available")
        env.put_task(job_id, 4, member, prelabel_status="Failed",
                     prelabel_error="model error: boom")
        env.put_task(job_id, 5, member)  # no prelabel_status at all

        status, response = env.get_job(job_id)
        assert status == 200
        job = response["job"]
        assert job["prelabel_available_count"] == 3
        assert job["prelabel_failed_count"] == 1

    def test_inactive_tasks_are_excluded_from_the_counts(self, env):
        """Req 10.3: Inactive tasks (deactivated after a distribution
        shortfall) never count, whatever their prelabel_status."""
        member = f"labeler-{uuid.uuid4().hex[:8]}"
        team_id = env.put_team([member])
        job_id = env.put_job(
            labeling_backend="DDA", team_id=team_id,
            task_type="Segmentation", image_count=2,
            auto_label={"enabled": True, "model": "llm:model-x",
                        "detection_prompt": "p"})
        env.put_task(job_id, 0, member, prelabel_status="Available")
        env.put_task(job_id, 1, member, prelabel_status="Failed")
        env.put_task(job_id, 2, member, status="Inactive",
                     prelabel_status="Available")
        env.put_task(job_id, 3, member, status="Inactive",
                     prelabel_status="Failed")

        status, response = env.get_job(job_id)
        assert status == 200
        job = response["job"]
        assert job["prelabel_available_count"] == 1
        assert job["prelabel_failed_count"] == 1

    def test_job_without_auto_labeling_reports_zeros(self, env):
        """Req 10.3: a plain team job with no auto-labeling (no
        prelabel_status on any task) reports both counts as zero."""
        member = f"labeler-{uuid.uuid4().hex[:8]}"
        team_id = env.put_team([member])
        job_id = env.put_job(
            labeling_backend="DDA", team_id=team_id,
            task_type="Classification", image_count=2)
        env.put_task(job_id, 0, member)
        env.put_task(job_id, 1, member, status="Submitted")

        status, response = env.get_job(job_id)
        assert status == 200
        job = response["job"]
        assert job["prelabel_available_count"] == 0
        assert job["prelabel_failed_count"] == 0

    def test_detail_carries_model_and_untruncated_prompt(self, env):
        """Req 10.1: the response carries the model identifier and the
        stored Detection_Prompt byte-for-byte — including leading and
        trailing whitespace, newlines, quotes, and braces — at full
        length, untruncated."""
        prompt = ("  Find every defect.\n"
                  'Report {"detections": [...]} with "box" entries.\n'
                  + ("x" * 1900) + "\n  ")
        model = "llm:us.amazon.nova-pro-v1:0"
        job_id = env.put_job(
            labeling_backend="DDA", task_type="ObjectDetection",
            image_count=1,
            auto_label={"enabled": True, "model": model,
                        "detection_prompt": prompt})
        status, response = env.get_job(job_id)
        assert status == 200
        auto_label = response["job"]["auto_label"]
        assert auto_label["model"] == model
        assert auto_label["detection_prompt"] == prompt
        assert len(auto_label["detection_prompt"]) == len(prompt)
