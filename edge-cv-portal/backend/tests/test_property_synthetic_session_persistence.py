"""Property test for Generation_Session persistence (synthetic-defect-
data-generation, task 4.6).

**Feature: synthetic-defect-data-generation, Property 13: Session
persistence round trip**

_For any_ Generation_Session state (META fields — Use_Case,
Generation_Model, Object_Type, Defect_Type, Prompt_Template text,
Source_Image references, generation parameters — plus any set of
Preview_Images with prompts and approval marks): persisting the session
and loading it back restores every field and every preview's approval
state and prompt text unchanged.

**Validates: Requirements 10.1, 10.2**

Persists through POST /synthetic/sessions and the preview store, then
restores through GET /synthetic/sessions/{id}, against moto DynamoDB
(conftest.py + synthetic_env.py).
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from synthetic_env import SyntheticEnv

MODEL_IDS = ("amazon.nova-canvas-v1:0", "amazon.titan-image-generator-v2:0")

# Printable text for names/prompts (non-empty, stripped of NUL etc.).
names = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=0x2FF),
    min_size=1, max_size=40,
)
s3_keys = st.from_regex(r"datasets/[a-z0-9\-]{1,12}/[a-z0-9\-]{1,12}\.png",
                        fullmatch=True)

source_images = st.lists(
    st.fixed_dictionaries({"bucket": st.just("test-synthetic-data-bucket"),
                           "key": s3_keys}),
    min_size=1, max_size=4,
)

# Generation params: ints and repr-round-trippable floats (DynamoDB stores
# Decimal; the handler converts back).
generation_params = st.fixed_dictionaries({
    "variation_count": st.integers(min_value=1, max_value=20),
    "seed": st.integers(min_value=0, max_value=858_993_459),
    "cfg_scale": st.floats(min_value=1.1, max_value=10.0,
                           allow_nan=False, allow_infinity=False),
})

meta_fields = st.fixed_dictionaries({
    "generation_model_id": st.sampled_from(MODEL_IDS),
    "object_type": names,
    "defect_type": names,
    "prompt_template_text": names,
    "source_class": st.sampled_from(["defect", "normal"]),
    "source_images": source_images,
    "generation_params": generation_params,
    "target_dataset_prefix": st.just("datasets/target/"),
    "target_manifest_key": st.just("datasets/target/train.manifest"),
})

previews = st.lists(
    st.fixed_dictionaries({
        "resolved_prompt": names,
        "approval_state": st.sampled_from(["pending", "approved",
                                           "rejected"]),
        "status": st.sampled_from(["completed", "failed"]),
        "variation_index": st.integers(min_value=0, max_value=19),
    }),
    max_size=5,
)


@pytest.fixture(scope="module")
def senv(aws_stack):
    return SyntheticEnv(aws_stack)


@settings(deadline=None)
@given(fields=meta_fields, preview_specs=previews)
def test_session_persistence_round_trip(senv, fields, preview_specs):
    """Persist-then-load restores every META field and every preview's
    approval state and prompt text unchanged (Requirements 10.1, 10.2)."""
    usecase_id = senv.create_usecase()
    user = senv.actor_with_role(usecase_id, "DataScientist")

    body = dict(fields)
    body["usecase_id"] = usecase_id
    status, created = senv.invoke("POST", "/synthetic/sessions", user,
                                  body=body)
    assert status == 201, created
    session_id = created["session"]["session_id"]

    expected_previews = {}
    for spec in preview_specs:
        preview_id = senv.put_preview(session_id, **spec)
        expected_previews[preview_id] = spec

    status, restored = senv.invoke("GET", "/synthetic/sessions/{id}", user,
                                   session_id=session_id)
    assert status == 200, restored
    session = restored["session"]

    # Every META field restored unchanged (Req 10.1, 10.2).
    for key, value in fields.items():
        assert session[key] == value, (
            f"META field {key!r} not restored: "
            f"{session[key]!r} != {value!r}")
    assert session["usecase_id"] == usecase_id

    # Every preview's approval state and prompt text restored (Req 10.2).
    restored_previews = {p["preview_id"]: p for p in restored["previews"]}
    assert set(restored_previews) == set(expected_previews)
    for preview_id, spec in expected_previews.items():
        restored_preview = restored_previews[preview_id]
        assert restored_preview["approval_state"] == spec["approval_state"]
        assert restored_preview["resolved_prompt"] == spec["resolved_prompt"]
        assert restored_preview["status"] == spec["status"]
        assert restored_preview["variation_index"] == \
            spec["variation_index"]

    # Listing shows the session with status and creation time (Req 10.4
    # supporting check).
    status, listing = senv.invoke("GET", "/synthetic/sessions", user,
                                  query={"usecase_id": usecase_id})
    assert status == 200
    listed = [s for s in listing["sessions"]
              if s["session_id"] == session_id]
    assert len(listed) == 1
    assert listed[0]["status"] == session["status"]
    assert listed[0]["created_at"] == session["created_at"]
