"""
Packaging and compilation for a Conversion_Record (detector-checkpoint-import
task 7.3).

The real functions/packaging.py and functions/compilation.py run against the
moto-backed conftest stack, with a training-jobs table and a use case bucket.
Artifacts are real gzip tarballs holding a hand-encoded ONNX ModelProto
(fixtures.checkpoints.builders protobuf helpers) and a training_metadata.json
shaped like export_checkpoint.py's.

Covered:
* The finalize (Finalizing): validate, then package through the unchanged
  trained-detection packager. The device manifest is byte-equal to a
  portal-trained record's with the same Detection_Record_Fields.
  Finalizing -> Completed records the ONNX facts, and component creation is
  triggered exactly once.
* A hostile artifact: Failed with the rule named, nothing uploaded, nothing
  published.
* The InProgress and Failed 400s, a Finalizing retry via the Package action,
  a repeated finalize, and a lost race.
* The compilation bypass, and that a Conversion_Record never takes the
  imported-ONNX path.
# Validates: Requirements 7.9, 8.1-8.6, 9.1-9.7, 12.2
"""
import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import uuid
import zipfile
from types import SimpleNamespace

import boto3
import pytest

import detector_conversion as dc
from conftest import REGION
from fixtures.checkpoints.builders import _pb_ld, _pb_str, _pb_vi, _value_info
from fixtures.detector_probes import PPE_PROBE, PPE_SHA256

TABLE = "test-training-jobs-dci-packaging"
BUCKET = "test-dci-packaging-bucket"
_FUNCTIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "functions")
PPE_NAMES = ["helmet", "human", "no-helmet", "vest"]
TARGETS = ["jetson-xavier-jp5", "jetson-xavier-jp6", "jetson-xavier-jp7", "x86_64-cpu"]


def _load(filename, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(_FUNCTIONS, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def onnx_bytes(outputs=(("output0", [1, 8, 8400]),), size=640):
    node = _pb_str(1, "images") + _pb_str(1, "w0") + _pb_str(2, outputs[0][0]) + _pb_str(4, "Conv")
    init = _pb_vi(1, 16) + _pb_vi(2, 1) + _pb_str(8, "w0") + _pb_ld(9, b"\x00" * 64)
    graph = _pb_ld(1, node) + _pb_str(2, "main_graph") + _pb_ld(5, init)
    graph += _pb_ld(11, _value_info("images", [1, 3, size, size]))
    for name, shape in outputs:
        graph += _pb_ld(12, _value_info(name, shape))
    return _pb_vi(1, 8) + _pb_str(2, "pytorch") + _pb_ld(7, graph) + _pb_ld(8, _pb_vi(2, 17))


def metadata(onnx, **overrides):
    meta = {
        "detection_arch": "yolo", "imgsz": 640, "num_classes": 4, "class_names": PPE_NAMES,
        "onnx_output_shape": [1, 8, 8400], "opset": 17, "ir_version": 8,
        "onnx_sha256": hashlib.sha256(onnx).hexdigest(), "source_sha256": PPE_SHA256,
        "exporter": "ultralytics 8.4.162",
        "fleet_floor": {"onnxruntime": "1.16.3", "loaded": True, "finite": True},
        "parity": {"tolerance": {"box_atol": 0.1, "score_atol": 1e-3},
                   "max_abs": {"box_max_abs": 0.0018, "score_max_abs": 2.5e-6},
                   "runtimes": {"onnxruntime 1.16.3": {}, "onnxruntime 1.30.0": {}}},
        "device_manifest_hints": {"preserve_aspect": True, "network_input": 640, "layout": "yolo",
                                  "iou_threshold": 0.45, "score_threshold": 0.25},
        "conversion": True,
    }
    meta.update(overrides)
    return meta


def tarball(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def regular(name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def good_artifact():
    onnx = onnx_bytes()
    return onnx, tarball([regular("model.onnx", onnx),
                          regular("training_metadata.json", json.dumps(metadata(onnx)).encode())])


@pytest.fixture(scope="module")
def env(aws_stack):
    os.environ["TRAINING_JOBS_TABLE"] = TABLE
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "training_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")
    aws_stack.s3.create_bucket(Bucket=BUCKET)
    packaging = _load("packaging.py", "portal_packaging_dci")
    compilation = _load("compilation.py", "portal_compilation_dci")
    usecase_id = f"uc-{uuid.uuid4()}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id, "name": "Conversion Packaging", "account_id": "123456789012",
        "s3_bucket": BUCKET, "region": REGION})
    user_id = f"user-{uuid.uuid4()}"
    aws_stack.tables.user_roles.put_item(Item={"user_id": user_id, "usecase_id": usecase_id,
                                               "role": "DataScientist"})
    yield SimpleNamespace(packaging=packaging, compilation=compilation, s3=aws_stack.s3,
                          table=boto3.resource("dynamodb", region_name=REGION).Table(TABLE),
                          usecase_id=usecase_id, user_id=user_id)


@pytest.fixture(autouse=True)
def triggers(env, monkeypatch):
    calls = []
    monkeypatch.setattr(env.packaging, "_trigger_component_creation",
                        lambda training_id, job: calls.append(training_id))
    monkeypatch.setattr(env.packaging, "package_onnx_component",
                        lambda *a, **k: pytest.fail("a Conversion_Record took the imported-ONNX path"))
    return calls


def put_artifact(env, data):
    key = f"models/conversion/job-{uuid.uuid4().hex[:8]}/job/output/model.tar.gz"
    env.s3.put_object(Bucket=BUCKET, Key=key, Body=data)
    return f"s3://{BUCKET}/{key}"


def seed_conversion(env, conversion_status="Finalizing", artifact_s3=None):
    assessment = dc.assess_checkpoint(PPE_PROBE)
    params = dc.validate_conversion_request({"image_width": 640, "image_height": 640}, assessment)
    training_id = str(uuid.uuid4())
    record = dc.build_conversion_record(
        training_id=training_id, usecase_id=env.usecase_id, model_name="ppe-detection",
        model_version="1.0.0", created_by="ds@example.com", params=params, assessment=assessment,
        fine_tunable=None, job_name=f"ppe-detection-cnv-{training_id[:8]}",
        job_arn="arn:aws:sagemaker:us-east-1:123456789012:training-job/x",
        image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/dda-detector-export@sha256:" + "a" * 64,
        source_s3=f"s3://{BUCKET}/model-uploads/x/best.pt", source_sha256=PPE_SHA256,
        source_bytes=5475290, model_file="checkpoint.pt", now_ms=1)
    record["conversion"]["status"] = conversion_status
    record["status"] = {"Completed": "Completed", "Failed": "Failed"}.get(conversion_status, "InProgress")
    record["progress"] = dc.PROGRESS_FOR_STATUS[conversion_status]
    if artifact_s3:
        record["artifact_s3"] = artifact_s3
    if conversion_status == "Failed":
        record["failure_reason"] = "AlgorithmError: FATAL: checkpoint task is 'segment', exit code: 1"
    env.table.put_item(Item=record)
    return record


def seed_trained_twin(env, artifact_s3):
    """A portal-trained record with the same Detection_Record_Fields."""
    training_id = str(uuid.uuid4())
    env.table.put_item(Item={
        "training_id": training_id, "usecase_id": env.usecase_id, "model_name": "ppe-detection",
        "model_type": "object_detection", "runtime": "onnx", "status": "Completed",
        "artifact_s3": artifact_s3,
        "detection": dc.to_dynamo({
            "detection_arch": "yolo", "network_input_width": 640, "network_input_height": 640,
            "class_names": PPE_NAMES, "num_classes": 4, "score_threshold": 0.25,
            "iou_threshold": 0.45, "preserve_aspect": True, "onnx_opset": 17, "imgsz": 640}),
    })
    return training_id


def package(env, training_id, body=None, system=True):
    sub = "system" if system else env.user_id
    response = env.packaging.package_components({
        "httpMethod": "POST", "path": f"/api/v1/training/{training_id}/package",
        "pathParameters": {"id": training_id}, "body": json.dumps(body or {}),
        "requestContext": {"authorizer": {"claims": {
            "sub": sub, "email": f"{sub}@example.com", "cognito:username": sub}}},
    }, None)
    return response["statusCode"], json.loads(response["body"])


def stored(env, training_id):
    return env.table.get_item(Key={"training_id": training_id})["Item"]


def manifest_of(env, component_s3):
    bucket, key = component_s3[5:].split("/", 1)
    data = env.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read("manifest.json"), sorted(zf.namelist())


FINALIZE = {"finalize_conversion": True, "auto_triggered": True}


# ---------------------------------------------------------------------------
# The finalize
# ---------------------------------------------------------------------------

def test_finalize_validates_packages_and_completes(env, triggers):
    onnx, data = good_artifact()
    rec = seed_conversion(env, artifact_s3=put_artifact(env, data))
    status, body = package(env, rec["training_id"], FINALIZE)
    assert status == 200, body
    assert [c["target"] for c in body["packaged_components"]] == TARGETS
    assert body["component_creation_triggered"] is True
    assert triggers == [rec["training_id"]]  # exactly one component creation

    item = stored(env, rec["training_id"])
    assert (item["status"], item["progress"]) == ("Completed", 100)
    conv = item["conversion"]
    assert conv["status"] == "Completed" and "completed_at" in conv
    assert conv["onnx_sha256"] == hashlib.sha256(onnx).hexdigest()
    summary = conv["onnx_summary"]
    assert summary["ir_version"] == 8 and summary["opset"] == 17
    assert summary["input"] == [1, 3, 640, 640] and summary["outputs"] == [[1, 8, 8400]]
    assert summary["exporter"] == "ultralytics 8.4.162"
    assert summary["fleet_floor_onnxruntime"] == "1.16.3"
    assert float(summary["parity_max_abs"]["box_max_abs"]) == pytest.approx(0.0018)
    assert [c["target"] for c in item["packaged_components"]] == TARGETS

    # The component: manifest at the root, model.onnx under the stage dir.
    manifest, names = manifest_of(env, body["packaged_components"][0]["component_package_s3"])
    assert names == ["manifest.json", "yolo_object_detection/model.onnx"]
    parsed = json.loads(manifest)
    assert parsed["runtime"] == "onnx"


def test_manifest_is_byte_equal_to_a_trained_record_with_the_same_fields(env):
    _onnx, data = good_artifact()
    artifact = put_artifact(env, data)
    rec = seed_conversion(env, artifact_s3=artifact)
    status, conv_body = package(env, rec["training_id"], FINALIZE)
    assert status == 200, conv_body
    twin = seed_trained_twin(env, artifact)
    status, trained_body = package(env, twin, {}, system=False)
    assert status == 200, trained_body
    conv_manifest, _ = manifest_of(env, conv_body["packaged_components"][0]["component_package_s3"])
    trained_manifest, _ = manifest_of(env, trained_body["packaged_components"][0]["component_package_s3"])
    assert conv_manifest == trained_manifest


def test_the_record_not_the_metadata_names_the_classes(env):
    onnx = onnx_bytes()
    meta = metadata(onnx, class_names=["x", "y", "z", "injected"])
    data = tarball([regular("model.onnx", onnx),
                    regular("training_metadata.json", json.dumps(meta).encode())])
    rec = seed_conversion(env, artifact_s3=put_artifact(env, data))
    status, body = package(env, rec["training_id"], FINALIZE)
    assert status == 200, body
    manifest, _ = manifest_of(env, body["packaged_components"][0]["component_package_s3"])
    assert "injected" not in manifest.decode()
    assert all(name in manifest.decode() for name in PPE_NAMES)


# ---------------------------------------------------------------------------
# Hostile artifacts: Failed, nothing uploaded, nothing published
# ---------------------------------------------------------------------------

def _component_keys(env):
    listing = env.s3.list_objects_v2(Bucket=BUCKET, Prefix="model_artifacts/")
    return {o["Key"] for o in listing.get("Contents", [])}


@pytest.mark.parametrize("name,members,rule", [
    ("symlink", lambda onnx: [(_symlink("model.onnx", "/proc/self/environ"), None),
                              regular("training_metadata.json", b"{}")], "tar-member"),
    ("extra member", lambda onnx: [regular("model.onnx", onnx),
                                   regular("training_metadata.json",
                                           json.dumps(metadata(onnx)).encode()),
                                   regular("best.pt", b"pickle")], "tar-member"),
    ("nms output", lambda _: _artifact_members(onnx_bytes(outputs=(("output0", [1, 300, 6]),)),
                                               onnx_output_shape=[1, 300, 6]), "output"),
    ("wrong input size", lambda _: _artifact_members(onnx_bytes(size=320)), "input"),
    ("sha mismatch", lambda onnx: [regular("model.onnx", onnx),
                                   regular("training_metadata.json",
                                           json.dumps(metadata(onnx, onnx_sha256="0" * 64)).encode())],
     "sha256"),
])
def test_a_hostile_artifact_fails_the_conversion(env, triggers, name, members, rule):
    onnx = onnx_bytes()
    rec = seed_conversion(env, artifact_s3=put_artifact(env, tarball(members(onnx))))
    before = _component_keys(env)
    status, body = package(env, rec["training_id"], FINALIZE)
    assert status == 422 and body["rule"] == rule, body
    item = stored(env, rec["training_id"])
    assert (item["status"], item["progress"], item["conversion"]["status"]) == ("Failed", 0, "Failed")
    assert item["failure_reason"].startswith(f"Conversion output rejected ({rule}):")
    assert "packaged_components" not in item
    assert _component_keys(env) == before
    assert triggers == []


def _symlink(name, target):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = tarfile.SYMTYPE, target
    return info


def _artifact_members(onnx, **meta_overrides):
    return [regular("model.onnx", onnx),
            regular("training_metadata.json", json.dumps(metadata(onnx, **meta_overrides)).encode())]


def test_an_oversize_artifact_fails_before_download(env, monkeypatch, triggers):
    _onnx, data = good_artifact()
    rec = seed_conversion(env, artifact_s3=put_artifact(env, data))
    monkeypatch.setattr(dc, "ARTIFACT_SIZE_CAP", len(data) - 1)
    status, body = package(env, rec["training_id"], FINALIZE)
    assert status == 422 and body["rule"] == "size"
    assert stored(env, rec["training_id"])["conversion"]["status"] == "Failed"
    assert triggers == []


def test_a_missing_artifact_fails(env, triggers):
    rec = seed_conversion(env, artifact_s3=f"s3://{BUCKET}/models/conversion/nope/model.tar.gz")
    status, body = package(env, rec["training_id"], FINALIZE)
    assert status == 422 and body["rule"] == "artifact"
    assert stored(env, rec["training_id"])["status"] == "Failed"


# ---------------------------------------------------------------------------
# Status gates, retries, races
# ---------------------------------------------------------------------------

def test_in_progress_and_failed_are_400(env, triggers):
    running = seed_conversion(env, "InProgress")
    status, body = package(env, running["training_id"], {}, system=False)
    assert status == 400 and "still running" in body["error"]
    failed = seed_conversion(env, "Failed")
    status, body = package(env, failed["training_id"], {}, system=False)
    assert status == 400 and "segment" in body["error"]
    assert triggers == []


def test_the_package_action_finalizes_a_record_left_finalizing(env, triggers):
    _onnx, data = good_artifact()
    rec = seed_conversion(env, artifact_s3=put_artifact(env, data))
    status, body = package(env, rec["training_id"], {}, system=False)  # the user's Package click
    assert status == 200, body
    assert stored(env, rec["training_id"])["conversion"]["status"] == "Completed"
    assert triggers == [rec["training_id"]]


def test_a_repeated_finalize_neither_repackages_nor_republishes(env, triggers):
    _onnx, data = good_artifact()
    rec = seed_conversion(env, artifact_s3=put_artifact(env, data))
    package(env, rec["training_id"], FINALIZE)
    keys = _component_keys(env)
    status, body = package(env, rec["training_id"], FINALIZE)
    assert status == 200 and body["message"] == "Conversion already finalized"
    assert _component_keys(env) == keys
    assert triggers == [rec["training_id"]]


def test_a_completed_record_can_be_repackaged_explicitly(env, triggers):
    _onnx, data = good_artifact()
    rec = seed_conversion(env, artifact_s3=put_artifact(env, data))
    package(env, rec["training_id"], FINALIZE)
    status, body = package(env, rec["training_id"], {"targets": ["jetson-xavier-jp7"]}, system=False)
    assert status == 200, body
    assert [c["target"] for c in stored(env, rec["training_id"])["packaged_components"]] == [
        "jetson-xavier-jp7"]
    assert triggers == [rec["training_id"]]  # no auto_triggered -> no second publish


def test_a_lost_finalize_race_does_not_publish(env, triggers, monkeypatch):
    _onnx, data = good_artifact()
    rec = seed_conversion(env, artifact_s3=put_artifact(env, data))
    real = env.packaging.package_trained_detection_component

    def finalize_elsewhere_first(*args, **kwargs):
        # another finalize completes the record while this one is packaging
        env.table.update_item(
            Key={"training_id": rec["training_id"]},
            UpdateExpression="SET #c.#s = :done", ExpressionAttributeNames={"#c": "conversion", "#s": "status"},
            ExpressionAttributeValues={":done": "Completed"})
        return real(*args, **kwargs)

    monkeypatch.setattr(env.packaging, "package_trained_detection_component", finalize_elsewhere_first)
    status, body = package(env, rec["training_id"], FINALIZE)
    assert status == 409
    assert triggers == []


# ---------------------------------------------------------------------------
# Compilation bypass (Req 9.1)
# ---------------------------------------------------------------------------

def test_compilation_skips_neo_for_a_conversion_record(env):
    rec = seed_conversion(env, "Completed", artifact_s3=f"s3://{BUCKET}/x/model.tar.gz")
    response = env.compilation.start_compilation_job({
        "httpMethod": "POST", "path": f"/api/v1/training/{rec['training_id']}/compile",
        "pathParameters": {"id": rec["training_id"]},
        "body": json.dumps({"targets": ["jetson-xavier-jp5"]}),
        "requestContext": {"authorizer": {"claims": {
            "sub": env.user_id, "email": "ds@example.com", "cognito:username": "ds"}}},
    }, None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 200, body
    assert body["compilation_jobs"] == [] and "compilation is not required" in body["message"]
    assert stored(env, rec["training_id"])["compilation_skipped"] is True
