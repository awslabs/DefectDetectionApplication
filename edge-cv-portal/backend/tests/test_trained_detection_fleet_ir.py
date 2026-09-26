"""
Portal-trained detection packaging keeps the component loadable on the fleet
floor. onnxruntime 1.16.3 runs on the JP5 GPU build and the CPU / x86 images
and loads IR <= 9 only.

The YOLO trainer's artifact carried ir_version 10, stamped by onnxslim / onnx
1.17 on an opset-17 graph. `package_trained_detection_component` now lowers
the header of the copy it packages. That fixes existing trained records on
re-package and is a no-op for IR <= 9 artifacts, including every
Conversion_Record's.

The real functions/packaging.py runs against the moto-backed conftest stack.
Artifacts are real gzip tarballs holding hand-encoded ONNX ModelProtos.
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

TABLE = "test-training-jobs-fleet-ir"
BUCKET = "test-fleet-ir-bucket"
_FUNCTIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "functions")


def _load(filename, alias):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(_FUNCTIONS, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def onnx_bytes(ir, opset=17):
    node = _pb_str(1, "images") + _pb_str(1, "w0") + _pb_str(2, "output0") + _pb_str(4, "Conv")
    init = _pb_vi(1, 16) + _pb_vi(2, 1) + _pb_str(8, "w0") + _pb_ld(9, b"\x00" * 64)
    graph = (_pb_ld(1, node) + _pb_str(2, "main_graph") + _pb_ld(5, init)
             + _pb_ld(11, _value_info("images", [1, 3, 1280, 1280]))
             + _pb_ld(12, _value_info("output0", [1, 5, 33600])))
    return _pb_vi(1, ir) + _pb_str(2, "pytorch") + _pb_ld(7, graph) + _pb_ld(8, _pb_vi(2, opset))


def trained_artifact(onnx):
    """The trainer's flat artifact: model.onnx + best.pt + training_metadata.json."""
    meta = {"imgsz": 1280, "opset": 17, "num_classes": 1, "class_names": ["blue_plate"],
            "onnx_output_shape": [1, 5, 33600],
            "device_manifest_hints": {"preserve_aspect": True, "network_input": 1280,
                                      "layout": "yolo", "iou_threshold": 0.45,
                                      "score_threshold": 0.25}}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("model.onnx", onnx), ("best.pt", b"pt"),
                           ("training_metadata.json", json.dumps(meta).encode())):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture(scope="module")
def env(aws_stack):
    os.environ["TRAINING_JOBS_TABLE"] = TABLE
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "training_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")
    aws_stack.s3.create_bucket(Bucket=BUCKET)
    packaging = _load("packaging.py", "portal_packaging_fleet_ir")
    usecase_id = f"uc-{uuid.uuid4()}"
    aws_stack.tables.usecases.put_item(Item={"usecase_id": usecase_id, "name": "Fleet IR",
                                             "account_id": "123456789012", "s3_bucket": BUCKET})
    user_id = f"user-{uuid.uuid4()}"
    aws_stack.tables.user_roles.put_item(Item={"user_id": user_id, "usecase_id": usecase_id,
                                               "role": "DataScientist"})
    yield SimpleNamespace(packaging=packaging, s3=aws_stack.s3, usecase_id=usecase_id,
                          user_id=user_id,
                          table=boto3.resource("dynamodb", region_name=REGION).Table(TABLE))


def package_trained(env, onnx):
    key = f"models/training/job-{uuid.uuid4().hex[:8]}/output/model.tar.gz"
    env.s3.put_object(Bucket=BUCKET, Key=key, Body=trained_artifact(onnx))
    training_id = str(uuid.uuid4())
    env.table.put_item(Item={
        "training_id": training_id, "usecase_id": env.usecase_id, "model_name": "blue-plate",
        "model_type": "object_detection", "runtime": "onnx", "status": "Completed",
        "artifact_s3": f"s3://{BUCKET}/{key}",
        "detection": dc.to_dynamo({"detection_arch": "yolo", "network_input_width": 1280,
                                   "network_input_height": 1280, "class_names": ["blue_plate"],
                                   "num_classes": 1, "score_threshold": 0.25,
                                   "iou_threshold": 0.45, "preserve_aspect": True})})
    response = env.packaging.package_components({
        "httpMethod": "POST", "path": f"/api/v1/training/{training_id}/package",
        "pathParameters": {"id": training_id}, "body": "{}",
        "requestContext": {"authorizer": {"claims": {
            "sub": env.user_id, "email": "ds@example.com", "cognito:username": "ds"}}},
    }, None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 200, body
    bucket, zkey = body["packaged_components"][0]["component_package_s3"][5:].split("/", 1)
    with zipfile.ZipFile(io.BytesIO(env.s3.get_object(Bucket=bucket, Key=zkey)["Body"].read())) as zf:
        return zf.read("yolo_object_detection/model.onnx")


def ir_of(data, tmp_path):
    path = tmp_path / f"{hashlib.sha256(data).hexdigest()[:8]}.onnx"
    path.write_bytes(data)
    return dc.read_onnx_structure(str(path))["ir_version"]


def test_an_ir10_trained_graph_is_packaged_as_ir8(env, tmp_path):
    original = onnx_bytes(ir=10)
    packaged = package_trained(env, original)
    assert ir_of(packaged, tmp_path) == 8
    assert len(packaged) == len(original)
    assert [i for i, (a, b) in enumerate(zip(original, packaged)) if a != b] == [1]


@pytest.mark.parametrize("ir", [8, 9])
def test_graphs_within_the_floor_are_packaged_byte_for_byte(env, ir):
    original = onnx_bytes(ir=ir)
    assert package_trained(env, original) == original


def test_a_graph_that_needs_ir10_is_packaged_unchanged(env):
    original = onnx_bytes(ir=10, opset=21)
    assert package_trained(env, original) == original
