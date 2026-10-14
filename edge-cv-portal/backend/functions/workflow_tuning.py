"""
VLM/LLM Anomaly Tuning Lambda (spec: quality-prompt-tuning).

Serves every ``/workflow-tuning/anomaly/**`` route (Tuning_Sessions, sample
index, Labels, Candidates, Score_Runs, comparison and apply) and the
non-HTTP action branches the Bedrock_Scorer and the Device_Score_Job
dispatcher self-invoke themselves with
(``{"action": "execute_score_run", ...}`` / ``{"action": "poll_score_job",
...}``).

Landed here (task 6.1 — Requirements 1.2, 1.6, 3.*, 4.*, 5.1-5.5, 5.7,
9.1, 9.2, 9.4, 9.5, 10.2, 10.5):

    GET    /workflow-tuning/anomaly/workflows           overview
    POST   /workflow-tuning/anomaly/sessions            create-or-get
    GET    /workflow-tuning/anomaly/sessions/{id}       session view
    DELETE /workflow-tuning/anomaly/sessions/{id}       delete
    POST   .../sessions/{id}/refresh                    additive re-index
    GET    .../sessions/{id}/samples                    paged, filtered
    PUT    .../sessions/{id}/samples/labels             multi-set Labels
    PUT    .../sessions/{id}/synthetic-negatives        toggle
    POST   .../sessions/{id}/candidates                 create
    PUT    .../sessions/{id}/candidates/{cid}           edit
    DELETE .../sessions/{id}/candidates/{cid}           delete
    GET    .../candidates/{cid}/preview                 exact request text

Landed here (task 6.2 — Requirements 6.1, 6.3, 6.5-6.14, 7.1-7.5, 9.3,
9.6, 10.3, 10.4):

    POST   .../sessions/{id}/score-runs                 start a Score_Run
    PUT    .../sessions/{id}/selection                  select a Candidate
    GET    .../score-runs/{rid}                         progress + summary
    GET    .../score-runs/{rid}/outcomes                Sample_Outcomes
    POST   .../score-runs/{rid}/cancel                  cancellation
    GET    .../score-runs/{rid}/diff/{other}            differing samples

    action execute_score_run    one chunked Bedrock_Scorer step
    action poll_score_job       one Device_Score_Job poll step

Landed here (task 6.3 — Requirements 8.1-8.6, 11.4):

    POST   .../sessions/{id}/apply                      save a new version

Applying the session's selected Candidate saves a new Workflow_Definition
version in which the target node's ``prompt`` (``prompt_template`` for
``llm_inference``), ``system_prompt`` and ``max_tokens`` carry the
Candidate's Prompt_Set and nothing else changes: the previous latest
document is patched in place and stored through the designer save path's
own canonicalization, version allocation and version item
(``workflows.canonicalize_definition`` / ``put_definition`` /
``put_version_item``), so the version is indistinguishable from a manual
edit of the same three parameters (Requirements 8.1, 8.6). The Candidate
must have a completed Score_Run (Requirement 8.2), the target must still
be a Tunable_Node of the latest version (Requirement 8.4), and applying
never validates, packages or deploys (Requirement 8.5).

A ``bedrock_inference`` node is replayed by the Portal itself: the run is
admitted (at most one in progress per session, at most 600 planned
invocations), then executed in self-invoked steps of at most 100
invocations with 4 in flight, each outcome persisted as it completes and
the cursor advanced until the plan is exhausted. An ``llm_inference``
node's model is device-local, so its run is delivered as a
Device_Score_Job: a manifest under ``workflow-tuning/jobs/`` plus
``desired.jobs[jobId]`` on the device's ``dda-workflow-tuning`` named
shadow, with a poll step that ingests the device's outcome batches exactly
once and finalizes on the reported status or 15 minutes of silence.

Still ``501 not_implemented``: nothing — every designed route is served.

Authorization (Requirements 9.1, 9.2) runs FIRST in every workflow-scoped
route through the workflow handlers' own ``authorize_workflow_access``
semantics, re-stated here over this module's error envelope: a caller
without ``workflow:read`` on the owning Use_Case receives the uniform 404
(existence is never leaked across tenants), a reader without the
operation's permission a 403 with a denied-access audit entry.
``workflow:read`` for GETs, ``workflow:edit`` for mutations,
``workflow:save`` for apply.

Data boundaries (Requirements 9.4, 9.5): Sample_Store objects are read and
written only under the requesting Use_Case's bucket and the
``workflow-tuning/`` prefix, through the cross-account client the captures
listing uses; images are never stored in DynamoDB — the index carries the
device's sidecar (which itself carries no bytes) plus object keys, and the
browser sees images only through presigned URLs valid for 30 minutes.
"""
import base64
import json
import logging
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
sys.path.append('/opt/python')
from shared_utils import (  # noqa: E402
    Permission,
    create_response,
    get_usecase,
    get_usecase_client,
    get_user_from_event,
    log_audit_event,
    rbac_manager,
)
from workflow_core.anomaly_invocation import (  # noqa: E402
    BEDROCK_DEFAULT_REGION,
    BEDROCK_READ_TIMEOUT_SEC,
    DEFAULT_MAX_TOKENS,
    LABEL_NOK,
    LABEL_OK,
    NODE_TYPE_LLM_INFERENCE,
    build_bedrock_invocation,
    build_llm_invocation,
    categorize_outcome,
    is_tunable_node,
    parse_verdict,
    prompt_fingerprint,
    summarize_outcomes,
)
from workflow_core.catalog.nodes import get_node_type  # noqa: E402
import tuning_settings  # noqa: E402
# The designer save path (Requirements 8.1, 8.6, 11.4): applying a
# Candidate stores its new Workflow_Definition version through the very
# functions ``PUT /workflows/{id}`` uses — canonicalization, the portal-S3
# document, the immutable version item and its Custom_Node_Type pins — so
# an applied version is indistinguishable from a manual edit of the same
# three parameters. Nothing in this module reaches the workflow routes.
import workflows  # noqa: E402

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
#: Portal-account S3: the stored Workflow_Definition documents. Sample_Store
#: access always goes through the Use_Case's (possibly cross-account) client.
s3 = boto3.client('s3')

#: The single-table store of Tuning_Sessions, samples, Candidates,
#: Score_Runs and Sample_Outcomes (storage-stack.ts WorkflowTuningTable).
TUNING_TABLE = os.environ.get('WORKFLOW_TUNING_TABLE',
                              'dda-portal-workflow-tuning')
WORKFLOWS_TABLE = os.environ.get('WORKFLOWS_TABLE')
WORKFLOW_VERSIONS_TABLE = os.environ.get('WORKFLOW_VERSIONS_TABLE')
DEPLOYMENTS_TABLE = os.environ.get('DEPLOYMENTS_TABLE')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
WORKFLOWS_S3_PREFIX = os.environ.get('WORKFLOWS_S3_PREFIX', 'workflows')

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': (
        'Content-Type,Authorization,X-Amz-Date,X-Api-Key,'
        'X-Amz-Security-Token'),
    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
}

NOT_IMPLEMENTED_MESSAGE = (
    'VLM/LLM Anomaly Tuning does not serve this route.')

# --------------------------------------------------------------------------
# Bounds and fixed values
# --------------------------------------------------------------------------

#: Newest Tuning_Samples an index holds; the remainder is reported as
#: ``lastRefresh.beyondBound`` (Requirement 3.1).
SAMPLE_INDEX_BOUND = 2000

#: Objects a single Sample_Store listing walks before it reports itself
#: truncated. Bounds Lambda memory on a Use_Case with a very large store;
#: well above the index bound's own needs (3 objects per sample).
MAX_LISTED_OBJECTS = 60000

#: Presigned sample-image URL lifetime (Requirement 4.8: at most 30
#: minutes) — the value datasets/captures already use.
PRESIGNED_URL_EXPIRY_SECONDS = 1800

#: Sidecar reads and existence checks are IO-bound; a small pool keeps a
#: refresh of a few thousand samples inside the request budget.
S3_READ_THREADS = 8

DEFAULT_SAMPLE_PAGE_SIZE = 50
MAX_SAMPLE_PAGE_SIZE = 200

#: Labels a Workflow_Author may set (Requirement 4.2). ``None`` clears the
#: Label, returning the sample to "unlabelled" (Requirement 4.3).
LABELS = ('OK', 'NOK', 'EXCLUDE')

#: The Baseline_Candidate's fixed id: one per session, always present,
#: read-only (Requirement 5.1).
BASELINE_CANDIDATE_ID = 'baseline'
BASELINE_CANDIDATE_NAME = 'Baseline (deployed prompt)'

#: Below this budget a verdict answer is likely to be truncated, which the
#: Verdict_Parser rejects (Requirement 5.5).
MIN_SAFE_MAX_TOKENS = 64

#: Never sent anywhere: the preview builds a real invocation so the text it
#: shows is the text the Invocation_Builder would send (Requirement 5.3),
#: and an invocation needs image bytes. These four bytes are an empty JPEG's
#: start/end markers.
PREVIEW_PLACEHOLDER_IMAGE = b'\xff\xd8\xff\xd9'

#: Object suffixes the device writes per Tuning_Sample (sample_export.py).
SIDECAR_SUFFIX = '.json'
INPUT_SUFFIX = '.input.jpg'
REFERENCE_SUFFIX = '.reference.jpg'

#: Separator between a Synthetic_Negative's source sample id and the
#: sibling node whose Reference_Image it borrows. Not '/' (which separates
#: the device name from the execution id inside a sample id) and not '#'
#: (which separates the sort-key parts).
SYNTHETIC_MARKER = '|syn|'

# --------------------------------------------------------------------------
# Score_Run bounds and fixed values (Requirements 6.*, 10.3, 10.4)
# --------------------------------------------------------------------------

#: A Score_Run is bounded to this many invocations — labelled,
#: non-excluded samples × repeats (Requirement 6.13).
MAX_PLANNED_INVOCATIONS = 600

#: Repeats per sample (Requirement 6.7), defaulting to 1.
MIN_REPEATS = 1
MAX_REPEATS = 3
DEFAULT_REPEATS = 1

#: Invocations one self-invoked Bedrock execution step issues at most
#: (Lambda-safe chunking, Requirements 6.8, 10.4).
CHUNK_INVOCATIONS = 100

#: Concurrent Bedrock invocations inside one step (Requirement 6.8).
SCORE_THREADS = 4

#: A run that has not finished within this budget is finalized as failed
#: on the next execution step (Requirement 10.4).
RUN_STALE_SECONDS = 3600

#: A Device_Score_Job without progress for this long is finalized as
#: failed (Requirement 6.12).
JOB_SILENCE_SECONDS = 900

#: Delay between Device_Score_Job poll steps (design: self-invoke every
#: 30 s). Read at call time so tests can drive polling without waiting.
POLL_INTERVAL_SECONDS = 30

#: Sample_Outcome retention (design: outcomes carry a TTL of 90 days).
OUTCOME_TTL_SECONDS = 90 * 24 * 3600

#: Score_Runs kept per Candidate; older runs and their Sample_Outcomes are
#: deleted (Requirement 10.3).
MAX_RUNS_PER_CANDIDATE = 20

#: Sample_Outcomes served per page.
DEFAULT_OUTCOME_PAGE_SIZE = 200
MAX_OUTCOME_PAGE_SIZE = 1000

#: Score_Run statuses (design data model).
RUN_RUNNING = 'running'
RUN_COMPLETED = 'completed'
RUN_CANCELLED = 'cancelled'
RUN_FAILED = 'failed'
TERMINAL_RUN_STATUSES = (RUN_COMPLETED, RUN_CANCELLED, RUN_FAILED)

#: Scorer of a run: the Portal's Bedrock_Scorer or a Device_Score_Job.
MODE_BEDROCK = 'bedrock'
MODE_DEVICE = 'device'

#: The sort key of the per-session run lock: at most one Score_Run may be
#: in progress per Tuning_Session (Requirement 6.10), enforced by a
#: conditional write on this single item.
RUNLOCK_SK = 'RUNLOCK'

#: The named shadow carrying Device_Score_Jobs, and its job map key —
#: exactly what src/backend/workflow_engine/tuning/job_runner.py reads.
TUNING_SHADOW_NAME = 'dda-workflow-tuning'
SHADOW_JOBS_KEY = 'jobs'

#: Device-reported job statuses (job_runner.py).
JOB_STATUS_QUEUED = 'queued'
JOB_STATUS_RUNNING = 'running'
JOB_STATUS_COMPLETED = 'completed'
JOB_STATUS_FAILED = 'failed'
JOB_STATUS_CANCELLED = 'cancelled'

#: Deployment statuses that still put a workflow on a device — the
#: workflows module's own set, restated (workflows.py).
ACTIVE_DEPLOYMENT_STATUSES = ('ACTIVE', 'COMPLETED', 'IN_PROGRESS',
                              'PENDING', 'QUEUED', 'DEPLOYED')

#: The self-invoked action branches (design: the Bedrock_Scorer chunk and
#: the Device_Score_Job poll step).
ACTION_EXECUTE_SCORE_RUN = 'execute_score_run'
ACTION_POLL_SCORE_JOB = 'poll_score_job'


class TuningError(Exception):
    """A request that cannot be served, carrying its HTTP shape."""

    def __init__(self, status_code: int, code: str, message: str,
                 details: Optional[Dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}

    def response(self) -> Dict:
        return error_response(self.status_code, self.code, self.message,
                              self.details)


# ==========================================================================
# Responses, conversions, keys
# ==========================================================================

def error_response(status_code: int, code: str, message: str,
                   details: Optional[Dict] = None) -> Dict:
    """The workflow handlers' error envelope: {error: {code, message,
    details}} (workflows.py)."""
    return create_response(status_code, {
        'error': {'code': code, 'message': message, 'details': details or {}},
    })


def not_found_response() -> Dict:
    """The workflow handlers' uniform 404, which never confirms whether a
    workflow (or session) exists (Requirement 9.1)."""
    return error_response(404, 'WORKFLOW_NOT_FOUND', 'Workflow not found')


def decimal_to_native(obj: Any) -> Any:
    """DynamoDB Decimals to native Python numbers (workflows.py)."""
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    if isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decimal_to_native(i) for i in obj]
    return obj


def to_dynamo(obj: Any) -> Any:
    """A document as DynamoDB accepts it: floats (e.g. a recorded
    ``confidence``) become Decimals, everything else is unchanged."""
    return json.loads(json.dumps(obj), parse_float=Decimal)


def now_ms() -> int:
    return int(time.time() * 1000)


def now_s() -> int:
    return int(time.time())


def session_pk(session_id: str) -> str:
    return f"SESSION#{session_id}"


def sample_sk(sample_id: str) -> str:
    return f"SAMPLE#{sample_id}"


def candidate_sk(candidate_id: str) -> str:
    return f"CAND#{candidate_id}"


def run_pk(run_id: str) -> str:
    return f"RUN#{run_id}"


def run_sk(run_id: str) -> str:
    return f"RUN#{run_id}"


def outcome_sk(sample_id: str, repeat: Any) -> str:
    """One Sample_Outcome's sort key inside its run's own partition."""
    return f"OUT#{sample_id}#{int(repeat)}"


def run_lock_key(session_id: str) -> Dict[str, str]:
    """The per-session run lock item's key (Requirement 6.10)."""
    return {'pk': session_pk(session_id), 'sk': RUNLOCK_SK}


def run_pointer_key(run_id: str) -> Dict[str, str]:
    """The run's by-id lookup item, in the run's own partition: the
    ``.../score-runs/{rid}`` routes carry no session id, and the run item
    itself lives under its session's partition. Deleted with the rest of
    the partition whenever the run's outcomes are deleted."""
    return {'pk': run_pk(run_id), 'sk': 'META'}


def lookup_key(workflow_id: str, node_id: str) -> Dict[str, str]:
    """The uniqueness item pinning one Tuning_Session per (workflow, node)
    (Requirement 10.2)."""
    return {'pk': f"WF#{workflow_id}", 'sk': f"NODE#{node_id}"}


def table():
    return dynamodb.Table(TUNING_TABLE)


def query_items(pk: str, sk_prefix: Optional[str] = None,
                projection: Optional[str] = None) -> List[Dict]:
    """Every item of one partition (optionally one sort-key prefix)."""
    kwargs: Dict[str, Any] = {
        'KeyConditionExpression': 'pk = :pk',
        'ExpressionAttributeValues': {':pk': pk},
    }
    if sk_prefix:
        kwargs['KeyConditionExpression'] += ' AND begins_with(sk, :sk)'
        kwargs['ExpressionAttributeValues'][':sk'] = sk_prefix
    if projection:
        kwargs['ProjectionExpression'] = projection
    items: List[Dict] = []
    store = table()
    while True:
        response = store.query(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return items


def delete_items(keys: List[Dict[str, str]]) -> int:
    """Delete a batch of items by key; returns the number deleted."""
    if not keys:
        return 0
    store = table()
    with store.batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key=key)
    return len(keys)


# ==========================================================================
# Client seams
#
# Every client this module builds outside the Portal's own tables goes
# through one of these functions, so a test can substitute a recording
# double without patching boto3 itself.
# ==========================================================================

def bedrock_client(region: str):
    """The Bedrock runtime client the Bedrock_Scorer replays through.

    The executor's transport rules, unchanged (Requirement 6.3): the
    node's configured region, the executor's read timeout and no
    automatic retries — a throttled or slow invocation becomes one
    ``invocation_error`` outcome instead of silently issuing the same
    request again.
    """
    return boto3.client(
        'bedrock-runtime', region_name=region,
        config=BotoConfig(read_timeout=BEDROCK_READ_TIMEOUT_SEC,
                          retries={'max_attempts': 1}))


def iot_data_client(usecase: Dict):
    """The Use_Case's ``iot-data`` client — the assumed-role path
    ``deliver_camera_bindings`` uses to write a device's named shadow."""
    return get_usecase_client('iot-data', usecase,
                              session_name='tuning-score-job')


def dispatch_action(payload: Dict) -> None:
    """Asynchronously self-invoke this Lambda with an action payload.

    The Bedrock execution steps and the Device_Score_Job poll steps run
    outside API Gateway's 29 s integration bound and re-invoke themselves
    with the next cursor (design: the labeling preview executor's
    pattern). A dispatch failure is raised to the caller, which marks the
    run failed rather than leaving it running forever.
    """
    function_name = (os.environ.get('WORKFLOW_TUNING_FUNCTION_NAME')
                     or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'))
    if not function_name:
        raise TuningError(500, 'DISPATCH_UNAVAILABLE',
                          'This deployment cannot run scoring steps: no '
                          'Lambda function name is configured')
    boto3.client('lambda').invoke(
        FunctionName=function_name,
        InvocationType='Event',
        Payload=json.dumps(payload).encode('utf-8'))


# ==========================================================================
# Authorization (Requirements 9.1, 9.2)
# ==========================================================================

def has_workflow_permission(user: Dict, usecase_id: str,
                            permission: Permission) -> bool:
    return rbac_manager.has_permission(user['user_id'], usecase_id,
                                       permission, user_info=user)


def authorize(user: Dict, event: Dict, usecase_id: str,
              permission: Permission) -> None:
    """Authorize an operation on a Use_Case's workflow, or raise.

    ``authorize_workflow_access``'s rule, unchanged: without
    ``workflow:read`` the caller gets the uniform 404 (no cross-tenant
    existence leak); a reader lacking the operation's permission gets an
    audited 403. Called before any other validation (Requirement 9.2).
    """
    if not has_workflow_permission(user, usecase_id, Permission.WORKFLOW_READ):
        raise TuningError(404, 'WORKFLOW_NOT_FOUND', 'Workflow not found')
    if permission != Permission.WORKFLOW_READ and not has_workflow_permission(
            user, usecase_id, permission):
        raise _forbidden(user, event, usecase_id, permission)


def _forbidden(user: Dict, event: Dict, usecase_id: str,
               permission: Permission) -> TuningError:
    """Audit the denial and build the 403 error."""
    log_audit_event(
        user_id=user['user_id'],
        action='unauthorized_access',
        resource_type='workflow',
        resource_id=event.get('resource', 'unknown'),
        result='denied',
        details={
            'required_permissions': [permission.value],
            'usecase_id': usecase_id,
            'method': event.get('httpMethod'),
            'path': event.get('path'),
        },
    )
    return TuningError(403, 'FORBIDDEN', 'Insufficient permissions', {
        'required_permissions': [permission.value],
        'usecase_id': usecase_id,
    })


# ==========================================================================
# Workflow_Definition access
# ==========================================================================

def get_workflow_item(workflow_id: str) -> Optional[Dict]:
    response = dynamodb.Table(WORKFLOWS_TABLE).get_item(
        Key={'workflow_id': workflow_id})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def require_workflow(workflow_id: str) -> Dict:
    item = get_workflow_item(workflow_id)
    if not item:
        raise TuningError(404, 'WORKFLOW_NOT_FOUND', 'Workflow not found')
    return item


def load_latest_definition(workflow_item: Dict) -> Tuple[int, Dict]:
    """``(version, definition_document)`` of the workflow's latest version."""
    workflow_id = workflow_item['workflow_id']
    version = int(workflow_item.get('latest_version') or 1)
    response = dynamodb.Table(WORKFLOW_VERSIONS_TABLE).get_item(
        Key={'workflow_id': workflow_id, 'version': version})
    version_item = response.get('Item')
    if not version_item:
        raise TuningError(404, 'VERSION_NOT_FOUND',
                          f'Version {version} not found for workflow')
    try:
        body = s3.get_object(Bucket=PORTAL_ARTIFACTS_BUCKET,
                             Key=version_item['s3_definition_key'])
        document = json.loads(body['Body'].read().decode('utf-8'))
    except (ClientError, ValueError, KeyError) as exc:
        logger.error(f"Failed to load definition for {workflow_id} "
                     f"v{version}: {exc}")
        raise TuningError(500, 'DEFINITION_LOAD_FAILED',
                          'Stored workflow definition could not be loaded')
    return version, document


def definition_nodes(document: Any) -> List[Dict]:
    nodes = document.get('nodes') if isinstance(document, dict) else None
    return [n for n in (nodes or []) if isinstance(n, dict)]


def find_node(document: Any, node_id: str) -> Optional[Dict]:
    for node in definition_nodes(document):
        if node.get('id') == node_id:
            return node
    return None


def node_is_tunable(node: Optional[Dict]) -> bool:
    """The shared Tunable_Node rule applied to a definition node
    (Requirement 1.5, Property 1) — one function everywhere."""
    if not isinstance(node, dict):
        return False
    parameters = node.get('parameters')
    parameters = parameters if isinstance(parameters, dict) else {}
    return is_tunable_node(node.get('type'), parameters.get('anomaly_mode'))


def tunable_nodes(document: Any) -> List[Dict]:
    return [n for n in definition_nodes(document) if node_is_tunable(n)]


def node_model(node: Dict) -> Optional[str]:
    """The node's model parameter, whatever its type calls it."""
    parameters = node.get('parameters') or {}
    if node.get('type') == NODE_TYPE_LLM_INFERENCE:
        return parameters.get('modelName')
    return parameters.get('model')


def prompt_key_for(node_type: Any) -> str:
    """The Prompt_Set's prompt parameter name for a node type:
    ``prompt_template`` for ``llm_inference``, ``prompt`` otherwise."""
    return ('prompt_template' if node_type == NODE_TYPE_LLM_INFERENCE
            else 'prompt')


def prompt_set_of_node(node: Dict) -> Dict[str, Any]:
    """The node's Prompt_Set — the three tunable parameters."""
    parameters = node.get('parameters') or {}
    return {
        'prompt': parameters.get(prompt_key_for(node.get('type'))) or '',
        'systemPrompt': parameters.get('system_prompt') or '',
        'maxTokens': parameters.get('max_tokens'),
    }


def max_tokens_bounds(node_type: Any) -> Tuple[int, Optional[int]]:
    """``(min, max)`` of the node type's catalog ``max_tokens`` constraint
    (Requirement 5.2); ``max`` is None when the type declares none."""
    try:
        descriptor = get_node_type(str(node_type))
    except Exception:  # pragma: no cover - unknown type
        descriptor = None
    if descriptor is not None:
        for parameter in descriptor.parameters:
            if parameter.name == 'max_tokens':
                constraints = parameter.constraints or {}
                return (int(constraints.get('min', 1)),
                        constraints.get('max'))
    return 1, None


def invocation_parameters(node: Dict, prompt_set: Dict[str, Any]
                          ) -> Dict[str, Any]:
    """The parameter mapping the Invocation_Builder is called with: the
    Tunable_Node's Node_Parameters with a Candidate's Prompt_Set laid over
    them (Requirement 6.1). Used by the preview here and by the scorers in
    tasks 6.2/6.3 so all three paths merge identically."""
    parameters = dict(node.get('parameters') or {})
    parameters[prompt_key_for(node.get('type'))] = prompt_set.get('prompt') or ''
    parameters['system_prompt'] = prompt_set.get('systemPrompt') or ''
    if prompt_set.get('maxTokens') is not None:
        parameters['max_tokens'] = prompt_set['maxTokens']
    return parameters


# ==========================================================================
# Sample_Store access (Requirement 9.4)
# ==========================================================================

def sample_store(usecase: Dict) -> Tuple[Any, str]:
    """``(s3_client, bucket)`` for the Use_Case's Sample_Store, through the
    cross-account mechanism the captures listing uses. Raises when no
    bucket resolves for the Use_Case."""
    bucket = tuning_settings.sample_store_bucket(usecase)
    if not bucket:
        raise TuningError(
            400, 'SAMPLE_STORE_UNAVAILABLE',
            'No inference results bucket is configured for this use case, '
            'so no Sample_Store exists yet')
    client = get_usecase_client('s3', usecase, session_name='tuning-samples')
    return client, bucket


def workflow_samples_prefix(workflow_id: str) -> str:
    return f"{tuning_settings.SAMPLE_STORE_PREFIX}{workflow_id}/"


def node_samples_prefix(workflow_id: str, node_id: str) -> str:
    return f"{workflow_samples_prefix(workflow_id)}{node_id}/"


def session_runs_prefix(session_id: str) -> str:
    """Where a session's Score_Run outcome batches live — the only objects
    a session owns (Requirements 9.6, 10.5)."""
    return f"{tuning_settings.SESSION_STORE_PREFIX}{session_id}/"


def list_objects(client, bucket: str, prefix: str) -> Tuple[Dict[str, int], bool]:
    """``({key: size}, truncated)`` for everything under ``prefix``."""
    keys: Dict[str, int] = {}
    truncated = False
    token = None
    while True:
        kwargs = {'Bucket': bucket, 'Prefix': prefix, 'MaxKeys': 1000}
        if token:
            kwargs['ContinuationToken'] = token
        response = client.list_objects_v2(**kwargs)
        for obj in response.get('Contents', []) or []:
            keys[obj['Key']] = obj.get('Size', 0)
            if len(keys) >= MAX_LISTED_OBJECTS:
                return keys, True
        if not response.get('IsTruncated'):
            break
        token = response.get('NextContinuationToken')
        if not token:
            break
    return keys, truncated


def presign(client, bucket: str, key: Optional[str]) -> Optional[str]:
    """A 30-minute presigned GET URL, or None (Requirement 4.8)."""
    if not key:
        return None
    try:
        return client.generate_presigned_url(
            'get_object', Params={'Bucket': bucket, 'Key': key},
            ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS)
    except Exception as exc:  # pragma: no cover - botocore signing failure
        logger.warning(f"Failed to presign {key}: {exc}")
        return None


def sample_id_from_key(key: str, prefix: str) -> Optional[str]:
    """``{thingName}/{executionId}`` of a sidecar key under a node prefix,
    or None when the key is not one of the device's sidecars."""
    if not key.startswith(prefix) or not key.endswith(SIDECAR_SUFFIX):
        return None
    relative = key[len(prefix):-len(SIDECAR_SUFFIX)]
    parts = relative.split('/')
    if len(parts) != 2 or not all(parts):
        return None
    return relative


def synthetic_sample_id(source_sample_id: str, sibling_node_id: str) -> str:
    return f"{source_sample_id}{SYNTHETIC_MARKER}{sibling_node_id}"


# ==========================================================================
# Session items
# ==========================================================================

def get_session(session_id: str) -> Optional[Dict]:
    response = table().get_item(Key={'pk': session_pk(session_id),
                                     'sk': 'META'})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def require_session(session_id: str) -> Dict:
    session = get_session(session_id)
    if not session:
        # The uniform 404: a session id of another tenant is
        # indistinguishable from one that does not exist.
        raise TuningError(404, 'WORKFLOW_NOT_FOUND', 'Workflow not found')
    return session


def session_context(user: Dict, event: Dict, session_id: str,
                    permission: Permission) -> Tuple[Dict, Dict]:
    """``(session, workflow_item)`` after authorizing the operation on the
    session's workflow. Authorization happens before anything else is read
    or validated (Requirement 9.2)."""
    session = require_session(session_id)
    workflow_item = get_workflow_item(session['workflowId'])
    if not workflow_item:
        raise TuningError(404, 'WORKFLOW_NOT_FOUND', 'Workflow not found')
    authorize(user, event, workflow_item['usecase_id'], permission)
    return session, workflow_item


def session_summary(session: Dict) -> Dict:
    """The public shape of a Tuning_Session item."""
    return {
        'sessionId': session['sessionId'],
        'usecaseId': session.get('usecaseId'),
        'workflowId': session.get('workflowId'),
        'nodeId': session.get('nodeId'),
        'nodeType': session.get('nodeType'),
        'baselineVersion': session.get('baselineVersion'),
        'baselineCandidateId': session.get('baselineCandidateId'),
        'baselineFingerprint': session.get('baselineFingerprint'),
        'selectedCandidateId': session.get('selectedCandidateId'),
        'syntheticNegativesEnabled': bool(
            session.get('syntheticNegativesEnabled')),
        'lastRefresh': session.get('lastRefresh'),
        'latestTuningResult': session.get('latestTuningResult'),
        'createdBy': session.get('createdBy'),
        'createdAt': session.get('createdAt'),
        'updatedAt': session.get('updatedAt'),
    }


def candidate_summary(item: Dict) -> Dict:
    return {
        'candidateId': item.get('candidateId'),
        'name': item.get('name'),
        'prompt': item.get('prompt'),
        'systemPrompt': item.get('systemPrompt'),
        'maxTokens': item.get('maxTokens'),
        'isBaseline': bool(item.get('isBaseline')),
        'baselineVersion': item.get('baselineVersion'),
        'fingerprint': item.get('fingerprint'),
        'createdAt': item.get('createdAt'),
        'updatedAt': item.get('updatedAt'),
    }


def candidate_fingerprint(node_type: Any, prompt_set: Dict[str, Any]) -> str:
    """The Prompt_Set's fingerprint, keyed the way the device fingerprints
    the deployed node's Prompt_Set so the two are comparable
    (Requirement 3.6)."""
    return prompt_fingerprint({
        prompt_key_for(node_type): prompt_set.get('prompt') or '',
        'system_prompt': prompt_set.get('systemPrompt') or '',
        'max_tokens': prompt_set.get('maxTokens'),
    })


def get_candidate(session_id: str, candidate_id: str) -> Optional[Dict]:
    response = table().get_item(Key={'pk': session_pk(session_id),
                                     'sk': candidate_sk(candidate_id)})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def put_baseline_candidate(session_id: str, node: Dict, version: int,
                           user: Dict) -> Dict:
    """Write (or refresh) the read-only Baseline_Candidate from the latest
    Workflow_Definition version (Requirement 5.1). Prior Score_Runs are
    untouched, so superseded baselines stay visible as history."""
    prompt_set = prompt_set_of_node(node)
    timestamp = now_ms()
    existing = get_candidate(session_id, BASELINE_CANDIDATE_ID)
    item = {
        'pk': session_pk(session_id),
        'sk': candidate_sk(BASELINE_CANDIDATE_ID),
        'candidateId': BASELINE_CANDIDATE_ID,
        'name': BASELINE_CANDIDATE_NAME,
        'prompt': prompt_set['prompt'],
        'systemPrompt': prompt_set['systemPrompt'],
        'maxTokens': prompt_set['maxTokens'],
        'isBaseline': True,
        'baselineVersion': version,
        'fingerprint': candidate_fingerprint(node.get('type'), prompt_set),
        'createdAt': (existing or {}).get('createdAt', timestamp),
        'createdBy': (existing or {}).get('createdBy', user['user_id']),
        'updatedAt': timestamp,
    }
    table().put_item(Item=to_dynamo(item))
    return item


def refresh_baseline(session: Dict, node: Dict, version: int,
                     user: Dict) -> Dict:
    """Keep the session's Baseline_Candidate on the latest version
    (Requirement 5.1). Returns the (possibly updated) session item."""
    if session.get('baselineVersion') == version:
        return session
    baseline = put_baseline_candidate(session['sessionId'], node, version, user)
    updated = table().update_item(
        Key={'pk': session_pk(session['sessionId']), 'sk': 'META'},
        UpdateExpression=(
            'SET baselineVersion = :v, baselineFingerprint = :f, '
            'nodeType = :t, updatedAt = :u'),
        ExpressionAttributeValues={
            ':v': version, ':f': baseline['fingerprint'],
            ':t': node.get('type'), ':u': now_ms()},
        ReturnValues='ALL_NEW',
    )
    return decimal_to_native(updated['Attributes'])


# ==========================================================================
# Sample index (Requirement 3)
# ==========================================================================

def sample_items(session_id: str) -> List[Dict]:
    return [decimal_to_native(i)
            for i in query_items(session_pk(session_id), 'SAMPLE#')]


def _read_sidecar(client, bucket: str, key: str) -> Tuple[str, Any]:
    try:
        body = client.get_object(Bucket=bucket, Key=key)['Body'].read()
        return key, json.loads(body.decode('utf-8'))
    except Exception as exc:  # ClientError, UnicodeError, ValueError
        logger.info(f"Unreadable Tuning_Sample sidecar {key}: {exc}")
        return key, None


def index_samples(session: Dict, client, bucket: str) -> Dict[str, Any]:
    """Index the Sample_Store into the session, additively.

    The refresh contract (Requirement 3.1-3.6, Property 5):

    - every readable sample under the node's prefix that is not indexed yet
      is indexed, bounded to the newest :data:`SAMPLE_INDEX_BOUND` by
      ``exportedAt`` across the union of indexed and discovered samples;
      the remainder is reported as ``beyondBound``;
    - the device's sidecar is recorded VERBATIM (it carries no image bytes
      by construction) beside the derived index attributes; no image bytes
      are written (Requirements 3.2, 9.5);
    - a sample whose sidecar is unreadable/malformed or whose Input_Image
      object is missing is omitted and counted by reason (Requirement 3.5);
    - a newly indexed sample whose input hash equals an earlier sample's is
      marked ``duplicateOf`` that earliest sample (Requirement 3.3);
    - a sample whose prompt fingerprint differs from the
      Baseline_Candidate's is flagged ``differentPrompt`` (Requirement 3.6;
      the flag is also recomputed against the CURRENT baseline whenever
      samples are served, so a later baseline refresh cannot leave a stale
      flag behind);
    - pre-existing samples and their Labels are never rewritten
      (Requirement 3.4): every write is conditional on the item not
      existing.
    """
    session_id = session['sessionId']
    prefix = node_samples_prefix(session['workflowId'], session['nodeId'])
    listing, truncated = list_objects(client, bucket, prefix)

    existing = sample_items(session_id)
    existing_by_id = {item['sampleId']: item for item in existing}

    skipped: Dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    discovered: List[Tuple[str, str]] = []   # (sample_id, sidecar key)
    for key in sorted(listing):
        sample_id = sample_id_from_key(key, prefix)
        if sample_id is None:
            continue
        if sample_id in existing_by_id:
            continue
        discovered.append((sample_id, key))

    documents: Dict[str, Any] = {}
    if discovered:
        with ThreadPoolExecutor(max_workers=S3_READ_THREADS) as pool:
            for key, document in pool.map(
                    lambda k: _read_sidecar(client, bucket, k),
                    [key for _, key in discovered]):
                documents[key] = document

    candidates: List[Tuple[int, str, str, Dict]] = []
    for sample_id, key in discovered:
        document = documents.get(key)
        if not isinstance(document, dict):
            skip('unreadable')
            continue
        input_block = document.get('input')
        input_key = (input_block or {}).get('key') if isinstance(
            input_block, dict) else None
        if not input_key:
            skip('malformed')
            continue
        if input_key not in listing:
            skip('missing_input')
            continue
        exported_at = document.get('exportedAt')
        exported_at = int(exported_at) if isinstance(
            exported_at, (int, float)) else 0
        candidates.append((exported_at, sample_id, key, document))

    # The newest-N window over indexed and newly discovered samples.
    window: List[Tuple[int, str]] = [
        (int(item.get('exportedAt') or 0), item['sampleId'])
        for item in existing if not item.get('synthetic')
    ]
    window.extend((exported_at, sample_id)
                  for exported_at, sample_id, _key, _doc in candidates)
    window.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    in_bound = {sample_id for _at, sample_id in window[:SAMPLE_INDEX_BOUND]}

    # Earliest sample per input hash, for duplicate marking.
    earliest: Dict[str, Tuple[int, str]] = {}
    for item in existing:
        digest = item.get('inputSha256')
        if digest:
            entry = (int(item.get('exportedAt') or 0), item['sampleId'])
            if digest not in earliest or entry < earliest[digest]:
                earliest[digest] = entry

    baseline_fingerprint = session.get('baselineFingerprint')
    indexed = 0
    beyond_bound = 0
    store = table()
    for exported_at, sample_id, key, document in sorted(
            candidates, key=lambda entry: (entry[0], entry[1])):
        if sample_id not in in_bound:
            beyond_bound += 1
            continue
        digest = (document.get('input') or {}).get('sha256')
        duplicate_of = None
        if digest:
            entry = (exported_at, sample_id)
            previous = earliest.get(digest)
            if previous is not None and previous < entry:
                duplicate_of = previous[1]
            elif previous is None or entry < previous:
                earliest[digest] = entry
        item = build_sample_item(session_id, sample_id, key, document,
                                 duplicate_of, baseline_fingerprint)
        try:
            store.put_item(Item=to_dynamo(item),
                           ConditionExpression='attribute_not_exists(sk)')
            indexed += 1
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') != \
                    'ConditionalCheckFailedException':
                raise
            # Indexed concurrently: leave the existing item (and its Label)
            # exactly as it is.
            skip('already_indexed')

    summary = {
        'at': now_s(),
        'indexed': indexed,
        'skipped': skipped,
        'beyondBound': beyond_bound,
        'discovered': len(discovered),
        'listingTruncated': truncated,
    }
    return summary


def build_sample_item(session_id: str, sample_id: str, sidecar_key: str,
                      document: Dict, duplicate_of: Optional[str],
                      baseline_fingerprint: Optional[str]) -> Dict:
    """The indexed Sample item: the sidecar verbatim plus the derived
    attributes the Portal filters and scores on. No image bytes
    (Requirements 3.2, 9.5)."""
    input_block = document.get('input') or {}
    reference_block = document.get('reference')
    reference_block = reference_block if isinstance(
        reference_block, dict) else None
    recorded = document.get('recorded')
    recorded = recorded if isinstance(recorded, dict) else {}
    thing_name, _, execution_id = sample_id.partition('/')
    fingerprint = document.get('promptFingerprint')
    return {
        'pk': session_pk(session_id),
        'sk': sample_sk(sample_id),
        'sampleId': sample_id,
        'sidecar': document,
        'sidecarKey': sidecar_key,
        'workflowId': document.get('workflowId'),
        'nodeId': document.get('nodeId'),
        'nodeType': document.get('nodeType'),
        'thingName': document.get('thingName') or thing_name,
        'executionId': document.get('executionId') or execution_id,
        'version': document.get('version'),
        'exportedAt': document.get('exportedAt'),
        'source': document.get('source'),
        'inputKey': input_block.get('key'),
        'inputSha256': input_block.get('sha256'),
        'referenceKey': (reference_block or {}).get('key'),
        'recordedIsAnomalous': recorded.get('isAnomalous'),
        'promptFingerprint': fingerprint,
        'detectionId': document.get('detectionId'),
        'detectionSlot': document.get('detectionSlot'),
        'duplicateOf': duplicate_of,
        'differentPrompt': bool(
            baseline_fingerprint and fingerprint
            and fingerprint != baseline_fingerprint),
        'synthetic': False,
        'indexedAt': now_s(),
    }


def sample_view(item: Dict, baseline_fingerprint: Optional[str],
                client=None, bucket: Optional[str] = None,
                urls: bool = False, include_metadata: bool = False,
                unavailable: Optional[bool] = None) -> Dict:
    """The public shape of an indexed Tuning_Sample (Requirement 4.1)."""
    sidecar = item.get('sidecar') or {}
    recorded = sidecar.get('recorded') or {}
    input_block = dict(sidecar.get('input') or {})
    reference_block = sidecar.get('reference')
    reference_block = dict(reference_block) if isinstance(
        reference_block, dict) else None
    if item.get('synthetic'):
        # A Synthetic_Negative borrows a sibling node's Reference_Image;
        # its own recorded verdict is the source sample's.
        input_block['key'] = item.get('inputKey')
        reference_block = {'key': item.get('referenceKey')}
    if urls:
        input_block['url'] = presign(client, bucket, input_block.get('key'))
        if reference_block:
            reference_block['url'] = presign(client, bucket,
                                             reference_block.get('key'))
    fingerprint = item.get('promptFingerprint')
    view = {
        'sampleId': item.get('sampleId'),
        'workflowId': item.get('workflowId'),
        'nodeId': item.get('nodeId'),
        'nodeType': item.get('nodeType'),
        'thingName': item.get('thingName'),
        'executionId': item.get('executionId'),
        'version': item.get('version'),
        'exportedAt': item.get('exportedAt'),
        'source': item.get('source'),
        'label': item.get('label'),
        'duplicateOf': item.get('duplicateOf'),
        'differentPrompt': bool(fingerprint and baseline_fingerprint
                                and fingerprint != baseline_fingerprint),
        'synthetic': bool(item.get('synthetic')),
        'sourceSampleId': item.get('sourceSampleId'),
        'siblingNodeId': item.get('siblingNodeId'),
        'detectionId': item.get('detectionId'),
        'detectionSlot': item.get('detectionSlot'),
        'promptFingerprint': fingerprint,
        'recorded': {
            'isAnomalous': recorded.get('isAnomalous'),
            'confidence': recorded.get('confidence'),
            'answer': recorded.get('answer'),
            'parseError': recorded.get('parseError'),
        },
        'input': input_block,
        'reference': reference_block,
        'singleImage': reference_block is None,
    }
    if unavailable is not None:
        view['unavailable'] = unavailable
    if include_metadata:
        view['metadataSnippet'] = sidecar.get('metadataSnippet')
    return view


def label_counts(items: List[Dict]) -> Dict[str, int]:
    counts = {'OK': 0, 'NOK': 0, 'EXCLUDE': 0, 'unlabelled': 0,
              'synthetic': 0, 'duplicates': 0, 'total': len(items)}
    for item in items:
        label = item.get('label')
        if label in LABELS:
            counts[label] += 1
        else:
            counts['unlabelled'] += 1
        if item.get('synthetic'):
            counts['synthetic'] += 1
        if item.get('duplicateOf'):
            counts['duplicates'] += 1
    return counts


# ==========================================================================
# Routes: overview (Requirements 1.2, 1.6)
# ==========================================================================

def overview(event: Dict, user: Dict) -> Dict:
    """GET /workflow-tuning/anomaly/workflows?usecase_id=[&workflow_id=]

    Every workflow of the Use_Case with at least one Tunable_Node in its
    latest version, each node's type, model and Sample_Store count, plus
    ``sampleExportEnabled`` so the UI can explain an empty store
    (Requirements 1.2, 1.6).
    """
    params = event.get('queryStringParameters') or {}
    usecase_id = params.get('usecase_id')
    if not usecase_id:
        return error_response(400, 'MISSING_FIELDS',
                              'usecase_id query parameter is required')
    # Authorization first, and the uniform 404 for a caller without read
    # access so the Use_Case's existence is not leaked (Requirements 9.1,
    # 9.2).
    authorize(user, event, usecase_id, Permission.WORKFLOW_READ)
    try:
        usecase = get_usecase(usecase_id)
    except ValueError:
        raise TuningError(404, 'WORKFLOW_NOT_FOUND', 'Workflow not found')

    export_enabled = tuning_settings.sample_export_enabled(usecase)
    workflow_filter = params.get('workflow_id')

    workflows: List[Dict] = []
    table_ref = dynamodb.Table(WORKFLOWS_TABLE)
    kwargs = {
        'IndexName': 'usecase-workflows-index',
        'KeyConditionExpression': 'usecase_id = :uid',
        'ExpressionAttributeValues': {':uid': usecase_id},
    }
    while True:
        response = table_ref.query(**kwargs)
        workflows.extend(decimal_to_native(i)
                         for i in response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key

    if workflow_filter:
        workflows = [w for w in workflows
                     if w['workflow_id'] == workflow_filter]

    client = None
    bucket = ''
    store_error = None
    try:
        client, bucket = sample_store(usecase)
    except TuningError as exc:
        store_error = exc.message
    except Exception as exc:  # pragma: no cover - STS/role failure
        store_error = str(exc)

    entries: List[Dict] = []
    for workflow in sorted(workflows,
                           key=lambda w: w.get('updated_at') or 0,
                           reverse=True):
        try:
            version, document = load_latest_definition(workflow)
        except TuningError:
            continue
        nodes = tunable_nodes(document)
        if not nodes:
            continue
        counts: Dict[str, int] = {}
        if client is not None:
            try:
                listing, _truncated = list_objects(
                    client, bucket,
                    workflow_samples_prefix(workflow['workflow_id']))
                for key in listing:
                    if not key.endswith(SIDECAR_SUFFIX):
                        continue
                    relative = key[len(workflow_samples_prefix(
                        workflow['workflow_id'])):]
                    parts = relative.split('/')
                    if len(parts) == 3:
                        counts[parts[0]] = counts.get(parts[0], 0) + 1
            except Exception as exc:
                store_error = store_error or str(exc)
                client = None
        sessions = existing_session_ids(workflow['workflow_id'],
                                        [n['id'] for n in nodes])
        entries.append({
            'workflowId': workflow['workflow_id'],
            'name': workflow.get('name'),
            'latestVersion': version,
            'updatedAt': workflow.get('updated_at'),
            'nodes': [{
                'nodeId': node['id'],
                'nodeType': node.get('type'),
                'model': node_model(node),
                'sampleCount': counts.get(node['id'], 0),
                'sessionId': sessions.get(node['id']),
            } for node in sorted(nodes, key=lambda n: n['id'])],
        })

    return create_response(200, {
        'usecaseId': usecase_id,
        'sampleExportEnabled': export_enabled,
        'sampleRetentionDays': tuning_settings.sample_retention_days(usecase),
        'workflows': entries,
        'count': len(entries),
        'sampleStoreError': store_error,
    })


def existing_session_ids(workflow_id: str,
                         node_ids: List[str]) -> Dict[str, str]:
    """The Tuning_Session id per node from the WF#/NODE# uniqueness items."""
    if not node_ids:
        return {}
    found: Dict[str, str] = {}
    for item in query_items(f"WF#{workflow_id}", 'NODE#'):
        node_id = str(item.get('sk', ''))[len('NODE#'):]
        if node_id in node_ids and item.get('sessionId'):
            found[node_id] = item['sessionId']
    return found


# ==========================================================================
# Routes: session lifecycle (Requirements 3.1, 5.1, 10.2, 10.5)
# ==========================================================================

def parse_body(event: Dict) -> Dict:
    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        raise TuningError(400, 'INVALID_JSON',
                          'Request body is not valid JSON')
    if not isinstance(body, dict):
        raise TuningError(400, 'INVALID_JSON',
                          'Request body must be a JSON object')
    return body


def create_or_get_session(event: Dict, user: Dict) -> Dict:
    """POST /workflow-tuning/anomaly/sessions  {workflow_id, node_id}

    At most one Tuning_Session per (workflowId, nodeId) (Requirement 10.2):
    the ``WF#/NODE#`` item is written conditionally, so a second create
    returns the existing session. A new session snapshots the
    Baseline_Candidate from the latest version (Requirement 5.1) and
    indexes the Sample_Store once (Requirement 3.1).
    """
    body = parse_body(event)
    workflow_id = body.get('workflow_id') or body.get('workflowId')
    node_id = body.get('node_id') or body.get('nodeId')
    if not workflow_id or not node_id:
        raise TuningError(400, 'MISSING_FIELDS',
                          'Missing required fields: workflow_id, node_id')

    workflow_item = require_workflow(str(workflow_id))
    authorize(user, event, workflow_item['usecase_id'],
              Permission.WORKFLOW_EDIT)

    version, document = load_latest_definition(workflow_item)
    node = find_node(document, str(node_id))
    if not node_is_tunable(node):
        parameters = (node or {}).get('parameters') or {}
        raise TuningError(
            400, 'NODE_NOT_TUNABLE',
            f"Node '{node_id}' is not an anomaly-mode Bedrock or VLM "
            f"inspection node in version {version}",
            {'nodeId': node_id, 'nodeType': (node or {}).get('type'),
             'anomaly_mode': parameters.get('anomaly_mode'),
             'version': version})

    lookup = lookup_key(str(workflow_id), str(node_id))
    session_id = str(uuid.uuid4())
    store = table()
    created = True
    try:
        store.put_item(Item={**lookup, 'sessionId': session_id,
                             'createdAt': now_ms()},
                       ConditionExpression='attribute_not_exists(pk)')
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') != \
                'ConditionalCheckFailedException':
            raise
        created = False
        existing = store.get_item(Key=lookup).get('Item') or {}
        session_id = existing.get('sessionId') or session_id
        session = get_session(session_id)
        if session is None:
            # A lookup item without its session (an interrupted create):
            # re-create the session under the recorded id.
            created = True
        else:
            session = refresh_baseline(session, node, version, user)
            return create_response(200, {
                'session': session_summary(session),
                'created': False,
                'node': node_view(node),
                'latestVersion': version,
            })

    prompt_set = prompt_set_of_node(node)
    timestamp = now_ms()
    session = {
        'pk': session_pk(session_id),
        'sk': 'META',
        'sessionId': session_id,
        'usecaseId': workflow_item['usecase_id'],
        'workflowId': str(workflow_id),
        'nodeId': str(node_id),
        'nodeType': node.get('type'),
        'baselineVersion': version,
        'baselineCandidateId': BASELINE_CANDIDATE_ID,
        'baselineFingerprint': candidate_fingerprint(node.get('type'),
                                                     prompt_set),
        'selectedCandidateId': None,
        'syntheticNegativesEnabled': False,
        'createdBy': user['user_id'],
        'createdAt': timestamp,
        'updatedAt': timestamp,
    }
    store.put_item(Item=to_dynamo(session))
    put_baseline_candidate(session_id, node, version, user)

    # First index. Best effort: a Sample_Store that cannot be read (not
    # configured yet, cross-account role not deployed) must not prevent the
    # session from existing — the refresh route retries.
    summary = None
    try:
        usecase = get_usecase(workflow_item['usecase_id'])
        client, bucket = sample_store(usecase)
        summary = index_samples(session, client, bucket)
    except Exception as exc:
        logger.warning(f"Initial sample index failed for session "
                       f"{session_id}: {exc}")
        summary = {'at': now_s(), 'indexed': 0, 'skipped': {},
                   'beyondBound': 0, 'error': str(exc)}
    session = record_refresh(session_id, summary)

    log_audit_event(
        user_id=user['user_id'], action='create_tuning_session',
        resource_type='workflow', resource_id=str(workflow_id),
        result='success',
        details={'usecase_id': workflow_item['usecase_id'],
                 'session_id': session_id, 'node_id': str(node_id),
                 'baseline_version': version,
                 'indexed': summary.get('indexed')})

    return create_response(201 if created else 200, {
        'session': session_summary(session),
        'created': True,
        'node': node_view(node),
        'latestVersion': version,
        'refresh': summary,
    })


def node_view(node: Dict) -> Dict:
    """The Tunable_Node as the UI shows it: identity, model and the
    Node_Parameters (never a credential — parameters only)."""
    return {
        'nodeId': node.get('id'),
        'nodeType': node.get('type'),
        'model': node_model(node),
        'parameters': node.get('parameters') or {},
        'promptSet': prompt_set_of_node(node),
        'maxTokensBounds': dict(zip(('min', 'max'),
                                    max_tokens_bounds(node.get('type')))),
    }


def record_refresh(session_id: str, summary: Dict) -> Dict:
    updated = table().update_item(
        Key={'pk': session_pk(session_id), 'sk': 'META'},
        UpdateExpression='SET lastRefresh = :r, updatedAt = :u',
        ExpressionAttributeValues={':r': to_dynamo(summary), ':u': now_ms()},
        ReturnValues='ALL_NEW',
    )
    return decimal_to_native(updated['Attributes'])


def view_session(event: Dict, user: Dict, session_id: str) -> Dict:
    """GET .../sessions/{id} — the session, its Candidates with their latest
    Score_Run, the Label counts and the last refresh summary."""
    session, workflow_item = session_context(user, event, session_id,
                                             Permission.WORKFLOW_READ)
    items = [decimal_to_native(i) for i in query_items(session_pk(session_id))]
    candidates = [i for i in items if str(i['sk']).startswith('CAND#')]
    samples = [i for i in items if str(i['sk']).startswith('SAMPLE#')]
    runs = [i for i in items if str(i['sk']).startswith('RUN#')]

    latest_run: Dict[str, Dict] = {}
    for run in sorted(runs, key=lambda r: r.get('startedAt') or 0):
        if run.get('candidateId'):
            latest_run[run['candidateId']] = run

    node = None
    latest_version = None
    try:
        latest_version, document = load_latest_definition(workflow_item)
        found = find_node(document, session['nodeId'])
        node = node_view(found) if found else None
    except TuningError:
        pass

    usecase = None
    try:
        usecase = get_usecase(session['usecaseId'])
    except ValueError:
        pass

    candidate_entries = []
    for item in sorted(candidates,
                       key=lambda c: (not c.get('isBaseline'),
                                      c.get('createdAt') or 0)):
        entry = candidate_summary(item)
        run = latest_run.get(item.get('candidateId'))
        entry['latestRun'] = run_view(run) if run else None
        candidate_entries.append(entry)

    return create_response(200, {
        'session': session_summary(session),
        'node': node,
        'latestVersion': latest_version,
        'nodeStillTunable': node is not None,
        'labelCounts': label_counts(samples),
        'candidates': candidate_entries,
        'runCount': len(runs),
        'sampleExportEnabled': (tuning_settings.sample_export_enabled(usecase)
                                if usecase else False),
    })


def run_view(run: Dict) -> Dict:
    """The public shape of a Score_Run item."""
    return {
        'runId': run.get('runId'),
        'sessionId': run.get('sessionId'),
        'candidateId': run.get('candidateId'),
        'candidateName': run.get('candidateName'),
        'status': run.get('status'),
        'mode': run.get('mode'),
        'repeats': run.get('repeats'),
        'plannedInvocations': run.get('plannedInvocations'),
        'plannedSampleCount': len(run.get('plannedSamples') or []),
        'done': run.get('done'),
        'cursor': run.get('cursor'),
        'cancelRequested': bool(run.get('cancelRequested')),
        'deviceThingName': run.get('deviceThingName'),
        'jobId': run.get('jobId'),
        'reportedJob': run.get('reportedJob'),
        'startedAt': run.get('startedAt'),
        'startedBy': run.get('startedBy'),
        'lastProgressAt': run.get('lastProgressAt'),
        'finishedAt': run.get('finishedAt'),
        'summary': run.get('summary'),
        'error': run.get('error'),
    }


def refresh_session(event: Dict, user: Dict, session_id: str) -> Dict:
    """POST .../sessions/{id}/refresh — additive re-index (Requirement 3.4)
    plus a Baseline_Candidate refresh when the workflow gained a version."""
    session, workflow_item = session_context(user, event, session_id,
                                             Permission.WORKFLOW_EDIT)
    try:
        version, document = load_latest_definition(workflow_item)
        node = find_node(document, session['nodeId'])
        if node_is_tunable(node):
            session = refresh_baseline(session, node, version, user)
    except TuningError:
        pass

    usecase = get_usecase(session['usecaseId'])
    client, bucket = sample_store(usecase)
    summary = index_samples(session, client, bucket)
    session = record_refresh(session_id, summary)
    samples = sample_items(session_id)
    return create_response(200, {
        'session': session_summary(session),
        'refresh': summary,
        'labelCounts': label_counts(samples),
    })


def delete_session(event: Dict, user: Dict, session_id: str) -> Dict:
    """DELETE .../sessions/{id} — the session and everything it owns
    (Requirement 10.2), including its Score_Run outcome objects, while
    every exported Tuning_Sample stays in the Sample_Store for other and
    future sessions (Requirement 10.5)."""
    session, _workflow_item = session_context(user, event, session_id,
                                              Permission.WORKFLOW_EDIT)
    items = [decimal_to_native(i) for i in query_items(session_pk(session_id))]
    run_ids = [i['sk'][len('RUN#'):] for i in items
               if str(i['sk']).startswith('RUN#')]

    # Sample_Outcomes live in their run's own partition.
    outcome_keys: List[Dict[str, str]] = []
    for run_id in run_ids:
        outcome_keys.extend(
            {'pk': i['pk'], 'sk': i['sk']}
            for i in query_items(run_pk(run_id), projection='pk, sk'))
    deleted_outcomes = delete_items(outcome_keys)
    deleted_items = delete_items([{'pk': i['pk'], 'sk': i['sk']}
                                  for i in items])
    table().delete_item(Key=lookup_key(session['workflowId'],
                                       session['nodeId']))

    deleted_objects = 0
    try:
        usecase = get_usecase(session['usecaseId'])
        client, bucket = sample_store(usecase)
        deleted_objects = delete_prefix(client, bucket,
                                        session_runs_prefix(session_id))
    except Exception as exc:
        logger.warning(f"Could not delete run objects of session "
                       f"{session_id}: {exc}")

    log_audit_event(
        user_id=user['user_id'], action='delete_tuning_session',
        resource_type='workflow', resource_id=session['workflowId'],
        result='success',
        details={'usecase_id': session.get('usecaseId'),
                 'session_id': session_id,
                 'node_id': session.get('nodeId'),
                 'items_deleted': deleted_items,
                 'outcomes_deleted': deleted_outcomes,
                 'objects_deleted': deleted_objects})

    return create_response(200, {
        'sessionId': session_id,
        'deleted': {'items': deleted_items, 'outcomes': deleted_outcomes,
                    'objects': deleted_objects},
        'message': 'Tuning session deleted; exported samples were kept',
    })


def delete_prefix(client, bucket: str, prefix: str) -> int:
    """Delete every object under a prefix; returns the count. Only ever
    called with a ``workflow-tuning/sessions/{id}/`` prefix
    (Requirements 9.4, 10.5)."""
    if not prefix.startswith(tuning_settings.SESSION_STORE_PREFIX):
        raise TuningError(500, 'INVALID_PREFIX',
                          'Refusing to delete outside the session prefix')
    listing, _truncated = list_objects(client, bucket, prefix)
    keys = sorted(listing)
    for start in range(0, len(keys), 1000):
        chunk = keys[start:start + 1000]
        client.delete_objects(
            Bucket=bucket,
            Delete={'Objects': [{'Key': key} for key in chunk]})
    return len(keys)


# ==========================================================================
# Routes: samples (Requirements 3.7, 4.1, 4.4, 4.8)
# ==========================================================================

def _bool_param(value: Any) -> Optional[bool]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ('true', '1', 'yes', 'only'):
        return True
    if text in ('false', '0', 'no', 'exclude'):
        return False
    return None


def list_samples(event: Dict, user: Dict, session_id: str) -> Dict:
    """GET .../sessions/{id}/samples — a filtered, sorted page with
    presigned image URLs (Requirements 4.1, 4.4, 4.8)."""
    session, _workflow_item = session_context(user, event, session_id,
                                              Permission.WORKFLOW_READ)
    params = event.get('queryStringParameters') or {}
    baseline_fingerprint = session.get('baselineFingerprint')

    items = sample_items(session_id)
    all_items = list(items)

    label = params.get('label')
    if label:
        wanted = str(label).strip().upper()
        if wanted in ('UNLABELLED', 'UNLABELED', 'NONE'):
            items = [i for i in items if i.get('label') not in LABELS]
        else:
            items = [i for i in items if i.get('label') == wanted]
    verdict = params.get('verdict')
    if verdict:
        text = str(verdict).strip().lower()
        wanted_verdict = None
        if text in ('anomalous', 'true', 'nok', 'anomaly'):
            wanted_verdict = True
        elif text in ('normal', 'false', 'ok'):
            wanted_verdict = False
        if wanted_verdict is not None:
            items = [i for i in items
                     if bool(i.get('recordedIsAnomalous')) is wanted_verdict]
    device = params.get('device')
    if device:
        items = [i for i in items if i.get('thingName') == device]
    version = params.get('version')
    if version:
        items = [i for i in items if str(i.get('version')) == str(version)]
    source = params.get('source')
    if source:
        items = [i for i in items if i.get('source') == source]
    duplicates = _bool_param(params.get('duplicates'))
    if duplicates is True:
        items = [i for i in items if i.get('duplicateOf')]
    elif duplicates is False:
        items = [i for i in items if not i.get('duplicateOf')]
    synthetic = _bool_param(params.get('synthetic'))
    if synthetic is not None:
        items = [i for i in items if bool(i.get('synthetic')) is synthetic]
    different_prompt = _bool_param(params.get('differentPrompt')
                                   or params.get('different_prompt'))
    if different_prompt is not None:
        def is_different(item):
            fingerprint = item.get('promptFingerprint')
            return bool(fingerprint and baseline_fingerprint
                        and fingerprint != baseline_fingerprint)
        items = [i for i in items if is_different(i) is different_prompt]
    disagree = _bool_param(params.get('disagree'))
    if disagree is not None:
        def disagrees(item):
            label_value = item.get('label')
            if label_value not in ('OK', 'NOK'):
                return False
            recorded = bool(item.get('recordedIsAnomalous'))
            return (label_value == 'NOK') != recorded
        items = [i for i in items if disagrees(i) is disagree]

    items.sort(key=lambda i: (int(i.get('exportedAt') or 0),
                              str(i.get('sampleId'))), reverse=True)

    try:
        limit = int(params.get('limit') or DEFAULT_SAMPLE_PAGE_SIZE)
    except (TypeError, ValueError):
        limit = DEFAULT_SAMPLE_PAGE_SIZE
    limit = max(1, min(limit, MAX_SAMPLE_PAGE_SIZE))
    offset = decode_cursor(params.get('cursor'))
    page = items[offset:offset + limit]
    next_cursor = (encode_cursor(offset + limit)
                   if offset + limit < len(items) else None)

    include_metadata = bool(_bool_param(params.get('include_metadata')))
    client, bucket = sample_store(get_usecase(session['usecaseId']))
    availability = check_availability(client, bucket, page)

    return create_response(200, {
        'sessionId': session_id,
        'samples': [sample_view(item, baseline_fingerprint, client, bucket,
                                urls=True,
                                include_metadata=include_metadata,
                                unavailable=availability.get(
                                    item['sampleId']))
                    for item in page],
        'count': len(page),
        'matched': len(items),
        'nextCursor': next_cursor,
        'expiresInSeconds': PRESIGNED_URL_EXPIRY_SECONDS,
        'labelCounts': label_counts(all_items),
    })


def check_availability(client, bucket: str,
                       page: List[Dict]) -> Dict[str, bool]:
    """Whether each page sample's Input_Image object still exists: a sample
    whose images have expired from the Sample_Store is shown as unavailable
    while keeping its Label (Requirement 3.7)."""
    def head(item):
        key = item.get('inputKey')
        if not key:
            return item['sampleId'], True
        try:
            client.head_object(Bucket=bucket, Key=key)
            return item['sampleId'], False
        except ClientError:
            return item['sampleId'], True
        except Exception:  # pragma: no cover - transport failure
            return item['sampleId'], False

    if not page:
        return {}
    with ThreadPoolExecutor(max_workers=S3_READ_THREADS) as pool:
        return dict(pool.map(head, page))


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({'offset': int(offset)}).encode('utf-8')).decode('ascii')


def decode_cursor(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        payload = json.loads(base64.urlsafe_b64decode(
            cursor.encode('ascii')).decode('utf-8'))
        return max(0, int(payload.get('offset') or 0))
    except Exception:
        raise TuningError(400, 'INVALID_CURSOR', 'cursor is not valid')


def set_labels(event: Dict, user: Dict, session_id: str) -> Dict:
    """PUT .../sessions/{id}/samples/labels  {sampleIds, label}

    Sets the Label of one or many samples; ``label: null`` clears it
    (Requirements 4.2, 4.3). Each change is persisted immediately.
    """
    session, _workflow_item = session_context(user, event, session_id,
                                              Permission.WORKFLOW_EDIT)
    body = parse_body(event)
    sample_ids = body.get('sampleIds') or body.get('sample_ids')
    if not isinstance(sample_ids, list) or not sample_ids:
        raise TuningError(400, 'MISSING_FIELDS',
                          'sampleIds must be a non-empty list')
    if not all(isinstance(i, str) and i for i in sample_ids):
        raise TuningError(400, 'INVALID_SAMPLE_IDS',
                          'sampleIds must be non-empty strings')
    label = body.get('label')
    if label is not None:
        label = str(label).strip().upper()
        if label not in LABELS:
            raise TuningError(400, 'INVALID_LABEL',
                              f"label must be one of {', '.join(LABELS)} "
                              f"or null", {'label': body.get('label')})

    store = table()
    updated: List[str] = []
    missing: List[str] = []
    for sample_id in dict.fromkeys(sample_ids):
        key = {'pk': session_pk(session_id), 'sk': sample_sk(sample_id)}
        try:
            if label is None:
                store.update_item(
                    Key=key, UpdateExpression='REMOVE #l',
                    ExpressionAttributeNames={'#l': 'label'},
                    ConditionExpression='attribute_exists(sk)')
            else:
                store.update_item(
                    Key=key,
                    UpdateExpression='SET #l = :l, labelledAt = :t, '
                                     'labelledBy = :u',
                    ExpressionAttributeNames={'#l': 'label'},
                    ExpressionAttributeValues={':l': label, ':t': now_s(),
                                               ':u': user['user_id']},
                    ConditionExpression='attribute_exists(sk)')
            updated.append(sample_id)
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') != \
                    'ConditionalCheckFailedException':
                raise
            missing.append(sample_id)

    return create_response(200, {
        'sessionId': session_id,
        'label': label,
        'updated': updated,
        'missing': missing,
        'labelCounts': label_counts(sample_items(session_id)),
    })


# ==========================================================================
# Routes: synthetic negatives (Requirements 4.5, 4.6, 4.7)
# ==========================================================================

def toggle_synthetic_negatives(event: Dict, user: Dict,
                               session_id: str) -> Dict:
    """PUT .../sessions/{id}/synthetic-negatives  {enabled}

    Enabling creates exactly one NOK Synthetic_Negative per (OK-labelled
    sample, sibling Inspection_Node with an exported Reference_Image for
    the same execution on the same device), each linked to its source
    (Requirement 4.5). Disabling removes every Synthetic_Negative and
    leaves every indexed sample and Label unchanged (Requirement 4.6).
    """
    session, _workflow_item = session_context(user, event, session_id,
                                              Permission.WORKFLOW_EDIT)
    body = parse_body(event)
    if 'enabled' not in body or not isinstance(body['enabled'], bool):
        raise TuningError(400, 'MISSING_FIELDS',
                          'enabled must be a boolean')
    enabled = body['enabled']
    items = sample_items(session_id)

    if not enabled:
        removed = delete_items([
            {'pk': session_pk(session_id), 'sk': sample_sk(i['sampleId'])}
            for i in items if i.get('synthetic')])
        session = set_synthetic_flag(session_id, False)
        return create_response(200, {
            'sessionId': session_id, 'enabled': False, 'created': 0,
            'removed': removed,
            'labelCounts': label_counts(sample_items(session_id)),
        })

    usecase = get_usecase(session['usecaseId'])
    client, bucket = sample_store(usecase)
    workflow_prefix = workflow_samples_prefix(session['workflowId'])
    listing, _truncated = list_objects(client, bucket, workflow_prefix)

    # (thingName, executionId) -> [(siblingNodeId, referenceKey)]
    siblings: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for key in sorted(listing):
        if not key.endswith(REFERENCE_SUFFIX):
            continue
        relative = key[len(workflow_prefix):]
        parts = relative.split('/')
        if len(parts) != 3:
            continue
        node_id, thing_name, tail = parts
        if node_id == session['nodeId']:
            continue
        execution_id = tail[:-len(REFERENCE_SUFFIX)]
        siblings.setdefault((thing_name, execution_id), []).append(
            (node_id, key))

    store = table()
    created = 0
    for item in items:
        if item.get('synthetic') or item.get('label') != 'OK':
            continue
        pairs = siblings.get((str(item.get('thingName')),
                              str(item.get('executionId'))), [])
        for sibling_node_id, reference_key in pairs:
            sample_id = synthetic_sample_id(item['sampleId'],
                                            sibling_node_id)
            synthetic_item = {
                'pk': session_pk(session_id),
                'sk': sample_sk(sample_id),
                'sampleId': sample_id,
                'sidecar': item.get('sidecar') or {},
                'workflowId': item.get('workflowId'),
                'nodeId': item.get('nodeId'),
                'nodeType': item.get('nodeType'),
                'thingName': item.get('thingName'),
                'executionId': item.get('executionId'),
                'version': item.get('version'),
                'exportedAt': item.get('exportedAt'),
                'source': item.get('source'),
                'inputKey': item.get('inputKey'),
                'inputSha256': item.get('inputSha256'),
                'referenceKey': reference_key,
                'recordedIsAnomalous': item.get('recordedIsAnomalous'),
                'promptFingerprint': item.get('promptFingerprint'),
                'detectionId': item.get('detectionId'),
                'detectionSlot': item.get('detectionSlot'),
                'label': 'NOK',
                'synthetic': True,
                'sourceSampleId': item['sampleId'],
                'siblingNodeId': sibling_node_id,
                'duplicateOf': None,
                'differentPrompt': item.get('differentPrompt'),
                'indexedAt': now_s(),
            }
            try:
                store.put_item(Item=to_dynamo(synthetic_item),
                               ConditionExpression='attribute_not_exists(sk)')
                created += 1
            except ClientError as exc:
                if exc.response.get('Error', {}).get('Code') != \
                        'ConditionalCheckFailedException':
                    raise

    session = set_synthetic_flag(session_id, True)
    return create_response(200, {
        'sessionId': session_id, 'enabled': True, 'created': created,
        'removed': 0,
        'labelCounts': label_counts(sample_items(session_id)),
    })


def set_synthetic_flag(session_id: str, enabled: bool) -> Dict:
    updated = table().update_item(
        Key={'pk': session_pk(session_id), 'sk': 'META'},
        UpdateExpression=('SET syntheticNegativesEnabled = :e, '
                          'updatedAt = :u'),
        ExpressionAttributeValues={':e': enabled, ':u': now_ms()},
        ReturnValues='ALL_NEW')
    return decimal_to_native(updated['Attributes'])


# ==========================================================================
# Routes: candidates (Requirements 5.2, 5.7)
# ==========================================================================

def validate_prompt_set(body: Dict, node_type: Any,
                        existing: Optional[Dict] = None) -> Dict:
    """The Candidate's Prompt_Set from a request body, validated against
    the node type's catalog bounds (Requirement 5.2)."""
    existing = existing or {}
    prompt = body.get('prompt', existing.get('prompt'))
    if not isinstance(prompt, str) or not prompt.strip():
        raise TuningError(400, 'INVALID_PROMPT',
                          'prompt must be a non-empty string')
    system_prompt = body.get('systemPrompt',
                             body.get('system_prompt',
                                      existing.get('systemPrompt', '')))
    if system_prompt is None:
        system_prompt = ''
    if not isinstance(system_prompt, str):
        raise TuningError(400, 'INVALID_SYSTEM_PROMPT',
                          'systemPrompt must be a string')
    max_tokens = body.get('maxTokens', body.get('max_tokens',
                                                existing.get('maxTokens')))
    minimum, maximum = max_tokens_bounds(node_type)
    if max_tokens is None:
        # The catalog default, clamped into the type's own bounds.
        max_tokens = DEFAULT_MAX_TOKENS
        if maximum is not None:
            max_tokens = min(max_tokens, maximum)
        max_tokens = max(max_tokens, minimum)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, (int, float)):
        raise TuningError(400, 'INVALID_MAX_TOKENS',
                          'maxTokens must be an integer')
    if float(max_tokens) != int(max_tokens):
        raise TuningError(400, 'INVALID_MAX_TOKENS',
                          'maxTokens must be an integer')
    max_tokens = int(max_tokens)
    if max_tokens < minimum or (maximum is not None and max_tokens > maximum):
        raise TuningError(400, 'INVALID_MAX_TOKENS',
                          f"maxTokens must be between {minimum} and "
                          f"{maximum if maximum is not None else 'unbounded'}",
                          {'min': minimum, 'max': maximum})
    return {'prompt': prompt, 'systemPrompt': system_prompt,
            'maxTokens': max_tokens}


def validate_candidate_name(body: Dict, existing: Optional[Dict] = None
                            ) -> str:
    name = body.get('name', (existing or {}).get('name'))
    if not isinstance(name, str) or not name.strip():
        raise TuningError(400, 'INVALID_NAME',
                          'name must be a non-empty string')
    name = name.strip()
    if len(name) > 128:
        raise TuningError(400, 'INVALID_NAME',
                          'name must be at most 128 characters')
    return name


def create_candidate(event: Dict, user: Dict, session_id: str) -> Dict:
    """POST .../sessions/{id}/candidates — a new Candidate (Requirement
    5.2). Duplication is the same call with the source's Prompt_Set."""
    session, _workflow_item = session_context(user, event, session_id,
                                              Permission.WORKFLOW_EDIT)
    body = parse_body(event)
    name = validate_candidate_name(body)
    prompt_set = validate_prompt_set(body, session.get('nodeType'))
    candidate_id = str(uuid.uuid4())
    timestamp = now_ms()
    item = {
        'pk': session_pk(session_id),
        'sk': candidate_sk(candidate_id),
        'candidateId': candidate_id,
        'name': name,
        'prompt': prompt_set['prompt'],
        'systemPrompt': prompt_set['systemPrompt'],
        'maxTokens': prompt_set['maxTokens'],
        'isBaseline': False,
        'fingerprint': candidate_fingerprint(session.get('nodeType'),
                                             prompt_set),
        'createdBy': user['user_id'],
        'createdAt': timestamp,
        'updatedAt': timestamp,
    }
    table().put_item(Item=to_dynamo(item))
    log_audit_event(
        user_id=user['user_id'], action='create_tuning_candidate',
        resource_type='workflow', resource_id=session['workflowId'],
        result='success',
        details={'usecase_id': session.get('usecaseId'),
                 'session_id': session_id, 'candidate_id': candidate_id,
                 'name': name})
    return create_response(201, {'candidate': candidate_summary(item)})


def update_candidate(event: Dict, user: Dict, session_id: str,
                     candidate_id: str) -> Dict:
    """PUT .../sessions/{id}/candidates/{cid} — edit a Candidate. The
    Baseline_Candidate is read-only (Requirement 5.1)."""
    session, _workflow_item = session_context(user, event, session_id,
                                              Permission.WORKFLOW_EDIT)
    item = get_candidate(session_id, candidate_id)
    if not item:
        raise TuningError(404, 'CANDIDATE_NOT_FOUND', 'Candidate not found')
    if item.get('isBaseline'):
        raise TuningError(409, 'BASELINE_READ_ONLY',
                          'The baseline candidate is read-only')
    body = parse_body(event)
    name = validate_candidate_name(body, item)
    prompt_set = validate_prompt_set(body, session.get('nodeType'), item)
    timestamp = now_ms()
    updated = table().update_item(
        Key={'pk': session_pk(session_id), 'sk': candidate_sk(candidate_id)},
        UpdateExpression=('SET #n = :n, prompt = :p, systemPrompt = :s, '
                          'maxTokens = :m, fingerprint = :f, updatedAt = :u'),
        ExpressionAttributeNames={'#n': 'name'},
        ExpressionAttributeValues=to_dynamo({
            ':n': name, ':p': prompt_set['prompt'],
            ':s': prompt_set['systemPrompt'], ':m': prompt_set['maxTokens'],
            ':f': candidate_fingerprint(session.get('nodeType'), prompt_set),
            ':u': timestamp}),
        ConditionExpression='attribute_exists(sk)',
        ReturnValues='ALL_NEW')
    return create_response(200, {
        'candidate': candidate_summary(decimal_to_native(
            updated['Attributes']))})


def delete_candidate(event: Dict, user: Dict, session_id: str,
                     candidate_id: str) -> Dict:
    """DELETE .../sessions/{id}/candidates/{cid} — deletes the Candidate and
    its Score_Runs, leaving every other Candidate, Score_Run,
    Tuning_Sample and Label unchanged (Requirement 5.7)."""
    session, _workflow_item = session_context(user, event, session_id,
                                              Permission.WORKFLOW_EDIT)
    item = get_candidate(session_id, candidate_id)
    if not item:
        raise TuningError(404, 'CANDIDATE_NOT_FOUND', 'Candidate not found')
    if item.get('isBaseline'):
        raise TuningError(409, 'BASELINE_READ_ONLY',
                          'The baseline candidate is read-only')

    runs = [decimal_to_native(i)
            for i in query_items(session_pk(session_id), 'RUN#')]
    run_ids = [r['runId'] for r in runs
               if r.get('candidateId') == candidate_id and r.get('runId')]
    outcome_keys: List[Dict[str, str]] = []
    for run_id in run_ids:
        outcome_keys.extend(
            {'pk': i['pk'], 'sk': i['sk']}
            for i in query_items(run_pk(run_id), projection='pk, sk'))
    delete_items(outcome_keys)
    delete_items([{'pk': session_pk(session_id), 'sk': run_sk(run_id)}
                  for run_id in run_ids])
    table().delete_item(Key={'pk': session_pk(session_id),
                             'sk': candidate_sk(candidate_id)})

    updates = {}
    if session.get('selectedCandidateId') == candidate_id:
        table().update_item(
            Key={'pk': session_pk(session_id), 'sk': 'META'},
            UpdateExpression='SET selectedCandidateId = :n, updatedAt = :u',
            ExpressionAttributeValues={':n': None, ':u': now_ms()})
        updates['selectionCleared'] = True

    log_audit_event(
        user_id=user['user_id'], action='delete_tuning_candidate',
        resource_type='workflow', resource_id=session['workflowId'],
        result='success',
        details={'usecase_id': session.get('usecaseId'),
                 'session_id': session_id, 'candidate_id': candidate_id,
                 'runs_deleted': len(run_ids)})

    return create_response(200, {
        'candidateId': candidate_id, 'runsDeleted': len(run_ids), **updates})


# ==========================================================================
# Routes: candidate preview (Requirements 5.3, 5.4, 5.5)
# ==========================================================================

def preview_warnings(prompt: str, system_prompt: Optional[str],
                     max_tokens: Any) -> List[Dict[str, str]]:
    """The Candidate's parser-hostile-settings warnings, never blocking
    (Requirements 5.4, 5.5).

    - ``max_tokens`` below 64: a truncated answer fails the Verdict_Parser.
    - a prompt or system prompt that demands a JSON answer format without
      naming ``is_anomalous``: the Verdict_Parser needs a JSON object
      carrying that key, and the model tends to obey the operator's schema
      over the appended Verdict_Instruction (the blue-plate finding).
    """
    warnings: List[Dict[str, str]] = []
    try:
        budget = int(max_tokens)
    except (TypeError, ValueError):
        budget = None
    if budget is not None and budget < MIN_SAFE_MAX_TOKENS:
        warnings.append({
            'code': 'max_tokens_truncation',
            'message': (f'max_tokens is {budget}: answers truncated below '
                        f'{MIN_SAFE_MAX_TOKENS} tokens fail the verdict '
                        f'parser, which needs a complete JSON object.'),
        })
    for field, text in (('prompt', prompt), ('systemPrompt', system_prompt)):
        if not text:
            continue
        lowered = str(text).lower()
        demands_json = 'json' in lowered or ('{' in text and '}' in text)
        if demands_json and 'is_anomalous' not in lowered:
            warnings.append({
                'code': 'answer_schema_missing_is_anomalous',
                'field': field,
                'message': (f'The {field} specifies an answer format that '
                            f'does not include "is_anomalous". The verdict '
                            f'parser requires a JSON object carrying '
                            f'is_anomalous (and optionally confidence).'),
            })
    return warnings


def build_preview(node: Dict, prompt_set: Dict[str, Any]) -> Dict:
    """``{userMessage, systemText}`` exactly as the Invocation_Builder would
    send them for this Prompt_Set (Requirement 5.3)."""
    parameters = invocation_parameters(node, prompt_set)
    if node.get('type') == NODE_TYPE_LLM_INFERENCE:
        # The prompt template is shown UNRENDERED: a preview has no run
        # metadata, and the module never invents any.
        invocation = build_llm_invocation(
            parameters, parameters.get('prompt_template') or '',
            PREVIEW_PLACEHOLDER_IMAGE, PREVIEW_PLACEHOLDER_IMAGE)
        return {'userMessage': invocation.prompt,
                'systemText': invocation.system_prompt,
                'maxTokens': invocation.generation.get('max_tokens'),
                'model': invocation.model_name,
                'templateRendered': False}
    invocation = build_bedrock_invocation(
        parameters, PREVIEW_PLACEHOLDER_IMAGE, PREVIEW_PLACEHOLDER_IMAGE)
    return {'userMessage': invocation.prompt,
            'systemText': invocation.system_prompt,
            'maxTokens': invocation.max_tokens,
            'model': invocation.model,
            'region': invocation.region,
            'templateRendered': True}


def preview_candidate(event: Dict, user: Dict, candidate_id: str) -> Dict:
    """GET .../candidates/{cid}/preview?session_id=

    The exact user-message and system text the Invocation_Builder will send
    plus the warnings (Requirements 5.3-5.5). The route carries no session
    in its path, so the session is named by ``session_id`` (or resolved from
    ``workflow_id``+``node_id``).
    """
    params = event.get('queryStringParameters') or {}
    session_id = params.get('session_id') or params.get('sessionId')
    if not session_id:
        workflow_id = params.get('workflow_id') or params.get('workflowId')
        node_id = params.get('node_id') or params.get('nodeId')
        if workflow_id and node_id:
            lookup = table().get_item(
                Key=lookup_key(str(workflow_id), str(node_id))).get('Item')
            session_id = (lookup or {}).get('sessionId')
        if not session_id:
            raise TuningError(
                400, 'MISSING_FIELDS',
                'session_id query parameter is required (or workflow_id '
                'and node_id of an existing session)')

    session, workflow_item = session_context(user, event, str(session_id),
                                             Permission.WORKFLOW_READ)
    candidate = get_candidate(str(session_id), candidate_id)
    if not candidate:
        raise TuningError(404, 'CANDIDATE_NOT_FOUND', 'Candidate not found')

    _version, document = load_latest_definition(workflow_item)
    node = find_node(document, session['nodeId'])
    if node is None:
        # The node was removed from the workflow: preview the Prompt_Set
        # against the session's recorded node type so the editor keeps
        # working (apply is what refuses — Requirement 8.4).
        node = {'id': session['nodeId'], 'type': session.get('nodeType'),
                'parameters': {'anomaly_mode': True}}

    prompt_set = {'prompt': candidate.get('prompt'),
                  'systemPrompt': candidate.get('systemPrompt'),
                  'maxTokens': candidate.get('maxTokens')}
    preview = build_preview(node, prompt_set)
    preview['warnings'] = preview_warnings(prompt_set['prompt'],
                                           prompt_set['systemPrompt'],
                                           prompt_set['maxTokens'])
    preview['candidateId'] = candidate_id
    preview['sessionId'] = str(session_id)
    preview['nodeType'] = node.get('type')
    return create_response(200, preview)


# ==========================================================================
# Score_Runs: store access (Requirements 6.*, 7.*, 10.3, 10.4)
# ==========================================================================

def get_run(session_id: str, run_id: str) -> Optional[Dict]:
    response = table().get_item(Key={'pk': session_pk(session_id),
                                     'sk': run_sk(run_id)})
    item = response.get('Item')
    return decimal_to_native(item) if item else None


def resolve_run(run_id: str) -> Tuple[Dict, Dict]:
    """``(session, run)`` of a Score_Run named by id alone.

    The by-id routes (``.../score-runs/{rid}``) carry no session, so the
    run's own partition holds a pointer item written at admission. A
    missing pointer, session or run answers the uniform 404 — a run id of
    another tenant is indistinguishable from one that never existed
    (Requirement 9.1).
    """
    pointer = table().get_item(Key=run_pointer_key(str(run_id))).get('Item')
    session_id = (pointer or {}).get('sessionId')
    if not session_id:
        raise TuningError(404, 'WORKFLOW_NOT_FOUND', 'Workflow not found')
    session = require_session(str(session_id))
    run = get_run(str(session_id), str(run_id))
    if not run:
        raise TuningError(404, 'WORKFLOW_NOT_FOUND', 'Workflow not found')
    return session, run


def run_context(user: Dict, event: Dict, run_id: str,
                permission: Permission) -> Tuple[Dict, Dict]:
    """``(session, run)`` after authorizing the operation on the run's
    workflow — before anything else is read or validated
    (Requirement 9.2)."""
    session, run = resolve_run(run_id)
    workflow_item = get_workflow_item(session['workflowId'])
    if not workflow_item:
        raise TuningError(404, 'WORKFLOW_NOT_FOUND', 'Workflow not found')
    authorize(user, event, workflow_item['usecase_id'], permission)
    return session, run


def outcome_items(run_id: str) -> List[Dict]:
    """Every persisted Sample_Outcome of a run (its partition's ``OUT#``
    items — never the pointer item)."""
    return [decimal_to_native(i)
            for i in query_items(run_pk(run_id), 'OUT#')]


def outcome_view(item: Dict) -> Dict:
    """The public shape of one Sample_Outcome (Requirements 6.5, 7.2,
    7.4)."""
    return {
        'sampleId': item.get('sampleId'),
        'repeat': item.get('repeat'),
        'label': item.get('label'),
        'category': item.get('category'),
        'isAnomalous': item.get('isAnomalous'),
        'confidence': item.get('confidence'),
        'rawAnswer': item.get('rawAnswer'),
        'parseError': item.get('parseError'),
        'outputTokens': item.get('outputTokens'),
        'latencyMs': item.get('latencyMs'),
        'error': item.get('error'),
        'thingName': item.get('thingName'),
    }


def outcome_item(run_id: str, session_id: str, outcome: Dict) -> Dict:
    """One Sample_Outcome as it is persisted: the run's partition, the
    sample/repeat sort key and a 90-day TTL (design data model). Never any
    image bytes (Requirement 9.5)."""
    item = {
        'pk': run_pk(run_id),
        'sk': outcome_sk(outcome['sampleId'], outcome.get('repeat') or 1),
        'runId': run_id,
        'sessionId': session_id,
        'sampleId': outcome.get('sampleId'),
        'repeat': int(outcome.get('repeat') or 1),
        'label': outcome.get('label'),
        'category': outcome.get('category'),
        'isAnomalous': outcome.get('isAnomalous'),
        'confidence': outcome.get('confidence'),
        'rawAnswer': outcome.get('rawAnswer'),
        'parseError': outcome.get('parseError'),
        'outputTokens': outcome.get('outputTokens'),
        'latencyMs': outcome.get('latencyMs'),
        'error': outcome.get('error'),
        'thingName': outcome.get('thingName'),
        'recordedAt': now_s(),
        'ttl': now_s() + OUTCOME_TTL_SECONDS,
    }
    return to_dynamo(item)


def persist_outcomes(run_id: str, session_id: str,
                     outcomes: List[Dict]) -> int:
    """Persist a batch of Sample_Outcomes, each exactly once.

    Every write is conditional on the ``(sample, repeat)`` item not
    existing, so re-ingesting a device outcome batch or resuming a step
    that already persisted its units cannot duplicate or overwrite an
    outcome (Requirements 6.11, 10.4, Property 11).
    """
    store = table()
    written = 0
    for outcome in outcomes:
        if not outcome.get('sampleId'):
            continue
        try:
            store.put_item(Item=outcome_item(run_id, session_id, outcome),
                           ConditionExpression='attribute_not_exists(sk)')
            written += 1
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') != \
                    'ConditionalCheckFailedException':
                raise
    return written


def update_run(session_id: str, run_id: str, expression: str,
               values: Dict[str, Any],
               names: Optional[Dict[str, str]] = None,
               condition: Optional[str] = None) -> Optional[Dict]:
    """Update a Score_Run item; ``None`` when a condition rejected it."""
    kwargs: Dict[str, Any] = {
        'Key': {'pk': session_pk(session_id), 'sk': run_sk(run_id)},
        'UpdateExpression': expression,
        'ExpressionAttributeValues': to_dynamo(values),
        'ReturnValues': 'ALL_NEW',
    }
    if names:
        kwargs['ExpressionAttributeNames'] = names
    if condition:
        kwargs['ConditionExpression'] = condition
    try:
        updated = table().update_item(**kwargs)
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') == \
                'ConditionalCheckFailedException':
            return None
        raise
    return decimal_to_native(updated['Attributes'])


# ==========================================================================
# Score_Runs: admission (Requirements 6.6, 6.10, 6.13, 4.3)
# ==========================================================================

def scored_samples(session_id: str) -> List[Dict]:
    """The samples a Score_Run replays: exactly the OK/NOK-labelled ones.

    Unlabelled and EXCLUDE samples are absent and every OK/NOK sample is
    present (Requirement 4.3, Property 13). The order is deterministic
    (oldest first, ties by id) so a resumed run's unit list is the same
    list it started with.
    """
    items = [i for i in sample_items(session_id)
             if i.get('label') in (LABEL_OK, LABEL_NOK)]
    items.sort(key=lambda i: (int(i.get('exportedAt') or 0),
                              str(i.get('sampleId'))))
    return items


def validate_repeats(raw: Any) -> int:
    """Repeats within 1..3 (Requirement 6.7); anything else is a 400."""
    if raw is None:
        return DEFAULT_REPEATS
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise TuningError(400, 'INVALID_REPEATS',
                          f'repeats must be an integer between '
                          f'{MIN_REPEATS} and {MAX_REPEATS}')
    try:
        repeats = int(str(raw).strip())
    except (TypeError, ValueError):
        raise TuningError(400, 'INVALID_REPEATS',
                          f'repeats must be an integer between '
                          f'{MIN_REPEATS} and {MAX_REPEATS}')
    if repeats < MIN_REPEATS or repeats > MAX_REPEATS:
        raise TuningError(400, 'INVALID_REPEATS',
                          f'repeats must be between {MIN_REPEATS} and '
                          f'{MAX_REPEATS}',
                          {'repeats': repeats, 'min': MIN_REPEATS,
                           'max': MAX_REPEATS})
    return repeats


def registered_devices(usecase_id: str, workflow_id: str) -> List[str]:
    """Devices the Portal records as running the workflow.

    The Deployments table is the Portal's own record of what a device
    runs (``models.py`` reads ``target_devices`` the same way): the union
    of the target devices of the Use_Case's active deployments that
    reference the workflow, either through the ``component_type``/
    ``workflow_id`` association or through the packaged
    ``dda.workflow.{id}`` component name. Thing-group targets are
    resolved only when they were recorded with their members.
    """
    if not DEPLOYMENTS_TABLE:
        return []
    component_name = f"dda.workflow.{workflow_id}"
    devices: List[str] = []
    kwargs: Dict[str, Any] = {
        'IndexName': 'usecase-deployments-index',
        'KeyConditionExpression': 'usecase_id = :uid',
        'ExpressionAttributeValues': {':uid': usecase_id},
    }
    store = dynamodb.Table(DEPLOYMENTS_TABLE)
    while True:
        response = store.query(**kwargs)
        for deployment in response.get('Items', []) or []:
            status = str(deployment.get('deployment_status') or '').upper()
            if status not in ACTIVE_DEPLOYMENT_STATUSES:
                continue
            references = (
                deployment.get('component_type') == 'workflow'
                and str(deployment.get('workflow_id') or '') == workflow_id)
            for component in deployment.get('components') or []:
                if isinstance(component, dict):
                    if component.get('component_name') == component_name:
                        references = True
                elif component == component_name:
                    references = True
            if not references:
                continue
            for device in deployment.get('target_devices') or []:
                if device and str(device) not in devices:
                    devices.append(str(device))
            for device in deployment.get('target_device_names') or []:
                if device and str(device) not in devices:
                    devices.append(str(device))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return sorted(devices)


def device_eligibility(session: Dict, samples: List[Dict]) -> Dict[str, Any]:
    """Which devices may execute a Device_Score_Job for this session.

    A device is eligible when it exported Tuning_Samples for the node AND
    the Portal records it as running the workflow (Requirement 6.9). The
    ineligible list — devices that exported samples but do not report the
    registration — is what the 400 names.
    """
    exported: List[str] = []
    for item in samples:
        thing = item.get('thingName')
        if thing and str(thing) not in exported:
            exported.append(str(thing))
    exported.sort()
    registered = registered_devices(session['usecaseId'],
                                    session['workflowId'])
    eligible = [d for d in exported if d in registered]
    return {
        'exported': exported,
        'registered': registered,
        'eligible': eligible,
        'ineligible': [d for d in exported if d not in registered],
    }


def acquire_run_lock(session_id: str, run_id: str) -> Optional[str]:
    """Take the session's single run slot, or name the run that holds it.

    At most one Score_Run may be in progress per Tuning_Session
    (Requirement 6.10): the lock is one item written conditionally. A lock
    held by a run that is already terminal, that has vanished, or whose
    60-minute budget has expired is taken over — and the expired run is
    finalized as failed with its partial Score_Summary (Requirement 10.4)
    so a dead step can never block a session forever.
    """
    store = table()
    lock = {**run_lock_key(session_id), 'runId': run_id,
            'acquiredAt': now_ms()}
    try:
        store.put_item(Item=lock,
                       ConditionExpression='attribute_not_exists(sk)')
        return None
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') != \
                'ConditionalCheckFailedException':
            raise

    existing = store.get_item(Key=run_lock_key(session_id)).get('Item') or {}
    holder_id = existing.get('runId')
    holder = get_run(session_id, str(holder_id)) if holder_id else None
    if holder and holder.get('status') == RUN_RUNNING \
            and not run_is_stale(holder):
        return str(holder_id)
    try:
        store.put_item(
            Item=lock, ConditionExpression='runId = :held',
            ExpressionAttributeValues={':held': holder_id})
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') != \
                'ConditionalCheckFailedException':
            raise
        # Another request took the slot in between.
        return str(holder_id) if holder_id else run_id
    if holder and holder.get('status') == RUN_RUNNING:
        finalize_run(get_session(session_id) or {'sessionId': session_id},
                     holder, RUN_FAILED, RUN_STALE_MESSAGE,
                     release_lock=False)
    return None


def release_run_lock(session_id: str, run_id: str) -> None:
    """Free the session's run slot, but only when this run holds it."""
    try:
        table().delete_item(
            Key=run_lock_key(session_id),
            ConditionExpression='runId = :r',
            ExpressionAttributeValues={':r': run_id})
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') != \
                'ConditionalCheckFailedException':
            raise


def run_is_stale(run: Dict) -> bool:
    """Whether a running Score_Run has outlived its 60-minute budget
    (Requirement 10.4)."""
    started = int(run.get('startedAt') or 0)
    return bool(started) and (now_s() - started) > RUN_STALE_SECONDS


RUN_STALE_MESSAGE = (
    'The score run could not be resumed within 60 minutes of its start '
    'and was finalized with its partial score summary')


def start_score_run(event: Dict, user: Dict, session_id: str) -> Dict:
    """POST .../sessions/{id}/score-runs  {candidateId, repeats,
    deviceThingName?}

    Admits a Score_Run and hands it to its scorer, answering 202 with the
    invocation count the run will issue (Requirements 6.6, 6.8, 6.9,
    6.10, 6.13):

    - repeats outside 1..3 and more than 600 planned invocations are
      rejected before any invocation is issued;
    - a second run in the session is rejected naming the in-progress one;
    - an ``llm_inference`` node is scored by a Device_Score_Job on a
      device that exported samples and reports the workflow — without one
      the run is refused naming the devices that exported samples but do
      not report the registration;
    - a ``bedrock_inference`` node is scored by the Portal's chunked
      Bedrock_Scorer.
    """
    session, workflow_item = session_context(user, event, session_id,
                                             Permission.WORKFLOW_EDIT)
    body = parse_body(event)
    candidate_id = body.get('candidateId') or body.get('candidate_id')
    if not candidate_id:
        raise TuningError(400, 'MISSING_FIELDS', 'candidateId is required')
    repeats = validate_repeats(body.get('repeats'))
    candidate = get_candidate(session_id, str(candidate_id))
    if not candidate:
        raise TuningError(404, 'CANDIDATE_NOT_FOUND', 'Candidate not found')

    version, document = load_latest_definition(workflow_item)
    node = find_node(document, session['nodeId'])
    if not node_is_tunable(node):
        parameters = (node or {}).get('parameters') or {}
        raise TuningError(
            400, 'NODE_NOT_TUNABLE',
            f"Node '{session['nodeId']}' is not an anomaly-mode Bedrock or "
            f"VLM inspection node in version {version}",
            {'nodeId': session['nodeId'], 'nodeType': (node or {}).get('type'),
             'anomaly_mode': parameters.get('anomaly_mode'),
             'version': version})

    samples = scored_samples(session_id)
    if not samples:
        raise TuningError(
            400, 'NO_LABELLED_SAMPLES',
            'No samples are labelled OK or NOK, so there is nothing to '
            'score')
    planned = len(samples) * repeats
    if planned > MAX_PLANNED_INVOCATIONS:
        raise TuningError(
            400, 'RUN_TOO_LARGE',
            f'A score run is bounded to {MAX_PLANNED_INVOCATIONS} '
            f'invocations (labelled samples × repeats); this run would '
            f'issue {planned}',
            {'plannedInvocations': planned, 'samples': len(samples),
             'repeats': repeats, 'bound': MAX_PLANNED_INVOCATIONS})

    node_type = node.get('type')
    mode = MODE_DEVICE if node_type == NODE_TYPE_LLM_INFERENCE \
        else MODE_BEDROCK
    device_thing_name = None
    eligibility = None
    if mode == MODE_DEVICE:
        eligibility = device_eligibility(session, samples)
        requested = (body.get('deviceThingName')
                     or body.get('device_thing_name'))
        if not eligibility['eligible']:
            raise TuningError(
                400, 'NO_ELIGIBLE_DEVICE',
                'No device both exported samples for this node and reports '
                'the workflow, so a VLM score run cannot be executed',
                eligibility)
        if requested:
            if str(requested) not in eligibility['eligible']:
                raise TuningError(
                    400, 'DEVICE_NOT_ELIGIBLE',
                    f"Device '{requested}' cannot execute this run: it must "
                    f"have exported samples for the node and report the "
                    f"workflow",
                    {**eligibility, 'requested': str(requested)})
            device_thing_name = str(requested)
        elif len(eligibility['eligible']) == 1:
            # The picker has exactly one choice; not making it is not an
            # error (Requirement 6.6 is about showing the device).
            device_thing_name = eligibility['eligible'][0]
        else:
            raise TuningError(
                400, 'DEVICE_REQUIRED',
                'deviceThingName is required: more than one device can '
                'execute this run', eligibility)

    run_id = str(uuid.uuid4())
    in_progress = acquire_run_lock(session_id, run_id)
    if in_progress:
        raise TuningError(
            409, 'RUN_IN_PROGRESS',
            f'Score run {in_progress} is still in progress for this tuning '
            f'session; cancel it before starting another',
            {'runId': in_progress})

    prompt_set = {'prompt': candidate.get('prompt'),
                  'systemPrompt': candidate.get('systemPrompt'),
                  'maxTokens': candidate.get('maxTokens')}
    timestamp = now_s()
    run = {
        'pk': session_pk(session_id),
        'sk': run_sk(run_id),
        'runId': run_id,
        'sessionId': session_id,
        'usecaseId': session.get('usecaseId'),
        'workflowId': session.get('workflowId'),
        'nodeId': session.get('nodeId'),
        'nodeType': node_type,
        # Parameters only — never a credential (Requirement 9.3). Snapshotted
        # so every step of the run replays through the same Node_Parameters
        # the run was admitted with (Requirement 6.1).
        'nodeParameters': node.get('parameters') or {},
        'promptSet': prompt_set,
        'definitionVersion': version,
        'candidateId': str(candidate_id),
        'candidateName': candidate.get('name'),
        'mode': mode,
        'status': RUN_RUNNING,
        'repeats': repeats,
        'plannedInvocations': planned,
        'plannedSamples': [{'sampleId': i['sampleId'], 'label': i['label']}
                           for i in samples],
        'cursor': 0,
        'done': 0,
        'cancelRequested': False,
        'ingestedBatches': [],
        'deviceThingName': device_thing_name,
        'startedAt': timestamp,
        'startedBy': user['user_id'],
        'lastProgressAt': timestamp,
        'finishedAt': None,
        'summary': summarize_outcomes([]),
        'error': None,
    }
    table().put_item(Item=to_dynamo(run))
    table().put_item(Item={**run_pointer_key(run_id), 'runId': run_id,
                           'sessionId': session_id,
                           'createdAt': now_ms()})

    dispatched: Dict[str, Any] = {}
    try:
        if mode == MODE_DEVICE:
            dispatched = dispatch_score_job(session, run, samples)
        else:
            dispatch_action({'action': ACTION_EXECUTE_SCORE_RUN,
                             'run_id': run_id, 'session_id': session_id,
                             'cursor': 0})
    except Exception as exc:  # noqa: BLE001 - the run reports the failure
        logger.error(f"Score run {run_id} could not be dispatched: {exc}",
                     exc_info=True)
        run = finalize_run(session, run, RUN_FAILED,
                           f'The score run could not be started: {exc}')
        return create_response(200, {'run': run_view(run),
                                     'dispatchFailed': True})
    if dispatched:
        run = update_run(session_id, run_id,
                         'SET jobId = :j, manifestKey = :m, '
                         'lastProgressAt = :t',
                         {':j': dispatched.get('jobId'),
                          ':m': dispatched.get('manifestKey'),
                          ':t': now_s()}) or run
        dispatch_action({'action': ACTION_POLL_SCORE_JOB,
                         'run_id': run_id, 'session_id': session_id,
                         'immediate': True})

    log_audit_event(
        user_id=user['user_id'], action='start_tuning_score_run',
        resource_type='workflow', resource_id=session['workflowId'],
        result='success',
        details={'usecase_id': session.get('usecaseId'),
                 'session_id': session_id, 'run_id': run_id,
                 'candidate_id': str(candidate_id), 'mode': mode,
                 'repeats': repeats, 'planned_invocations': planned,
                 'device_thing_name': device_thing_name})

    return create_response(202, {
        'run': run_view(run),
        'runId': run_id,
        'plannedInvocations': planned,
        'samples': len(samples),
        'repeats': repeats,
        'mode': mode,
        'deviceThingName': device_thing_name,
        'deviceEligibility': eligibility,
    })


# ==========================================================================
# Bedrock_Scorer (Requirements 6.1, 6.3, 6.5, 6.8, 6.11, 6.13, 10.4)
# ==========================================================================

def plan_units(run: Dict) -> List[Tuple[str, str, int]]:
    """The run's ``(sampleId, label, repeat)`` units, in order.

    Derived from the plan persisted at admission, so relabelling a sample
    mid-run neither adds nor removes an invocation and a resumed step
    walks the very same list.
    """
    repeats = max(MIN_REPEATS, min(MAX_REPEATS,
                                   int(run.get('repeats') or DEFAULT_REPEATS)))
    units: List[Tuple[str, str, int]] = []
    for entry in run.get('plannedSamples') or []:
        sample_id = entry.get('sampleId')
        if not sample_id:
            continue
        for repeat in range(1, repeats + 1):
            units.append((str(sample_id), entry.get('label'), repeat))
    return units


def persisted_units(run_id: str) -> set:
    """The ``(sampleId, repeat)`` pairs already persisted, so a resumed
    run never re-issues one (Requirement 10.4, Property 10)."""
    persisted = set()
    for item in query_items(run_pk(run_id), 'OUT#', projection='sk'):
        parts = str(item.get('sk', '')).split('#')
        if len(parts) < 3:
            continue
        try:
            persisted.add(('#'.join(parts[1:-1]), int(parts[-1])))
        except ValueError:
            continue
    return persisted


def read_sample_bytes(client, bucket: str,
                      key: Optional[str]) -> Optional[bytes]:
    if not key:
        return None
    return client.get_object(Bucket=bucket, Key=key)['Body'].read()


def invoke_bedrock(client, invocation) -> Tuple[str, Optional[int]]:
    """Issue exactly one Converse request and return
    ``(answer_text, output_tokens)`` — the executor's own transport shape
    (Requirement 6.3)."""
    response = client.converse(**invocation.converse_kwargs())
    parts = ((response.get('output') or {}).get('message') or {}
             ).get('content') or []
    text = ''.join(part.get('text', '') for part in parts
                   if isinstance(part, dict))
    usage = response.get('usage') or {}
    tokens = usage.get('outputTokens')
    try:
        tokens = int(tokens) if tokens is not None else None
    except (TypeError, ValueError):
        tokens = None
    return text, tokens


def replay_bedrock_unit(client, parameters: Dict[str, Any], sample_id: str,
                        label: str, repeat: int,
                        images: Tuple[Optional[bytes], Optional[bytes]],
                        read_error: Optional[str],
                        thing_name: Optional[str] = None) -> Dict:
    """Replay one Candidate on one sample once: the Sample_Outcome.

    The whole path is the executor's (Requirements 6.1, 6.2, 6.5): the
    shared Invocation_Builder constructs the request from the
    Node_Parameters with the Candidate's Prompt_Set laid over them, the
    shared parser reads the answer and the shared categorizer assigns
    exactly one category.
    """
    outcome: Dict[str, Any] = {'sampleId': sample_id, 'repeat': int(repeat),
                               'label': label, 'thingName': thing_name}
    if label not in (LABEL_OK, LABEL_NOK):
        # Only labelled, non-excluded samples are ever planned
        # (Requirement 4.3); a planned unit without one is a defect that
        # must not be silently counted as correct.
        outcome.update({'category': categorize_outcome(
            LABEL_OK, None, 'the sample is not labelled OK or NOK'),
            'error': 'the sample is not labelled OK or NOK'})
        return outcome
    if read_error is not None:
        outcome.update({'category': categorize_outcome(label, None,
                                                       read_error),
                        'error': read_error})
        return outcome
    input_bytes, reference_bytes = images
    invocation = build_bedrock_invocation(parameters, input_bytes or b'',
                                         reference_bytes)
    started = time.time()
    try:
        text, tokens = invoke_bedrock(client, invocation)
    except Exception as exc:  # noqa: BLE001 - one outcome, the run continues
        outcome['latencyMs'] = int((time.time() - started) * 1000)
        outcome['category'] = categorize_outcome(
            label, None, f"{type(exc).__name__}: {exc}")
        outcome['error'] = f"{type(exc).__name__}: {exc}"
        return outcome
    outcome['latencyMs'] = int((time.time() - started) * 1000)
    outcome['rawAnswer'] = text
    outcome['outputTokens'] = tokens
    try:
        verdict = parse_verdict(text)
    except ValueError as exc:
        outcome['category'] = categorize_outcome(label, None, None)
        outcome['parseError'] = str(exc)
        return outcome
    outcome['category'] = categorize_outcome(label, verdict, None)
    outcome['isAnomalous'] = bool(verdict.get('is_anomalous'))
    outcome['confidence'] = verdict.get('confidence')
    return outcome


def execute_score_run(event: Dict) -> Dict:
    """The ``execute_score_run`` action: one Bedrock execution step.

    Up to :data:`CHUNK_INVOCATIONS` invocations with at most
    :data:`SCORE_THREADS` in flight, each outcome persisted as it
    completes, then either a self-invoke with the next cursor or the
    finalize (Requirements 6.8, 6.11, 6.13, 10.4).
    """
    run_id = str(event.get('run_id') or '')
    cursor = int(event.get('cursor') or 0)
    try:
        session, run = resolve_run(run_id)
    except TuningError:
        logger.warning(f"Score run {run_id} no longer exists; step skipped")
        return {'status': 'gone', 'runId': run_id}
    session_id = session['sessionId']

    if run.get('status') != RUN_RUNNING:
        return {'status': run.get('status'), 'runId': run_id}
    if run.get('cancelRequested'):
        run = finalize_run(session, run, RUN_CANCELLED)
        return {'status': run.get('status'), 'runId': run_id}
    if run_is_stale(run):
        run = finalize_run(session, run, RUN_FAILED, RUN_STALE_MESSAGE)
        return {'status': run.get('status'), 'runId': run_id}

    units = plan_units(run)
    persisted = persisted_units(run_id)
    remaining = [(index, unit) for index, unit in enumerate(units)
                 if index >= cursor and (unit[0], unit[2]) not in persisted]
    if not remaining:
        run = finalize_run(session, run, RUN_COMPLETED)
        return {'status': run.get('status'), 'runId': run_id,
                'done': run.get('done')}
    chunk = remaining[:CHUNK_INVOCATIONS]
    next_cursor = chunk[-1][0] + 1

    usecase = get_usecase(session['usecaseId'])
    client, bucket = sample_store(usecase)
    sample_index = {i['sampleId']: i for i in sample_items(session_id)}
    parameters = invocation_parameters(
        {'type': run.get('nodeType'),
         'parameters': run.get('nodeParameters') or {}},
        run.get('promptSet') or {})
    region = str(parameters.get('region') or BEDROCK_DEFAULT_REGION)

    # Sample bytes are read once per sample per step, never persisted and
    # never sent anywhere but Bedrock (Requirements 9.3, 9.5).
    wanted = list(dict.fromkeys(unit[0] for _index, unit in chunk))

    def load(sample_id: str):
        item = sample_index.get(sample_id) or {}
        key = item.get('inputKey')
        try:
            input_bytes = read_sample_bytes(client, bucket, key)
            if input_bytes is None:
                return sample_id, (None, None), (
                    'the sample has no input image key')
            reference_bytes = read_sample_bytes(client, bucket,
                                                item.get('referenceKey'))
            return sample_id, (input_bytes, reference_bytes), None
        except Exception as exc:  # noqa: BLE001 - per-sample outcome
            return sample_id, (None, None), (
                f"could not read the sample image {key}: "
                f"{type(exc).__name__}: {exc}")

    images: Dict[str, Tuple[Any, Optional[str]]] = {}
    with ThreadPoolExecutor(max_workers=S3_READ_THREADS) as pool:
        for sample_id, payload, error in pool.map(load, wanted):
            images[sample_id] = (payload, error)

    bedrock = bedrock_client(region)

    def replay(entry):
        _index, (sample_id, label, repeat) = entry
        payload, error = images.get(sample_id, ((None, None), None))
        return replay_bedrock_unit(
            bedrock, parameters, sample_id, label, repeat, payload, error,
            (sample_index.get(sample_id) or {}).get('thingName'))

    with ThreadPoolExecutor(max_workers=SCORE_THREADS) as pool:
        outcomes = list(pool.map(replay, chunk))

    persist_outcomes(run_id, session_id, outcomes)
    done = len(persisted_units(run_id))
    summary = summarize_outcomes(outcome_items(run_id))
    run = update_run(session_id, run_id,
                     'SET #cur = :c, done = :d, lastProgressAt = :t, '
                     'summary = :s',
                     {':c': next_cursor, ':d': done, ':t': now_s(),
                      ':s': summary, ':running': RUN_RUNNING},
                     condition='#st = :running',
                     names={'#st': 'status', '#cur': 'cursor'}
                     ) or get_run(session_id, run_id)
    if run is None or run.get('status') != RUN_RUNNING:
        return {'status': (run or {}).get('status'), 'runId': run_id}

    if len(remaining) > len(chunk):
        dispatch_action({'action': ACTION_EXECUTE_SCORE_RUN,
                         'run_id': run_id, 'session_id': session_id,
                         'cursor': next_cursor})
        return {'status': RUN_RUNNING, 'runId': run_id, 'done': done,
                'cursor': next_cursor, 'issued': len(chunk)}
    run = finalize_run(session, run, RUN_COMPLETED)
    return {'status': run.get('status'), 'runId': run_id,
            'done': run.get('done'), 'issued': len(chunk)}


# ==========================================================================
# Device_Score_Job dispatcher (Requirements 6.9, 6.12, 9.6)
# ==========================================================================

def job_manifest_key(job_id: str) -> str:
    return f"{tuning_settings.JOB_STORE_PREFIX}{job_id}/manifest.json"


def run_outcomes_prefix(session_id: str, run_id: str) -> str:
    """Where the device appends this run's outcome batches — the prefix
    ``job_runner.outcomes_prefix`` computes on the device."""
    return f"{session_runs_prefix(session_id)}runs/{run_id}/"


def build_job_manifest(session: Dict, run: Dict,
                       samples: List[Dict]) -> Dict[str, Any]:
    """The Device_Score_Job manifest.

    Identifiers, the Candidate's Prompt_Set, the Node_Parameters and the
    Sample_Store keys of the samples to replay — and nothing else
    (Requirement 9.6): no image bytes, no credentials, no presigned URLs.
    """
    prompt_set = run.get('promptSet') or {}
    parameters = invocation_parameters(
        {'type': run.get('nodeType'),
         'parameters': run.get('nodeParameters') or {}}, prompt_set)
    manifest_samples = []
    for item in samples:
        entry: Dict[str, Any] = {
            'sampleId': item.get('sampleId'),
            'inputKey': item.get('inputKey'),
            'label': item.get('label'),
        }
        if item.get('referenceKey'):
            entry['referenceKey'] = item['referenceKey']
        snippet = (item.get('sidecar') or {}).get('metadataSnippet')
        if isinstance(snippet, dict):
            entry['metadataSnippet'] = snippet
        manifest_samples.append(entry)
    return {
        'schemaVersion': 1,
        'jobId': run.get('jobId'),
        'sessionId': session['sessionId'],
        'runId': run['runId'],
        'workflowId': session.get('workflowId'),
        'nodeId': session.get('nodeId'),
        'nodeType': run.get('nodeType'),
        'nodeParameters': parameters,
        'promptSet': prompt_set,
        'repeats': int(run.get('repeats') or DEFAULT_REPEATS),
        'samples': manifest_samples,
    }


def dispatch_score_job(session: Dict, run: Dict,
                       samples: List[Dict]) -> Dict[str, Any]:
    """Deliver a Device_Score_Job: the manifest to S3, then
    ``desired.jobs[jobId]`` on the device's ``dda-workflow-tuning`` named
    shadow through the Use_Case's ``iot-data`` client (Requirement 6.9)."""
    job_id = str(uuid.uuid4())
    key = job_manifest_key(job_id)
    manifest = build_job_manifest(session, {**run, 'jobId': job_id}, samples)
    usecase = get_usecase(session['usecaseId'])
    client, bucket = sample_store(usecase)
    client.put_object(Bucket=bucket, Key=key,
                      Body=json.dumps(manifest, default=str).encode('utf-8'),
                      ContentType='application/json')
    iot_data_client(usecase).update_thing_shadow(
        thingName=run['deviceThingName'],
        shadowName=TUNING_SHADOW_NAME,
        payload=json.dumps({'state': {'desired': {SHADOW_JOBS_KEY: {
            job_id: {'manifestKey': key, 'cancel': False}}}}}).encode('utf-8'))
    logger.info(f"Device_Score_Job {job_id} delivered to "
                f"{run['deviceThingName']} for run {run['runId']}")
    return {'jobId': job_id, 'manifestKey': key}


def update_desired_job(session: Dict, run: Dict,
                       entry: Optional[Dict]) -> None:
    """Write (or, with ``entry=None``, remove) this run's
    ``desired.jobs[jobId]`` entry. Best effort: a shadow that cannot be
    written is logged, never raised — the run's own status is the truth
    the Portal reports."""
    job_id = run.get('jobId')
    thing_name = run.get('deviceThingName')
    if not job_id or not thing_name:
        return
    try:
        iot_data_client(get_usecase(session['usecaseId'])
                        ).update_thing_shadow(
            thingName=thing_name, shadowName=TUNING_SHADOW_NAME,
            payload=json.dumps({'state': {'desired': {SHADOW_JOBS_KEY: {
                job_id: entry}}}}).encode('utf-8'))
    except Exception as exc:  # noqa: BLE001 - best effort by contract
        logger.warning(f"Could not update desired.jobs[{job_id}] on "
                       f"{thing_name}: {exc}")


def read_reported_job(session: Dict, run: Dict) -> Optional[Dict]:
    """``reported.jobs[jobId]`` from the device's tuning shadow, or None
    when the shadow (or the entry) does not exist yet."""
    job_id = run.get('jobId')
    thing_name = run.get('deviceThingName')
    if not job_id or not thing_name:
        return None
    try:
        response = iot_data_client(get_usecase(session['usecaseId'])
                                   ).get_thing_shadow(
            thingName=thing_name, shadowName=TUNING_SHADOW_NAME)
        payload = response['payload'].read()
        document = json.loads(payload)
    except Exception as exc:  # noqa: BLE001 - unreadable => no progress yet
        logger.info(f"Tuning shadow of {thing_name} is unreadable: {exc}")
        return None
    reported = ((document.get('state') or {}).get('reported') or {})
    jobs = reported.get(SHADOW_JOBS_KEY)
    entry = (jobs or {}).get(job_id) if isinstance(jobs, dict) else None
    return entry if isinstance(entry, dict) else None


def ingest_outcome_batches(session: Dict, run: Dict) -> Dict[str, Any]:
    """Ingest the device's ``outcomes-*.json`` batches exactly once.

    Every batch object under the run's own prefix that has not been
    ingested yet is read and its Sample_Outcomes persisted; each outcome
    write is conditional on its ``(sample, repeat)`` item not existing, so
    a re-read batch adds nothing (Requirement 6.12, Property 11).
    """
    session_id = session['sessionId']
    run_id = run['runId']
    ingested = [str(k) for k in (run.get('ingestedBatches') or [])]
    result = {'batches': 0, 'outcomes': 0, 'errors': 0}
    try:
        usecase = get_usecase(session['usecaseId'])
        client, bucket = sample_store(usecase)
        listing, _truncated = list_objects(
            client, bucket, run_outcomes_prefix(session_id, run_id))
    except Exception as exc:  # noqa: BLE001 - polled again on the next step
        logger.warning(f"Could not list outcome batches of run {run_id}: "
                       f"{exc}")
        return result
    new_keys = sorted(key for key in listing
                      if key not in ingested and key.endswith('.json'))
    for key in new_keys:
        try:
            body = client.get_object(Bucket=bucket, Key=key)['Body'].read()
            document = json.loads(body.decode('utf-8'))
        except Exception as exc:  # noqa: BLE001 - a batch may be mid-write
            logger.warning(f"Outcome batch {key} is unreadable: {exc}")
            result['errors'] += 1
            continue
        outcomes = document.get('outcomes') if isinstance(document, dict) \
            else None
        outcomes = [o for o in (outcomes or []) if isinstance(o, dict)]
        for outcome in outcomes:
            outcome.setdefault('thingName',
                               (document.get('thingName')
                                if isinstance(document, dict) else None)
                               or run.get('deviceThingName'))
        result['outcomes'] += persist_outcomes(run_id, session_id, outcomes)
        ingested.append(key)
        result['batches'] += 1
    if result['batches']:
        update_run(session_id, run_id,
                   'SET ingestedBatches = :b, lastProgressAt = :t',
                   {':b': ingested, ':t': now_s()})
    return result


def poll_score_job(event: Dict) -> Dict:
    """The ``poll_score_job`` action: one Device_Score_Job poll step.

    Ingests every new outcome batch, then decides the run's fate from the
    device's report (Requirements 6.9, 6.11, 6.12): ``completed`` only
    when the device reports ``done == total``, ``failed`` on a reported
    failure or 15 minutes without progress, ``cancelled`` when the device
    confirms the cancellation. While the job is alive the step
    re-invokes itself after :data:`POLL_INTERVAL_SECONDS`.
    """
    run_id = str(event.get('run_id') or '')
    try:
        session, run = resolve_run(run_id)
    except TuningError:
        logger.warning(f"Score run {run_id} no longer exists; poll skipped")
        return {'status': 'gone', 'runId': run_id}
    session_id = session['sessionId']

    if not event.get('immediate'):
        delay = POLL_INTERVAL_SECONDS
        if delay:
            time.sleep(delay)
        run = get_run(session_id, run_id) or run

    ingestion = ingest_outcome_batches(session, run)
    run = get_run(session_id, run_id) or run

    if run.get('status') != RUN_RUNNING:
        # A terminal run: the trailing batch has just been ingested, so
        # refresh the summary, stop the device and stop polling.
        summary = summarize_outcomes(outcome_items(run_id))
        update_run(session_id, run_id, 'SET summary = :s, done = :d',
                   {':s': summary, ':d': len(persisted_units(run_id))})
        update_desired_job(session, run, None)
        return {'status': run.get('status'), 'runId': run_id,
                'ingested': ingestion}

    reported = read_reported_job(session, run)
    if run.get('cancelRequested'):
        update_desired_job(session, run, {'manifestKey': run.get('manifestKey'),
                                          'cancel': True})
        if reported and str(reported.get('status')) in (
                JOB_STATUS_CANCELLED, JOB_STATUS_COMPLETED,
                JOB_STATUS_FAILED):
            run = finalize_run(session, run, RUN_CANCELLED)
            return {'status': run.get('status'), 'runId': run_id}
    if reported:
        update_run(session_id, run_id, 'SET reportedJob = :r',
                   {':r': reported})
        status = str(reported.get('status') or '')
        done = _int_or_none(reported.get('done'))
        total = _int_or_none(reported.get('total'))
        if status == JOB_STATUS_FAILED:
            run = finalize_run(
                session, run, RUN_FAILED,
                f"The device reported the score job failed: "
                f"{reported.get('error') or 'no reason reported'}")
            return {'status': run.get('status'), 'runId': run_id}
        if status == JOB_STATUS_CANCELLED:
            run = finalize_run(session, run, RUN_CANCELLED)
            return {'status': run.get('status'), 'runId': run_id}
        if status == JOB_STATUS_COMPLETED:
            if done is not None and total is not None and done == total:
                run = finalize_run(session, run, RUN_COMPLETED)
            else:
                run = finalize_run(
                    session, run, RUN_FAILED,
                    f"The device reported the score job complete with "
                    f"{done} of {total} invocations")
            return {'status': run.get('status'), 'runId': run_id}

    last_progress = int(run.get('lastProgressAt')
                        or run.get('startedAt') or now_s())
    if (now_s() - last_progress) > JOB_SILENCE_SECONDS:
        run = finalize_run(
            session, run, RUN_FAILED,
            'The device reported no progress for 15 minutes')
        return {'status': run.get('status'), 'runId': run_id}

    dispatch_action({'action': ACTION_POLL_SCORE_JOB, 'run_id': run_id,
                     'session_id': session_id})
    return {'status': RUN_RUNNING, 'runId': run_id, 'ingested': ingestion,
            'reported': reported}


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ==========================================================================
# Finalize and prune (Requirements 6.14, 10.3)
# ==========================================================================

def finalize_run(session: Dict, run: Dict, status: str,
                 error: Optional[str] = None,
                 release_lock: bool = True) -> Dict:
    """Finalize a Score_Run with its (partial) Score_Summary.

    The summary is recomputed from the persisted Sample_Outcomes on every
    finalize — completed, cancelled or failed — so it is always a function
    of what was actually produced (Requirements 6.11, 6.14, Property 9).
    Finalizing also frees the session's run slot, stops the device job and
    prunes the Candidate's run history (Requirement 10.3).
    """
    session_id = session.get('sessionId') or run.get('sessionId')
    run_id = run['runId']
    outcomes = outcome_items(run_id)
    summary = summarize_outcomes(outcomes)
    updated = update_run(
        session_id, run_id,
        'SET #st = :s, finishedAt = :f, summary = :sum, done = :d, '
        '#err = :e',
        {':s': status, ':f': now_s(), ':sum': summary,
         ':d': len(outcomes), ':e': error, ':running': RUN_RUNNING},
        names={'#st': 'status', '#err': 'error'},
        condition='#st = :running')
    if updated is None:
        # Already finalized by another step; report what is stored.
        return get_run(session_id, run_id) or run
    updated['error'] = error
    if release_lock:
        release_run_lock(session_id, run_id)
    if updated.get('jobId'):
        update_desired_job(session, updated, None)
    prune_candidate_runs(session_id, updated.get('candidateId'))
    logger.info(f"Score run {run_id} finalized as {status} with "
                f"{len(outcomes)} outcome(s)")
    return updated


def prune_candidate_runs(session_id: str,
                         candidate_id: Optional[str]) -> List[str]:
    """Keep at most the 20 most recent Score_Runs of a Candidate, deleting
    older runs with their Sample_Outcomes (Requirement 10.3)."""
    if not candidate_id:
        return []
    runs = [decimal_to_native(i)
            for i in query_items(session_pk(session_id), 'RUN#')]
    mine = [r for r in runs if r.get('candidateId') == candidate_id
            and r.get('runId')]
    mine.sort(key=lambda r: (int(r.get('startedAt') or 0), str(r['runId'])),
              reverse=True)
    stale = mine[MAX_RUNS_PER_CANDIDATE:]
    pruned: List[str] = []
    for run in stale:
        run_id = str(run['runId'])
        delete_items([{'pk': i['pk'], 'sk': i['sk']}
                      for i in query_items(run_pk(run_id),
                                           projection='pk, sk')])
        table().delete_item(Key={'pk': session_pk(session_id),
                                 'sk': run_sk(run_id)})
        pruned.append(run_id)
    if pruned:
        logger.info(f"Pruned {len(pruned)} score run(s) of candidate "
                    f"{candidate_id} beyond the {MAX_RUNS_PER_CANDIDATE} "
                    f"most recent")
    return pruned


# ==========================================================================
# Routes: run progress, outcomes, diff, cancel (Requirements 6.8, 6.11,
# 7.2, 7.3, 7.4)
# ==========================================================================

def live_run(session: Dict, run: Dict) -> Dict:
    """A run's view with the summary recomputed while it is running, so
    progress and the running Score_Summary are always current
    (Requirement 6.8)."""
    view = run_view(run)
    if run.get('status') == RUN_RUNNING:
        outcomes = outcome_items(run['runId'])
        view['summary'] = summarize_outcomes(outcomes)
        view['done'] = len(outcomes)
    return view


def get_score_run(event: Dict, user: Dict, run_id: str) -> Dict:
    """GET .../score-runs/{rid} — progress, the Score_Summary and the
    Candidate it scores."""
    session, run = run_context(user, event, run_id, Permission.WORKFLOW_READ)
    candidate = get_candidate(session['sessionId'],
                              str(run.get('candidateId') or ''))
    return create_response(200, {
        'run': live_run(session, run),
        'session': session_summary(session),
        'candidate': candidate_summary(candidate) if candidate else None,
    })


def list_run_outcomes(event: Dict, user: Dict, run_id: str) -> Dict:
    """GET .../score-runs/{rid}/outcomes?category=&cursor=&sort=

    Every Sample_Outcome with its sample's images, Label, category,
    parsed verdict, confidence and raw answer, filterable by category
    (Requirements 7.2, 7.4).
    """
    session, run = run_context(user, event, run_id, Permission.WORKFLOW_READ)
    params = event.get('queryStringParameters') or {}
    outcomes = outcome_items(run_id)
    category = params.get('category')
    if category:
        wanted = str(category).strip().lower()
        outcomes = [o for o in outcomes if o.get('category') == wanted]
    sort = str(params.get('sort') or '').strip().lower()
    if sort == 'confidence':
        descending = str(params.get('order') or 'desc').lower() != 'asc'
        outcomes.sort(key=lambda o: (o.get('confidence') is None,
                                     o.get('confidence') or 0),
                      reverse=descending)
    else:
        outcomes.sort(key=lambda o: (str(o.get('sampleId')),
                                     int(o.get('repeat') or 1)))
    try:
        limit = int(params.get('limit') or DEFAULT_OUTCOME_PAGE_SIZE)
    except (TypeError, ValueError):
        limit = DEFAULT_OUTCOME_PAGE_SIZE
    limit = max(1, min(limit, MAX_OUTCOME_PAGE_SIZE))
    offset = decode_cursor(params.get('cursor'))
    page = outcomes[offset:offset + limit]
    next_cursor = (encode_cursor(offset + limit)
                   if offset + limit < len(outcomes) else None)

    samples = {}
    wanted_ids = {o.get('sampleId') for o in page}
    if wanted_ids:
        baseline = session.get('baselineFingerprint')
        client = None
        bucket = None
        try:
            client, bucket = sample_store(get_usecase(session['usecaseId']))
        except Exception as exc:  # noqa: BLE001 - views degrade to no URLs
            logger.info(f"Sample_Store unavailable for run {run_id}: {exc}")
        for item in sample_items(session['sessionId']):
            if item.get('sampleId') in wanted_ids:
                samples[item['sampleId']] = sample_view(
                    item, baseline, client, bucket,
                    urls=client is not None)

    return create_response(200, {
        'runId': run_id,
        'run': live_run(session, run),
        'outcomes': [outcome_view(o) for o in page],
        'samples': samples,
        'count': len(page),
        'matched': len(outcomes),
        'nextCursor': next_cursor,
        'summary': summarize_outcomes(outcome_items(run_id)),
    })


def categories_by_sample(run_id: str) -> Dict[str, Dict[str, Any]]:
    """Per sample, the categories a run produced for it (a repeated
    sample can carry more than one)."""
    grouped: Dict[str, Dict[str, Any]] = {}
    for outcome in outcome_items(run_id):
        sample_id = outcome.get('sampleId')
        if not sample_id:
            continue
        entry = grouped.setdefault(sample_id, {'categories': [],
                                               'outcomes': []})
        entry['outcomes'].append(outcome_view(outcome))
        if outcome.get('category') not in entry['categories']:
            entry['categories'].append(outcome.get('category'))
    for entry in grouped.values():
        entry['categories'].sort(key=lambda c: str(c))
        entry['outcomes'].sort(key=lambda o: int(o.get('repeat') or 1))
    return grouped


def diff_score_runs(event: Dict, user: Dict, run_id: str,
                    other_run_id: str) -> Dict:
    """GET .../score-runs/{rid}/diff/{other} — the samples on which the
    two runs' categories differ (Requirement 7.3)."""
    session, run = run_context(user, event, run_id, Permission.WORKFLOW_READ)
    other = get_run(session['sessionId'], str(other_run_id))
    if not other:
        raise TuningError(404, 'RUN_NOT_FOUND',
                          'The other score run does not belong to this '
                          'tuning session')
    left = categories_by_sample(run_id)
    right = categories_by_sample(str(other_run_id))
    labels = {i['sampleId']: i.get('label')
              for i in sample_items(session['sessionId'])}
    differing = []
    for sample_id in sorted(set(left) | set(right)):
        a = left.get(sample_id)
        b = right.get(sample_id)
        if a and b and a['categories'] == b['categories']:
            continue
        differing.append({
            'sampleId': sample_id,
            'label': labels.get(sample_id),
            'a': a, 'b': b,
        })
    return create_response(200, {
        'a': live_run(session, run),
        'b': live_run(session, other),
        'differing': differing,
        'count': len(differing),
    })


def cancel_score_run(event: Dict, user: Dict, run_id: str) -> Dict:
    """POST .../score-runs/{rid}/cancel — stop the run, keep everything it
    already produced (Requirement 6.11)."""
    session, run = run_context(user, event, run_id, Permission.WORKFLOW_EDIT)
    if run.get('status') != RUN_RUNNING:
        return create_response(200, {'run': run_view(run),
                                     'alreadyFinished': True})
    run = update_run(session['sessionId'], run_id,
                     'SET cancelRequested = :c, cancelledBy = :u',
                     {':c': True, ':u': user['user_id']}) or run
    if run.get('jobId'):
        # Ask the device to stop after the batch in flight; the poll step
        # ingests that batch before it stops.
        update_desired_job(session, run,
                           {'manifestKey': run.get('manifestKey'),
                            'cancel': True})
    run = finalize_run(session, run, RUN_CANCELLED)
    log_audit_event(
        user_id=user['user_id'], action='cancel_tuning_score_run',
        resource_type='workflow', resource_id=session['workflowId'],
        result='success',
        details={'usecase_id': session.get('usecaseId'),
                 'session_id': session['sessionId'], 'run_id': run_id,
                 'done': run.get('done')})
    return create_response(200, {'run': run_view(run)})


# ==========================================================================
# Route: selection (Requirement 7.5)
# ==========================================================================

def set_selection(event: Dict, user: Dict, session_id: str) -> Dict:
    """PUT .../sessions/{id}/selection  {candidateId}

    Marks exactly one Candidate of the session as selected and persists it
    (``null`` clears the selection). The selected Candidate's latest run's
    false-pass count is returned so the caller can show it prominently
    (Requirement 7.6).
    """
    session, _workflow_item = session_context(user, event, session_id,
                                              Permission.WORKFLOW_EDIT)
    body = parse_body(event)
    candidate_id = body.get('candidateId', body.get('candidate_id'))
    if candidate_id is not None:
        candidate_id = str(candidate_id)
        if not get_candidate(session_id, candidate_id):
            raise TuningError(404, 'CANDIDATE_NOT_FOUND',
                              'Candidate not found')
    updated = table().update_item(
        Key={'pk': session_pk(session_id), 'sk': 'META'},
        UpdateExpression=('SET selectedCandidateId = :c, selectedBy = :u, '
                          'selectedAt = :t, updatedAt = :t'),
        ExpressionAttributeValues={':c': candidate_id, ':u': user['user_id'],
                                   ':t': now_ms()},
        ReturnValues='ALL_NEW')
    session = decimal_to_native(updated['Attributes'])
    latest = latest_run_of(session_id, candidate_id) if candidate_id else None
    return create_response(200, {
        'session': session_summary(session),
        'selectedCandidateId': candidate_id,
        'latestRun': live_run(session, latest) if latest else None,
        'falsePasses': ((latest or {}).get('summary') or {}).get('falsePass'),
    })


def latest_run_of(session_id: str, candidate_id: str) -> Optional[Dict]:
    """The Candidate's most recently started Score_Run, if any."""
    runs = [decimal_to_native(i)
            for i in query_items(session_pk(session_id), 'RUN#')]
    mine = [r for r in runs if r.get('candidateId') == candidate_id]
    if not mine:
        return None
    mine.sort(key=lambda r: (int(r.get('startedAt') or 0),
                             str(r.get('runId'))))
    return mine[-1]


# ==========================================================================
# Route: apply (Requirement 8)
# ==========================================================================

def candidate_runs(session_id: str, candidate_id: str) -> List[Dict]:
    """A Candidate's Score_Runs, oldest first."""
    runs = [decimal_to_native(i)
            for i in query_items(session_pk(session_id), 'RUN#')]
    mine = [r for r in runs
            if r.get('candidateId') == candidate_id and r.get('runId')]
    mine.sort(key=lambda r: (int(r.get('startedAt') or 0), str(r['runId'])))
    return mine


def latest_completed_run(session_id: str,
                         candidate_id: str) -> Optional[Dict]:
    """The Candidate's most recent completed Score_Run, if any — what
    Requirement 8.2 requires before a Candidate may be applied."""
    completed = [r for r in candidate_runs(session_id, candidate_id)
                 if r.get('status') == RUN_COMPLETED]
    return completed[-1] if completed else None


def patch_prompt_set(document: Dict, node_id: str,
                     prompt_set: Dict[str, Any]) -> Dict:
    """A copy of ``document`` in which the target node's Prompt_Set
    parameters — and nothing else — carry the Candidate's values
    (Requirement 8.1).

    Exactly three parameters are written: the prompt (``prompt_template``
    for ``llm_inference``), ``system_prompt`` and ``max_tokens``. A value
    the Prompt_Set does not hold leaves its parameter alone rather than
    introducing an empty one, so applying a Candidate whose Prompt_Set
    equals the deployed node's produces a byte-identical document.
    """
    patched = json.loads(json.dumps(document))
    for node in definition_nodes(patched):
        if node.get('id') != node_id:
            continue
        parameters = node.get('parameters')
        if not isinstance(parameters, dict):
            parameters = {}
            node['parameters'] = parameters
        prompt_key = prompt_key_for(node.get('type'))
        prompt = prompt_set.get('prompt') or ''
        if prompt or prompt_key in parameters:
            parameters[prompt_key] = prompt
        system_prompt = prompt_set.get('systemPrompt') or ''
        if system_prompt or 'system_prompt' in parameters:
            parameters['system_prompt'] = system_prompt
        if prompt_set.get('maxTokens') is not None:
            parameters['max_tokens'] = int(prompt_set['maxTokens'])
        break
    return patched


def allocate_next_version(workflow_id: str, expected_version: int) -> Dict:
    """Atomically allocate the workflow's next version number.

    The designer save path's own allocation (``workflows.update_workflow``):
    ``latest_version + 1`` with ``updated_at`` refreshed, under
    ``attribute_exists(workflow_id)``. Applying additionally requires the
    version it patched to still be the latest — it is a read-modify-write
    of a document the caller never sent, so a concurrent designer save
    would otherwise be silently discarded (Requirement 8.1: every other
    node, parameter and connection byte-identical to the *previous latest*
    version). A losing caller sees 409 and can retry against the new
    latest version.
    """
    try:
        updated = dynamodb.Table(WORKFLOWS_TABLE).update_item(
            Key={'workflow_id': workflow_id},
            UpdateExpression=('SET latest_version = latest_version + :one, '
                              'updated_at = :updated'),
            ExpressionAttributeValues={':one': 1, ':updated': now_ms(),
                                       ':expected': expected_version},
            ConditionExpression=('attribute_exists(workflow_id) AND '
                                 'latest_version = :expected'),
            ReturnValues='ALL_NEW')
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') \
                != 'ConditionalCheckFailedException':
            raise
        raise TuningError(
            409, 'STALE_DEFINITION',
            'The workflow gained a new version while this candidate was '
            'being applied; reload the session and apply again',
            {'expectedVersion': expected_version})
    return decimal_to_native(updated['Attributes'])


def version_item(workflow_id: str, version: int) -> Dict:
    """The stored version item of a workflow version (may be empty)."""
    response = dynamodb.Table(WORKFLOW_VERSIONS_TABLE).get_item(
        Key={'workflow_id': workflow_id, 'version': version})
    return decimal_to_native(response.get('Item') or {})


def apply_candidate(event: Dict, user: Dict, session_id: str) -> Dict:
    """POST .../sessions/{id}/apply  {candidateId?, runId?}

    Saves the session's selected Candidate as a new Workflow_Definition
    version (Requirement 8):

    - ``workflow:save`` is required, checked before anything else
      (Requirements 9.1, 9.2);
    - the selected Candidate must have a completed Score_Run
      (Requirement 8.2) — ``runId`` may name which one, otherwise the most
      recent completed run is recorded;
    - a ``candidateId`` in the body must equal the session's selection, so
      the confirmation the user saw is the thing that is applied;
    - the target must still be a Tunable_Node of the latest version
      (Requirement 8.4);
    - exactly the three Prompt_Set parameters are written into the latest
      document, which is then stored through the designer save path
      (Requirements 8.1, 8.6, 11.4);
    - the application is recorded as the session's Tuning_Result and as an
      ``apply_prompt_tuning`` audit event (Requirement 8.3);
    - nothing is validated, packaged or deployed (Requirement 8.5).
    """
    session, workflow_item = session_context(user, event, session_id,
                                             Permission.WORKFLOW_SAVE)
    body = parse_body(event)

    selected = session.get('selectedCandidateId')
    if not selected:
        raise TuningError(
            409, 'NO_SELECTION',
            'Select the candidate to apply before applying it')
    candidate_id = str(selected)
    requested = body.get('candidateId', body.get('candidate_id'))
    if requested is not None and str(requested) != candidate_id:
        raise TuningError(
            409, 'CANDIDATE_NOT_SELECTED',
            f"Candidate '{requested}' is not the candidate selected for "
            f"this tuning session",
            {'selectedCandidateId': candidate_id,
             'requested': str(requested)})
    candidate = get_candidate(session_id, candidate_id)
    if not candidate:
        raise TuningError(404, 'CANDIDATE_NOT_FOUND', 'Candidate not found')

    requested_run = body.get('runId', body.get('run_id'))
    if requested_run is not None:
        run = get_run(session_id, str(requested_run))
        if not run or run.get('candidateId') != candidate_id:
            raise TuningError(404, 'RUN_NOT_FOUND',
                              'Score run not found for this candidate')
        if run.get('status') != RUN_COMPLETED:
            raise TuningError(
                409, 'RUN_NOT_COMPLETED',
                f"Score run {run['runId']} is {run.get('status')}, not "
                f"completed",
                {'runId': run['runId'], 'status': run.get('status')})
    else:
        run = latest_completed_run(session_id, candidate_id)
        if not run:
            raise TuningError(
                409, 'NO_COMPLETED_RUN',
                'The selected candidate has no completed score run, so it '
                'cannot be applied',
                {'candidateId': candidate_id})

    workflow_id = workflow_item['workflow_id']
    usecase_id = workflow_item['usecase_id']
    version, document = load_latest_definition(workflow_item)
    node = find_node(document, session['nodeId'])
    if not node_is_tunable(node):
        parameters = (node or {}).get('parameters') or {}
        raise TuningError(
            409, 'NODE_NOT_TUNABLE',
            f"Node '{session['nodeId']}' no longer exists in version "
            f"{version} of this workflow or is no longer in anomaly mode, "
            f"so the tuned prompt cannot be applied",
            {'nodeId': session['nodeId'],
             'nodeType': (node or {}).get('type'),
             'anomaly_mode': parameters.get('anomaly_mode'),
             'version': version})

    prompt_set = {'prompt': candidate.get('prompt'),
                  'systemPrompt': candidate.get('systemPrompt'),
                  'maxTokens': candidate.get('maxTokens')}
    patched = patch_prompt_set(document, session['nodeId'], prompt_set)
    canonical_json, definition_error = workflows.canonicalize_definition(
        patched)
    if definition_error is not None:
        # The designer save path's own rejection of this document, verbatim
        # (same error envelope, same codes).
        return definition_error

    # The Custom_Node_Type pins of the new version (custom-node-designer
    # 14.2). Applying changes no node type and no reference, so the
    # previous version's pins are exactly right when the fresh scan finds
    # nothing (the tuning Lambda may not read the catalog table).
    pins = workflows.custom_node_type_references(usecase_id, canonical_json)
    if not pins:
        previous = version_item(workflow_id, version)
        pins = previous.get('custom_node_types') or {}

    new_item = allocate_next_version(workflow_id, version)
    new_version = int(new_item['latest_version'])
    s3_key = workflows.put_definition(usecase_id, workflow_id, new_version,
                                      canonical_json)
    workflows.put_version_item(workflow_id, new_version, s3_key, user, pins)

    # Requirement 5.1: the workflow gained a new latest version, so the
    # session's Baseline_Candidate follows it. Contained — the version is
    # already saved, and a session refresh would do it anyway.
    try:
        stored = json.loads(canonical_json)
        new_node = find_node(stored, session['nodeId'])
        if new_node is not None:
            session = refresh_baseline(session, new_node, new_version, user)
    except Exception as exc:  # noqa: BLE001 - never fails a saved apply
        logger.warning(f"Baseline candidate of session {session_id} could "
                       f"not be refreshed after apply: {exc}")

    baseline_run = latest_completed_run(session_id, BASELINE_CANDIDATE_ID)
    result = {
        'appliedAt': now_ms(),
        'appliedBy': user['user_id'],
        'newVersion': new_version,
        'previousVersion': version,
        'candidateId': candidate_id,
        'candidateName': candidate.get('name'),
        'scoreRunId': run.get('runId'),
        'summary': run.get('summary'),
        'baselineSummary': (baseline_run or {}).get('summary'),
    }
    updated = table().update_item(
        Key={'pk': session_pk(session_id), 'sk': 'META'},
        UpdateExpression=('SET latestTuningResult = :r, appliedVersion = :v, '
                          'updatedAt = :u'),
        ExpressionAttributeValues=to_dynamo({':r': result, ':v': new_version,
                                            ':u': now_ms()}),
        ReturnValues='ALL_NEW')
    session = decimal_to_native(updated['Attributes'])

    log_audit_event(
        user_id=user['user_id'], action='apply_prompt_tuning',
        resource_type='workflow', resource_id=workflow_id, result='success',
        details={'usecase_id': usecase_id, 'workflow_id': workflow_id,
                 'version': new_version, 'previous_version': version,
                 'node_id': session.get('nodeId'), 'session_id': session_id,
                 'candidate_id': candidate_id,
                 'candidate_name': candidate.get('name'),
                 'score_run_id': run.get('runId')})
    logger.info(f"Applied tuning candidate {candidate_id} of session "
                f"{session_id} to workflow {workflow_id} as version "
                f"{new_version}")

    return create_response(200, {
        'session': session_summary(session),
        'workflowId': workflow_id,
        'nodeId': session.get('nodeId'),
        'version': new_version,
        'newVersion': new_version,
        'previousVersion': version,
        'promptSet': prompt_set,
        'candidate': candidate_summary(candidate),
        'run': run_view(run),
        'tuningResult': result,
    })


# ==========================================================================
# Routing
# ==========================================================================

def not_implemented(event: Dict) -> Dict:
    logger.info(f"workflow_tuning request {event.get('httpMethod')} "
                f"{event.get('resource', '')} is not implemented yet")
    return error_response(501, 'NOT_IMPLEMENTED', NOT_IMPLEMENTED_MESSAGE)


ANOMALY = '/workflow-tuning/anomaly'


def route(event: Dict, user: Dict) -> Dict:
    resource = event.get('resource', '')
    method = event.get('httpMethod')
    path_params = event.get('pathParameters') or {}
    session_id = path_params.get('id')
    candidate_id = path_params.get('cid')
    run_id = path_params.get('rid')
    other_run_id = path_params.get('other')

    if resource == f'{ANOMALY}/workflows' and method == 'GET':
        return overview(event, user)
    if resource == f'{ANOMALY}/sessions' and method == 'POST':
        return create_or_get_session(event, user)
    if resource == f'{ANOMALY}/sessions/{{id}}' and session_id:
        if method == 'GET':
            return view_session(event, user, session_id)
        if method == 'DELETE':
            return delete_session(event, user, session_id)
    if resource == f'{ANOMALY}/sessions/{{id}}/refresh' and session_id \
            and method == 'POST':
        return refresh_session(event, user, session_id)
    if resource == f'{ANOMALY}/sessions/{{id}}/samples' and session_id \
            and method == 'GET':
        return list_samples(event, user, session_id)
    if resource == f'{ANOMALY}/sessions/{{id}}/samples/labels' \
            and session_id and method == 'PUT':
        return set_labels(event, user, session_id)
    if resource == f'{ANOMALY}/sessions/{{id}}/synthetic-negatives' \
            and session_id and method == 'PUT':
        return toggle_synthetic_negatives(event, user, session_id)
    if resource == f'{ANOMALY}/sessions/{{id}}/candidates' and session_id \
            and method == 'POST':
        return create_candidate(event, user, session_id)
    if resource == f'{ANOMALY}/sessions/{{id}}/candidates/{{cid}}' \
            and session_id and candidate_id:
        if method == 'PUT':
            return update_candidate(event, user, session_id, candidate_id)
        if method == 'DELETE':
            return delete_candidate(event, user, session_id, candidate_id)
    if resource == f'{ANOMALY}/candidates/{{cid}}/preview' and candidate_id \
            and method == 'GET':
        return preview_candidate(event, user, candidate_id)
    if resource == f'{ANOMALY}/sessions/{{id}}/score-runs' and session_id \
            and method == 'POST':
        return start_score_run(event, user, session_id)
    if resource == f'{ANOMALY}/sessions/{{id}}/selection' and session_id \
            and method == 'PUT':
        return set_selection(event, user, session_id)
    if resource == f'{ANOMALY}/sessions/{{id}}/apply' and session_id \
            and method == 'POST':
        return apply_candidate(event, user, session_id)
    if resource == f'{ANOMALY}/score-runs/{{rid}}' and run_id \
            and method == 'GET':
        return get_score_run(event, user, run_id)
    if resource == f'{ANOMALY}/score-runs/{{rid}}/outcomes' and run_id \
            and method == 'GET':
        return list_run_outcomes(event, user, run_id)
    if resource == f'{ANOMALY}/score-runs/{{rid}}/cancel' and run_id \
            and method == 'POST':
        return cancel_score_run(event, user, run_id)
    if resource == f'{ANOMALY}/score-runs/{{rid}}/diff/{{other}}' \
            and run_id and other_run_id and method == 'GET':
        return diff_score_runs(event, user, run_id, other_run_id)

    # Any other path under the section: not a route this handler serves.
    if resource.startswith(ANOMALY):
        return not_implemented(event)
    return error_response(404, 'NOT_FOUND', 'Not found')


def handler(event, context):
    """Route an Anomaly_Tuning request (or a self-invoked action)."""
    event = event if isinstance(event, dict) else {}
    http_method = event.get('httpMethod')

    if http_method == 'OPTIONS':
        return {'statusCode': 200, 'headers': dict(CORS_HEADERS), 'body': ''}

    action = event.get('action')
    if action:
        # Self-invoked execution steps: one Bedrock_Scorer chunk or one
        # Device_Score_Job poll step. Never an API request, so no
        # authorization is evaluated here — the step only continues work a
        # route already authorized (Requirement 9.2).
        try:
            if action == ACTION_EXECUTE_SCORE_RUN:
                return execute_score_run(event)
            if action == ACTION_POLL_SCORE_JOB:
                return poll_score_job(event)
        except Exception as exc:  # noqa: BLE001 - a step never crash-loops
            logger.error(f"workflow_tuning action '{action}' failed: {exc}",
                         exc_info=True)
            return {'status': 'error', 'action': action, 'error': str(exc)}
        logger.warning(f"workflow_tuning action '{action}' is unknown")
        return {'status': 'unknown_action', 'action': action}

    try:
        user = get_user_from_event(event)
        return route(event, user)
    except TuningError as exc:
        return exc.response()
    except ClientError as exc:
        logger.error(f"AWS error: {exc}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
    except Exception as exc:
        logger.error(f"Handler error: {exc}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
