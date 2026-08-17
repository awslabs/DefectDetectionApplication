"""Property tests for the Stability inpainting mask (stability-generation-
models, tasks 3.5 and 7.1).

Covers the deterministic mask-rectangle derivation in ``synthetic_core``
(pure logic, no AWS) and the Pillow-based mask PNG rendering in
``synthetic_data`` (in-memory only, no AWS mocks).
"""
import io
import math
import os
import sys

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "functions"))

from synthetic_core import SEED_MODULUS, derive_mask_rect

_SIDE_FRACTION_MIN = 0.15
_SIDE_FRACTION_MAX = 0.40

task_seeds = st.integers(min_value=0, max_value=SEED_MODULUS - 1)

# Dimension strategy biased toward the degenerate cases: 1-pixel and tiny
# images alongside realistic sizes.
dimensions = st.one_of(
    st.integers(min_value=1, max_value=8),
    st.integers(min_value=1, max_value=4096),
)


def _side_band(dimension):
    """Inclusive [lo, hi] band for a mask side of ``dimension``: the
    clamped 15-40% band, robust to rounding (at least 1 px, at most the
    full dimension)."""
    lo = max(1, math.floor(_SIDE_FRACTION_MIN * dimension))
    hi = min(dimension, math.ceil(_SIDE_FRACTION_MAX * dimension))
    return lo, hi


# ---------------------------------------------------------------------------
# Task 3.5
#
# **Feature: stability-generation-models, Property 5: Mask rectangle
# derivation is deterministic and in-bounds**
#
# _For any_ Task_Seed in 0..858,993,459 and any image dimensions with
# width >= 1 and height >= 1: derive_mask_rect called twice with the same
# inputs returns the same rectangle; the rectangle lies fully within the
# image (left >= 0, top >= 0, left + width <= image_width,
# top + height <= image_height); and each side is within the clamped
# 15-40% size band (at least 1 pixel, at most the full dimension).
#
# **Validates: Requirements 3.3**
# ---------------------------------------------------------------------------

@settings(deadline=None)
@example(task_seed=0, image_width=1, image_height=1)
@example(task_seed=SEED_MODULUS - 1, image_width=1, image_height=1)
@example(task_seed=0, image_width=1, image_height=4096)
@example(task_seed=12345, image_width=2, image_height=3)
@example(task_seed=SEED_MODULUS - 1, image_width=7, image_height=5)
@given(task_seed=task_seeds, image_width=dimensions, image_height=dimensions)
def test_mask_rect_deterministic_and_in_bounds(task_seed, image_width,
                                               image_height):
    """derive_mask_rect is deterministic, in-bounds, and sized within the
    clamped 15-40% band (Requirement 3.3)."""
    rect = derive_mask_rect(task_seed, image_width, image_height)

    # Deterministic: a second call with the same inputs is identical.
    assert derive_mask_rect(task_seed, image_width, image_height) == rect

    # Exact shape.
    assert set(rect) == {"left", "top", "width", "height"}
    assert all(isinstance(rect[k], int) for k in rect)

    # Fully within the image.
    assert rect["left"] >= 0
    assert rect["top"] >= 0
    assert rect["left"] + rect["width"] <= image_width
    assert rect["top"] + rect["height"] <= image_height

    # Each side within the clamped 15-40% band (min 1 px, max the full
    # dimension), robust to rounding.
    width_lo, width_hi = _side_band(image_width)
    assert width_lo <= rect["width"] <= width_hi
    height_lo, height_hi = _side_band(image_height)
    assert height_lo <= rect["height"] <= height_hi


@settings(deadline=None)
@given(task_seed=task_seeds)
def test_one_pixel_image_yields_origin_rect(task_seed):
    """Degenerate 1x1 images always produce the 1x1 rectangle at the
    origin, for every seed (Requirement 3.3)."""
    assert derive_mask_rect(task_seed, 1, 1) == {
        "left": 0, "top": 0, "width": 1, "height": 1}


# ---------------------------------------------------------------------------
# Task 7.1
#
# **Feature: stability-generation-models, Property 6: Rendered mask PNG is
# binary and matches the rectangle**
#
# _For any_ image dimensions and any rectangle fully within them, decoding
# the PNG produced by _render_mask_png yields an image of exactly the
# source dimensions in which every pixel is 0 or 255, and the set of
# 255-valued pixels is exactly the rectangle's area.
#
# **Validates: Requirements 3.2**
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def render_mask_png():
    """synthetic_data._render_mask_png imported lazily.

    Some standalone tests install a fake ``shared_utils`` into sys.modules
    at collection time; importing synthetic_data at run time (popping any
    fake first) binds the real layer module, the same way conftest's
    aws_stack does."""
    shared = sys.modules.get("shared_utils")
    if shared is not None and not hasattr(shared, "check_user_access"):
        sys.modules.pop("shared_utils", None)
        sys.modules.pop("synthetic_data", None)
    from synthetic_data import _render_mask_png
    return _render_mask_png


@st.composite
def dims_and_rects(draw):
    """(width, height, rect) with the rectangle fully within the image.

    Dimensions capped at 64 so the full pixel scan stays fast; 1-pixel
    images are covered."""
    width = draw(st.integers(min_value=1, max_value=64))
    height = draw(st.integers(min_value=1, max_value=64))
    rect_width = draw(st.integers(min_value=1, max_value=width))
    rect_height = draw(st.integers(min_value=1, max_value=height))
    left = draw(st.integers(min_value=0, max_value=width - rect_width))
    top = draw(st.integers(min_value=0, max_value=height - rect_height))
    return width, height, {"left": left, "top": top,
                           "width": rect_width, "height": rect_height}


@settings(deadline=None)
@given(case=dims_and_rects())
def test_rendered_mask_png_binary_and_matches_rect(case, render_mask_png):
    """The rendered mask PNG has exactly the source dimensions, every
    pixel is 0 or 255, and the 255 set equals exactly the rectangle's
    area (Requirement 3.2)."""
    from PIL import Image

    width, height, rect = case
    png_bytes = render_mask_png(rect, width, height)

    with Image.open(io.BytesIO(png_bytes)) as mask:
        assert mask.size == (width, height)
        grayscale = mask.convert("L")
        pixels = grayscale.load()

        white = 0
        for y in range(height):
            for x in range(width):
                value = pixels[x, y]
                inside = (rect["left"] <= x < rect["left"] + rect["width"]
                          and rect["top"] <= y < rect["top"] + rect["height"])
                assert value in (0, 255)
                assert value == (255 if inside else 0)
                white += value == 255

        # Histogram cross-check: the 255 count is exactly the rect area.
        assert white == rect["width"] * rect["height"]
