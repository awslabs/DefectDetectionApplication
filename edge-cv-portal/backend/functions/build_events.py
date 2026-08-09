"""
Build events Lambda function (Build_Manager, portal build fleet)

EventBridge consumer for every asynchronous signal about Build_Jobs and
Dedicated_Build_Servers (design §4 "Event consumer"). One Lambda behind
four EventBridge rules:

  - Custom ``dda.portal.builds`` / ``BuildPhaseChange`` phase events from
    the build agent (scripts/portal-build-agent.sh + portal-build.sh):
    conditional status transitions along the build_domain state machine,
    start/end time recording, the succeeded event's result metadata
    (component version, image refs) recorded verbatim on the Build_Job
    with a ``build_published`` Audit_Log entry (Req 5.1, 5.3, 5.5), and
    publishing-stage failures recorded with a PUBLISHING_FAILED error
    kind distinct from a build failure plus the per-artifact
    published/unpublished lists exactly as reported (``publish_partial``,
    Req 5.4).
  - EC2 Spot Instance Interruption Warning, and EC2 instance state-change
    to stopped/terminated, for an instance whose Build_Job is
    non-terminal: the job becomes ``interrupted`` (logs are already
    durable in CloudWatch; the retry action is served by build_jobs.py)
    (Req 3.5).
  - EC2 instance state-change for fleet instances: the BuildServers
    record's ``lifecycle_state`` and ``last_state_change_at`` are
    updated, ``terminated_at`` recorded on termination, and the
    ``pending_action`` marker cleared when the accepted fleet action's
    expected state is reached (Req 6.2, 6.3, 6.9, 6.11).
  - SSM command status change to Success/Failed/TimedOut/Cancelled for a
    job's agent command (build-fleet-execution-failures task 5.1): the
    consumer resolves the correlated attempt/command/instance identity,
    retrieves the final invocation READ-ONLY via ``GetCommandInvocation``,
    sanitizes it immediately through build_reconciliation (raw provider
    payloads never reach any log/persistence/API sink), and delegates
    classification to ``build_reconciliation.classify_attempt`` instead of
    the generic fallback. ``InvocationDoesNotExist`` is eventual
    consistency: a bounded retry/settlement state is persisted on the job
    and the scheduled dispatcher tick re-drives it. Late diagnostics are
    persisted/merged INDEPENDENTLY of the terminal transition
    (``apply_evidence`` terminal absorption), so callback-first,
    command-first, duplicate, and reordered deliveries converge without
    duplicate side effects. Evidence gate (historical-evidence.md task
    3.3): this path is authorized by hypothesis rows 2 (invocation
    evidence discarded — CONFIRMED), 3 (Build Log source incomplete —
    CONFIRMED), and 4 (terminal fallback premature/generic — CONFIRMED).
    Legacy jobs whose instance identity cannot be resolved keep the
    original generic fallback (``decide_ssm_fallback``) byte-compatibly.

Idempotence (Req 4.1): every job status transition is a DynamoDB
conditional update (ConditionExpression on the expected current status)
computed by build_domain.next_status with terminal absorption, and every
server state write is conditional on the state actually changing —
duplicate or stale EventBridge delivery is a no-op; a terminal Build_Job
is never resurrected.

The event-application decisions are PURE functions in this module
(``apply_phase_event``, ``decide_ssm_fallback``,
``apply_server_state_change``): event payload -> job/server field
updates, with no AWS clients, so design Property 10 (result and failure
recording on completion events) tests ``apply_phase_event`` directly.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates
Requirements: 3.5, 5.1, 5.3, 5.4, 5.5, 6.2, 6.3, 6.9, 6.11
"""
import hashlib
import json
import logging
import os
import time
from decimal import Decimal
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import log_audit_event

# Pure decision module (no AWS clients): every status transition is
# computed by the build_domain state machine (terminal absorption,
# Req 4.1).
import build_domain
# Pure shared reconciliation contract (build-fleet-execution-failures
# tasks 4.1-4.3): sanitization/bounding, deterministic classification,
# diagnostic merge, terminal absorption, settlement planning.
import build_reconciliation

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
# READ-ONLY invocation retrieval (GetCommandInvocation) for terminal
# command reconciliation (Req 2.1); this consumer sends no commands.
ssm = boto3.client('ssm')
# Best-effort dispatcher wake after an allocation release (task 6.3):
# the 1-minute schedule remains the authoritative promotion fallback,
# so a missing function name or a refused invoke costs latency only.
lambda_client = boto3.client('lambda')

# Environment variables (build-fleet-stack.ts lambdaEnvironment)
BUILD_JOBS_TABLE = os.environ.get('BUILD_JOBS_TABLE')
BUILD_SERVERS_TABLE = os.environ.get('BUILD_SERVERS_TABLE')
#: Optional dispatcher function name for the promotion wakeup (task 6.3).
#: Unset means the wakeup effect stays pending and the scheduled tick —
#: the retained fallback — completes the promotion within a minute.
BUILD_DISPATCHER_FUNCTION_NAME = os.environ.get(
    'BUILD_DISPATCHER_FUNCTION_NAME', '')

#: Audit_Log actor for event-driven transitions (no requesting user).
SYSTEM_USER = 'system'

# ---------------------------------------------------------------- constants

#: Custom phase events emitted by the build agent (design §5).
PHASE_EVENT_SOURCE = 'dda.portal.builds'
PHASE_EVENT_DETAIL_TYPE = 'BuildPhaseChange'

#: AWS event sources / detail types routed to this consumer.
EC2_EVENT_SOURCE = 'aws.ec2'
SSM_EVENT_SOURCE = 'aws.ssm'
SPOT_INTERRUPTION_DETAIL_TYPE = 'EC2 Spot Instance Interruption Warning'
INSTANCE_STATE_CHANGE_DETAIL_TYPE = 'EC2 Instance State-change Notification'
SSM_COMMAND_DETAIL_TYPES = (
    'EC2 Command Status-change Notification',
    'EC2 Command Invocation Status-change Notification',
)

#: Build_Job phases reported by the agent (portal-build-agent.sh /
#: portal-build.sh phase events).
PHASE_BUILDING = 'building'
PHASE_PUBLISHING = 'publishing'
PHASE_SUCCEEDED = 'succeeded'
PHASE_FAILED = 'failed'
KNOWN_PHASES = frozenset({
    PHASE_BUILDING, PHASE_PUBLISHING, PHASE_SUCCEEDED, PHASE_FAILED,
})

#: Correlated agent liveness/progress events (build-fleet-execution-
#: failures task 7.2, Req 2.6/2.16/2.18): emitted by the agent when it
#: was dispatched with an ATTEMPT_ID. They update ONLY the job's timing
#: evidence (execution-start anchor, heartbeat/progress leases) and the
#: preflight/disk evidence record — never a status.
PHASE_EXECUTION_START = 'execution_start'
PHASE_HEARTBEAT = 'heartbeat'
PHASE_PROGRESS = 'progress'
AGENT_ACTIVITY_PHASES = frozenset({
    PHASE_EXECUTION_START, PHASE_HEARTBEAT, PHASE_PROGRESS,
})

#: error_kind reported on a phase=failed event for a publish-stage
#: failure (portal-build-agent.sh, Req 5.4).
ERROR_KIND_PUBLISHING = 'publishing'
#: error_kind the agent reports when its LOCAL detector found ENOSPC
#: evidence in the failure output (task 7.4); classification honors it
#: without pattern matching (task 4.4 seam, Req 2.21).
ERROR_KIND_DISK = build_reconciliation.AGENT_ERROR_KIND_DISK
#: error_kind the agent reports when its own preflight failed before
#: any build/publish work (task 7.1, Req 2.8).
ERROR_KIND_PREFLIGHT = build_reconciliation.AGENT_ERROR_KIND_PREFLIGHT

#: Error codes recorded on failed Build_Jobs (design Error Handling):
#: a publishing failure is DISTINCT from a build failure (Req 5.4).
ERROR_BUILD_FAILED = 'BUILD_FAILED'
ERROR_PUBLISHING_FAILED = 'PUBLISHING_FAILED'
ERROR_AGENT_COMMAND_FAILED = 'AGENT_COMMAND_FAILED'
ERROR_AGENT_COMMAND_TIMED_OUT = 'AGENT_COMMAND_TIMED_OUT'
#: Stable disk-exhaustion code (task 4.4 / 7.5 wiring, evidence-gate
#: row 9 — CONFIRMED: JP6 job bd91c5d8 collapsed ENOSPC into a generic
#: BUILD_FAILED); a failed callback bearing ENOSPC evidence classifies
#: RUNNER_DISK_FULL while disk-free failures keep BUILD_FAILED (Req
#: 2.21, 3.15).
ERROR_RUNNER_DISK_FULL = build_reconciliation.CODE_RUNNER_DISK_FULL
#: Stable preflight-failure code (task 7.1, evidence-gate rows 1/8).
ERROR_COMMAND_PREFLIGHT_FAILED = \
    build_reconciliation.CODE_COMMAND_PREFLIGHT_FAILED

#: Audit_Log actions recorded by this consumer.
AUDIT_BUILD_PUBLISHED = 'build_published'            # Req 5.5
AUDIT_BUILD_PUBLISHING_FAILED = 'build_publishing_failed'  # Req 5.4
AUDIT_BUILD_FAILED = 'build_failed'
AUDIT_BUILD_INTERRUPTED = 'build_interrupted'

#: SSM command statuses handled by the fallback path.
SSM_STATUS_FAILED = 'Failed'
SSM_STATUS_TIMED_OUT = 'TimedOut'
SSM_STATUS_CANCELLED = 'Cancelled'
SSM_FAILURE_STATUSES = frozenset({
    SSM_STATUS_FAILED, SSM_STATUS_TIMED_OUT, SSM_STATUS_CANCELLED,
})
#: All terminal command statuses routed through reconciliation (Req 2.1):
#: Success is routed too, so a missing agent result after a successful
#: command can be settled to AGENT_RESULT_MISSING (Req 2.4).
SSM_STATUS_SUCCESS = 'Success'
SSM_TERMINAL_EVENT_STATUSES = frozenset({
    SSM_STATUS_SUCCESS, SSM_STATUS_FAILED, SSM_STATUS_TIMED_OUT,
    SSM_STATUS_CANCELLED,
})

#: Bounded window for GetCommandInvocation eventual consistency
#: (InvocationDoesNotExist is retried, never fabricated as failure,
#: Req 2.5); after the window the evidence is identified as unavailable.
INVOCATION_LOOKUP_WINDOW_MS = int(os.environ.get(
    'BUILD_INVOCATION_LOOKUP_WINDOW_MS', str(10 * 60 * 1000)))
#: Settlement window after a terminal command observation in which a
#: valid already-in-flight terminal agent result may still arrive.
SETTLEMENT_WINDOW_MS = int(os.environ.get(
    'BUILD_SETTLEMENT_WINDOW_MS',
    str(build_reconciliation.DEFAULT_SETTLEMENT_WINDOW_MS)))
#: Diagnostic source tag for this consumer (design data model).
EVIDENCE_SOURCE_EVENTBRIDGE = 'eventbridge'

#: EC2 instance states that mean the compute is gone: a non-terminal
#: Build_Job on that instance is interrupted (Req 3.5).
INSTANCE_LOST_STATES = frozenset({
    build_domain.SERVER_STATE_STOPPED,
    build_domain.SERVER_STATE_TERMINATED,
})

#: EC2 lifecycle states tracked on BuildServers records (Req 6.1).
EC2_LIFECYCLE_STATES = frozenset({
    build_domain.SERVER_STATE_PENDING,
    build_domain.SERVER_STATE_RUNNING,
    build_domain.SERVER_STATE_STOPPING,
    build_domain.SERVER_STATE_STOPPED,
    build_domain.SERVER_STATE_SHUTTING_DOWN,
    build_domain.SERVER_STATE_TERMINATED,
})

#: Statuses in which a Build_Job is actually occupying build compute:
#: instance loss interrupts exactly these (queued jobs on a dead server
#: are the dispatcher's dead-server sweep concern, Req 7.9).
ACTIVE_ON_COMPUTE_STATUSES = frozenset({
    build_domain.STATUS_PROVISIONING,
    build_domain.STATUS_BUILDING,
    build_domain.STATUS_PUBLISHING,
})

#: Statuses in which the agent SSM command failing means the job's
#: outcome was never reported (the fallback path).
AGENT_RUNNING_STATUSES = frozenset({
    build_domain.STATUS_BUILDING,
    build_domain.STATUS_PUBLISHING,
})


# =========================================================================
# PURE event-application functions (no AWS clients)
# =========================================================================

class PhaseApplication(NamedTuple):
    """Pure application of one agent phase event to a Build_Job.

    - ``steps``: the ordered chain of ``(expected_status, next_status)``
      conditional transitions to execute, each a defined edge of the
      build_domain state machine (empty for a no-op: duplicate delivery,
      stale event, or terminal job — Req 4.1 idempotence).
    - ``updates``: the Build_Job field updates applied together with the
      FINAL transition of the chain (start/end times, verbatim result
      metadata, error record, ``publish_partial`` lists).
    - ``audit_action`` / ``audit_details``: the Audit_Log entry recorded
      when the final transition actually applied (``build_published`` on
      success Req 5.5, the publishing failure Req 5.4).
    """
    steps: Tuple[Tuple[str, str], ...]
    updates: Dict[str, Any]
    audit_action: Optional[str]
    audit_details: Dict[str, Any]

    @property
    def is_noop(self) -> bool:
        return not self.steps

    @property
    def final_status(self) -> Optional[str]:
        return self.steps[-1][1] if self.steps else None


#: Shared no-op application (duplicate/stale/terminal, Req 4.1).
NO_APPLICATION = PhaseApplication((), {}, None, {})


def apply_phase_event(current_status: str, detail: Dict[str, Any],
                      now: int) -> PhaseApplication:
    """Pure event-application function: agent phase event payload ->
    Build_Job field updates (design §4, Property 10).

    ``current_status`` is the Build_Job's status at delivery time;
    ``detail`` is the EventBridge detail emitted by the build agent
    (``phase`` plus phase-specific fields); ``now`` is the ms-epoch
    receipt time used for start/end timestamps.

    Every transition in the returned chain is computed by
    build_domain.next_status, so terminal absorption holds by
    construction (Req 4.1): a terminal ``current_status`` or an unknown
    phase yields the no-op application, making duplicate EventBridge
    delivery a no-op. A lost intermediate event is healed by chaining
    the skipped defined edges (e.g. a succeeded event arriving while the
    job is still ``building`` chains building -> publishing ->
    succeeded).

    Field updates:
      - entering ``building`` records the start time (Req 4.3);
      - a terminal final status records the end time (Req 4.3);
      - a succeeded event records the agent-reported result metadata
        (component version, image references) VERBATIM on the Build_Job
        and produces the ``build_published`` Audit_Log entry
        (Req 5.3, 5.5);
      - a failed event with ``error_kind=publishing`` marks the job
        failed with the PUBLISHING_FAILED error kind — distinct from a
        build failure — and preserves the published/unpublished artifact
        lists exactly as reported (``publish_partial``), with the
        publishing failure audited (Req 5.4); any other failed event
        records a BUILD_FAILED error.

    Raises ValueError for a status outside the defined set (programming
    error), mirroring build_domain.next_status.
    """
    if current_status not in build_domain.ALL_STATUSES:
        raise ValueError(f"Unknown Build_Job status '{current_status}'")
    detail = detail or {}
    phase = detail.get('phase')
    if phase not in KNOWN_PHASES or build_domain.is_terminal(current_status):
        return NO_APPLICATION

    steps: List[Tuple[str, str]] = []
    status = current_status

    def advance(event: str) -> None:
        """Append the (status, event) edge when it is a defined move."""
        nonlocal status
        next_ = build_domain.next_status(status, event)
        if next_ != status:
            steps.append((status, next_))
            status = next_

    error_kind = detail.get('error_kind')

    if phase == PHASE_BUILDING:
        # provisioning -> building (ephemeral runner ready). A dedicated
        # job is already `building` (the dispatcher transitions before
        # the agent SendCommand): no-op. `queued` is the dispatcher's
        # transition to make: no-op here.
        if status == build_domain.STATUS_PROVISIONING:
            advance(build_domain.EVENT_RUNNER_READY)
    elif phase == PHASE_PUBLISHING:
        # building -> publishing (build step succeeded, Req 5.1); a lost
        # building event is healed from provisioning.
        if status == build_domain.STATUS_PROVISIONING:
            advance(build_domain.EVENT_RUNNER_READY)
        if status == build_domain.STATUS_BUILDING:
            advance(build_domain.EVENT_BUILD_SUCCEEDED)
    elif phase == PHASE_SUCCEEDED:
        # publishing -> succeeded (Req 5.3), healing lost intermediates.
        if status == build_domain.STATUS_PROVISIONING:
            advance(build_domain.EVENT_RUNNER_READY)
        if status == build_domain.STATUS_BUILDING:
            advance(build_domain.EVENT_BUILD_SUCCEEDED)
        if status == build_domain.STATUS_PUBLISHING:
            advance(build_domain.EVENT_PUBLISH_SUCCEEDED)
    elif phase == PHASE_FAILED:
        if error_kind == ERROR_KIND_PUBLISHING:
            # A publish-stage failure implies the build step succeeded:
            # heal lost intermediates, then publishing -> failed (5.4).
            if status == build_domain.STATUS_PROVISIONING:
                advance(build_domain.EVENT_RUNNER_READY)
            if status == build_domain.STATUS_BUILDING:
                advance(build_domain.EVENT_BUILD_SUCCEEDED)
            if status == build_domain.STATUS_PUBLISHING:
                advance(build_domain.EVENT_PUBLISH_FAILED)
        else:
            # Build-stage failure: building -> failed. A build failure
            # reported while the job is already `publishing` is out of
            # order (stale): no-op.
            if status == build_domain.STATUS_PROVISIONING:
                advance(build_domain.EVENT_RUNNER_READY)
            if status == build_domain.STATUS_BUILDING:
                advance(build_domain.EVENT_BUILD_FAILED)

    if not steps:
        return NO_APPLICATION

    updates: Dict[str, Any] = {}
    audit_action: Optional[str] = None
    audit_details: Dict[str, Any] = {}
    final_status = steps[-1][1]

    # Start time from the moment the job enters `building` (Req 4.3).
    if any(next_ == build_domain.STATUS_BUILDING for _, next_ in steps):
        updates['started_at'] = now
    # End time from the moment the job reaches a terminal status (Req 4.3).
    if build_domain.is_terminal(final_status):
        updates['ended_at'] = now
    # Resolved commit SHA (build-source-selection Req 4.5): a present
    # source_commit on the event detail is persisted with the applied
    # transition; its absence (legacy agents) changes nothing.
    if detail.get('source_commit'):
        updates['source_commit'] = detail['source_commit']

    if final_status == build_domain.STATUS_SUCCEEDED:
        # Result metadata recorded VERBATIM as the agent reported it:
        # published component version identifier + pushed image
        # references (Req 5.3).
        result = detail.get('result')
        updates['result'] = result if isinstance(result, dict) else {}
        audit_action = AUDIT_BUILD_PUBLISHED  # Req 5.5
        audit_details = {
            'component_name': updates['result'].get('component_name'),
            'component_version': (
                updates['result'].get('published_version')
                or updates['result'].get('component_version')),
            'image_refs': (
                updates['result'].get('pushed_image_refs')
                or updates['result'].get('image_refs') or []),
        }
    elif final_status == build_domain.STATUS_FAILED:
        message = detail.get('error_message') or 'The build failed.'
        if error_kind == ERROR_KIND_PUBLISHING:
            # Distinct error kind + the per-artifact lists EXACTLY as
            # reported (Req 5.4).
            updates['error'] = {
                'code': ERROR_PUBLISHING_FAILED,
                'message': message,
            }
            updates['publish_partial'] = {
                'published': list(detail.get('published_artifacts') or []),
                'unpublished': list(detail.get('unpublished_artifacts') or []),
            }
            audit_action = AUDIT_BUILD_PUBLISHING_FAILED  # Req 5.4
            audit_details = {
                'error_kind': ERROR_KIND_PUBLISHING,
                'error_code': ERROR_PUBLISHING_FAILED,
                'message': message,
                'published': updates['publish_partial']['published'],
                'unpublished': updates['publish_partial']['unpublished'],
            }
        else:
            # Build-stage failure classification (tasks 7.1/7.5 wiring;
            # evidence-gate rows 1, 8, 9 — all CONFIRMED):
            #   - the agent's own failed preflight is the stable
            #     COMMAND_PREFLIGHT_FAILED (no costly work ran);
            #   - ENOSPC evidence in the agent's message, or the agent's
            #     local error_kind=disk shortcut, is the stable
            #     RUNNER_DISK_FULL (Req 2.21) instead of collapsing into
            #     a generic build failure;
            #   - disk-free failures keep BUILD_FAILED byte-compatibly
            #     (preservation Req 3.15).
            if build_reconciliation.is_preflight_failure_evidence(
                    message, agent_error_kind=error_kind):
                error_code = ERROR_COMMAND_PREFLIGHT_FAILED
            elif build_reconciliation.is_disk_exhaustion_evidence(
                    message, agent_error_kind=error_kind):
                error_code = ERROR_RUNNER_DISK_FULL
            else:
                error_code = ERROR_BUILD_FAILED
            updates['error'] = {
                'code': error_code,
                'message': message,
            }
            audit_action = AUDIT_BUILD_FAILED
            audit_details = {
                'error_kind': error_kind or 'building',
                'error_code': error_code,
                'message': message,
            }

    return PhaseApplication(tuple(steps), updates, audit_action,
                            audit_details)


class SsmFallback(NamedTuple):
    """Pure fallback decision for a Failed/TimedOut/Cancelled agent SSM
    command (design §4 last row).

    ``apply`` is True iff the Build_Job must be moved; then
    ``next_status`` is ``failed`` (command Failed/TimedOut) or
    ``interrupted`` (command Cancelled — the agent was torn down under
    the job, e.g. by instance loss), and ``error`` carries the error
    record for the failed case.
    """
    apply: bool
    next_status: Optional[str]
    error: Optional[Dict[str, str]]


def decide_ssm_fallback(current_status: str,
                        command_status: str) -> SsmFallback:
    """Fallback for a job's agent command reaching Failed/TimedOut/
    Cancelled while the job is still building/publishing and no agent
    terminal phase event arrived (pure).

    Any other job status is a no-op: a terminal job already has its
    outcome (the agent's own event won the race — idempotence, Req 4.1),
    and a queued/provisioning job has no agent outcome to lose.
    """
    if current_status not in build_domain.ALL_STATUSES:
        raise ValueError(f"Unknown Build_Job status '{current_status}'")
    if current_status not in AGENT_RUNNING_STATUSES:
        return SsmFallback(False, None, None)
    if command_status == SSM_STATUS_CANCELLED:
        return SsmFallback(
            True, build_domain.apply_interruption(current_status), None)
    if command_status in (SSM_STATUS_FAILED, SSM_STATUS_TIMED_OUT):
        code = ERROR_AGENT_COMMAND_TIMED_OUT \
            if command_status == SSM_STATUS_TIMED_OUT \
            else ERROR_AGENT_COMMAND_FAILED
        event = build_domain.EVENT_BUILD_FAILED \
            if current_status == build_domain.STATUS_BUILDING \
            else build_domain.EVENT_PUBLISH_FAILED
        return SsmFallback(
            True,
            build_domain.next_status(current_status, event),
            {'code': code,
             'message': (
                 f"The build agent SSM command ended with status "
                 f"'{command_status}' before reporting a build result.")},
        )
    return SsmFallback(False, None, None)


class ServerStateApplication(NamedTuple):
    """Pure application of an EC2 instance state-change to a BuildServers
    record (Req 6.2, 6.3, 6.9, 6.11).

    ``changed`` is False for a duplicate/stale delivery (state already
    recorded) or an unknown state — a no-op. ``updates`` carries the
    fields to SET; ``clear_pending`` is True when the accepted fleet
    action's expected lifecycle state was reached, so the
    ``pending_action`` marker is removed (Req 6.11).
    """
    changed: bool
    updates: Dict[str, Any]
    clear_pending: bool


def apply_server_state_change(server: Dict[str, Any], new_state: str,
                              now: int) -> ServerStateApplication:
    """EC2 instance state-change -> BuildServers field updates (pure).

    Records the observed lifecycle state and the time of the state
    change (Req 6.1, 6.9), ``terminated_at`` on the first transition to
    terminated, and clears the ``pending_action`` marker when its
    ``expected_state`` is reached (Req 6.2, 6.3, 6.11). A state equal to
    the stored one (duplicate delivery) or outside the EC2 lifecycle
    state set is a no-op.
    """
    server = server or {}
    if new_state not in EC2_LIFECYCLE_STATES \
            or new_state == server.get('lifecycle_state'):
        return ServerStateApplication(False, {}, False)

    updates: Dict[str, Any] = {
        'lifecycle_state': new_state,
        'last_state_change_at': now,
    }
    if new_state == build_domain.SERVER_STATE_TERMINATED \
            and not server.get('terminated_at'):
        updates['terminated_at'] = now

    pending = server.get('pending_action') or {}
    clear_pending = bool(pending) and \
        pending.get('expected_state') == new_state

    return ServerStateApplication(True, updates, clear_pending)


# =========================================================================
# Persistence (DynamoDB) — same conditional-update conventions as
# build_dispatcher.py
# =========================================================================

def now_ms() -> int:
    """Current epoch milliseconds (BuildJobs timestamps are ms epoch)"""
    return int(time.time() * 1000)


def to_native(value: Any) -> Any:
    """Convert DynamoDB Decimals to native ints/floats (deep)"""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_native(v) for v in value]
    return value


def to_dynamo(value: Any) -> Any:
    """Convert native floats to Decimals for DynamoDB persistence (deep);
    sanitized diagnostics may carry numeric fields."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_dynamo(v) for v in value]
    return value


def jobs_table():
    """BuildJobs DynamoDB table accessor"""
    return dynamodb.Table(BUILD_JOBS_TABLE)


def servers_table():
    """BuildServers DynamoDB table accessor"""
    return dynamodb.Table(BUILD_SERVERS_TABLE)


def scan_all(table) -> List[Dict]:
    """Full paginated scan of a table (native types)"""
    items: List[Dict] = []
    kwargs: Dict[str, Any] = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(to_native(item) for item in response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            return items
        kwargs['ExclusiveStartKey'] = last_key


def get_job(build_job_id: str) -> Optional[Dict]:
    """Fetch one Build_Job record (native types) or None"""
    response = jobs_table().get_item(Key={'build_job_id': build_job_id})
    item = response.get('Item')
    return to_native(item) if item else None


def transition_job(build_job_id: str, expected_status: str,
                   new_status: str,
                   extra: Optional[Dict[str, Any]] = None) -> bool:
    """Conditionally transition a Build_Job's status (ConditionExpression
    on the expected current status, Req 4.1: terminal jobs are never
    resurrected, duplicate EventBridge delivery is a no-op). ``extra``
    carries additional top-level attributes to SET. Returns False when
    the job moved in the meantime."""
    names = {'#status': 'status'}
    values: Dict[str, Any] = {':new': new_status, ':expected': expected_status}
    sets = ['#status = :new']
    for index, (key, value) in enumerate(sorted((extra or {}).items())):
        names[f'#a{index}'] = key
        values[f':a{index}'] = value
        sets.append(f'#a{index} = :a{index}')
    try:
        jobs_table().update_item(
            Key={'build_job_id': build_job_id},
            UpdateExpression='SET ' + ', '.join(sets),
            ConditionExpression='#status = :expected',
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == \
                'ConditionalCheckFailedException':
            return False
        raise


def set_job_fields(build_job_id: str, fields: Dict[str, Any]) -> None:
    """Unconditional SET of non-status Build_Job bookkeeping attributes
    (sanitized diagnostics, reconciliation state). Never touches
    ``status``/``ended_at``/``error`` — terminal absorption is enforced
    by the conditional transition path."""
    names = {}
    values = {}
    sets = []
    for index, (key, value) in enumerate(sorted(fields.items())):
        names[f'#f{index}'] = key
        values[f':f{index}'] = value
        sets.append(f'#f{index} = :f{index}')
    jobs_table().update_item(
        Key={'build_job_id': build_job_id},
        UpdateExpression='SET ' + ', '.join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def release_server(server_id: str, build_job_id: str) -> bool:
    """Release a server allocation held by one Build_Job (conditional on
    the job actually holding it, so a stale release can never free a
    slot another job took) — lets the dispatcher promote the oldest
    queued job on its next tick (Req 7.3)."""
    try:
        servers_table().update_item(
            Key={'server_id': server_id},
            UpdateExpression='REMOVE running_build_job_id',
            ConditionExpression='running_build_job_id = :job',
            ExpressionAttributeValues={':job': build_job_id},
        )
        return True
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == \
                'ConditionalCheckFailedException':
            return False
        raise


def write_server_state(server: Dict[str, Any],
                       application: ServerStateApplication) -> bool:
    """Persist a ServerStateApplication onto the BuildServers record.
    Conditional on the lifecycle state actually changing, so duplicate
    EventBridge delivery is a no-op. Returns False on the no-op."""
    updates = application.updates
    names: Dict[str, str] = {}
    values: Dict[str, Any] = {}
    sets: List[str] = []
    for index, (key, value) in enumerate(sorted(updates.items())):
        names[f'#s{index}'] = key
        values[f':s{index}'] = value
        sets.append(f'#s{index} = :s{index}')
    expression = 'SET ' + ', '.join(sets)
    if application.clear_pending:
        # The accepted fleet action reached its expected state: the
        # marker is cleared (Req 6.11).
        expression += ' REMOVE pending_action'
    values[':current'] = updates['lifecycle_state']
    try:
        servers_table().update_item(
            Key={'server_id': server['server_id']},
            UpdateExpression=expression,
            ConditionExpression=(
                'attribute_not_exists(lifecycle_state) '
                'OR lifecycle_state <> :current'),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == \
                'ConditionalCheckFailedException':
            return False  # duplicate delivery: state already recorded
        raise


def audit(action: str, resource_id: str, result: str,
          details: Optional[Dict[str, Any]] = None) -> None:
    """Best-effort Audit_Log entry for an event-driven transition; an
    audit failure never fails the event handling."""
    try:
        log_audit_event(
            user_id=SYSTEM_USER,
            action=action,
            resource_type='build_job' if action.startswith('build') else
                          'build_server',
            resource_id=resource_id,
            result=result,
            details=details or {},
        )
    except Exception as e:
        logger.warning(f"Audit_Log write failed for {action}/{resource_id}: {e}")


# =========================================================================
# Terminal-effects ledger adapters (build-fleet-execution-failures
# task 6.1, Req 2.6/2.7/3.11): ONE conditional terminal write carries the
# status, error-or-result, ended_at, evidence digest, and the stable
# effect ID; every remaining side effect (audit, verified cleanup,
# allocation release, promotion wakeup) is a retryable ledger effect
# completed by a conditional pending -> done write, so retries and races
# converge on exactly one logical effect each.
# =========================================================================

#: Stable attempt component of the effect identity for jobs that never
#: recorded an execution attempt (queued cancellations, legacy jobs).
NO_ATTEMPT_ID = 'no-attempt'

#: Ledger effects whose completion is gated on a predecessor effect
#: (advance_effect ordering, enforced here as a DynamoDB condition):
#: allocation release requires VERIFIED compute cleanup first
#: (stop-before-release, Req 3.11); promotion requires the release.
_EFFECT_ORDER_GUARDS = {
    build_reconciliation.EFFECT_ALLOCATION_RELEASE:
        build_reconciliation.EFFECT_COMPUTE_CLEANUP,
    build_reconciliation.EFFECT_PROMOTION_WAKEUP:
        build_reconciliation.EFFECT_ALLOCATION_RELEASE,
}


def evidence_digest(evidence: Any) -> Optional[str]:
    """Stable content digest of one terminal outcome's sanitized
    evidence (the 'evidence digest' of the design's terminal
    finalization write). Digests only already-sanitized structures."""
    if not evidence:
        return None
    return hashlib.sha256(
        json.dumps(to_native(evidence), sort_keys=True,
                   default=str).encode('utf-8')).hexdigest()


def plan_job_ledger(job: Dict[str, Any],
                    cleanup_required: bool) -> Dict[str, Any]:
    """The terminal-effects ledger for one job's terminal outcome
    (build_reconciliation.plan_terminal_effects) keyed by the job's
    current attempt identity; jobs without an attempt use a stable
    placeholder so the effect ID stays deterministic."""
    attempt_id = ((job.get('execution_attempt') or {}).get('attempt_id')
                  or NO_ATTEMPT_ID)
    return build_reconciliation.plan_terminal_effects(
        job['build_job_id'], attempt_id,
        job.get('execution_mode') or '', cleanup_required)


def complete_effect(build_job_id: str, effect_id: Optional[str],
                    effect: str) -> bool:
    """Conditionally advance ONE ledger effect pending -> done.

    The DynamoDB condition is the concurrency arbiter (the persistence
    adapter of build_reconciliation.advance_effect): the effect must
    still be pending under the SAME stable effect_id, and ordered
    effects require their predecessor to be done or not_applicable.
    Returns False on a duplicate/out-of-order completion — the retry
    must not repeat the side effect (Req 2.7)."""
    if not effect_id:
        return False
    names = {'#fx': 'terminal_effects', '#e': effect}
    values: Dict[str, Any] = {
        ':eid': effect_id,
        ':pending': build_reconciliation.EFFECT_PENDING,
        ':done': build_reconciliation.EFFECT_DONE,
    }
    condition = '#fx.effect_id = :eid AND #fx.#e = :pending'
    guard = _EFFECT_ORDER_GUARDS.get(effect)
    if guard:
        names['#g'] = guard
        values[':na'] = build_reconciliation.EFFECT_NOT_APPLICABLE
        condition += ' AND (#fx.#g = :done OR #fx.#g = :na)'
    try:
        jobs_table().update_item(
            Key={'build_job_id': build_job_id},
            UpdateExpression='SET #fx.#e = :done',
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == \
                'ConditionalCheckFailedException':
            return False
        raise


def audit_terminal_effect(build_job_id: str, ledger: Dict[str, Any],
                          action: str, result: str,
                          details: Optional[Dict[str, Any]] = None) -> None:
    """ONE logical Audit_Log entry per terminal outcome, deduplicated by
    the stable effect identity (Req 2.7): only the writer that completes
    the pending audit effect writes the entry, so a retry may complete a
    pending audit but can never create a second logical audit."""
    effect_id = ledger.get('effect_id')
    if complete_effect(build_job_id, effect_id,
                       build_reconciliation.EFFECT_AUDIT):
        audit(action, build_job_id, result,
              {**(details or {}), 'terminal_effect_id': effect_id})


def wake_dispatcher(build_job_id: str) -> bool:
    """Best-effort async dispatcher wake after an allocation release
    (task 6.3): the 1-minute schedule remains the promotion fallback, so
    any failure here costs latency, never correctness."""
    if not BUILD_DISPATCHER_FUNCTION_NAME:
        return False
    try:
        lambda_client.invoke(
            FunctionName=BUILD_DISPATCHER_FUNCTION_NAME,
            InvocationType='Event',
            Payload=json.dumps({'action': 'promote',
                                'build_job_ids': [build_job_id]}),
        )
        return True
    except Exception as e:
        logger.warning(f"Dispatcher wake after release of Build_Job "
                       f"{build_job_id} failed (schedule fallback "
                       f"promotes): {e}")
        return False


def release_and_promote(job: Dict[str, Any],
                        ledger: Dict[str, Any]) -> None:
    """Drive the allocation_release and promotion_wakeup effects for one
    finalized terminal outcome (Req 2.7/3.11).

    The release completes ONLY when the ledger's compute cleanup is done
    or not applicable (the conditional guard), so a follower can never
    be promoted onto a server whose process state is unknown; the actual
    slot release stays conditional on this job still owning it
    (stale-release protection preserved). The wakeup is best-effort —
    the scheduled tick remains the fallback and completes the pending
    effect itself."""
    build_job_id = job['build_job_id']
    effect_id = ledger.get('effect_id')
    if job.get('execution_mode') == build_domain.EXECUTION_MODE_DEDICATED \
            and job.get('server_id'):
        if complete_effect(build_job_id, effect_id,
                           build_reconciliation.EFFECT_ALLOCATION_RELEASE):
            release_server(job['server_id'], build_job_id)
    if wake_dispatcher(build_job_id):
        complete_effect(build_job_id, effect_id,
                        build_reconciliation.EFFECT_PROMOTION_WAKEUP)


# =========================================================================
# Job lookup helpers
# =========================================================================

def find_jobs_on_instance(instance_id: str) -> List[Dict]:
    """Build_Jobs actively occupying the given EC2 instance: ephemeral
    jobs whose runner is the instance, and dedicated jobs assigned to
    the fleet server registered with that instance."""
    jobs = [j for j in scan_all(jobs_table())
            if j.get('status') in ACTIVE_ON_COMPUTE_STATUSES]
    matched = [j for j in jobs
               if (j.get('runner') or {}).get('instance_id') == instance_id]
    dedicated = [j for j in jobs
                 if j.get('execution_mode') ==
                 build_domain.EXECUTION_MODE_DEDICATED and j.get('server_id')]
    if dedicated:
        server_ids = {
            s['server_id'] for s in scan_all(servers_table())
            if s.get('instance_id') == instance_id and s.get('server_id')}
        matched.extend(j for j in dedicated
                       if j['server_id'] in server_ids
                       and j not in matched)
    return matched


def find_job_by_command(command_id: str) -> Optional[Dict]:
    """The Build_Job whose agent SSM command has the given command id."""
    for job in scan_all(jobs_table()):
        if (job.get('ssm') or {}).get('command_id') == command_id:
            return job
    return None


def find_server_by_instance(instance_id: str) -> Optional[Dict]:
    """The fleet BuildServers record registered with an EC2 instance."""
    for server in scan_all(servers_table()):
        if server.get('instance_id') == instance_id:
            return server
    return None


# =========================================================================
# Event handlers
# =========================================================================

def apply_agent_activity(job: Dict[str, Any],
                         detail: Dict[str, Any]) -> None:
    """Apply one correlated agent activity event (task 7.2, Req 2.6,
    2.16, 2.18) to the Build_Job's timing evidence through the pure
    build_reconciliation observers:

      - ``execution_start`` anchors active runtime exactly once (first
        writer wins) and carries the agent's machine-side preflight
        summary and disk-capacity recording (task 7.5, Req 2.23),
        persisted sanitized on the job's ``preflight`` record;
      - ``heartbeat`` renews liveness only; ``progress`` renews both
        liveness and the progress lease. Monotonic sequences make
        duplicates and reordered deliveries no-ops.

    Stale-attempt evidence is rejected by attempt correlation; terminal
    jobs are absorbing (nothing here touches status/result/ended_at).
    Heartbeats carry no raw build output or environment; only the known
    fields below are ever read or persisted.
    """
    if build_domain.is_terminal(job.get('status', '')):
        return  # absorbing terminal state (Req 2.6)
    build_job_id = job['build_job_id']
    phase = detail.get('phase')
    observed_at = detail.get('observed_at')
    observed_at = int(observed_at) \
        if isinstance(observed_at, (int, float)) and observed_at > 0 \
        else now_ms()
    evidence = {'attempt_id': detail.get('attempt_id')}
    fields: Dict[str, Any] = {}
    update = None

    if phase == PHASE_EXECUTION_START:
        update = build_reconciliation.observe_execution_start(
            job, evidence, observed_at)
        # Machine-side preflight evidence + disk-capacity recording
        # travel on the execution-start event (tasks 7.1/7.5): recorded
        # sanitized, with an unusable measurement identified as
        # unavailable rather than fabricated.
        existing = job.get('preflight') or {}
        preflight_updates: Dict[str, Any] = {}
        if 'disk' in detail:
            preflight_updates['disk'] = \
                build_reconciliation.sanitize_disk_evidence(
                    detail.get('disk'))
        if isinstance(detail.get('preflight'), dict):
            preflight_updates['agent_checks'] = \
                build_reconciliation.sanitize_evidence_tree(
                    detail['preflight'])
        if preflight_updates:
            fields['preflight'] = to_dynamo(
                {**existing, **preflight_updates})
    elif phase in (PHASE_HEARTBEAT, PHASE_PROGRESS):
        sequence = detail.get('sequence')
        if not isinstance(sequence, (int, float)):
            return  # unusable evidence: never fabricate a sequence
        sequence = int(sequence)
        if phase == PHASE_HEARTBEAT:
            update = build_reconciliation.observe_heartbeat(
                job, evidence, sequence, observed_at)
        else:
            kind = detail.get('progress_kind')
            if kind not in (build_reconciliation.PROGRESS_KIND_PHASE,
                            build_reconciliation.PROGRESS_KIND_CHECKPOINT,
                            build_reconciliation
                            .PROGRESS_KIND_OUTPUT_GROWTH):
                kind = build_reconciliation.PROGRESS_KIND_OUTPUT_GROWTH
            update = build_reconciliation.observe_progress(
                job, evidence, sequence, observed_at, kind)

    if update is not None and update.accepted:
        fields['timing'] = to_dynamo(update.timing)
    if fields:
        set_job_fields(build_job_id, fields)
        logger.info(f"Build_Job {build_job_id}: agent activity "
                    f"'{phase}' recorded")


def handle_phase_event(detail: Dict[str, Any]) -> None:
    """Custom dda.portal.builds phase event from the build agent:
    conditional transitions along the state machine, start/end times,
    verbatim result metadata, publish_partial lists, and the
    build_published audit entry on success (Req 5.1, 5.3, 5.4, 5.5)."""
    build_job_id = detail.get('build_job_id')
    if not build_job_id:
        logger.warning(f"Phase event without build_job_id ignored: {detail}")
        return
    job = get_job(build_job_id)
    if job is None:
        logger.warning(f"Phase event for unknown Build_Job "
                       f"{build_job_id} ignored")
        return

    # Correlated liveness/progress evidence (task 7.2): timing and
    # preflight/disk bookkeeping only; the phase state machine below is
    # untouched by these events.
    if detail.get('phase') in AGENT_ACTIVITY_PHASES:
        apply_agent_activity(job, detail)
        return

    application = apply_phase_event(job['status'], detail, now_ms())
    if application.is_noop:
        logger.info(f"Phase event '{detail.get('phase')}' for Build_Job "
                    f"{build_job_id} (status '{job['status']}') is a "
                    f"no-op (duplicate/stale delivery)")
        return

    # Surface disk evidence in failure diagnostics (task 7.5, Req 2.23,
    # evidence-gate row 9): a disk-classified failure always carries a
    # disk block — the measured preflight/agent evidence when available,
    # else the truthful {"available": False} marker — and any other
    # failure carries it when a measurement exists. Disk-free failures
    # without a measurement keep their records unchanged (Req 3.14).
    failure_code = (application.updates.get('error') or {}).get('code')
    measured_disk = detail.get('disk') \
        if isinstance(detail.get('disk'), dict) \
        else (job.get('preflight') or {}).get('disk')
    if failure_code in (ERROR_RUNNER_DISK_FULL,
                        ERROR_COMMAND_PREFLIGHT_FAILED) \
            or (failure_code and isinstance(measured_disk, dict)):
        diagnostic = dict(job.get('execution_diagnostic') or {})
        diagnostic['disk'] = build_reconciliation.sanitize_disk_evidence(
            measured_disk)
        application.updates['execution_diagnostic'] = to_dynamo(diagnostic)

    # Execute the conditional chain; any lost race means another writer
    # (duplicate delivery, cancellation, watchdog) already moved the job:
    # stop without side effects (Req 4.1 idempotence).
    for expected, next_ in application.steps[:-1]:
        if not transition_job(build_job_id, expected, next_):
            logger.info(f"Build_Job {build_job_id} moved during phase "
                        f"application ({expected} -> {next_} raced); "
                        f"treating delivery as stale")
            return
    expected, final = application.steps[-1]
    updates: Dict[str, Any] = dict(application.updates)
    ledger: Optional[Dict[str, Any]] = None
    if build_domain.is_terminal(final):
        # Terminal finalization (task 6.1): ONE conditional write carries
        # the status, result-or-error, ended_at, evidence digest, and the
        # stable effect ID. An agent-reported terminal outcome means the
        # agent process exited on its own, so a dedicated server needs no
        # stop verification (cleanup not applicable); an ephemeral runner
        # still needs its idempotent termination, completed by the
        # termination watchdog (Req 2.7, preservation Req 3.9).
        ledger = plan_job_ledger(
            job,
            cleanup_required=(job.get('execution_mode') ==
                              build_domain.EXECUTION_MODE_EPHEMERAL))
        if not application.audit_action:
            # No audit belongs to this outcome: never leave a pending
            # audit for a reconciler to invent one (preservation 3.1).
            ledger[build_reconciliation.EFFECT_AUDIT] = \
                build_reconciliation.EFFECT_DONE
        updates['terminal_effects'] = ledger
        updates['evidence_digest'] = evidence_digest(application.updates)
    if not transition_job(build_job_id, expected, final, extra=updates):
        logger.info(f"Build_Job {build_job_id} moved during phase "
                    f"application ({expected} -> {final} raced); "
                    f"treating delivery as stale")
        return
    logger.info(f"Build_Job {build_job_id}: phase '{detail.get('phase')}' "
                f"applied, status '{job['status']}' -> '{final}'")

    if ledger is not None:
        if application.audit_action:
            audit_terminal_effect(
                build_job_id, ledger, application.audit_action,
                'success' if final == build_domain.STATUS_SUCCEEDED
                else 'failure',
                application.audit_details)
        release_and_promote(job, ledger)


def interrupt_jobs_on_instance(instance_id: str, cause: str,
                               compute_gone: bool = False) -> None:
    """Mark every non-terminal Build_Job occupying an instance as
    interrupted (Req 3.5): conditional transition per job (terminal jobs
    unchanged — Req 4.1), end time recorded, logs already durable in
    CloudWatch, retry served by build_jobs.py.

    ``compute_gone`` True means the interruption evidence IS an observed
    terminal instance state (stopped/terminated): the design lets that
    observation complete the cleanup effect directly, so the allocation
    release is not blocked on a process check that can never run. A spot
    interruption WARNING leaves cleanup pending — the instance is still
    up, so the termination watchdog / effects reconciliation verifies it
    (Req 2.7/3.11)."""
    for job in find_jobs_on_instance(instance_id):
        current = job['status']
        interrupted = build_domain.apply_interruption(current)
        if interrupted == current:
            continue  # terminal: unchanged
        evidence = {'instance_id': instance_id, 'cause': cause,
                    'status_at_interruption': current}
        ledger = plan_job_ledger(job, cleanup_required=True)
        if transition_job(job['build_job_id'], current, interrupted,
                          extra={'ended_at': now_ms(),
                                 'terminal_effects': ledger,
                                 'evidence_digest':
                                     evidence_digest(evidence)}):
            if compute_gone:
                complete_effect(job['build_job_id'],
                                ledger.get('effect_id'),
                                build_reconciliation.EFFECT_COMPUTE_CLEANUP)
            audit_terminal_effect(job['build_job_id'], ledger,
                                  AUDIT_BUILD_INTERRUPTED, 'failure',
                                  evidence)
            release_and_promote(job, ledger)
            logger.info(f"Build_Job {job['build_job_id']} interrupted "
                        f"({cause}, instance {instance_id})")


def handle_spot_interruption(detail: Dict[str, Any]) -> None:
    """EC2 Spot Instance Interruption Warning: the compute provider is
    reclaiming the runner — every non-terminal Build_Job on it becomes
    interrupted (Req 3.5). The instance is still running at warning
    time, so cleanup stays pending until verified (Req 3.11)."""
    instance_id = detail.get('instance-id')
    if not instance_id:
        return
    interrupt_jobs_on_instance(instance_id, 'spot_interruption')


def handle_instance_state_change(detail: Dict[str, Any]) -> None:
    """EC2 instance state-change: update the fleet BuildServers record
    (lifecycle state, last_state_change_at, pending_action clearing —
    Req 6.2, 6.3, 6.9, 6.11), and interrupt the non-terminal Build_Jobs
    of an instance that went away (Req 3.5)."""
    instance_id = detail.get('instance-id')
    state = detail.get('state')
    if not instance_id or not state:
        return

    server = find_server_by_instance(instance_id)
    if server is not None:
        application = apply_server_state_change(server, state, now_ms())
        if application.changed:
            if write_server_state(server, application):
                logger.info(
                    f"Build server {server['server_id']} lifecycle state "
                    f"'{server.get('lifecycle_state')}' -> '{state}'"
                    + (' (pending action completed)'
                       if application.clear_pending else ''))

    if state in INSTANCE_LOST_STATES:
        # Observed terminal instance state: the design lets this
        # observation set cleanup done (the compute is gone, so no
        # protected build process can remain).
        interrupt_jobs_on_instance(instance_id, f'instance_{state}',
                                   compute_gone=True)


def resolve_command_identity(job: Dict[str, Any],
                             event_instance_id: Optional[str] = None
                             ) -> Tuple[Dict[str, Any], Optional[str]]:
    """The correlated (attempt, instance_id) identity for the job's
    agent command (Req 2.6): the execution attempt record first, then
    the ssm marker, the ephemeral runner, the dedicated server record,
    and finally the event's own instance id."""
    attempt = job.get('execution_attempt') or {}
    ssm_info = job.get('ssm') or {}
    instance_id = (attempt.get('instance_id')
                   or ssm_info.get('instance_id')
                   or (job.get('runner') or {}).get('instance_id'))
    if not instance_id and job.get('server_id'):
        try:
            response = servers_table().get_item(
                Key={'server_id': job['server_id']})
            instance_id = (response.get('Item') or {}).get('instance_id')
        except ClientError as e:
            logger.warning(f"Server lookup for Build_Job "
                           f"{job.get('build_job_id')} failed: "
                           f"{e.response.get('Error', {}).get('Code')}")
    return attempt, instance_id or event_instance_id


def retrieve_invocation(command_id: Optional[str],
                        instance_id: Optional[str]
                        ) -> Optional[Dict[str, Any]]:
    """READ-ONLY GetCommandInvocation for the persisted command identity
    (Req 2.1). Returns None on ``InvocationDoesNotExist`` (eventual
    consistency, Req 2.5) or when identity is incomplete. The raw
    response exists only in local memory long enough to be sanitized by
    the caller — it is NEVER logged or persisted as-is (Req 2.10)."""
    if not command_id or not instance_id:
        return None
    try:
        return ssm.get_command_invocation(CommandId=command_id,
                                          InstanceId=instance_id)
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code not in ('InvocationDoesNotExist', 'InvalidCommandId'):
            # Service unavailability is represented (retried by the
            # scheduled tick), never fabricated as command failure.
            logger.warning(f"GetCommandInvocation({command_id}) "
                           f"failed: {code}")
        return None


def classification_message(classification:
                           'build_reconciliation.Classification',
                           command_status: Optional[str]) -> str:
    """Safe failed-job message built ONLY from stable vocabulary — no
    raw provider text ever enters the error record (Req 2.10); the
    bounded redacted evidence lives in ``execution_diagnostic``."""
    return (f"The build agent SSM command ended with status "
            f"'{command_status}' before reporting a build result: "
            f"{classification.reason}. Retained command evidence was "
            f"recorded in the execution diagnostic.")


def reconcile_command_evidence(job: Dict[str, Any], command_id: str,
                               event_status: Optional[str],
                               instance_id: Optional[str],
                               source: str,
                               now: Optional[int] = None) -> None:
    """Reconcile one terminal command observation for one Build_Job
    (build-fleet-execution-failures task 5.1, design "Reconciliation
    Flow"): retrieve the final invocation, sanitize immediately,
    classify deterministically via ``classify_attempt``, and persist/
    merge the bounded redacted diagnostic INDEPENDENTLY of the terminal
    transition, so delivery order/duplication cannot change the outcome
    or duplicate side effects (Req 2.1, 2.2, 2.4, 2.6, 2.10)."""
    now = now if now is not None else now_ms()
    build_job_id = job['build_job_id']
    job_status = job['status']
    attempt = job.get('execution_attempt') or {}

    # Stale/mismatched evidence rejection (Req 2.6): evidence carrying a
    # different attempt/command/instance identity can never affect this
    # job's current attempt.
    evidence_identity: Dict[str, Any] = {'command_id': command_id}
    if instance_id:
        evidence_identity['instance_id'] = instance_id
    if not build_reconciliation.evidence_matches_attempt(
            attempt, evidence_identity):
        logger.info(f"Build_Job {build_job_id}: stale/mismatched command "
                    f"evidence for {command_id} rejected")
        return

    recon = job.get('reconciliation') or {}
    first_observed_at = recon.get('first_observed_at') or now
    settlement_deadline_ms = recon.get('settlement_deadline')

    raw_invocation = retrieve_invocation(command_id, instance_id)
    lookup = build_reconciliation.decide_invocation_lookup(
        raw_invocation, first_observed_at, now,
        INVOCATION_LOOKUP_WINDOW_MS)

    invocation_status = (raw_invocation or {}).get('Status')
    if raw_invocation is not None and invocation_status not in \
            build_reconciliation.SSM_TERMINAL_STATUSES:
        # A genuinely nonterminal invocation keeps the job nonterminal
        # (Req 2.5): record the observation, decide nothing.
        set_job_fields(build_job_id, {'reconciliation': to_dynamo({
            'command_id': command_id,
            'command_status': invocation_status,
            'first_observed_at': first_observed_at,
            'lookup_state': lookup.state,
            'updated_at': now,
        })})
        return

    if lookup.state == build_reconciliation.LOOKUP_PENDING:
        # InvocationDoesNotExist inside the bounded window is eventual
        # consistency (Req 2.5): persist the retry/settlement state on
        # the job; the scheduled dispatcher tick re-drives it.
        state = {
            'command_id': command_id,
            'command_status': event_status,
            'first_observed_at': first_observed_at,
            'lookup_state': build_reconciliation.LOOKUP_PENDING,
            'updated_at': now,
        }
        if settlement_deadline_ms is not None:
            state['settlement_deadline'] = settlement_deadline_ms
        set_job_fields(build_job_id,
                       {'reconciliation': to_dynamo(state)})
        return

    # Settled evidence: the retrieved final invocation, or — only after
    # the bounded window is exhausted — the event's own terminal status
    # with every invocation field identified as unavailable (Req 2.2:
    # unavailable is identified, never fabricated).
    if lookup.state == build_reconciliation.LOOKUP_RETRIEVED:
        evidence: Optional[Dict[str, Any]] = raw_invocation
    else:
        evidence = {'Status': event_status} if event_status else None

    terminal_status = (evidence or {}).get('Status')
    if terminal_status == SSM_STATUS_SUCCESS \
            and settlement_deadline_ms is None:
        # Success without a terminal agent result waits out the
        # settlement window; only after it may AGENT_RESULT_MISSING be
        # classified (Req 2.4, 2.5 — the dispatcher settles it).
        settlement_deadline_ms = build_reconciliation.settlement_deadline(
            now, SETTLEMENT_WINDOW_MS)

    classification = build_reconciliation.classify_attempt(
        current_status=job_status,
        invocation=evidence,
        settlement_deadline_ms=settlement_deadline_ms,
        now=now)

    incoming = build_reconciliation.build_execution_diagnostic(
        attempt={'attempt_id': attempt.get('attempt_id'),
                 'command_id': command_id,
                 'instance_id': instance_id},
        invocation=raw_invocation,
        classification=classification.error_code,
        source=source,
        observed_at=now,
        disk=(job.get('preflight') or {}).get('disk'))

    application = build_reconciliation.apply_evidence(
        job_status, job.get('execution_diagnostic'), incoming,
        classification, now)

    recon_state = {
        'command_id': command_id,
        'command_status': terminal_status or event_status,
        'first_observed_at': first_observed_at,
        'lookup_state': lookup.state,
        'updated_at': now,
    }
    if settlement_deadline_ms is not None:
        recon_state['settlement_deadline'] = settlement_deadline_ms

    # Terminal transitions apply exactly where the previous fallback
    # applied them — jobs the agent was running (building/publishing);
    # genuinely in-progress phases and terminal jobs only gain
    # diagnostic completeness (preservation Req 3.1, terminal
    # absorption Req 2.6).
    if application.update_status is not None \
            and job_status in AGENT_RUNNING_STATUSES:
        # Terminal finalization (task 6.1): the ONE conditional write
        # carries status, error, ended_at, the sanitized evidence
        # digest, and the terminal-effects ledger under its stable
        # effect ID. A terminal invocation means the command's shell
        # exited on the server, so a dedicated slot needs no separate
        # stop verification here (cleanup not applicable, preserving
        # same-event release/promotion); an ephemeral runner's
        # idempotent termination stays a pending retryable effect.
        ledger = plan_job_ledger(
            job,
            cleanup_required=(job.get('execution_mode') ==
                              build_domain.EXECUTION_MODE_EPHEMERAL))
        extra: Dict[str, Any] = {
            'ended_at': application.update_ended_at,
            'reconciliation': to_dynamo(recon_state),
            'terminal_effects': ledger,
            'evidence_digest': evidence_digest(
                application.update_diagnostic or incoming),
        }
        if application.update_diagnostic is not None:
            extra['execution_diagnostic'] = to_dynamo(
                application.update_diagnostic)
        if application.update_status == build_domain.STATUS_FAILED:
            extra['error'] = {
                'code': (application.update_error_code
                         or ERROR_AGENT_COMMAND_FAILED),
                'message': classification_message(
                    classification, terminal_status or event_status),
            }
        if not transition_job(build_job_id, job_status,
                              application.update_status, extra=extra):
            return  # raced: another writer recorded the outcome (Req 2.6)
        if application.update_status == build_domain.STATUS_INTERRUPTED:
            audit_terminal_effect(
                build_job_id, ledger, AUDIT_BUILD_INTERRUPTED, 'failure',
                {'command_id': command_id,
                 'command_status': terminal_status or event_status,
                 'error_code': application.update_error_code,
                 'status_at_interruption': job_status})
        else:
            audit_terminal_effect(
                build_job_id, ledger, AUDIT_BUILD_FAILED, 'failure',
                {'command_id': command_id,
                 'command_status': terminal_status or event_status,
                 'error_code': application.update_error_code,
                 'status_at_failure': job_status})
        release_and_promote(job, ledger)
        logger.info(f"Build_Job {build_job_id}: agent command "
                    f"{command_id} reconciled "
                    f"'{terminal_status or event_status}' -> job "
                    f"'{application.update_status}' "
                    f"({application.update_error_code or 'agent result'})")
        return

    # No terminal transition here: persist increased diagnostic
    # completeness and the settlement/lookup state. Duplicate or
    # non-increasing evidence is a no-op (Req 2.6 idempotence).
    fields: Dict[str, Any] = {}
    if application.update_diagnostic is not None:
        fields['execution_diagnostic'] = to_dynamo(
            application.update_diagnostic)
    state_changed = (
        recon.get('lookup_state') != recon_state['lookup_state']
        or recon.get('command_status') != recon_state['command_status']
        or recon.get('settlement_deadline')
        != recon_state.get('settlement_deadline')
        or recon.get('first_observed_at')
        != recon_state['first_observed_at'])
    if state_changed and not build_domain.is_terminal(job_status):
        fields['reconciliation'] = to_dynamo(recon_state)
    if fields:
        set_job_fields(build_job_id, fields)


def legacy_ssm_fallback(job: Dict[str, Any], command_id: str,
                        status: str) -> None:
    """The original generic fallback, retained BYTE-COMPATIBLY for jobs
    whose instance identity cannot be resolved (no invocation retrieval
    is possible): failed for Failed/TimedOut, interrupted for
    Cancelled (preservation Req 3.1/3.8)."""
    fallback = decide_ssm_fallback(job['status'], status)
    if not fallback.apply:
        return  # agent outcome already recorded / job not running
    # Same task 6.1 terminal-effects contract as the reconciled path:
    # the outcome/status/audit meanings stay byte-compatible; only the
    # additive ledger/digest fields and the audit's effect-identity
    # deduplication are new (preservation Req 3.1/3.8).
    ledger = plan_job_ledger(
        job, cleanup_required=(job.get('execution_mode') ==
                               build_domain.EXECUTION_MODE_EPHEMERAL))
    extra: Dict[str, Any] = {
        'ended_at': now_ms(),
        'terminal_effects': ledger,
        'evidence_digest': evidence_digest(
            {'command_id': command_id, 'command_status': status}),
    }
    if fallback.error:
        extra['error'] = fallback.error
    if not transition_job(job['build_job_id'], job['status'],
                          fallback.next_status, extra=extra):
        return  # raced: another writer recorded the outcome (Req 4.1)
    if fallback.next_status == build_domain.STATUS_INTERRUPTED:
        audit_terminal_effect(
            job['build_job_id'], ledger, AUDIT_BUILD_INTERRUPTED,
            'failure',
            {'command_id': command_id, 'command_status': status,
             'status_at_interruption': job['status']})
    else:
        audit_terminal_effect(
            job['build_job_id'], ledger, AUDIT_BUILD_FAILED, 'failure',
            {'command_id': command_id, 'command_status': status,
             'error_code': (fallback.error or {}).get('code'),
             'status_at_failure': job['status']})
    release_and_promote(job, ledger)
    logger.info(f"Build_Job {job['build_job_id']}: agent command "
                f"{command_id} ended '{status}' -> job "
                f"'{fallback.next_status}' (fallback)")


def handle_ssm_command_status(detail: Dict[str, Any]) -> None:
    """SSM command status change to a terminal status for a job's agent
    command (task 5.1): every terminal status — Success included — is
    reconciled through final invocation evidence via
    ``reconcile_command_evidence`` (Req 2.1, 2.4). Jobs without a
    resolvable instance identity keep the original generic fallback."""
    command_id = detail.get('command-id')
    status = detail.get('status')
    if not command_id or status not in SSM_TERMINAL_EVENT_STATUSES:
        return
    job = find_job_by_command(command_id)
    if job is None:
        return

    attempt, instance_id = resolve_command_identity(
        job, detail.get('instance-id'))
    if not instance_id:
        # Legacy record: no invocation retrieval is possible. Success
        # cannot be acted on without evidence; failure statuses keep the
        # original byte-compatible fallback.
        if status in SSM_FAILURE_STATUSES:
            legacy_ssm_fallback(job, command_id, status)
        return
    reconcile_command_evidence(job, command_id, status, instance_id,
                               EVIDENCE_SOURCE_EVENTBRIDGE)


# =========================================================================
# Lambda entry point
# =========================================================================

def handler(event: Dict, context: Any) -> Dict:
    """Lambda entry point: route one EventBridge event by source and
    detail-type (design §4). Unrecognized events are logged and ignored
    so a rule misconfiguration cannot poison the event queue."""
    source = event.get('source')
    detail_type = event.get('detail-type')
    detail = event.get('detail') or {}
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Unparseable event detail ignored: {detail!r}")
            return {'statusCode': 400}

    try:
        if source == PHASE_EVENT_SOURCE:
            handle_phase_event(detail)
        elif source == EC2_EVENT_SOURCE and \
                detail_type == SPOT_INTERRUPTION_DETAIL_TYPE:
            handle_spot_interruption(detail)
        elif source == EC2_EVENT_SOURCE and \
                detail_type == INSTANCE_STATE_CHANGE_DETAIL_TYPE:
            handle_instance_state_change(detail)
        elif source == SSM_EVENT_SOURCE and \
                detail_type in SSM_COMMAND_DETAIL_TYPES:
            handle_ssm_command_status(detail)
        else:
            logger.info(f"Ignoring event source='{source}' "
                        f"detail-type='{detail_type}'")
        return {'statusCode': 200}
    except Exception as e:
        # EventBridge retries failed deliveries; every transition here is
        # a conditional update, so the retry is safe (Req 4.1).
        logger.exception(f"Build event handling failed: {e}")
        raise
