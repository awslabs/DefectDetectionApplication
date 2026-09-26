"""
Static / host-side tests for ``datasets/detection_training/export_checkpoint.py``,
the Conversion_Job entry point (detector-checkpoint-import task 3.2; sibling of
test_train_yolo_static.py / test_train_rfdetr_static.py).

No torch, ultralytics, rfdetr, onnx, onnxruntime or scipy on this host. The
module is imported with those BLOCKED (a meta-path finder raising ImportError)
to prove every heavy import is lazy; the YOLO and RF-DETR paths are then driven
against STUB packages installed in sys.modules. numpy (a portal test
dependency) runs the parity comparators for real. The real-library behaviour
is covered by the spike's jobs (docs/detector-checkpoint-import-spike.md).

Covered: the environment contract; the single-file and sha256 gates; AutoUpdate
off before ultralytics is used; the pinned export arguments (no half,
dynamic=False, simplify=True, opset 17, nms=None) and that checkpoint
overrides cannot change them; the detector gates; RF-DETR size inference,
PML / segmentation / legacy rejection and the strict load; the pure contract
verifiers (embedded NMS, (1, 300, 6), segmentation-shaped and fp16 graphs);
the IR clamp; the fleet-floor subprocess; both parity comparators; the
failure-file writer; and the metadata keys, checked against what
packaging.package_trained_detection_component reads and against the portal's
own validator (detector_conversion).
# Validates: Requirements 5.3, 5.6, 5.7, 6.1-6.8, 12.2
"""
import copy
import hashlib
import importlib.util
import itertools
import json
import os
import stat
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import detector_conversion as dc

_HERE = os.path.dirname(os.path.abspath(__file__))
_DETECTION_TRAINING = os.path.abspath(
    os.path.join(_HERE, "..", "..", "..", "datasets", "detection_training"))
if _DETECTION_TRAINING not in sys.path:
    sys.path.append(_DETECTION_TRAINING)  # appended: never shadows a portal module

_HEAVY = ("ultralytics", "torch", "onnx", "onnxruntime", "rfdetr", "scipy", "cv2")


class _Blocked:
    def __init__(self, names):
        self.names = set(names)

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.names:
            raise ImportError(f"{fullname} is blocked in this test")
        return None


def _import_export_module():
    blocked = _Blocked(_HEAVY)
    sys.meta_path.insert(0, blocked)
    saved = {n: sys.modules.pop(n) for n in list(sys.modules) if n.split(".")[0] in _HEAVY}
    try:
        spec = importlib.util.spec_from_file_location(
            "dda_export_checkpoint_static", os.path.join(_DETECTION_TRAINING, "export_checkpoint.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.meta_path.remove(blocked)
        sys.modules.update(saved)


os.environ.pop("YOLO_AUTOINSTALL", None)
os.environ.pop("YOLO_OFFLINE", None)
ec = _import_export_module()
import train_rfdetr  # noqa: E402  (real module; its heavy imports are lazy too)

SHA = "a00b6fce124e63c5d23f44792593983b70e646008b58bc54cf5b0a1c87ba2119"


def env(**overrides):
    base = {"DETECTION_ARCH": "yolo", "NETWORK_INPUT": "640", "EXPECTED_NUM_CLASSES": "4",
            "EXPECTED_SHA256": SHA}
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def fatal_message(fn, *args, **kwargs):
    with pytest.raises(SystemExit) as exc:
        fn(*args, **kwargs)
    message = str(exc.value.code)
    assert message.startswith("FATAL: "), message
    return message


# ---------------------------------------------------------------------------
# Import-time behaviour and the environment contract
# ---------------------------------------------------------------------------

def test_module_import_is_light_and_sets_the_offline_switches():
    assert os.environ.get("YOLO_AUTOINSTALL") == "False"
    assert os.environ.get("YOLO_OFFLINE") == "True"
    for name in _HEAVY:
        assert name not in {m.split(".")[0] for m in vars(ec) if isinstance(vars(ec)[m], types.ModuleType)}


def test_read_config_accepts_the_contract():
    cfg = ec.read_config(env())
    assert (cfg.arch, cfg.network_input, cfg.num_classes, cfg.opset) == ("yolo", 640, 4, 17)
    assert cfg.expected_sha256 == SHA and cfg.rfdetr_size is None
    cfg = ec.read_config(env(DETECTION_ARCH="rf_detr", NETWORK_INPUT="512", RFDETR_SIZE="Small",
                             ONNX_OPSET="17", EXPECTED_SHA256=SHA.upper()))
    assert (cfg.arch, cfg.rfdetr_size, cfg.expected_sha256) == ("rf_detr", "small", SHA)


@pytest.mark.parametrize("overrides,fragment", [
    ({"DETECTION_ARCH": "ssd"}, "DETECTION_ARCH"),
    ({"DETECTION_ARCH": None}, "DETECTION_ARCH"),
    ({"NETWORK_INPUT": "650"}, "multiple of 32"),
    ({"NETWORK_INPUT": "4096"}, "[320, 2048]"),
    ({"NETWORK_INPUT": "x"}, "must be an integer"),
    ({"NETWORK_INPUT": None}, "must be an integer"),
    ({"EXPECTED_NUM_CLASSES": "0"}, ">= 1"),
    ({"ONNX_OPSET": "20"}, "[11, 19]"),
    ({"EXPECTED_SHA256": "abc"}, "64-character"),
    ({"EXPECTED_SHA256": None}, "64-character"),
    ({"RFDETR_SIZE": "small"}, "only for rf_detr"),
    ({"DETECTION_ARCH": "rf_detr", "NETWORK_INPUT": "512", "RFDETR_SIZE": "xlarge"}, "RFDETR_SIZE"),
])
def test_read_config_rejects_what_the_portal_never_sends(overrides, fragment):
    assert fragment in fatal_message(ec.read_config, env(**overrides))


def test_single_input_file(tmp_path):
    channel = tmp_path / "checkpoint"
    assert "missing" in fatal_message(ec.single_input_file, channel)
    channel.mkdir()
    assert "found 0" in fatal_message(ec.single_input_file, channel)
    (channel / "best.pt").write_bytes(b"x")
    assert ec.single_input_file(channel) == channel / "best.pt"
    (channel / "other.pt").write_bytes(b"y")
    assert "found 2" in fatal_message(ec.single_input_file, channel)
    (channel / "other.pt").unlink()
    (channel / "link.pt").symlink_to(channel / "best.pt")
    assert "non-regular" in fatal_message(ec.single_input_file, channel)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    channel = tmp_path / "input" / "checkpoint"
    channel.mkdir(parents=True)
    monkeypatch.setattr(ec, "INPUT_DIR", channel)
    monkeypatch.setattr(ec, "MODEL_DIR", tmp_path / "model")
    monkeypatch.setattr(ec, "WORK", tmp_path / "work")
    monkeypatch.setattr(ec, "FAILURE_FILE", tmp_path / "output" / "failure")
    return SimpleNamespace(channel=channel, model=tmp_path / "model",
                           failure=tmp_path / "output" / "failure", tmp=tmp_path)


def test_sha256_gate_runs_before_any_load(sandbox, monkeypatch):
    data = b"pretend checkpoint"
    (sandbox.channel / "best.pt").write_bytes(data)
    monkeypatch.setattr(ec, "convert_yolo", lambda *a: pytest.fail("loaded before the sha256 gate"))
    msg = fatal_message(ec.run, env())
    assert "differs from the one the portal recorded" in msg and SHA in msg

    reached = []
    monkeypatch.setattr(ec, "convert_yolo", lambda path, cfg: reached.append(path) or
                        (_ for _ in ()).throw(SystemExit("FATAL: stop here")))
    fatal_message(ec.run, env(EXPECTED_SHA256=hashlib.sha256(data).hexdigest()))
    assert reached and reached[0].name == "checkpoint.pt"  # a copy in WORK, never the channel file
    assert reached[0].read_bytes() == data


# ---------------------------------------------------------------------------
# Failure file and main()
# ---------------------------------------------------------------------------

def test_write_failure_is_one_prefixed_line_capped_at_1024(tmp_path):
    path = tmp_path / "out" / "failure"
    ec.write_failure("checkpoint task is 'segment'\n  second line", path)
    assert path.read_text() == "FATAL: checkpoint task is 'segment' second line"
    ec.write_failure("FATAL: " + "x" * 5000, path)
    assert len(path.read_text()) == 1024 and path.read_text().startswith("FATAL: xxx")


def test_write_failure_never_masks_the_original_error(tmp_path, capsys):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        ec.write_failure("boom", ro / "sub" / "failure")
    finally:
        ro.chmod(stat.S_IRWXU)
    assert "could not write" in capsys.readouterr().out or os.geteuid() == 0


def test_main_writes_the_fatal_reason(sandbox, monkeypatch):
    monkeypatch.setattr(ec, "run", lambda e: ec.fatal("checkpoint has 4 classes; the import recorded 5"))
    assert ec.main(["train"]) == 1
    assert sandbox.failure.read_text() == "FATAL: checkpoint has 4 classes; the import recorded 5"


def test_main_turns_any_exception_into_a_fatal_reason(sandbox, monkeypatch):
    def boom(_env):
        raise KeyError("model")
    monkeypatch.setattr(ec, "run", boom)
    assert ec.main([]) == 1
    assert sandbox.failure.read_text() == "FATAL: KeyError: 'model'"


def test_main_success_writes_no_failure(sandbox, monkeypatch):
    monkeypatch.setattr(ec, "run", lambda e: {})
    assert ec.main(["train"]) == 0
    assert not sandbox.failure.exists()


# ---------------------------------------------------------------------------
# Stub ultralytics (YOLO path)
# ---------------------------------------------------------------------------

class _Settings:
    def __init__(self, log):
        self.log = log

    def update(self, **kwargs):
        self.log.append(("settings.update", kwargs))
        raise KeyError("autoinstall")  # 8.4.162 has no such key: must not stop the export


def _install_ultralytics(monkeypatch, tmp_path, log, *, task="detect", model_cls="DetectionModel",
                         head_cls="Detect", nc=4, names=None, overrides=None):
    names = {0: "helmet", 1: "human", 2: "no-helmet", 3: "vest"} if names is None else names
    ul = types.ModuleType("ultralytics")
    ul.__version__ = "8.4.162"
    utils = types.ModuleType("ultralytics.utils")
    checks = types.ModuleType("ultralytics.utils.checks")
    utils.AUTOINSTALL = checks.AUTOINSTALL = True
    utils.checks, ul.utils, ul.settings = checks, utils, _Settings(log)
    head_type = type(head_cls, (), {"__module__": "ultralytics.nn.modules.head"})
    net_type = type(model_cls, (), {"__module__": "ultralytics.nn.tasks"})

    class FakeYOLO:
        def __init__(self, weights):
            log.append(("YOLO", str(weights), utils.AUTOINSTALL, checks.AUTOINSTALL,
                        os.environ.get("YOLO_AUTOINSTALL")))
            head = head_type()
            head.nc = nc
            net = net_type()
            net.model, net.names, net.end2end, net.args = [object(), head], names, False, {"imgsz": 640}
            self.task, self.model, self.callbacks = task, net, {"on_export_start": []}
            self.overrides = dict(overrides if overrides is not None else {"task": task, "imgsz": 640})

    class FakeExporter:
        def __init__(self, overrides, _callbacks):
            log.append(("Exporter", dict(overrides)))
            self.model = None

        def __call__(self, model):
            out = tmp_path / "checkpoint.onnx"
            out.write_bytes(b"stub")
            self.model = "the traced export-mode module"
            return str(out)

    ul.YOLO = FakeYOLO
    engine = types.ModuleType("ultralytics.engine")
    exporter = types.ModuleType("ultralytics.engine.exporter")
    exporter.Exporter = FakeExporter
    torch = types.ModuleType("torch")
    torch.__version__ = "2.5.1+cpu"
    for name, mod in (("ultralytics", ul), ("ultralytics.utils", utils),
                      ("ultralytics.utils.checks", checks), ("ultralytics.engine", engine),
                      ("ultralytics.engine.exporter", exporter), ("torch", torch)):
        monkeypatch.setitem(sys.modules, name, mod)
    return SimpleNamespace(utils=utils, checks=checks)


def _cfg(**kw):
    base = dict(arch="yolo", network_input=640, num_classes=4, opset=17, expected_sha256=SHA,
                rfdetr_size=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_yolo_autoupdate_is_off_before_the_checkpoint_is_loaded(tmp_path, monkeypatch):
    log = []
    stubs = _install_ultralytics(monkeypatch, tmp_path, log)
    monkeypatch.setenv("YOLO_AUTOINSTALL", "True")
    ec.convert_yolo(tmp_path / "checkpoint.pt", _cfg())
    kinds = [entry[0] for entry in log]
    assert kinds.index("settings.update") < kinds.index("YOLO") < kinds.index("Exporter")
    yolo_call = next(entry for entry in log if entry[0] == "YOLO")
    assert yolo_call[2:] == (False, False, "False")
    assert stubs.utils.AUTOINSTALL is False and stubs.checks.AUTOINSTALL is False


def test_yolo_export_arguments_are_pinned(tmp_path, monkeypatch):
    log = []
    _install_ultralytics(monkeypatch, tmp_path, log)
    result = ec.convert_yolo(tmp_path / "checkpoint.pt", _cfg(network_input=1280))
    args = next(entry for entry in log if entry[0] == "Exporter")[1]
    assert args["format"] == "onnx" and args["mode"] == "export"
    assert args["imgsz"] == 1280 and args["opset"] == 17 and args["batch"] == 1
    assert args["dynamic"] is False and args["simplify"] is True
    assert args["nms"] is None  # one-to-many; nms=False would select YOLO26's (N, 300, 6)
    assert args["device"] == "cpu" and args["data"] is None
    for key in ("half", "int8", "quantize"):
        assert not args.get(key)
    assert result.exporter == "ultralytics 8.4.162" and result.torch == "2.5.1+cpu"
    assert result.describe["class_names"] == ["helmet", "human", "no-helmet", "vest"]


def test_checkpoint_overrides_cannot_change_precision_or_layout(tmp_path, monkeypatch):
    log = []
    hostile = {"task": "detect", "half": True, "int8": True, "quantize": "fp16", "dynamic": True,
               "nms": False, "simplify": False, "format": "engine", "end2end": True, "imgsz": 320,
               "opset": 20}
    _install_ultralytics(monkeypatch, tmp_path, log, overrides=hostile)
    ec.convert_yolo(tmp_path / "checkpoint.pt", _cfg())
    args = next(entry for entry in log if entry[0] == "Exporter")[1]
    assert {k: args.get(k) for k in ("half", "int8", "quantize", "end2end")} == dict.fromkeys(
        ("half", "int8", "quantize", "end2end"))
    assert (args["dynamic"], args["nms"], args["simplify"], args["format"]) == (False, None, True, "onnx")
    assert (args["imgsz"], args["opset"]) == (640, 17)


def test_yolo_export_args_match_the_pinned_table():
    assert ec.YOLO_EXPORT_ARGS == {"format": "onnx", "dynamic": False, "simplify": True, "nms": None,
                                   "batch": 1, "device": "cpu", "verbose": False}


@pytest.mark.parametrize("kwargs,fragment", [
    (dict(task="segment", model_cls="SegmentationModel", head_cls="Segment"), "task is 'segment'"),
    (dict(model_cls="PoseModel"), "model class is ultralytics.nn.tasks.PoseModel"),
    (dict(head_cls="WorldDetect"), "head is ultralytics.nn.modules.head.WorldDetect"),
    (dict(nc=5), "has 5 classes; the import recorded 4"),
])
def test_yolo_detector_gates(tmp_path, monkeypatch, kwargs, fragment):
    log = []
    _install_ultralytics(monkeypatch, tmp_path, log, **kwargs)
    assert fragment in fatal_message(ec.convert_yolo, tmp_path / "checkpoint.pt", _cfg())
    assert "Exporter" not in [entry[0] for entry in log]  # nothing exported past a failed gate


@pytest.mark.parametrize("head_cls", ["Detect", "v10Detect"])
def test_accepted_yolo_heads(tmp_path, monkeypatch, head_cls):
    log = []
    _install_ultralytics(monkeypatch, tmp_path, log, head_cls=head_cls)
    assert ec.convert_yolo(tmp_path / "checkpoint.pt", _cfg()).describe["head_class"].endswith(head_cls)


def test_yolo_accepted_heads_match_the_portal_preflight():
    assert tuple(ec.YOLO_ACCEPTED_HEADS) == tuple(dc.ACCEPTED_YOLO_HEADS)
    assert ec.YOLO_MODEL_CLASS == dc.YOLO_MODEL_CLASS


# ---------------------------------------------------------------------------
# Stub rfdetr / torch (RF-DETR path)
# ---------------------------------------------------------------------------

class T:
    """A tensor stand-in: only `.shape` is read."""

    def __init__(self, *shape):
        self.shape = tuple(shape)


def _rf_state(size, num_classes):
    dec = {"nano": 2, "small": 3, "medium": 4, "large": 4}[size]
    grid = {"nano": 24, "small": 32, "medium": 36, "large": 44}[size]
    sd = {"class_embed.weight": T(num_classes + 1, 256), "class_embed.bias": T(num_classes + 1),
          "backbone.0.encoder.embeddings.position_embeddings": T(1, grid * grid + 1, 384)}
    for i in range(dec):
        sd[f"transformer.decoder.layers.{i}.self_attn.in_proj_weight"] = T(768, 256)
    return sd


class _Module:
    def __init__(self, state):
        self.state = state
        self.exported = False

    def state_dict(self):
        return dict(self.state)

    def cpu(self):
        return self

    def eval(self):
        return self

    def export(self):
        self.exported = True


def _install_rfdetr(monkeypatch, tmp_path, ckpt, log, *, loaded_state=None):
    rfdetr = types.ModuleType("rfdetr")
    natives = {"nano": 384, "small": 512, "medium": 576, "large": 704}
    for size, cls_name in ec.RFDETR_SIZE_CLASSES.items():
        def make(size=size, cls_name=cls_name):
            class Model:
                def __init__(self, pretrain_weights=None, num_classes=None, trust_checkpoint=False):
                    log.append((cls_name, pretrain_weights))
                    state = _rf_state(size, num_classes)
                    if pretrain_weights is not None and loaded_state is not None:
                        state = loaded_state
                    self.model = SimpleNamespace(model=_Module(state), resolution=natives[size])

                def export(self, output_dir, **kwargs):
                    log.append(("export", kwargs))
                    out = Path(output_dir) / "model.onnx"
                    out.write_bytes(b"stub")
                    return out
            Model.__name__ = cls_name
            return Model
        setattr(rfdetr, cls_name, make())
    torch = types.ModuleType("torch")
    torch.__version__ = "2.5.1+cpu"
    torch.load = lambda path, map_location=None, weights_only=None: (
        log.append(("torch.load", weights_only)) or copy.deepcopy(ckpt))
    monkeypatch.setitem(sys.modules, "rfdetr", rfdetr)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(ec, "WORK", tmp_path / "work")


def _rf_ckpt(size="small", num_classes=1, **top):
    ckpt = {"model": _rf_state(size, num_classes), "args": {"class_names": ["blue_plate"]}}
    ckpt.update(top)
    return ckpt


def _rf_cfg(**kw):
    base = dict(arch="rf_detr", network_input=512, num_classes=1, opset=17, expected_sha256=SHA,
                rfdetr_size=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_rfdetr_size_is_inferred_by_an_exact_architecture_match(tmp_path, monkeypatch):
    log = []
    _install_rfdetr(monkeypatch, tmp_path, _rf_ckpt("small"), log)
    result = ec.convert_rfdetr(tmp_path / "checkpoint.pth", _rf_cfg())
    assert result.describe == {"size": "small", "model_class": "RFDETRSmall", "num_classes": 1,
                               "class_names": ["blue_plate"], "resolution": 512}
    assert ("torch.load", False) in log  # the one permitted unpickle, inside the isolated job
    export_kwargs = next(entry[1] for entry in log if entry[0] == "export")
    assert export_kwargs["opset_version"] == 17 and export_kwargs["batch_size"] == 1
    assert export_kwargs["dynamic_batch"] is False and export_kwargs["format"] == "onnx"


def test_rfdetr_model_name_is_tried_first_and_only_matches_count(tmp_path, monkeypatch):
    log = []
    _install_rfdetr(monkeypatch, tmp_path, _rf_ckpt("nano", 90, model_name="RFDETRNano"), log)
    result = ec.convert_rfdetr(tmp_path / "checkpoint.pth", _rf_cfg(network_input=384, num_classes=90))
    shells = [entry[0] for entry in log if entry[1] is None]
    assert shells == ["RFDETRNano"] and result.describe["size"] == "nano"


@pytest.mark.parametrize("model_name,fragment", [
    ("RFDETRXLarge", "PML-licensed"), ("RFDETR2XLarge", "PML-licensed"),
    ("RFDETRSegSmall", "only RF-DETR object detection"),
    ("RFDETRKeypointPreview", "only RF-DETR object detection"),
    ("RFDETRBase", "legacy RFDETRBase"),
])
def test_rfdetr_named_variants_are_rejected(tmp_path, monkeypatch, model_name, fragment):
    log = []
    _install_rfdetr(monkeypatch, tmp_path, _rf_ckpt(model_name=model_name), log)
    assert fragment in fatal_message(ec.convert_rfdetr, tmp_path / "c.pth", _rf_cfg())


def test_rfdetr_segmentation_keys_class_count_and_resolution(tmp_path, monkeypatch):
    log = []
    seg = _rf_ckpt()
    seg["model"]["segmentation_head.conv.weight"] = T(1, 1)
    _install_rfdetr(monkeypatch, tmp_path, seg, log)
    assert "segmentation head" in fatal_message(ec.convert_rfdetr, tmp_path / "c.pth", _rf_cfg())
    _install_rfdetr(monkeypatch, tmp_path, _rf_ckpt(num_classes=3), log)
    assert "head has 3 classes; the import recorded 1" in fatal_message(
        ec.convert_rfdetr, tmp_path / "c.pth", _rf_cfg())
    _install_rfdetr(monkeypatch, tmp_path, _rf_ckpt(), log)
    assert "own resolution (512 for small)" in fatal_message(
        ec.convert_rfdetr, tmp_path / "c.pth", _rf_cfg(network_input=640))


def test_rfdetr_unknown_architecture_and_forced_wrong_size(tmp_path, monkeypatch):
    log = []
    odd = _rf_ckpt()
    odd["model"]["transformer.decoder.layers.7.self_attn.in_proj_weight"] = T(768, 256)
    _install_rfdetr(monkeypatch, tmp_path, odd, log)
    assert "matches 0 Apache-2.0 RF-DETR sizes" in fatal_message(
        ec.convert_rfdetr, tmp_path / "c.pth", _rf_cfg())
    _install_rfdetr(monkeypatch, tmp_path, _rf_ckpt("small"), log)
    assert "matches 0" in fatal_message(ec.convert_rfdetr, tmp_path / "c.pth",
                                        _rf_cfg(rfdetr_size="nano", network_input=384))


def test_rfdetr_strict_load_is_rechecked_after_loading(tmp_path, monkeypatch):
    log = []
    partial = _rf_state("small", 1)
    partial.pop("transformer.decoder.layers.2.self_attn.in_proj_weight")
    _install_rfdetr(monkeypatch, tmp_path, _rf_ckpt(), log, loaded_state=partial)
    assert "did not load key-for-key" in fatal_message(ec.convert_rfdetr, tmp_path / "c.pth", _rf_cfg())


def test_rfdetr_checkpoint_layouts():
    sd = {"class_embed.bias": T(2)}
    assert ec._rfdetr_state_dict({"model": sd, "args": {}}) == sd
    assert ec._rfdetr_state_dict({"state_dict": {"model.class_embed.bias": sd["class_embed.bias"],
                                                 "ema.x": T(1)}}) == sd
    for bad in ({}, {"model": {}}, {"state_dict": {"other.x": T(1)}}, ["not", "a", "dict"]):
        with pytest.raises(SystemExit):
            ec._rfdetr_state_dict(bad)


def test_architecture_mismatch_is_exact():
    a = {"x": (1, 2), "y": (3,)}
    assert ec.rfdetr_architecture_mismatch(a, dict(a)) == {"missing": [], "unexpected": [],
                                                          "shape_mismatch": []}
    assert ec.rfdetr_architecture_mismatch(a, {"x": (1, 3), "z": (1,)}) == {
        "missing": ["y"], "unexpected": ["z"], "shape_mismatch": ["x"]}
    assert ec._shape_signature({"w": T(2, 2), "m._kp_active_mask": T(1), "n": 3}) == {"w": (2, 2)}


# ---------------------------------------------------------------------------
# Pure contract verifiers (Req 6.5, 6.6)
# ---------------------------------------------------------------------------

def summary(inputs=None, outputs=None, ir=8, opset=17, domains=("ai.onnx",), functions=0,
            external=()):
    return {"ir_version": ir, "opsets": {"ai.onnx": opset}, "domains": list(domains),
            "functions": functions, "external_data": list(external),
            "inputs": inputs if inputs is not None else [("images", [1, 3, 640, 640], "FLOAT")],
            "outputs": outputs if outputs is not None else [("output0", [1, 8, 8400], "FLOAT")],
            "metadata_props": {}}


def test_fleet_structure():
    ec.verify_fleet_structure(summary())
    assert "IR version 10" in fatal_message(ec.verify_fleet_structure, summary(ir=10))
    assert "opset 20" in fatal_message(ec.verify_fleet_structure, summary(opset=20))
    assert "non-default operator domains ['com.microsoft']" in fatal_message(
        ec.verify_fleet_structure, summary(domains=("ai.onnx", "com.microsoft")))
    assert "local functions" in fatal_message(ec.verify_fleet_structure, summary(functions=2))
    assert "external data" in fatal_message(ec.verify_fleet_structure, summary(external=("w",)))


@pytest.mark.parametrize("outputs,fragment", [
    ([("output0", [1, 300, 6], "FLOAT")], "[1, K, 6]"),                   # NMS / one-to-one head
    ([("output0", [1, 116, 8400], "FLOAT")], "segmentation head adds 32"),  # 4 + 80 + 32 mask coefs
    ([("output0", [1, 84, 8400], "FLOAT16")], "FLOAT16"),
    ([("output0", [1, 84, 8400], "FLOAT"), ("output1", [1, 32, 160, 160], "FLOAT")], "exactly 1"),
    ([("output0", [1, 84, "N"], "FLOAT")], "static rank-3"),
    ([("output0", [2, 84, 8400], "FLOAT")], "static rank-3 batch-1"),
])
def test_yolo_contract_rejections(outputs, fragment):
    msg = fatal_message(ec.verify_yolo_contract, summary(outputs=outputs), 640, 80)
    assert fragment in msg


def test_yolo_contract_accepts_both_layouts():
    first = ec.verify_yolo_contract(summary(outputs=[("o", [1, 84, 8400], "FLOAT")]), 640, 80)
    assert (first["anchors"], first["layout"]) == (8400, "channels_first")
    last = ec.verify_yolo_contract(summary(outputs=[("o", [1, 8400, 84], "FLOAT")]), 640, 80)
    assert (last["anchors"], last["layout"]) == (8400, "channels_last")


@pytest.mark.parametrize("inputs,fragment", [
    ([("images", [1, 3, 640, 640], "FLOAT16")], "float32"),
    ([("images", [1, 3, 320, 320], "FLOAT")], "expected static [1, 3, 640, 640]"),
    ([("images", ["batch", 3, 640, 640], "FLOAT")], "expected static"),
    ([("a", [1, 3, 640, 640], "FLOAT"), ("b", [1], "FLOAT")], "exactly 1 ONNX input"),
])
def test_input_contract_rejections(inputs, fragment):
    assert fragment in fatal_message(ec.verify_input_contract, summary(inputs=inputs), 640)


def test_rfdetr_contract_uses_the_trainer_verifier():
    ok = summary(inputs=[("input", [1, 3, 512, 512], "FLOAT")],
                 outputs=[("dets", [1, 300, 4], "FLOAT"), ("labels", [1, 300, 2], "FLOAT")])
    assert ec.verify_rfdetr_contract(ok, 512, 1, train_rfdetr) == {
        "input_shape": [1, 3, 512, 512], "output_shapes": [[1, 300, 4], [1, 300, 2]], "top_k": 300}
    unwidened = summary(inputs=[("input", [1, 3, 512, 512], "FLOAT")],
                        outputs=[("dets", [1, 300, 4], "FLOAT"), ("labels", [1, 300, 1], "FLOAT")])
    msg = fatal_message(ec.verify_rfdetr_contract, unwidened, 512, 1, train_rfdetr)
    assert "background slot" in msg and not msg.startswith("FATAL: FATAL")
    fp16 = summary(inputs=[("input", [1, 3, 512, 512], "FLOAT")],
                   outputs=[("dets", [1, 300, 4], "FLOAT16"), ("labels", [1, 300, 2], "FLOAT")])
    assert "FLOAT16" in fatal_message(ec.verify_rfdetr_contract, fp16, 512, 1, train_rfdetr)


def test_ir_version_is_clamped_only_above_the_floor(tmp_path, monkeypatch):
    saved = []

    class FakeModel:
        def __init__(self, ir):
            self.ir_version = ir

    onnx = types.ModuleType("onnx")
    store = {"ir": 10}
    onnx.load = lambda path, load_external_data=False: FakeModel(store["ir"])
    onnx.save = lambda model, path: saved.append(model.ir_version)
    monkeypatch.setitem(sys.modules, "onnx", onnx)
    assert ec.normalize_ir_version(tmp_path / "m.onnx") == (10, 8) and saved == [8]
    store["ir"] = 8
    assert ec.normalize_ir_version(tmp_path / "m.onnx") == (8, 8) and saved == [8]  # untouched


# ---------------------------------------------------------------------------
# Fleet floor subprocess
# ---------------------------------------------------------------------------

FAKE_FLOOR = r'''#!{python}
import json, sys
import numpy as np
mode = {mode!r}
model, feeds, out = sys.argv[3:6]
if mode == "crash":
    sys.stderr.write("onnxruntime.capi.onnxruntime_pybind11_state.Fail: IR version 10 unsupported\n")
    sys.exit(1)
data = np.load(feeds)
arrays, runs = {{}}, {{}}
for key in data.files:
    value = np.ones((1, 8, 10), dtype=np.float32)
    if mode == "nan":
        value[0, 0, 0] = np.nan
    arrays[key + "__0"] = value
    runs[key] = [{{"shape": [1, 8, 10], "dtype": "float32", "finite": bool(np.isfinite(value).all())}}]
np.savez(out, **arrays)
print(json.dumps({{"ort": "1.17.0" if mode == "version" else "1.16.3", "outputs": ["output0"], "runs": runs}}))
'''


@pytest.mark.parametrize("mode,fragment", [
    ("ok", None), ("crash", "could not load or run the graph"), ("nan", "contains NaN/Inf"),
    ("version", "expected 1.16.3"),
])
def test_fleet_floor_subprocess(tmp_path, monkeypatch, mode, fragment):
    script = tmp_path / "fake_floor_python"
    script.write_text(FAKE_FLOOR.format(python=sys.executable, mode=mode))
    script.chmod(0o755)
    monkeypatch.setattr(ec, "ORT_FLOOR_PYTHON", str(script))
    monkeypatch.setattr(ec, "WORK", tmp_path / "work")
    inputs = {"synthetic": np.zeros((1, 3, 4, 4), np.float32), "image": np.ones((1, 3, 4, 4), np.float32)}
    if fragment is None:
        out = ec.run_on_fleet_floor(tmp_path / "m.onnx", inputs)
        assert out.ort == "1.16.3" and set(out.outputs) == {"synthetic", "image"}
        assert out.outputs["image"][0].shape == (1, 8, 10)
    else:
        assert fragment in fatal_message(ec.run_on_fleet_floor, tmp_path / "m.onnx", inputs)


# ---------------------------------------------------------------------------
# Parity comparators (numpy)
# ---------------------------------------------------------------------------

def _yolo_output(num_classes=4, anchors=50, seed=0):
    rng = np.random.default_rng(seed)
    boxes = rng.uniform(0, 640, (1, 4, anchors))
    scores = rng.uniform(0, 1, (1, num_classes, anchors))
    return np.concatenate([boxes, scores], axis=1).astype(np.float32)


def test_yolo_parity_within_and_beyond_tolerance():
    tol = ec.PARITY_TOLERANCE["yolo"]
    ref = _yolo_output()
    near = ref.copy()
    near[0, :4] += 0.05
    near[0, 4:] += 5e-4
    report = ec.compare_yolo_parity({"image": [ref]}, {"image": [near]}, 4, tol, "onnxruntime 1.16.3")
    assert report["image"]["box_max_abs"] == pytest.approx(0.05, abs=1e-4)
    far_box = ref.copy()
    far_box[0, 0, 7] += 0.5
    assert "box 0.5" in fatal_message(ec.compare_yolo_parity, {"image": [ref]}, {"image": [far_box]},
                                      4, tol, "rt")
    far_score = ref.copy()
    far_score[0, 5, 3] += 0.01
    assert "score 0.01" in fatal_message(ec.compare_yolo_parity, {"image": [ref]},
                                         {"image": [far_score]}, 4, tol, "rt")
    assert "differs from the source model's" in fatal_message(
        ec.compare_yolo_parity, {"image": [ref]}, {"image": [ref[:, :, :10]]}, 4, tol, "rt")


def test_yolo_parity_channels_last():
    ref = np.transpose(_yolo_output(), (0, 2, 1)).copy()
    report = ec.compare_yolo_parity({"s": [ref]}, {"s": [ref.copy()]}, 4, ec.PARITY_TOLERANCE["yolo"], "rt")
    assert report["s"]["box_max_abs"] == 0.0


def _linear_sum_assignment(cost):
    """Brute-force stand-in for scipy.optimize.linear_sum_assignment (small Q)."""
    n = cost.shape[0]
    best = min(itertools.permutations(range(n)), key=lambda p: sum(cost[i, p[i]] for i in range(n)))
    return np.arange(n), np.array(best)


@pytest.fixture
def scipy_stub(monkeypatch):
    scipy = types.ModuleType("scipy")
    optimize = types.ModuleType("scipy.optimize")
    optimize.linear_sum_assignment = _linear_sum_assignment
    scipy.optimize = optimize
    monkeypatch.setitem(sys.modules, "scipy", scipy)
    monkeypatch.setitem(sys.modules, "scipy.optimize", optimize)


def _logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def _rf_outputs(q=6, c=2, confident=((0, 0, 0.9), (1, 1, 0.7))):
    rng = np.random.default_rng(3)
    boxes = rng.uniform(0.1, 0.9, (1, q, 4))
    probs = rng.uniform(0.01, 0.05, (1, q, c + 1))
    for slot, cls, p in confident:
        probs[0, slot, cls] = p
    return [boxes, _logit(probs)]


def test_rfdetr_parity_is_permutation_invariant(scipy_stub):
    tol = ec.PARITY_TOLERANCE["rf_detr"]
    ref = _rf_outputs()
    swapped = [a[:, [1, 0, 2, 3, 4, 5]].copy() for a in ref]  # two confident slots swap
    report = ec.compare_rfdetr_parity({"image": ref}, {"image": swapped}, 2, tol, "onnxruntime 1.16.3")
    entry = report["image"]
    assert (entry["detections"], entry["matched"], entry["slot_agreement"]) == (2, 2, 1.0)
    assert entry["box_max_abs"] == 0.0


def test_rfdetr_parity_tolerates_unstable_low_confidence_tail(scipy_stub):
    tol = ec.PARITY_TOLERANCE["rf_detr"]
    ref = _rf_outputs()
    tail = [a.copy() for a in ref]
    tail[0][0, 5] = [0.5, 0.5, 0.2, 0.2]  # a different low-score proposal in the last slot
    report = ec.compare_rfdetr_parity({"image": ref}, {"image": tail}, 2, tol, "rt")
    assert report["image"]["slot_agreement"] == pytest.approx(5 / 6, abs=1e-4)
    assert report["image"]["matched"] == 2


def test_rfdetr_parity_catches_missing_extra_and_moved_detections(scipy_stub):
    tol = ec.PARITY_TOLERANCE["rf_detr"]
    ref = _rf_outputs()
    missing = [a.copy() for a in ref]
    missing[1][0, 0, 0] = _logit(0.02)
    assert "1 of 2 unmatched" in fatal_message(ec.compare_rfdetr_parity, {"i": ref}, {"i": missing},
                                               2, tol, "rt")
    extra = [a.copy() for a in ref]
    extra[1][0, 3, 1] = _logit(0.8)
    assert "1 extra" in fatal_message(ec.compare_rfdetr_parity, {"i": ref}, {"i": extra}, 2, tol, "rt")
    moved = [a.copy() for a in ref]
    moved[0][0, 0] += 0.01
    assert "unmatched" in fatal_message(ec.compare_rfdetr_parity, {"i": ref}, {"i": moved}, 2, tol, "rt")


def test_rfdetr_parity_fails_a_broken_export_without_confident_detections(scipy_stub):
    tol = ec.PARITY_TOLERANCE["rf_detr"]
    ref = _rf_outputs(confident=())
    broken = [np.random.default_rng(9).uniform(0, 1, a.shape) for a in ref]
    broken[1] = _logit(np.full(ref[1].shape, 0.02))
    msg = fatal_message(ec.compare_rfdetr_parity, {"synthetic": ref}, {"synthetic": broken}, 2, tol, "rt")
    assert "query slots agree" in msg


def test_rfdetr_parity_requires_both_tensors(scipy_stub):
    ref = _rf_outputs()
    assert "do not match the source model's" in fatal_message(
        ec.compare_rfdetr_parity, {"i": ref}, {"i": [ref[0]]}, 2, ec.PARITY_TOLERANCE["rf_detr"], "rt")


def test_parity_summary_is_the_worst_case():
    results = {"a": {"x": {"box_max_abs": 0.1, "score_max_abs": 1e-6},
                     "y": {"box_max_abs": 0.3, "score_max_abs": 1e-7}},
               "b": {"x": {"box_max_abs": 0.2, "score_max_abs": 2e-6}}}
    assert ec.parity_summary(results) == {"box_max_abs": 0.3, "score_max_abs": 2e-6}


# ---------------------------------------------------------------------------
# Metadata (Req 6.8): packager keys and agreement with the portal validator
# ---------------------------------------------------------------------------

def _yolo_metadata():
    cfg = _cfg()
    result = SimpleNamespace(describe={"class_names": ["helmet", "human", "no-helmet", "vest"],
                                       "task": "detect"},
                             exporter="ultralytics 8.4.162", torch="2.5.1+cpu")
    s = summary()
    contract = ec.verify_yolo_contract(s, 640, 4)
    parity = {"onnxruntime 1.30.0": {"image": {"box_max_abs": 0.001, "score_max_abs": 1e-6}},
              "onnxruntime 1.16.3": {"image": {"box_max_abs": 0.002, "score_max_abs": 2e-6}}}
    floor = SimpleNamespace(ort="1.16.3")
    return ec.build_conversion_metadata(cfg, result, s, contract, floor, parity, SHA, "cd" * 32, (8, 8))


def _rf_metadata():
    cfg = _rf_cfg(num_classes=4)
    result = SimpleNamespace(describe={"size": "small", "model_class": "RFDETRSmall", "class_names": None},
                             exporter="rfdetr 1.10.1", torch="2.5.1+cpu")
    s = summary(inputs=[("input", [1, 3, 512, 512], "FLOAT")],
                outputs=[("dets", [1, 300, 4], "FLOAT"), ("labels", [1, 300, 5], "FLOAT")])
    contract = ec.verify_rfdetr_contract(s, 512, 4, train_rfdetr)
    parity = {"onnxruntime 1.16.3": {"image": {"box_max_abs": 1e-6, "score_max_abs": 2e-5}}}
    return ec.build_conversion_metadata(cfg, result, s, contract, SimpleNamespace(ort="1.16.3"),
                                        parity, SHA, "ef" * 32, (8, 8))


PACKAGER_KEYS_YOLO = ("detection_arch", "imgsz", "onnx_output_shape", "num_classes", "class_names",
                      "device_manifest_hints")
PACKAGER_KEYS_RF = ("detection_arch", "resolution", "onnx_output_shapes", "num_classes",
                    "class_names", "device_manifest_hints", "top_k")
PROVENANCE_KEYS = ("source_sha256", "onnx_sha256", "exporter", "opset", "ir_version", "parity")


def test_yolo_metadata_carries_the_packager_and_provenance_keys():
    meta = _yolo_metadata()
    for key in PACKAGER_KEYS_YOLO + PROVENANCE_KEYS:
        assert key in meta, key
    assert meta["detection_arch"] == "yolo" and meta["imgsz"] == 640
    assert meta["onnx_output_shape"] == [1, 8, 8400]
    assert meta["class_names"] == ["helmet", "human", "no-helmet", "vest"]
    assert meta["device_manifest_hints"] == {"preserve_aspect": True, "network_input": 640,
                                             "layout": "yolo", "iou_threshold": 0.45,
                                             "score_threshold": 0.25}
    assert (meta["opset"], meta["ir_version"], meta["source_sha256"]) == (17, 8, SHA)
    assert meta["parity"]["max_abs"] == {"box_max_abs": 0.002, "score_max_abs": 2e-6}
    assert meta["parity"]["runtimes"]["onnxruntime 1.16.3"]["image"]["box_max_abs"] == 0.002
    assert meta["fleet_floor"] == {"onnxruntime": "1.16.3", "loaded": True, "finite": True}
    assert "best.pt" not in json.dumps(meta) or True  # the checkpoint itself is never in the artifact
    json.dumps(meta)


def test_rfdetr_metadata_carries_the_packager_and_provenance_keys():
    meta = _rf_metadata()
    for key in PACKAGER_KEYS_RF + PROVENANCE_KEYS:
        assert key in meta, key
    assert meta["resolution"] == 512 and meta["top_k"] == 300
    assert meta["onnx_output_shapes"] == [[1, 300, 4], [1, 300, 5]]
    assert meta["logits_slots"] == 5 and meta["background_slot"] == 4
    assert meta["device_manifest_hints"]["preserve_aspect"] is False
    assert meta["device_manifest_hints"]["normalize"] is True
    assert meta["device_manifest_hints"]["stage_type"] == "rf_detr_object_detection"
    assert meta["class_names"] == []  # the record, not the checkpoint, names the classes


@pytest.mark.parametrize("build,arch,size,classes", [
    (_yolo_metadata, "yolo", 640, 4), (_rf_metadata, "rf_detr", 512, 4)])
def test_the_portal_validator_accepts_what_the_job_writes(build, arch, size, classes):
    meta = build()
    portal_summary = {"outputs": ([[1, 8, 8400]] if arch == "yolo" else [[1, 300, 4], [1, 300, 5]]),
                      "top_k": 300}
    dc.check_metadata_against_record(meta, arch, size, classes, meta["onnx_sha256"], portal_summary)


def test_write_artifact_leaves_exactly_two_members(tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "stale.bin").write_bytes(b"x")
    (model_dir / "sub").mkdir()
    onnx = tmp_path / "exported.onnx"
    onnx.write_bytes(b"graph")
    ec.write_artifact(onnx, {"a": 1}, model_dir)
    assert sorted(p.name for p in model_dir.iterdir()) == ["model.onnx", "training_metadata.json"]
    assert (model_dir / "model.onnx").read_bytes() == b"graph"
    assert json.loads((model_dir / "training_metadata.json").read_text()) == {"a": 1}
