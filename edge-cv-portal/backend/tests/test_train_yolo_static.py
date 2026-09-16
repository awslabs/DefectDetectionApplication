"""
Static / host-side tests for `datasets/detection_training/train.py`, the YOLO
SageMaker entry point (rfdetr-training-and-transfer-learning Requirement 7.4;
sibling of test_train_rfdetr_static.py).

No GPU, torch or ultralytics on this host: the module is imported with
`ultralytics` / `torch` / `onnxruntime` BLOCKED (a meta-path finder that
raises ImportError) to prove every heavy import is lazy, then `export()` is
driven against a STUB `ultralytics` package installed in sys.modules -- a
`YOLO` model whose `.export()` records its call and returns a tiny stand-in
file, a `settings` object recording `update()` calls, and `utils` /
`utils.checks` submodules carrying the `AUTOINSTALL` constant the real
`check_requirements` gates on. Covered:

  * `training_metadata.json` carries `num_classes` and `class_names` in the
    converter's data.yaml order, plus every pre-existing key unchanged;
  * ultralytics AutoUpdate is disabled BEFORE `model.export()`:
    `settings.update(autoinstall=False)` is attempted first in call order,
    `YOLO_AUTOINSTALL=False` is in the environment and the `AUTOINSTALL`
    constants are forced False -- including when `settings.update` raises
    the KeyError ultralytics 8.3.40 actually raises (it has no such key), in
    which case the export still proceeds.

# Validates: Requirements 7.4
"""
import importlib.util
import json
import os
import sys
import types

import pytest

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


_HEAVY = ("ultralytics", "torch", "onnx", "onnxruntime")
_BLOCKED = _Blocked(_HEAVY)


def _import_train_py():
    """Import train.py under a private module name with the heavy packages
    blocked (the module must not touch them at import time)."""
    sys.meta_path.insert(0, _BLOCKED)
    saved = {name: sys.modules.pop(name) for name in list(sys.modules)
             if name.split(".")[0] in _HEAVY}
    try:
        spec = importlib.util.spec_from_file_location(
            "dda_detection_train_py_static", os.path.join(_DETECTION_TRAINING, "train.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.meta_path.remove(_BLOCKED)
        sys.modules.update(saved)


# Make sure a stale YOLO_AUTOINSTALL from the environment cannot mask the
# module's own setdefault (asserted in test_module_import_sets_yolo_autoinstall).
os.environ.pop("YOLO_AUTOINSTALL", None)
tr = _import_train_py()


# ---------------------------------------------------------------------------
# Stub ultralytics
# ---------------------------------------------------------------------------

class _FakeYolo:
    """Stands in for `ultralytics.YOLO(...)`: records `.export()` in the shared
    call log and returns the path of a pre-written stand-in file."""

    def __init__(self, log, export_path, names=None):
        self.log = log
        self.export_path = export_path
        self.names = names if names is not None else {0: "blue_plate"}

    def export(self, **kwargs):
        self.log.append(("model.export", dict(kwargs)))
        return str(self.export_path)


class _FakeSettings:
    """Stands in for `ultralytics.settings` (a SettingsManager)."""

    def __init__(self, log, raise_on_update=None):
        self.log = log
        self.raise_on_update = raise_on_update
        self.values = {}

    def update(self, *args, **kwargs):
        self.log.append(("settings.update", dict(kwargs)))
        if self.raise_on_update is not None:
            raise self.raise_on_update
        self.values.update(*args, **kwargs)


def _install_ultralytics_stub(monkeypatch, log, raise_on_update=None):
    """Put a fake `ultralytics` package (+ `utils`, `utils.checks`) into
    sys.modules; returns (ultralytics, utils, checks) so tests can read the
    AUTOINSTALL constants back."""
    ul = types.ModuleType("ultralytics")
    utils = types.ModuleType("ultralytics.utils")
    checks = types.ModuleType("ultralytics.utils.checks")
    # What 8.3.40 computes at import from YOLO_AUTOINSTALL (default True).
    utils.AUTOINSTALL = True
    checks.AUTOINSTALL = True
    utils.checks = checks
    ul.utils = utils
    ul.settings = _FakeSettings(log, raise_on_update)
    ul.YOLO = lambda weights: _FakeYolo(log, weights)
    monkeypatch.setitem(sys.modules, "ultralytics", ul)
    monkeypatch.setitem(sys.modules, "ultralytics.utils", utils)
    monkeypatch.setitem(sys.modules, "ultralytics.utils.checks", checks)
    # `import onnxruntime` inside export() must fail deterministically (the
    # stand-in export file is not a real graph); None in sys.modules raises
    # ImportError, which export() catches and logs.
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    return ul, utils, checks


DATA_YAML = """path: /opt/ml/input/work/dataset
train: images/train
val: images/val
test: images/test
names:
  0: blue_plate
  1: blue_plate_b
  2: scratch
"""


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect MODEL_DIR / WORK into tmp and pin the module constants the
    metadata reads, so export() is hermetic."""
    model_dir = tmp_path / "model"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(tr, "MODEL_DIR", model_dir)
    monkeypatch.setattr(tr, "WORK", work)
    monkeypatch.setattr(tr, "MANIFEST_S3", "s3://b/labels/output.manifest")
    monkeypatch.setattr(tr, "IMAGES_S3", None)
    monkeypatch.setattr(tr, "IMGSZ", 1280)
    monkeypatch.setattr(tr, "EPOCHS", 100)
    monkeypatch.setattr(tr, "OPSET", 17)
    monkeypatch.setattr(tr, "BASE_WEIGHTS", "yolo11s.pt")
    monkeypatch.setattr(tr, "BASE_WEIGHTS_S3", None)
    monkeypatch.setattr(tr, "BASE_WEIGHTS_MEMBER", None)
    exported = tmp_path / "runs" / "best.onnx"
    exported.parent.mkdir(parents=True)
    exported.write_bytes(b"\x08\x07stub-onnx")  # not a real graph; never parsed
    yaml_path = work / "data.yaml"
    yaml_path.write_text(DATA_YAML)
    return types.SimpleNamespace(model_dir=model_dir, work=work, exported=exported,
                                 yaml_path=yaml_path)


def _read_metadata(model_dir):
    return json.loads((model_dir / "training_metadata.json").read_text())


# ---------------------------------------------------------------------------
# Import hygiene
# ---------------------------------------------------------------------------

def test_module_imports_without_ultralytics_torch_onnx():
    # `tr` was imported with ultralytics/torch/onnx/onnxruntime blocked (see
    # _import_train_py); the heavy imports must all be inside functions.
    assert tr.CHECKPOINT_MEMBER == "best.pt"
    for name in _HEAVY:
        assert name not in vars(tr)
    for fn in ("disable_autoupdate", "build_metadata", "export", "_manifest_class_names"):
        assert callable(getattr(tr, fn))


def test_module_import_sets_yolo_autoinstall():
    # ultralytics reads YOLO_AUTOINSTALL once at first import, so the entry
    # point must have it in the environment before any `from ultralytics
    # import ...` (all of which are lazy) can run.
    assert os.environ.get("YOLO_AUTOINSTALL") == "False"


# ---------------------------------------------------------------------------
# training_metadata.json: num_classes / class_names (Req 7.4)
# ---------------------------------------------------------------------------

def test_manifest_class_names_from_data_yaml(sandbox):
    assert tr._manifest_class_names(sandbox.yaml_path) == ["blue_plate", "blue_plate_b", "scratch"]


def test_build_metadata_carries_num_classes_and_class_names(sandbox):
    metrics = {"test_map50": 0.9, "test_map50_95": 0.6, "test_precision": 0.8, "test_recall": 0.7}
    meta = tr.build_metadata(metrics, [1, 7, 33600], None, ["blue_plate", "blue_plate_b", "scratch"])
    assert meta["num_classes"] == 3
    assert meta["class_names"] == ["blue_plate", "blue_plate_b", "scratch"]
    # Pre-existing keys, unchanged (the packager reads imgsz, onnx_output_shape,
    # metrics, device_manifest_hints).
    assert meta["imgsz"] == 1280 and meta["epochs"] == 100 and meta["opset"] == 17
    assert meta["base_weights"] == "yolo11s.pt" and "base_weights_member" not in meta
    assert meta["onnx_output_shape"] == [1, 7, 33600]
    assert meta["metrics"] == metrics
    assert meta["manifest_s3"] == "s3://b/labels/output.manifest" and meta["images_s3"] is None
    assert meta["device_manifest_hints"] == {
        "preserve_aspect": True, "network_input": 1280, "layout": "yolo",
        "iou_threshold": 0.45, "score_threshold": 0.25,
    }
    json.dumps(meta)  # serialisable


def test_build_metadata_with_base_weights_and_no_names(sandbox, monkeypatch):
    monkeypatch.setattr(tr, "BASE_WEIGHTS_S3", "s3://b/prior/model.tar.gz")
    meta = tr.build_metadata({}, None, base="/w/base_weights/best.pt", class_names=None)
    assert meta["base_weights"] == "s3://b/prior/model.tar.gz"
    assert meta["base_weights_member"] == "best.pt"
    # No names known -> an honest empty list / zero, never a fabricated head.
    assert meta["num_classes"] == 0 and meta["class_names"] == []
    assert meta["onnx_output_shape"] is None


def test_export_writes_metadata_with_manifest_classes(sandbox, monkeypatch):
    log = []
    _install_ultralytics_stub(monkeypatch, log)
    model = _FakeYolo(log, sandbox.exported)
    metrics = {"test_map50": 0.99}
    names = tr._manifest_class_names(sandbox.yaml_path)

    tr.export(model, metrics, base=None, class_names=names)

    assert (sandbox.model_dir / "model.onnx").read_bytes() == sandbox.exported.read_bytes()
    meta = _read_metadata(sandbox.model_dir)
    assert meta["num_classes"] == 3
    assert meta["class_names"] == ["blue_plate", "blue_plate_b", "scratch"]
    assert meta["metrics"] == metrics and meta["imgsz"] == 1280
    assert meta["device_manifest_hints"]["preserve_aspect"] is True
    # The export kwargs are the device contract: static, no in-graph NMS.
    exports = [kw for what, kw in log if what == "model.export"]
    assert exports == [{"format": "onnx", "imgsz": 1280, "opset": 17, "dynamic": False,
                        "simplify": True, "nms": False}]


# ---------------------------------------------------------------------------
# AutoUpdate disabled before export (Req 7.4)
# ---------------------------------------------------------------------------

def test_settings_update_autoinstall_false_runs_before_model_export(sandbox, monkeypatch):
    log = []
    ul, utils, checks = _install_ultralytics_stub(monkeypatch, log)
    monkeypatch.delenv("YOLO_AUTOINSTALL", raising=False)

    tr.export(_FakeYolo(log, sandbox.exported), {}, None, ["blue_plate"])

    what = [w for w, _kw in log]
    assert "settings.update" in what and "model.export" in what
    assert what.index("settings.update") < what.index("model.export")
    assert ("settings.update", {"autoinstall": False}) in log
    assert ul.settings.values == {"autoinstall": False}
    # The switches 8.3.40 actually honours are set too.
    assert os.environ["YOLO_AUTOINSTALL"] == "False"
    assert utils.AUTOINSTALL is False and checks.AUTOINSTALL is False


def test_autoupdate_still_disabled_when_settings_has_no_autoinstall_key(sandbox, monkeypatch, capsys):
    # ultralytics 8.3.40: SettingsManager.update raises KeyError for a key
    # not in its defaults, and `autoinstall` is not one. The export must
    # still run, and AutoUpdate must still be off via the constants/env.
    log = []
    _ul, utils, checks = _install_ultralytics_stub(
        monkeypatch, log,
        raise_on_update=KeyError("No Ultralytics setting 'autoinstall'."))

    tr.export(_FakeYolo(log, sandbox.exported), {}, None, ["blue_plate"])

    what = [w for w, _kw in log]
    assert what.index("settings.update") < what.index("model.export")
    assert os.environ["YOLO_AUTOINSTALL"] == "False"
    assert utils.AUTOINSTALL is False and checks.AUTOINSTALL is False
    assert (sandbox.model_dir / "training_metadata.json").is_file()
    out = capsys.readouterr().out
    assert "settings.update(autoinstall=False) not applied" in out
    assert "AutoUpdate disabled" in out


def test_disable_autoupdate_reports_switches(monkeypatch):
    log = []
    _install_ultralytics_stub(monkeypatch, log)
    assert tr.disable_autoupdate() == {"env": True, "constant": True, "settings": True}
    log.clear()
    _install_ultralytics_stub(monkeypatch, log, raise_on_update=KeyError("autoinstall"))
    assert tr.disable_autoupdate() == {"env": True, "constant": True, "settings": False}


def test_disable_autoupdate_never_fatal_without_ultralytics(monkeypatch, capsys):
    # Even with ultralytics missing entirely the helper only logs.
    sys.meta_path.insert(0, _BLOCKED)
    saved = {name: sys.modules.pop(name) for name in list(sys.modules)
             if name.split(".")[0] == "ultralytics"}
    try:
        applied = tr.disable_autoupdate()
    finally:
        sys.meta_path.remove(_BLOCKED)
        sys.modules.update(saved)
    assert applied == {"env": True, "constant": False, "settings": False}
    assert os.environ["YOLO_AUTOINSTALL"] == "False"
    assert "WARN: could not force ultralytics AUTOINSTALL off" in capsys.readouterr().out
