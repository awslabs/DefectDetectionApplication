"""
Smart Import keeps the fine-tunable checkpoint (rfdetr-training-and-transfer-
learning task 7.2; Requirement 7.1 / 7.2 / 7.5; spike §4.1).

`model_converter.convert_model` on the non-ONNX (`export_format='pytorch'`)
path packages the `.pt`/`.pth` verbatim — there is no ONNX conversion — so the
only thing that makes an import usable as a base model later is keeping the
UNMODIFIED source bytes as a bare sidecar object beside the package and
recording `fine_tunable = {arch, kind, checkpoint_s3, class_names,
num_classes}` in the auto-import body / 200 response.

Harness: moto S3 for the use-case bucket (source object in, package + sidecar
out), the real `generate_dda_package`, the real `classify_checkpoint` over
synthetic checkpoints from `fixtures.checkpoints`. The torch inspector, RBAC,
STS, DynamoDB use-case lookup and audit log are swapped for recorders — none
of them is under test and none must touch AWS.

# Validates: Requirements 7.1, 7.2, 7.5
"""
import io
import json
import os
import re
import sys

import boto3
import pytest
from moto import mock_aws

import model_converter as mc
from fixtures import checkpoints as fx


REGION = "us-east-1"
USER = {"sub": "user-72", "email": "user-72@example.com", "cognito:username": "user-72"}
SIDECAR_RE = re.compile(r"^converted-models/([a-z0-9_]+)-([0-9a-f]{8})/checkpoint\.(pt|pth)$")
PACKAGE_RE = re.compile(r"^converted-models/([a-z0-9_]+)-([0-9a-f]{8})\.tar\.gz$")


class _FakeLambda:
    """Records the auto-import payload and answers like a 201 import."""

    def __init__(self):
        self.payloads = []

    def invoke(self, FunctionName, InvocationType, Payload):
        event = json.loads(Payload)
        self.payloads.append(json.loads(event["body"]))
        body = json.dumps({"training_id": "tid-72", "status": "Completed"})
        return {"Payload": io.BytesIO(json.dumps(
            {"statusCode": 201, "body": body}).encode())}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """moto S3 + stubbed collaborators; yields a SimpleNamespace-ish dict."""
    with mock_aws():
        bucket = f"uc-bucket-{os.getpid()}-{id(tmp_path) & 0xffff:x}"
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=bucket)
        usecase = {
            "usecase_id": "uc-72",
            "s3_bucket": bucket,
            "cross_account_role_arn": "arn:aws:iam::123456789012:role/uc",
            "external_id": "ext",
        }
        inspector_calls = []

        def fake_inspect(path, trusted_source=False):
            inspector_calls.append(path)
            return {"type": "pytorch", "architecture_hints": ["stub"]}

        fake_lambda = _FakeLambda()
        real_client = boto3.client

        def fake_boto_client(service, *args, **kwargs):
            if service == "lambda":
                return fake_lambda
            return real_client(service, *args, **kwargs)

        monkeypatch.setattr(mc, "check_user_access", lambda *a, **k: True)
        monkeypatch.setattr(mc, "get_usecase_details", lambda _id: dict(usecase))
        monkeypatch.setattr(mc, "assume_usecase_role", lambda *a, **k: {"AccessKeyId": "t"})
        monkeypatch.setattr(mc, "make_usecase_s3_client", lambda _creds: s3)
        monkeypatch.setattr(mc, "inspect_pytorch_model", fake_inspect)
        monkeypatch.setattr(mc, "log_audit_event", lambda **k: None)
        monkeypatch.setattr(mc.boto3, "client", fake_boto_client)
        monkeypatch.setenv("MODEL_IMPORT_FUNCTION_NAME", "import-fn")

        yield {
            "s3": s3, "bucket": bucket, "usecase": usecase,
            "lambda": fake_lambda, "inspector_calls": inspector_calls,
            "tmp": tmp_path,
        }


def _put_source(h, builder, key):
    path = h["tmp"] / os.path.basename(key)
    builder(str(path))
    data = path.read_bytes()
    h["s3"].put_object(Bucket=h["bucket"], Key=key, Body=data)
    return data


def _convert(h, key, export_format="pytorch", model_name="Blue Plate v2"):
    body = {
        "usecase_id": "uc-72",
        "model_s3_uri": f"s3://{h['bucket']}/{key}",
        "model_name": model_name,
        "model_type": "object_detection",
        "image_width": 640,
        "image_height": 640,
        "num_classes": 1,
        "class_names": ["blue_plate"],
        "auto_import": True,
        "export_format": export_format,
    }
    event = {"body": json.dumps(body),
             "requestContext": {"authorizer": {"claims": USER}}}
    resp = mc.convert_model(event, None)
    return resp["statusCode"], json.loads(resp["body"])


def _keys(h):
    listing = h["s3"].list_objects_v2(Bucket=h["bucket"], Prefix="converted-models/")
    return sorted(o["Key"] for o in listing.get("Contents", []))


def _sidecars(h):
    return [k for k in _keys(h) if SIDECAR_RE.match(k)]


# ---------------------------------------------------------------------------
# ultralytics .pt → sidecar with identical bytes + fine_tunable everywhere
# ---------------------------------------------------------------------------

def test_ultralytics_checkpoint_is_kept_verbatim_as_sidecar(harness):
    h = harness
    src_bytes = _put_source(h, fx.build_ultralytics_ckpt, "uploads/best.pt")

    status, result = _convert(h, "uploads/best.pt")
    assert status == 200, result

    sidecars = _sidecars(h)
    assert len(sidecars) == 1, _keys(h)
    sidecar_key = sidecars[0]
    name, hex8, ext = SIDECAR_RE.match(sidecar_key).groups()
    assert name == "blue_plate_v2" and ext == "pt"

    # Same <hex> as the package key → the two objects are visibly paired.
    package_key = result["converted_model_s3_uri"].split(f"s3://{h['bucket']}/", 1)[1]
    assert PACKAGE_RE.match(package_key).groups() == (name, hex8)

    # Unmodified source bytes.
    stored = h["s3"].get_object(Bucket=h["bucket"], Key=sidecar_key)["Body"].read()
    assert stored == src_bytes

    expected = {
        "arch": "yolo",
        "kind": "ultralytics_checkpoint",
        "checkpoint_s3": f"s3://{h['bucket']}/{sidecar_key}",
        "class_names": ["blue_plate"],
        "num_classes": 1,
    }
    assert result["fine_tunable"] == expected
    assert len(h["lambda"].payloads) == 1
    assert h["lambda"].payloads[0]["fine_tunable"] == expected
    # The package itself still went through the (stubbed) torch inspector.
    assert h["inspector_calls"], "inspect_pytorch_model must still run"


def test_rfdetr_pth_keeps_source_extension(harness):
    h = harness
    _put_source(h, lambda p: fx.build_rfdetr_v1101(p, class_names=["blue_plate"]),
                "uploads/checkpoint_best_total.PTH")

    status, result = _convert(h, "uploads/checkpoint_best_total.PTH", model_name="rf-detr small")
    assert status == 200, result
    sidecars = _sidecars(h)
    assert len(sidecars) == 1
    assert sidecars[0].endswith("/checkpoint.pth"), sidecars
    assert result["fine_tunable"]["arch"] == "rf_detr"
    assert result["fine_tunable"]["kind"] == "rfdetr_checkpoint"
    assert result["fine_tunable"]["checkpoint_s3"] == f"s3://{h['bucket']}/{sidecars[0]}"


# ---------------------------------------------------------------------------
# TorchScript / state_dict → no sidecar, fine_tunable: null
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("builder", [fx.build_torchscript, fx.build_state_dict],
                         ids=["torchscript", "state_dict"])
def test_non_fine_tunable_pt_writes_no_sidecar(harness, builder):
    h = harness
    _put_source(h, builder, "uploads/model.pt")

    status, result = _convert(h, "uploads/model.pt")
    assert status == 200, result
    assert _sidecars(h) == []
    # Only the package landed under converted-models/.
    assert [k for k in _keys(h) if PACKAGE_RE.match(k)] == _keys(h)
    assert "fine_tunable" in result and result["fine_tunable"] is None
    assert h["lambda"].payloads[0]["fine_tunable"] is None


# ---------------------------------------------------------------------------
# ONNX path never calls the probe
# ---------------------------------------------------------------------------

def test_onnx_path_never_probes(harness, monkeypatch):
    h = harness

    def boom(_path):
        raise AssertionError("classify_checkpoint must not run on the ONNX path")

    monkeypatch.setattr(mc, "classify_checkpoint", boom)
    _put_source(h, lambda p: fx.build_onnx(p, names={0: "blue_plate"}), "uploads/model.onnx")

    status, result = _convert(h, "uploads/model.onnx", export_format="onnx")
    assert status == 200, result
    assert _sidecars(h) == []
    assert result["fine_tunable"] is None
    assert h["lambda"].payloads[0]["fine_tunable"] is None
    assert h["inspector_calls"] == []  # ONNX path is byte-identical: no torch inspector
