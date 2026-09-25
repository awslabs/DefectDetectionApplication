"""
Unit tests for `datasets/detection_training/_common.py` -- the helpers the
detection-training SageMaker entry points (`train.py`, `train_rfdetr.py`)
share as a flat sibling module (rfdetr-training-and-transfer-learning
Requirements 1.2, 6.5).

Covers `hp()` env parsing, the converter command line for both archs,
`stage_manifest_and_images` (source-ref vs IMAGES_S3 prefix), `write_metadata`,
and `fetch_base_weights` across its four outcomes: unset -> None, tarball with
the member -> extracted path, tarball without the member -> clear FATAL that
lists the archive, bare weights file -> downloaded path.

S3 is a tiny in-memory stub (`download_file` / `list_objects_v2` paginator)
injected through the helpers' `s3=` parameter -- no moto, no network.
ultralytics / torch are not needed: `_common` never imports them, and
`train.py` imports ultralytics lazily inside `train()`.
"""
import importlib.util
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_DETECTION_TRAINING = os.path.abspath(
    os.path.join(_HERE, "..", "..", "..", "datasets", "detection_training"))
if _DETECTION_TRAINING not in sys.path:
    # Appended, not prepended: must never shadow a portal layer or backend
    # module for the other tests sharing the session.
    sys.path.append(_DETECTION_TRAINING)

import _common as common  # noqa: E402


# ---------------------------------------------------------------------------
# Stubs / builders
# ---------------------------------------------------------------------------

class _Paginator:
    def __init__(self, objects):
        self._objects = objects

    def paginate(self, Bucket, Prefix):
        keys = sorted(k for (b, k) in self._objects if b == Bucket and k.startswith(Prefix))
        # Two pages, to exercise the loop the way S3 actually pages.
        half = (len(keys) + 1) // 2
        for chunk in (keys[:half], keys[half:]):
            yield {"Contents": [{"Key": k} for k in chunk]}


class StubS3:
    """The two boto3 S3 calls _common makes, over an in-memory object map."""

    def __init__(self, objects):
        self.objects = dict(objects)          # (bucket, key) -> bytes
        self.downloads = []

    def download_file(self, bucket, key, filename):
        self.downloads.append((bucket, key, filename))
        body = self.objects.get((bucket, key))
        if body is None:
            raise RuntimeError(f"NoSuchKey: s3://{bucket}/{key}")
        Path(filename).write_bytes(body)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _Paginator(self.objects)


def _tarball(members, gz=True):
    """Bytes of a tar(.gz) holding {name: bytes}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# hp()
# ---------------------------------------------------------------------------

def test_hp_reads_env_with_default_and_cast(monkeypatch):
    monkeypatch.delenv("DDA_TEST_HP", raising=False)
    assert common.hp("DDA_TEST_HP") is None
    assert common.hp("DDA_TEST_HP", "7") == "7"
    assert common.hp("DDA_TEST_HP", "7", int) == 7
    # No cast on a None default: the caller gets None, not cast(None).
    assert common.hp("DDA_TEST_HP", None, int) is None
    monkeypatch.setenv("DDA_TEST_HP", "1e-4")
    assert common.hp("DDA_TEST_HP", "1", float) == pytest.approx(1e-4)
    assert common.hp("DDA_TEST_HP", "1") == "1e-4"


# ---------------------------------------------------------------------------
# Converter command
# ---------------------------------------------------------------------------

def test_converter_command_yolo_matches_original_train_py_order():
    cmd = common.converter_command(Path("/w/output.manifest"), Path("/w/images"),
                                   Path("/w/dataset"), "yolo", net_input_height=1280)
    assert cmd == [sys.executable, str(common.CONVERTER),
                   "--manifest", "/w/output.manifest",
                   "--images-dir", "/w/images",
                   "--out", "/w/dataset",
                   "--format", "yolo",
                   "--net-input-height", "1280"]
    assert common.CONVERTER.name == "manifest_to_detector_dataset.py"
    assert common.CONVERTER.parent == common.CODE_DIR


def test_converter_command_rfdetr_layout():
    cmd = common.converter_command("m", "i", "o", "coco", coco_layout="rfdetr",
                                   extra_args=("--link",))
    assert cmd[-5:] == ["--format", "coco", "--coco-layout", "rfdetr", "--link"]
    assert "--net-input-height" not in cmd


def test_run_converter_fatal_on_nonzero_exit(monkeypatch):
    class R:
        returncode = 2
    monkeypatch.setattr(common, "sh", lambda cmd, **kw: R())
    with pytest.raises(SystemExit) as ei:
        common.run_converter("m", "i", "o", "yolo")
    assert "FATAL: dataset conversion failed" in str(ei.value)


def test_run_converter_returns_out_dir(monkeypatch, tmp_path):
    class R:
        returncode = 0
    seen = {}
    monkeypatch.setattr(common, "sh", lambda cmd, **kw: seen.setdefault("cmd", cmd) and R())
    out = common.run_converter("m", "i", tmp_path / "ds", "coco", coco_layout="rfdetr")
    assert out == tmp_path / "ds"
    assert seen["cmd"][-4:] == ["--format", "coco", "--coco-layout", "rfdetr"]


# ---------------------------------------------------------------------------
# stage_manifest_and_images
# ---------------------------------------------------------------------------

def _manifest_lines(bucket, names):
    return [json.dumps({"source-ref": f"s3://{bucket}/frames/{n}", "bounding-box": {}})
            for n in names]


def test_stage_downloads_source_refs_when_images_s3_unset(tmp_path):
    lines = _manifest_lines("uc", ["a.jpg", "b.png"])
    s3 = StubS3({
        ("uc", "labeled/output.manifest"): ("\n".join(lines) + "\n\n").encode(),
        ("uc", "frames/a.jpg"): b"A",
        ("uc", "frames/b.png"): b"B",
        ("uc", "frames/unreferenced.jpg"): b"X",
    })
    manifest, images = common.stage_manifest_and_images(
        "s3://uc/labeled/output.manifest", None, tmp_path, s3=s3)
    assert manifest == tmp_path / "output.manifest"
    assert images == tmp_path / "images"
    assert sorted(p.name for p in images.iterdir()) == ["a.jpg", "b.png"]
    assert (images / "a.jpg").read_bytes() == b"A"
    # Exactly the referenced images, nothing else from the prefix.
    assert not (images / "unreferenced.jpg").exists()


def test_stage_downloads_prefix_when_images_s3_set(tmp_path):
    lines = _manifest_lines("uc", ["a.jpg"])
    s3 = StubS3({
        ("uc", "labeled/output.manifest"): "\n".join(lines).encode(),
        ("uc", "frames/a.jpg"): b"A",
        ("uc", "frames/b.JPEG"): b"B",
        ("uc", "frames/notes.txt"): b"nope",
        ("uc", "frames/sub/"): b"",
        # Sibling prefix sharing the name: must NOT be pulled (trailing slash).
        ("uc", "frames-other/c.jpg"): b"C",
    })
    _m, images = common.stage_manifest_and_images(
        "s3://uc/labeled/output.manifest", "s3://uc/frames", tmp_path, s3=s3)
    assert sorted(p.name for p in images.iterdir()) == ["a.jpg", "b.JPEG"]


def test_stage_fatal_when_manifest_missing(tmp_path):
    with pytest.raises(SystemExit) as ei:
        common.stage_manifest_and_images("s3://uc/missing.manifest", None, tmp_path,
                                         s3=StubS3({}))
    assert str(ei.value).startswith("FATAL: could not download manifest s3://uc/missing.manifest")


def test_stage_fatal_when_no_images(tmp_path):
    s3 = StubS3({("uc", "m"): b'{"no-source-ref": 1}\nnot json\n'})
    with pytest.raises(SystemExit) as ei:
        common.stage_manifest_and_images("s3://uc/m", None, tmp_path, s3=s3)
    assert str(ei.value) == "FATAL: no images downloaded from s3://uc/m (source-ref)"


# ---------------------------------------------------------------------------
# write_metadata
# ---------------------------------------------------------------------------

def test_write_metadata_round_trips_and_creates_dir(tmp_path):
    meta = {"imgsz": 1280, "metrics": {"test_map50": 0.99}, "onnx_output_shape": None}
    path = common.write_metadata(tmp_path / "model", meta)
    assert path == tmp_path / "model" / "training_metadata.json"
    assert json.loads(path.read_text()) == meta
    # Same serialisation train.py used before the refactor.
    assert path.read_text() == json.dumps(meta, indent=2)


# ---------------------------------------------------------------------------
# fetch_base_weights
# ---------------------------------------------------------------------------

@pytest.fixture
def no_base_env(monkeypatch):
    monkeypatch.delenv("BASE_WEIGHTS_S3", raising=False)
    monkeypatch.delenv("BASE_WEIGHTS_MEMBER", raising=False)


def test_fetch_base_weights_unset_returns_none(no_base_env, tmp_path):
    s3 = StubS3({})
    assert common.fetch_base_weights(tmp_path) is None
    assert common.fetch_base_weights(tmp_path, "", None, s3=s3) is None
    assert common.fetch_base_weights(tmp_path, "   ", "best.pt", s3=s3) is None
    assert s3.downloads == []
    # Nothing was created either: the published-checkpoint path is untouched.
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


def test_fetch_base_weights_tarball_extracts_member(no_base_env, tmp_path):
    tar = _tarball({"model.onnx": b"onnx", "best.pt": b"PT-BYTES",
                    "training_metadata.json": b"{}"})
    s3 = StubS3({("uc", "models/training/job/output/model.tar.gz"): tar})
    path = common.fetch_base_weights(
        tmp_path / "bw", "s3://uc/models/training/job/output/model.tar.gz",
        "best.pt", s3=s3)
    assert path == tmp_path / "bw" / "best.pt"
    assert path.read_bytes() == b"PT-BYTES"
    # Only the member survives; the multi-hundred-MB tarball is gone.
    assert sorted(p.name for p in (tmp_path / "bw").iterdir()) == ["best.pt"]
    assert s3.downloads == [("uc", "models/training/job/output/model.tar.gz",
                             str(tmp_path / "bw" / "model.tar.gz"))]


def test_fetch_base_weights_reads_env_when_args_omitted(monkeypatch, tmp_path):
    tar = _tarball({"checkpoint_best_total.pth": b"PTH"})
    s3 = StubS3({("uc", "a/model.tar.gz"): tar})
    monkeypatch.setenv("BASE_WEIGHTS_S3", "s3://uc/a/model.tar.gz")
    monkeypatch.setenv("BASE_WEIGHTS_MEMBER", "checkpoint_best_total.pth")
    path = common.fetch_base_weights(tmp_path, s3=s3)
    assert path == tmp_path / "checkpoint_best_total.pth"
    assert path.read_bytes() == b"PTH"


def test_fetch_base_weights_default_member_used_when_env_member_unset(no_base_env, tmp_path):
    tar = _tarball({"best.pt": b"PT", "model.onnx": b"o"})
    s3 = StubS3({("uc", "m.tar.gz"): tar})
    path = common.fetch_base_weights(tmp_path, "s3://uc/m.tar.gz", None,
                                     default_member="best.pt", s3=s3)
    assert path.name == "best.pt" and path.read_bytes() == b"PT"


def test_fetch_base_weights_tarball_missing_member_is_fatal_and_lists_members(
        no_base_env, tmp_path):
    tar = _tarball({"model.onnx": b"o", "training_metadata.json": b"{}",
                    "checkpoint_best_total.pth": b"p"})
    s3 = StubS3({("uc", "m.tar.gz"): tar})
    with pytest.raises(SystemExit) as ei:
        common.fetch_base_weights(tmp_path, "s3://uc/m.tar.gz", "best.pt", s3=s3)
    msg = str(ei.value)
    assert msg.startswith("FATAL: BASE_WEIGHTS_MEMBER 'best.pt' not found in m.tar.gz")
    assert "checkpoint_best_total.pth, model.onnx, training_metadata.json" in msg


def test_fetch_base_weights_tarball_without_any_member_is_fatal(no_base_env, tmp_path):
    tar = _tarball({"best.pt": b"PT", "last.pt": b"L"})
    s3 = StubS3({("uc", "m.tgz"): tar})
    with pytest.raises(SystemExit) as ei:
        common.fetch_base_weights(tmp_path, "s3://uc/m.tgz", None, s3=s3)
    msg = str(ei.value)
    assert "is a tarball; set BASE_WEIGHTS_MEMBER" in msg
    assert "best.pt, last.pt" in msg


def test_fetch_base_weights_tarball_member_matched_by_dot_slash_or_basename(
        no_base_env, tmp_path):
    # `./best.pt` (how `tar -czf . ` names members) and a nested single hit.
    tar = _tarball({"./best.pt": b"DOT"})
    s3 = StubS3({("uc", "a.tar.gz"): tar})
    p = common.fetch_base_weights(tmp_path / "1", "s3://uc/a.tar.gz", "best.pt", s3=s3)
    assert p.read_bytes() == b"DOT"

    tar = _tarball({"output/weights/best.pt": b"NESTED", "model.onnx": b"o"})
    s3 = StubS3({("uc", "b.tar.gz"): tar})
    p = common.fetch_base_weights(tmp_path / "2", "s3://uc/b.tar.gz", "best.pt", s3=s3)
    assert p == tmp_path / "2" / "best.pt" and p.read_bytes() == b"NESTED"


def test_fetch_base_weights_detects_gzip_tar_by_content(no_base_env, tmp_path):
    # Key has no tar suffix, but the object is a gzip tar: still extracted.
    tar = _tarball({"best.pt": b"CONTENT-SNIFFED"})
    s3 = StubS3({("uc", "artifacts/blob"): tar})
    p = common.fetch_base_weights(tmp_path, "s3://uc/artifacts/blob", "best.pt", s3=s3)
    assert p == tmp_path / "best.pt" and p.read_bytes() == b"CONTENT-SNIFFED"


def test_fetch_base_weights_bare_file(no_base_env, tmp_path, capsys):
    # An ultralytics .pt is a zip, not gzip -> treated as a bare weights file.
    s3 = StubS3({("uc", "imports/x/checkpoint.pt"): b"PK\x03\x04zipzip"})
    p = common.fetch_base_weights(tmp_path, "s3://uc/imports/x/checkpoint.pt", None, s3=s3)
    assert p == tmp_path / "checkpoint.pt"
    assert p.read_bytes() == b"PK\x03\x04zipzip"
    out = capsys.readouterr().out
    assert f"base weights: s3://uc/imports/x/checkpoint.pt -> {p} (10 bytes)" in out

    # A stale member for a bare file is a warning, not an error.
    p2 = common.fetch_base_weights(tmp_path / "2", "s3://uc/imports/x/checkpoint.pt",
                                   "best.pt", s3=s3)
    assert p2.name == "checkpoint.pt"
    assert "WARN: BASE_WEIGHTS_MEMBER='best.pt' ignored" in capsys.readouterr().out


def test_fetch_base_weights_bad_uri_or_download_failure_is_fatal(no_base_env, tmp_path):
    with pytest.raises(SystemExit) as ei:
        common.fetch_base_weights(tmp_path, "/local/best.pt", None, s3=StubS3({}))
    assert "must be an s3:// URI" in str(ei.value)
    with pytest.raises(SystemExit) as ei:
        common.fetch_base_weights(tmp_path, "s3://uc/prefix/", None, s3=StubS3({}))
    assert "must name an object" in str(ei.value)
    with pytest.raises(SystemExit) as ei:
        common.fetch_base_weights(tmp_path, "s3://uc/missing.pt", None, s3=StubS3({}))
    assert str(ei.value).startswith("FATAL: could not download base weights s3://uc/missing.pt")


_member_names = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-"),
    min_size=1, max_size=20).map(lambda s: s + ".pt")


@settings(max_examples=25, deadline=None)
@given(member=_member_names,
       body=st.binary(min_size=0, max_size=4096),
       decoys=st.lists(st.tuples(_member_names, st.binary(max_size=64)),
                       max_size=3))
def test_fetch_base_weights_tarball_roundtrip_property(tmp_path_factory, member, body, decoys):
    """Property: for any member name/bytes, tarball -> fetch_base_weights yields
    a file named after the member holding exactly those bytes, whatever else
    the archive contains.

    **Validates: Requirements 6.5**
    """
    tmp_path = tmp_path_factory.mktemp("bw")
    members = {name: b for name, b in decoys if name != member}
    members[member] = body
    s3 = StubS3({("uc", "job/output/model.tar.gz"): _tarball(members)})
    p = common.fetch_base_weights(tmp_path, "s3://uc/job/output/model.tar.gz", member, s3=s3)
    assert p == tmp_path / member
    assert p.read_bytes() == body
    assert not (tmp_path / "model.tar.gz").exists()


# ---------------------------------------------------------------------------
# train.py still imports (its _common sibling import + module constants)
# ---------------------------------------------------------------------------

def test_train_py_imports_without_ultralytics_and_keeps_defaults(monkeypatch):
    for name in ("MANIFEST_S3", "IMAGES_S3", "IMGSZ", "EPOCHS", "BATCH", "BASE_WEIGHTS",
                 "PATIENCE", "ONNX_OPSET", "BASE_WEIGHTS_S3", "BASE_WEIGHTS_MEMBER"):
        monkeypatch.delenv(name, raising=False)
    spec = importlib.util.spec_from_file_location(
        "dda_detection_train_py", os.path.join(_DETECTION_TRAINING, "train.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # The published-checkpoint defaults are byte-identical to the pre-_common
    # entry point; base weights are off unless BASE_WEIGHTS_S3 is set.
    assert (mod.IMGSZ, mod.EPOCHS, mod.BATCH, mod.PATIENCE, mod.OPSET) == (1280, 100, 4, 30, 17)
    assert mod.BASE_WEIGHTS == "yolo11s.pt"
    assert mod.BASE_WEIGHTS_S3 is None and mod.BASE_WEIGHTS_MEMBER is None
    assert mod.CHECKPOINT_MEMBER == "best.pt"
    assert mod.MODEL_DIR is common.MODEL_DIR and mod.WORK is common.WORK
    assert "ultralytics" not in sys.modules


# ---------------------------------------------------------------------------
# cap_onnx_ir_version: exported models keep the IR the edge runtimes load
# ---------------------------------------------------------------------------

class _FakeModel:
    def __init__(self, ir_version):
        self.ir_version = ir_version


def _install_fake_onnx(monkeypatch, ir_version):
    """A stand-in `onnx` module recording load/check/save calls, so the cap
    logic is tested without the real (heavy) package."""
    calls = []
    model = _FakeModel(ir_version)
    fake = type(sys)("onnx")
    fake.load = lambda path: calls.append(("load", path)) or model
    fake.save = lambda m, path: calls.append(("save", m.ir_version, path))
    fake.checker = type(sys)("onnx.checker")
    fake.checker.check_model = lambda m: calls.append(("check", m.ir_version))
    monkeypatch.setitem(sys.modules, "onnx", fake)
    return calls


def test_cap_onnx_ir_version_lowers_newer_ir_and_revalidates(monkeypatch, tmp_path):
    # onnx 1.22 stamps IR 13 on re-serialised graphs; onnxruntime < 1.20
    # (the in-job 1.19.2 and older device builds) only load IR <= 10.
    calls = _install_fake_onnx(monkeypatch, 13)
    path = tmp_path / "model.onnx"
    assert common.cap_onnx_ir_version(path) == (13, 10)
    assert common.EDGE_MAX_ONNX_IR_VERSION == 10
    # Validated under the lowered IR BEFORE the file is rewritten.
    assert calls == [("load", str(path)), ("check", 10), ("save", 10, str(path))]


def test_cap_onnx_ir_version_leaves_supported_ir_untouched(monkeypatch, tmp_path):
    calls = _install_fake_onnx(monkeypatch, 10)
    path = tmp_path / "model.onnx"
    assert common.cap_onnx_ir_version(path) == (10, 10)
    assert calls == [("load", str(path))]


def test_cap_onnx_ir_version_without_onnx_leaves_file_alone(monkeypatch, tmp_path):
    # None in sys.modules makes `import onnx` raise ImportError.
    monkeypatch.setitem(sys.modules, "onnx", None)
    path = tmp_path / "model.onnx"
    path.write_bytes(b"stub")
    assert common.cap_onnx_ir_version(path) is None
    assert path.read_bytes() == b"stub"
