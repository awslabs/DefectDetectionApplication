"""Property test for manifest record validity round trip (synthetic-defect-
data-generation, task 2.12).

**Feature: synthetic-defect-data-generation, Property 10: Manifest record
validity round trip**

_For any_ approved synthetic image and its session metadata: the produced
manifest record, serialized and re-parsed as a JSON line, passes the
Training_Subsystem's manifest validation requirements (string `source-ref`,
numeric `anomaly-label`, object `anomaly-label-metadata`) and contains the
Defect_Type class label, a bounding box annotation, the synthetic metadata
attribute marking the record as synthetic, the Generation_Model identifier,
and the resolved prompt text used to produce the image.

**Validates: Requirements 7.1, 7.4, 7.8, 10.3**

Pure-logic test over synthetic_core.build_manifest_record. Per the design's
Testing Strategy, assertions run against a local mirror of
training.py::validate_marketplace_manifest's required-attribute/type rules
(string source-ref, numeric anomaly-label, dict metadata) so the round trip
is validated against the real Training_Subsystem rules without importing
the AWS-heavy training module. No AWS mocks.
"""
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from synthetic_core import build_manifest_record

# ---------------------------------------------------------------------------
# Local mirror of training.py::validate_marketplace_manifest's
# required-attribute and type rules for classification manifests
# (training.py lines ~146-224).
# ---------------------------------------------------------------------------

REQUIRED_ATTRS = ["source-ref", "anomaly-label", "anomaly-label-metadata"]


def validate_entry_like_training_subsystem(entry):
    """Mirror of the Training_Subsystem's per-entry manifest rules; returns
    a list of errors (empty means valid)."""
    errors = []
    for attr in REQUIRED_ATTRS:
        if attr not in entry:
            errors.append(f"Missing required attribute: {attr}")
    if errors:
        return errors
    if not isinstance(entry.get("source-ref"), str):
        errors.append("source-ref must be a string (S3 URI)")
    if not isinstance(entry.get("anomaly-label"), (int, float)):
        errors.append("anomaly-label must be a number (0 or 1)")
    if not isinstance(entry.get("anomaly-label-metadata"), dict):
        errors.append("anomaly-label-metadata must be an object")
    # Classification training with segmentation fields is rejected.
    if "anomaly-mask-ref" in entry:
        errors.append("manifest contains segmentation fields")
    return errors


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

s3_keys = st.text(
    alphabet=st.characters(min_codepoint=45, max_codepoint=122,
                           categories=("Lu", "Ll", "Nd"),
                           include_characters="-_/."),
    min_size=1, max_size=40,
)
image_uris = s3_keys.map(lambda k: f"s3://usecase-bucket/{k}.png")

defect_types = st.text(min_size=1, max_size=30).filter(lambda s: s.strip())

bboxes = st.fixed_dictionaries({
    "left": st.integers(min_value=0, max_value=2000),
    "top": st.integers(min_value=0, max_value=2000),
    "width": st.integers(min_value=1, max_value=2000),
    "height": st.integers(min_value=1, max_value=2000),
})

image_sizes = st.fixed_dictionaries(
    {
        "width": st.integers(min_value=1, max_value=4096),
        "height": st.integers(min_value=1, max_value=4096),
    },
    optional={"depth": st.sampled_from([1, 3, 4])},
)

session_metas = st.fixed_dictionaries({
    "session_id": st.uuids().map(str),
    "generation_model_id": st.sampled_from([
        "amazon.nova-canvas-v1:0", "amazon.titan-image-generator-v2:0",
    ]),
})

resolved_prompts = st.text(min_size=1, max_size=300)

timestamps = st.one_of(
    st.just("2025-01-01T00:00:00.000000"),
    st.integers(min_value=0, max_value=4_000_000_000),
)

bbox_sources = st.sampled_from(
    ["inpainting_mask", "image_diff", "full_image"])


@settings(deadline=None)
@given(image_uri=image_uris, defect_type=defect_types, bbox=bboxes,
       image_size=image_sizes, session_meta=session_metas,
       prompt=resolved_prompts, timestamp=timestamps,
       bbox_source=bbox_sources)
def test_record_round_trips_and_passes_training_validation(
        image_uri, defect_type, bbox, image_size, session_meta, prompt,
        timestamp, bbox_source):
    """The record, serialized as a JSON line and re-parsed, passes the
    Training_Subsystem's required-attribute/type rules and carries the class
    label, bounding box, synthetic marker, model id, and resolved prompt
    (Requirements 7.1, 7.4, 7.8, 10.3)."""
    record = build_manifest_record(
        image_uri, defect_type, bbox, image_size, session_meta, prompt,
        timestamp, bbox_source=bbox_source)

    # Round trip: serialize as one JSON line and re-parse (Req 7.8).
    line = json.dumps(record)
    assert "\n" not in line, "a manifest record must be a single JSON line"
    parsed = json.loads(line)
    assert parsed == record

    # Training_Subsystem validation rules (local mirror) pass.
    assert validate_entry_like_training_subsystem(parsed) == []

    # Defect_Type class label (Req 7.1).
    assert parsed["anomaly-label-metadata"]["class-name"] == defect_type
    assert parsed["synthetic-defect-metadata"]["class-map"] == {
        "0": defect_type}
    assert parsed["anomaly-label"] == 1

    # Bounding box annotation matching the derived box (Req 7.1).
    annotations = parsed["synthetic-defect"]["annotations"]
    assert len(annotations) == 1
    annotation = annotations[0]
    for edge in ("left", "top", "width", "height"):
        assert annotation[edge] == bbox[edge]
    assert parsed["synthetic-defect"]["image_size"] == [{
        "width": image_size["width"],
        "height": image_size["height"],
        "depth": image_size.get("depth", 3),
    }]

    # Synthetic marker, model id, session id, resolved prompt, bbox source
    # (Req 7.4, 10.3).
    metadata = parsed["synthetic-defect-metadata"]
    assert metadata["synthetic"] is True
    assert metadata["generation-model-id"] == \
        session_meta["generation_model_id"]
    assert metadata["generation-session-id"] == session_meta["session_id"]
    assert metadata["resolved-prompt"] == prompt
    assert metadata["bounding-box-source"] == bbox_source
    assert metadata["human-annotated"] == "no"
