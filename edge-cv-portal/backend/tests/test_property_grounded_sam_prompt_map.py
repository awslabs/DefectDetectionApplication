"""Prompt_Map derivation property test (grounded-sam-autolabel, task 3.2).

**Feature: grounded-sam-autolabel, Property 5: Prompt_Map derivation is
total, ordered, and falls back to label names**

_For any_ Label_Set and _any_ `prompt_overrides` value (a conforming map,
a map with extra or non-string entries, None, or a non-dict), the
consumer's Prompt_Map SHALL contain exactly one `{label, prompt}` pair per
Label_Set label in Label_Set order, with `prompt` equal to the label's
override exactly when that override is a string non-empty after trimming,
and the label name otherwise — including for pre-feature job records
carrying no overrides at all. The derivation never raises: a malformed
job record must never fail the image here (Req 7.6).

**Validates: Requirements 2.7, 7.6**

`_grounded_sam_prompts` is pure (no AWS calls), so this test runs against
a plain import of `dda_autolabel_worker` with no moto stack — conftest.py
already provides the fake credentials, region, and the functions/layer
sys.path entries the module needs at import time (it only *constructs*
boto3 clients at import; nothing is invoked).
"""
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.fixture(scope="module")
def grounded_sam_prompts():
    """The pure Prompt_Map function, imported without moto.

    Mirrors the minimal slice of test_dda_autolabel_worker.py's `worker`
    fixture: a lazy (fixture-time, not collection-time) import so the
    standalone suites that install a collection-time fake `shared_utils`
    (the test_captures.py pattern) can be displaced first — the real
    layer module is already on sys.path via conftest.py, exactly as the
    conftest `aws_stack` fixture itself does.
    """
    shared = sys.modules.get("shared_utils")
    if shared is not None and not hasattr(shared, "get_s3_client_for_bucket"):
        sys.modules.pop("shared_utils")
    worker = sys.modules.get("dda_autolabel_worker")
    if worker is None or not hasattr(worker, "_grounded_sam_prompts"):
        sys.modules.pop("dda_autolabel_worker", None)
        import dda_autolabel_worker as worker
    return worker._grounded_sam_prompts


# ------------------------------------------------------------- strategies

_LABEL = st.text(max_size=16)  # incl. empty, whitespace-only, unicode

# Arbitrary override *values*: conforming strings, empty/whitespace-only
# strings, and the non-string junk a hand-edited or corrupted job record
# could carry (None, ints, floats incl. NaN, bools, lists).
_ARBITRARY_VALUE = st.one_of(
    st.text(max_size=32),
    st.none(),
    st.integers(),
    st.floats(),
    st.booleans(),
    st.lists(st.text(max_size=4), max_size=3),
)

# Entire non-dict overrides values (the isinstance gate's fallback arm).
_NON_DICT_OVERRIDES = st.one_of(
    st.none(),
    st.text(max_size=12),
    st.integers(),
    st.floats(),
    st.booleans(),
    st.lists(st.text(max_size=6), max_size=4),
)


@st.composite
def _prompt_map_cases(draw):
    """(label_set, overrides) pairs biased toward the interesting arms:
    conforming maps keyed on the drawn labels, messy maps mixing in-set
    and extra keys (incl. non-string keys) with arbitrary values, and
    non-dict values standing in for pre-feature/malformed records."""
    labels = draw(st.lists(_LABEL, max_size=8))
    if labels:
        in_set_keys = st.sampled_from(labels)
        conforming = st.dictionaries(
            in_set_keys,
            st.text(min_size=1, max_size=32).filter(lambda s: s.strip()),
            max_size=len(labels),
        )
        messy_keys = st.one_of(
            in_set_keys, st.text(max_size=12), st.integers())
    else:
        conforming = st.just({})
        messy_keys = st.one_of(st.text(max_size=12), st.integers())
    messy = st.dictionaries(messy_keys, _ARBITRARY_VALUE, max_size=10)
    overrides = draw(st.one_of(conforming, messy, _NON_DICT_OVERRIDES))
    return labels, overrides


def _expected_prompt(label, overrides):
    """The spec's rule, restated locally (Req 2.7): the override exactly
    when it is a string non-empty after trimming, the label otherwise."""
    if isinstance(overrides, dict):
        override = overrides.get(label)
        if isinstance(override, str) and override.strip():
            return override
    return label


# --------------------------------------------------------------- property

@settings(max_examples=100, deadline=None)
@given(case=_prompt_map_cases())
def test_property_prompt_map_total_ordered_label_fallback(
        grounded_sam_prompts, case):
    """
    **Feature: grounded-sam-autolabel, Property 5: Prompt_Map derivation
    is total, ordered, and falls back to label names**

    Exactly one `{label, prompt}` pair per Label_Set label in Label_Set
    order; `prompt` is the override exactly when the override is a string
    non-empty after trimming (transmitted raw, character-for-character),
    the label name otherwise; and the call never raises, whatever the
    overrides value (Req 7.6 — malformed records degrade, never fail).

    **Validates: Requirements 2.7, 7.6**
    """
    labels, overrides = case

    # Totality: any exception here fails the property.
    result = grounded_sam_prompts(labels, overrides)

    assert isinstance(result, list)
    assert len(result) == len(labels)
    for label, entry in zip(labels, result):
        # Exact two-key shape, positional (Label_Set) order, and the
        # override-else-label prompt rule, raw values compared
        # character-for-character.
        assert entry == {"label": label,
                         "prompt": _expected_prompt(label, overrides)}
