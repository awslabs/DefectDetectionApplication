"""
The YOLO trainer lowers the ONNX IR header it exports to what the graph needs
(datasets/detection_training/_common.normalize_onnx_ir_version, called by
train.export).

onnxslim 0.1.34 re-serializes ultralytics' `simplify=True` export through the
pinned onnx 1.17.0 and stamps ir_version 10. Reproduced with the trainer's
exact pins: IR 10 with simplify=True, IR 8 without. onnxruntime 1.16.3 (JP5,
CPU / x86 images) refuses IR 10.

The onnx library is not installed on this host, so a small fake `onnx`
module stands in for it. The real library was exercised against the
trainer's own export in a container with the same pins
(docs/detector-checkpoint-import-spike.md).
"""
import sys
import types
from types import SimpleNamespace

import pytest

# Reuse the trainer static suite's module import (heavy packages blocked) and
# its ultralytics stubs / sandbox fixture.
from test_train_yolo_static import (  # noqa: F401 - `sandbox` is a fixture
    _FakeYolo,
    _install_ultralytics_stub,
    _read_metadata,
    sandbox,
    tr,
)

import _common  # the trainer's shared helpers (datasets/detection_training)


class FakeType:
    def __init__(self, elem_type=1, kind="tensor_type"):
        self.kind = kind
        setattr(self, kind, SimpleNamespace(elem_type=elem_type))

    def WhichOneof(self, _name):
        return self.kind


class FakeAttr:
    def __init__(self, t=None, g=None):
        self.t, self.g = t, g
        self.tensors, self.graphs, self.type_protos = [], [], []
        self.sparse_tensor = self.tp = None

    def HasField(self, name):
        return getattr(self, name) is not None


def fake_graph(dtypes=(1,), attrs=()):
    return SimpleNamespace(
        initializer=[SimpleNamespace(data_type=t) for t in dtypes], sparse_initializer=[],
        input=[SimpleNamespace(type=FakeType())], output=[SimpleNamespace(type=FakeType())],
        value_info=[], node=[SimpleNamespace(attribute=list(attrs))])


def fake_model(ir=10, opset=17, functions=0, **graph_kwargs):
    return SimpleNamespace(ir_version=ir, opset_import=[SimpleNamespace(domain="", version=opset)],
                           functions=[object()] * functions, graph=fake_graph(**graph_kwargs))


@pytest.fixture
def fake_onnx(monkeypatch):
    state = SimpleNamespace(model=None, saved=[], checked=[], check_error=None)
    onnx = types.ModuleType("onnx")
    onnx.load = lambda path, load_external_data=False: state.model
    onnx.save = lambda model, path: state.saved.append((model.ir_version, str(path)))
    checker = types.ModuleType("onnx.checker")

    def check_model(model):
        state.checked.append(model.ir_version)
        if state.check_error:
            raise state.check_error
    checker.check_model = check_model
    onnx.checker = checker
    monkeypatch.setitem(sys.modules, "onnx", onnx)
    monkeypatch.setitem(sys.modules, "onnx.checker", checker)
    return state


def test_ir10_opset17_is_lowered_to_ir8_and_validated(fake_onnx, tmp_path):
    fake_onnx.model = fake_model()
    before, after, note = _common.normalize_onnx_ir_version(tmp_path / "model.onnx")
    assert (before, after) == (10, 8) and "10 -> 8" in note
    assert fake_onnx.checked == [8]
    assert fake_onnx.saved == [(8, str(tmp_path / "model.onnx"))]


@pytest.mark.parametrize("kwargs,after,saved", [
    (dict(ir=9), 9, False), (dict(ir=8), 8, False),
    (dict(opset=19), 9, True), (dict(opset=21), 10, False),
    (dict(functions=1), 10, False),
    (dict(dtypes=(1, 17)), 9, True),        # FLOAT8 needs IR 9
    (dict(dtypes=(1, 22)), 10, False),      # INT4 needs IR 10
])
def test_the_same_policy_as_the_packager(fake_onnx, tmp_path, kwargs, after, saved):
    fake_onnx.model = fake_model(**kwargs)
    _before, got, _note = _common.normalize_onnx_ir_version(tmp_path / "m.onnx")
    assert got == after
    assert bool(fake_onnx.saved) is saved


def test_subgraph_types_count(fake_onnx, tmp_path):
    inner = fake_graph(dtypes=(21,))
    fake_onnx.model = fake_model(attrs=[FakeAttr(g=inner)])
    assert _common.normalize_onnx_ir_version(tmp_path / "m.onnx")[1] == 10
    assert fake_onnx.saved == []


def test_a_graph_the_checker_rejects_is_left_as_is(fake_onnx, tmp_path):
    fake_onnx.model = fake_model()
    fake_onnx.check_error = ValueError("bad")
    before, after, note = _common.normalize_onnx_ir_version(tmp_path / "m.onnx")
    assert (before, after) == (10, 10) and "does not validate" in note
    assert fake_onnx.saved == []


def test_without_onnx_the_file_is_left_untouched(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "onnx", None)
    assert _common.normalize_onnx_ir_version(tmp_path / "m.onnx")[:2] == (None, None)


def test_export_lowers_the_ir_and_records_it(sandbox, monkeypatch, fake_onnx, capsys):
    log = []
    _install_ultralytics_stub(monkeypatch, log)
    fake_onnx.model = fake_model()
    tr.export(_FakeYolo(log, sandbox.exported), {"test_map50": 0.9}, base=None,
              class_names=["blue_plate"])
    meta = _read_metadata(sandbox.model_dir)
    assert meta["ir_version"] == 8 and meta["ir_version_exported"] == 10
    assert fake_onnx.saved == [(8, str(sandbox.model_dir / "model.onnx"))]
    assert "ONNX IR version: ir_version 10 -> 8" in capsys.readouterr().out
    # Every key the packager reads is still there, unchanged.
    assert meta["imgsz"] == 1280 and meta["onnx_output_shape"] is None
    assert meta["device_manifest_hints"]["preserve_aspect"] is True
