"""
Property-based tests for the span-based rasterizer
(layers/shared/python/dda_llm_guidance.py): rasterize_to_rle.

Spec: llm-auto-labeling, task 3.3. Properties 6-8.

**Feature: llm-auto-labeling, Property 6: RLE well-formedness**
**Validates: Requirements 5.2**
**Feature: llm-auto-labeling, Property 7: Rasterization fidelity**
**Validates: Requirements 5.1, 5.2**
**Feature: llm-auto-labeling, Property 8: Emitted geometry containment**
**Validates: Requirements 5.6**

Fidelity is checked against a naive dense per-pixel-center reference
rasterizer defined in this file (never in production code): for boxes a
pixel is filled iff its center lies inside the rectangle (half-open
[start, end) intervals); for polygons the even-odd rule is evaluated at
the pixel center by counting, per pixel, the intersections of the
vertical line through the center with every polygon edge (half-open
x-crossing rule, so vertex crossings count exactly once) whose y value
is at most the center's. The reference is O(width * height * edges), so the
fidelity generator keeps images small.

The module under test is pure (no boto3, no I/O), so these tests need
no moto fixtures and no AWS credentials — conftest.py already places
the shared layer on sys.path and registers the hypothesis profile these
tests run under.

Generator note: coordinates are drawn on a quarter-pixel grid (i / 4.0).
Dyadic quarters at these magnitudes are exact in IEEE doubles, so edge
intersections computed by the rasterizer and by the reference agree
bit-for-bit. Geometry touching the image bounds exactly (left = 0,
left + width = image width, vertices at x = width or y = height) is
generated deliberately.
"""
import dda_manifest
from hypothesis import given
from hypothesis import strategies as st

from dda_llm_guidance import rasterize_to_rle

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Small dimensions for the O(w*h*edges) dense reference (Property 7).
_small_dimensions = st.integers(min_value=1, max_value=20)
# Larger dimensions where no dense reference is involved (Properties 6, 8).
_large_dimensions = st.integers(min_value=1, max_value=128)


def _quarter_coordinate(draw, limit):
    """A quarter-grid coordinate in [0, limit], biased toward the exact
    bounds 0 and limit so border-touching geometry is generated often."""
    grid = draw(st.one_of(
        st.just(0),
        st.just(4 * limit),
        st.integers(min_value=0, max_value=4 * limit),
    ))
    return grid / 4.0


@st.composite
def _boxes(draw, width, height):
    """An in-bounds box (positive extent, left + width <= width), biased
    toward edges touching the image bounds exactly."""
    li = draw(st.one_of(
        st.just(0),
        st.integers(min_value=0, max_value=4 * width - 1),
    ))
    ti = draw(st.one_of(
        st.just(0),
        st.integers(min_value=0, max_value=4 * height - 1),
    ))
    wi = draw(st.one_of(
        st.just(4 * width - li),      # right edge exactly on the bound
        st.integers(min_value=1, max_value=4 * width - li),
    ))
    hi = draw(st.one_of(
        st.just(4 * height - ti),     # bottom edge exactly on the bound
        st.integers(min_value=1, max_value=4 * height - ti),
    ))
    return {'left': li / 4.0, 'top': ti / 4.0,
            'width': wi / 4.0, 'height': hi / 4.0}


@st.composite
def _vertices(draw, width, height):
    """3-8 in-bounds polygon vertices, biased toward the exact bounds.
    Degenerate and self-intersecting polygons are deliberately allowed —
    the rasterizer must still emit a well-formed (possibly all-background)
    RLE for them under the even-odd rule."""
    count = draw(st.integers(min_value=3, max_value=8))
    return [
        (_quarter_coordinate(draw, width), _quarter_coordinate(draw, height))
        for _ in range(count)
    ]


@st.composite
def _detections(draw, width, height):
    """One internal-model Detection (box or polygon) for rasterization."""
    if draw(st.booleans()):
        return {'class': 'defect', 'geometry': 'box',
                'box': draw(_boxes(width, height))}
    return {'class': 'defect', 'geometry': 'polygon',
            'vertices': draw(_vertices(width, height))}


@st.composite
def _cases(draw, dimensions):
    """(width, height, detection) with the detection in bounds."""
    width = draw(dimensions)
    height = draw(dimensions)
    return width, height, draw(_detections(width, height))


# ---------------------------------------------------------------------------
# Naive dense per-pixel-center reference rasterizer (test-only oracle)
# ---------------------------------------------------------------------------

def _reference_box_pixels(box, width, height):
    """Pixel (x, y) is filled iff its center (x + 0.5, y + 0.5) lies in
    [left, left + width) x [top, top + height)."""
    return {
        (x, y)
        for x in range(width)
        for y in range(height)
        if box['left'] <= x + 0.5 < box['left'] + box['width']
        and box['top'] <= y + 0.5 < box['top'] + box['height']
    }


def _reference_polygon_pixels(vertices, width, height):
    """Even-odd rule evaluated at every pixel center.

    For center (cx, cy): intersect the vertical line x = cx with every
    edge under the half-open crossing rule (min(xa, xb) <= cx <
    max(xa, xb), so a vertex crossing counts exactly once and horizontal
    edges never cross), and count the intersections with y <= cy. The
    center is inside iff that count is odd — the dense per-pixel
    formulation of pairing sorted intersections into half-open [ya, yb)
    spans.
    """
    count = len(vertices)
    pixels = set()
    for x in range(width):
        cx = x + 0.5
        for y in range(height):
            cy = y + 0.5
            crossings = 0
            for i in range(count):
                xa, ya = vertices[i]
                xb, yb = vertices[(i + 1) % count]
                if xa == xb:
                    continue
                if (xa <= cx < xb) or (xb <= cx < xa):
                    t = (cx - xa) / (xb - xa)
                    if ya + t * (yb - ya) <= cy:
                        crossings += 1
            if crossings % 2 == 1:
                pixels.add((x, y))
    return pixels


def _reference_pixels(detection, width, height):
    if detection['geometry'] == 'box':
        return _reference_box_pixels(detection['box'], width, height)
    return _reference_polygon_pixels(detection['vertices'], width, height)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decoded_pixels(rle, width, height):
    """Decode via dda_manifest.rle_decode into a set of (x, y) pixels."""
    mask = dda_manifest.rle_decode(rle, width, height)
    return {(x, y)
            for y in range(height)
            for x in range(width)
            if mask[y * width + x]}


def _foreground_indices(rle):
    """Column-major flat indices of every foreground pixel, read directly
    from the emitted counts (independently of rle_decode)."""
    indices = []
    position = 0
    foreground = False
    for token in rle.split():
        run = int(token)
        if foreground:
            indices.extend(range(position, position + run))
        position += run
        foreground = not foreground
    return indices


# ---------------------------------------------------------------------------
# Property 6: RLE well-formedness
# ---------------------------------------------------------------------------

@given(case=_cases(_large_dimensions))
def test_rle_well_formedness(case):
    """**Feature: llm-auto-labeling, Property 6: RLE well-formedness**

    Every emitted RLE string is a sequence of non-negative integer
    counts summing to exactly width * height — the invariant
    dda_manifest.rle_decode enforces, so rle_decode accepts every
    emitted string.

    **Validates: Requirements 5.2**
    """
    width, height, detection = case

    rle = rasterize_to_rle(detection, width, height)

    counts = [int(token) for token in rle.split()]
    assert counts, 'emitted RLE must contain at least one count'
    assert all(count >= 0 for count in counts)
    assert sum(counts) == width * height

    # rle_decode accepts the string (raises ValueError otherwise).
    mask = dda_manifest.rle_decode(rle, width, height)
    assert len(mask) == width * height


# ---------------------------------------------------------------------------
# Property 7: Rasterization fidelity
# ---------------------------------------------------------------------------

@given(case=_cases(_small_dimensions))
def test_rasterization_fidelity(case):
    """**Feature: llm-auto-labeling, Property 7: Rasterization fidelity**

    Decoding the emitted RLE yields exactly the pixel set the naive
    dense per-pixel-center reference rasterizer selects:
    `rle_decode(rasterize_to_rle(d, w, h), w, h)` equals the reference
    mask for boxes (center-in-rectangle) and polygons (even-odd rule at
    the center) alike.

    **Validates: Requirements 5.1, 5.2**
    """
    width, height, detection = case

    rle = rasterize_to_rle(detection, width, height)

    assert _decoded_pixels(rle, width, height) == \
        _reference_pixels(detection, width, height)


# ---------------------------------------------------------------------------
# Property 8: Emitted geometry containment
# ---------------------------------------------------------------------------

@given(case=_cases(_large_dimensions))
def test_emitted_geometry_containment(case):
    """**Feature: llm-auto-labeling, Property 8: Emitted geometry containment**

    No decoded foreground pixel lies outside the image frame, for any
    in-bounds detection — including geometry touching the bounds
    exactly, which the generators produce deliberately. Foreground
    indices are read directly from the emitted counts, so the check is
    independent of rle_decode's own validation.

    **Validates: Requirements 5.6**
    """
    width, height, detection = case

    rle = rasterize_to_rle(detection, width, height)

    for index in _foreground_indices(rle):
        x, y = divmod(index, height)
        assert 0 <= x < width, f'foreground pixel column {x} outside frame'
        assert 0 <= y < height, f'foreground pixel row {y} outside frame'
