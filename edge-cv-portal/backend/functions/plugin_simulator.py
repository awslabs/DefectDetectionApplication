"""
Plugin_Simulator API Lambda function (Custom Node Designer, task 8.2)

Starts, tracks, and finalizes Plugin_Simulator runs against the
Plugin_Simulator Step Functions state machine
(Guard -> Prepare -> RunSandbox -> Collect, node-designer-stack.ts)
running the test-sandbox image with HARNESS_MODE=simulate
(Requirements 7.1, 7.2, 7.4, 7.5, 7.6, 7.7).

Routes (API Gateway REST):
    POST /plugins/{id}/versions/{v}/simulate
        Start a simulation run for one Plugin_Record version with
        parameter values. Input is either an existing Test_Dataset of
        the same Use_Case (dataset_id) or uploaded sample frames
        (sample_frames, 7.1). Re-running with changed parameter values
        is a new POST with new `parameters` (7.4). Refused with a 409
        describing the missing build when the version has no successful
        x86_64 Plugin_Artifact (7.5).
    GET  /simulations/{runId}
        Run status plus the results document the sandbox harness
        flushed to S3 (input/output frame refs and per-frame metadata,
        7.3; partial results for failed/timed-out runs, 7.6/7.7).

State machine steps (invoked by the state machine with
{step, input}, mirroring workflow_test_steps.py):
    guard           Re-check the x86_64-artifact guard inside the
                    execution; a failing guard marks the run failed
                    and short-circuits to a Fail state (7.5).
    prepare         Stage the run inputs under the run's S3 prefix:
                    copy the selected Test_Dataset (uploaded sample
                    frames are already staged by the start endpoint)
                    and copy the plugin's x86_64 .so from the
                    Plugin_Library into the run prefix, so the sandbox
                    task role never needs Plugin_Library access (7.2).
    collect         Finalize the SimulationRuns item from the results
                    document the harness flushed.
    record_timeout  Mark the run failed with a timeout indication;
                    the partial results flushed before termination
                    stay in S3 untouched (7.7).
    record_failure  Mark the run failed, carrying the error the
                    harness flushed (plugin error output included)
                    when available (7.6).

Storage layout (design "Data Models"):
    SimulationRuns table (SIMULATION_RUNS_TABLE)
        PK run_id, GSI usecase-runs-index (usecase_id, started_at)
        Attributes: plugin_id, version, usecase_id, dataset ref,
        parameters, element_factory, status, results_s3_key,
        failure {message, timeout}, started_at/finished_at,
        created_by, execution_arn.
    Run objects in portal S3 under
        plugin-simulations/{usecase_id}/{run_id}/...

Error envelope: {"error": {"code", "message", "details"}} with 400
parse/validation, 403 RBAC denial, 404 scoped to avoid cross-tenant
existence leaks, 409 for the missing-x86_64-build guard (7.5), and
503 while the simulator state machine is not provisioned.

Access control: node-designer:simulate (UseCaseAdmin within the
Use_Case, PortalAdmin) to start runs; node-designer:read for status/
results (Requirement 13). Every started run writes the AuditLog table.
"""
import base64
import json
import os
import logging
import re
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
    rbac_manager, Permission
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')
stepfunctions = boto3.client('stepfunctions')

# Environment variables
SIMULATION_RUNS_TABLE = os.environ.get('SIMULATION_RUNS_TABLE')
PLUGIN_RECORDS_TABLE = os.environ.get('PLUGIN_RECORDS_TABLE')
TEST_DATASETS_TABLE = os.environ.get('TEST_DATASETS_TABLE')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
PLUGIN_SIMULATIONS_PREFIX = os.environ.get('PLUGIN_SIMULATIONS_PREFIX',
                                           'plugin-simulations')
SIMULATOR_STATE_MACHINE_ARN = os.environ.get('SIMULATOR_STATE_MACHINE_ARN')

# ---------------------------------------------------------------- constants

#: The only architecture the Plugin_Simulator executes (requirements
#: glossary; the Fargate sandbox is plain x86_64).
SIMULATOR_ARCH = 'x86_64'
BUILD_SUCCEEDED = 'succeeded'

#: Run statuses on the SimulationRuns item.
STATUS_PENDING = 'pending'
STATUS_RUNNING = 'running'
STATUS_COMPLETED = 'completed'
STATUS_FAILED = 'failed'

#: Step Functions execution status -> simulation run status.
SFN_STATUS_MAP = {
    'RUNNING': STATUS_RUNNING,
    'SUCCEEDED': STATUS_COMPLETED,
    'FAILED': STATUS_FAILED,
    'TIMED_OUT': STATUS_FAILED,
    'ABORTED': STATUS_FAILED,
}

TIMEOUT_FAILURE_MESSAGE = ('Simulation run exceeded the 5 minute execution '
                           'limit; partial results produced before '
                           'termination were retained')

#: Uploaded sample frame constraints (7.1 upload path). Bounded well below
#: the API Gateway payload limit; larger inputs use a Test_Dataset.
SUPPORTED_FRAME_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
MAX_SAMPLE_FRAMES = 64
MAX_SAMPLE_FRAMES_BYTES = 6 * 1024 * 1024

#: GStreamer element factory name shape (mirrors the simulate harness's
#: launch-safety validation).
ELEMENT_FACTORY_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_-]*$')

#: Scalar JSON types accepted as declared parameter values.
SCALAR_TYPES = (str, int, float, bool)


# ------------------------------------------------------------------ helpers

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
    """Build the error envelope: {error: {code, message, details}}"""
    return create_response(status_code, {
        'error': {
            'code': code,
            'message': message,
            'details': details or {}
        }
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


def run_s3_prefix(usecase_id: str, run_id: str) -> str:
    """S3 prefix holding every object of one simulation run"""
    return f"{PLUGIN_SIMULATIONS_PREFIX}/{usecase_id}/{run_id}/"


def run_results_key(usecase_id: str, run_id: str) -> str:
    """S3 key of the incrementally flushed simulation results document"""
    return run_s3_prefix(usecase_id, run_id) + 'results.json'


def run_uploads_prefix(usecase_id: str, run_id: str) -> str:
    """S3 prefix uploaded sample frames are staged under (7.1)"""
    return run_s3_prefix(usecase_id, run_id) + 'uploads/'


def run_inputs_prefix(usecase_id: str, run_id: str) -> str:
    """S3 prefix the Prepare step stages a Test_Dataset copy under"""
    return run_s3_prefix(usecase_id, run_id) + 'inputs/'


def plugin_not_found_response() -> Dict:
    """Uniform 404 that never confirms whether a Plugin_Record exists"""
    return error_response(404, 'PLUGIN_NOT_FOUND', 'Plugin record not found')


def run_not_found_response() -> Dict:
    """Uniform 404 that never confirms whether a simulation run exists"""
    return error_response(404, 'SIMULATION_RUN_NOT_FOUND', 'Simulation run not found')


def dataset_not_found_response() -> Dict:
    """Uniform 404 that never confirms whether a Test_Dataset exists"""
    return error_response(404, 'TEST_DATASET_NOT_FOUND', 'Test dataset not found')


def has_node_designer_permission(user: Dict, usecase_id: str,
                                 permission: Permission) -> bool:
    """Check a registered node-designer RBAC action for the acting user"""
    return rbac_manager.has_permission(user['user_id'], usecase_id,
                                       permission, user_info=user)


def forbidden_response(user: Dict, event: Dict, usecase_id: str,
                       required: Permission) -> Dict:
    """Standard authorization error envelope with a denied-access audit
    entry (13.4), matching plugin_records.py"""
    log_audit_event(
        user_id=user['user_id'],
        action='unauthorized_access',
        resource_type='simulation_run',
        resource_id=event.get('resource', 'unknown'),
        result='denied',
        details={
            'required_permissions': [required.value],
            'usecase_id': usecase_id,
            'method': event.get('httpMethod'),
            'path': event.get('path')
        }
    )
    return error_response(403, 'FORBIDDEN', 'Insufficient permissions', {
        'required_permissions': [required.value],
        'usecase_id': usecase_id
    })


# ------------------------------------------------------------- persistence

def runs_table():
    return dynamodb.Table(SIMULATION_RUNS_TABLE)


def get_run_item(run_id: str) -> Optional[Dict]:
    """Fetch one SimulationRuns item, or None"""
    response = runs_table().get_item(Key={'run_id': run_id})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def get_plugin_version_item(plugin_id: str, version: int) -> Optional[Dict]:
    """Fetch one Plugin_Record version item, or None"""
    response = dynamodb.Table(PLUGIN_RECORDS_TABLE).get_item(
        Key={'plugin_id': plugin_id, 'version': version})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def get_dataset_item(dataset_id: str) -> Optional[Dict]:
    """Fetch a Test_Dataset record, or None"""
    response = dynamodb.Table(TEST_DATASETS_TABLE).get_item(
        Key={'dataset_id': dataset_id})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def mark_run_failed(run_id: str, message: str, timeout: bool = False) -> None:
    """
    Mark a run failed with {message, timeout}. Only the SimulationRuns
    item is touched: partial results the harness already flushed to S3
    survive (7.6, 7.7).
    """
    runs_table().update_item(
        Key={'run_id': run_id},
        UpdateExpression='SET #s = :status, finished_at = :finished, failure = :failure',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':status': STATUS_FAILED,
            ':finished': now_ms(),
            ':failure': {'message': message, 'timeout': timeout},
        },
    )


# ---------------------------------------------------------------- the guard
#
# Kept pure over the Plugin_Record item dict so the start-guard decision is
# property-testable without AWS (task 8.3, Requirement 7.5).

def evaluate_simulation_guard(item: Dict) -> Tuple[bool, Optional[Dict]]:
    """
    Decide whether a simulation run may start for a Plugin_Record
    version item.

    Returns (True, None) exactly when the version has a successfully
    built x86_64 Plugin_Artifact with a stored Plugin_Library key;
    otherwise (False, {code, message, details}) describing that
    simulation requires a successful x86_64 build (7.5).
    """
    artifacts = item.get('artifacts') or {}
    entry = artifacts.get(SIMULATOR_ARCH)
    if (isinstance(entry, dict)
            and entry.get('buildStatus') == BUILD_SUCCEEDED
            and entry.get('s3Key')):
        return True, None
    return False, {
        'code': 'SIMULATION_REQUIRES_X86_64_BUILD',
        'message': ('Simulation requires a successful x86_64 build: this '
                    'Plugin_Record version has no successfully built x86_64 '
                    'Plugin_Artifact. Build the plugin for x86_64 and retry.'),
        'details': {
            'plugin_id': item.get('plugin_id'),
            'version': item.get('version'),
            'missing': 'successful x86_64 Plugin_Artifact',
            'x86_64_build_status': (entry or {}).get('buildStatus')
            if isinstance(entry, dict) else None,
        },
    }


# ------------------------------------------------------------- input checks

def validate_parameters(parameters: Any) -> Tuple[Optional[Dict], Optional[Dict]]:
    """
    Validate the declared parameter values of a run (7.4): a flat JSON
    object of scalar values. Returns (parameters, None) or
    (None, error_response).
    """
    if parameters is None:
        return {}, None
    if not isinstance(parameters, dict):
        return None, error_response(400, 'INVALID_PARAMETERS',
                                    'parameters must be a JSON object of '
                                    'scalar parameter values')
    for name, value in parameters.items():
        if not ELEMENT_FACTORY_PATTERN.match(str(name)):
            return None, error_response(400, 'INVALID_PARAMETERS',
                                        f"Invalid parameter name '{name}'",
                                        {'parameter': str(name)})
        if value is not None and not isinstance(value, SCALAR_TYPES):
            return None, error_response(400, 'INVALID_PARAMETERS',
                                        f"Parameter '{name}' must be a scalar value",
                                        {'parameter': str(name)})
    return parameters, None


def validate_sample_frames(frames: Any) -> Tuple[Optional[List[Dict]], Optional[Dict]]:
    """
    Validate uploaded sample frames (7.1): a bounded list of
    {name, content_base64} JPEG/PNG entries. Returns
    ([{name, content(bytes)}], None) or (None, error_response).
    """
    if not isinstance(frames, list) or not frames:
        return None, error_response(400, 'INVALID_SAMPLE_FRAMES',
                                    'sample_frames must be a non-empty list of '
                                    '{name, content_base64} entries')
    if len(frames) > MAX_SAMPLE_FRAMES:
        return None, error_response(400, 'INVALID_SAMPLE_FRAMES',
                                    f'At most {MAX_SAMPLE_FRAMES} sample frames '
                                    'are accepted per run',
                                    {'max_frames': MAX_SAMPLE_FRAMES})
    decoded: List[Dict] = []
    total = 0
    for index, entry in enumerate(frames):
        if not isinstance(entry, dict) or not entry.get('name') \
                or not entry.get('content_base64'):
            return None, error_response(400, 'INVALID_SAMPLE_FRAMES',
                                        f'sample_frames[{index}] must carry '
                                        'name and content_base64')
        name = str(entry['name'])
        if '/' in name or '\\' in name or name in ('.', '..'):
            return None, error_response(400, 'INVALID_SAMPLE_FRAMES',
                                        f'sample_frames[{index}].name must be '
                                        'a plain file name', {'name': name})
        extension = os.path.splitext(name)[1].lower()
        if extension not in SUPPORTED_FRAME_EXTENSIONS:
            return None, error_response(
                400, 'UNSUPPORTED_FORMAT',
                f"Unsupported sample frame format '{extension or name}': only "
                'JPEG and PNG images are supported',
                {'file': name,
                 'supported_extensions': sorted(SUPPORTED_FRAME_EXTENSIONS)})
        try:
            content = base64.b64decode(entry['content_base64'], validate=True)
        except (ValueError, TypeError):
            return None, error_response(400, 'INVALID_SAMPLE_FRAMES',
                                        f'sample_frames[{index}].content_base64 '
                                        'is not valid base64', {'name': name})
        total += len(content)
        if total > MAX_SAMPLE_FRAMES_BYTES:
            return None, error_response(
                400, 'SAMPLE_FRAMES_TOO_LARGE',
                f'Uploaded sample frames exceed the {MAX_SAMPLE_FRAMES_BYTES} '
                'byte limit; use a Test_Dataset for larger inputs',
                {'max_bytes': MAX_SAMPLE_FRAMES_BYTES})
        decoded.append({'name': name, 'content': content})
    return decoded, None


def default_element_factory(item: Dict) -> str:
    """Derive a launch-safe element factory name from the plugin name"""
    cleaned = re.sub(r'[^A-Za-z0-9_]+', '', str(item.get('name') or '').lower())
    if cleaned and not ELEMENT_FACTORY_PATTERN.match(cleaned):
        cleaned = f'_{cleaned}'
    return cleaned or f"plugin{re.sub(r'[^a-z0-9]', '', str(item.get('plugin_id'))[:8])}"


def run_summary(item: Dict) -> Dict:
    """Public shape of a SimulationRuns record"""
    return {
        'run_id': item['run_id'],
        'plugin_id': item.get('plugin_id'),
        'version': item.get('version'),
        'usecase_id': item.get('usecase_id'),
        'dataset': item.get('dataset'),
        'parameters': item.get('parameters'),
        'element_factory': item.get('element_factory'),
        'status': item.get('status'),
        'results_s3_key': item.get('results_s3_key'),
        'failure': item.get('failure'),
        'started_at': item.get('started_at'),
        'finished_at': item.get('finished_at'),
        'created_by': item.get('created_by'),
    }


# ----------------------------------------------------------------- handlers

def start_simulation(event: Dict, user: Dict, plugin_id: str, version: int) -> Dict:
    """
    POST /plugins/{id}/versions/{v}/simulate
    Body: {dataset_id? | sample_frames?: [{name, content_base64}],
           parameters?: {name: value}, element_factory?}

    Starts a Plugin_Simulator run (7.1). Exactly one input source is
    required: an existing Test_Dataset of the same Use_Case, or
    uploaded sample frames staged under the run's prefix. `parameters`
    carries the declared parameter values for this run; a re-run with
    changed values is simply another POST (7.4). Refused with a 409
    describing the missing build when the version has no successful
    x86_64 Plugin_Artifact (7.5).
    """
    item = get_plugin_version_item(plugin_id, version)
    if not item:
        return plugin_not_found_response()
    usecase_id = item['usecase_id']
    if not has_node_designer_permission(user, usecase_id,
                                        Permission.NODE_DESIGNER_READ):
        return plugin_not_found_response()
    if not has_node_designer_permission(user, usecase_id,
                                        Permission.NODE_DESIGNER_SIMULATE):
        return forbidden_response(user, event, usecase_id,
                                  Permission.NODE_DESIGNER_SIMULATE)

    body, err = parse_body(event)
    if err:
        return err

    # The x86_64-artifact guard (7.5): refuse before anything is created.
    ok, guard_error = evaluate_simulation_guard(item)
    if not ok:
        log_audit_event(
            user_id=user['user_id'],
            action='start_simulation_run',
            resource_type='simulation_run',
            resource_id=plugin_id,
            result='rejected',
            details={'usecase_id': usecase_id, 'plugin_id': plugin_id,
                     'version': version, 'reason': guard_error['code']}
        )
        return error_response(409, guard_error['code'], guard_error['message'],
                              guard_error['details'])

    parameters, err = validate_parameters(body.get('parameters'))
    if err:
        return err

    element_factory = body.get('element_factory') or default_element_factory(item)
    if not ELEMENT_FACTORY_PATTERN.match(str(element_factory)):
        return error_response(400, 'INVALID_ELEMENT_FACTORY',
                              'element_factory must be a plain GStreamer '
                              'element factory name',
                              {'element_factory': str(element_factory)})

    dataset_id = body.get('dataset_id')
    sample_frames = body.get('sample_frames')
    if bool(dataset_id) == bool(sample_frames):
        return error_response(400, 'MISSING_INPUT',
                              'Provide exactly one input source: dataset_id '
                              '(an existing Test_Dataset) or sample_frames '
                              '(uploaded frames)')

    dataset_ref: Dict[str, Any]
    source_dataset_prefix: Optional[str] = None
    decoded_frames: Optional[List[Dict]] = None
    if dataset_id:
        # The Test_Dataset must exist in the same Use_Case; a dataset of
        # another tenant is indistinguishable from a missing one (7.1).
        dataset = get_dataset_item(dataset_id)
        if not dataset or dataset.get('usecase_id') != usecase_id:
            return dataset_not_found_response()
        source_dataset_prefix = dataset.get('s3_prefix')
        dataset_ref = {'kind': 'dataset', 'dataset_id': dataset_id}
    else:
        decoded_frames, err = validate_sample_frames(sample_frames)
        if err:
            return err
        dataset_ref = {'kind': 'uploaded', 'frame_count': len(decoded_frames)}

    if not SIMULATOR_STATE_MACHINE_ARN:
        return error_response(
            503, 'SIMULATOR_NOT_CONFIGURED',
            'The plugin simulator is not configured: no Step Functions state '
            'machine ARN is available. Deploy the node-designer simulator '
            'infrastructure.')

    run_id = str(uuid.uuid4())
    timestamp = now_ms()
    results_key = run_results_key(usecase_id, run_id)

    # Uploaded sample frames are staged under the run's prefix here, so the
    # Prepare step (and the sandbox task role) only ever touch
    # plugin-simulations/... (7.2).
    if decoded_frames is not None:
        uploads_prefix = run_uploads_prefix(usecase_id, run_id)
        for frame in decoded_frames:
            s3.put_object(Bucket=PORTAL_ARTIFACTS_BUCKET,
                          Key=uploads_prefix + frame['name'],
                          Body=frame['content'])
        source_dataset_prefix = uploads_prefix

    run_item = {
        'run_id': run_id,
        'plugin_id': plugin_id,
        'version': version,
        'usecase_id': usecase_id,
        'dataset': dataset_ref,
        'parameters': parameters,
        'element_factory': element_factory,
        'status': STATUS_PENDING,
        'results_s3_key': results_key,
        'failure': None,
        'started_at': timestamp,
        'finished_at': None,
        'created_by': user['user_id'],
    }
    runs_table().put_item(Item=run_item,
                          ConditionExpression='attribute_not_exists(run_id)')

    execution_input = {
        'run_id': run_id,
        'plugin_id': plugin_id,
        'version': version,
        'usecase_id': usecase_id,
        'artifacts_bucket': PORTAL_ARTIFACTS_BUCKET,
        'results_s3_key': results_key,
        # Prepare-step staging sources: the Test_Dataset objects (or the
        # already-staged uploads prefix) and the Plugin_Library artifact key.
        'input_kind': dataset_ref['kind'],
        'source_dataset_s3_prefix': source_dataset_prefix,
        'plugin_source_s3_key': item['artifacts'][SIMULATOR_ARCH]['s3Key'],
        'element_factory': element_factory,
        # The object for readability, plus the pre-serialized JSON string the
        # RunSandbox containerOverrides pass through as ELEMENT_PARAMETERS
        # (env values must be strings; 7.4).
        'parameters': parameters,
        'parameters_json': json.dumps(parameters),
    }
    try:
        execution = stepfunctions.start_execution(
            stateMachineArn=SIMULATOR_STATE_MACHINE_ARN,
            name=run_id,
            input=json.dumps(execution_input)
        )
    except ClientError as e:
        logger.error(f"Error starting simulation run {run_id}: {str(e)}")
        mark_run_failed(run_id, 'Simulation run could not be started')
        return error_response(502, 'SIMULATION_START_FAILED',
                              'The simulation run could not be started')

    runs_table().update_item(
        Key={'run_id': run_id},
        UpdateExpression='SET #s = :status, execution_arn = :arn',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':status': STATUS_RUNNING,
                                   ':arn': execution['executionArn']}
    )
    run_item['status'] = STATUS_RUNNING
    run_item['execution_arn'] = execution['executionArn']

    log_audit_event(
        user_id=user['user_id'],
        action='start_simulation_run',
        resource_type='simulation_run',
        resource_id=run_id,
        result='success',
        details={'usecase_id': usecase_id, 'plugin_id': plugin_id,
                 'version': version, 'input': dataset_ref,
                 'parameters': sorted(parameters)}
    )

    return create_response(202, {'simulation_run': run_summary(run_item)})


def sync_run_status_from_execution(item: Dict) -> Dict:
    """
    Refresh a non-terminal run's status from its Step Functions
    execution. Best-effort: on describe failure the stored status is
    returned unchanged. A TIMED_OUT execution (state-machine-level
    expiry) is marked failed-with-timeout (7.7).
    """
    if item.get('status') not in (STATUS_PENDING, STATUS_RUNNING) \
            or not item.get('execution_arn'):
        return item
    try:
        execution = stepfunctions.describe_execution(
            executionArn=item['execution_arn'])
    except ClientError as e:
        logger.warning(f"Could not describe execution for run "
                       f"{item['run_id']}: {str(e)}")
        return item

    sfn_status = execution.get('status')
    mapped = SFN_STATUS_MAP.get(sfn_status)
    if not mapped or mapped == item.get('status'):
        return item

    update_expr = 'SET #s = :status'
    expr_values: Dict[str, Any] = {':status': mapped}
    if mapped in (STATUS_COMPLETED, STATUS_FAILED):
        update_expr += ', finished_at = :finished'
        expr_values[':finished'] = now_ms()
    if sfn_status == 'TIMED_OUT':
        update_expr += ', failure = :failure'
        expr_values[':failure'] = {'message': TIMEOUT_FAILURE_MESSAGE,
                                   'timeout': True}
    try:
        updated = runs_table().update_item(
            Key={'run_id': item['run_id']},
            UpdateExpression=update_expr,
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues=expr_values,
            ReturnValues='ALL_NEW'
        )
        return decimal_to_native(updated['Attributes'])
    except ClientError as e:
        logger.warning(f"Could not update run status for "
                       f"{item['run_id']}: {str(e)}")
        item['status'] = mapped
        return item


def load_results_document(results_s3_key: Optional[str]) -> Optional[Dict]:
    """
    Load the simulation results document the harness flushed to S3.
    Incremental flushing means partial results are available for
    running, failed, and timed-out runs (7.6, 7.7). Missing or
    malformed document -> None.
    """
    if not results_s3_key:
        return None
    try:
        response = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET,
                                 Key=results_s3_key)
        document = json.loads(response['Body'].read().decode('utf-8'))
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            return None
        logger.error(f"Error loading simulation results "
                     f"{results_s3_key}: {str(e)}")
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(f"Malformed simulation results document "
                     f"{results_s3_key}: {str(e)}")
        return None
    return document if isinstance(document, dict) else None


def get_simulation(event: Dict, user: Dict, run_id: str) -> Dict:
    """
    GET /simulations/{runId}
    Run status plus the results document produced so far: per-frame
    {frameIndex, inputRef, outputRef, metadata} records for the
    side-by-side display (7.3); partial results with the failure for
    failed and timed-out runs (7.6, 7.7).
    """
    item = get_run_item(run_id)
    if not item:
        return run_not_found_response()
    if not has_node_designer_permission(user, item['usecase_id'],
                                        Permission.NODE_DESIGNER_READ):
        return run_not_found_response()

    item = sync_run_status_from_execution(item)
    results = load_results_document(item.get('results_s3_key'))

    return create_response(200, {
        'simulation_run': run_summary(item),
        'results': results,
    })


# ---------------------------------------------------------------------------
# State machine steps (invoked with {step, input}; see node-designer-stack.ts)
# ---------------------------------------------------------------------------

def step_guard(step_input: Dict) -> Dict:
    """
    Guard state (7.5): re-evaluate the x86_64-artifact guard inside the
    execution. A failing guard marks the run failed and returns
    {ok: false}; the state machine short-circuits to its Fail state.
    """
    run_id = step_input['run_id']
    item = get_plugin_version_item(step_input['plugin_id'],
                                   int(step_input['version']))
    ok, guard_error = evaluate_simulation_guard(item or {})
    if not ok:
        mark_run_failed(run_id, guard_error['message'])
        return {'ok': False, 'error': guard_error}
    return {'ok': True}


def step_prepare(step_input: Dict) -> Dict:
    """
    Prepare state: stage the run inputs under the run's S3 prefix so
    the sandbox task role never needs access outside
    plugin-simulations/... (7.2).

    - Test_Dataset input: copy the dataset objects to the run's
      inputs/ prefix. Uploaded sample frames were already staged under
      the run's uploads/ prefix by the start endpoint.
    - Plugin: copy the x86_64 .so from the Plugin_Library into the
      run's plugin/ prefix.

    Returns {dataset_s3_prefix, plugin_s3_key} for the RunSandbox env
    overrides (the simulate harness env contract).
    """
    run_id = step_input['run_id']
    usecase_id = step_input['usecase_id']
    source_prefix = step_input['source_dataset_s3_prefix']

    if step_input.get('input_kind') == 'uploaded':
        staged_prefix = source_prefix
    else:
        staged_prefix = run_inputs_prefix(usecase_id, run_id)
        continuation_token = None
        while True:
            kwargs = {'Bucket': PORTAL_ARTIFACTS_BUCKET, 'Prefix': source_prefix}
            if continuation_token:
                kwargs['ContinuationToken'] = continuation_token
            listed = s3.list_objects_v2(**kwargs)
            for obj in listed.get('Contents', []):
                relative = obj['Key'][len(source_prefix):]
                if not relative:
                    continue
                s3.copy_object(
                    Bucket=PORTAL_ARTIFACTS_BUCKET,
                    Key=staged_prefix + relative,
                    CopySource={'Bucket': PORTAL_ARTIFACTS_BUCKET,
                                'Key': obj['Key']},
                )
            if not listed.get('IsTruncated'):
                break
            continuation_token = listed.get('NextContinuationToken')

    plugin_source_key = step_input['plugin_source_s3_key']
    plugin_key = (run_s3_prefix(usecase_id, run_id) + 'plugin/'
                  + os.path.basename(plugin_source_key))
    s3.copy_object(
        Bucket=PORTAL_ARTIFACTS_BUCKET,
        Key=plugin_key,
        CopySource={'Bucket': PORTAL_ARTIFACTS_BUCKET,
                    'Key': plugin_source_key},
    )

    return {'dataset_s3_prefix': staged_prefix, 'plugin_s3_key': plugin_key}


def step_collect(step_input: Dict) -> Dict:
    """
    Collect state: finalize the SimulationRuns item from the results
    document the harness flushed (status completed, or failed with the
    harness error when the document reports one).
    """
    run_id = step_input['run_id']
    document = load_results_document(step_input.get('results_s3_key')) or {}
    if document.get('status') == STATUS_FAILED or document.get('error'):
        error = document.get('error') or {}
        mark_run_failed(run_id, str(error.get('message')
                                    or 'Simulation run failed'))
        return {'ok': False}
    runs_table().update_item(
        Key={'run_id': run_id},
        UpdateExpression='SET #s = :status, finished_at = :finished',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':status': STATUS_COMPLETED,
                                   ':finished': now_ms()},
    )
    return {'ok': True}


def step_record_timeout(step_input: Dict) -> Dict:
    """
    5-minute limit exceeded (7.7): Step Functions stopped the sandbox
    task; mark the run failed with a timeout indication. The partial
    results the harness flushed before termination stay in S3.
    """
    mark_run_failed(step_input['run_id'], TIMEOUT_FAILURE_MESSAGE,
                    timeout=True)
    return {'ok': True}


def step_record_failure(step_input: Dict) -> Dict:
    """
    Sandbox or step failure (7.6): mark the run failed, preferring the
    error the harness flushed (which includes the plugin's captured
    error output) over the generic message. Partial results stay in S3.
    """
    document = load_results_document(step_input.get('results_s3_key')) or {}
    error = document.get('error') or {}
    message = str(error.get('message') or 'Simulation run failed')
    mark_run_failed(step_input['run_id'], message)
    return {'ok': True}


STEP_HANDLERS = {
    'guard': step_guard,
    'prepare': step_prepare,
    'collect': step_collect,
    'record_timeout': step_record_timeout,
    'record_failure': step_record_failure,
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler - API routes plus state machine step dispatch"""
    # State machine step invocation ({step, input}), no API Gateway shape.
    if isinstance(event, dict) and 'step' in event and 'httpMethod' not in event:
        step = event.get('step')
        step_handler = STEP_HANDLERS.get(step)
        if not step_handler:
            raise ValueError(f"Unknown simulator step '{step}'")
        return step_handler(event.get('input') or {})

    try:
        http_method = event.get('httpMethod')

        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }

        user = get_user_from_event(event)
        resource = event.get('resource', '')
        path_params = event.get('pathParameters') or {}

        if resource == '/plugins/{id}/versions/{v}/simulate':
            plugin_id = path_params.get('id')
            try:
                version = int(path_params.get('v'))
            except (TypeError, ValueError):
                return error_response(400, 'INVALID_VERSION',
                                      'version must be an integer')
            if http_method == 'POST' and plugin_id:
                return start_simulation(event, user, plugin_id, version)
        elif resource == '/simulations/{runId}':
            run_id = path_params.get('runId')
            if http_method == 'GET' and run_id:
                return get_simulation(event, user, run_id)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
