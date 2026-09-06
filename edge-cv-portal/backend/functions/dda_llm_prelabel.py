"""
dda_llm_prelabel — the one implementation of the `llm:` family model
invocation, shared by the Auto_Labeler and the Prompt_Tuning_Preview
(llm-autolabel-prompt-tuning, Requirements 3.1, 3.2, 3.3, 3.10, 3.11,
9.1, 9.2, 9.3).

This is the literal extraction of `dda_autolabel_worker`'s
`_generate_llm_prelabel` body from prompt construction through Pre_Label
conversion: build the Converse request, issue exactly one invocation,
then strictly parse the Coordinate_Guidance and convert it to the
modality's Pre_Label. Because both callers run this code, a Preview_Run
is a faithful predictor of labeling-time behavior (Req 3.1, 3.2) —
faithfulness is a property of there being one implementation, not of two
implementations agreeing.

Scope of the module, deliberately narrow:

- **Request construction and layout** live in the shared layer
  (`dda_llm_request.build_llm_request`), so the content list is
  byte-identical for both callers and unchanged from the pre-feature
  request when no Few_Shot_Examples are attached (Req 10.2).
- **No I/O other than the Bedrock call.** Callers read the target image
  bytes and the attached Few_Shot_Example bytes themselves (each through
  its own cross-account mechanism) and pass them in; this module never
  touches S3, DynamoDB or the filesystem.
- **The failure taxonomy** is `LlmPrelabelError`, carrying the exact
  reason strings the Auto_Labeler records today so translating it back
  into `GenerationFailure(reason)` keeps `prelabel_error` values
  byte-identical for every existing failure mode.

This module lives in the functions bundle rather than the shared layer
because `bedrock_common` (Bedrock_Configuration + client construction)
is a functions-bundle module; `backend/functions` is one code asset, so
both `dda_labeling.py` and `dda_autolabel_worker.py` import it directly.

Requirements: 3.1, 3.2, 3.3, 3.10, 3.11, 9.1, 9.2, 9.3
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError

from bedrock_common import (
    build_inference_config,
    get_bedrock_client,
    get_bedrock_configuration,
)

# Shared layer (Lambda layer path)
import sys
sys.path.append('/opt/python')
from dda_llm_guidance import (  # noqa: E402
    MODALITY_CLASSIFICATION,
    GuidanceError,
    guidance_to_prelabel,
    parse_guidance,
    scale_detections,
)
from dda_llm_image import (  # noqa: E402
    DOWNSCALE_OFF,
    DownscaleError,
    downscale_image,
    normalize_downscale_setting,
)
from dda_llm_request import (  # noqa: E402
    MODEL_IMAGE_LIMIT_DEFAULT,
    build_llm_request,
    image_format_for_key,
    resolve_token_budget,
    select_few_shot_examples,
)

logger = logging.getLogger()

# A model invocation exceeding 120 seconds is a Pre_Label generation
# failure — the Auto_Labeler's existing bound, now shared with the
# Preview_API (Req 3.3).
LLM_MAX_TIMEOUT_SECONDS = 120

# The failure categories this module can produce (Req 9.1, 9.2, 9.3).
# The callers add their own pre-invocation category
# (image_access_failure), which implies zero invocations.
CATEGORY_MODEL_ERROR = 'model_error'
CATEGORY_TIMEOUT = 'timeout'
CATEGORY_UNUSABLE_MODEL_OUTPUT = 'unusable_model_output'

# Two more of the same closed, pre-existing six-category set, needed here
# because the Image_Downscaler's refusals now originate *inside* this
# shared module rather than in each caller — which is what makes the
# reason strings identical on both paths automatically
# (llm-model-token-and-image-sizing Req 9.1, 9.2, 8.5). No category is
# added: both are categories the callers already produce.
CATEGORY_UNSUPPORTED_IMAGE = 'unsupported_image_content'
CATEGORY_UNREADABLE_EXAMPLE = 'unreadable_example_image'


class LlmPrelabelResult(NamedTuple):
    """One image's Pre_Label plus the dimensions of the image actually
    sent to the model (llm-model-token-and-image-sizing Req 5.10, 7.1).

    Deliberately a 3-field named tuple rather than a dict: the worker
    ignores the dimensions and reads only `result.prelabel`, while the
    Preview_API reports `sent_width` / `sent_height` beside the
    Source_Dimensions it already knows.

    Attributes:
        prelabel: The modality's Pre_Label dict, exactly as
            `guidance_to_prelabel` produces it, in **Source** coordinate
            space (Req 7.4)
        sent_width: Width of the Downscaled_Image actually sent; equal to
            the Source_Dimensions width at Downscale_Off (Req 7.1)
        sent_height: Height of the Downscaled_Image actually sent
    """

    prelabel: Dict
    sent_width: int
    sent_height: int


class LlmPrelabelError(Exception):
    """
    A Pre_Label generation failure attributable to the model invocation
    or its output.

    Attributes:
        category: 'model_error' | 'timeout' | 'unusable_model_output'
        reason: The failure reason, character-for-character the string
            the Auto_Labeler records today for this failure mode
        raw_text: The model's raw text output when a response was
            received (only `unusable_model_output` can carry it), else
            None
    """

    def __init__(self, category: str, reason: str,
                 raw_text: Optional[str] = None):
        super().__init__(reason)
        self.category = category
        self.reason = reason
        self.raw_text = raw_text


def _refusal_reason(subject: str, exc: DownscaleError,
                    max_image_edge: int) -> str:
    """The failure reason for one Image_Downscaler refusal.

    Two shapes, selected without parsing any text: `DownscaleError`
    carries a `pixel_count` **only** for the Max_Source_Pixel_Count
    refusal, whose reason already reads
    ``declares {w}x{h} = {n} pixels, above the {max} pixel limit`` and so
    is appended to the subject directly. Every other refusal is reported
    as a resize failure naming the requested bound, with the downscaler's
    reason as the cause.

    Args:
        subject: The image being reported, already in its final form —
            `unsupported image content: {key}` for a target image,
            `few-shot example image {ref}` for an attached example
        exc: The Image_Downscaler's refusal
        max_image_edge: The requested Max_Image_Edge

    Returns:
        The reason string
    """
    if exc.pixel_count is not None:
        return f'{subject} {exc.reason}'
    return (f'{subject} could not be resized to a longer edge of '
            f'{max_image_edge} pixels: {exc.reason}')


def _example_reference(example: Dict, index: int) -> str:
    """How one attached Few_Shot_Example is named in a failure reason.

    The stored reference when the caller carried it through, else the
    example's attachment position, so the reason always identifies which
    example failed even for callers that pass image bytes alone.
    """
    ref = example.get('ref') if isinstance(example, dict) else None
    if isinstance(ref, str) and ref.strip():
        return ref.strip()
    return f'at position {index + 1}'


def _downscale_target(image_bytes: bytes, image_key: str,
                      width: int, height: int, max_image_edge: int,
                      ) -> Tuple[bytes, int, int]:
    """The target image's Downscaled_Image and Sent_Dimensions.

    The already-known Source_Dimensions are passed through so the header
    is never parsed a second time (Req 7.6). A refusal is an
    `unsupported_image_content` failure with no invocation (Req 9.1, 9.2).
    """
    try:
        return downscale_image(image_bytes, image_format_for_key(image_key),
                               max_image_edge,
                               source_dimensions=(width, height))
    except DownscaleError as exc:
        raise LlmPrelabelError(
            CATEGORY_UNSUPPORTED_IMAGE,
            _refusal_reason(f'unsupported image content: {image_key}',
                            exc, max_image_edge)) from exc


def _downscale_examples(attached: List[Dict],
                        max_image_edge: int) -> List[Dict]:
    """Every attached Few_Shot_Example downscaled with the target's
    setting, exactly once each (Req 8.1).

    Example dimensions reach neither the prompt nor the
    Coordinate_Guidance bounds (Req 8.2), so only the bytes are replaced;
    `designation` and every other key are carried through untouched so
    the content layout is unchanged. A refusal is an
    `unreadable_example_image` failure with no invocation (Req 8.5).
    """
    downscaled: List[Dict] = []
    for index, example in enumerate(attached):
        try:
            example_bytes, _sent_w, _sent_h = downscale_image(
                example.get('bytes'), example.get('format'), max_image_edge)
        except DownscaleError as exc:
            reference = _example_reference(example, index)
            raise LlmPrelabelError(
                CATEGORY_UNREADABLE_EXAMPLE,
                _refusal_reason(f'few-shot example image {reference}',
                                exc, max_image_edge)) from exc
        downscaled.append(dict(example, bytes=example_bytes))
    return downscaled


def response_text(response: Dict) -> str:
    """
    The model's text output, joined across text blocks — the
    Auto_Labeler's existing extraction, including its reason string for
    a response carrying no text at all.

    Args:
        response: The Converse response

    Returns:
        The concatenated text output

    Raises:
        LlmPrelabelError: category 'unusable_model_output' when the
            response contains no text block
    """
    content = (((response or {}).get('output') or {})
               .get('message') or {}).get('content') or []
    texts = [block['text'] for block in content
             if isinstance(block, dict) and isinstance(block.get('text'), str)]
    if not texts:
        raise LlmPrelabelError(
            CATEGORY_UNUSABLE_MODEL_OUTPUT,
            'model response contained no text output')
    return '\n'.join(texts)


def generate_llm_prelabel(*, model_identifier: str, modality: str,
                          label_set: List[str], detection_prompt: str,
                          per_label_prompts: Optional[Dict[str, str]],
                          image_bytes: bytes, image_key: str,
                          width: int, height: int,
                          few_shot_images: Optional[List[Dict]] = None,
                          model_image_limit: int = MODEL_IMAGE_LIMIT_DEFAULT,
                          # --- new, all defaulting to pre-feature behavior ---
                          downscale_setting: Optional[int] = None,
                          token_budget_selection: Any = None,
                          model_token_limits: Optional[Dict[str, Any]] = None,
                          ) -> LlmPrelabelResult:
    """
    One Converse request for one image, then Coordinate_Guidance parsing,
    coordinate scale-back and Pre_Label conversion.

    Exactly one invocation is issued regardless of outcome: the client
    comes from `bedrock_common.get_bedrock_client` with the read timeout
    clamped to `min(config['timeout_seconds'], 120)` and retries
    disabled, so total wall time cannot exceed the bound and no
    re-invocation can happen (Req 3.1, 3.3).

    Only image bytes and derived prompt text reach the model — no
    dataset credentials and no portal secrets (Req 3.4).

    Args:
        model_identifier: The LLM_Auto_Label_Model identifier (the part
            after the `llm:` prefix), used as the Converse `modelId`
        modality: 'Segmentation' | 'ObjectDetection' | 'Classification'
        label_set: The job's ordered class names
        detection_prompt: The Detection_Prompt, inserted
            character-for-character
        per_label_prompts: Per_Label_Prompts (skip-verification jobs
            only), or None
        image_bytes: The target image's bytes
        image_key: The target image's object key, for the Converse image
            format ('png' for `.png` keys, else 'jpeg'), and for the
            reason string of a downscale refusal
        width: Target image width in pixels — the **Source_Dimensions**
            width, i.e. the dimensions of the object in storage, which is
            what this argument has always carried
        height: Target image height in pixels — the Source_Dimensions
            height
        few_shot_images: The Few_Shot_Examples to attach, already
            resolved to bytes by the caller, each
            `{'bytes', 'format'?, 'designation'}`; None or empty attaches
            nothing and yields the pre-feature request (Req 10.2). The
            deterministic good-then-bad, `model_image_limit`-bounded
            prefix is re-applied here, so the request can never exceed
            the bound whatever the caller passes (Req 7.2, 7.3, 7.4)
        model_image_limit: The resolved Model_Image_Limit (>= 1)
        downscale_setting: None (Downscale_Off) or one Max_Image_Edge,
            applied through `dda_llm_image.downscale_image` to the target
            image and to every attached Few_Shot_Example, exactly once
            each, after selection and before any image becomes a Converse
            block (llm-model-token-and-image-sizing Req 6.1, 8.1, 8.3).
            At Downscale_Off the downscaler is not called at all, nothing
            is decoded and the request is byte-identical to the
            pre-feature request (Req 10.1)
        token_budget_selection: The Token_Budget_Selection for this
            request, of any type; resolved here through
            `resolve_token_budget` so the request's `maxTokens` is the
            resolver's output by construction and cannot diverge between
            the two callers (Req 1.3, 1.4)
        model_token_limits: The Model_Token_Limits mapping, or None

    Returns:
        `LlmPrelabelResult(prelabel, sent_width, sent_height)` — the
        modality's Pre_Label exactly as `guidance_to_prelabel` produces
        it, in Source coordinate space, plus the target's
        Sent_Dimensions (Req 5.10, 7.4)

    Raises:
        LlmPrelabelError: 'timeout' when the invocation exceeds the
            bound, 'model_error' for any other invocation failure,
            'unusable_model_output' when the response cannot be parsed,
            validated or converted (carrying `raw_text`),
            'unsupported_image_content' when the Image_Downscaler refuses
            the target image (Req 9.1, 9.2) and
            'unreadable_example_image' when it refuses an attached
            example (Req 8.5). The two downscale categories are
            pre-existing members of the closed category set and both
            imply zero invocations
    """
    # Bound the attached set exactly as the selection contract defines
    # it, so preview and Auto_Labeler send identical image sets and the
    # per-request image count is always within the Model_Image_Limit
    # (Req 7.2, 7.6). Already-selected input is unchanged by this.
    attached, _omitted = select_few_shot_examples(
        list(few_shot_images or []), model_image_limit)

    # Downscaling happens after selection — which images are attached is
    # independent of the Downscale_Setting (Req 8.3) — and before any
    # image becomes a Converse block, so every image is downscaled
    # exactly once (Req 6.1, 8.1). Downscale_Off short-circuits the
    # downscaler entirely: nothing is decoded, no bytes object is
    # replaced and no list is rebuilt, which is what makes the request
    # byte-identical to the pre-feature request (Req 10.1).
    max_image_edge = normalize_downscale_setting(downscale_setting)
    if max_image_edge is DOWNSCALE_OFF:
        target_bytes, sent_width, sent_height = image_bytes, width, height
        attached_images = attached
    else:
        target_bytes, sent_width, sent_height = _downscale_target(
            image_bytes, image_key, width, height, max_image_edge)
        attached_images = _downscale_examples(attached, max_image_edge)

    # The prompt describes the image that is actually sent (Req 7.1).
    request = build_llm_request(
        modality, label_set, detection_prompt, sent_width, sent_height,
        per_label_prompts,
        {'bytes': target_bytes, 'format': image_format_for_key(image_key)},
        attached_images,
    )

    # Client via bedrock_common: read timeout equals the invocation
    # timeout capped at 120 s, retries disabled — so exactly one request
    # per image and no re-invocation (Req 3.1, 3.3).
    config = get_bedrock_configuration()
    timeout = min(int(config['timeout_seconds']), LLM_MAX_TIMEOUT_SECONDS)
    client = get_bedrock_client(config['region'], timeout)

    # `build_inference_config` is called unchanged and its returned dict is
    # never mutated in place — only `maxTokens` is replaced, on a copy — so
    # the Global_Max_Tokens is decoupled for this family while every other
    # Bedrock_Consumer and the sampling-parameter exclusivity rule are
    # untouched (Req 1.3, 1.5, 10.2, 10.5, 10.8).
    inference_config = dict(build_inference_config(config))
    inference_config['maxTokens'] = resolve_token_budget(
        model_identifier, token_budget_selection, model_token_limits)
    try:
        response = client.converse(
            modelId=model_identifier,
            messages=request['messages'],
            inferenceConfig=inference_config,
        )
    except (ReadTimeoutError, ConnectTimeoutError) as exc:
        # Req 3.10, 9.2: timeout is distinguishable from a model error.
        raise LlmPrelabelError(
            CATEGORY_TIMEOUT,
            f'model invocation timed out after {timeout}s') from exc
    except Exception as exc:  # noqa: BLE001 — model error (Req 3.10, 9.1)
        raise LlmPrelabelError(
            CATEGORY_MODEL_ERROR, f'model error: {exc}') from exc

    # Strict parse + modality conversion; the GuidanceError reason
    # reaches the caller unchanged, with the raw model text alongside it
    # character-for-character (Req 3.11, 9.3).
    #
    # The returned coordinates are validated against the Sent_Dimensions,
    # the space the model saw (Req 7.2), then mapped back into Source
    # space for the geometry modalities only — Classification carries no
    # coordinates and must never reach `scale_detections` (Req 7.3, 7.8).
    # `scale_detections` returns the same list object when the two
    # dimension pairs are equal, so Downscale_Off and an already-fitting
    # source produce the pre-feature Pre_Label bit-for-bit (Req 7.5).
    text = response_text(response)
    try:
        detections = parse_guidance(text, label_set, sent_width, sent_height)
        if modality != MODALITY_CLASSIFICATION:
            detections = scale_detections(detections, sent_width, sent_height,
                                          width, height)
        prelabel = guidance_to_prelabel(detections, modality, label_set,
                                        width, height)
    except GuidanceError as exc:
        raise LlmPrelabelError(CATEGORY_UNUSABLE_MODEL_OUTPUT, str(exc),
                               raw_text=text) from exc
    return LlmPrelabelResult(prelabel=prelabel, sent_width=sent_width,
                             sent_height=sent_height)
