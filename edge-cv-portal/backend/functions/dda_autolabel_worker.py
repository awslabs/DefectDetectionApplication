"""
dda_autolabel_worker — SQS consumer of the auto-label fan-out queue
(dda-data-labeling, task 10.1).

The Task Distributor (dda_labeling_worker.py) enqueues one message per
dataset image on `dda-portal-autolabel-queue`:

    {job_id, task_id, image_s3_uri, modality, label_set, model,
     per_label_prompts?}

For each record this worker generates a Pre_Label with the selected
Auto_Labeler model and resolves the task's `prelabel_status`:

- **Bedrock path** (model `bedrock:<model_id>`): a Converse request with
  the image block and a structured prompt (job Label_Set; in
  skip-verification mode the Per_Label_Prompts, one section per label)
  demanding a JSON answer — `{"label": "normal"|"anomaly"}` for
  Classification, `{"boxes": [{"class", "left", "top", "width",
  "height"}]}` for ObjectDetection. The client comes from
  `bedrock_common.get_bedrock_client` with the read timeout capped at
  120 s and retries disabled (Req 8.5). The response is strictly
  validated: any class outside the Label_Set, malformed geometry, or an
  out-of-bounds box is a generation failure for that image (Req 8.2).
- **LLM path** (model `llm:<model_identifier>`, llm-auto-labeling):
  delegated to `dda_llm_prelabel.generate_llm_prelabel`, the one
  implementation the Prompt_Tuning_Preview also calls
  (llm-autolabel-prompt-tuning Req 3.1, 3.2) — a
  single Converse request per image with the image block and a
  prompt built by `dda_llm_guidance.build_detection_prompt` (the job's
  Detection_Prompt verbatim, the Label_Set, the pixel dimensions, and
  in skip-verification mode the Per_Label_Prompts). The strict
  `parse_guidance` validates the returned Coordinate_Guidance JSON and
  `guidance_to_prelabel` converts it to the modality's existing
  Pre_Label shape. The job record's persisted Downscale_Setting
  (`auto_label.downscale_max_edge`) and Token_Budget_Selection
  (`auto_label.token_budget`) are read beside the few-shot document and
  passed into the same chokepoint, with the Model_Token_Limits mapping
  from the persisted settings item over the LLM_MODEL_TOKEN_LIMITS
  bootstrap (llm-model-token-and-image-sizing Req 3.7, 5.8); malformed
  or absent values degrade to Downscale_Off and the default budget with
  no failure. A timeout is recorded distinguishably from a model
  error, and only image and prompt content are sent to the model.
- **SAM path** (model `sam`): synchronous invoke of the dda_sam_worker
  container Lambda (env SAM_WORKER_FUNCTION_NAME) with a 15-minute
  presigned image URL; the `{regions: [{class: null, rle}],
  image_width, image_height}` response is stored as a class-agnostic
  pre-label. The invocation wall clock is bounded at 120 s.
- **Grounded-SAM path** (model `grounded-sam`, grounded-sam-autolabel
  spec): synchronous invoke of the dda_grounded_sam_worker container
  Lambda (env GROUNDED_SAM_WORKER_FUNCTION_NAME) with a 15-minute
  presigned image URL, the Prompt_Map derived from the message's
  Label_Set and the job record's `auto_label.prompt_overrides` (a
  label's override is used when it is a non-empty string after
  trimming, otherwise the label name itself), and the modality. Unlike
  `sam`, the returned regions carry Label_Set classes, so the stored
  Pre_Label is classified: Segmentation stores `{regions: [{class,
  rle, score?}], image_width, image_height}`; ObjectDetection maps the
  worker's per-region `box` to the Bedrock-shape `{boxes: [{class,
  left, top, width, height}], image_width, image_height}` (score
  dropped). The invocation wall clock is bounded at 240 s (CPU
  Grounding DINO latency; the sam family keeps its 120 s bound).

Successful generations write the pre-label JSON to the portal artifacts
bucket at `labeling/{usecase_id}/{job_id}/prelabels/{task_id}.json` and
conditionally update the task item to `prelabel_status=Available`;
failures mark it `Failed` with `prelabel_error` (plus `autolabel_error`
in skip-verification mode, rendering the image review-ineligible,
Req 9.10). In skip-verification mode every resolution also atomically
decrements the job's `autolabel_pending` counter and sets
`review_ready=true` when it reaches zero (Req 9.4, 9.5).

Dataset images are read through the portal's cross-account mechanism
(`shared_utils.get_s3_client_for_bucket`, with the single-account
direct-access fallback) after resolving the Use_Case from the job item
(Req 12.1, 12.2).

Batch semantics (partial batch responses, ReportBatchItemFailures):
generation failures are caught per record, marked Failed, and the loop
continues — one bad image never fails the batch. Only unexpected
infrastructure errors (e.g. a DynamoDB write failure) surface the
record's messageId in `batchItemFailures` so SQS redrive semantics
apply to that record alone.

Requirements: 8.2, 8.5, 8.6, 9.4, 9.10, 12.1, 12.2
"""
import json
import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Import shared utilities (Lambda layer path)
import sys
sys.path.append('/opt/python')
from shared_utils import get_s3_client_for_bucket, get_usecase
from bedrock_common import (
    build_inference_config,
    get_bedrock_client,
    get_bedrock_configuration,
)
# The one implementation of the `llm:` family model invocation, shared
# with the Prompt_Tuning_Preview (llm-autolabel-prompt-tuning Req 3.1,
# 3.2). Same functions bundle, so it is imported directly.
import dda_llm_prelabel
from dda_llm_prelabel import LlmPrelabelError, generate_llm_prelabel
# The one copy of the PNG IHDR / JPEG SOF header parser, relocated
# verbatim to the shared layer (llm-model-token-and-image-sizing
# Req 7.6); `_image_dimensions` below is a thin delegation to it.
# `normalize_downscale_setting` makes the job record's Downscale_Setting
# total and safe: a malformed, null or absent `downscale_max_edge`
# resolves to Downscale_Off with no failure (Req 5.9, 5.12, 10.10).
# `dda_llm_image` imports no Pillow at import time, so attaching it here
# costs nothing.
from dda_llm_image import declared_dimensions, normalize_downscale_setting
# Few_Shot_Example selection and the Model_Image_Limit contract live in
# the shared layer, so labeling time and the Prompt_Tuning_Preview
# attach the same subset in the same order
# (llm-autolabel-prompt-tuning Req 7.2, 7.3, 7.4, 7.6).
from dda_llm_request import (
    image_format_for_key,
    resolve_model_image_limit,
    select_few_shot_examples,
)

# AWS clients
dynamodb = boto3.resource('dynamodb')
s3_client = boto3.client('s3')

# Environment configuration
LABELING_JOBS_TABLE = os.environ.get('LABELING_JOBS_TABLE', 'LabelingJobs')
LABELING_TASKS_TABLE = os.environ.get(
    'LABELING_TASKS_TABLE', 'dda-portal-labeling-tasks')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
SAM_WORKER_FUNCTION_NAME = os.environ.get('SAM_WORKER_FUNCTION_NAME')
GROUNDED_SAM_WORKER_FUNCTION_NAME = os.environ.get(
    'GROUNDED_SAM_WORKER_FUNCTION_NAME', '')
# Portal settings table holding the persisted Model_Token_Limits item —
# the same table bedrock_common already reads the Bedrock_Configuration
# from (llm-model-token-and-image-sizing Req 1.6, 1.8).
SETTINGS_TABLE = os.environ.get('SETTINGS_TABLE')

jobs_table = dynamodb.Table(LABELING_JOBS_TABLE)
tasks_table = dynamodb.Table(LABELING_TASKS_TABLE)

# Model invocation bounds (Req 8.5: a model invocation exceeding 120
# seconds is a Pre_Label generation failure).
BEDROCK_MAX_TIMEOUT_SECONDS = 120
SAM_MAX_TIMEOUT_SECONDS = 120
# Grounded-SAM family bound (grounded-sam-autolabel Req 4.3): 240 s —
# Grounding DINO on Lambda CPU runs tens of seconds per forward, and
# the Segmentation mask pass adds more; a distinct constant keeps the
# sam family's 120 s bound byte-identical (Req 7.4).
GROUNDED_SAM_MAX_TIMEOUT_SECONDS = 240
# 15-minute presigned URL for the SAM worker's image fetch (Req 12.6
# convention for time-limited single-object grants).
PRESIGNED_URL_EXPIRY_SECONDS = 900

# Model family whose pre-label storage failures are terminal (Req 6.2,
# 6.5): see _process_message.
LLM_MODEL_PREFIX = 'llm:'

MODALITY_CLASSIFICATION = 'Classification'
MODALITY_SEGMENTATION = 'Segmentation'
MODALITY_OBJECT_DETECTION = 'ObjectDetection'
BEDROCK_MODALITIES = (MODALITY_CLASSIFICATION, MODALITY_OBJECT_DETECTION)
SAM_MODALITIES = (MODALITY_SEGMENTATION, MODALITY_OBJECT_DETECTION)
GROUNDED_SAM_MODALITIES = (MODALITY_SEGMENTATION, MODALITY_OBJECT_DETECTION)

# Test injection point: when set, used instead of a boto3 Lambda client
# for synchronous SAM worker invocations.
sam_lambda_client = None
_cached_sam_lambda_client = None

# Test injection point: when set, used instead of a boto3 Lambda client
# for synchronous Grounded-SAM worker invocations.
grounded_sam_lambda_client = None
_cached_grounded_sam_lambda_client = None


class MalformedMessage(Exception):
    """An SQS record that can never be processed (log and drop)."""


class GenerationFailure(Exception):
    """Pre_Label generation failed for this image (Req 8.5): mark the
    task Failed and continue with the rest of the batch."""


def _now() -> int:
    return int(datetime.utcnow().timestamp())


def _parse_s3_uri(uri: str) -> Tuple[str, str]:
    if not isinstance(uri, str) or not uri.startswith('s3://'):
        raise GenerationFailure(f"invalid image S3 URI {uri!r}")
    remainder = uri[len('s3://'):]
    bucket, _, key = remainder.partition('/')
    if not bucket or not key:
        raise GenerationFailure(f"invalid image S3 URI {uri!r}")
    return bucket, key


def _image_dimensions(image_bytes: bytes) -> Optional[Tuple[int, int]]:
    """(width, height) parsed from PNG IHDR / JPEG SOF headers, or None.

    A thin delegation to `dda_llm_image.declared_dimensions` — the same
    algorithm this function has always used, relocated verbatim to the
    shared layer so exactly one copy exists
    (llm-model-token-and-image-sizing Req 7.6). Accepts exactly the
    inputs it accepted before, stays dependency-free (no Pillow), and is
    still used to validate bounding boxes against the image bounds
    (Req 8.2).
    """
    return declared_dimensions(image_bytes)


def _image_format(key: str) -> str:
    return 'png' if key.lower().endswith('.png') else 'jpeg'


# ---------------------------------------------------------------------------
# Message parsing and image access
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ('job_id', 'task_id', 'image_s3_uri', 'modality',
                    'label_set', 'model')


def _parse_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Parse and structurally validate one SQS record body."""
    try:
        message = json.loads(record.get('body') or '')
    except (json.JSONDecodeError, ValueError) as exc:
        raise MalformedMessage(f'unparseable message body: {exc}') from exc
    if not isinstance(message, dict):
        raise MalformedMessage('message body is not a JSON object')
    missing = [field for field in _REQUIRED_FIELDS
               if not message.get(field)]
    if missing:
        raise MalformedMessage(
            f"message is missing required fields: {', '.join(missing)}")
    if (not isinstance(message['label_set'], list)
            or not all(isinstance(label, str)
                       for label in message['label_set'])):
        raise MalformedMessage('label_set must be a list of class names')
    return message


def _dataset_s3_client(job: Dict, bucket: str):
    """S3 client for the Use_Case's data bucket via the cross-account
    role mechanism with the direct-access fallback (Req 12.1, 12.2)."""
    try:
        usecase = get_usecase(job['usecase_id'])
    except ValueError as exc:
        raise GenerationFailure(
            f"use case {job.get('usecase_id')!r} not found") from exc
    try:
        return get_s3_client_for_bucket(usecase, bucket, 'dda-autolabel')
    except Exception as exc:  # noqa: BLE001 — role assumption failed
        raise GenerationFailure(
            f'could not obtain credentials for bucket {bucket}: {exc}'
        ) from exc


def _read_image_bytes(job: Dict, image_s3_uri: str) -> Tuple[bytes, str]:
    bucket, key = _parse_s3_uri(image_s3_uri)
    dataset_s3 = _dataset_s3_client(job, bucket)
    try:
        body = dataset_s3.get_object(Bucket=bucket, Key=key)['Body'].read()
    except Exception as exc:  # noqa: BLE001 — inaccessible object (Req 12.3)
        raise GenerationFailure(
            f'image s3://{bucket}/{key} is not accessible: {exc}') from exc
    return body, key


# ---------------------------------------------------------------------------
# Model_Token_Limits delivery (llm-model-token-and-image-sizing Req 1.6,
# 1.8, 3.7, 3.8) — the same loader shape dda_labeling.py and
# data_accounts.py carry, so all three read equal entries for equal
# persisted configuration.
# ---------------------------------------------------------------------------

# The persisted Model_Token_Limits mapping lives in the portal settings
# table under this key, maintained by data_accounts.py independently of
# the bedrock_configuration item.
LLM_MODEL_TOKEN_LIMITS_SETTING_KEY = 'llm_model_token_limits'

# Per-invocation memo of the effective Model_Token_Limits mapping.
# Cleared at the top of handler() so one SQS batch resolves against one
# self-consistent mapping and a warm container can never serve a stale
# mapping after an administrator's write — see _llm_model_token_limits().
_model_token_limits_cache: Optional[Dict[str, Any]] = None


def _reset_model_token_limits_cache() -> None:
    """Drop the per-invocation Model_Token_Limits memo.

    Called at the top of handler(), so the mapping is re-read at the
    start of every invocation.
    """
    global _model_token_limits_cache
    _model_token_limits_cache = None


def _decimal_to_native(obj):
    """Convert Decimal objects from DynamoDB to native Python types.

    DynamoDB returns every number as Decimal, and both
    `resolve_token_budget` and `normalize_downscale_setting` reject
    non-int types by design (Req 2.8) — without this conversion every
    persisted value would silently fall through to its default. A whole
    number becomes int; a fractional number becomes float, which the
    resolvers correctly treat as invalid.
    """
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    elif isinstance(obj, dict):
        return {k: _decimal_to_native(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_decimal_to_native(i) for i in obj]
    return obj


def _read_stored_model_token_limits() -> Optional[Dict[str, Any]]:
    """The persisted Model_Token_Limits mapping, or None when there is none.

    Returns None — meaning "fall back to the environment bootstrap" —
    when the settings table is not configured, the item is absent, the
    read fails, or the item's value is not a mapping. An empty persisted
    mapping is a real mapping and is returned as {} (Req 4.8), not as
    None.
    """
    if not SETTINGS_TABLE:
        return None
    try:
        response = dynamodb.Table(SETTINGS_TABLE).get_item(
            Key={'setting_key': LLM_MODEL_TOKEN_LIMITS_SETTING_KEY}
        )
    except Exception as exc:  # ClientError, table missing, throttling
        logger.warning('Could not read model token limits, falling back '
                       'to the environment bootstrap: %s', exc)
        return None

    item = response.get('Item')
    if not item:
        return None
    value = item.get('value')
    if not isinstance(value, dict):
        return None
    return _decimal_to_native(value)


def _env_model_token_limits() -> Dict[str, Any]:
    """The LLM_MODEL_TOKEN_LIMITS deploy-time bootstrap mapping.

    An absent, blank, malformed or non-object value resolves to an empty
    mapping, in which case every model resolves
    Model_Token_Limit_Default rather than erroring.
    """
    raw = (os.environ.get('LLM_MODEL_TOKEN_LIMITS') or '').strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning('LLM_MODEL_TOKEN_LIMITS is not valid JSON; using '
                       'the default Model_Token_Limit for every model')
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _llm_model_token_limits() -> Dict[str, Any]:
    """The effective Model_Token_Limits mapping, memoized per invocation.

    Source of truth is the persisted `llm_model_token_limits` settings
    item (Req 1.6, 4.1). When that item is absent, unreadable, or its
    value is not a mapping, the LLM_MODEL_TOKEN_LIMITS environment
    variable is used instead — the deploy-time bootstrap for an
    environment where no PortalAdmin has written the item yet.

    WHOLE-MAPPING precedence, never a per-key merge: a merge would let
    an environment entry survive a deletion from the persisted mapping,
    contradicting Req 4.1 ("retain no entry that the submitted mapping
    omits") and Req 4.8 (an empty mapping makes every model resolve the
    default). An empty persisted mapping is therefore honored as empty.
    """
    global _model_token_limits_cache
    if _model_token_limits_cache is None:
        stored = _read_stored_model_token_limits()
        _model_token_limits_cache = (stored if stored is not None
                                     else _env_model_token_limits())
    return _model_token_limits_cache


# ---------------------------------------------------------------------------
# Few_Shot_Examples (llm-autolabel-prompt-tuning Req 6.5, 6.7, 7.1-7.4, 10.3)
# ---------------------------------------------------------------------------

def _llm_model_image_limits() -> Dict[str, Any]:
    """The Model_Image_Limit configuration mapping from
    LLM_MODEL_IMAGE_LIMITS (Req 7.1).

    Read per call so the environment stays authoritative. An absent,
    blank, malformed or non-object value resolves to an empty mapping,
    in which case every model resolves the shared default of 20 rather
    than erroring — configuration can never widen or zero the bound.
    """
    raw = (os.environ.get('LLM_MODEL_IMAGE_LIMITS') or '').strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning('LLM_MODEL_IMAGE_LIMITS is not valid JSON; using '
                       'the default Model_Image_Limit for every model')
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _few_shot_ref_location(ref: str) -> Tuple[Optional[str], Optional[str]]:
    """(bucket, key) for one stored example reference. References are
    portal artifacts bucket keys (the wizard's presigned-PUT uploads) or
    full s3:// URIs — the two spellings the job record carries."""
    if not isinstance(ref, str) or not ref:
        return None, None
    if ref.startswith('s3://'):
        remainder = ref[len('s3://'):]
        bucket, _, key = remainder.partition('/')
        return (bucket or None), (key or None)
    return (PORTAL_ARTIFACTS_BUCKET or None), ref


def _resolve_few_shot_images(job: Dict,
                            model_identifier: str) -> Tuple[List[Dict], int]:
    """The Few_Shot_Example images to attach for this job, plus the
    resolved Model_Image_Limit.

    Few-shot state comes from the job record (`auto_label.few_shot`), so
    no SQS message schema change is needed and messages already in
    flight across a deployment keep processing. An absent, `null`,
    non-dict or `enabled`-falsy document, and an empty or malformed
    `examples` list, all resolve to *disabled* — no attachment and no
    failure attributed to the configuration (Req 10.3).

    When enabled, the attached subset is
    `select_few_shot_examples(stored examples, resolved limit)` — the
    deterministic good-then-bad, stored-order prefix the preview path
    computes from the same references (Req 7.2, 7.3, 7.4, 7.6) — and
    only those refs are read, through the same
    `get_s3_client_for_bucket` mechanism the dataset image uses. Omitted
    references are never fetched.

    Raises:
        GenerationFailure: an attached example that cannot be read,
            naming the reference (Req 6.7). It fails only this dataset
            image; the batch loop continues.
    """
    limit = resolve_model_image_limit(model_identifier,
                                      _llm_model_image_limits())

    few_shot = (job.get('auto_label') or {}).get('few_shot')
    if not isinstance(few_shot, dict) or not few_shot.get('enabled'):
        return [], limit
    stored = few_shot.get('examples')
    if not isinstance(stored, list):
        return [], limit
    # Only well-formed references participate; a document whose entries
    # are all malformed is indistinguishable from the option being off.
    candidates = [example for example in stored
                  if isinstance(example, dict)
                  and isinstance(example.get('ref'), str)
                  and example['ref']]
    if not candidates:
        return [], limit

    attached, _omitted = select_few_shot_examples(candidates, limit)

    clients: Dict[str, Any] = {}
    images: List[Dict] = []
    for example in attached:
        ref = example['ref']
        bucket, key = _few_shot_ref_location(ref)
        if not bucket or not key:
            raise GenerationFailure(
                f'few-shot example image {ref} is not accessible: '
                f'the reference could not be resolved to an S3 object')
        if bucket not in clients:
            clients[bucket] = _dataset_s3_client(job, bucket)
        try:
            body = clients[bucket].get_object(
                Bucket=bucket, Key=key)['Body'].read()
        except Exception as exc:  # noqa: BLE001 — unreadable example (Req 6.7)
            raise GenerationFailure(
                f'few-shot example image {ref} is not accessible: '
                f'{exc}') from exc
        images.append({
            'bytes': body,
            'format': image_format_for_key(key),
            'designation': example.get('designation'),
        })
    return images, limit


# ---------------------------------------------------------------------------
# Bedrock path (Req 8.2, 8.5, 9.4)
# ---------------------------------------------------------------------------

def _build_prompt(modality: str, label_set: List[str],
                  dimensions: Optional[Tuple[int, int]],
                  per_label_prompts: Optional[Dict[str, str]]) -> str:
    """Structured prompt demanding JSON output; Per_Label_Prompts are
    appended one section per label in skip-verification mode (Req 9.4)."""
    labels = ', '.join(label_set)
    if modality == MODALITY_CLASSIFICATION:
        lines = [
            'You are labeling images for a defect-detection dataset.',
            'Decide which single label applies to the image.',
            f'Allowed labels: {labels}.',
        ]
    else:
        width, height = dimensions
        lines = [
            'You are labeling images for a defect-detection dataset.',
            'Locate every object belonging to the allowed classes and '
            'report its bounding box in pixel coordinates.',
            f'Allowed class names: {labels}.',
            f'The image is {width} pixels wide and {height} pixels tall; '
            'every box must lie entirely within these bounds.',
        ]
    if per_label_prompts:
        for label in label_set:
            prompt = per_label_prompts.get(label)
            if prompt:
                lines.append(f"Guidance for label '{label}': {prompt}")
    if modality == MODALITY_CLASSIFICATION:
        lines.append(
            'Respond with ONLY a JSON object of the form '
            '{"label": "<one of the allowed labels>"} and no other text.')
    else:
        lines.append(
            'Respond with ONLY a JSON object of the form '
            '{"boxes": [{"class": "<allowed class>", "left": <px>, '
            '"top": <px>, "width": <px>, "height": <px>}]} and no other '
            'text. Use {"boxes": []} when no object is present.')
    return '\n'.join(lines)


def _response_text(response: Dict) -> str:
    content = (((response or {}).get('output') or {})
               .get('message') or {}).get('content') or []
    texts = [block['text'] for block in content
             if isinstance(block, dict) and isinstance(block.get('text'), str)]
    if not texts:
        raise GenerationFailure('model response contained no text output')
    return '\n'.join(texts)


def _extract_json(text: str) -> Dict:
    """The JSON object from the model's text output (code fences and
    surrounding prose tolerated; anything unparseable is a failure)."""
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end <= start:
        raise GenerationFailure('model output is not JSON')
    try:
        payload = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError) as exc:
        raise GenerationFailure(f'model output is not valid JSON: {exc}')
    if not isinstance(payload, dict):
        raise GenerationFailure('model output is not a JSON object')
    return payload


def _validate_classification(payload: Dict, label_set: List[str]) -> Dict:
    label = payload.get('label')
    if not isinstance(label, str) or label not in label_set:
        raise GenerationFailure(
            f"model returned label {label!r}, which is not in the job's "
            f"label set {label_set}")
    return {'modality': MODALITY_CLASSIFICATION, 'label': label}


def _validate_boxes(payload: Dict, label_set: List[str],
                    dimensions: Tuple[int, int]) -> Dict:
    width, height = dimensions
    raw_boxes = payload.get('boxes')
    if not isinstance(raw_boxes, list):
        raise GenerationFailure("model output has no 'boxes' list")
    boxes = []
    for raw in raw_boxes:
        if not isinstance(raw, dict):
            raise GenerationFailure('malformed box entry (not an object)')
        class_name = raw.get('class')
        if not isinstance(class_name, str) or class_name not in label_set:
            raise GenerationFailure(
                f"box class {class_name!r} is not in the job's label set "
                f'{label_set}')
        geometry = {}
        for field in ('left', 'top', 'width', 'height'):
            value = raw.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise GenerationFailure(
                    f"malformed box geometry: '{field}' is {value!r}")
            geometry[field] = float(value)
        if geometry['width'] <= 0 or geometry['height'] <= 0:
            raise GenerationFailure(
                'malformed box geometry: width and height must be positive')
        if geometry['left'] < 0 or geometry['top'] < 0:
            raise GenerationFailure(
                'malformed box geometry: negative coordinates')
        if (geometry['left'] + geometry['width'] > width
                or geometry['top'] + geometry['height'] > height):
            raise GenerationFailure(
                f"box {geometry} lies outside the {width}x{height} "
                f'image bounds')
        boxes.append({'class': class_name, **geometry})
    return {
        'modality': MODALITY_OBJECT_DETECTION,
        'boxes': boxes,
        'image_width': width,
        'image_height': height,
    }


def _generate_bedrock_prelabel(message: Dict, job: Dict,
                               model_id: str) -> Dict:
    modality = message['modality']
    label_set = message['label_set']
    if modality not in BEDROCK_MODALITIES:
        raise GenerationFailure(
            f'Bedrock auto-labeling does not support the {modality} '
            f'modality')

    image_bytes, image_key = _read_image_bytes(job, message['image_s3_uri'])
    dimensions = None
    if modality == MODALITY_OBJECT_DETECTION:
        dimensions = _image_dimensions(image_bytes)
        if not dimensions:
            raise GenerationFailure(
                'unsupported image content: could not determine image '
                'dimensions for bounding-box validation')

    # Per_Label_Prompts apply only in skip-verification mode (Req 9.4).
    per_label_prompts = None
    if job.get('skip_verification'):
        per_label_prompts = (message.get('per_label_prompts')
                             or job.get('per_label_prompts') or {})

    prompt = _build_prompt(modality, label_set, dimensions, per_label_prompts)

    # Client via bedrock_common: read timeout equals the invocation
    # timeout capped at 120 s, retries disabled (Req 8.5).
    config = get_bedrock_configuration()
    timeout = min(int(config['timeout_seconds']), BEDROCK_MAX_TIMEOUT_SECONDS)
    client = get_bedrock_client(config['region'], timeout)
    try:
        response = client.converse(
            modelId=model_id,
            messages=[{
                'role': 'user',
                'content': [
                    {'image': {'format': _image_format(image_key),
                               'source': {'bytes': image_bytes}}},
                    {'text': prompt},
                ],
            }],
            inferenceConfig=build_inference_config(config),
        )
    except Exception as exc:  # noqa: BLE001 — model error/timeout (Req 8.5)
        raise GenerationFailure(f'Bedrock invocation failed: {exc}') from exc

    payload = _extract_json(_response_text(response))
    if modality == MODALITY_CLASSIFICATION:
        return _validate_classification(payload, label_set)
    return _validate_boxes(payload, label_set, dimensions)


# ---------------------------------------------------------------------------
# LLM guidance path (llm-auto-labeling; Req 3.1, 3.3, 3.4, 4.2, 5.x)
# ---------------------------------------------------------------------------

def _generate_llm_prelabel(message: Dict, job: Dict, model_id: str) -> Dict:
    """Prompt-guided LLM Pre_Label: one Converse call asking for
    Coordinate_Guidance JSON, converted to the modality's Pre_Label.

    Request construction, invocation and response handling live in
    `dda_llm_prelabel.generate_llm_prelabel` — the same implementation
    the Prompt_Tuning_Preview calls, so a preview predicts what this
    worker does rather than imitating it (llm-autolabel-prompt-tuning
    Req 3.1, 3.2). What stays here is what is worker-specific: the
    cross-account image read, the pixel-dimension gate, the
    Detection_Prompt / Per_Label_Prompts resolution from the message
    with the job item as fallback, the Few_Shot_Example resolution from
    the job record, the Downscale_Setting and Token_Budget_Selection
    reads from the job record (llm-model-token-and-image-sizing Req 5.8,
    3.7), and the `GenerationFailure` translation that keeps
    `prelabel_error` byte-identical for every failure mode (Req 3.10,
    3.11).

    Only the image bytes and prompt content are sent to the model — no
    dataset credentials, no portal secrets (Req 9.7).
    """
    modality = message['modality']
    label_set = message['label_set']

    # Cross-account read with the direct-access fallback (Req 9.5, 9.6).
    image_bytes, image_key = _read_image_bytes(job, message['image_s3_uri'])

    # Req 3.3: undeterminable dimensions fail the image before any
    # model invocation is attempted.
    dimensions = _image_dimensions(image_bytes)
    if not dimensions:
        raise GenerationFailure(
            'unsupported image content: could not determine image '
            'dimensions for coordinate guidance')
    width, height = dimensions

    # Detection_Prompt from the message, falling back to the job item so
    # messages already in flight across a deployment still process.
    detection_prompt = (message.get('detection_prompt')
                        or (job.get('auto_label') or {}).get('detection_prompt')
                        or '')
    if not detection_prompt.strip():
        raise GenerationFailure('job has no detection prompt configured')

    # Per_Label_Prompts apply only in skip-verification mode (Req 2.6).
    per_label_prompts = None
    if job.get('skip_verification'):
        per_label_prompts = (message.get('per_label_prompts')
                             or job.get('per_label_prompts') or {})

    # Few_Shot_Examples from the job record, read after the target image
    # so an unreadable example can never mask an unreadable target
    # (llm-autolabel-prompt-tuning Req 6.5, 6.7, 10.3). With the option
    # off this is ([], resolved limit) and the request is exactly the
    # pre-feature one (Req 10.2).
    few_shot_images, model_image_limit = _resolve_few_shot_images(
        job, model_id)

    # Sizing inputs from the job record, beside the few_shot document
    # they were persisted with (llm-model-token-and-image-sizing Req 5.8,
    # 3.7). DynamoDB returns numbers as Decimal, so both values pass
    # through _decimal_to_native before the total-and-safe resolvers see
    # them. A malformed, null or absent downscale_max_edge is
    # Downscale_Off, and a malformed or absent token_budget falls through
    # to the Model_Token_Limits mapping and then the default — neither is
    # ever a failure (Req 3.8, 5.9, 5.12, 10.10). A persisted valid
    # budget wins the resolution unchanged for the life of the job,
    # whatever the mapping says today (Req 3.7). Only the `llm:` family
    # reaches this function, so neither value is read for `sam` or
    # `bedrock:` jobs (Req 10.4).
    auto_label = job.get('auto_label') or {}
    downscale_setting = normalize_downscale_setting(
        _decimal_to_native(auto_label.get('downscale_max_edge')))
    token_budget_selection = _decimal_to_native(
        auto_label.get('token_budget'))

    # This module's `get_bedrock_client` stays the single Bedrock client
    # seam for every model family the worker drives, so the shared
    # invocation module builds its client through the same binding.
    dda_llm_prelabel.get_bedrock_client = get_bedrock_client
    try:
        result = generate_llm_prelabel(
            model_identifier=model_id,
            modality=modality,
            label_set=label_set,
            detection_prompt=detection_prompt,
            per_label_prompts=per_label_prompts,
            image_bytes=image_bytes,
            image_key=image_key,
            width=width,
            height=height,
            few_shot_images=few_shot_images,
            model_image_limit=model_image_limit,
            downscale_setting=downscale_setting,
            token_budget_selection=token_budget_selection,
            model_token_limits=_llm_model_token_limits(),
        )
    except LlmPrelabelError as exc:
        # Every category carries the reason string this worker has
        # always recorded — timeout, model error, and unusable model
        # output alike (Req 3.10, 3.11) — so prelabel_error /
        # autolabel_error and the autolabel_pending accounting are
        # unchanged.
        raise GenerationFailure(exc.reason) from exc
    # The shared chokepoint now returns the Pre_Label plus the
    # Sent_Dimensions (llm-model-token-and-image-sizing Req 5.10); the
    # worker stores the Pre_Label alone — the Sent_Dimensions are the
    # Preview_API's reporting concern (Req 7.10, 10.1).
    return result.prelabel


# ---------------------------------------------------------------------------
# SAM path (Req 8.2: class-agnostic geometry pre-labels)
# ---------------------------------------------------------------------------

def _get_sam_lambda_client():
    """Lambda client bounding the synchronous SAM invocation wall clock
    at 120 s (read timeout, retries disabled)."""
    global _cached_sam_lambda_client
    if sam_lambda_client is not None:
        return sam_lambda_client
    if _cached_sam_lambda_client is None:
        _cached_sam_lambda_client = boto3.client(
            'lambda',
            config=BotoConfig(
                connect_timeout=10,
                read_timeout=SAM_MAX_TIMEOUT_SECONDS,
                retries={'max_attempts': 0},
            ),
        )
    return _cached_sam_lambda_client


def _generate_sam_prelabel(message: Dict, job: Dict) -> Dict:
    modality = message['modality']
    if modality not in SAM_MODALITIES:
        raise GenerationFailure(
            f'SAM auto-labeling does not support the {modality} modality')
    if not SAM_WORKER_FUNCTION_NAME:
        raise GenerationFailure('SAM worker function is not configured')

    bucket, key = _parse_s3_uri(message['image_s3_uri'])
    dataset_s3 = _dataset_s3_client(job, bucket)
    presigned_url = dataset_s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': key},
        ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
    )

    try:
        response = _get_sam_lambda_client().invoke(
            FunctionName=SAM_WORKER_FUNCTION_NAME,
            InvocationType='RequestResponse',
            Payload=json.dumps({'image_s3_presigned_url': presigned_url}),
        )
        payload_bytes = response['Payload'].read()
    except Exception as exc:  # noqa: BLE001 — invocation error/timeout
        raise GenerationFailure(f'SAM worker invocation failed: {exc}') from exc

    if response.get('FunctionError'):
        raise GenerationFailure(
            f'SAM worker failed: '
            f"{payload_bytes.decode('utf-8', errors='replace')[:512]}")
    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, ValueError) as exc:
        raise GenerationFailure(
            f'SAM worker returned unparseable output: {exc}') from exc

    regions = payload.get('regions') if isinstance(payload, dict) else None
    image_width = payload.get('image_width') if isinstance(payload, dict) else None
    image_height = payload.get('image_height') if isinstance(payload, dict) else None
    if (not isinstance(regions, list)
            or not isinstance(image_width, int)
            or not isinstance(image_height, int)):
        raise GenerationFailure('SAM worker returned a malformed response')

    # SAM is class-agnostic: regions are stored with class null and the
    # Labeler_Interface assigns Label_Set classes downstream (Req 8.2).
    prelabel_regions = []
    for region in regions:
        if not isinstance(region, dict) or not region.get('rle'):
            raise GenerationFailure(
                'SAM worker returned a region without RLE geometry')
        prelabel_region = {'class': None, 'rle': region['rle']}
        if region.get('score') is not None:
            prelabel_region['score'] = region['score']
        prelabel_regions.append(prelabel_region)

    return {
        'modality': modality,
        'regions': prelabel_regions,
        'image_width': image_width,
        'image_height': image_height,
    }


# ---------------------------------------------------------------------------
# Grounded-SAM path (grounded-sam-autolabel Req 4.x: classified
# text-prompted pre-labels)
# ---------------------------------------------------------------------------

def _get_grounded_sam_lambda_client():
    """Lambda client bounding the synchronous Grounded-SAM invocation
    wall clock at 240 s (read timeout, retries disabled — Req 4.3)."""
    global _cached_grounded_sam_lambda_client
    if grounded_sam_lambda_client is not None:
        return grounded_sam_lambda_client
    if _cached_grounded_sam_lambda_client is None:
        _cached_grounded_sam_lambda_client = boto3.client(
            'lambda',
            config=BotoConfig(
                connect_timeout=10,
                read_timeout=GROUNDED_SAM_MAX_TIMEOUT_SECONDS,
                retries={'max_attempts': 0},
            ),
        )
    return _cached_grounded_sam_lambda_client


def _grounded_sam_prompts(label_set: List[str],
                          overrides: Any) -> List[Dict[str, str]]:
    """The Prompt_Map (Req 2.7): one `{label, prompt}` pair per
    Label_Set label in Label_Set order. A label's prompt is its
    Prompt_Override when the override is a string that is non-empty
    after trimming, otherwise the label name itself.

    Pure and total over malformed `overrides` (None, non-dict, or
    non-string values all degrade to the label-name fallback) — a
    malformed job record never fails the image here (Req 7.6).
    """
    if not isinstance(overrides, dict):
        overrides = {}
    prompts = []
    for label in label_set:
        override = overrides.get(label)
        if isinstance(override, str) and override.strip():
            prompt = override
        else:
            prompt = label
        prompts.append({'label': label, 'prompt': prompt})
    return prompts


def _generate_grounded_sam_prelabel(message: Dict, job: Dict) -> Dict:
    modality = message['modality']
    label_set = message['label_set']
    if modality not in GROUNDED_SAM_MODALITIES:
        raise GenerationFailure(
            f'Grounded-SAM auto-labeling does not support the {modality} '
            f'modality')
    if not GROUNDED_SAM_WORKER_FUNCTION_NAME:
        raise GenerationFailure(
            'Grounded-SAM worker function is not configured')

    bucket, key = _parse_s3_uri(message['image_s3_uri'])
    dataset_s3 = _dataset_s3_client(job, bucket)
    presigned_url = dataset_s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': key},
        ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
    )

    # Prompt_Overrides ride the job record, not the fan-out message
    # (Req 4.1): the record is fetched per message anyway, so in-flight
    # messages across a deployment process correctly by construction.
    prompt_overrides = (job.get('auto_label') or {}).get('prompt_overrides')
    payload_out = {
        'image_s3_presigned_url': presigned_url,
        'prompts': _grounded_sam_prompts(label_set, prompt_overrides),
        'modality': modality,
    }

    try:
        response = _get_grounded_sam_lambda_client().invoke(
            FunctionName=GROUNDED_SAM_WORKER_FUNCTION_NAME,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload_out),
        )
        payload_bytes = response['Payload'].read()
    except Exception as exc:  # noqa: BLE001 — invocation error/timeout
        raise GenerationFailure(
            f'Grounded-SAM worker invocation failed: {exc}') from exc

    if response.get('FunctionError'):
        raise GenerationFailure(
            f'Grounded-SAM worker failed: '
            f"{payload_bytes.decode('utf-8', errors='replace')[:512]}")
    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, ValueError) as exc:
        raise GenerationFailure(
            f'Grounded-SAM worker returned unparseable output: {exc}') from exc

    regions = payload.get('regions') if isinstance(payload, dict) else None
    image_width = payload.get('image_width') if isinstance(payload, dict) else None
    image_height = payload.get('image_height') if isinstance(payload, dict) else None
    if (not isinstance(regions, list)
            or not isinstance(image_width, int)
            or not isinstance(image_height, int)):
        raise GenerationFailure(
            'Grounded-SAM worker returned a malformed response')

    # Unlike sam, every region is classified: its class must be a
    # member of the message's Label_Set (Req 4.5).
    if modality == MODALITY_SEGMENTATION:
        prelabel_regions = []
        for region in regions:
            if not isinstance(region, dict):
                raise GenerationFailure(
                    'Grounded-SAM worker returned a malformed region entry')
            class_name = region.get('class')
            if not isinstance(class_name, str) or class_name not in label_set:
                raise GenerationFailure(
                    f'Grounded-SAM worker returned class {class_name!r}, '
                    f"which is not in the job's label set {label_set}")
            if not region.get('rle'):
                raise GenerationFailure(
                    'Grounded-SAM worker returned a region without RLE '
                    'geometry')
            prelabel_region = {'class': class_name, 'rle': region['rle']}
            if region.get('score') is not None:
                prelabel_region['score'] = region['score']
            prelabel_regions.append(prelabel_region)
        return {
            'modality': modality,
            'regions': prelabel_regions,
            'image_width': image_width,
            'image_height': image_height,
        }

    # ObjectDetection: the worker nests geometry under `box`; the
    # stored Pre_Label is the Bedrock shape byte-exactly (Req 4.7) —
    # `{class, left, top, width, height}` floats, score dropped —
    # validated with the _validate_boxes rules (Req 4.5).
    boxes = []
    for region in regions:
        if not isinstance(region, dict):
            raise GenerationFailure(
                'Grounded-SAM worker returned a malformed region entry')
        class_name = region.get('class')
        if not isinstance(class_name, str) or class_name not in label_set:
            raise GenerationFailure(
                f'Grounded-SAM worker returned class {class_name!r}, '
                f"which is not in the job's label set {label_set}")
        box = region.get('box')
        if not isinstance(box, dict):
            raise GenerationFailure(
                'Grounded-SAM worker returned a region without box geometry')
        geometry = {}
        for field in ('left', 'top', 'width', 'height'):
            value = box.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise GenerationFailure(
                    f"malformed box geometry: '{field}' is {value!r}")
            geometry[field] = float(value)
        if geometry['width'] <= 0 or geometry['height'] <= 0:
            raise GenerationFailure(
                'malformed box geometry: width and height must be positive')
        if geometry['left'] < 0 or geometry['top'] < 0:
            raise GenerationFailure(
                'malformed box geometry: negative coordinates')
        if (geometry['left'] + geometry['width'] > image_width
                or geometry['top'] + geometry['height'] > image_height):
            raise GenerationFailure(
                f'box {geometry} lies outside the '
                f'{image_width}x{image_height} image bounds')
        boxes.append({'class': class_name, **geometry})
    return {
        'modality': modality,
        'boxes': boxes,
        'image_width': image_width,
        'image_height': image_height,
    }


# ---------------------------------------------------------------------------
# Persistence: pre-label object + task/job state
# ---------------------------------------------------------------------------

def _prelabel_s3_key(usecase_id: str, job_id: str, task_id: str) -> str:
    return f'labeling/{usecase_id}/{job_id}/prelabels/{task_id}.json'


def _write_prelabel(usecase_id: str, job_id: str, task_id: str,
                    prelabel: Dict) -> str:
    if not PORTAL_ARTIFACTS_BUCKET:
        raise RuntimeError('PORTAL_ARTIFACTS_BUCKET is not configured')
    key = _prelabel_s3_key(usecase_id, job_id, task_id)
    s3_client.put_object(
        Bucket=PORTAL_ARTIFACTS_BUCKET,
        Key=key,
        Body=json.dumps(prelabel).encode('utf-8'),
        ContentType='application/json',
    )
    return key


def _mark_task(job_id: str, task_id: str, status: str,
               prelabel_s3_key: Optional[str] = None,
               error: Optional[str] = None,
               skip_verification: bool = False) -> bool:
    """Conditionally resolve the task's prelabel_status.

    The condition (task exists and its pre-label is still unresolved)
    makes duplicate SQS deliveries idempotent: a redelivered message
    can never overwrite a resolution or double-decrement the
    skip-verification counter. Returns True when this call performed
    the resolution.
    """
    update_parts = ['prelabel_status = :status', 'updated_at = :now']
    values: Dict[str, Any] = {
        ':status': status,
        ':now': _now(),
        ':available': 'Available',
        ':failed': 'Failed',
    }
    if prelabel_s3_key:
        update_parts.append('prelabel_s3_key = :prelabel_key')
        values[':prelabel_key'] = prelabel_s3_key
    if error is not None:
        message = str(error)[:1024]
        update_parts.append('prelabel_error = :error')
        values[':error'] = message
        if skip_verification:
            # Skip-verification failures are review-ineligible (Req 9.10).
            update_parts.append('autolabel_error = :error')
    try:
        tasks_table.update_item(
            Key={'job_id': job_id, 'task_id': task_id},
            UpdateExpression='SET ' + ', '.join(update_parts),
            ConditionExpression=(
                'attribute_exists(job_id) AND '
                '(attribute_not_exists(prelabel_status) OR '
                'NOT (prelabel_status IN (:available, :failed)))'),
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as exc:
        if (exc.response.get('Error', {}).get('Code')
                == 'ConditionalCheckFailedException'):
            logger.info(
                'Pre-label for task %s/%s already resolved (or task '
                'missing); skipping duplicate resolution', job_id, task_id)
            return False
        raise


def _resolve_skip_verification_counters(job_id: str) -> None:
    """Atomically decrement autolabel_pending / count the completion;
    at zero pending the job becomes review-ready (Req 9.5)."""
    response = jobs_table.update_item(
        Key={'job_id': job_id},
        UpdateExpression=('ADD autolabel_pending :minus_one, '
                          'autolabel_completed_count :one'),
        ConditionExpression='attribute_exists(job_id)',
        ExpressionAttributeValues={':minus_one': -1, ':one': 1},
        ReturnValues='UPDATED_NEW',
    )
    pending = int(response.get('Attributes', {}).get('autolabel_pending', 0))
    if pending <= 0:
        jobs_table.update_item(
            Key={'job_id': job_id},
            UpdateExpression='SET review_ready = :ready, updated_at = :now',
            ConditionExpression='attribute_exists(job_id)',
            ExpressionAttributeValues={':ready': True, ':now': _now()},
        )


# ---------------------------------------------------------------------------
# Record processing
# ---------------------------------------------------------------------------

def _generate_prelabel(message: Dict, job: Dict) -> Dict:
    model = message['model']
    if model == 'sam':
        return _generate_sam_prelabel(message, job)
    if model == 'grounded-sam':
        return _generate_grounded_sam_prelabel(message, job)
    if isinstance(model, str) and model.startswith('bedrock:'):
        model_id = model.split(':', 1)[1]
        if model_id:
            return _generate_bedrock_prelabel(message, job, model_id)
    if isinstance(model, str) and model.startswith('llm:'):
        model_id = model.split(':', 1)[1]
        if model_id:
            return _generate_llm_prelabel(message, job, model_id)
    raise GenerationFailure(f'unsupported auto-label model {model!r}')


def _process_message(message: Dict) -> None:
    job_id = message['job_id']
    task_id = message['task_id']

    job = jobs_table.get_item(Key={'job_id': job_id}).get('Item')
    if not job:
        raise MalformedMessage(f'labeling job {job_id!r} does not exist')
    skip_verification = bool(job.get('skip_verification'))

    # Storage-failure semantics are scoped per model family (Req 6.2,
    # 6.5, 1.7): for the LLM family a _write_prelabel failure is a
    # terminal resolution (the task is marked Failed with a storage
    # reason), while sam / bedrock: keep today's transient behavior
    # (the exception escapes, the record is reported as a batch item
    # failure, and the task stays Pending for SQS redrive).
    storage_failure_is_terminal = (
        str(message.get('model', '')).startswith(LLM_MODEL_PREFIX))

    try:
        prelabel = _generate_prelabel(message, job)
        try:
            prelabel_key = _write_prelabel(
                job['usecase_id'], job_id, task_id, prelabel)
        except Exception as exc:  # noqa: BLE001 — storage failure
            if not storage_failure_is_terminal:
                raise  # today's transient/retry behavior (sam, bedrock:)
            # Deliberate tradeoff: an LLM image hit by a transient S3
            # error is marked Failed rather than retried. That is
            # acceptable because a Failed task is still labelable from
            # scratch, while a task left permanently Pending is
            # withheld from labelers forever.
            raise GenerationFailure(
                f'pre-label storage failed: {exc}') from exc
        resolved = _mark_task(job_id, task_id, 'Available',
                              prelabel_s3_key=prelabel_key)
    except GenerationFailure as exc:
        logger.warning('Pre-label generation failed for task %s/%s: %s',
                       job_id, task_id, exc)
        resolved = _mark_task(job_id, task_id, 'Failed', error=str(exc),
                              skip_verification=skip_verification)

    # The skip-verification counter moves exactly once per task: only
    # the call that performed the conditional resolution decrements it.
    if resolved and skip_verification:
        _resolve_skip_verification_counters(job_id)


def handler(event, context):
    """SQS consumer entry point.

    Returns a partial batch response so a transient infrastructure
    failure retries only the affected record; generation failures are
    absorbed per record (marked Failed on the task) and malformed
    messages are logged and dropped — neither poisons the batch.
    """
    # One Model_Token_Limits read per invocation: the memo is dropped
    # here so this batch resolves against one self-consistent mapping
    # and a warm container never serves a stale one
    # (llm-model-token-and-image-sizing Req 1.6, 1.8).
    _reset_model_token_limits_cache()
    batch_item_failures = []
    for record in (event or {}).get('Records', []):
        message_id = record.get('messageId')
        try:
            message = _parse_record(record)
            _process_message(message)
        except MalformedMessage as exc:
            logger.error('Dropping unprocessable auto-label message %s: %s',
                         message_id, exc)
        except Exception:  # noqa: BLE001 — transient: retry this record only
            logger.exception(
                'Transient failure processing auto-label message %s; '
                'reporting batch item failure', message_id)
            if message_id:
                batch_item_failures.append({'itemIdentifier': message_id})
    return {'batchItemFailures': batch_item_failures}
