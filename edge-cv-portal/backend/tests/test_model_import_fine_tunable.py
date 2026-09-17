"""
`model_import.import_model` accepts, validates and persists the optional
`fine_tunable` descriptor Smart Import sends when it kept a checkpoint
(rfdetr-training-and-transfer-learning task 7.2; Requirement 7.1 / 7.2 / 7.5;
design §Data Models `metadata.fine_tunable`).

The record later feeds `resolve_base_model('imported', ...)` and therefore
`BASE_WEIGHTS_S3` for a SageMaker job, so the handler pins `checkpoint_s3`
to THIS use case's bucket under the converter's prefix and 400s on any
vocabulary violation. Absent → an explicit `null` so Model Detail and the
base-model resolver never have to guess.

Harness: the artifact validator, RBAC, STS, use-case lookup and audit log are
swapped for stubs (none is under test); the DynamoDB table is a recorder so
the persisted item can be inspected directly.

# Validates: Requirements 7.1, 7.2, 7.5
"""
import json

import pytest

import model_import as mi


BUCKET = "uc-bucket-72"
USER = {"sub": "user-72", "email": "user-72@example.com", "cognito:username": "user-72"}
VALID = {
    "arch": "yolo",
    "kind": "ultralytics_checkpoint",
    "checkpoint_s3": f"s3://{BUCKET}/converted-models/blue_plate_v2-0badcafe/checkpoint.pt",
    "class_names": ["blue_plate"],
    "num_classes": 1,
}
VALIDATION_RESULT = {
    "valid": True,
    "metadata": {
        "image_width": 640,
        "image_height": 640,
        "input_shape": [1, 3, 640, 640],
        "model_type": "object_detection",
        "pt_file": "blue_plate_v2.pt",
        "model_file": "blue_plate_v2.pt",
        "framework": "pytorch",
    },
}


class _Table:
    def __init__(self):
        self.items = []

    def put_item(self, Item):
        self.items.append(Item)


class _Dynamo:
    def __init__(self):
        self.table = _Table()

    def Table(self, _name):
        return self.table


@pytest.fixture
def harness(monkeypatch):
    dynamo = _Dynamo()
    monkeypatch.setattr(mi, "check_user_access", lambda *a, **k: True)
    monkeypatch.setattr(mi, "get_usecase_details", lambda _id: {
        "usecase_id": "uc-72", "s3_bucket": BUCKET,
        "cross_account_role_arn": "arn:aws:iam::123456789012:role/uc",
        "external_id": "ext"})
    monkeypatch.setattr(mi, "assume_usecase_role", lambda *a, **k: {"AccessKeyId": "t"})
    monkeypatch.setattr(mi, "validate_model_artifact",
                        lambda uri, creds: json.loads(json.dumps(VALIDATION_RESULT)))
    monkeypatch.setattr(mi, "log_audit_event", lambda **k: None)
    monkeypatch.setattr(mi, "dynamodb", dynamo)
    monkeypatch.setattr(mi, "TRAINING_JOBS_TABLE", "test-training-jobs")
    return dynamo.table


def _import(body_extra=None, omit_fine_tunable=False):
    body = {
        "usecase_id": "uc-72",
        "model_name": "Blue Plate v2",
        "model_version": "1.0.0",
        "model_s3_uri": f"s3://{BUCKET}/converted-models/blue_plate_v2-0badcafe.tar.gz",
        "description": "auto-converted",
    }
    if not omit_fine_tunable:
        body["fine_tunable"] = VALID
    if body_extra:
        body.update(body_extra)
    event = {"body": json.dumps(body),
             "requestContext": {"authorizer": {"claims": USER}}}
    resp = mi.import_model(event, None)
    return resp["statusCode"], json.loads(resp["body"])


# ---------------------------------------------------------------------------
# Persisted shape
# ---------------------------------------------------------------------------

def test_valid_descriptor_is_persisted_under_metadata(harness):
    status, body = _import()
    assert status == 201, body
    assert len(harness.items) == 1
    item = harness.items[0]
    assert item["source"] == "imported"
    assert item["metadata"]["fine_tunable"] == VALID
    # The rest of metadata is untouched.
    for k, v in VALIDATION_RESULT["metadata"].items():
        assert item["metadata"][k] == v
    # validation_result keeps its original shape (no fine_tunable leaks in).
    assert "fine_tunable" not in item["validation_result"]["metadata"]


def test_null_class_names_and_num_classes_are_accepted(harness):
    """Published RF-DETR COCO files carry no names (spike §2.1)."""
    desc = dict(VALID, arch="rf_detr", kind="rfdetr_checkpoint",
                checkpoint_s3=f"s3://{BUCKET}/converted-models/rf_detr-00000001/checkpoint.pth",
                class_names=None, num_classes=None)
    status, body = _import({"fine_tunable": desc})
    assert status == 201, body
    assert harness.items[0]["metadata"]["fine_tunable"] == desc


def test_absent_field_persists_explicit_null(harness):
    status, body = _import(omit_fine_tunable=True)
    assert status == 201, body
    item = harness.items[0]
    assert "fine_tunable" in item["metadata"]
    assert item["metadata"]["fine_tunable"] is None


def test_explicit_null_persists_null(harness):
    status, body = _import({"fine_tunable": None})
    assert status == 201, body
    assert harness.items[0]["metadata"]["fine_tunable"] is None


# ---------------------------------------------------------------------------
# 400s — nothing persisted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("patch,needle", [
    ({"checkpoint_s3": "s3://someone-elses-bucket/converted-models/x-00000001/checkpoint.pt"},
     "checkpoint_s3"),
    ({"checkpoint_s3": f"s3://{BUCKET}/uploads/best.pt"}, "checkpoint_s3"),
    ({"checkpoint_s3": f"s3://{BUCKET}/converted-models/"}, "checkpoint_s3"),
    ({"checkpoint_s3": None}, "checkpoint_s3"),
    ({"arch": "detr"}, "arch"),
    ({"arch": None}, "arch"),
    ({"kind": "torchscript"}, "kind"),
    ({"kind": "onnx"}, "kind"),
    ({"class_names": "blue_plate"}, "class_names"),
    ({"class_names": ["blue_plate", 1]}, "class_names"),
    ({"num_classes": "1"}, "num_classes"),
    ({"num_classes": True}, "num_classes"),
], ids=["wrong-bucket", "wrong-prefix", "prefix-only", "missing-uri",
        "wrong-arch", "missing-arch", "torchscript-kind", "onnx-kind",
        "class_names-str", "class_names-mixed", "num_classes-str", "num_classes-bool"])
def test_invalid_descriptor_is_rejected_with_400(harness, patch, needle):
    desc = dict(VALID)
    desc.update(patch)
    status, body = _import({"fine_tunable": desc})
    assert status == 400, body
    assert needle in body["error"]
    assert harness.items == []


@pytest.mark.parametrize("raw", ["yes", 1, ["yolo"], True], ids=["str", "int", "list", "bool"])
def test_non_object_descriptor_is_rejected_with_400(harness, raw):
    status, body = _import({"fine_tunable": raw})
    assert status == 400, body
    assert "fine_tunable" in body["error"]
    assert harness.items == []


# ---------------------------------------------------------------------------
# The validator itself (pure)
# ---------------------------------------------------------------------------

def test_validate_fine_tunable_returns_normalised_copy():
    value, err = mi.validate_fine_tunable(dict(VALID, extra="ignored"), BUCKET)
    assert err == ""
    assert value == VALID  # unknown keys dropped, known keys kept verbatim
    assert mi.validate_fine_tunable(None, BUCKET) == (None, "")
    assert mi.FINE_TUNABLE_KINDS == ("ultralytics_checkpoint", "rfdetr_checkpoint")
