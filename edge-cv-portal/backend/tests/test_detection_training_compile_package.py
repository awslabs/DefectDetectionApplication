"""
compilation.py / packaging.py tests for portal-trained Object Detection
records (portal-detection-training task 5.2).

- compile: a detection record takes the ONNX Compilation_Bypass — no
  SageMaker call, no extract_and_repackage_model, `compilation_skipped`.
- package: the flat train.py artifact (model.onnx + best.pt +
  training_metadata.json) becomes a component ZIP with manifest.json at the
  root (runtime onnx, task object_detection, preserve_aspect true, class
  names, thresholds) and yolo_object_detection/model.onnx nested; one
  packaged_components entry per default target; training_metadata.json's
  imgsz wins over the record; an artifact without .onnx -> 500 and no
  packaged_components; imported-ONNX and LFV records keep their own paths.

# Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.8, 9.2
"""
import importlib.util
import io
import json
import os
import sys
import tarfile
import uuid
import zipfile
from decimal import Decimal
from types import SimpleNamespace

import pytest

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-detection-pkg"
USECASE_BUCKET = "test-detection-pkg-bucket"
ACCOUNT_ID = "123456789012"

_FUNCTIONS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "functions")

DETECTION_ARTIFACT_KEY = "models/training/blue-plate-x/output/model.tar.gz"
DETECTION_ARTIFACT_640_KEY = "models/training/blue-plate-640/output/model.tar.gz"
NO_ONNX_ARTIFACT_KEY = "models/training/broken/output/model.tar.gz"
LFV_ARTIFACT_KEY = "models/training/lfv/output/model.tar.gz"
IMPORTED_ARTIFACT_KEY = "converted-models/imported-yolo.tar.gz"

ONNX_BYTES = b"\x08\x07onnx-not-really-but-fine"


def _load_module(filename, alias):
    spec = importlib.util.spec_from_file_location(
        alias, os.path.join(_FUNCTIONS_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


class ExplodingSageMaker:
    """Any SageMaker call on a detection record is a bug (Req 5.1)."""
    def __getattr__(self, name):
        raise AssertionError(f"SageMaker.{name} must not be called for a detection record")


def _tar(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _detection_artifact(imgsz, output_shape):
    meta = {
        "imgsz": imgsz, "base_weights": "yolo11s.pt", "epochs": 100, "opset": 17,
        "onnx_output_shape": output_shape,
        "metrics": {"test_map50": 0.995},
        "manifest_s3": "s3://x/labeled/output.manifest", "images_s3": None,
        "device_manifest_hints": {"preserve_aspect": True, "network_input": imgsz,
                                  "layout": "yolo", "iou_threshold": 0.45,
                                  "score_threshold": 0.25},
    }
    return _tar({
        "model.onnx": ONNX_BYTES,
        "best.pt": b"pt",
        "training_metadata.json": json.dumps(meta).encode(),
    })


def _imported_onnx_package():
    manifest = {
        "runtime": "onnx", "runtime_artifact": "model.onnx",
        "model_graph": {"model_graph_type": "single_stage_model_graph",
                        "stages": [{"type": "yolo_object_detection",
                                    "input_shape": [1, 3, 640, 640]}]},
        "task": "object_detection",
        "detection": {"layout": "yolo", "num_classes": 1, "score_threshold": 0.25,
                      "network_input": 640, "preserve_aspect": False, "iou_threshold": 0.45},
    }
    return _tar({
        "config.yaml": b"dataset:\n  image_width: 640\n  image_height: 640\n",
        "export_artifacts/manifest.json": json.dumps(manifest).encode(),
        "export_artifacts/model.onnx": ONNX_BYTES,
    })


@pytest.fixture(scope="module")
def env(aws_stack):
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME
    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "training_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    s3 = aws_stack.s3
    s3.create_bucket(Bucket=USECASE_BUCKET)
    s3.put_object(Bucket=USECASE_BUCKET, Key=DETECTION_ARTIFACT_KEY,
                  Body=_detection_artifact(1280, [1, 5, 33600]))
    s3.put_object(Bucket=USECASE_BUCKET, Key=DETECTION_ARTIFACT_640_KEY,
                  Body=_detection_artifact(640, [1, 6, 8400]))
    s3.put_object(Bucket=USECASE_BUCKET, Key=NO_ONNX_ARTIFACT_KEY,
                  Body=_tar({"best.pt": b"pt", "training_metadata.json": b"{}"}))
    s3.put_object(Bucket=USECASE_BUCKET, Key=LFV_ARTIFACT_KEY,
                  Body=_tar({"mochi.pt": b"pt",
                             "mochi.json": json.dumps({"stages": [{"input_shape": [1, 3, 224, 224]}]}).encode()}))
    s3.put_object(Bucket=USECASE_BUCKET, Key=IMPORTED_ARTIFACT_KEY, Body=_imported_onnx_package())

    compilation = _load_module("compilation.py", "portal_compilation_detection")
    packaging = _load_module("packaging.py", "portal_packaging_detection")

    def _dispatch(service_name, usecase, session_name=None, region=None):
        if service_name == "sagemaker":
            return ExplodingSageMaker()
        return boto3.client(service_name, region_name=region or REGION)

    compilation.get_usecase_client = _dispatch
    packaging.get_usecase_client = _dispatch

    usecase_id = f"uc-{uuid.uuid4()}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id, "name": "Detection Pkg Use Case",
        "account_id": ACCOUNT_ID, "s3_bucket": USECASE_BUCKET, "region": REGION,
        "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:root",
    })
    user_id = f"user-{uuid.uuid4()}"
    aws_stack.tables.user_roles.put_item(Item={
        "user_id": user_id, "usecase_id": usecase_id, "role": "DataScientist"})

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        compilation=compilation, packaging=packaging, s3=s3,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        usecase_id=usecase_id, user_id=user_id,
    )


def _auth(env):
    return {"authorizer": {"claims": {
        "sub": env.user_id, "email": f"{env.user_id}@example.com",
        "cognito:username": env.user_id}}}


def seed_detection_record(env, artifact_key=DETECTION_ARTIFACT_KEY, **overrides):
    training_id = str(uuid.uuid4())
    item = {
        "training_id": training_id, "usecase_id": env.usecase_id,
        "model_name": "blue-plate", "model_version": "2.0.0",
        "model_type": "object_detection", "runtime": "onnx",
        "status": "Completed", "artifact_s3": f"s3://{USECASE_BUCKET}/{artifact_key}",
        "created_at": 1_700_000_000_000, "updated_at": 1_700_000_000_000,
        "detection": {
            "detection_arch": "yolo",
            "network_input_width": 1280, "network_input_height": 1280,
            "class_names": ["blue_plate"], "num_classes": 1,
            "score_threshold": Decimal("0.25"), "iou_threshold": Decimal("0.45"),
            "preserve_aspect": True, "imgsz": 1280, "epochs": 100, "batch": 4,
            "base_weights": "yolo11s.pt", "patience": 30, "onnx_opset": 17,
        },
    }
    item.update(overrides)
    env.training_jobs.put_item(Item=item)
    return training_id


def compile_(env, training_id, targets=None):
    response = env.compilation.start_compilation_job({
        "httpMethod": "POST", "path": f"/api/v1/training/{training_id}/compile",
        "pathParameters": {"id": training_id},
        "body": json.dumps({"targets": targets or ["jetson-xavier-jp6"]}),
        "requestContext": _auth(env),
    }, None)
    return response["statusCode"], json.loads(response["body"])


def package(env, training_id, body=None):
    response = env.packaging.package_components({
        "httpMethod": "POST", "path": f"/api/v1/training/{training_id}/package",
        "pathParameters": {"id": training_id},
        "body": json.dumps(body or {}),
        "requestContext": _auth(env),
    }, None)
    return response["statusCode"], json.loads(response["body"])


def _read_zip(env, s3_uri):
    bucket, key = s3_uri[len("s3://"):].split("/", 1)
    data = env.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = sorted(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        onnx = zf.read("yolo_object_detection/model.onnx") \
            if "yolo_object_detection/model.onnx" in names else None
    return names, manifest, onnx


# ---------------------------------------------------------------------------
# Compile bypass (Req 5.1)
# ---------------------------------------------------------------------------

def test_compile_detection_record_is_bypassed_without_sagemaker(env):
    training_id = seed_detection_record(env)
    status, body = compile_(env, training_id)
    assert status == 200, body
    assert body["compilation_skipped"] is True
    assert body["compilation_jobs"] == []
    assert "not required" in body["message"]
    item = env.training_jobs.get_item(Key={"training_id": training_id})["Item"]
    assert item["compilation_skipped"] is True
    assert "compilation_jobs" not in item


def test_compile_detection_record_not_completed_is_400(env):
    training_id = seed_detection_record(env, status="InProgress")
    status, body = compile_(env, training_id)
    assert status == 400
    assert "must be completed" in body["error"]


def test_compile_imported_detection_still_uses_import_predicate(env):
    """source='imported' is NOT a trained detection record; it keeps taking the
    existing imported-ONNX bypass (same outward behaviour)."""
    training_id = seed_detection_record(
        env, source="imported", metadata={"framework": "ONNX"})
    assert not env.compilation.is_trained_detection_record(
        env.training_jobs.get_item(Key={"training_id": training_id})["Item"])
    status, body = compile_(env, training_id)
    assert status == 200 and body["compilation_skipped"] is True


# ---------------------------------------------------------------------------
# Package (Req 5.2 – 5.6)
# ---------------------------------------------------------------------------

def test_package_detection_builds_component_zip(env):
    training_id = seed_detection_record(env)
    status, body = package(env, training_id)
    assert status == 200, body
    pcs = body["packaged_components"]
    assert [c["target"] for c in pcs] == [
        "jetson-xavier-jp5", "jetson-xavier-jp6", "jetson-xavier-jp7", "x86_64-cpu"]
    assert all(c["status"] == "packaged" for c in pcs)
    uris = {c["component_package_s3"] for c in pcs}
    assert len(uris) == 1
    uri = uris.pop()
    assert uri.startswith(f"s3://{USECASE_BUCKET}/model_artifacts/model-")
    assert body["component_creation_triggered"] is False

    names, manifest, onnx = _read_zip(env, uri)
    assert names == ["manifest.json", "yolo_object_detection/model.onnx"]
    assert onnx == ONNX_BYTES

    assert manifest["runtime"] == "onnx" and manifest["runtime_artifact"] == "model.onnx"
    assert manifest["task"] == "object_detection"
    stage = manifest["model_graph"]["stages"][0]
    assert stage["type"] == "yolo_object_detection"
    assert stage["input_shape"] == [1, 3, 1280, 1280]
    assert stage["output_shape"] == [1, 5, 33600]        # from training_metadata.json
    assert stage["image_width"] == 1280 and stage["image_height"] == 1280
    assert stage["image_range_scale"] is True and stage["normalize"] is False
    assert stage["threshold"] == 0.25 and stage["num_classes"] == 1
    assert manifest["input_shape"] == [1, 3, 1280, 1280]
    assert manifest["preprocessing"] == {"resize": [1280, 1280], "channel_order": "RGB"}
    assert manifest["dataset"] == {"image_width": 1280, "image_height": 1280}
    assert manifest["detection"] == {
        "layout": "yolo", "num_classes": 1, "score_threshold": 0.25,
        "network_input": 1280, "preserve_aspect": True, "iou_threshold": 0.45,
        "class_names": ["blue_plate"],
    }

    item = env.training_jobs.get_item(Key={"training_id": training_id})["Item"]
    assert [c["target"] for c in item["packaged_components"]] == [c["target"] for c in pcs]


def test_package_detection_respects_requested_targets_and_thresholds(env):
    training_id = seed_detection_record(env, detection={
        "detection_arch": "yolo", "network_input_width": 1280, "network_input_height": 1280,
        "class_names": ["plate", "luggage"], "num_classes": 2,
        "score_threshold": Decimal("0.3"), "iou_threshold": Decimal("0.6"),
        "preserve_aspect": True,
    })
    status, body = package(env, training_id, {"targets": ["jetson-xavier-jp7"]})
    assert status == 200, body
    assert [c["target"] for c in body["packaged_components"]] == ["jetson-xavier-jp7"]
    _names, manifest, _ = _read_zip(env, body["packaged_components"][0]["component_package_s3"])
    assert manifest["detection"]["class_names"] == ["plate", "luggage"]
    assert manifest["detection"]["num_classes"] == 2
    assert manifest["detection"]["score_threshold"] == 0.3
    assert manifest["detection"]["iou_threshold"] == 0.6
    assert manifest["model_graph"]["stages"][0]["threshold"] == 0.3


def test_package_detection_artifact_imgsz_wins_over_record(env):
    """Req 5.5: the graph was exported at 640 even though the record says 1280."""
    training_id = seed_detection_record(env, artifact_key=DETECTION_ARTIFACT_640_KEY)
    status, body = package(env, training_id)
    assert status == 200, body
    _names, manifest, _ = _read_zip(env, body["packaged_components"][0]["component_package_s3"])
    stage = manifest["model_graph"]["stages"][0]
    assert stage["input_shape"] == [1, 3, 640, 640]
    assert stage["output_shape"] == [1, 6, 8400]
    assert manifest["detection"]["network_input"] == 640
    assert manifest["preprocessing"]["resize"] == [640, 640]
    assert manifest["dataset"] == {"image_width": 640, "image_height": 640}


def test_package_detection_without_onnx_is_500_and_leaves_record_untouched(env):
    training_id = seed_detection_record(env, artifact_key=NO_ONNX_ARTIFACT_KEY)
    status, body = package(env, training_id)
    assert status == 500, body
    assert "No .onnx model file found" in body["error"]
    item = env.training_jobs.get_item(Key={"training_id": training_id})["Item"]
    assert "packaged_components" not in item


def test_package_detection_not_completed_is_400(env):
    training_id = seed_detection_record(env, status="InProgress")
    status, body = package(env, training_id)
    assert status == 400
    assert "must be completed" in body["error"]


def test_package_detection_auto_triggered_invokes_publish(env, monkeypatch):
    calls = []

    def fake_trigger(training_id, training_job):
        calls.append((training_id, training_job.get("model_name")))

    monkeypatch.setattr(env.packaging, "_trigger_component_creation", fake_trigger)
    training_id = seed_detection_record(env)
    status, body = package(env, training_id, {"auto_triggered": True})
    assert status == 200, body
    assert body["component_creation_triggered"] is True
    assert calls == [(training_id, "blue-plate")]


# ---------------------------------------------------------------------------
# Other record kinds keep their own path (Req 5.8)
# ---------------------------------------------------------------------------

def test_package_imported_onnx_still_uses_import_path(env, monkeypatch):
    seen = {"detection": 0, "import": 0}
    real_import = env.packaging.package_onnx_component

    def spy_import(*a, **kw):
        seen["import"] += 1
        return real_import(*a, **kw)

    def spy_detection(*a, **kw):
        seen["detection"] += 1
        raise AssertionError("trained-detection packager must not run for an import")

    monkeypatch.setattr(env.packaging, "package_onnx_component", spy_import)
    monkeypatch.setattr(env.packaging, "package_trained_detection_component", spy_detection)
    training_id = seed_detection_record(
        env, artifact_key=IMPORTED_ARTIFACT_KEY, source="imported",
        metadata={"framework": "ONNX", "model_file": "model.onnx"})
    status, body = package(env, training_id)
    assert status == 200, body
    assert seen == {"detection": 0, "import": 1}
    assert body["message"].startswith("Packaged ONNX component")


def test_package_lfv_record_requires_compilation_jobs(env):
    training_id = seed_detection_record(
        env, artifact_key=LFV_ARTIFACT_KEY, model_type="classification")
    env.training_jobs.update_item(
        Key={"training_id": training_id},
        UpdateExpression="REMOVE runtime, detection")
    status, body = package(env, training_id)
    assert status == 400
    assert "No compilation jobs found" in body["error"]


# ---------------------------------------------------------------------------
# RF-DETR (rfdetr-training-and-transfer-learning Req 4.2, 4.3)
# ---------------------------------------------------------------------------
# Validates: Requirements 4.1, 4.2, 4.3

RFDETR_STAGE_DIR = "rf_detr_object_detection"


def _rfdetr_artifact(resolution, num_classes, top_k=300, class_names=None,
                     detection_arch="rf_detr"):
    """The FLAT artifact train_rfdetr.py writes (Req 1.7): model.onnx +
    checkpoint_best_total.pth + training_metadata.json with the two-output
    shapes recorded under `onnx_output_shapes`."""
    class_names = class_names or [f"c{i}" for i in range(num_classes)]
    meta = {
        "detection_arch": detection_arch, "rfdetr_size": "small",
        "resolution": resolution, "num_classes": num_classes, "class_names": class_names,
        "epochs": 100, "opset": 17, "onnx_opset": 17,
        "onnx_input_shape": [1, 3, resolution, resolution],
        "onnx_output_shapes": [[1, top_k, 4], [1, top_k, num_classes]],
        "top_k": top_k, "normalize": True, "preserve_aspect": False,
        "metrics": {"test_map50": 0.9}, "base_model": None,
        "manifest_s3": "s3://x/labeled/output.manifest", "images_s3": None,
        "device_manifest_hints": {"layout": "rf_detr", "network_input": resolution,
                                  "preserve_aspect": False, "normalize": True,
                                  "score_threshold": 0.5, "top_k": top_k},
    }
    return _tar({
        "model.onnx": ONNX_BYTES,
        "checkpoint_best_total.pth": b"pth",
        "training_metadata.json": json.dumps(meta).encode(),
    })


def _seed_rfdetr_artifact(env, **kwargs):
    key = f"models/training/rfdetr-{uuid.uuid4()}/output/model.tar.gz"
    env.s3.put_object(Bucket=USECASE_BUCKET, Key=key, Body=_rfdetr_artifact(**kwargs))
    return key


def _rfdetr_detection_fields(resolution=512, class_names=("scratch", "dent"), **overrides):
    """Detection_Record_Fields training.py persists for an RF-DETR job
    (Req 3.3): top_k instead of iou_threshold, preserve_aspect False."""
    fields = {
        "detection_arch": "rf_detr",
        "network_input_width": resolution, "network_input_height": resolution,
        "class_names": list(class_names), "num_classes": len(class_names),
        "score_threshold": Decimal("0.5"), "top_k": 300, "preserve_aspect": False,
        "rfdetr_size": "small", "resolution": resolution, "epochs": 100, "batch": 4,
        "grad_accum": 4, "lr": Decimal("0.0001"), "patience": 10, "onnx_opset": 17,
    }
    fields.update(overrides)
    return fields


def _read_zip_members(env, s3_uri):
    """Like _read_zip but arch-agnostic: (sorted names, manifest, {name: bytes})."""
    bucket, key = s3_uri[len("s3://"):].split("/", 1)
    data = env.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = sorted(zf.namelist())
        members = {n: zf.read(n) for n in names}
    return names, json.loads(members["manifest.json"]), members


def test_package_rfdetr_builds_component_zip(env):
    """Req 4.2: an RF-DETR artifact becomes a component with the
    rf_detr_object_detection stage, ImageNet normalisation, square resize,
    top_k and NO iou_threshold; model.onnx nests under the RF-DETR stage dir
    and the checkpoint does not ship."""
    key = _seed_rfdetr_artifact(env, resolution=512, num_classes=2, top_k=300,
                                class_names=["scratch", "dent"])
    training_id = seed_detection_record(
        env, artifact_key=key, detection=_rfdetr_detection_fields(512))
    status, body = package(env, training_id)
    assert status == 200, body
    pcs = body["packaged_components"]
    assert [c["target"] for c in pcs] == [
        "jetson-xavier-jp5", "jetson-xavier-jp6", "jetson-xavier-jp7", "x86_64-cpu"]
    assert len({c["component_package_s3"] for c in pcs}) == 1

    names, manifest, members = _read_zip_members(env, pcs[0]["component_package_s3"])
    assert names == ["manifest.json", f"{RFDETR_STAGE_DIR}/model.onnx"]
    assert members[f"{RFDETR_STAGE_DIR}/model.onnx"] == ONNX_BYTES
    assert not any(n.endswith(".pth") or n.endswith(".pt") for n in names)

    assert manifest["runtime"] == "onnx" and manifest["runtime_artifact"] == "model.onnx"
    assert manifest["task"] == "object_detection"
    stage = manifest["model_graph"]["stages"][0]
    assert stage["type"] == RFDETR_STAGE_DIR
    assert stage["input_shape"] == [1, 3, 512, 512]
    assert stage["normalize"] is True and stage["image_range_scale"] is True
    assert stage["image_width"] == 512 and stage["image_height"] == 512
    assert stage["threshold"] == 0.5 and stage["num_classes"] == 2
    # Real two-output shape from training_metadata.json: the single
    # `output_shape` slot carries the logits tensor, both are kept alongside.
    assert stage["output_shape"] == [1, 300, 2]
    assert stage["output_shapes"] == [[1, 300, 4], [1, 300, 2]]
    assert manifest["input_shape"] == [1, 3, 512, 512]
    assert manifest["preprocessing"] == {"resize": [512, 512], "channel_order": "RGB"}
    assert manifest["dataset"] == {"image_width": 512, "image_height": 512}
    assert manifest["detection"] == {
        "layout": "rf_detr", "num_classes": 2, "score_threshold": 0.5,
        "network_input": 512, "preserve_aspect": False, "top_k": 300,
        "class_names": ["scratch", "dent"],
    }
    assert "iou_threshold" not in json.dumps(manifest)


def test_package_rfdetr_artifact_resolution_and_top_k_win_over_record(env):
    """The graph was exported at 576 with Q=300 even though the record says
    512 / top_k 100: training_metadata.json's `resolution` and `top_k`
    describe the real graph and win."""
    key = _seed_rfdetr_artifact(env, resolution=576, num_classes=1, top_k=300,
                                class_names=["blue_plate"])
    training_id = seed_detection_record(
        env, artifact_key=key,
        detection=_rfdetr_detection_fields(512, class_names=("blue_plate",), top_k=100))
    status, body = package(env, training_id)
    assert status == 200, body
    _names, manifest, _ = _read_zip_members(env, body["packaged_components"][0]["component_package_s3"])
    stage = manifest["model_graph"]["stages"][0]
    assert stage["type"] == RFDETR_STAGE_DIR
    assert stage["input_shape"] == [1, 3, 576, 576]
    assert stage["output_shape"] == [1, 300, 1]
    assert stage["output_shapes"] == [[1, 300, 4], [1, 300, 1]]
    assert manifest["detection"]["network_input"] == 576
    assert manifest["detection"]["top_k"] == 300
    assert manifest["preprocessing"]["resize"] == [576, 576]
    assert manifest["dataset"] == {"image_width": 576, "image_height": 576}


def test_package_rfdetr_arch_falls_back_to_training_metadata(env):
    """Arch resolution order is record -> training_metadata.json -> yolo: a
    record whose detection block never got `detection_arch` still packages as
    RF-DETR when the artifact says so."""
    key = _seed_rfdetr_artifact(env, resolution=384, num_classes=1, top_k=300,
                                class_names=["blue_plate"])
    fields = _rfdetr_detection_fields(384, class_names=("blue_plate",))
    del fields["detection_arch"]
    training_id = seed_detection_record(env, artifact_key=key, detection=fields)
    status, body = package(env, training_id)
    assert status == 200, body
    names, manifest, _ = _read_zip_members(env, body["packaged_components"][0]["component_package_s3"])
    assert names == ["manifest.json", f"{RFDETR_STAGE_DIR}/model.onnx"]
    stage = manifest["model_graph"]["stages"][0]
    assert stage["type"] == RFDETR_STAGE_DIR and stage["normalize"] is True
    assert manifest["detection"]["layout"] == "rf_detr"
    assert manifest["detection"]["preserve_aspect"] is False
    assert manifest["detection"]["top_k"] == 300
    assert "iou_threshold" not in manifest["detection"]


def test_package_legacy_yolo_record_without_arch_packages_as_yolo(env):
    """Req 4.3: a portal-detection-training record (no `detection_arch` on the
    record, none in its training_metadata.json) takes the YOLO path exactly
    as before."""
    training_id = seed_detection_record(env, detection={
        "network_input_width": 1280, "network_input_height": 1280,
        "class_names": ["blue_plate"], "num_classes": 1,
        "score_threshold": Decimal("0.25"), "iou_threshold": Decimal("0.45"),
        "preserve_aspect": True,
    })
    status, body = package(env, training_id)
    assert status == 200, body
    names, manifest, onnx = _read_zip(env, body["packaged_components"][0]["component_package_s3"])
    assert names == ["manifest.json", "yolo_object_detection/model.onnx"]
    assert onnx == ONNX_BYTES
    stage = manifest["model_graph"]["stages"][0]
    assert stage["type"] == "yolo_object_detection" and stage["normalize"] is False
    assert stage["output_shape"] == [1, 5, 33600]
    assert "output_shapes" not in stage
    assert manifest["detection"] == {
        "layout": "yolo", "num_classes": 1, "score_threshold": 0.25,
        "network_input": 1280, "preserve_aspect": True, "iou_threshold": 0.45,
        "class_names": ["blue_plate"],
    }
