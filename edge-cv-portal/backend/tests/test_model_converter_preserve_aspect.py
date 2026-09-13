# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the detection resize geometry (`preserve_aspect`) that Smart Import
writes into the device manifest — `model_converter.generate_dda_package`.

Why this is pinned end to end rather than just asserting a dict key: the flag
has to survive three hops that live in different codebases, and every one of
them fails silently.

    generate_dda_package  ->  manifest['detection']['preserve_aspect']
    lfv_model_template.__load_model_graph_config
                          ->  stage.setdefault('detection', <that block>)
    BasicPreProcessor._preserve_aspect
                          ->  letterbox instead of squash

If the flag lands in the wrong place, nothing raises: the device just squashes a
letterbox-trained detector, costing ~1.35x mean confidence and up to 5.7x on
high-resolution frames (docs/detection-training-gap.md §7). That is exactly how
the previously deployed yolo-world-blue-plate model ended up on the squash path,
so the last test below drives the real device-side lookup against the real
converter output instead of trusting either in isolation.

The device module needs cv2, which is not installed on the host (it lives in the
flask-app image), so cv2 is stubbed — `_preserve_aspect` reads only `self.config`
and never touches it.
"""
import importlib.util
import json
import os
import sys
import tarfile
import tempfile
import types
from pathlib import Path

import pytest

import model_converter

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_SRC_BACKEND = os.path.join(_REPO, "src", "backend")
if _SRC_BACKEND not in sys.path:
    # Appended so the device tree can never shadow a portal layer module.
    sys.path.append(_SRC_BACKEND)


def _manifest_for(preserve_aspect, model_type="object_detection", **kwargs):
    """Run the real packager over a dummy artifact, return the device manifest."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "model.onnx"
        src.write_bytes(b"not-a-real-onnx-graph")
        out = Path(tmp) / "pkg.tar.gz"
        model_converter.generate_dda_package(
            model_path=str(src),
            model_name="blue_plate_yolo",
            model_type=model_type,
            image_width=1280,
            image_height=1280,
            num_classes=1,
            output_path=str(out),
            export_format="onnx",
            preserve_aspect=preserve_aspect,
            **kwargs,
        )
        with tarfile.open(out, "r:gz") as tar:
            member = tar.extractfile("export_artifacts/manifest.json")
            return json.load(member)


def _merge_detection_block_like_the_device(manifest):
    """Replicate lfv_model_template.__load_model_graph_config's merge.

    (Verbatim semantics: `stage.setdefault('detection', <top-level block>)`.)
    """
    model_graph = manifest["model_graph"]
    detection_config = manifest.get("detection")
    if isinstance(detection_config, dict):
        for stage in model_graph.get("stages", []):
            stage.setdefault("detection", detection_config)
    return model_graph["stages"][0]


def _load_device_preprocessor():
    """Import the REAL basic_preprocessor.py from the device tree.

    Loaded by file path rather than as a normal import because
    ``model_processors/__init__.py`` eagerly imports every postprocessor, one of
    which reaches sklearn — not installed on the host. A stub package entry in
    sys.modules keeps that ``__init__`` from executing while the module under
    test is still the genuine source file. ``lyra_science_processing_utils``
    itself has an empty ``__init__``, so the base-class import it does is cheap.
    """
    if "cv2" not in sys.modules:
        stub = types.ModuleType("cv2")
        stub.INTER_AREA = 3  # only referenced by __call__, not by the lookup
        sys.modules["cv2"] = stub

    pkg_name = "lyra_science_processing_utils.model_processors"
    pkg_dir = os.path.join(_SRC_BACKEND, "lyra_science_processing_utils",
                           "model_processors")
    if pkg_name not in sys.modules:
        shim = types.ModuleType(pkg_name)
        shim.__path__ = [pkg_dir]
        sys.modules[pkg_name] = shim

    mod_name = f"{pkg_name}.basic_preprocessor"
    if mod_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            mod_name, os.path.join(pkg_dir, "basic_preprocessor.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
    return sys.modules[mod_name].BasicPreProcessor


def _device_preserve_aspect(stage_config):
    """Call the REAL BasicPreProcessor._preserve_aspect against a stage dict."""
    pytest.importorskip("numpy", reason="numpy needed by the device module")
    basic_pre_processor = _load_device_preprocessor()
    # _preserve_aspect only reads self.config, so a stand-in object is enough
    # and avoids the abstract base's constructor requirements.
    return basic_pre_processor._preserve_aspect(
        types.SimpleNamespace(config=stage_config))


# ---------------------------------------------------------------------------
# What the converter writes
# ---------------------------------------------------------------------------

def test_detection_manifest_records_letterbox_when_requested():
    manifest = _manifest_for(True)
    assert manifest["detection"]["preserve_aspect"] is True


def test_detection_manifest_records_squash_by_default():
    """Explicit False, not absent: the manifest states which path was chosen."""
    manifest = _manifest_for(False)
    assert manifest["detection"]["preserve_aspect"] is False


def test_flag_does_not_disturb_the_rest_of_the_detection_block():
    manifest = _manifest_for(True, score_threshold=0.3, iou_threshold=0.5)
    detection = manifest["detection"]
    assert detection["layout"] == "yolo"
    assert detection["network_input"] == 1280
    assert detection["score_threshold"] == 0.3
    assert detection["iou_threshold"] == 0.5
    assert detection["num_classes"] == 1
    assert manifest["task"] == "object_detection"
    assert manifest["runtime"] == "onnx"
    assert manifest["runtime_artifact"] == "model.onnx"


def test_classification_package_gets_no_detection_block():
    """Non-detection models are untouched by this parameter."""
    manifest = _manifest_for(True, model_type="classification")
    assert "detection" not in manifest
    assert "task" not in manifest


# ---------------------------------------------------------------------------
# ... and what the device does with it
# ---------------------------------------------------------------------------

def test_letterbox_flag_reaches_the_device_preprocessor():
    """The whole point: converter output -> device merge -> device lookup."""
    stage = _merge_detection_block_like_the_device(_manifest_for(True))
    assert _device_preserve_aspect(stage) is True


def test_squash_default_reaches_the_device_preprocessor_as_false():
    stage = _merge_detection_block_like_the_device(_manifest_for(False))
    assert _device_preserve_aspect(stage) is False


def test_top_level_preprocessing_block_would_not_have_worked():
    """Guards the trap: the manifest's top-level `preprocessing` block is never
    merged into stages, so putting the flag there is silently ignored. If a
    future change moves it, this test fails and says why."""
    manifest = _manifest_for(False)
    manifest["preprocessing"]["preserve_aspect"] = True
    stage = _merge_detection_block_like_the_device(manifest)
    assert _device_preserve_aspect(stage) is False, (
        "preserve_aspect must go in the top-level 'detection' block, not "
        "'preprocessing' — the latter is not merged into stages")


# ---------------------------------------------------------------------------
# Handler wiring
# ---------------------------------------------------------------------------
# The tests above call generate_dda_package directly, so they cannot see the
# handler dropping the request field on the floor — a mutation that hardcoded
# `preserve_aspect = False` in convert_model passed all of them. These cover
# that seam: request body -> packager kwarg.

def _convert_with(monkeypatch, body_extra):
    """Drive convert_model with its AWS/auth collaborators stubbed, and return
    the kwargs it passed to generate_dda_package."""
    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        Path(kwargs["output_path"]).write_bytes(b"tar")
        return kwargs["output_path"]

    class FakeS3:
        def download_file(self, *a, **k):
            Path(a[2]).write_bytes(b"onnx")

        def upload_file(self, *a, **k):
            pass

    monkeypatch.setattr(model_converter, "generate_dda_package", fake_generate)
    monkeypatch.setattr(model_converter, "get_user_from_event",
                        lambda e: {"user_id": "u1", "email": "u@example.com"})
    monkeypatch.setattr(model_converter, "check_user_access",
                        lambda *a, **k: True)
    monkeypatch.setattr(model_converter, "is_trusted_model_source",
                        lambda *a, **k: True)
    monkeypatch.setattr(model_converter, "get_usecase_details",
                        lambda uid: {"cross_account_role_arn": "arn:role",
                                     "external_id": "x", "s3_bucket": "bucket"})
    monkeypatch.setattr(model_converter, "assume_usecase_role",
                        lambda *a, **k: {})
    monkeypatch.setattr(model_converter, "make_usecase_s3_client",
                        lambda creds: FakeS3())
    monkeypatch.setattr(model_converter, "log_audit_event", lambda **k: None)

    body = {
        "usecase_id": "uc1",
        "model_s3_uri": "s3://bucket/raw-models/bp/model.onnx",
        "model_name": "blue plate yolo",
        "model_type": "object_detection",
        "image_width": 1280,
        "image_height": 1280,
        "num_classes": 1,
        "export_format": "onnx",
        "auto_import": False,
    }
    body.update(body_extra)
    response = model_converter.convert_model({"body": json.dumps(body)}, None)
    assert response["statusCode"] == 200, response
    return captured


def test_handler_forwards_preserve_aspect_true(monkeypatch):
    captured = _convert_with(monkeypatch, {"preserve_aspect": True})
    assert captured["preserve_aspect"] is True


def test_handler_forwards_preserve_aspect_false(monkeypatch):
    captured = _convert_with(monkeypatch, {"preserve_aspect": False})
    assert captured["preserve_aspect"] is False


def test_handler_defaults_preserve_aspect_to_false_when_absent(monkeypatch):
    """Existing callers that never heard of the field keep the old geometry."""
    captured = _convert_with(monkeypatch, {})
    assert captured["preserve_aspect"] is False
