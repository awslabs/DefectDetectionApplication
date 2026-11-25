"""
Lambda function for inference-results capture operations.

Surfaces on-device inference-results captures (source image, overlay, mask, and
the results `.jsonl` Capture_Metadata) to the Portal frontend, mirroring the
presigned-URL pattern used by `datasets.get_image_preview`.

Captures are pushed to the inference-results S3 bucket
(`dda-inference-results-{account_id}`, or the use case's configured
`inference_uploader_s3_bucket`) by the opt-in `aws.edgeml.dda.InferenceUploader`
Greengrass component. Each capture folder contains:
  - {capture_id}.jsonl       -> Capture_Metadata (deviceFleetAuxiliaryOutputs, ...)
  - {capture_id}.jpg         -> source image
  - {capture_id}.overlay.jpg -> bounding-box / mask overlay (optional)
  - {capture_id}.mask.png    -> anomaly mask (optional)
"""

import json
import base64
import boto3
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Any, Optional, Tuple

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    get_usecase,
    assume_usecase_role,
    create_response,
    handle_error,
    get_user_from_event,
    check_user_access,
    log_audit_event,
)

s3_client = boto3.client('s3')


def _insufficient_permissions(usecase_id, user, resource_id=None, required_role=None):
    """The 403 a data route answers when `check_user_access` fails, after
    one `unauthorized_access` audit row.

    Mirrors pre_labeled_datasets: nothing has been read from the use case's
    account at this point (no cross-account role assumption, no S3 listing,
    no presigned URL), so a refused caller reaches none of it.
    """
    details = {'usecase_id': usecase_id or ''}
    if required_role:
        details['required_role'] = required_role
    log_audit_event(
        user_id=user['user_id'], action='unauthorized_access',
        resource_type='capture', resource_id=resource_id or usecase_id or 'unknown',
        result='denied', details=details)
    return create_response(403, {'error': 'Insufficient permissions'})

# observedContentType markers written by the Marshal
# (see marshal_for_capture_template.py / utils/constants.py).
INFERENCE_RESULT_CONTENT_TYPE = "json"                       # the inf_result summary
DETECTIONS_BLOCK_CONTENT_TYPE = "json_with_base64_encoding"  # detections / anomalies block
OVERLAY_CONTENT_TYPE = "overlay.jpg"
MASK_CONTENT_TYPE = "mask.png"

# Presigned URLs are valid for 30 minutes (mirrors datasets.get_image_preview).
PRESIGNED_URL_EXPIRY_SECONDS = 1800

# Per-record work of a page runs on a bounded pool sharing the one boto3
# client of the request (low-level clients are thread-safe; rfdetr-seg-support
# design §11 "Page cost", mitigation 2).
PAGE_PARSE_WORKERS = 8

# The S3 object-key maximum, used only as the input bound of a detail `key`.
DETAIL_KEY_MAX_BYTES = 1024
DETAIL_KEY_ERROR = 'key must be a .jsonl object under the requested prefix'

# Sentinel: `_extract_inference_data` decodes the detections block itself
# unless the caller already did (`_parse_capture` decodes each record once).
_UNSET = object()


def handler(event, context):
    """Main Lambda handler for capture operations."""
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')

        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }

        if 'captures' in path:
            if http_method == 'GET':
                return list_captures(event)

        return create_response(404, {'error': 'Not found'})

    except Exception as e:
        return handle_error(e, 'Capture operation failed')


def get_inference_results_bucket_and_credentials(usecase):
    """
    Resolve the inference-results bucket and credentials for a use case.

    The inference-results bucket lives in the UseCase Account (the same account
    the InferenceUploader component runs against), so we assume the use case's
    cross-account role. For single-account setups `assume_usecase_role` returns
    the Lambda's own credentials.

    The bucket name mirrors the deployment wiring in `deployments.py`:
    the use case's `inference_uploader_s3_bucket` override if present, otherwise
    the default `dda-inference-results-{account_id}`.
    """
    account_id = usecase.get('account_id', '')
    bucket = usecase.get('inference_uploader_s3_bucket') or f"dda-inference-results-{account_id}"

    credentials = assume_usecase_role(
        usecase['cross_account_role_arn'],
        usecase.get('external_id'),
        'inference-results-access'
    )

    return bucket, credentials


def results_bucket_owner_args(usecase, bucket: str) -> Dict[str, str]:
    """``{'ExpectedBucketOwner': <use case account>}`` when `bucket` is the
    use case's DERIVED default (``dda-inference-results-{account_id}``), else
    ``{}``. The default's name is predictable from the account id and nothing
    creates the bucket for the operator, so a bucket of that name may have
    been pre-created by another account (S3 bucket squatting); asserting the
    owner on every list and read makes S3 answer 403 instead of serving that
    account's objects as the use case's captures. An admin-configured
    override is not a derived name and is left to the bucket allowlist."""
    account_id = str(usecase.get('account_id') or '').strip()
    if account_id and bucket == f"dda-inference-results-{account_id}":
        return {'ExpectedBucketOwner': account_id}
    return {}


def list_captures(event):
    """
    List inference-results captures under a prefix and return parsed metadata
    plus presigned URLs for the associated artifacts.

    Query Parameters:
        - usecase_id:    Required. The use case ID.
        - prefix:        Required. The S3 prefix (capture folder) to list.
        - device_id:     Optional. Device/thing identifier; appended to the
                         prefix when the prefix does not already scope to a
                         device.
        - limit:         Optional. Max captures to return (default: 20, max: 100).
        - key:           Optional. The `.jsonl` object key of ONE capture under
                         the scoped prefix: the per-capture DETAIL request
                         (rfdetr-seg-support Requirement 11.1, design §11).
        - include_masks: Optional ('1' / 'true'). On a detail request, pass
                         each instance's `mask_rle` through. Ignored on a page.

    Two request shapes on the one route (no new API Gateway resource):
      - PAGE (no `key`): today's listing. Always the LIGHT form -- boxes,
        `mask_area`, `mask_truncated`, `has_masks`, `detections_truncated`,
        `jsonl_key`, presigned URLs, never `mask_rle` -- so a page stays
        under the Lambda response cap and the Portal MCP tool
        `portal_data_captures_list` (same handler, no per-field drop) keeps
        its captures.
      - DETAIL (`key` present): one `get_object` and no LIST; `mask_rle`
        per entry when `include_masks` is set; the artifact URLs are None
        (the viewer keeps the page's); `total_found` is 1; `limit` ignored.

    Returns:
        { captures: [ { capture_id, inference_result_type, detection_count,
                        detections: [...], detections_truncated, has_masks,
                        jsonl_key, source_url, overlay_url, mask_url } ],
          bucket, prefix, total_found, expires_in_seconds }
    """
    try:
        params = event.get('queryStringParameters', {}) or {}
        usecase_id = params.get('usecase_id')
        prefix = params.get('prefix')
        device_id = params.get('device_id')
        limit = min(int(params.get('limit', '20')), 100)
        detail_key = params.get('key')
        include_masks = str(params.get('include_masks') or '').strip().lower() in ('1', 'true')

        if not usecase_id or not prefix:
            return create_response(400, {
                'error': 'usecase_id and prefix are required'
            })

        # Membership of the use case named in the query, before any
        # cross-account credential assumption or presigned-URL generation
        # (data-route authorization consistency; mirrors pre_labeled_datasets
        # and the use case's other read routes).
        user = get_user_from_event(event)
        if not check_user_access(user['user_id'], usecase_id):
            return _insufficient_permissions(usecase_id, user)

        # Scope the search prefix to the device when provided and not already present
        search_prefix = prefix
        if device_id and device_id not in search_prefix:
            search_prefix = f"{search_prefix.rstrip('/')}/{device_id}"

        # Detail request: the key must name a `.jsonl` object the page for the
        # same prefix could list -- checked after the membership gate and
        # before any credential resolution (the prefix depends only on the
        # query, so a bad key costs no STS call).
        if detail_key is not None and not _validate_detail_key(detail_key, search_prefix):
            return create_response(400, {'error': DETAIL_KEY_ERROR})

        # Get use case details
        usecase = get_usecase(usecase_id)

        # Resolve the inference-results bucket and credentials
        bucket, credentials = get_inference_results_bucket_and_credentials(usecase)

        # Create S3 client with the use case account's credentials
        s3 = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )

        # Derived bucket name: the list and every read assert the owner.
        # (The presigned GET URLs below carry no header the browser could
        # send; they are only issued for a bucket this list has verified.)
        owner_args = results_bucket_owner_args(usecase, bucket)

        if detail_key is not None:
            # One read, no LIST. Only the metadata key is "listed", so the
            # sibling-artifact URLs come back None (`_presign_if_exists` signs
            # listed keys only); a missing or unreadable object degrades to an
            # empty-detections entry inside `_parse_capture`, never a 5xx.
            capture = _parse_capture(s3, bucket, detail_key, {detail_key}, owner_args,
                                     include_masks=include_masks)
            return create_response(200, {
                'captures': [capture] if capture else [],
                'bucket': bucket,
                'prefix': search_prefix,
                'total_found': 1,
                'expires_in_seconds': PRESIGNED_URL_EXPIRY_SECONDS
            })

        # List all keys under the prefix so we can (a) find capture metadata files
        # and (b) know which sibling artifacts actually exist (missing artifacts
        # -> null URLs) without extra head_object calls.
        all_keys = set()
        jsonl_keys = []

        paginator = s3.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket, Prefix=search_prefix, **owner_args)

        for page in pages:
            for obj in page.get('Contents', []):
                key = obj['Key']
                all_keys.add(key)
                if key.endswith('.jsonl'):
                    jsonl_keys.append(key)

        # Page: parse the records on a bounded pool in order-preserving slices
        # of `limit` until `limit` entries are collected or the keys run out.
        # The page never passes `include_masks` (light form by construction);
        # the per-key try/except lives in `_parse_capture`, so one unreadable
        # record degrades to an empty-detections entry and never fails the page.
        captures = []
        slice_size = max(1, limit)
        with ThreadPoolExecutor(max_workers=PAGE_PARSE_WORKERS) as executor:
            for start in range(0, len(jsonl_keys), slice_size):
                if len(captures) >= limit:
                    break
                keys = jsonl_keys[start:start + slice_size]
                for capture in executor.map(
                        lambda k: _parse_capture(s3, bucket, k, all_keys, owner_args), keys):
                    if capture and len(captures) < limit:
                        captures.append(capture)

        return create_response(200, {
            'captures': captures,
            'bucket': bucket,
            'prefix': search_prefix,
            'total_found': len(jsonl_keys),
            'expires_in_seconds': PRESIGNED_URL_EXPIRY_SECONDS
        })

    except Exception as e:
        return handle_error(e, 'Failed to list captures')


def _validate_detail_key(key, search_prefix: str) -> bool:
    """True when `key` is a string of 1..DETAIL_KEY_MAX_BYTES bytes naming a
    `.jsonl` object under `search_prefix` -- exactly the objects a page for
    the same prefix could list, and no other."""
    if not isinstance(key, str):
        return False
    if not 1 <= len(key.encode('utf-8')) <= DETAIL_KEY_MAX_BYTES:
        return False
    return key.startswith(search_prefix) and key.endswith('.jsonl')


def _parse_capture(s3, bucket: str, jsonl_key: str, all_keys: set,
                   owner_args: Optional[Dict[str, str]] = None,
                   include_masks: bool = False) -> Optional[Dict[str, Any]]:
    """
    Parse a single capture's `.jsonl` metadata and build its response entry.

    Tolerates a missing/parse-failed metadata file (returns an entry with empty
    detections) and missing sibling artifacts (null URLs). Returns None only if
    the metadata object cannot be fetched at all. `owner_args` is the
    ExpectedBucketOwner assertion of `results_bucket_owner_args` for the read.
    `include_masks` passes each instance's `mask_rle` through (the detail
    request only); the detections block is base64/JSON-decoded once and feeds
    both the entries and the per-capture mask flags.
    """
    # capture_id and the sibling-artifact key base derive from the jsonl key,
    # e.g. "device/folder/<capture_id>.jsonl" -> base "device/folder/<capture_id>"
    key_base = jsonl_key[:-len('.jsonl')]
    capture_id = os.path.basename(key_base)

    inference_result_type = None
    detection_count = 0
    detections: List[Dict[str, Any]] = []
    detections_truncated = False
    has_masks = False

    try:
        body = s3.get_object(Bucket=bucket, Key=jsonl_key, **(owner_args or {}))['Body'].read()
        # A capture .jsonl holds one metadata object per line; use the last
        # non-empty line as the capture's metadata.
        metadata = _load_last_metadata(body)
        if metadata is not None:
            block = _decode_detections_block(metadata)
            inference_result_type, detection_count, detections = _extract_inference_data(
                metadata, include_masks=include_masks, block=block)
            detections_truncated, has_masks = _capture_mask_flags(block)
    except Exception as e:
        # Missing / unreadable / unparseable metadata degrades to empty
        # detections rather than failing the whole listing.
        print(f"Failed to parse capture metadata for {jsonl_key}: {str(e)}")

    source_url = _presign_if_exists(s3, bucket, f"{key_base}.jpg", all_keys)
    overlay_url = _presign_if_exists(s3, bucket, f"{key_base}.overlay.jpg", all_keys)
    mask_url = _presign_if_exists(s3, bucket, f"{key_base}.mask.png", all_keys)

    return {
        'capture_id': capture_id,
        'inference_result_type': inference_result_type,
        'detection_count': detection_count,
        'detections': detections,
        'detections_truncated': detections_truncated,
        'has_masks': has_masks,
        'jsonl_key': jsonl_key,
        'source_url': source_url,
        'overlay_url': overlay_url,
        'mask_url': mask_url,
    }


def _load_last_metadata(body: bytes) -> Optional[Dict[str, Any]]:
    """Parse the last non-empty JSON line from a capture `.jsonl` body."""
    text = body.decode('utf-8', errors='replace')
    metadata = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        metadata = json.loads(line)
    return metadata


def _get_aux_output(output_list: List[Dict[str, Any]], content_type: str, field: str) -> Optional[Any]:
    """Return the requested field of the first aux output matching content_type."""
    for entry in output_list:
        if entry.get('observedContentType') == content_type:
            return entry.get(field)
    return None


def _decode_detections_block(metadata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    The decoded `json_with_base64_encoding` aux output of a parsed
    Capture_Metadata object (the Marshal's `{"detections": {...}}` or
    `{"anomalies": {...}}` block), or None when absent or corrupt (logged).
    """
    output_list = metadata.get('deviceFleetAuxiliaryOutputs') or []
    block_b64 = _get_aux_output(output_list, DETECTIONS_BLOCK_CONTENT_TYPE, 'data')
    if not block_b64:
        return None
    try:
        block = json.loads(base64.b64decode(block_b64))
    except Exception as e:
        print(f"Failed to decode detections block: {str(e)}")
        return None
    return block if isinstance(block, dict) else None


def _extract_inference_data(metadata: Dict[str, Any], include_masks: bool = False,
                            block: Any = _UNSET):
    """
    Extract (inference_result_type, detection_count, detections) from a parsed
    Capture_Metadata object.

    - inference_result_type comes from the base64 `json` summary
      ("Inference result": Detection / Anomaly / Normal).
    - detection_count comes from the same summary ("Detection_count").
    - detections come from the base64 `json_with_base64_encoding` block whose
      decoded payload carries a top-level "detections" map; each entry is
      normalized to {class_index, class_label, bounding_box, confidence} plus,
      only when the raw entry carries them, the instance keys `mask_area`,
      `mask_truncated` and -- with `include_masks` -- `mask_rle`.
      An anomaly capture (payload with an "anomalies" map) yields no detections.
    - `block` is the already-decoded detections block (`_decode_detections_block`);
      when not supplied the block is decoded here.
    """
    inference_result_type = None
    detection_count = 0
    detections: List[Dict[str, Any]] = []

    output_list = metadata.get('deviceFleetAuxiliaryOutputs') or []

    # Inference-result summary (base64-encoded JSON, observedContentType "json")
    summary_b64 = _get_aux_output(output_list, INFERENCE_RESULT_CONTENT_TYPE, 'data')
    if summary_b64:
        try:
            summary = json.loads(base64.b64decode(summary_b64))
            inference_result_type = summary.get('Inference result')
            if summary.get('Detection_count') is not None:
                detection_count = int(summary.get('Detection_count'))
        except Exception as e:
            print(f"Failed to decode inference result summary: {str(e)}")

    # Detections block (base64-encoded JSON, observedContentType
    # "json_with_base64_encoding"). Only treat it as detections when the
    # decoded payload carries a "detections" map (anomaly captures carry an
    # "anomalies" map instead).
    if block is _UNSET:
        block = _decode_detections_block(metadata)
    if isinstance(block, dict):
        det_map = block.get('detections')
        if isinstance(det_map, dict):
            detections = _normalize_detections(det_map, include_masks=include_masks)

    return inference_result_type, detection_count, detections


def _capture_mask_flags(block: Optional[Dict[str, Any]]) -> Tuple[bool, bool]:
    """
    `(detections_truncated, has_masks)` of a decoded detections block.

    `has_masks` is true when any entry carried a mask on the device -- a
    `mask_rle`, or `mask_truncated` where the device's payload ceiling dropped
    it -- whether or not the RLE is returned in this response. `(False, False)`
    for None (absent / corrupt block) and for anomaly blocks.
    """
    if not isinstance(block, dict):
        return False, False
    det_map = block.get('detections')
    if not isinstance(det_map, dict):
        return False, False
    detections_truncated = bool(block.get('detections_truncated'))
    has_masks = any(
        isinstance(det, dict) and ('mask_rle' in det or bool(det.get('mask_truncated')))
        for det in det_map.values()
    )
    return detections_truncated, has_masks


def _normalize_detections(det_map: Dict[str, Any],
                          include_masks: bool = False) -> List[Dict[str, Any]]:
    """
    Convert the Marshal's detections map ({"0": {...}, "1": {...}}) into an
    ordered list of {class_index, class_label, bounding_box, confidence}.
    An instance entry's `mask_area` and `mask_truncated` pass through only
    when the raw entry carries them (no key otherwise), and its `mask_rle`
    only when it does AND `include_masks` is set (the per-capture detail).
    """
    detections = []
    # Preserve the numeric ordering of the string keys ("0","1","2",...).
    for key in sorted(det_map.keys(), key=_safe_int_key):
        det = det_map[key]
        if not isinstance(det, dict):
            continue
        entry = {
            'class_index': det.get('class_index'),
            'class_label': det.get('class_label'),
            'bounding_box': det.get('bounding_box'),
            'confidence': det.get('confidence'),
        }
        for field in ('mask_area', 'mask_truncated'):
            if field in det:
                entry[field] = det[field]
        if include_masks and 'mask_rle' in det:
            entry['mask_rle'] = det['mask_rle']
        detections.append(entry)
    return detections


def _safe_int_key(key: str):
    try:
        return int(key)
    except (TypeError, ValueError):
        return key


def _presign_if_exists(s3, bucket: str, key: str, all_keys: set) -> Optional[str]:
    """
    Generate a presigned GET URL for `key` if it exists under the listed prefix.
    Missing artifacts return None; presigned-URL failures are logged and skipped
    (mirrors datasets.get_image_preview error handling).
    """
    if key not in all_keys:
        return None
    try:
        return s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket, 'Key': key},
            ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS
        )
    except Exception as e:
        print(f"Failed to generate presigned URL for {key}: {str(e)}")
        return None
