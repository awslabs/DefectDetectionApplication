"""
Labeling job deletion property tests.

Spec: labeling-job-cleanup-work-stealing-and-podium, task 1.5.

Four properties over the real deletion machinery — the
`DELETE /labeling/{id}` route (dda_labeling.request_job_deletion,
driven through the module handler's router so
`_inject_job_usecase_scope` and the real @rbac_check path run) and the
worker's `delete_job` action (dda_labeling_worker.delete_job_data,
driven through the worker handler's action dispatcher) — against the
moto-backed stack from conftest.py, 100 Hypothesis examples each:

**Feature: labeling-job-cleanup-work-stealing-and-podium, Property 1:
The Deletion_Route accepts exactly the deletable predicate**
**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5**

**Feature: labeling-job-cleanup-work-stealing-and-podium, Property 2:
Completed deletion leaves zero traces of the job's own state**
**Validates: Requirements 2.1, 2.2, 2.3, 2.8**

**Feature: labeling-job-cleanup-work-stealing-and-podium, Property 3:
Deletion is confined to the job's own artifact prefix**
**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 10.6**

**Feature: labeling-job-cleanup-work-stealing-and-podium, Property 4:
Deletion failure and skip paths are total and ordered**
**Validates: Requirements 2.4, 2.5, 2.6**

Oracles
-------
Restated in this file, never imported from the code under test:

- **Deletable predicate** (requirements glossary): a DELETE answers
  202 exactly when the job exists, is DDA-backed (an absent
  `labeling_backend` reads GroundTruth), and holds a Deletable_Status
  (Completed / Failed / Stopped / DeleteFailed / Deleting). An absent
  job answers 404; every other rejection answers 400. Acceptance
  leaves the record in Deleting with `delete_requested_by/at`
  recorded (a job already Deleting stays byte-identical — Req 1.5's
  recovery path re-invokes without a write; a fresh flip touches
  exactly status / delete_requested_by / delete_requested_at /
  updated_at) and invokes the worker exactly once with
  {action: 'delete_job', job_id}; every rejection leaves the record
  byte-identical with zero invocations.
- **Zero-trace end state** (Req 2.1-2.3): after a completed
  delete_job run, the artifacts bucket holds zero objects under
  labeling/{usecase_id}/{job_id}/, the tasks table holds zero items
  under the job's PK, and the job record is gone. Idempotence
  (Req 2.8): the same end state falls out when a crashed prior run
  already removed an arbitrary subset of the artifacts and task
  items, and a re-invocation after completion reports skipped with
  the end state unchanged.
- **Confinement** (Req 3.1-3.5, 10.6): every S3 deletion the worker
  issues names the portal artifacts bucket with a key beginning with
  the deleted job's own Job_Artifact_Prefix; zero S3 calls of any
  kind name the dataset bucket; dataset objects, sibling jobs'
  records / task items / artifact prefixes (including a
  prefix-adversarial sibling whose job id textually extends the
  deleted id), labeling-previews/ objects, and the output bucket's
  labeled/{job_id}/ deliverables all end byte-identical.
- **Failure/skip totality and ordering** (Req 2.4-2.6): an absent or
  non-Deleting job is recorded skipped with zero deletions and the
  environment byte-identical; an injected failure at the artifact
  step leaves artifacts, task items, and the record all present; at
  the task-item step leaves task items and the record present (the
  artifacts already deleted stay deleted); at the job-record step
  leaves the record present with the artifacts and task items gone —
  in every failure case the surviving record holds status
  DeleteFailed and a failure_reason carrying the injected error, with
  every untouched attribute preserved.

Harness: the module-scoped fixture imports the real modules inside
the moto mock (sys.modules popped first, the
test_dda_labeling_worker_distribute.py convention), installs a fake
Lambda client at dda_labeling.lambda_client capturing the
`_invoke_labeling_worker` payloads (DDA_LABELING_WORKER_FUNCTION_NAME
set so the invoke engages), and wraps the worker's module-level
s3_client in a transparent recording spy (the confinement evidence).
Per-step failure injection swaps the worker's s3_client / table
attributes for raising proxies inside try/finally per example.
Hypothesis cannot consume function-scoped fixtures, so per-example
isolation comes from uuid use-case / job / task ids (the sibling
property suites' convention). The >1000-object artifact population
(Req 2.1's pagination) rides Property 2 as one pinned @example so the
generated examples stay fast.
"""
import json
import os
import sys
import uuid
from datetime import datetime
from types import SimpleNamespace

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

REGION = "us-east-1"
ARTIFACTS_BUCKET = "test-portal-artifacts"  # conftest PORTAL_ARTIFACTS_BUCKET
DATASET_BUCKET = "test-deletion-prop-dataset"
WORKER_FUNCTION_NAME = "test-dda-deletion-prop-worker"

_ALL_STATUSES = ("InProgress", "Completed", "Failed", "Stopped",
                 "Deleting", "DeleteFailed")
_DELETABLE_STATUSES = ("Completed", "Failed", "Stopped", "DeleteFailed",
                       "Deleting")
# The acceptance flip's moving parts on the job record; everything
# else must survive the flip byte-identical.
_FLIP_TOUCHED = ("status", "delete_requested_by", "delete_requested_at",
                 "updated_at")
# The DeleteFailed transition's moving parts.
_FAIL_TOUCHED = ("status", "failure_reason", "updated_at")


# ------------------------------------------------------- fakes and spies

class FakeLambdaClient:
    """Captures _invoke_labeling_worker's async invocations (the
    test_dda_labeling_worker_distribute.py FakeLambdaClient shape)."""

    def __init__(self):
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        return {"StatusCode": 202}


class RecordingS3Client:
    """Transparent spy around the worker's module-level s3_client:
    forwards every call to the wrapped (moto) client while recording
    each call's target bucket and every deleted key — the Property 3/4
    confinement evidence."""

    def __init__(self, real):
        self._real = real
        self.calls = []       # (method name, Bucket kwarg)
        self.deletions = []   # (bucket, key) per deleted object

    def reset(self):
        self.calls.clear()
        self.deletions.clear()

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        def wrapper(*args, **kwargs):
            self.calls.append((name, kwargs.get("Bucket")))
            if name == "delete_objects":
                for obj in (kwargs.get("Delete") or {}).get("Objects", []):
                    self.deletions.append(
                        (kwargs.get("Bucket"), obj["Key"]))
            elif name == "delete_object":
                self.deletions.append(
                    (kwargs.get("Bucket"), kwargs.get("Key")))
            return attr(*args, **kwargs)

        return wrapper


class FailingProxy:
    """Delegates to the wrapped client/table, raising at exactly the
    chosen method — the per-step failure injection of Property 4."""

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


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def stack(aws_stack):
    """The real dda_labeling + dda_labeling_worker modules imported
    inside the moto mock (sys.modules popped first so their
    module-level boto3 clients bind to moto), the route's
    lambda_client replaced by the capturing fake (with
    DDA_LABELING_WORKER_FUNCTION_NAME set so _invoke_labeling_worker
    engages), and the worker's s3_client wrapped in the recording
    spy. The dataset bucket exists so confinement seeds have a real
    Dataset_Location to survive in."""
    sys.modules.pop("dda_labeling", None)
    sys.modules.pop("dda_labeling_worker", None)
    import dda_labeling
    import dda_labeling_worker

    fake_lambda = FakeLambdaClient()
    dda_labeling.lambda_client = fake_lambda
    s3_spy = RecordingS3Client(dda_labeling_worker.s3_client)
    dda_labeling_worker.s3_client = s3_spy

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
            s3_spy=s3_spy,
            tables=aws_stack.tables,
        )
    finally:
        if previous is None:
            os.environ.pop("DDA_LABELING_WORKER_FUNCTION_NAME", None)
        else:
            os.environ["DDA_LABELING_WORKER_FUNCTION_NAME"] = previous


class DeletionEnv:
    """Per-example seeding + invocation facade (fresh Use_Case, caller,
    and job/task/artifact ids per Hypothesis example — uuid-isolated,
    the retry-route property suite's shape)."""

    def __init__(self, stack):
        self.stack = stack
        self.tables = stack.tables
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.dataset_prefix = f"training-images/{uuid.uuid4().hex[:8]}/"
        self.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Deletion Property Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        user_id = f"user-{uuid.uuid4()}"
        # DataScientist holds MANAGE_LABELING_JOBS via the JWT
        # custom:role fallback — the retry-route property suite's
        # authorization posture (authorization edges themselves are
        # the example suite's concern).
        self.manager = {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": "DataScientist",
        }

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
            # Production construction: only the deletion route writes
            # these statuses, recording the requester in the same
            # update — a Deleting/DeleteFailed record always carries
            # them.
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

    def put_object(self, bucket, key):
        body = f"adversarial:{bucket}:{key}".encode()
        self.s3.put_object(Bucket=bucket, Key=key, Body=body)
        return body

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

    def body(self, bucket, key):
        return self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()

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


# ------------------------------------------------------------- generators

_SEGMENT_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-_"
_segments = st.text(alphabet=_SEGMENT_ALPHABET, min_size=1, max_size=10)
# Relative artifact keys: 1-3 path segments (nesting included) plus an
# optional extension — always non-empty, never starting or ending
# with '/'.
_relative_keys = st.builds(
    lambda parts, suffix: "/".join(parts) + suffix,
    st.lists(_segments, min_size=1, max_size=3),
    st.sampled_from(("", ".json", ".png")),
)

# Arbitrary extra job attributes the route/worker must carry through
# or preserve untouched; prefixed so they can never collide with the
# attributes under test.
_extra_names = st.text(alphabet="abcdefghijklmnop_", min_size=1,
                       max_size=10).map(lambda name: f"x_{name}")
_extra_values = st.one_of(
    st.text(alphabet=_SEGMENT_ALPHABET, min_size=1, max_size=12),
    st.integers(min_value=0, max_value=10**6),
    st.booleans(),
)
_extras = st.dictionaries(_extra_names, _extra_values, max_size=3)

# Property 1's scenario atoms: every backend x status combination plus
# the absent job, sampled uniformly so 100 examples cover each arm.
_ROUTE_SCENARIOS = tuple(
    (backend, status)
    for backend in ("DDA", "GroundTruth", None)  # None = attribute absent
    for status in _ALL_STATUSES
) + (("absent", None),)


@st.composite
def _route_cases(draw):
    backend, status = draw(st.sampled_from(_ROUTE_SCENARIOS))
    return SimpleNamespace(
        exists=backend != "absent",
        backend=None if backend == "absent" else backend,
        status=status,
        extras=draw(_extras),
    )


@st.composite
def _cleanup_cases(draw):
    return SimpleNamespace(
        task_count=draw(st.integers(min_value=0, max_value=6)),
        relative_keys=draw(st.lists(_relative_keys, min_size=0,
                                    max_size=8, unique=True)),
        # 'none': one straight run; 'partial': a crashed prior run
        # already removed a subset (idempotent completion); 'rerun':
        # a second invocation after completion (idempotent no-op).
        interruption=draw(st.sampled_from(("none", "partial", "rerun"))),
        pre_deleted_artifacts=draw(st.frozensets(
            st.integers(min_value=0, max_value=63), max_size=8)),
        pre_deleted_tasks=draw(st.frozensets(
            st.integers(min_value=0, max_value=63), max_size=6)),
    )


# The >1000-object population (Req 2.1's pagination + batching),
# pinned as one deterministic example so the generated cases stay
# fast: 1005 objects forces a second list/delete pass (moto pages at
# 1000 keys, the delete_objects batch limit).
_GIANT_CASE = SimpleNamespace(
    task_count=2,
    relative_keys=tuple(f"prelabels/task-{index:05d}.json"
                        for index in range(1005)),
    interruption="none",
    pre_deleted_artifacts=frozenset(),
    pre_deleted_tasks=frozenset(),
)


@st.composite
def _confinement_cases(draw):
    return SimpleNamespace(
        own_keys=draw(st.lists(_relative_keys, min_size=1, max_size=6,
                               unique=True)),
        own_tasks=draw(st.integers(min_value=0, max_value=4)),
        sibling_keys=draw(st.lists(_relative_keys, min_size=1,
                                   max_size=3, unique=True)),
        sibling_tasks=draw(st.integers(min_value=1, max_value=3)),
        dataset_names=draw(st.lists(_segments, min_size=1, max_size=4,
                                    unique=True)),
        preview_names=draw(st.lists(_segments, min_size=1, max_size=2,
                                    unique=True)),
        output_names=draw(st.lists(_segments, min_size=1, max_size=2,
                                   unique=True)),
    )


_SKIP_STATUSES = ("InProgress", "Completed", "Failed", "Stopped",
                  "DeleteFailed")


@st.composite
def _failure_cases(draw):
    scenario = draw(st.sampled_from(
        ("absent",)
        + tuple(f"skip:{status}" for status in _SKIP_STATUSES)
        + ("fail:artifacts", "fail:tasks", "fail:record")))
    return SimpleNamespace(
        scenario=scenario,
        task_count=draw(st.integers(min_value=0, max_value=5)),
        relative_keys=draw(st.lists(_relative_keys, min_size=0,
                                    max_size=5, unique=True)),
    )


# =========================================================================== #
# Property 1: The Deletion_Route accepts exactly the deletable predicate
# =========================================================================== #

class TestProperty1DeletionRouteAcceptsExactlyTheDeletablePredicate:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_route_cases())
    def test_property_deletion_route_accepts_exactly_the_deletable_predicate(
            self, stack, case):
        """Feature: labeling-job-cleanup-work-stealing-and-podium,
        Property 1: The Deletion_Route accepts exactly the deletable
        predicate — *For any* job record state (existing or absent;
        DDA or Ground Truth backend; status across InProgress,
        Completed, Failed, Stopped, Deleting, DeleteFailed), a
        `DELETE /labeling/{id}` request SHALL answer 202 exactly when
        the job exists, is DDA-backed, and holds a Deletable_Status —
        transitioning to Deleting (or staying Deleting) with
        `delete_requested_by/at` recorded and exactly one `delete_job`
        worker invocation — and SHALL otherwise answer 404 (absent) or
        400 (Ground Truth / non-deletable status) with the record
        byte-identical and zero worker invocations.

        **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5**
        """
        env = DeletionEnv(stack)
        if case.exists:
            job_id = env.put_job(backend=case.backend, status=case.status,
                                 extras=case.extras)
        else:
            job_id = f"labeling-{uuid.uuid4().hex[:10]}"

        before = env.get_job(job_id)
        invocations_before = len(stack.lambda_client.invocations)
        floor = int(datetime.utcnow().timestamp())

        status_code, body = env.delete_route(job_id)

        accepted = (case.exists and case.backend == "DDA"
                    and case.status in _DELETABLE_STATUSES)
        if accepted:
            assert status_code == 202, (status_code, body)
            assert body == {"job_id": job_id, "status": "Deleting"}, body

            after = env.get_job(job_id)
            assert after is not None, "accepted job record vanished"
            assert after["status"] == "Deleting"
            # delete_requested_by/at recorded on the surviving record.
            assert after.get("delete_requested_by"), after
            assert int(after["delete_requested_at"]) > 0, after

            if case.status == "Deleting":
                # Req 1.5: the recovery path re-invokes without a
                # write — the record stays byte-identical.
                assert after == before, (
                    f"already-Deleting job modified by the re-trigger: "
                    f"{after!r} != {before!r}")
            else:
                # The fresh flip: requester + timestamp from this
                # request, updated_at the same :now, everything else
                # byte-identical.
                assert after["delete_requested_by"] == \
                    env.manager["user_id"], after
                assert int(after["delete_requested_at"]) >= floor, after
                assert after["updated_at"] == \
                    after["delete_requested_at"], after
                untouched_after = {
                    key: value for key, value in after.items()
                    if key not in _FLIP_TOUCHED}
                untouched_before = {
                    key: value for key, value in before.items()
                    if key not in _FLIP_TOUCHED}
                assert untouched_after == untouched_before, (
                    f"the Deleting flip disturbed unrelated attributes: "
                    f"{untouched_after!r} != {untouched_before!r}")

            # Exactly one delete_job worker invocation.
            new = stack.lambda_client.invocations[invocations_before:]
            assert len(new) == 1, (
                f"expected exactly one worker invocation, saw "
                f"{len(new)}: {new!r}")
            assert json.loads(new[0]["Payload"]) == {
                "action": "delete_job", "job_id": job_id}
            assert new[0]["FunctionName"] == WORKER_FUNCTION_NAME
            assert new[0]["InvocationType"] == "Event"
        else:
            expected_status = 404 if not case.exists else 400
            assert status_code == expected_status, (
                f"backend={case.backend!r} status={case.status!r} "
                f"exists={case.exists!r}: expected {expected_status}, "
                f"got {status_code} {body!r}")
            assert "error" in body, body
            # Nothing changed: the record byte-identical (or still
            # absent), zero worker invocations.
            assert env.get_job(job_id) == before, (
                "rejected request modified the job record")
            assert (len(stack.lambda_client.invocations)
                    == invocations_before), (
                "rejected request invoked the worker")


# =========================================================================== #
# Property 2: Completed deletion leaves zero traces of the job's own state
# =========================================================================== #

class TestProperty2CompletedDeletionLeavesZeroTraces:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_cleanup_cases())
    @example(case=_GIANT_CASE)
    def test_property_completed_deletion_leaves_zero_traces(
            self, stack, case):
        """Feature: labeling-job-cleanup-work-stealing-and-podium,
        Property 2: Completed deletion leaves zero traces of the job's
        own state — *For any* generated job environment (task counts,
        artifact object sets under the Job_Artifact_Prefix including
        nested keys and >1000-object populations), running the
        Deletion_Worker on the Deleting job SHALL leave zero objects
        under the Job_Artifact_Prefix, zero task items for the job,
        and no job record — and running it again after an interruption
        SHALL complete the same end state (idempotence).

        **Validates: Requirements 2.1, 2.2, 2.3, 2.8**
        """
        env = DeletionEnv(stack)
        job_id = env.put_job(status="Deleting")
        task_ids = env.put_tasks(job_id, case.task_count)
        keys = env.put_artifacts(job_id, case.relative_keys)

        if case.interruption == "partial":
            # A crashed prior run: an arbitrary subset of artifacts
            # and task items is already gone, the job still Deleting
            # (the record is deleted last, so a crash always leaves
            # it) — the re-run must complete the remainder (Req 2.8).
            for index in case.pre_deleted_artifacts:
                if keys:
                    env.s3.delete_object(
                        Bucket=ARTIFACTS_BUCKET,
                        Key=keys[index % len(keys)])
            for index in case.pre_deleted_tasks:
                if task_ids:
                    env.tables.labeling_tasks.delete_item(Key={
                        "job_id": job_id,
                        "task_id": task_ids[index % len(task_ids)]})

        remaining_artifacts = len(
            env.keys_under(ARTIFACTS_BUCKET, env.job_prefix(job_id)))
        remaining_tasks = len(env.task_items(job_id))

        result = env.run_worker(job_id)

        assert result.get("deleted") is True, result
        # The reported counts equal what actually remained to delete
        # (the giant example proves the pagination/batching walked all
        # 1005 objects).
        assert result["artifact_objects_deleted"] == remaining_artifacts, (
            result)
        assert result["tasks_deleted"] == remaining_tasks, result

        # Zero traces: no objects under the prefix, no task items, no
        # job record.
        assert env.keys_under(
            ARTIFACTS_BUCKET, env.job_prefix(job_id)) == []
        assert env.task_items(job_id) == []
        assert env.get_job(job_id) is None

        if case.interruption == "rerun":
            # Re-running after completion is a recorded skip with the
            # end state unchanged (Req 2.8's idempotence).
            second = env.run_worker(job_id)
            assert second.get("skipped") is True, second
            assert env.keys_under(
                ARTIFACTS_BUCKET, env.job_prefix(job_id)) == []
            assert env.task_items(job_id) == []
            assert env.get_job(job_id) is None


# =========================================================================== #
# Property 3: Deletion is confined to the job's own artifact prefix
# =========================================================================== #

class TestProperty3DeletionConfinedToTheJobsOwnArtifactPrefix:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_confinement_cases())
    def test_property_deletion_confined_to_the_jobs_own_artifact_prefix(
            self, stack, case):
        """Feature: labeling-job-cleanup-work-stealing-and-podium,
        Property 3: Deletion is confined to the job's own artifact
        prefix — *For any* adversarially seeded environment (dataset
        objects under the job's Dataset_Location, sibling jobs with
        their own task items and artifact prefixes, labeling-previews/
        objects, and labeled/{job_id}/ output-bucket objects), a
        completed deletion SHALL leave every one of those
        byte-identical — every S3 deletion the worker issued SHALL
        target the Portal_Artifacts_Bucket with a key beginning with
        the deleted job's own Job_Artifact_Prefix, and zero S3 calls
        SHALL target the dataset bucket.

        **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 10.6**
        """
        env = DeletionEnv(stack)
        spy = stack.s3_spy

        job_id = env.put_job(status="Deleting")
        env.put_tasks(job_id, case.own_tasks)
        own_keys = env.put_artifacts(job_id, case.own_keys)

        # Adversarial seeds — every one must end byte-identical.
        preserved = {}  # (bucket, key) -> seeded body

        # (1) Dataset objects under the job's own Dataset_Location
        #     (Req 3.1: no delete or write ever targets it).
        for name in case.dataset_names:
            key = f"{env.dataset_prefix}{name}.jpg"
            preserved[(DATASET_BUCKET, key)] = env.put_object(
                DATASET_BUCKET, key)

        # (2) A sibling job in the same Use_Case: record, task items,
        #     and its own artifact prefix (Req 3.4, 10.6).
        sibling_id = env.put_job(status="InProgress")
        env.put_tasks(sibling_id, case.sibling_tasks)
        for key in env.put_artifacts(sibling_id, case.sibling_keys):
            preserved[(ARTIFACTS_BUCKET, key)] = env.body(
                ARTIFACTS_BUCKET, key)

        # (3) The prefix-adversarial sibling: its job id textually
        #     extends the deleted id, so its keys share every byte of
        #     the deleted prefix except the trailing '/' boundary
        #     (Req 3.2's sharpest edge).
        edge_key = (f"labeling/{env.usecase_id}/{job_id}-x/"
                    f"prelabels/task-000000.json")
        preserved[(ARTIFACTS_BUCKET, edge_key)] = env.put_object(
            ARTIFACTS_BUCKET, edge_key)

        # (4) labeling-previews/ objects (Req 3.5).
        for name in case.preview_names:
            key = f"labeling-previews/{env.usecase_id}/{name}.png"
            preserved[(ARTIFACTS_BUCKET, key)] = env.put_object(
                ARTIFACTS_BUCKET, key)

        # (5) The Use_Case output bucket's labeled/{job_id}/
        #     deliverables — the deleted job's own manifest is
        #     retained (Req 3.3).
        for name in case.output_names:
            key = f"labeled/{job_id}/{name}.json"
            preserved[(DATASET_BUCKET, key)] = env.put_object(
                DATASET_BUCKET, key)

        sibling_record_before = env.get_job(sibling_id)
        sibling_tasks_before = env.task_items(sibling_id)

        spy.reset()
        result = env.run_worker(job_id)
        assert result.get("deleted") is True, result

        # The deletion completed: the job's own state is gone (the
        # confinement claim is not vacuous).
        assert env.keys_under(
            ARTIFACTS_BUCKET, env.job_prefix(job_id)) == []
        assert env.task_items(job_id) == []
        assert env.get_job(job_id) is None

        # Every S3 deletion named the artifacts bucket and a key under
        # the deleted job's own prefix (Req 3.2).
        own_prefix = env.job_prefix(job_id)
        assert len(spy.deletions) == len(own_keys), (
            f"deletion count drifted from the job's own artifact "
            f"count: {spy.deletions!r}")
        for bucket, key in spy.deletions:
            assert bucket == ARTIFACTS_BUCKET, (
                f"S3 deletion targeted a foreign bucket: "
                f"{bucket!r}:{key!r}")
            assert key.startswith(own_prefix), (
                f"S3 deletion escaped the Job_Artifact_Prefix "
                f"{own_prefix!r}: {key!r}")

        # Zero S3 calls of any kind targeted the dataset bucket
        # (Req 3.1 structurally).
        dataset_calls = [(method, bucket) for method, bucket in spy.calls
                         if bucket == DATASET_BUCKET]
        assert dataset_calls == [], (
            f"the worker touched the dataset bucket: {dataset_calls!r}")

        # Every adversarial object byte-identical (Req 3.3, 3.4, 3.5).
        for (bucket, key), body in preserved.items():
            assert env.body(bucket, key) == body, (
                f"adversarial object modified: {bucket}:{key}")

        # The sibling job's record and task items byte-identical
        # (Req 3.4, 10.6).
        assert env.get_job(sibling_id) == sibling_record_before
        assert env.task_items(sibling_id) == sibling_tasks_before


# =========================================================================== #
# Property 4: Deletion failure and skip paths are total and ordered
# =========================================================================== #

class TestProperty4DeletionFailureAndSkipPathsAreTotalAndOrdered:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_failure_cases())
    def test_property_deletion_failure_and_skip_paths_total_and_ordered(
            self, stack, case):
        """Feature: labeling-job-cleanup-work-stealing-and-podium,
        Property 4: Deletion failure and skip paths are total and
        ordered — *For any* worker invocation state (job absent, job
        in any non-Deleting status, or an injected failure at the
        artifact, task-item, or job-record step), the Deletion_Worker
        SHALL perform zero deletions on the skip paths, and on a
        failure SHALL leave the job record present with status
        DeleteFailed and a recorded failure_reason — the job record
        surviving every pre-record-step failure.

        **Validates: Requirements 2.4, 2.5, 2.6**
        """
        env = DeletionEnv(stack)
        spy = stack.s3_spy
        worker = stack.worker

        if case.scenario == "absent":
            # Req 2.5: an absent job is a recorded skip with zero
            # deletions.
            job_id = f"labeling-{uuid.uuid4().hex[:10]}"
            spy.reset()
            result = env.run_worker(job_id)
            assert result.get("skipped") is True, result
            assert spy.calls == [], (
                f"skip path touched S3: {spy.calls!r}")
            assert env.get_job(job_id) is None
            return

        if case.scenario.startswith("skip:"):
            # Req 2.5: any non-Deleting status is a recorded skip with
            # zero deletions and the environment byte-identical.
            status = case.scenario.split(":", 1)[1]
            job_id = env.put_job(status=status)
            env.put_tasks(job_id, case.task_count)
            keys = env.put_artifacts(job_id, case.relative_keys)
            record_before = env.get_job(job_id)
            tasks_before = env.task_items(job_id)

            spy.reset()
            result = env.run_worker(job_id)

            assert result.get("skipped") is True, result
            assert spy.calls == [], (
                f"skip path touched S3: {spy.calls!r}")
            assert env.get_job(job_id) == record_before, (
                "skip path modified the job record")
            assert env.task_items(job_id) == tasks_before, (
                "skip path modified task items")
            assert sorted(env.keys_under(
                ARTIFACTS_BUCKET, env.job_prefix(job_id))) == sorted(keys)
            return

        # Injected failure at a chosen step (Req 2.4, 2.6).
        step = case.scenario.split(":", 1)[1]
        job_id = env.put_job(status="Deleting")
        env.put_tasks(job_id, case.task_count)
        keys = env.put_artifacts(job_id, case.relative_keys)
        record_before = env.get_job(job_id)
        tasks_before = env.task_items(job_id)
        marker = f"injected {step} failure {uuid.uuid4().hex[:6]}"

        spy.reset()
        if step == "artifacts":
            original = worker.s3_client
            worker.s3_client = FailingProxy(
                original, "list_objects_v2", marker)
            try:
                result = env.run_worker(job_id)
            finally:
                worker.s3_client = original
        elif step == "tasks":
            original = worker.labeling_tasks_table
            worker.labeling_tasks_table = FailingProxy(
                original, "query", marker)
            try:
                result = env.run_worker(job_id)
            finally:
                worker.labeling_tasks_table = original
        else:  # step == "record"
            original = worker.labeling_jobs_table
            worker.labeling_jobs_table = FailingProxy(
                original, "delete_item", marker)
            try:
                result = env.run_worker(job_id)
            finally:
                worker.labeling_jobs_table = original

        assert result.get("status") == "DeleteFailed", result
        assert "error" in result, result

        # The record survives every failure, holding DeleteFailed and
        # the failure reason (Req 2.6); untouched attributes
        # preserved.
        record_after = env.get_job(job_id)
        assert record_after is not None, (
            f"the job record did not survive the {step}-step failure")
        assert record_after["status"] == "DeleteFailed", record_after
        assert marker in record_after.get("failure_reason", ""), (
            f"failure_reason does not carry the injected error: "
            f"{record_after.get('failure_reason')!r}")
        untouched_after = {key: value
                           for key, value in record_after.items()
                           if key not in _FAIL_TOUCHED}
        untouched_before = {key: value
                            for key, value in record_before.items()
                            if key not in _FAIL_TOUCHED}
        assert untouched_after == untouched_before, (
            f"the DeleteFailed transition disturbed unrelated "
            f"attributes: {untouched_after!r} != {untouched_before!r}")

        remaining_keys = sorted(env.keys_under(
            ARTIFACTS_BUCKET, env.job_prefix(job_id)))
        if step == "artifacts":
            # Ordered: a step-(a) failure means nothing anywhere was
            # deleted.
            assert remaining_keys == sorted(keys), (
                "artifact-step failure deleted artifact objects")
            assert env.task_items(job_id) == tasks_before, (
                "artifact-step failure deleted task items")
            assert spy.deletions == [], (
                f"artifact-step failure issued S3 deletions: "
                f"{spy.deletions!r}")
        elif step == "tasks":
            # Step (a) completed; already-deleted artifacts stay
            # deleted (Req 2.6) while every task item survives.
            assert remaining_keys == [], (
                "task-step failure left the completed artifact step "
                "undone")
            assert env.task_items(job_id) == tasks_before, (
                "task-step failure deleted task items")
        else:
            # Steps (a)+(b) completed; the record is the sole survivor
            # of the job's own state (Req 2.4: record last).
            assert remaining_keys == []
            assert env.task_items(job_id) == []
