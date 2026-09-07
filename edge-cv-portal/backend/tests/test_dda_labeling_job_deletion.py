"""
Labeling job deletion example tests.

Spec: labeling-job-cleanup-work-stealing-and-podium, task 1.8.
Feature: labeling-job-cleanup-work-stealing-and-podium

Example (non-property) coverage of the deletion machinery — the
`DELETE /labeling/{id}` route (dda_labeling.request_job_deletion,
driven through the module handler's router so
`_inject_job_usecase_scope` and the real @rbac_check path run) and the
worker's `delete_job` action (dda_labeling_worker, driven through the
worker handler's action dispatcher) — against the moto-backed stack
from conftest.py:

- Non-admin denial with the job's Use_Case scope resolved for rbac
  (Req 1.6): a DataLabeler caller answers the standard rbac 403 whose
  body names the job's usecase_id (the router's
  _inject_job_usecase_scope observation) even when they hold a
  managing role in a *different* Use_Case, and the same caller granted
  that role in exactly the job's Use_Case is accepted — the check
  consulted the injected job scope, not 'global' and not some other
  scope.
- `job_delete_requested` audit fields (acting user, job id, prior
  status) and `job_deleted` audit fields + counts (usecase_id,
  tasks_deleted, artifact_objects_deleted), read back from the audit
  table shared_utils.log_audit_event persists to (Req 1.7, 2.7).
- The Req 1.8 race: a status flip injected between the route's read
  and its conditional write (the wrapped table's update_item lands the
  flip first, so the ConditionExpression on the read status fails) —
  the answer follows the fresh status (400 non-deletable / 409
  deletable-but-changed / 202 already-Deleting) with no partial write,
  zero worker invocations, and no audit event from the losing request.
- Exact rejection wordings pinned: the Ground Truth
  (SageMaker-managed) message and the stop-first message naming the
  status (Req 1.3, 1.4).
- Worker skip on a non-Deleting status at the dispatcher level with
  zero deletions (Req 2.5).
- The DeleteFailed retry flow end-to-end: accepted DELETE → injected
  artifact failure → DeleteFailed with failure_reason → DELETE again
  (the route flips DeleteFailed → Deleting and re-invokes the worker)
  → the retried run completes to zero traces (Req 1.5, 2.6, 2.8).

Harness: the module-scoped fixture reuses the
test_property_labeling_job_deletion.py patterns — the real modules
imported inside the moto mock (sys.modules popped first), a
FakeLambdaClient at dda_labeling.lambda_client capturing the
_invoke_labeling_worker payloads (DDA_LABELING_WORKER_FUNCTION_NAME
set so the invoke engages), and the DeletionEnv facade shape with
uuid-isolated Use_Case / job / task ids per test.
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

REGION = "us-east-1"
ARTIFACTS_BUCKET = "test-portal-artifacts"  # conftest PORTAL_ARTIFACTS_BUCKET
DATASET_BUCKET = "test-deletion-example-dataset"
WORKER_FUNCTION_NAME = "test-dda-deletion-example-worker"

# ------------------------------------------------- pinned wordings (Req 1.3,
# 1.4): byte-exact copies of the route's rejection messages.

GROUND_TRUTH_REJECTION = (
    "Only DDA labeling jobs can be deleted through this operation. "
    "This job uses the '{backend}' backend, whose lifecycle is managed "
    "by SageMaker Ground Truth."
)
STOP_FIRST_REJECTION = (
    "Labeling job {job_id} cannot be deleted: its status is "
    "'{status}'. Only Completed, Failed, Stopped, or DeleteFailed jobs "
    "can be deleted — stop the job first, then delete it. The job is "
    "unchanged."
)
CONCURRENT_RETRY_REJECTION = (
    "Labeling job {job_id} changed status concurrently (now "
    "'{status}'); please retry the deletion."
)


# ------------------------------------------------------- fakes and wrappers

class FakeLambdaClient:
    """Captures _invoke_labeling_worker's async invocations (the
    property suite's FakeLambdaClient shape)."""

    def __init__(self):
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        return {"StatusCode": 202}


class FailingProxy:
    """Delegates to the wrapped client/table, raising at exactly the
    chosen method — the artifact-step failure injection of the
    DeleteFailed retry flow."""

    def __init__(self, target, method, message):
        self._target = target
        self._method = method
        self._message = message

    def __getattr__(self, name):
        if name == self._method:
            message = self._message

            def boom(*args, **kwargs):
                raise RuntimeError(message)

            return boom
        return getattr(self._target, name)


class FlipOnFirstUpdateTable:
    """Wraps the route's jobs table for the Req 1.8 race: the first
    update_item call first lands a concurrent status flip through the
    real table, then delegates the original call — injecting the flip
    exactly between the route's read and its conditional write, so the
    ConditionExpression on the read status fails and the route must
    re-read and answer per the fresh status."""

    def __init__(self, real, job_id, flip_to):
        self._real = real
        self._job_id = job_id
        self._flip_to = flip_to
        self.flips = 0

    def update_item(self, **kwargs):
        if self.flips == 0:
            self.flips += 1
            self._real.update_item(
                Key={"job_id": self._job_id},
                UpdateExpression="SET #status = :fresh",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":fresh": self._flip_to},
            )
        return self._real.update_item(**kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def stack(aws_stack):
    """The real dda_labeling + dda_labeling_worker modules imported
    inside the moto mock (sys.modules popped first, the property
    suite's convention), the route's lambda_client replaced by the
    capturing fake, and DDA_LABELING_WORKER_FUNCTION_NAME set so
    _invoke_labeling_worker engages."""
    sys.modules.pop("dda_labeling", None)
    sys.modules.pop("dda_labeling_worker", None)
    import dda_labeling
    import dda_labeling_worker

    fake_lambda = FakeLambdaClient()
    dda_labeling.lambda_client = fake_lambda

    try:
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=DATASET_BUCKET)
    except ClientError:  # already created by an earlier module
        pass

    previous = os.environ.get("DDA_LABELING_WORKER_FUNCTION_NAME")
    os.environ["DDA_LABELING_WORKER_FUNCTION_NAME"] = WORKER_FUNCTION_NAME
    try:
        yield SimpleNamespace(
            route=dda_labeling,
            worker=dda_labeling_worker,
            lambda_client=fake_lambda,
            tables=aws_stack.tables,
        )
    finally:
        if previous is None:
            os.environ.pop("DDA_LABELING_WORKER_FUNCTION_NAME", None)
        else:
            os.environ["DDA_LABELING_WORKER_FUNCTION_NAME"] = previous


class DeletionEnv:
    """Per-test seeding + invocation facade (the property suite's
    DeletionEnv shape): fresh uuid Use_Case, caller, and job/task/
    artifact ids per test."""

    def __init__(self, stack):
        self.stack = stack
        self.tables = stack.tables
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.dataset_prefix = f"training-images/{uuid.uuid4().hex[:8]}/"
        self.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Deletion Example Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        user_id = f"user-{uuid.uuid4()}"
        # DataScientist holds MANAGE_LABELING_JOBS via the JWT
        # custom:role fallback — the property suite's default caller.
        self.manager = {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": "DataScientist",
        }

    # ------------------------------------------------------------ users
    def make_user(self, role):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def grant_role(self, user, usecase_id, role):
        """A per-Use_Case role row (the Team Management assignment
        path shared_utils.RBACManager.get_user_role consults)."""
        self.tables.user_roles.put_item(Item={
            "user_id": user["user_id"],
            "usecase_id": usecase_id,
            "role": role,
        })

    # ------------------------------------------------------------ seeding
    def put_job(self, backend="DDA", status="Deleting", extras=None,
                job_id=None):
        job_id = job_id or f"labeling-{uuid.uuid4().hex[:10]}"
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": f"job-{job_id}",
            "status": status,
            "task_type": "Classification",
            "label_set": ["normal", "anomaly"],
            "dataset_bucket": DATASET_BUCKET,
            "dataset_prefix": self.dataset_prefix,
            "image_count": 4,
            "created_at": 1,
            "created_by": self.manager["user_id"],
        }
        if backend is not None:
            item["labeling_backend"] = backend
        if status in ("Deleting", "DeleteFailed"):
            item["delete_requested_by"] = f"admin-{uuid.uuid4().hex[:6]}"
            item["delete_requested_at"] = 1_700_000_000
        if status == "DeleteFailed":
            item["failure_reason"] = "prior cleanup failed"
        item.update(extras or {})
        self.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def put_tasks(self, job_id, count):
        task_ids = []
        statuses = ("Assigned", "Submitted", "Inactive")
        for index in range(count):
            task_id = f"task-{index:06d}"
            self.tables.labeling_tasks.put_item(Item={
                "job_id": job_id,
                "task_id": task_id,
                "image_s3_uri": (f"s3://{DATASET_BUCKET}/"
                                 f"{self.dataset_prefix}img-{index:03d}.jpg"),
                "image_key": f"{self.dataset_prefix}img-{index:03d}.jpg",
                "usecase_id": self.usecase_id,
                "assignee_user_id": f"labeler-{index % 2}",
                "status": statuses[index % len(statuses)],
                "created_at": 1,
            })
            task_ids.append(task_id)
        return task_ids

    def job_prefix(self, job_id):
        return f"labeling/{self.usecase_id}/{job_id}/"

    def put_artifacts(self, job_id, relative_keys):
        keys = []
        for relative in relative_keys:
            key = f"{self.job_prefix(job_id)}{relative}"
            self.s3.put_object(Bucket=ARTIFACTS_BUCKET, Key=key,
                               Body=f"artifact:{key}".encode())
            keys.append(key)
        return keys

    # ------------------------------------------------------------ reading
    def get_job(self, job_id):
        return self.tables.labeling_jobs.get_item(
            Key={"job_id": job_id}).get("Item")

    def task_items(self, job_id):
        items = []
        kwargs = {"KeyConditionExpression": Key("job_id").eq(job_id)}
        while True:
            response = self.tables.labeling_tasks.query(**kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return sorted(items, key=lambda item: item["task_id"])

    def keys_under(self, bucket, prefix):
        keys = []
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        while True:
            response = self.s3.list_objects_v2(**kwargs)
            keys.extend(obj["Key"]
                        for obj in response.get("Contents", []))
            if not response.get("IsTruncated"):
                break
            kwargs["ContinuationToken"] = response["NextContinuationToken"]
        return keys

    def audit_events(self, action, job_id):
        """Audit entries for one action + job, oldest first (the audit
        table log_audit_event writes to; uuid job ids keep the filter
        precise across the shared session table)."""
        items, kwargs = [], {}
        while True:
            response = self.tables.audit_log.scan(**kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return sorted(
            (item for item in items
             if item.get("action") == action
             and item.get("resource_id") == job_id),
            key=lambda item: item["timestamp"])

    # ----------------------------------------------------------- invoking
    def delete_route(self, job_id, user=None):
        """DELETE /labeling/{id} through the real router (scope
        injection + @rbac_check included)."""
        user = user or self.manager
        event = {
            "httpMethod": "DELETE",
            "resource": "/labeling/{id}",
            "path": f"/labeling/{job_id}",
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
        response = self.stack.route.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def run_worker(self, job_id):
        """The delete_job action through the worker's dispatcher."""
        return self.stack.worker.handler(
            {"action": "delete_job", "job_id": job_id}, None)


@pytest.fixture
def env(stack):
    return DeletionEnv(stack)


# --------------------------------------------------- authorization (Req 1.6)

class TestDeletionAuthorization:
    def test_non_admin_denied_with_the_jobs_usecase_scope_resolved(
            self, env, stack):
        """Req 1.6: a DataLabeler (no MANAGE_LABELING_JOBS) answers the
        standard rbac 403 whose body names the job's usecase_id — the
        observable proof the router injected the job's Use_Case scope
        for @rbac_check rather than falling back to 'global'. A
        managing role held in a *different* Use_Case does not help:
        the check ran in exactly the job's scope. Nothing changes and
        the worker is never invoked."""
        job_id = env.put_job(status="Completed")
        before = env.get_job(job_id)
        invocations_before = len(stack.lambda_client.invocations)

        labeler = env.make_user("DataLabeler")
        # A managing role in a foreign Use_Case must not authorize the
        # deletion of this job.
        env.grant_role(labeler, f"uc-{uuid.uuid4()}", "DataScientist")

        status, body = env.delete_route(job_id, user=labeler)

        assert status == 403, (status, body)
        assert body == {
            "error": "Insufficient permissions",
            "required_permissions": ["manage_labeling_jobs"],
            "usecase_id": env.usecase_id,  # the injected job scope
        }, body
        assert env.get_job(job_id) == before, (
            "denied request modified the job record")
        assert (len(stack.lambda_client.invocations)
                == invocations_before), (
            "denied request invoked the worker")

    def test_role_granted_in_the_jobs_usecase_is_accepted(
            self, env, stack):
        """Req 1.6 (the positive scope observation): the same
        DataLabeler caller granted a managing role in exactly the
        job's Use_Case is authorized — the rbac check consulted the
        injected job scope."""
        job_id = env.put_job(status="Completed")
        labeler = env.make_user("DataLabeler")
        env.grant_role(labeler, env.usecase_id, "DataScientist")

        status, body = env.delete_route(job_id, user=labeler)

        assert status == 202, (status, body)
        assert body == {"job_id": job_id, "status": "Deleting"}
        after = env.get_job(job_id)
        assert after["status"] == "Deleting"
        assert after["delete_requested_by"] == labeler["user_id"]


# ---------------------------------------------- audit events (Req 1.7, 2.7)

class TestDeletionAuditEvents:
    def test_delete_requested_and_deleted_audit_fields_and_counts(
            self, env, stack):
        """Req 1.7: the accepted request writes `job_delete_requested`
        carrying the acting user, the job id, and the prior status.
        Req 2.7: the completed cleanup writes `job_deleted` carrying
        the job id, the Use_Case, and the deleted task-item and
        artifact-object counts — both read back from the audit table
        log_audit_event persists to."""
        job_id = env.put_job(status="Completed")
        env.put_tasks(job_id, 3)
        env.put_artifacts(job_id, [
            "prelabels/task-000000.json",
            "annotations/task-000000.json",
        ])

        status, _ = env.delete_route(job_id)
        assert status == 202

        requested = env.audit_events("job_delete_requested", job_id)
        assert len(requested) == 1, requested
        event = requested[0]
        assert event["user_id"] == env.manager["user_id"]  # acting user
        assert event["resource_type"] == "labeling_job"
        assert event["resource_id"] == job_id                 # job id
        assert event["result"] == "success"
        assert event["details"]["previous_status"] == "Completed"
        assert event["details"]["usecase_id"] == env.usecase_id

        result = env.run_worker(job_id)
        assert result.get("deleted") is True, result

        deleted = env.audit_events("job_deleted", job_id)
        assert len(deleted) == 1, deleted
        event = deleted[0]
        # Attributed to the deletion requester the route recorded.
        assert event["user_id"] == env.manager["user_id"]
        assert event["resource_type"] == "labeling_job"
        assert event["resource_id"] == job_id
        assert event["result"] == "success"
        assert event["details"]["usecase_id"] == env.usecase_id
        assert event["details"]["tasks_deleted"] == 3
        assert event["details"]["artifact_objects_deleted"] == 2


# --------------------------------------------- the concurrent flip (Req 1.8)

class TestConcurrentStatusFlipRace:
    @pytest.mark.parametrize("flip_to,expected_code", [
        # Fresh status non-deletable → the stop-first 400 naming it.
        ("InProgress", 400),
        # Fresh status deletable but changed → 409 asking for a retry.
        ("Failed", 409),
        # A concurrent deletion request won → 202, deletion in effect.
        ("Deleting", 202),
    ])
    def test_answer_follows_the_fresh_status_with_no_partial_write(
            self, env, stack, flip_to, expected_code):
        """Req 1.8: a status flip lands between the route's read and
        its conditional write (the wrapped table's update_item flips
        first), the ConditionExpression on the read status fails, and
        the route re-reads and answers per the fresh status — with no
        partial write: the record is exactly the seeded record plus
        the concurrent flip, the losing request records nothing
        (no delete_requested_by/at, no audit event) and invokes no
        worker."""
        job_id = env.put_job(status="Completed")
        before = env.get_job(job_id)
        invocations_before = len(stack.lambda_client.invocations)

        route = stack.route
        real_table = route.labeling_jobs_table
        wrapped = FlipOnFirstUpdateTable(real_table, job_id, flip_to)
        route.labeling_jobs_table = wrapped
        try:
            status, body = env.delete_route(job_id)
        finally:
            route.labeling_jobs_table = real_table

        assert wrapped.flips == 1, "the injected race never ran"
        assert status == expected_code, (status, body)
        if expected_code == 400:
            assert body == {
                "error": STOP_FIRST_REJECTION.format(
                    job_id=job_id, status=flip_to),
                "status": flip_to,
            }, body
        elif expected_code == 409:
            assert body == {
                "error": CONCURRENT_RETRY_REJECTION.format(
                    job_id=job_id, status=flip_to),
                "status": flip_to,
            }, body
        else:
            assert body == {"job_id": job_id, "status": "Deleting"}, body

        # No partial write: exactly the seeded record with the flipped
        # status; the losing request added nothing.
        after = env.get_job(job_id)
        assert after == {**before, "status": flip_to}, (
            f"the losing request left a partial write: {after!r}")
        assert "delete_requested_by" not in after
        assert "delete_requested_at" not in after
        assert (len(stack.lambda_client.invocations)
                == invocations_before), (
            "the losing request invoked the worker")
        assert env.audit_events("job_delete_requested", job_id) == [], (
            "the losing request wrote an audit event")


# --------------------------------------- rejection wordings (Req 1.3, 1.4)

class TestRejectionWordings:
    def test_ground_truth_rejection_wording_pinned(self, env, stack):
        """Req 1.3: the Ground Truth rejection states the lifecycle is
        SageMaker-managed — wording pinned byte-exact, nothing
        changed."""
        job_id = env.put_job(backend="GroundTruth", status="Completed")
        before = env.get_job(job_id)
        invocations_before = len(stack.lambda_client.invocations)

        status, body = env.delete_route(job_id)

        assert status == 400, (status, body)
        assert body == {
            "error": GROUND_TRUTH_REJECTION.format(backend="GroundTruth"),
            "labeling_backend": "GroundTruth",
        }, body
        assert env.get_job(job_id) == before
        assert (len(stack.lambda_client.invocations)
                == invocations_before)

    def test_stop_first_rejection_wording_names_the_status(
            self, env, stack):
        """Req 1.4: the InProgress rejection names the current status
        and the stop-first path — wording pinned byte-exact, nothing
        changed."""
        job_id = env.put_job(status="InProgress")
        before = env.get_job(job_id)
        invocations_before = len(stack.lambda_client.invocations)

        status, body = env.delete_route(job_id)

        assert status == 400, (status, body)
        assert body == {
            "error": STOP_FIRST_REJECTION.format(
                job_id=job_id, status="InProgress"),
            "status": "InProgress",
        }, body
        assert env.get_job(job_id) == before
        assert (len(stack.lambda_client.invocations)
                == invocations_before)


# ----------------------------------------------- worker skip (Req 2.5)

class TestWorkerSkipOnNonDeletingStatus:
    def test_dispatcher_skips_a_non_deleting_job_with_zero_deletions(
            self, env, stack):
        """Req 2.5: a delete_job invocation for a job whose status is
        not Deleting (here: Completed, e.g. the invoke racing a manual
        status change) is recorded as skipped at the dispatcher level,
        with the record, task items, and artifact objects all
        untouched."""
        job_id = env.put_job(status="Completed")
        env.put_tasks(job_id, 2)
        keys = env.put_artifacts(job_id, ["prelabels/task-000000.json"])
        record_before = env.get_job(job_id)
        tasks_before = env.task_items(job_id)

        result = env.run_worker(job_id)

        assert result == {
            "job_id": job_id,
            "action": "delete_job",
            "skipped": True,
            "status": "Completed",
            "reason": "job is Completed",
        }, result
        assert env.get_job(job_id) == record_before, (
            "the skip modified the job record")
        assert env.task_items(job_id) == tasks_before, (
            "the skip modified task items")
        assert sorted(env.keys_under(
            ARTIFACTS_BUCKET, env.job_prefix(job_id))) == sorted(keys), (
            "the skip touched artifact objects")


# ------------------------------- DeleteFailed retry (Req 1.5, 2.6, 2.8)

class TestDeleteFailedRetryFlow:
    def test_artifact_failure_then_retry_completes_to_zero_traces(
            self, env, stack):
        """End-to-end: the accepted DELETE flips Completed → Deleting
        and invokes the worker; the worker run fails at the artifact
        step (injected) and records DeleteFailed with the
        failure_reason (Req 2.6); a second DELETE flips
        DeleteFailed → Deleting and re-invokes the worker (Req 1.5's
        re-trigger discipline applied to the retry); the retried run
        completes the cleanup to zero traces (Req 2.8)."""
        job_id = env.put_job(status="Completed")
        env.put_tasks(job_id, 3)
        keys = env.put_artifacts(job_id, [
            "prelabels/task-000000.json",
            "prelabels/task-000001.json",
            "annotations/task-000000.json",
        ])

        # (1) The accepted request: Completed → Deleting + invocation.
        status, body = env.delete_route(job_id)
        assert status == 202, (status, body)
        assert body == {"job_id": job_id, "status": "Deleting"}
        first_invocation = stack.lambda_client.invocations[-1]
        assert json.loads(first_invocation["Payload"]) == {
            "action": "delete_job", "job_id": job_id}
        assert first_invocation["FunctionName"] == WORKER_FUNCTION_NAME
        assert first_invocation["InvocationType"] == "Event"

        # (2) The worker run fails at the artifact step (injected).
        worker = stack.worker
        marker = f"injected artifact failure {uuid.uuid4().hex[:6]}"
        original = worker.s3_client
        worker.s3_client = FailingProxy(original, "list_objects_v2",
                                        marker)
        try:
            result = env.run_worker(job_id)
        finally:
            worker.s3_client = original

        assert result.get("status") == "DeleteFailed", result
        failed = env.get_job(job_id)
        assert failed is not None, "the record did not survive the failure"
        assert failed["status"] == "DeleteFailed"
        assert marker in failed.get("failure_reason", ""), (
            f"failure_reason does not carry the injected error: "
            f"{failed.get('failure_reason')!r}")
        # The failure preceded every deletion: everything still here.
        assert sorted(env.keys_under(
            ARTIFACTS_BUCKET, env.job_prefix(job_id))) == sorted(keys)
        assert len(env.task_items(job_id)) == 3

        # (3) DELETE again: DeleteFailed → Deleting + re-invocation.
        invocations_before = len(stack.lambda_client.invocations)
        status, body = env.delete_route(job_id)
        assert status == 202, (status, body)
        assert body == {"job_id": job_id, "status": "Deleting"}
        retrying = env.get_job(job_id)
        assert retrying["status"] == "Deleting"
        assert retrying["delete_requested_by"] == env.manager["user_id"]
        new = stack.lambda_client.invocations[invocations_before:]
        assert len(new) == 1, new
        assert json.loads(new[0]["Payload"]) == {
            "action": "delete_job", "job_id": job_id}
        # The audit trail names each request's prior status (Req 1.7
        # applied across the retry).
        requested = env.audit_events("job_delete_requested", job_id)
        assert [event["details"]["previous_status"]
                for event in requested] == ["Completed", "DeleteFailed"]

        # (4) The retried worker run completes: zero traces (Req 2.8).
        result = env.run_worker(job_id)
        assert result.get("deleted") is True, result
        assert result["tasks_deleted"] == 3
        assert result["artifact_objects_deleted"] == 3
        assert env.keys_under(
            ARTIFACTS_BUCKET, env.job_prefix(job_id)) == []
        assert env.task_items(job_id) == []
        assert env.get_job(job_id) is None
        assert len(env.audit_events("job_deleted", job_id)) == 1
