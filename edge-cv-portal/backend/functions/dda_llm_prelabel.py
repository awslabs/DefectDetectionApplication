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
from typing import Dict, List, Optional

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
    GuidanceError,
    guidance_to_prelabel,
    parse_guidance,
)
from dda_llm_request import (  # noqa: E402
    MODEL_IMAGE_LIMIT_DEFAULT,
    build_llm_request,
    image_format_for_key,
    select_few_shot_examples,
)

logger = logging.getLogger()

# A model invocation exceeding 120 seconds is a Pre_Label generation
# failure — the Auto_Labeler's existing bound, now shared with the
# Preview_API (Req 3.3).
LLM_MAX_TIMEOUT_SECONDS = 120

# The failure categories this module can produce (Req 9.1, 9.2, 9.3).
# The preview executor adds its own pre-invocation categories
# (image_access_failure, unsupported_image_content,
# unreadable_example_image), which imply zero invocations.
CATEGORY_MODEL_ERROR = 'model_error'
CATEGORY_TIMEOUT = 'timeout'
CATEGORY_UNUSABLE_MODEL_OUTPUT = 'unusable_model_output'


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
                          ) -> Dict:
    """
    One Converse request for one image, then Coordinate_Guidance parsing
    and Pre_Label conversion.

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
            format ('png' for `.png` keys, else 'jpeg')
        width: Target image width in pixels
        height: Target image height in pixels
        few_shot_images: The Few_Shot_Examples to attach, already
            resolved to bytes by the caller, each
            `{'bytes', 'format'?, 'designation'}`; None or empty attaches
            nothing and yields the pre-feature request (Req 10.2). The
            deterministic good-then-bad, `model_image_limit`-bounded
            prefix is re-applied here, so the request can never exceed
            the bound whatever the caller passes (Req 7.2, 7.3, 7.4)
        model_image_limit: The resolved Model_Image_Limit (>= 1)

    Returns:
        The modality's Pre_Label dict, as `guidance_to_prelabel` produces
        it

    Raises:
        LlmPrelabelError: 'timeout' when the invocation exceeds the
            bound, 'model_error' for any other invocation failure,
            'unusable_model_output' when the response cannot be parsed,
            validated or converted (carrying `raw_text`)
    """
    # Bound the attached set exactly as the selection contract defines
    # it, so preview and Auto_Labeler send identical image sets and the
    # per-request image count is always within the Model_Image_Limit
    # (Req 7.2, 7.6). Already-selected input is unchanged by this.
    attached, _omitted = select_few_shot_examples(
        list(few_shot_images or []), model_image_limit)

    request = build_llm_request(
        modality, label_set, detection_prompt, width, height,
        per_label_prompts,
        {'bytes': image_bytes, 'format': image_format_for_key(image_key)},
        attached,
    )

    # Client via bedrock_common: read timeout equals the invocation
    # timeout capped at 120 s, retries disabled — so exactly one request
    # per image and no re-invocation (Req 3.1, 3.3).
    config = get_bedrock_configuration()
    timeout = min(int(config['timeout_seconds']), LLM_MAX_TIMEOUT_SECONDS)
    client = get_bedrock_client(config['region'], timeout)
    try:
        response = client.converse(
            modelId=model_identifier,
            messages=request['messages'],
            inferenceConfig=build_inference_config(config),
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
    text = response_text(response)
    try:
        detections = parse_guidance(text, label_set, width, height)
        return guidance_to_prelabel(detections, modality, label_set,
                                    width, height)
    except GuidanceError as exc:
        raise LlmPrelabelError(CATEGORY_UNUSABLE_MODEL_OUTPUT, str(exc),
                               raw_text=text) from exc
