"""
``checkpoint_probe.classify_checkpoint`` — the shared-layer checkpoint
classifier promoted from the spike prototype (rfdetr-training-and-transfer-
learning task 7.1; contract in docs/transfer-learning-spike.md §2.1).

Every fixture is a tiny synthetic envelope built into ``tmp_path`` by
``fixtures.checkpoints`` (torch zips with hand-assembled ``data.pkl`` opcodes,
a hand-encoded ONNX ``ModelProto``, a legacy tar, and garbage). Nothing binary
is checked in and ``torch`` is never imported.

# Validates: Requirements 7.1, 7.2, 7.5, 8.2
"""
import ast
import json
import os
import sys

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import checkpoint_probe as cp
import detection_training as dt
from fixtures import checkpoints as fx


FIELDS = ("kind", "arch", "fine_tunable", "num_classes", "class_names")
UNKNOWN = dict(kind="unknown", arch=None, fine_tunable=False, num_classes=None, class_names=None)
NOT_TUNABLE = dict(arch=None, fine_tunable=False, num_classes=None, class_names=None)

# (id, builder, expected {kind, arch, fine_tunable, num_classes, class_names})
CASES = [
    ("ultralytics_best_pt", fx.build_ultralytics_ckpt,
     dict(kind="ultralytics_checkpoint", arch="yolo", fine_tunable=True,
          num_classes=1, class_names=["blue_plate"])),
    ("rfdetr_published_namespace", fx.build_rfdetr_published,
     dict(kind="rfdetr_checkpoint", arch="rf_detr", fine_tunable=True,
          num_classes=90, class_names=None)),
    ("rfdetr_v1101_dict_args", lambda p: fx.build_rfdetr_v1101(p, class_names=["blue_plate"]),
     dict(kind="rfdetr_checkpoint", arch="rf_detr", fine_tunable=True,
          num_classes=1, class_names=["blue_plate"])),
    ("rfdetr_ptl_payload", lambda p: fx.build_rfdetr_ptl(p, class_names=["blue_plate", "blue_plate_b"]),
     dict(kind="rfdetr_checkpoint", arch="rf_detr", fine_tunable=True,
          num_classes=2, class_names=["blue_plate", "blue_plate_b"])),
    ("torchscript", fx.build_torchscript, dict(kind="torchscript", **NOT_TUNABLE)),
    ("state_dict_wrapped", fx.build_state_dict, dict(kind="state_dict", **NOT_TUNABLE)),
    ("state_dict_raw", lambda p: fx.build_state_dict(p, wrapped=False), dict(kind="state_dict", **NOT_TUNABLE)),
    ("legacy_torch_tar", fx.build_legacy_torch_tar, dict(kind="legacy_torch", **NOT_TUNABLE)),
    ("onnx_ultralytics_names", lambda p: fx.build_onnx(p, names={0: "blue_plate"}),
     dict(kind="onnx", arch="yolo", fine_tunable=False, num_classes=1, class_names=["blue_plate"])),
    ("onnx_rfdetr_no_metadata", fx.build_onnx, dict(kind="onnx", **NOT_TUNABLE)),
    ("elf_shared_object", fx.build_elf, UNKNOWN),
    ("garbage_bytes", fx.build_garbage, UNKNOWN),
    ("zero_byte_file", fx.build_empty, UNKNOWN),
    ("truncated_zip", fx.build_truncated_zip, UNKNOWN),
    ("truncated_pickle", fx.build_truncated_pickle, UNKNOWN),
    ("zip_without_data_pkl", fx.build_zip_without_data_pkl, UNKNOWN),
    ("gzip_tarball", fx.build_gzip_tarball, UNKNOWN),
    ("plain_pickle_dict", fx.build_plain_pickle_dict, UNKNOWN),
]
CASE_IDS = [c[0] for c in CASES]


def _classify(tmp_path, name, builder):
    path = tmp_path / f"{name}.bin"
    builder(str(path))
    return path, cp.classify_checkpoint(str(path))


# ---------------------------------------------------------------------------
# Every kind → exact {kind, arch, fine_tunable, num_classes, class_names}
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,builder,expected", CASES, ids=CASE_IDS)
def test_every_kind_classified_exactly(tmp_path, name, builder, expected):
    path, res = _classify(tmp_path, name, builder)
    assert os.path.getsize(path) < 50 * 1024, "fixtures must stay tiny"
    assert {k: res[k] for k in FIELDS} == expected, res["evidence"]
    assert set(res) == set(FIELDS) | {"evidence"}
    assert res["kind"] in cp.KINDS and res["arch"] in cp.ARCHES


@pytest.mark.parametrize("name,builder,expected", CASES, ids=CASE_IDS)
def test_fine_tunable_iff_kind_in_fine_tunable_kinds(tmp_path, name, builder, expected):
    _, res = _classify(tmp_path, name, builder)
    assert res["fine_tunable"] is (res["kind"] in cp.FINE_TUNABLE_KINDS)


@pytest.mark.parametrize("name,builder,expected", CASES, ids=CASE_IDS)
def test_evidence_is_json_serialisable(tmp_path, name, builder, expected):
    _, res = _classify(tmp_path, name, builder)
    # strict json.dumps — no default=str escape hatch
    round_trip = json.loads(json.dumps(res))
    assert round_trip["kind"] == res["kind"]
    assert isinstance(res["evidence"], dict)


def test_vocabulary_matches_spike_contract():
    assert cp.KINDS == ("onnx", "torchscript", "ultralytics_checkpoint", "rfdetr_checkpoint",
                        "state_dict", "legacy_torch", "unknown")
    assert cp.ARCHES == ("yolo", "rf_detr", None)
    assert cp.FINE_TUNABLE_KINDS == ("ultralytics_checkpoint", "rfdetr_checkpoint")
    assert set(cp.FINE_TUNABLE_KINDS) < set(cp.KINDS)


# ---------------------------------------------------------------------------
# RF-DETR: num_classes from class_embed.bias − 1, never args.num_classes
# ---------------------------------------------------------------------------

def test_rfdetr_published_ignores_stale_args_num_classes(tmp_path):
    # Real rf-detr-nano.pth: class_embed.bias is 91 wide, args.num_classes == 2.
    path = tmp_path / "rf-detr-nano.pth"
    fx.build_rfdetr_published(str(path), num_classes=90, args_num_classes=2)
    res = cp.classify_checkpoint(str(path))
    assert res["num_classes"] == 90
    assert res["class_names"] is None
    ev = res["evidence"]
    assert ev["num_classes_source"] == "class_embed.bias.shape[0] - 1"
    assert ev["args_picks"]["num_classes"] == 2          # seen …
    assert ev["rfdetr_signals"]["args_type"] == "argparse.Namespace"
    assert ev["rfdetr_signals"]["enc_out_class_embed_groups"] == 13
    assert "class_names" not in ev.get("names_source", "")  # … and ignored


@pytest.mark.parametrize("builder", [fx.build_rfdetr_v1101, fx.build_rfdetr_ptl], ids=["v1101", "ptl"])
def test_rfdetr_dict_args_head_width_wins_over_args_num_classes(tmp_path, builder):
    path = tmp_path / "checkpoint_best_total.pth"
    builder(str(path), class_names=["a", "b"], args_num_classes=7)
    res = cp.classify_checkpoint(str(path))
    assert res["kind"] == "rfdetr_checkpoint"
    assert res["num_classes"] == 2                        # 3-wide head − background
    assert res["class_names"] == ["a", "b"]
    assert res["evidence"]["args_picks"]["num_classes"] == 7
    assert res["evidence"]["names_source"] == "args.class_names"
    assert res["evidence"]["rfdetr_signals"]["args_type"] == "dict"
    assert res["evidence"]["model_name"] == "RFDETRSmall"


def test_rfdetr_ptl_layout_has_no_model_config_and_classifies(tmp_path):
    path = tmp_path / "checkpoint_best_total.pth"
    fx.build_rfdetr_ptl(str(path))
    res = cp.classify_checkpoint(str(path))
    assert res["kind"] == "rfdetr_checkpoint"
    assert "model_config.num_classes" not in res["evidence"]
    assert "state_dict" in res["evidence"]["top_level_keys"]
    assert res["evidence"]["rfdetr_signals"]["state_dict_prefix"] == ""   # `model` member used


def test_rfdetr_v1101_layout_records_model_config(tmp_path):
    path = tmp_path / "checkpoint_best_total.pth"
    fx.build_rfdetr_v1101(str(path), class_names=["blue_plate"])
    res = cp.classify_checkpoint(str(path))
    assert res["evidence"]["model_config.num_classes"] == 1
    assert res["evidence"]["epoch"] == 3


# ---------------------------------------------------------------------------
# ultralytics: names order → list, nc
# ---------------------------------------------------------------------------

def test_ultralytics_names_sorted_by_class_id(tmp_path):
    path = tmp_path / "best.pt"
    fx.build_ultralytics_ckpt(str(path), names={2: "c", 0: "a", 1: "b"}, nc=3)
    res = cp.classify_checkpoint(str(path))
    assert res["class_names"] == ["a", "b", "c"]
    assert res["num_classes"] == 3
    ev = res["evidence"]
    assert ev["model_member"] == "model"
    assert ev["model_class"] == "ultralytics.nn.tasks.DetectionModel"
    assert ev["names_source"] == "model.<state>.names"
    assert ev["yaml_file"] == "yolo11s.yaml"
    assert ev["train_args"]["model"] == "yolo11s.pt"
    assert ev["version"] == "8.3.40"


def test_ultralytics_evidence_shows_no_unpickling(tmp_path):
    """The classifier records what it *would* have called; it never imports it."""
    path = tmp_path / "best.pt"
    fx.build_ultralytics_ckpt(str(path))
    before = set(sys.modules)
    res = cp.classify_checkpoint(str(path))
    newly_imported = set(sys.modules) - before
    assert "ultralytics.nn.tasks.DetectionModel" in res["evidence"]["pickle_globals"]
    assert not {m for m in newly_imported if m.split(".")[0] in ("torch", "ultralytics")}


# ---------------------------------------------------------------------------
# ONNX
# ---------------------------------------------------------------------------

def test_onnx_metadata_props_names_recovered(tmp_path):
    path = tmp_path / "model.onnx"
    fx.build_onnx(str(path), names={0: "blue plate", 1: "it's"})
    res = cp.classify_checkpoint(str(path))
    assert res["kind"] == "onnx" and res["fine_tunable"] is False
    assert res["class_names"] == ["blue plate", "it's"]
    assert res["num_classes"] == 2
    assert res["arch"] == "yolo"
    ev = res["evidence"]["onnx"]
    assert ev["producer_name"] == "pytorch" and ev["ir_version"] == 8
    assert ev["opset_import"] == [{"domain": "", "version": 17}]
    assert ev["outputs"] == [{"name": "output0", "shape": [1, 6, 8400]}]
    assert res["evidence"]["names_source"] == "metadata_props.names"


def test_onnx_without_names_is_nameless_and_archless(tmp_path):
    path = tmp_path / "rf-detr-base-coco.onnx"
    fx.build_onnx(str(path))
    res = cp.classify_checkpoint(str(path))
    assert res["kind"] == "onnx"
    assert res["class_names"] is None and res["num_classes"] is None and res["arch"] is None
    assert [o["name"] for o in res["evidence"]["onnx"]["outputs"]] == ["pred_boxes", "pred_logits"]
    assert "metadata_props" not in res["evidence"]["onnx"]


# ---------------------------------------------------------------------------
# Never raises
# ---------------------------------------------------------------------------

def test_missing_path_is_unknown(tmp_path):
    res = cp.classify_checkpoint(str(tmp_path / "does-not-exist.pt"))
    assert {k: res[k] for k in FIELDS} == UNKNOWN
    assert "error" in res["evidence"]


def test_directory_path_is_unknown(tmp_path):
    res = cp.classify_checkpoint(str(tmp_path))
    assert res["kind"] == "unknown" and res["fine_tunable"] is False


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.binary(max_size=2048))
def test_property_arbitrary_bytes_never_raise(tmp_path_factory, data):
    """Property: for any byte string, classify_checkpoint returns a well-formed
    result (kind ∈ KINDS, fine_tunable ⇔ kind ∈ FINE_TUNABLE_KINDS, JSON-safe)
    and never raises.

    # Validates: Requirements 7.5
    """
    path = tmp_path_factory.mktemp("arb") / "blob"
    path.write_bytes(data)
    res = cp.classify_checkpoint(str(path))
    assert res["kind"] in cp.KINDS
    assert res["arch"] in cp.ARCHES
    assert res["fine_tunable"] is (res["kind"] in cp.FINE_TUNABLE_KINDS)
    assert res["fine_tunable"] is False  # random bytes are never a trainable checkpoint
    json.dumps(res)


_ENVELOPES = {
    "ultralytics": fx.build_ultralytics_ckpt,
    "rfdetr_published": fx.build_rfdetr_published,
    "rfdetr_ptl": fx.build_rfdetr_ptl,
    "torchscript": fx.build_torchscript,
    "legacy_tar": fx.build_legacy_torch_tar,
    "onnx": lambda p: fx.build_onnx(p, names={0: "x"}),
}


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(which=st.sampled_from(sorted(_ENVELOPES)), data=st.data())
def test_property_corrupted_envelopes_never_raise(tmp_path_factory, which, data):
    """Property: truncating a valid envelope anywhere, or flipping bytes in it,
    still yields a well-formed result — the failure lands under evidence.

    # Validates: Requirements 7.5
    """
    d = tmp_path_factory.mktemp("corrupt")
    src = d / "src"
    blob = _ENVELOPES[which](str(src))
    cut = data.draw(st.integers(min_value=0, max_value=len(blob)))
    mutated = bytearray(blob[:cut])
    for _ in range(data.draw(st.integers(min_value=0, max_value=3))):
        if mutated:
            i = data.draw(st.integers(min_value=0, max_value=len(mutated) - 1))
            mutated[i] = data.draw(st.integers(min_value=0, max_value=255))
    out = d / "mutated"
    out.write_bytes(bytes(mutated))
    res = cp.classify_checkpoint(str(out))
    assert res["kind"] in cp.KINDS
    assert res["fine_tunable"] is (res["kind"] in cp.FINE_TUNABLE_KINDS)
    json.dumps(res)


# ---------------------------------------------------------------------------
# Module hygiene: re-export, stdlib-only, CLI
# ---------------------------------------------------------------------------

def test_detection_training_reexports_same_objects():
    assert dt.classify_checkpoint is cp.classify_checkpoint
    assert dt.FINE_TUNABLE_KINDS is cp.FINE_TUNABLE_KINDS


def test_module_imports_are_stdlib_only():
    tree = ast.parse(open(cp.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    imported.discard("__future__")
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
    assert "torch" not in imported


def test_cli_prints_one_json_object_per_path(tmp_path, capsys):
    a = tmp_path / "a.pt"
    b = tmp_path / "b.onnx"
    fx.build_ultralytics_ckpt(str(a))
    fx.build_onnx(str(b))
    assert cp._main([str(a), str(b)]) == 0
    out = capsys.readouterr().out
    objs, pos, dec = [], 0, json.JSONDecoder()
    while pos < len(out.rstrip()):
        obj, pos = dec.raw_decode(out, pos)
        objs.append(obj)
        while pos < len(out) and out[pos].isspace():
            pos += 1
    assert [o["path"] for o in objs] == [str(a), str(b)]
    assert [o["kind"] for o in objs] == ["ultralytics_checkpoint", "onnx"]
    assert cp._main([]) == 2
