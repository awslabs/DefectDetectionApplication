# SPDX-License-Identifier: Apache-2.0
"""
`dda_manifest` Instance_Manifest codec (rfdetr-seg-support task 3.4;
Requirements 2.2, 3.1, 3.2, 3.6, 10.2; design sections 2 and 3, DD5, DD9).

Pure functions of the shared layer, no AWS:

- serialize -> parse round trip of an InstanceSegmentation record through
  `serialize_manifest` / `parse_manifest` (the first caller of
  `_parse_instance_segmentation`), and the exact line shape of Req 3.1:
  `source-ref`, `instance-segmentation` {image_size, annotations with one
  `rle` each and the tight box}, `instance-segmentation-metadata`; never an
  `anomaly-mask-ref` / `anomaly-mask-ref-metadata` key, never a PNG.
- Each ValueError of task 3.1: a class outside the Label_Set, a stored box
  outside the image, counts not summing to width*height, a non-integer or
  negative count, an all-background mask, a missing rle, a parse of an
  unknown class_id.
- The written box is always the tight box derived from the rle, whatever
  box the stored annotation carried (DD9: "derived, never typed").
- `tight_box_from_rle`: border-touching, column-crossing run, hollow,
  single pixel, all-background, and a brute-force comparison against a
  pixel-scan oracle over random small masks.
- The shared vector file `test/fixtures/canonical_rle_vectors.json` --
  created with `dda_manifest.rle_encode` as the oracle -- is re-derived
  here (DD5), so the device codec's byte-identity pin in
  `test/backend-test/test_mask_rle.py` is anchored to this module.
- The three existing modalities' serializers are untouched: an
  ObjectDetection and a Segmentation line carry no instance key.

conftest.py places the shared layer on sys.path for `dda_manifest`.
"""
import json
import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import dda_manifest as dm

_HERE = os.path.dirname(os.path.abspath(__file__))
#: Repo-root `test/fixtures/`, shared with the device suite and the frontend.
VECTORS_PATH = os.path.abspath(os.path.join(
    _HERE, "..", "..", "..", "test", "fixtures", "canonical_rle_vectors.json"))

LABEL_SET = ["scratch", "dent"]
JOB = {"job_name": "labeling-job/inst-1", "modality": "InstanceSegmentation",
       "label_set": LABEL_SET}
WIDTH, HEIGHT = 6, 4


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _mask(pixels, width=WIDTH, height=HEIGHT):
    """Row-major 0/1 list with the given (x, y) pixels set."""
    flat = [0] * (width * height)
    for x, y in pixels:
        flat[y * width + x] = 1
    return flat


def _rle(pixels, width=WIDTH, height=HEIGHT):
    return dm.rle_encode(_mask(pixels, width, height), width, height)


def _bbox_oracle(flat, width, height):
    """Tight box by scanning every pixel; None when all background."""
    xs = [p % width for p, v in enumerate(flat) if v]
    ys = [p // width for p, v in enumerate(flat) if v]
    if not xs:
        return None
    return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


def _instance(class_name, rle, box=None):
    inst = {"class": class_name, "rle": rle}
    if box is not None:
        inst["box"] = box
    return inst


def _record(name, instances, width=WIDTH, height=HEIGHT, **extra):
    record = {
        "source_ref": f"s3://example-bucket/frames/{name}",
        "annotation": {
            "modality": "InstanceSegmentation",
            "image_size": {"width": width, "height": height},
            "instances": instances,
            "classification": "anomaly" if instances else "normal",
        },
        "human_annotated": True,
        "creation_date": "2026-11-22T10:00:00Z",
    }
    record.update(extra)
    return record


# Two instances: a 2x2 block and a single pixel, both in bounds.
BLOCK = [(1, 1), (2, 1), (1, 2), (2, 2)]
DOT = [(5, 3)]


# ---------------------------------------------------------------------------
# Serialize -> parse round trip and the line shape (Req 3.1)
# ---------------------------------------------------------------------------

def test_serialize_writes_the_instance_manifest_line_shape():
    record = _record("a.jpg", [_instance("dent", _rle(BLOCK)),
                               _instance("scratch", _rle(DOT))])

    lines = dm.serialize_manifest([record], JOB)

    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert set(entry) == {"source-ref", "instance-segmentation",
                          "instance-segmentation-metadata"}
    assert entry["source-ref"] == record["source_ref"]
    seg = entry["instance-segmentation"]
    assert seg["image_size"] == [{"width": WIDTH, "height": HEIGHT, "depth": 3}]
    assert seg["annotations"] == [
        {"class_id": 1, "left": 1, "top": 1, "width": 2, "height": 2,
         "rle": _rle(BLOCK)},
        {"class_id": 0, "left": 5, "top": 3, "width": 1, "height": 1,
         "rle": _rle(DOT)},
    ]
    meta = entry["instance-segmentation-metadata"]
    assert meta == {
        "objects": [{"confidence": 1.0}, {"confidence": 1.0}],
        "class-map": {"0": "scratch", "1": "dent"},
        "type": "groundtruth/instance-segmentation",
        "human-annotated": "yes",
        "creation-date": "2026-11-22T10:00:00Z",
        "job-name": "labeling-job/inst-1",
    }


def test_serialize_never_emits_a_semantic_mask_key_or_png():
    """The LFV `segmentation` validator keys on anomaly-mask-ref; an
    instance line must not look like a semantic manifest (Req 3.1, 3.2)."""
    record = _record("a.jpg", [_instance("dent", _rle(BLOCK))],
                     mask_s3_uri="s3://example-bucket/masks/a.png")

    entry = json.loads(dm.serialize_manifest([record], JOB)[0])

    for key in entry:
        assert not key.startswith("anomaly-"), key
    assert "anomaly-mask-ref" not in json.dumps(entry)
    assert "png" not in json.dumps(entry).lower()


def test_negative_record_serializes_with_no_annotations():
    entry = json.loads(dm.serialize_manifest([_record("neg.jpg", [])], JOB)[0])

    assert entry["instance-segmentation"]["annotations"] == []
    assert entry["instance-segmentation-metadata"]["objects"] == []
    assert entry["instance-segmentation-metadata"]["class-map"] == {
        "0": "scratch", "1": "dent"}


def test_serialize_then_parse_round_trips():
    """`parse_manifest` dispatches on InstanceSegmentation; the parsed record
    carries the derived box, the verbatim rle and source_ref, and the
    derived classification (anomaly iff instances)."""
    records = [
        _record("a.jpg", [_instance("dent", _rle(BLOCK)),
                          _instance("scratch", _rle(DOT))]),
        _record("neg.jpg", []),
    ]

    parsed = dm.parse_manifest(dm.serialize_manifest(records, JOB),
                               "InstanceSegmentation")

    assert [p["source_ref"] for p in parsed] == [r["source_ref"] for r in records]
    first = parsed[0]
    assert first["annotation"] == {
        "modality": "InstanceSegmentation",
        "image_size": {"width": WIDTH, "height": HEIGHT},
        "instances": [
            {"class": "dent", "rle": _rle(BLOCK),
             "box": {"left": 1, "top": 1, "width": 2, "height": 2}},
            {"class": "scratch", "rle": _rle(DOT),
             "box": {"left": 5, "top": 3, "width": 1, "height": 1}},
        ],
        "classification": "anomaly",
    }
    assert first["human_annotated"] is True
    assert first["confidence"] == 1.0
    assert first["creation_date"] == "2026-11-22T10:00:00Z"
    assert parsed[1]["annotation"]["instances"] == []
    assert parsed[1]["annotation"]["classification"] == "normal"
    # A negative has no `objects` entry to read a confidence from, exactly as
    # `_parse_object_detection` reports it.
    assert parsed[1]["confidence"] is None

    # Re-serializing the parsed positive record reproduces its line byte for byte.
    assert dm.serialize_manifest([first], JOB) == dm.serialize_manifest([records[0]], JOB)


def test_machine_record_markers_round_trip():
    """skip-verification style record: human-annotated 'no' and the stored
    confidence survive the round trip (Req 2.5 markers apply unchanged)."""
    record = _record("m.jpg", [_instance("dent", _rle(BLOCK))],
                     human_annotated=False, confidence=0.73)

    entry = json.loads(dm.serialize_manifest([record], JOB)[0])
    assert entry["instance-segmentation-metadata"]["human-annotated"] == "no"
    assert entry["instance-segmentation-metadata"]["objects"] == [
        {"confidence": 0.73}]

    parsed = dm.parse_manifest([json.dumps(entry)], "InstanceSegmentation")[0]
    assert parsed["human_annotated"] is False
    assert parsed["confidence"] == 0.73


def test_written_box_is_the_tight_box_whatever_the_stored_box_says():
    """DD9: the serializer re-derives the box; a stale (but in-bounds) client
    box is overwritten, never trusted."""
    stale = {"left": 0, "top": 0, "width": 3, "height": 3}
    record = _record("a.jpg", [_instance("dent", _rle(BLOCK), box=stale)])

    entry = json.loads(dm.serialize_manifest([record], JOB)[0])

    ann = entry["instance-segmentation"]["annotations"][0]
    assert (ann["left"], ann["top"], ann["width"], ann["height"]) == (1, 1, 2, 2)


# ---------------------------------------------------------------------------
# Each ValueError of task 3.1
# ---------------------------------------------------------------------------

def test_class_outside_the_label_set_raises():
    record = _record("a.jpg", [_instance("rust", _rle(BLOCK))])
    with pytest.raises(ValueError, match="not in the Label_Set"):
        dm.serialize_manifest([record], JOB)


def test_stored_box_outside_the_image_raises():
    outside = {"left": 4, "top": 2, "width": 5, "height": 1}  # 4 + 5 > 6
    record = _record("a.jpg", [_instance("dent", _rle(BLOCK), box=outside)])
    with pytest.raises(ValueError, match="outside the image bounds"):
        dm.serialize_manifest([record], JOB)


def test_counts_not_summing_to_width_times_height_raises():
    record = _record("a.jpg", [_instance("dent", "3 2 4")])  # sums to 9, not 24
    with pytest.raises(ValueError, match=r"sum 9 != width\*height 24"):
        dm.serialize_manifest([record], JOB)


def test_non_integer_count_raises():
    record = _record("a.jpg", [_instance("dent", "3 x 21")])
    with pytest.raises(ValueError, match="invalid RLE counts"):
        dm.serialize_manifest([record], JOB)


def test_negative_count_raises():
    record = _record("a.jpg", [_instance("dent", "30 -6")])
    with pytest.raises(ValueError, match="non-negative"):
        dm.serialize_manifest([record], JOB)


def test_all_background_mask_raises():
    record = _record("a.jpg", [_instance("dent", _rle([]))])
    with pytest.raises(ValueError, match="no foreground pixels"):
        dm.serialize_manifest([record], JOB)


def test_missing_rle_raises():
    record = _record("a.jpg", [{"class": "dent"}])
    with pytest.raises(ValueError, match="no RLE mask"):
        dm.serialize_manifest([record], JOB)


def test_record_modality_mismatch_raises():
    record = _record("a.jpg", [_instance("dent", _rle(BLOCK))])
    record["annotation"]["modality"] = "Segmentation"
    with pytest.raises(ValueError, match="does not match"):
        dm.serialize_manifest([record], JOB)


def test_parse_rejects_a_class_id_outside_the_class_map():
    entry = json.loads(dm.serialize_manifest(
        [_record("a.jpg", [_instance("dent", _rle(BLOCK))])], JOB)[0])
    entry["instance-segmentation"]["annotations"][0]["class_id"] = 7
    with pytest.raises(ValueError, match="class_id 7 is not in the class-map"):
        dm.parse_manifest([json.dumps(entry)], "InstanceSegmentation")


def test_parse_reports_the_line_of_a_malformed_entry():
    entry = {"source-ref": "s3://example-bucket/x.jpg"}  # no instance attribute
    with pytest.raises(ValueError, match="line 1: missing or invalid field"):
        dm.parse_manifest([json.dumps(entry)], "InstanceSegmentation")


# ---------------------------------------------------------------------------
# tight_box_from_rle (DD9)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, pixels, expected", [
    ("border_touching", [(0, 0), (5, 0), (0, 3), (5, 3)], (0, 0, 6, 4)),
    # Bottom of column 1 (rows 2, 3) and top of column 2 (rows 0, 1) are one
    # column-major run: rows span the full height, columns 1..2.
    ("column_crossing_run", [(1, 2), (1, 3), (2, 0), (2, 1)], (1, 0, 2, 4)),
    ("hollow", [(1, 0), (2, 0), (3, 0), (1, 1), (3, 1), (1, 2), (2, 2), (3, 2)],
     (1, 0, 3, 3)),
    ("single_pixel", [(4, 2)], (4, 2, 1, 1)),
    ("full", [(x, y) for x in range(WIDTH) for y in range(HEIGHT)],
     (0, 0, WIDTH, HEIGHT)),
])
def test_tight_box_from_rle_cases(name, pixels, expected):
    assert dm.tight_box_from_rle(_rle(pixels), WIDTH, HEIGHT) == expected
    assert _bbox_oracle(_mask(pixels), WIDTH, HEIGHT) == expected, name


def test_tight_box_from_rle_all_background_raises():
    with pytest.raises(ValueError, match="no foreground pixels"):
        dm.tight_box_from_rle(_rle([]), WIDTH, HEIGHT)


def test_tight_box_from_rle_applies_the_rle_decode_rules():
    with pytest.raises(ValueError, match="invalid RLE counts"):
        dm.tight_box_from_rle("1 a", 2, 2)
    with pytest.raises(ValueError, match="non-negative"):
        dm.tight_box_from_rle("5 -1", 2, 2)
    with pytest.raises(ValueError, match=r"sum 3 != width\*height 4"):
        dm.tight_box_from_rle("1 2", 2, 2)
    with pytest.raises(ValueError, match="positive"):
        dm.tight_box_from_rle("0", 0, 4)


def test_rle_decode_still_raises_its_three_errors():
    """`_rle_counts` was factored out of `rle_decode`; behaviour unchanged."""
    with pytest.raises(ValueError, match="invalid RLE counts"):
        dm.rle_decode("1 a", 2, 2)
    with pytest.raises(ValueError, match="non-negative"):
        dm.rle_decode("5 -1", 2, 2)
    with pytest.raises(ValueError, match=r"sum 3 != width\*height 4"):
        dm.rle_decode("1 2", 2, 2)


@settings(max_examples=200, deadline=None)
@given(
    width=st.integers(min_value=1, max_value=9),
    height=st.integers(min_value=1, max_value=9),
    data=st.data(),
)
def test_property_tight_box_matches_a_pixel_scan(width, height, data):
    """For any mask with at least one foreground pixel, the box derived from
    the runs alone equals the box from scanning every pixel."""
    flat = data.draw(st.lists(st.integers(min_value=0, max_value=1),
                              min_size=width * height, max_size=width * height))
    rle = dm.rle_encode(flat, width, height)
    expected = _bbox_oracle(flat, width, height)
    if expected is None:
        with pytest.raises(ValueError, match="no foreground pixels"):
            dm.tight_box_from_rle(rle, width, height)
    else:
        assert dm.tight_box_from_rle(rle, width, height) == expected


# ---------------------------------------------------------------------------
# The shared Canonical_RLE vector file (DD5, Req 10.2)
# ---------------------------------------------------------------------------

def _load_vectors():
    with open(VECTORS_PATH) as fh:
        doc = json.load(fh)
    return doc["vectors"]


def _vector_mask(vector):
    rows = vector["mask_rows"]
    assert len(rows) == vector["height"]
    assert all(len(r) == vector["width"] for r in rows)
    return [1 if ch == "1" else 0 for row in rows for ch in row]


def test_vector_file_covers_the_named_mask_classes():
    names = {v["name"] for v in _load_vectors()}
    assert {"border_touching", "hollow", "empty", "full",
            "single_pixel"} <= names


@pytest.mark.parametrize("vector", _load_vectors(), ids=lambda v: v["name"])
def test_vector_rle_is_what_rle_encode_produces(vector):
    """`dda_manifest.rle_encode` is the oracle the file was written with; the
    device codec (`test/backend-test/test_mask_rle.py`) pins against the same
    strings, so the two encoders stay byte-identical."""
    flat = _vector_mask(vector)
    assert dm.rle_encode(flat, vector["width"], vector["height"]) == vector["rle"]
    assert list(dm.rle_decode(vector["rle"], vector["width"],
                              vector["height"])) == flat
    counts = [int(c) for c in vector["rle"].split()]
    assert sum(counts) == vector["width"] * vector["height"]


# ---------------------------------------------------------------------------
# The other modalities carry no instance key (Req 3.2 preservation)
# ---------------------------------------------------------------------------

def test_object_detection_line_is_unchanged_by_the_new_modality():
    job = {"job_name": "j", "modality": "ObjectDetection", "label_set": LABEL_SET}
    record = {
        "source_ref": "s3://example-bucket/frames/a.jpg",
        "annotation": {"modality": "ObjectDetection",
                       "image_size": {"width": WIDTH, "height": HEIGHT},
                       "boxes": [{"class": "dent", "left": 1, "top": 1,
                                  "width": 2, "height": 2}]},
        "human_annotated": True, "creation_date": "2026-11-22T10:00:00Z",
    }
    entry = json.loads(dm.serialize_manifest([record], job)[0])
    assert set(entry) == {"source-ref", "bounding-box", "bounding-box-metadata"}
    assert "rle" not in json.dumps(entry)


def test_unsupported_modality_still_rejected():
    with pytest.raises(ValueError, match="unsupported modality"):
        dm.serialize_manifest([], {"job_name": "j", "modality": "Instances",
                                   "label_set": LABEL_SET})
    with pytest.raises(ValueError, match="unsupported modality"):
        dm.parse_manifest([], "Instances")
