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


# ---------------------------------------------------------------------------
# RF-DETR-Seg (rfdetr-seg-support task 6.3, Requirement 6.2, DD6)
#
# generate_dda_package's instance_segmentation branch CALLS the shared builder
# (the one packaging.package_trained_detection_component uses for a trained Seg
# record) and then writes the three real output shapes on the stage exactly as
# packaging does. These cases pin that the two call paths produce the same
# bytes for every Output_Mode x Seg size x with / without names.
# ---------------------------------------------------------------------------

SEG_SIZES = sorted(dt.RFDETR_SEG_SIZES.items(), key=lambda kv: kv[1])
SEG_OUTPUT_NAMES = ["dets", "labels", "masks"]


def _seg_package(image_size, class_names, num_classes, output_mode, mask_threshold, output_names,
                 num_queries, score=0.5):
    """generate_dda_package for a Seg ONNX import; returns the three members."""
    with tempfile.TemporaryDirectory() as td:
        onnx_path = os.path.join(td, "model.onnx")
        with open(onnx_path, "wb") as fh:
            fh.write(b"\x08\x07")
        out = os.path.join(td, "pkg.tar.gz")
        model_converter.generate_dda_package(
            model_path=onnx_path,
            model_name="parity",
            model_type="instance_segmentation",
            image_width=image_size,
            image_height=image_size,
            num_classes=num_classes,
            class_names=list(class_names) if class_names else None,
            output_path=out,
            export_format="onnx",
            score_threshold=score,
            iou_threshold=0.45,       # accepted and ignored, as for rf_detr
            detection_arch="rf_detr_seg",
            preserve_aspect=False,
            output_mode=output_mode,
            mask_threshold=mask_threshold,
            num_queries=num_queries,
            output_names=output_names,
        )
        with tarfile.open(out, "r:gz") as tar:
            members = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
    import yaml
    return (json.loads(members["export_artifacts/manifest.json"]),
            json.loads(members["mochi.json"]),
            yaml.safe_load(members["config.yaml"]))


def _seg_smart_import_manifest(*args, **kwargs):
    return _seg_package(*args, **kwargs)[0]


def _seg_trained_manifest(image_size, class_names, num_classes, output_mode, mask_threshold,
                          output_names, num_queries, score=0.5):
    """What package_trained_detection_component writes for a Seg record: the
    builder's manifest minus the `dataset` block, with the three real shapes
    training_metadata.json recorded -- [boxes, logits, masks] -- on the stage
    (output_shape = logits)."""
    m = dt.build_detection_device_manifest(
        image_width=image_size, image_height=image_size, num_classes=num_classes,
        class_names=list(class_names) if class_names else None, score_threshold=score,
        preserve_aspect=False, detection_arch="rf_detr_seg", top_k=num_queries,
        output_mode=output_mode, mask_threshold=mask_threshold, output_names=output_names)
    m.pop("dataset")
    grid = image_size // dt.RFDETR_SEG_MASK_DOWNSAMPLE
    boxes, logits, masks = [1, num_queries, 4], [1, num_queries, num_classes + 1], [1, num_queries, grid, grid]
    stage = m["model_graph"]["stages"][0]
    stage["output_shape"] = logits
    stage["output_shapes"] = [boxes, logits, masks]
    return m


@pytest.mark.parametrize("with_names", [True, False], ids=["named", "nameless"])
@pytest.mark.parametrize("size,native", SEG_SIZES)
@pytest.mark.parametrize("output_mode", ["instance", "semantic"])
def test_rfdetr_seg_trained_manifest_equals_smart_import_manifest(output_mode, size, native, with_names):
    names = ["disc", "block"]
    num_classes = 2
    queries = dt.RFDETR_SEG_QUERIES_FOR_SIZE[size]
    class_names = names if with_names else None
    output_names = SEG_OUTPUT_NAMES if with_names else None
    trained = _seg_trained_manifest(native, class_names, num_classes, output_mode, 0.5, output_names, queries)
    imported = _seg_smart_import_manifest(native, class_names, num_classes, output_mode, 0.5, output_names,
                                          queries)
    _assert_same_including_key_order(trained, imported)
    stage = trained["model_graph"]["stages"][0]
    assert "detection" not in stage  # the decoder block is top-level only (the base model merges it)
    assert stage["type"] == dt.stage_type_for("rf_detr_seg", output_mode) and stage["normalize"] is True
    assert stage["output_shape"] == [1, queries, 3]
    assert stage["output_shapes"] == [[1, queries, 4], [1, queries, 3], [1, queries, native // 4, native // 4]]
    assert len(trained["model_graph"]["pixel_level_classes"]["names"]) == num_classes + 1
    assert trained["model_graph"]["pixel_level_classes"]["normal_ids"] == [0]
    det = trained["detection"]
    assert (det["layout"], det["top_k"], det["output_mode"], det["mask_threshold"]) == ("rf_detr", queries,
                                                                                      output_mode, 0.5)
    assert det["preserve_aspect"] is False and "iou_threshold" not in det
    assert ("task" in trained) == (output_mode == "instance")
    assert "dataset" not in imported
    if with_names:
        assert det["class_names"] == names and det["output_names"] == SEG_OUTPUT_NAMES
        assert trained["model_graph"]["pixel_level_classes"]["names"] == ["background", "disc", "block"]
    else:
        assert "class_names" not in det and "output_names" not in det
        assert trained["model_graph"]["pixel_level_classes"]["names"] == ["background", "class_0", "class_1"]


def test_rfdetr_seg_smart_import_defaults_match_the_builder_defaults():
    # No mode / threshold / queries / names given: instance, 0.5, the Seg size
    # whose native resolution is the input (312 -> nano -> Q 100), no names.
    imported = _seg_smart_import_manifest(312, ["disc", "block"], 2, None, None, None, None)
    trained = _seg_trained_manifest(312, ["disc", "block"], 2, "instance", 0.5, None, 100)
    _assert_same_including_key_order(trained, imported)
    # An off-table input falls back to RFDETR_TOP_K, as the detection manifest does.
    imported = _seg_smart_import_manifest(640, ["x"], 1, "semantic", 0.3, None, None)
    trained = _seg_trained_manifest(640, ["x"], 1, "semantic", 0.3, None, dt.RFDETR_TOP_K)
    _assert_same_including_key_order(trained, imported)
    assert imported["model_graph"]["stages"][0]["output_shapes"][2] == [1, 300, 160, 160]
    # num_classes may come from the names alone; a request with neither is refused.
    imported = _seg_smart_import_manifest(384, ["a", "b", "c"], None, "instance", 0.5, SEG_OUTPUT_NAMES, 100)
    assert imported["detection"]["num_classes"] == 3 and imported["model_graph"]["stages"][0]["num_classes"] == 3
    with pytest.raises(ValueError, match="num_classes or class_names"):
        _seg_smart_import_manifest(384, None, None, "instance", 0.5, None, 100)


def test_rfdetr_seg_package_members_describe_the_seg_stage():
    manifest, mochi, config = _seg_package(432, ["disc", "block"], 2, "semantic", 0.4, SEG_OUTPUT_NAMES, 200)
    assert mochi["stages"][0] == {"type": "rf_detr_semantic_segmentation", "input_shape": [1, 3, 432, 432],
                                  "output_shape": [1, 200, 3], "num_classes": 2}
    assert config == {"dataset": {"image_width": 432, "image_height": 432, "num_classes": 2,
                                  "class_names": ["disc", "block"]}}
    assert manifest["runtime"] == "onnx" and manifest["runtime_artifact"] == "model.onnx"
    assert manifest["detection"]["mask_threshold"] == 0.4 and "task" not in manifest


def test_rfdetr_seg_branch_is_keyed_on_the_model_type_alone():
    # The predicate is `model_type == 'instance_segmentation'` (Req 6.2); the
    # legacy pair (model_type 'segmentation' + detection_arch 'rf_detr'),
    # which convert_model rewrites before this call, takes the generic
    # segmentation branch here and the detection branches are untouched.
    with tempfile.TemporaryDirectory() as td:
        onnx_path = os.path.join(td, "model.onnx")
        with open(onnx_path, "wb") as fh:
            fh.write(b"\x08\x07")
        out = os.path.join(td, "pkg.tar.gz")
        model_converter.generate_dda_package(
            model_path=onnx_path, model_name="legacy", model_type="segmentation", image_width=384,
            image_height=384, num_classes=2, class_names=["disc", "block"], output_path=out,
            export_format="onnx", detection_arch="rf_detr")
        with tarfile.open(out, "r:gz") as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("export_artifacts/manifest.json"))
            generic = json.load(tar.extractfile(member))
    stage = generic["model_graph"]["stages"][0]
    assert stage["type"] == "segmentation" and stage["normalize"] is False
    assert "pixel_level_classes" not in generic["model_graph"] and "detection" not in generic
    assert "detection" not in stage and "output_shapes" not in stage
    # and the rf_detr detection manifest is still byte-identical to the builder's
    trained, imported = _rfdetr_pair(512, ["x"], 0.5)
    _assert_same_including_key_order(trained, imported)
