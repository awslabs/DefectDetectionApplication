"""
Pure-Python mask utilities for the dda_sam_worker container-image Lambda.

Everything in this module depends only on the standard library so the
core logic (RLE encoding, region post-processing) is unit-testable
without onnxruntime / numpy / Pillow installed.

RLE format: identical to the canonical shared-layer implementation
(``dda_manifest.rle_encode``) — COCO-style uncompressed counts over the
mask flattened in column-major (Fortran) order, alternating
background/foreground runs and always starting with the background
count (which may be 0). Matching the shared layer exactly is what lets
SAM region proposals plug straight into the portal's annotation model.

Requirements: 8.1, 8.2
"""
from __future__ import annotations

from itertools import groupby
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# Greedy NMS threshold for near-duplicate proposals from adjacent prompts
DEFAULT_IOU_THRESHOLD = 0.85
# Cap on returned regions when the caller does not specify one
DEFAULT_MAX_REGIONS = 20


# ---------------------------------------------------------------------------
# RLE encoding (matches dda_manifest.rle_encode)
# ---------------------------------------------------------------------------

def runs_to_rle(first_value: int, run_lengths: Sequence[int]) -> str:
    """
    Assemble COCO-style counts from consecutive run lengths.

    Args:
        first_value: Value (0 or 1) of the first run in the flattening
        run_lengths: Length of each consecutive run, in order

    Returns:
        Space-separated counts starting with the background (0) run —
        a leading 0 is prepended when the mask starts with foreground.
    """
    counts: List[int] = []
    if first_value:
        counts.append(0)
    for run in run_lengths:
        run = int(run)
        if run < 0:
            raise ValueError('run lengths must be non-negative')
        counts.append(run)
    if not counts:
        counts.append(0)
    return ' '.join(str(c) for c in counts)


def _column_major_values(mask: Sequence[int], width: int, height: int) -> Iterable[int]:
    """Yield the mask's 0/1 values in column-major (Fortran) order."""
    for x in range(width):
        for y in range(height):
            yield 1 if mask[y * width + x] else 0


def rle_encode(mask: Sequence[int], width: int, height: int) -> str:
    """
    Encode a row-major binary mask into COCO-style RLE counts.

    Byte-for-byte compatible with the shared layer's
    ``dda_manifest.rle_encode``.

    Args:
        mask: Row-major flat sequence of 0/1 values, length width*height
        width: Mask width in pixels
        height: Mask height in pixels

    Returns:
        Space-separated run lengths over the column-major flattening,
        starting with the background (0) run count.
    """
    if width < 1 or height < 1:
        raise ValueError('width and height must be positive')
    if len(mask) != width * height:
        raise ValueError(f'mask length {len(mask)} != width*height {width * height}')

    first_value: Optional[int] = None
    run_lengths: List[int] = []
    for value, group in groupby(_column_major_values(mask, width, height)):
        if first_value is None:
            first_value = value
        run_lengths.append(sum(1 for _ in group))
    return runs_to_rle(first_value or 0, run_lengths)


# ---------------------------------------------------------------------------
# Mask arithmetic (big-int bitwise tricks keep this fast in pure Python)
# ---------------------------------------------------------------------------

def _bit_count(value: int) -> int:
    """Population count with a fallback for very old interpreters."""
    try:
        return value.bit_count()
    except AttributeError:  # pragma: no cover - Python < 3.10
        return bin(value).count('1')


def _mask_int(mask: Sequence[int]) -> int:
    """
    Pack a 0/1 mask into a big integer (one bit per pixel, byte-aligned)
    so intersection/union reduce to C-speed bitwise operations.
    """
    return int.from_bytes(bytes(bytearray(mask)), 'big')


def mask_area(mask: Sequence[int]) -> int:
    """Number of foreground pixels in a 0/1 mask."""
    return int(sum(mask))


def mask_iou(mask_a: Sequence[int], mask_b: Sequence[int]) -> float:
    """
    Intersection-over-union of two equal-length 0/1 masks.

    Returns 0.0 when both masks are empty.
    """
    if len(mask_a) != len(mask_b):
        raise ValueError('masks must have equal length')
    int_a = _mask_int(mask_a)
    int_b = _mask_int(mask_b)
    union = _bit_count(int_a | int_b)
    if union == 0:
        return 0.0
    return _bit_count(int_a & int_b) / union


def dedupe_masks(candidates: List[Dict],
                 iou_threshold: float = DEFAULT_IOU_THRESHOLD) -> List[Dict]:
    """
    Greedy non-maximum suppression over mask candidates.

    Candidates are dicts carrying at least ``mask`` (flat 0/1 sequence)
    and ``score`` (float). Higher-scoring masks are kept; a candidate is
    dropped when its IoU with any already-kept mask reaches the
    threshold.

    Returns:
        The kept candidate dicts (originals, not copies) in descending
        score order.
    """
    ordered = sorted(candidates, key=lambda c: c.get('score', 0.0), reverse=True)
    kept: List[Dict] = []
    kept_ints: List[Tuple[int, int]] = []  # (packed mask, area bits)
    for candidate in ordered:
        packed = _mask_int(candidate['mask'])
        duplicate = False
        for other_packed, _ in kept_ints:
            union = _bit_count(packed | other_packed)
            if union and _bit_count(packed & other_packed) / union >= iou_threshold:
                duplicate = True
                break
        if not duplicate:
            kept.append(candidate)
            kept_ints.append((packed, _bit_count(packed)))
    return kept


# ---------------------------------------------------------------------------
# Prompt grid and region post-processing
# ---------------------------------------------------------------------------

def build_point_grid(width: int, height: int,
                     points_per_side: int) -> List[Tuple[float, float]]:
    """
    Evenly spaced, cell-centered (x, y) prompt coordinates in pixel
    space — the standard automatic-mask-generation prompt layout.

    Returns points row by row (top-left first), all strictly inside
    [0, width) x [0, height).
    """
    if width < 1 or height < 1:
        raise ValueError('width and height must be positive')
    if points_per_side < 1:
        raise ValueError('points_per_side must be positive')
    points: List[Tuple[float, float]] = []
    for row in range(points_per_side):
        y = (row + 0.5) * height / points_per_side
        for col in range(points_per_side):
            x = (col + 0.5) * width / points_per_side
            points.append((x, y))
    return points


def select_regions(candidates: List[Dict], width: int, height: int,
                   max_regions: int = DEFAULT_MAX_REGIONS,
                   min_area_fraction: float = 0.0,
                   iou_threshold: float = DEFAULT_IOU_THRESHOLD,
                   encode: Optional[Callable[[Dict], str]] = None) -> List[Dict]:
    """
    Post-process mask candidates into class-agnostic RLE regions.

    Pipeline: drop empty/too-small masks -> greedy NMS dedupe -> cap at
    ``max_regions`` (highest scores win) -> RLE-encode. Output regions
    carry ``class: None`` because SAM proposals are class-agnostic; the
    labeler (or reviewer) assigns a Label_Set class downstream (req 8.2).

    Args:
        candidates: [{'mask': <row-major flat 0/1 seq>, 'score': float,
                      'area': int (optional precomputed)}]
        width: Source image width in pixels
        height: Source image height in pixels
        max_regions: Hard cap on returned regions (>= 1)
        min_area_fraction: Minimum foreground area as a fraction of the
            image; masks below it (or empty) are dropped
        iou_threshold: NMS threshold for near-duplicate suppression
        encode: Optional candidate -> RLE-counts callable (lets callers
            plug in a faster vectorized encoder); defaults to the pure
            ``rle_encode``

    Returns:
        [{'class': None, 'rle': '<counts>', 'score': float}] in
        descending score order, RLE at source resolution.
    """
    if width < 1 or height < 1:
        raise ValueError('width and height must be positive')
    if max_regions < 1:
        raise ValueError('max_regions must be positive')

    min_area = max(1, int(min_area_fraction * width * height))
    sized = []
    for candidate in candidates:
        area = candidate.get('area')
        if area is None:
            area = mask_area(candidate['mask'])
        if area >= min_area:
            sized.append(candidate)

    kept = dedupe_masks(sized, iou_threshold=iou_threshold)[:max_regions]

    if encode is None:
        encode = lambda c: rle_encode(c['mask'], width, height)  # noqa: E731

    regions: List[Dict] = []
    for candidate in kept:
        regions.append({
            'class': None,
            'rle': encode(candidate),
            'score': float(candidate.get('score', 0.0)),
        })
    return regions
