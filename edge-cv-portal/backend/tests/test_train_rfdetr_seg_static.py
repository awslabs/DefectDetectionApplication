"""
Static / host-side tests for `datasets/detection_training/train_rfdetr_seg.py`,
the RF-DETR-Seg SageMaker entry point (spec rfdetr-seg-support, Requirement 4;
task 4.4).

No GPU, torch, rfdetr, onnx or numpy on this host: the module is imported with
those packages BLOCKED (a meta-path finder that raises ImportError) to prove
every heavy import is lazy, and only the pure helpers are exercised -- the Seg
size table against the shared layer and the rfdetr 1.10.1 block values, the
block resolution rule, the instance-manifest first-line gate, the TEST METRICS
mapping with its six keys, the three-output ONNX contract (logits exactly
`[1, Q, C + 1]`, masks `[1, Q, R/4, R/4]`, names settle a three-class graph),
the post-export static-dim stamp (spike S2 / mismatch M-1), the metadata
builder, and the Seg sourcedir bundles built from the real
`datasets/detection_training` directory. `train_rfdetr.py` and its own suite
stay untouched (Requirement 17.1).

# Validates: Requirements 1.6, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 17.1
"""
import importlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import types
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import detection_training as dt

_HERE = os.path.dirname(os.path.abspath(__file__))
_DETECTION_TRAINING = os.path.abspath(
    os.path.join(_HERE, "..", "..", "..", "datasets", "detection_training"))
if _DETECTION_TRAINING not in sys.path:
    # Appended, not prepended: must never shadow a portal layer or backend
    # module for the other tests sharing the session.
    sys.path.append(_DETECTION_TRAINING)

_HEAVY = ("rfdetr", "torch", "onnx", "onnxruntime", "numpy")


class _Blocked:
    """Meta-path finder that makes `import <name>` raise ImportError."""

    def __init__(self, names):
        self.names = set(names)

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.names:
            raise ImportError(f"{fullname} is blocked in this test")
        return None


def _import_train_rfdetr_seg():
    blocked = _Blocked(_HEAVY)
    sys.meta_path.insert(0, blocked)
    saved = {n: sys.modules.pop(n) for n in list(sys.modules) if n.split(".")[0] in _HEAVY}
    try:
        for mod in ("train_rfdetr_seg", "train_rfdetr"):
            sys.modules.pop(mod, None)
        return importlib.import_module("train_rfdetr_seg")
    finally:
        sys.meta_path.remove(blocked)
        sys.modules.update(saved)


ts = _import_train_rfdetr_seg()
det = ts.det

SEG_SIZE_ORDER = ["nano", "small", "medium", "large", "xlarge", "2xlarge"]
# rfdetr 1.10.1 config.py:879-973: every RFDETRSeg*Config has patch_size 12;
# num_windows is 1 for nano and 2 for the other five sizes.
RFDETR_1_10_1_PATCH_SIZE = 12
RFDETR_1_10_1_NUM_WINDOWS = {"nano": 1, "small": 2, "medium": 2, "large": 2,
                             "xlarge": 2, "2xlarge": 2}


def _cfg(**over):
    base = dict(manifest_s3="s3://b/m.manifest", images_s3=None, size="small",
                model_class="RFDETRSegSmall", native=384, top_k=100, block=24,
                resolution=384, epochs=100, batch=4, grad_accum=4, lr=1e-4,
                patience=10, opset=17, base_weights_s3=None, base_weights_member=None)
    base.update(over)
    return SimpleNamespace(**base)


SEG_METRICS = {"test_map50": 0.9, "test_map50_95": 0.6, "test_precision": 0.8,
               "test_recall": 0.7, "test_mask_map50": 0.85, "test_mask_map50_95": 0.55}


# ---------------------------------------------------------------------------
# Import hygiene / size table (Req 1.6, 4.1, 4.4)
# ---------------------------------------------------------------------------

def test_module_imports_without_the_heavy_libraries():
    assert ts.SEG_SIZE_CLASSES["small"] == ("RFDETRSegSmall", 384, 100, 24)
    for name in _HEAVY:
        assert name not in vars(ts)
    assert ts.det is det and det.SIZE_CLASSES["small"] == ("RFDETRSmall", 512)


def test_seg_size_classes_match_shared_layer_and_rfdetr_blocks():
    assert list(ts.SEG_SIZE_CLASSES) == SEG_SIZE_ORDER
    assert {k: v[0] for k, v in ts.SEG_SIZE_CLASSES.items()} == dt.RFDETR_SEG_CLASS_FOR_SIZE
    assert {k: v[1] for k, v in ts.SEG_SIZE_CLASSES.items()} == dt.RFDETR_SEG_SIZES
    assert {k: v[2] for k, v in ts.SEG_SIZE_CLASSES.items()} == dt.RFDETR_SEG_QUERIES_FOR_SIZE
    assert {k: v[3] for k, v in ts.SEG_SIZE_CLASSES.items()} == dt.RFDETR_SEG_INPUT_BLOCK_FOR_SIZE
    for size, (cls, native, queries, block) in ts.SEG_SIZE_CLASSES.items():
        # Req 4.4: the block is patch_size x num_windows of rfdetr 1.10.1.
        assert block == RFDETR_1_10_1_PATCH_SIZE * RFDETR_1_10_1_NUM_WINDOWS[size], size
        assert native % block == 0 and ts.RESOLUTION_MIN <= native <= ts.RESOLUTION_MAX
        assert ts.native_resolution(size) == native
        assert cls.startswith("RFDETRSeg") and queries in (100, 200, 300)
    assert (ts.RESOLUTION_MIN, ts.RESOLUTION_MAX) == (240, 1152)
    assert ts.DEFAULT_SIZE == "small" == dt.RFDETR_SEG_DEFAULTS["rfdetr_seg_size"]
    assert ts.MASK_DOWNSAMPLE == dt.RFDETR_SEG_MASK_DOWNSAMPLE == 4
    assert ts.STAGE_TYPE == dt.RFDETR_SEG_INSTANCE_STAGE_TYPE == "rf_detr_instance_segmentation"
    assert ts.OUTPUT_MODE == dt.DEFAULT_OUTPUT_MODE == "instance"
    assert ts.CHECKPOINT_MEMBER == dt.CHECKPOINT_MEMBER_FOR_ARCH["rf_detr_seg"] == det.CHECKPOINT_MEMBER
    assert ts.DEVICE_SCORE_THRESHOLD == ts.DEVICE_MASK_THRESHOLD == 0.5
    assert ts.ONNX_MASKS_NAME == "masks"


def test_seg_table_is_separate_from_the_detection_table():
    # Req 4.1: `det.SIZE_CLASSES` keeps exactly the four detection sizes and
    # the detection rule of 32 (test_train_rfdetr_static.py pins them).
    assert set(det.SIZE_CLASSES) == {"nano", "small", "medium", "large"}
    assert det.RESOLUTION_STEP == 32
    assert not set(v[0] for v in det.SIZE_CLASSES.values()) & \
        set(v[0] for v in ts.SEG_SIZE_CLASSES.values())
    assert ts.SEG_SIZE_CLASSES is not det.SIZE_CLASSES


def test_metric_keys_extend_the_detection_keys():
    assert ts.METRIC_KEYS == det.METRIC_KEYS + ("test_mask_map50", "test_mask_map50_95")
    assert ts._SEG_EVAL_KEY_MAP == {**det._EVAL_KEY_MAP, "segm_mAP_50": "test_mask_map50",
                                    "segm_mAP_50_95": "test_mask_map50_95"}
    assert det.METRIC_KEYS == ("test_map50", "test_map50_95", "test_precision", "test_recall")


# ---------------------------------------------------------------------------
# read_config (Req 4.1, 4.4)
# ---------------------------------------------------------------------------

_ENV = ("RESOLUTION", "EPOCHS", "BATCH", "GRAD_ACCUM", "LR", "PATIENCE", "ONNX_OPSET",
        "BASE_WEIGHTS_S3", "BASE_WEIGHTS_MEMBER", "IMAGES_S3", "RFDETR_SEG_SIZE", "RFDETR_SIZE")


def test_read_config_defaults_resolution_to_the_native(monkeypatch):
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MANIFEST_S3", "s3://b/k.manifest")
    for size, (cls, native, queries, block) in ts.SEG_SIZE_CLASSES.items():
        monkeypatch.setenv("RFDETR_SEG_SIZE", size)
        cfg = ts.read_config()
        assert (cfg.size, cfg.model_class, cfg.native, cfg.top_k, cfg.block) == \
            (size, cls, native, queries, block)
        assert cfg.resolution == native
    assert (cfg.epochs, cfg.batch, cfg.grad_accum, cfg.lr, cfg.patience, cfg.opset) == \
        (100, 4, 4, 1e-4, 10, 17)
    assert cfg.base_weights_s3 is None and cfg.base_weights_member is None
    monkeypatch.delenv("RFDETR_SEG_SIZE")
    cfg = ts.read_config()
    assert cfg.size == "small" and cfg.resolution == 384
    monkeypatch.setenv("RESOLUTION", "480")
    assert ts.read_config().resolution == 480
    monkeypatch.setenv("RESOLUTION", "")
    assert ts.read_config().resolution == 384
    # The detection size variable is not read.
    monkeypatch.setenv("RFDETR_SIZE", "large")
    assert ts.read_config().size == "small"


def test_read_config_rejects_unknown_size_and_bad_resolution(monkeypatch):
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RFDETR_SEG_SIZE", "huge")
    with pytest.raises(SystemExit) as exc:
        ts.read_config()
    assert str(exc.value).startswith("FATAL: RFDETR_SEG_SIZE must be one of") and "huge" in str(exc.value)
    monkeypatch.setenv("RFDETR_SEG_SIZE", "small")
    monkeypatch.setenv("RESOLUTION", "320")
    with pytest.raises(SystemExit) as exc:
        ts.read_config()
    assert str(exc.value).startswith(
        "FATAL: RESOLUTION must be a multiple of 24 between 240 and 1152 for RF-DETR-Seg small")


# ---------------------------------------------------------------------------
# Block resolution rule (Req 4.4; O5 b)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size", SEG_SIZE_ORDER)
def test_validate_seg_resolution_accepts_natives_and_block_multiples(size):
    native = ts.SEG_SIZE_CLASSES[size][1]
    block = ts.SEG_SIZE_CLASSES[size][3]
    assert ts.validate_seg_resolution(size, native) == native
    assert ts.validate_seg_resolution(size, str(native)) == native
    assert ts.validate_seg_resolution(size, 240) == 240
    assert ts.validate_seg_resolution(size, 1152) == 1152
    assert ts.validate_seg_resolution(size, native + block) == native + block


@pytest.mark.parametrize("size", SEG_SIZE_ORDER)
def test_validate_seg_resolution_rejects_320_for_every_size(size):
    # 320 is a multiple of 32 (the detection rule) but of neither 12 nor 24.
    block = ts.SEG_SIZE_CLASSES[size][3]
    with pytest.raises(SystemExit) as exc:
        ts.validate_seg_resolution(size, 320)
    assert str(exc.value) == (f"FATAL: RESOLUTION must be a multiple of {block} between "
                              f"240 and 1152 for RF-DETR-Seg {size} (got 320)")


def test_validate_seg_resolution_324_nano_only():
    assert ts.validate_seg_resolution("nano", 324) == 324
    for size in SEG_SIZE_ORDER[1:]:
        with pytest.raises(SystemExit) as exc:
            ts.validate_seg_resolution(size, 324)
        assert "multiple of 24" in str(exc.value) and size in str(exc.value)


@pytest.mark.parametrize("resolution", [228, 216, 1164, 1176, 0, -24, "abc", None, "384.5"])
def test_validate_seg_resolution_bounds_and_junk(resolution):
    with pytest.raises(SystemExit) as exc:
        ts.validate_seg_resolution("small", resolution)
    text = str(exc.value)
    assert text.startswith("FATAL: RESOLUTION must be a multiple of 24 between 240 and 1152 "
                           "for RF-DETR-Seg small")
    assert "(got " in text


def test_validate_seg_resolution_unknown_size():
    with pytest.raises(SystemExit) as exc:
        ts.validate_seg_resolution("tiny", 384)
    assert "RFDETR_SEG_SIZE must be one of" in str(exc.value)


@settings(max_examples=300, deadline=None)
@given(size=st.sampled_from(SEG_SIZE_ORDER), r=st.integers(min_value=-2000, max_value=3000))
def test_validate_seg_resolution_property(size, r):
    block = ts.SEG_SIZE_CLASSES[size][3]
    ok = r > 0 and r % block == 0 and 240 <= r <= 1152
    if ok:
        assert ts.validate_seg_resolution(size, r) == r
    else:
        with pytest.raises(SystemExit):
            ts.validate_seg_resolution(size, r)


# ---------------------------------------------------------------------------
# Instance-manifest gate (Req 4.2)
# ---------------------------------------------------------------------------

_INSTANCE_LINE = json.dumps({
    "source-ref": "s3://b/i.jpg",
    "instance-segmentation": {"image_size": [{"width": 4, "height": 2, "depth": 3}],
                              "annotations": [{"class_id": 0, "left": 0, "top": 0, "width": 2,
                                               "height": 2, "rle": "0 2 2 2 2"}]},
    "instance-segmentation-metadata": {"type": "groundtruth/instance-segmentation",
                                       "class-map": {"0": "a"}},
})
_GT_INSTANCE_LINE = json.dumps({
    "source-ref": "s3://b/i.jpg",
    "my-job": {"image_size": [{"width": 4, "height": 2, "depth": 3}], "annotations": []},
    "my-job-metadata": {"type": "groundtruth/instance-segmentation", "job-name": "my-job"},
})
_BBOX_LINE = json.dumps({
    "source-ref": "s3://b/i.jpg",
    "bounding-box": {"image_size": [{"width": 4, "height": 2, "depth": 3}],
                     "annotations": [{"class_id": 0, "left": 0, "top": 0, "width": 2, "height": 2}]},
    "bounding-box-metadata": {"type": "groundtruth/object-detection", "class-map": {"0": "a"}},
})
_MASK_REF_LINE = json.dumps({
    "source-ref": "s3://b/i.jpg",
    "anomaly-label": 1,
    "anomaly-mask-ref": "s3://b/i_mask.png",
    "anomaly-mask-ref-metadata": {"internal-color-map": {"1": {"class-name": "scratch"}}},
})


@pytest.mark.parametrize("line,expected", [
    (_INSTANCE_LINE, True),
    (_GT_INSTANCE_LINE, True),
    (_BBOX_LINE, False),
    (_MASK_REF_LINE, False),
    (json.dumps({"source-ref": "s3://b/i.jpg"}), False),
    (json.dumps({"source-ref": "s3://b/i.jpg",
                 "orphan-metadata": {"type": "groundtruth/instance-segmentation"}}), False),
    ("not json", False),
    ("[1, 2]", False),
    ("", False),
])
def test_first_line_is_instance_manifest(line, expected):
    assert ts.first_line_is_instance_manifest(line) is expected


def test_stage_dataset_refuses_a_bbox_manifest_before_converting(monkeypatch, tmp_path):
    manifest = tmp_path / "output.manifest"
    manifest.write_text("\n" + _BBOX_LINE + "\n" + _INSTANCE_LINE + "\n")
    monkeypatch.setattr(ts, "stage_manifest_and_images",
                        lambda m, i, w: (manifest, tmp_path / "images"))
    calls = []
    monkeypatch.setattr(ts, "run_converter", lambda *a, **kw: calls.append((a, kw)))
    with pytest.raises(SystemExit) as exc:
        ts.stage_dataset(_cfg())
    assert str(exc.value) == ts.INSTANCE_MANIFEST_FATAL
    assert str(exc.value).startswith("FATAL: manifest line 1 carries no instance-segmentation")
    assert "anomaly-mask-ref" in str(exc.value) and calls == []


def test_stage_dataset_empty_manifest_is_fatal(monkeypatch, tmp_path):
    manifest = tmp_path / "output.manifest"
    manifest.write_text("\n\n")
    monkeypatch.setattr(ts, "stage_manifest_and_images",
                        lambda m, i, w: (manifest, tmp_path / "images"))
    with pytest.raises(SystemExit) as exc:
        ts.stage_dataset(_cfg())
    assert str(exc.value).startswith("FATAL:") and "empty" in str(exc.value)


def _write_split(root, split, n_images, categories=None):
    d = os.path.join(root, split)
    os.makedirs(d, exist_ok=True)
    data = {"images": [{"id": i, "file_name": f"f{i}.jpg"} for i in range(n_images)],
            "annotations": [],
            "categories": categories or [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]}
    with open(os.path.join(d, "_annotations.coco.json"), "w") as fh:
        json.dump(data, fh)


def test_stage_dataset_runs_the_rfdetr_coco_converter_and_checks_splits(monkeypatch, tmp_path, capsys):
    manifest = tmp_path / "output.manifest"
    manifest.write_text(_INSTANCE_LINE + "\n")
    images = tmp_path / "images"
    dataset = tmp_path / "dataset"
    monkeypatch.setattr(ts, "stage_manifest_and_images", lambda m, i, w: (manifest, images))
    calls = []
    splits = ["train", "valid"]

    def fake_converter(m, imgs, out, fmt, **kw):
        calls.append((m, imgs, out, fmt, kw))
        if "train" in splits:
            _write_split(str(dataset), "train", 2, [{"id": 2, "name": "b"}, {"id": 1, "name": "a"}])
        if "valid" in splits:
            _write_split(str(dataset), "valid", 1)
        return dataset

    monkeypatch.setattr(ts, "run_converter", fake_converter)
    monkeypatch.setattr(ts, "WORK", tmp_path)
    out, names = ts.stage_dataset(_cfg())
    assert out == dataset and names == ["a", "b"]
    assert calls == [(manifest, images, tmp_path / "dataset", "coco", {"coco_layout": "rfdetr"})]
    captured = capsys.readouterr().out
    assert "WARN: no test split" in captured and "classes (2): ['a', 'b']" in captured
    # A missing valid split is FATAL naming the split (Req 4.2).
    shutil.rmtree(dataset)
    splits.remove("valid")
    with pytest.raises(SystemExit) as exc:
        ts.stage_dataset(_cfg())
    assert "FATAL" in str(exc.value) and "'valid'" in str(exc.value)


# ---------------------------------------------------------------------------
# TEST METRICS line (Req 4.7)
# ---------------------------------------------------------------------------

def test_seg_metrics_from_eval_maps_the_six_keys(capsys):
    result = {"test/loss": 9.36, "test/mAP_50_95": 0.6818, "test/mAP_50": 0.6985,
              "test/mAP_75": 0.6985, "test/mAR": 0.695, "test/segm_mAP_50_95": 0.6288,
              "test/segm_mAP_50": 0.7, "test/F1": 0.4762, "test/precision": 0.4545,
              "test/recall": 0.5}
    m = ts.seg_metrics_from_eval(result, "test")
    assert list(m) == list(ts.METRIC_KEYS)
    assert m == {"test_map50": 0.6985, "test_map50_95": 0.6818, "test_precision": 0.4545,
                 "test_recall": 0.5, "test_mask_map50": 0.7, "test_mask_map50_95": 0.6288}
    assert "WARN" not in capsys.readouterr().out
    # The detection mapping over the same dict is unchanged (Req 17.1).
    assert det.metrics_from_eval(result, "test") == {k: m[k] for k in det.METRIC_KEYS}


def test_seg_metrics_from_eval_missing_key_is_nan_with_one_warn(capsys):
    result = {"test/mAP_50": 0.9, "test/mAP_50_95": 0.8, "test/precision": 0.7,
              "test/recall": 0.6, "test/segm_mAP_50": 0.5}
    m = ts.seg_metrics_from_eval(result, "test")
    assert math.isnan(m["test_mask_map50_95"])
    assert m["test_mask_map50"] == 0.5 and m["test_map50"] == 0.9
    out = capsys.readouterr().out
    assert out.count("WARN") == 1
    assert "WARN: evaluate() returned no test/segm_mAP_50_95" in out
    # NaN never matches the MetricDefinitions regex: the metric is absent, not 0.0.
    line = "TEST METRICS: " + json.dumps(m)
    md = next(d for d in dt.RFDETR_SEG_METRIC_DEFINITIONS if d["Name"] == "test:mask-mAP50-95")
    assert re.search(md["Regex"], line) is None


def test_seg_metrics_from_eval_val_prefix_and_non_numeric(capsys):
    m = ts.seg_metrics_from_eval({"val/segm_mAP_50": 0.5, "val/mAP_50": "n/a"}, "val")
    assert m["test_mask_map50"] == 0.5
    assert all(math.isnan(m[k]) for k in ts.METRIC_KEYS if k != "test_mask_map50")
    out = capsys.readouterr().out
    assert out.count("WARN") == 5
    assert "non-numeric val/mAP_50" in out
    # Tensors with .item() are accepted, as in the detection mapping.
    class _T:
        def item(self):
            return 0.25
    assert ts.seg_metrics_from_eval({"test/segm_mAP_50": _T()})["test_mask_map50"] == 0.25


def test_read_test_metrics_prints_the_six_key_line(tmp_path, capsys):
    _write_split(str(tmp_path), "test", 1)

    class _Model:
        def evaluate(self, **kw):
            assert kw["split"] == "test" and kw["batch_size"] == 4
            return {"test/mAP_50": 0.9, "test/mAP_50_95": 0.8, "test/precision": 0.7,
                    "test/recall": 0.6, "test/segm_mAP_50": 0.5, "test/segm_mAP_50_95": 0.4}

    m = ts.read_test_metrics(_Model(), tmp_path, _cfg())
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("TEST METRICS: "))
    assert json.loads(line[len("TEST METRICS: "):]) == m
    assert list(m) == list(ts.METRIC_KEYS)
    assert "rfdetr evaluate(test):" in out


def test_read_test_metrics_never_fatal(tmp_path, capsys):
    class _Broken:
        def evaluate(self, **kw):
            raise RuntimeError("no coco eval")

    assert ts.read_test_metrics(_Broken(), tmp_path, _cfg()) == {}
    out = capsys.readouterr().out
    assert "WARN: no test split" in out and "WARN: val-split evaluation failed" in out


def test_rf_detr_seg_metric_definitions_lift_a_seg_line_and_only_their_own_key():
    line = "TEST METRICS: " + json.dumps(SEG_METRICS)
    seen = {}
    for md in dt.METRIC_DEFINITIONS_FOR_ARCH["rf_detr_seg"]:
        hits = re.findall(md["Regex"], line)
        assert len(hits) == 1, md
        seen[md["Name"]] = float(hits[0])
    assert seen == {"test:mAP50": 0.9, "test:mAP50-95": 0.6, "test:precision": 0.8,
                    "test:recall": 0.7, "test:mask-mAP50": 0.85, "test:mask-mAP50-95": 0.55}
    # Each box regex matches only its own key: `"test_map50": ` is not a
    # substring of `"test_mask_map50": `, so the detection list over a Seg
    # line lifts exactly the four box values.
    for md in dt.DETECTION_METRIC_DEFINITIONS:
        hits = re.findall(md["Regex"], line)
        assert len(hits) == 1, md
    assert dt.METRIC_DEFINITIONS_FOR_ARCH["rf_detr"] is dt.DETECTION_METRIC_DEFINITIONS
    assert dt.METRIC_DEFINITIONS_FOR_ARCH["rf_detr_seg"][:4] == dt.DETECTION_METRIC_DEFINITIONS


# ---------------------------------------------------------------------------
# Three-output ONNX contract (Req 4.5; O6 a)
# ---------------------------------------------------------------------------

Q = 100
GRID = 96   # 384 // 4


def test_verify_seg_outputs_accepts_the_contract_and_returns_graph_order():
    outs = [("dets", [1, Q, 4]), ("labels", [1, Q, 3]), ("masks", [1, Q, GRID, GRID])]
    assert ts.verify_seg_outputs(outs, 2, 384) == (
        Q, [1, Q, 4], [1, Q, 3], [1, Q, GRID, GRID], ["dets", "labels", "masks"])
    # Any order: the returned names follow the graph, the shapes their roles.
    shuffled = [("masks", [1, Q, GRID, GRID]), ("labels", [1, Q, 3]), ("dets", [1, Q, 4])]
    assert ts.verify_seg_outputs(shuffled, 2, 384) == (
        Q, [1, Q, 4], [1, Q, 3], [1, Q, GRID, GRID], ["masks", "labels", "dets"])
    # Bare shapes (no names) are identified by shape alone when S != 4.
    assert ts.verify_seg_outputs([[1, Q, 3], [1, Q, GRID, GRID], [1, Q, 4]], 2, 384) == (
        Q, [1, Q, 4], [1, Q, 3], [1, Q, GRID, GRID], [None, None, None])
    # Foreign names are fine: shape decides.
    assert ts.verify_seg_outputs([("a", [1, Q, 92]), ("b", [1, Q, 4]), ("c", [1, Q, 78, 78])],
                                 91, 312)[:4] == (Q, [1, Q, 4], [1, Q, 92], [1, Q, 78, 78])


def test_verify_seg_outputs_names_settle_a_three_class_graph():
    # num_classes == 3 -> logits [1, Q, 4], the same shape as boxes.
    outs = [("labels", [1, Q, 4]), ("masks", [1, Q, GRID, GRID]), ("dets", [1, Q, 4])]
    q, boxes, logits, masks, names = ts.verify_seg_outputs(outs, 3, 384)
    assert (q, boxes, logits, masks) == (Q, [1, Q, 4], [1, Q, 4], [1, Q, GRID, GRID])
    assert names == ["labels", "masks", "dets"]
    # One usable name is enough: the other rank-3 output is the counterpart.
    assert ts.verify_seg_outputs([("dets", [1, Q, 4]), ("x", [1, Q, 4]), ("m", [1, Q, GRID, GRID])],
                                 3, 384)[4] == ["dets", "x", "m"]
    assert ts.verify_seg_outputs([("y", [1, Q, 4]), ("labels", [1, Q, 4]), ("m", [1, Q, GRID, GRID])],
                                 3, 384)[1:3] == ([1, Q, 4], [1, Q, 4])


def test_verify_seg_outputs_nameless_three_class_graph_is_ambiguous():
    with pytest.raises(ValueError) as exc:
        ts.verify_seg_outputs([[1, Q, 4], [1, Q, 4], [1, Q, GRID, GRID]], 3, 384)
    text = str(exc.value)
    assert text.startswith("FATAL: ONNX outputs [")
    assert f"are ambiguous (two [1, {Q}, 4] tensors, no dets/labels names)" in text
    # Names that are neither dets nor labels do not help.
    with pytest.raises(ValueError) as exc:
        ts.verify_seg_outputs([("a", [1, Q, 4]), ("b", [1, Q, 4]), ("c", [1, Q, GRID, GRID])], 3, 384)
    assert "are ambiguous" in str(exc.value)


@pytest.mark.parametrize("outputs,num_classes", [
    ([("dets", [1, Q, 4]), ("labels", [1, Q, 2]), ("masks", [1, Q, GRID, GRID])], 2),
    ([[1, Q, 4], [1, Q, 2], [1, Q, GRID, GRID]], 2),
    ([[1, Q, GRID, GRID], [1, Q, 3], [1, Q, 4]], 3),
    ([("labels", [1, Q, 1]), ("masks", [1, Q, GRID, GRID]), ("dets", [1, Q, 4])], 1),
])
def test_verify_seg_outputs_rejects_num_classes_slots_with_the_detection_message(outputs, num_classes):
    with pytest.raises(ValueError) as exc:
        ts.verify_seg_outputs(outputs, num_classes, 384)
    text = str(exc.value)
    assert text.startswith("FATAL: ONNX logits output")
    assert f"has only num_classes={num_classes} slots" in text
    assert "background slot" in text and "was not widened" in text
    assert f"[1, {Q}, {num_classes}]" in text and f"[1, {Q}, {num_classes + 1}]" in text
    # Identical wording to verify_two_outputs (Req 4.5 names that message).
    with pytest.raises(ValueError) as det_exc:
        det.verify_two_outputs([("dets", [1, Q, 4]), ("labels", [1, Q, num_classes])], num_classes)
    assert str(det_exc.value).split(" -- got ")[0] == text.split(" -- got ")[0]


@pytest.mark.parametrize("outputs,num_classes,resolution,msg", [
    ([[1, Q, 4], [1, Q, 3]], 2, 384, "exactly 3"),
    ([[1, Q, 4], [1, Q, 3], [1, Q, GRID, GRID], [1, Q, 1]], 2, 384, "exactly 3"),
    ([[1, Q, 4], [1, Q, 3], [1, Q, 3]], 2, 384, "two rank-3 tensors and one rank-4 tensor"),
    ([[1, Q, 4, 4], [1, Q, 3, 3], [1, Q, GRID, GRID]], 2, 384, "two rank-3 tensors and one rank-4 tensor"),
    ([[1, Q, 4], [1, 200, 3], [1, Q, GRID, GRID]], 2, 384, "disagree on Q"),
    ([[1, Q, 4], [1, Q, 3], [1, 200, GRID, GRID]], 2, 384, "disagree on Q"),
    ([[1, Q, 4], [1, Q, 3], [1, Q, 78, 78]], 2, 384, "is not [1, Q, 96, 96] (RESOLUTION 384 // 4)"),
    ([[1, Q, 4], [1, Q, 3], [1, Q, GRID, 95]], 2, 384, "is not [1, Q, 96, 96]"),
    ([[1, Q, 4], [1, Q, 7], [1, Q, GRID, GRID]], 2, 384, "do not match"),
    ([[1, Q, 5], [1, Q, 4], [1, Q, GRID, GRID]], 3, 384, "do not match"),
    ([[1, Q, 5], [1, Q, 6], [1, Q, GRID, GRID]], 4, 384, "do not match"),
    ([("dets", [1, Q, 3]), ("labels", [1, Q, 4]), ("masks", [1, Q, GRID, GRID])], 3, 384, "do not match"),
    ([[2, Q, 4], [2, Q, 3], [2, Q, GRID, GRID]], 2, 384, "batch 1"),
    ([[Q, 4], [Q, 3], [1, Q, GRID, GRID]], 2, 384, "static rank-3"),
    ([[1, "N", 4], [1, "N", 3], [1, "N", GRID, GRID]], 2, 384, "static rank-3"),
    ([[1, Q, 4], [1, Q, 3], [1, Q, "Addmasks_dim_2", "Addmasks_dim_3"]], 2, 384, "static rank-3"),
    ([[1, Q, 4], [1, Q, 3], [1, Q, 1, GRID, GRID]], 2, 384, "static rank-3"),
])
def test_verify_seg_outputs_rejects(outputs, num_classes, resolution, msg):
    with pytest.raises(ValueError) as exc:
        ts.verify_seg_outputs(outputs, num_classes, resolution)
    text = str(exc.value)
    assert text.startswith("FATAL:") and msg in text
    # The message quotes every output.
    for o in outputs:
        shape = list(o[1]) if isinstance(o, tuple) else list(o)
        assert str(shape) in text


@settings(max_examples=150, deadline=None)
@given(q=st.integers(min_value=1, max_value=600), c=st.integers(min_value=1, max_value=200),
       size=st.sampled_from(SEG_SIZE_ORDER), order=st.permutations([0, 1, 2]))
def test_verify_seg_outputs_property_accepts_c_plus_one(q, c, size, order):
    r = ts.SEG_SIZE_CLASSES[size][1]
    named = [("dets", [1, q, 4]), ("labels", [1, q, c + 1]), ("masks", [1, q, r // 4, r // 4])]
    outs = [named[i] for i in order]
    got = ts.verify_seg_outputs(outs, c, r)
    assert got == (q, [1, q, 4], [1, q, c + 1], [1, q, r // 4, r // 4], [n for n, _s in outs])


@settings(max_examples=150, deadline=None)
@given(q=st.integers(min_value=1, max_value=600), c=st.integers(min_value=1, max_value=200),
       order=st.permutations([0, 1, 2]))
def test_verify_seg_outputs_property_rejects_c(q, c, order):
    named = [("dets", [1, q, 4]), ("labels", [1, q, c]), ("masks", [1, q, GRID, GRID])]
    with pytest.raises(ValueError) as exc:
        ts.verify_seg_outputs([named[i] for i in order], c, 384)
    text = str(exc.value)
    assert text.startswith("FATAL:")
    assert f"[1, {q}, {c}]" in text and f"[1, {q}, {c + 1}]" in text


def test_verify_input_is_the_detection_helper():
    assert det.verify_input([("input", [1, 3, 312, 312])], 312) == [1, 3, 312, 312]
    with pytest.raises(ValueError):
        det.verify_input([("input", [1, 3, 384, 384])], 312)


# ---------------------------------------------------------------------------
# Static output dims (spike S2 / mismatch M-1)
# ---------------------------------------------------------------------------

class _Dim:
    """Protobuf TensorShapeProto.Dimension double: a oneof of dim_value / dim_param."""

    def __init__(self, value=None, param=None):
        self._value, self._param = value, param

    def HasField(self, name):
        return {"dim_value": self._value is not None, "dim_param": self._param is not None}[name]

    def ClearField(self, name):
        if name == "dim_value":
            self._value = None
        elif name == "dim_param":
            self._param = None
        else:
            raise AssertionError(name)

    @property
    def dim_value(self):
        return self._value if self._value is not None else 0

    @dim_value.setter
    def dim_value(self, v):
        self._value, self._param = int(v), None

    @property
    def dim_param(self):
        return self._param if self._param is not None else ""


def _output(name, declared):
    dims = [_Dim(value=d) if isinstance(d, int) else _Dim(param=d) for d in declared]
    return SimpleNamespace(name=name, type=SimpleNamespace(tensor_type=SimpleNamespace(
        shape=SimpleNamespace(dim=dims))))


def _declared(out):
    return [d.dim_value if d.HasField("dim_value") else d.dim_param
            for d in out.type.tensor_type.shape.dim]


def _raw_export_outputs():
    # What rfdetr 1.10.1 writes for Seg-Nano (spike S2): dets / labels static,
    # masks with four symbolic dims.
    return [_output("dets", [1, 100, 4]), _output("labels", [1, 100, 3]),
            _output("masks", ["Addmasks_dim_0", "Addmasks_dim_1", "Addmasks_dim_2", "Addmasks_dim_3"])]


def test_apply_output_dims_stamps_the_symbolic_mask_dims_only():
    outs = _raw_export_outputs()
    observed = {"dets": [1, 100, 4], "labels": [1, 100, 3], "masks": (1, 100, 78, 78)}
    assert ts.apply_output_dims(outs, observed) == ["masks"]
    assert _declared(outs[2]) == [1, 100, 78, 78]
    assert all(not d.HasField("dim_param") for d in outs[2].type.tensor_type.shape.dim)
    assert _declared(outs[0]) == [1, 100, 4] and _declared(outs[1]) == [1, 100, 3]
    # Idempotent: a second pass changes nothing.
    assert ts.apply_output_dims(outs, observed) == []


def test_apply_output_dims_already_static_graph_is_untouched():
    outs = [_output("dets", [1, 100, 4]), _output("labels", [1, 100, 3]),
            _output("masks", [1, 100, 78, 78])]
    assert ts.apply_output_dims(outs, {"dets": [1, 100, 4], "labels": [1, 100, 3],
                                       "masks": [1, 100, 78, 78]}) == []
    assert [_declared(o) for o in outs] == [[1, 100, 4], [1, 100, 3], [1, 100, 78, 78]]


def test_apply_output_dims_partially_symbolic_and_unset_dims():
    out = _output("masks", ["Addmasks_dim_0", 100, "Addmasks_dim_2", "Addmasks_dim_3"])
    out.type.tensor_type.shape.dim.append(_Dim())   # neither field set
    out.type.tensor_type.shape.dim.pop(3)
    assert ts.apply_output_dims([out], {"masks": [1, 100, 78, 78]}) == ["masks"]
    assert _declared(out) == [1, 100, 78, 78]


@pytest.mark.parametrize("observed,msg", [
    ({"dets": [1, 100, 4], "labels": [1, 100, 3]}, "no observed shape for ONNX output 'masks'"),
    ({"dets": [1, 100, 4], "labels": [1, 100, 3], "masks": [1, 100, 78]}, "declares rank 4 but the runtime produced rank 3"),
    ({"dets": [1, 100, 4], "labels": [1, 100, 5], "masks": [1, 100, 78, 78]}, "declares a static dim 3 where the runtime produced 5"),
])
def test_apply_output_dims_rejects(observed, msg):
    outs = _raw_export_outputs()
    with pytest.raises(ValueError) as exc:
        ts.apply_output_dims(outs, observed)
    assert msg in str(exc.value)


def test_stamp_static_output_dims_runs_the_graph_and_saves_only_when_changed(monkeypatch, tmp_path):
    """The onnx / onnxruntime / numpy glue with the three libraries stubbed."""
    path = tmp_path / "model.onnx"
    path.write_bytes(b"onnx")
    outs = _raw_export_outputs()
    model = SimpleNamespace(graph=SimpleNamespace(output=outs), ir_version=8)
    log = []

    class _Arr:
        def __init__(self, shape):
            self.shape = tuple(shape)

    class _Session:
        def __init__(self, p, providers):
            log.append(("session", p, tuple(providers)))

        def get_inputs(self):
            return [SimpleNamespace(name="input", shape=[1, 3, 312, 312], type="tensor(float)")]

        def get_outputs(self):
            return [SimpleNamespace(name=n) for n in ("dets", "labels", "masks")]

        def run(self, names, feeds):
            log.append(("run", names, {k: (v.shape, v.dtype) for k, v in feeds.items()}))
            return [_Arr([1, 100, 4]), _Arr([1, 100, 3]), _Arr([1, 100, 78, 78])]

    numpy = types.ModuleType("numpy")
    numpy.zeros = lambda shape, dtype: SimpleNamespace(shape=tuple(shape), dtype=dtype)
    onnx = types.ModuleType("onnx")
    onnx.load = lambda p: (log.append(("load", p)), model)[1]
    onnx.checker = SimpleNamespace(check_model=lambda m: log.append(("check", m.ir_version)))
    onnx.save = lambda m, p: log.append(("save", p))
    ort = types.ModuleType("onnxruntime")
    ort.InferenceSession = _Session
    monkeypatch.setitem(sys.modules, "numpy", numpy)
    monkeypatch.setitem(sys.modules, "onnx", onnx)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)

    observed = ts.stamp_static_output_dims(path)
    assert observed == {"dets": [1, 100, 4], "labels": [1, 100, 3], "masks": [1, 100, 78, 78]}
    assert _declared(outs[2]) == [1, 100, 78, 78]
    assert log == [
        ("session", str(path), ("CPUExecutionProvider",)),
        ("run", None, {"input": ((1, 3, 312, 312), "float32")}),
        ("load", str(path)),
        ("check", 8),
        ("save", str(path)),
    ]
    # Second call: already static -> no checker run, no save.
    del log[:]
    ts.stamp_static_output_dims(path)
    assert [e[0] for e in log] == ["session", "run", "load"]


# ---------------------------------------------------------------------------
# Metadata (Req 4.6)
# ---------------------------------------------------------------------------

def test_build_seg_metadata_keys():
    meta = ts.build_seg_metadata(_cfg(), ["scratch", "dent"], SEG_METRICS, [1, 3, 384, 384],
                                 [[1, Q, 4], [1, Q, 3], [1, Q, GRID, GRID]],
                                 ["dets", "labels", "masks"], Q)
    assert list(meta) == [
        "detection_arch", "model_type", "rfdetr_seg_size", "model_class", "resolution",
        "num_classes", "class_names", "epochs", "batch", "grad_accum", "lr", "patience",
        "opset", "onnx_opset", "onnx_input_shape", "onnx_output_shapes", "onnx_output_names",
        "logits_slots", "background_slot", "mask_downsample_ratio", "top_k", "base_weights",
        "metrics", "manifest_s3", "images_s3", "device_manifest_hints"]
    assert meta["detection_arch"] == "rf_detr_seg" and meta["model_type"] == "instance_segmentation"
    assert meta["rfdetr_seg_size"] == "small" and meta["model_class"] == "RFDETRSegSmall"
    assert meta["resolution"] == 384
    assert meta["num_classes"] == 2 and meta["class_names"] == ["scratch", "dent"]
    assert (meta["epochs"], meta["batch"], meta["grad_accum"], meta["lr"], meta["patience"],
            meta["opset"], meta["onnx_opset"]) == (100, 4, 4, 1e-4, 10, 17, 17)
    assert meta["onnx_input_shape"] == [1, 3, 384, 384]
    assert meta["onnx_output_shapes"] == [[1, Q, 4], [1, Q, 3], [1, Q, GRID, GRID]]
    assert meta["onnx_output_names"] == ["dets", "labels", "masks"]
    assert meta["logits_slots"] == 3 and meta["background_slot"] == 2
    assert meta["mask_downsample_ratio"] == 4 and meta["top_k"] == Q
    assert meta["base_weights"] == "RFDETRSegSmall" and "base_weights_member" not in meta
    assert meta["metrics"] == SEG_METRICS and set(meta["metrics"]) == set(ts.METRIC_KEYS)
    assert meta["manifest_s3"] == "s3://b/m.manifest" and meta["images_s3"] is None
    assert meta["device_manifest_hints"] == {
        "layout": "rf_detr", "stage_type": "rf_detr_instance_segmentation",
        "output_mode": "instance", "network_input": 384, "preserve_aspect": False,
        "normalize": True, "score_threshold": 0.5, "mask_threshold": 0.5, "top_k": Q}
    json.dumps(meta)  # serialisable
    # The detection keys are a subset with the same values where they overlap.
    det_meta = det.build_metadata(_cfg(size="small", model_class="RFDETRSmall", resolution=384),
                                  ["scratch", "dent"], {}, [1, 3, 384, 384], [1, Q, 4], [1, Q, 3], Q)
    for key in det_meta:
        if key in ("detection_arch", "rfdetr_size", "model_class", "base_weights", "metrics",
                   "onnx_output_shapes", "device_manifest_hints"):
            continue
        assert meta[key] == det_meta[key], key


def test_build_seg_metadata_with_base_weights_and_graph_order():
    cfg = _cfg(base_weights_s3="s3://b/prior/model.tar.gz", base_weights_member=None)
    meta = ts.build_seg_metadata(cfg, ["a"], {}, [1, 3, 384, 384],
                                 [[1, Q, 4], [1, Q, 2], [1, Q, GRID, GRID]],
                                 ["masks", "labels", "dets"], Q,
                                 base="/w/base_weights/checkpoint_best_total.pth")
    assert meta["base_weights"] == "s3://b/prior/model.tar.gz"
    assert meta["base_weights_member"] == "checkpoint_best_total.pth"
    assert meta["logits_slots"] == 2 and meta["background_slot"] == 1
    # onnx_output_names is the graph order; onnx_output_shapes the role order.
    assert meta["onnx_output_names"] == ["masks", "labels", "dets"]
    assert meta["onnx_output_shapes"] == [[1, Q, 4], [1, Q, 2], [1, Q, GRID, GRID]]
    cfg = _cfg(size="nano", model_class="RFDETRSegNano", resolution=312, top_k=100,
               base_weights_s3="s3://b/prior/model.tar.gz", base_weights_member="last_ema.pth")
    meta = ts.build_seg_metadata(cfg, ["a"], {}, [1, 3, 312, 312],
                                 [[1, Q, 4], [1, Q, 2], [1, Q, 78, 78]], ["dets", "labels", "masks"],
                                 Q, base="/w/base_weights/last_ema.pth")
    assert meta["base_weights_member"] == "last_ema.pth" and meta["rfdetr_seg_size"] == "nano"
    assert meta["device_manifest_hints"]["network_input"] == 312


# ---------------------------------------------------------------------------
# Model construction and training kwargs (Req 4.3)
# ---------------------------------------------------------------------------

class _FakeModel:
    calls = []

    def __init__(self, **kwargs):
        type(self).calls.append(kwargs)


def test_build_model_is_the_detection_helper_with_the_seg_class():
    _FakeModel.calls.clear()
    cfg = _cfg(base_weights_s3="s3://b/prior/model.tar.gz")
    det.build_model(_FakeModel, cfg, "/w/base_weights/checkpoint_best_total.pth", num_classes=2)
    det.build_model(_FakeModel, _cfg(), None, num_classes=2)
    assert _FakeModel.calls == [
        {"pretrain_weights": "/w/base_weights/checkpoint_best_total.pth",
         "trust_checkpoint": True, "num_classes": 2},
        {},
    ]
    # No seg loss knob reaches .train(): TrainConfig is extra="forbid".
    kw = det.train_kwargs(_cfg(), "/w/dataset", ["a", "b"], "/w/out")
    assert set(kw) == {"dataset_dir", "epochs", "batch_size", "grad_accum_steps", "lr",
                       "output_dir", "resolution", "early_stopping", "early_stopping_patience",
                       "run_test", "tensorboard", "wandb", "class_names"}
    assert kw["resolution"] == 384 and kw["class_names"] == ["a", "b"]


# ---------------------------------------------------------------------------
# Sourcedir bundles (Req 4.1)
# ---------------------------------------------------------------------------

def _stage_code_dir(code):
    datasets_dir = os.path.dirname(_DETECTION_TRAINING)
    for src_dir, name in ((_DETECTION_TRAINING, "train_rfdetr_seg.py"),
                          (_DETECTION_TRAINING, "train_rfdetr.py"),
                          (_DETECTION_TRAINING, "requirements-rfdetr.txt"),
                          (_DETECTION_TRAINING, "_common.py"),
                          (datasets_dir, "manifest_to_detector_dataset.py"),
                          (datasets_dir, "dedupe_frames.py")):
        shutil.copyfile(os.path.join(src_dir, name), os.path.join(code, name))


def test_real_sourcedir_tarball_for_rfdetr_seg():
    with tempfile.TemporaryDirectory() as td:
        code = os.path.join(td, "code")
        os.makedirs(code)
        _stage_code_dir(code)
        out = os.path.join(td, "sourcedir.tar.gz")
        names = dt.build_sourcedir_tarball(code, out, "train_rfdetr_seg.py")
        assert set(names) == {"train_rfdetr_seg.py", "train_rfdetr.py", "requirements.txt",
                              "_common.py", "manifest_to_detector_dataset.py", "dedupe_frames.py"}
        with tarfile.open(out, "r:gz") as tar:
            contents = {m.name: tar.extractfile(m).read().decode() for m in tar.getmembers()}
        with open(os.path.join(_DETECTION_TRAINING, "requirements-rfdetr.txt")) as fh:
            assert contents["requirements.txt"] == fh.read()
        with open(os.path.join(_DETECTION_TRAINING, "train_rfdetr_seg.py")) as fh:
            assert contents["train_rfdetr_seg.py"] == fh.read()
        assert "train.py" not in contents
        assert "transformers==5.17.0" in contents["requirements.txt"].splitlines()


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("tar") is None,
                    reason="bash and tar are needed to run build_sourcedir.sh")
def test_build_sourcedir_sh_rf_detr_seg_matches_the_portal_bundle():
    script = os.path.join(_DETECTION_TRAINING, "build_sourcedir.sh")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "seg.tar.gz")
        r = subprocess.run(["bash", script, "--arch", "rf_detr_seg", out],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        with tarfile.open(out, "r:gz") as tar:
            members = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
        assert set(members) == {"train_rfdetr_seg.py", "train_rfdetr.py", "requirements.txt",
                                "_common.py", "manifest_to_detector_dataset.py", "dedupe_frames.py"}
        with open(os.path.join(_DETECTION_TRAINING, "requirements-rfdetr.txt"), "rb") as fh:
            assert members["requirements.txt"] == fh.read()
        # The detection bundles are unchanged by the new arch.
        for arch, entry in (("yolo", "train.py"), ("rf_detr", "train_rfdetr.py")):
            out2 = os.path.join(td, f"{arch}.tar.gz")
            r = subprocess.run(["bash", script, "--arch", arch, out2], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            with tarfile.open(out2, "r:gz") as tar:
                names = {m.name for m in tar.getmembers()}
            assert names == {entry, "requirements.txt", "_common.py",
                             "manifest_to_detector_dataset.py", "dedupe_frames.py"}
        r = subprocess.run(["bash", script, "--arch", "rf_detr_segx", os.path.join(td, "x.tar.gz")],
                           capture_output=True, text=True)
        assert r.returncode != 0 and "rf_detr_seg" in r.stderr


def test_shared_layer_names_the_seg_entry_point():
    assert dt.ENTRY_POINT_FOR_ARCH["rf_detr_seg"] == "train_rfdetr_seg.py"
    assert dt.REQUIREMENTS_FOR_ARCH["rf_detr_seg"] == "requirements-rfdetr.txt"
    assert dt.SOURCEDIR_SIBLINGS_FOR_ARCH["rf_detr_seg"] == ("train_rfdetr.py",)
    assert os.path.isfile(os.path.join(_DETECTION_TRAINING, dt.ENTRY_POINT_FOR_ARCH["rf_detr_seg"]))
