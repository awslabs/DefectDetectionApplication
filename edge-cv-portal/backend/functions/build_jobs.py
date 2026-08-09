"""
Build Jobs API Lambda function (Build_Manager, portal build fleet)

Submission, monitoring, cancellation, and retry of edge component
Build_Jobs (JP5 / JP6 / AMD64 / AMD64_NVIDIA), following the portal
handler conventions (error envelope {error: {code, message, details}},
get_user_from_event, log_audit_event, RBAC via rbac_middleware).

Every decision is delegated to the pure module build_domain.py
(validate_build_request, create_build_jobs, decide_cancellation,
retry_clone); this handler does I/O and wiring only.

Routes (API Gateway REST):
    POST /builds               Validate the build request, create one
                               Build_Job per selected Build_Target
                               (BuildJobs table), audit build_requested,
                               async-invoke the dispatcher
                               (Req 1.1, 1.2, 1.5, 1.7, 1.9)
    GET  /builds               90-day history, most recent first,
                               paginated; succeeded jobs carry their
                               published artifact identifiers (Req 4.7)
    GET  /builds/{id}          Build_Job detail (Req 4.3)
    GET  /builds/{id}/logs     One CloudWatch Logs page of the job's log
                               stream with nextToken pagination (Req 4.4)
    POST /builds/{id}/cancel   queued -> immediate cancellation;
                               running -> SSM stop + pgrep confirmation
                               within 5 minutes; terminal -> 409
                               (Req 4.5, 4.6, 4.8, 4.9)
    POST /builds/{id}/retry    Retry-clone of an interrupted Build_Job
                               with a retry_of reference (Req 3.6)
    GET  /build-branches       Branch discovery for a validated repository
                               URL, behind the builds read boundary
                               (build-source-selection Req 3.1, 3.3-3.5)

Access control (registered by task 7.1 in shared_utils / rbac_middleware,
global scope — builds are not Use_Case-scoped):
    builds:submit   POST /builds, POST /builds/{id}/retry
    builds:read     GET routes
    builds:cancel   POST /builds/{id}/cancel
Denials return the standard authorization error and record a
denied-access Audit_Log entry (Req 1.6, 4.10).

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates
Requirements: 1.1, 1.2, 1.5, 1.6, 1.7, 1.9, 3.6, 4.3, 4.4, 4.5, 4.6,
4.7, 4.8, 4.9, 4.10
"""
import base64
import json
import logging
import os
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event
)
from rbac_middleware import (
    require_builds_submit, require_builds_cancel, require_builds_read
)

# Pure decision module (no AWS clients): every accept/reject/transition
# decision this handler acts on comes from build_domain.
import build_domain
# Pure shared reconciliation contract (build-fleet-execution-failures):
# the terminal-effects ledger for completed cancellations (task 6.2).
import build_reconciliation
# Pure source-selection module: repository/ref normalization for the
# per-submission source (build-source-selection Req 1.6, 2.5).
import build_source

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
logs_client = boto3.client('logs')
ssm = boto3.client('ssm')
lambda_client = boto3.client('lambda')

# Environment variables (build-fleet-stack.ts lambdaEnvironment)
BUILD_JOBS_TABLE = os.environ.get('BUILD_JOBS_TABLE')
BUILD_SERVERS_TABLE = os.environ.get('BUILD_SERVERS_TABLE')
SETTINGS_TABLE = os.environ.get('SETTINGS_TABLE')
BUILD_DISPATCHER_FUNCTION_NAME = os.environ.get('BUILD_DISPATCHER_FUNCTION_NAME')
#: CloudWatch Logs group the build agent streams to (design §5; 90-day
#: retention is set on the group by the infrastructure stack).
BUILD_LOG_GROUP = os.environ.get('BUILD_LOG_GROUP', '/dda/portal-builds')

# ---------------------------------------------------------------- constants

#: PortalSettings item key holding the build infrastructure configuration
#: (design §7; build_config.py owns reads/writes of the full config API).
BUILD_CONFIG_SETTING_KEY = 'build_infrastructure_config'

#: Documented defaults applied on read for absent values (Req 9.2).
#: build_domain.DEFAULT_BUILD_CONFIG is the one authoritative parameter
#: table (build-source-selection design B1 collapsed the former duplicate
#: literal); readers copy it (dict(...)) before merging stored values.
DEFAULT_BUILD_CONFIG: Dict[str, Any] = build_domain.DEFAULT_BUILD_CONFIG

#: Build history window (Req 4.7) and item retention (design: DynamoDB
#: TTL of 180 days > the 90-day retention floor, Req 3.4 / 4.7).
HISTORY_WINDOW_DAYS = 90
JOB_TTL_DAYS = 180

#: GET /builds pagination bounds.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

#: GET /builds/{id}/logs page bounds (CloudWatch get_log_events limit).
DEFAULT_LOG_EVENTS_LIMIT = 200
MAX_LOG_EVENTS_LIMIT = 1000

#: Running-job cancellation: window within which the SSM stop must be
#: confirmed via pgrep verification (Req 4.6, 4.9; design Property 9).
#: Overridable for tests; the domain constant is the source of truth.
CANCEL_CONFIRMATION_WINDOW_SECONDS = int(os.environ.get(
    'CANCEL_CONFIRMATION_WINDOW_SECONDS',
    str(build_domain.CANCELLATION_CONFIRMATION_WINDOW_MINUTES * 60)))
#: Interval between pgrep verification attempts during that window.
CANCEL_VERIFY_INTERVAL_SECONDS = int(os.environ.get(
    'CANCEL_VERIFY_INTERVAL_SECONDS', '10'))
#: Safety margin kept from the Lambda's own deadline while polling.
LAMBDA_TIME_RESERVE_MS = 5000

#: SSM stop command for a running build (design §5 cancellation): kill
#: the build process trees the serialization rules name
#: (.kiro/steering/builds.md) plus the portal agent entry point.
STOP_BUILD_COMMANDS = [
    'pkill -f "gdk component build" || true',
    'pkill -f "build-custom.sh" || true',
    'pkill -f "portal-build.sh" || true',
]

#: pgrep verification command: prints BUILD_PROCESS_COUNT=<n> and always
#: exits 0 so the SSM invocation itself cannot fail on "no match".
VERIFY_STOPPED_COMMANDS = [
    'C1=$(pgrep -cf "gdk component build" 2>/dev/null || true)',
    'C2=$(pgrep -cf "build-custom.sh" 2>/dev/null || true)',
    'C3=$(pgrep -cf "portal-build.sh" 2>/dev/null || true)',
    'echo "BUILD_PROCESS_COUNT=$(( ${C1:-0} + ${C2:-0} + ${C3:-0} ))"',
]

#: Terminal SSM command invocation statuses.
SSM_TERMINAL_STATUSES = ('Success', 'Failed', 'TimedOut', 'Cancelled')


# ------------------------------------------------------------ pure helpers

def error_response(status_code: int, code: str, message: str,
                   details: Optional[Dict] = None) -> Dict:
    """Build the portal error envelope: {error: {code, message, details}}"""
    return create_response(status_code, {
        'error': {
            'code': code,
            'message': message,
            'details': details or {},
        }
    })


def now_ms() -> int:
    """Current epoch milliseconds (BuildJobs timestamps are ms epoch)"""
    return int(time.time() * 1000)


def parse_body(event: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Parse the request body; returns (body, None) or (None, error_response)"""
    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return None, error_response(400, 'INVALID_BODY',
                                    'Request body must be valid JSON')
    if not isinstance(body, dict):
        return None, error_response(400, 'INVALID_BODY',
                                    'Request body must be a JSON object')
    return body, None


def to_native(value: Any) -> Any:
    """Convert DynamoDB Decimals to native ints/floats (deep)"""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_native(v) for v in value]
    return value


def encode_page_token(offset: int) -> str:
    """Opaque nextToken for GET /builds pagination (offset over the
    deterministically sorted 90-day history)"""
    return base64.urlsafe_b64encode(
        json.dumps({'offset': offset}).encode('utf-8')).decode('ascii')


def decode_page_token(token: Optional[str]) -> Optional[int]:
    """Decode a GET /builds nextToken; returns the offset or None when
    the token is absent/invalid"""
    if not token:
        return 0
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(token.encode('ascii')).decode('utf-8'))
        offset = int(payload.get('offset'))
        return offset if offset >= 0 else None
    except (ValueError, TypeError, KeyError, json.JSONDecodeError,
            base64.binascii.Error):
        return None


def parse_build_process_count(stdout: str) -> Optional[int]:
    """Parse BUILD_PROCESS_COUNT=<n> from the pgrep verification output;
    None when the marker is absent (unparseable output is never treated
    as confirmation — fail closed, Req 4.9)"""
    for line in (stdout or '').splitlines():
        line = line.strip()
        if line.startswith('BUILD_PROCESS_COUNT='):
            try:
                return int(line.split('=', 1)[1].strip())
            except ValueError:
                return None
    return None


def history_sort_key(job: Dict) -> Tuple[int, str]:
    """Deterministic most-recent-first ordering for the history list
    (created_at desc, build_job_id as the tie-break, Req 4.7)"""
    created = job.get('created_at') or 0
    return (int(created), str(job.get('build_job_id') or ''))


# ------------------------------------------------------------- persistence

def jobs_table():
    """BuildJobs DynamoDB table accessor"""
    return dynamodb.Table(BUILD_JOBS_TABLE)


def servers_table():
    """BuildServers DynamoDB table accessor"""
    return dynamodb.Table(BUILD_SERVERS_TABLE)


def scan_all(table) -> List[Dict]:
    """Full paginated scan of a table"""
    items: List[Dict] = []
    kwargs: Dict[str, Any] = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            return items
        kwargs['ExclusiveStartKey'] = last_key


def get_job(build_job_id: str) -> Optional[Dict]:
    """Fetch one Build_Job record (native types) or None"""
    response = jobs_table().get_item(Key={'build_job_id': build_job_id})
    item = response.get('Item')
    return to_native(item) if item else None


def list_fleet_servers() -> List[Dict]:
    """BuildServers fleet state in the shape build_domain expects for
    validation ({server_id, lifecycle_state, arch})"""
    servers = []
    for item in scan_all(servers_table()):
        record = to_native(item)
        record['arch'] = record.get('cpu_architecture') or record.get('arch')
        servers.append(record)
    return servers


def effective_build_config() -> Dict[str, Any]:
    """Effective build infrastructure configuration: stored PortalSettings
    values merged over the documented defaults (Req 9.2). Jobs snapshot
    this at creation (Req 9.3); read failures fall back to defaults so a
    submit never fails on a missing settings item."""
    config = dict(DEFAULT_BUILD_CONFIG)
    if not SETTINGS_TABLE:
        return config
    try:
        response = dynamodb.Table(SETTINGS_TABLE).get_item(
            Key={'setting_key': BUILD_CONFIG_SETTING_KEY})
        item = response.get('Item')
        if item:
            stored = item.get('value') if isinstance(item.get('value'), dict) \
                else item
            stored = to_native(stored)
            for key in DEFAULT_BUILD_CONFIG:
                if stored.get(key) is not None:
                    config[key] = stored[key]
    except ClientError as e:
        logger.warning(
            f"Could not read build configuration, using defaults: {e}")
    return config


def to_dynamo_safe(value: Any) -> Any:
    """JSON-shaped value with floats as Decimal: DynamoDB rejects Python
    floats, and the job's config_snapshot may carry non-integral numbers
    (e.g. a fractional max_runtime_hours)."""
    return json.loads(json.dumps(value), parse_float=Decimal)


#: Attributes that are a key of the BuildJobs table or of one of its GSIs
#: (`status-index`: status/created_at, `server-index`: server_id/created_at,
#: `request-index`: request_id/request_order). DynamoDB rejects an item
#: whose *indexed* key attribute is present with a type other than the one
#: the index declares — an ephemeral Build_Job carries `server_id = None`,
#: which serializes to the NULL type and fails `server-index` (typed S)
#: with `ValidationException: Type mismatch for Index Key server_id`.
#: An *absent* key attribute is legal: the item is simply not indexed by
#: that GSI, which is exactly the desired semantics for a job with no
#: Dedicated_Build_Server.
INDEXED_KEY_ATTRIBUTES = (
    'build_job_id', 'status', 'created_at',
    'server_id', 'request_id', 'request_order',
)


def without_null_index_keys(item: Dict[str, Any]) -> Dict[str, Any]:
    """The item as stored: indexed key attributes whose value is None are
    omitted rather than written as NULL (see INDEXED_KEY_ATTRIBUTES).

    Only indexed keys are stripped; every other None-valued attribute
    (e.g. `predecessor_job_id` of the first job of a request, or a
    `config_snapshot.source_ref` of None) is stored as NULL exactly as
    before, since non-key attributes accept any type."""
    return {key: value for key, value in item.items()
            if value is not None or key not in INDEXED_KEY_ATTRIBUTES}


def put_new_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a freshly created Build_Job record, adding the log
    location (design data model) and the 180-day TTL (seconds epoch, as
    DynamoDB TTL requires; > the 90-day retention floor).

    The returned record is the full job shape (an ephemeral job keeps
    `server_id: None`, the API response contract); only the stored item
    drops None-valued indexed keys."""
    item = dict(job)
    item['log'] = {'group': BUILD_LOG_GROUP, 'stream': job['build_job_id']}
    item['ttl'] = int(job['created_at'] / 1000) + JOB_TTL_DAYS * 24 * 60 * 60
    jobs_table().put_item(
        Item=to_dynamo_safe(without_null_index_keys(item)))
    return item


def invoke_dispatcher(build_job_ids: List[str]) -> None:
    """Async-invoke the dispatcher so provisioning starts well within
    60 s of submit (design §3). Best-effort: the dispatcher's 1-minute
    EventBridge schedule is the fallback, so an invocation failure never
    fails the submit."""
    if not BUILD_DISPATCHER_FUNCTION_NAME:
        logger.warning('BUILD_DISPATCHER_FUNCTION_NAME unset; relying on '
                       'the dispatcher schedule')
        return
    try:
        lambda_client.invoke(
            FunctionName=BUILD_DISPATCHER_FUNCTION_NAME,
            InvocationType='Event',
            Payload=json.dumps({
                'action': 'dispatch',
                'build_job_ids': build_job_ids,
            }).encode('utf-8'),
        )
    except Exception as e:
        logger.warning(f"Dispatcher async invoke failed (schedule will "
                       f"pick the jobs up): {e}")


# ------------------------------------------------------------ POST /builds

@require_builds_submit()
def submit_build(event: Dict, context: Any) -> Dict:
    """POST /builds — validate the request against the fleet state via
    build_domain.validate_build_request, create one queued Build_Job per
    selected Build_Target (request order, predecessor chaining, shared
    request_id, config snapshot), audit build_requested per job, and
    async-invoke the dispatcher (Req 1.1, 1.2, 1.5, 1.7, 1.9).

    The body may carry an optional per-submission source selection
    (build-source-selection Req 1.2, 1.3, 1.6, 2.4, 2.5): a validated
    ``repository``/``source_ref`` pair is resolved against the effective
    configuration and recorded on the jobs' config_snapshot with ADDITIVE
    keys only — ``repository`` is the normalized submitted URL or the
    configured ``default_repository``; ``source_ref`` is overwritten only
    when a ref was actually submitted (otherwise the configured value
    stands, ``None`` meaning the repository's default branch)."""
    body, err = parse_body(event)
    if err:
        return err
    user = get_user_from_event(event)

    servers = list_fleet_servers()
    config = effective_build_config()

    result = build_domain.validate_build_request(body, servers, config)
    if not result.valid:
        # Rejected without creating any Build_Job (Req 1.4, 1.8, 2.4,
        # 2.6, 2.8); every error names its failing rule.
        return error_response(
            400, 'BUILD_REQUEST_INVALID',
            'The build request is invalid: '
            + ' '.join(e['message'] for e in result.errors),
            {'errors': [dict(e) for e in result.errors]})

    # Resolve the per-submission source AFTER validation (the values were
    # accepted above, so both normalizations succeed here) and extend the
    # snapshot in place — effective_build_config() returned a fresh dict,
    # and create_build_jobs deep-copies it per job (Req 1.6, 2.4, 2.5;
    # config_snapshot is extended with additive keys only, Req 7.5).
    normalized_repository = None
    if body.get('repository') is not None:
        normalized_repository, _ = build_source.normalize_repository_url(
            body['repository'])
    submitted_ref, _ = build_source.normalize_source_ref(
        body.get('source_ref'))
    config['repository'] = (normalized_repository
                            or config['default_repository'])
    if submitted_ref is not None:
        config['source_ref'] = submitted_ref

    targets = body['targets']
    execution_mode = body['execution_mode']
    server_id = body.get('server_id')
    request_id = str(uuid.uuid4())
    job_ids = [str(uuid.uuid4()) for _ in targets]
    created_at = now_ms()

    jobs = build_domain.create_build_jobs(
        targets=targets,
        execution_mode=execution_mode,
        server_id=server_id,
        request_id=request_id,
        job_ids=job_ids,
        requested_by=user['user_id'],
        created_at=created_at,
        config_snapshot=config,
    )

    stored_jobs = [put_new_job(job) for job in jobs]

    # One build_requested Audit_Log entry per created Build_Job with the
    # requesting user, Build_Target, execution mode, and submission time
    # (Req 1.7).
    for job in stored_jobs:
        log_audit_event(
            user_id=user['user_id'],
            action='build_requested',
            resource_type='build_job',
            resource_id=job['build_job_id'],
            result='success',
            details={
                'build_target': job['build_target'],
                'execution_mode': job['execution_mode'],
                'server_id': job.get('server_id'),
                'request_id': request_id,
                'request_order': job['request_order'],
                'submitted_at': created_at,
                # The resolved source the job will be built from
                # (build-source-selection Req 1.6, 2.5).
                'repository': config['repository'],
                'source_ref': config.get('source_ref'),
            })

    invoke_dispatcher(job_ids)

    return create_response(201, {'request_id': request_id,
                                 'jobs': stored_jobs})


# ------------------------------------------------------------- GET /builds

@require_builds_read()
def list_builds(event: Dict, context: Any) -> Dict:
    """GET /builds — the 90-day Build_Job history, most recent first,
    paginated via an opaque nextToken; succeeded jobs carry their
    published artifact identifiers in `result` (Req 4.7)."""
    params = event.get('queryStringParameters') or {}

    try:
        limit = int(params.get('limit') or DEFAULT_PAGE_SIZE)
    except ValueError:
        return error_response(400, 'INVALID_PARAMETER',
                              'limit must be an integer')
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    offset = decode_page_token(params.get('nextToken'))
    if offset is None:
        return error_response(400, 'INVALID_PARAMETER',
                              'nextToken is not a valid page token')

    cutoff = now_ms() - HISTORY_WINDOW_DAYS * 24 * 60 * 60 * 1000
    jobs = [to_native(item) for item in scan_all(jobs_table())]
    history = sorted(
        (job for job in jobs if (job.get('created_at') or 0) >= cutoff),
        key=history_sort_key, reverse=True)

    page = history[offset:offset + limit]
    next_token = (encode_page_token(offset + limit)
                  if offset + limit < len(history) else None)

    return create_response(200, {'jobs': page, 'nextToken': next_token,
                                 'total': len(history)})


# -------------------------------------------------------- GET /builds/{id}

@require_builds_read()
def get_build(event: Dict, context: Any) -> Dict:
    """GET /builds/{id} — Build_Job detail (Req 4.3)."""
    build_job_id = (event.get('pathParameters') or {}).get('id')
    job = get_job(build_job_id) if build_job_id else None
    if not job:
        return error_response(404, 'BUILD_JOB_NOT_FOUND',
                              'Build job not found')
    return create_response(200, {'job': job})


# --------------------------------------------------- GET /builds/{id}/logs

# Optional Build Log / detail diagnostic projection
# (build-fleet-execution-failures task 9.1, Req 2.2, 2.3, 2.10, 2.18,
# 3.6). Pure re-keying of PERSISTED, already normalized/redacted/bounded
# data (build_reconciliation wrote it; nothing raw is read here). When a
# job carries no persisted diagnostic evidence, the projection is None
# and the legacy response is returned byte-compatible (Req 3.6).


def _elapsed_ms(start: Any, end: Any) -> Optional[int]:
    """Elapsed milliseconds between two recorded instants, or None when
    either endpoint was never recorded (identified as unavailable, never
    fabricated — Req 2.18)."""
    if start is None or end is None:
        return None
    return max(0, int(end) - int(start))


def _project_stream_field(field: Any) -> Dict[str, Any]:
    """One persisted stdout/stderr record in the approved API shape
    {text, available, truncated} (design "Build Log Persistence, API,
    and UI"). A field the provider never returned stays
    {available: false}."""
    if not isinstance(field, dict) or not field.get('available'):
        return {'available': False}
    return {
        'available': True,
        'text': field.get('text') or '',
        'truncated': bool(field.get('truncated')),
    }


def _project_timing_block(job: Dict[str, Any]) -> Dict[str, Any]:
    """Phase durations {queueMs, provisioningMs, executionMs} from the
    persisted safe timing evidence: the terminal ``timeout_evidence``
    record when one was decided (task 8.2), else the job's ``timing``
    map endpoints. A phase without recorded evidence is None."""
    timeout_evidence = job.get('timeout_evidence')
    if isinstance(timeout_evidence, dict) and timeout_evidence:
        return {
            'queueMs': timeout_evidence.get('queue_wait_ms'),
            'provisioningMs': timeout_evidence.get('provisioning_ms'),
            'executionMs': timeout_evidence.get('execution_runtime_ms'),
        }
    timing = job.get('timing')
    timing = timing if isinstance(timing, dict) else {}
    queue_ms = _elapsed_ms(
        timing.get('queue_started_at', job.get('created_at')),
        timing.get('queue_ended_at', job.get('dispatched_at')))
    provisioning = _elapsed_ms(
        timing.get('provisioning_started_at', job.get('dispatched_at')),
        timing.get('provisioning_ended_at'))
    execution = _elapsed_ms(
        timing.get('execution_started_at'),
        timing.get('execution_ended_at', job.get('ended_at')))
    return {'queueMs': queue_ms, 'provisioningMs': provisioning,
            'executionMs': execution}


def _project_timeout_block(timeout_evidence: Any) -> Optional[Dict[str, Any]]:
    """Timeout evidence fields where present (Req 2.18): kind, budget +
    source, last heartbeat/progress, target/mode. None when the job
    never reached a timeout decision."""
    if not isinstance(timeout_evidence, dict) or not timeout_evidence:
        return None
    return {
        'kind': timeout_evidence.get('timeout_kind'),
        'phase': timeout_evidence.get('phase'),
        'observedMs': timeout_evidence.get('observed_ms'),
        'budgetMs': timeout_evidence.get('budget_ms'),
        'budgetSource': timeout_evidence.get('budget_source'),
        'hardRuntimeMs': timeout_evidence.get('hard_runtime_ms'),
        'lastHeartbeatAt': timeout_evidence.get('last_heartbeat_at'),
        'lastProgressAt': timeout_evidence.get('last_progress_at'),
        'buildTarget': timeout_evidence.get('build_target'),
        'executionMode': timeout_evidence.get('execution_mode'),
        'decidedAt': timeout_evidence.get('timeout_decided_at'),
    }


def project_job_diagnostic(job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The optional versioned ``diagnostic`` for Build Log pages and
    detail (design "Build Log Persistence, API, and UI"):

    - built ONLY from the persisted normalized/redacted/bounded
      ``execution_diagnostic`` / ``timing`` / ``timeout_evidence``
      records — never from raw provider data (Req 2.10);
    - None when the job carries no persisted diagnostic evidence, so
      legacy responses stay byte-compatible (Req 3.6);
    - independent of CloudWatch stream existence and immutable across
      pages (Req 2.3).
    """
    persisted = job.get('execution_diagnostic')
    persisted = persisted if isinstance(persisted, dict) and persisted \
        else None
    timeout_evidence = job.get('timeout_evidence')
    timeout_evidence = timeout_evidence \
        if isinstance(timeout_evidence, dict) and timeout_evidence else None
    if persisted is None and timeout_evidence is None:
        return None

    diag = persisted or {}
    projected: Dict[str, Any] = {
        'schemaVersion': diag.get('schema_version', 1),
        'classification': (diag.get('classification')
                           or (timeout_evidence or {}).get('timeout_kind')),
        'status': diag.get('status'),
        'statusDetails': diag.get('status_details'),
        'responseCode': diag.get('response_code'),
        'stdout': _project_stream_field(diag.get('stdout')),
        'stderr': _project_stream_field(diag.get('stderr')),
        'timing': _project_timing_block(job),
        'observedAt': (diag.get('observed_at')
                       if diag.get('observed_at') is not None
                       else (timeout_evidence or {}).get(
                           'timeout_decided_at')),
        'complete': bool(diag.get('complete')),
    }
    timeout_block = _project_timeout_block(timeout_evidence)
    if timeout_block is not None:
        projected['timeout'] = timeout_block
    # Optional disk-capacity evidence (task 7.5, Req 2.23): projected
    # only when a measurement block was persisted; {available: false}
    # markers pass through truthfully.
    disk = diag.get('disk')
    if isinstance(disk, dict) and disk:
        projected['disk'] = disk
    return projected


@require_builds_read()
def get_build_logs(event: Dict, context: Any) -> Dict:
    """GET /builds/{id}/logs — one CloudWatch Logs page of the job's log
    stream, forward-paginated with nextToken (Req 4.4). The stream is
    written by the build agent via SSM CloudWatchOutputConfig; a stream
    that does not exist yet (job not started) yields an empty page.

    `events` and `nextToken` are preserved exactly; when the job carries
    persisted diagnostic evidence, the optional versioned `diagnostic`
    is added — independently of CloudWatch stream existence, repeated
    across pages as immutable metadata (build-fleet-execution-failures
    Req 2.2, 2.3, 3.6). Jobs without persisted diagnostics keep the
    legacy response shape unchanged."""
    build_job_id = (event.get('pathParameters') or {}).get('id')
    job = get_job(build_job_id) if build_job_id else None
    if not job:
        return error_response(404, 'BUILD_JOB_NOT_FOUND',
                              'Build job not found')

    params = event.get('queryStringParameters') or {}
    try:
        limit = int(params.get('limit') or DEFAULT_LOG_EVENTS_LIMIT)
    except ValueError:
        return error_response(400, 'INVALID_PARAMETER',
                              'limit must be an integer')
    limit = max(1, min(limit, MAX_LOG_EVENTS_LIMIT))

    log_location = job.get('log') or {}
    group = log_location.get('group') or BUILD_LOG_GROUP
    stream = log_location.get('stream') or build_job_id

    kwargs: Dict[str, Any] = {
        'logGroupName': group,
        'logStreamName': stream,
        'limit': limit,
        'startFromHead': True,
    }
    next_token = params.get('nextToken')
    if next_token:
        kwargs['nextToken'] = next_token

    # Persisted diagnostic evidence is returned independently of
    # CloudWatch stream existence (Req 2.3); None for legacy jobs keeps
    # the response byte-compatible (Req 3.6).
    diagnostic = project_job_diagnostic(job)

    try:
        response = logs_client.get_log_events(**kwargs)
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code')
        if code == 'ResourceNotFoundException':
            # No output yet (queued/provisioning job) or the stream was
            # never created (the incident shape): empty event page, but
            # the retained diagnostic still surfaces when one exists.
            body: Dict[str, Any] = {'events': [], 'nextToken': None}
            if diagnostic is not None:
                body['diagnostic'] = diagnostic
            return create_response(200, body)
        if code == 'InvalidParameterException':
            return error_response(400, 'INVALID_PARAMETER',
                                  'nextToken is not a valid log page token')
        raise

    events = [{'timestamp': e.get('timestamp'), 'message': e.get('message')}
              for e in response.get('events', [])]
    forward_token = response.get('nextForwardToken')
    # CloudWatch returns the same token when the page is exhausted; the
    # client polls the same token for new output of a running build.
    body = {'events': events, 'nextToken': forward_token}
    if diagnostic is not None:
        body['diagnostic'] = diagnostic
    return create_response(200, body)


# ------------------------------------------------------ GET /build-branches

#: HTTP status per distinct discovery failure code (build-source-selection
#: Req 3.3): each upstream condition surfaces as its own status + code in
#: the standard error envelope, never as an empty-list success.
DISCOVERY_ERROR_STATUS: Dict[str, int] = {
    build_source.REPOSITORY_NOT_FOUND: 404,
    build_source.REPOSITORY_EMPTY: 409,
    build_source.REPOSITORY_FORBIDDEN: 403,
    build_source.DISCOVERY_RATE_LIMITED: 429,
    build_source.DISCOVERY_TIMEOUT: 504,
    build_source.DISCOVERY_UPSTREAM_ERROR: 502,
}


@require_builds_read()
def list_build_branches(event: Dict, context: Any) -> Dict:
    """GET /build-branches?repository= — the selected repository's
    branches and its default branch, for the submission form's branch
    dropdown (build-source-selection Req 3.1, 3.3, 3.4, 3.5).

    Guarded by the same builds read boundary as the rest of the builds
    surface (Req 3.4). The ``repository`` query parameter is validated
    and normalized through ``build_source.normalize_repository_url``
    BEFORE any outbound call (Req 3.5); a rejected value returns the
    standard envelope naming the field and no discovery call is made.
    Every discovery failure maps to its own distinct status/code pair
    from :data:`DISCOVERY_ERROR_STATUS` (Req 3.3)."""
    params = event.get('queryStringParameters') or {}
    normalized, rejection = build_source.normalize_repository_url(
        params.get('repository'))
    if rejection is not None:
        return error_response(
            400, 'REPOSITORY_INVALID', rejection['message'],
            {'field': rejection['field'], 'rule': rejection['rule']})

    result = build_source.discover_branches(normalized)
    if 'error' in result:
        failure = result['error']
        return error_response(
            DISCOVERY_ERROR_STATUS.get(failure['code'], 502),
            failure['code'], failure['message'],
            {'repository': normalized})

    return create_response(200, result)


# ------------------------------------------------- POST /builds/{id}/cancel

def resolve_build_instance(job: Dict) -> Tuple[Optional[str], Optional[str]]:
    """(instance_id, server_name) of the Build_Server a running job's
    build process executes on: the job's ephemeral runner, or the
    dedicated server's registered EC2 instance."""
    if job.get('execution_mode') == build_domain.EXECUTION_MODE_EPHEMERAL:
        runner = job.get('runner') or {}
        instance_id = runner.get('instance_id')
        return instance_id, instance_id
    server_id = job.get('server_id')
    if not server_id:
        return None, None
    response = servers_table().get_item(Key={'server_id': server_id})
    server = to_native(response.get('Item') or {})
    return server.get('instance_id'), server.get('name') or server_id


def send_shell_command(instance_id: str, commands: List[str]) -> str:
    """SSM SendCommand (AWS-RunShellScript) returning the command id"""
    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName='AWS-RunShellScript',
        Parameters={'commands': commands},
    )
    return response['Command']['CommandId']


def wait_for_command(command_id: str, instance_id: str,
                     deadline: float) -> Optional[Dict]:
    """Poll one SSM command invocation to a terminal status before the
    deadline; returns the invocation or None on window expiry."""
    while time.time() < deadline:
        try:
            invocation = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id)
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') == \
                    'InvocationDoesNotExist':
                time.sleep(1)  # registration lag right after send_command
                continue
            raise
        if invocation.get('Status') in SSM_TERMINAL_STATUSES:
            return invocation
        time.sleep(min(CANCEL_VERIFY_INTERVAL_SECONDS,
                       max(1, deadline - time.time())))
    return None


def confirm_build_stopped(instance_id: str, deadline: float) -> bool:
    """Repeated pgrep verification until no build process is found or
    the confirmation window expires (Req 4.6, 4.9). Fail-closed: any
    verification failure or window expiry counts as not confirmed."""
    while time.time() < deadline:
        try:
            command_id = send_shell_command(instance_id,
                                            VERIFY_STOPPED_COMMANDS)
            invocation = wait_for_command(command_id, instance_id, deadline)
        except ClientError as e:
            logger.warning(f"Cancellation pgrep verification failed on "
                           f"{instance_id}: {e}")
            return False
        if invocation and invocation.get('Status') == 'Success':
            count = parse_build_process_count(
                invocation.get('StandardOutputContent') or '')
            if count == 0:
                return True
        if time.time() + CANCEL_VERIFY_INTERVAL_SECONDS < deadline:
            time.sleep(CANCEL_VERIFY_INTERVAL_SECONDS)
        else:
            break
    return False


def cancellation_ledger(job: Dict, dispatched: bool,
                        stop_verified: bool) -> Dict:
    """Terminal-effects ledger for one COMPLETED cancellation
    (build-fleet-execution-failures task 6.2): the conflict and
    fail-closed semantics above are unchanged — this only routes the
    already-verified cleanup through the same retryable ledger every
    other terminal path uses (Req 2.7/3.11).

    - the ``build_cancelled`` audit is written by this handler itself,
      so the audit effect is pre-advanced (one logical audit);
    - a queued job never dispatched compute: cleanup not applicable;
    - a running DEDICATED job cancels only after the SSM stop was
      pgrep-confirmed (fail-closed), so cleanup is verified done;
    - a running EPHEMERAL job still needs its idempotent runner
      termination: cleanup stays pending for the dispatcher's
      termination watchdog / effects reconciliation."""
    attempt_id = ((job.get('execution_attempt') or {}).get('attempt_id')
                  or 'no-attempt')
    ledger = build_reconciliation.plan_terminal_effects(
        job['build_job_id'], attempt_id,
        job.get('execution_mode') or '',
        cleanup_required=dispatched)
    ledger = build_reconciliation.advance_effect(
        ledger, build_reconciliation.EFFECT_AUDIT).ledger
    if dispatched and stop_verified and \
            job.get('execution_mode') == \
            build_domain.EXECUTION_MODE_DEDICATED:
        ledger = build_reconciliation.advance_effect(
            ledger, build_reconciliation.EFFECT_COMPUTE_CLEANUP).ledger
    return ledger


def apply_job_cancellation(build_job_id: str, expected_status: str,
                           ended_at: int,
                           terminal_effects: Optional[Dict] = None) -> bool:
    """Conditionally transition a Build_Job to cancelled (condition on
    the expected current status, so a stale writer can never resurrect a
    terminal job or double-transition). Returns False when the job moved
    in the meantime. ``terminal_effects`` (task 6.2) records the
    cancellation's effect ledger in the same conditional write."""
    update = 'SET #status = :cancelled, ended_at = :ended'
    values = {
        ':cancelled': build_domain.STATUS_CANCELLED,
        ':expected': expected_status,
        ':ended': ended_at,
    }
    if terminal_effects is not None:
        update += ', terminal_effects = :fx'
        values[':fx'] = terminal_effects
    try:
        jobs_table().update_item(
            Key={'build_job_id': build_job_id},
            UpdateExpression=update,
            ConditionExpression='#status = :expected',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == \
                'ConditionalCheckFailedException':
            return False
        raise


@require_builds_cancel()
def cancel_build(event: Dict, context: Any) -> Dict:
    """POST /builds/{id}/cancel — build_domain.decide_cancellation drives
    the outcome: queued cancels immediately (Req 4.5); running cancels
    only after the SSM stop is pgrep-confirmed within the 5-minute window
    (Req 4.6, 4.9, fail-closed); terminal (and provisioning) requests are
    rejected with 409, job unchanged (Req 4.8)."""
    build_job_id = (event.get('pathParameters') or {}).get('id')
    job = get_job(build_job_id) if build_job_id else None
    if not job:
        return error_response(404, 'BUILD_JOB_NOT_FOUND',
                              'Build job not found')

    user = get_user_from_event(event)
    status = job.get('status')

    # --- Queued: immediate cancellation + queue removal (Req 4.5) ---
    if status == build_domain.STATUS_QUEUED:
        decision = build_domain.decide_cancellation(status)
        if decision.cancelled and apply_job_cancellation(
                build_job_id, status, now_ms(),
                terminal_effects=cancellation_ledger(
                    job, dispatched=False, stop_verified=False)):
            log_audit_event(
                user_id=user['user_id'],
                action='build_cancelled',
                resource_type='build_job',
                resource_id=build_job_id,
                result='success',
                details={'status_at_request': status,
                         'removed_from_queue': decision.remove_from_queue})
            return create_response(200, {'job': get_job(build_job_id)})
        # Raced with a dispatch/transition: re-read and reject on the
        # job's current status.
        job = get_job(build_job_id) or job
        status = job.get('status')

    # --- Running: SSM stop + pgrep confirmation (Req 4.6, 4.9) ---
    if status in build_domain.RUNNING_STATUSES:
        instance_id, server_name = resolve_build_instance(job)
        stop_confirmed = False
        if instance_id:
            window = CANCEL_CONFIRMATION_WINDOW_SECONDS
            if context is not None and hasattr(
                    context, 'get_remaining_time_in_millis'):
                remaining = (context.get_remaining_time_in_millis()
                             - LAMBDA_TIME_RESERVE_MS) / 1000.0
                window = max(0, min(window, remaining))
            deadline = time.time() + window
            try:
                send_shell_command(instance_id, STOP_BUILD_COMMANDS)
                stop_confirmed = confirm_build_stopped(instance_id, deadline)
            except ClientError as e:
                logger.error(f"SSM stop for Build_Job {build_job_id} on "
                             f"{instance_id} failed: {e}")
                stop_confirmed = False
        else:
            logger.error(f"Build_Job {build_job_id} is {status} but no "
                         f"Build_Server instance could be resolved")

        decision = build_domain.decide_cancellation(
            status, stop_confirmed=stop_confirmed,
            server_id=server_name or job.get('server_id'))

        if decision.cancelled and apply_job_cancellation(
                build_job_id, status, now_ms(),
                terminal_effects=cancellation_ledger(
                    job, dispatched=True, stop_verified=True)):
            log_audit_event(
                user_id=user['user_id'],
                action='build_cancelled',
                resource_type='build_job',
                resource_id=build_job_id,
                result='success',
                details={'status_at_request': status,
                         'server': server_name,
                         'stop_confirmed': True})
            return create_response(200, {'job': get_job(build_job_id)})

        # Stop not confirmed within the window (or the conditional update
        # raced): the job keeps its status, the caller gets an error
        # naming the Build_Server, and the failed cancellation is
        # recorded in the Audit_Log (Req 4.9).
        log_audit_event(
            user_id=user['user_id'],
            action='build_cancelled',
            resource_type='build_job',
            resource_id=build_job_id,
            result='failure',
            details={'status_at_request': status,
                     'server': server_name,
                     'stop_confirmed': stop_confirmed,
                     'errors': [dict(e) for e in decision.errors]})
        errors = decision.errors or ({'rule': 'cancel_conflict',
                                      'message': 'The Build_Job changed '
                                                 'status during the '
                                                 'cancellation request.'},)
        return error_response(409, 'CANCELLATION_FAILED',
                              errors[0]['message'],
                              {'errors': [dict(e) for e in errors],
                               'server': server_name})

    # --- Terminal / provisioning: rejected unchanged (Req 4.8) ---
    decision = build_domain.decide_cancellation(status)
    return error_response(
        409, 'CANCELLATION_REJECTED',
        decision.errors[0]['message'] if decision.errors
        else f"The Build_Job cannot be cancelled in status '{status}'.",
        {'status': status,
         'errors': [dict(e) for e in decision.errors]})


# -------------------------------------------------- POST /builds/{id}/retry

@require_builds_submit()
def retry_build(event: Dict, context: Any) -> Dict:
    """POST /builds/{id}/retry — new queued Build_Job cloned from an
    interrupted job via build_domain.retry_clone: same Build_Target and
    execution mode (including the selected Dedicated_Build_Server), a
    retry_of reference, its own requester/submission time, and a fresh
    effective configuration snapshot (Req 3.6, 9.3)."""
    build_job_id = (event.get('pathParameters') or {}).get('id')
    source = get_job(build_job_id) if build_job_id else None
    if not source:
        return error_response(404, 'BUILD_JOB_NOT_FOUND',
                              'Build job not found')

    user = get_user_from_event(event)
    try:
        job = build_domain.retry_clone(
            interrupted_job=source,
            new_job_id=str(uuid.uuid4()),
            requested_by=user['user_id'],
            created_at=now_ms(),
            config_snapshot=effective_build_config(),
        )
    except ValueError as e:
        # Retry exists only for interrupted Build_Jobs (Req 3.6).
        return error_response(409, 'RETRY_NOT_AVAILABLE', str(e),
                              {'status': source.get('status')})

    stored = put_new_job(job)

    log_audit_event(
        user_id=user['user_id'],
        action='build_requested',
        resource_type='build_job',
        resource_id=stored['build_job_id'],
        result='success',
        details={
            'build_target': stored['build_target'],
            'execution_mode': stored['execution_mode'],
            'server_id': stored.get('server_id'),
            'retry_of': build_job_id,
            'submitted_at': stored['created_at'],
        })

    invoke_dispatcher([stored['build_job_id']])

    return create_response(201, {'job': stored})


# ------------------------------------------------------------------ routing

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
                    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }

        resource = event.get('resource', '')

        if resource == '/builds':
            if http_method == 'POST':
                return submit_build(event, context)
            if http_method == 'GET':
                return list_builds(event, context)
        elif resource == '/builds/{id}':
            if http_method == 'GET':
                return get_build(event, context)
        elif resource == '/builds/{id}/logs':
            if http_method == 'GET':
                return get_build_logs(event, context)
        elif resource == '/builds/{id}/cancel':
            if http_method == 'POST':
                return cancel_build(event, context)
        elif resource == '/builds/{id}/retry':
            if http_method == 'POST':
                return retry_build(event, context)
        elif resource == '/build-branches':
            if http_method == 'GET':
                return list_build_branches(event, context)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
