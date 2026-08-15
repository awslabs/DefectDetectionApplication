"""Property test for manifest append preservation (synthetic-defect-data-
generation, task 2.11).

**Feature: synthetic-defect-data-generation, Property 9: Manifest append
preservation**

_For any_ existing Data_Manifest content (including the empty manifest) and
any non-empty approved image set: the appended manifest starts with all
existing records unchanged and in their original order, followed by exactly
one new record per approved image, and every appended record references only
approved images.

**Validates: Requirements 7.4, 7.5, 6.6**

Pure-logic test over synthetic_core.append_manifest_lines /
build_manifest_record / parse_manifest_lines: no AWS mocks.
"""
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from synthetic_core import (
    append_manifest_lines,
    build_manifest_record,
    parse_manifest_lines,
)

# Existing manifest records: DDA-shaped JSON objects with a little
# variability (extra attributes, unicode class names).
json_scalars = st.one_of(
    st.integers(min_value=-1000, max_value=1000),
    st.text(max_size=20),
    st.booleans(),
)

existing_records = st.lists(
    st.fixed_dictionaries(
        {
            "source-ref": st.text(min_size=1, max_size=40).map(
                lambda k: f"s3://bucket/{k}"),
            "anomaly-label": st.sampled_from([0, 1]),
            "anomaly-label-metadata": st.fixed_dictionaries({
                "class-name": st.text(min_size=1, max_size=20),
                "confidence": st.just(1.0),
            }),
        },
        optional={"extra-attribute": json_scalars},
    ),
    max_size=8,
)

approved_image_keys = st.lists(
    st.text(
        alphabet=st.characters(min_codepoint=48, max_codepoint=122,
                               categories=("Lu", "Ll", "Nd")),
        min_size=1, max_size=20),
    min_size=1, max_size=5, unique=True,
)

bboxes = st.fixed_dictionaries({
    "left": st.integers(min_value=0, max_value=500),
    "top": st.integers(min_value=0, max_value=500),
    "width": st.integers(min_value=1, max_value=500),
    "height": st.integers(min_value=1, max_value=500),
})


@st.composite
def existing_manifest_contents(draw):
    """(content, records): serialized JSON Lines, sometimes without the
    trailing newline, including the empty manifest."""
    records = draw(existing_records)
    if not records:
        return draw(st.sampled_from(["", "\n"])), records
    content = "\n".join(json.dumps(r) for r in records)
    if draw(st.booleans()):
        content += "\n"
    return content, records


@settings(deadline=None)
@given(existing=existing_manifest_contents(),
       image_keys=approved_image_keys,
       defect_type=st.text(min_size=1, max_size=20),
       bbox=bboxes)
def test_append_preserves_existing_and_adds_one_record_per_approved_image(
        existing, image_keys, defect_type, bbox):
    """The appended manifest preserves every existing record unchanged and
    in order (byte-for-byte up to trailing-newline normalization), followed
    by exactly one record per approved image, each referencing only approved
    images (Requirements 7.4, 7.5, 6.6)."""
    existing_content, old_records = existing
    session_meta = {"session_id": "session-1",
                    "generation_model_id": "amazon.nova-canvas-v1:0"}

    approved_uris = [
        f"s3://usecase-bucket/datasets/x/synthetic/session-1/{key}.png"
        for key in image_keys
    ]
    new_records = [
        build_manifest_record(
            uri, defect_type, bbox, {"width": 1024, "height": 768},
            session_meta, "resolved prompt", "2025-01-01T00:00:00",
            bbox_source="inpainting_mask")
        for uri in approved_uris
    ]

    result = append_manifest_lines(existing_content, new_records)

    # Existing content preserved byte-for-byte, trailing newline normalized.
    normalized = existing_content
    if normalized and not normalized.endswith("\n"):
        normalized += "\n"
    assert result.startswith(normalized)

    # Parsed view: old records unchanged and in original order, then
    # exactly one new record per approved image, in order.
    parsed = parse_manifest_lines(result)
    assert parsed[:len(old_records)] == old_records
    appended = parsed[len(old_records):]
    assert len(appended) == len(approved_uris)

    # Every appended record references only approved images.
    assert [r["source-ref"] for r in appended] == approved_uris
    assert appended == [json.loads(json.dumps(r)) for r in new_records]
