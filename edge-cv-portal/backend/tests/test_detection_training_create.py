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
