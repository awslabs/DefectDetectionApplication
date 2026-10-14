"""
training.py create/get tests for portal-detection-training (tasks 2.2, 5.1).

Two halves:

1. LFV preservation — a classification request produces the exact pre-change
   SageMaker `create_training_job` kwargs (marketplace AlgorithmName,
   AugmentedManifestFile / RecordIO / Pipe, EnableNetworkIsolation=True) and
   the pre-change DynamoDB item keys. Written and run green BEFORE the
   detection branch landed; must stay green after.
2. Detection path — `model_type='object_detection'` launches the script-mode
   entry point (TrainingImage, sourcedir in the use-case bucket, Environment,
   MetricDefinitions, no InputDataConfig, no network isolation) and persists
   the Detection_Record_Fields; wrong manifest / bad hyperparameters -> 400
   with no SageMaker call; GET hydrates metrics from FinalMetricDataList.

Fixture conventions follow test_onnx_compile_diagnostics_units.py (module env
on the moto aws_stack, FakeSageMakerService recording stub, own table/bucket).

# Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.4, 2.5, 3.1, 3.3, 3.4, 3.6,
# 3.7, 3.8, 3.10, 4.1, 4.2, 4.3, 9.2
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-detection-create"
USECASE_BUCKET = "test-detection-create-bucket"
ACCOUNT_ID = "123456789012"

_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS_DIR = os.path.join(_HERE, "..", "functions")
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))


def _load_module(filename, alias):
    spec = importlib.util.spec_from_file_location(
        alias, os.path.join(_FUNCTIONS_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


class FakeSageMakerService:
    def __init__(self):
        self.create_training_error = None
        self.training_jobs = {}
        self.create_training_calls = []
        self.describe_calls = []

    @staticmethod
    def _validate_like_botocore(kwargs):
        """The subset of botocore's client-side parameter validation that has
        bitten this handler: CreateTrainingJob's optional list members must be
        NON-EMPTY when present (a real 500 in production came from
        InputDataConfig=[]). Raises the same ParamValidationError botocore does."""
        from botocore.exceptions import ParamValidationError
        for key in ("InputDataConfig", "Tags"):
            if key in kwargs and len(kwargs[key]) == 0:
                raise ParamValidationError(
                    report=f"Invalid length for parameter {key}, value: 0, valid min length: 1")
        spec = kwargs.get("AlgorithmSpecification", {})
        if "MetricDefinitions" in spec and len(spec["MetricDefinitions"]) == 0:
            raise ParamValidationError(
                report="Invalid length for parameter AlgorithmSpecification.MetricDefinitions, "
                       "value: 0, valid min length: 1")

    def create_training_job(self, **kwargs):
        self.create_training_calls.append(kwargs)
        self._validate_like_botocore(kwargs)
        if self.create_training_error is not None:
            raise self.create_training_error
        name = kwargs["TrainingJobName"]
        self.training_jobs[name] = {
            "TrainingJobName": name,
            "TrainingJobStatus": "InProgress",
        }
        return {"TrainingJobArn":
                f"arn:aws:sagemaker:{REGION}:{ACCOUNT_ID}:training-job/{name}"}

    def describe_training_job(self, TrainingJobName):
        self.describe_calls.append(TrainingJobName)
        if TrainingJobName in self.training_jobs:
            return self.training_jobs[TrainingJobName]
        raise ClientError(
            {"Error": {"Code": "ValidationException",
                       "Message": f"Training job '{TrainingJobName}' does not exist."}},
            "DescribeTrainingJob")


class _Holder:
    current = None


def fresh_service():
    _Holder.current = FakeSageMakerService()
    return _Holder.current


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------

CLASSIFICATION_MANIFEST_KEY = "manifests/classification.manifest"
DETECTION_MANIFEST_KEY = "labeled/labeling-abc/output.manifest"
GT_DETECTION_MANIFEST_KEY = "labeled/gt-plates/output.manifest"
IMAGE_KEY = "images/frame_0001.jpg"


def _classification_line(bucket):
    return json.dumps({
        "source-ref": f"s3://{bucket}/{IMAGE_KEY}",
        "anomaly-label": 1,
        "anomaly-label-metadata": {
            "class-name": "anomaly", "confidence": 1.0,
            "type": "groundtruth/image-classification", "job-name": "j",
            "human-annotated": "yes", "creation-date": "2026-09-12T00:00:00",
        },
    })


def _detection_line(bucket, attr="bounding-box", class_map=None):
    class_map = class_map or {"0": "blue_plate"}
    return json.dumps({
        "source-ref": f"s3://{bucket}/{IMAGE_KEY}",
        attr: {
            "image_size": [{"width": 2001, "height": 2352, "depth": 3}],
            "annotations": [{"class_id": 0, "left": 10, "top": 20, "width": 300, "height": 280}],
        },
        f"{attr}-metadata": {
            "objects": [{"confidence": 1.0}],
            "class-map": class_map,
            "type": "groundtruth/object-detection",
            "human-annotated": "yes",
            "creation-date": "2026-09-12T00:00:00",
            "job-name": attr,
        },
    })


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def env(aws_stack):
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME
    os.environ.pop("DETECTION_TRAINING_IMAGE", None)

    # Stage the four entry-point files flat, the way the CDK bundler does.
    code_dir = tempfile.mkdtemp(prefix="dda-detection-code-")
    for rel in ("datasets/detection_training/train.py",
                "datasets/detection_training/requirements.txt",
                "datasets/manifest_to_detector_dataset.py",
                "datasets/dedupe_frames.py"):
        shutil.copy(os.path.join(_REPO_ROOT, rel), code_dir)
    os.environ["DETECTION_TRAINING_CODE_DIR"] = code_dir

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-training-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    s3 = aws_stack.s3
    s3.create_bucket(Bucket=USECASE_BUCKET)
    s3.put_object(Bucket=USECASE_BUCKET, Key=IMAGE_KEY, Body=b"\xff\xd8jpeg")
    s3.put_object(Bucket=USECASE_BUCKET, Key=CLASSIFICATION_MANIFEST_KEY,
                  Body=(_classification_line(USECASE_BUCKET) + "\n").encode())
    s3.put_object(Bucket=USECASE_BUCKET, Key=DETECTION_MANIFEST_KEY,
                  Body=(_detection_line(USECASE_BUCKET) + "\n").encode())
    s3.put_object(Bucket=USECASE_BUCKET, Key=GT_DETECTION_MANIFEST_KEY,
                  Body=(_detection_line(USECASE_BUCKET, attr="plates-bbox",
                                        class_map={"1": "luggage", "0": "plate"}) + "\n").encode())

    training = _load_module("training.py", "portal_training_detection")

    def _dispatch_get_usecase_client(service_name, usecase, session_name=None, region=None):
        if service_name == "sagemaker":
            return _Holder.current
        return boto3.client(service_name, region_name=region or REGION)

    training.get_usecase_client = _dispatch_get_usecase_client

    class _ModuleSageMakerProxy:
        """get_training_job's single-account branch uses the module-level
        `sagemaker` client; forward it to whichever fake is current."""
        def __getattr__(self, name):
            return getattr(_Holder.current, name)

    training.sagemaker = _ModuleSageMakerProxy()

    usecase_id = f"uc-{uuid.uuid4()}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "Detection Create Use Case",
        "account_id": ACCOUNT_ID,
        "s3_bucket": USECASE_BUCKET,
        "region": REGION,
        # Single-account shape: root ARN -> Lambda's own credentials.
        "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:root",
    })
    user_id = f"user-{uuid.uuid4()}"
    aws_stack.tables.user_roles.put_item(Item={
        "user_id": user_id, "usecase_id": usecase_id, "role": "DataScientist",
    })

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        training=training,
        s3=s3,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        usecase_id=usecase_id,
        user_id=user_id,
        code_dir=code_dir,
    )
    shutil.rmtree(code_dir, ignore_errors=True)


def _auth(env):
    return {"authorizer": {"claims": {
        "sub": env.user_id, "email": f"{env.user_id}@example.com",
        "cognito:username": env.user_id,
    }}}


def create(env, body):
    response = env.training.create_training_job({
        "httpMethod": "POST", "path": "/api/v1/training", "resource": "/training",
        "body": json.dumps(body), "requestContext": _auth(env),
    }, None)
    return response["statusCode"], json.loads(response["body"])


def get(env, training_id):
    response = env.training.get_training_job({
        "httpMethod": "GET", "path": f"/api/v1/training/{training_id}",
        "resource": "/training/{id}", "pathParameters": {"id": training_id},
        "requestContext": _auth(env),
    }, None)
    return response["statusCode"], json.loads(response["body"])


def lfv_body(env, **overrides):
    body = {
        "usecase_id": env.usecase_id,
        "model_source": "marketplace",
        "model_name": "cookie-cls",
        "model_version": "1.0.0",
        "model_type": "classification",
        "dataset_manifest_s3": f"s3://{USECASE_BUCKET}/{CLASSIFICATION_MANIFEST_KEY}",
        "instance_type": "ml.g4dn.2xlarge",
        "max_runtime_seconds": 3600,
    }
    body.update(overrides)
    return body


def detection_body(env, **overrides):
    body = {
        "usecase_id": env.usecase_id,
        "model_source": "marketplace",   # ignored for detection (Req 1.3)
        "model_name": "blue-plate",
        "model_version": "2.0.0",
        "model_type": "object_detection",
        "dataset_manifest_s3": f"s3://{USECASE_BUCKET}/{DETECTION_MANIFEST_KEY}",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 1. LFV preservation (Req 1.2)
# ---------------------------------------------------------------------------

LFV_ITEM_KEYS = {
    "training_id", "usecase_id", "model_name", "model_version", "model_type",
    "dataset_manifest_s3", "algorithm_uri", "hyperparameters", "instance_type",
    "training_job_name", "training_job_arn", "status", "progress", "created_by",
    "created_at", "updated_at", "auto_compile", "compilation_targets",
}


def test_lfv_classification_request_shape_preserved(env):
    svc = fresh_service()
    status, body = create(env, lfv_body(env))
    assert status == 201, body
    assert len(svc.create_training_calls) == 1
    kw = svc.create_training_calls[0]

    assert kw["AlgorithmSpecification"] == {
        "AlgorithmName": env.training.MARKETPLACE_ALGORITHM_ARN,
        "TrainingInputMode": "File",
        "EnableSageMakerMetricsTimeSeries": False,
    }
    assert "TrainingImage" not in kw["AlgorithmSpecification"]
    assert "MetricDefinitions" not in kw["AlgorithmSpecification"]
    assert kw["EnableNetworkIsolation"] is True
    assert "Environment" not in kw
    channel = kw["InputDataConfig"][0]
    assert channel["ChannelName"] == "training"
    assert channel["DataSource"]["S3DataSource"]["S3DataType"] == "AugmentedManifestFile"
    assert channel["DataSource"]["S3DataSource"]["AttributeNames"] == [
        "source-ref", "anomaly-label-metadata", "anomaly-label"]
    assert channel["RecordWrapperType"] == "RecordIO"
    assert channel["InputMode"] == "Pipe"
    assert kw["HyperParameters"] == {
        "ModelType": "classification",
        "TrainingInputDataAttributeNames": "source-ref,anomaly-label-metadata,anomaly-label",
        "TestInputDataAttributeNames": "source-ref,anomaly-label-metadata,anomaly-label",
    }
    assert kw["ResourceConfig"] == {
        "InstanceType": "ml.g4dn.2xlarge", "InstanceCount": 1, "VolumeSizeInGB": 20}
    assert kw["StoppingCondition"] == {"MaxRuntimeInSeconds": 3600}
    assert kw["RoleArn"] == f"arn:aws:iam::{ACCOUNT_ID}:role/DDASageMakerExecutionRole"
    assert kw["OutputDataConfig"]["S3OutputPath"].startswith(f"s3://{USECASE_BUCKET}/")
    assert {t["Key"] for t in kw["Tags"]} == {"UseCase", "ModelName", "ModelVersion", "CreatedBy"}

    item = env.training_jobs.get_item(Key={"training_id": body["training_id"]})["Item"]
    assert set(item.keys()) == LFV_ITEM_KEYS
    assert item["algorithm_uri"] == env.training.MARKETPLACE_ALGORITHM_ARN
    assert item["status"] == "InProgress" and item["progress"] == 10
    assert "runtime" not in item and "detection" not in item and "source" not in item


def test_lfv_rejects_unknown_model_type(env):
    svc = fresh_service()
    status, body = create(env, lfv_body(env, model_type="pose"))
    assert status == 400
    assert "Invalid model_type" in body["error"]
    assert svc.create_training_calls == []


def test_lfv_detection_manifest_still_goes_through_marketplace_validator(env):
    """Req 2.7: an LFV job with a bounding-box manifest keeps the existing
    marketplace validation failure (the detection validator is not used)."""
    svc = fresh_service()
    status, body = create(env, lfv_body(
        env, dataset_manifest_s3=f"s3://{USECASE_BUCKET}/{DETECTION_MANIFEST_KEY}"))
    assert status == 400
    assert body["error"] == "Manifest validation failed"
    assert "Manifest Transformer" in body["suggestion"]
    assert svc.create_training_calls == []


def test_lfv_get_does_not_touch_metrics(env):
    svc = fresh_service()
    status, body = create(env, lfv_body(env))
    assert status == 201
    svc.training_jobs[body["training_job_name"]].update({
        "TrainingJobStatus": "Completed",
        "ModelArtifacts": {"S3ModelArtifacts": f"s3://{USECASE_BUCKET}/out/model.tar.gz"},
        "FinalMetricDataList": [{"MetricName": "test:mAP50", "Value": 0.5}],
    })
    status, job = get(env, body["training_id"])
    assert status == 200
    assert job["status"] == "Completed"
    assert "metrics" not in job
    item = env.training_jobs.get_item(Key={"training_id": body["training_id"]})["Item"]
    assert "metrics" not in item


# ---------------------------------------------------------------------------
# 2. Detection path (Req 1.1, 1.3, 2.1, 2.4, 3.1, 3.3, 3.4, 3.6, 3.7, 3.8, 4.1)
# ---------------------------------------------------------------------------

def test_detection_request_launches_script_mode_job(env):
    svc = fresh_service()
    status, body = create(env, detection_body(env))
    assert status == 201, body
    assert len(svc.create_training_calls) == 1
    kw = svc.create_training_calls[0]
    job_name = kw["TrainingJobName"]
    assert job_name.startswith("blue-plate-")

    # Script mode on the regional DLC, never the marketplace algorithm.
    spec = kw["AlgorithmSpecification"]
    assert "AlgorithmName" not in spec
    assert spec["TrainingImage"] == (
        f"763104351884.dkr.ecr.{REGION}.amazonaws.com/"
        "pytorch-training:2.5.1-gpu-py311-cu124-ubuntu22.04-sagemaker")
    assert spec["TrainingInputMode"] == "File"
    metric_names = {m["Name"] for m in spec["MetricDefinitions"]}
    assert metric_names == {"test:mAP50", "test:mAP50-95", "test:precision", "test:recall"}

    # sourcedir staged in the use-case bucket next to the job.
    code_s3 = kw["HyperParameters"]["sagemaker_submit_directory"]
    assert code_s3 == f"s3://{USECASE_BUCKET}/models/detection-training/{job_name}/sourcedir.tar.gz"
    assert kw["HyperParameters"]["sagemaker_program"] == "train.py"
    obj = env.s3.get_object(Bucket=USECASE_BUCKET,
                            Key=f"models/detection-training/{job_name}/sourcedir.tar.gz")
    import io, tarfile
    with tarfile.open(fileobj=io.BytesIO(obj["Body"].read()), mode="r:gz") as tar:
        assert sorted(m.name for m in tar.getmembers()) == [
            "dedupe_frames.py", "manifest_to_detector_dataset.py",
            "requirements.txt", "train.py"]

    # Entry-point env passed BOTH as HyperParameters and Environment; no IMAGES_S3.
    expected_env = {
        "MANIFEST_S3": f"s3://{USECASE_BUCKET}/{DETECTION_MANIFEST_KEY}",
        "IMGSZ": "1280", "EPOCHS": "100", "BATCH": "4",
        "BASE_WEIGHTS": "yolo11s.pt", "PATIENCE": "30", "ONNX_OPSET": "17",
    }
    assert kw["Environment"] == expected_env
    for k, v in expected_env.items():
        assert kw["HyperParameters"][k] == v
    assert "IMAGES_S3" not in kw["Environment"]

    # No input channel at all — the entry point pulls manifest + images itself.
    # (An empty list is rejected client-side by botocore; this was a real 500.)
    assert "InputDataConfig" not in kw
    assert kw["EnableNetworkIsolation"] is False
    assert kw["ResourceConfig"] == {
        "InstanceType": "ml.g4dn.xlarge", "InstanceCount": 1, "VolumeSizeInGB": 60}
    assert kw["StoppingCondition"] == {"MaxRuntimeInSeconds": 10800}
    assert kw["RoleArn"] == f"arn:aws:iam::{ACCOUNT_ID}:role/DDASageMakerExecutionRole"
    assert kw["OutputDataConfig"]["S3OutputPath"] == \
        f"s3://{USECASE_BUCKET}/models/training/{job_name}"
    assert {t["Key"] for t in kw["Tags"]} == {"UseCase", "ModelName", "ModelVersion", "CreatedBy"}

    # Record: LFV base keys + runtime/detection block (Req 4.1, 4.4).
    item = env.training_jobs.get_item(Key={"training_id": body["training_id"]})["Item"]
    assert set(item.keys()) == LFV_ITEM_KEYS | {"runtime", "detection"}
    assert item["model_type"] == "object_detection"
    assert item["runtime"] == "onnx"
    assert item["algorithm_uri"] == spec["TrainingImage"]
    assert "source" not in item
    det = item["detection"]
    assert det["detection_arch"] == "yolo"
    assert det["network_input_width"] == 1280 and det["network_input_height"] == 1280
    assert det["class_names"] == ["blue_plate"] and det["num_classes"] == 1
    assert det["score_threshold"] == Decimal("0.25") and det["iou_threshold"] == Decimal("0.45")
    assert det["preserve_aspect"] is True
    assert det["base_weights"] == "yolo11s.pt" and det["epochs"] == 100
    assert det["sourcedir_s3"] == code_s3


def test_detection_hyperparameters_and_overrides_flow_through(env):
    svc = fresh_service()
    status, body = create(env, detection_body(
        env,
        instance_type="ml.g5.xlarge",
        max_runtime_seconds=7200,
        hyperparameters={"imgsz": 640, "epochs": 20, "batch": 16, "base_weights": "yolo11n.pt",
                         "patience": 5, "score_threshold": 0.3, "iou_threshold": 0.5},
        class_names=["plate", "luggage"],
    ))
    assert status == 201, body
    kw = svc.create_training_calls[0]
    assert kw["Environment"]["IMGSZ"] == "640"
    assert kw["Environment"]["EPOCHS"] == "20"
    assert kw["Environment"]["BATCH"] == "16"
    assert kw["Environment"]["BASE_WEIGHTS"] == "yolo11n.pt"
    assert kw["Environment"]["PATIENCE"] == "5"
    assert kw["ResourceConfig"]["InstanceType"] == "ml.g5.xlarge"
    assert kw["StoppingCondition"] == {"MaxRuntimeInSeconds": 7200}
    det = env.training_jobs.get_item(Key={"training_id": body["training_id"]})["Item"]["detection"]
    assert det["class_names"] == ["plate", "luggage"] and det["num_classes"] == 2
    assert det["network_input_width"] == 640
    assert det["score_threshold"] == Decimal("0.3")


def test_detection_accepts_ground_truth_job_named_manifest(env):
    svc = fresh_service()
    status, body = create(env, detection_body(
        env, dataset_manifest_s3=f"s3://{USECASE_BUCKET}/{GT_DETECTION_MANIFEST_KEY}"))
    assert status == 201, body
    det = env.training_jobs.get_item(Key={"training_id": body["training_id"]})["Item"]["detection"]
    # class-map {"1": "luggage", "0": "plate"} sorted by integer id.
    assert det["class_names"] == ["plate", "luggage"]
    assert len(svc.create_training_calls) == 1


def test_detection_rejects_classification_manifest_without_transformer_advice(env):
    svc = fresh_service()
    status, body = create(env, detection_body(
        env, dataset_manifest_s3=f"s3://{USECASE_BUCKET}/{CLASSIFICATION_MANIFEST_KEY}"))
    assert status == 400, body
    assert body["error"] == "Object Detection requires a bounding-box manifest"
    assert any("anomaly-label" in d for d in body["details"])
    assert "anomaly-label" in body["detected_attributes"]
    assert "Transform" not in json.dumps(body)
    assert svc.create_training_calls == []
    assert env.s3.list_objects_v2(Bucket=USECASE_BUCKET,
                                  Prefix="models/detection-training/blue-plate-").get("KeyCount", 0) == \
        _sourcedir_count_before(env)


def _sourcedir_count_before(env):
    """Helper: number of sourcedirs staged so far (rejections must add none)."""
    return env.s3.list_objects_v2(Bucket=USECASE_BUCKET,
                                  Prefix="models/detection-training/blue-plate-").get("KeyCount", 0)


def test_detection_rejects_missing_manifest(env):
    svc = fresh_service()
    status, body = create(env, detection_body(
        env, dataset_manifest_s3=f"s3://{USECASE_BUCKET}/labeled/nope/output.manifest"))
    assert status == 400, body
    assert "not found" in " ".join(body["details"]).lower()
    assert "labeled/nope/output.manifest" in " ".join(body["details"])
    assert svc.create_training_calls == []


@pytest.mark.parametrize("hp,field", [
    ({"imgsz": 1000}, "imgsz"),
    ({"epochs": 0}, "epochs"),
    ({"batch": 100}, "batch"),
    ({"base_weights": "weights"}, "base_weights"),
    ({"score_threshold": 1.0}, "score_threshold"),
    ({"iou_threshold": -0.1}, "iou_threshold"),
    ({"classification_logic": "seg_head"}, "classification_logic"),
])
def test_detection_bad_hyperparameter_is_400_with_no_sagemaker_call(env, hp, field):
    svc = fresh_service()
    before = _sourcedir_count_before(env)
    status, body = create(env, detection_body(env, hyperparameters=hp))
    assert status == 400, body
    assert field in body["error"]
    assert svc.create_training_calls == []
    assert _sourcedir_count_before(env) == before


def test_detection_sagemaker_failure_maps_like_lfv_and_writes_no_record(env):
    svc = fresh_service()
    svc.create_training_error = ClientError(
        {"Error": {"Code": "ResourceLimitExceeded",
                   "Message": "The account-level service limit 'ml.g4dn.xlarge for training job usage' is 1 Instances"}},
        "CreateTrainingJob")
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, detection_body(env))
    assert status == 429, body
    assert env.training_jobs.scan(Select="COUNT")["Count"] == n_before


def test_detection_image_override_env(env, monkeypatch):
    svc = fresh_service()
    monkeypatch.setenv("DETECTION_TRAINING_IMAGE", "999.dkr.ecr.us-east-1.amazonaws.com/custom-yolo:2")
    status, body = create(env, detection_body(env))
    assert status == 201, body
    assert svc.create_training_calls[0]["AlgorithmSpecification"]["TrainingImage"] == \
        "999.dkr.ecr.us-east-1.amazonaws.com/custom-yolo:2"
    item = env.training_jobs.get_item(Key={"training_id": body["training_id"]})["Item"]
    assert item["algorithm_uri"] == "999.dkr.ecr.us-east-1.amazonaws.com/custom-yolo:2"


def test_detection_requires_datascientist_role(env, aws_stack):
    svc = fresh_service()
    viewer = f"user-{uuid.uuid4()}"
    aws_stack.tables.user_roles.put_item(Item={
        "user_id": viewer, "usecase_id": env.usecase_id, "role": "Viewer"})
    response = env.training.create_training_job({
        "httpMethod": "POST", "path": "/api/v1/training", "resource": "/training",
        "body": json.dumps(detection_body(env)),
        "requestContext": {"authorizer": {"claims": {
            "sub": viewer, "email": f"{viewer}@example.com", "cognito:username": viewer}}},
    }, None)
    assert response["statusCode"] == 403
    assert svc.create_training_calls == []


# ---------------------------------------------------------------------------
# GET hydrates metrics (Req 4.2)
# ---------------------------------------------------------------------------

def _complete(svc, job_name, with_metrics=True):
    svc.training_jobs[job_name].update({
        "TrainingJobStatus": "Completed",
        "ModelArtifacts": {"S3ModelArtifacts": f"s3://{USECASE_BUCKET}/models/training/{job_name}/output/model.tar.gz"},
        "FinalMetricDataList": [
            {"MetricName": "test:mAP50", "Value": 0.995},
            {"MetricName": "test:mAP50-95", "Value": 0.919},
            {"MetricName": "test:precision", "Value": 0.9989},
            {"MetricName": "test:recall", "Value": 1.0},
        ] if with_metrics else [],
    })


def test_detection_get_hydrates_metrics_on_status_transition(env):
    svc = fresh_service()
    status, body = create(env, detection_body(env))
    assert status == 201
    _complete(svc, body["training_job_name"])
    status, job = get(env, body["training_id"])
    assert status == 200
    assert job["status"] == "Completed"
    assert job["artifact_s3"].endswith("/output/model.tar.gz")
    assert job["metrics"] == {"test:mAP50": 0.995, "test:mAP50-95": 0.919,
                              "test:precision": 0.9989, "test:recall": 1.0}
    item = env.training_jobs.get_item(Key={"training_id": body["training_id"]})["Item"]
    assert item["metrics"]["test:mAP50"] == Decimal("0.995")


def test_detection_get_hydrates_metrics_when_eventbridge_already_completed(env):
    """training_events.py may flip status first; metrics must still land."""
    svc = fresh_service()
    status, body = create(env, detection_body(env))
    assert status == 201
    _complete(svc, body["training_job_name"])
    env.training_jobs.update_item(
        Key={"training_id": body["training_id"]},
        UpdateExpression="SET #s = :s, progress = :p",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "Completed", ":p": 100})
    status, job = get(env, body["training_id"])
    assert status == 200
    assert job["metrics"]["test:recall"] == 1.0
    # A second GET is a no-op (already hydrated).
    status, job2 = get(env, body["training_id"])
    assert job2["metrics"] == job["metrics"]


def test_detection_get_without_final_metrics_leaves_metrics_absent(env):
    svc = fresh_service()
    status, body = create(env, detection_body(env))
    _complete(svc, body["training_job_name"], with_metrics=False)
    status, job = get(env, body["training_id"])
    assert status == 200 and job["status"] == "Completed"
    assert "metrics" not in job


# ---------------------------------------------------------------------------
# 3. RF-DETR arch smoke (rfdetr-training-and-transfer-learning task 5.1).
#    The full RF-DETR / base-model matrix lands in task 5.2.
# ---------------------------------------------------------------------------

def test_rfdetr_request_reaches_sagemaker_with_rfdetr_entry_point(env, monkeypatch):
    """`detection_arch='rf_detr'` launches train_rfdetr.py with the RF-DETR env
    and persists the RF-DETR record fields (top_k, no iou_threshold) plus the
    default published Base_Model_Descriptor. The entry point itself is task
    4.2, so the code dir is staged with stubs here."""
    svc = fresh_service()
    code_dir = tempfile.mkdtemp(prefix="dda-rfdetr-code-")
    try:
        for rel in ("datasets/manifest_to_detector_dataset.py", "datasets/dedupe_frames.py"):
            shutil.copy(os.path.join(_REPO_ROOT, rel), code_dir)
        with open(os.path.join(code_dir, "train_rfdetr.py"), "w") as fh:
            fh.write("# stub entry point (task 4.2)\n")
        with open(os.path.join(code_dir, "requirements-rfdetr.txt"), "w") as fh:
            fh.write("rfdetr\n")
        monkeypatch.setenv("DETECTION_TRAINING_CODE_DIR", code_dir)

        status, body = create(env, detection_body(env, detection_arch="rf_detr"))
        assert status == 201, body
        kw = svc.create_training_calls[0]
        assert kw["HyperParameters"]["sagemaker_program"] == "train_rfdetr.py"
        assert kw["Environment"] == {
            "MANIFEST_S3": f"s3://{USECASE_BUCKET}/{DETECTION_MANIFEST_KEY}",
            "RFDETR_SIZE": "small", "RESOLUTION": "512", "EPOCHS": "100", "BATCH": "4",
            "GRAD_ACCUM": "4", "LR": "0.0001", "PATIENCE": "10", "ONNX_OPSET": "17",
        }
        assert "BASE_WEIGHTS_S3" not in kw["Environment"]

        det = env.training_jobs.get_item(Key={"training_id": body["training_id"]})["Item"]["detection"]
        assert det["detection_arch"] == "rf_detr"
        assert det["network_input_width"] == 512 and det["network_input_height"] == 512
        assert det["preserve_aspect"] is False and det["top_k"] == 300
        assert "iou_threshold" not in det
        assert det["rfdetr_size"] == "small" and det["resolution"] == 512
        assert det["grad_accum"] == 4 and det["lr"] == Decimal("0.0001")
        assert det["base_model"]["kind"] == "published" and det["base_model"]["ref"] == "small"
        assert det["base_model"]["weights_s3"] is None
    finally:
        shutil.rmtree(code_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. RF-DETR request shape + base-model resolution matrix
#    (rfdetr-training-and-transfer-learning task 5.2).
#    Req 3.2 / 3.3 (RF-DETR launch + record), Req 6.3–6.6 (base model ->
#    BASE_WEIGHTS_S3 / BASE_WEIGHTS_MEMBER, every rejection a 400 with no S3
#    write and no SageMaker call), Req 8.1 / 8.2 (YOLO shape unchanged).
# ---------------------------------------------------------------------------

# Exactly what the RF-DETR entry point reads (design §Shared layer): no
# SCORE_THRESHOLD, no IOU — thresholds only shape the device manifest.
RFDETR_ENV_KEYS = {"MANIFEST_S3", "RFDETR_SIZE", "RESOLUTION", "EPOCHS", "BATCH",
                   "GRAD_ACCUM", "LR", "PATIENCE", "ONNX_OPSET"}

# The YOLO env test_detection_request_launches_script_mode_job pins for a
# request that names neither detection_arch nor base_model.
YOLO_DEFAULT_ENV = {
    "MANIFEST_S3": f"s3://{USECASE_BUCKET}/{DETECTION_MANIFEST_KEY}",
    "IMGSZ": "1280", "EPOCHS": "100", "BATCH": "4",
    "BASE_WEIGHTS": "yolo11s.pt", "PATIENCE": "30", "ONNX_OPSET": "17",
}
YOLO_SOURCEDIR_MEMBERS = ["dedupe_frames.py", "manifest_to_detector_dataset.py",
                          "requirements.txt", "train.py"]
# The pre-change YOLO Detection_Record_Fields (portal-detection-training Req 4.1).
YOLO_RECORD_KEYS = {
    "detection_arch", "network_input_width", "network_input_height", "class_names",
    "num_classes", "score_threshold", "iou_threshold", "preserve_aspect", "imgsz",
    "epochs", "batch", "base_weights", "patience", "onnx_opset", "sourcedir_s3",
}
CREATE_TRAINING_JOB_KWARGS = {
    "TrainingJobName", "HyperParameters", "Environment", "AlgorithmSpecification",
    "RoleArn", "OutputDataConfig", "ResourceConfig", "StoppingCondition",
    "EnableNetworkIsolation", "Tags",
}

RFDETR_STUB_ENTRY_POINT = "# stub RF-DETR entry point (task 4.2)\n"
RFDETR_STUB_REQUIREMENTS = "rfdetr[onnxexport]==1.2.1\nonnxruntime\nnumpy<2\n"


@pytest.fixture
def both_arch_code_dir(env, monkeypatch):
    """Stage a code dir holding BOTH entry points flat, the way the CDK bundler
    will after task 8.1. `train_rfdetr.py` / `requirements-rfdetr.txt` are
    task 4.2: the real files are copied when they exist on disk, otherwise
    stubbed (the handler only tars them — nothing here executes them). No
    `_common.py`, so the YOLO tarball keeps its pinned four members."""
    code_dir = tempfile.mkdtemp(prefix="dda-both-arch-code-")
    for rel in ("datasets/detection_training/train.py",
                "datasets/detection_training/requirements.txt",
                "datasets/manifest_to_detector_dataset.py",
                "datasets/dedupe_frames.py"):
        shutil.copy(os.path.join(_REPO_ROOT, rel), code_dir)
    for rel, stub in (("datasets/detection_training/train_rfdetr.py", RFDETR_STUB_ENTRY_POINT),
                      ("datasets/detection_training/requirements-rfdetr.txt", RFDETR_STUB_REQUIREMENTS)):
        src = os.path.join(_REPO_ROOT, rel)
        if os.path.isfile(src):
            shutil.copy(src, code_dir)
        else:
            with open(os.path.join(code_dir, os.path.basename(rel)), "w") as fh:
                fh.write(stub)
    monkeypatch.setenv("DETECTION_TRAINING_CODE_DIR", code_dir)
    yield code_dir
    shutil.rmtree(code_dir, ignore_errors=True)


class _RecordingS3:
    """Wraps the moto S3 client the handler gets from get_usecase_client and
    records every WRITE, so a 400 can prove the handler staged nothing in the
    use-case bucket (Req 6.4). Reads pass straight through."""
    WRITE_METHODS = ("put_object", "upload_file", "upload_fileobj", "copy_object", "copy",
                     "delete_object", "delete_objects")

    def __init__(self, inner):
        self._inner = inner
        self.writes = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name in self.WRITE_METHODS and callable(attr):
            def _record(*args, **kwargs):
                self.writes.append((name, args, kwargs))
                return attr(*args, **kwargs)
            return _record
        return attr


@pytest.fixture
def s3_spy(env, monkeypatch):
    import boto3
    spy = _RecordingS3(boto3.client("s3", region_name=REGION))
    original = env.training.get_usecase_client

    def _dispatch(service_name, usecase, session_name=None, region=None):
        if service_name == "s3":
            return spy
        return original(service_name, usecase, session_name=session_name, region=region)

    monkeypatch.setattr(env.training, "get_usecase_client", _dispatch)
    return spy


def _sourcedir_members(env, kw):
    """(member names, {name: bytes}) of the sourcedir.tar.gz a call staged."""
    import io, tarfile
    code_s3 = kw["HyperParameters"]["sagemaker_submit_directory"]
    key = code_s3.split(f"s3://{USECASE_BUCKET}/", 1)[1]
    obj = env.s3.get_object(Bucket=USECASE_BUCKET, Key=key)
    contents = {}
    with tarfile.open(fileobj=io.BytesIO(obj["Body"].read()), mode="r:gz") as tar:
        for member in tar.getmembers():
            contents[member.name] = tar.extractfile(member).read()
    return sorted(contents), contents


def _completed_base_job(env, svc, arch="yolo"):
    """Create a detection job through the handler, mark it Completed in the
    fake SageMaker and GET it so training.py persists artifact_s3 — the exact
    record a later run's `base_model = {kind: 'training_job'}` resolves
    against. Returns (training_id, artifact_s3)."""
    body = detection_body(env, model_name=f"base-{arch.replace('_', '-')}", model_version="1.0.0")
    if arch == "rf_detr":
        body["detection_arch"] = "rf_detr"
    status, created = create(env, body)
    assert status == 201, created
    _complete(svc, created["training_job_name"])
    status, job = get(env, created["training_id"])
    assert status == 200 and job["status"] == "Completed", job
    assert job["artifact_s3"].endswith("/output/model.tar.gz")
    return created["training_id"], job["artifact_s3"]


def _published_descriptor(arch, ref):
    return {"kind": "published", "ref": ref, "weights_s3": None, "member": None,
            "detection_arch": arch, "class_names": None}


def _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_records_before, expected_calls=0):
    assert status == 400, body
    assert len(svc.create_training_calls) == expected_calls
    assert s3_spy.writes == [], [w[0] for w in s3_spy.writes]
    assert env.training_jobs.scan(Select="COUNT")["Count"] == n_records_before
    return body["error"]


# --- (1) RF-DETR request shape --------------------------------------------

def test_rfdetr_request_shape_env_sourcedir_and_record(env, both_arch_code_dir):
    """Req 3.2 / 3.3: the RF-DETR entry point, ITS env (no IOU, no score),
    a sourcedir whose requirements.txt IS requirements-rfdetr.txt, and the
    RF-DETR Detection_Record_Fields (top_k, no iou_threshold, published
    Base_Model_Descriptor)."""
    svc = fresh_service()
    status, body = create(env, detection_body(
        env, detection_arch="rf_detr",
        hyperparameters={"rfdetr_size": "medium", "resolution": 640, "epochs": 50, "batch": 2,
                         "grad_accum": 8, "lr": 0.0002, "patience": 5, "score_threshold": 0.4},
    ))
    assert status == 201, body
    # The 201 body is the LFV shape (training_id / job name / arn / status /
    # message); arch and base model live on the record, asserted below.
    assert set(body) == {"training_id", "training_job_name", "training_job_arn", "status", "message"}
    assert len(svc.create_training_calls) == 1
    kw = svc.create_training_calls[0]
    assert set(kw) == CREATE_TRAINING_JOB_KWARGS

    # Entry point + env (passed both as HyperParameters and Environment).
    assert kw["HyperParameters"]["sagemaker_program"] == "train_rfdetr.py"
    env_vars = kw["Environment"]
    assert env_vars == {
        "MANIFEST_S3": f"s3://{USECASE_BUCKET}/{DETECTION_MANIFEST_KEY}",
        "RFDETR_SIZE": "medium", "RESOLUTION": "640", "EPOCHS": "50", "BATCH": "2",
        "GRAD_ACCUM": "8", "LR": "0.0002", "PATIENCE": "5", "ONNX_OPSET": "17",
    }
    assert set(env_vars) == RFDETR_ENV_KEYS
    assert not any("IOU" in k or "SCORE" in k or "IMGSZ" in k for k in env_vars)
    assert "BASE_WEIGHTS_S3" not in env_vars and "BASE_WEIGHTS_MEMBER" not in env_vars
    for k, v in env_vars.items():
        assert kw["HyperParameters"][k] == v
    assert kw["EnableNetworkIsolation"] is False
    assert kw["ResourceConfig"]["InstanceType"] == "ml.g4dn.xlarge"
    assert "InputDataConfig" not in kw
    assert {m["Name"] for m in kw["AlgorithmSpecification"]["MetricDefinitions"]} == \
        {"test:mAP50", "test:mAP50-95", "test:precision", "test:recall"}

    # Sourcedir: the RF-DETR entry point + ITS requirements renamed to
    # requirements.txt; the YOLO entry point is NOT bundled (Req 3.4).
    members, contents = _sourcedir_members(env, kw)
    assert members == ["dedupe_frames.py", "manifest_to_detector_dataset.py",
                       "requirements.txt", "train_rfdetr.py"]
    with open(os.path.join(both_arch_code_dir, "requirements-rfdetr.txt"), "rb") as fh:
        assert contents["requirements.txt"] == fh.read()
    with open(os.path.join(both_arch_code_dir, "train_rfdetr.py"), "rb") as fh:
        assert contents["train_rfdetr.py"] == fh.read()

    # Record.
    item = env.training_jobs.get_item(Key={"training_id": body["training_id"]})["Item"]
    assert set(item.keys()) == LFV_ITEM_KEYS | {"runtime", "detection"}
    assert item["model_type"] == "object_detection" and item["runtime"] == "onnx"
    det = item["detection"]
    assert det["detection_arch"] == "rf_detr"
    assert det["rfdetr_size"] == "medium"
    assert det["resolution"] == 640
    assert det["network_input_width"] == 640 and det["network_input_height"] == 640
    assert det["grad_accum"] == 8 and det["lr"] == Decimal("0.0002")
    assert det["epochs"] == 50 and det["batch"] == 2 and det["patience"] == 5
    assert det["top_k"] == 300
    assert det["preserve_aspect"] is False
    assert det["score_threshold"] == Decimal("0.4")
    assert det["class_names"] == ["blue_plate"] and det["num_classes"] == 1
    assert det["sourcedir_s3"] == kw["HyperParameters"]["sagemaker_submit_directory"]
    assert "iou_threshold" not in det
    assert "imgsz" not in det and "base_weights" not in det
    assert det["base_model"] == _published_descriptor("rf_detr", "medium")


def test_rfdetr_resolution_defaults_to_the_size_native_value(env, both_arch_code_dir):
    svc = fresh_service()
    status, body = create(env, detection_body(
        env, detection_arch="rf_detr", hyperparameters={"rfdetr_size": "large"}))
    assert status == 201, body
    assert svc.create_training_calls[0]["Environment"]["RESOLUTION"] == "704"
    det = env.training_jobs.get_item(Key={"training_id": body["training_id"]})["Item"]["detection"]
    assert det["resolution"] == 704 and det["network_input_width"] == 704


# --- (2) resolution 500 -> 400, nothing staged ----------------------------

def test_rfdetr_resolution_not_multiple_of_32_is_400_with_no_side_effects(env, both_arch_code_dir, s3_spy):
    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, detection_body(
        env, detection_arch="rf_detr", hyperparameters={"resolution": 500}))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert error == ("Invalid hyperparameter 'resolution': must be a multiple of 32 "
                     "between 224 and 1120")


@pytest.mark.parametrize("hp,field", [
    ({"rfdetr_size": "xlarge"}, "rfdetr_size"),
    ({"grad_accum": 0}, "grad_accum"),
    ({"lr": 1.0}, "lr"),
    ({"imgsz": 640}, "imgsz"),              # YOLO-only knob on an RF-DETR request
    ({"iou_threshold": 0.5}, "iou_threshold"),
])
def test_rfdetr_bad_hyperparameter_is_400_with_no_side_effects(env, both_arch_code_dir, s3_spy, hp, field):
    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, detection_body(env, detection_arch="rf_detr", hyperparameters=hp))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert field in error


# --- (8) unknown arch -> 400 ----------------------------------------------

@pytest.mark.parametrize("arch", ["detr", "yolov8", "RF-DETR"])
def test_unknown_detection_arch_is_400_with_no_side_effects(env, s3_spy, arch):
    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, detection_body(env, detection_arch=arch))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert "detection_arch" in error and arch in error
    assert "yolo" in error and "rf_detr" in error


# --- (3) base model from a completed job -> BASE_WEIGHTS_S3 + member ------

def test_base_model_from_completed_yolo_job_sets_base_weights_env(env, both_arch_code_dir):
    """Req 6.3 / 6.5 / 6.6: the env carries the base job's Detection_Artifact
    (the tarball — the entry point extracts best.pt itself) and the record
    persists the resolved Base_Model_Descriptor. Everything else about the
    YOLO request is unchanged."""
    svc = fresh_service()
    base_id, artifact_s3 = _completed_base_job(env, svc, arch="yolo")

    status, body = create(env, detection_body(
        env, base_model={"kind": "training_job", "ref": base_id}))
    assert status == 201, body
    assert len(svc.create_training_calls) == 2
    kw = svc.create_training_calls[1]
    assert kw["HyperParameters"]["sagemaker_program"] == "train.py"
    assert kw["Environment"] == {
        **YOLO_DEFAULT_ENV,
        "BASE_WEIGHTS_S3": artifact_s3,
        "BASE_WEIGHTS_MEMBER": "best.pt",
    }
    assert kw["HyperParameters"]["BASE_WEIGHTS_S3"] == artifact_s3
    assert kw["HyperParameters"]["BASE_WEIGHTS_MEMBER"] == "best.pt"
    assert artifact_s3.startswith(f"s3://{USECASE_BUCKET}/models/training/base-yolo-")

    det = env.training_jobs.get_item(Key={"training_id": body["training_id"]})["Item"]["detection"]
    assert det["base_model"] == {
        "kind": "training_job", "ref": base_id, "weights_s3": artifact_s3,
        "member": "best.pt", "detection_arch": "yolo", "class_names": ["blue_plate"],
    }
    # The rest of the YOLO record is what it always was.
    assert set(det) == YOLO_RECORD_KEYS | {"base_model"}
    assert det["detection_arch"] == "yolo" and det["base_weights"] == "yolo11s.pt"
    assert det["iou_threshold"] == Decimal("0.45") and det["preserve_aspect"] is True


def test_base_model_from_completed_rfdetr_job_uses_the_rfdetr_checkpoint_member(env, both_arch_code_dir):
    svc = fresh_service()
    base_id, artifact_s3 = _completed_base_job(env, svc, arch="rf_detr")

    status, body = create(env, detection_body(
        env, detection_arch="rf_detr", base_model={"kind": "training_job", "ref": base_id}))
    assert status == 201, body
    kw = svc.create_training_calls[1]
    assert kw["HyperParameters"]["sagemaker_program"] == "train_rfdetr.py"
    assert kw["Environment"]["BASE_WEIGHTS_S3"] == artifact_s3
    assert kw["Environment"]["BASE_WEIGHTS_MEMBER"] == "checkpoint_best_total.pth"
    assert set(kw["Environment"]) == RFDETR_ENV_KEYS | {"BASE_WEIGHTS_S3", "BASE_WEIGHTS_MEMBER"}
    det = env.training_jobs.get_item(Key={"training_id": body["training_id"]})["Item"]["detection"]
    assert det["base_model"]["kind"] == "training_job" and det["base_model"]["ref"] == base_id
    assert det["base_model"]["weights_s3"] == artifact_s3
    assert det["base_model"]["member"] == "checkpoint_best_total.pth"
    assert det["base_model"]["detection_arch"] == "rf_detr"


# --- (4) arch mismatch -> 400 ---------------------------------------------

@pytest.mark.parametrize("base_arch,request_arch,expected", [
    ("yolo", "rf_detr", "is a yolo detector; cannot start an rf_detr run from it"),
    ("rf_detr", "yolo", "is a rf_detr detector; cannot start a yolo run from it"),
])
def test_base_model_arch_mismatch_is_400_with_no_side_effects(
        env, both_arch_code_dir, s3_spy, base_arch, request_arch, expected):
    svc = fresh_service()
    base_id, _artifact = _completed_base_job(env, svc, arch=base_arch)
    s3_spy.writes.clear()          # the base job legitimately staged its own sourcedir
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]

    status, body = create(env, detection_body(
        env, detection_arch=request_arch, base_model={"kind": "training_job", "ref": base_id}))
    error = _assert_rejected_without_side_effects(
        env, svc, s3_spy, status, body, n_before, expected_calls=1)
    assert f"base-{base_arch.replace('_', '-')} v1.0.0" in error
    assert expected in error


# --- (5) InProgress base -> 400 -------------------------------------------

def test_base_model_not_completed_is_400_with_no_side_effects(env, s3_spy):
    svc = fresh_service()
    status, created = create(env, detection_body(env, model_name="base-yolo", model_version="1.0.0"))
    assert status == 201, created            # InProgress, no artifact yet
    s3_spy.writes.clear()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]

    status, body = create(env, detection_body(
        env, base_model={"kind": "training_job", "ref": created["training_id"]}))
    error = _assert_rejected_without_side_effects(
        env, svc, s3_spy, status, body, n_before, expected_calls=1)
    assert "base-yolo v1.0.0" in error
    assert "is not Completed (status: InProgress)" in error


# --- (6) other use case / unknown ref -> 400 (one message, no leak) -------

@pytest.mark.parametrize("where", ["other-usecase", "unknown"])
def test_base_model_outside_this_usecase_is_400_with_no_side_effects(env, s3_spy, where):
    svc = fresh_service()
    ref = str(uuid.uuid4())
    if where == "other-usecase":
        # A perfectly good Completed YOLO detector — in someone else's use case.
        env.training_jobs.put_item(Item={
            "training_id": ref, "usecase_id": f"uc-other-{uuid.uuid4()}",
            "model_name": "their-plates", "model_version": "3.0.0",
            "model_type": "object_detection", "runtime": "onnx", "status": "Completed",
            "artifact_s3": f"s3://their-bucket/models/training/their-plates-x/output/model.tar.gz",
            "detection": {"detection_arch": "yolo", "class_names": ["plate"], "num_classes": 1},
        })
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]

    status, body = create(env, detection_body(
        env, base_model={"kind": "training_job", "ref": ref}))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert error == f"Base model training job '{ref}' was not found in this use case"
    assert "their-plates" not in json.dumps(body)


@pytest.mark.parametrize("base_model,expected", [
    ({"kind": "checkpoint", "ref": "x"}, "Invalid base_model.kind 'checkpoint'"),
    ({"kind": "training_job"}, "base_model.ref is required when base_model.kind is 'training_job'"),
    ("yolo11s.pt", "base_model must be an object of the form {kind, ref}"),
])
def test_malformed_base_model_is_400_with_no_side_effects(env, s3_spy, base_model, expected):
    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, detection_body(env, base_model=base_model))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert expected in error


# --- (7) published / omitted -> no BASE_WEIGHTS_* -------------------------

@pytest.mark.parametrize("arch,base_model,expected_ref", [
    ("yolo", None, "yolo11s.pt"),                                    # omitted
    ("yolo", {"kind": "published"}, "yolo11s.pt"),                  # kind only
    ("yolo", {"kind": "published", "ref": "yolo11s.pt"}, "yolo11s.pt"),
    ("rf_detr", None, "small"),
    ("rf_detr", {"kind": "published", "ref": "small"}, "small"),
])
def test_published_base_model_adds_no_base_weights_env(env, both_arch_code_dir, arch, base_model, expected_ref):
    svc = fresh_service()
    overrides = {"detection_arch": arch}
    if base_model is not None:
        overrides["base_model"] = base_model
    status, body = create(env, detection_body(env, **overrides))
    assert status == 201, body
    env_vars = svc.create_training_calls[0]["Environment"]
    assert "BASE_WEIGHTS_S3" not in env_vars and "BASE_WEIGHTS_MEMBER" not in env_vars
    assert "BASE_WEIGHTS_S3" not in svc.create_training_calls[0]["HyperParameters"]
    if arch == "yolo":
        assert env_vars == YOLO_DEFAULT_ENV
    else:
        assert set(env_vars) == RFDETR_ENV_KEYS
    det = env.training_jobs.get_item(Key={"training_id": body["training_id"]})["Item"]["detection"]
    assert det["base_model"] == _published_descriptor(arch, expected_ref)


# --- (9) YOLO without detection_arch / base_model: unchanged -------------

def test_yolo_request_without_arch_or_base_model_is_unchanged(env):
    """Req 8.1: a caller that predates this spec gets the exact
    create_training_job kwargs test_detection_request_launches_script_mode_job
    pinned before the arch/base-model plumbing landed, and the same record —
    plus the one additive `detection.base_model` key Req 6.6 mandates
    (published yolo11s.pt, no weights). Uses the module code dir (no
    RF-DETR files staged), exactly as the pinned test does."""
    svc = fresh_service()
    status, body = create(env, detection_body(env))
    assert status == 201, body
    kw = svc.create_training_calls[0]
    job_name = kw["TrainingJobName"]
    code_s3 = f"s3://{USECASE_BUCKET}/models/detection-training/{job_name}/sourcedir.tar.gz"

    assert set(kw) == CREATE_TRAINING_JOB_KWARGS
    assert kw["HyperParameters"] == {
        "sagemaker_program": "train.py",
        "sagemaker_submit_directory": code_s3,
        **YOLO_DEFAULT_ENV,
    }
    assert kw["Environment"] == YOLO_DEFAULT_ENV
    assert kw["AlgorithmSpecification"] == {
        "TrainingImage": (f"763104351884.dkr.ecr.{REGION}.amazonaws.com/"
                          "pytorch-training:2.5.1-gpu-py311-cu124-ubuntu22.04-sagemaker"),
        "TrainingInputMode": "File",
        "MetricDefinitions": env.training.DETECTION_METRIC_DEFINITIONS,
    }
    assert kw["ResourceConfig"] == {
        "InstanceType": "ml.g4dn.xlarge", "InstanceCount": 1, "VolumeSizeInGB": 60}
    assert kw["StoppingCondition"] == {"MaxRuntimeInSeconds": 10800}
    assert kw["EnableNetworkIsolation"] is False
    assert kw["RoleArn"] == f"arn:aws:iam::{ACCOUNT_ID}:role/DDASageMakerExecutionRole"
    assert kw["OutputDataConfig"] == {"S3OutputPath": f"s3://{USECASE_BUCKET}/models/training/{job_name}"}
    assert [t["Key"] for t in kw["Tags"]] == ["UseCase", "ModelName", "ModelVersion", "CreatedBy"]
    members, _contents = _sourcedir_members(env, kw)
    assert members == YOLO_SOURCEDIR_MEMBERS

    item = env.training_jobs.get_item(Key={"training_id": body["training_id"]})["Item"]
    assert set(item.keys()) == LFV_ITEM_KEYS | {"runtime", "detection"}
    assert item["runtime"] == "onnx" and "source" not in item
    det = item["detection"]
    assert set(det) == YOLO_RECORD_KEYS | {"base_model"}
    assert {k: det[k] for k in YOLO_RECORD_KEYS} == {
        "detection_arch": "yolo",
        "network_input_width": 1280, "network_input_height": 1280,
        "class_names": ["blue_plate"], "num_classes": 1,
        "score_threshold": Decimal("0.25"), "iou_threshold": Decimal("0.45"),
        "preserve_aspect": True,
        "imgsz": 1280, "epochs": 100, "batch": 4, "base_weights": "yolo11s.pt",
        "patience": 30, "onnx_opset": 17,
        "sourcedir_s3": code_s3,
    }
    assert det["base_model"] == _published_descriptor("yolo", "yolo11s.pt")
    assert "top_k" not in det and "rfdetr_size" not in det and "resolution" not in det
