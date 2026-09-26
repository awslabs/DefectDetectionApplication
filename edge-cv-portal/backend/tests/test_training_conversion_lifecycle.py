"""
Both status writers on a Conversion_Record (detector-checkpoint-import task
6.3): ``training_events.handle_training_state_change`` (EventBridge) and
``training.get_training_job`` (sync-on-read).

Real handler modules against the moto-backed conftest stack (a training-jobs
table with the production key shape). SageMaker's DescribeTrainingJob is a
settable stand-in, and the Lambda client both writers use for the finalize
invoke is a recorder bound into each module's namespace only.

Covered:
* InProgress -> Finalizing from either writer, with one finalize invoke even
  when the two race over stale reads;
* Failed and Stopped carry the FailureReason verbatim;
* terminal and Finalizing records are never moved by SageMaker;
* non-conversion records keep today's generic updates and auto-compile.
# Validates: Requirements 7.1-7.8
"""
import importlib.util
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import boto3
import pytest

import detector_conversion as dc
from conftest import REGION

TABLE = "test-training-jobs-dci-lifecycle"
_FUNCTIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "functions")
ARTIFACT = "s3://uc-bucket/models/conversion/job-a/job-a/output/model.tar.gz"
FATAL = ("AlgorithmError: FATAL: checkpoint has 4 classes; the import recorded 5, "
         "exit code: 1")


def _load(filename, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(_FUNCTIONS, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


class FakeLambda:
    def __init__(self):
        self.invokes = []

    def invoke(self, **kwargs):
        self.invokes.append(kwargs)
        return {"StatusCode": 202}


class FakeSageMaker:
    def __init__(self):
        self.jobs = {}

    def describe_training_job(self, TrainingJobName):
        return dict(self.jobs[TrainingJobName])


@pytest.fixture(scope="module")
def env(aws_stack):
    os.environ["TRAINING_JOBS_TABLE"] = TABLE
    os.environ["PACKAGING_FUNCTION_NAME"] = "test-packaging-fn"
    os.environ["COMPILATION_FUNCTION_NAME"] = "test-compilation-fn"
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "training_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")
    training = _load("training.py", "portal_training_dci_lifecycle")
    events = _load("training_events.py", "portal_training_events_dci_lifecycle")
    fake_lambda, fake_sm = FakeLambda(), FakeSageMaker()
    real_client = boto3.client

    def client(name, *args, **kwargs):
        return fake_lambda if name == "lambda" else real_client(name, *args, **kwargs)

    for module in (training, events):
        module.boto3 = SimpleNamespace(client=client, resource=boto3.resource)
    training.sagemaker = fake_sm
    usecase_id = f"uc-{uuid.uuid4()}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id, "name": "Conversion Lifecycle", "account_id": "123456789012",
        "s3_bucket": "uc-bucket", "region": REGION,
        "cross_account_role_arn": "arn:aws:iam::123456789012:root"})
    user_id = f"user-{uuid.uuid4()}"
    aws_stack.tables.user_roles.put_item(Item={"user_id": user_id, "usecase_id": usecase_id,
                                               "role": "DataScientist"})
    yield SimpleNamespace(training=training, events=events, fake_lambda=fake_lambda,
                          sm=fake_sm, table=boto3.resource("dynamodb", region_name=REGION).Table(TABLE),
                          usecase_id=usecase_id, user_id=user_id)


@pytest.fixture(autouse=True)
def reset(env):
    env.fake_lambda.invokes.clear()
    env.sm.jobs.clear()


def seed_conversion(env, conversion_status="InProgress", **extra):
    training_id = str(uuid.uuid4())
    job_name = f"ppe-detection-cnv-{uuid.uuid4().hex[:12]}"
    item = {
        "training_id": training_id, "usecase_id": env.usecase_id, "model_name": "ppe-detection",
        "model_type": "object_detection", "source": "imported", "runtime": "onnx",
        "status": "InProgress" if conversion_status in ("InProgress", "Finalizing") else conversion_status,
        "progress": dc.PROGRESS_FOR_STATUS[conversion_status],
        "training_job_name": job_name, "auto_compile": True,
        "compilation_targets": ["jetson-xavier-jp5"],
        "detection": {"detection_arch": "yolo", "network_input_width": 640, "num_classes": 4},
        "metadata": {"framework": "PYTORCH", "model_file": "checkpoint.pt"},
        "conversion": {"status": conversion_status, "job_name": job_name},
    }
    item.update(extra)
    env.table.put_item(Item=item)
    return item


def event_for(record, status, **detail):
    payload = {"TrainingJobName": record["training_job_name"], "TrainingJobStatus": status,
               "TrainingJobArn": f"arn:aws:sagemaker:{REGION}:123456789012:training-job/"
                                 f"{record['training_job_name']}"}
    payload.update(detail)
    return {"source": "aws.sagemaker", "detail-type": "SageMaker Training Job State Change",
            "detail": payload}


def eventbridge(env, record, status, **detail):
    response = env.events.handle_training_state_change(event_for(record, status, **detail), None)
    return response["statusCode"], json.loads(response["body"])


def describe(env, record, status, **fields):
    job = {"TrainingJobName": record["training_job_name"], "TrainingJobStatus": status}
    job.update(fields)
    env.sm.jobs[record["training_job_name"]] = job


def get(env, record):
    response = env.training.get_training_job({
        "httpMethod": "GET", "path": f"/api/v1/training/{record['training_id']}",
        "pathParameters": {"id": record["training_id"]},
        "requestContext": {"authorizer": {"claims": {
            "sub": env.user_id, "email": "ds@example.com", "cognito:username": "ds"}}},
    }, None)
    return response["statusCode"], json.loads(response["body"])


def stored(env, record):
    return env.table.get_item(Key={"training_id": record["training_id"]})["Item"]


def finalize_invokes(env):
    return [i for i in env.fake_lambda.invokes if i["FunctionName"] == "test-packaging-fn"]


# ---------------------------------------------------------------------------
# InProgress -> Finalizing
# ---------------------------------------------------------------------------

def test_eventbridge_completed_claims_finalizing_and_invokes_packaging_once(env):
    rec = seed_conversion(env)
    status, body = eventbridge(env, rec, "Completed",
                               ModelArtifacts={"S3ModelArtifacts": ARTIFACT})
    assert status == 200
    assert body["conversion_status"] == "Finalizing" and body["finalize_invoked"] is True
    assert body["compilation_triggered"] is False
    item = stored(env, rec)
    assert item["conversion"]["status"] == "Finalizing"
    assert (item["status"], item["progress"], item["artifact_s3"]) == ("InProgress", 80, ARTIFACT)
    assert "completed_at" not in item  # never the generic status copy
    invokes = finalize_invokes(env)
    assert len(invokes) == 1 and invokes[0]["InvocationType"] == "Event"
    payload = json.loads(invokes[0]["Payload"])
    assert payload["pathParameters"] == {"id": rec["training_id"]}
    assert json.loads(payload["body"]) == {"finalize_conversion": True, "auto_triggered": True}
    # no auto-compile for a conversion, even with auto_compile + targets on the record
    assert [i for i in env.fake_lambda.invokes if i["FunctionName"] != "test-packaging-fn"] == []


def test_sync_on_read_completed_claims_and_returns_the_post_transition_record(env):
    rec = seed_conversion(env)
    describe(env, rec, "Completed", ModelArtifacts={"S3ModelArtifacts": ARTIFACT})
    status, body = get(env, rec)
    assert status == 200
    assert body["conversion"]["status"] == "Finalizing"
    assert body["status"] == "InProgress" and body["progress"] == 80
    assert body["artifact_s3"] == ARTIFACT
    assert stored(env, rec)["conversion"]["status"] == "Finalizing"
    assert len(finalize_invokes(env)) == 1


def test_racing_writers_finalize_exactly_once(env):
    rec = seed_conversion(env)
    stale = stored(env, rec)  # sync-on-read's view, taken before EventBridge lands
    eventbridge(env, rec, "Completed", ModelArtifacts={"S3ModelArtifacts": ARTIFACT})
    sm_view = {"TrainingJobStatus": "Completed", "ModelArtifacts": {"S3ModelArtifacts": ARTIFACT}}
    result = env.training.sync_conversion_record(env.table, stale, sm_view, 123)
    assert result["conversion"]["status"] == "Finalizing"  # the winner's write, re-read
    assert len(finalize_invokes(env)) == 1
    # ...and a later GET or a duplicate event is a no-op as well
    describe(env, rec, "Completed", ModelArtifacts={"S3ModelArtifacts": ARTIFACT})
    get(env, rec)
    eventbridge(env, rec, "Completed", ModelArtifacts={"S3ModelArtifacts": ARTIFACT})
    assert len(finalize_invokes(env)) == 1


def test_sync_first_then_eventbridge_also_finalizes_once(env):
    rec = seed_conversion(env)
    describe(env, rec, "Completed", ModelArtifacts={"S3ModelArtifacts": ARTIFACT})
    get(env, rec)
    status, body = eventbridge(env, rec, "Completed",
                               ModelArtifacts={"S3ModelArtifacts": ARTIFACT})
    assert status == 200 and body["finalize_invoked"] is False
    assert len(finalize_invokes(env)) == 1


def test_completed_without_an_artifact_fails(env):
    rec = seed_conversion(env)
    eventbridge(env, rec, "Completed")
    item = stored(env, rec)
    assert item["conversion"]["status"] == "Failed" and item["status"] == "Failed"
    assert item["failure_reason"] == "Conversion job completed without an artifact"
    assert finalize_invokes(env) == []


# ---------------------------------------------------------------------------
# Failed / Stopped
# ---------------------------------------------------------------------------

def test_eventbridge_failed_records_the_reason_verbatim(env):
    rec = seed_conversion(env)
    status, body = eventbridge(env, rec, "Failed", FailureReason=FATAL)
    assert status == 200 and body["conversion_status"] == "Failed"
    item = stored(env, rec)
    assert (item["status"], item["progress"], item["failure_reason"]) == ("Failed", 0, FATAL)
    assert item["conversion"]["status"] == "Failed" and "failed_at" in item["conversion"]
    assert env.fake_lambda.invokes == []


def test_sync_on_read_failed_records_the_reason_verbatim(env):
    rec = seed_conversion(env)
    describe(env, rec, "Failed", FailureReason=FATAL)
    status, body = get(env, rec)
    assert status == 200
    assert body["status"] == "Failed" and body["failure_reason"] == FATAL
    assert body["conversion"]["status"] == "Failed"
    assert stored(env, rec)["failure_reason"] == FATAL


@pytest.mark.parametrize("reason,expected", [(None, "Conversion job was stopped"),
                                             ("Stopped by user", "Stopped by user")])
def test_stopped_is_failed(env, reason, expected):
    rec = seed_conversion(env)
    detail = {"FailureReason": reason} if reason else {}
    eventbridge(env, rec, "Stopped", **detail)
    item = stored(env, rec)
    assert item["conversion"]["status"] == "Failed" and item["failure_reason"] == expected


# ---------------------------------------------------------------------------
# Terminal / Finalizing records are never moved by SageMaker
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("conversion_status", ["Completed", "Failed", "Finalizing"])
@pytest.mark.parametrize("sm_status", ["Completed", "Failed", "Stopped"])
def test_no_sagemaker_status_moves_a_finalizing_or_terminal_record(env, conversion_status,
                                                                   sm_status):
    rec = seed_conversion(env, conversion_status, failure_reason="kept")
    before = stored(env, rec)
    detail = {"FailureReason": FATAL, "ModelArtifacts": {"S3ModelArtifacts": ARTIFACT}}
    status, body = eventbridge(env, rec, sm_status, **detail)
    assert status == 200 and body["conversion_status"] == conversion_status
    describe(env, rec, sm_status, **detail)
    status, got = get(env, rec)
    assert status == 200 and got["conversion"]["status"] == conversion_status
    assert stored(env, rec) == before
    assert env.fake_lambda.invokes == []


def test_a_running_job_changes_nothing(env):
    rec = seed_conversion(env)
    before = stored(env, rec)
    describe(env, rec, "InProgress", SecondaryStatus="Training")
    status, body = get(env, rec)
    assert status == 200 and body["conversion"]["status"] == "InProgress"
    assert stored(env, rec) == before


def test_a_lost_finalize_invoke_leaves_the_record_finalizing(env, monkeypatch):
    rec = seed_conversion(env)

    class Broken:
        def invoke(self, **_):
            raise RuntimeError("AccessDenied")

    monkeypatch.setattr(env.events, "boto3", SimpleNamespace(client=lambda *a, **k: Broken()))
    status, body = eventbridge(env, rec, "Completed",
                               ModelArtifacts={"S3ModelArtifacts": ARTIFACT})
    assert status == 200 and body["conversion_status"] == "Finalizing"
    assert stored(env, rec)["conversion"]["status"] == "Finalizing"  # Package action retries (7.9)


# ---------------------------------------------------------------------------
# Non-conversion records keep today's behaviour
# ---------------------------------------------------------------------------

def seed_plain(env, **extra):
    training_id = str(uuid.uuid4())
    item = {"training_id": training_id, "usecase_id": env.usecase_id, "model_name": "plain",
            "model_type": "object_detection", "runtime": "onnx", "status": "InProgress",
            "progress": 10, "training_job_name": f"plain-{uuid.uuid4().hex[:10]}",
            "auto_compile": True, "compilation_targets": ["jetson"]}
    item.update(extra)
    env.table.put_item(Item=item)
    return item


def test_trained_record_keeps_the_generic_copy_and_auto_compile(env):
    rec = seed_plain(env)
    status, body = eventbridge(env, rec, "Completed",
                               ModelArtifacts={"S3ModelArtifacts": ARTIFACT},
                               TrainingEndTime=datetime(2026, 9, 25, tzinfo=timezone.utc).isoformat())
    assert status == 200 and body["compilation_triggered"] is True
    item = stored(env, rec)
    assert (item["status"], item["progress"], item["artifact_s3"]) == ("Completed", 100, ARTIFACT)
    assert "completed_at" in item
    compile_invokes = [i for i in env.fake_lambda.invokes
                       if i["FunctionName"] == "test-compilation-fn"]
    assert len(compile_invokes) == 1
    # The legacy 'jetson' short name maps to the oldest supported JetPack (5).
    assert json.loads(json.loads(compile_invokes[0]["Payload"])["body"])["targets"] == ["jetson-xavier-jp5"]
    assert finalize_invokes(env) == []


def test_an_import_without_a_conversion_block_keeps_the_generic_copy(env):
    rec = seed_plain(env, source="imported", auto_compile=False,
                     metadata={"framework": "ONNX", "model_file": "model.onnx"})
    describe(env, rec, "Failed", FailureReason="boom")
    status, body = get(env, rec)
    assert status == 200 and body["status"] == "Failed" and body["failure_reason"] == "boom"
    assert "conversion" not in stored(env, rec)
