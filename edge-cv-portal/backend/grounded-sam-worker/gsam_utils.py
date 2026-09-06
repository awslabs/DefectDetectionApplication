"""
Pure-Python prompt and detection utilities for the dda_grounded_sam_worker
container-image Lambda.

Everything in this module depends only on the standard library so the core
Grounded-SAM logic — Prompt_Map validation, Grounding DINO caption
building, phrase token-span attribution, box conversion/clamping, and
per-label greedy NMS — is unit-testable without onnxruntime / numpy /
Pillow / tokenizers installed (the sibling sam-worker's ``mask_utils``
precedent).

Pipeline context: the worker submits all of a job's prompts to Grounding
DINO as one caption ('small surface dent. scratch. discoloration.') and
runs the model once per image. Each detection query is attributed back to
exactly one prompt by mapping its per-token confidences onto the caption's
phrase token spans; retained detections are converted from the model's
normalized (cx, cy, w, h) boxes to pixel boxes clamped to the source
image, deduplicated per label, and capped. RLE encoding lives in
``mask_utils`` (a verbatim copy of the sam-worker module).

Requirements: 3.3, 3.4, 3.6, 3.8, 3.10 (grounded-sam-autolabel)
"""
from __future__ import annotations

from math import isfinite
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Minimum overall detection confidence — the IDEA-Research Grounded-SAM
# demo default
DEFAULT_BOX_THRESHOLD = 0.35
# Minimum prompt-attribution confidence (ditto)
DEFAULT_TEXT_THRESHOLD = 0.25
# Cap on returned detections when the caller does not specify one
# (mirrors mask_utils.DEFAULT_MAX_REGIONS)
DEFAULT_MAX_DETECTIONS = 20
# Greedy per-label NMS threshold for near-duplicate boxes of one label
DEFAULT_BOX_NMS_IOU = 0.8


# ---------------------------------------------------------------------------
# Prompt_Map normalization (worker boundary)
# ---------------------------------------------------------------------------

def normalize_prompts(prompts: object) -> List[Dict]:
    """
    Validate the invocation event's Prompt_Map into an ordered
    ``[{'label', 'prompt'}]`` list.

    An entry's prompt falls back to its label when the prompt is absent,
    None, or empty after stripping, so a bare label list is already a
    complete Prompt_Map. Malformed input raises instead of degrading, so
    the synchronous caller records the invocation as a Pre_Label
    generation failure (req 3.8).

    Args:
        prompts: The event's prompts value — expected
            ``[{'label': str, 'prompt': str?}, ...]``

    Returns:
        New ``{'label': str, 'prompt': str}`` dicts in input order, every
        prompt a non-blank string.

    Raises:
        ValueError: When prompts is not a non-empty list, an entry is not
            a dict, a label is missing/not a string/blank, or a prompt
            value is neither a string nor None.
    """
    if not isinstance(prompts, list):
        raise ValueError('prompts must be a list')
    if not prompts:
        raise ValueError('prompts must not be empty')
    normalized: List[Dict] = []
    for index, entry in enumerate(prompts):
        if not isinstance(entry, dict):
            raise ValueError(f'prompts[{index}] must be an object')
        label = entry.get('label')
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f'prompts[{index}] label must be a non-empty string')
        prompt = entry.get('prompt')
        if prompt is None:
            prompt = label
        elif not isinstance(prompt, str):
            raise ValueError(f'prompts[{index}] prompt must be a string')
        elif not prompt.strip():
            prompt = label
        normalized.append({'label': label, 'prompt': prompt})
    return normalized


# ---------------------------------------------------------------------------
# Caption building and phrase token spans
# ---------------------------------------------------------------------------

def build_caption(prompt_texts: object) -> Tuple[str, List[str]]:
    """
    Normalize prompt phrases and join them into the model's canonical
    multi-phrase caption.

    Each phrase is stripped, lowercased, its inner whitespace runs are
    collapsed to single spaces, and its trailing dots are removed; the
    phrases then join as ``'p1. p2. p3.'`` — every phrase followed by a
    dot, the Grounding DINO multi-phrase query format (req 3.3). The
    returned phrase list is index-aligned with the input so detections
    attribute back to the originating prompt.

    Args:
        prompt_texts: Sequence of prompt strings, one per label
            (typically the ``prompt`` values of ``normalize_prompts``)

    Returns:
        ``(caption, phrases)`` — the caption string and the normalized
        phrases in input order.

    Raises:
        ValueError: When prompt_texts is not a non-empty sequence of
            strings, or a phrase normalizes to nothing (e.g. only dots
            and whitespace) — an empty phrase would break the
            one-span-per-phrase caption alignment.
    """
    if isinstance(prompt_texts, (str, bytes)):
        raise ValueError('prompt_texts must be a sequence of strings, not a string')
    try:
        texts = list(prompt_texts)
    except TypeError:
        raise ValueError('prompt_texts must be a sequence of strings')
    if not texts:
        raise ValueError('prompt_texts must not be empty')

    phrases: List[str] = []
    for index, text in enumerate(texts):
        if not isinstance(text, str):
            raise ValueError(f'prompt_texts[{index}] must be a string')
        # strip + collapse inner whitespace runs, then lowercase, then
        # drop trailing dots (and any whitespace the removal exposes)
        phrase = ' '.join(text.split()).lower().rstrip('. ')
        if not phrase:
            raise ValueError(f'prompt_texts[{index}] normalizes to an empty phrase')
        phrases.append(phrase)

    caption = '. '.join(phrases) + '.'
    return caption, phrases


def phrase_token_spans(token_ids: Sequence[int],
                       separator_ids: Iterable[int],
                       special_ids: Iterable[int]) -> List[Tuple[int, int]]:
    """
    Locate the ``[start, end)`` token index span of each caption phrase.

    A span is a maximal run of consecutive tokens that are neither
    separator tokens (the '.' between phrases) nor special tokens
    ([CLS]/[SEP]/[PAD]); runs come back in caption order, one per phrase
    of a well-formed caption. Pure over plain int lists so the span
    bookkeeping is testable without a tokenizer.

    Args:
        token_ids: The tokenized caption's token ids, in order
        separator_ids: Token ids acting as phrase separators
        special_ids: The tokenizer's special token ids

    Returns:
        Disjoint, ordered ``[start, end)`` tuples, one per token run.
    """
    ids = list(token_ids)
    breaks = set(separator_ids) | set(special_ids)
    spans: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for index, token_id in enumerate(ids):
        if token_id in breaks:
            if start is not None:
                spans.append((start, index))
                start = None
        elif start is None:
            start = index
    if start is not None:
        spans.append((start, len(ids)))
    return spans


# ---------------------------------------------------------------------------
# Detection attribution and box geometry
# ---------------------------------------------------------------------------

def attribute_detection(token_scores: Sequence[float],
                        spans: Sequence[Tuple[int, int]],
                        box_threshold: float = DEFAULT_BOX_THRESHOLD,
                        text_threshold: float = DEFAULT_TEXT_THRESHOLD,
                        ) -> Optional[Tuple[int, float]]:
    """
    Attribute one detection query to the single best phrase span.

    Computes each span's maximum token score; the winning span is the one
    with the highest maximum (the first wins ties) and the detection's
    score is that maximum. The detection is dropped when its score misses
    the Box_Threshold or the winning span's maximum misses the
    Text_Threshold — so every retained detection maps to exactly one
    prompt (req 3.3, 3.4).

    Args:
        token_scores: One query's per-token confidence scores
            (sigmoided logits over the caption tokens)
        spans: ``[start, end)`` phrase spans from ``phrase_token_spans``
        box_threshold: Minimum overall detection confidence
        text_threshold: Minimum prompt-attribution confidence

    Returns:
        ``(phrase_index, score)`` for a retained detection, or None when
        the detection falls below either threshold (or no span holds a
        score at all).
    """
    scores = list(token_scores)
    best_index: Optional[int] = None
    best_score = 0.0
    for index, (start, end) in enumerate(spans):
        window = scores[start:end]
        if not window:
            continue
        span_max = max(window)
        if best_index is None or span_max > best_score:
            best_index = index
            best_score = span_max
    if best_index is None:
        return None
    score = float(best_score)
    if score < box_threshold or score < text_threshold:
        return None
    return (best_index, score)


def cxcywh_to_pixel_box(box: Sequence[float], width: float,
                        height: float) -> Optional[Dict[str, float]]:
    """
    Convert one normalized (cx, cy, w, h) box to a clamped pixel box.

    Grounding DINO emits box centers and sizes normalized to [0, 1]; the
    pixel corners are clamped to ``[0, width] x [0, height]`` and the
    detection is dropped when the clamped box has no positive area
    (req 3.6). Total: malformed, non-finite, or degenerate input yields
    None, never an exception.

    Args:
        box: ``(cx, cy, w, h)`` sequence in normalized coordinates
        width: Source image width in pixels
        height: Source image height in pixels

    Returns:
        ``{'left', 'top', 'width', 'height'}`` floats in pixel space, or
        None when the clamped box has no positive area.
    """
    try:
        cx, cy, w, h = (float(value) for value in box)
        width = float(width)
        height = float(height)
    except (TypeError, ValueError):
        return None
    if not all(isfinite(value) for value in (cx, cy, w, h, width, height)):
        return None
    if width <= 0 or height <= 0:
        return None

    # Corner arithmetic on finite inputs can overflow to +/-inf but never
    # NaN; the clamps below make every result finite again.
    x0 = (cx - w / 2.0) * width
    x1 = (cx + w / 2.0) * width
    y0 = (cy - h / 2.0) * height
    y1 = (cy + h / 2.0) * height

    left = min(max(x0, 0.0), width)
    right = min(max(x1, 0.0), width)
    top = min(max(y0, 0.0), height)
    bottom = min(max(y1, 0.0), height)

    box_width = right - left
    box_height = bottom - top
    if box_width <= 0 or box_height <= 0:
        return None
    return {'left': left, 'top': top, 'width': box_width, 'height': box_height}


def box_iou(a: Dict[str, float], b: Dict[str, float]) -> float:
    """
    Intersection-over-union of two ``{'left', 'top', 'width', 'height'}``
    pixel boxes.

    Returns 0.0 when the union has no area (both boxes degenerate).
    """
    a_left = float(a['left'])
    a_top = float(a['top'])
    a_right = a_left + float(a['width'])
    a_bottom = a_top + float(a['height'])
    b_left = float(b['left'])
    b_top = float(b['top'])
    b_right = b_left + float(b['width'])
    b_bottom = b_top + float(b['height'])

    inter_width = min(a_right, b_right) - max(a_left, b_left)
    inter_height = min(a_bottom, b_bottom) - max(a_top, b_top)
    intersection = 0.0
    if inter_width > 0 and inter_height > 0:
        intersection = inter_width * inter_height

    area_a = max(a_right - a_left, 0.0) * max(a_bottom - a_top, 0.0)
    area_b = max(b_right - b_left, 0.0) * max(b_bottom - b_top, 0.0)
    union = area_a + area_b - intersection
    if union <= 0:
        return 0.0
    return intersection / union


# ---------------------------------------------------------------------------
# Detection selection
# ---------------------------------------------------------------------------

def select_detections(candidates: List[Dict],
                      max_detections: int = DEFAULT_MAX_DETECTIONS,
                      iou_threshold: float = DEFAULT_BOX_NMS_IOU) -> List[Dict]:
    """
    Post-process attributed detections into the returned detection set.

    Pipeline: stable score-descending sort -> greedy per-label NMS (a
    candidate is dropped when its box overlaps an already-kept candidate
    of the *same* ``label_index`` at or above the IoU threshold) -> cap
    at ``max_detections``, highest scores kept (req 3.4). Zero survivors
    is an empty selection, not an error (req 3.10).

    Args:
        candidates: ``[{'label_index': int, 'score': float,
            'box': {'left', 'top', 'width', 'height'}}]``
        max_detections: Hard cap on returned detections (>= 1)
        iou_threshold: Per-label NMS threshold for near-duplicate boxes

    Returns:
        The kept candidate dicts (originals, not copies) in descending
        score order, at most ``max_detections`` of them.
    """
    if max_detections < 1:
        raise ValueError('max_detections must be positive')
    ordered = sorted(candidates, key=lambda c: c.get('score', 0.0), reverse=True)
    kept: List[Dict] = []
    for candidate in ordered:
        duplicate = False
        for other in kept:
            if other.get('label_index') != candidate.get('label_index'):
                continue
            if box_iou(candidate['box'], other['box']) >= iou_threshold:
                duplicate = True
                break
        if not duplicate:
            kept.append(candidate)
    return kept[:max_detections]
