"""
``detector_conversion`` request validation, Conversion_Job request, record and
predicate (detector-checkpoint-import tasks 2.2 and 2.3).

The job-request tests also feed the built ``Environment`` to the export entry
point's own ``read_config`` (datasets/detection_training/export_checkpoint.py,
importable on a bare host), so the portal/job contract is checked from both
ends.
# Validates: Requirements 4.3, 4.4, 4.5, 4.6, 5.1, 5.2, 5.3, 5.4, 9.1
"""
import copy
import importlib.util
import json
import os
import sys
from decimal import Decimal

import pytest

import detection_training
import detector_conversion as dc
from fixtures.detector_probes import (
    PPE_NAMES,
    PPE_PROBE,
    PPE_SHA256,
    RFDETR_OWN_PROBE,
    RFDETR_PUBLISHED_PROBE,
    RFDETR_SEG_OWN_PROBE,
    YOLO11N_SEG_PROBE,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPORT_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "datasets", "detection_training"))
IMAGE = ("123456789012.dkr.ecr.us-east-1.amazonaws.com/dda-detector-export@sha256:"
         + "ab" * 32)
ROLE = "arn:aws:iam::123456789012:role/DDASageMakerExecutionRole"
PREFIX = "s3://uc-bucket/converted-models/ppe_detection-1a2b3c4d/"

PPE = dc.assess_checkpoint(PPE_PROBE)
RF_OWN = dc.assess_checkpoint(RFDETR_OWN_PROBE)
RF_PUBLISHED = dc.assess_checkpoint(RFDETR_PUBLISHED_PROBE)
RF_SEG_OWN = dc.assess_checkpoint(RFDETR_SEG_OWN_PROBE)  # rfdetr-seg-support: Seg-Nano, 312, Q 100


def _load_export_module():
    spec = importlib.util.spec_from_file_location("export_checkpoint_contract",
                                                  os.path.join(_EXPORT_DIR, "export_checkpoint.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bad(body, assessment=PPE):
    with pytest.raises(ValueError) as exc:
        dc.validate_conversion_request(body, assessment)
    return str(exc.value)


# ---------------------------------------------------------------------------
# validate_conversion_request (Req 4.3-4.5)
# ---------------------------------------------------------------------------

def test_yolo_defaults_come_from_the_checkpoint():
    params = dc.validate_conversion_request({}, PPE)
    assert params == {
        "arch": "yolo", "network_input": 640, "num_classes": 4, "class_names": PPE_NAMES,
        "score_threshold": 0.25, "iou_threshold": 0.45, "top_k": None, "preserve_aspect": True,
        "onnx_opset": 17, "rfdetr_size": None,
    }


def test_rfdetr_defaults_are_native_resolution_top_k_and_squash():
    params = dc.validate_conversion_request({}, RF_OWN)
    assert params["network_input"] == 512
    assert params["preserve_aspect"] is False
    assert params["iou_threshold"] is None and params["top_k"] == 300
    assert params["score_threshold"] == 0.5
    assert params["rfdetr_size"] == "small"
    assert params["class_names"] == ["blue_plate"]


@pytest.mark.parametrize("size", [320, 352, 640, 1280, 2048])
def test_yolo_input_bounds_accept(size):
    assert dc.validate_conversion_request(
        {"image_width": size, "image_height": size}, PPE)["network_input"] == size


@pytest.mark.parametrize("width,height,fragment", [
    (288, 288, "between 320 and 2048"), (2080, 2080, "between 320 and 2048"),
    (650, 650, "multiple of 32"), (640, 480, "must be square"), ("abc", "abc", "integers"),
    (None, 640, "are required"),
])
def test_yolo_input_bounds_reject(width, height, fragment):
    assert fragment in bad({"image_width": width, "image_height": height})


def test_rfdetr_accepts_only_the_native_resolution():
    assert "native resolution (small: 512)" in bad({"image_width": 640, "image_height": 640}, RF_OWN)
    assert "native resolution" in bad({"image_width": 224, "image_height": 224}, RF_OWN)
    unknown = dict(RF_OWN, rfdetr_size=None, train_input_size=None)
    for native in (384, 512, 576, 704):
        assert dc.validate_conversion_request(
            {"image_width": native, "image_height": native}, unknown)["network_input"] == native
    assert "384, 512, 576, 704" in bad({"image_width": 640, "image_height": 640}, unknown)


@pytest.mark.parametrize("value", [0, 1, 1.5, -0.1, "abc", True])
def test_score_threshold_open_interval(value):
    assert "score_threshold" in bad({"score_threshold": value})


@pytest.mark.parametrize("value", [0, 1, 2, "x", False])
def test_yolo_iou_threshold_open_interval(value):
    assert "iou_threshold" in bad({"iou_threshold": value})


def test_rfdetr_rejects_any_iou_threshold():
    assert "does not apply to RF-DETR" in bad({"iou_threshold": 0.45}, RF_OWN)


def test_class_names_may_be_renamed_but_not_recounted():
    renamed = ["hard-hat", "person", "bare-head", "hi-vis"]
    assert dc.validate_conversion_request({"class_names": renamed}, PPE)["class_names"] == renamed
    assert dc.validate_conversion_request(
        {"class_names": [" hard-hat ", "person", "bare-head", "hi-vis"]}, PPE)["class_names"][0] == "hard-hat"
    assert "exactly 4 classes" in bad({"class_names": renamed[:3]})
    assert "exactly 4 classes" in bad({"class_names": renamed + ["extra"]})
    assert "non-empty strings" in bad({"class_names": ["a", "", "c", "d"]})
    assert "non-empty strings" in bad({"class_names": ["a", 2, "c", "d"]})
    assert "non-empty strings" in bad({"class_names": "helmet,human"})


def test_class_names_default_and_are_required_without_checkpoint_names():
    assert dc.validate_conversion_request({"class_names": []}, PPE)["class_names"] == PPE_NAMES
    assert "'class_names' is required" in bad({}, RF_PUBLISHED)
    names = [f"coco-{i}" for i in range(90)]
    assert dc.validate_conversion_request({"class_names": names}, RF_PUBLISHED)["class_names"] == names


def test_num_classes_and_arch_contradictions():
    assert "contradicts the checkpoint's 4" in bad({"num_classes": 5})
    assert dc.validate_conversion_request({"num_classes": 4}, PPE)["num_classes"] == 4
    assert "contradicts the checkpoint" in bad({"detection_arch": "rf_detr"})
    assert dc.validate_conversion_request({"detection_arch": "YOLO"}, PPE)["arch"] == "yolo"
    assert "model_type" in bad({"model_type": "classification"})


def test_preserve_aspect_is_derived_and_a_contradiction_is_400():
    assert "must be true for YOLO" in bad({"preserve_aspect": False})
    assert "must be false for RF-DETR" in bad({"preserve_aspect": True}, RF_OWN)
    assert dc.validate_conversion_request({"preserve_aspect": True}, PPE)["preserve_aspect"] is True
    assert dc.validate_conversion_request({"preserve_aspect": False}, RF_OWN)["preserve_aspect"] is False


def test_rfdetr_size_must_agree():
    assert "contradicts the checkpoint (small)" in bad({"rfdetr_size": "nano"}, RF_OWN)
    assert "must be one of" in bad({"rfdetr_size": "xlarge"}, RF_OWN)
    unknown = dict(RF_OWN, rfdetr_size=None)
    assert dc.validate_conversion_request({"rfdetr_size": "Small"}, unknown)["rfdetr_size"] == "small"


def test_non_convertible_checkpoint_is_rejected_with_its_reasons():
    message = bad({}, dc.assess_checkpoint(YOLO11N_SEG_PROBE))
    assert message.startswith("Checkpoint cannot be converted to ONNX: ")
    assert "segmentation" in message


# ---------------------------------------------------------------------------
# RF-DETR-Seg requests (rfdetr-seg-support task 7.6; Req 7.3, 7.9)
# ---------------------------------------------------------------------------

def _seg_assessment(size=None, class_names=("disc", "block")):
    """The Seg-Nano assessment re-sized: `size` None = the stripped published
    weights (no size evidence at all), else that Seg size with its native
    pre-fill."""
    out = dict(RF_SEG_OWN, class_names=list(class_names) if class_names else None)
    out["rfdetr_seg_size"] = size
    out["num_queries"] = dc.RFDETR_SEG_QUERIES_FOR_SIZE[size] if size else None
    out["train_input_size"] = dc.RFDETR_SEG_SIZES[size] if size else None
    out["model_class"] = dc.RFDETR_SEG_CLASS_FOR_SIZE[size] if size else None
    return out


def test_seg_defaults_are_native_resolution_top_k_mask_threshold_and_instance_mode():
    params = dc.validate_conversion_request({}, RF_SEG_OWN)
    assert params == {
        "arch": "rf_detr_seg", "network_input": 312, "num_classes": 2, "class_names": ["disc", "block"],
        "score_threshold": 0.5, "iou_threshold": None, "top_k": 100, "preserve_aspect": False,
        "onnx_opset": 17, "rfdetr_size": None,
        # the Seg-only keys follow the ten every arch shares (C28)
        "rfdetr_seg_size": "nano", "mask_threshold": 0.5, "output_mode": "instance",
    }
    assert list(params)[:10] == list(dc.validate_conversion_request({}, PPE))


@pytest.mark.parametrize("size,native,queries", [
    ("nano", 312, 100), ("small", 384, 100), ("medium", 432, 200),
    ("large", 504, 200), ("xlarge", 624, 300), ("2xlarge", 768, 300),
])
def test_seg_accepts_exactly_its_sizes_native_resolution(size, native, queries):
    assessment = _seg_assessment(size)
    params = dc.validate_conversion_request({}, assessment)
    assert (params["network_input"], params["top_k"], params["rfdetr_seg_size"]) == (native, queries, size)
    explicit = dc.validate_conversion_request({"image_width": native, "image_height": native}, assessment)
    assert explicit == params
    for other in dc.RFDETR_SEG_NATIVE_INPUTS:
        if other != native:
            assert f"native resolution ({size}: {native}); got {other}" in bad(
                {"image_width": other, "image_height": other}, assessment)
    # a multiple of 32 inside the detection bounds is no help: native only
    assert f"native resolution ({size}: {native}); got 320" in bad(
        {"image_width": 320, "image_height": 320}, assessment)
    assert "must be square" in bad({"image_width": native, "image_height": 320}, assessment)


def test_seg_unknown_size_accepts_any_native_and_names_top_k_from_the_input():
    # tasks review pass 10, finding 4: the stripped published weights carry
    # neither model_name nor a usable args_picks.resolution, so the request
    # must name one of the six natives and that width names top_k; the
    # record's rfdetr_seg_size stays None for the job's own native check.
    unknown = _seg_assessment(None)
    assert "'image_width' and 'image_height' (the network input) are required" in bad({}, unknown)
    for native, size in dc.RFDETR_SEG_SIZE_FOR_NATIVE.items():
        params = dc.validate_conversion_request({"image_width": native, "image_height": native}, unknown)
        assert params["top_k"] == dc.RFDETR_SEG_QUERIES_FOR_SIZE[size]
        assert params["rfdetr_seg_size"] is None and params["network_input"] == native
    for off in (320, 640, 224):
        assert f"native resolution (312, 384, 432, 504, 624, 768); got {off}" in bad(
            {"image_width": off, "image_height": off}, unknown)
    params = dc.validate_conversion_request({"image_width": 432, "image_height": 432}, unknown)
    assert params["top_k"] == 200 and params["rfdetr_seg_size"] is None
    rec = _record(unknown, body={"image_width": 432, "image_height": 432}, fine_tunable=None)
    assert "rfdetr_seg_size" in rec["detection"] and rec["detection"]["rfdetr_seg_size"] is None
    assert rec["detection"]["top_k"] == 200 and rec["detection"]["resolution"] == 432
    env = _job(params=params)["Environment"]
    assert "RFDETR_SIZE" not in env
    assert (env["DETECTION_ARCH"], env["NETWORK_INPUT"], env["TASK"], env["MASK_THRESHOLD"]) == (
        "rf_detr_seg", "432", "segmentation", "0.5")


@pytest.mark.parametrize("body,fragment", [
    ({"iou_threshold": 0.45}, "'iou_threshold' does not apply to RF-DETR-Seg"),
    ({"rfdetr_size": "nano"}, "'rfdetr_size' does not apply to RF-DETR-Seg"),
    ({"rfdetr_size": "small"}, "'rfdetr_size' does not apply to RF-DETR-Seg"),
    ({"model_type": "object_detection"},
     "'model_type' must be 'instance_segmentation' for an RF-DETR-Seg checkpoint conversion; "
     "got 'object_detection'"),
    ({"detection_arch": "rf_detr"}, "contradicts the checkpoint, which is an RF-DETR-Seg checkpoint"),
    ({"preserve_aspect": True}, "must be false for RF-DETR"),
    ({"output_mode": "panoptic"}, "'output_mode' must be one of instance, semantic; got 'panoptic'"),
    ({"mask_threshold": 0}, "'mask_threshold' must be strictly between 0 and 1"),
    ({"mask_threshold": 1}, "'mask_threshold' must be strictly between 0 and 1"),
    ({"mask_threshold": "x"}, "'mask_threshold' must be a number"),
    ({"mask_threshold": True}, "'mask_threshold' must be a number"),
    ({"score_threshold": 1.5}, "'score_threshold' must be strictly between 0 and 1"),
    ({"class_names": ["disc"]}, "exactly 2 classes"),
    ({"class_names": ["a", "b", "c"]}, "exactly 2 classes"),
    ({"num_classes": 3}, "contradicts the checkpoint's 2"),
])
def test_seg_request_rejections(body, fragment):
    assert fragment in bad(body, RF_SEG_OWN)


def test_seg_output_mode_and_mask_threshold_are_validated_and_normalised():
    params = dc.validate_conversion_request({"output_mode": "Semantic", "mask_threshold": 0.3}, RF_SEG_OWN)
    assert params["output_mode"] == "semantic" and params["mask_threshold"] == 0.3
    assert dc.validate_conversion_request({"output_mode": None}, RF_SEG_OWN)["output_mode"] == "instance"
    assert dc.validate_conversion_request({"mask_threshold": None}, RF_SEG_OWN)["mask_threshold"] == 0.5
    agreed = {"model_type": "instance_segmentation", "detection_arch": "RF_DETR_SEG", "preserve_aspect": False}
    assert dc.validate_conversion_request(agreed, RF_SEG_OWN)["arch"] == "rf_detr_seg"
    # The Seg-only keys never reach a yolo / rf_detr params dict (C28).
    for assessment in (PPE, RF_OWN):
        assert set(dc.validate_conversion_request({}, assessment)) == set(
            dc.validate_conversion_request({}, PPE))


def test_seg_class_names_are_required_without_checkpoint_names():
    anonymous = _seg_assessment("nano", class_names=None)
    assert "'class_names' is required: the checkpoint stores no class names; supply exactly 2" in bad(
        {}, anonymous)
    assert dc.validate_conversion_request({"class_names": ["a", "b"]}, anonymous)["class_names"] == ["a", "b"]
    assert "exactly 2 classes" in bad({"class_names": ["a"]}, anonymous)


# ---------------------------------------------------------------------------
# Job request (Req 5.1-5.4)
# ---------------------------------------------------------------------------

def _job(params=None, **overrides):
    kwargs = dict(job_name="ppe_detection-cnv-20261001120000", image_uri=IMAGE, role_arn=ROLE,
                  input_prefix_s3=PREFIX, output_s3="s3://uc-bucket/models/conversion/",
                  params=params or dc.validate_conversion_request({}, PPE),
                  source_sha256=PPE_SHA256,
                  tags=[{"Key": "UseCase", "Value": "uc-1"}, {"Key": "Purpose", "Value": "conversion"}])
    kwargs.update(overrides)
    return dc.build_conversion_job_request(**kwargs)


def test_job_request_is_isolated_bounded_and_code_free():
    req = _job()
    assert req["EnableNetworkIsolation"] is True
    assert "HyperParameters" not in req  # no sagemaker_program / sagemaker_submit_directory
    blob = json.dumps(req)
    assert "sagemaker_program" not in blob and "sagemaker_submit_directory" not in blob
    assert "requirements.txt" not in blob
    assert req["AlgorithmSpecification"] == {"TrainingImage": IMAGE, "TrainingInputMode": "File"}
    assert req["RoleArn"] == ROLE
    assert req["ResourceConfig"] == {"InstanceType": "ml.m5.xlarge", "InstanceCount": 1,
                                     "VolumeSizeInGB": 30}
    assert req["StoppingCondition"] == {"MaxRuntimeInSeconds": 1800}
    assert req["OutputDataConfig"] == {"S3OutputPath": "s3://uc-bucket/models/conversion/"}
    assert req["Tags"] == [{"Key": "UseCase", "Value": "uc-1"}, {"Key": "Purpose", "Value": "conversion"}]


def test_exactly_one_channel_on_the_sidecar_prefix_with_trailing_slash():
    channels = _job()["InputDataConfig"]
    assert len(channels) == 1
    channel = channels[0]
    assert channel["ChannelName"] == "checkpoint"
    source = channel["DataSource"]["S3DataSource"]
    assert source == {"S3DataType": "S3Prefix", "S3Uri": PREFIX,
                      "S3DataDistributionType": "FullyReplicated"}
    assert source["S3Uri"].endswith("/")
    with pytest.raises(ValueError, match="ending in /"):
        _job(input_prefix_s3=PREFIX.rstrip("/"))
    with pytest.raises(ValueError, match="ending in /"):
        _job(input_prefix_s3="https://uc-bucket/converted-models/x/")


def test_environment_contract_yolo():
    env = _job()["Environment"]
    assert env == {"DETECTION_ARCH": "yolo", "NETWORK_INPUT": "640", "EXPECTED_NUM_CLASSES": "4",
                   "EXPECTED_SHA256": PPE_SHA256, "ONNX_OPSET": "17"}
    assert all(isinstance(v, str) for v in env.values())


def test_environment_contract_rfdetr_passes_the_known_size():
    env = _job(params=dc.validate_conversion_request({}, RF_OWN))["Environment"]
    assert env["DETECTION_ARCH"] == "rf_detr" and env["NETWORK_INPUT"] == "512"
    assert env["EXPECTED_NUM_CLASSES"] == "1" and env["RFDETR_SIZE"] == "small"
    unknown = dict(RF_OWN, rfdetr_size=None)
    env = _job(params=dc.validate_conversion_request({}, unknown))["Environment"]
    assert "RFDETR_SIZE" not in env


def test_environment_contract_seg_adds_task_and_mask_threshold_for_the_seg_arch_only():
    # rfdetr-seg-support task 7.6 (Req 7.3, C26): one RFDETR_SIZE key for both
    # DETR archs, TASK + MASK_THRESHOLD for rf_detr_seg only.
    env = _job(params=dc.validate_conversion_request({"mask_threshold": 0.35}, RF_SEG_OWN))["Environment"]
    assert env == {"DETECTION_ARCH": "rf_detr_seg", "NETWORK_INPUT": "312", "EXPECTED_NUM_CLASSES": "2",
                   "EXPECTED_SHA256": PPE_SHA256, "ONNX_OPSET": "17", "RFDETR_SIZE": "nano",
                   "TASK": "segmentation", "MASK_THRESHOLD": "0.35"}
    assert all(isinstance(v, str) for v in env.values())
    # An rf_detr record's environment is byte-identical to today's: six keys.
    env = _job(params=dc.validate_conversion_request({}, RF_OWN))["Environment"]
    assert env == {"DETECTION_ARCH": "rf_detr", "NETWORK_INPUT": "512", "EXPECTED_NUM_CLASSES": "1",
                   "EXPECTED_SHA256": PPE_SHA256, "ONNX_OPSET": "17", "RFDETR_SIZE": "small"}


@pytest.mark.parametrize("assessment,body", [
    (PPE, {}), (PPE, {"image_width": 1280, "image_height": 1280}), (RF_OWN, {}),
    (RF_PUBLISHED, {"class_names": [f"c{i}" for i in range(90)]}),
    (RF_SEG_OWN, {}),                                                      # rfdetr-seg-support 7.6
    (RF_SEG_OWN, {"output_mode": "semantic", "mask_threshold": 0.3}),
    (_seg_assessment(None), {"image_width": 624, "image_height": 624}),    # unknown size: no RFDETR_SIZE
])
def test_the_export_entry_point_accepts_every_environment_the_portal_builds(assessment, body):
    export = _load_export_module()
    env = _job(params=dc.validate_conversion_request(body, assessment))["Environment"]
    cfg = export.read_config(env)
    assert cfg.arch == env["DETECTION_ARCH"]
    assert cfg.network_input == int(env["NETWORK_INPUT"])
    assert cfg.num_classes == int(env["EXPECTED_NUM_CLASSES"])
    assert cfg.expected_sha256 == PPE_SHA256
    assert cfg.opset == 17
    assert cfg.rfdetr_size == env.get("RFDETR_SIZE")
    assert cfg.task == ("segmentation" if assessment["arch"] == "rf_detr_seg" else "detection")
    assert cfg.mask_threshold == float(env.get("MASK_THRESHOLD", "0.5"))


def test_job_request_rejects_a_bad_digest():
    with pytest.raises(ValueError, match="64-character"):
        _job(source_sha256="abc")
    with pytest.raises(ValueError, match="64-character"):
        _job(source_sha256=PPE_SHA256.upper())


def test_job_name_is_sagemaker_legal():
    name = dc.conversion_job_name("ppe_detection", "2026-10-01T12:00:00")
    assert name == "ppe-detection-cnv-20261001120000"
    long_name = dc.conversion_job_name("x" * 200 + "!!", "20261001120000")
    assert len(long_name) <= 63 and long_name.endswith("-cnv-20261001120000")
    assert dc.conversion_job_name("___", "1") == "model-cnv-1"
    for n in (name, long_name):
        assert all(c.isalnum() or c == "-" for c in n) and not n.startswith("-")


def test_image_region_and_availability():
    assert dc.image_region(IMAGE) == "us-east-1"
    assert dc.image_region("public.ecr.aws/x/y:1") is None
    assert dc.conversion_unavailable_reason(IMAGE, "us-east-1") is None
    assert dc.conversion_unavailable_reason(IMAGE, None) is None
    assert dc.conversion_unavailable_reason("", "us-east-1") == (
        "Checkpoint conversion is not configured on this portal (no detector export image)")
    assert dc.conversion_unavailable_reason(None, "us-east-1") == (
        "Checkpoint conversion is not configured on this portal (no detector export image)")
    assert "region eu-west-1" in dc.conversion_unavailable_reason(IMAGE, "eu-west-1")
    assert "not an ECR image URI" in dc.conversion_unavailable_reason("docker.io/x:1", "us-east-1")


# ---------------------------------------------------------------------------
# Conversion_Record + predicate (Req 4.6, 9.1)
# ---------------------------------------------------------------------------

FINE_TUNABLE = {
    "arch": "yolo", "kind": "ultralytics_checkpoint",
    "checkpoint_s3": "s3://uc-bucket/converted-models/ppe_detection-1a2b3c4d/checkpoint.pt",
    "class_names": PPE_NAMES, "num_classes": 4,
}


def _record(assessment=PPE, body=None, fine_tunable=FINE_TUNABLE):
    params = dc.validate_conversion_request(body or {}, assessment)
    return dc.build_conversion_record(
        training_id="t-1", usecase_id="uc-1", model_name="ppe-detection", model_version="1.0.0",
        created_by="ds@example.com", params=params, assessment=assessment,
        fine_tunable=fine_tunable, job_name="ppe-detection-cnv-20261001120000",
        job_arn="arn:aws:sagemaker:us-east-1:123456789012:training-job/ppe-detection-cnv-20261001120000",
        image_uri=IMAGE, source_s3="s3://uc-bucket/model-uploads/0000/best.pt",
        source_sha256=PPE_SHA256, source_bytes=5475290, model_file="best.pt", now_ms=1790000000000)


def test_yolo_record_has_the_requirement_4_6_shape():
    rec = _record()
    assert {k: rec[k] for k in ("source", "model_type", "runtime", "status", "progress")} == {
        "source": "imported", "model_type": "object_detection", "runtime": "onnx",
        "status": "InProgress", "progress": 10}
    assert rec["training_job_name"] == "ppe-detection-cnv-20261001120000"
    assert rec["training_job_arn"].endswith("training-job/ppe-detection-cnv-20261001120000")
    assert rec["detection"] == {
        "detection_arch": "yolo", "network_input_width": 640, "network_input_height": 640,
        "class_names": PPE_NAMES, "num_classes": 4, "score_threshold": Decimal("0.25"),
        "iou_threshold": Decimal("0.45"), "preserve_aspect": True, "onnx_opset": 17,
        "imgsz": 640,
    }
    meta = rec["metadata"]
    assert meta["framework"] == "PYTORCH"
    assert meta["framework_version"] == "ultralytics 8.4.2"
    assert meta["model_file"] == "best.pt"
    assert meta["fine_tunable"] == FINE_TUNABLE
    assert rec["conversion"] == {
        "status": "InProgress", "job_name": "ppe-detection-cnv-20261001120000",
        "export_image": IMAGE, "source_s3": "s3://uc-bucket/model-uploads/0000/best.pt",
        "source_sha256": PPE_SHA256, "source_bytes": 5475290, "source_framework": "ultralytics",
        "source_framework_version": "8.4.2", "started_at": 1790000000000,
    }
    assert rec["algorithm_uri"] == IMAGE  # the image digest is on every record (Req 5.5)
    assert rec["instance_type"] == "ml.m5.xlarge"
    assert rec["auto_compile"] is False


def test_rfdetr_record_carries_size_resolution_and_top_k():
    rec = _record(RF_OWN, fine_tunable=None)
    det = rec["detection"]
    assert det["detection_arch"] == "rf_detr"
    assert det["top_k"] == 300 and "iou_threshold" not in det
    assert det["rfdetr_size"] == "small" and det["resolution"] == 512
    assert det["preserve_aspect"] is False
    assert det["score_threshold"] == Decimal("0.5")
    assert rec["metadata"]["framework_version"] == "rfdetr"
    assert rec["metadata"]["fine_tunable"] is None


SEG_FINE_TUNABLE = {
    "arch": "rf_detr_seg", "kind": "rfdetr_checkpoint",
    "checkpoint_s3": "s3://uc-bucket/converted-models/disc_seg-1a2b3c4d/checkpoint.pth",
    "class_names": ["disc", "block"], "num_classes": 2,
}


def test_seg_record_writes_the_sidecar_descriptor_exactly_as_for_detection():
    # rfdetr-seg-support task 7.5 (Req 7.7): metadata.fine_tunable is the
    # descriptor verbatim -- the same key scheme as a detection record, no
    # validation step -- with `arch` the probe's rf_detr_seg.
    rec = _record(RF_SEG_OWN, fine_tunable=SEG_FINE_TUNABLE)
    assert rec["metadata"]["fine_tunable"] == SEG_FINE_TUNABLE
    assert rec["metadata"]["fine_tunable"]["arch"] == rec["detection"]["detection_arch"] == "rf_detr_seg"
    assert rec["model_type"] == rec["metadata"]["model_type"] == "instance_segmentation"
    assert rec["metadata"]["checkpoint_kind"] == "rfdetr_checkpoint"
    det = rec["detection"]
    assert det["rfdetr_seg_size"] == "nano" and det["resolution"] == 312 and det["top_k"] == 100
    assert det["mask_threshold"] == Decimal("0.5") and det["output_mode"] == "instance"
    assert det["preserve_aspect"] is False and det["score_threshold"] == Decimal("0.5")
    assert "iou_threshold" not in det and "rfdetr_size" not in det and "imgsz" not in det
    assert dc.is_detector_conversion_record(rec)
    assert detection_training.is_trained_detection_record(rec) is False


def test_seg_record_is_dynamodb_ready_and_keeps_the_conversion_identifiers():
    # rfdetr-seg-support task 7.6: the record carries the request's mode and
    # threshold as Decimals, and every identifier a Conversion_Record has today.
    rec = _record(RF_SEG_OWN, body={"output_mode": "semantic", "mask_threshold": 0.3}, fine_tunable=None)

    def walk(v):
        assert not isinstance(v, float), v
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(rec)
    json.dumps(rec, default=str)
    det = rec["detection"]
    assert det["output_mode"] == "semantic" and det["mask_threshold"] == Decimal("0.3")
    assert {k: rec[k] for k in ("source", "model_type", "runtime", "status", "progress")} == {
        "source": "imported", "model_type": "instance_segmentation", "runtime": "onnx",
        "status": "InProgress", "progress": 10}
    assert rec["metadata"]["input_shape"] == [1, 3, 312, 312] and rec["metadata"]["fine_tunable"] is None
    assert rec["conversion"]["source_framework"] == "rfdetr" and rec["algorithm_uri"] == IMAGE
    assert set(rec) == set(_record(RF_OWN, fine_tunable=None))  # no new top-level key for Seg


def test_record_is_dynamodb_ready():
    rec = _record()

    def walk(v):
        assert not isinstance(v, float), v
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(rec)
    json.dumps(rec, default=str)


def test_predicate_matches_only_conversion_records():
    rec = _record()
    assert dc.is_detector_conversion_record(rec)
    assert dc.is_detector_conversion_record(json.loads(json.dumps(rec, default=float)))
    for key, value in (("source", "trained"), ("model_type", "classification"),
                       ("runtime", "tensorrt"), ("conversion", None), ("conversion", "yes")):
        mutated = copy.deepcopy(rec)
        mutated[key] = value
        assert not dc.is_detector_conversion_record(mutated), key
    trained = {"training_id": "t", "model_type": "object_detection", "runtime": "onnx",
               "detection": {"detection_arch": "yolo"}}
    assert not dc.is_detector_conversion_record(trained)
    onnx_import = {"source": "imported", "model_type": "object_detection",
                   "metadata": {"framework": "ONNX", "model_file": "model.onnx"}}
    assert not dc.is_detector_conversion_record(onnx_import)
    assert not dc.is_detector_conversion_record(None)
    assert not dc.is_detector_conversion_record([rec])


def test_trained_detection_predicate_never_matches_a_conversion_record():
    assert detection_training.is_trained_detection_record(_record()) is False
    assert detection_training.is_trained_detection_record(_record(RF_OWN)) is False


def _load_packaging_module():
    path = os.path.join(_HERE, "..", "functions", "packaging.py")
    spec = importlib.util.spec_from_file_location("portal_packaging_dci_request", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["portal_packaging_dci_request"] = module
    spec.loader.exec_module(module)
    return module


def test_onnx_import_predicate_never_matches_a_conversion_record(aws_stack):
    # The routing in packaging.package_components (Req 9.1) depends on this:
    # a Conversion_Record must reach the conversion block, never the BYO-ONNX one.
    packaging = _load_packaging_module()
    for rec in (_record(), _record(RF_OWN, fine_tunable=None)):
        assert packaging.is_onnx_import(rec) is False
