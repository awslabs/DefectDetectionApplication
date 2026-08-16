"""Property test for variation count validation (synthetic-defect-data-
generation, task 2.5).

**Feature: synthetic-defect-data-generation, Property 4: Variation count
bounds**

_For any_ submitted Variation_Count value (integers, non-integers, booleans,
strings, out-of-range numbers): validation accepts the value if and only if
it is an integer between 1 and 20 inclusive, and every rejection reports the
valid range.

**Validates: Requirements 4.1, 4.4**

Pure-logic test over synthetic_core.validate_variation_count: no AWS mocks.
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from synthetic_core import ValidationError, validate_variation_count

variation_count_inputs = st.one_of(
    st.integers(min_value=-1000, max_value=1000),   # in and out of range
    st.booleans(),                                   # bool is not a count
    st.floats(allow_nan=False, allow_infinity=False),
    st.floats(min_value=1, max_value=20),            # integral-ish floats
    st.text(max_size=10),                            # includes "5" etc.
    st.none(),
    st.lists(st.integers(), max_size=3),
    st.decimals(allow_nan=False, allow_infinity=False),
)


def _is_valid(value):
    return (isinstance(value, int) and not isinstance(value, bool)
            and 1 <= value <= 20)


@settings(deadline=None)
@given(value=variation_count_inputs)
def test_accepts_exactly_integers_one_to_twenty(value):
    """Acceptance iff the value is a non-boolean integer in [1, 20]
    (Requirement 4.1); every rejection reports the valid range
    (Requirement 4.4)."""
    if _is_valid(value):
        assert validate_variation_count(value) == value
    else:
        with pytest.raises(ValidationError) as exc_info:
            validate_variation_count(value)
        message = str(exc_info.value)
        assert "1" in message and "20" in message, (
            f"rejection must report the valid range, got: {message!r}")
