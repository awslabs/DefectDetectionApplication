"""Property test for generation request validation (synthetic-defect-data-
generation, task 2.4).

**Feature: synthetic-defect-data-generation, Property 3: Generation request
validation**

_For any_ generation request: the request is accepted if and only if it has
at least one Source_Image, a source classification of Defect_Images or
Normal_Images, and - when classified as Normal_Images - a non-blank
Defect_Type; every rejection identifies the violated condition (including
the at-least-one-Source_Image message for empty selections).

**Validates: Requirements 3.2, 3.3, 3.6**

Pure-logic test over synthetic_core.validate_generation_request: no AWS
mocks. Variation count is held valid so this property isolates the source /
classification / defect-type conditions (Property 4 covers the count).
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from synthetic_core import ValidationError, validate_generation_request

source_image_refs = st.fixed_dictionaries({
    "bucket": st.just("usecase-bucket"),
    "key": st.text(min_size=1, max_size=40),
})

source_image_lists = st.lists(source_image_refs, min_size=0, max_size=5)

valid_source_classes = st.sampled_from(["defect", "normal"])
invalid_source_classes = st.one_of(
    st.none(),
    st.text(max_size=10).filter(lambda s: s not in ("defect", "normal")),
    st.integers(),
    st.booleans(),
)
source_classes = st.one_of(valid_source_classes, invalid_source_classes)

blank_defect_types = st.one_of(
    st.none(),
    st.sampled_from(["", " ", "   ", "\t", "\n"]),
    st.integers(),  # non-string is not a usable Defect_Type
)
non_blank_defect_types = st.text(min_size=1, max_size=30).filter(
    lambda s: s.strip())
defect_types = st.one_of(blank_defect_types, non_blank_defect_types)

valid_variation_counts = st.integers(min_value=1, max_value=20)


def _is_non_blank(defect_type):
    return isinstance(defect_type, str) and bool(defect_type.strip())


@settings(deadline=None)
@given(source_images=source_image_lists, source_class=source_classes,
       defect_type=defect_types, variation_count=valid_variation_counts)
def test_request_accepted_iff_all_conditions_hold(
        source_images, source_class, defect_type, variation_count):
    """Acceptance iff: >=1 source, classification in {defect, normal}, and
    (classification != normal or non-blank Defect_Type); every rejection
    identifies the violated condition (Requirements 3.2, 3.3, 3.6)."""
    has_sources = len(source_images) > 0
    class_valid = source_class in ("defect", "normal")
    defect_ok = source_class != "normal" or _is_non_blank(defect_type)
    should_accept = has_sources and class_valid and defect_ok

    if should_accept:
        validated = validate_generation_request(
            source_images, source_class, defect_type, variation_count)
        assert validated["source_images"] == list(source_images)
        assert validated["source_class"] == source_class
        assert validated["defect_type"] == defect_type
        assert validated["variation_count"] == variation_count
    else:
        with pytest.raises(ValidationError) as exc_info:
            validate_generation_request(
                source_images, source_class, defect_type, variation_count)
        message = str(exc_info.value).lower()
        # The message identifies the (first) violated condition.
        if not has_sources:
            assert "at least one source image" in message
        elif not class_valid:
            assert "classified" in message or "classification" in message
        else:
            assert "defect type" in message
