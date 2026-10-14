"""
Static / host-side tests for `datasets/detection_training/train_rfdetr.py`, the
RF-DETR SageMaker entry point (rfdetr-training-and-transfer-learning
Requirements 1.1, 1.4-1.9).

No GPU, torch or rfdetr on this host: the module is imported with those
packages BLOCKED (a stub that raises ImportError) to prove every heavy import
is lazy, and only the pure helpers are exercised -- size -> native resolution,
the resolution rule, the `.train()` kwargs, the TEST METRICS mapping, the
two-output ONNX contract check (logits must be exactly `[1, Q, C + 1]`; a
`C`-slot graph -- an un-widened head -- is FATAL, Req 7.4), the metadata
builder, the `build_model(num_classes=)` pin on the base-weights path, and
the real sourcedir tarball built from the real `datasets/detection_training`
directory.

# Validates: Requirements 1.1, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 3.4, 7.4
"""
import importlib
import json
import os
import re
import sys
import tarfile
import tempfile
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


class _Blocked:
    """Meta-path finder that makes `import <name>` raise ImportError."""

    def __init__(self, names):
        self.names = set(names)

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.names:
            raise ImportError(f"{fullname} is blocked in this test")
        return None


_BLOCKED = _Blocked({"rfdetr", "torch", "onnx", "onnxruntime"})


def _import_train_rfdetr():
    sys.meta_path.insert(0, _BLOCKED)
    try:
        for mod in ("train_rfdetr", "rfdetr", "torch", "onnx", "onnxruntime"):
            sys.modules.pop(mod, None)
        return importlib.import_module("train_rfdetr")
    finally:
        sys.meta_path.remove(_BLOCKED)


tr = _import_train_rfdetr()


def _cfg(**over):
    base = dict(manifest_s3="s3://b/m.manifest", images_s3=None, size="small",
                model_class="RFDETRSmall", resolution=512, epochs=100, batch=4,
                grad_accum=4, lr=1e-4, patience=10, opset=17,
                base_weights_s3=None, base_weights_member=None)
    base.update(over)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Import hygiene / size map (Req 1.1)
# ---------------------------------------------------------------------------

def test_module_imports_without_rfdetr_torch_onnx():
    # `tr` was imported with rfdetr/torch/onnx/onnxruntime blocked (see
    # _import_train_rfdetr); the heavy imports must all be inside functions.
    assert tr.SIZE_CLASSES["small"] == ("RFDETRSmall", 512)
    for name in ("rfdetr", "torch", "onnx", "onnxruntime"):
        assert name not in vars(tr)


def test_size_classes_match_shared_layer():
    assert {k: v[1] for k, v in tr.SIZE_CLASSES.items()} == dt.RFDETR_SIZES
    assert set(tr.SIZE_CLASSES) == {"nano", "small", "medium", "large"}
    for size, (cls, native) in tr.SIZE_CLASSES.items():
        assert cls == "RFDETR" + size.capitalize()
        assert tr.native_resolution(size) == native
        assert native % tr.RESOLUTION_STEP == 0
    assert tr.CHECKPOINT_MEMBER == dt.CHECKPOINT_MEMBER_FOR_ARCH["rf_detr"]
    assert tr.TOP_K == dt.RFDETR_TOP_K
    assert tr.STAGE_TYPE == dt.RFDETR_STAGE_TYPE


def test_read_config_defaults_resolution_to_native(monkeypatch):
    for var in ("RESOLUTION", "EPOCHS", "BATCH", "GRAD_ACCUM", "LR", "PATIENCE",
                "ONNX_OPSET", "BASE_WEIGHTS_S3", "BASE_WEIGHTS_MEMBER", "IMAGES_S3"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MANIFEST_S3", "s3://b/k.manifest")
    for size, (cls, native) in tr.SIZE_CLASSES.items():
        monkeypatch.setenv("RFDETR_SIZE", size)
        cfg = tr.read_config()
        assert cfg.size == size and cfg.model_class == cls
        assert cfg.resolution == native
    # Portal defaults (Req 1.1).
    assert (cfg.epochs, cfg.batch, cfg.grad_accum, cfg.lr, cfg.patience, cfg.opset) == \
        (100, 4, 4, 1e-4, 10, 17)
    monkeypatch.delenv("RFDETR_SIZE")
    assert tr.read_config().size == "small"
    monkeypatch.setenv("RESOLUTION", "640")
    assert tr.read_config().resolution == 640


def test_read_config_rejects_unknown_size(monkeypatch):
    monkeypatch.setenv("RFDETR_SIZE", "xlarge")
    with pytest.raises(SystemExit) as exc:
        tr.read_config()
    assert "FATAL" in str(exc.value) and "xlarge" in str(exc.value)


# ---------------------------------------------------------------------------
# Resolution rule (Req 1.5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("resolution", [224, 512, 1120, "384", 704])
def test_validate_resolution_accepts_multiples_of_32(resolution):
    assert tr.validate_resolution(resolution) == int(resolution)


@pytest.mark.parametrize("resolution", [500, 216, 1152, 0, -32, "abc", None, 560])
def test_validate_resolution_rejects(resolution):
    with pytest.raises(SystemExit) as exc:
        tr.validate_resolution(resolution)
    assert "FATAL: RESOLUTION must be a multiple of 32" in str(exc.value)
    assert str(exc.value).startswith(tr.RESOLUTION_FATAL)


@settings(max_examples=200, deadline=None)
@given(st.integers(min_value=-2000, max_value=3000))
def test_validate_resolution_property(r):
    ok = r > 0 and r % 32 == 0 and 224 <= r <= 1120
    if ok:
        assert tr.validate_resolution(r) == r
    else:
        with pytest.raises(SystemExit):
            tr.validate_resolution(r)


# ---------------------------------------------------------------------------
# .train() kwargs (Req 1.4)
# ---------------------------------------------------------------------------

def test_train_kwargs_pin():
    kw = tr.train_kwargs(_cfg(), "/w/dataset", ["a", "b"], "/w/out")
    assert kw == {
        "dataset_dir": "/w/dataset", "epochs": 100, "batch_size": 4,
        "grad_accum_steps": 4, "lr": 1e-4, "output_dir": "/w/out",
        "resolution": 512, "early_stopping": True, "early_stopping_patience": 10,
        "run_test": True, "tensorboard": False, "wandb": False,
        "class_names": ["a", "b"],
    }


# ---------------------------------------------------------------------------
# TEST METRICS line (Req 1.8)
# ---------------------------------------------------------------------------

def test_metrics_from_eval_maps_rfdetr_keys():
    result = {"test/mAP_50_95": 0.61, "test/mAP_50": 0.93, "test/mAR": 0.7,
              "test/F1": 0.9, "test/precision": 0.88, "test/recall": 0.95}
    m = tr.metrics_from_eval(result, "test")
    assert list(m) == list(tr.METRIC_KEYS)
    assert m == {"test_map50": 0.93, "test_map50_95": 0.61,
                 "test_precision": 0.88, "test_recall": 0.95}
    # The portal's MetricDefinitions regexes lift these from the log line.
    line = "TEST METRICS: " + json.dumps(m)
    for md in dt.DETECTION_METRIC_DEFINITIONS:
        assert re.search(md["Regex"], line), md


def test_metrics_from_eval_val_split_and_missing_keys():
    m = tr.metrics_from_eval({"val/mAP_50": 0.5}, "val")
    assert m["test_map50"] == 0.5
    assert all(m[k] != m[k] for k in ("test_map50_95", "test_precision", "test_recall"))  # NaN


# ---------------------------------------------------------------------------
# Two-output ONNX contract (Req 1.6)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("outputs", [
    [[1, 300, 4], [1, 300, 3]],
    [[1, 300, 3], [1, 300, 4]],
    [("dets", [1, 300, 4]), ("labels", [1, 300, 3])],
    [("labels", [1, 300, 3]), ("dets", [1, 300, 4])],
])
def test_verify_two_outputs_accepts_either_order(outputs):
    # 2 classes -> logits [1, Q, 3] (2 + the trailing background slot).
    assert tr.verify_two_outputs(outputs, 2) == (300, [1, 300, 4], [1, 300, 3])


def test_verify_two_outputs_requires_background_slot():
    # rfdetr 1.10.1 always allocates num_classes + 1 logit slots (trailing
    # background): C + 1 is accepted, exactly C is FATAL (Req 1.6, 7.4).
    assert tr.verify_two_outputs([[1, 300, 4], [1, 300, 3]], 2) == (300, [1, 300, 4], [1, 300, 3])
    assert tr.verify_two_outputs([[1, 300, 4], [1, 300, 2]], 1) == (300, [1, 300, 4], [1, 300, 2])
    # num_classes == 3 -> logits [1, Q, 4]: names disambiguate.
    q, boxes, logits = tr.verify_two_outputs(
        [("labels", [1, 300, 4]), ("dets", [1, 300, 4])], 3)
    assert (q, boxes, logits) == (300, [1, 300, 4], [1, 300, 4])


@pytest.mark.parametrize("outputs,num_classes", [
    # Spike job tl13-rfdetr-nc2-0603: a 1-class base head kept for a 2-class
    # manifest exported labels [1, 300, 2] -- num_classes slots, no background.
    ([("dets", [1, 300, 4]), ("labels", [1, 300, 2])], 2),
    ([[1, 300, 4], [1, 300, 2]], 2),
    ([[1, 300, 3], [1, 300, 4]], 3),
    ([("labels", [1, 300, 1]), ("dets", [1, 300, 4])], 1),
])
def test_verify_two_outputs_rejects_num_classes_slots(outputs, num_classes):
    with pytest.raises(ValueError) as exc:
        tr.verify_two_outputs(outputs, num_classes)
    text = str(exc.value)
    assert text.startswith("FATAL:")
    assert "background slot" in text and f"num_classes={num_classes}" in text
    # Quotes both the observed and the expected logits shape.
    assert f"[1, 300, {num_classes}]" in text
    assert f"[1, 300, {num_classes + 1}]" in text


@pytest.mark.parametrize("outputs,num_classes,msg", [
    ([[1, 300, 4]], 3, "exactly 2"),
    ([[1, 300, 4], [1, 300, 4], [1, 300, 1]], 3, "exactly 2"),
    ([[1, 300, 4], [1, 200, 4]], 3, "disagree on Q"),
    ([[1, 300, 4], [1, 300, 7]], 3, "do not match"),
    ([[1, 300, 5], [1, 300, 4]], 3, "do not match"),
    ([[1, 300, 4], [1, 300, 5]], 2, "do not match"),
    ([[2, 300, 4], [2, 300, 4]], 3, "batch 1"),
    ([[300, 4], [300, 4]], 3, "rank-3"),
    ([[1, "N", 4], [1, "N", 4]], 3, "rank-3"),
])
def test_verify_two_outputs_rejects(outputs, num_classes, msg):
    with pytest.raises(ValueError) as exc:
        tr.verify_two_outputs(outputs, num_classes)
    text = str(exc.value)
    assert text.startswith("FATAL:") and msg in text
    # The message quotes the offending shapes.
    assert str(list(outputs[0]) if not isinstance(outputs[0], tuple) else list(outputs[0][1])) in text


@settings(max_examples=100, deadline=None)
@given(q=st.integers(min_value=1, max_value=1000), c=st.integers(min_value=1, max_value=200),
       swap=st.booleans())
def test_verify_two_outputs_property_accepts_c_plus_one(q, c, swap):
    logits = [1, q, c + 1]
    outs = [("dets", [1, q, 4]), ("labels", logits)]
    if swap:
        outs.reverse()
    assert tr.verify_two_outputs(outs, c) == (q, [1, q, 4], logits)


@settings(max_examples=100, deadline=None)
@given(q=st.integers(min_value=1, max_value=1000), c=st.integers(min_value=1, max_value=200),
       swap=st.booleans())
def test_verify_two_outputs_property_rejects_c(q, c, swap):
    # Exactly num_classes slots is never accepted, whatever the order.
    outs = [("dets", [1, q, 4]), ("labels", [1, q, c])]
    if swap:
        outs.reverse()
    with pytest.raises(ValueError) as exc:
        tr.verify_two_outputs(outs, c)
    text = str(exc.value)
    assert text.startswith("FATAL:") and "background slot" in text
    assert f"[1, {q}, {c}]" in text and f"[1, {q}, {c + 1}]" in text


def test_verify_input():
    assert tr.verify_input([("input", [1, 3, 512, 512])], 512) == [1, 3, 512, 512]
    for bad in ([("input", [1, 3, 640, 640])], [("input", ["N", 3, 512, 512])],
                [("a", [1, 3, 512, 512]), ("b", [1, 3, 512, 512])]):
        with pytest.raises(ValueError) as exc:
            tr.verify_input(bad, 512)
        assert str(exc.value).startswith("FATAL:")


# ---------------------------------------------------------------------------
# Metadata (Req 1.7, 1.9)
# ---------------------------------------------------------------------------

def test_build_metadata_rf_detr():
    metrics = {"test_map50": 0.9, "test_map50_95": 0.6, "test_precision": 0.8, "test_recall": 0.7}
    meta = tr.build_metadata(_cfg(), ["a", "b"], metrics, [1, 3, 512, 512],
                             [1, 300, 4], [1, 300, 3], 300)
    assert meta["detection_arch"] == "rf_detr"
    assert meta["rfdetr_size"] == "small" and meta["resolution"] == 512
    assert meta["num_classes"] == 2 and meta["class_names"] == ["a", "b"]
    assert (meta["epochs"], meta["batch"], meta["grad_accum"], meta["lr"],
            meta["patience"], meta["opset"], meta["onnx_opset"]) == (100, 4, 4, 1e-4, 10, 17, 17)
    assert meta["onnx_input_shape"] == [1, 3, 512, 512]
    assert meta["onnx_output_shapes"] == [[1, 300, 4], [1, 300, 3]]
    assert meta["logits_slots"] == 3 and meta["background_slot"] == 2
    assert meta["top_k"] == 300
    assert meta["base_weights"] == "RFDETRSmall" and "base_weights_member" not in meta
    assert meta["metrics"] == metrics
    hints = meta["device_manifest_hints"]
    assert hints["normalize"] is True and hints["preserve_aspect"] is False
    assert hints["top_k"] == 300 and hints["stage_type"] == "rf_detr_object_detection"
    assert hints["layout"] == "rf_detr" and hints["network_input"] == 512
    assert hints["score_threshold"] == 0.5
    json.dumps(meta)  # serialisable


def test_build_metadata_with_base_weights():
    cfg = _cfg(base_weights_s3="s3://b/prior/model.tar.gz", base_weights_member=None)
    meta = tr.build_metadata(cfg, ["a"], {}, [1, 3, 512, 512], [1, 300, 4], [1, 300, 2],
                             300, base="/w/base_weights/checkpoint_best_total.pth")
    assert meta["base_weights"] == "s3://b/prior/model.tar.gz"
    assert meta["base_weights_member"] == "checkpoint_best_total.pth"
    # verify_two_outputs has enforced C + 1 before this runs, so the
    # background slot is always the last logits index (never None).
    assert meta["background_slot"] == 1 and meta["logits_slots"] == 2


def test_checkpoint_class_helpers_pure():
    class _T:
        shape = (4, 256)

    ckpt = {"model": {"class_embed.weight": _T()}, "args": {"class_names": ["x", "y", "z"]}}
    assert tr.checkpoint_class_names(ckpt) == ["x", "y", "z"]
    assert tr.checkpoint_num_classes(ckpt) == 3
    assert tr.checkpoint_class_names({"model": {}}) is None
    assert tr.checkpoint_num_classes({"model_config": {"num_classes": 5}}) == 5
    assert tr.checkpoint_num_classes("not a dict") is None


class _FakeModel:
    """Records the constructor kwargs build_model passes (no rfdetr needed)."""
    calls = []

    def __init__(self, **kwargs):
        type(self).calls.append(kwargs)


def test_build_model_pins_num_classes_on_base_weights_path():
    # Spike 1.3 (c-rfdetr) finding: without an explicit num_classes, rfdetr
    # 1.10.1 keeps the base checkpoint's head width and refuses to widen it to
    # the dataset's class count, so the manifest's count is pinned here.
    _FakeModel.calls.clear()
    cfg = _cfg(base_weights_s3="s3://b/prior/model.tar.gz")
    tr.build_model(_FakeModel, cfg, "/w/base_weights/checkpoint_best_total.pth", num_classes=2)
    assert _FakeModel.calls == [{
        "pretrain_weights": "/w/base_weights/checkpoint_best_total.pth",
        "trust_checkpoint": True,
        "num_classes": 2,
    }]


def test_build_model_published_weights_passes_no_kwargs():
    # Published COCO weights: default num_classes is not user-set, so .train()
    # aligns it from the dataset (verified in spike run (b1)); nothing pinned.
    _FakeModel.calls.clear()
    tr.build_model(_FakeModel, _cfg(), None, num_classes=2)
    assert _FakeModel.calls == [{}]


# ---------------------------------------------------------------------------
# Splits (Req 1.3)
# ---------------------------------------------------------------------------

def _write_split(root, split, n_images, categories=None):
    d = os.path.join(root, split)
    os.makedirs(d, exist_ok=True)
    data = {"images": [{"id": i, "file_name": f"f{i}.jpg"} for i in range(n_images)],
            "annotations": [],
            "categories": categories or [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]}
    with open(os.path.join(d, "_annotations.coco.json"), "w") as fh:
        json.dump(data, fh)


def test_assert_splits_and_class_names():
    with tempfile.TemporaryDirectory() as td:
        _write_split(td, "train", 3, [{"id": 2, "name": "b"}, {"id": 1, "name": "a"}])
        _write_split(td, "valid", 1)
        tr.assert_splits(td, ("train", "valid"))
        assert tr.read_class_names(td) == ["a", "b"]  # sorted by id
        with pytest.raises(SystemExit) as exc:
            tr.assert_splits(td, ("train", "valid", "test"))
        assert "FATAL" in str(exc.value) and "'test'" in str(exc.value) \
            and "_annotations.coco.json" in str(exc.value)
        _write_split(td, "test", 0)
        with pytest.raises(SystemExit) as exc:
            tr.assert_splits(td, ("test",))
        assert "empty" in str(exc.value) and "'test'" in str(exc.value)


# ---------------------------------------------------------------------------
# Real sourcedir tarball (Req 3.4)
# ---------------------------------------------------------------------------

def test_real_sourcedir_tarball_for_rfdetr():
    # Stage the code dir the way build_sourcedir.sh / the TrainingHandler
    # bundle do: the entry points + _common.py come from detection_training/,
    # the two converter files from datasets/ (they are not siblings in git).
    datasets_dir = os.path.dirname(_DETECTION_TRAINING)
    with tempfile.TemporaryDirectory() as td:
        code = os.path.join(td, "code")
        os.makedirs(code)
        for src_dir, name in ((_DETECTION_TRAINING, "train_rfdetr.py"),
                              (_DETECTION_TRAINING, "requirements-rfdetr.txt"),
                              (_DETECTION_TRAINING, "_common.py"),
                              (datasets_dir, "manifest_to_detector_dataset.py"),
                              (datasets_dir, "dedupe_frames.py")):
            with open(os.path.join(src_dir, name), "rb") as fh, \
                    open(os.path.join(code, name), "wb") as out_fh:
                out_fh.write(fh.read())
        out = os.path.join(td, "sourcedir.tar.gz")
        names = dt.build_sourcedir_tarball(code, out, "train_rfdetr.py")
        assert set(names) == {"train_rfdetr.py", "requirements.txt", "_common.py",
                              "manifest_to_detector_dataset.py", "dedupe_frames.py"}
        with tarfile.open(out, "r:gz") as tar:
            contents = {m.name: tar.extractfile(m).read().decode() for m in tar.getmembers()}
        with open(os.path.join(_DETECTION_TRAINING, "requirements-rfdetr.txt")) as fh:
            assert contents["requirements.txt"] == fh.read()
        with open(os.path.join(_DETECTION_TRAINING, "train_rfdetr.py")) as fh:
            assert contents["train_rfdetr.py"] == fh.read()
        assert "train.py" not in contents


def test_requirements_rfdetr_pins():
    with open(os.path.join(_DETECTION_TRAINING, "requirements-rfdetr.txt")) as fh:
        lines = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    assert any(re.match(r"^rfdetr\[[a-z,]+\]==\d+\.\d+\.\d+$", ln) for ln in lines), lines
    rfdetr_line = next(ln for ln in lines if ln.startswith("rfdetr["))
    extras = set(rfdetr_line[len("rfdetr["):rfdetr_line.index("]")].split(","))
    assert {"train", "onnx"} <= extras
    assert any(re.match(r"^onnxruntime==", ln) for ln in lines)
    assert "numpy<2" in lines
