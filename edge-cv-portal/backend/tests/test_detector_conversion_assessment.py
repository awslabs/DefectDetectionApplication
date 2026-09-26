"""
``detector_conversion.assess_checkpoint`` / ``assessment_prefill`` -- the
Smart Import pre-flight for detector checkpoints
(detector-checkpoint-import task 2.1).

The probe dicts (tests/fixtures/detector_probes.py) are
``checkpoint_probe.classify_checkpoint`` output: the ultralytics / RF-DETR
ones are the REAL evidence the probe returned for the spike fixtures (PPE
``best.pt``, the published yolo26n / yolov10n / yolo11n-seg checkpoints, the
portal's own RF-DETR small ``checkpoint_best_total.pth`` (R2') and the
published RF-DETR nano ``.pth`` (R2)). The other ultralytics tasks are
synthetic GLOBAL sets.
# Validates: Requirements 2.3, 2.4, 2.5, 2.6
"""
import copy

import pytest
from hypothesis import given, settings, strategies as st

import detector_conversion as dc

from fixtures.detector_probes import (  # noqa: F401 - real probe evidence (spike fixtures)
    PPE_NAMES,
    PPE_PROBE,
    RFDETR_OWN_PROBE,
    RFDETR_PUBLISHED_PROBE,
    YOLO11N_SEG_PROBE,
    YOLO26N_PROBE,
    YOLOV10N_PROBE,
    other_kind,
    ultralytics_probe,
)


FIELDS = ("kind", "arch", "task", "model_class", "head_classes", "num_classes", "class_names",
          "train_input_size", "framework", "framework_version", "convertible", "reasons")


# ---------------------------------------------------------------------------
# Convertible checkpoints
# ---------------------------------------------------------------------------

def test_ppe_checkpoint_is_convertible_with_every_field():
    out = dc.assess_checkpoint(PPE_PROBE)
    assert set(FIELDS) <= set(out)
    assert out["convertible"] is True and out["reasons"] == []
    assert out["kind"] == "ultralytics_checkpoint"
    assert out["arch"] == "yolo"
    assert out["task"] == "detect"
    assert out["model_class"] == "ultralytics.nn.tasks.DetectionModel"
    assert out["head_classes"] == ["ultralytics.nn.modules.head.Detect"]
    assert out["num_classes"] == 4
    assert out["class_names"] == PPE_NAMES  # index order, as the probe sorted names keys
    assert out["train_input_size"] == 640
    assert out["framework"] == "ultralytics"
    assert out["framework_version"] == "8.4.2"


def test_ppe_prefill_matches_requirement_2_5():
    prefill = dc.assessment_prefill(dc.assess_checkpoint(PPE_PROBE))
    assert prefill["suggested_type"] == "object_detection"
    assert prefill["detection_arch"] == "yolo"
    assert prefill["num_classes"] == 4
    assert prefill["class_names"] == ["helmet", "human", "no-helmet", "vest"]
    assert prefill["input_width"] == prefill["input_height"] == 640


def test_yolo26_detect_head_is_convertible():
    out = dc.assess_checkpoint(YOLO26N_PROBE)
    assert out["convertible"] is True, out["reasons"]
    assert out["framework_version"] == "8.3.222"


def test_task_absent_is_accepted_for_a_detection_model():
    probe = ultralytics_probe("DetectionModel", ["Detect"], task=None)
    out = dc.assess_checkpoint(probe)
    assert out["convertible"] is True
    assert out["task"] == "detect"


def test_class_names_keep_the_probe_index_order():
    names = ["zebra", "apple", "mango"]  # index order, deliberately not alphabetical
    out = dc.assess_checkpoint(ultralytics_probe("DetectionModel", ["Detect"], names=names))
    assert out["class_names"] == names
    assert dc.assessment_prefill(out)["class_names"] == names


def test_rfdetr_own_checkpoint_r2_prime():
    out = dc.assess_checkpoint(RFDETR_OWN_PROBE)
    assert out["convertible"] is True, out["reasons"]
    assert (out["arch"], out["framework"], out["task"]) == ("rf_detr", "rfdetr", "detect")
    assert out["rfdetr_size"] == "small"
    assert out["model_class"] == "RFDETRSmall"
    assert out["train_input_size"] == 512
    assert out["class_names"] == ["blue_plate"] and out["num_classes"] == 1
    prefill = dc.assessment_prefill(out)
    assert prefill["detection_arch"] == "rf_detr"
    assert prefill["input_width"] == prefill["input_height"] == 512


def test_rfdetr_published_r2_size_from_args_and_no_names():
    out = dc.assess_checkpoint(RFDETR_PUBLISHED_PROBE)
    assert out["convertible"] is True, out["reasons"]
    assert out["rfdetr_size"] == "nano"
    assert out["train_input_size"] == 384
    assert out["num_classes"] == 90  # the head, never the stale args.num_classes (2)
    assert out["class_names"] is None


# ---------------------------------------------------------------------------
# Non-convertible: every failed condition is named (Req 2.4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model_class,head,task,word", [
    ("SegmentationModel", "Segment", "segment", "segmentation"),
    ("PoseModel", "Pose", "pose", "pose"),
    ("OBBModel", "OBB", "obb", "oriented"),
    ("ClassificationModel", "Classify", "classify", "classification"),
    ("WorldModel", "WorldDetect", "detect", "YOLO-World"),
    ("YOLOEModel", "YOLOEDetect", "detect", "YOLOE"),
    ("RTDETRDetectionModel", "RTDETRDecoder", "detect", "RT-DETR"),
])
def test_ultralytics_non_detect_models_are_named_by_task(model_class, head, task, word):
    out = dc.assess_checkpoint(ultralytics_probe(model_class, [head], task=task))
    assert out["convertible"] is False
    joined = " | ".join(out["reasons"])
    assert word in joined
    assert model_class in joined
    if task != "detect":
        assert repr(task) in joined


def test_real_segmentation_checkpoint_names_model_task_and_head():
    out = dc.assess_checkpoint(YOLO11N_SEG_PROBE)
    assert out["convertible"] is False
    assert len(out["reasons"]) == 3
    assert any("segmentation model (SegmentationModel)" in r for r in out["reasons"])
    assert any("'segment' task" in r for r in out["reasons"])
    assert any("segmentation head" in r for r in out["reasons"])
    assert "ultralytics.nn.modules.head.Segment" in out["head_classes"]


def test_yolov10_head_converts_through_its_one_to_many_branch():
    # Spike 1.3(b): ultralytics 8.4.162 exports v10Detect's trained one-to-many
    # branch as [1, 84, 8400] with nms=None (same detections as its one-to-one
    # head on the bundled images), so v10 is accepted like YOLO26.
    out = dc.assess_checkpoint(YOLOV10N_PROBE)
    assert out["convertible"] is True, out["reasons"]
    assert out["head_classes"] == ["ultralytics.nn.modules.head.v10Detect"]


def test_unknown_head_and_missing_head():
    unknown = dc.assess_checkpoint(ultralytics_probe("DetectionModel", ["FancyDetect"]))
    assert unknown["reasons"] == ["detection head FancyDetect is not supported"]
    none = dc.assess_checkpoint(ultralytics_probe("DetectionModel", []))
    assert none["reasons"] == ["no ultralytics detection head found in the checkpoint"]


def test_unrecovered_classes_block_conversion():
    no_nc = ultralytics_probe("DetectionModel", ["Detect"], names=None)
    no_nc["num_classes"] = None
    assert "class count could not be read" in dc.assess_checkpoint(no_nc)["reasons"][0]
    mismatch = ultralytics_probe("DetectionModel", ["Detect"], names=["a", "b"], nc=3)
    assert dc.assess_checkpoint(mismatch)["reasons"] == [
        "the class names could not be read from the checkpoint"]
    no_names = ultralytics_probe("DetectionModel", ["Detect"])
    no_names["class_names"] = None
    assert dc.assess_checkpoint(no_names)["convertible"] is False


@pytest.mark.parametrize("model_name,word", [
    ("RFDETRXLarge", "PML-licensed"),
    ("RFDETR2XLarge", "PML-licensed"),
    ("RFDETRSegSmall", "not an RF-DETR detection model"),
    ("RFDETRKeypointPreview", "not an RF-DETR detection model"),
    ("RFDETRBase", "legacy"),
])
def test_rfdetr_rejected_variants(model_name, word):
    probe = copy.deepcopy(RFDETR_OWN_PROBE)
    probe["evidence"]["model_name"] = model_name
    out = dc.assess_checkpoint(probe)
    assert out["convertible"] is False
    assert word in out["reasons"][0] and model_name in out["reasons"][0]


def test_rfdetr_non_native_resolution_is_rejected():
    probe = copy.deepcopy(RFDETR_OWN_PROBE)
    probe["evidence"]["args_picks"] = {"resolution": 640}
    out = dc.assess_checkpoint(probe)
    assert out["convertible"] is False
    assert out["reasons"] == ["the checkpoint was trained at 640px; RF-DETR small converts only "
                              "at its native 512px"]
    anonymous = copy.deepcopy(RFDETR_PUBLISHED_PROBE)
    anonymous["evidence"]["args_picks"]["resolution"] = 640
    reasons = dc.assess_checkpoint(anonymous)["reasons"]
    assert len(reasons) == 1 and "not a native RF-DETR resolution" in reasons[0]


def test_rfdetr_legacy_encoder_and_missing_head_classes():
    legacy = copy.deepcopy(RFDETR_PUBLISHED_PROBE)
    legacy["evidence"]["args_picks"]["encoder"] = "dinov2_windowed_base"
    assert "legacy/unsupported" in dc.assess_checkpoint(legacy)["reasons"][0]
    headless = copy.deepcopy(RFDETR_OWN_PROBE)
    headless["num_classes"] = None
    assert dc.assess_checkpoint(headless)["reasons"] == [
        "the class count could not be read from the RF-DETR head"]


def test_rfdetr_without_size_evidence_is_left_to_the_job():
    probe = copy.deepcopy(RFDETR_OWN_PROBE)
    probe["evidence"].pop("model_name")
    out = dc.assess_checkpoint(probe)
    assert out["convertible"] is True
    assert out["rfdetr_size"] is None and out["train_input_size"] is None
    assert dc.assessment_prefill(out)["input_width"] is None


@pytest.mark.parametrize("kind,fragment", [
    ("torchscript", "frozen TorchScript graph"),
    ("state_dict", "weights without a model definition"),
    ("legacy_torch", "without a model definition"),
    ("onnx", "already an ONNX graph"),
    ("unknown", "not a recognised ultralytics YOLO or RF-DETR checkpoint"),
    ("something-new", "not a recognised ultralytics YOLO or RF-DETR checkpoint"),
])
def test_other_kinds_are_not_convertible(kind, fragment):
    out = dc.assess_checkpoint(other_kind(kind))
    assert out["convertible"] is False
    assert len(out["reasons"]) == 1 and fragment in out["reasons"][0]
    prefill = dc.assessment_prefill(out)
    assert "suggested_type" not in prefill and "detection_arch" not in prefill
    assert prefill["input_width"] is None


def test_conversion_unavailable_is_reported_as_the_reason():
    reason = dc.conversion_unavailable_reason(None, "us-east-1")
    out = dc.assess_checkpoint(PPE_PROBE, conversion_available=False, unavailable_reason=reason)
    assert out["convertible"] is False
    assert out["reasons"] == [
        "Checkpoint conversion is not configured on this portal (no detector export image)"]
    # A checkpoint that is not convertible anyway keeps its own reasons only.
    seg = dc.assess_checkpoint(YOLO11N_SEG_PROBE, conversion_available=False,
                               unavailable_reason=reason)
    assert reason not in seg["reasons"]


# ---------------------------------------------------------------------------
# train_input_size (Req 2.5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("imgsz,expected", [
    (640, 640), (320, 320), (2048, 2048), (1280, 1280),
    (641, None), (288, None), (2080, None), ([640, 480], None), ("640", None), (True, None),
    (None, None),
])
def test_yolo_train_input_size_bounds(imgsz, expected):
    out = dc.assess_checkpoint(ultralytics_probe("DetectionModel", ["Detect"], imgsz=imgsz))
    assert out["train_input_size"] == expected
    assert out["convertible"] is True  # an odd imgsz only disables the pre-fill
    assert dc.assessment_prefill(out)["input_width"] == expected


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

_kinds = st.sampled_from(["ultralytics_checkpoint", "rfdetr_checkpoint", "torchscript",
                          "state_dict", "legacy_torch", "onnx", "unknown", "", None])
_scalar = st.one_of(st.none(), st.booleans(), st.integers(-5, 5000), st.text(max_size=8))


@settings(max_examples=300, deadline=None)
@given(kind=_kinds,
       model_class=st.sampled_from([None, dc.YOLO_MODEL_CLASS, "ultralytics.nn.tasks.PoseModel", 7]),
       heads=st.lists(st.sampled_from(["Detect", "v10Detect", "Segment", "Weird", "OBB"]), max_size=3),
       num_classes=_scalar, names=st.one_of(st.none(), st.lists(st.text(max_size=5), max_size=4)),
       imgsz=_scalar, task=_scalar, model_name=_scalar, resolution=_scalar)
def test_assessment_is_total_and_convertible_iff_no_reasons(kind, model_class, heads, num_classes,
                                                            names, imgsz, task, model_name,
                                                            resolution):
    probe = {
        "kind": kind, "num_classes": num_classes, "class_names": names,
        "evidence": {
            "model_class": model_class,
            "pickle_globals": [f"ultralytics.nn.modules.head.{h}" for h in heads],
            "train_args": {"task": task, "imgsz": imgsz},
            "model_name": model_name,
            "args_picks": {"resolution": resolution},
        },
    }
    out = dc.assess_checkpoint(probe)
    assert out["convertible"] == (out["reasons"] == [])
    assert all(isinstance(r, str) and r for r in out["reasons"])
    if out["convertible"]:
        assert out["arch"] in ("yolo", "rf_detr")
        assert isinstance(out["num_classes"], int) and out["num_classes"] >= 1
    prefill = dc.assessment_prefill(out)
    assert ("suggested_type" in prefill) == out["convertible"]


def test_garbage_probe_never_raises():
    for probe in (None, [], "x", {"evidence": "nope"}, {"kind": "ultralytics_checkpoint",
                                                         "evidence": {"pickle_globals": None}}):
        out = dc.assess_checkpoint(probe)
        assert out["convertible"] is False and out["reasons"]
