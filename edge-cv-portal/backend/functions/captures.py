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
from typing import Dict, List, Any, Optional

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    get_usecase,
    assume_usecase_role,
    create_response,
    handle_error
)

s3_client = boto3.client('s3')

# observedContentType markers written by the Marshal
# (see marshal_for_capture_template.py / utils/constants.py).
INFERENCE_RESULT_CONTENT_TYPE = "json"                       # the inf_result summary
DETECTIONS_BLOCK_CONTENT_TYPE = "json_with_base64_encoding"  # detections / anomalies block
OVERLAY_CONTENT_TYPE = "overlay.jpg"
MASK_CONTENT_TYPE = "mask.png"

# Presigned URLs are valid for 30 minutes (mirrors datasets.get_image_preview).
PRESIGNED_URL_EXPIRY_SECONDS = 1800


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


def list_captures(event):
    """
    List inference-results captures under a prefix and return parsed metadata
    plus presigned URLs for the associated artifacts.

    Query Parameters:
        - usecase_id: Required. The use case ID.
        - prefix:     Required. The S3 prefix (capture folder) to list.
        - device_id:  Optional. Device/thing identifier; appended to the prefix
                      when the prefix does not already scope to a device.
        - limit:      Optional. Max captures to return (default: 20, max: 100).

    Returns:
        { captures: [ { capture_id, inference_result_type, detection_count,
                        detections: [...], source_url, overlay_url, mask_url } ],
          bucket, prefix, total_found, expires_in_seconds }
    """
    try:
        params = event.get('queryStringParameters', {}) or {}
        usecase_id = params.get('usecase_id')
        prefix = params.get('prefix')
        device_id = params.get('device_id')
        limit = min(int(params.get('limit', '20')), 100)

        if not usecase_id or not prefix:
            return create_response(400, {
                'error': 'usecase_id and prefix are required'
            })

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

        # Scope the search prefix to the device when provided and not already present
        search_prefix = prefix
        if device_id and device_id not in search_prefix:
            search_prefix = f"{search_prefix.rstrip('/')}/{device_id}"

        # List all keys under the prefix so we can (a) find capture metadata files
        # and (b) know which sibling artifacts actually exist (missing artifacts
        # -> null URLs) without extra head_object calls.
        all_keys = set()
        jsonl_keys = []

        paginator = s3.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket, Prefix=search_prefix)

        for page in pages:
            for obj in page.get('Contents', []):
                key = obj['Key']
                all_keys.add(key)
                if key.endswith('.jsonl'):
                    jsonl_keys.append(key)

        captures = []
        for jsonl_key in jsonl_keys:
            if len(captures) >= limit:
                break

            capture = _parse_capture(s3, bucket, jsonl_key, all_keys)
            if capture:
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


def _parse_capture(s3, bucket: str, jsonl_key: str, all_keys: set) -> Optional[Dict[str, Any]]:
    """
    Parse a single capture's `.jsonl` metadata and build its response entry.

    Tolerates a missing/parse-failed metadata file (returns an entry with empty
    detections) and missing sibling artifacts (null URLs). Returns None only if
    the metadata object cannot be fetched at all.
    """
    # capture_id and the sibling-artifact key base derive from the jsonl key,
    # e.g. "device/folder/<capture_id>.jsonl" -> base "device/folder/<capture_id>"
    key_base = jsonl_key[:-len('.jsonl')]
    capture_id = os.path.basename(key_base)

    inference_result_type = None
    detection_count = 0
    detections: List[Dict[str, Any]] = []

    try:
        body = s3.get_object(Bucket=bucket, Key=jsonl_key)['Body'].read()
        # A capture .jsonl holds one metadata object per line; use the last
        # non-empty line as the capture's metadata.
        metadata = _load_last_metadata(body)
        if metadata is not None:
            inference_result_type, detection_count, detections = _extract_inference_data(metadata)
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


def _extract_inference_data(metadata: Dict[str, Any]):
    """
    Extract (inference_result_type, detection_count, detections) from a parsed
    Capture_Metadata object.

    - inference_result_type comes from the base64 `json` summary
      ("Inference result": Detection / Anomaly / Normal).
    - detection_count comes from the same summary ("Detection_count").
    - detections come from the base64 `json_with_base64_encoding` block whose
      decoded payload carries a top-level "detections" map; each entry is
      normalized to {class_index, class_label, bounding_box, confidence}.
      An anomaly capture (payload with an "anomalies" map) yields no detections.
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
    # "json_with_base64_encoding"). Decode and only treat it as detections when
    # the decoded payload carries a "detections" map (anomaly captures carry an
    # "anomalies" map instead).
    block_b64 = _get_aux_output(output_list, DETECTIONS_BLOCK_CONTENT_TYPE, 'data')
    if block_b64:
        try:
            block = json.loads(base64.b64decode(block_b64))
            det_map = block.get('detections')
            if isinstance(det_map, dict):
                detections = _normalize_detections(det_map)
        except Exception as e:
            print(f"Failed to decode detections block: {str(e)}")

    return inference_result_type, detection_count, detections


def _normalize_detections(det_map: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Convert the Marshal's detections map ({"0": {...}, "1": {...}}) into an
    ordered list of {class_index, class_label, bounding_box, confidence}.
    """
    detections = []
    # Preserve the numeric ordering of the string keys ("0","1","2",...).
    for key in sorted(det_map.keys(), key=_safe_int_key):
        det = det_map[key]
        if not isinstance(det, dict):
            continue
        detections.append({
            'class_index': det.get('class_index'),
            'class_label': det.get('class_label'),
            'bounding_box': det.get('bounding_box'),
            'confidence': det.get('confidence'),
        })
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
