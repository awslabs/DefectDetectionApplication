"""Property test for approval filtering (synthetic-defect-data-generation,
task 2.8).

**Feature: synthetic-defect-data-generation, Property 7: Approval filtering**

_For any_ set of Preview_Images with arbitrary approval states (approved,
rejected, pending): the integration set equals exactly the approved subset -
no rejected or pending preview is included - and confirming approval with
zero approved previews is rejected.

**Validates: Requirements 6.3, 6.5, 6.6**

Pure-logic test over synthetic_core.select_approved: no AWS mocks.
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from synthetic_core import ValidationError, select_approved

previews = st.lists(
    st.builds(
        lambda i, state: {
            "preview_id": f"preview-{i}",
            "approval_state": state,
            "staging_key": f"synthetic-staging/session/preview-{i}.png",
        },
        st.integers(min_value=0, max_value=10 ** 6),
        st.sampled_from(["approved", "rejected", "pending"]),
    ),
    max_size=25,
)


@settings(deadline=None)
@given(previews=previews)
def test_integration_set_is_exactly_the_approved_subset(previews):
    """select_approved returns exactly the approved previews in order, and
    rejects the confirmation when zero previews are approved
    (Requirements 6.3, 6.5, 6.6)."""
    expected = [p for p in previews if p["approval_state"] == "approved"]

    if expected:
        selected = select_approved(previews)
        # Exactly the approved subset, original order.
        assert selected == expected
        # No rejected or pending preview is included; every selected
        # element is one of the input preview objects.
        assert all(p["approval_state"] == "approved" for p in selected)
        assert all(any(s is p for p in previews) for s in selected)
    else:
        with pytest.raises(ValidationError) as exc_info:
            select_approved(previews)
        assert "at least one approved image" in str(exc_info.value).lower()
