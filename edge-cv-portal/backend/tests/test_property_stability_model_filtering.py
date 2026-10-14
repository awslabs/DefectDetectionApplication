"""Property test for lifecycle-aware availability filtering
(stability-generation-models, task 3.1).

**Feature: stability-generation-models, Property 1: Lifecycle-aware
availability filtering is exact**

_For any_ Model_Catalog (entries with and without an ``invocation_id``) and
any list of available-model summaries with arbitrary lifecycle statuses,
``filter_available_models`` returns exactly the catalog entries whose bare
``model_id`` appears in the summaries with status ACTIVE - in catalog
order, with each returned entry equal to the original catalog entry - and
the result is unaffected by the presence or value of any entry's
``invocation_id``. Entries absent from the summaries or present with a
non-ACTIVE status (e.g. Nova Canvas as LEGACY) are excluded.

**Validates: Requirements 1.3, 4.3, 5.1, 5.2, 6.2, 8.2**

Pure-logic test over synthetic_core.filter_available_models: no AWS mocks.
"""
import copy

from hypothesis import example, given, settings
from hypothesis import strategies as st

from synthetic_core import filter_available_models


model_id_pool = st.sampled_from([
    "amazon.nova-canvas-v1:0",
    "amazon.titan-image-generator-v2:0",
    "stability.stable-image-inpaint-v1:0",
    "stability.stable-outpaint-v1:0",
    "provider.other-model-v9:0",
])

lifecycle_statuses = st.sampled_from(["ACTIVE", "LEGACY", "", "DEPRECATED"])


@st.composite
def catalogs(draw):
    """A Model_Catalog: entries with unique model ids, each with or
    without an ``invocation_id`` (present on Stability-style entries,
    absent on Amazon-style entries)."""
    ids = draw(st.lists(model_id_pool, unique=True, max_size=5))
    catalog = []
    for index, model_id in enumerate(ids):
        entry = {
            "model_id": model_id,
            "display_name": f"Model {index}",
            "capabilities": {
                "text_to_image": draw(st.booleans()),
                "inpainting": draw(st.booleans()),
                "image_variation": draw(st.booleans()),
                "seed": draw(st.booleans()),
                "cfg_scale": draw(st.booleans()),
            },
            "max_images_per_call": draw(st.integers(min_value=1,
                                                    max_value=5)),
            "randomization_defaults": {"seed": None},
        }
        if draw(st.booleans()):
            entry["invocation_id"] = "us." + model_id
        catalog.append(entry)
    return catalog


@st.composite
def summaries(draw, catalog):
    """Available-model summaries: a mix of catalog ids and foreign ids,
    each with an arbitrary lifecycle status. A model id may be absent
    entirely."""
    pool = [entry["model_id"] for entry in catalog] + [
        "foreign.model-a-v1:0", "foreign.model-b-v1:0"]
    chosen = draw(st.lists(st.sampled_from(pool) if pool else st.nothing(),
                           unique=True, max_size=len(pool)))
    return [{"model_id": model_id,
             "lifecycle_status": draw(lifecycle_statuses)}
            for model_id in chosen]


@st.composite
def filter_cases(draw):
    catalog = draw(catalogs())
    return catalog, draw(summaries(catalog))


@settings(deadline=None)
@example(case=(
    # Nova Canvas LEGACY excluded, Stability ACTIVE included (Req 5.2).
    [
        {"model_id": "amazon.nova-canvas-v1:0", "display_name": "Nova",
         "capabilities": {}, "max_images_per_call": 1,
         "randomization_defaults": {}},
        {"model_id": "stability.stable-image-inpaint-v1:0",
         "invocation_id": "us.stability.stable-image-inpaint-v1:0",
         "display_name": "Stability", "capabilities": {},
         "max_images_per_call": 1, "randomization_defaults": {}},
    ],
    [
        {"model_id": "amazon.nova-canvas-v1:0",
         "lifecycle_status": "LEGACY"},
        {"model_id": "stability.stable-image-inpaint-v1:0",
         "lifecycle_status": "ACTIVE"},
    ],
))
@given(case=filter_cases())
def test_lifecycle_filtering_is_exact(case):
    """filter_available_models admits exactly the ACTIVE-matched entries,
    in catalog order, unchanged, independent of invocation_id
    (Requirements 1.3, 4.3, 5.1, 5.2, 6.2, 8.2)."""
    catalog, available = case
    catalog_snapshot = copy.deepcopy(catalog)

    result = filter_available_models(catalog, available)

    # Exactness: exactly the catalog entries whose bare model_id appears
    # in the summaries with status ACTIVE, in catalog order, each equal
    # to (indeed, the same object as) the original entry.
    active_ids = {s["model_id"] for s in available
                  if s["lifecycle_status"] == "ACTIVE"}
    expected = [entry for entry in catalog
                if entry["model_id"] in active_ids]
    assert result == expected
    assert all(got is orig for got, orig in zip(result, expected))

    # Non-ACTIVE or absent entries are excluded.
    excluded = [entry for entry in catalog if entry not in result]
    for entry in excluded:
        assert entry["model_id"] not in active_ids

    # Inputs are not mutated.
    assert catalog == catalog_snapshot

    # Invocation-id independence: stripping or altering invocation_id on
    # every entry never changes which model ids are admitted (Req 4.3).
    stripped = []
    for entry in copy.deepcopy(catalog):
        entry.pop("invocation_id", None)
        stripped.append(entry)
    assert ([e["model_id"] for e in
             filter_available_models(stripped, available)]
            == [e["model_id"] for e in result])

    altered = []
    for entry in copy.deepcopy(catalog):
        entry["invocation_id"] = "zz.made-up-profile-id"
        altered.append(entry)
    assert ([e["model_id"] for e in
             filter_available_models(altered, available)]
            == [e["model_id"] for e in result])
