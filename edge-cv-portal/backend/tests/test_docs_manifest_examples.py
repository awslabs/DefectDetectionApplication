"""
Drift guard for the documented device manifests (spec rfdetr-seg-support,
Requirement 16.6; task 15.3).

`README.md` and `docs/multi-runtime-inference.md` show the manifest the Portal
writes for RF-DETR and RF-DETR-Seg models. Each such example is a ```json
block preceded by an HTML comment naming the builder arguments:

    <!-- manifest: rf_detr <size> -->
    <!-- manifest: rf_detr_seg <instance|semantic> <size> -->

This test extracts every tagged block and asserts it is exactly (keys, order
and values) what `detection_training.build_detection_device_manifest` returns
for the tagged arch / mode / size — the native resolution and query count of
the size from the shared tables — and the example's own `detection` values
(num_classes, class_names, thresholds, output_names, preserve_aspect), minus
the builder's `dataset` block. Any RF-DETR-family example in the two files
that carries no tag fails too, so the `normalize: false` class of drift
(README.md at 43cd4ff1) cannot recur for either arch.
# Validates: Requirements 16.2, 16.6
"""
import json
import os
import re

import pytest

import detection_training as dt

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
DOC_FILES = ("README.md", os.path.join("docs", "multi-runtime-inference.md"))

_TAG_RE = re.compile(r"<!--\s*manifest:\s*(?P<args>[^>]*?)\s*-->")
_BLOCK_RE = re.compile(r"```json\n(?P<body>.*?)\n```", re.DOTALL)
# What must stay documented, per file: (arch, mode) pairs.
REQUIRED_TAGS = {
    "README.md": {("rf_detr", None), ("rf_detr_seg", "instance"), ("rf_detr_seg", "semantic")},
    os.path.join("docs", "multi-runtime-inference.md"): {
        ("rf_detr", None), ("rf_detr_seg", "instance"), ("rf_detr_seg", "semantic")},
}


def _read(rel_path):
    with open(os.path.join(_REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _json_blocks(text):
    """Every ```json block as (start offset, raw body)."""
    return [(m.start(), m.group("body")) for m in _BLOCK_RE.finditer(text)]


def _tag_before(text, block_start):
    """The manifest tag immediately preceding a block (only whitespace between), or None."""
    preceding = text[:block_start].rstrip()
    m = None
    for m in _TAG_RE.finditer(preceding):
        pass
    if m is None or preceding[m.end():].strip():
        return None
    return m.group("args").split()


def _is_rfdetr_family(manifest):
    detection = manifest.get("detection") if isinstance(manifest, dict) else None
    stages = (manifest.get("model_graph") or {}).get("stages") if isinstance(manifest, dict) else None
    layout = (detection or {}).get("layout")
    stage_type = (stages[0].get("type") if isinstance(stages, list) and stages else None) or ""
    return layout == "rf_detr" or stage_type.startswith("rf_detr_")


def _expected_manifest(args, example):
    """The builder's output for the tag's arch / mode / size and the example's
    own detection values, minus `dataset`."""
    arch = args[0]
    det = example["detection"]
    common = dict(
        num_classes=det["num_classes"],
        class_names=det.get("class_names"),
        score_threshold=det["score_threshold"],
        preserve_aspect=det["preserve_aspect"],
        detection_arch=arch,
    )
    if arch == "rf_detr":
        (size,) = args[1:]
        image = dt.RFDETR_SIZES[size]
        manifest = dt.build_detection_device_manifest(
            image_width=image, image_height=image, top_k=dt.RFDETR_TOP_K, **common)
    elif arch == "rf_detr_seg":
        mode, size = args[1:]
        assert mode in dt.OUTPUT_MODES, f"unknown output mode in tag {args}"
        image = dt.RFDETR_SEG_SIZES[size]
        manifest = dt.build_detection_device_manifest(
            image_width=image, image_height=image,
            top_k=dt.RFDETR_SEG_QUERIES_FOR_SIZE[size],
            output_mode=mode,
            mask_threshold=det["mask_threshold"],
            output_names=det.get("output_names"),
            **common)
    else:
        raise AssertionError(f"unsupported manifest tag {args}: expected rf_detr or rf_detr_seg")
    manifest.pop("dataset")
    return manifest


def _tagged_examples(rel_path):
    text = _read(rel_path)
    found = []
    for start, body in _json_blocks(text):
        args = _tag_before(text, start)
        try:
            parsed = json.loads(body)
        except ValueError:
            assert args is None, f"{rel_path}: tagged block {args} is not valid JSON"
            continue
        if args is None:
            assert not _is_rfdetr_family(parsed), (
                f"{rel_path}: an RF-DETR-family manifest example without a "
                f"`<!-- manifest: ... -->` tag (line {text.count(chr(10), 0, start) + 1}); "
                "tag it so it is checked against the shared builder (Requirement 16.6)")
            continue
        found.append((args, parsed, text.count("\n", 0, start) + 1))
    return found


_CASES = [(rel_path, args, parsed, line)
          for rel_path in DOC_FILES
          for args, parsed, line in _tagged_examples(rel_path)]


@pytest.mark.parametrize(
    "rel_path, args, example, line", _CASES,
    ids=[f"{os.path.basename(p)}:{l}:{' '.join(a)}" for p, a, _, l in _CASES])
def test_tagged_example_equals_the_shared_builder(rel_path, args, example, line):
    expected = _expected_manifest(args, example)
    # Keys, order and values: the example is the builder's output verbatim.
    assert json.dumps(example, indent=2) == json.dumps(expected, indent=2), (
        f"{rel_path} line {line}: the `{' '.join(args)}` example differs from "
        "build_detection_device_manifest; regenerate it from the builder")


@pytest.mark.parametrize("rel_path", DOC_FILES, ids=[os.path.basename(p) for p in DOC_FILES])
def test_every_required_example_is_tagged(rel_path):
    present = {(args[0], args[1] if args[0] == "rf_detr_seg" else None)
               for args, _, _ in _tagged_examples(rel_path)}
    missing = REQUIRED_TAGS[rel_path] - present
    assert not missing, f"{rel_path}: untagged or missing manifest examples for {sorted(missing)}"


def test_the_tag_grammar_rejects_an_unknown_arch():
    with pytest.raises(AssertionError, match="unsupported manifest tag"):
        _expected_manifest(["yolo", "small"], {"detection": {
            "num_classes": 1, "score_threshold": 0.25, "preserve_aspect": True}})


def test_a_drifted_normalize_flag_is_caught():
    """The 43cd4ff1 README drift: `normalize: false` on an RF-DETR stage."""
    args = ["rf_detr", "small"]
    good = _expected_manifest(args, {"detection": {
        "num_classes": 2, "class_names": ["plate", "scratch"],
        "score_threshold": 0.5, "preserve_aspect": False}})
    assert good["model_graph"]["stages"][0]["normalize"] is True
    drifted = json.loads(json.dumps(good))
    drifted["model_graph"]["stages"][0]["normalize"] = False
    assert json.dumps(drifted, indent=2) != json.dumps(_expected_manifest(args, drifted), indent=2)
