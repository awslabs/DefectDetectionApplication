"""
Manifest parity: the device manifest packaging writes for a portal-trained
detector equals the one Smart Import writes for the same inputs
(portal-detection-training task 5.3, Requirement 5.7).

`detection_training.build_detection_device_manifest` is a deliberate copy of
`model_converter.generate_dda_package`'s ONNX/yolo detection branch (that
module is a handler with boto3 clients at import time and is
preservation-tracked, so it is not imported from the shared layer). This test
is what keeps the two from drifting.

model_converter's module-level boto3 clients construct fine under conftest's
fake credentials (test_model_converter_preserve_aspect.py relies on the same).
"""
import json
import os
import tarfile
import tempfile

import pytest
from hypothesis import given, settings, strategies as st

import detection_training as dt
import model_converter


def _smart_import_manifest(image_size, class_names, score, iou, preserve_aspect=True,
                           detection_arch="yolo"):
    with tempfile.TemporaryDirectory() as td:
        onnx_path = os.path.join(td, "model.onnx")
        with open(onnx_path, "wb") as fh:
            fh.write(b"\x08\x07")
        out = os.path.join(td, "pkg.tar.gz")
        model_converter.generate_dda_package(
            model_path=onnx_path,
            model_name="parity",
            model_type="object_detection",
            image_width=image_size,
            image_height=image_size,
            num_classes=len(class_names),
            class_names=list(class_names),
            output_path=out,
            export_format="onnx",
            score_threshold=score,
            iou_threshold=iou,
            detection_arch=detection_arch,
            preserve_aspect=preserve_aspect,
        )
        with tarfile.open(out, "r:gz") as tar:
            member = next(m for m in tar.getmembers()
                          if m.name.endswith("export_artifacts/manifest.json"))
            return json.load(tar.extractfile(member))


def _trained_manifest(image_size, class_names, score, iou, preserve_aspect=True,
                      detection_arch="yolo"):
    m = dt.build_detection_device_manifest(
        image_width=image_size, image_height=image_size,
        num_classes=len(class_names), class_names=list(class_names),
        score_threshold=score, iou_threshold=iou, preserve_aspect=preserve_aspect,
        detection_arch=detection_arch)
    # Packaging adds the top-level dataset block that Smart Import keeps in
    # config.yaml (package_onnx_component merges it in later).
    m.pop("dataset")
    return m


@pytest.mark.parametrize("image_size,class_names,score,iou", [
    (1280, ["blue_plate"], 0.25, 0.45),
    (640, ["plate", "luggage"], 0.3, 0.5),
    (1088, ["a", "b", "c"], 0.08, 0.7),
])
def test_trained_manifest_equals_smart_import_manifest(image_size, class_names, score, iou):
    assert _trained_manifest(image_size, class_names, score, iou) == \
        _smart_import_manifest(image_size, class_names, score, iou)


def test_parity_holds_with_preserve_aspect_off():
    assert _trained_manifest(640, ["x"], 0.25, 0.45, preserve_aspect=False) == \
        _smart_import_manifest(640, ["x"], 0.25, 0.45, preserve_aspect=False)


@settings(max_examples=15, deadline=None)
@given(
    k=st.integers(min_value=10, max_value=40),
    n=st.integers(min_value=1, max_value=6),
    score=st.floats(min_value=0.01, max_value=0.99),
    iou=st.floats(min_value=0.01, max_value=0.99),
)
def test_parity_property(k, n, score, iou):
    names = [f"c{i}" for i in range(n)]
    assert _trained_manifest(k * 32, names, score, iou) == \
        _smart_import_manifest(k * 32, names, score, iou)


# ---------------------------------------------------------------------------
# RF-DETR (rfdetr-training-and-transfer-learning task 3.2, Requirement 4.1)
#
# Same mechanics as the yolo cases above: build_detection_device_manifest's
# rf_detr branch minus the `dataset` block must equal what generate_dda_package
# writes for detection_arch='rf_detr'. Byte-parity wins over the spec's
# nominal `[1, 300, C]` output_shape — model_converter.py is
# preservation-tracked, and packaging overwrites output_shape with the real
# shape recorded in training_metadata.json anyway.
# ---------------------------------------------------------------------------

def _rfdetr_pair(image_size, class_names, score, preserve_aspect=False):
    # RF-DETR is NMS-free: build_detection_device_manifest takes no
    # iou_threshold for it, while generate_dda_package accepts (and ignores)
    # one. Passing a value on the Smart Import side proves it never surfaces.
    trained = _trained_manifest(image_size, class_names, score, None,
                                preserve_aspect=preserve_aspect, detection_arch="rf_detr")
    imported = _smart_import_manifest(image_size, class_names, score, 0.45,
                                      preserve_aspect=preserve_aspect, detection_arch="rf_detr")
    return trained, imported


def _assert_same_including_key_order(a, b):
    assert a == b
    # dict equality ignores insertion order; the serialized manifest must not.
    assert json.dumps(a) == json.dumps(b)


@pytest.mark.parametrize("image_size,class_names,score", [
    (512, ["blue_plate"], 0.5),
    (576, ["plate", "luggage"], 0.3),
    (704, ["a", "b", "c"], 0.08),
    (384, ["x"], 0.25),
])
def test_rfdetr_trained_manifest_equals_smart_import_manifest(image_size, class_names, score):
    trained, imported = _rfdetr_pair(image_size, class_names, score)
    _assert_same_including_key_order(trained, imported)
    stage = trained["model_graph"]["stages"][0]
    assert stage["type"] == "rf_detr_object_detection" and stage["normalize"] is True
    assert trained["detection"]["layout"] == "rf_detr"
    assert trained["detection"]["top_k"] == 300
    assert trained["detection"]["preserve_aspect"] is False
    assert "iou_threshold" not in trained["detection"]


def test_rfdetr_parity_default_preserve_aspect_is_smart_import_default():
    """build_detection_device_manifest defaults preserve_aspect to False for
    rf_detr (square resize), which is also generate_dda_package's default."""
    trained = dt.build_detection_device_manifest(
        image_width=512, image_height=512, num_classes=1, class_names=["x"],
        score_threshold=0.5, detection_arch="rf_detr")
    trained.pop("dataset")
    imported = _smart_import_manifest(512, ["x"], 0.5, 0.45, preserve_aspect=False,
                                      detection_arch="rf_detr")
    _assert_same_including_key_order(trained, imported)


def test_rfdetr_parity_holds_with_preserve_aspect_on():
    trained, imported = _rfdetr_pair(512, ["x"], 0.5, preserve_aspect=True)
    _assert_same_including_key_order(trained, imported)
    assert trained["detection"]["preserve_aspect"] is True


def test_rfdetr_and_yolo_manifests_differ_only_where_the_arch_does():
    """Guard against the rf_detr branch drifting into a different document
    shape: the two arches share every key except the stage type / normalize
    flag / decoder layout / NMS-vs-top-k field."""
    yolo = _trained_manifest(512, ["x"], 0.5, 0.45, preserve_aspect=False)
    rf = _trained_manifest(512, ["x"], 0.5, None, preserve_aspect=False, detection_arch="rf_detr")
    assert set(yolo) == set(rf)
    assert set(yolo["model_graph"]["stages"][0]) == set(rf["model_graph"]["stages"][0])
    assert set(yolo["detection"]) - set(rf["detection"]) == {"iou_threshold"}
    assert set(rf["detection"]) - set(yolo["detection"]) == {"top_k"}
    assert yolo["model_graph"]["stages"][0]["output_shape"] == \
        rf["model_graph"]["stages"][0]["output_shape"]


@settings(max_examples=15, deadline=None)
@given(
    k=st.integers(min_value=7, max_value=35),          # 224..1120, multiples of 32
    n=st.integers(min_value=1, max_value=6),
    score=st.floats(min_value=0.01, max_value=0.99),
    preserve_aspect=st.booleans(),
)
def test_rfdetr_parity_property(k, n, score, preserve_aspect):
    names = [f"c{i}" for i in range(n)]
    trained, imported = _rfdetr_pair(k * 32, names, score, preserve_aspect=preserve_aspect)
    _assert_same_including_key_order(trained, imported)
