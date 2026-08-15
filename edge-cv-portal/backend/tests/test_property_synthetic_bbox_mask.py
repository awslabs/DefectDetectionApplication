"""Property test for bounding box derivation from a mask (synthetic-defect-
data-generation, task 2.9).

**Feature: synthetic-defect-data-generation, Property 8: Bounding box
derivation from mask region**

_For any_ mask grid containing at least one nonzero cell: the derived
bounding box lies within the image bounds, contains every nonzero cell, and
is minimal (shrinking any edge by one would exclude at least one nonzero
cell); an all-zero mask yields no mask-derived box (triggering the fallback
derivation).

**Validates: Requirements 7.2**

Pure-logic test over synthetic_core.bbox_from_mask: no AWS mocks.
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from synthetic_core import bbox_from_mask


@st.composite
def masks_with_nonzero_cells(draw):
    """(mask, nonzero_cells) with at least one nonzero cell."""
    height = draw(st.integers(min_value=1, max_value=20))
    width = draw(st.integers(min_value=1, max_value=20))
    cells = draw(st.sets(
        st.tuples(st.integers(min_value=0, max_value=height - 1),
                  st.integers(min_value=0, max_value=width - 1)),
        min_size=1, max_size=min(30, height * width),
    ))
    mask = [[1 if (r, c) in cells else 0 for c in range(width)]
            for r in range(height)]
    return mask, cells, width, height


@settings(deadline=None)
@given(case=masks_with_nonzero_cells())
def test_bbox_is_within_bounds_containing_and_minimal(case):
    """The derived box lies within the image bounds, contains every nonzero
    cell, and each of its four edges touches at least one nonzero cell
    (minimality) (Requirement 7.2)."""
    mask, cells, width, height = case

    box = bbox_from_mask(mask)

    assert box is not None
    left, top = box["left"], box["top"]
    right = left + box["width"] - 1     # inclusive
    bottom = top + box["height"] - 1    # inclusive

    # Within image bounds.
    assert 0 <= left <= right < width
    assert 0 <= top <= bottom < height

    # Contains every nonzero cell.
    for row, col in cells:
        assert top <= row <= bottom
        assert left <= col <= right

    # Minimal: shrinking any edge by one would exclude a nonzero cell,
    # i.e. every edge row/column contains at least one nonzero cell.
    assert any(row == top for row, _ in cells)
    assert any(row == bottom for row, _ in cells)
    assert any(col == left for _, col in cells)
    assert any(col == right for _, col in cells)


@settings(deadline=None)
@given(height=st.integers(min_value=0, max_value=20),
       width=st.integers(min_value=0, max_value=20))
def test_all_zero_mask_yields_none(height, width):
    """An all-zero (or empty) mask yields no mask-derived box, triggering
    the fallback derivation (Requirement 7.2)."""
    mask = [[0] * width for _ in range(height)]
    assert bbox_from_mask(mask) is None


@settings(deadline=None)
@given(case=masks_with_nonzero_cells(),
       values=st.integers(min_value=2, max_value=255))
def test_any_nonzero_value_counts(case, values):
    """Nonzero mask values other than 1 (e.g. 255 grayscale masks) derive
    the same box (Requirement 7.2)."""
    mask, cells, _, _ = case
    scaled = [[values if cell else 0 for cell in row] for row in mask]
    assert bbox_from_mask(scaled) == bbox_from_mask(mask)
