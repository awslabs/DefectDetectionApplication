"""
Reference files uploaded for Bedrock Inference nodes (Workflow Manager).

A user who may edit a workflow uploads a reference file for a
``bedrock_inference`` node in the workflow editor. The browser PUTs the
bytes to a staging key of the Portal artifacts bucket through a presigned
URL; the complete route checks the staged bytes by their content, copies
them to a content-addressed key of the workflow that no client can write,
and answers the line ``upload://{sha256}.{ext}``, which the editor puts
into the node's ``reference_payload_path`` list. Saving the workflow saves
the line in the new version; packaging ships every file a version names
inside the workflow component (workflow_packaging.py), and the device reads
it from its own version directory.

Routes (API Gateway REST), dispatched and authorized by workflows.handler:
    POST /workflows/{id}/reference-files/upload-url  workflow:edit  presigned staging PUT
    POST /workflows/{id}/reference-files             workflow:edit  check and store a staged upload
    GET  /workflows/{id}/reference-files             workflow:read  the workflow's upload records

Storage (Portal artifacts bucket):
    workflow-reference-staging/{workflowId}/{32 hex}                    staged upload (1-day lifecycle rule)
    {WORKFLOWS_S3_PREFIX}/{usecaseId}/{workflowId}/references/{name}       stored file, written once
    {WORKFLOWS_S3_PREFIX}/{usecaseId}/{workflowId}/references/{name}.json  its upload record
where name is "{sha256}.{ext}". delete_workflow removes the stored files and
records with the rest of the workflow's prefix.

Every S3 call of this module goes through reference_s3_client() and asserts
the Portal's account as the bucket owner. File names are user data: they go
into the upload record and the audit event only, never into a log line, and
no log line carries file content or a presigned URL.
"""
import hashlib
import io
import json
import logging
import os
import re
import struct
import unicodedata
import uuid
import zipfile
import zlib
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import (
    BUCKET_OWNER_MISMATCH_MESSAGE, component_bucket_owner, create_response,
    is_bucket_owner_mismatch, log_audit_event
)
from workflow_core.anomaly_invocation import (
    KIND_DOCUMENT, KIND_IMAGE, MAX_DOCUMENT_BYTES, MAX_IMAGE_SIDE_PX,
    document_signature_problem, uploaded_reference_names
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
WORKFLOWS_S3_PREFIX = os.environ.get('WORKFLOWS_S3_PREFIX', 'workflows')

#: Staged uploads; the bucket's ExpireWorkflowReferenceStaging rule expires
#: what the complete route never consumed after one day.
STAGING_PREFIX = 'workflow-reference-staging/'
UPLOAD_URL_TTL_SECONDS = 900
#: An image as the device loads it (payload_fetch.MAX_REFERENCE_BYTES), a
#: document as Bedrock Converse takes it.
MAX_REFERENCE_IMAGE_BYTES = 8 * 1024 * 1024
MAX_REFERENCE_DOCUMENT_BYTES = MAX_DOCUMENT_BYTES
MAX_FILE_NAME_LENGTH = 255
RECORD_SCHEMA = 1
BEDROCK_INFERENCE_TYPE = 'bedrock_inference'

#: The accepted file-name extensions (lowercased) and their normalized form.
#: An image's stored extension is decided by its content (jpg or png).
NORMALIZED_EXTENSIONS = {
    '.jpg': 'jpg', '.jpeg': 'jpg', '.png': 'png',
    '.pdf': 'pdf', '.txt': 'txt', '.md': 'md', '.markdown': 'md',
    '.csv': 'csv', '.html': 'html', '.htm': 'html', '.doc': 'doc',
    '.docx': 'docx', '.xls': 'xls', '.xlsx': 'xlsx',
}
ACCEPTED_EXTENSIONS = tuple(NORMALIZED_EXTENSIONS)
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png')

#: Content type of a stored file by its extension; HTML is stored as plain
#: text, so a browser opening it shows its source instead of running it.
CONTENT_TYPES = {
    'jpg': 'image/jpeg',
    'png': 'image/png',
    'pdf': 'application/pdf',
    'txt': 'text/plain; charset=utf-8',
    'html': 'text/plain; charset=utf-8',
    'md': 'text/markdown; charset=utf-8',
    'csv': 'text/csv; charset=utf-8',
    'doc': 'application/msword',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xls': 'application/vnd.ms-excel',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}

#: Presigned staging PUTs must be SigV4, as camera_registry.PIN_S3_CLIENT_CONFIG
#: explains: a SigV2 presign signs Content-Type, which the browser sets from
#: the File, so S3 would answer every upload with 403 SignatureDoesNotMatch.
#: A SigV4 query-string presign signs only host and x-amz-expected-bucket-owner.
REFERENCE_UPLOAD_S3_CONFIG = Config(signature_version='s3v4')
_reference_s3_client = None


def reference_s3_client():
    """Lazy S3 client for every call of this module (presign, HeadObject,
    GetObject, CopyObject, PutObject, DeleteObject, ListObjectsV2). A
    swappable module seam, as camera_registry.pin_s3_client(), so tests can
    install recording or failing fakes."""
    global _reference_s3_client
    if _reference_s3_client is None:
        _reference_s3_client = boto3.client('s3', config=REFERENCE_UPLOAD_S3_CONFIG)
    return _reference_s3_client


class ReferenceFileCopyError(Exception):
    """copy_reference_files could not copy every named file and record."""


# --------------------------------------------------------------------------
# Keys, responses, request fields
# --------------------------------------------------------------------------

def stored_prefix(usecase_id: str, workflow_id: str) -> str:
    """The prefix of a workflow's stored reference files and records."""
    return f"{WORKFLOWS_S3_PREFIX}/{usecase_id}/{workflow_id}/references/"


def stored_key(usecase_id: str, workflow_id: str, name: str) -> str:
    return stored_prefix(usecase_id, workflow_id) + name


def record_key(usecase_id: str, workflow_id: str, name: str) -> str:
    return stored_key(usecase_id, workflow_id, name) + '.json'


def staging_prefix(workflow_id: str) -> str:
    return f"{STAGING_PREFIX}{workflow_id}/"


def definition_reference_names(definition: Mapping[str, Any]) -> List[str]:
    """The sorted union of the uploaded file names ("{sha256}.{ext}") that
    the upload lines of a stored definition's bedrock_inference nodes name."""
    names = set()
    for node in (definition or {}).get('nodes') or []:
        if isinstance(node, dict) and node.get('type') == BEDROCK_INFERENCE_TYPE:
            names.update(uploaded_reference_names(node.get('parameters') or {}))
    return sorted(names)


def error_response(status_code: int, code: str, message: str,
                   details: Optional[Dict] = None) -> Dict:
    """The Workflow Manager error envelope: {error: {code, message, details}}"""
    return create_response(status_code, {
        'error': {'code': code, 'message': message, 'details': details or {}}
    })


def now_ms() -> int:
    return int(datetime.utcnow().timestamp() * 1000)


def parse_body(event: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Parse the request body; returns (body, None) or (None, error_response)"""
    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return None, error_response(400, 'INVALID_JSON', 'Request body is not valid JSON')
    if not isinstance(body, dict):
        return None, error_response(400, 'INVALID_JSON', 'Request body must be a JSON object')
    return body, None


def checked_file_name(raw: Any) -> Tuple[Optional[str], Optional[Dict]]:
    """``(file_name, None)`` for an acceptable ``fileName``, else
    ``(None, error_response)``: the part after the last / or \\, trimmed,
    1 to 255 characters without control characters, with one of the
    accepted extensions."""
    if not isinstance(raw, str):
        return None, error_response(400, 'INVALID_FILE_NAME', 'fileName must be a string')
    name = re.split(r'[/\\]', raw)[-1].strip()
    if (not 1 <= len(name) <= MAX_FILE_NAME_LENGTH
            or any(unicodedata.category(ch) == 'Cc' for ch in name)):
        return None, error_response(
            400, 'INVALID_FILE_NAME',
            f'fileName must be 1 to {MAX_FILE_NAME_LENGTH} characters without '
            'control characters')
    if os.path.splitext(name)[1].lower() not in NORMALIZED_EXTENSIONS:
        return None, error_response(
            400, 'UNSUPPORTED_REFERENCE_FILE',
            'Reference files must be one of: ' + ', '.join(ACCEPTED_EXTENSIONS),
            {'accepted_extensions': list(ACCEPTED_EXTENSIONS)})
    return name, None


def is_image_file_name(file_name: str) -> bool:
    return os.path.splitext(file_name)[1].lower() in IMAGE_EXTENSIONS


def size_limit(file_name: str) -> int:
    """The byte cap of the file's family, by its extension."""
    return MAX_REFERENCE_IMAGE_BYTES if is_image_file_name(file_name) else MAX_REFERENCE_DOCUMENT_BYTES


def is_staging_key(staging_key: Any, workflow_id: str) -> bool:
    """True when ``staging_key`` is ``workflow-reference-staging/{this
    workflow}/`` followed by exactly 32 lowercase hexadecimal characters."""
    prefix = staging_prefix(workflow_id)
    return (isinstance(staging_key, str) and staging_key.startswith(prefix)
            and re.fullmatch(r'[0-9a-f]{32}', staging_key[len(prefix):]) is not None)


# --------------------------------------------------------------------------
# Content validation (standard library only; design §9.13.4)
# --------------------------------------------------------------------------

JPEG_SIGNATURE = b'\xff\xd8\xff'
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
#: Output bytes of one inflate step of the PNG image-data check.
INFLATE_STEP_BYTES = 1 << 20
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_PNG_BIT_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8),
                   4: (8, 16), 6: (8, 16)}
#: Adam7 passes: (x0, y0, dx, dy).
ADAM7_PASSES = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
                (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
#: JPEG start-of-frame markers (C0 to CF without DHT C4, JPG C8 and DAC CC).
_JPEG_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
_OOXML_MAIN_PARTS = {'docx': 'word/document.xml', 'xlsx': 'xl/workbook.xml'}


def _dimension_problem(width: int, height: int) -> Optional[str]:
    if 1 <= width <= MAX_IMAGE_SIDE_PX and 1 <= height <= MAX_IMAGE_SIDE_PX:
        return None
    return (f'it is {width} × {height} px; each side must be 1 to '
            f'{MAX_IMAGE_SIDE_PX} px')


def jpeg_dimensions(data: bytes) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    """``((width, height), None)`` for a structurally valid JPEG: a
    start-of-frame segment before the first start-of-scan, 1 to 8000 px a
    side, 1, 3 or 4 components, and an end marker after the first scan.
    Otherwise ``(None, reason)``. The entropy-coded data is not decoded."""
    n = len(data)
    pos = 2
    frame = None
    while True:
        if pos >= n or data[pos] != 0xFF:
            return None, 'its segment structure is broken or truncated'
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            return None, 'its segment structure is broken or truncated'
        marker = data[pos]
        pos += 1
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            continue  # markers without a length
        if marker == 0xD9:
            return None, 'it ends before its image data'
        if pos + 2 > n:
            return None, 'its segment structure is broken or truncated'
        length = (data[pos] << 8) | data[pos + 1]
        if length < 2 or pos + length > n:
            return None, 'its segment structure is broken or truncated'
        if marker in _JPEG_SOF_MARKERS:
            if length < 8:
                return None, 'its frame header is too short'
            height = (data[pos + 3] << 8) | data[pos + 4]
            width = (data[pos + 5] << 8) | data[pos + 6]
            components = data[pos + 7]
            problem = _dimension_problem(width, height)
            if problem:
                return None, problem
            if components not in (1, 3, 4):
                return None, f'it has {components} colour components; 1, 3 or 4 are accepted'
            frame = (width, height)
        elif marker == 0xDA:
            if frame is None:
                return None, 'it has no frame header before its image data'
            if data.find(b'\xff\xd9', pos + length) < 0:
                return None, 'it has no end marker'
            return frame, None
        pos += length


def png_image_data_length(width: int, height: int, colour_type: int,
                          bit_depth: int, interlace: int) -> int:
    """The inflated length of a PNG's image data that its IHDR declares:
    over the image's passes, pass_height × (1 + ceil(pass_width × channels
    × bit_depth / 8)), the 1 being each row's filter byte. One pass of the
    full image without interlacing, the seven Adam7 passes with it; a pass
    whose width or height is 0 has no rows."""
    channels = _PNG_CHANNELS[colour_type]
    passes = ADAM7_PASSES if interlace == 1 else ((0, 0, 1, 1),)
    total = 0
    for x0, y0, dx, dy in passes:
        pass_width = -(-(width - x0) // dx) if width > x0 else 0
        pass_height = -(-(height - y0) // dy) if height > y0 else 0
        if pass_width and pass_height:
            total += pass_height * (1 + -(-(pass_width * channels * bit_depth) // 8))
    return total


def png_inflate_problem(idat: Sequence[memoryview], expected: int) -> Optional[str]:
    """Inflate the IDAT data with one decompressor, in steps of at most
    INFLATE_STEP_BYTES of output that are counted and discarded, and stop as
    soon as the count exceeds ``expected``: the work is bounded by
    ``expected`` plus one step, whatever the input. Data after the end of the
    zlib stream is ignored."""
    decompressor = zlib.decompressobj()
    produced = 0
    try:
        for chunk in idat:
            data = chunk
            while True:
                out = decompressor.decompress(data, INFLATE_STEP_BYTES)
                produced += len(out)
                if produced > expected:
                    return 'its image data inflates beyond its declared size'
                if decompressor.eof:
                    break
                data = decompressor.unconsumed_tail
                # Input consumed and output not cut at the step: nothing pending.
                if not data and len(out) < INFLATE_STEP_BYTES:
                    break
            if decompressor.eof:
                break
    except zlib.error:
        return 'its image data is corrupt'
    if not decompressor.eof or produced != expected:
        return 'its image data is truncated'
    return None


def png_dimensions(data: bytes) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    """``((width, height), None)`` for a valid PNG: IHDR first, 1 to 8000 px
    a side, a bit depth valid for its colour type, compression and filter
    method 0, interlace 0 or 1, a valid CRC on every chunk, at least one
    IDAT, IEND last, and image data that inflates to exactly the length the
    IHDR declares. Otherwise ``(None, reason)``."""
    view = memoryview(data)
    n = len(data)
    pos = len(PNG_SIGNATURE)
    header = None
    idat: List[memoryview] = []
    while True:
        if pos + 12 > n:
            return None, 'it is truncated'
        length, chunk_type = struct.unpack('>I4s', view[pos:pos + 8])
        end = pos + 12 + length
        if length > 0x7FFFFFFF or end > n:
            return None, 'it is truncated'
        chunk = view[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack('>I', view[end - 4:end])
        if zlib.crc32(chunk, zlib.crc32(chunk_type)) & 0xFFFFFFFF != crc:
            return None, 'a chunk fails its CRC check'
        if header is None:
            if chunk_type != b'IHDR' or length != 13:
                return None, 'its first chunk is not IHDR'
            (width, height, bit_depth, colour_type, compression, filter_method,
             interlace) = struct.unpack('>IIBBBBB', chunk)
            problem = _dimension_problem(width, height)
            if problem:
                return None, problem
            if bit_depth not in _PNG_BIT_DEPTHS.get(colour_type, ()):
                return None, (f'its bit depth {bit_depth} is not valid for '
                              f'colour type {colour_type}')
            if compression != 0 or filter_method != 0:
                return None, 'it uses an unknown compression or filter method'
            if interlace not in (0, 1):
                return None, 'it uses an unknown interlace method'
            header = (width, height, bit_depth, colour_type, interlace)
        elif chunk_type == b'IDAT':
            idat.append(chunk)
        elif chunk_type == b'IEND':
            break
        pos = end
    if not idat:
        return None, 'it has no image data'
    width, height, bit_depth, colour_type, interlace = header
    problem = png_inflate_problem(
        idat, png_image_data_length(width, height, colour_type, bit_depth, interlace))
    if problem:
        return None, problem
    return (width, height), None


def ooxml_problem(fmt: str, data: bytes) -> Optional[str]:
    """A DOCX or XLSX: a ZIP archive whose central directory lists
    [Content_Types].xml and the format's main part. Nothing is
    decompressed."""
    problem = document_signature_problem(fmt, data)
    if problem:
        return problem
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError, OSError):
        return 'it is not a readable ZIP archive'
    for part in ('[Content_Types].xml', _OOXML_MAIN_PARTS[fmt]):
        if part not in names:
            return f'it has no {part} part'
    return None


def inspect_reference_file(file_name: str, data: bytes
                           ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Check an upload's bytes against its file name's extension.

    Returns ``(description, None)`` with ``ext`` (the stored extension),
    ``kind``, ``format`` and, for an image, ``width`` and ``height``; or
    ``(None, reason)``. The content decides the family: an image extension
    accepts a JPEG or a PNG, whichever the content is (a PNG named .jpg is
    stored as png). The reason never contains the file name or content."""
    extension = os.path.splitext(file_name)[1].lower()
    if extension in IMAGE_EXTENSIONS:
        if data.startswith(JPEG_SIGNATURE):
            size, reason = jpeg_dimensions(data)
            ext, fmt = 'jpg', 'jpeg'
        elif data.startswith(PNG_SIGNATURE):
            size, reason = png_dimensions(data)
            ext, fmt = 'png', 'png'
        else:
            return None, 'it starts with neither the JPEG nor the PNG signature'
        if reason:
            return None, reason
        return {'ext': ext, 'kind': KIND_IMAGE, 'format': fmt,
                'width': size[0], 'height': size[1]}, None
    ext = NORMALIZED_EXTENSIONS[extension]
    reason = (ooxml_problem(ext, data) if ext in _OOXML_MAIN_PARTS
              else document_signature_problem(ext, data))
    if reason:
        return None, reason
    return {'ext': ext, 'kind': KIND_DOCUMENT, 'format': ext}, None


def rejection_message(file_name: str, reason: str) -> str:
    if is_image_file_name(file_name):
        return f'{file_name} is not a valid JPEG or PNG image: {reason}'
    ext = NORMALIZED_EXTENSIONS[os.path.splitext(file_name)[1].lower()]
    return f'{file_name} is not a valid {ext.upper()} document: {reason}'


# --------------------------------------------------------------------------
# S3 helpers
# --------------------------------------------------------------------------

def _error_code(error: Exception) -> str:
    if isinstance(error, ClientError):
        return str((error.response.get('Error') or {}).get('Code') or 'unknown')
    return type(error).__name__


def _is_not_found(error: Exception) -> bool:
    if not isinstance(error, ClientError):
        return False
    status = (error.response.get('ResponseMetadata') or {}).get('HTTPStatusCode')
    return _error_code(error) in ('404', 'NoSuchKey', 'NotFound') or status == 404


def _delete_staged(s3, staging_key: str, owner: str) -> None:
    """Best-effort staging cleanup; the 1-day lifecycle rule is the backstop."""
    try:
        s3.delete_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=staging_key,
                         ExpectedBucketOwner=owner)
    except Exception as e:  # noqa: BLE001 — best-effort cleanup only
        logger.warning(f"Best-effort delete of staged reference upload {staging_key} "
                       f"failed: {_error_code(e)}")


def _store_failure(s3, staging_key: str, owner: str, workflow_id: str,
                   error: Exception, operation: str) -> Dict:
    """502 for a failed S3 call after the request was accepted; the staged
    object is deleted best-effort and nothing else changes."""
    _delete_staged(s3, staging_key, owner)
    if is_bucket_owner_mismatch(error):
        logger.error(BUCKET_OWNER_MISMATCH_MESSAGE.format(
            bucket=PORTAL_ARTIFACTS_BUCKET, account=owner) + f" ({operation})")
        return error_response(
            502, 'BUCKET_OWNERSHIP_CONFLICT',
            'Reference file bucket ownership conflict: the Portal artifacts '
            'bucket is not owned by the portal account; refusing to use it. '
            'Contact the portal administrator.')
    logger.error(f"Reference file store failed for workflow {workflow_id} at "
                 f"{operation}: {_error_code(error)}")
    return error_response(502, 'REFERENCE_FILE_STORE_FAILED',
                          'The reference file could not be stored; upload it again')


def _parse_record(raw: bytes, name: str) -> Optional[Dict[str, Any]]:
    """An upload record: a JSON object of schema 1 whose name is ``name``."""
    try:
        record = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return None
    if (not isinstance(record, dict) or record.get('schema') != RECORD_SCHEMA
            or record.get('name') != name):
        return None
    return record


def _read_record(s3, key: str, name: str, owner: str) -> Optional[Dict[str, Any]]:
    """The valid record at ``key``, or None when it is missing or invalid."""
    try:
        raw = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=key,
                            ExpectedBucketOwner=owner)['Body'].read()
    except ClientError as e:
        if _is_not_found(e):
            return None
        raise
    return _parse_record(raw, name)


def _put_record(s3, key: str, record: Dict[str, Any], owner: str) -> None:
    s3.put_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=key,
                  Body=json.dumps(record).encode('utf-8'),
                  ContentType='application/json', ExpectedBucketOwner=owner)


def _list_keys(s3, prefix: str, owner: str) -> Iterable[str]:
    token = None
    while True:
        kwargs = {'Bucket': PORTAL_ARTIFACTS_BUCKET, 'Prefix': prefix,
                  'ExpectedBucketOwner': owner}
        if token:
            kwargs['ContinuationToken'] = token
        listed = s3.list_objects_v2(**kwargs)
        for entry in listed.get('Contents', []):
            yield entry['Key']
        if not listed.get('IsTruncated'):
            return
        token = listed.get('NextContinuationToken')


def build_record(name: str, sha256: str, info: Mapping[str, Any], size: int,
                 file_name: str, user: Mapping[str, Any]) -> Dict[str, Any]:
    """The upload record of a stored file (design §9.13.2)."""
    record = {
        'schema': RECORD_SCHEMA,
        'name': name,
        'line': f'upload://{name}',
        'sha256': sha256,
        'ext': info['ext'],
        'kind': info['kind'],
        'format': info['format'],
        'bytes': size,
    }
    if info['kind'] == KIND_IMAGE:
        record['width'] = info['width']
        record['height'] = info['height']
    record['fileName'] = file_name
    record['uploadedBy'] = user['user_id']
    record['uploadedAt'] = now_ms()
    return record


# --------------------------------------------------------------------------
# Routes (the workflow item is authorized by workflows.handler)
# --------------------------------------------------------------------------

def upload_url(event: Dict, user: Dict, item: Dict) -> Dict:
    """POST /workflows/{id}/reference-files/upload-url
    Body: {fileName}. A presigned SigV4 PUT to a fresh staging key, valid for
    15 minutes; no S3 call is made."""
    body, err = parse_body(event)
    if err:
        return err
    file_name, err = checked_file_name(body.get('fileName'))
    if err:
        return err
    workflow_id = item['workflow_id']
    staging_key = f"{staging_prefix(workflow_id)}{uuid.uuid4().hex}"
    owner = component_bucket_owner()
    # ExpectedBucketOwner is a header member of PutObject, so the presign
    # signs x-amz-expected-bucket-owner and the browser must send it
    # (uploadHeaders); no ContentType, so the File's type is not signed.
    url = reference_s3_client().generate_presigned_url(
        'put_object',
        Params={'Bucket': PORTAL_ARTIFACTS_BUCKET, 'Key': staging_key,
                'ExpectedBucketOwner': owner},
        ExpiresIn=UPLOAD_URL_TTL_SECONDS,
    )
    logger.info(f"Issued a reference file upload URL for workflow {workflow_id}")
    return create_response(200, {
        'uploadUrl': url,
        'uploadHeaders': {'x-amz-expected-bucket-owner': owner},
        'stagingKey': staging_key,
        'expiresInSeconds': UPLOAD_URL_TTL_SECONDS,
        'maxBytes': size_limit(file_name),
    })


def complete_upload(event: Dict, user: Dict, item: Dict) -> Dict:
    """POST /workflows/{id}/reference-files
    Body: {stagingKey, fileName}. Checks the staged upload, stores it once
    under its content digest with its record, and deletes the staged
    object: 201 {file, created: true}, or 200 {file, created: false} when the
    same file is already stored."""
    workflow_id = item['workflow_id']
    usecase_id = item['usecase_id']
    body, err = parse_body(event)
    if err:
        return err
    staging_key = body.get('stagingKey')
    if not is_staging_key(staging_key, workflow_id):
        return error_response(
            400, 'INVALID_STAGING_KEY',
            'stagingKey must be a key the upload-url route issued for this workflow')
    file_name, err = checked_file_name(body.get('fileName'))
    if err:
        return err

    s3 = reference_s3_client()
    owner = component_bucket_owner()
    limit = size_limit(file_name)

    def reject(status: int, code: str, message: str, reason: str) -> Dict:
        _delete_staged(s3, staging_key, owner)
        logger.info(f"Rejected a reference file for workflow {workflow_id}: {code}: {reason}")
        return error_response(status, code, message)

    too_large = f'{file_name} is larger than the {limit}-byte limit for its file type'
    try:
        head = s3.head_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=staging_key,
                              ExpectedBucketOwner=owner)
    except ClientError as e:
        if _is_not_found(e):
            return error_response(
                400, 'STAGED_UPLOAD_NOT_FOUND',
                'Staged upload not found; request a new upload URL and upload '
                'the file again')
        return _store_failure(s3, staging_key, owner, workflow_id, e, 'HeadObject')
    if int(head.get('ContentLength') or 0) > limit:
        return reject(400, 'REFERENCE_FILE_TOO_LARGE', too_large,
                      f'{head.get("ContentLength")} bytes')
    try:
        data = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=staging_key,
                             ExpectedBucketOwner=owner)['Body'].read()
    except ClientError as e:
        return _store_failure(s3, staging_key, owner, workflow_id, e, 'GetObject')
    if len(data) > limit:  # defense in depth over the HeadObject check
        return reject(400, 'REFERENCE_FILE_TOO_LARGE', too_large, f'{len(data)} bytes')

    info, reason = inspect_reference_file(file_name, data)
    if reason:
        return reject(400, 'REFERENCE_FILE_INVALID',
                      rejection_message(file_name, reason), reason)

    sha256 = hashlib.sha256(data).hexdigest()
    name = f"{sha256}.{info['ext']}"
    key = stored_key(usecase_id, workflow_id, name)
    json_key = record_key(usecase_id, workflow_id, name)
    operation = 'HeadObject'
    try:
        try:
            s3.head_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=key,
                           ExpectedBucketOwner=owner)
            exists = True
        except ClientError as e:
            if not _is_not_found(e):
                raise
            exists = False
        if exists:
            # Written once: an existing stored file keeps its bytes and its
            # record; only an interrupted earlier complete leaves no record.
            operation = 'GetObject'
            record = _read_record(s3, json_key, name, owner)
            if record is None:
                operation = 'PutObject'
                record = build_record(name, sha256, info, len(data), file_name, user)
                _put_record(s3, json_key, record, owner)
        else:
            operation = 'CopyObject'
            s3.copy_object(
                Bucket=PORTAL_ARTIFACTS_BUCKET, Key=key,
                CopySource={'Bucket': PORTAL_ARTIFACTS_BUCKET, 'Key': staging_key},
                MetadataDirective='REPLACE', ContentType=CONTENT_TYPES[info['ext']],
                ExpectedBucketOwner=owner, ExpectedSourceBucketOwner=owner,
            )
            operation = 'PutObject'
            record = build_record(name, sha256, info, len(data), file_name, user)
            _put_record(s3, json_key, record, owner)
    except ClientError as e:
        return _store_failure(s3, staging_key, owner, workflow_id, e, operation)
    _delete_staged(s3, staging_key, owner)

    if exists:
        logger.info(f"Reference file {name} ({len(data)} bytes) is already stored "
                    f"for workflow {workflow_id}")
        return create_response(200, {'file': record, 'created': False})
    log_audit_event(
        user_id=user['user_id'],
        action='upload_workflow_reference_file',
        resource_type='workflow',
        resource_id=workflow_id,
        result='success',
        details={'usecase_id': usecase_id, 'name': name, 'kind': info['kind'],
                 'format': info['format'], 'bytes': len(data), 'file_name': file_name}
    )
    logger.info(f"Stored reference file {name} ({len(data)} bytes) for workflow {workflow_id}")
    return create_response(201, {'file': record, 'created': True})


def list_files(event: Dict, user: Dict, item: Dict) -> Dict:
    """GET /workflows/{id}/reference-files
    The workflow's upload records, sorted by uploadedAt, then name. A record
    that does not parse, or whose name differs from its key, is skipped."""
    s3 = reference_s3_client()
    owner = component_bucket_owner()
    prefix = stored_prefix(item['usecase_id'], item['workflow_id'])
    records = []
    for key in list(_list_keys(s3, prefix, owner)):
        name = key[len(prefix):]
        if not name.endswith('.json') or '/' in name:
            continue
        name = name[:-len('.json')]
        record = _read_record(s3, key, name, owner)
        if record is None:
            logger.warning(f"Skipping the unreadable reference file record {key}")
            continue
        records.append(record)
    records.sort(key=lambda r: (r['uploadedAt'] if isinstance(r.get('uploadedAt'), (int, float))
                                else 0, r['name']))
    return create_response(200, {'files': records})


def copy_reference_files(usecase_id: str, source_workflow_id: str,
                         target_workflow_id: str, names: Sequence[str]) -> None:
    """Copy each named stored file and its record from the source workflow's
    prefix to the target's (duplicate_workflow, design §9.13.5). On a failure
    the objects already copied are deleted best-effort and
    ReferenceFileCopyError is raised."""
    s3 = reference_s3_client()
    owner = component_bucket_owner()
    copied: List[str] = []
    try:
        for name in names:
            for source, target in (
                    (stored_key(usecase_id, source_workflow_id, name),
                     stored_key(usecase_id, target_workflow_id, name)),
                    (record_key(usecase_id, source_workflow_id, name),
                     record_key(usecase_id, target_workflow_id, name))):
                s3.copy_object(
                    Bucket=PORTAL_ARTIFACTS_BUCKET, Key=target,
                    CopySource={'Bucket': PORTAL_ARTIFACTS_BUCKET, 'Key': source},
                    ExpectedBucketOwner=owner, ExpectedSourceBucketOwner=owner,
                )
                copied.append(target)
    except ClientError as e:
        logger.error(f"Copying reference files from workflow {source_workflow_id} to "
                     f"{target_workflow_id} failed: {_error_code(e)}")
        for key in copied:
            try:
                s3.delete_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=key,
                                 ExpectedBucketOwner=owner)
            except Exception as cleanup_error:  # noqa: BLE001 — best-effort cleanup only
                logger.warning(f"Best-effort delete of copied reference object {key} "
                               f"failed: {_error_code(cleanup_error)}")
        raise ReferenceFileCopyError(_error_code(e)) from e
    logger.info(f"Copied {len(names)} reference file(s) from workflow {source_workflow_id} "
                f"to {target_workflow_id}")
