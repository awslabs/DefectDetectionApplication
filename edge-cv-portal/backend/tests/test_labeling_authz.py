"""
Authorization of the labeling data routes (functions/labeling.py).

Data-route authorization consistency with the already-gated sibling routes
(stop_labeling_job, dda_labeling.create_dda_job, pre_labeled_datasets). Under
PORTAL_REGISTRY_ENFORCED=true:

- GET /labeling (list_labeling_jobs): the query's use case is membership-checked
  before the jobs query / SageMaker status sync; a non-member is denied 403, a
  member lists the use case's jobs.
- GET /labeling/{id} (get_labeling_job): the job record is read first (404 for
  an unknown id), its use case resolved from the record, then membership checked
  before any SageMaker sync / cross-account assumption; a non-member is denied
  403, a member reads the job.
- POST /labeling GroundTruth (_create_ground_truth_labeling_job): gated by
  @rbac_check([MANAGE_LABELING_JOBS]) in the body's use-case scope — identical
  to the DDA create path and stop_labeling_job, so both POST /labeling backends
  enforce identically. A non-member is denied 403 with nothing persisted; a
  member creates the job.

Runs against the moto-backed stack from conftest.py (real shared_utils role
resolution, real @rbac_check), SageMaker replaced by a recording fake (moto has
no Ground Truth labeling-job support).
"""
import json
import sys
import uuid
from types import SimpleNamespace

import boto3 as real_boto3
import pytest

REGION = "us-east-1"


@pytest.fixture(scope="module")
def labeling(aws_stack):
    """The real labeling module imported inside the moto mock."""
    sys.modules.pop("labeling", None)
    import labeling
    return labeling


@pytest.fixture
def enforcement_on(aws_stack, monkeypatch):
    """Registry enforcement as production runs it (design.md Decision 4)."""
    import shared_utils
    monkeypatch.setenv(shared_utils.PORTAL_REGISTRY_ENFORCED_ENV, "true")


class FakeSageMakerClient:
    """Recording fake for the one SageMaker API the Ground Truth flow calls."""

    def __init__(self):
        self.created = []

    def create_labeling_job(self, **params):
        self.created.append(params)
        return {"LabelingJobArn":
                f"arn:aws:sagemaker:{REGION}:123456789012:labeling-job/"
                f"{params['LabelingJobName']}"}


class LabelingAuthzEnv:
    def __init__(self, stack, labeling):
        self.stack = stack
        self.labeling = labeling
        self.usecase_id = f"uc-{uuid.uuid4()}"

    # ----------------------------------------------------------- principals
    def user(self, role="DataScientist"):
        uid = f"user-{uuid.uuid4()}"
        return {"user_id": uid, "email": f"{uid}@example.com",
                "username": uid, "role": role}

    def seed_member(self, user, role="DataScientist"):
        """Provision the caller in the Portal_Identity registry (enabled
        global provisioning row + the use-case membership row)."""
        for scope in ("global", self.usecase_id):
            self.stack.tables.user_roles.put_item(Item={
                "user_id": user["user_id"], "usecase_id": scope,
                "role": role, "status": "enabled"})

    # ---------------------------------------------------------------- store
    def put_job(self, **attrs):
        """Seed a DDA job (portal-managed detail: no SageMaker sync needed)."""
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        item = {"job_id": job_id, "usecase_id": self.usecase_id,
                "job_name": "seeded", "created_at": 1, "status": "InProgress",
                "labeling_backend": "DDA", "task_type": "Classification",
                "image_count": 1}
        item.update(attrs)
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def usecase_jobs(self):
        response = self.stack.tables.labeling_jobs.query(
            IndexName="usecase-jobs-index",
            KeyConditionExpression=real_boto3.dynamodb.conditions.Key(
                "usecase_id").eq(self.usecase_id))
        return response.get("Items", [])

    def seed_gt_usecase(self, bucket, prefix="datasets/", image_count=2):
        """Single-account use case (root ARN -> no STS assume) with a moto S3
        bucket holding images under the dataset prefix."""
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id, "name": "GT UC",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "external_id": "test-external-id", "s3_bucket": bucket})
        s3 = real_boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=bucket)
        for index in range(image_count):
            s3.put_object(Bucket=bucket, Key=f"{prefix}img-{index}.png",
                          Body=b"fake-png")

    def gt_body(self, prefix="datasets/"):
        return {"labeling_backend": "GroundTruth", "usecase_id": self.usecase_id,
                "job_name": "gt job", "dataset_prefix": prefix,
                "task_type": "Classification",
                "label_categories": ["normal", "anomaly"],
                "workforce_arn": ("arn:aws:sagemaker:us-east-1:123456789012:"
                                  "workteam/private-crowd/team")}

    # --------------------------------------------------------------- events
    def event(self, method, resource, user, path=None, path_params=None,
              query=None, body=None):
        return {
            "httpMethod": method, "resource": resource,
            "path": path or f"/v1{resource}",
            "pathParameters": path_params,
            "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {
                "authorizer": {"claims": {
                    "sub": user["user_id"], "email": user["email"],
                    "cognito:username": user["username"],
                    "custom:role": user["role"]}},
                "identity": {"sourceIp": "203.0.113.7",
                             "userAgent": "authz-test/1.0"},
            },
        }

    def invoke(self, event):
        response = self.labeling.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def list_jobs(self, user):
        return self.invoke(self.event(
            "GET", "/labeling", user, query={"usecase_id": self.usecase_id}))

    def get_job(self, user, job_id):
        return self.invoke(self.event(
            "GET", "/labeling/{id}", user, path=f"/v1/labeling/{job_id}",
            path_params={"id": job_id}))

    def create_gt(self, user, body):
        return self.invoke(self.event("POST", "/labeling", user, body=body))

    def unauthorized_rows(self, user_id):
        return [item for item in self.stack.tables.audit_log.scan().get("Items", [])
                if item.get("user_id") == user_id
                and item.get("action") == "unauthorized_access"]


@pytest.fixture
def env(aws_stack, labeling):
    return LabelingAuthzEnv(aws_stack, labeling)


@pytest.fixture
def fake_sagemaker(labeling, monkeypatch):
    """Route boto3.client('sagemaker') inside labeling.py to a recording fake;
    every other service keeps its real (moto-backed) client."""
    fake = FakeSageMakerClient()

    def client(service, **kwargs):
        if service == "sagemaker":
            return fake
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return real_boto3.client(service, **clean)

    monkeypatch.setattr(labeling, "boto3", SimpleNamespace(
        client=client, resource=real_boto3.resource))
    return fake


# ------------------------------------------------------- GET /labeling (list)

class TestListLabelingJobsAuthorization:
    def test_non_member_denied(self, env, enforcement_on):
        env.put_job()  # a job exists in the use case
        user = env.user()  # no registry rows

        status, body = env.list_jobs(user)

        assert status == 403
        assert body == {"error": "Insufficient permissions"}
        denials = env.unauthorized_rows(user["user_id"])
        assert len(denials) == 1
        assert denials[0]["details"]["usecase_id"] == env.usecase_id

    def test_member_lists_jobs(self, env, enforcement_on):
        job_id = env.put_job()
        user = env.user()
        env.seed_member(user)

        status, body = env.list_jobs(user)

        assert status == 200, body
        assert body["count"] == 1
        assert body["jobs"][0]["job_id"] == job_id
        assert env.unauthorized_rows(user["user_id"]) == []


# --------------------------------------------------- GET /labeling/{id} (get)

class TestGetLabelingJobAuthorization:
    def test_unknown_job_is_404_before_authorization(self, env, enforcement_on):
        user = env.user()  # no rows

        status, _ = env.get_job(user, f"job-{uuid.uuid4().hex[:12]}")

        assert status == 404
        assert env.unauthorized_rows(user["user_id"]) == []

    def test_non_member_denied(self, env, enforcement_on):
        job_id = env.put_job()
        user = env.user()

        status, body = env.get_job(user, job_id)

        assert status == 403
        assert body == {"error": "Insufficient permissions"}
        denials = env.unauthorized_rows(user["user_id"])
        assert len(denials) == 1
        assert denials[0]["resource_id"] == job_id

    def test_member_reads_job(self, env, enforcement_on):
        job_id = env.put_job()
        user = env.user()
        env.seed_member(user)

        status, body = env.get_job(user, job_id)

        assert status == 200, body
        assert body["job"]["job_id"] == job_id
        assert env.unauthorized_rows(user["user_id"]) == []


# -------------------------------------------------- POST /labeling GroundTruth

class TestCreateGroundTruthAuthorization:
    def test_non_member_denied_nothing_persisted(self, env, enforcement_on,
                                                 fake_sagemaker):
        user = env.user()  # no rows

        status, _ = env.create_gt(user, env.gt_body())

        assert status == 403
        assert env.usecase_jobs() == []
        assert fake_sagemaker.created == []

    def test_member_creates_job(self, env, enforcement_on, fake_sagemaker):
        bucket = f"gt-bucket-{uuid.uuid4().hex[:8]}"
        env.seed_gt_usecase(bucket, "datasets/")
        user = env.user()
        env.seed_member(user)

        status, body = env.create_gt(user, env.gt_body("datasets/"))

        assert status == 201, body
        assert len(fake_sagemaker.created) == 1
        jobs = env.usecase_jobs()
        assert len(jobs) == 1
        assert jobs[0]["labeling_backend"] == "GroundTruth"
        assert jobs[0]["job_id"] == body["job_id"]
