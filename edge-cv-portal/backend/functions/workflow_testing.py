"""
Workflow_Test_Runner API Lambda function (Workflow Manager)

Test dataset upload/list/delete and test run start/status/results
(Requirements 12.2, 12.3, 12.11). Fronts the Workflow_Test_Runner
Step Functions state machine (built with the test-runner infrastructure);
this handler reads the state machine ARN from configuration and returns a
clear error when the runner is not yet configured.

Routes (API Gateway REST):
    GET    /test-datasets                    List Test_Datasets scoped to Use_Case (12.2)
    POST   /test-datasets                    Initiate / finalize a dataset upload  (12.3, 12.11)
    GET    /test-datasets/{id}               Get one Test_Dataset
    DELETE /test-datasets/{id}               Delete a Test_Dataset
    POST   /workflows/{id}/test-runs         Start a test run                      (12.3)
    GET    /workflows/{id}/test-runs         List test runs of a workflow
    GET    /test-runs/{id}                   Test run status + per-node results    (12.3)

Dataset upload flow (design section 10):
    1. POST /test-datasets (initiate) declares the file set; the declared
       total size and file formats are pre-checked, then S3 multipart
       uploads are created with presigned part URLs for the client.
       No DynamoDB record is written at this stage.
    2. POST /test-datasets (action=finalize) completes the multipart
       uploads and performs server-side verification: actual total size
       <= 500 MB and JPEG/PNG content (magic bytes). Only when
       verification passes is the TestDatasets record committed;
       violations reject with the reason and persist nothing - all
       uploaded objects are removed (12.3, 12.11).

Storage layout:
    TestDatasets table (TEST_DATASETS_TABLE)  PK dataset_id, GSI usecase-datasets-index
    TestRuns table     (TEST_RUNS_TABLE)      PK test_run_id, GSI workflow-runs-index
    Objects in portal S3 under
        {WORKFLOWS_S3_PREFIX}/{usecase_id}/test-datasets/{dataset_id}/...
        {WORKFLOWS_S3_PREFIX}/{usecase_id}/test-runs/{test_run_id}/results.json

Error envelope (design): {"error": {"code", "message", "details"}} with
400 parse/validation, 403 RBAC denial, 404 scoped to avoid cross-tenant
existence leaks, 503 when the test runner is not configured.
"""
import json
import os
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from decimal import Decimal
import boto3
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    get_usecase, get_usecase_client, get_usecase_region,
    rbac_manager, Permission
)

# Triton model staging for test runs (same functions/ Lambda asset).
import workflow_model_staging as model_staging

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')
stepfunctions = boto3.client('stepfunctions')

# Environment variables
TEST_DATASETS_TABLE = os.environ.get('TEST_DATASETS_TABLE')
TEST_RUNS_TABLE = os.environ.get('TEST_RUNS_TABLE')
MODELS_TABLE = os.environ.get('MODELS_TABLE')
WORKFLOWS_TABLE = os.environ.get('WORKFLOWS_TABLE')
WORKFLOW_VERSIONS_TABLE = os.environ.get('WORKFLOW_VERSIONS_TABLE')
SETTINGS_TABLE = os.environ.get('SETTINGS_TABLE')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
WORKFLOWS_S3_PREFIX = os.environ.get('WORKFLOWS_S3_PREFIX', 'workflows')
# The Step Functions state machine is provisioned with the test-runner
# infrastructure; until then the ARN may instead be supplied through the
# portal settings table (setting_key below).
TEST_RUN_STATE_MACHINE_ARN = os.environ.get('TEST_RUN_STATE_MACHINE_ARN')
TEST_RUN_STATE_MACHINE_SETTING_KEY = 'workflow-test-runner.state-machine-arn'

# Upload constraints (Requirements 12.3, 12.11)
MAX_DATASET_BYTES = 500 * 1024 * 1024          # 500 MB total
SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
SUPPORTED_CONTENT_TYPES = {'image/jpeg', 'image/png'}
JPEG_MAGIC = b'\xff\xd8\xff'
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
PART_SIZE = 100 * 1024 * 1024                  # multipart part size
PRESIGN_EXPIRY_SECONDS = 3600

# Default simulated inference outcome injected for stubbed model
# inference nodes when the request does not configure one (12.6): the
# model is not executed in the cloud sandbox, so the user chooses the
# outcome per run in the Test panel.
DEFAULT_SIMULATED_INFERENCE = {'is_anomalous': False, 'confidence': 0.9}

# Step Functions execution status -> test run status
SFN_STATUS_MAP = {
    'RUNNING': 'running',
    'SUCCEEDED': 'completed',
    'FAILED': 'failed',
    'TIMED_OUT': 'failed',
    'ABORTED': 'failed',
}


def decimal_to_native(obj):
    """Convert Decimal objects from DynamoDB to native Python types"""
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    elif isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [decimal_to_native(i) for i in obj]
    return obj


def error_response(status_code: int, code: str, message: str, details: Optional[Dict] = None) -> Dict:
    """Build the Workflow Manager error envelope: {error: {code, message, details}}"""
    return create_response(status_code, {
        'error': {
            'code': code,
            'message': message,
            'details': details or {}
        }
    })


def now_ms() -> int:
    return int(datetime.utcnow().timestamp() * 1000)


def dataset_s3_prefix(usecase_id: str, dataset_id: str) -> str:
    """S3 prefix holding all sample-input objects of one Test_Dataset"""
    return f"{WORKFLOWS_S3_PREFIX}/{usecase_id}/test-datasets/{dataset_id}/"


def test_run_results_key(usecase_id: str, test_run_id: str) -> str:
    """S3 key of the per-node results document of a test run"""
    return f"{WORKFLOWS_S3_PREFIX}/{usecase_id}/test-runs/{test_run_id}/results.json"


def has_workflow_permission(user: Dict, usecase_id: str, permission: Permission) -> bool:
    """Check a workflow permission for the acting user on a Use_Case"""
    return rbac_manager.has_permission(user['user_id'], usecase_id, permission, user_info=user)


def forbidden_response(user: Dict, event: Dict, usecase_id: str, permissions: List[Permission]) -> Dict:
    """Uniform 403 authorization error with a denied-access audit entry (11.4)"""
    log_audit_event(
        user_id=user['user_id'],
        action='unauthorized_access',
        resource_type='workflow_test',
        resource_id=event.get('resource', 'unknown'),
        result='denied',
        details={
            'required_permissions': [p.value for p in permissions],
            'usecase_id': usecase_id,
            'method': event.get('httpMethod'),
            'path': event.get('path')
        }
    )
    return error_response(403, 'FORBIDDEN', 'Insufficient permissions', {
        'required_permissions': [p.value for p in permissions],
        'usecase_id': usecase_id
    })


def authorize_usecase_access(user: Dict, event: Dict, usecase_id: str,
                             permission: Permission,
                             not_found: Dict) -> Optional[Dict]:
    """
    Authorize an operation on an existing Use_Case-scoped resource.

    Returns an error response, or None when authorized.

    Cross-tenant handling (design error section): a user without even read
    access to the owning Use_Case receives the same 404 as for a missing
    resource, so existence is never leaked across tenants. A user who can
    read but lacks the operation permission receives a 403.
    """
    if not has_workflow_permission(user, usecase_id, Permission.WORKFLOW_READ):
        return not_found
    if permission != Permission.WORKFLOW_READ and not has_workflow_permission(user, usecase_id, permission):
        return forbidden_response(user, event, usecase_id, [permission])
    return None


def dataset_not_found_response() -> Dict:
    """Uniform 404 that never confirms whether a dataset exists"""
    return error_response(404, 'TEST_DATASET_NOT_FOUND', 'Test dataset not found')


def test_run_not_found_response() -> Dict:
    """Uniform 404 that never confirms whether a test run exists"""
    return error_response(404, 'TEST_RUN_NOT_FOUND', 'Test run not found')


def workflow_not_found_response() -> Dict:
    """Uniform 404 that never confirms whether a workflow exists"""
    return error_response(404, 'WORKFLOW_NOT_FOUND', 'Workflow not found')


def parse_body(event: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Parse the request body; returns (body, None) or (None, error_response)"""
    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return None, error_response(400, 'INVALID_JSON', 'Request body is not valid JSON')
    if not isinstance(body, dict):
        return None, error_response(400, 'INVALID_JSON', 'Request body must be a JSON object')
    return body, None


def get_dataset_item(dataset_id: str) -> Optional[Dict]:
    """Fetch a Test_Dataset record, or None"""
    table = dynamodb.Table(TEST_DATASETS_TABLE)
    response = table.get_item(Key={'dataset_id': dataset_id})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def get_test_run_item(test_run_id: str) -> Optional[Dict]:
    """Fetch a TestRuns record, or None"""
    table = dynamodb.Table(TEST_RUNS_TABLE)
    response = table.get_item(Key={'test_run_id': test_run_id})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def get_workflow_item(workflow_id: str) -> Optional[Dict]:
    """Fetch a workflow metadata item, or None"""
    table = dynamodb.Table(WORKFLOWS_TABLE)
    response = table.get_item(Key={'workflow_id': workflow_id})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def dataset_summary(item: Dict) -> Dict:
    """Public shape of a Test_Dataset record"""
    return {
        'dataset_id': item['dataset_id'],
        'usecase_id': item['usecase_id'],
        'account_id': item.get('account_id'),
        'name': item.get('name'),
        'description': item.get('description', ''),
        's3_prefix': item.get('s3_prefix'),
        'total_bytes': item.get('total_bytes'),
        'file_count': item.get('file_count'),
        'format': item.get('format'),
        'created_at': item.get('created_at'),
        'created_by': item.get('created_by')
    }


def test_run_summary(item: Dict) -> Dict:
    """Public shape of a TestRuns record"""
    return {
        'test_run_id': item['test_run_id'],
        'workflow_id': item.get('workflow_id'),
        'version': item.get('version'),
        'usecase_id': item.get('usecase_id'),
        'dataset_id': item.get('dataset_id'),
        'status': item.get('status'),
        'progress': item.get('progress'),
        'failure': item.get('failure'),
        'started_at': item.get('started_at'),
        'finished_at': item.get('finished_at'),
        'created_by': item.get('created_by')
    }


def validate_declared_files(files: Any) -> Optional[Dict]:
    """
    Pre-check the declared file set of an upload initiation (12.11).

    Returns an error response on violation, or None. Rejections identify
    the reason; nothing has been uploaded or persisted at this point.
    """
    if not isinstance(files, list) or not files:
        return error_response(400, 'MISSING_FIELDS',
                              'files must be a non-empty list of {name, size} entries')
    total = 0
    for index, entry in enumerate(files):
        if not isinstance(entry, dict) or not entry.get('name'):
            return error_response(400, 'INVALID_FILE_ENTRY',
                                  f'files[{index}] must be an object with a name')
        name = str(entry['name'])
        if '/' in name or '\\' in name or name in ('.', '..'):
            return error_response(400, 'INVALID_FILE_ENTRY',
                                  f'files[{index}].name must be a plain file name',
                                  {'name': name})
        extension = os.path.splitext(name)[1].lower()
        if extension not in SUPPORTED_EXTENSIONS:
            return error_response(
                400, 'UNSUPPORTED_FORMAT',
                f"Unsupported file format '{extension or name}': only JPEG and PNG "
                f"images are supported",
                {'file': name, 'supported_extensions': sorted(SUPPORTED_EXTENSIONS)}
            )
        content_type = entry.get('content_type')
        if content_type and content_type not in SUPPORTED_CONTENT_TYPES:
            return error_response(
                400, 'UNSUPPORTED_FORMAT',
                f"Unsupported content type '{content_type}': only JPEG and PNG "
                f"images are supported",
                {'file': name, 'supported_content_types': sorted(SUPPORTED_CONTENT_TYPES)}
            )
        size = entry.get('size')
        if not isinstance(size, (int, float)) or isinstance(size, bool) or size <= 0:
            return error_response(400, 'INVALID_FILE_ENTRY',
                                  f'files[{index}].size must be a positive number of bytes',
                                  {'file': name})
        total += int(size)
    if total > MAX_DATASET_BYTES:
        return error_response(
            400, 'DATASET_TOO_LARGE',
            f'Total upload size {total} bytes exceeds the {MAX_DATASET_BYTES} byte '
            f'(500 MB) limit',
            {'total_bytes': total, 'max_bytes': MAX_DATASET_BYTES}
        )
    return None


def object_content_format(key: str) -> Optional[str]:
    """
    Identify an uploaded object as 'jpeg' or 'png' by its magic bytes,
    or None when it is neither (server-side format verification, 12.11).
    """
    try:
        response = s3.get_object(
            Bucket=PORTAL_ARTIFACTS_BUCKET, Key=key,
            Range=f'bytes=0-{len(PNG_MAGIC) - 1}'
        )
        head = response['Body'].read()
    except ClientError as e:
        logger.error(f"Error reading object head for {key}: {str(e)}")
        return None
    if head.startswith(JPEG_MAGIC):
        return 'jpeg'
    if head.startswith(PNG_MAGIC):
        return 'png'
    return None


def list_dataset_objects(prefix: str) -> List[Dict]:
    """List all objects under a dataset staging prefix"""
    objects: List[Dict] = []
    continuation_token = None
    while True:
        kwargs = {'Bucket': PORTAL_ARTIFACTS_BUCKET, 'Prefix': prefix}
        if continuation_token:
            kwargs['ContinuationToken'] = continuation_token
        listed = s3.list_objects_v2(**kwargs)
        objects.extend(listed.get('Contents', []))
        if not listed.get('IsTruncated'):
            break
        continuation_token = listed.get('NextContinuationToken')
    return objects


def delete_prefix_objects(prefix: str) -> int:
    """Delete every object under a prefix; returns the count removed"""
    deleted = 0
    while True:
        listed = s3.list_objects_v2(Bucket=PORTAL_ARTIFACTS_BUCKET, Prefix=prefix)
        objects = [{'Key': o['Key']} for o in listed.get('Contents', [])]
        if objects:
            s3.delete_objects(Bucket=PORTAL_ARTIFACTS_BUCKET, Delete={'Objects': objects})
            deleted += len(objects)
        if not listed.get('IsTruncated'):
            break
    return deleted


def abort_pending_multipart_uploads(prefix: str) -> None:
    """Abort any in-progress multipart uploads under a prefix (cleanup)"""
    try:
        listed = s3.list_multipart_uploads(Bucket=PORTAL_ARTIFACTS_BUCKET, Prefix=prefix)
        for upload in listed.get('Uploads', []) or []:
            try:
                s3.abort_multipart_upload(
                    Bucket=PORTAL_ARTIFACTS_BUCKET,
                    Key=upload['Key'],
                    UploadId=upload['UploadId']
                )
            except ClientError as e:
                logger.warning(f"Could not abort multipart upload {upload.get('Key')}: {str(e)}")
    except ClientError as e:
        logger.warning(f"Could not list multipart uploads under {prefix}: {str(e)}")


def cleanup_rejected_upload(prefix: str) -> None:
    """Remove everything an aborted/rejected upload left behind (12.11)"""
    abort_pending_multipart_uploads(prefix)
    delete_prefix_objects(prefix)


def get_test_run_state_machine_arn() -> Optional[str]:
    """
    Resolve the Workflow_Test_Runner state machine ARN.

    Prefers the TEST_RUN_STATE_MACHINE_ARN environment variable (set once
    the test-runner CDK infrastructure exists); falls back to runtime
    configuration in the portal settings table.
    """
    if TEST_RUN_STATE_MACHINE_ARN:
        return TEST_RUN_STATE_MACHINE_ARN
    if not SETTINGS_TABLE:
        return None
    try:
        response = dynamodb.Table(SETTINGS_TABLE).get_item(
            Key={'setting_key': TEST_RUN_STATE_MACHINE_SETTING_KEY}
        )
        item = response.get('Item')
        if item and item.get('value'):
            return str(item['value'])
    except ClientError as e:
        logger.error(f"Error reading test runner setting: {str(e)}")
    return None


def test_runner_unconfigured_response() -> Dict:
    """503 returned while the test-runner state machine is not yet provisioned"""
    return error_response(
        503, 'TEST_RUNNER_NOT_CONFIGURED',
        'The workflow test runner is not configured: no Step Functions state '
        'machine ARN is available. Deploy the test-runner infrastructure or set '
        f"the '{TEST_RUN_STATE_MACHINE_SETTING_KEY}' portal setting."
    )


# ---------------------------------------------------------------------------
# Test dataset endpoints (12.2, 12.3, 12.11)
# ---------------------------------------------------------------------------

def initiate_dataset_upload(event: Dict, user: Dict, body: Dict) -> Dict:
    """
    POST /test-datasets  (default action: initiate)
    Body: {usecase_id, name, files: [{name, size, content_type?}], description?}

    Pre-checks the declared file set (formats, 500 MB total), then creates
    S3 multipart uploads with presigned part URLs. No dataset record is
    written until the finalize step verifies the uploaded content (12.3).
    """
    usecase_id = body.get('usecase_id')
    name = body.get('name')
    files = body.get('files')

    missing = [f for f in ('usecase_id', 'name', 'files') if not body.get(f)]
    if missing:
        return error_response(400, 'MISSING_FIELDS',
                              f"Missing required fields: {', '.join(missing)}")

    if not has_workflow_permission(user, usecase_id, Permission.WORKFLOW_TEST):
        return forbidden_response(user, event, usecase_id, [Permission.WORKFLOW_TEST])

    try:
        get_usecase(usecase_id)
    except ValueError:
        return error_response(404, 'USECASE_NOT_FOUND', 'Use case not found')

    err = validate_declared_files(files)
    if err:
        return err

    dataset_id = str(uuid.uuid4())
    prefix = dataset_s3_prefix(usecase_id, dataset_id)

    upload_files: List[Dict] = []
    try:
        for entry in files:
            file_name = str(entry['name'])
            size = int(entry['size'])
            extension = os.path.splitext(file_name)[1].lower()
            content_type = entry.get('content_type') or (
                'image/png' if extension == '.png' else 'image/jpeg'
            )
            key = f"{prefix}{file_name}"
            multipart = s3.create_multipart_upload(
                Bucket=PORTAL_ARTIFACTS_BUCKET, Key=key, ContentType=content_type
            )
            upload_id = multipart['UploadId']
            part_count = max(1, (size + PART_SIZE - 1) // PART_SIZE)
            parts = [
                {
                    'part_number': part_number,
                    'url': s3.generate_presigned_url(
                        'upload_part',
                        Params={
                            'Bucket': PORTAL_ARTIFACTS_BUCKET,
                            'Key': key,
                            'UploadId': upload_id,
                            'PartNumber': part_number
                        },
                        ExpiresIn=PRESIGN_EXPIRY_SECONDS
                    )
                }
                for part_number in range(1, part_count + 1)
            ]
            upload_files.append({
                'name': file_name,
                'key': key,
                'upload_id': upload_id,
                'part_size': PART_SIZE,
                'parts': parts
            })
    except ClientError as e:
        logger.error(f"Error creating multipart uploads for dataset {dataset_id}: {str(e)}")
        cleanup_rejected_upload(prefix)
        return error_response(500, 'UPLOAD_INIT_FAILED',
                              'Could not initiate the dataset upload')

    return create_response(201, {
        'dataset_id': dataset_id,
        'usecase_id': usecase_id,
        'name': name,
        's3_prefix': prefix,
        'upload': {
            'files': upload_files,
            'expires_in': PRESIGN_EXPIRY_SECONDS
        },
        'message': 'Upload the parts, then finalize with action=finalize. '
                   'The dataset is committed only after server-side verification.'
    })


def finalize_dataset_upload(event: Dict, user: Dict, body: Dict) -> Dict:
    """
    POST /test-datasets  (action: finalize)
    Body: {usecase_id, dataset_id, name, description?,
           files: [{key, upload_id, parts: [{part_number, etag}]}]}

    Completes the multipart uploads and performs server-side verification
    (total size <= 500 MB, JPEG/PNG magic bytes) before committing the
    TestDatasets record. Violations reject with the reason and persist
    nothing - uploaded objects are removed (12.3, 12.11).
    """
    usecase_id = body.get('usecase_id')
    dataset_id = body.get('dataset_id')
    name = body.get('name')
    files = body.get('files')

    missing = [f for f in ('usecase_id', 'dataset_id', 'name', 'files') if not body.get(f)]
    if missing:
        return error_response(400, 'MISSING_FIELDS',
                              f"Missing required fields: {', '.join(missing)}")

    if not has_workflow_permission(user, usecase_id, Permission.WORKFLOW_TEST):
        return forbidden_response(user, event, usecase_id, [Permission.WORKFLOW_TEST])

    try:
        usecase = get_usecase(usecase_id)
    except ValueError:
        return error_response(404, 'USECASE_NOT_FOUND', 'Use case not found')

    if get_dataset_item(dataset_id):
        return error_response(409, 'DATASET_ALREADY_EXISTS',
                              'This dataset has already been finalized')

    prefix = dataset_s3_prefix(usecase_id, dataset_id)
    if not isinstance(files, list) or not files:
        return error_response(400, 'MISSING_FIELDS',
                              'files must be a non-empty list of completed uploads')

    # Complete the multipart uploads. Keys outside the dataset prefix are
    # rejected outright - a client cannot commit foreign objects.
    for index, entry in enumerate(files):
        if not isinstance(entry, dict) or not entry.get('key') or not entry.get('upload_id'):
            return error_response(400, 'INVALID_FILE_ENTRY',
                                  f'files[{index}] must carry key, upload_id and parts')
        if not str(entry['key']).startswith(prefix):
            return error_response(400, 'INVALID_FILE_ENTRY',
                                  f'files[{index}].key does not belong to this dataset',
                                  {'key': entry['key']})
        parts = entry.get('parts')
        if not isinstance(parts, list) or not parts:
            return error_response(400, 'INVALID_FILE_ENTRY',
                                  f'files[{index}].parts must be a non-empty list')
        try:
            s3.complete_multipart_upload(
                Bucket=PORTAL_ARTIFACTS_BUCKET,
                Key=entry['key'],
                UploadId=entry['upload_id'],
                MultipartUpload={
                    'Parts': sorted(
                        (
                            {'PartNumber': int(p['part_number']), 'ETag': str(p['etag'])}
                            for p in parts
                        ),
                        key=lambda p: p['PartNumber']
                    )
                }
            )
        except (ClientError, KeyError, TypeError, ValueError) as e:
            logger.error(f"Error completing upload for {entry.get('key')}: {str(e)}")
            cleanup_rejected_upload(prefix)
            return error_response(400, 'UPLOAD_INCOMPLETE',
                                  'A file upload could not be completed; the upload '
                                  'was discarded and no dataset was persisted',
                                  {'key': entry.get('key')})

    # Server-side verification against what actually landed in S3 (12.11)
    objects = list_dataset_objects(prefix)
    if not objects:
        return error_response(400, 'UPLOAD_INCOMPLETE',
                              'No uploaded files were found; no dataset was persisted')

    total_bytes = sum(int(o.get('Size', 0)) for o in objects)
    if total_bytes > MAX_DATASET_BYTES:
        cleanup_rejected_upload(prefix)
        return error_response(
            400, 'DATASET_TOO_LARGE',
            f'Uploaded total size {total_bytes} bytes exceeds the '
            f'{MAX_DATASET_BYTES} byte (500 MB) limit; the upload was discarded '
            f'and no dataset was persisted',
            {'total_bytes': total_bytes, 'max_bytes': MAX_DATASET_BYTES}
        )

    formats = set()
    for obj in objects:
        detected = object_content_format(obj['Key'])
        if detected is None:
            cleanup_rejected_upload(prefix)
            file_name = obj['Key'][len(prefix):]
            return error_response(
                400, 'UNSUPPORTED_FORMAT',
                f"File '{file_name}' is not a JPEG or PNG image; the upload was "
                f"discarded and no dataset was persisted",
                {'file': file_name}
            )
        formats.add(detected)

    timestamp = now_ms()
    item = {
        'dataset_id': dataset_id,
        'usecase_id': usecase_id,
        'account_id': usecase.get('account_id'),
        'name': name,
        'description': body.get('description', ''),
        's3_prefix': prefix,
        'total_bytes': total_bytes,
        'file_count': len(objects),
        'format': '+'.join(sorted(formats)),
        'created_at': timestamp,
        'created_by': user['user_id']
    }
    dynamodb.Table(TEST_DATASETS_TABLE).put_item(
        Item=item,
        ConditionExpression='attribute_not_exists(dataset_id)'
    )

    log_audit_event(
        user_id=user['user_id'],
        action='create_test_dataset',
        resource_type='test_dataset',
        resource_id=dataset_id,
        result='success',
        details={'usecase_id': usecase_id, 'name': name,
                 'total_bytes': total_bytes, 'file_count': len(objects)}
    )

    return create_response(201, {'dataset': dataset_summary(item)})


def create_test_dataset(event: Dict, user: Dict) -> Dict:
    """POST /test-datasets - dispatch initiate vs finalize on body.action"""
    body, err = parse_body(event)
    if err:
        return err
    action = body.get('action', 'initiate')
    if action == 'initiate':
        return initiate_dataset_upload(event, user, body)
    if action == 'finalize':
        return finalize_dataset_upload(event, user, body)
    return error_response(400, 'INVALID_ACTION',
                          "action must be 'initiate' or 'finalize'")


def list_test_datasets(event: Dict, user: Dict) -> Dict:
    """
    GET /test-datasets[?usecase_id=...]
    Test_Datasets scoped to Use_Cases the user is authorized to access (12.2).
    """
    params = event.get('queryStringParameters') or {}
    usecase_id = params.get('usecase_id')

    if usecase_id:
        if not has_workflow_permission(user, usecase_id, Permission.WORKFLOW_READ):
            return forbidden_response(user, event, usecase_id, [Permission.WORKFLOW_READ])
        usecase_ids = [usecase_id]
    else:
        usecase_ids = [
            uc for uc in rbac_manager.get_accessible_usecases(user['user_id'], user_info=user)
            if has_workflow_permission(user, uc, Permission.WORKFLOW_READ)
        ]

    table = dynamodb.Table(TEST_DATASETS_TABLE)
    datasets: List[Dict] = []
    for uc in usecase_ids:
        kwargs = {
            'IndexName': 'usecase-datasets-index',
            'KeyConditionExpression': 'usecase_id = :uid',
            'ExpressionAttributeValues': {':uid': uc}
        }
        while True:
            response = table.query(**kwargs)
            datasets.extend(dataset_summary(decimal_to_native(i))
                            for i in response.get('Items', []))
            last_key = response.get('LastEvaluatedKey')
            if not last_key:
                break
            kwargs['ExclusiveStartKey'] = last_key

    datasets.sort(key=lambda d: d.get('created_at') or 0, reverse=True)
    return create_response(200, {'datasets': datasets, 'count': len(datasets)})


def get_test_dataset(event: Dict, user: Dict, dataset_id: str) -> Dict:
    """GET /test-datasets/{id}"""
    item = get_dataset_item(dataset_id)
    if not item:
        return dataset_not_found_response()
    err = authorize_usecase_access(user, event, item['usecase_id'],
                                   Permission.WORKFLOW_READ,
                                   dataset_not_found_response())
    if err:
        return err
    return create_response(200, {'dataset': dataset_summary(item)})


def delete_test_dataset(event: Dict, user: Dict, dataset_id: str) -> Dict:
    """DELETE /test-datasets/{id} - removes the record and the S3 objects"""
    item = get_dataset_item(dataset_id)
    if not item:
        return dataset_not_found_response()
    err = authorize_usecase_access(user, event, item['usecase_id'],
                                   Permission.WORKFLOW_TEST,
                                   dataset_not_found_response())
    if err:
        return err

    prefix = item.get('s3_prefix') or dataset_s3_prefix(item['usecase_id'], dataset_id)
    delete_prefix_objects(prefix)
    dynamodb.Table(TEST_DATASETS_TABLE).delete_item(Key={'dataset_id': dataset_id})

    log_audit_event(
        user_id=user['user_id'],
        action='delete_test_dataset',
        resource_type='test_dataset',
        resource_id=dataset_id,
        result='success',
        details={'usecase_id': item['usecase_id'], 'name': item.get('name')}
    )

    return create_response(200, {
        'dataset_id': dataset_id,
        'message': 'Test dataset deleted successfully'
    })


# ---------------------------------------------------------------------------
# Test run endpoints (12.3)
# ---------------------------------------------------------------------------

def validate_simulated_inference(value: Any) -> Tuple[Optional[Dict], Optional[Dict]]:
    """
    Validate the optional ``simulated_inference`` request field:
    {is_anomalous: bool, confidence: number 0..1}, both fields optional
    with the DEFAULT_SIMULATED_INFERENCE values. Returns
    (normalized_dict, None) or (None, 400 error_response) on a bad shape.
    """
    if value is None:
        return dict(DEFAULT_SIMULATED_INFERENCE), None
    if not isinstance(value, dict):
        return None, error_response(
            400, 'INVALID_SIMULATED_INFERENCE',
            'simulated_inference must be an object with is_anomalous '
            '(boolean) and confidence (number between 0 and 1)')
    unknown = sorted(set(value) - {'is_anomalous', 'confidence'})
    if unknown:
        return None, error_response(
            400, 'INVALID_SIMULATED_INFERENCE',
            f"simulated_inference has unknown fields: {', '.join(unknown)}",
            {'unknown_fields': unknown})
    is_anomalous = value.get('is_anomalous',
                             DEFAULT_SIMULATED_INFERENCE['is_anomalous'])
    if not isinstance(is_anomalous, bool):
        return None, error_response(
            400, 'INVALID_SIMULATED_INFERENCE',
            'simulated_inference.is_anomalous must be a boolean')
    confidence = value.get('confidence',
                           DEFAULT_SIMULATED_INFERENCE['confidence'])
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float, Decimal)):
        return None, error_response(
            400, 'INVALID_SIMULATED_INFERENCE',
            'simulated_inference.confidence must be a number between 0 and 1')
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        return None, error_response(
            400, 'INVALID_SIMULATED_INFERENCE',
            'simulated_inference.confidence must be between 0 and 1',
            {'confidence': confidence})
    return {'is_anomalous': is_anomalous, 'confidence': confidence}, None


# ---------------------------------------------------------------------------
# Triton model staging for test runs (see workflow_model_staging.py)
# ---------------------------------------------------------------------------

def load_stored_definition(definition_s3_key: Optional[str]) -> Optional[Dict]:
    """The stored Workflow_Definition JSON document, or None when it
    cannot be read (the validate step would fail the run anyway)."""
    if not definition_s3_key:
        return None
    try:
        response = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET,
                                 Key=definition_s3_key)
        return json.loads(response['Body'].read().decode('utf-8'))
    except (ClientError, ValueError, UnicodeDecodeError) as e:
        logger.error(f"Could not read definition {definition_s3_key}: {str(e)}")
        return None


def query_usecase_model_items(usecase_id: str) -> List[Dict]:
    """Registry items of one Use_Case from the models table (the
    component_arns source for CPU variant selection)."""
    if not MODELS_TABLE:
        return []
    table = dynamodb.Table(MODELS_TABLE)
    items: List[Dict] = []
    kwargs = {
        'IndexName': 'usecase-models-index',
        'KeyConditionExpression': 'usecase_id = :uid',
        'ExpressionAttributeValues': {':uid': usecase_id},
    }
    while True:
        response = table.query(**kwargs)
        items.extend(decimal_to_native(i) for i in response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return items


def model_staging_clients(usecase: Dict) -> Tuple[Any, Any]:
    """(greengrass, source-S3) clients for reading the Use_Case's model
    components and artifact bucket. Follows the shared_utils per-usecase
    client pattern (assume-role for cross-account Use_Cases, the Lambda
    role directly otherwise)."""
    region = get_usecase_region(usecase)
    greengrass = get_usecase_client('greengrassv2', usecase, region=region)
    source_s3 = get_usecase_client('s3', usecase, region=region)
    return greengrass, source_s3


def append_run_progress(test_run_id: str, message: str) -> None:
    """Append one {at, message} progress entry to the TestRuns item (the
    same additive list workflow_test_steps.append_run_progress feeds);
    best-effort only."""
    try:
        dynamodb.Table(TEST_RUNS_TABLE).update_item(
            Key={'test_run_id': test_run_id},
            UpdateExpression='SET progress = '
                             'list_append(if_not_exists(progress, :empty), :entries)',
            ExpressionAttributeValues={
                ':empty': [],
                ':entries': [{'at': now_ms(), 'message': message}],
            },
        )
    except ClientError as e:
        logger.warning(f"Could not record progress for run {test_run_id}: {str(e)}")


def staging_fallbacks_from_errors(error_records: List[Dict]) -> List[Dict]:
    """``[{nodeId, modelName, reason}]`` from per-node staging error records.

    Model staging is best-effort (12.16, 12.17): a model that cannot be
    staged no longer fails the run. Its node is omitted from
    ``STAGED_MODELS`` and surfaced here instead; the sandbox harness runs
    the node with the injected simulated inference outcome and reports
    the fallback reason in the node's results.
    """
    return [{
        'nodeId': record.get('nodeId'),
        'modelName': record.get('modelName'),
        'reason': ((record.get('error') or {}).get('message')
                   or 'Model staging failed'),
    } for record in error_records]


def stage_test_run_models(run_item: Dict, definition: Optional[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Stage the Triton model artifacts of every model_inference node.

    Resolves each node's modelName against the Use_Case's model
    registry, prefers CPU-runnable component variants (-x86-64-cpu then
    -onnx), copies each component's S3 artifact zip into the portal
    artifacts bucket under the run's prefix, and returns the staging
    manifest plus any per-node error records (see
    workflow_model_staging.stage_models_for_run).
    """
    nodes = model_staging.model_inference_nodes(definition)
    if not nodes:
        return [], []

    usecase_id = run_item['usecase_id']
    append_run_progress(
        run_item['test_run_id'],
        'Staging {0} model(s) for cloud inference'.format(len(nodes)))
    try:
        usecase = get_usecase(usecase_id)
        greengrass, source_s3 = model_staging_clients(usecase)
    except Exception as e:  # per-usecase client setup (assume-role) failed
        logger.error(f"Model staging clients unavailable for usecase "
                     f"{usecase_id}: {str(e)}")
        return [], [{
            'nodeId': node.get('nodeId'),
            'modelName': node.get('modelName'),
            'status': 'error',
            'outputs': [],
            'stubActivity': [],
            'error': {
                'code': model_staging.CODE_MODEL_STAGING_FAILED,
                'message': 'Model {0} could not be staged: the use-case '
                           'model registry is not reachable'.format(
                               node.get('modelName') or '(unselected)'),
            },
        } for node in nodes]

    return model_staging.stage_models_for_run(
        nodes,
        query_usecase_model_items(usecase_id),
        greengrass,
        source_s3,
        s3,
        PORTAL_ARTIFACTS_BUCKET,
        run_item['results_s3_key'],
    )


def start_test_run(event: Dict, user: Dict, workflow_id: str) -> Dict:
    """
    POST /workflows/{id}/test-runs
    Body: {dataset_id, version?, simulated_inference?}

    ``simulated_inference`` ({is_anomalous, confidence}) configures the
    outcome injected for simulation-stubbed model inference nodes — the
    model itself is never executed in the cloud sandbox (12.6). It is
    forwarded to the sandbox container through the state machine input
    (pre-serialized as simulated_inference_json for the env override).

    Creates the TestRuns record and starts the Workflow_Test_Runner Step
    Functions execution (Validate -> Compile -> RunSandbox -> CollectResults,
    task 11.2). Requires workflow:test on the owning Use_Case.
    """
    workflow = get_workflow_item(workflow_id)
    if not workflow:
        return workflow_not_found_response()
    usecase_id = workflow['usecase_id']
    err = authorize_usecase_access(user, event, usecase_id,
                                   Permission.WORKFLOW_TEST,
                                   workflow_not_found_response())
    if err:
        return err

    body, err = parse_body(event)
    if err:
        return err
    dataset_id = body.get('dataset_id')
    if not dataset_id:
        return error_response(400, 'MISSING_FIELDS',
                              'Missing required fields: dataset_id')

    simulated_inference, err = validate_simulated_inference(
        body.get('simulated_inference'))
    if err:
        return err

    # The Test_Dataset must exist in the same Use_Case; a dataset of another
    # tenant is indistinguishable from a missing one (no existence leak).
    dataset = get_dataset_item(dataset_id)
    if not dataset or dataset.get('usecase_id') != usecase_id:
        return dataset_not_found_response()

    version_param = body.get('version')
    if version_param is not None:
        try:
            version = int(version_param)
        except (TypeError, ValueError):
            return error_response(400, 'INVALID_VERSION', 'version must be an integer')
    else:
        version = int(workflow.get('latest_version', 1))

    versions_table = dynamodb.Table(WORKFLOW_VERSIONS_TABLE)
    response = versions_table.get_item(Key={'workflow_id': workflow_id, 'version': version})
    version_item = response.get('Item')
    if not version_item:
        return error_response(404, 'VERSION_NOT_FOUND',
                              f'Version {version} not found for workflow')
    version_item = decimal_to_native(version_item)

    state_machine_arn = get_test_run_state_machine_arn()
    if not state_machine_arn:
        return test_runner_unconfigured_response()

    test_run_id = str(uuid.uuid4())
    timestamp = now_ms()
    results_key = test_run_results_key(usecase_id, test_run_id)

    run_item = {
        'test_run_id': test_run_id,
        'workflow_id': workflow_id,
        'version': version,
        'usecase_id': usecase_id,
        'dataset_id': dataset_id,
        'status': 'pending',
        'started_at': timestamp,
        'finished_at': None,
        'results_s3_key': results_key,
        'failure': None,
        'created_by': user['user_id']
    }
    runs_table = dynamodb.Table(TEST_RUNS_TABLE)
    runs_table.put_item(Item=run_item,
                        ConditionExpression='attribute_not_exists(test_run_id)')

    # Triton model staging: every model_inference node's model artifact
    # is copied into the portal artifacts bucket before the execution
    # starts, so the sandbox harness can populate the (initially empty)
    # Triton model repository and actually run inference on CPU. Staging
    # is best-effort (12.16, 12.17): a model without a CPU-compatible
    # variant (or one that cannot be staged) no longer fails the run —
    # its node is omitted from STAGED_MODELS and reported in
    # STAGING_FALLBACKS, and the sandbox runs it with the injected
    # simulated inference outcome.
    definition = load_stored_definition(version_item.get('s3_definition_key'))
    staged_models, staging_errors = stage_test_run_models(run_item, definition)
    staging_fallbacks = staging_fallbacks_from_errors(staging_errors)
    for fallback in staging_fallbacks:
        append_run_progress(
            test_run_id,
            'Model staging fallback for node {0}: {1}'.format(
                fallback.get('nodeId'), fallback.get('reason')))

    # State machine input per design section 10: the sandbox compiles for
    # x86_64 with simulation=true and feeds sources from the Test_Dataset.
    execution_input = {
        'test_run_id': test_run_id,
        'workflow_id': workflow_id,
        'workflow_version': version,
        'usecase_id': usecase_id,
        'dataset_id': dataset_id,
        'dataset_s3_prefix': dataset.get('s3_prefix'),
        'definition_s3_key': version_item.get('s3_definition_key'),
        'results_s3_key': results_key,
        'artifacts_bucket': PORTAL_ARTIFACTS_BUCKET,
        'target_arch': 'x86_64',
        'simulation': True,
        # Custom_Node_Type versions pinned at workflow save ({typeId:
        # typeVersion} from the WorkflowVersions item, custom-node-designer
        # 14.2): the validate/compile steps resolve the merged catalog
        # against exactly these versions (12.1).
        'custom_node_type_pins': version_item.get('custom_node_types') or {},
        # Simulated inference outcome for stubbed model inference nodes
        # (12.6): the object for readability, plus the pre-serialized
        # JSON string the RunSandbox containerOverrides pass through as
        # the SIMULATED_INFERENCE env value.
        'simulated_inference': simulated_inference,
        'simulated_inference_json': json.dumps(simulated_inference),
        # Model staging manifest: the models copied under the run's
        # prefix for the sandbox to unpack into the Triton model
        # repository. The list for readability, plus the pre-serialized
        # JSON string the RunSandbox containerOverrides pass through as
        # the STAGED_MODELS env value.
        'staged_models': staged_models,
        'staged_models_json': json.dumps(staged_models),
        # Best-effort staging fallbacks [{nodeId, modelName, reason}]:
        # the nodes whose models could not be staged (12.16, 12.17). The
        # list for readability, plus the pre-serialized JSON string the
        # RunSandbox containerOverrides pass through as the
        # STAGING_FALLBACKS env value.
        'staging_fallbacks': staging_fallbacks,
        'staging_fallbacks_json': json.dumps(staging_fallbacks)
    }
    try:
        execution = stepfunctions.start_execution(
            stateMachineArn=state_machine_arn,
            name=test_run_id,
            input=json.dumps(execution_input)
        )
    except ClientError as e:
        logger.error(f"Error starting test run execution {test_run_id}: {str(e)}")
        runs_table.update_item(
            Key={'test_run_id': test_run_id},
            UpdateExpression='SET #s = :status, finished_at = :finished, failure = :failure',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':status': 'failed',
                ':finished': now_ms(),
                ':failure': {'nodeId': None, 'message': 'Test run could not be started',
                             'timeout': False}
            }
        )
        return error_response(502, 'TEST_RUN_START_FAILED',
                              'The test run execution could not be started')

    runs_table.update_item(
        Key={'test_run_id': test_run_id},
        UpdateExpression='SET #s = :status, execution_arn = :arn',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':status': 'running',
                                   ':arn': execution['executionArn']}
    )
    run_item['status'] = 'running'
    run_item['execution_arn'] = execution['executionArn']

    log_audit_event(
        user_id=user['user_id'],
        action='start_test_run',
        resource_type='test_run',
        resource_id=test_run_id,
        result='success',
        details={'usecase_id': usecase_id, 'workflow_id': workflow_id,
                 'version': version, 'dataset_id': dataset_id}
    )

    return create_response(202, {'test_run': test_run_summary(run_item)})


def list_test_runs(event: Dict, user: Dict, workflow_id: str) -> Dict:
    """GET /workflows/{id}/test-runs - runs of one workflow, newest first"""
    workflow = get_workflow_item(workflow_id)
    if not workflow:
        return workflow_not_found_response()
    err = authorize_usecase_access(user, event, workflow['usecase_id'],
                                   Permission.WORKFLOW_READ,
                                   workflow_not_found_response())
    if err:
        return err

    table = dynamodb.Table(TEST_RUNS_TABLE)
    runs: List[Dict] = []
    kwargs = {
        'IndexName': 'workflow-runs-index',
        'KeyConditionExpression': 'workflow_id = :wid',
        'ExpressionAttributeValues': {':wid': workflow_id},
        'ScanIndexForward': False
    }
    while True:
        response = table.query(**kwargs)
        runs.extend(test_run_summary(decimal_to_native(i))
                    for i in response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key

    return create_response(200, {'test_runs': runs, 'count': len(runs)})


def sync_run_status_from_execution(item: Dict) -> Dict:
    """
    Refresh a non-terminal run's status from its Step Functions execution.
    Best-effort: on describe failure the stored status is returned unchanged.
    """
    if item.get('status') not in ('pending', 'running') or not item.get('execution_arn'):
        return item
    try:
        execution = stepfunctions.describe_execution(executionArn=item['execution_arn'])
    except ClientError as e:
        logger.warning(f"Could not describe execution for run {item['test_run_id']}: {str(e)}")
        return item

    sfn_status = execution.get('status')
    mapped = SFN_STATUS_MAP.get(sfn_status)
    if not mapped or mapped == item.get('status'):
        return item

    update_expr = 'SET #s = :status'
    expr_values: Dict[str, Any] = {':status': mapped}
    if mapped in ('completed', 'failed'):
        update_expr += ', finished_at = :finished'
        expr_values[':finished'] = now_ms()
    if sfn_status == 'TIMED_OUT':
        # 10-minute limit exceeded: failed with a timeout indication (12.13)
        update_expr += ', failure = :failure'
        expr_values[':failure'] = {
            'nodeId': item.get('failure', {}).get('nodeId') if isinstance(item.get('failure'), dict) else None,
            'message': 'Test run exceeded the 10 minute execution limit',
            'timeout': True
        }
    try:
        updated = dynamodb.Table(TEST_RUNS_TABLE).update_item(
            Key={'test_run_id': item['test_run_id']},
            UpdateExpression=update_expr,
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues=expr_values,
            ReturnValues='ALL_NEW'
        )
        return decimal_to_native(updated['Attributes'])
    except ClientError as e:
        logger.warning(f"Could not update run status for {item['test_run_id']}: {str(e)}")
        item['status'] = mapped
        return item


def load_node_results(results_s3_key: Optional[str]) -> List[Dict]:
    """
    Load per-node results from S3. The sandbox harness flushes them
    incrementally, so partial results are available for in-progress and
    mid-run-failed executions (12.10). Missing document -> empty list.
    """
    if not results_s3_key:
        return []
    try:
        response = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET, Key=results_s3_key)
        document = json.loads(response['Body'].read().decode('utf-8'))
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            return []
        logger.error(f"Error loading test results {results_s3_key}: {str(e)}")
        return []
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(f"Malformed test results document {results_s3_key}: {str(e)}")
        return []
    if isinstance(document, dict):
        nodes = document.get('nodes')
        return nodes if isinstance(nodes, list) else []
    return document if isinstance(document, list) else []


def get_test_run(event: Dict, user: Dict, test_run_id: str) -> Dict:
    """
    GET /test-runs/{id}
    Status plus per-node results {nodeId, status, outputs, stubActivity,
    error} produced so far (12.3, 12.7, 12.10).
    """
    item = get_test_run_item(test_run_id)
    if not item:
        return test_run_not_found_response()
    err = authorize_usecase_access(user, event, item['usecase_id'],
                                   Permission.WORKFLOW_READ,
                                   test_run_not_found_response())
    if err:
        return err

    item = sync_run_status_from_execution(item)
    node_results = load_node_results(item.get('results_s3_key'))

    return create_response(200, {
        'test_run': test_run_summary(item),
        'node_results': node_results
    })


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler - routes to the appropriate operation"""
    try:
        http_method = event.get('httpMethod')

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

        user = get_user_from_event(event)
        resource = event.get('resource', '')
        path_params = event.get('pathParameters') or {}
        resource_id = path_params.get('id')

        if resource == '/test-datasets':
            if http_method == 'GET':
                return list_test_datasets(event, user)
            if http_method == 'POST':
                return create_test_dataset(event, user)
        elif resource == '/test-datasets/{id}' and resource_id:
            if http_method == 'GET':
                return get_test_dataset(event, user, resource_id)
            if http_method == 'DELETE':
                return delete_test_dataset(event, user, resource_id)
        elif resource == '/workflows/{id}/test-runs' and resource_id:
            if http_method == 'POST':
                return start_test_run(event, user, resource_id)
            if http_method == 'GET':
                return list_test_runs(event, user, resource_id)
        elif resource == '/test-runs/{id}' and resource_id:
            if http_method == 'GET':
                return get_test_run(event, user, resource_id)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
