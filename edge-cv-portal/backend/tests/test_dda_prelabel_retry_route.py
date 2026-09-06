"""
POST /labeling/{id}/rerun-prelabels example tests in dda_labeling.py
(grounded-sam-prompt-guardrails-and-prelabel-retry, task 1.4).

Feature: grounded-sam-prompt-guardrails-and-prelabel-retry

Covers, against the moto-backed stack from conftest.py (real
shared_utils / rbac_middleware, synthetic API Gateway events with
Cognito claims, moto DynamoDB, a fake Lambda client installed at
dda_labeling.lambda_client capturing the async worker invokes — the
test_dda_labeling_create_job.py precedent), seeding jobs and Failed
pre-label task items:

- Route dispatch smoke: a POST /labeling/{id}/rerun-prelabels event
  reaches rerun_prelabels through the module handler's router
  (Req 5.1)
- A caller without MANAGE_LABELING_JOBS (Viewer, DataLabeler) is
  denied 403 through the real @rbac_check path (Req 5.2)
- A skip-verification job answers a non-admin manager (DataScientist)
  with 403 and writes an `unauthorized_access` audit event, mirroring
  the Admin_Review gate; a UseCaseAdmin succeeds (Req 5.3)
- An accepted request answers 202 {job_id, retried_count, message},
  writes a `prelabels_rerun` audit row (usecase_id, retried_count,
  overrides_updated), and async-invokes the worker with exactly
  {action: 'retry_prelabels', job_id} (Req 5.9)
- The incident replay (job labeling-8022a9dc's shape): a grounded-sam
  Segmentation job persisted with an instruction-style, period-bearing
  Prompt_Override for 'cookie_gap' and Failed tasks — a bodyless retry
  is refused 400 with the corrective per-label error naming cookie_gap
  (Req 5.6), then the corrected noun-phrase retry answers 202 with the
  job record's auto_label.prompt_overrides updated (Req 5.5)
"""
import json
import sys
import uuid
from types import SimpleNamespace

import pytest
from boto3.dynamodb.conditions import Key

REGION = "us-east-1"
DATASET_BUCKET = "test-retry-dataset"
WORKER_FUNCTION_NAME = "test-dda-labeling-worker"

# The labeling-8022a9dc shape: one label, an instruction-style
# Prompt_Override with inner sentence punctuation, and the alignment
# guard's verbatim failure on every image.
INCIDENT_LABEL = "cookie_gap"
INCIDENT_PROMPT = (
    "draw and fill in the gaps between the broken cookie pieces within "
    "the bounds of the image. If there is a large crack, draw and fill "
    "it in too.")
INCIDENT_ERROR = (
    'Grounded-SAM worker failed: {"errorMessage": "caption token spans '
    '(2) do not align with the 1 prompts; a prompt likely contains '
    'inner sentence punctuation", "errorType": "ValueError"}')
CORRECTED_PROMPT = "gap between broken cookie pieces"


class FakeLambdaClient:
    """Records async invocations of dda_labeling_worker (the
    test_dda_labeling_create_job.py fake-client precedent)."""

    def __init__(self):
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        return {"StatusCode": 202}

    def payloads(self):
        return [json.loads(call["Payload"]) for call in self.invocations]


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, with
    the fake Lambda client installed at dda_labeling.lambda_client."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    fake_lambda = FakeLambdaClient()
    dda_labeling.lambda_client = fake_lambda
    return SimpleNamespace(module=dda_labeling, lambda_client=fake_lambda)


@pytest.fixture
def env(aws_stack, dda, monkeypatch):
    """Per-test facade with a fresh Use_Case, a manager caller, the
    worker function name wired (so _invoke_labeling_worker reaches the
    fake client), and the invocation log cleared."""
    monkeypatch.setenv("DDA_LABELING_WORKER_FUNCTION_NAME",
                       WORKER_FUNCTION_NAME)
    dda.lambda_client.invocations.clear()
    return RetryEnv(aws_stack, dda)


class RetryEnv:
    def __init__(self, stack, dda):
        self.stack = stack
        self.dda = dda
        self.usecase_id = f"uc-{uuid.uuid4()}"
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Prelabel Retry Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        # DataScientist holds MANAGE_LABELING_JOBS but is not an admin
        # role — exactly the caller Req 5.3 distinguishes from Req 5.2.
        self.manager = self.make_user("DataScientist")

    # ------------------------------------------------------------ setup
    def make_user(self, role):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def put_job(self, auto_label=None, skip_verification=False,
                backend="DDA", label_set=None, task_type="Segmentation",
                **attrs):
        job_id = f"labeling-{uuid.uuid4().hex[:8]}"
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": f"job-{job_id}",
            "labeling_backend": backend,
            "status": "InProgress",
            "task_type": task_type,
            "label_set": label_set or ["scratch", "dent"],
            "dataset_bucket": DATASET_BUCKET,
            "dataset_prefix": "datasets/x/",
            "image_count": 0,
            "created_at": 1,
            "created_by": self.manager["user_id"],
        }
        if auto_label is not None:
            item["auto_label"] = auto_label
        if skip_verification:
            item["skip_verification"] = True
        item.update(attrs)
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def put_task(self, job_id, index, prelabel_status="Failed",
                 error=None):
        """A task item as the distributor + consumer leave it — Failed
        items retain prelabel_error/autolabel_error (the shape the
        Retry_Action later resets)."""
        task_id = f"task-{index:06d}"
        image_key = f"datasets/x/img-{index:03d}.jpg"
        item = {
            "job_id": job_id,
            "task_id": task_id,
            "image_s3_uri": f"s3://{DATASET_BUCKET}/{image_key}",
            "image_key": image_key,
            "usecase_id": self.usecase_id,
            "assignee_user_id": "AUTO",
            "status": "Assigned",
            "prelabel_status": prelabel_status,
            "created_at": 1,
            "updated_at": 1700000000,
        }
        if prelabel_status == "Failed":
            reason = error or "model failure"
            item["prelabel_error"] = reason
            item["autolabel_error"] = reason
        self.stack.tables.labeling_tasks.put_item(Item=item)
        return item

    # ------------------------------------------------------------ invoke
    def event(self, method, resource, job_id, user=None, body=None):
        user = user or self.manager
        return {
            "httpMethod": method,
            "resource": resource,
            "path": resource.replace("{id}", job_id),
            "pathParameters": {"id": job_id},
            "queryStringParameters": None,
            "body": json.dumps(body) if body is not None else None,
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

    def invoke(self, method, resource, job_id, user=None, body=None):
        response = self.dda.module.handler(
            self.event(method, resource, job_id, user, body), None)
        return response["statusCode"], json.loads(response["body"])

    def rerun(self, job_id, user=None, body=None):
        """POST /labeling/{id}/rerun-prelabels; body None = bodyless."""
        return self.invoke("POST", "/labeling/{id}/rerun-prelabels",
                           job_id, user=user, body=body)

    # ------------------------------------------------------------- store
    def get_job(self, job_id):
        return self.stack.tables.labeling_jobs.get_item(
            Key={"job_id": job_id}).get("Item")

    def tasks(self, job_id):
        return self.stack.tables.labeling_tasks.query(
            KeyConditionExpression=Key("job_id").eq(job_id),
        ).get("Items", [])

    def audit_events(self, action):
        response = self.stack.tables.audit_log.scan()
        return [item for item in response.get("Items", [])
                if item.get("action") == action
                and item.get("details", {}).get("usecase_id")
                == self.usecase_id]


# --------------------------------------------------------- route dispatch

class TestRouteDispatch:
    def test_post_rerun_prelabels_dispatches_to_handler(self, env):
        """Req 5.1: the router dispatches POST
        /labeling/{id}/rerun-prelabels to rerun_prelabels — an unknown
        job answers the handler's own 404 wording (not the router's
        generic 'Not found'), and an eligible job answers 202."""
        status, body = env.rerun(f"labeling-{uuid.uuid4().hex[:8]}")
        assert status == 404
        assert body["error"] == "Labeling job not found"

        job_id = env.put_job(auto_label={"enabled": True, "model": "sam"})
        env.put_task(job_id, 0)
        status, body = env.rerun(job_id)
        assert status == 202
        assert body["job_id"] == job_id

    def test_other_methods_do_not_reach_the_handler(self, env):
        """Req 5.1: the rerun route is POST-only — a GET on the same
        resource falls through to the router's generic 404."""
        job_id = env.put_job(auto_label={"enabled": True, "model": "sam"})
        env.put_task(job_id, 0)
        status, body = env.invoke(
            "GET", "/labeling/{id}/rerun-prelabels", job_id)
        assert status == 404
        assert body["error"] == "Not found"


# ---------------------------------------------------------- authorization

class TestAuthorization:
    def test_caller_without_manage_permission_denied_403(self, env):
        """Req 5.2: MANAGE_LABELING_JOBS is authorized in the job's
        Use_Case scope before any other processing — a Viewer and a
        DataLabeler are denied through the real @rbac_check path with
        nothing triggered."""
        job_id = env.put_job(auto_label={"enabled": True, "model": "sam"})
        env.put_task(job_id, 0)

        for role in ("Viewer", "DataLabeler"):
            status, body = env.rerun(job_id, user=env.make_user(role))
            assert status == 403
            assert body["error"] == "Insufficient permissions"

        assert env.dda.lambda_client.invocations == []
        assert env.audit_events("prelabels_rerun") == []

    def test_skip_verification_requires_admin_role(self, env):
        """Req 5.3: a skip-verification job rejects a non-admin caller
        who does hold MANAGE_LABELING_JOBS (DataScientist) with 403
        plus an `unauthorized_access` audit event, mirroring the
        Admin_Review gate; a UseCaseAdmin succeeds."""
        job_id = env.put_job(skip_verification=True)
        env.put_task(job_id, 0)

        status, body = env.rerun(job_id, user=env.manager)
        assert status == 403
        assert "administrator" in body["error"].lower()
        assert env.dda.lambda_client.invocations == []

        denials = [event for event
                   in env.audit_events("unauthorized_access")
                   if event.get("resource_id") == job_id]
        assert len(denials) == 1
        assert denials[0]["resource_type"] == "labeling_job"
        assert denials[0]["result"] == "denied"
        assert denials[0]["user_id"] == env.manager["user_id"]
        assert "administrator" in denials[0]["details"]["reason"]

        admin = env.make_user("UseCaseAdmin")
        status, body = env.rerun(job_id, user=admin)
        assert status == 202
        assert body["job_id"] == job_id


# ------------------------------------------------------- accepted request

class TestAcceptedRequest:
    def test_202_with_audit_row_and_worker_invoke(self, env):
        """Req 5.9: acceptance answers 202 {job_id, retried_count,
        message}, writes a `prelabels_rerun` audit event recording the
        usecase, the retried count, and whether overrides were updated,
        and async-invokes the worker with exactly
        {action: 'retry_prelabels', job_id}."""
        job_id = env.put_job(auto_label={"enabled": True, "model": "sam"})
        env.put_task(job_id, 0)
        env.put_task(job_id, 1)
        env.put_task(job_id, 2, prelabel_status="Available")

        status, body = env.rerun(job_id)
        assert status == 202
        assert body["job_id"] == job_id
        assert body["retried_count"] == 2
        assert "2" in body["message"]

        events = env.audit_events("prelabels_rerun")
        assert len(events) == 1
        event = events[0]
        assert event["resource_type"] == "labeling_job"
        assert event["resource_id"] == job_id
        assert event["result"] == "success"
        assert event["user_id"] == env.manager["user_id"]
        details = event["details"]
        assert details["usecase_id"] == env.usecase_id
        assert details["retried_count"] == 2
        assert details["overrides_updated"] is False

        assert env.dda.lambda_client.payloads() == [
            {"action": "retry_prelabels", "job_id": job_id}]
        call = env.dda.lambda_client.invocations[0]
        assert call["FunctionName"] == WORKER_FUNCTION_NAME
        assert call["InvocationType"] == "Event"


# -------------------------------------------------------- incident replay

class TestIncidentReplay:
    """The motivating incident, replayed end to end at the route: job
    labeling-8022a9dc's shape — a grounded-sam Segmentation job whose
    persisted Prompt_Override carries inner sentence punctuation, every
    task Failed on the worker's caption-alignment guard."""

    def incident_job(self, env, failed=3):
        job_id = env.put_job(
            label_set=[INCIDENT_LABEL],
            auto_label={
                "enabled": True,
                "model": "grounded-sam",
                "prompt_overrides": {INCIDENT_LABEL: INCIDENT_PROMPT},
            })
        for index in range(failed):
            env.put_task(job_id, index, error=INCIDENT_ERROR)
        return job_id

    def test_bodyless_retry_refused_with_corrective_error(self, env):
        """Req 5.6: a retry that omits prompt_overrides is judged
        against the persisted Effective_Prompts — the unfixed incident
        job is refused 400 with the corrective per-label error naming
        cookie_gap, and nothing is mutated or triggered."""
        job_id = self.incident_job(env)

        status, body = env.rerun(job_id)
        assert status == 400
        assert body["error"] == "Validation failed"
        errors = body["validation_errors"]
        assert len(errors) == 1
        assert errors[0]["label"] == INCIDENT_LABEL
        assert errors[0]["parameter"] == "auto_label"
        assert errors[0]["message"] == (
            "The text prompt for label 'cookie_gap' contains a period; "
            "periods separate labels in the detection caption")

        # Req 5.8: nothing persisted, triggered, or audited.
        job = env.get_job(job_id)
        assert job["auto_label"]["prompt_overrides"] == {
            INCIDENT_LABEL: INCIDENT_PROMPT}
        assert env.dda.lambda_client.invocations == []
        assert env.audit_events("prelabels_rerun") == []
        for task in env.tasks(job_id):
            assert task["prelabel_status"] == "Failed"
            assert task["prelabel_error"] == INCIDENT_ERROR

    def test_corrected_override_accepted_and_persisted(self, env):
        """Req 5.5: retrying with the corrected noun-phrase override is
        accepted 202 and the job record's auto_label.prompt_overrides
        is replaced by the corrected map before the worker is
        triggered."""
        job_id = self.incident_job(env, failed=3)

        status, body = env.rerun(job_id, body={
            "prompt_overrides": {INCIDENT_LABEL: CORRECTED_PROMPT}})
        assert status == 202
        assert body["job_id"] == job_id
        assert body["retried_count"] == 3

        job = env.get_job(job_id)
        assert job["auto_label"]["prompt_overrides"] == {
            INCIDENT_LABEL: CORRECTED_PROMPT}

        assert env.dda.lambda_client.payloads() == [
            {"action": "retry_prelabels", "job_id": job_id}]

        events = env.audit_events("prelabels_rerun")
        assert len(events) == 1
        assert events[0]["details"]["retried_count"] == 3
        assert events[0]["details"]["overrides_updated"] is True
