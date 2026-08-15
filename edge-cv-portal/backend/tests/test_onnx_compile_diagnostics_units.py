"""
Unit tests for onnx-compile-error-diagnostics (task 5.1, design Testing
Strategy "Unit Tests").

Covers, with specific examples rather than generated inputs (the property
suites in test_onnx_compile_diagnostics_properties.py cover the generated
spaces):

- `classify_poll_kind` totality over every entry shape the system writes,
  each mapping to exactly one kind
- `normalize_status` / `is_terminal_status` / `is_transient_status`:
  mixed case, None, empty string, unknown values
- `entry_reason`: `failure_reason` precedence over `error`; None when
  neither is present
- `derive_compilation_status`: [] -> None; all COMPLETED -> Completed;
  any running -> InProgress; genuine FAILED/STOPPED -> Failed;
  transient-only -> InProgress; {FAILED, ERROR} -> Failed; an unmodeled
  value logs a warning and returns a non-latching value
- The ONNX except branch writes no `compilation_job_name` and does write
  `job_started: False`, `export_format: 'onnx'`, `error`, `failed_step`
- The audit event's job-name list tolerates an entry with no
  `compilation_job_name`
- Poller A's `ClientError` handler: `error` / `failure_reason` untouched;
  the three poll-diagnostic fields set; a terminal status not overwritten;
  a successful poll clearing the fault fields; `POLL_ERROR_MAX_ATTEMPTS`
  promoting to a terminal `FAILED` with `failure_reason` set only by
  `setdefault`
- `models.py::get_model`: `jobs_to_sync` excludes terminal and
  `POLL_KIND_NONE` entries; per-kind dispatch; the shared derivation is
  called; warn-and-continue preserved

Fixture conventions follow test_onnx_compile_diagnostics_properties.py
(module-scoped env on the moto-backed aws_stack, FakeSageMakerService
recording stub, own table and bucket so the suites stay independent).

# Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.6, 2.7, 2.8, 2.9, 2.10,
# 2.11, 2.12, 2.18, 2.19
"""
import importlib.util
import io
import json
import logging
import os
import sys
import tarfile
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from conftest import REGION

# Pure module — safe to import outside the moto mock (no boto3, no I/O).
# conftest puts the shared layer on sys.path.
import compilation_status as cs

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-onnx-units"
USECASE_BUCKET = "test-onnx-units-bucket"
MODEL_NAME = "unitsmodel"

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
# SageMaker service stub (properties-suite pattern)
# ---------------------------------------------------------------------------

class FakeSageMakerService:
    """Behaves like the SageMaker service: raises ValidationException for
    any name it was not seeded with, records every create/describe call."""

    def __init__(self):
        self.create_training_error = None       # ClientError to raise
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
def units_env(aws_stack):
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

    compilation = _load_module("compilation.py", "portal_compilation_units")
    models = _load_module("models.py", "portal_models_units")

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
        "name": "ONNX Units Use Case",
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
        audit_log=aws_stack.tables.audit_log,
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


# ===========================================================================
# Pure-function unit tests (compilation_status.py — no moto needed)
# ===========================================================================

class TestClassifyPollKind:
    """classify_poll_kind is total over every entry shape the system
    writes, each mapping to exactly one kind.
    # Validates: Requirements 2.1, 2.2, 2.3, 2.18
    """

    ALL_KINDS = {cs.POLL_KIND_NONE, cs.POLL_KIND_TRAINING,
                 cs.POLL_KIND_COMPILATION}

    # (entry the system writes, expected kind)
    CASES = [
        # Neo success entry (start_compilation_job)
        ({"target": "jetson-xavier-jp6",
          "compilation_job_name": "m-jetsonjp6-20240101000000",
          "compilation_job_arn": "arn:...", "status": "InProgress"},
         cs.POLL_KIND_COMPILATION),
        # Neo entry after a poll (uppercase status, artifacts)
        ({"target": "jetson-xavier-jp6",
          "compilation_job_name": "m-jetsonjp6-20240101000000",
          "status": "COMPLETED", "compiled_model_s3": "s3://b/k"},
         cs.POLL_KIND_COMPILATION),
        # ONNX success entry (live training job)
        ({"target": "onnx", "export_format": "onnx",
          "compilation_job_name": "m-onnx-20240101000000",
          "status": "InProgress"},
         cs.POLL_KIND_TRAINING),
        # ONNX start-failure entry (the fixed except branch: no name)
        ({"target": "onnx", "export_format": "onnx", "status": "Failed",
          "job_started": False, "error": "boom",
          "failed_step": "start_onnx_export_job"},
         cs.POLL_KIND_NONE),
        # Absent compilation_job_name (no marker at all)
        ({"target": "onnx", "status": "Failed"}, cs.POLL_KIND_NONE),
        # Empty-string name is falsy -> none
        ({"target": "onnx", "compilation_job_name": "", "status": "Failed"},
         cs.POLL_KIND_NONE),
        # Absent export_format with a name present -> Neo
        ({"target": "x86_64-cpu", "compilation_job_name": "m-x86cpu-1",
          "status": "INPROGRESS"},
         cs.POLL_KIND_COMPILATION),
        # job_started False dominates even with a name present
        ({"target": "onnx", "compilation_job_name": "phantom-name",
          "export_format": "onnx", "job_started": False, "status": "Failed"},
         cs.POLL_KIND_NONE),
        # Empty entry never raises
        ({}, cs.POLL_KIND_NONE),
    ]

    def test_every_written_shape_maps_to_exactly_one_kind(self):
        for entry, expected in self.CASES:
            kind = cs.classify_poll_kind(entry)
            assert kind == expected, entry
            assert kind in self.ALL_KINDS

    def test_job_started_true_does_not_mask_the_format_markers(self):
        entry = {"compilation_job_name": "n", "export_format": "onnx",
                 "job_started": True}
        assert cs.classify_poll_kind(entry) == cs.POLL_KIND_TRAINING


class TestStatusPredicates:
    """normalize_status / is_terminal_status / is_transient_status over
    mixed case, None, empty string, and unknown values.
    # Validates: Requirements 2.6, 2.7, 2.12
    """

    def test_normalize_status(self):
        assert cs.normalize_status("Failed") == "FAILED"
        assert cs.normalize_status("inprogress") == "INPROGRESS"
        assert cs.normalize_status("COMPLETED") == "COMPLETED"
        assert cs.normalize_status(None) == ""
        assert cs.normalize_status("") == ""
        assert cs.normalize_status("banana") == "BANANA"

    def test_is_terminal_status(self):
        for value in ("COMPLETED", "Completed", "FAILED", "failed",
                      "STOPPING", "Stopped", "STOPPED"):
            assert cs.is_terminal_status(value), value
        for value in ("INPROGRESS", "InProgress", "IN_PROGRESS", "STARTING",
                      "ERROR", "error", None, "", "BANANA"):
            assert not cs.is_terminal_status(value), value

    def test_is_transient_status(self):
        assert cs.is_transient_status("ERROR")
        assert cs.is_transient_status("error")
        for value in ("FAILED", "COMPLETED", "INPROGRESS", None, "",
                      "BANANA"):
            assert not cs.is_transient_status(value), value


class TestEntryReason:
    """entry_reason: failure_reason precedence over error; None when
    neither is present.
    # Validates: Requirements 2.4
    """

    def test_failure_reason_takes_precedence_over_error(self):
        entry = {"failure_reason": "from-describe", "error": "from-start"}
        assert cs.entry_reason(entry) == "from-describe"

    def test_error_is_the_fallback(self):
        assert cs.entry_reason({"error": "from-start"}) == "from-start"

    def test_none_when_neither_present(self):
        assert cs.entry_reason({}) is None
        assert cs.entry_reason({"status": "Failed"}) is None

    def test_poll_error_is_not_a_reason(self):
        # Poll faults are Poll_Diagnostics, not the Originating_Reason.
        assert cs.entry_reason({"poll_error": "throttled"}) is None


class TestDeriveCompilationStatus:
    """derive_compilation_status over the documented rule table.
    # Validates: Requirements 2.8, 2.9, 2.10
    """

    @staticmethod
    def derive(statuses):
        return cs.derive_compilation_status(
            [{"status": s} for s in statuses])

    def test_empty_list_returns_none(self):
        assert cs.derive_compilation_status([]) is None

    def test_all_completed_is_completed(self):
        assert self.derive(["COMPLETED", "Completed"]) == "Completed"

    def test_any_running_is_inprogress(self):
        assert self.derive(["STARTING"]) == "InProgress"
        assert self.derive(["FAILED", "INPROGRESS"]) == "InProgress"
        assert self.derive(["COMPLETED", "In_Progress"]) == "InProgress"

    def test_genuine_failure_is_failed(self):
        assert self.derive(["FAILED"]) == "Failed"
        assert self.derive(["STOPPED", "COMPLETED"]) == "Failed"
        assert self.derive(["Failed", "Completed"]) == "Failed"

    def test_transient_only_is_inprogress_never_latched(self):
        assert self.derive(["ERROR"]) == "InProgress"
        assert self.derive(["ERROR", "COMPLETED"]) == "InProgress"

    def test_genuine_failure_dominates_a_transient_fault(self):
        assert self.derive(["FAILED", "ERROR"]) == "Failed"

    def test_unmodeled_value_warns_and_returns_non_latching(self, caplog):
        with caplog.at_level(logging.WARNING, logger="compilation_status"):
            result = self.derive(["BANANA"])
        assert result == "InProgress"          # non-latching, never 'Failed'
        warnings = [r for r in caplog.records
                    if "unmodeled status value" in r.getMessage()]
        assert warnings, "expected a warning naming the unmodeled value"
        assert "BANANA" in warnings[0].getMessage()


# ===========================================================================
# Handler-level unit tests (moto env)
# ===========================================================================

def _client_error(code, message, operation="CreateTrainingJob"):
    return ClientError({"Error": {"Code": code, "Message": message}},
                       operation)


class TestOnnxExceptBranch:
    """The ONNX except branch writes no compilation_job_name and does write
    job_started: False, export_format: 'onnx', error, failed_step.
    # Validates: Requirements 2.1, 2.3
    """

    def test_entry_shape_on_start_failure(self, units_env):
        service = fresh_service()
        message = f"role denied {uuid.uuid4().hex[:8]}"
        service.create_training_error = _client_error(
            "AccessDeniedException", message)
        training_id = seed_training_record(units_env)

        body = start_compile(units_env, training_id, ["onnx"])

        (entry,) = body["compilation_jobs"]
        assert "compilation_job_name" not in entry
        assert entry["job_started"] is False
        assert entry["export_format"] == "onnx"
        assert entry["target"] == "onnx"
        assert entry["status"] == "Failed"
        assert message in entry["error"]
        assert entry["failed_step"] == "start_onnx_export_job"

        # The stored record carries the identical shape.
        jobs, overall = stored_record(units_env, training_id)
        (stored,) = jobs
        assert "compilation_job_name" not in stored
        assert stored["job_started"] is False
        assert stored["export_format"] == "onnx"
        assert message in stored["error"]
        assert stored["failed_step"] == "start_onnx_export_job"
        assert overall == "Failed"


class TestAuditEventJobNames:
    """The start_compilation audit event's job-name list tolerates an
    entry with no compilation_job_name.
    # Validates: Requirements 2.1
    """

    def test_audit_event_written_with_placeholder_name(self, units_env):
        service = fresh_service()
        service.create_training_error = _client_error(
            "AccessDeniedException", "no role")
        training_id = seed_training_record(units_env)

        start_compile(units_env, training_id, ["onnx"])

        events = [
            item for item in units_env.audit_log.scan()["Items"]
            if item.get("action") == "start_compilation"
            and item.get("resource_id") == training_id
        ]
        assert len(events) == 1, "audit event must still be logged"
        details = events[0]["details"]
        assert details["compilation_jobs"] == ["onnx:not-started"]
        assert details["targets"] == ["onnx"]


class TestPollerAClientErrorHandler:
    """Poller A's ClientError handler is additive: error / failure_reason
    untouched, the three poll-diagnostic fields set, a terminal status not
    overwritten, a successful poll clearing the fault fields, and
    POLL_ERROR_MAX_ATTEMPTS promoting to a terminal FAILED with
    failure_reason set only by setdefault.
    # Validates: Requirements 2.4, 2.6, 2.7
    """

    def _seed(self, env, entry):
        return seed_training_record(
            env, compilation_jobs=[entry], compilation_status="InProgress")

    def test_fault_is_additive_and_reasons_untouched(self, units_env):
        fresh_service()   # unknown name -> describe raises ValidationException
        entry = {
            "target": "jetson-xavier-jp6",
            "compilation_job_name": f"units-unknown-{uuid.uuid4().hex[:8]}",
            "status": "INPROGRESS",
            "error": "orig-error",
            "failure_reason": "orig-reason",
        }
        training_id = self._seed(units_env, entry)

        poll_status(units_env, training_id)

        jobs, _ = stored_record(units_env, training_id)
        (job,) = jobs
        assert job["error"] == "orig-error"                 # untouched
        assert job["failure_reason"] == "orig-reason"       # untouched
        assert "ValidationException" in job["poll_error"]
        assert int(job["poll_error_count"]) == 1
        assert int(job["poll_error_at"]) > 0
        assert job["status"] == cs.STATUS_POLL_ERROR        # 'ERROR'

    def test_terminal_status_is_not_overwritten(self, units_env):
        fresh_service()
        entry = {
            "target": "jetson-xavier-jp6",
            "compilation_job_name": f"units-unknown-{uuid.uuid4().hex[:8]}",
            "status": "COMPLETED",
            "compiled_model_s3": "s3://b/compiled.tar.gz",
        }
        training_id = self._seed(units_env, entry)

        poll_status(units_env, training_id)

        jobs, _ = stored_record(units_env, training_id)
        (job,) = jobs
        assert job["status"] == "COMPLETED"                 # never overwritten
        assert "poll_error" in job                          # diagnostic added

    def test_successful_poll_clears_the_fault_fields(self, units_env):
        service = fresh_service()
        name = f"units-healthy-{uuid.uuid4().hex[:8]}"
        service.compilation_jobs[name] = {
            "CompilationJobName": name,
            "CompilationJobStatus": "INPROGRESS",
        }
        entry = {
            "target": "jetson-xavier-jp6",
            "compilation_job_name": name,
            "status": "ERROR",
            "poll_error": "old fault",
            "poll_error_at": 1_700_000_000_000,
            "poll_error_count": 3,
        }
        training_id = self._seed(units_env, entry)

        poll_status(units_env, training_id)

        jobs, _ = stored_record(units_env, training_id)
        (job,) = jobs
        assert job["status"] == "INPROGRESS"                # true status back
        assert "poll_error" not in job
        assert "poll_error_count" not in job

    def test_max_attempts_promotes_to_terminal_failed(self, units_env):
        fresh_service()
        entry = {
            "target": "jetson-xavier-jp6",
            "compilation_job_name": f"units-unknown-{uuid.uuid4().hex[:8]}",
            "status": "INPROGRESS",
            "poll_error_count": cs.POLL_ERROR_MAX_ATTEMPTS - 1,
        }
        training_id = self._seed(units_env, entry)

        poll_status(units_env, training_id)

        jobs, overall = stored_record(units_env, training_id)
        (job,) = jobs
        assert int(job["poll_error_count"]) == cs.POLL_ERROR_MAX_ATTEMPTS
        assert job["status"] == "FAILED"                    # genuinely terminal
        assert job["failure_reason"].startswith(
            f"status could not be retrieved after "
            f"{cs.POLL_ERROR_MAX_ATTEMPTS} attempts")
        assert overall == "Failed"

    def test_max_attempts_setdefault_keeps_an_existing_reason(self, units_env):
        fresh_service()
        entry = {
            "target": "jetson-xavier-jp6",
            "compilation_job_name": f"units-unknown-{uuid.uuid4().hex[:8]}",
            "status": "INPROGRESS",
            "failure_reason": "originating-reason",
            "poll_error_count": cs.POLL_ERROR_MAX_ATTEMPTS - 1,
        }
        training_id = self._seed(units_env, entry)

        poll_status(units_env, training_id)

        jobs, _ = stored_record(units_env, training_id)
        (job,) = jobs
        assert job["status"] == "FAILED"
        # setdefault: the Originating_Reason is never displaced.
        assert job["failure_reason"] == "originating-reason"


class TestPollerBGetModel:
    """models.py::get_model: jobs_to_sync excludes terminal and
    POLL_KIND_NONE entries; per-kind dispatch; the shared derivation is
    called; warn-and-continue preserved.
    # Validates: Requirements 2.11, 2.12, 2.18, 2.19
    """

    def test_jobs_to_sync_excludes_terminal_and_none(self, units_env):
        service = fresh_service()
        training_id = seed_training_record(units_env, compilation_jobs=[
            {"target": "jetson-xavier-jp6",
             "compilation_job_name": "units-done-1", "status": "COMPLETED",
             "compiled_model_s3": "s3://b/k"},
            {"target": "x86_64-cpu",
             "compilation_job_name": "units-done-2", "status": "FAILED",
             "failure_reason": "compile broke"},
            {"target": "arm64-cpu",
             "compilation_job_name": "units-done-3", "status": "STOPPED"},
            # POLL_KIND_NONE: the ONNX start-failure entry
            {"target": "onnx", "export_format": "onnx", "status": "Failed",
             "job_started": False, "error": "boom",
             "failed_step": "start_onnx_export_job"},
        ], compilation_status="Failed")

        get_model(units_env, training_id)

        # Terminal + no-live-job entries are never described.
        assert service.describe_calls == []

    def test_per_kind_dispatch(self, units_env):
        service = fresh_service()
        onnx_name = f"units-onnx-{uuid.uuid4().hex[:8]}"
        neo_name = f"units-neo-{uuid.uuid4().hex[:8]}"
        service.training_jobs[onnx_name] = {
            "TrainingJobName": onnx_name,
            "TrainingJobStatus": "InProgress",
        }
        service.compilation_jobs[neo_name] = {
            "CompilationJobName": neo_name,
            "CompilationJobStatus": "INPROGRESS",
        }
        training_id = seed_training_record(units_env, compilation_jobs=[
            {"target": "onnx", "export_format": "onnx",
             "compilation_job_name": onnx_name, "status": "InProgress"},
            {"target": "jetson-xavier-jp6",
             "compilation_job_name": neo_name, "status": "INPROGRESS"},
        ], compilation_status="InProgress")

        get_model(units_env, training_id)

        # The ONNX export entry goes to the training API, the Neo entry to
        # the compilation API — and to nothing else.
        assert service.calls(api="describe_training_job") == [
            ("describe_training_job", onnx_name, "ok")]
        assert service.calls(api="describe_compilation_job") == [
            ("describe_compilation_job", neo_name, "ok")]

    def test_shared_derivation_is_used(self, units_env):
        # One shared implementation (2.11): models.py binds the very same
        # function object the shared layer module defines.
        assert units_env.models.derive_compilation_status \
            is cs.derive_compilation_status
        assert units_env.compilation.derive_compilation_status \
            is cs.derive_compilation_status
        # And no inline re-implementation remains in models.py.
        source_path = os.path.join(_FUNCTIONS_DIR, "models.py")
        with open(source_path) as f:
            source = f.read()
        assert "def derive_compilation_status" not in source

    def test_warn_and_continue_preserved(self, units_env):
        service = fresh_service()
        entry = {
            "target": "jetson-xavier-jp6",
            "compilation_job_name": f"units-unknown-{uuid.uuid4().hex[:8]}",
            "status": "INPROGRESS",
            "error": "orig-error",
        }
        training_id = seed_training_record(
            units_env, compilation_jobs=[entry],
            compilation_status="InProgress")

        body = get_model(units_env, training_id)   # asserts 200 inside

        # The describe failed (unknown name) but was warned away: nothing
        # on the record is mutated — reason intact, no poll fields, status
        # unchanged.
        assert len(service.describe_calls) == 1
        jobs, overall = stored_record(units_env, training_id)
        (job,) = jobs
        assert job["status"] == "INPROGRESS"
        assert job["error"] == "orig-error"
        assert "poll_error" not in job
        assert overall == "InProgress"
        assert body["model"]["compilation_jobs"][0]["error"] == "orig-error"
