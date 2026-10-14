"""Property test for generation plan construction (synthetic-defect-data-
generation, task 2.6).

**Feature: synthetic-defect-data-generation, Property 5: Generation plan
completeness**

_For any_ Generation_Session with a selected Generation_Model, a non-empty
Source_Image set, a valid Variation_Count, and a resolved prompt: the
generation plan contains exactly Variation_Count tasks per Source_Image (so
|sources| x count tasks total), and every task carries the session's
selected Generation_Model identifier, the resolved prompt text, and a
per-task seed.

**Validates: Requirements 1.2, 4.2, 5.3**

Pure-logic test over synthetic_core.build_generation_plan: no AWS mocks.
"""
from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from synthetic_core import SEED_MODULUS, build_generation_plan

model_ids = st.sampled_from([
    "amazon.nova-canvas-v1:0", "amazon.titan-image-generator-v2:0",
    "stability.stable-image-inpaint-v1:0",
])

source_images = st.lists(
    st.fixed_dictionaries({
        "bucket": st.just("usecase-bucket"),
        "key": st.text(min_size=1, max_size=40),
    }),
    min_size=1, max_size=6,
)

resolved_prompts = st.text(min_size=1, max_size=200)

generation_params = st.fixed_dictionaries(
    {},
    optional={
        "seed": st.one_of(st.none(),
                          st.integers(min_value=0, max_value=SEED_MODULUS - 1)),
        "cfg_scale": st.floats(min_value=1.0, max_value=10.0,
                               allow_nan=False),
    },
)


@settings(deadline=None)
@given(model_id=model_ids, sources=source_images,
       variation_count=st.integers(min_value=1, max_value=20),
       prompt=resolved_prompts, params=generation_params)
def test_plan_is_complete_and_every_task_fully_specified(
        model_id, sources, variation_count, prompt, params):
    """The plan has exactly |sources| x count tasks - count per source -
    and every task carries the session's model id, the resolved prompt, and
    a deterministic per-task seed (Requirements 1.2, 4.2, 5.3)."""
    session_meta = {"session_id": "session-1",
                    "generation_model_id": model_id}

    plan = build_generation_plan(session_meta, sources, variation_count,
                                 prompt, params)

    # Exactly |sources| x count tasks, count per source image.
    assert len(plan) == len(sources) * variation_count
    per_source = Counter(task["source_index"] for task in plan)
    assert per_source == {i: variation_count for i in range(len(sources))}

    for task in plan:
        # Every task carries the session's selected model and the resolved
        # prompt text (the edited prompt on regeneration, Req 5.3).
        assert task["model_id"] == model_id
        assert task["resolved_prompt"] == prompt
        assert task["source_image"] == sources[task["source_index"]]
        # Per-task seed: an integer within the model seed domain.
        assert isinstance(task["seed"], int)
        assert not isinstance(task["seed"], bool)
        assert 0 <= task["seed"] < SEED_MODULUS

    # Variation indexes within each source cover 0..count-1.
    for source_index in range(len(sources)):
        variation_indexes = sorted(
            task["variation_index"] for task in plan
            if task["source_index"] == source_index)
        assert variation_indexes == list(range(variation_count))

    # Deterministic: rebuilding the plan from the same inputs yields the
    # same tasks (including the derived per-task seeds).
    replay = build_generation_plan(session_meta, sources, variation_count,
                                   prompt, params)
    assert replay == plan
