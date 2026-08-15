"""
Backend integration tests for onnx-compile-error-diagnostics (task 5.2,
design Testing Strategy "Integration Tests").

End-to-end flows on the moto-backed stack with the real compilation.py /
models.py handlers:

1. `start_compilation_job` with `targets=['onnx']` and a raising
   `create_training_job`, then THREE successive `get_compilation_status`
   calls — the originating reason is byte-identical after each, zero
   describe calls are issued, and `compilation_status` is `Failed`
   throughout.
2. Mixed request `targets=['jetson-xavier-jp6', 'onnx']` where the Neo
   target starts and the ONNX target fails: the Neo entry polls normally
   through `describe_compilation_job`, the ONNX entry is skipped, and the
   overall status follows the Neo job.
3. Transient-recovery flow: a Neo job whose describe raises
   `ThrottlingException` on polls 1-2 and succeeds on poll 3 — never
   latched to `Failed`, `poll_error` cleared on success, the true status
   recorded.
4. Poller B: a live ONNX export entry advances from `InProgress` to
   `Completed` through `models.get_model` with `compiled_model_s3` set.

Fixture conventions follow test_onnx_compile_diagnostics_properties.py
(module-scoped env on the moto-backed aws_stack, FakeSageMakerService
recording stub, own table and bucket so the suites stay independent).

# Validates: Requirements 2.5, 2.7, 2.10, 2.18, 3.7
"""
import importlib.util
import io
import json
import os
import sys
import tarfile
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-onnx-integ"
USECASE_BUCKET = "test-onnx-integ-bucket"
MODEL_NAME = "integmodel"

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
# SageMaker service stub (exploration-suite pattern, with a forced
# describe fault for the transient-recovery flow)
# ---------------------------------------------------------------------------

class FakeSageMakerService:
    """Behaves like the SageMaker service: raises ValidationException for
    any name it was not seeded with, records every create/describe call,
    and can be forced to raise a chosen fault (e.g. ThrottlingException)
    from describe_compilation_job."""

    def __init__(self):
        self.create_training_error = None       # ClientError to raise
        self.describe_compilation_error = None  # forced fault (throttling)
        self.training_jobs = {}                 # name -> describe response
        self.compilation_jobs = {}              # name -> describe response
        self.describe_calls = []                # (api, name, 'ok'|'raised')
        self.create_training_calls = []
        self.create_compilation_calls = []

    def calls(self, api=None, name=None):
        return [c for c in self.describe_calls
                if (api is None or c[0] == api)
                and (name is None or c[1] == name)]

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
        self.create_compilation_calls.append(kwargs)
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
def integ_env(aws_stack):
    """Own training-jobs table (production key shape) + use-case bucket +
    compilation.py / models.py loaded inside the mock, with the SageMaker
    use-case client routed to the recording stub."""
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

    compilation = _load_module("compilation.py", "portal_compilation_integ")
    models = _load_module("models.py", "portal_models_integ")

    def _dispatch_get_usecase_client(service_name, usecase,
                                     session_name=None, region=None):
        if service_name == "sagemaker":
            return _ServiceHolder.current
        return boto3.client(service_name, region_name=region or REGION)

    compilation.get_usecase_client = _dispatch_get_usecase_client
    models.get_usecase_client = _dispatch_get_usecase_client

    usecase_id = f"uc-{uuid.uuid4()}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "ONNX Integration Use Case",
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


def _client_error(code, message, operation="CreateTrainingJob"):
    return ClientError({"Error": {"Code": code, "Message": message}},
                       operation)


def _onnx_entry(jobs):
    (entry,) = [j for j in jobs if j.get("target") == "onnx"]
    return entry


# ---------------------------------------------------------------------------
# 1. End-to-end: ONNX start failure survives three polls untouched
# Validates: Requirements 2.5, 3.7
# ---------------------------------------------------------------------------

def test_onnx_start_failure_end_to_end_three_polls(integ_env):
    """start_compilation_job with targets=['onnx'] and a raising
    create_training_job, then THREE successive get_compilation_status
    calls: the originating reason is byte-identical after each, zero
    describe calls are issued, and compilation_status is Failed
    throughout.
    # Validates: Requirements 2.5, 3.7
    """
    service = fresh_service()
    message = f"could not assume DDASageMakerExecutionRole {uuid.uuid4().hex}"
    service.create_training_error = _client_error(
        "AccessDeniedException", message)
    training_id = seed_training_record(integ_env)

    body = start_compile(integ_env, training_id, ["onnx"])
    original_error = _onnx_entry(body["compilation_jobs"])["error"]
    assert message in original_error

    jobs, overall = stored_record(integ_env, training_id)
    assert _onnx_entry(jobs)["error"] == original_error
    assert overall == "Failed"

    for n in range(1, 4):
        poll_body = poll_status(integ_env, training_id)

        # The originating reason is byte-identical after poll N.
        entry = _onnx_entry(poll_body["compilation_jobs"])
        assert entry["error"] == original_error, f"poll {n} changed the reason"
        assert "failure_reason" not in entry
        assert entry["status"] == "Failed"
        assert "compilation_job_name" not in entry

        stored_jobs, stored_overall = stored_record(integ_env, training_id)
        assert _onnx_entry(stored_jobs)["error"] == original_error
        assert stored_overall == "Failed"
        assert poll_body["compilation_status"] == "Failed"

        # Zero describe calls, cumulatively, across all polls.
        assert service.describe_calls == [], f"poll {n} issued a describe"


# ---------------------------------------------------------------------------
# 2. Mixed request: the Neo entry polls normally, the ONNX entry is skipped
# Validates: Requirements 2.10, 2.18, 3.7
# ---------------------------------------------------------------------------

def test_mixed_targets_neo_polls_normally_onnx_skipped(integ_env):
    """targets=['jetson-xavier-jp6', 'onnx'] where the Neo target starts
    and the ONNX target fails: the Neo entry polls normally through
    describe_compilation_job, the ONNX entry is skipped, and the overall
    status follows the Neo job.
    # Validates: Requirements 2.10, 2.18, 3.7
    """
    service = fresh_service()
    message = f"onnx start denied {uuid.uuid4().hex[:8]}"
    service.create_training_error = _client_error(
        "AccessDeniedException", message)
    training_id = seed_training_record(integ_env)

    # One target's start failure never aborts the other (3.7): the request
    # is still 200 with both entries.
    body = start_compile(integ_env, training_id, ["jetson-xavier-jp6", "onnx"])
    assert len(body["compilation_jobs"]) == 2

    (neo_entry,) = [j for j in body["compilation_jobs"]
                    if j["target"] == "jetson-xavier-jp6"]
    neo_name = neo_entry["compilation_job_name"]
    onnx_error = _onnx_entry(body["compilation_jobs"])["error"]
    assert message in onnx_error

    # Poll 1: the Neo job is running -> the overall status follows it.
    poll_body = poll_status(integ_env, training_id)
    assert service.calls(api="describe_compilation_job") == [
        ("describe_compilation_job", neo_name, "ok")]
    assert service.calls(api="describe_training_job") == []

    (neo_polled,) = [j for j in poll_body["compilation_jobs"]
                     if j["target"] == "jetson-xavier-jp6"]
    assert neo_polled["status"] == "INPROGRESS"
    assert poll_body["compilation_status"] == "InProgress"
    # The skipped ONNX entry is byte-for-byte untouched.
    assert _onnx_entry(poll_body["compilation_jobs"])["error"] == onnx_error

    # Poll 2: the Neo job completes; its artifacts are recorded and the
    # ONNX entry is still skipped. With nothing left running, the recorded
    # ONNX start failure now dominates the shared derivation.
    artifact = f"s3://{USECASE_BUCKET}/compiled/{neo_name}/model.tar.gz"
    service.compilation_jobs[neo_name] = {
        "CompilationJobName": neo_name,
        "CompilationJobStatus": "COMPLETED",
        "ModelArtifacts": {"S3ModelArtifacts": artifact},
    }
    poll_body = poll_status(integ_env, training_id)

    (neo_polled,) = [j for j in poll_body["compilation_jobs"]
                     if j["target"] == "jetson-xavier-jp6"]
    assert neo_polled["status"] == "COMPLETED"
    assert neo_polled["compiled_model_s3"] == artifact
    assert _onnx_entry(poll_body["compilation_jobs"])["error"] == onnx_error
    assert poll_body["compilation_status"] == "Failed"

    # Across both polls the ONNX entry never triggered a describe: the
    # only calls are the two Neo ones.
    assert service.calls(api="describe_training_job") == []
    assert [c[1] for c in service.calls(api="describe_compilation_job")] == \
        [neo_name, neo_name]


# ---------------------------------------------------------------------------
# 3. Transient recovery: throttled twice, then the true status lands
# Validates: Requirements 2.7, 2.10
# ---------------------------------------------------------------------------

def test_transient_throttling_recovers_without_latching(integ_env):
    """A Neo job whose describe raises ThrottlingException on polls 1-2
    and succeeds on poll 3: never latched to Failed, poll_error cleared
    on success, the true status recorded.
    # Validates: Requirements 2.7, 2.10
    """
    service = fresh_service()
    name = f"integ-throttle-{uuid.uuid4().hex[:8]}"
    artifact = f"s3://{USECASE_BUCKET}/compiled/{name}/model.tar.gz"
    service.compilation_jobs[name] = {
        "CompilationJobName": name,
        "CompilationJobStatus": "COMPLETED",
        "ModelArtifacts": {"S3ModelArtifacts": artifact},
    }
    training_id = seed_training_record(integ_env, compilation_jobs=[{
        "target": "jetson-xavier-jp6",
        "compilation_job_name": name,
        "status": "INPROGRESS",
    }], compilation_status="InProgress")

    # Polls 1-2: describe raises ThrottlingException.
    service.describe_compilation_error = _client_error(
        "ThrottlingException", "Rate exceeded", "DescribeCompilationJob")

    for n in (1, 2):
        poll_body = poll_status(integ_env, training_id)
        (entry,) = poll_body["compilation_jobs"]
        assert entry["status"] == "ERROR", f"poll {n}"     # transient marker
        assert "ThrottlingException" in entry["poll_error"]
        assert int(entry["poll_error_count"]) == n
        # Never latched terminal, at the entry or the aggregate.
        assert entry["status"] != "FAILED"
        assert poll_body["compilation_status"] == "InProgress", f"poll {n}"

    # Poll 3: the fault clears and the true status lands.
    service.describe_compilation_error = None
    poll_body = poll_status(integ_env, training_id)

    (entry,) = poll_body["compilation_jobs"]
    assert entry["status"] == "COMPLETED"                  # true status
    assert entry["compiled_model_s3"] == artifact
    assert "poll_error" not in entry                       # cleared on success
    assert "poll_error_count" not in entry
    assert poll_body["compilation_status"] == "Completed"

    jobs, overall = stored_record(integ_env, training_id)
    assert jobs[0]["status"] == "COMPLETED"
    assert "poll_error" not in jobs[0]
    assert overall == "Completed"


# ---------------------------------------------------------------------------
# 4. Poller B advances a live ONNX export to Completed
# Validates: Requirements 2.18
# ---------------------------------------------------------------------------

def test_poller_b_advances_live_onnx_export_to_completed(integ_env):
    """A live ONNX export entry advances from InProgress to Completed
    through models.get_model, with compiled_model_s3 set.
    # Validates: Requirements 2.18
    """
    service = fresh_service()
    name = f"integ-onnx-{uuid.uuid4().hex[:8]}"
    artifact = f"s3://{USECASE_BUCKET}/models/compilation/{name}/onnx/model.onnx"
    service.training_jobs[name] = {
        "TrainingJobName": name,
        "TrainingJobStatus": "Completed",
        "ModelArtifacts": {"S3ModelArtifacts": artifact},
    }
    training_id = seed_training_record(integ_env, compilation_jobs=[{
        "target": "onnx",
        "export_format": "onnx",
        "compilation_job_name": name,
        "status": "InProgress",
    }], compilation_status="InProgress")

    body = get_model(integ_env, training_id)

    # The ONNX export is polled with the *training* API only (2.18).
    assert service.calls(api="describe_training_job") == [
        ("describe_training_job", name, "ok")]
    assert service.calls(api="describe_compilation_job") == []

    (entry,) = body["model"]["compilation_jobs"]
    assert entry["status"] == "Completed"
    assert entry["compiled_model_s3"] == artifact
    assert body["model"]["compilation_status"] == "Completed"

    # And the advance is persisted.
    jobs, overall = stored_record(integ_env, training_id)
    assert jobs[0]["status"] == "Completed"
    assert jobs[0]["compiled_model_s3"] == artifact
    assert overall == "Completed"
