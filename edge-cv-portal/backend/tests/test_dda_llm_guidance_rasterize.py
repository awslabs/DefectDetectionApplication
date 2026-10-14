"""
Example-based unit tests for the span-based rasterizer
(layers/shared/python/dda_llm_guidance.py): rasterize_to_rle against
hand-computed pixel sets.

Spec: llm-auto-labeling, task 3.2.
Requirements: 5.1, 5.2, 5.6, 5.7

Semantics under test: pixels are selected by pixel-center containment
(pixel (x, y) is filled when (x + 0.5, y + 0.5) lies inside the
geometry, with half-open [start, end) intervals), and polygons fill
under the even-odd rule. Every case asserts the emitted string decodes
via dda_manifest.rle_decode and matches the expected pixel set —
decoding also enforces the counts-sum-to-width*height invariant.

The module under test is pure (no boto3, no I/O), so these tests need
no moto fixtures — conftest.py already places the shared layer on
sys.path.
"""
import dda_manifest

from dda_llm_guidance import rasterize_to_rle


def _box_detection(left, top, width, height, cls='scratch'):
    return {'class': cls, 'geometry': 'box',
            'box': {'left': left, 'top': top,
                    'width': width, 'height': height}}


def _polygon_detection(vertices, cls='dent'):
    return {'class': cls, 'geometry': 'polygon',
            'vertices': [tuple(v) for v in vertices]}


def _decoded_pixels(rle, width, height):
    """Decode via dda_manifest.rle_decode into a set of (x, y) pixels."""
    mask = dda_manifest.rle_decode(rle, width, height)
    return {(x, y)
            for y in range(height)
            for x in range(width)
            if mask[y * width + x]}


def _assert_pixels(detection, width, height, expected):
    rle = rasterize_to_rle(detection, width, height)
    assert _decoded_pixels(rle, width, height) == expected


# ---------------------------------------------------------------------------
# Boxes (Requirements 5.1, 5.2, 5.6)
# ---------------------------------------------------------------------------

class TestBoxRasterization:
    def test_1x1_full_image_box(self):
        # The single pixel's center (0.5, 0.5) lies inside [0, 1) x [0, 1).
        rle = rasterize_to_rle(_box_detection(0, 0, 1, 1), 1, 1)
        assert rle == '0 1'
        assert _decoded_pixels(rle, 1, 1) == {(0, 0)}

    def test_box_covering_one_interior_pixel(self):
        # Box [2, 3) x [2, 3) on a 5x5 image contains only center
        # (2.5, 2.5), i.e. pixel (2, 2). Column-major flat index
        # x*height + y = 2*5 + 2 = 12, so the exact counts are known.
        rle = rasterize_to_rle(_box_detection(2, 2, 1, 1), 5, 5)
        assert rle == '12 1 12'
        assert _decoded_pixels(rle, 5, 5) == {(2, 2)}

    def test_box_edges_exactly_on_pixel_centers(self):
        # Box [1.5, 3.5) x [1.5, 3.5) on a 6x6 image: the left/top edges
        # coincide with the centers of pixel index 1 (included, half-open
        # lower bound is inclusive) and the right/bottom edges coincide
        # with the centers of pixel index 3 (excluded).
        _assert_pixels(
            _box_detection(1.5, 1.5, 2, 2), 6, 6,
            {(1, 1), (1, 2), (2, 1), (2, 2)})


# ---------------------------------------------------------------------------
# Polygons (Requirements 5.1, 5.2, 5.6)
# ---------------------------------------------------------------------------

class TestPolygonRasterization:
    def test_axis_aligned_triangle(self):
        # Right triangle with legs on the axes: (0,0), (4,0), (0,4) on a
        # 5x5 image. A column at cx = x + 0.5 spans [0, 4 - cx); rows
        # with centers below the hypotenuse y = 4 - x are filled.
        _assert_pixels(
            _polygon_detection([(0, 0), (4, 0), (0, 4)]), 5, 5,
            {(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (2, 0)})

    def test_convex_quadrilateral(self):
        # Trapezoid (1,1), (4,1), (3,3), (1,3) on a 5x5 image. Columns
        # 1 and 2 span [1, 3) -> rows 1-2; column 3 is cut by the
        # slanted right edge to [1, 2) -> row 1 only.
        _assert_pixels(
            _polygon_detection([(1, 1), (4, 1), (3, 3), (1, 3)]), 5, 5,
            {(1, 1), (1, 2), (2, 1), (2, 2), (3, 1)})

    def test_concave_l_shaped_polygon(self):
        # L-shape (0,0), (3,0), (3,1), (1,1), (1,3), (0,3) on a 4x4
        # image: a 1-wide vertical arm (column 0, rows 0-2) and a
        # horizontal arm (row 0, columns 0-2). The even-odd pairing
        # yields the tall span in column 0 and the short row-0 spans in
        # columns 1-2; the notch (x > 1, y > 1) stays empty.
        _assert_pixels(
            _polygon_detection(
                [(0, 0), (3, 0), (3, 1), (1, 1), (1, 3), (0, 3)]), 4, 4,
            {(0, 0), (0, 1), (0, 2), (1, 0), (2, 0)})

    def test_self_intersecting_polygon_even_odd(self):
        # Hourglass (0,0), (6,0), (0,6), (6,6) on a 6x6 image — the two
        # diagonal edges cross at (3, 3). Under the even-odd rule each
        # column has four sorted intersections [0, cx, 6-cx, 6] pairing
        # into a top span [0, min) and a bottom span [max, 6); the
        # middle between the crossing diagonals stays empty. The result
        # is asserted, not rejected.
        _assert_pixels(
            _polygon_detection([(0, 0), (6, 0), (0, 6), (6, 6)]), 6, 6,
            {
                # top lobe: rows with center y < cx and y < 6 - cx
                (1, 0), (2, 0), (3, 0), (4, 0), (2, 1), (3, 1),
                # bottom lobe: rows with center y >= max(cx, 6 - cx)
                (0, 5), (1, 4), (1, 5), (2, 3), (2, 4), (2, 5),
                (3, 3), (3, 4), (3, 5), (4, 4), (4, 5), (5, 5),
            })


# ---------------------------------------------------------------------------
# Zero spans (Requirement 5.7)
# ---------------------------------------------------------------------------

class TestSubPixelSliver:
    def test_sub_pixel_box_yields_zero_spans(self):
        # Box [0.6, 0.9) x [0.6, 0.9) on a 4x4 image contains no pixel
        # center (nearest centers are 0.5 and 1.5): the emitted RLE is
        # all background — one run of width*height.
        rle = rasterize_to_rle(_box_detection(0.6, 0.6, 0.3, 0.3), 4, 4)
        assert rle == '16'
        assert _decoded_pixels(rle, 4, 4) == set()

    def test_sub_pixel_polygon_yields_zero_spans(self):
        # A tiny triangle inside pixel (0, 0) but missing its center.
        rle = rasterize_to_rle(
            _polygon_detection([(0.1, 0.1), (0.4, 0.1), (0.25, 0.4)]), 4, 4)
        assert rle == '16'
        assert _decoded_pixels(rle, 4, 4) == set()


# ---------------------------------------------------------------------------
# Border-touching geometry (Requirement 5.6)
# ---------------------------------------------------------------------------

class TestBorderTouchingGeometry:
    def test_box_touching_left_border(self):
        _assert_pixels(_box_detection(0, 1, 1, 1), 4, 4, {(0, 1)})

    def test_box_touching_top_border(self):
        _assert_pixels(_box_detection(1, 0, 1, 1), 4, 4, {(1, 0)})

    def test_box_touching_right_border(self):
        # left + width == image width is in bounds (Requirement 4.5) and
        # fills exactly the last column's pixel.
        _assert_pixels(_box_detection(3, 1, 1, 1), 4, 4, {(3, 1)})

    def test_box_touching_bottom_border(self):
        _assert_pixels(_box_detection(1, 3, 1, 1), 4, 4, {(1, 3)})

    def test_full_frame_box_touches_all_four_borders(self):
        _assert_pixels(
            _box_detection(0, 0, 4, 4), 4, 4,
            {(x, y) for x in range(4) for y in range(4)})

    def test_full_frame_polygon_touches_all_four_borders(self):
        # Quad (0,0), (4,0), (4,4), (0,4) on a 4x4 image: every vertex
        # sits exactly on the image bounds and every pixel is filled;
        # no decoded foreground pixel can lie outside the frame because
        # rle_decode rejects counts not summing to width*height.
        _assert_pixels(
            _polygon_detection([(0, 0), (4, 0), (4, 4), (0, 4)]), 4, 4,
            {(x, y) for x in range(4) for y in range(4)})
