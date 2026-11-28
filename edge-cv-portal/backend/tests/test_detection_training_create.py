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
3. RF-DETR-Seg path (rfdetr-seg-support Group 5, section 5 below) —
   `model_type='instance_segmentation'` launches train_rfdetr_seg.py with the
   Seg env and MetricDefinitions, persists the Seg record (mask_threshold,
   top_k, output_mode, no iou_threshold), every 400 of Req 5.1 / 5.2 / 5.4 /
   5.5 and the Seg_Switch 403 before the access check (Req 15.2).
4. Manifest first line (section 6 below) — both validators read the whole
   first line, not a 10,240-byte prefix: an Instance_Manifest whose line 1 is
   over 10 KB launches, and a first line over the 16 MiB cap is a 400 naming
   the limit.

Fixture conventions follow test_onnx_compile_diagnostics_units.py (module env
on the moto aws_stack, FakeSageMakerService recording stub, own table/bucket).

# Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.4, 2.5, 3.1, 3.3, 3.4, 3.6,
# 3.7, 3.8, 3.10, 4.1, 4.2, 4.3, 9.2
# rfdetr-seg-support: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.7, 15.2, 15.3, 17.1
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
INSTANCE_MANIFEST_KEY = "labeled/labeling-inst/output.manifest"
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


def _instance_line(bucket):
    """An Instance_Manifest line (rfdetr-seg-support Req 3.1): an 8x4 image
    with one 2x2 instance at (1, 1); the rle is its Canonical_RLE."""
    return json.dumps({
        "source-ref": f"s3://{bucket}/{IMAGE_KEY}",
        "instance-segmentation": {
            "image_size": [{"width": 8, "height": 4, "depth": 3}],
            "annotations": [{"class_id": 0, "left": 1, "top": 1, "width": 2,
                             "height": 2, "rle": "5 2 2 2 21"}],
        },
        "instance-segmentation-metadata": {
            "objects": [{"confidence": 1.0}],
            "class-map": {"0": "blue_plate"},
            "type": "groundtruth/instance-segmentation",
            "human-annotated": "yes",
            "creation-date": "2026-11-22T00:00:00",
            "job-name": "labeling-inst",
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
    s3.put_object(Bucket=USECASE_BUCKET, Key=INSTANCE_MANIFEST_KEY,
                  Body=(_instance_line(USECASE_BUCKET) + "\n").encode())

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


INSTANCE_MISMATCH_ERROR = (
    "Manifest is an instance-segmentation manifest; it trains RF-DETR-Seg "
    "(model_type instance_segmentation), not the Marketplace "
    "classification/segmentation model.")


@pytest.mark.parametrize("model_type", ["segmentation", "classification"])
def test_lfv_rejects_an_instance_manifest_naming_instance_segmentation(env, model_type):
    """rfdetr-seg-support Req 3.2 / 3.6 (task 3.3): an Instance_Manifest offered
    to the Marketplace path gets the validator's early-return 400 naming
    model_type instance_segmentation -- not the Ground Truth "requires
    transformation" dead end -- and no SageMaker call."""
    svc = fresh_service()
    status, body = create(env, lfv_body(
        env, model_type=model_type,
        dataset_manifest_s3=f"s3://{USECASE_BUCKET}/{INSTANCE_MANIFEST_KEY}"))

    assert status == 400, body
    assert body["error"] == "Manifest validation failed"
    assert body["details"] == [INSTANCE_MISMATCH_ERROR]
    assert "instance_segmentation" in body["details"][0]
    assert body["detected_attributes"] == [
        "source-ref", "instance-segmentation", "instance-segmentation-metadata"]
    assert body["required_attributes"] == []
    assert "transformation_needed" not in body
    assert svc.create_training_calls == []


@pytest.mark.parametrize("model_type", ["segmentation", "classification"])
def test_validate_marketplace_manifest_early_returns_on_an_instance_manifest(env, model_type):
    """The validator's own result has the task-type-mismatch shape: no
    transformation_needed, no suggestion, no field mapping (design section 5)."""
    usecase = {
        "usecase_id": env.usecase_id, "account_id": ACCOUNT_ID,
        "s3_bucket": USECASE_BUCKET, "region": REGION,
        "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:root",
    }

    result = env.training.validate_marketplace_manifest(
        f"s3://{USECASE_BUCKET}/{INSTANCE_MANIFEST_KEY}", usecase, model_type)

    assert result == {
        "valid": False,
        "errors": [INSTANCE_MISMATCH_ERROR],
        "message": "Task type mismatch: manifest is instance segmentation",
        "detected_attributes": [
            "source-ref", "instance-segmentation", "instance-segmentation-metadata"],
    }

    # The existing paths are untouched: a classification manifest validates,
    # a bounding-box manifest still takes the transformation branch.
    ok = env.training.validate_marketplace_manifest(
        f"s3://{USECASE_BUCKET}/{CLASSIFICATION_MANIFEST_KEY}", usecase, "classification")
    assert ok["valid"] is True
    bbox = env.training.validate_marketplace_manifest(
        f"s3://{USECASE_BUCKET}/{DETECTION_MANIFEST_KEY}", usecase, model_type)
    assert bbox["valid"] is False and bbox["transformation_needed"] is True


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


# ---------------------------------------------------------------------------
# 5. RF-DETR-Seg request shape, 400s, base-model matrix and the Seg_Switch
#    (rfdetr-seg-support tasks 5.1-5.3; Req 5.1-5.5, 5.7, 15.2, 15.3, 17.1).
#    The LFV / YOLO / RF-DETR cases above are the preservation pins and are
#    unmodified; `env.training.DETECTION_METRIC_DEFINITIONS` is still read
#    off the handler module by test_yolo_request_without_arch_or_base_model_is_unchanged.
# ---------------------------------------------------------------------------

# Exactly what train_rfdetr_seg.py reads (detection_job_environment for the
# Seg arch): RFDETR_SEG_SIZE instead of RFDETR_SIZE, no IOU, no SCORE, no MASK.
SEG_ENV_KEYS = {"MANIFEST_S3", "RFDETR_SEG_SIZE", "RESOLUTION", "EPOCHS", "BATCH",
                "GRAD_ACCUM", "LR", "PATIENCE", "ONNX_OPSET"}
# The Seg Detection_Record_Fields (design "Portal records"): the RF-DETR set
# with rfdetr_seg_size for rfdetr_size, plus mask_threshold and output_mode.
SEG_RECORD_KEYS = {
    "detection_arch", "network_input_width", "network_input_height", "class_names",
    "num_classes", "score_threshold", "mask_threshold", "top_k", "preserve_aspect",
    "rfdetr_seg_size", "resolution", "epochs", "batch", "grad_accum", "lr", "patience",
    "onnx_opset", "sourcedir_s3", "output_mode",
}
DETECTION_METRIC_NAMES = {"test:mAP50", "test:mAP50-95", "test:precision", "test:recall"}
SEG_METRIC_NAMES = DETECTION_METRIC_NAMES | {"test:mask-mAP50", "test:mask-mAP50-95"}
SEG_SOURCEDIR_MEMBERS = ["dedupe_frames.py", "manifest_to_detector_dataset.py",
                         "requirements.txt", "train_rfdetr.py", "train_rfdetr_seg.py"]
SEG_DISABLED_ERROR = "RF-DETR-Seg is disabled on this portal"
SEMANTIC_MANIFEST_KEY = "labeled/semantic/output.manifest"


def _semantic_line(bucket):
    """An LFV semantic-segmentation line (anomaly-mask-ref), the manifest
    validate_detection_manifest_entry(expected='instance') names precisely."""
    return json.dumps({
        "source-ref": f"s3://{bucket}/{IMAGE_KEY}",
        "anomaly-label": 1,
        "anomaly-label-metadata": {
            "class-name": "anomaly", "confidence": 1.0,
            "type": "groundtruth/image-classification", "job-name": "j",
            "human-annotated": "yes", "creation-date": "2026-09-12T00:00:00",
        },
        "anomaly-mask-ref": f"s3://{bucket}/masks/frame_0001.png",
        "anomaly-mask-ref-metadata": {
            "internal-color-map": {"0": {"class-name": "BACKGROUND", "hex-color": "#ffffff"}},
            "type": "groundtruth/semantic-segmentation", "job-name": "j",
            "human-annotated": "yes", "creation-date": "2026-09-12T00:00:00",
        },
    })


@pytest.fixture
def all_arch_code_dir(env, monkeypatch):
    """Stage a code dir holding the THREE entry points flat, the way the CDK
    TrainingHandler asset does: the real repo files (train_rfdetr_seg.py
    imports train_rfdetr.py as a sibling, so build_sourcedir_tarball bundles
    both for the Seg arch). No `_common.py`, so the YOLO tarball keeps its
    pinned four members."""
    code_dir = tempfile.mkdtemp(prefix="dda-all-arch-code-")
    for rel in ("datasets/detection_training/train.py",
                "datasets/detection_training/requirements.txt",
                "datasets/detection_training/train_rfdetr.py",
                "datasets/detection_training/requirements-rfdetr.txt",
                "datasets/detection_training/train_rfdetr_seg.py",
                "datasets/manifest_to_detector_dataset.py",
                "datasets/dedupe_frames.py"):
        shutil.copy(os.path.join(_REPO_ROOT, rel), code_dir)
    monkeypatch.setenv("DETECTION_TRAINING_CODE_DIR", code_dir)
    yield code_dir
    shutil.rmtree(code_dir, ignore_errors=True)


@pytest.fixture
def seg_switch(monkeypatch):
    """Set the Seg_Switch for one test ('false' = OFF, 'true' = ON, None =
    absent, which rfdetr_seg_enabled reads as ON)."""
    def _set(value):
        if value is None:
            monkeypatch.delenv("PORTAL_RFDETR_SEG_ENABLED", raising=False)
        else:
            monkeypatch.setenv("PORTAL_RFDETR_SEG_ENABLED", value)
    _set(None)
    return _set


def seg_body(env, **overrides):
    body = {
        "usecase_id": env.usecase_id,
        "model_source": "marketplace",   # ignored for the detection model types
        "model_name": "blue-plate-seg",
        "model_version": "1.0.0",
        "model_type": "instance_segmentation",
        "dataset_manifest_s3": f"s3://{USECASE_BUCKET}/{INSTANCE_MANIFEST_KEY}",
    }
    body.update(overrides)
    return body


def _record(env, training_id):
    return env.training_jobs.get_item(Key={"training_id": training_id})["Item"]


def _audit_details(aws_stack, training_id):
    entries = [e for e in aws_stack.tables.audit_log.scan()["Items"]
               if e.get("resource_id") == training_id and e.get("action") == "create_training_job"]
    assert len(entries) == 1, entries
    return entries[0]["details"]


def _completed_seg_base_job(env, svc):
    """A Completed instance_segmentation job of this use case with its
    artifact_s3 persisted by GET — the record a Seg `training_job` base
    resolves against. Returns (training_id, artifact_s3)."""
    status, created = create(env, seg_body(env, model_name="base-rf-detr-seg", model_version="1.0.0"))
    assert status == 201, created
    _complete(svc, created["training_job_name"])
    status, job = get(env, created["training_id"])
    assert status == 200 and job["status"] == "Completed", job
    assert job["model_type"] == "instance_segmentation"
    assert job["artifact_s3"].endswith("/output/model.tar.gz")
    return created["training_id"], job["artifact_s3"]


# --- (1) request shape: entry point, env, MetricDefinitions, sourcedir, record

def test_seg_request_shape_env_sourcedir_record_and_audit(env, aws_stack, all_arch_code_dir):
    """Req 5.1 / 5.3 / 5.5: the Seg entry point, ITS env (RFDETR_SEG_SIZE, no
    IOU, no thresholds), the Seg MetricDefinitions list, a sourcedir carrying
    train_rfdetr.py as a sibling with requirements-rfdetr.txt renamed, and
    the Seg record (model_type instance_segmentation, mask_threshold, top_k
    = the size's query count, output_mode, no iou_threshold); the audit
    entry names the model type too."""
    svc = fresh_service()
    status, body = create(env, seg_body(
        env, detection_arch="rf_detr_seg", output_mode="instance",
        hyperparameters={"rfdetr_seg_size": "medium", "resolution": 432, "epochs": 50,
                         "batch": 2, "grad_accum": 8, "lr": 0.0002, "patience": 5,
                         "score_threshold": 0.4, "mask_threshold": 0.6},
    ))
    assert status == 201, body
    assert set(body) == {"training_id", "training_job_name", "training_job_arn", "status", "message"}
    assert len(svc.create_training_calls) == 1
    kw = svc.create_training_calls[0]
    assert set(kw) == CREATE_TRAINING_JOB_KWARGS
    # Entry point + env (passed both as HyperParameters and Environment).
    assert kw["HyperParameters"]["sagemaker_program"] == "train_rfdetr_seg.py"
    env_vars = kw["Environment"]
    assert env_vars == {
        "MANIFEST_S3": f"s3://{USECASE_BUCKET}/{INSTANCE_MANIFEST_KEY}",
        "RFDETR_SEG_SIZE": "medium", "RESOLUTION": "432", "EPOCHS": "50", "BATCH": "2",
        "GRAD_ACCUM": "8", "LR": "0.0002", "PATIENCE": "5", "ONNX_OPSET": "17",
    }
    assert set(env_vars) == SEG_ENV_KEYS
    assert not any("IOU" in k or "SCORE" in k or "MASK" in k or "IMGSZ" in k for k in env_vars)
    assert "RFDETR_SIZE" not in env_vars
    assert "BASE_WEIGHTS_S3" not in env_vars and "BASE_WEIGHTS_MEMBER" not in env_vars
    for k, v in env_vars.items():
        assert kw["HyperParameters"][k] == v
    assert kw["EnableNetworkIsolation"] is False
    assert kw["ResourceConfig"] == {
        "InstanceType": "ml.g4dn.xlarge", "InstanceCount": 1, "VolumeSizeInGB": 60}
    assert kw["StoppingCondition"] == {"MaxRuntimeInSeconds": 10800}
    assert "InputDataConfig" not in kw
    # MetricDefinitions = METRIC_DEFINITIONS_FOR_ARCH['rf_detr_seg'] (Req 4.7):
    # the detection four plus the two mask-mAP regexes.
    metric_defs = kw["AlgorithmSpecification"]["MetricDefinitions"]
    assert metric_defs is env.training.METRIC_DEFINITIONS_FOR_ARCH["rf_detr_seg"]
    assert {m["Name"] for m in metric_defs} == SEG_METRIC_NAMES
    assert metric_defs[:4] == env.training.DETECTION_METRIC_DEFINITIONS
    # Sourcedir: the Seg entry point, its sibling train_rfdetr.py, and
    # requirements-rfdetr.txt renamed to requirements.txt; no train.py.
    members, contents = _sourcedir_members(env, kw)
    assert members == SEG_SOURCEDIR_MEMBERS
    for name, src in (("requirements.txt", "requirements-rfdetr.txt"),
                      ("train_rfdetr_seg.py", "train_rfdetr_seg.py"),
                      ("train_rfdetr.py", "train_rfdetr.py")):
        with open(os.path.join(all_arch_code_dir, src), "rb") as fh:
            assert contents[name] == fh.read(), name
    # Record.
    item = _record(env, body["training_id"])
    assert set(item.keys()) == LFV_ITEM_KEYS | {"runtime", "detection"}
    assert item["model_type"] == "instance_segmentation" and item["runtime"] == "onnx"
    assert "source" not in item
    det = item["detection"]
    assert set(det) == SEG_RECORD_KEYS | {"base_model"}
    assert det["detection_arch"] == "rf_detr_seg"
    assert det["rfdetr_seg_size"] == "medium"
    assert det["resolution"] == 432
    assert det["network_input_width"] == 432 and det["network_input_height"] == 432
    assert det["preserve_aspect"] is False
    assert det["score_threshold"] == Decimal("0.4")
    assert det["mask_threshold"] == Decimal("0.6")
    assert det["top_k"] == 200                       # RFDETR_SEG_QUERIES_FOR_SIZE['medium']
    assert det["output_mode"] == "instance"
    assert det["epochs"] == 50 and det["batch"] == 2 and det["patience"] == 5
    assert det["grad_accum"] == 8 and det["lr"] == Decimal("0.0002") and det["onnx_opset"] == 17
    assert det["class_names"] == ["blue_plate"] and det["num_classes"] == 1
    assert det["sourcedir_s3"] == kw["HyperParameters"]["sagemaker_submit_directory"]
    assert "iou_threshold" not in det
    assert "rfdetr_size" not in det and "imgsz" not in det and "base_weights" not in det
    assert det["base_model"] == _published_descriptor("rf_detr_seg", "medium")
    # Audit entry (C33): the model type the handler was given.
    details = _audit_details(aws_stack, body["training_id"])
    assert details["model_type"] == "instance_segmentation"
    assert details["detection_arch"] == "rf_detr_seg" and details["imgsz"] == 432


def test_seg_detection_arch_defaults_and_resolution_native(env, all_arch_code_dir):
    """Req 5.1 / 5.2 / 5.5: `detection_arch` absent -> rf_detr_seg; no
    hyperparameters -> size small at its native 384, batch 4 x grad_accum 4,
    thresholds 0.5, top_k 100, output_mode 'instance', published base 'small'."""
    svc = fresh_service()
    status, body = create(env, seg_body(env))
    assert status == 201, body
    kw = svc.create_training_calls[0]
    assert kw["HyperParameters"]["sagemaker_program"] == "train_rfdetr_seg.py"
    assert kw["Environment"] == {
        "MANIFEST_S3": f"s3://{USECASE_BUCKET}/{INSTANCE_MANIFEST_KEY}",
        "RFDETR_SEG_SIZE": "small", "RESOLUTION": "384", "EPOCHS": "100", "BATCH": "4",
        "GRAD_ACCUM": "4", "LR": "0.0001", "PATIENCE": "10", "ONNX_OPSET": "17",
    }
    det = _record(env, body["training_id"])["detection"]
    assert det["detection_arch"] == "rf_detr_seg" and det["rfdetr_seg_size"] == "small"
    assert det["resolution"] == 384 and det["network_input_width"] == 384
    assert det["score_threshold"] == Decimal("0.5") and det["mask_threshold"] == Decimal("0.5")
    assert det["top_k"] == 100 and det["output_mode"] == "instance"
    assert det["base_model"] == _published_descriptor("rf_detr_seg", "small")


@pytest.mark.parametrize("size,native,top_k", [
    ("nano", 312, 100), ("small", 384, 100), ("medium", 432, 200),
    ("large", 504, 200), ("xlarge", 624, 300), ("2xlarge", 768, 300),
])
def test_seg_every_size_resolves_native_resolution_and_query_count(env, all_arch_code_dir, size, native, top_k):
    """D4: all six Seg sizes launch; the record's top_k is the size's decoder
    query count and the published base ref is the size."""
    svc = fresh_service()
    status, body = create(env, seg_body(env, hyperparameters={"rfdetr_seg_size": size}))
    assert status == 201, body
    assert svc.create_training_calls[0]["Environment"]["RFDETR_SEG_SIZE"] == size
    assert svc.create_training_calls[0]["Environment"]["RESOLUTION"] == str(native)
    det = _record(env, body["training_id"])["detection"]
    assert det["top_k"] == top_k and det["resolution"] == native
    assert det["base_model"]["ref"] == size


def test_seg_nano_accepts_a_multiple_of_12(env, all_arch_code_dir):
    """Req 5.2: the resolution rule is the selected size's block (12 for
    nano), never 32 — 324 launches for nano."""
    svc = fresh_service()
    status, body = create(env, seg_body(
        env, hyperparameters={"rfdetr_seg_size": "nano", "resolution": 324}))
    assert status == 201, body
    assert svc.create_training_calls[0]["Environment"]["RESOLUTION"] == "324"
    det = _record(env, body["training_id"])["detection"]
    assert det["resolution"] == 324 and det["network_input_width"] == 324


@pytest.mark.parametrize("requested,stored", [
    ("instance", "instance"), ("semantic", "semantic"), ("SEMANTIC", "semantic"),
    (None, "instance"), ("", "instance"),
])
def test_seg_output_mode_is_a_top_level_field_persisted_on_the_record(env, all_arch_code_dir, requested, stored):
    """Req 5.5: `output_mode` is read from the request's top level (default
    'instance' when absent or empty, case-insensitive) and persisted; it
    never reaches the env."""
    svc = fresh_service()
    overrides = {} if requested is None else {"output_mode": requested}
    status, body = create(env, seg_body(env, **overrides))
    assert status == 201, body
    assert "OUTPUT_MODE" not in svc.create_training_calls[0]["Environment"]
    assert _record(env, body["training_id"])["detection"]["output_mode"] == stored


def test_seg_instance_type_and_max_runtime_stay_request_fields(env, all_arch_code_dir):
    """Req 5.3: the instance type is the request's (default
    DETECTION_DEFAULT_INSTANCE_TYPE, asserted above) — no per-size backend map."""
    svc = fresh_service()
    status, body = create(env, seg_body(
        env, instance_type="ml.g5.xlarge", max_runtime_seconds=7200,
        hyperparameters={"rfdetr_seg_size": "2xlarge"}))
    assert status == 201, body
    kw = svc.create_training_calls[0]
    assert kw["ResourceConfig"]["InstanceType"] == "ml.g5.xlarge"
    assert kw["StoppingCondition"] == {"MaxRuntimeInSeconds": 7200}
    assert _record(env, body["training_id"])["instance_type"] == "ml.g5.xlarge"


# --- (2) model_type <-> detection_arch cross-check (Req 5.1) -----------------

@pytest.mark.parametrize("arch", ["rf_detr", "yolo"])
def test_seg_model_type_with_a_detection_arch_is_400_with_no_side_effects(env, s3_spy, arch):
    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, seg_body(env, detection_arch=arch))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert error == "instance_segmentation requires detection_arch rf_detr_seg"


def test_seg_arch_with_object_detection_model_type_is_400_with_no_side_effects(env, s3_spy):
    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, detection_body(env, detection_arch="rf_detr_seg"))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert error == "detection_arch rf_detr_seg requires model_type instance_segmentation"


def test_seg_unknown_detection_arch_is_still_the_vocabulary_400(env, s3_spy):
    """An unknown arch on a Seg request gets normalize_detection_arch's
    message (naming rf_detr_seg), not the cross-check text."""
    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, seg_body(env, detection_arch="mask-rcnn"))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert error.startswith("Invalid detection_arch 'mask-rcnn'")
    assert "rf_detr_seg" in error


# --- (3) output_mode 400s (Req 5.5) -----------------------------------------

@pytest.mark.parametrize("value", ["panoptic", "both", 7])
def test_seg_output_mode_outside_the_vocabulary_is_400_with_no_side_effects(env, s3_spy, value):
    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, seg_body(env, output_mode=value))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert error == f"Invalid output_mode '{value}': expected one of instance, semantic"


@pytest.mark.parametrize("arch,mode", [("yolo", "instance"), ("rf_detr", "semantic")])
def test_output_mode_on_a_detection_request_is_400_with_no_side_effects(env, s3_spy, arch, mode):
    """A YOLO / RF-DETR request cannot carry a silent no-op output_mode."""
    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, detection_body(env, detection_arch=arch, output_mode=mode))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert error == "output_mode applies to instance_segmentation only"


# --- (4) hyperparameter 400s (Req 5.2) --------------------------------------

@pytest.mark.parametrize("hp,field", [
    ({"rfdetr_seg_size": "tiny"}, "rfdetr_seg_size"),
    ({"resolution": 320}, "resolution"),                       # not a multiple of 24 (small)
    ({"rfdetr_seg_size": "small", "resolution": 324}, "resolution"),  # 12-multiple, not 24
    ({"resolution": 1176}, "resolution"),                      # above 1152
    ({"mask_threshold": 1.0}, "mask_threshold"),
    ({"mask_threshold": 0}, "mask_threshold"),
    ({"score_threshold": 1.5}, "score_threshold"),
    ({"grad_accum": 0}, "grad_accum"),
    ({"lr": 1.0}, "lr"),
    ({"iou_threshold": 0.5}, "iou_threshold"),                 # no IoU on a set-based decoder
    ({"rfdetr_size": "small"}, "rfdetr_size"),                 # the detection size knob
    ({"imgsz": 640}, "imgsz"),                                 # the YOLO knob
])
def test_seg_bad_hyperparameter_is_400_with_no_side_effects(env, s3_spy, hp, field):
    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, seg_body(env, hyperparameters=hp))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert field in error


def test_seg_resolution_message_names_the_block_and_the_seg_bounds(env, s3_spy):
    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, seg_body(env, hyperparameters={"resolution": 500}))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert error == ("Invalid hyperparameter 'resolution': must be a multiple of 24 "
                     "between 240 and 1152 for RF-DETR-Seg small")


# --- (5) manifest kind (Req 3.5 / 5.1) --------------------------------------

def test_seg_request_with_a_bbox_manifest_is_400_with_no_side_effects(env, s3_spy):
    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, seg_body(
        env, dataset_manifest_s3=f"s3://{USECASE_BUCKET}/{DETECTION_MANIFEST_KEY}"))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert error == "RF-DETR-Seg requires an instance-segmentation manifest"
    assert body["message"] == "Manifest is not an instance-segmentation manifest"
    assert body["details"] == ["RF-DETR-Seg needs instance masks; this manifest has boxes only"]
    assert body["detected_attributes"] == ["source-ref", "bounding-box", "bounding-box-metadata"]
    assert "Manifest Transformer" not in json.dumps(body)


def test_seg_request_with_a_semantic_manifest_is_400_naming_anomaly_mask_ref(env, s3_spy):
    svc = fresh_service()
    env.s3.put_object(Bucket=USECASE_BUCKET, Key=SEMANTIC_MANIFEST_KEY,
                      Body=(_semantic_line(USECASE_BUCKET) + "\n").encode())
    s3_spy.writes.clear()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, seg_body(
        env, dataset_manifest_s3=f"s3://{USECASE_BUCKET}/{SEMANTIC_MANIFEST_KEY}"))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert error == "RF-DETR-Seg requires an instance-segmentation manifest"
    assert body["details"] == [
        "Manifest is a semantic segmentation manifest (anomaly-mask-ref); "
        "RF-DETR-Seg requires an instance-segmentation manifest"]
    assert "Manifest Transformer" not in json.dumps(body)


def test_detection_request_with_an_instance_manifest_keeps_the_bbox_400(env, s3_spy):
    """The reverse: an RF-DETR (object_detection) request offered the
    Instance_Manifest gets the unchanged bounding-box 400 (expected='bbox'
    is byte-identical to the pre-Seg validator)."""
    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, detection_body(
        env, detection_arch="rf_detr",
        dataset_manifest_s3=f"s3://{USECASE_BUCKET}/{INSTANCE_MANIFEST_KEY}"))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert error == "Object Detection requires a bounding-box manifest"
    assert body["message"] == "Manifest is not an Object Detection manifest"
    assert body["detected_attributes"] == [
        "source-ref", "instance-segmentation", "instance-segmentation-metadata"]


def test_validate_detection_manifest_expected_kinds(env):
    """The handler-side validator forwards `expected` to the shared entry
    check: 'instance' accepts the Instance_Manifest and names the model
    type; the default is the pre-Seg bbox behaviour, message included."""
    usecase = {
        "usecase_id": env.usecase_id, "account_id": ACCOUNT_ID,
        "s3_bucket": USECASE_BUCKET, "region": REGION,
        "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:root",
    }
    instance = env.training.validate_detection_manifest(
        f"s3://{USECASE_BUCKET}/{INSTANCE_MANIFEST_KEY}", usecase, expected="instance")
    assert instance["valid"] is True, instance
    assert instance["message"] == "Manifest is valid for instance_segmentation training"
    assert instance["class_names"] == ["blue_plate"]
    assert instance["attribute"] == "instance-segmentation"
    bbox = env.training.validate_detection_manifest(
        f"s3://{USECASE_BUCKET}/{DETECTION_MANIFEST_KEY}", usecase)
    assert bbox["valid"] is True
    assert bbox["message"] == "Manifest is valid for object_detection training"
    assert bbox["attribute"] == "bounding-box"
    crossed = env.training.validate_detection_manifest(
        f"s3://{USECASE_BUCKET}/{INSTANCE_MANIFEST_KEY}", usecase)
    assert crossed["valid"] is False
    assert crossed["message"] == "Manifest is not an Object Detection manifest"


# --- (6) base model (Req 5.4) -----------------------------------------------

def test_seg_base_model_from_a_completed_seg_job_uses_the_rfdetr_checkpoint_member(env, all_arch_code_dir):
    svc = fresh_service()
    base_id, artifact_s3 = _completed_seg_base_job(env, svc)
    status, body = create(env, seg_body(env, base_model={"kind": "training_job", "ref": base_id}))
    assert status == 201, body
    kw = svc.create_training_calls[1]
    assert kw["HyperParameters"]["sagemaker_program"] == "train_rfdetr_seg.py"
    assert kw["Environment"]["BASE_WEIGHTS_S3"] == artifact_s3
    assert kw["Environment"]["BASE_WEIGHTS_MEMBER"] == "checkpoint_best_total.pth"
    assert set(kw["Environment"]) == SEG_ENV_KEYS | {"BASE_WEIGHTS_S3", "BASE_WEIGHTS_MEMBER"}
    det = _record(env, body["training_id"])["detection"]
    assert det["base_model"] == {
        "kind": "training_job", "ref": base_id, "weights_s3": artifact_s3,
        "member": "checkpoint_best_total.pth", "detection_arch": "rf_detr_seg",
        "class_names": ["blue_plate"],
    }


@pytest.mark.parametrize("base_arch,request_arch,expected", [
    ("rf_detr", "rf_detr_seg", "is a rf_detr detector; cannot start an rf_detr_seg run from it"),
    ("yolo", "rf_detr_seg", "is a yolo detector; cannot start an rf_detr_seg run from it"),
    ("rf_detr_seg", "rf_detr", "is a rf_detr_seg detector; cannot start an rf_detr run from it"),
    ("rf_detr_seg", "yolo", "is a rf_detr_seg detector; cannot start a yolo run from it"),
])
def test_seg_base_model_arch_mismatch_is_400_naming_both_archs(
        env, all_arch_code_dir, s3_spy, base_arch, request_arch, expected):
    """A detection checkpoint offered as a Seg base, or the reverse, is a 400
    naming both archs, with nothing staged for the rejected request."""
    svc = fresh_service()
    if base_arch == "rf_detr_seg":
        base_id, _artifact = _completed_seg_base_job(env, svc)
        base_name = "base-rf-detr-seg v1.0.0"
    else:
        base_id, _artifact = _completed_base_job(env, svc, arch=base_arch)
        base_name = f"base-{base_arch.replace('_', '-')} v1.0.0"
    s3_spy.writes.clear()          # the base job legitimately staged its own sourcedir
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    base_model = {"kind": "training_job", "ref": base_id}
    if request_arch == "rf_detr_seg":
        request = seg_body(env, base_model=base_model)
    else:
        request = detection_body(env, detection_arch=request_arch, base_model=base_model)
    status, body = create(env, request)
    error = _assert_rejected_without_side_effects(
        env, svc, s3_spy, status, body, n_before, expected_calls=1)
    assert base_name in error
    assert expected in error


def test_seg_base_model_from_an_imported_seg_checkpoint(env, all_arch_code_dir, s3_spy):
    """`imported` -> an Imported_Model whose metadata.fine_tunable.arch is
    rf_detr_seg (its bare checkpoint, no member); a detection import is a 400
    naming both archs."""
    svc = fresh_service()
    seg_ref, det_ref = str(uuid.uuid4()), str(uuid.uuid4())
    checkpoint_s3 = f"s3://{USECASE_BUCKET}/models/imported/{seg_ref}/checkpoint.pth"
    env.training_jobs.put_item(Item={
        "training_id": seg_ref, "usecase_id": env.usecase_id, "source": "imported",
        "model_name": "seg-import", "model_version": "0.1.0",
        "model_type": "instance_segmentation", "status": "Completed",
        "metadata": {"fine_tunable": {"arch": "rf_detr_seg", "checkpoint_s3": checkpoint_s3,
                                      "class_names": ["plate", "cup"]}},
        "detection": {"detection_arch": "rf_detr_seg", "class_names": ["plate", "cup"]},
    })
    env.training_jobs.put_item(Item={
        "training_id": det_ref, "usecase_id": env.usecase_id, "source": "imported",
        "model_name": "det-import", "model_version": "0.1.0",
        "model_type": "object_detection", "status": "Completed",
        "metadata": {"fine_tunable": {"arch": "rf_detr",
                                      "checkpoint_s3": f"s3://{USECASE_BUCKET}/models/imported/{det_ref}/c.pth"}},
    })
    status, body = create(env, seg_body(env, base_model={"kind": "imported", "ref": seg_ref}))
    assert status == 201, body
    kw = svc.create_training_calls[0]
    assert kw["Environment"]["BASE_WEIGHTS_S3"] == checkpoint_s3
    assert "BASE_WEIGHTS_MEMBER" not in kw["Environment"]
    det = _record(env, body["training_id"])["detection"]
    assert det["base_model"] == {
        "kind": "imported", "ref": seg_ref, "weights_s3": checkpoint_s3, "member": None,
        "detection_arch": "rf_detr_seg", "class_names": ["plate", "cup"],
    }
    # The manifest's class names win on the record; the base's are kept on the descriptor.
    assert det["class_names"] == ["blue_plate"]
    s3_spy.writes.clear()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, seg_body(env, base_model={"kind": "imported", "ref": det_ref}))
    error = _assert_rejected_without_side_effects(
        env, svc, s3_spy, status, body, n_before, expected_calls=1)
    assert "det-import v0.1.0 is a rf_detr model; cannot start an rf_detr_seg run from it" in error


def test_seg_base_model_outside_this_usecase_is_400_with_no_leak(env, s3_spy):
    """Cross-use-case refs are rejected as today: one message, no existence leak."""
    svc = fresh_service()
    ref = str(uuid.uuid4())
    env.training_jobs.put_item(Item={
        "training_id": ref, "usecase_id": f"uc-other-{uuid.uuid4()}",
        "model_name": "their-seg", "model_version": "3.0.0",
        "model_type": "instance_segmentation", "runtime": "onnx", "status": "Completed",
        "artifact_s3": "s3://their-bucket/models/training/their-seg-x/output/model.tar.gz",
        "detection": {"detection_arch": "rf_detr_seg", "class_names": ["plate"], "num_classes": 1},
    })
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, seg_body(env, base_model={"kind": "training_job", "ref": ref}))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert error == f"Base model training job '{ref}' was not found in this use case"
    assert "their-seg" not in json.dumps(body)


@pytest.mark.parametrize("base_model,expected_ref", [
    (None, "small"),
    ({"kind": "published"}, "small"),
    ({"kind": "published", "ref": "large"}, "large"),
])
def test_seg_published_base_model_adds_no_base_weights_env(env, all_arch_code_dir, base_model, expected_ref):
    svc = fresh_service()
    overrides = {} if base_model is None else {"base_model": base_model}
    status, body = create(env, seg_body(env, **overrides))
    assert status == 201, body
    env_vars = svc.create_training_calls[0]["Environment"]
    assert set(env_vars) == SEG_ENV_KEYS
    assert "BASE_WEIGHTS_S3" not in svc.create_training_calls[0]["HyperParameters"]
    det = _record(env, body["training_id"])["detection"]
    assert det["base_model"] == _published_descriptor("rf_detr_seg", expected_ref)


# --- (7) Seg_Switch: 403 before the access check, both states (Req 15.2 / 15.3)

def _create_as(env, user_id, body):
    response = env.training.create_training_job({
        "httpMethod": "POST", "path": "/api/v1/training", "resource": "/training",
        "body": json.dumps(body),
        "requestContext": {"authorizer": {"claims": {
            "sub": user_id, "email": f"{user_id}@example.com", "cognito:username": user_id}}},
    }, None)
    return response["statusCode"], json.loads(response["body"])


def test_seg_switch_off_is_403_before_validation_with_no_side_effects(env, s3_spy, seg_switch):
    """OFF: the Seg request is refused with the disabled message before any
    body validation — a request that would otherwise fail three 400s (bad
    arch, bad hyperparameters, missing manifest) still gets the 403 — and
    nothing is staged, recorded or launched."""
    seg_switch("false")
    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, seg_body(
        env, detection_arch="yolo", hyperparameters={"imgsz": 7},
        dataset_manifest_s3=f"s3://{USECASE_BUCKET}/labeled/does-not-exist/output.manifest"))
    assert status == 403, body
    assert body == {"error": SEG_DISABLED_ERROR}
    assert svc.create_training_calls == []
    assert s3_spy.writes == []
    assert env.training_jobs.scan(Select="COUNT")["Count"] == n_before


def test_seg_switch_off_precedes_the_access_check(env, aws_stack, seg_switch):
    """A Viewer gets the disabled message when OFF (the switch is read before
    check_user_access) and 'Insufficient permissions' when ON."""
    viewer = f"user-{uuid.uuid4()}"
    aws_stack.tables.user_roles.put_item(Item={
        "user_id": viewer, "usecase_id": env.usecase_id, "role": "Viewer"})
    svc = fresh_service()
    seg_switch("false")
    status, body = _create_as(env, viewer, seg_body(env))
    assert (status, body) == (403, {"error": SEG_DISABLED_ERROR})
    seg_switch("true")
    status, body = _create_as(env, viewer, seg_body(env))
    assert (status, body) == (403, {"error": "Insufficient permissions"})
    assert svc.create_training_calls == []


@pytest.mark.parametrize("value", [None, "true", "TRUE", "1"])
def test_seg_switch_on_or_absent_launches(env, all_arch_code_dir, seg_switch, value):
    """ON (explicit) or absent: the Seg request launches."""
    seg_switch(value)
    svc = fresh_service()
    status, body = create(env, seg_body(env))
    assert status == 201, body
    assert svc.create_training_calls[0]["HyperParameters"]["sagemaker_program"] == "train_rfdetr_seg.py"


@pytest.mark.parametrize("arch", ["yolo", "rf_detr"])
def test_seg_switch_off_leaves_detection_and_lfv_requests_alone(env, both_arch_code_dir, seg_switch, arch):
    """The switch gates only the Seg model type: YOLO / RF-DETR and an LFV
    classification job launch exactly as with the switch ON."""
    seg_switch("false")
    svc = fresh_service()
    status, body = create(env, detection_body(env, detection_arch=arch))
    assert status == 201, body
    assert _record(env, body["training_id"])["model_type"] == "object_detection"
    status, body = create(env, lfv_body(env))
    assert status == 201, body
    assert len(svc.create_training_calls) == 2


def test_seg_switch_off_still_rejects_an_unknown_model_type_first(env, seg_switch):
    """The model-type vocabulary check is the dispatch and runs first: an
    unknown type is the 400 whatever the switch says, and its message lists
    instance_segmentation."""
    seg_switch("false")
    svc = fresh_service()
    status, body = create(env, lfv_body(env, model_type="pose"))
    assert status == 400
    assert "instance_segmentation" in body["error"] and "object_detection" in body["error"]
    assert svc.create_training_calls == []


# --- (8) preservation: the detection archs' MetricDefinitions object -------

@pytest.mark.parametrize("arch", ["yolo", "rf_detr"])
def test_detection_archs_still_send_the_detection_metric_definitions_object(env, both_arch_code_dir, arch):
    """Req 17.1 / 4.7: METRIC_DEFINITIONS_FOR_ARCH maps yolo / rf_detr to the
    DETECTION_METRIC_DEFINITIONS object itself, so the SageMaker request and
    the audit / record model_type are byte-identical to the pre-Seg handler."""
    svc = fresh_service()
    status, body = create(env, detection_body(env, detection_arch=arch))
    assert status == 201, body
    metric_defs = svc.create_training_calls[0]["AlgorithmSpecification"]["MetricDefinitions"]
    assert metric_defs is env.training.DETECTION_METRIC_DEFINITIONS
    assert {m["Name"] for m in metric_defs} == DETECTION_METRIC_NAMES
    item = _record(env, body["training_id"])
    assert item["model_type"] == "object_detection"
    assert "output_mode" not in item["detection"] and "mask_threshold" not in item["detection"]


def test_detection_audit_model_type_is_unchanged(env, aws_stack):
    svc = fresh_service()
    status, body = create(env, detection_body(env))
    assert status == 201, body
    details = _audit_details(aws_stack, body["training_id"])
    assert details["model_type"] == "object_detection"
    assert details["detection_arch"] == "yolo"
    assert svc.create_training_calls[0]["HyperParameters"]["sagemaker_program"] == "train.py"


# ---------------------------------------------------------------------------
# 6. Manifest first line longer than the validators' old 10,240-byte read.
#    An instance-segmentation line carries every instance's full-resolution
#    RLE mask, so line 1 of the car-assembly manifests is ~44 KB; both
#    validators parsed only the first 10,240 bytes and POST /training
#    answered 400 "First manifest line is not valid JSON". They now read to
#    the end of the line, up to MANIFEST_FIRST_LINE_MAX_BYTES (16 MiB).
# ---------------------------------------------------------------------------

LONG_INSTANCE_MANIFEST_KEY = "labeled/labeling-inst-full-res/output.manifest"
AT_CAP_MANIFEST_KEY = "labeled/labeling-inst-at-cap/output.manifest"
OVER_CAP_MANIFEST_KEY = "labeled/labeling-inst-over-cap/output.manifest"
FIRST_LINE_TOO_LONG_ERROR = (
    "First manifest line is longer than the 16 MiB limit (16,777,216 bytes)")


def _usecase(env):
    return {
        "usecase_id": env.usecase_id, "account_id": ACCOUNT_ID,
        "s3_bucket": USECASE_BUCKET, "region": REGION,
        "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:root",
    }


def _rect_rle(image_w, image_h, left, top, width, height):
    """Column-major RLE of a width x height rectangle at (left, top) over the
    whole image, the form of the RLE the labeling tool writes."""
    tail = (image_h - top - height) + (image_w - left - width) * image_h
    counts = ([left * image_h + top] + [height, image_h - height] * (width - 1)
              + [height, tail])
    return " ".join(str(c) for c in counts)


def _full_resolution_instance_line(bucket):
    """Line 1 of a car-assembly-like Instance_Manifest: a 4608x3288 image with
    three instances, each mask a full-resolution RLE (~50 KB in all)."""
    image_w, image_h = 4608, 3288
    boxes = [(0, 2522, 1815, 1532, 1274), (1, 400, 1700, 1400, 1300),
             (2, 1200, 300, 2200, 900)]
    return json.dumps({
        "source-ref": f"s3://{bucket}/{IMAGE_KEY}",
        "instance-segmentation": {
            "image_size": [{"width": image_w, "height": image_h, "depth": 3}],
            "annotations": [
                {"class_id": c, "left": x, "top": y, "width": w, "height": h,
                 "rle": _rect_rle(image_w, image_h, x, y, w, h)}
                for c, x, y, w, h in boxes],
        },
        "instance-segmentation-metadata": {
            "objects": [{"confidence": 1.0}] * len(boxes),
            "class-map": {"0": "front_fender", "1": "rear_fender", "2": "roof"},
            "type": "groundtruth/instance-segmentation",
            "human-annotated": "yes",
            "creation-date": "2026-11-27T16:46:29.418694Z",
            "job-name": "car-assembly-instances-r2",
        },
    })


def _put_full_resolution_manifest(env):
    line = _full_resolution_instance_line(USECASE_BUCKET)
    assert len(line.encode()) > 10240
    env.s3.put_object(Bucket=USECASE_BUCKET, Key=LONG_INSTANCE_MANIFEST_KEY,
                      Body=(line + "\n" + line + "\n").encode())
    return f"s3://{USECASE_BUCKET}/{LONG_INSTANCE_MANIFEST_KEY}"


def _instance_line_of_size(bucket, n_bytes):
    """A valid Instance_Manifest line exactly `n_bytes` long (padded inside
    its metadata, which the entry check ignores)."""
    entry = json.loads(_instance_line(bucket))
    entry["instance-segmentation-metadata"]["padding"] = ""
    entry["instance-segmentation-metadata"]["padding"] = "x" * (n_bytes - len(json.dumps(entry)))
    line = json.dumps(entry)
    assert len(line.encode()) == n_bytes
    return line


def test_seg_request_with_a_first_line_over_10_kb_launches(env, all_arch_code_dir):
    """The car-assembly failure: an Instance_Manifest whose first line is
    longer than 10,240 bytes validates, and the RF-DETR-Seg job launches."""
    manifest = _put_full_resolution_manifest(env)

    result = env.training.validate_detection_manifest(
        manifest, _usecase(env), expected="instance")
    assert result["valid"] is True, result
    assert result["class_names"] == ["front_fender", "rear_fender", "roof"]

    svc = fresh_service()
    status, body = create(env, seg_body(env, dataset_manifest_s3=manifest))
    assert status == 201, body
    assert len(svc.create_training_calls) == 1
    assert _record(env, body["training_id"])["detection"]["class_names"] == [
        "front_fender", "rear_fender", "roof"]


def test_lfv_validator_reads_an_instance_first_line_over_10_kb(env):
    """validate_marketplace_manifest read the same 10,240 bytes: the long
    Instance_Manifest got 'Manifest is not valid JSON Lines format' instead of
    the task-type mismatch that names RF-DETR-Seg."""
    manifest = _put_full_resolution_manifest(env)

    result = env.training.validate_marketplace_manifest(
        manifest, _usecase(env), "segmentation")

    assert result == {
        "valid": False,
        "errors": [INSTANCE_MISMATCH_ERROR],
        "message": "Task type mismatch: manifest is instance segmentation",
        "detected_attributes": [
            "source-ref", "instance-segmentation", "instance-segmentation-metadata"],
    }


def test_first_line_over_the_cap_is_refused_with_a_clear_error(env, s3_spy):
    """A first line one byte over MANIFEST_FIRST_LINE_MAX_BYTES is refused by
    both validators with the limit named, and POST /training answers 400 with
    nothing staged; a first line of exactly the cap still validates."""
    cap = env.training.MANIFEST_FIRST_LINE_MAX_BYTES
    assert cap == 16 * 1024 * 1024
    second = _instance_line(USECASE_BUCKET)
    for key, size in ((AT_CAP_MANIFEST_KEY, cap), (OVER_CAP_MANIFEST_KEY, cap + 1)):
        env.s3.put_object(Bucket=USECASE_BUCKET, Key=key, Body=(
            _instance_line_of_size(USECASE_BUCKET, size) + "\n" + second + "\n").encode())
    at_cap = f"s3://{USECASE_BUCKET}/{AT_CAP_MANIFEST_KEY}"
    over_cap = f"s3://{USECASE_BUCKET}/{OVER_CAP_MANIFEST_KEY}"
    usecase = _usecase(env)

    at_cap_result = env.training.validate_detection_manifest(at_cap, usecase, expected="instance")
    assert at_cap_result["valid"] is True, at_cap_result["errors"]

    assert env.training.validate_detection_manifest(over_cap, usecase, expected="instance") == {
        "valid": False,
        "message": "First manifest line is too long",
        "errors": [FIRST_LINE_TOO_LONG_ERROR],
        "class_names": [],
        "detected_attributes": [],
    }
    assert env.training.validate_marketplace_manifest(over_cap, usecase, "segmentation") == {
        "valid": False,
        "errors": [FIRST_LINE_TOO_LONG_ERROR],
        "message": "First manifest line is too long",
    }

    svc = fresh_service()
    n_before = env.training_jobs.scan(Select="COUNT")["Count"]
    status, body = create(env, seg_body(env, dataset_manifest_s3=over_cap))
    error = _assert_rejected_without_side_effects(env, svc, s3_spy, status, body, n_before)
    assert error == "RF-DETR-Seg requires an instance-segmentation manifest"
    assert body["details"] == [FIRST_LINE_TOO_LONG_ERROR]
    assert body["message"] == "First manifest line is too long"
