"""
DDA LLM Request Utility
Pure functions that own the two invariants the Preview_API and the
Auto_Labeler must share for the `llm:` auto-label family: which
Few_Shot_Examples a request carries, and how the Converse content list
is laid out.

Module contract: pure functions only — no boto3, no Pillow, no I/O.
Imports are limited to `typing` and the sibling `dda_llm_guidance`
prompt builder, so both the preview path and the auto-label consumer
build byte-identical requests from the same code (Requirements 3.1,
6.6, 7.6).

Example reference model (as persisted with the Labeling_Job record in
`auto_label.few_shot.examples`, in stored order):

    {'ref': 's3://bucket/labeling-examples/job/good/0-a.jpg',
     'designation': 'good' | 'bad',
     'position': 0}

Image model (what build_llm_request turns into Converse image blocks):

    {'bytes': b'...', 'format': 'png' | 'jpeg',
     'designation'?: 'good' | 'bad'}

Requirements: 3.1, 6.5, 7.1, 7.2, 7.3, 7.4, 10.2
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from dda_llm_guidance import build_detection_prompt

# Default Model_Image_Limit for models without a configured limit
# (Requirement 7.1). The bound counts the target image plus every
# attached Few_Shot_Example.
MODEL_IMAGE_LIMIT_DEFAULT = 20

FEW_SHOT_GOOD = 'good'
FEW_SHOT_BAD = 'bad'

# Content that introduces the Few_Shot_Examples and, after them, the
# image actually being labeled. Both are constants so the preview and
# the Auto_Labeler emit the same text (Requirement 6.6).
FEW_SHOT_HEADER = (
    'The following images are reference examples from this dataset. '
    'Each one is identified as a good example (what to detect) or a bad '
    'example (what not to detect). Use them as guidance only; do not '
    'report detections from them.'
)
FEW_SHOT_TARGET_INTRO = (
    'End of reference examples. The image to label follows.'
)

# Per-example identification text, one block immediately before each
# example image (Requirement 6.5).
_DESIGNATION_LABELS = {
    FEW_SHOT_GOOD: 'Good example',
    FEW_SHOT_BAD: 'Bad example',
}

_IMAGE_FORMAT_PNG = 'png'
_IMAGE_FORMAT_JPEG = 'jpeg'


# ---------------------------------------------------------------------------
# Model_Image_Limit resolution
# ---------------------------------------------------------------------------

def resolve_model_image_limit(model_identifier: str,
                              limits: Optional[Dict[str, Any]]) -> int:
    """
    Resolve the Model_Image_Limit for one model identifier
    (Requirement 7.1).

    Total and safe by construction: the configured value is used only
    when it is a genuine integer of at least 1, so a missing entry, a
    non-integer entry (including bool, JSON strings and floats) or a
    value below 1 can never widen the bound or drive it to zero — each
    falls back to MODEL_IMAGE_LIMIT_DEFAULT.

    Args:
        model_identifier: The LLM_Auto_Label_Model identifier (the part
            after the `llm:` prefix, as configured)
        limits: The Model_Image_Limit configuration mapping, or None

    Returns:
        An integer of at least 1
    """
    if not isinstance(limits, dict) or not isinstance(model_identifier, str):
        return MODEL_IMAGE_LIMIT_DEFAULT
    configured = limits.get(model_identifier)
    # bool is an int subclass; reject it before the int check.
    if isinstance(configured, bool) or not isinstance(configured, int):
        return MODEL_IMAGE_LIMIT_DEFAULT
    if configured < 1:
        return MODEL_IMAGE_LIMIT_DEFAULT
    return configured


# ---------------------------------------------------------------------------
# Few_Shot_Example selection
# ---------------------------------------------------------------------------

def select_few_shot_examples(examples: List[Dict],
                             model_image_limit: int
                             ) -> Tuple[List[Dict], List[Dict]]:
    """
    Split stored example references into (attached, omitted)
    deterministically (Requirements 7.3, 7.4, 7.6).

    The candidate ordering is *good examples in stored order followed by
    bad examples in stored order* — the list order as persisted, with
    `designation` selecting the group and `position` carried through
    untouched. The attached list is the prefix of that ordering of
    length ``max(0, model_image_limit - 1)``: one slot of the bound is
    always reserved for the target image, so a limit of 1 attaches
    nothing. The omitted list is exactly the remainder of the same
    ordering, so attached + omitted always accounts for every input
    entry and the resulting request carries at least 1 and at most
    model_image_limit images (Requirement 7.2).

    Selection depends only on the input, so the Preview_API and the
    Auto_Labeler select identical subsets in identical order for the
    same Labeling_Job configuration (Requirement 7.6).

    Args:
        examples: Stored example references in stored order (see module
            docstring); a non-list resolves to no examples
        model_image_limit: The resolved Model_Image_Limit (>= 1)

    Returns:
        (attached, omitted) — both lists of the same reference dicts,
        in attachment order
    """
    if not isinstance(examples, list):
        return [], []

    good = [e for e in examples
            if isinstance(e, dict) and e.get('designation') == FEW_SHOT_GOOD]
    # Everything that is not a good example keeps its stored order after
    # the good ones, so no reference is silently dropped.
    rest = [e for e in examples
            if not (isinstance(e, dict) and e.get('designation') == FEW_SHOT_GOOD)]
    ordered = good + rest

    try:
        limit = int(model_image_limit)
    except (TypeError, ValueError):
        limit = MODEL_IMAGE_LIMIT_DEFAULT
    slots = max(0, limit - 1)
    return ordered[:slots], ordered[slots:]


# ---------------------------------------------------------------------------
# Converse request construction
# ---------------------------------------------------------------------------

def image_format_for_key(key: str) -> str:
    """
    Converse image format for an object key — the Auto_Labeler's
    existing rule: 'png' for `.png` keys (case-insensitive), 'jpeg' for
    everything else.

    Args:
        key: The object key or reference

    Returns:
        'png' or 'jpeg'
    """
    if isinstance(key, str) and key.lower().endswith('.png'):
        return _IMAGE_FORMAT_PNG
    return _IMAGE_FORMAT_JPEG


def _image_block(image: Dict) -> Dict:
    """One Converse image content block, in the exact shape the
    Auto_Labeler has always emitted."""
    return {
        'image': {
            'format': image.get('format') or _IMAGE_FORMAT_JPEG,
            'source': {'bytes': image['bytes']},
        },
    }


def few_shot_identification_text(designation: str, ordinal: int) -> str:
    """
    The text block identifying one Few_Shot_Example to the model
    (Requirement 6.5), e.g. ``'Good example 1:'``.

    Args:
        designation: 'good' | 'bad' (anything else is identified as a
            bad example, matching select_few_shot_examples' grouping)
        ordinal: 1-based index within the designation

    Returns:
        The identification text
    """
    label = _DESIGNATION_LABELS.get(designation, _DESIGNATION_LABELS[FEW_SHOT_BAD])
    return f'{label} {ordinal}:'


def build_llm_request(modality: str, label_set: List[str],
                      detection_prompt: str, width: int, height: int,
                      per_label_prompts: Optional[Dict[str, str]],
                      target_image: Dict,
                      few_shot_images: Optional[List[Dict]] = None) -> Dict:
    """
    Build the Converse `messages` list and the prompt text for one image.

    The prompt is ``build_detection_prompt(...)`` verbatim — identical
    for every job whether or not Few_Shot_Examples are attached, so the
    Detection_Prompt reaches the model character-for-character
    (Requirements 3.1, 10.2).

    Content layout::

        few-shot empty:  [ {'image': target}, {'text': prompt} ]
        few-shot set:    [ {'text': FEW_SHOT_HEADER},
                           {'text': 'Good example 1:'}, {'image': good1},
                           ... ,
                           {'text': 'Bad example 1:'},  {'image': bad1},
                           ... ,
                           {'text': FEW_SHOT_TARGET_INTRO},
                           {'image': target}, {'text': prompt} ]

    So a request without Few_Shot_Examples is byte-identical to the
    pre-feature request (Requirement 10.2), and a request with them
    keeps the same `target image then prompt` suffix while identifying
    every example as good or bad in a block immediately preceding it
    (Requirement 6.5). Nothing but image bytes and derived text ever
    enters the content list — no credentials, URLs or ARNs
    (Requirement 3.4).

    Args:
        modality: 'Segmentation' | 'ObjectDetection' | 'Classification'
        label_set: The job's ordered class names
        detection_prompt: The Job_Creator's Detection_Prompt, inserted
            character-for-character
        width: Target image width in pixels
        height: Target image height in pixels
        per_label_prompts: Optional per-label guidance (skip-verification
            jobs), inserted character-for-character
        target_image: The image being labeled (see module docstring)
        few_shot_images: The attached Few_Shot_Examples in attachment
            order, each carrying its 'designation'; None or empty means
            no few-shot content at all

    Returns:
        ``{'messages': [{'role': 'user', 'content': [...]}],
        'prompt': prompt}``
    """
    prompt = build_detection_prompt(modality, label_set, detection_prompt,
                                    width, height, per_label_prompts)

    content: List[Dict] = []
    if few_shot_images:
        content.append({'text': FEW_SHOT_HEADER})
        ordinals: Dict[str, int] = {}
        for example in few_shot_images:
            designation = example.get('designation') or FEW_SHOT_BAD
            if designation not in _DESIGNATION_LABELS:
                designation = FEW_SHOT_BAD
            ordinals[designation] = ordinals.get(designation, 0) + 1
            content.append({
                'text': few_shot_identification_text(designation,
                                                     ordinals[designation]),
            })
            content.append(_image_block(example))
        content.append({'text': FEW_SHOT_TARGET_INTRO})

    content.append(_image_block(target_image))
    content.append({'text': prompt})

    return {
        'messages': [{'role': 'user', 'content': content}],
        'prompt': prompt,
    }
