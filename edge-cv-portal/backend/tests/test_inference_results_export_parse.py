"""
Portal-side schema round trip of the device's instance-manifest export
(rfdetr-seg-support task 11.4, Requirements 11.4, 11.5; design §11 "Export",
C7): every line `src/backend/utils/inference_results_utils.generate_instance_manifest`
writes is accepted by the Portal layer's `dda_manifest._parse_instance_segmentation`
and carries each stored `mask_rle` verbatim, so an exported result zip can seed
an `InstanceSegmentation` labeling job with lossless masks.

The REAL edge module is loaded by path the way
test_camera_shadow_sync_integration.py loads edge packages
(`sys.path.insert(0, _SRC_BACKEND)` around the import, then detached from
sys.modules). Its import chain reaches device-only neighbours this test image
does not carry -- `fastapi` (HTTPException), `utils.utils` (Starlette,
marshmallow) and `utils.feature_configs_utils` (Greengrass IPC, Triton) -- so
those three are stood in by empty modules for the import; the manifest writers
use none of them. `utils.constants` is the real file. conftest.py places the
shared layer on sys.path for `dda_manifest`.
"""
import json
import os
import pathlib
import sys
import types

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

import dda_manifest as dm

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SRC_BACKEND = _REPO_ROOT / "src" / "backend"


# --- the real edge module (loaded by path) -----------------------------------
def _load_edge_inference_results_utils():
    saved = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == "utils" or name.startswith("utils.")
    }
    added_fastapi = False
    if "fastapi" not in sys.modules:
        fastapi_stub = types.ModuleType("fastapi")
        fastapi_stub.HTTPException = type("HTTPException", (Exception,), {})
        sys.modules["fastapi"] = fastapi_stub
        added_fastapi = True
    sys.path.insert(0, str(_SRC_BACKEND))
    try:
        import utils as edge_utils  # the real package (license header only)

        utils_stub = types.ModuleType("utils.utils")
        fcu_stub = types.ModuleType("utils.feature_configs_utils")
        fcu_stub.get_default_configs_lfv = lambda *args, **kwargs: {}
        sys.modules["utils.utils"] = utils_stub
        sys.modules["utils.feature_configs_utils"] = fcu_stub
        edge_utils.utils = utils_stub
        edge_utils.feature_configs_utils = fcu_stub

        from utils import inference_results_utils
    finally:
        for name in list(sys.modules):
            if name == "utils" or name.startswith("utils."):
                del sys.modules[name]
        sys.modules.update(saved)
        if added_fastapi:
            del sys.modules["fastapi"]
        sys.path.remove(str(_SRC_BACKEND))
    return inference_results_utils


iru = _load_edge_inference_results_utils()

WIDTH, HEIGHT = 6, 4
CREATED = 1700000000  # 10-digit epoch seconds -> "2023-11-14T22:13:20"


# --- builders ----------------------------------------------------------------
def _mask(pixels, width=WIDTH, height=HEIGHT):
    flat = [0] * (width * height)
    for x, y in pixels:
        flat[y * width + x] = 1
    return flat


def _entry(index, label, box, confidence, mask=None, truncated=False, area=None):
    entry = {"class_index": str(index), "class_label": label, "bounding_box": box,
             "confidence": confidence}
    if mask is not None:
        entry["mask_rle"] = dm.rle_encode(mask, WIDTH, HEIGHT)
        entry["mask_area"] = sum(mask)
    if area is not None:
        entry["mask_area"] = area
    if truncated:
        entry["mask_truncated"] = True
    return entry


def _row(image_path, detections, **overrides):
    row = {"inputImageFilePath": image_path, "prediction": "detection", "confidence": 0.9,
           "inferenceCreationTime": CREATED, "humanClassification": None, "textNote": None,
           "detections": detections}
    row.update(overrides)
    return row


@pytest.fixture
def image_path(tmp_path):
    path = tmp_path / "cap-1.jpg"
    Image.new("RGB", (WIDTH, HEIGHT)).save(path)
    return str(path)


SCRATCH_MASK = _mask([(1, 1), (2, 1), (1, 2), (2, 2)])          # 2x2 block
DENT_MASK = _mask([(4, 0), (5, 0), (4, 3)])                      # two corners
RIM_MASK = _mask([(0, 0), (0, 1), (0, 2), (0, 3)])               # left column


def _jsonl_round_trip(line):
    """One manifest line as the export zip carries it: a JSON text line."""
    return json.loads(json.dumps(line))


# --- the round trip -----------------------------------------------------------
class TestRoundTrip:
    def test_every_line_parses_and_carries_the_stored_rle_verbatim(self, image_path):
        scratch = _entry(0, "scratch", [1, 1, 3, 3], 0.9, mask=SCRATCH_MASK)
        dent = _entry(2, "dent", [4, 0, 6, 4], 0.6, mask=DENT_MASK)
        shed = _entry(0, "scratch", [0, 0, 1, 1], 0.5, truncated=True, area=1)
        plain = _entry(7, "truck", [1, 2, 3, 4], 0.8)
        rows = [_row(image_path, [dent, scratch, shed, plain])]

        lines = iru.generate_instance_manifest(rows)

        assert len(lines) == 1
        parsed = dm._parse_instance_segmentation(_jsonl_round_trip(lines[0]))
        assert parsed["source_ref"] == os.path.basename(image_path)
        assert parsed["annotation"]["modality"] == dm.MODALITY_INSTANCE_SEGMENTATION
        assert parsed["annotation"]["image_size"] == {"width": WIDTH, "height": HEIGHT}
        assert parsed["annotation"]["classification"] == dm.ANOMALY_LABEL
        assert parsed["human_annotated"] is False
        assert parsed["confidence"] == 0.6                      # the first annotation's (dent)
        assert parsed["creation_date"] == iru.convert_timestamp(CREATED)

        instances = parsed["annotation"]["instances"]
        # Mask-carrying entries only (the truncated and the plain entry yield nothing), in order.
        assert [i["class"] for i in instances] == ["dent", "scratch"]
        assert [i["rle"] for i in instances] == [dent["mask_rle"], scratch["mask_rle"]]
        assert instances[0]["box"] == {"left": 4, "top": 0, "width": 2, "height": 4}
        assert instances[1]["box"] == {"left": 1, "top": 1, "width": 2, "height": 2}
        # Each RLE is schema-valid at the image size and decodes to the stored mask.
        for instance, mask in zip(instances, (DENT_MASK, SCRATCH_MASK)):
            assert dm._rle_counts(instance["rle"], WIDTH, HEIGHT)
            assert list(dm.rle_decode(instance["rle"], WIDTH, HEIGHT)) == mask

    def test_class_map_is_export_wide_and_resolves_for_every_line(self, image_path):
        rows = [
            _row(image_path, [_entry(0, "scratch", [1, 1, 3, 3], 0.9, mask=SCRATCH_MASK)]),
            _row(image_path, [_entry(2, "dent", [4, 0, 6, 4], 0.6, mask=DENT_MASK),
                              _entry(5, "rim", [0, 0, 1, 4], 0.7, mask=RIM_MASK)]),
        ]
        lines = iru.generate_instance_manifest(rows)
        assert len(lines) == 2
        class_maps = {json.dumps(l["instance-segmentation-metadata"]["class-map"], sort_keys=True)
                      for l in lines}
        assert class_maps == {json.dumps({"0": "dent", "1": "rim", "2": "scratch"}, sort_keys=True)}
        parsed = [dm._parse_instance_segmentation(_jsonl_round_trip(l)) for l in lines]
        assert [[i["class"] for i in p["annotation"]["instances"]] for p in parsed] == \
            [["scratch"], ["dent", "rim"]]
        assert [i["rle"] for i in parsed[1]["annotation"]["instances"]] == \
            [rows[1]["detections"][0]["mask_rle"], rows[1]["detections"][1]["mask_rle"]]

    def test_rows_without_a_mask_carrying_entry_yield_no_line(self, image_path):
        rows = [
            _row(image_path, None, prediction="anomaly"),                                 # classification row
            _row(image_path, []),                                                          # zero instances
            _row(image_path, [_entry(0, "scratch", [0, 0, 1, 1], 0.5, truncated=True, area=1),
                              _entry(1, "dent", [1, 1, 2, 2], 0.4, truncated=True, area=1)]),  # all truncated
            _row(image_path, [_entry(7, "truck", [1, 2, 3, 4], 0.8)]),                     # detection-only
            _row(image_path, [_entry(0, "scratch", [1, 1, 3, 3], 0.9, mask=SCRATCH_MASK)]),
        ]
        lines = iru.generate_instance_manifest(rows)
        assert len(lines) == 1
        parsed = dm._parse_instance_segmentation(_jsonl_round_trip(lines[0]))
        assert parsed["annotation"]["instances"][0]["rle"] == rows[4]["detections"][0]["mask_rle"]

    def test_line_shape_matches_the_requirement_3_format(self, image_path):
        rows = [_row(image_path, [_entry(0, "scratch", [1, 1, 3, 3], 0.9, mask=SCRATCH_MASK)])]
        line = _jsonl_round_trip(iru.generate_instance_manifest(rows)[0])
        assert set(line) == {"source-ref", "instance-segmentation", "instance-segmentation-metadata"}
        assert line["instance-segmentation"]["image_size"] == [{"width": WIDTH, "height": HEIGHT, "depth": 3}]
        assert set(line["instance-segmentation"]["annotations"][0]) == \
            {"class_id", "left", "top", "width", "height", "rle"}
        metadata = line["instance-segmentation-metadata"]
        assert metadata["type"] == "groundtruth/instance-segmentation"
        assert metadata["human-annotated"] == "no"
        assert metadata["job-name"] == iru.INSTANCE_MANIFEST_JOB_NAME
        assert metadata["objects"] == [{"confidence": 0.9}]

    def test_classification_export_ignores_the_detections_key(self, image_path):
        with_detections = [_row(image_path, [_entry(0, "scratch", [1, 1, 3, 3], 0.9, mask=SCRATCH_MASK)],
                                prediction="anomaly", textNote="note")]
        without = [{k: v for k, v in with_detections[0].items() if k != "detections"}]
        assert json.dumps(iru.generate_smgt_format_manifest(with_detections)) == \
            json.dumps(iru.generate_smgt_format_manifest(without))

    @settings(max_examples=25, deadline=None)
    @given(bits=st.lists(st.booleans(), min_size=WIDTH * HEIGHT, max_size=WIDTH * HEIGHT).filter(any))
    def test_any_mask_survives_the_export_and_parse(self, tmp_path_factory, bits):
        folder = tmp_path_factory.mktemp("mask")
        path = folder / "cap.jpg"
        Image.new("RGB", (WIDTH, HEIGHT)).save(path)
        mask = [int(b) for b in bits]
        rows = [_row(str(path), [_entry(3, "blob", [0, 0, WIDTH, HEIGHT], 0.75, mask=mask)])]

        line = _jsonl_round_trip(iru.generate_instance_manifest(rows)[0])
        parsed = dm._parse_instance_segmentation(line)

        instance = parsed["annotation"]["instances"][0]
        assert instance["rle"] == dm.rle_encode(mask, WIDTH, HEIGHT)
        assert list(dm.rle_decode(instance["rle"], WIDTH, HEIGHT)) == mask
        assert parsed["confidence"] == 0.75


def test_the_edge_module_is_the_real_file():
    assert pathlib.Path(iru.__file__).resolve() == (_SRC_BACKEND / "utils" / "inference_results_utils.py").resolve()
    # The import left no edge `utils` package behind for the Portal suites.
    assert "utils.inference_results_utils" not in sys.modules
