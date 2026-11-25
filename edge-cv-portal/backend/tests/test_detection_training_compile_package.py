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
- RF-DETR-Seg (rfdetr-seg-support task 6.4, last section): the Seg record's
  component per Output_Mode, the three-shape rule and its 400s, the
  output_mode override + packaged_output_mode persistence, the Seg_Switch,
  the imported-ONNX Seg record, the audit model_type; the YOLO / RF-DETR
  cases above are preservation pins and are unmodified.

# Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.8, 9.2
# Validates (rfdetr-seg-support): Requirements 6.3, 6.4, 6.5, 6.6, 8.6, 15.2, 15.3, 17.1
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


# ---------------------------------------------------------------------------
# RF-DETR-Seg (rfdetr-seg-support task 6.4)
# ---------------------------------------------------------------------------
# Validates (rfdetr-seg-support): Requirements 6.3, 6.4, 6.5, 6.6, 8.6, 15.2, 15.3, 17.1
#
# The Seg record (training.py task 5.2) and the FLAT artifact
# train_rfdetr_seg.py writes (design.md Data contracts "training_metadata.json
# (Seg)"): model.onnx + checkpoint_best_total.pth + training_metadata.json with
# the THREE output shapes [1, Q, 4] + [1, Q, C+1] + [1, Q, R/4, R/4] and their
# graph-order names. The YOLO / RF-DETR cases above are preservation pins and
# are untouched.

SEG_INSTANCE_DIR = "rf_detr_instance_segmentation"
SEG_SEMANTIC_DIR = "rf_detr_semantic_segmentation"
SEG_NAMES = ["scratch", "dent"]
SEG_DISABLED = "RF-DETR-Seg is disabled on this portal"
IMPORT_FIXED = ("output_mode cannot be changed for an imported ONNX model: its manifest "
                "was fixed at import; re-import with the other output_mode")


def _seg_artifact(resolution=384, num_classes=2, top_k=100, class_names=None,
                  output_shapes=None, output_names=("dets", "labels", "masks"),
                  omit_shapes=False, hints_mode="instance"):
    class_names = class_names if class_names is not None else SEG_NAMES[:num_classes]
    grid = resolution // 4
    if output_shapes is None:
        output_shapes = [[1, top_k, 4], [1, top_k, num_classes + 1], [1, top_k, grid, grid]]
    meta = {
        "detection_arch": "rf_detr_seg", "model_type": "instance_segmentation",
        "rfdetr_seg_size": "small", "model_class": "RFDETRSegSmall",
        "resolution": resolution, "num_classes": num_classes, "class_names": class_names,
        "epochs": 100, "batch": 4, "grad_accum": 4, "lr": 1e-4, "patience": 10,
        "opset": 17, "onnx_opset": 17, "onnx_input_shape": [1, 3, resolution, resolution],
        "logits_slots": num_classes + 1, "background_slot": num_classes,
        "mask_downsample_ratio": 4, "top_k": top_k,
        "metrics": {"test_map50": 0.9, "test_mask_map50": 0.8},
        "manifest_s3": "s3://x/labeled/output.manifest", "images_s3": None,
        "device_manifest_hints": {"layout": "rf_detr", "stage_type": f"rf_detr_{hints_mode}_segmentation",
                                  "output_mode": hints_mode, "network_input": resolution,
                                  "preserve_aspect": False, "normalize": True,
                                  "score_threshold": 0.5, "mask_threshold": 0.5, "top_k": top_k},
    }
    if not omit_shapes:
        meta["onnx_output_shapes"] = output_shapes
    if output_names is not None:
        meta["onnx_output_names"] = list(output_names)
    return _tar({
        "model.onnx": ONNX_BYTES,
        "checkpoint_best_total.pth": b"pth",
        "training_metadata.json": json.dumps(meta).encode(),
    })


def _seed_seg_artifact(env, **kwargs):
    key = f"models/training/rfdetr-seg-{uuid.uuid4()}/output/model.tar.gz"
    env.s3.put_object(Bucket=USECASE_BUCKET, Key=key, Body=_seg_artifact(**kwargs))
    return key


def _seg_detection_fields(resolution=384, class_names=tuple(SEG_NAMES), output_mode="instance",
                          top_k=100, **overrides):
    """The Detection_Record_Fields training.py persists for a Seg job (Seg
    spec Req 5.5): top_k, mask_threshold and output_mode, no iou_threshold."""
    fields = {
        "detection_arch": "rf_detr_seg",
        "network_input_width": resolution, "network_input_height": resolution,
        "class_names": list(class_names), "num_classes": len(class_names),
        "score_threshold": Decimal("0.5"), "mask_threshold": Decimal("0.5"),
        "top_k": top_k, "preserve_aspect": False,
        "rfdetr_seg_size": "small", "resolution": resolution, "epochs": 100, "batch": 4,
        "grad_accum": 4, "lr": Decimal("0.0001"), "patience": 10, "onnx_opset": 17,
    }
    if output_mode is not None:
        fields["output_mode"] = output_mode
    fields.update(overrides)
    return fields


def seed_seg_record(env, artifact_key, detection=None, **overrides):
    return seed_detection_record(
        env, artifact_key=artifact_key, model_type="instance_segmentation",
        detection=detection or _seg_detection_fields(), **overrides)


def _item(env, training_id):
    return env.training_jobs.get_item(Key={"training_id": training_id})["Item"]


def _packaged(env, body):
    pcs = body["packaged_components"]
    assert len({c["component_package_s3"] for c in pcs}) == 1
    return _read_zip_members(env, pcs[0]["component_package_s3"])


def test_package_seg_instance_builds_component_zip_and_persists_the_mode(env):
    """Req 6.3 / 6.4: the instance-mode component — model.onnx under the
    instance stage dir, the Seg manifest with the real three shapes, and
    `packaged_output_mode` equal to the manifest's detection.output_mode."""
    key = _seed_seg_artifact(env)
    training_id = seed_seg_record(env, key)
    status, body = package(env, training_id)
    assert status == 200, body
    pcs = body["packaged_components"]
    assert [c["target"] for c in pcs] == [
        "jetson-xavier-jp5", "jetson-xavier-jp6", "jetson-xavier-jp7", "x86_64-cpu"]
    assert all(c["status"] == "packaged" for c in pcs)
    assert body["message"].startswith("Packaged detection (ONNX) component")

    names, manifest, members = _packaged(env, body)
    assert names == ["manifest.json", f"{SEG_INSTANCE_DIR}/model.onnx"]
    assert members[f"{SEG_INSTANCE_DIR}/model.onnx"] == ONNX_BYTES
    assert not any(n.endswith(".pth") for n in names)

    assert manifest["runtime"] == "onnx" and manifest["runtime_artifact"] == "model.onnx"
    assert manifest["task"] == "object_detection"
    stage = manifest["model_graph"]["stages"][0]
    assert stage["type"] == SEG_INSTANCE_DIR
    assert stage["input_shape"] == [1, 3, 384, 384]
    assert stage["normalize"] is True and stage["image_range_scale"] is True
    assert stage["threshold"] == 0.5 and stage["num_classes"] == 2
    assert stage["output_shape"] == [1, 100, 3]
    assert stage["output_shapes"] == [[1, 100, 4], [1, 100, 3], [1, 100, 96, 96]]
    assert "detection" not in stage
    assert manifest["model_graph"]["pixel_level_classes"] == {
        "names": ["background", "scratch", "dent"], "normal_ids": [0]}
    assert manifest["preprocessing"] == {"resize": [384, 384], "channel_order": "RGB"}
    assert manifest["dataset"] == {"image_width": 384, "image_height": 384}
    assert manifest["detection"] == {
        "layout": "rf_detr", "num_classes": 2, "score_threshold": 0.5,
        "network_input": 384, "preserve_aspect": False, "top_k": 100,
        "mask_threshold": 0.5, "output_mode": "instance",
        "output_names": ["dets", "labels", "masks"], "class_names": ["scratch", "dent"],
    }
    assert "iou_threshold" not in json.dumps(manifest)

    item = _item(env, training_id)
    assert [c["target"] for c in item["packaged_components"]] == [c["target"] for c in pcs]
    assert item["packaged_output_mode"] == "instance" == manifest["detection"]["output_mode"]


def test_package_seg_semantic_record_mode_selects_the_semantic_stage(env):
    key = _seed_seg_artifact(env)
    training_id = seed_seg_record(env, key, detection=_seg_detection_fields(output_mode="semantic"))
    status, body = package(env, training_id)
    assert status == 200, body
    names, manifest, _ = _packaged(env, body)
    assert names == ["manifest.json", f"{SEG_SEMANTIC_DIR}/model.onnx"]
    assert manifest["model_graph"]["stages"][0]["type"] == SEG_SEMANTIC_DIR
    assert "task" not in manifest
    assert manifest["detection"]["output_mode"] == "semantic"
    assert manifest["model_graph"]["pixel_level_classes"]["names"] == ["background", "scratch", "dent"]
    assert _item(env, training_id)["packaged_output_mode"] == "semantic"


def test_package_seg_output_mode_override_repackages_in_the_other_mode(env):
    """Req 6.4: the Package body's output_mode wins over the record's and is
    what gets persisted; the next package without an override returns to the
    record's stored mode (the override is per package, not a record edit)."""
    key = _seed_seg_artifact(env)
    training_id = seed_seg_record(env, key)
    status, body = package(env, training_id, {"output_mode": "semantic"})
    assert status == 200, body
    names, manifest, _ = _packaged(env, body)
    assert names == ["manifest.json", f"{SEG_SEMANTIC_DIR}/model.onnx"]
    assert manifest["detection"]["output_mode"] == "semantic" and "task" not in manifest
    item = _item(env, training_id)
    assert item["packaged_output_mode"] == "semantic"
    assert item["detection"]["output_mode"] == "instance"      # the record itself is untouched

    status, body = package(env, training_id, {"targets": ["jetson-xavier-jp6"]})
    assert status == 200, body
    names, manifest, _ = _packaged(env, body)
    assert names == ["manifest.json", f"{SEG_INSTANCE_DIR}/model.onnx"]
    assert manifest["detection"]["output_mode"] == "instance"
    assert _item(env, training_id)["packaged_output_mode"] == "instance"


def test_package_seg_record_without_a_stored_mode_falls_back_to_the_default(env):
    """A Seg record whose detection block carries no output_mode packages in
    DEFAULT_OUTPUT_MODE (record -> hints -> default; the handler never reads
    the artifact's hints, so the default is what it resolves to)."""
    key = _seed_seg_artifact(env, hints_mode="semantic")
    training_id = seed_seg_record(env, key, detection=_seg_detection_fields(output_mode=None))
    status, body = package(env, training_id)
    assert status == 200, body
    _names, manifest, _ = _packaged(env, body)
    assert manifest["detection"]["output_mode"] == "instance"
    assert _item(env, training_id)["packaged_output_mode"] == "instance"


@pytest.mark.parametrize("bad", ["panoptic", "", 7])
def test_package_seg_invalid_output_mode_is_400_before_anything_is_packaged(env, bad):
    key = _seed_seg_artifact(env)
    training_id = seed_seg_record(env, key)
    status, body = package(env, training_id, {"output_mode": bad})
    assert status == 400, body
    assert "output_mode" in body["error"] and "instance, semantic" in body["error"]
    item = _item(env, training_id)
    assert "packaged_components" not in item and "packaged_output_mode" not in item


def test_effective_output_mode_precedence(env):
    f = env.packaging.effective_output_mode
    assert f({"output_mode": "semantic"}, {"output_mode": "instance"}, "instance") == "instance"
    assert f({"output_mode": "semantic"}, {"output_mode": "instance"}) == "semantic"
    assert f({}, {"output_mode": "semantic"}) == "semantic"
    assert f({"output_mode": None}, {"output_mode": "semantic"}) == "semantic"
    assert f({}, {}) == "instance" == env.packaging.DEFAULT_OUTPUT_MODE
    assert f(None) == "instance"
    assert f({"output_mode": " Semantic "}) == "semantic"      # normalised like the handler


# ── the three-shape rule (Req 6.3) ──────────────────────────────────────────

def _seed_seg_with_shapes(env, shapes, names=("dets", "labels", "masks"), **kwargs):
    key = _seed_seg_artifact(env, output_shapes=shapes, output_names=names, **kwargs)
    return seed_seg_record(env, key)


def _assert_refused_untouched(env, training_id, status, body, *fragments):
    assert status == 400, body
    for fragment in fragments:
        assert fragment in body["error"], body
    item = _item(env, training_id)
    assert "packaged_components" not in item and "packaged_output_mode" not in item


def test_package_seg_two_output_artifact_is_400_quoting_the_shapes(env):
    """A detection artifact mislabelled as Seg: refused, never packaged."""
    training_id = _seed_seg_with_shapes(env, [[1, 100, 4], [1, 100, 3]], names=("dets", "labels"))
    status, body = package(env, training_id)
    _assert_refused_untouched(env, training_id, status, body,
                              "Seg record has a two-output artifact", "[[1, 100, 4], [1, 100, 3]]")


@pytest.mark.parametrize("shapes", [
    [[1, 100, 4], [1, 100, 3], [1, 100, 96]],            # rank-3 mask entry
    [[1, 100, 4], [1, 100, 96, 96], [1, 100, 96, 96]],   # two rank-4 entries, no logits
    [[1, 100, 4, 1], [1, 100, 3], [1, 100, 96, 96]],     # a rank-4 entry where boxes should be
    [[1, 100, 4], [1, 100, 3], [1, 100, 96, 96], [1]],   # four entries
    [[1, 100, 4], [1, None, 3], [1, 100, 96, 96]],       # non-int dim
    [[1, 100, 4], [1, 100, "3"], [1, 100, 96, 96]],      # non-int dim (str)
    [[1, 100, 5], [1, 100, 3], [1, 100, 96, 96]],        # no [1, Q, 4] boxes entry
], ids=["rank3-mask", "two-rank4", "rank4-boxes", "four-entries", "none-dim", "str-dim", "no-boxes"])
def test_package_seg_wrong_rank_or_non_int_shapes_are_400_not_a_silent_skip(env, shapes):
    """tasks review pass 9 finding 2 (C29): a guard that only widened the
    length test would have skipped the shapes silently; every malformed set
    is refused with the shapes quoted."""
    training_id = _seed_seg_with_shapes(env, shapes)
    status, body = package(env, training_id)
    _assert_refused_untouched(env, training_id, status, body, "Seg record", str(shapes))


def test_package_seg_missing_output_shapes_is_400(env):
    key = _seed_seg_artifact(env, omit_shapes=True)
    training_id = seed_seg_record(env, key)
    status, body = package(env, training_id)
    _assert_refused_untouched(env, training_id, status, body, "Seg record", "None")


def test_package_seg_shapes_are_identified_by_shape_not_position(env):
    """masks listed before labels still yields output_shape = logits and
    output_shapes = [boxes, logits, masks]."""
    training_id = _seed_seg_with_shapes(
        env, [[1, 100, 4], [1, 100, 96, 96], [1, 100, 3]], names=("dets", "masks", "labels"))
    status, body = package(env, training_id)
    assert status == 200, body
    _names, manifest, _ = _packaged(env, body)
    stage = manifest["model_graph"]["stages"][0]
    assert stage["output_shape"] == [1, 100, 3]
    assert stage["output_shapes"] == [[1, 100, 4], [1, 100, 3], [1, 100, 96, 96]]
    assert manifest["detection"]["output_names"] == ["dets", "masks", "labels"]  # graph order kept


def test_package_seg_logits_listed_first_still_pick_the_boxes_by_shape(env):
    training_id = _seed_seg_with_shapes(
        env, [[1, 100, 3], [1, 100, 96, 96], [1, 100, 4]], names=("labels", "masks", "dets"))
    status, body = package(env, training_id)
    assert status == 200, body
    stage = _packaged(env, body)[1]["model_graph"]["stages"][0]
    assert stage["output_shape"] == [1, 100, 3]
    assert stage["output_shapes"] == [[1, 100, 4], [1, 100, 3], [1, 100, 96, 96]]


@pytest.mark.parametrize("names", [
    ("dets", "labels", "masks"), ("labels", "dets", "masks"), None, ("a", "b", "c"),
], ids=["dets-first", "labels-first", "nameless", "foreign-names"])
def test_package_seg_three_class_record_is_never_a_400(env, names):
    """Three classes: C + 1 == 4, so both rank-3 tensors are [1, Q, 4]. With
    aligned names `dets` is the box tensor; nameless (or with foreign names)
    list order decides. The two shapes are equal, so the manifest is the same
    either way (C33)."""
    three = ["a", "b", "c"]
    key = _seed_seg_artifact(env, num_classes=3, class_names=three,
                             output_shapes=[[1, 100, 4], [1, 100, 4], [1, 100, 96, 96]],
                             output_names=names)
    training_id = seed_seg_record(env, key, detection=_seg_detection_fields(class_names=three))
    status, body = package(env, training_id)
    assert status == 200, body
    _n, manifest, _ = _packaged(env, body)
    stage = manifest["model_graph"]["stages"][0]
    assert stage["output_shape"] == [1, 100, 4]
    assert stage["output_shapes"] == [[1, 100, 4], [1, 100, 4], [1, 100, 96, 96]]
    assert manifest["model_graph"]["pixel_level_classes"]["names"] == ["background"] + three
    if names is None:
        assert "output_names" not in manifest["detection"]
    else:
        assert manifest["detection"]["output_names"] == list(names)


def test_seg_output_shapes_helper_settles_three_classes_by_name(env):
    """The pure picker: with aligned names the `dets` entry is boxes whatever
    its position; without, list order (the shapes being equal, the result is
    identical to the caller)."""
    pick = env.packaging._seg_output_shapes
    boxes, logits, masks = pick([[1, 100, 4], [1, 100, 4], [1, 100, 96, 96]], ["labels", "dets", "masks"])
    assert (boxes, logits, masks) == ([1, 100, 4], [1, 100, 4], [1, 100, 96, 96])
    # Unequal three-class look-alike: Q differs, so the pick is visible.
    assert pick([[1, 100, 4], [1, 200, 4], [1, 100, 96, 96]], ["labels", "dets", "masks"]) == (
        [1, 200, 4], [1, 100, 4], [1, 100, 96, 96])
    assert pick([[1, 100, 4], [1, 200, 4], [1, 100, 96, 96]], None) == (
        [1, 100, 4], [1, 200, 4], [1, 100, 96, 96])
    assert pick([[1, 100, 96, 96], [1, 100, 3], [1, 100, 4]], ["masks", "labels", "dets"]) == (
        [1, 100, 4], [1, 100, 3], [1, 100, 96, 96])
    with pytest.raises(env.packaging.DetectionPackagingError):
        pick([[1, 100, 4], [1, 100, 3]], None)


# ── preservation of the detection paths (Req 6.5, 17.1) ─────────────────────

def test_yolo_and_rfdetr_packages_write_no_packaged_output_mode(env):
    """The post-package item key set is exactly today's: the seeded keys plus
    packaged_components (updated_at is already seeded); no packaged_output_mode."""
    yolo_id = seed_detection_record(env)
    before = set(_item(env, yolo_id))
    status, body = package(env, yolo_id)
    assert status == 200, body
    after = _item(env, yolo_id)
    assert set(after) == before | {"packaged_components"}
    assert "packaged_output_mode" not in after

    key = _seed_rfdetr_artifact(env, resolution=512, num_classes=2, top_k=300, class_names=SEG_NAMES)
    rf_id = seed_detection_record(env, artifact_key=key, detection=_rfdetr_detection_fields(512))
    before = set(_item(env, rf_id))
    status, body = package(env, rf_id)
    assert status == 200, body
    after = _item(env, rf_id)
    assert set(after) == before | {"packaged_components"}
    assert "packaged_output_mode" not in after
    _names, manifest, _ = _packaged(env, body)
    assert "output_mode" not in manifest["detection"] and "pixel_level_classes" not in manifest["model_graph"]


def test_package_output_mode_on_a_non_seg_record_is_400_naming_the_field(env):
    """design.md Data contracts: a Seg-specific field on a non-Seg request is
    refused, never a silent no-op."""
    training_id = seed_detection_record(env)
    status, body = package(env, training_id, {"output_mode": "semantic"})
    assert status == 400, body
    assert body["error"] == "output_mode applies to instance_segmentation records only"
    assert "packaged_components" not in _item(env, training_id)


# ── Seg_Switch (Req 15.2, 15.3) ─────────────────────────────────────────────

def test_package_seg_override_is_403_when_the_switch_is_off(env, monkeypatch):
    monkeypatch.setenv("PORTAL_RFDETR_SEG_ENABLED", "false")
    key = _seed_seg_artifact(env)
    training_id = seed_seg_record(env, key)
    status, body = package(env, training_id, {"output_mode": "semantic"})
    assert status == 403, body
    assert body["error"] == SEG_DISABLED
    item = _item(env, training_id)
    assert "packaged_components" not in item and "packaged_output_mode" not in item


def test_package_seg_without_an_override_packages_the_stored_mode_when_off(env, monkeypatch):
    monkeypatch.setenv("PORTAL_RFDETR_SEG_ENABLED", "false")
    key = _seed_seg_artifact(env)
    training_id = seed_seg_record(env, key, detection=_seg_detection_fields(output_mode="semantic"))
    status, body = package(env, training_id)
    assert status == 200, body
    names, manifest, _ = _packaged(env, body)
    assert names == ["manifest.json", f"{SEG_SEMANTIC_DIR}/model.onnx"]
    assert manifest["detection"]["output_mode"] == "semantic"
    assert _item(env, training_id)["packaged_output_mode"] == "semantic"


@pytest.mark.parametrize("value", [None, "true", "TRUE", "1", "anything"])
def test_package_seg_override_allowed_when_the_switch_is_on_or_absent(env, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PORTAL_RFDETR_SEG_ENABLED", raising=False)
    else:
        monkeypatch.setenv("PORTAL_RFDETR_SEG_ENABLED", value)
    key = _seed_seg_artifact(env)
    training_id = seed_seg_record(env, key)
    status, body = package(env, training_id, {"output_mode": "semantic"})
    assert status == 200, body
    assert _item(env, training_id)["packaged_output_mode"] == "semantic"


def test_switch_off_leaves_yolo_packaging_alone(env, monkeypatch):
    monkeypatch.setenv("PORTAL_RFDETR_SEG_ENABLED", "false")
    training_id = seed_detection_record(env)
    status, body = package(env, training_id)
    assert status == 200, body
    assert "packaged_output_mode" not in _item(env, training_id)


# ── imported ONNX Seg record (Req 8.6, C34) ─────────────────────────────────

def _imported_seg_package(stage_type=SEG_INSTANCE_DIR):
    manifest = {
        "runtime": "onnx", "runtime_artifact": "model.onnx",
        "model_graph": {"model_graph_type": "single_stage_model_graph",
                        "stages": [{"type": stage_type, "input_shape": [1, 3, 384, 384]}],
                        "pixel_level_classes": {"names": ["background"] + SEG_NAMES, "normal_ids": [0]}},
        "task": "object_detection",
        "detection": {"layout": "rf_detr", "num_classes": 2, "score_threshold": 0.5,
                      "network_input": 384, "preserve_aspect": False, "top_k": 100,
                      "mask_threshold": 0.5, "output_mode": "instance",
                      "output_names": ["dets", "labels", "masks"], "class_names": SEG_NAMES},
    }
    return _tar({
        "config.yaml": b"dataset:\n  image_width: 384\n  image_height: 384\n",
        "export_artifacts/manifest.json": json.dumps(manifest).encode(),
        "export_artifacts/model.onnx": ONNX_BYTES,
    })


def _seed_imported_seg_record(env):
    key = f"converted-models/imported-seg-{uuid.uuid4()}.tar.gz"
    env.s3.put_object(Bucket=USECASE_BUCKET, Key=key, Body=_imported_seg_package())
    return seed_detection_record(
        env, artifact_key=key, source="imported", model_type="instance_segmentation",
        metadata={"framework": "ONNX", "model_file": "model.onnx"},
        detection={"detection_arch": "rf_detr_seg", "num_classes": 2, "class_names": SEG_NAMES,
                   "score_threshold": Decimal("0.5"), "mask_threshold": Decimal("0.5"), "top_k": 100,
                   "network_input_width": 384, "network_input_height": 384,
                   "preserve_aspect": False, "output_mode": "instance"})


def test_package_imported_seg_record_refuses_an_output_mode_override(env, monkeypatch):
    seen = []
    monkeypatch.setattr(env.packaging, "package_trained_detection_component",
                        lambda *a, **kw: seen.append("trained") or pytest.fail("trained packager ran"))
    training_id = _seed_imported_seg_record(env)
    status, body = package(env, training_id, {"output_mode": "semantic"})
    assert status == 400, body
    assert body["error"] == IMPORT_FIXED
    item = _item(env, training_id)
    assert "packaged_components" not in item and "packaged_output_mode" not in item
    assert seen == []


def test_package_imported_seg_record_without_an_override_packages_as_today(env, monkeypatch):
    """Req 8.6: the import path re-zips the import-time package (model.onnx
    under the Seg_Stage_Type dir, manifest untouched) and writes no
    packaged_output_mode."""
    monkeypatch.setattr(env.packaging, "package_trained_detection_component",
                        lambda *a, **kw: pytest.fail("trained-detection packager must not run for an import"))
    training_id = _seed_imported_seg_record(env)
    before = set(_item(env, training_id))
    status, body = package(env, training_id)
    assert status == 200, body
    assert body["message"].startswith("Packaged ONNX component")
    names, manifest, _ = _packaged(env, body)
    assert names == ["manifest.json", f"{SEG_INSTANCE_DIR}/model.onnx"]
    assert manifest["detection"]["output_mode"] == "instance"
    assert manifest["dataset"] == {"image_width": 384, "image_height": 384}
    after = _item(env, training_id)
    assert set(after) == before | {"packaged_components"}
    assert "packaged_output_mode" not in after


# ── audit (C33) ─────────────────────────────────────────────────────────────

def test_audit_model_type_names_the_record_type(env, monkeypatch):
    audits = []
    monkeypatch.setattr(env.packaging, "log_audit_event", lambda **kw: audits.append(kw))
    key = _seed_seg_artifact(env)
    seg_id = seed_seg_record(env, key)
    status, _ = package(env, seg_id)
    assert status == 200
    yolo_id = seed_detection_record(env)
    status, _ = package(env, yolo_id)
    assert status == 200
    by_id = {a["resource_id"]: a for a in audits}
    assert by_id[seg_id]["details"]["model_type"] == "instance_segmentation"
    assert by_id[seg_id]["details"]["runtime"] == "onnx" and by_id[seg_id]["result"] == "success"
    assert by_id[yolo_id]["details"] == {
        "targets": ["jetson-xavier-jp5", "jetson-xavier-jp6", "jetson-xavier-jp7", "x86_64-cpu"],
        "packaged_count": 4, "runtime": "onnx", "model_type": "object_detection"}



# ── direct call: the in-function fallback (record -> hints -> default) ───────

def test_direct_call_without_output_mode_falls_back_to_record_then_hints(env):
    """Only a direct call reaches the in-function precedence: a record with no
    stored mode takes the artifact's device_manifest_hints.output_mode; the
    handler path always passes the mode it resolved (and persists)."""
    key = _seed_seg_artifact(env, hints_mode="semantic")
    training_id = seed_seg_record(env, key, detection=_seg_detection_fields(output_mode=None))
    job = _item(env, training_id)
    uri = env.packaging.package_trained_detection_component(
        job["artifact_s3"], job, env.s3, {"s3_bucket": USECASE_BUCKET})
    names, manifest, _ = _read_zip_members(env, uri)
    assert names == ["manifest.json", f"{SEG_SEMANTIC_DIR}/model.onnx"]
    assert manifest["detection"]["output_mode"] == "semantic" and "task" not in manifest
    # A given output_mode is used verbatim, whatever the record / hints say.
    uri = env.packaging.package_trained_detection_component(
        job["artifact_s3"], job, env.s3, {"s3_bucket": USECASE_BUCKET}, output_mode="instance")
    names, manifest, _ = _read_zip_members(env, uri)
    assert names == ["manifest.json", f"{SEG_INSTANCE_DIR}/model.onnx"]
    assert manifest["detection"]["output_mode"] == "instance" and manifest["task"] == "object_detection"
    # The record's stored mode beats the hints.
    stored_id = seed_seg_record(env, key, detection=_seg_detection_fields(output_mode="instance"))
    job = _item(env, stored_id)
    uri = env.packaging.package_trained_detection_component(
        job["artifact_s3"], job, env.s3, {"s3_bucket": USECASE_BUCKET})
    assert _read_zip_members(env, uri)[1]["detection"]["output_mode"] == "instance"
