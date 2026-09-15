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


def _smart_import_manifest(image_size, class_names, score, iou, preserve_aspect=True):
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
            detection_arch="yolo",
            preserve_aspect=preserve_aspect,
        )
        with tarfile.open(out, "r:gz") as tar:
            member = next(m for m in tar.getmembers()
                          if m.name.endswith("export_artifacts/manifest.json"))
            return json.load(tar.extractfile(member))


def _trained_manifest(image_size, class_names, score, iou, preserve_aspect=True):
    m = dt.build_detection_device_manifest(
        image_width=image_size, image_height=image_size,
        num_classes=len(class_names), class_names=list(class_names),
        score_threshold=score, iou_threshold=iou, preserve_aspect=preserve_aspect)
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
