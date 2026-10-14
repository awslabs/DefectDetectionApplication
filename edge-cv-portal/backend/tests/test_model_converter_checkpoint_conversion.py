"""
``model_converter`` for imported detector checkpoints (detector-checkpoint-
import task 5.4): the inspect ``checkpoint`` block, ``POST /models/upload-url``
and the ``convert`` conversion branch.

Harness (the test_model_converter_fine_tunable.py pattern): moto S3 for the use
case bucket and moto DynamoDB for the training-jobs table, the real
``classify_checkpoint`` over synthetic checkpoints from
``fixtures.checkpoints``, and a recording SageMaker stand-in. RBAC, STS, the
use-case lookup and the audit log are swapped for recorders. The torch
inspector is replaced by a tripwire: nothing on these paths may reach it.
# Validates: Requirements 2.1, 2.2, 2.5, 2.7, 3.1-3.7, 4.1, 4.2, 4.6-4.10, 12.1-12.3
"""
import hashlib
import inspect
import json
import os
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import detector_conversion as dc
import model_converter as mc
import s3_cors
from fixtures import checkpoints as fx
from fixtures.detector_probes import YOLO11N_SEG_PROBE

REGION = "us-east-1"
ACCOUNT = "123456789012"
USER = {"sub": "user-dci", "email": "ds@example.com", "cognito:username": "ds"}
IMAGE = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/dda-detector-export@sha256:" + "ab" * 32
PPE_NAMES = {0: "helmet", 1: "human", 2: "no-helmet", 3: "vest"}
SIDECAR_RE = re.compile(r"^converted-models/([a-z0-9_]+)-([0-9a-f]{8})/checkpoint\.(pt|pth)$")
UPLOAD_KEY_RE = re.compile(
    r"^model-uploads/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/(.+)$")


class FakeSageMaker:
    def __init__(self):
        self.created, self.stopped, self.error = [], [], None

    def create_training_job(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.created.append(kwargs)
        return {"TrainingJobArn": f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:training-job/"
                                  f"{kwargs['TrainingJobName']}"}

    def stop_training_job(self, TrainingJobName):
        self.stopped.append(TrainingJobName)


class RecordingS3:
    """Delegates to the moto client, recording download_file / head_object."""

    def __init__(self, inner):
        self.inner, self.calls = inner, []

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if name in ("download_file", "head_object", "upload_file", "delete_object"):
            def recorded(*args, **kwargs):
                self.calls.append(name)
                return attr(*args, **kwargs)
            return recorded
        return attr


@pytest.fixture
def h(monkeypatch, tmp_path):
    with mock_aws():
        bucket = "uc-bucket-dci"
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=bucket)
        ddb = boto3.resource("dynamodb", region_name=REGION)
        table = ddb.create_table(
            TableName="test-training-jobs-dci",
            KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "training_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        usecase = {"usecase_id": "uc-dci", "s3_bucket": bucket, "account_id": ACCOUNT,
                   "region": REGION, "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT}:root",
                   "external_id": "ext"}
        sagemaker = FakeSageMaker()
        recording = RecordingS3(s3)
        audits, access = [], {"allowed": True, "roles": []}
        real_client = boto3.client

        def fake_client(service, *args, **kwargs):
            if service == "sagemaker":
                return sagemaker
            if service == "lambda":
                raise AssertionError("the conversion path must not invoke ModelImport")
            return real_client(service, *args, **kwargs)

        def fake_access(user_id, usecase_id, role=None, **_):
            access["roles"].append(role)
            return access["allowed"]

        def tripwire(*_a, **_k):
            raise AssertionError("inspect_pytorch_model (torch) must not run for a checkpoint")

        monkeypatch.setattr(mc, "check_user_access", fake_access)
        monkeypatch.setattr(mc, "get_usecase_details", lambda _id: dict(usecase))
        monkeypatch.setattr(mc, "assume_usecase_role", lambda *a, **k: {"is_default_credentials": True})
        monkeypatch.setattr(mc, "make_usecase_s3_client", lambda _creds: recording)
        monkeypatch.setattr(mc, "inspect_pytorch_model", tripwire)
        monkeypatch.setattr(mc, "log_audit_event", lambda **k: audits.append(k))
        monkeypatch.setattr(mc.boto3, "client", fake_client)
        monkeypatch.setattr(mc, "dynamodb", ddb)
        monkeypatch.setattr(mc, "TRAINING_JOBS_TABLE", table.name)
        monkeypatch.setattr(mc, "DETECTOR_EXPORT_IMAGE", IMAGE)
        monkeypatch.delenv("CLOUDFRONT_DOMAIN", raising=False)
        yield SimpleNamespace(s3=s3, recording=recording, bucket=bucket, table=table,
                              usecase=usecase, sagemaker=sagemaker, audits=audits,
                              access=access, tmp=tmp_path)


def put_source(h, builder, key):
    path = h.tmp / os.path.basename(key)
    builder(str(path))
    data = path.read_bytes()
    h.s3.put_object(Bucket=h.bucket, Key=key, Body=data)
    return data


def ppe_like(path):
    return fx.build_ultralytics_ckpt(path, names=dict(PPE_NAMES), version="8.4.2")


def event(body):
    return {"body": json.dumps(body), "requestContext": {"authorizer": {"claims": USER}}}


def call(fn, body):
    resp = fn(event(body), None)
    return resp["statusCode"], json.loads(resp["body"])


def inspect_body(h, key):
    return {"usecase_id": "uc-dci", "model_s3_uri": f"s3://{h.bucket}/{key}"}


def convert_body(h, key, **overrides):
    body = {"usecase_id": "uc-dci", "model_s3_uri": f"s3://{h.bucket}/{key}",
            "model_name": "PPE Detection", "model_type": "object_detection",
            "image_width": 1280, "image_height": 1280, "export_format": "onnx",
            "auto_import": True}
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not None}


def keys(h, prefix=""):
    listing = h.s3.list_objects_v2(Bucket=h.bucket, Prefix=prefix)
    return sorted(o["Key"] for o in listing.get("Contents", []))


def records(h):
    return h.table.scan()["Items"]


# ---------------------------------------------------------------------------
# Inspect (Requirement 2)
# ---------------------------------------------------------------------------

def test_inspect_reports_a_convertible_checkpoint_and_prefills(h):
    put_source(h, ppe_like, "uploads/best.pt")
    status, body = call(mc.inspect_model_endpoint, inspect_body(h, "uploads/best.pt"))
    assert status == 200, body
    info = body["inspection_result"]
    ckpt = info["checkpoint"]
    for field in ("kind", "arch", "task", "model_class", "head_classes", "num_classes",
                  "class_names", "train_input_size", "framework", "framework_version",
                  "convertible", "reasons"):
        assert field in ckpt, field
    assert ckpt["convertible"] is True and ckpt["reasons"] == []
    assert ckpt["kind"] == "ultralytics_checkpoint" and ckpt["framework_version"] == "8.4.2"
    assert ckpt["class_names"] == ["helmet", "human", "no-helmet", "vest"]
    assert ckpt["head_classes"] == ["ultralytics.nn.modules.head.Detect"]
    assert info["suggested_type"] == "object_detection" and info["detection_arch"] == "yolo"
    assert info["num_classes"] == 4
    assert info["class_names"] == ["helmet", "human", "no-helmet", "vest"]
    assert info["input_width"] == info["input_height"] == 1280
    assert info["fine_tunable"] is True
    assert h.recording.calls.index("head_object") < h.recording.calls.index("download_file")


def test_inspect_without_an_export_image_reports_not_configured(h, monkeypatch):
    monkeypatch.setattr(mc, "DETECTOR_EXPORT_IMAGE", "")
    put_source(h, ppe_like, "uploads/best.pt")
    status, body = call(mc.inspect_model_endpoint, inspect_body(h, "uploads/best.pt"))
    assert status == 200
    assert body["inspection_result"]["checkpoint"]["convertible"] is False
    assert body["inspection_result"]["checkpoint"]["reasons"] == [
        "Checkpoint conversion is not configured on this portal (no detector export image)"]


def test_inspect_reports_torchscript_as_not_convertible(h):
    put_source(h, fx.build_torchscript, "uploads/traced.pt")
    status, body = call(mc.inspect_model_endpoint, inspect_body(h, "uploads/traced.pt"))
    assert status == 200
    ckpt = body["inspection_result"]["checkpoint"]
    assert ckpt["kind"] == "torchscript" and ckpt["convertible"] is False
    assert "frozen TorchScript graph" in ckpt["reasons"][0]
    assert "suggested_type" not in body["inspection_result"]


def test_inspect_rejects_an_oversize_checkpoint_before_download(h, monkeypatch):
    data = put_source(h, ppe_like, "uploads/huge.pth")
    monkeypatch.setattr(dc, "CHECKPOINT_SIZE_CAP", len(data) - 1)
    status, body = call(mc.inspect_model_endpoint, inspect_body(h, "uploads/huge.pth"))
    assert status == 400
    assert f"Checkpoint is {len(data)} bytes" in body["error"]
    assert "download_file" not in h.recording.calls


def test_inspect_of_onnx_never_heads_or_probes(h, monkeypatch):
    monkeypatch.setattr(mc, "classify_checkpoint",
                        lambda p: pytest.fail("the probe must not run on ONNX"))
    put_source(h, lambda p: fx.build_onnx(p, names={0: "blue_plate"}), "uploads/model.onnx")
    status, body = call(mc.inspect_model_endpoint, inspect_body(h, "uploads/model.onnx"))
    assert status == 200, body
    assert "checkpoint" not in body["inspection_result"]
    assert "head_object" not in h.recording.calls


# ---------------------------------------------------------------------------
# Upload URL (Requirement 3)
# ---------------------------------------------------------------------------

def upload_body(**overrides):
    body = {"usecase_id": "uc-dci", "file_name": "best.pt", "size_bytes": 5475290}
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not None}


def test_upload_url_issues_a_server_chosen_key_and_a_sigv4_presign(h):
    status, body = call(mc.get_model_upload_url, upload_body(file_name="../../My Model (v2).PT"))
    assert status == 200, body
    assert h.access["roles"] == ["DataScientist"]
    assert body["expires_in"] == 900
    bucket, key = body["model_s3_uri"][5:].split("/", 1)
    assert bucket == h.bucket
    match = UPLOAD_KEY_RE.match(key)
    assert match and match.group(1) == "My_Model_v2.pt"
    url = urlparse(body["upload_url"])
    query = parse_qs(url.query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["900"]
    assert query["X-Amz-SignedHeaders"] == ["host"]  # Content-Type is not signed
    assert key in url.path or key.replace("(", "%28") in url.path
    assert mc.is_trusted_model_source(body["model_s3_uri"], h.usecase) is True
    assert h.audits[-1]["action"] == "get_model_upload_url"


@pytest.mark.parametrize("name,expected", [
    ("best.pt", "best.pt"), ("CKPT.PTH", "CKPT.pth"), ("a/b\\c.onnx", "c.onnx"),
    ("...pt", "model.pt"), ("x y+z.pt", "x_y_z.pt"), ("é.pt", "model.pt"),
    ("model.PT.pth", "model_PT.pth"),
])
def test_sanitised_upload_names(name, expected):
    assert mc.sanitise_upload_name(name) == expected


def test_upload_url_configures_cors_like_data_management(h, monkeypatch):
    status, _ = call(mc.get_model_upload_url, upload_body())
    assert status == 200
    rules = h.s3.get_bucket_cors(Bucket=h.bucket)["CORSRules"]
    assert rules[0]["AllowedOrigins"] == ["*"] and "PUT" in rules[0]["AllowedMethods"]
    monkeypatch.setenv("CLOUDFRONT_DOMAIN", "portal.example.com")
    call(mc.get_model_upload_url, upload_body())
    rules = h.s3.get_bucket_cors(Bucket=h.bucket)["CORSRules"]
    assert rules[0]["AllowedOrigins"] == ["*"]  # wildcard already allows the portal: unchanged


def test_data_management_uses_the_shared_cors_helper():
    import data_management
    assert data_management.ensure_bucket_cors is s3_cors.ensure_bucket_cors
    assert mc.ensure_bucket_cors is s3_cors.ensure_bucket_cors


@pytest.mark.parametrize("overrides,fragment", [
    ({"file_name": "model.bin"}, "must end in .pt, .pth, .onnx"),
    ({"file_name": "model.pt.exe"}, "must end in"),
    ({"size_bytes": dc.CHECKPOINT_SIZE_CAP + 1}, "checkpoint size cap"),
    ({"size_bytes": 0}, "positive integer"),
    ({"size_bytes": "12"}, "positive integer"),
    ({"size_bytes": True}, "positive integer"),
    ({"file_name": None}, "file_name"),
    ({"usecase_id": None}, "usecase_id"),
])
def test_upload_url_rejects_bad_requests_without_issuing_a_url(h, overrides, fragment):
    status, body = call(mc.get_model_upload_url, upload_body(**overrides))
    assert status == 400 and fragment in body["error"]
    assert "upload_url" not in body


def test_upload_url_requires_the_datascientist_role(h):
    h.access["allowed"] = False
    status, body = call(mc.get_model_upload_url, upload_body())
    assert status == 403 and "upload_url" not in body


# ---------------------------------------------------------------------------
# Convert (Requirement 4)
# ---------------------------------------------------------------------------

def test_convert_starts_one_isolated_job_and_writes_the_record(h):
    src = put_source(h, ppe_like, "model-uploads/0000/best.pt")
    status, body = call(mc.convert_model, convert_body(h, "model-uploads/0000/best.pt"))
    assert status == 200, body

    # (a) the sidecar: identical bytes, today's key scheme, no package written
    sidecars = [k for k in keys(h, "converted-models/") if SIDECAR_RE.match(k)]
    assert len(sidecars) == 1 and keys(h, "converted-models/") == sidecars
    name, hex8, ext = SIDECAR_RE.match(sidecars[0]).groups()
    assert (name, ext) == ("ppe_detection", "pt")
    assert h.s3.get_object(Bucket=h.bucket, Key=sidecars[0])["Body"].read() == src
    fine_tunable = {"arch": "yolo", "kind": "ultralytics_checkpoint",
                    "checkpoint_s3": f"s3://{h.bucket}/{sidecars[0]}",
                    "class_names": ["helmet", "human", "no-helmet", "vest"], "num_classes": 4}
    assert body["fine_tunable"] == fine_tunable

    # (b)+(c) exactly one job, isolated, on the sidecar prefix, sha256-pinned
    assert len(h.sagemaker.created) == 1
    job = h.sagemaker.created[0]
    assert job["EnableNetworkIsolation"] is True
    assert job["AlgorithmSpecification"]["TrainingImage"] == IMAGE
    assert job["RoleArn"] == f"arn:aws:iam::{ACCOUNT}:role/DDASageMakerExecutionRole"
    prefix = sidecars[0].rsplit("/", 1)[0] + "/"
    assert job["InputDataConfig"][0]["DataSource"]["S3DataSource"]["S3Uri"] == \
        f"s3://{h.bucket}/{prefix}"
    assert len(job["InputDataConfig"]) == 1
    assert job["Environment"]["EXPECTED_SHA256"] == hashlib.sha256(src).hexdigest()
    assert job["Environment"]["NETWORK_INPUT"] == "1280"
    assert job["Environment"]["EXPECTED_NUM_CLASSES"] == "4"
    assert job["OutputDataConfig"]["S3OutputPath"] == \
        f"s3://{h.bucket}/models/conversion/{job['TrainingJobName']}/"
    assert "HyperParameters" not in job

    # (d)+(e) the Conversion_Record and the response
    items = records(h)
    assert len(items) == 1
    rec = items[0]
    assert dc.is_detector_conversion_record(rec)
    assert rec["training_id"] == body["training_id"]
    assert (rec["source"], rec["runtime"], rec["status"], rec["progress"]) == (
        "imported", "onnx", "InProgress", 10)
    assert rec["training_job_name"] == job["TrainingJobName"]
    assert rec["training_job_arn"].endswith(job["TrainingJobName"])
    assert rec["detection"]["class_names"] == ["helmet", "human", "no-helmet", "vest"]
    assert rec["detection"]["network_input_width"] == 1280
    assert rec["metadata"]["fine_tunable"] == fine_tunable
    assert rec["conversion"]["source_sha256"] == hashlib.sha256(src).hexdigest()
    assert rec["conversion"]["source_bytes"] == len(src)
    assert rec["conversion"]["export_image"] == IMAGE
    assert rec["conversion"]["source_s3"] == f"s3://{h.bucket}/model-uploads/0000/best.pt"
    assert rec["created_by"] == "ds@example.com"
    assert body == {"training_id": rec["training_id"], "model_name": "PPE Detection",
                    "status": "InProgress",
                    "conversion": {"status": "InProgress", "job_name": job["TrainingJobName"]},
                    "fine_tunable": fine_tunable}
    assert h.audits[-1]["action"] == "convert_checkpoint"
    assert h.audits[-1]["details"]["export_image"] == IMAGE
    assert h.access["roles"] == ["DataScientist"]


def test_convert_honours_renamed_classes(h):
    put_source(h, ppe_like, "uploads/best.pt")
    renamed = ["hard-hat", "person", "bare-head", "hi-vis"]
    status, body = call(mc.convert_model, convert_body(h, "uploads/best.pt", class_names=renamed))
    assert status == 200, body
    assert records(h)[0]["detection"]["class_names"] == renamed


def nothing_created(h):
    assert h.sagemaker.created == []
    assert records(h) == []
    assert keys(h, "converted-models/") == []


def test_convert_rejects_a_segmentation_checkpoint(h, monkeypatch):
    put_source(h, ppe_like, "uploads/seg.pt")
    monkeypatch.setattr(mc, "classify_checkpoint", lambda p: YOLO11N_SEG_PROBE)
    status, body = call(mc.convert_model, convert_body(h, "uploads/seg.pt"))
    assert status == 400
    assert body["error"].startswith("Checkpoint cannot be converted to ONNX: ")
    assert "segmentation" in body["error"]
    nothing_created(h)


def test_convert_rejects_torchscript(h):
    put_source(h, fx.build_torchscript, "uploads/traced.pt")
    status, body = call(mc.convert_model, convert_body(h, "uploads/traced.pt"))
    assert status == 400 and "frozen TorchScript graph" in body["error"]
    nothing_created(h)


@pytest.mark.parametrize("overrides,fragment", [
    ({"class_names": ["only", "three", "names"]}, "exactly 4 classes"),
    ({"preserve_aspect": False}, "must be true for YOLO"),
    ({"image_width": 650, "image_height": 650}, "multiple of 32"),
    ({"score_threshold": 1.5}, "score_threshold"),
])
def test_convert_validation_errors_create_nothing(h, overrides, fragment):
    put_source(h, ppe_like, "uploads/best.pt")
    status, body = call(mc.convert_model, convert_body(h, "uploads/best.pt", **overrides))
    assert status == 400 and fragment in body["error"]
    nothing_created(h)


def test_convert_without_an_export_image_is_503(h, monkeypatch):
    monkeypatch.setattr(mc, "DETECTOR_EXPORT_IMAGE", "")
    put_source(h, ppe_like, "uploads/best.pt")
    status, body = call(mc.convert_model, convert_body(h, "uploads/best.pt"))
    assert status == 503
    assert body["error"] == ("Checkpoint conversion is not configured on this portal "
                             "(no detector export image)")
    nothing_created(h)


def test_convert_in_another_region_is_503(h):
    h.usecase["region"] = "eu-west-1"
    put_source(h, ppe_like, "uploads/best.pt")
    status, body = call(mc.convert_model, convert_body(h, "uploads/best.pt"))
    assert status == 503 and "region eu-west-1" in body["error"]
    nothing_created(h)


def test_convert_surfaces_sagemakers_reason_and_leaves_no_record(h):
    put_source(h, ppe_like, "uploads/best.pt")
    h.sagemaker.error = ClientError(
        {"Error": {"Code": "ResourceLimitExceeded",
                   "Message": "The account-level service limit 'ml.m5.xlarge for training job usage' is 0"}},
        "CreateTrainingJob")
    status, body = call(mc.convert_model, convert_body(h, "uploads/best.pt"))
    assert status == 502
    assert "ml.m5.xlarge for training job usage" in body["error"]
    assert records(h) == []
    assert keys(h, "converted-models/") == []  # the orphan sidecar is removed


def test_a_failed_record_write_stops_the_job(h, monkeypatch):
    put_source(h, ppe_like, "uploads/best.pt")

    class BrokenTable:
        def put_item(self, **_):
            raise ClientError({"Error": {"Code": "ValidationException", "Message": "x"}}, "PutItem")

    monkeypatch.setattr(mc, "dynamodb", SimpleNamespace(Table=lambda _n: BrokenTable()))
    status, _ = call(mc.convert_model, convert_body(h, "uploads/best.pt"))
    assert status == 500
    assert h.sagemaker.stopped == [h.sagemaker.created[0]["TrainingJobName"]]


def test_convert_enforces_the_size_cap_before_download(h, monkeypatch):
    data = put_source(h, ppe_like, "uploads/best.pt")
    monkeypatch.setattr(dc, "CHECKPOINT_SIZE_CAP", len(data) - 1)
    status, body = call(mc.convert_model, convert_body(h, "uploads/best.pt"))
    assert status == 400 and "checkpoint size cap" in body["error"]
    assert "download_file" not in h.recording.calls
    nothing_created(h)


def test_convert_of_a_missing_source_is_400(h):
    status, body = call(mc.convert_model, convert_body(h, "uploads/missing.pt"))
    assert status == 400 and "does not exist" in body["error"]


def test_the_rfdetr_path_uses_native_resolution(h):
    put_source(h, lambda p: fx.build_rfdetr_v1101(p, class_names=["blue_plate"]),
               "uploads/checkpoint_best_total.pth")
    status, body = call(mc.convert_model, convert_body(
        h, "uploads/checkpoint_best_total.pth", image_width=512, image_height=512,
        model_name="rf small"))
    # The synthetic v1.10.1 checkpoint names no size, so any native input converts.
    assert status == 200, body
    job = h.sagemaker.created[0]
    assert job["Environment"]["DETECTION_ARCH"] == "rf_detr"
    assert records(h)[0]["detection"]["preserve_aspect"] is False
    assert records(h)[0]["detection"]["top_k"] == 300


def test_new_code_never_deserializes_a_checkpoint():
    """No import of, or reference to, a deserializing library in the new
    code paths (Requirement 2.2; repo_audit scans the file as a whole)."""
    import ast
    import textwrap

    forbidden = {"torch", "pickle", "ultralytics", "rfdetr", "joblib", "dill", "cloudpickle"}
    for fn in (mc.inspect_checkpoint_file, mc.convert_checkpoint, mc.get_model_upload_url,
               mc.checkpoint_size_rejection, mc.make_usecase_client, mc.sanitise_upload_name):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            names = set()
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            elif isinstance(node, ast.Name):
                names = {node.id}
            assert not names & forbidden, (fn.__name__, names & forbidden)
