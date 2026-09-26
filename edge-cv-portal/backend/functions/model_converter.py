"""
Model Converter Lambda functions
Auto-generates DDA-compatible metadata from raw PyTorch models
Enables easy BYOM by accepting just a .pt file and user-provided dimensions
"""
import json
import os
import logging
from typing import Dict, Any, List, Tuple, Optional
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
import uuid
import tarfile
import tempfile
import shutil
from urllib.parse import urlparse
import yaml

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, validate_required_fields
)
# Envelope-only checkpoint classifier (stdlib, never raises, no torch import).
# Smart Import keeps a fine-tunable .pt/.pth as a sidecar so it can later be a
# base model (rfdetr-training-and-transfer-learning Requirement 7).
from checkpoint_probe import classify_checkpoint
# Checkpoint -> ONNX conversion of imported detectors (detector-checkpoint-
# import): the pure assessment / request / job / record pieces (stdlib only).
# The checkpoint itself is only ever deserialized inside the network-isolated
# Conversion_Job, never in this Lambda.
import hashlib
import re
import time
import detector_conversion as dconv
from s3_cors import ensure_bucket_cors
from botocore.config import Config

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
sts = boto3.client('sts')

# Environment variables
TRAINING_JOBS_TABLE = os.environ.get('TRAINING_JOBS_TABLE')
USECASES_TABLE = os.environ.get('USECASES_TABLE')
# The pinned Export_Image (-c detectorExportImage); '' = conversion disabled.
DETECTOR_EXPORT_IMAGE = os.environ.get('DETECTOR_EXPORT_IMAGE', '').strip()
# Browser uploads (detector-checkpoint-import Requirement 3): a server-issued
# key under this prefix of the use case bucket, presigned for 15 minutes with
# SigV4. SigV4 query-string presigns sign only `host`, so the Content-Type the
# browser picks cannot break the signature (camera_registry.PIN_S3_CLIENT_CONFIG).
MODEL_UPLOAD_PREFIX = 'model-uploads'
MODEL_UPLOAD_URL_TTL_S = 900
UPLOAD_S3_CLIENT_CONFIG = Config(signature_version='s3v4')


# ── Trusted model-source allowlist (#8) ─────────────────────────────────────
# A user-provided model_s3_uri is untrusted: a malicious .pt could execute
# arbitrary code during torch.load (torch.load is RCE-capable via its
# serializer). The primary neutralization is weights_only=True (see
# inspect_pytorch_model). As defense-in-depth AND to gate
# the legitimate full-checkpoint weights_only=False fallback, the source S3
# bucket/account is validated against a CONFIG-DRIVEN allowlist populated from
# environment variables (comma-separated):
#   * TRUSTED_MODEL_BUCKETS  — exact bucket names that are trusted.
#   * TRUSTED_MODEL_ACCOUNTS — account IDs; a bucket whose name embeds one of
#     these account IDs (a common naming convention) is treated as trusted.
# The use case's own application-owned s3_bucket is always trusted.
def _config_trusted_model_buckets() -> set:
    raw = os.environ.get('TRUSTED_MODEL_BUCKETS', '')
    return {b.strip() for b in raw.split(',') if b.strip()}


def _config_trusted_model_accounts() -> set:
    raw = os.environ.get('TRUSTED_MODEL_ACCOUNTS', '')
    return {a.strip() for a in raw.split(',') if a.strip()}


def is_trusted_model_source(model_s3_uri: str, usecase: Optional[Dict] = None) -> bool:
    """Return True iff the model_s3_uri points at a bucket/account on the
    config-driven trusted allowlist.

    Only allowlisted sources are downloaded/inspected, and only allowlisted
    sources may use the weights_only=False full-checkpoint fallback. Anything
    else is rejected (the caller returns a 400)."""
    try:
        parsed = urlparse(model_s3_uri)
    except Exception:
        return False
    bucket = parsed.netloc
    if not bucket:
        return False

    trusted_buckets = _config_trusted_model_buckets()
    # The use case's own application-owned bucket is always trusted.
    if usecase and usecase.get('s3_bucket'):
        trusted_buckets.add(usecase['s3_bucket'])
    if bucket in trusted_buckets:
        return True

    # Account-based allowlisting: a bucket that embeds a trusted account id.
    for account in _config_trusted_model_accounts():
        if account and account in bucket:
            return True
    return False

# Supported model types
MODEL_TYPES = {
    'classification': {
        'description': 'Image classification (binary or multi-class)',
        'output_format': '[batch, num_classes]'
    },
    'object_detection': {
        'description': 'Object detection (YOLO, SSD, etc.)',
        'output_format': '[batch, detections, attributes]'
    },
    'segmentation': {
        'description': 'Semantic segmentation',
        'output_format': '[batch, num_classes, height, width]'
    },
    'anomaly_detection': {
        'description': 'Anomaly detection (normal vs anomaly)',
        'output_format': '[batch, 2]'
    }
}


def assume_usecase_role(role_arn: str, external_id: str, session_name: str) -> Dict:
    """Assume cross-account role for UseCase Account access.

    Single-account setups store the account *root* ARN
    (arn:aws:iam::ACCOUNT_ID:root) as the "cross_account_role_arn" — that is not
    an assumable role, so attempting sts:AssumeRole on it fails with
    AccessDenied. In that case the Lambda's own execution role already has
    access to the (same-account) UseCase resources, so signal the caller to use
    the default credential chain instead of assuming a role.
    """
    if role_arn and role_arn.endswith(':root'):
        logger.info("Single-account setup (root ARN) — using Lambda execution role credentials")
        return {'is_default_credentials': True}
    try:
        response = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            ExternalId=external_id,
            DurationSeconds=3600
        )
        return response['Credentials']
    except ClientError as e:
        logger.error(f"Error assuming role {role_arn}: {str(e)}")
        raise


def make_usecase_s3_client(credentials: Dict):
    """Build an S3 client from assume_usecase_role() output.

    For single-account setups (is_default_credentials) use the Lambda's own
    credentials; otherwise use the assumed-role credentials.
    """
    if credentials.get('is_default_credentials'):
        return boto3.client('s3')
    return boto3.client(
        's3',
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken'],
    )


def get_usecase_details(usecase_id: str) -> Dict:
    """Get use case details from DynamoDB"""
    try:
        table = dynamodb.Table(USECASES_TABLE)
        response = table.get_item(Key={'usecase_id': usecase_id})
        
        if 'Item' not in response:
            raise ValueError(f"Use case {usecase_id} not found")
        
        return response['Item']
    except Exception as e:
        logger.error(f"Error getting use case details: {str(e)}")
        raise


# ── Imported detector checkpoints (detector-checkpoint-import) ──────────────
# Inspect classifies a .pt/.pth with the envelope-only probe and reports
# whether it converts (Requirement 2); upload-url issues a presigned PUT into a
# server-chosen staging key (Requirement 3); convert starts a network-isolated
# Conversion_Job and writes the Conversion_Record (Requirement 4). Nothing here
# imports torch or deserializes a checkpoint.

def make_usecase_client(service: str, credentials: Dict, region: Optional[str] = None,
                        config: Optional[Config] = None):
    """Any boto3 client from assume_usecase_role() output (the
    make_usecase_s3_client pattern, plus region and botocore config)."""
    kwargs: Dict[str, Any] = {}
    if region:
        kwargs['region_name'] = region
    if config is not None:
        kwargs['config'] = config
    if credentials.get('is_default_credentials'):
        return boto3.client(service, **kwargs)
    return boto3.client(
        service,
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken'],
        **kwargs,
    )


def _usecase_region(usecase: Dict) -> str:
    return str(usecase.get('region') or os.environ.get('AWS_REGION', 'us-east-1'))


def checkpoint_size_rejection(s3_client, bucket: str, key: str) -> Tuple[Optional[int], Optional[Dict]]:
    """HeadObject the source and enforce the Checkpoint_Size_Cap BEFORE any
    download (Requirements 2.7 and 3.6: a presigned PUT cannot bound what was
    uploaded). Returns (size, None), or (None, the 400 response)."""
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = str(e.response.get('Error', {}).get('Code', ''))
        if code in ('404', 'NoSuchKey', 'NotFound'):
            return None, create_response(400, {'error': f"model_s3_uri s3://{bucket}/{key} does not exist"})
        raise
    size = int(head.get('ContentLength') or 0)
    if size > dconv.CHECKPOINT_SIZE_CAP:
        return None, create_response(400, {'error': (
            f"Checkpoint is {size} bytes; the checkpoint size cap is {dconv.CHECKPOINT_SIZE_CAP} "
            f"bytes ({dconv.CHECKPOINT_SIZE_CAP >> 20} MiB)")})
    return size, None


def inspect_checkpoint_file(local_path: str, usecase: Dict) -> Dict:
    """Smart Import's inspection result for a .pt/.pth (Requirement 2): the
    probe's classification as the `checkpoint` block, with today's fields
    (type, suggested_type, detection_arch, num_classes, class_names,
    input_width/height, architecture_hints) pre-filled from it."""
    probe = classify_checkpoint(local_path)
    unavailable = dconv.conversion_unavailable_reason(DETECTOR_EXPORT_IMAGE, _usecase_region(usecase))
    assessment = dconv.assess_checkpoint(probe, conversion_available=unavailable is None,
                                         unavailable_reason=unavailable)
    info = dconv.assessment_prefill(assessment)
    info['checkpoint'] = assessment
    info['fine_tunable'] = bool(probe.get('fine_tunable'))
    return info


_UPLOAD_NAME_UNSAFE = re.compile(r'[^A-Za-z0-9._-]+')
_TAG_VALUE_UNSAFE = re.compile(r'[^\w\s.:/=+\-@]')


def sanitise_upload_name(file_name: str) -> str:
    """The last path component, reduced to [A-Za-z0-9_-] plus its accepted
    extension in lower case (so the stored key keeps the .pt / .pth / .onnx
    that inspect and convert route on); never empty, never a path."""
    base = str(file_name or '').replace('\\', '/').rsplit('/', 1)[-1]
    lower = base.lower()
    ext = next((e for e in dconv.UPLOAD_EXTENSIONS if lower.endswith(e)), '')
    stem = base[:len(base) - len(ext)]
    stem = _UPLOAD_NAME_UNSAFE.sub('_', stem.replace('.', '_')).strip('._-')[:100] or 'model'
    return f"{stem}{ext}"


def _tag_value(value: Any) -> str:
    return _TAG_VALUE_UNSAFE.sub('_', str(value))[:256]


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def get_model_upload_url(event: Dict, context: Any) -> Dict:
    """
    Presigned PUT for uploading a model file from the browser (Requirement 3).
    POST /api/v1/models/upload-url

    Request body: {"usecase_id": "...", "file_name": "best.pt", "size_bytes": 5475290}
    Response: {"upload_url", "model_s3_uri", "expires_in"}. The key is
    `model-uploads/<uuid4>/<sanitised file_name>` in the use case's own bucket;
    the caller never chooses bucket or prefix.
    """
    try:
        user = get_user_from_event(event)
        user_id = user['user_id']
        try:
            body = json.loads(event.get('body') or '{}')
        except ValueError:
            return create_response(400, {'error': 'Request body must be JSON'})
        if not isinstance(body, dict):
            return create_response(400, {'error': 'Request body must be a JSON object'})
        error = validate_required_fields(body, ['usecase_id', 'file_name', 'size_bytes'])
        if error:
            return create_response(400, {'error': error})
        usecase_id = str(body['usecase_id'])
        if not check_user_access(user_id, usecase_id, 'DataScientist'):
            return create_response(403, {'error': 'Insufficient permissions'})

        file_name = str(body['file_name'])
        if not file_name.lower().endswith(dconv.UPLOAD_EXTENSIONS):
            return create_response(400, {'error': (
                f"file_name must end in {', '.join(dconv.UPLOAD_EXTENSIONS)}; got {file_name!r}")})
        size = body['size_bytes']
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            return create_response(400, {'error': 'size_bytes must be a positive integer'})
        if size > dconv.CHECKPOINT_SIZE_CAP:
            return create_response(400, {'error': (
                f"File is {size} bytes; the checkpoint size cap is {dconv.CHECKPOINT_SIZE_CAP} bytes "
                f"({dconv.CHECKPOINT_SIZE_CAP >> 20} MiB)")})

        usecase = get_usecase_details(usecase_id)
        bucket = usecase.get('s3_bucket')
        if not bucket:
            return create_response(400, {'error': 'The use case has no S3 bucket'})
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase.get('external_id'),
            f"upload-{user_id[:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        )
        s3_client = make_usecase_client('s3', credentials, region=_usecase_region(usecase),
                                        config=UPLOAD_S3_CLIENT_CONFIG)
        key = f"{MODEL_UPLOAD_PREFIX}/{uuid.uuid4()}/{sanitise_upload_name(file_name)}"
        # Same bucket CORS handling as data_management.get_upload_url (Req 3.5).
        ensure_bucket_cors(s3_client, bucket)
        # No ContentType in Params: the browser's header is then not signed.
        upload_url = s3_client.generate_presigned_url(
            'put_object', Params={'Bucket': bucket, 'Key': key},
            ExpiresIn=MODEL_UPLOAD_URL_TTL_S)
        model_s3_uri = f"s3://{bucket}/{key}"
        log_audit_event(
            user_id=user_id,
            action='get_model_upload_url',
            resource_type='model',
            resource_id=model_s3_uri,
            result='success',
            details={'usecase_id': usecase_id, 'size_bytes': size},
        )
        return create_response(200, {'upload_url': upload_url, 'model_s3_uri': model_s3_uri,
                                     'expires_in': MODEL_UPLOAD_URL_TTL_S})
    except Exception as e:
        logger.error(f"Error issuing model upload URL: {str(e)}")
        return create_response(500, {'error': 'Failed to create upload URL'})


def convert_checkpoint(body: Dict, user: Dict, usecase: Dict, credentials: Dict, s3_client,
                       source_bucket: str, source_key: str, model_s3_uri: str) -> Dict:
    """POST /models/convert for a .pt/.pth with export_format='onnx'
    (Requirement 4): start a Conversion_Job and return the record id without
    waiting. The caller has already checked the DataScientist role and the
    trusted-source allowlist."""
    user_id = user['user_id']
    usecase_id = body['usecase_id']
    model_name = body['model_name'].strip()

    size, rejection = checkpoint_size_rejection(s3_client, source_bucket, source_key)
    if rejection is not None:
        return rejection
    temp_dir = tempfile.mkdtemp(prefix="model_convert_ckpt_")
    try:
        source_ext = os.path.splitext(source_key)[1].lstrip('.').lower() or 'pt'
        local_model = os.path.join(temp_dir, f'checkpoint.{source_ext}')
        s3_client.download_file(source_bucket, source_key, local_model)

        # (1) Re-classify server-side: the inspect result the client saw is
        # never trusted (Req 4.1). 400 with the reasons, nothing created.
        probe = classify_checkpoint(local_model)
        assessment = dconv.assess_checkpoint(probe)
        # (2) Boundary validation (Req 4.3-4.5).
        try:
            params = dconv.validate_conversion_request(body, assessment)
        except ValueError as e:
            return create_response(400, {'error': str(e), 'checkpoint': assessment})
        # (3) Is conversion deployed for this use case's region? (Req 4.8)
        region = _usecase_region(usecase)
        unavailable = dconv.conversion_unavailable_reason(DETECTOR_EXPORT_IMAGE, region)
        if unavailable:
            return create_response(503, {'error': unavailable})

        # (4) The fine-tunable sidecar, exactly as the 'pytorch' branch writes
        # it (same key scheme, same bytes, same fine_tunable shape), plus its
        # sha256. Its prefix is the job's only input channel.
        bucket = usecase['s3_bucket']
        safe_model_name = model_name.replace(' ', '_').replace('-', '_').lower()
        hex8 = uuid.uuid4().hex[:8]
        sidecar_prefix = f"converted-models/{safe_model_name}-{hex8}/"
        checkpoint_key = f"{sidecar_prefix}checkpoint.{source_ext}"
        checkpoint_s3 = f"s3://{bucket}/{checkpoint_key}"
        logger.info(f"Keeping fine-tunable {probe.get('kind')} checkpoint at {checkpoint_s3}")
        s3_client.upload_file(local_model, bucket, checkpoint_key)
        fine_tunable = {
            'arch': probe.get('arch'),
            'kind': probe.get('kind'),
            'checkpoint_s3': checkpoint_s3,
            'class_names': probe.get('class_names'),
            'num_classes': probe.get('num_classes'),
        }
        source_sha256 = _sha256_file(local_model)

        # (5) The Conversion_Job, from the same use-case credentials (Req 5).
        job_name = dconv.conversion_job_name(safe_model_name, datetime.utcnow().strftime('%Y%m%d%H%M%S'))
        request = dconv.build_conversion_job_request(
            job_name=job_name,
            image_uri=DETECTOR_EXPORT_IMAGE,
            role_arn=f"arn:aws:iam::{usecase['account_id']}:role/DDASageMakerExecutionRole",
            input_prefix_s3=f"s3://{bucket}/{sidecar_prefix}",
            output_s3=f"s3://{bucket}/models/conversion/{job_name}/",
            params=params,
            source_sha256=source_sha256,
            tags=[
                {'Key': 'UseCase', 'Value': _tag_value(usecase_id)},
                {'Key': 'ModelName', 'Value': _tag_value(model_name)},
                {'Key': 'CreatedBy', 'Value': _tag_value(user_id)},
                {'Key': 'Purpose', 'Value': 'detector-checkpoint-conversion'},
            ],
        )
        sagemaker_client = make_usecase_client('sagemaker', credentials, region=region)
        try:
            job_arn = sagemaker_client.create_training_job(**request)['TrainingJobArn']
        except ClientError as e:
            message = e.response.get('Error', {}).get('Message') or str(e)
            logger.error(f"create_training_job failed for {job_name}: {message}")
            try:  # no record will point at the sidecar: remove it (best effort)
                s3_client.delete_object(Bucket=bucket, Key=checkpoint_key)
            except Exception as cleanup_error:  # noqa: BLE001
                logger.warning(f"Could not remove sidecar {checkpoint_s3}: {cleanup_error}")
            return create_response(502, {'error': f"Could not start the conversion job: {message}"})

        # (6) The Conversion_Record, written here (Req 4.10).
        training_id = str(uuid.uuid4())
        record = dconv.build_conversion_record(
            training_id=training_id,
            usecase_id=usecase_id,
            model_name=model_name,
            model_version=str(body.get('model_version') or '1.0.0'),
            created_by=user.get('email') or user_id,
            params=params,
            assessment=assessment,
            fine_tunable=fine_tunable,
            job_name=job_name,
            job_arn=job_arn,
            image_uri=DETECTOR_EXPORT_IMAGE,
            source_s3=model_s3_uri,
            source_sha256=source_sha256,
            source_bytes=size,
            model_file=f"checkpoint.{source_ext}",
            now_ms=int(time.time() * 1000),
        )
        try:
            dynamodb.Table(TRAINING_JOBS_TABLE).put_item(Item=record)
        except Exception:
            # Never leave a job running that no record tracks.
            try:
                sagemaker_client.stop_training_job(TrainingJobName=job_name)
            except Exception as stop_error:  # noqa: BLE001
                logger.error(f"Could not stop orphaned conversion job {job_name}: {stop_error}")
            raise

        log_audit_event(
            user_id=user_id,
            action='convert_checkpoint',
            resource_type='model',
            resource_id=training_id,
            result='success',
            details={
                'source_uri': model_s3_uri,
                'source_sha256': source_sha256,
                'job_name': job_name,
                'export_image': DETECTOR_EXPORT_IMAGE,
                'detection_arch': params['arch'],
                'network_input': params['network_input'],
            },
        )
        return create_response(200, {
            'training_id': training_id,
            'model_name': model_name,
            'status': 'InProgress',
            'conversion': {'status': dconv.CONVERSION_IN_PROGRESS, 'job_name': job_name},
            'fine_tunable': fine_tunable,
        })
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ── Dependency-free ONNX graph reader ──────────────────────────────────────
# We only need the input/output tensor shapes to auto-detect model attributes.
# Rather than ship the heavy onnx/onnxruntime packages in the Lambda, parse the
# few protobuf fields we need straight from the ModelProto wire format:
#   ModelProto.graph            = field 7  (message GraphProto)
#   GraphProto.input            = field 11 (repeated ValueInfoProto)
#   GraphProto.output           = field 12 (repeated ValueInfoProto)
#   ValueInfoProto.name         = field 1  (string)
#   ValueInfoProto.type         = field 2  (TypeProto)
#   TypeProto.tensor_type       = field 1  (Tensor)
#   Tensor.elem_type            = field 1  (varint)
#   Tensor.shape                = field 2  (TensorShapeProto)
#   TensorShapeProto.dim        = field 1  (repeated Dimension)
#   Dimension.dim_value         = field 1  (varint int64)
#   Dimension.dim_param         = field 2  (string; dynamic axis => unknown)
def _pb_read_varint(buf: bytes, i: int):
    shift = 0
    result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7


def _pb_fields(buf: bytes):
    """Yield (field_number, wire_type, value) for a protobuf message. value is
    an int for varint/fixed wire types, or a bytes slice for length-delimited."""
    i = 0
    n = len(buf)
    while i < n:
        key, i = _pb_read_varint(buf, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:      # varint
            val, i = _pb_read_varint(buf, i)
            yield fn, wt, val
        elif wt == 2:    # length-delimited
            ln, i = _pb_read_varint(buf, i)
            yield fn, wt, buf[i:i + ln]
            i += ln
        elif wt == 1:    # 64-bit
            yield fn, wt, buf[i:i + 8]
            i += 8
        elif wt == 5:    # 32-bit
            yield fn, wt, buf[i:i + 4]
            i += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wt}")


def _pb_first(buf: bytes, field: int):
    for fn, _wt, val in _pb_fields(buf):
        if fn == field:
            return val
    return None


def _pb_all(buf: bytes, field: int):
    return [val for fn, _wt, val in _pb_fields(buf) if fn == field]


def _onnx_value_info_shape(vi_bytes: bytes):
    """Return (name, [dims]) for a ValueInfoProto. A dim is an int (static) or
    None (dynamic/unknown)."""
    name = None
    dims = []
    name_raw = _pb_first(vi_bytes, 1)
    if isinstance(name_raw, (bytes, bytearray)):
        name = name_raw.decode('utf-8', 'replace')
    type_proto = _pb_first(vi_bytes, 2)
    if type_proto is None:
        return name, dims
    tensor = _pb_first(type_proto, 1)   # TypeProto.tensor_type
    if tensor is None:
        return name, dims
    shape = _pb_first(tensor, 2)        # Tensor.shape
    if shape is None:
        return name, dims
    for dim_bytes in _pb_all(shape, 1):  # repeated Dimension
        dim_value = None
        for fn, wt, val in _pb_fields(dim_bytes):
            if fn == 1 and wt == 0:      # dim_value (static)
                dim_value = int(val)
            elif fn == 2:                # dim_param (dynamic axis) => unknown
                dim_value = None
        dims.append(dim_value)
    return name, dims


def inspect_onnx_model(model_path: str) -> Dict:
    """Parse an ONNX model's input/output tensor shapes and infer model
    attributes (input size, task/architecture, num_classes) for UI pre-fill.

    Detection heuristics from output tensor shapes:
      * 1 output  -> YOLO detection ([1, 4+C, N] or [1, N, 4+C]); C = the
        non-anchor dim minus 4.
      * 2 outputs -> RF-DETR detection (boxes [1,Q,4] + logits [1,Q,C]).
      * 3 outputs -> RF-DETR instance segmentation (adds a 4-D mask tensor).
      * 2-D single output [1, C] -> classification (C classes).
    """
    info: Dict[str, Any] = {'type': 'onnx', 'architecture_hints': []}
    try:
        with open(model_path, 'rb') as f:
            model_bytes = f.read()
        graph = _pb_first(model_bytes, 7)  # ModelProto.graph
        if graph is None:
            info['architecture_hints'].append('Could not read ONNX graph.')
            return info

        inputs = [_onnx_value_info_shape(v) for v in _pb_all(graph, 11)]
        outputs = [_onnx_value_info_shape(v) for v in _pb_all(graph, 12)]
        # Exclude initializer-backed inputs (weights): real inputs usually the
        # first entry. Use the first graph input as the data input.
        in_shapes = [dims for _n, dims in inputs if dims]
        out_shapes = [dims for _n, dims in outputs if dims]
        info['input_shapes'] = in_shapes
        info['output_shapes'] = out_shapes
        info['num_outputs'] = len(out_shapes)

        # Input NCHW -> channels/H/W (dynamic dims come back as None).
        if in_shapes:
            d = in_shapes[0]
            if len(d) == 4:
                info['input_channels'] = d[1]
                info['input_height'] = d[2]
                info['input_width'] = d[3]

        def last_dim(shape):
            return shape[-1] if shape else None

        n_out = len(out_shapes)
        if n_out == 2:
            # RF-DETR detection: one tensor ends in 4 (boxes), other is logits.
            boxes = next((s for s in out_shapes if last_dim(s) == 4), None)
            logits = next((s for s in out_shapes if s is not boxes), None)
            info['suggested_type'] = 'object_detection'
            info['detection_arch'] = 'rf_detr'
            if logits is not None and last_dim(logits):
                info['num_classes'] = int(last_dim(logits))
            info['architecture_hints'].append(
                'RF-DETR-style detection: 2 output tensors (boxes + logits), NMS-free.')
        elif n_out >= 3:
            # RF-DETR instance segmentation: boxes + logits + mask tensor(s).
            logits = next((s for s in out_shapes if s and len(s) == 3 and last_dim(s) != 4), None)
            info['suggested_type'] = 'segmentation'
            info['detection_arch'] = 'rf_detr'
            if logits is not None and last_dim(logits):
                info['num_classes'] = int(last_dim(logits))
            info['architecture_hints'].append(
                'RF-DETR-style instance segmentation: 3 output tensors '
                '(boxes + logits + masks).')
        elif n_out == 1:
            s = out_shapes[0]
            if s and len(s) == 3:
                # YOLO detection [1, 4+C, N] or [1, N, 4+C]; anchors is the
                # larger dim, channels the smaller.
                a, b = s[1], s[2]
                if a and b:
                    ch = min(a, b)
                    info['suggested_type'] = 'object_detection'
                    info['detection_arch'] = 'yolo'
                    info['num_classes'] = int(ch - 4)
                    info['architecture_hints'].append(
                        'YOLO-style detection: single output tensor, NMS decode.')
            elif s and len(s) == 2 and s[1]:
                info['suggested_type'] = 'classification'
                info['num_classes'] = int(s[1])
                info['architecture_hints'].append(
                    'Classification: single [batch, num_classes] output.')

        if not info['architecture_hints']:
            info['architecture_hints'].append(
                'ONNX model — could not infer task from output shapes; set the '
                'model type manually.')
        return info
    except Exception as e:  # noqa: BLE001 - inspection must never hard-fail
        logger.error(f"Error parsing ONNX model: {str(e)}")
        return {
            'type': 'onnx',
            'architecture_hints': [
                'ONNX model — shape inspection failed; set attributes manually.'],
        }


def inspect_pytorch_model(model_path: str, trusted_source: bool = False) -> Dict:
    """
    Inspect a PyTorch model file to extract metadata.
    Returns detected information about the model.

    Security (#8): the model file may originate from a user-provided S3 URI, so
    it is loaded with ``weights_only=True`` by default. That restricts loading
    to tensors/primitives, so a malicious ``.pt`` cannot execute arbitrary code
    during load (primary neutralization).

    Some legitimate inputs are NOT pure weights — JIT models, full-model objects,
    and some framework checkpoints — and fail under ``weights_only=True``. For
    those we retry with ``weights_only=False`` ONLY when ``trusted_source`` is
    True (the caller has validated the source S3 bucket/account against the
    config-driven trusted allowlist; see ``is_trusted_model_source``). A
    non-allowlisted source surfaces the error instead of loading an executable
    payload, and the broad ``except`` below degrades to the documented
    "Could not inspect model" contract for callers.
    """
    try:
        import torch

        # Primary neutralization: weights_only=True so a malicious .pt cannot
        # execute code during load.
        try:
            model_data = torch.load(model_path, map_location='cpu', weights_only=True)
        except Exception as weights_only_error:
            # Legitimate full checkpoints / JIT / full-model objects are not pure
            # weights and fail weights_only=True. Retry with weights_only=False
            # ONLY for allowlisted trusted sources; otherwise surface the error
            # (never load an executable payload from a non-allowlisted source).
            if not trusted_source:
                raise
            # ── allowlisted-trusted-source fallback ──────────────────────────
            # The source bucket/account was validated against the config-driven
            # trusted allowlist before this point, so a full-checkpoint load is
            # permitted here to preserve legitimate non-weights models (Req 3.6).
            logger.warning(
                "weights_only=True load failed (%s); retrying with "
                "weights_only=False for ALLOWLISTED trusted source: %s",
                weights_only_error, model_path,
            )
            model_data = torch.load(model_path, map_location='cpu', weights_only=False)  # nosem: source validated against the trusted-bucket/account allowlist before this allowlisted-trusted-source fallback is reached

        info = {
            'type': 'unknown',
            'is_state_dict': False,
            'is_jit': False,
            'is_full_model': False,
            'layers': [],
            'input_channels': None,
            'num_classes': None,
            'architecture_hints': []
        }
        
        # Check if it's a JIT model
        if hasattr(model_data, 'graph'):
            info['is_jit'] = True
            info['type'] = 'jit_model'
            return info
        
        # Check if it's a state dict
        if isinstance(model_data, dict):
            # Could be a state dict or a checkpoint
            if 'model' in model_data:
                # Checkpoint format (common in YOLO, etc.)
                state_dict = model_data.get('model', {})
                if hasattr(state_dict, 'state_dict'):
                    state_dict = state_dict.state_dict()
                info['is_checkpoint'] = True
            elif 'state_dict' in model_data:
                state_dict = model_data['state_dict']
                info['is_checkpoint'] = True
            else:
                # Assume it's a raw state dict
                state_dict = model_data
                info['is_state_dict'] = True
            
            # Analyze layer names
            layer_names = list(state_dict.keys()) if isinstance(state_dict, dict) else []
            info['layers'] = layer_names[:20]  # First 20 layers
            info['total_layers'] = len(layer_names)
            
            # Try to detect architecture from layer names
            layer_str = ' '.join(layer_names).lower()
            
            # Detect common architectures
            if 'yolo' in layer_str or 'detect' in layer_str:
                info['architecture_hints'].append('YOLO-like object detection')
                info['suggested_type'] = 'object_detection'
            elif 'classifier' in layer_str or 'fc' in layer_str:
                info['architecture_hints'].append('Classification network')
                info['suggested_type'] = 'classification'
            elif 'decoder' in layer_str and 'encoder' in layer_str:
                info['architecture_hints'].append('Encoder-Decoder (segmentation)')
                info['suggested_type'] = 'segmentation'
            elif 'resnet' in layer_str:
                info['architecture_hints'].append('ResNet architecture')
                info['suggested_type'] = 'classification'
            elif 'efficientnet' in layer_str:
                info['architecture_hints'].append('EfficientNet architecture')
                info['suggested_type'] = 'classification'
            elif 'vit' in layer_str or 'transformer' in layer_str:
                info['architecture_hints'].append('Vision Transformer')
                info['suggested_type'] = 'classification'
            
            # Try to detect input channels from first conv layer
            for name, param in state_dict.items() if isinstance(state_dict, dict) else []:
                if 'conv' in name.lower() and 'weight' in name.lower():
                    if hasattr(param, 'shape') and len(param.shape) == 4:
                        info['input_channels'] = param.shape[1]
                        break
            
            # Try to detect num_classes from last layer
            for name in reversed(layer_names):
                if 'fc' in name.lower() or 'classifier' in name.lower() or 'head' in name.lower():
                    if 'weight' in name.lower():
                        param = state_dict.get(name)
                        if hasattr(param, 'shape') and len(param.shape) == 2:
                            info['num_classes'] = param.shape[0]
                            break
        
        else:
            # Full model object
            info['is_full_model'] = True
            info['type'] = 'full_model'
        
        return info
        
    except Exception as e:
        logger.error(f"Error inspecting model: {str(e)}")
        return {
            'type': 'unknown',
            'error': str(e),
            'architecture_hints': ['Could not inspect model']
        }


def generate_dda_package(
    model_path: str,
    model_name: str,
    model_type: str,
    image_width: int,
    image_height: int,
    num_classes: Optional[int] = None,
    class_names: Optional[List[str]] = None,
    output_path: str = None,
    export_format: str = 'pytorch',
    score_threshold: float = 0.25,
    iou_threshold: float = 0.45,
    detection_arch: str = 'yolo',
    preserve_aspect: bool = False,
) -> str:
    """
    Generate a DDA-compatible package from a raw model file.
    Creates config.yaml, mochi.json, and manifest.json automatically.

    :param export_format: 'pytorch' (legacy .pt / DLR path) or 'onnx'. For
        'onnx' the package is written for the pluggable ONNX Runtime engine
        (manifest runtime="onnx", artifact model.onnx) and, for detection
        models, the object-detection task path (see
        docs/multi-runtime-inference.md). 'pytorch' preserves the original
        behavior.
    :param score_threshold/iou_threshold: detection decode thresholds (only used
        for object_detection).
    :param detection_arch: object-detection decoder family — 'yolo' (single
        tensor, NMS) or 'rf_detr' (DETR-family, two tensors, NMS-free).
    :param preserve_aspect: letterbox the frame into the network input instead
        of squashing it. MUST match how the model was trained: a detector
        fine-tuned letterboxed (ultralytics' default) and served squashed loses
        ~1.35x mean confidence and up to 5.7x on high-resolution frames, and
        nothing errors — see docs/detection-training-gap.md §7. Written into
        the manifest's top-level ``detection`` block, which
        lfv_model_template.__load_model_graph_config merges into every stage so
        BasicPreProcessor._preserve_aspect finds it. Defaults to False to keep
        existing squash-trained imports byte-identical.
    """
    temp_dir = None
    is_onnx = str(export_format).lower() == 'onnx'
    is_detection = model_type == 'object_detection'
    detection_arch = str(detection_arch or 'yolo').lower()
    # RF-DETR instance-segmentation ONNX -> rendered as a semantic mask through
    # the existing anomaly-localization path (colored mask + overlay + color map).
    is_rf_detr_seg = is_onnx and model_type == 'segmentation' and detection_arch == 'rf_detr'
    # Map the detection architecture to the on-device stage type / decoder.
    detection_stage_type = (
        'rf_detr_object_detection' if detection_arch == 'rf_detr'
        else 'yolo_object_detection'
    )

    try:
        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix="dda_convert_")
        export_dir = os.path.join(temp_dir, "export_artifacts")
        os.makedirs(export_dir, exist_ok=True)
        
        # Determine input shape (assume RGB)
        input_shape = [1, 3, image_height, image_width]
        
        # Determine output shape based on model type
        if model_type == 'classification':
            output_shape = [1, num_classes or 2]
        elif model_type == 'object_detection':
            # YOLO-style output
            output_shape = [1, (num_classes or 80) + 4, 8400]
        elif model_type == 'segmentation':
            output_shape = [1, num_classes or 2, image_height, image_width]
        elif model_type == 'anomaly_detection':
            output_shape = [1, 2]
            num_classes = 2
        else:
            output_shape = [1, num_classes or 2]
        
        # 1. Create config.yaml
        config = {
            'dataset': {
                'image_width': image_width,
                'image_height': image_height
            }
        }
        if num_classes:
            config['dataset']['num_classes'] = num_classes
        if class_names:
            config['dataset']['class_names'] = class_names
        
        with open(os.path.join(temp_dir, 'config.yaml'), 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
        
        # 2. Create mochi.json
        mochi_stage_type = detection_stage_type if (is_onnx and is_detection) else model_type
        mochi = {
            'stages': [
                {
                    'type': mochi_stage_type,
                    'input_shape': input_shape,
                    'output_shape': output_shape
                }
            ],
            'model_info': {
                'name': model_name,
                'version': '1.0.0',
                'framework': 'pytorch',
                'auto_generated': True,
                'generated_at': datetime.utcnow().isoformat()
            }
        }
        if num_classes:
            mochi['stages'][0]['num_classes'] = num_classes
        
        with open(os.path.join(temp_dir, 'mochi.json'), 'w') as f:
            json.dump(mochi, f, indent=2)
        
        # 3. Create manifest.json
        if is_onnx:
            # ONNX package for the pluggable ONNX Runtime engine. For detection
            # models, wire the object-detection task path the device serving
            # code (Phases B/C) reads.
            artifact_filename = "model.onnx"
            # Map the user-facing model_type to the device stage type and graph.
            if is_detection:
                stage_type = detection_stage_type
            elif is_rf_detr_seg:
                stage_type = 'rf_detr_semantic_segmentation'
            else:
                stage_type = model_type
            # Preprocessing differs by architecture:
            #  - YOLO: 0..1 scaling only (image_range_scale), NO ImageNet
            #    mean/std normalization.
            #  - RF-DETR (DETR-family, detection AND segmentation): 0..1 scaling
            #    THEN ImageNet mean/std normalization, i.e. (pixel/255 - mean)/std.
            #    BasicPreProcessor applies the ImageNet MEAN/STD when
            #    normalize=True; omitting it yields garbage output.
            detection_normalize = (is_detection and detection_arch == 'rf_detr') or is_rf_detr_seg
            stage = {
                "type": stage_type,
                "input_shape": input_shape,
                "output_shape": output_shape,
                "image_width": image_width,
                "image_height": image_height,
                "image_range_scale": True,
                "normalize": detection_normalize,
                "threshold": score_threshold,
            }
            if num_classes:
                stage["num_classes"] = num_classes
            manifest = {
                "runtime": "onnx",
                "runtime_artifact": artifact_filename,
                "model_graph": {
                    "model_graph_type": "single_stage_model_graph",
                    "stages": [stage],
                },
                "input_shape": input_shape,
                "preprocessing": {
                    "resize": [image_width, image_height],
                    "channel_order": "RGB",
                },
            }
            if is_rf_detr_seg:
                # Semantic segmentation via the anomaly-localization path: the
                # task stays "anomaly" (default) so the base model emits the
                # colored mask + overlay + per-class color map. pixel_level_classes
                # (index 0 = background) enables localization and provides the
                # class name / color-map labels.
                seg_num_classes = int(num_classes or 91)  # RF-DETR-seg-nano COCO default
                if class_names and len(class_names) >= seg_num_classes:
                    class_labels = list(class_names[:seg_num_classes])
                else:
                    class_labels = [f"class_{i}" for i in range(seg_num_classes)]
                manifest["model_graph"]["pixel_level_classes"] = {
                    "names": ["background"] + class_labels,
                    "normal_ids": [0],
                }
                # Seg decoder config (read from the stage by the post-processor).
                stage["detection"] = {
                    "layout": "rf_detr",
                    "num_classes": seg_num_classes,
                    "score_threshold": score_threshold,
                    "mask_threshold": 0.5,
                    "network_input": image_width,
                }
            elif is_detection:
                manifest["task"] = "object_detection"
                manifest["detection"] = {
                    "layout": detection_arch,  # 'yolo' | 'rf_detr' (decoder family)
                    "num_classes": num_classes or 80,
                    "score_threshold": score_threshold,
                    "network_input": image_width,
                    # Resize geometry, written explicitly (rather than left to
                    # the device default) so the manifest states which of the
                    # two paths the model was trained for instead of silently
                    # inheriting the squash.
                    "preserve_aspect": bool(preserve_aspect),
                }
                # NMS is YOLO-only; DETR-family is set-based (top-k, no NMS).
                if detection_arch == 'rf_detr':
                    manifest["detection"]["top_k"] = 300
                else:
                    manifest["detection"]["iou_threshold"] = iou_threshold
                if class_names:
                    manifest["detection"]["class_names"] = class_names
        else:
            # Legacy PyTorch/DLR package (unchanged behavior).
            pt_filename = f"{model_name}.pt"
            artifact_filename = pt_filename
            manifest = {
                'model_graph': {
                    'stages': [
                        {
                            'type': model_type,
                            'input_shape': input_shape,
                            'output_shape': output_shape
                        }
                    ]
                },
                'input_shape': input_shape,
                'compilable_models': [
                    {
                        'filename': pt_filename,
                        'data_input_config': {
                            'input': input_shape
                        },
                        'framework': 'PYTORCH'
                    }
                ],
                'preprocessing': {
                    'resize': [image_width, image_height],
                    'normalize': {
                        'mean': [0.485, 0.456, 0.406],
                        'std': [0.229, 0.224, 0.225]
                    },
                    'channel_order': 'RGB'
                }
            }
        
        with open(os.path.join(export_dir, 'manifest.json'), 'w') as f:
            json.dump(manifest, f, indent=2)
        
        # 4. Copy the model file (flat in the export dir). This is the imported
        # intermediate package; the final on-device stage-subdir layout is
        # applied later by packaging.package_onnx_component when the deployable
        # Greengrass component ZIP is built (it nests model.onnx under the
        # manifest's stage_type). Keeping it flat here lets that consumer locate
        # the .onnx by a top-level scan.
        shutil.copy(model_path, os.path.join(export_dir, artifact_filename))
        
        # 5. Create tar.gz archive
        if not output_path:
            output_path = os.path.join(temp_dir, f"{model_name}.tar.gz")
        
        with tarfile.open(output_path, 'w:gz') as tar:
            for item in os.listdir(temp_dir):
                item_path = os.path.join(temp_dir, item)
                if item != os.path.basename(output_path):  # Don't include the output file itself
                    tar.add(item_path, arcname=item)
        
        logger.info(f"Generated DDA package: {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"Error generating DDA package: {str(e)}")
        raise
    finally:
        # Don't cleanup if output_path is in temp_dir
        pass


def convert_model(event: Dict, context: Any) -> Dict:
    """
    Convert a raw PyTorch model to DDA-compatible format.
    POST /api/v1/models/convert
    
    Request body:
    {
        "usecase_id": "string",
        "model_s3_uri": "s3://bucket/path/model.pt",  // Raw .pt file
        "model_name": "string",
        "model_type": "classification" | "object_detection" | "segmentation" | "anomaly_detection",
        "image_width": 224,
        "image_height": 224,
        "num_classes": 10,  // optional
        "class_names": ["class1", "class2"],  // optional
        "preserve_aspect": true,  // optional, detection: letterbox instead of squash
        "auto_import": true  // optional, auto-import after conversion
    }
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['usecase_id', 'model_s3_uri', 'model_name', 'model_type', 'image_width', 'image_height']
        error = validate_required_fields(body, required_fields)
        if error:
            return create_response(400, {'error': error})
        
        usecase_id = body['usecase_id']
        model_s3_uri = body['model_s3_uri'].strip()
        model_name = body['model_name'].strip()
        model_type = body['model_type']
        image_width = int(body['image_width'])
        image_height = int(body['image_height'])
        num_classes = body.get('num_classes')
        class_names = body.get('class_names')
        auto_import = body.get('auto_import', False)
        # 'pytorch' (legacy .pt/DLR) or 'onnx' (pluggable ONNX Runtime engine).
        export_format = str(body.get('export_format', 'pytorch')).lower()
        score_threshold = float(body.get('score_threshold', 0.25))
        iou_threshold = float(body.get('iou_threshold', 0.45))
        # Detection decoder family: 'yolo' (default) or 'rf_detr'.
        detection_arch = str(body.get('detection_arch', 'yolo')).lower()
        # Letterbox vs squash. Default False preserves the behavior of every
        # existing caller; the geometry must match how the model was trained
        # (see generate_dda_package).
        preserve_aspect = bool(body.get('preserve_aspect', False))
        
        # Validate model type
        if model_type not in MODEL_TYPES:
            return create_response(400, {
                'error': f"Invalid model_type. Must be one of: {', '.join(MODEL_TYPES.keys())}"
            })
        
        # Validate dimensions
        if image_width <= 0 or image_height <= 0:
            return create_response(400, {'error': 'Image dimensions must be positive integers'})
        
        # Check user access (DataScientist role required)
        if not check_user_access(user_id, usecase_id, 'DataScientist'):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Validate S3 URI format
        if not model_s3_uri.startswith('s3://'):
            return create_response(400, {
                'error': 'Invalid model_s3_uri. Must be an S3 URI (s3://bucket/path/model.pt)'
            })
        
        # Get use case details
        usecase = get_usecase_details(usecase_id)
        
        # Assume cross-account role
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            f"convert-{user_id[:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        )

        # Create S3 client (assumed role for multi-account, Lambda role for single-account)
        s3_client = make_usecase_s3_client(credentials)
        
        # Parse S3 URI
        parsed = urlparse(model_s3_uri)
        source_bucket = parsed.netloc
        source_key = parsed.path.lstrip('/')

        # Restrict the model source to trusted buckets/accounts (#8). A
        # non-allowlisted source is rejected BEFORE download, narrowing the
        # attack surface; only an allowlisted source may later use the
        # weights_only=False full-checkpoint fallback in inspect_pytorch_model.
        trusted_source = is_trusted_model_source(model_s3_uri, usecase)
        if not trusted_source:
            return create_response(400, {
                'error': 'model_s3_uri source bucket is not on the trusted-source allowlist'
            })

        # detector-checkpoint-import (Requirement 4): a .pt/.pth asked for as
        # ONNX is converted by a network-isolated SageMaker job, instead of
        # being byte-copied into a package as model.onnx. The 'pytorch' and
        # ONNX-source paths below are unchanged.
        if export_format == 'onnx' and dconv.is_checkpoint_key(source_key):
            return convert_checkpoint(body, user, usecase, credentials, s3_client,
                                      source_bucket, source_key, model_s3_uri)

        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix="model_convert_")
        
        try:
            # Download the model file. Use an extension matching the source so
            # ONNX packages carry model.onnx and the torch inspector is skipped.
            is_onnx = export_format == 'onnx'
            local_name = 'model.onnx' if is_onnx else 'model.pt'
            local_model = os.path.join(temp_dir, local_name)
            logger.info(f"Downloading model from {model_s3_uri}")
            s3_client.download_file(source_bucket, source_key, local_model)

            safe_model_name = model_name.replace(' ', '_').replace('-', '_').lower()
            # One hex suffix shared by the package key and the checkpoint
            # sidecar key so the two objects are visibly paired in S3.
            hex8 = uuid.uuid4().hex[:8]

            # Inspect the model (PyTorch only; ONNX is opaque to the torch
            # inspector and doesn't need it).
            fine_tunable = None
            if is_onnx:
                model_info = {'type': 'onnx', 'architecture_hints': ['ONNX model']}
            else:
                # Requirement 7: a .pt/.pth Smart Import is packaged verbatim
                # (no ONNX conversion happens here), so the only thing that
                # makes it usable as a base model later is keeping the
                # UNMODIFIED source bytes as a bare sidecar object next to the
                # package. classify_checkpoint is envelope-only and never
                # raises; anything it does not recognise as an ultralytics or
                # RF-DETR checkpoint is simply not fine-tunable.
                probe = classify_checkpoint(local_model)
                if probe.get('fine_tunable'):
                    source_ext = os.path.splitext(source_key)[1].lstrip('.').lower() or 'pt'
                    checkpoint_key = f"converted-models/{safe_model_name}-{hex8}/checkpoint.{source_ext}"
                    checkpoint_s3 = f"s3://{usecase['s3_bucket']}/{checkpoint_key}"
                    logger.info(f"Keeping fine-tunable {probe.get('kind')} checkpoint at {checkpoint_s3}")
                    s3_client.upload_file(local_model, usecase['s3_bucket'], checkpoint_key)
                    fine_tunable = {
                        'arch': probe.get('arch'),
                        'kind': probe.get('kind'),
                        'checkpoint_s3': checkpoint_s3,
                        'class_names': probe.get('class_names'),
                        'num_classes': probe.get('num_classes'),
                    }
                logger.info("Inspecting model...")
                # trusted_source is True here (non-allowlisted sources were
                # rejected above), enabling the full-checkpoint fallback.
                model_info = inspect_pytorch_model(local_model, trusted_source=trusted_source)
            
            # Generate DDA package
            logger.info("Generating DDA-compatible package...")
            output_tar = os.path.join(temp_dir, f"{safe_model_name}.tar.gz")
            
            generate_dda_package(
                model_path=local_model,
                model_name=safe_model_name,
                model_type=model_type,
                image_width=image_width,
                image_height=image_height,
                num_classes=num_classes,
                class_names=class_names,
                output_path=output_tar,
                export_format=export_format,
                score_threshold=score_threshold,
                iou_threshold=iou_threshold,
                detection_arch=detection_arch,
                preserve_aspect=preserve_aspect,
            )
            
            # Upload converted package to S3
            output_key = f"converted-models/{safe_model_name}-{hex8}.tar.gz"
            output_s3_uri = f"s3://{usecase['s3_bucket']}/{output_key}"
            
            logger.info(f"Uploading converted model to {output_s3_uri}")
            s3_client.upload_file(output_tar, usecase['s3_bucket'], output_key)
            
            # Log audit event
            log_audit_event(
                user_id=user_id,
                action='convert_model',
                resource_type='model',
                resource_id=safe_model_name,
                result='success',
                details={
                    'source_uri': model_s3_uri,
                    'output_uri': output_s3_uri,
                    'model_type': model_type,
                    'dimensions': f"{image_width}x{image_height}",
                    # Recorded because a geometry mismatch is silent on device:
                    # this is the audit trail for which path was chosen.
                    'preserve_aspect': preserve_aspect,
                }
            )
            
            result = {
                'converted_model_s3_uri': output_s3_uri,
                'model_name': safe_model_name,
                'model_type': model_type,
                'input_shape': [1, 3, image_height, image_width],
                'model_info': model_info,
                'fine_tunable': fine_tunable,
                'message': 'Model converted successfully'
            }
            
            # Auto-import if requested
            if auto_import:
                # Invoke model import Lambda
                lambda_client = boto3.client('lambda')
                import_function_name = os.environ.get('MODEL_IMPORT_FUNCTION_NAME')
                
                if import_function_name:
                    import_event = {
                        'httpMethod': 'POST',
                        'path': '/api/v1/models/import',
                        'body': json.dumps({
                            'usecase_id': usecase_id,
                            'model_name': model_name,
                            'model_version': '1.0.0',
                            'model_s3_uri': output_s3_uri,
                            'description': f'Auto-converted from {model_s3_uri}',
                            'fine_tunable': fine_tunable,
                        }),
                        'requestContext': {
                            'authorizer': {
                                'claims': {
                                    'sub': user_id,
                                    'email': user['email'],
                                    'cognito:username': user.get('username', user_id)
                                }
                            }
                        }
                    }
                    
                    # Invoke synchronously to get result
                    response = lambda_client.invoke(
                        FunctionName=import_function_name,
                        InvocationType='RequestResponse',
                        Payload=json.dumps(import_event)
                    )
                    
                    import_result = json.loads(response['Payload'].read())
                    if import_result.get('statusCode') == 201:
                        import_body = json.loads(import_result.get('body', '{}'))
                        result['import_result'] = import_body
                        result['training_id'] = import_body.get('training_id')
                        result['message'] = 'Model converted and imported successfully'
                    else:
                        result['import_error'] = 'Auto-import failed'
            
            return create_response(200, result)
            
        finally:
            # Cleanup temp directory
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return create_response(400, {'error': str(e)})
    except ClientError as e:
        logger.error(f"AWS error: {str(e)}")
        return create_response(500, {'error': f"Failed to convert model: {str(e)}"})
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def inspect_model_endpoint(event: Dict, context: Any) -> Dict:
    """
    Inspect a PyTorch model file to detect its architecture.
    POST /api/v1/models/inspect
    
    Request body:
    {
        "usecase_id": "string",
        "model_s3_uri": "s3://bucket/path/model.pt"
    }
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['usecase_id', 'model_s3_uri']
        error = validate_required_fields(body, required_fields)
        if error:
            return create_response(400, {'error': error})
        
        usecase_id = body['usecase_id']
        model_s3_uri = body['model_s3_uri'].strip()
        
        # Check user access
        if not check_user_access(user_id, usecase_id):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Get use case details
        usecase = get_usecase_details(usecase_id)
        
        # Assume cross-account role
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            f"inspect-{user_id[:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        )

        # Create S3 client (assumed role for multi-account, Lambda role for single-account)
        s3_client = make_usecase_s3_client(credentials)
        
        # Parse S3 URI
        parsed = urlparse(model_s3_uri)
        bucket = parsed.netloc
        key = parsed.path.lstrip('/')

        # Restrict the model source to trusted buckets/accounts (#8): reject a
        # non-allowlisted source before download.
        trusted_source = is_trusted_model_source(model_s3_uri, usecase)
        if not trusted_source:
            return create_response(400, {
                'error': 'model_s3_uri source bucket is not on the trusted-source allowlist'
            })

        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix="model_inspect_")

        # ONNX models are opaque to the PyTorch inspector (torch.load). Parse
        # the ONNX graph's input/output shapes to auto-detect attributes
        # (input size, task, detection architecture, num_classes) for UI
        # pre-fill instead of the misleading "Could not inspect model".
        is_onnx = key.lower().endswith('.onnx')
        # .pt / .pth: the Checkpoint_Size_Cap is enforced on HeadObject before
        # any download (detector-checkpoint-import Requirement 2.7).
        is_checkpoint = dconv.is_checkpoint_key(key)
        if is_checkpoint:
            _size, rejection = checkpoint_size_rejection(s3_client, bucket, key)
            if rejection is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)
                return rejection

        try:
            # Download the model file (extension-matched so the torch path only
            # sees real .pt files).
            local_model = os.path.join(temp_dir, 'model.onnx' if is_onnx else 'model.pt')
            logger.info(f"Downloading model from {model_s3_uri}")
            s3_client.download_file(bucket, key, local_model)

            # Inspect the model
            if is_onnx:
                model_info = inspect_onnx_model(local_model)
            elif is_checkpoint:
                # Envelope-only classification + conversion assessment
                # (Requirement 2): never torch, never an unpickle.
                model_info = inspect_checkpoint_file(local_model, usecase)
            else:
                # trusted_source is True here (non-allowlisted sources were
                # rejected above), enabling the full-checkpoint fallback.
                model_info = inspect_pytorch_model(local_model, trusted_source=trusted_source)

            return create_response(200, {
                'model_s3_uri': model_s3_uri,
                'inspection_result': model_info,
                'supported_model_types': MODEL_TYPES
            })
            
        finally:
            # Cleanup
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        
    except Exception as e:
        logger.error(f"Error inspecting model: {str(e)}")
        return create_response(500, {'error': f"Failed to inspect model: {str(e)}"})


def get_supported_types(event: Dict, context: Any) -> Dict:
    """
    Get supported model types for conversion.
    GET /api/v1/models/types
    """
    return create_response(200, {
        'model_types': MODEL_TYPES,
        'common_dimensions': {
            'classification': [224, 256, 299, 384, 512],
            'object_detection': [320, 416, 512, 640, 1280],
            'segmentation': [256, 512, 768, 1024],
            'anomaly_detection': [224, 256, 512]
        },
        'supported_frameworks': ['PYTORCH'],
        'framework_versions': ['1.8', '1.9', '1.10', '1.11', '1.12', '1.13', '2.0']
    })


def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler - routes to appropriate function"""
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
        
        # Route to appropriate handler
        if http_method == 'POST' and '/models/convert' in path:
            return convert_model(event, context)
        elif http_method == 'POST' and '/models/inspect' in path:
            return inspect_model_endpoint(event, context)
        elif http_method == 'POST' and '/models/upload-url' in path:
            return get_model_upload_url(event, context)
        elif http_method == 'GET' and '/models/types' in path:
            return get_supported_types(event, context)
        else:
            return create_response(404, {'error': 'Not found'})
            
    except Exception as e:
        logger.error(f"Handler error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})
