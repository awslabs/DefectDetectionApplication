"""
Bug condition exploration suite for onnx-compile-error-diagnostics (task 1).

**Validates: Requirements 1.1-1.13, 1.15, 1.20, 1.21**

EXPLORATION SUITE — cases 1-8 are EXPECTED TO FAIL on the unfixed tree.
Their failures are the counterexamples proving the five hypothesized
defects exist:

- Case 1  (isBugCondition_2): the originating ONNX start-failure reason is
  destroyed on the FIRST poll, replaced by the ValidationException for the
  fabricated sentinel name `{safe_model_name}-onnx-failed`.
- Case 2  (property form of case 1): over generated error strings and
  N >= 1 polls, the reason no longer survives even N = 1.
- Case 3  (isBugCondition_1): the no-live-job entry IS described — one
  doomed describe_compilation_job for a job that never existed.
- Case 4: the entry's terminal `Failed` status is overwritten to `ERROR`.
- Case 5: a transient ThrottlingException against a healthy Neo job
  latches the record's overall status to `Failed`.
- Case 6  (isBugCondition_3): derive_compilation_status collapses the
  unmodeled `'ERROR'` value to `'Failed'` via the silent catch-all.
- Case 7: round-trip — polling an entry the system itself wrote triggers
  a failing describe call (the sentinel entry).
- Case 8  (isBugCondition_5): poller B (models.get_model) polls an ONNX
  export *training* job with the Neo describe_compilation_job API.
- Case 9  (non-goal guard, MUST PASS on unfixed AND fixed code, do NOT
  invert): no `jetson-xavier-jp7` compile target; exactly seven targets.

Follows test_vllm_packaging_dispatch.py: module-scoped fixture on the
moto-backed `aws_stack`, its own training-jobs table created with the
production key shape, and compilation.py / models.py loaded INSIDE the
mock so their module-level boto3 clients are intercepted. The SageMaker
use-case client is stubbed to behave like the service and record every
describe call.
"""
import importlib.util
import io
import json
import os
import string
import sys
import tarfile
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-onnx-diagnostics"
USECASE_BUCKET = "test-onnx-diagnostics-bucket"
# model_name is already hyphen-safe so safe_model_name == MODEL_NAME and the
# unfixed sentinel is exactly f"{MODEL_NAME}-onnx-failed".
MODEL_NAME = "diagmodel"
SENTINEL_JOB_NAME = f"{MODEL_NAME}-onnx-failed"

_FUNCTIONS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "functions")


def _load_module(filename, alias):
    """Load a functions/ handler under a distinct module name, INSIDE the
    moto mock, so its module-level boto3 clients are intercepted."""
    spec = importlib.util.spec_from_file_location(
        alias, os.path.join(_FUNCTIONS_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# SageMaker service stub
# ---------------------------------------------------------------------------

class FakeSageMakerService:
    """Behaves like the SageMaker service for this suite:

    - create_training_job raises the configured ClientError, or succeeds
      and seeds a describable training job
    - describe_compilation_job raises ValidationException for any name it
      was not seeded with (exactly what the real service does for the
      fabricated sentinel name)
    - describe_training_job answers only for seeded training-job names

    Every describe call is recorded as (api, name, outcome) so tests can
    assert on call counts and API choice.
    """

    def __init__(self):
        self.create_training_error = None       # ClientError to raise
        self.describe_compilation_error = None  # forced fault (throttling)
        self.training_jobs = {}                 # name -> describe response
        self.compilation_jobs = {}              # name -> describe response
        self.describe_calls = []                # (api, name, 'ok'|'raised')
        self.create_training_calls = []

    # -- assertions helpers -------------------------------------------------
    def calls(self, api=None, name=None):
        return [c for c in self.describe_calls
                if (api is None or c[0] == api)
                and (name is None or c[1] == name)]

    def failed_calls(self):
        return [c for c in self.describe_calls if c[2] == "raised"]

    # -- service surface ----------------------------------------------------
    def create_training_job(self, **kwargs):
        self.create_training_calls.append(kwargs)
        if self.create_training_error is not None:
            raise self.create_training_error
        name = kwargs["TrainingJobName"]
        self.training_jobs[name] = {
            "TrainingJobName": name,
            "TrainingJobStatus": "InProgress",
        }
        return {"TrainingJobArn":
                f"arn:aws:sagemaker:{REGION}:123456789012:training-job/{name}"}

    def create_compilation_job(self, **kwargs):
        name = kwargs["CompilationJobName"]
        self.compilation_jobs[name] = {
            "CompilationJobName": name,
            "CompilationJobStatus": "INPROGRESS",
        }
        return {"CompilationJobArn":
                f"arn:aws:sagemaker:{REGION}:123456789012:compilation-job/{name}"}

    def describe_training_job(self, TrainingJobName):
        if TrainingJobName in self.training_jobs:
            self.describe_calls.append(
                ("describe_training_job", TrainingJobName, "ok"))
            return self.training_jobs[TrainingJobName]
        self.describe_calls.append(
            ("describe_training_job", TrainingJobName, "raised"))
        raise ClientError(
            {"Error": {"Code": "ValidationException",
                       "Message": f"Requested resource not found: Training "
                                  f"job '{TrainingJobName}' does not exist."}},
            "DescribeTrainingJob")

    def describe_compilation_job(self, CompilationJobName):
        if self.describe_compilation_error is not None:
            self.describe_calls.append(
                ("describe_compilation_job", CompilationJobName, "raised"))
            raise self.describe_compilation_error
        if CompilationJobName in self.compilation_jobs:
            self.describe_calls.append(
                ("describe_compilation_job", CompilationJobName, "ok"))
            return self.compilation_jobs[CompilationJobName]
        self.describe_calls.append(
            ("describe_compilation_job", CompilationJobName, "raised"))
        raise ClientError(
            {"Error": {"Code": "ValidationException",
                       "Message": f"Could not find compilation job "
                                  f"'{CompilationJobName}'."}},
            "DescribeCompilationJob")


class _ServiceHolder:
    """Current FakeSageMakerService, swappable per test / per example."""
    current = None


def fresh_service():
    _ServiceHolder.current = FakeSageMakerService()
    return _ServiceHolder.current


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def _upload_model_artifact(s3):
    """A trained-model tarball shaped how extract_and_repackage_model
    expects it: a TorchScript .pt plus mochi.json carrying input_shape."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        pt = b"not-a-real-torchscript-model"
        info = tarfile.TarInfo("mochi.pt")
        info.size = len(pt)
        tar.addfile(info, io.BytesIO(pt))
        mochi = json.dumps(
            {"stages": [{"input_shape": [1, 3, 224, 224]}]}).encode()
        info = tarfile.TarInfo("mochi.json")
        info.size = len(mochi)
        tar.addfile(info, io.BytesIO(mochi))
    s3.put_object(Bucket=USECASE_BUCKET, Key="models/model.tar.gz",
                  Body=buf.getvalue())


@pytest.fixture(scope="module")
def diag_env(aws_stack):
    """Own training-jobs table (production key shape) + use-case bucket +
    the real compilation.py / models.py loaded inside the mock, with the
    SageMaker use-case client routed to the recording stub."""
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    aws_stack.s3.create_bucket(Bucket=USECASE_BUCKET)
    _upload_model_artifact(aws_stack.s3)

    compilation = _load_module("compilation.py", "portal_compilation_diag")
    models = _load_module("models.py", "portal_models_diag")

    # Route the SageMaker use-case client to the stub; S3 stays the real
    # moto-intercepted client so extract_and_repackage_model and the
    # sourcedir staging work against the use-case bucket.
    def _dispatch_get_usecase_client(service_name, usecase,
                                     session_name=None, region=None):
        if service_name == "sagemaker":
            return _ServiceHolder.current
        return boto3.client(service_name, region_name=region or REGION)

    compilation.get_usecase_client = _dispatch_get_usecase_client
    models.get_usecase_client = _dispatch_get_usecase_client

    # One use case (single-account, owning the bucket) + DataScientist.
    usecase_id = f"uc-{uuid.uuid4()}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "ONNX Diagnostics Use Case",
        "account_id": "123456789012",
        "s3_bucket": USECASE_BUCKET,
    })
    user_id = f"user-{uuid.uuid4()}"
    aws_stack.tables.user_roles.put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": "DataScientist",
    })

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        compilation=compilation,
        models=models,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        usecase_id=usecase_id,
        user_id=user_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth_context(env):
    return {"authorizer": {"claims": {
        "sub": env.user_id,
        "email": f"{env.user_id}@example.com",
        "cognito:username": env.user_id,
    }}}


def seed_training_record(env, **overrides):
    training_id = str(uuid.uuid4())
    item = {
        "training_id": training_id,
        "usecase_id": env.usecase_id,
        "model_name": MODEL_NAME,
        "model_type": "classification",
        "source": "trained",
        "status": "Completed",
        "artifact_s3": f"s3://{USECASE_BUCKET}/models/model.tar.gz",
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    item.update(overrides)
    env.training_jobs.put_item(Item=item)
    return training_id


def start_compile(env, training_id, targets):
    response = env.compilation.start_compilation_job({
        "httpMethod": "POST",
        "path": f"/api/v1/training/{training_id}/compile",
        "pathParameters": {"id": training_id},
        "body": json.dumps({"targets": targets}),
        "requestContext": _auth_context(env),
    }, None)
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def poll_status(env, training_id):
    """One invocation of get_compilation_status (poller A)."""
    response = env.compilation.get_compilation_status({
        "httpMethod": "GET",
        "path": f"/api/v1/training/{training_id}/compile",
        "pathParameters": {"id": training_id},
        "requestContext": _auth_context(env),
    }, None)
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def get_model(env, training_id):
    """One invocation of models.get_model (poller B)."""
    response = env.models.get_model({
        "httpMethod": "GET",
        "path": f"/api/v1/models/{training_id}",
        "pathParameters": {"id": training_id},
        "requestContext": _auth_context(env),
    }, None)
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def stored_record(env, training_id):
    item = env.training_jobs.get_item(
        Key={"training_id": training_id})["Item"]
    return item.get("compilation_jobs", []), item.get("compilation_status")


def entry_reason(job):
    """The diagnostic string the record carries: failure_reason or error."""
    return job.get("failure_reason") or job.get("error")


def onnx_entry(jobs):
    return next(j for j in jobs if j.get("target") == "onnx")


def start_onnx_failure(env, message, code="AccessDeniedException"):
    """Drive start_compilation_job with targets=['onnx'] and a raising
    create_training_job. Returns (training_id, service)."""
    service = fresh_service()
    service.create_training_error = ClientError(
        {"Error": {"Code": code, "Message": message}}, "CreateTrainingJob")
    training_id = seed_training_record(env)
    start_compile(env, training_id, ["onnx"])
    return training_id, service


# ---------------------------------------------------------------------------
# Case 1 — Reason destroyed on the first poll (isBugCondition_2, core bug)
# Validates: Requirements 1.5, 1.6, 1.7, 1.9
# ---------------------------------------------------------------------------

def test_case_1_reason_destroyed_on_first_poll(diag_env):
    originating = ("User: arn:aws:sts::123456789012:assumed-role is not "
                   "authorized to perform iam:PassRole on "
                   "role/DDASageMakerExecutionRole")
    training_id, service = start_onnx_failure(diag_env, originating)

    # The record carries the originating error after the start failure.
    jobs, _ = stored_record(diag_env, training_id)
    entry = onnx_entry(jobs)
    assert originating in (entry_reason(entry) or ""), (
        "start_compilation_job did not record the originating error")

    # ONE poll.
    poll_status(diag_env, training_id)

    # The originating error must STILL be the recorded reason. On unfixed
    # code the ValidationException for the fabricated sentinel
    # '{safe_model_name}-onnx-failed' has replaced it.
    jobs, _ = stored_record(diag_env, training_id)
    entry = onnx_entry(jobs)
    reason = entry_reason(entry) or ""
    assert originating in reason, (
        f"Originating reason destroyed by the first poll. Expected the "
        f"recorded reason to still contain the originating error, but the "
        f"record now carries: {reason!r}")


# ---------------------------------------------------------------------------
# Case 2 — Reason survives N polls (property form of Case 1)
# Validates: Requirements 1.5, 1.6, 1.9
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(
    fragment=st.text(alphabet=string.ascii_lowercase + string.digits,
                     min_size=1, max_size=24),
    n_polls=st.integers(min_value=1, max_value=3),
)
def test_case_2_reason_survives_n_polls(diag_env, fragment, n_polls):
    # 'boom-<fragment>' can never be a substring of the self-inflicted
    # ValidationException text, so the assertion is discriminating.
    originating = f"boom-{fragment}"
    training_id, _service = start_onnx_failure(diag_env, originating)

    for _ in range(n_polls):
        poll_status(diag_env, training_id)

    jobs, _ = stored_record(diag_env, training_id)
    reason = entry_reason(onnx_entry(jobs)) or ""
    assert originating in reason, (
        f"After {n_polls} poll(s) the originating error {originating!r} is "
        f"gone; the record now carries: {reason!r}")


# ---------------------------------------------------------------------------
# Case 3 — No-live-job entry is never described (isBugCondition_1)
# Validates: Requirements 1.1, 1.2, 1.3, 1.4
# ---------------------------------------------------------------------------

def test_case_3_no_live_job_entry_is_never_described(diag_env):
    training_id, service = start_onnx_failure(
        diag_env, "simulated ONNX export start failure")

    poll_status(diag_env, training_id)

    # ZERO describe calls of either kind for the ONNX failure entry. On
    # unfixed code: one doomed describe_compilation_job for the sentinel.
    assert service.calls(api="describe_compilation_job") == [], (
        f"The no-live-job ONNX failure entry was described with the Neo "
        f"API: {service.calls(api='describe_compilation_job')}")
    assert service.calls(api="describe_training_job") == [], (
        f"The no-live-job ONNX failure entry was described with the "
        f"training API: {service.calls(api='describe_training_job')}")


# ---------------------------------------------------------------------------
# Case 4 — Terminal status not overwritten
# Validates: Requirements 1.5, 1.8
# ---------------------------------------------------------------------------

def test_case_4_terminal_status_not_overwritten(diag_env):
    training_id, _service = start_onnx_failure(
        diag_env, "simulated ONNX export start failure")

    jobs, _ = stored_record(diag_env, training_id)
    assert onnx_entry(jobs)["status"] == "Failed"

    poll_status(diag_env, training_id)

    jobs, _ = stored_record(diag_env, training_id)
    status = onnx_entry(jobs)["status"]
    assert status == "Failed", (
        f"The entry's terminal 'Failed' status was overwritten by the poll "
        f"to {status!r}")


# ---------------------------------------------------------------------------
# Case 5 — Transient fault on a healthy Neo job
# Validates: Requirements 1.8, 1.15
# ---------------------------------------------------------------------------

def test_case_5_transient_fault_on_healthy_neo_job(diag_env):
    service = fresh_service()
    service.describe_compilation_error = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "DescribeCompilationJob")

    prior_reason = "prior transient describe fault (recorded earlier)"
    neo_name = f"{MODEL_NAME}-jetsonjp6-20240101000000"
    training_id = seed_training_record(diag_env, compilation_jobs=[{
        "target": "jetson-xavier-jp6",
        "compilation_job_name": neo_name,
        "status": "INPROGRESS",
        "failure_reason": prior_reason,
    }], compilation_status="InProgress")

    poll_status(diag_env, training_id)

    jobs, overall = stored_record(diag_env, training_id)
    entry = jobs[0]

    # The recorded status must not be a terminal failure.
    assert str(entry.get("status", "")).upper() not in \
        {"FAILED", "STOPPING", "STOPPED"}, (
        f"Transient describe fault latched the entry to a terminal status: "
        f"{entry.get('status')!r}")
    # The pre-existing failure_reason is intact.
    assert entry.get("failure_reason") == prior_reason, (
        f"Transient describe fault clobbered the pre-existing "
        f"failure_reason: {entry.get('failure_reason')!r}")
    # The derivation must not turn one transient poll fault on a healthy
    # job into an overall 'Failed'. On unfixed code, status was clobbered
    # to 'ERROR' and the silent catch-all latches 'Failed'.
    derived = diag_env.compilation.derive_compilation_status(jobs)
    assert derived != "Failed", (
        f"derive_compilation_status latched 'Failed' from a transient poll "
        f"fault (entry status {entry.get('status')!r})")
    assert overall != "Failed", (
        f"The record's stored compilation_status was latched to 'Failed' "
        f"by a transient poll fault")


# ---------------------------------------------------------------------------
# Case 6 — Derivation totality (isBugCondition_3)
# Validates: Requirements 1.10, 1.11, 1.15
# ---------------------------------------------------------------------------

# Every per-job status value the pollers can write: SageMaker's uppercase
# vocabulary, plus the portal-synthesized 'ERROR' poll-fault value.
EMITTABLE_STATUSES = ["STARTING", "INPROGRESS", "IN_PROGRESS", "COMPLETED",
                      "FAILED", "STOPPING", "STOPPED", "ERROR"]
GENUINE_FAILURES = {"FAILED", "STOPPING", "STOPPED"}


@given(statuses=st.sets(st.sampled_from(EMITTABLE_STATUSES), min_size=1))
def test_case_6_derivation_totality(diag_env, statuses):
    jobs = [{"target": f"t{i}", "status": s}
            for i, s in enumerate(sorted(statuses))]
    result = diag_env.compilation.derive_compilation_status(jobs)

    # The documented codomain (docstring: 'InProgress'|'Completed'|'Failed').
    assert result in {"InProgress", "Completed", "Failed"}, (
        f"derive_compilation_status left its documented codomain: {result!r}")

    # A transient poll fault with no genuine failure present must not be
    # collapsed to 'Failed'. On unfixed code the silent catch-all yields
    # derive_compilation_status([{'status': 'ERROR'}]) == 'Failed'.
    if "ERROR" in statuses and not (statuses & GENUINE_FAILURES):
        assert result != "Failed", (
            f"Silent catch-all collapsed the transient 'ERROR' value to "
            f"'Failed' for status set {sorted(statuses)}")


# ---------------------------------------------------------------------------
# Case 7 — Round-trip: every entry the system writes is pollable
# Validates: Requirements 1.1, 1.2, 1.3, 1.5
# ---------------------------------------------------------------------------

def test_case_7_round_trip_every_written_entry_is_pollable(diag_env):
    service = fresh_service()

    # Record A: successful Neo start + successful ONNX export start.
    tid_ok = seed_training_record(diag_env)
    start_compile(diag_env, tid_ok, ["jetson-xavier-jp6", "onnx"])

    # Record B: ONNX export start failure.
    service.create_training_error = ClientError(
        {"Error": {"Code": "AccessDeniedException",
                   "Message": "simulated start failure"}},
        "CreateTrainingJob")
    tid_failed = seed_training_record(diag_env)
    start_compile(diag_env, tid_failed, ["onnx"])
    service.create_training_error = None

    # A subsequent poll classifies every written entry and raises nothing —
    # including re-polling the entries the first poll wrote back.
    for training_id in (tid_ok, tid_failed):
        poll_status(diag_env, training_id)
        poll_status(diag_env, training_id)

    # On unfixed code the sentinel entry raises inside the describe branch.
    assert service.failed_calls() == [], (
        f"Polling entries the system itself wrote produced failing "
        f"describe calls: {service.failed_calls()}")
    for training_id in (tid_ok, tid_failed):
        jobs, _ = stored_record(diag_env, training_id)
        for job in jobs:
            assert str(job.get("status", "")).upper() != "ERROR", (
                f"Round-trip left entry {job.get('target')} of record "
                f"{training_id} latched to 'ERROR'")


# ---------------------------------------------------------------------------
# Case 8 — Poller B uses the Neo API for an ONNX job (isBugCondition_5)
# Validates: Requirements 1.12, 1.13, 1.20, 1.21
# ---------------------------------------------------------------------------

def test_case_8_poller_b_uses_training_api_for_onnx_job(diag_env):
    service = fresh_service()
    job_name = f"{MODEL_NAME}-onnx-20240101000000"
    service.training_jobs[job_name] = {
        "TrainingJobName": job_name,
        "TrainingJobStatus": "Completed",
        "ModelArtifacts": {
            "S3ModelArtifacts":
                f"s3://{USECASE_BUCKET}/onnx-out/{job_name}/model.tar.gz",
        },
    }
    training_id = seed_training_record(diag_env, compilation_jobs=[{
        "target": "onnx",
        "compilation_job_name": job_name,
        "status": "InProgress",
        "export_format": "onnx",
    }], compilation_status="InProgress")

    get_model(diag_env, training_id)

    # The ONNX export entry must be polled with the *training* API. On
    # unfixed code poller B calls describe_compilation_job unconditionally,
    # which fails and is warned away — the status never advances.
    training_calls = service.calls(api="describe_training_job")
    assert [c[1] for c in training_calls] == [job_name], (
        f"models.get_model did not poll the ONNX export job with "
        f"describe_training_job; describe calls: {service.describe_calls}")
    assert service.calls(api="describe_compilation_job") == [], (
        f"models.get_model polled the ONNX export training job with the "
        f"Neo API: {service.calls(api='describe_compilation_job')}")

    jobs, _ = stored_record(diag_env, training_id)
    status = str(onnx_entry(jobs).get("status", "")).upper()
    assert status != "INPROGRESS", (
        "The ONNX export job's status never advanced from the model detail "
        "page (poller B could not describe it)")


# ---------------------------------------------------------------------------
# Case 9 — Non-goal guard: documents F(X), MUST PASS unfixed AND fixed.
# Do NOT invert this case when implementing the fix.
# Validates: Requirements (non-goal guard; bugfix.md Non-goals, 3.3)
# ---------------------------------------------------------------------------

def test_case_9_non_goal_guard_no_jp7_compile_target(diag_env):
    targets = diag_env.compilation.COMPILATION_TARGETS
    assert "jetson-xavier-jp7" not in targets, (
        "A jetson-xavier-jp7 compile target must NOT exist (non-goal)")
    assert len(targets) == 7, (
        f"Exactly seven compile targets must be defined, found "
        f"{sorted(targets)}")
